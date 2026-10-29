"""Owner-only local/remote file browsing and local background downloads."""
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import secrets
import shutil
import stat
import tempfile
import threading
import time

from .errors import GatewayError
from .gateway import fields, require, text
from .store import now


class Files:
    def __init__(self, gateway):
        self.g = gateway
        self.lock = threading.RLock()
        self.workers = {}
        self.closed = False
        for row in self.g.store.rows("file_download"):
            if row["status"] in ("pending", "running"):
                row.update(status="failed", error="download_interrupted", finishedAt=now())
                self.g.store.put("file_download", row["id"], row)

    def local(self, path=None, offset=0):
        require(type(offset) is int and 0 <= offset <= 100000, "invalid_offset")
        try:
            root = Path(text(path)).expanduser().resolve(strict=True) if path else Path.home().resolve()
            require(root.is_dir(), "local_directory_required", 400)
            entries = []
            with os.scandir(root) as items:
                for entry in items:
                    require(len(entries) < 100000, "directory_limit", 413)
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue  # Entries may disappear while a directory is being listed.
                    kind = ("symlink" if stat.S_ISLNK(info.st_mode) else "directory" if stat.S_ISDIR(info.st_mode)
                            else "file" if stat.S_ISREG(info.st_mode) else "other")
                    entries.append(dict(name=entry.name, kind=kind, bytes=info.st_size, modifiedAt=info.st_mtime))
            entries.sort(key=lambda e: (e["kind"] != "directory", e["name"]))
            return dict(path=str(root), parent=str(root.parent), entries=entries[offset:offset + 100],
                        nextOffset=offset + 100 if offset + 100 < len(entries) else None)
        except OSError:
            raise GatewayError("local_directory_unavailable", 403) from None

    def remote_path(self, binding_id, path=".", *, directory=False):
        text(binding_id, 100)
        path = text(path)
        require(path == "." and directory or not path.startswith("/") and posixpath.normpath(path) == path
                and all(p not in ("", ".", "..") for p in path.split("/")), "file_path_denied", 403)
        absolute = "/data" if path == "." else "/data/" + path
        script = """import pathlib,sys
root=pathlib.Path('/data'); p=pathlib.Path(sys.argv[1])
assert p==root or root in p.parents
relative=p.relative_to(root); current=root
for part in relative.parts:
 current=current/part
 assert not current.is_symlink()
assert p.resolve()==root.resolve()/relative
assert p.is_dir() if sys.argv[2]=='directory' else p.is_file()
"""
        result = self.g.execute(dict(target={"bindingId": binding_id}, argv=["python3", "-c", script, absolute,
                                                                             "directory" if directory else "file"]))
        require(result["outcome"] == "SUCCEEDED", "file_path_denied", 403)
        return absolute

    def remote(self, binding_id, path=".", offset=0):
        require(type(offset) is int and 0 <= offset <= 100000, "invalid_offset")
        absolute = self.remote_path(binding_id, path, directory=True)
        return self.g.workspaces._list_files(binding_id, "/data", path, absolute, offset)

    def downloads(self):
        return self.g.store.rows("file_download")

    def start(self, body, submission_id):
        fields(body, ("bindingId", "path", "directory"))
        require(isinstance(submission_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{8,100}", submission_id),
                "submission_id_required")
        for key in ("bindingId", "path", "directory"):
            text(body.get(key))
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        with self.lock:
            require(not self.closed, "service_stopping", 503)
            previous = next((r for r in self.downloads() if r["submissionId"] == submission_id), None)
            if previous:
                require(previous["submissionHash"] == digest, "idempotency_conflict", 409)
                return self.public(previous)
            absolute = self.remote_path(body["bindingId"], body["path"])
            try:
                directory = Path(body["directory"]).expanduser().resolve(strict=True)
                require(directory.is_dir(), "local_directory_required")
                name = posixpath.basename(absolute)
                # An upstream POSIX filename must remain one filename on Windows.
                require(name not in (".", "..") and Path(name).name == name
                        and (os.name != "nt" or not any(c in name for c in '<>:"/\\|?*') and not name.endswith((".", " "))),
                        "local_filename_unsupported")
                destination = directory / name
                require(not os.path.lexists(destination), "local_file_exists", 409)
                require(not any(r["destination"] == str(destination) and r["status"] in ("pending", "running")
                                for r in self.downloads()), "download_in_progress", 409)
                fd, temporary = tempfile.mkstemp(prefix=".igw-download-", suffix=".part", dir=directory)
                os.close(fd)
            except OSError:
                raise GatewayError("local_directory_unavailable", 403) from None
            row = dict(id="d_" + secrets.token_hex(12), bindingId=body["bindingId"], path=absolute,
                       destination=str(destination), temporary=temporary, status="pending", bytes=0,
                       expectedBytes=None, createdAt=now(), submissionId=submission_id, submissionHash=digest)
            binding = self.g.store.get("binding", body["bindingId"])
            row.update(accountId=binding["accountId"], machineId=binding["machineId"])
            self.g.store.put("file_download", row["id"], row)
            event = threading.Event()
            thread = threading.Thread(target=self._download, args=(dict(row), event), name="igw-file-download", daemon=True)
            self.workers[row["id"]] = (thread, event)
            thread.start()
            return self.public(row)

    def public(self, row):
        value = {k: v for k, v in row.items() if k not in ("temporary", "submissionId", "submissionHash")}
        with self.lock:
            worker = self.workers.get(row["id"])
            value["cancelRequested"] = bool(worker and worker[1].is_set())
        return value

    def _save(self, row):
        with self.lock:
            if not self.closed:
                self.g.store.put("file_download", row["id"], row)

    def _download(self, row, event):
        try:
            row.update(status="running")
            self._save(row)
            digest = hashlib.sha256()
            with self.g._download({"bindingId": row["bindingId"]}, row["path"]) as (reader, audit):
                row.update(expectedBytes=audit["expectedBytes"], requestId=audit["id"])
                require(shutil.disk_usage(Path(row["destination"]).parent).free >= row["expectedBytes"],
                        "insufficient_local_space", 409)
                self._save(row)
                saved = time.monotonic()
                with open(row["temporary"], "wb") as output:
                    while row["bytes"] < row["expectedBytes"]:
                        require(not event.is_set(), "download_cancelled", 409)
                        chunk = reader.read(min(65536, row["expectedBytes"] - row["bytes"]))
                        require(bool(chunk), "incomplete_download", 502)
                        output.write(chunk)
                        digest.update(chunk)
                        row["bytes"] += len(chunk)
                        audit["bytesTransferred"] += len(chunk)
                        if time.monotonic() - saved >= 1:
                            self._save(row)
                            saved = time.monotonic()
                    output.flush()
                    os.fsync(output.fileno())
                require(not event.is_set(), "download_cancelled", 409)
                # Atomic no-overwrite publication on the destination filesystem.
                try:
                    os.link(row["temporary"], row["destination"])
                except FileExistsError:
                    raise GatewayError("local_file_exists", 409) from None
                row.update(status="succeeded", sha256=digest.hexdigest(), finishedAt=now())
        except GatewayError as exc:
            row.update(status="cancelled" if exc.code == "download_cancelled" else "failed", error=exc.code, finishedAt=now())
        except OSError:
            row.update(status="failed", error="local_write_failed", finishedAt=now())
        except Exception:
            row.update(status="failed", error="download_failed", finishedAt=now())
        finally:
            try:
                os.unlink(row["temporary"])
            except OSError:
                pass
            self._save(row)
            with self.lock:
                self.workers.pop(row["id"], None)

    def cancel(self, ident):
        with self.lock:
            row = self.g.store.get("file_download", text(ident, 100))
            require(row is not None, "download_not_found", 404)
            worker = self.workers.get(ident)
            if worker:
                worker[1].set()
            return self.public(row)

    def close(self):
        with self.lock:
            workers = list(self.workers.values())
            for _, event in workers:
                event.set()
        # Let the stream's 60s read timeout release its audit before Store closes.
        deadline = time.monotonic() + 65
        for thread, _ in workers:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        with self.lock:
            self.closed = True
            for ident, (_, event) in self.workers.items():
                event.set()
                row = self.g.store.get("file_download", ident)
                row.update(status="failed", error="download_interrupted", finishedAt=now())
                self.g.store.put("file_download", ident, row)
