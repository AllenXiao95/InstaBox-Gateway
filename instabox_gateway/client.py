"""Thin HTTP client for local Codex/scripts; only uses a Gateway execution token."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("IGW_URL", "http://127.0.0.1:18761"))
    parser.add_argument("--account")
    parser.add_argument("--machine")
    parser.add_argument("--binding")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("machines")
    select = sub.add_parser("select")
    select.add_argument("--allow-wake", action="store_true")
    execute = sub.add_parser("exec")
    execute.add_argument("--cwd")
    execute.add_argument("--timeout", type=int, default=60)
    execute.add_argument("argv", nargs=argparse.REMAINDER)
    for action in ("upload", "download"):
        file = sub.add_parser(action)
        file.add_argument("local", type=Path)
        file.add_argument("remote")
    resume = sub.add_parser("resume")
    resume.add_argument("--idempotency-key", required=True)
    resume.add_argument("--allow-wake", action="store_true", required=True)
    operation = sub.add_parser("operation")
    operation.add_argument("id")
    args = parser.parse_args(argv)
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
    if args.action in ("exec", "upload", "download", "resume") and not (args.binding or args.account and args.machine):
        parser.error("An explicit account + machine or binding is required")
    token = os.environ.get("IGW_TOKEN") or getpass.getpass("Gateway execution token: ")
    target = {"bindingId": args.binding} if args.binding else {"accountId": args.account, "machineId": args.machine}
    body, method, path = None, "GET", "/api/v1/machines"
    headers = {"Authorization": "Bearer " + token}
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
    elif args.action in ("upload", "download"):
        path = "/api/v1/file?" + urllib.parse.urlencode(dict(target, path=args.remote))
        if args.action == "upload":
            if args.local.stat().st_size > 8 * 1024 * 1024:
                parser.error("file exceeds Gateway 8 MiB limit")
            body, method = args.local.read_bytes(), "PUT"
            headers["Content-Type"] = "application/octet-stream"
    elif args.action == "resume":
        body, method, path = {"target": target, "allowWake": args.allow_wake}, "POST", "/api/v1/resumes"
        headers["Idempotency-Key"] = args.idempotency_key
    elif args.action == "operation":
        path = "/api/v1/resumes/" + urllib.parse.quote(args.id, safe="")
    elif args.account:
        path += "?" + urllib.parse.urlencode({"accountId": args.account})
    data = body
    if isinstance(body, dict):
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(args.url.rstrip("/") + path, data=data, method=method, headers=headers)
    from .upstream import NoRedirect
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=150) as response:
            result = response.read(16 * 1024 * 1024 + 1)
            if len(result) > 16 * 1024 * 1024:
                print("gateway_response_too_large", file=sys.stderr)
                return 1
            if args.action == "download":
                # Never overwrite an existing local file implicitly.
                with args.local.open("xb") as out:
                    out.write(result)
                return 0
            value = json.loads(result)
            print(json.dumps(value, ensure_ascii=True))
            if args.action == "exec":
                code = value["result"]["exitCode"]
                return code if 0 <= code <= 255 else 1
            return 0
    except urllib.error.HTTPError as exc:
        print("Gateway HTTP " + str(exc.code), file=sys.stderr)
        return 1
    except (OSError, urllib.error.URLError, ValueError):
        print("gateway_request_or_local_file_failed; do not automatically retry writes", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
