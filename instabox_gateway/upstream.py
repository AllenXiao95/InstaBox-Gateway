"""Fixed HTTPS origin; only an explicit local proxy, no redirects or retries."""
import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .errors import GatewayError

MAX_FILE = 8 * 1024 * 1024
MAX_JSON = 16 * 1024 * 1024  # Two escaped 1 MiB output streams fit.
SAFE_CODES = frozenset("missing_key malformed_key invalid_key expired_key revoked_key unauthenticated permission_denied missing_scope insufficient_scope invite_required not_found bad_request quota_exceeded rate_limited host_unavailable no_capacity machine_not_running operation_conflict missing_parent not_a_file no_space too_large".split())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect_refused", headers, fp)


class Upstream:
    def __init__(self):
        # Opt-in only. HTTPS_PROXY/HTTP_PROXY remain ignored so unrelated shell
        # settings cannot silently reroute credentials. CONNECT keeps TLS end to end.
        proxy = os.environ.get("IGW_UPSTREAM_PROXY", "")
        if proxy:
            try:
                parsed = urllib.parse.urlsplit(proxy)
                valid = (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
                         and parsed.port is not None and 1 <= parsed.port <= 65535
                         and not parsed.username and not parsed.password
                         and parsed.path in ("", "/") and not parsed.query and not parsed.fragment)
            except ValueError:
                valid = False
            if not valid:
                raise GatewayError("invalid_upstream_proxy")
        self.proxies = {"https": proxy} if proxy else {}

    def request(self, key, method, path, *, query=None, body=None, binary=False, idempotency=None, timeout=25):
        headers = {"X-API-Key": key, "Accept": "application/octet-stream" if binary else "application/json"}
        data = None
        if isinstance(body, bytes):
            data = body
            headers["Content-Type"] = "application/octet-stream"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if idempotency:
            headers["Idempotency-Key"] = idempotency
        url = "https://api.instabox.sh" + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(self.proxies), NoRedirect())
        uncertain = "UNKNOWN" if method in ("POST", "PUT") else "FAILED"
        try:
            try:
                response = opener.open(req, timeout=timeout)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                status = response.code
                limit = MAX_FILE if binary and status == 200 else MAX_JSON
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise GatewayError("upstream_response_too_large", 502, outcome=uncertain)
                if 300 <= status < 400:
                    raise GatewayError("upstream_redirect_refused", 502, outcome=uncertain)
                if binary and status == 200:
                    return status, raw
                try:
                    result = json.loads(raw)
                    if not isinstance(result, dict):
                        raise ValueError()
                except (ValueError, UnicodeError):
                    raise GatewayError("upstream_invalid_response", 502, outcome=uncertain) from None
                if status not in (200, 201, 202):
                    error = result.get("error", {})
                    code = error.get("code") if isinstance(error, dict) else None
                    # Error text can contain credentials or command content. Never relay it.
                    code = code if code in SAFE_CODES else "upstream_rejected"
                    outcome = "UNKNOWN" if method in ("POST", "PUT") and status >= 500 else "FAILED"
                    raise GatewayError(code, status if 400 <= status <= 599 else 502,
                                       outcome=outcome, upstream_status=status)
                return status, result
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            raise GatewayError("upstream_network_error", 502, outcome=uncertain) from None
