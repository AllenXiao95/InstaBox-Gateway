"""Local process lifecycle. No upstream requests or secret files are needed."""
import http.client
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

from .errors import GatewayError
from .gateway import Gateway
from .http import Server
from .upstream import Upstream


class ProcessLock:
    """One serving process per encrypted data directory, even on different ports."""
    def __init__(self, directory):
        self.file = open(Path(directory) / "server.lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self.file.seek(0, 2) == 0:
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise GatewayError("server_already_running", 409) from None

    def close(self):
        self.file.close()


def instance_path(directory):
    return Path(directory).expanduser().resolve() / "server.json"


def read_instance(directory):
    try:
        record = json.loads(instance_path(directory).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError):
        raise GatewayError("service_state_invalid") from None
    if (not isinstance(record, dict) or type(record.get("pid")) is not int
            or record["pid"] <= 0 or type(record.get("port")) is not int
            or not 1 <= record["port"] <= 65535
            or not isinstance(record.get("instanceId"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", record["instanceId"])):
        raise GatewayError("service_state_invalid")
    return record


def _write_instance(directory, record):
    target = instance_path(directory)
    fd, name = tempfile.mkstemp(prefix=".server-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run_server(store, port, *, worker=False):
    """Own the store until the local server has fully closed."""
    lock, server, record, old_term = None, None, None, None
    try:
        lock = ProcessLock(store.directory)
        server = Server(("127.0.0.1", port), Gateway(store, Upstream()))
        record = {"instanceId": server.instance_id, "pid": os.getpid(), "port": port}
        _write_instance(store.directory, record)
        if threading.current_thread() is threading.main_thread():
            old_term = signal.signal(signal.SIGTERM, lambda *_: threading.Thread(
                target=server.shutdown, daemon=True).start())
        if worker:
            print(json.dumps({"ready": True, **record}), flush=True)
            # Never retain a pipe back to the launching terminal while serving.
            sys.stdout.close()
        else:
            print(f"InstaBox Gateway (foreground): http://127.0.0.1:{port}", flush=True)
        server.serve_forever()
    finally:
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)
        if server:
            server.server_close()
        store.close()
        if record:
            try:
                if read_instance(store.directory) == record:
                    instance_path(store.directory).unlink()
            except (OSError, GatewayError):
                pass
        if lock:
            lock.close()


def launch_background(directory, password, port, *, timeout=20):
    """Pass unlock material through an anonymous pipe, never argv/env/disk."""
    command = [sys.executable, "-m", "instabox_gateway", "--data-dir",
               str(Path(directory).expanduser().resolve()), "serve", "--port", str(port),
               "--foreground", "--_worker"]
    options = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
               "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(command, **options)
    replies = queue.Queue()
    def read_reply():
        try:
            replies.put(process.stdout.readline(65536))
        except OSError:
            replies.put(b"")
    reader = threading.Thread(target=read_reply, daemon=True)
    reader.start()
    try:
        try:
            process.stdin.write((json.dumps({"password": password}) + "\n").encode())
            process.stdin.close()
            reply = json.loads(replies.get(timeout=timeout))
        except queue.Empty:
            raise GatewayError("service_start_timeout") from None
        except (OSError, ValueError, UnicodeError):
            raise GatewayError("service_start_failed") from None
        if isinstance(reply, dict) and isinstance(reply.get("error"), str):
            raise GatewayError(reply["error"])
        if (not isinstance(reply, dict) or reply.get("ready") is not True
                or reply.get("pid") != process.pid or reply.get("port") != port
                or read_instance(directory) != {k: reply.get(k) for k in ("instanceId", "pid", "port")}):
            raise GatewayError("service_start_failed")
        return reply
    except BaseException:
        # This is only our just-created child, never a PID read from disk.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        raise
    finally:
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()
        # Reap the detached child if this Python caller stays alive after main().
        threading.Thread(target=process.wait, daemon=True).start()


def _control_request(record, token, method="GET"):
    # HTTPConnection does not follow redirects or use ambient proxy settings.
    connection = http.client.HTTPConnection("127.0.0.1", record["port"], timeout=3)
    try:
        headers = {"Authorization": "Bearer " + token}
        path, body = "/api/admin/service", None
        if method == "POST":
            path += "/stop"
            body = json.dumps({"instanceId": record["instanceId"]})
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read(65537)
        if len(data) > 65536 or response.status != 200:
            raise GatewayError("service_control_rejected")
        result = json.loads(data)
        if not isinstance(result, dict) or result.get("instanceId") != record["instanceId"]:
            raise GatewayError("service_instance_mismatch")
        if method == "GET" and (result.get("pid") != record["pid"] or result.get("port") != record["port"]):
            raise GatewayError("service_instance_mismatch")
        return result
    except (OSError, http.client.HTTPException):
        raise GatewayError("service_unreachable") from None
    except (ValueError, UnicodeError):
        raise GatewayError("service_response_invalid") from None
    finally:
        connection.close()


def control_service(directory, token, *, stop=False):
    record = read_instance(directory)
    if record is None:
        return None
    _control_request(record, token)
    if stop:
        reply = _control_request(record, token, "POST")
        if reply.get("stopping") is not True:
            raise GatewayError("service_control_rejected")
        deadline = time.monotonic() + 10
        while read_instance(directory) == record:
            if time.monotonic() >= deadline:
                raise GatewayError("service_stop_timeout")
            time.sleep(.05)
    return record
