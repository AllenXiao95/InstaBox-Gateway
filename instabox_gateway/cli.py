import argparse
import getpass
import json
import sqlite3
from pathlib import Path
import os
import sys
import webbrowser
import time

from cryptography.exceptions import InvalidTag

from .errors import GatewayError
from .runtime import ProcessLock, control_service, launch_background, read_instance, run_server
from .store import Store
from .auth import local_request


def default_directory():
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "InstaBoxGateway"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "InstaBoxGateway"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "instabox-gateway"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local InstaBox Gateway (never loads .env)")
    parser.add_argument("--data-dir", type=Path, default=default_directory())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Initialize encrypted storage and display both local tokens")
    serve = sub.add_parser("serve", help="Unlock and start the local service in the background")
    serve.add_argument("--port", type=int, default=18761)
    serve.add_argument("--foreground", action="store_true", help="Keep serving in this terminal")
    serve.add_argument("--open", action="store_true", help="Open a signed-in console after background startup")
    serve.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("status", help="Check local service identity; legacy services require a passphrase")
    sub.add_parser("stop", help="Stop this local service, without stopping upstream machines")
    sub.add_parser("open", help="Open the running console, without copying a management token")
    token = sub.add_parser("token", help="Display a local Gateway token (never an upstream Key)")
    token.add_argument("role", choices=("admin", "execution"))
    args = parser.parse_args(argv)
    args.data_dir = args.data_dir.expanduser().resolve()
    worker = args.command == "serve" and args._worker
    store = None
    try:
        if args.command == "open":
            record, reply = local_request(args.data_dir, "ticket")
            webbrowser.open(f"http://127.0.0.1:{record['port']}/#login=" + reply["ticket"])
            print("Opened the local console. The single-use login ticket expires in 60 seconds.")
            return 0
        if args.command in ("status", "stop") and (args.data_dir / "local-access.json").exists():
            record, reply = local_request(args.data_dir, args.command)
            if args.command == "status":
                print(f"Running (PID {reply['pid']}): http://127.0.0.1:{reply['port']}")
            else:
                deadline = time.monotonic() + 45
                while read_instance(args.data_dir) is not None:
                    if time.monotonic() >= deadline:
                        raise GatewayError("service_stop_timeout")
                    time.sleep(.05)
                print("Local service stopped. Upstream machines and processes were not stopped.")
            return 0
        if args.command == "serve" and not 1 <= args.port <= 65535:
            raise GatewayError("invalid_port")
        if args.command in ("status", "stop") and read_instance(args.data_dir) is None:
            print("No managed local service is recorded for this data directory.")
            print("For a legacy foreground instance, use Ctrl+C in its terminal.")
            return 0
        if worker:
            if not args.foreground:
                raise GatewayError("invalid_worker_mode")
            try:
                payload = json.loads(sys.stdin.buffer.readline(1024 * 1024))
                password = payload["password"]
                if not isinstance(password, str):
                    raise ValueError
                del payload
            except (ValueError, KeyError, TypeError):
                raise GatewayError("invalid_worker_input") from None
            finally:
                sys.stdin.close()
        else:
            password = getpass.getpass("Store passphrase: ")
        if args.command == "init" and password != getpass.getpass("Repeat passphrase: "):
            raise GatewayError("passphrase_mismatch")
        store = Store(args.data_dir, password, create=args.command == "init")
        if args.command == "init":
            print("Encrypted store initialized.")
            print("Management token: " + store.token("admin"))
            print("Execution token: " + store.token("execution"))
            print("Keep these local credentials private. Run igw serve to start in the background.")
        elif args.command == "token":
            print(store.token(args.role))
        elif args.command in ("status", "stop"):
            record = control_service(args.data_dir, store.token("admin"), stop=args.command == "stop")
            if record is None:
                print("No managed local service is recorded for this data directory.")
            elif args.command == "stop":
                print("Local service stopped. Upstream machines and processes were not stopped.")
            else:
                print(f"Running (PID {record['pid']}): http://127.0.0.1:{record['port']}")
        elif args.foreground:
            del password
            owned_store, store = store, None
            run_server(owned_store, args.port, worker=worker)
        else:
            directory = store.directory
            store.close()
            store = None
            result = launch_background(directory, password, args.port)
            del password
            print(f"InstaBox Gateway started in background (PID {result['pid']}): http://127.0.0.1:{args.port}")
            print("Use igw status or igw stop to manage this local service.")
            if args.open:
                record, reply = local_request(directory, "ticket")
                webbrowser.open(f"http://127.0.0.1:{record['port']}/#login=" + reply["ticket"])
        return 0
    except KeyboardInterrupt:
        return 0
    except GatewayError as exc:
        if worker:
            print(json.dumps({"error": exc.code}), flush=True)
        else:
            print(exc.code, file=sys.stderr)
        return 1
    except OSError:
        if worker:
            print(json.dumps({"error": "local_storage_or_port_unavailable"}), flush=True)
        else:
            print("local_storage_or_port_unavailable", file=sys.stderr)
        return 1
    except (ValueError, sqlite3.DatabaseError, InvalidTag):
        if worker:
            print(json.dumps({"error": "local_store_invalid"}), flush=True)
        else:
            print("local_store_invalid", file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
