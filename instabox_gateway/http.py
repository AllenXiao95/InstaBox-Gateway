import hmac
import json
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit

from .errors import GatewayError
from .gateway import fields, require
from .upstream import MAX_FILE


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # POSIX TIME_WAIT must not block an immediate same-port restart. Keep the
    # Windows bind policy unchanged; never allow concurrent port sharing.
    allow_reuse_address = os.name != "nt"
    allow_reuse_port = False

    def __init__(self, address, gateway):
        require(address[0] == "127.0.0.1", "loopback_required")
        self.gateway = gateway
        self.instance_id = secrets.token_hex(16)
        super().__init__(address, Handler)

    def handle_error(self, request, client_address):
        # Never send request/response data or tracebacks to default HTTP logs.
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "InstaBoxGateway/0.1"

    def log_message(self, *args):
        pass

    def _send(self, status, data, content_type="application/json", request_id=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if request_id:
            self.send_header("X-Gateway-Request-Id", request_id)
        self.end_headers()
        self.wfile.write(data)

    def _body(self, binary=False):
        require(not self.headers.get("Transfer-Encoding"), "transfer_encoding_unsupported")
        raw_length = self.headers.get("Content-Length", "0")
        require(raw_length.isdecimal(), "invalid_content_length")
        require(len(self.headers.get_all("Content-Length", [])) <= 1, "invalid_content_length")
        length = int(raw_length)
        require(length <= (MAX_FILE if binary else 2 * 1024 * 1024), "request_too_large", 413)
        raw = self.rfile.read(length)
        require(len(raw) == length, "incomplete_body")
        if binary:
            require(self.headers.get_content_type() == "application/octet-stream", "content_type_required", 415)
            return raw
        require(self.headers.get_content_type() == "application/json", "content_type_required", 415)
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise GatewayError("invalid_json") from None
        require(isinstance(result, dict))
        return result

    def _auth(self, admin=False):
        authorization = self.headers.get("Authorization", "")
        require(authorization.startswith("Bearer "), "authentication_required", 401)
        token = authorization[7:]
        require(len(token) <= 256 and token.isascii(), "authentication_required", 401)
        store = self.server.gateway.store
        roles = ("admin",) if admin else ("admin", "execution")
        require(any(hmac.compare_digest(token, store.token(role)) for role in roles), "authentication_required", 401)

    def _dispatch(self):
        self.connection.settimeout(15)
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        require(self.headers.get("Host") in hosts, "host_denied", 403)
        origin = self.headers.get("Origin")
        require(origin is None or origin in {"http://" + h for h in hosts}, "origin_denied", 403)
        require(self.headers.get("Sec-Fetch-Site") not in ("cross-site",), "origin_denied", 403)
        parsed = urlsplit(self.path)
        require(not parsed.scheme and not parsed.netloc and not parsed.fragment, "invalid_path")
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)
        require(all(len(v) == 1 for v in query.values()))
        q = {k: v[0] for k, v in query.items()}
        method = self.command
        static = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/logo.svg": ("logo.svg", "image/svg+xml"),
                  "/favicon.png": ("favicon.png", "image/png"),
                  "/favicon.ico": ("favicon.ico", "image/x-icon"),
                  "/i18n.js": ("i18n.js", "text/javascript; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if method == "GET" and path in static:
            name, mime = static[path]
            return self._send(200, files("instabox_gateway").joinpath("web", name).read_bytes(), mime)
        self._auth(admin=path.startswith("/api/admin/"))
        if path == "/api/admin/service" and method == "GET":
            return self._send(200, {"instanceId": self.server.instance_id,
                                    "pid": os.getpid(), "port": port})
        if path == "/api/admin/service/stop" and method == "POST":
            body = self._body()
            fields(body, ("instanceId",))
            require(body.get("instanceId") == self.server.instance_id,
                    "service_instance_mismatch", 409)
            self._send(200, {"stopping": True, "instanceId": self.server.instance_id})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        g = self.server.gateway
        if path == "/api/admin/accounts":
            if method == "GET":
                return self._send(200, {"accounts": g.accounts()})
            if method == "POST":
                return self._send(201, g.save_account(self._body()))
        match = re.fullmatch(r"/api/admin/accounts/(a_[A-Za-z0-9_-]+)(/key)?", path)
        if match:
            ident, key = match.groups()
            if key and method == "PUT":
                return self._send(200, g.save_account(self._body(), ident))
            if not key and method == "PATCH":
                return self._send(200, g.patch_account(ident, self._body()))
            if not key and method == "DELETE":
                return self._send(200, g.remove_account(ident))
        if path == "/api/admin/settings" and method in ("GET", "PATCH"):
            return self._send(200, g.settings(self._body() if method == "PATCH" else None))
        if path == "/api/admin/protections":
            if method == "GET":
                return self._send(200, {"machines": g.store.rows("protection")})
            if method == "PUT":
                body = self._body()
                fields(body, ("machineId", "protected"))
                return self._send(200, g.protect(body.get("machineId"), body.get("protected")))
        if path == "/api/admin/requests" and method == "GET":
            return self._send(200, {"requests": g.requests()})
        if path == "/api/v1/machines" and method == "GET":
            fields(q, ("accountId",))
            return self._send(200, g.inventory(q.get("accountId")))
        if path == "/api/v1/machine" and method == "GET":
            fields(q, ("accountId", "machineId"))
            require(set(q) == {"accountId", "machineId"})
            return self._send(200, g.detail(q["accountId"], q["machineId"]))
        if path == "/api/v1/bindings" and method == "POST":
            return self._send(201, g.select(self._body()))
        if path == "/api/v1/exec" and method == "POST":
            return self._send(200, g.execute(self._body()))
        if path == "/api/v1/file" and method in ("GET", "PUT"):
            fields(q, ("bindingId", "accountId", "machineId", "path"))
            target = {k: v for k, v in q.items() if k != "path"}
            data = self._body(binary=True) if method == "PUT" else None
            status, result, request_id = g.file(target, q.get("path"), data)
            return self._send(status, result, "application/json" if method == "PUT" else "application/octet-stream", request_id)
        if path == "/api/v1/resumes" and method == "POST":
            body = self._body()
            fields(body, ("target", "allowWake"))
            return self._send(202, g.resume(body.get("target"), self.headers.get("Idempotency-Key"), body.get("allowWake")))
        match = re.fullmatch(r"/api/v1/resumes/([A-Za-z0-9_-]{8,128})", path)
        if match and method == "GET":
            return self._send(200, g.operation(match[1]))
        raise GatewayError("route_not_found", 404)

    def _handle(self):
        try:
            self._dispatch()
        except GatewayError as exc:
            self._send(exc.status, {"error": exc.public()})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            self._send(500, {"error": {"code": "internal_error", "outcome": "UNKNOWN"}})

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _handle
