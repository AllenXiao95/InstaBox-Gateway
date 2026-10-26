"""Single-process SQLite state. Secrets require a separately held passphrase."""
import base64
import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .errors import GatewayError


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, directory, password, *, create=False):
        self.directory = Path(directory).expanduser().resolve()
        if any((parent / ".git").exists() for parent in (self.directory, *self.directory.parents)):
            raise GatewayError("store_must_be_outside_repository")
        if not self.directory.exists():
            if not create:
                raise GatewayError("store_not_initialized")
            self.directory.mkdir(mode=0o700, parents=True)
        path = self.directory / "gateway.sqlite3"
        existed = path.exists()
        if create and existed:
            raise GatewayError("store_already_exists", 409)
        if not existed and not create:
            raise GatewayError("store_not_initialized")
        if create and len(password) < 12:
            raise GatewayError("passphrase_requires_12_characters")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        os.chmod(path, 0o600)
        self.db.execute("CREATE TABLE IF NOT EXISTS state (kind TEXT, id TEXT, value TEXT, PRIMARY KEY(kind,id))")
        salt = self.get("meta", "salt")
        if salt is None:
            salt = base64.b64encode(secrets.token_bytes(16)).decode()
            self.put("meta", "salt", salt)
        self._cipher = AESGCM(Scrypt(salt=base64.b64decode(salt), length=32,
                                    n=2**15, r=8, p=1).derive(password.encode()))
        try:
            if existed:
                self.decrypt(self.get("secret", "check"), "check")
            else:
                self.put("secret", "check", self.encrypt("igw-v1", "check"))
                for role in ("admin", "execution"):
                    self.put("secret", role, self.encrypt(secrets.token_urlsafe(32), role))
                self.put("settings", "main", {"fileRoot": "/data/home/igw-workspaces", "allowResume": False})
        except (InvalidTag, ValueError, TypeError):
            self.db.close()
            raise GatewayError("unlock_failed", 401) from None

    def encrypt(self, value, context):
        nonce = secrets.token_bytes(12)
        return base64.b64encode(nonce + self._cipher.encrypt(nonce, value.encode(), context.encode())).decode()

    def decrypt(self, value, context):
        raw = base64.b64decode(value)
        return self._cipher.decrypt(raw[:12], raw[12:], context.encode()).decode()

    def token(self, role):
        return self.decrypt(self.get("secret", role), role)

    def get(self, kind, ident):
        with self.lock:
            row = self.db.execute("SELECT value FROM state WHERE kind=? AND id=?", (kind, ident)).fetchone()
            return json.loads(row[0]) if row else None

    def rows(self, kind):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute(
                "SELECT value FROM state WHERE kind=? ORDER BY id", (kind,))]

    def put(self, kind, ident, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?,?)", (kind, ident, json.dumps(value)))

    def delete(self, kind, ident):
        with self.lock, self.db:
            self.db.execute("DELETE FROM state WHERE kind=? AND id=?", (kind, ident))

    def recover(self):
        # Never replay accepted writes after process restart.
        for row in self.rows("request"):
            if row["outcome"] == "ACCEPTED":
                row.update(outcome="UNKNOWN", error="interrupted", finishedAt=now())
                self.put("request", row["id"], row)
        for row in self.rows("resume"):
            if row["state"] == "SENDING":
                row["state"] = "UNKNOWN"
                self.put("resume", row["id"], row)

    def close(self):
        self.db.close()
