"""Local Google OAuth and selected-file access. Credentials never reach Guests."""
import base64
import hashlib
import hmac
import http.client
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import re
import secrets
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .errors import GatewayError
from .gateway import fields, require, text
from .store import now
from .upstream import NoRedirect, Upstream
from .transfer import CHUNK, FileBody

READ = "https://www.googleapis.com/auth/drive.readonly"
WRITE = "https://www.googleapis.com/auth/drive.file"
FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
FILE_FIELDS = "id,name,mimeType,parents,size,modifiedTime,version,md5Checksum,sha256Checksum,trashed,capabilities(canDownload,canAddChildren),isAppAuthorized"
EXPORTS = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
}


def drive_id(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value), "drive_invalid_id")
    return value


class GoogleHTTP:
    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler(Upstream().proxies), NoRedirect())

    def request(self, url, method="GET", *, token=None, data=None, headers=None, limit=2 * 1024 * 1024, raw=False, stream=False, incomplete=False):
        parsed = urllib.parse.urlsplit(url)
        require(parsed.scheme == "https" and parsed.netloc in ("oauth2.googleapis.com", "www.googleapis.com"), "drive_origin_denied")
        headers = dict(headers or {})
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=60)
            if stream:
                return response, response.headers
            with response:
                body = response.read(limit + 1)
                require(len(body) <= limit, "drive_response_too_large", 502)
                if raw:
                    response.headers["X-IGW-Response-Status"] = str(response.code)
                    return body, response.headers
                result = json.loads(body)
                require(isinstance(result, dict), "drive_invalid_response", 502)
                return result, response.headers
        except urllib.error.HTTPError as exc:
            status = exc.code
            if status == 308 and incomplete:
                with exc:
                    exc.headers["X-IGW-Response-Status"] = "308"
                    return b"", exc.headers
            exc.close()
            code = {401: "drive_reconnect_required", 403: "drive_permission_denied", 404: "drive_file_missing", 429: "drive_rate_limited"}.get(status, "drive_request_failed")
            raise GatewayError(code, 502, outcome="UNKNOWN" if method != "GET" else "FAILED", upstream_status=status) from None
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            raise GatewayError("drive_network_error", 502, outcome="UNKNOWN" if method != "GET" else "FAILED") from None
        except (ValueError, UnicodeError):
            raise GatewayError("drive_invalid_response", 502) from None


