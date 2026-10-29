"""Thin HTTP client for local Codex/scripts; only uses a Gateway execution token."""
import argparse
import getpass
import hashlib
import http.client
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .auth import local_request, private_json, read_private
from .errors import GatewayError
from .transfer import FileBody


def main(argv=None):
    from .cli import default_directory
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=default_directory())
    parser.add_argument("--url", default=os.environ.get("IGW_URL"))
    parser.add_argument("--account")
    parser.add_argument("--machine")
    parser.add_argument("--binding")
    parser.add_argument("--workspace", help="Fixed workspace ID; defaults to the paired profile")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("machines")
    sub.add_parser("connection-info")
    sub.add_parser("login", help="Pair once with the running, unlocked local service")
    sub.add_parser("logout", help="Remove this CLI's saved execution credential")
    sub.add_parser("use", help="Save a default workspace for this CLI profile").add_argument("id")
    ws = sub.add_parser("workspace")
    ws.add_argument("operation", choices=("list", "add"))
    ws.add_argument("--name")
    ws.add_argument("--remote-root")
    ws.add_argument("--local-root", type=Path, default=Path.cwd())
    select = sub.add_parser("select")
    select.add_argument("--allow-wake", action="store_true")
    execute = sub.add_parser("exec")
    execute.add_argument("--cwd")
    execute.add_argument("--timeout", type=int, default=60)
    execute.add_argument("argv", nargs=argparse.REMAINDER)
    execute.add_argument("--mode", choices=("sync", "background_task"), default="sync")
    execute.add_argument("--submission-id")
    for action in ("test", "git"):
        command = sub.add_parser(action)
        command.add_argument("argv", nargs=argparse.REMAINDER)
    task = sub.add_parser("task")
    task.add_argument("operation", choices=("start", "watch", "list", "status", "logs", "cancel", "resume", "verify", "events"))
    task.add_argument("id", nargs="?")
    task.add_argument("--submission-id")
    task.add_argument("--argv", help="JSON argument array")
    task.add_argument("--verify-argv", help="JSON verification argument array")
    task.add_argument("--preflight-argv", help="JSON preflight argument array, checked before starting")
    task.add_argument("--resume-argv", help="JSON checkpoint-aware resume argument array")
    task.add_argument("--expected-commit")
    task.add_argument("--status-path", help="Existing producer JSON state, relative to the workspace")
    task.add_argument("--stdout-path")
    task.add_argument("--stderr-path")
    task.add_argument("--cwd", default=".")
    task.add_argument("--channel", choices=("stdout", "stderr"), default="stdout")
    task.add_argument("--offset", type=int, default=0)
    for action in ("upload", "download"):
        file = sub.add_parser(action)
        file.add_argument("local", type=Path)
        file.add_argument("remote")
        if action == "download":
            file.add_argument("--sha256", help="Expected SHA256; verified before publishing the file")
            file.add_argument("--timeout", type=float, default=75, help="Socket inactivity timeout in seconds (default: 75)")
    resume = sub.add_parser("resume")
    resume.add_argument("--idempotency-key", required=True)
    resume.add_argument("--allow-wake", action="store_true", required=True)
    operation = sub.add_parser("operation")
    operation.add_argument("id")
    args = parser.parse_args(argv)
    args.data_dir = args.data_dir.expanduser().resolve()
    profile_path = args.data_dir / "client.json"
    try:
        profile = read_private(profile_path) if profile_path.exists() else {}
        args.url = args.url or profile.get("url", "http://127.0.0.1:18761")
        if args.action == "login":
            record, reply = local_request(args.data_dir, "pair")
            expected_url = f"http://127.0.0.1:{record['port']}"
            if args.url.rstrip("/") not in (expected_url, expected_url.replace("127.0.0.1", "localhost")):
                parser.error("Gateway URL does not match this running local service")
            private_json(profile_path, {"url": args.url.rstrip("/"), "token": reply["token"]})
            print("Paired with the local Gateway. Execution access only; no upstream keys saved.")
            return 0
        if args.action == "logout":
            profile_path.unlink(missing_ok=True)
            print("Local CLI credential removed. Shared execution token was not revoked.")
            return 0
    except (OSError, ValueError, GatewayError) as exc:
        print(getattr(exc, "code", "local_pairing_failed") + "; start/unlock Gateway and check the data directory", file=sys.stderr)
        return 1
    use_default = args.action in ("exec", "test", "git", "upload", "download", "resume", "connection-info") or args.action == "task" and args.operation in ("start", "watch")
    if use_default and not (args.binding or args.account or args.machine or args.workspace):
        args.workspace = profile.get("workspaceId")
    if args.action == "select" and args.workspace:
        parser.error("A workspace is already fixed; select creates a new binding without --workspace")
    if args.action == "download":
        if args.sha256 is not None and (len(args.sha256) != 64 or any(c not in "0123456789abcdefABCDEF" for c in args.sha256)):
            parser.error("--sha256 must contain 64 hexadecimal characters")
        if not 0 < args.timeout <= 7200:
            parser.error("--timeout must be between 0 and 7200 seconds")
    try:
        url = urllib.parse.urlsplit(args.url)
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError()
    except ValueError:
        parser.error("Invalid Gateway URL")
    if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost") or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment:
        parser.error("Gateway URL must be a local loopback HTTP origin")
    if args.binding and (args.account or args.machine):
        parser.error("Use either binding or account + machine")
    if args.workspace and (args.binding or args.account or args.machine):
        parser.error("Use a workspace or an explicit target, never both")
    if args.action in ("exec", "upload", "download", "resume", "connection-info") and not (args.workspace or args.binding or args.account and args.machine):
        parser.error("An explicit account + machine or binding is required")
    paired_token = profile.get("token") if profile.get("url") == args.url.rstrip("/") else None
    token = os.environ.get("IGW_TOKEN") or paired_token or getpass.getpass("Gateway execution token: ")
    target = {"bindingId": args.binding} if args.binding else {"accountId": args.account, "machineId": args.machine}
    body, method, path = None, "GET", "/api/v1/machines"
    headers = {"Authorization": "Bearer " + token}
    from .upstream import NoRedirect
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    workspace = None
    if args.workspace:
        try:
            req = urllib.request.Request(args.url.rstrip("/") + "/api/v1/workspaces", headers=headers)
            with opener.open(req, timeout=10) as response:
                workspace = next(w for w in json.loads(response.read(2 * 1024 * 1024))["workspaces"] if w["id"] == args.workspace)
            target = {"bindingId": workspace["bindingId"]}
        except (OSError, ValueError, KeyError, StopIteration):
            print("workspace_not_found_or_unavailable", file=sys.stderr)
            return 1
    if args.action == "select":
        body = {"allowWake": args.allow_wake}
        if args.binding or args.machine:
            body["target"] = target
        elif args.account:
            body["accountIds"] = [args.account]
        method, path = "POST", "/api/v1/bindings"
    elif args.action == "exec":
        command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        body = {"target": target, "argv": command, "timeoutSeconds": args.timeout}
        if args.cwd:
            body["cwd"] = args.cwd
        method, path = "POST", "/api/v1/exec"
        if args.mode == "background_task":
            if not args.workspace:
                parser.error("background_task requires --workspace")
            body = {"mode": "background_task", "workspaceId": args.workspace, "argv": command, "cwd": args.cwd or "."}
            headers["Idempotency-Key"] = args.submission_id or uuid.uuid4().hex
            print("Submission ID: " + headers["Idempotency-Key"], file=sys.stderr)
        elif args.workspace:
            body.pop("target")
            path = "/api/v1/workspaces/" + args.workspace + "/exec"
    elif args.action in ("upload", "download"):
        path = "/api/v1/file?" + urllib.parse.urlencode(dict(target, path=args.remote))
        if workspace:
            path = "/api/v1/file?" + urllib.parse.urlencode({"workspaceId": args.workspace, "path": args.remote})
            base = Path(workspace["localRoot"]).resolve()
            local = args.local.expanduser().resolve()
            if base != local and base not in local.parents:
                parser.error("Local file must be inside the workspace localRoot")
        if args.action == "upload":
            method = "PUT"
            headers["Content-Type"] = "application/octet-stream"
    elif args.action == "connection-info":
        path = "/api/v1/connection-info?" + urllib.parse.urlencode(target)
    elif args.action == "resume":
        body, method, path = {"target": target, "allowWake": args.allow_wake}, "POST", "/api/v1/resumes"
        headers["Idempotency-Key"] = args.idempotency_key
    elif args.action == "operation":
        path = "/api/v1/resumes/" + urllib.parse.quote(args.id, safe="")
    elif args.action == "workspace":
        path = "/api/v1/workspaces"
        if args.operation == "add":
            if not args.binding or not args.name or not args.remote_root:
                parser.error("workspace add needs --binding, --name and --remote-root")
            body, method = {"name": args.name, "bindingId": args.binding, "remoteRoot": args.remote_root, "localRoot": str(args.local_root)}, "POST"
    elif args.action in ("test", "git"):
        if not args.workspace:
            parser.error("test/git require a workspace")
        body = {"argv": args.argv[1:] if args.argv[:1] == ["--"] else args.argv}
        method, path = "POST", "/api/v1/workspaces/" + args.workspace + "/" + args.action
    elif args.action == "task":
        path = "/api/v1/tasks"
        if args.operation == "events":
            path = "/api/v1/task-events"
        elif args.operation == "watch":
            if not args.workspace or not args.status_path:
                parser.error("task watch needs a workspace and --status-path")
            path += "/watch"
            body, method = {"workspaceId": args.workspace, "statusPath": args.status_path}, "POST"
            if args.stdout_path:
                body["stdoutPath"] = args.stdout_path
            if args.stderr_path:
                body["stderrPath"] = args.stderr_path
        elif args.operation == "start":
            if not args.workspace or not args.argv:
                parser.error("task start needs a workspace and --argv JSON")
            try:
                body = {"workspaceId": args.workspace, "argv": json.loads(args.argv), "cwd": args.cwd}
                for name, argument in (("preflightArgv", "preflight_argv"), ("verifyArgv", "verify_argv"), ("resumeArgv", "resume_argv")):
                    value = getattr(args, argument)
                    if value:
                        body[name] = json.loads(value)
            except ValueError:
                parser.error("Task argv values must be JSON arrays")
            if args.expected_commit:
                body["expectedCommit"] = args.expected_commit
            method = "POST"
        elif args.operation != "list":
            if not args.id or not args.id.startswith("t_") or not args.id.replace("_", "").isalnum():
                parser.error("A valid task ID is required")
            path += "/" + args.id
            if args.operation == "logs":
                path += "/logs?" + urllib.parse.urlencode({"channel": args.channel, "offset": args.offset})
            elif args.operation != "status":
                path += "/" + args.operation
                body, method = {}, "POST"
        if method == "POST":
            headers["Idempotency-Key"] = args.submission_id or uuid.uuid4().hex
            print("Submission ID: " + headers["Idempotency-Key"], file=sys.stderr)
    elif args.action == "use":
        if not paired_token:
            parser.error("Run igw-client login before saving a profile")
        req = urllib.request.Request(args.url.rstrip("/") + "/api/v1/workspaces", headers=headers)
        try:
            with opener.open(req, timeout=10) as response:
                if not any(w["id"] == args.id for w in json.loads(response.read(2 * 1024 * 1024))["workspaces"]):
                    parser.error("Workspace not found")
            private_json(profile_path, {**profile, "workspaceId": args.id})
            print("Default workspace saved. Existing tasks remain fixed.")
            return 0
        except (OSError, ValueError):
            print("workspace_profile_failed", file=sys.stderr)
            return 1
    elif args.account:
        path += "?" + urllib.parse.urlencode({"accountId": args.account})
    data = body
    if isinstance(body, dict):
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    upload_source = None
    try:
        if args.action == "upload":
            upload_source = args.local.open("rb")
            data = FileBody(upload_source, os.fstat(upload_source.fileno()).st_size)
            headers["Content-Length"] = str(data.length)
        req = urllib.request.Request(args.url.rstrip("/") + path, data=data, method=method, headers=headers)
        if args.action == "download" and os.path.lexists(args.local):
            print("download_destination_exists", file=sys.stderr)
            return 1
        started = time.monotonic()
        with opener.open(req, timeout=args.timeout if args.action == "download" else 150) as response:
            if args.action == "download":
                lengths = response.headers.get_all("Content-Length", [])
                if (response.status != 200 or len(lengths) != 1 or not lengths[0].isascii()
                        or not lengths[0].isdecimal() or len(lengths[0]) > 20
                        or response.headers.get("Transfer-Encoding")
                        or response.headers.get("Content-Encoding", "identity") != "identity"):
                    raise ValueError("invalid download response")
                expected, received = int(lengths[0]), 0
                digest = hashlib.sha256()
                fd, name = tempfile.mkstemp(prefix="." + args.local.name + ".", suffix=".part", dir=args.local.parent)
                part = Path(name)
                # Keep incomplete files, but never treat them as resumable or complete.
                print("Downloading to " + str(part), file=sys.stderr)
                with os.fdopen(fd, "wb") as out:
                    while received < expected:
                        if time.monotonic() - started >= 7200:
                            raise ValueError("download deadline exceeded")
                        chunk = response.read(min(64 * 1024, expected - received))
                        if not chunk:
                            raise ValueError("incomplete download")
                        out.write(chunk)
                        digest.update(chunk)
                        received += len(chunk)
                checksum = digest.hexdigest()
                if args.sha256 and checksum != args.sha256.lower():
                    print("download_sha256_mismatch; incomplete file retained: " + str(part), file=sys.stderr)
                    return 1
                # POSIX rename replaces existing files; a hard link publishes without
                # overwriting a destination created while the transfer was running.
                if os.name == "nt":
                    os.rename(part, args.local)
                else:
                    os.link(part, args.local)
                    part.unlink()
                print(json.dumps({"bytes": received, "sha256": checksum,
                                  "checksumVerified": bool(args.sha256)}))
                return 0
            result = response.read(16 * 1024 * 1024 + 1)
            if len(result) > 16 * 1024 * 1024:
                print("gateway_response_too_large", file=sys.stderr)
                return 1
            value = json.loads(result)
            if args.action == "upload" and (data.transferred != data.length or value.get("size") != data.length or value.get("sha256") != data.sha256.hexdigest()):
                raise ValueError("upload checksum mismatch")
            print(json.dumps(value, ensure_ascii=True))
            if args.action in ("exec", "test", "git") and "result" in value:
                code = value["result"]["exitCode"]
                return code if 0 <= code <= 255 else 1
            return 0
    except urllib.error.HTTPError as exc:
        print("Gateway HTTP " + str(exc.code), file=sys.stderr)
        try:
            error = json.loads(exc.read(65536)).get("error", {})
            safe = {k: v for k, v in error.items() if k in ("code", "outcome", "requestId")
                    and isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", v)}
            if safe:
                print(json.dumps(safe), file=sys.stderr)
        except (ValueError, AttributeError):
            pass
        return 1
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
        print("gateway_request_or_local_file_failed; do not automatically retry writes", file=sys.stderr)
        return 1
    finally:
        if upload_source:
            upload_source.close()


if __name__ == "__main__":
    sys.exit(main())
