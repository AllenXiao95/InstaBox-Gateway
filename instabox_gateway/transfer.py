"""Known-length request bodies with bounded reads and incremental checksums."""
import hashlib

from .errors import GatewayError

CHUNK = 64 * 1024


class FileBody:
    def __init__(self, source, length, error="incomplete_upload"):
        self.source, self.length, self.transferred = source, length, 0
        self.error, self.exhausted = error, False
        self.sha256, self.md5 = hashlib.sha256(), hashlib.md5()

    def read(self, size=CHUNK):
        remaining = self.length - self.transferred if self.length is not None else CHUNK
        if not remaining:
            self.exhausted = True
            return b""
        data = self.source.read(min(CHUNK, remaining, size if size > 0 else CHUNK))
        if not data:
            if self.length is not None:
                raise GatewayError(self.error, 502, outcome="UNKNOWN")
            self.exhausted = True
            return b""
        self.transferred += len(data)
        self.sha256.update(data)
        self.md5.update(data)
        return data
