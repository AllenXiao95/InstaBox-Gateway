"""Owner-only local bootstrap, short-lived browser tickets and execution pairing."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import tempfile
import time

from .errors import GatewayError


def private_json(path, value):
    path = Path(path)
    if any((parent / ".git").exists() for parent in (path.parent, *path.parent.parents)):
        raise GatewayError("credentials_must_be_outside_repository")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".igw-")
    try:
        os.chmod(name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_private(path):
    path = Path(path)
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise GatewayError("credential_file_permissions")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise GatewayError("credential_file_invalid")
    return value


def local_request(directory, action):
    """Only the OS owner can read this per-service bootstrap credential."""
    record = read_private(Path(directory) / "local-access.json")
    from .runtime import read_instance
    if read_instance(directory) != {k: record.get(k) for k in ("instanceId", "pid", "port")}:
        raise GatewayError("service_instance_mismatch")
    if not isinstance(record.get("secret"), str) or not record["secret"].isascii() or not 1 <= len(record["secret"]) <= 256:
        raise GatewayError("credential_file_invalid")
    conn = http.client.HTTPConnection("127.0.0.1", record["port"], timeout=5)
    try:
        conn.request("POST", "/api/auth/" + action, "{}", {
            "Authorization": "Local " + record["secret"], "Content-Type": "application/json"})
        response = conn.getresponse()
        value = json.loads(response.read(65536))
        if response.status != 200 or value.get("instanceId") != record["instanceId"]:
            raise GatewayError("local_pairing_rejected")
        return record, value
    except (OSError, ValueError, http.client.HTTPException):
        raise GatewayError("local_service_unavailable") from None
    finally:
        conn.close()


class BrowserAuth:
    def __init__(self):
        self.bootstrap = secrets.token_urlsafe(32)
        self.tickets = {}
        self.sessions = {}

    def ticket(self):
        self.tickets = {k: v for k, v in self.tickets.items() if v > time.monotonic()}
        value = secrets.token_urlsafe(32)
        self.tickets[hashlib.sha256(value.encode()).hexdigest()] = time.monotonic() + 60
        return value

    def session(self, ticket=None):
        if ticket is not None:
            expiry = self.tickets.pop(hashlib.sha256(ticket.encode()).hexdigest(), 0)
            if expiry < time.monotonic():
                raise GatewayError("login_ticket_expired", 401)
        value, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.sessions[value] = {"csrf": csrf, "expires": time.monotonic() + 8 * 3600}
        return value, csrf