class Drive:
    def __init__(self, gateway):
        self.g = gateway
        self.http = GoogleHTTP()
        self.lock = threading.RLock()
        self.pending = None
        self.oauth_status = "idle"
        self.stop_event = threading.Event()
        self.thread = None

    def _secret(self, key):
        value = self.g.store.get("drive_secret", key)
        return json.loads(self.g.store.decrypt(value, "drive." + key)) if value else None

    def _save_secret(self, key, value):
        self.g.store.put("drive_secret", key, self.g.store.encrypt(json.dumps(value), "drive." + key))

    def status(self):
        connection = self._secret("connection")
        return dict(configured=self._secret("config") is not None, connected=bool(connection),
                    connectionId=connection["id"] if connection else None,
                    user=connection.get("user") if connection else None,
                    uploadEnabled=bool(connection and (WRITE in connection["scopes"] or connection.get("uploadTokens"))),
                    oauthStatus=self.oauth_status, maxFileBytes=None)

    def configure(self, body):
        fields(body, ("installed",))
        client = body.get("installed")
        require(isinstance(client, dict), "drive_desktop_client_required")
        client_id = text(client.get("client_id"), 256)
        require(client_id.endswith(".apps.googleusercontent.com"), "drive_desktop_client_required")
        config = dict(client_id=client_id, client_secret=text(client.get("client_secret"), 512))
        with self.lock:
            require(not self.pending, "drive_login_pending", 409)
            require(not self._secret("connection"), "drive_disconnect_first", 409)
            self._save_secret("config", config)
        return self.status()

    def connect(self, body):
        fields(body, ("allowUpload", "folderId"))
        require(type(body.get("allowUpload", False)) is bool)
        with self.lock:
            config = self._secret("config")
            require(config, "drive_config_required", 409)
            require(not self.pending, "drive_login_pending", 409)
            connection = self._secret("connection")
            folder = body.get("folderId")
            if folder:
                drive_id(folder)
                require(connection, "drive_reconnect_required", 409)
                metadata = self.metadata(folder, connection["id"])
                require(metadata["mimeType"] == FOLDER and metadata.get("capabilities", {}).get("canAddChildren"), "drive_folder_not_writable", 403)
            verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
            scopes = [WRITE] if folder else [READ, WRITE] if body.get("allowUpload") else [READ]
            class Callback(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_GET(self):
                    self.connection.settimeout(5)
                    url = urllib.parse.urlsplit(self.path)
                    query = urllib.parse.parse_qs(url.query)
                    valid = (url.path == "/" and self.headers.get("Host") == "127.0.0.1:" + str(self.server.server_port)
                             and len(query.get("state", [])) == 1
                             and hmac.compare_digest(query["state"][0].encode(), state.encode()))
                    if not valid:
                        self.send_error(400)
                        return
                    self.server.callback = query
                    page = b"<!doctype html><meta name=referrer content=no-referrer><title>InstaBox Gateway</title><p>Authorization received. You can close this tab and return to InstaBox Gateway.</p>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(page)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Security-Policy", "default-src 'none'")
                    self.end_headers()
                    self.wfile.write(page)

            server = HTTPServer(("127.0.0.1", 0), Callback)
            server.timeout = 1
            server.callback = None
            redirect = "http://127.0.0.1:" + str(server.server_port)
            attempt = dict(server=server, config=config, verifier=verifier, state=state, redirect=redirect, scopes=scopes,
                           folder=folder, connectionId=connection["id"] if connection else None)
            self.pending, self.oauth_status = attempt, "waiting"
            threading.Thread(target=self._authorize, args=(attempt,), name="igw-google-oauth", daemon=True).start()
            parameters = dict(
                client_id=config["client_id"], redirect_uri=redirect, response_type="code", scope=" ".join(scopes),
                access_type="offline", prompt="consent select_account", state=state,
                code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(), code_challenge_method="S256")
            if folder:
                parameters.update(prompt="consent", trigger_onepick="true", allow_folder_selection="true", allow_multiple="false",
                                  mimetypes=FOLDER, file_ids=folder, include_granted_scopes="false")
            url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(parameters)
            return dict(authorizationUrl=url, expiresIn=300)

    def _authorize(self, attempt):
        server = attempt["server"]
        try:
            deadline = time.monotonic() + 300
            while self.pending is attempt and not server.callback and time.monotonic() < deadline and not self.stop_event.is_set():
                server.handle_request()
            if self.pending is not attempt:
                return
            query = server.callback or {}
            require("error" not in query, "drive_consent_denied", 409)
            require(len(query.get("code", [])) == 1, "drive_login_expired", 409)
            tokens = self._token(dict(**attempt["config"], code=query["code"][0], redirect_uri=attempt["redirect"],
                                      code_verifier=attempt["verifier"], grant_type="authorization_code"))
            scopes = tokens.get("scope", " ".join(attempt["scopes"])).split()
            require((WRITE if attempt["folder"] else READ) in scopes, "drive_read_scope_required", 409)
            user, _ = self.http.request("https://www.googleapis.com/drive/v3/about?fields=user", token=tokens["access_token"])
            require(isinstance(user.get("user"), dict) and user["user"].get("permissionId"), "drive_invalid_response", 502)
            with self.lock:
                if self.pending is attempt:
                    if attempt["folder"]:
                        connection = self._secret("connection")
                        require(connection and connection["id"] == attempt["connectionId"], "drive_connection_changed", 409)
                        require(user["user"]["permissionId"] == connection["user"]["permissionId"], "drive_google_account_mismatch", 409)
                        require(query.get("picked_file_ids") == [attempt["folder"]], "drive_folder_selection_mismatch", 409)
                        connection["uploadTokens"] = tokens
                        self._save_secret("connection", connection)
                        self.select(dict(fileId=attempt["folder"], access="upload"))
                    else:
                        self._save_secret("connection", dict(id="gc_" + secrets.token_hex(12), user=user["user"], scopes=scopes, **tokens))
                    self.oauth_status = "connected"
        except GatewayError as exc:
            if self.pending is attempt:
                self.oauth_status = exc.code
        except Exception:
            if self.pending is attempt:
                self.oauth_status = "drive_authorization_failed"
        finally:
            server.server_close()
            with self.lock:
                if self.pending is attempt:
                    self.pending = None

    def _token(self, body):
        value, _ = self.http.request("https://oauth2.googleapis.com/token", "POST", data=urllib.parse.urlencode(body).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        require(isinstance(value.get("access_token"), str) and type(value.get("expires_in")) is int, "drive_invalid_response", 502)
        return {k: v for k, v in {**value, "expiresAt": time.time() + value["expires_in"]}.items()
                if k in ("access_token", "refresh_token", "scope", "expiresAt")}

    def disconnect(self):
        with self.lock:
            self.pending = None
            self.g.store.delete("drive_secret", "connection")
            self.oauth_status = "idle"
        return self.status()

    def request(self, path, *, query=None, connection_id=None, write=False, **kwargs):
        require(not self.stop_event.is_set(), "gateway_interrupted", 409)
        with self.lock:
            connection = self._secret("connection")
            require(connection, "drive_reconnect_required", 409)
            require(connection_id is None or connection_id == connection["id"], "drive_connection_changed", 409)
            credential = connection.get("uploadTokens", connection) if write else connection
            if write:
                require(WRITE in (credential.get("scope", "").split() if credential is not connection else connection["scopes"]), "drive_upload_scope_required", 403)
            if credential["expiresAt"] < time.time() + 30:
                require(credential.get("refresh_token"), "drive_reconnect_required", 409)
                tokens = self._token(dict(**self._secret("config"), refresh_token=credential["refresh_token"], grant_type="refresh_token"))
                credential.update(tokens)
                self._save_secret("connection", connection)
            token = credential["access_token"]
        url = "https://www.googleapis.com/" + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return self.http.request(url, token=token, **kwargs)

    def metadata(self, ident, connection_id=None):
        value, _ = self.request("drive/v3/files/" + drive_id(ident), query=dict(fields=FILE_FIELDS, supportsAllDrives="true"), connection_id=connection_id)
        require(not value.get("trashed") and value.get("id") == ident and isinstance(value.get("name"), str)
                and isinstance(value.get("mimeType"), str), "drive_invalid_file", 502)
        return value

    def files(self, parent="root", page=None, shared=False, connection_id=None):
        query = dict(q="trashed = false and " + ("sharedWithMe = true" if shared else "'" + drive_id(parent) + "' in parents"),
                     fields="nextPageToken,incompleteSearch,files(" + FILE_FIELDS + ")", pageSize=100,
                     orderBy="folder,name", supportsAllDrives="true", includeItemsFromAllDrives="true")
        if page:
            query["pageToken"] = text(page, 4096)
        value, _ = self.request("drive/v3/files", query=query, connection_id=connection_id)
        require(isinstance(value.get("files"), list) and not value.get("incompleteSearch"), "drive_incomplete_listing", 502)
        return value

    def selections(self):
        connection = self._secret("connection")
        return [s for s in self.g.store.rows("drive_selection") if connection and s["connectionId"] == connection["id"]]

    def select(self, body):
        fields(body, ("fileId", "access"))
        access = body.get("access", "read")
        require(access in ("read", "upload"))
        connection = self._secret("connection")
        require(connection, "drive_reconnect_required", 409)
        metadata = self.metadata(body.get("fileId"), connection["id"])
        require(metadata["mimeType"] != SHORTCUT, "drive_shortcut_unsupported")
        if access == "upload":
            require(WRITE in connection["scopes"] or connection.get("uploadTokens"), "drive_upload_scope_required", 403)
            require(metadata["mimeType"] == FOLDER and metadata.get("capabilities", {}).get("canAddChildren"), "drive_folder_not_writable", 403)
            require(metadata.get("isAppAuthorized") is True, "drive_folder_authorization_required", 403)
        row = dict(id="gs_" + secrets.token_hex(12), connectionId=connection["id"], fileId=metadata["id"],
                   name=metadata["name"], mimeType=metadata["mimeType"], access=access, createdAt=now())
        self.g.store.put("drive_selection", row["id"], row)
        return row

    def selection(self, ident, access="read"):
        value = self.g.store.get("drive_selection", text(ident, 100))
        connection = self._secret("connection")
        require(value and connection and value["connectionId"] == connection["id"], "drive_selection_missing", 404)
        require(value["access"] == access, "drive_selection_denied", 403)
        return value

    def selected_file(self, selection, ident):
        first = self.metadata(ident, selection["connectionId"])
        require(first["mimeType"] != SHORTCUT, "drive_shortcut_unsupported")
        node, seen = first, set()
        for _ in range(128):
            if node["id"] == selection["fileId"]:
                return first
            require(selection["mimeType"] == FOLDER and node["id"] not in seen and len(node.get("parents", [])) == 1, "drive_selection_denied", 403)
            seen.add(node["id"])
            node = self.metadata(node["parents"][0], selection["connectionId"])
        raise GatewayError("drive_selection_denied", 403)

    @contextmanager
    def content(self, selection_id, ident, *, partial=False):
        selection = self.selection(selection_id)
        before = self.selected_file(selection, ident)
        require(before["mimeType"] != FOLDER and before.get("capabilities", {}).get("canDownload") is True, "drive_download_denied", 403)
        mime, extension = EXPORTS.get(before["mimeType"], (before["mimeType"], ""))
        require(not before["mimeType"].startswith("application/vnd.google-apps.") or bool(extension), "drive_format_unsupported", 415)
        path = "drive/v3/files/" + ident + ("/export" if extension else "")
        query = {"mimeType": mime} if extension else {"alt": "media", "supportsAllDrives": "true"}
        response, headers = self.request(path, query=query, stream=True, connection_id=selection["connectionId"])
        try:
            with response:
                length = headers.get("Content-Length")
                require(length is None or length.isdecimal(), "drive_invalid_response", 502)
                length = int(length) if length is not None else None
                metadata = dict(file=before, name=before["name"] + extension, mimeType=mime, bytes=length)
                reader = FileBody(response, length, "incomplete_download")
                yield metadata, reader
                complete = reader.exhausted or length is not None and reader.transferred == length
                require(partial or complete, "incomplete_download", 502)
                after = self.selected_file(selection, ident)
                require(before.get("version") == after.get("version") and before.get("modifiedTime") == after.get("modifiedTime"), "drive_source_changed", 409)
                if complete and not extension:
                    require("size" not in before or reader.transferred == int(before["size"]), "drive_content_mismatch", 502)
                    for algorithm in ("md5", "sha256"):
                        checksum = before.get(algorithm + "Checksum")
                        require(not checksum or getattr(reader, algorithm).hexdigest() == checksum, "drive_content_mismatch", 502)
                if complete:
                    metadata.update(sha256=reader.sha256.hexdigest(), bytes=reader.transferred)
        except (OSError, http.client.HTTPException):
            raise GatewayError("drive_download_interrupted", 502) from None

    @contextmanager
    def stage(self, source, length=None):
        # Private temporary staging permits validation and resumable re-submission
        # without buffering whole files. No arbitrary file-size policy is applied.
        if length is not None:
            require(shutil.disk_usage(self.g.store.directory).free >= length, "drive_no_space", 507)
        reader = source if isinstance(source, FileBody) else FileBody(source, length)
        try:
            with tempfile.TemporaryFile(dir=self.g.store.directory) as staged:
                while True:
                    require(not self.stop_event.is_set(), "gateway_interrupted", 409)
                    chunk = reader.read(CHUNK)
                    if not chunk:
                        break
                    staged.write(chunk)
                staged.seek(0)
                yield staged, reader
        except OSError:
            raise GatewayError("drive_staging_failed", 507) from None

    @staticmethod
    def public_upload(row):
        return {k: v for k, v in row.items() if k != "encryptedSession"}

    def upload(self, selection_id, name, data, key):
        require(isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_-]{8,100}", key), "submission_id_required")
        self.selection(selection_id, "upload")
        name = text(name, 240)
        require(name not in (".", "..") and not any(c in name for c in "/\\\r\n"), "drive_invalid_name")
        source = io.BytesIO(data) if isinstance(data, bytes) else data
        length = len(data) if isinstance(data, bytes) else data.length
        with self.stage(source, length) as (staged, reader):
            digest = reader.sha256.hexdigest()
            with self.lock:
                previous = next((u for u in self.g.store.rows("drive_upload") if u["submissionId"] == key), None)
                if previous:
                    require((previous["selectionId"], previous["name"], previous["sha256"]) == (selection_id, name, digest), "idempotency_conflict", 409)
                    if previous["status"] == "verified" or not previous.get("encryptedSession"):
                        return self.public_upload(previous)
                return self._upload(selection_id, name, staged, reader, key, previous)

    def _upload(self, selection_id, name, staged, reader, key, previous):
        selection = self.selection(selection_id, "upload")
        parent = self.metadata(selection["fileId"], selection["connectionId"])
        require(parent["mimeType"] == FOLDER and parent.get("capabilities", {}).get("canAddChildren"), "drive_folder_not_writable", 403)
        require(parent.get("isAppAuthorized") is True, "drive_folder_authorization_required", 403)
        length = reader.transferred
        if previous:
            row = previous
        else:
            ids, _ = self.request("drive/v3/files/generateIds", query=dict(count=1, space="drive", type="files"), connection_id=selection["connectionId"], write=True)
            require(isinstance(ids.get("ids"), list) and len(ids["ids"]) == 1, "drive_invalid_response", 502)
            row = dict(id="du_" + secrets.token_hex(12), fileId=drive_id(ids["ids"][0]), selectionId=selection_id,
                       connectionId=selection["connectionId"], name=name, bytes=length,
                       sha256=reader.sha256.hexdigest(), md5=reader.md5.hexdigest(), submissionId=key, status="sending", createdAt=now())
            self.g.store.put("drive_upload", row["id"], row)
        try:
            offset, completed = 0, False
            if previous:
                location = self.g.store.decrypt(row["encryptedSession"], "drive.upload." + row["id"])
            else:
                _, headers = self.request("upload/drive/v3/files", query=dict(uploadType="resumable", supportsAllDrives="true"),
                                          method="POST", connection_id=selection["connectionId"], raw=True, write=True,
                                          data=json.dumps(dict(id=row["fileId"], name=name, parents=[parent["id"]])).encode(),
                                          headers={"Content-Type": "application/json", "X-Upload-Content-Type": "application/octet-stream", "X-Upload-Content-Length": str(length)})
                location = headers.get("Location", "")
            url = urllib.parse.urlsplit(location)
            require(url.scheme == "https" and url.netloc == "www.googleapis.com" and url.path == "/upload/drive/v3/files", "drive_invalid_upload_location", 502)
            row["encryptedSession"] = self.g.store.encrypt(location, "drive.upload." + row["id"])
            self.g.store.put("drive_upload", row["id"], row)

            def send(data, content_range):
                return self.request(url.path.lstrip("/"), query=dict(urllib.parse.parse_qsl(url.query)), method="PUT", raw=True,
                                    incomplete=True, connection_id=selection["connectionId"], write=True, data=data,
                                    headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(data)), "Content-Range": content_range})[1]

            def received(headers):
                value = headers.get("Range")
                require(value is None or re.fullmatch(r"bytes=0-[0-9]+", value), "drive_invalid_upload_range", 502)
                return int(value.split("-")[1]) + 1 if value else 0

            if previous:
                headers = send(b"", "bytes */" + str(length))
                completed = headers.get("X-IGW-Response-Status", "200") in ("200", "201")
                offset = length if completed else received(headers)
                require(0 <= offset <= length, "drive_invalid_upload_range", 502)
            sent = False
            while not completed and (offset < length or not sent):
                require(not self.stop_event.is_set(), "gateway_interrupted", 409)
                staged.seek(offset)
                chunk = staged.read(min(1024 * 1024, length - offset))
                content_range = f"bytes {offset}-{offset+len(chunk)-1}/{length}" if length else "bytes */0"
                headers = send(chunk, content_range)
                completed = headers.get("X-IGW-Response-Status", "200") in ("200", "201")
                acknowledged = length if completed else received(headers)
                require(completed or offset < acknowledged <= offset + len(chunk), "drive_invalid_upload_range", 502)
                offset, sent = acknowledged, True
                row.update(uploadedBytes=offset, status="sending")
                self.g.store.put("drive_upload", row["id"], row)
            require(completed, "drive_upload_incomplete", 502)
            return self.verify_upload(row["id"])
        except GatewayError as exc:
            row.update(status="unknown", error=exc.code)
            self.g.store.put("drive_upload", row["id"], row)
            return self.public_upload(row)

    def verify_upload(self, ident):
        row = self.g.store.get("drive_upload", ident)
        require(row, "drive_upload_missing", 404)
        selection = self.selection(row["selectionId"], "upload")
        actual = self.metadata(row["fileId"], row["connectionId"])
        require(actual.get("parents") == [selection["fileId"]] and int(actual.get("size", "-1")) == row["bytes"]
                and actual.get("md5Checksum") == row["md5"], "drive_upload_verification_failed", 502)
        row.update(status="verified", uploadedBytes=row["bytes"], finishedAt=now())
        row.pop("error", None)
        row.pop("encryptedSession", None)
        self.g.store.put("drive_upload", ident, row)
        return self.public_upload(row)

    def start_monitor(self):
        for row in self.g.store.rows("drive_upload"):
            if row["status"] == "sending":
                row.update(status="unknown", error="gateway_interrupted")
                self.g.store.put("drive_upload", row["id"], row)
        for row in self.g.store.rows("drive_import"):
            if row["status"] == "running":
                row.update(status="blocked", error="gateway_interrupted")
                self.g.store.put("drive_import", row["id"], row)
        self.thread = threading.Thread(target=self._monitor, name="igw-drive-import", daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        self.pending = None
        if self.thread:
            self.thread.join(timeout=90)

    def submit(self, body, key):
        fields(body, ("selectionId", "workspaceId"))
        require(isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_-]{8,100}", key), "submission_id_required")
        with self.lock:
            for previous in self.g.store.rows("drive_import"):
                if previous["submissionId"] == key:
                    require(all(previous[k] == body.get(k) for k in ("selectionId", "workspaceId")), "idempotency_conflict", 409)
                    return previous
            selection = self.selection(body.get("selectionId"))
            workspace = self.g.workspaces.get(body.get("workspaceId"))
            self.g.workspaces.probe(workspace)
            ident = "t_" + secrets.token_hex(12)
            binding = self.g.store.get("binding", workspace["bindingId"])
            row = dict(id=ident, kind="drive_import", selectionId=selection["id"], connectionId=selection["connectionId"],
                       workspaceId=workspace["id"], bindingId=workspace["bindingId"], machineId=binding["machineId"],
                       accountId=binding["accountId"], destination="inputs/google-drive/" + ident, submissionId=key,
                       status="pending", verification="not_requested", createdAt=now(), files=[], planned=False)
            self.g.store.put("drive_import", ident, row)
            return row

    def control(self, ident, action):
        with self.lock:
            row = self.g.store.get("drive_import", ident)
            require(row, "task_not_found", 404)
            if action == "cancel":
                row["cancelRequested"] = True
                if row["status"] == "pending":
                    row["status"] = "cancelled"
            else:
                require(row["status"] in ("failed", "blocked", "cancelled"), "task_still_running", 409)
                self.selection(row["selectionId"])
                self.g.workspaces.get(row["workspaceId"])
                row.update(status="pending", cancelRequested=False)
                row.pop("error", None)
                self.g.store.delete("task_event", ident)
            self.g.store.put("drive_import", ident, row)
            return row

    def _monitor(self):
        while not self.stop_event.is_set():
            for row in self.g.store.rows("drive_import"):
                if self.stop_event.is_set():
                    return
                if row["status"] == "pending":
                    self._run(row)
                elif row["status"] in ("succeeded", "failed", "blocked", "cancelled"):
                    self.g.tasks.notify(row)
            self.stop_event.wait(1)

    def _checkpoint(self, row):
        with self.lock:
            current = self.g.store.get("drive_import", row["id"])
            require(not current.get("cancelRequested"), "drive_import_cancelled", 409)
            require(not self.stop_event.is_set(), "gateway_interrupted", 409)
            self.selection(row["selectionId"])
            row["cancelRequested"] = current.get("cancelRequested", False)
            self.g.store.put("drive_import", row["id"], row)

    def _run(self, row):
        try:
            row.update(status="running", verification="not_requested")
            self._checkpoint(row)
            selection = self.selection(row["selectionId"])
            workspace = self.g.workspaces.get(row["workspaceId"])
            require(workspace["bindingId"] == row["bindingId"], "workspace_target_conflict", 409)
            if not row["planned"]:
                pending = [(selection["fileId"], "")]
                found, directories, seen = [], [], set()
                while pending:
                    self._checkpoint(row)
                    ident, prefix = pending.pop()
                    require(ident not in seen and len(seen) < 10000, "drive_tree_limit", 409)
                    seen.add(ident)
                    metadata = self.selected_file(selection, ident)
                    safe_name = re.sub(r"[^\w. -]", "_", metadata["name"], flags=re.UNICODE).strip(" .")[:64] or "file"
                    segment = safe_name + "--" + ident
                    if metadata["mimeType"] == FOLDER:
                        directories.append(prefix + segment)
                        page = None
                        while True:
                            listing = self.files(ident, page, connection_id=row["connectionId"])
                            pending.extend((drive_id(f["id"]), prefix + segment + "/") for f in listing["files"])
                            require(len(pending) + len(seen) <= 10000, "drive_tree_limit", 409)
                            page = listing.get("nextPageToken")
                            if not page:
                                break
                    else:
                        extension = EXPORTS.get(metadata["mimeType"], ("", ""))[1]
                        found.append(dict(fileId=ident, path=prefix + segment + extension, status="pending", source=metadata))
                row.update(files=found, directories=directories, planned=True)
                self._checkpoint(row)
            destination = row["destination"]
            # Exclusive job directory and ownership marker allow safe reconciliation.
            target = {"bindingId": row["bindingId"]}
            script = """import json,os,pathlib,sys
p=pathlib.Path(sys.argv[1]); owner=sys.argv[2]
for d in (p.parent.parent,p.parent):
 assert d.resolve()==d
 d.mkdir(exist_ok=True)
assert p.resolve()==p
if p.exists():
 assert (p/'.owner').read_text()==owner
else:
 p.mkdir(); (p/'.owner').write_text(owner)
for relative in json.load(sys.stdin):
 d=p/relative
 assert d.resolve()==d
 d.mkdir(parents=True,exist_ok=True)
"""
            result = self.g.workspaces.execute(workspace["id"], dict(argv=["python3", "-c", script, workspace["remoteRoot"] + "/" + destination, row["id"]], stdin=json.dumps(row.get("directories", []))))
            require(result["outcome"] == "SUCCEEDED", "drive_destination_conflict", 409)
            for entry in row["files"]:
                self._checkpoint(row)
                relative = destination + "/" + entry["path"]
                target, absolute = self.g.workspaces.file_path(workspace["id"], relative)
                require(absolute == workspace["remoteRoot"] + "/" + relative, "drive_destination_conflict", 409)
                # Existing files must match recorded evidence; never overwrite them.
                inspect = """import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1]); part=p.with_name(p.name+'.part')
def sha(path):
 if not path.is_file() or path.is_symlink(): return None
 h=hashlib.sha256()
 with path.open('rb') as f:
  while True:
   b=f.read(1048576)
   if not b: break
   h.update(b)
 return h.hexdigest()
print(json.dumps(dict(exists=p.exists(),sha256=sha(p),partExists=part.exists(),partSha256=sha(part))))
"""
                result = self.g.workspaces.execute(workspace["id"], dict(argv=["python3", "-c", inspect, absolute], timeoutSeconds=120))
                require(result["outcome"] == "SUCCEEDED" and not result["result"]["truncated"], "drive_destination_check_failed", 502)
                existing = json.loads(result["result"]["stdout"])
                if existing["exists"]:
                    require(entry.get("sha256") and entry["sha256"] == existing["sha256"], "drive_destination_conflict", 409)
                    entry["status"] = "verified"
                    continue
                require(not existing["partExists"] or entry.get("sha256"), "drive_destination_conflict", 409)
                part = absolute + ".part"
                _, checked_part = self.g.workspaces.file_path(workspace["id"], relative + ".part")
                require(checked_part == part, "drive_destination_conflict", 409)
                if not existing["partExists"] or existing["partSha256"] != entry.get("sha256"):
                    with tempfile.TemporaryFile(dir=self.g.store.directory) as staged:
                        with self.content(row["selectionId"], entry["fileId"]) as (metadata, reader):
                            require(metadata["file"].get("version") == entry["source"].get("version"), "drive_source_changed", 409)
                            if metadata["bytes"] is not None:
                                require(shutil.disk_usage(self.g.store.directory).free >= metadata["bytes"], "drive_no_space", 507)
                            while True:
                                require(not self.stop_event.is_set(), "gateway_interrupted", 409)
                                chunk = reader.read(CHUNK)
                                if not chunk:
                                    break
                                staged.write(chunk)
                        length = staged.tell()
                        entry.update(sha256=metadata["sha256"], bytes=length, mimeType=metadata["mimeType"], status="sending")
                        self._checkpoint(row)
                        space = self.g.workspaces.execute(workspace["id"], dict(argv=["python3", "-c", "import pathlib,shutil,sys; assert shutil.disk_usage(pathlib.Path(sys.argv[1]).parent).free>=int(sys.argv[2])", part, str(length)]))
                        require(space["outcome"] == "SUCCEEDED", "drive_no_space", 507)
                        staged.seek(0)
                        self.g.upload(target, part, staged, length)
                publish = """import hashlib,os,pathlib,sys
p=pathlib.Path(sys.argv[1]); part=p.with_name(p.name+'.part')
assert not part.is_symlink() and not p.exists()
h=hashlib.sha256()
with part.open('rb') as f:
 while True:
  b=f.read(1048576)
  if not b: break
  h.update(b)
assert h.hexdigest()==sys.argv[2]
os.link(part,p); part.unlink()
"""
                result = self.g.workspaces.execute(workspace["id"], dict(argv=["python3", "-c", publish, absolute, entry["sha256"]], timeoutSeconds=120))
                require(result["outcome"] == "SUCCEEDED", "drive_publish_failed", 502)
                entry["status"] = "verified"
                self._checkpoint(row)
            manifest = json.dumps(dict(id=row["id"], selectionId=row["selectionId"], files=row["files"]), ensure_ascii=True).encode()
            target, absolute = self.g.workspaces.file_path(workspace["id"], destination + "/manifest.json")
            require(absolute == workspace["remoteRoot"] + "/" + destination + "/manifest.json", "drive_destination_conflict", 409)
            self.g.file(target, absolute, manifest)
            row.update(status="succeeded", verification="verified", finishedAt=now())
        except GatewayError as exc:
            row.update(status="cancelled" if exc.code == "drive_import_cancelled" else "blocked" if exc.code == "gateway_interrupted" else "failed", error=exc.code, finishedAt=now())
        except Exception:
            row.update(status="failed", error="drive_import_failed", finishedAt=now())
        self.g.store.put("drive_import", row["id"], row)
        self.g.tasks.notify(row)
