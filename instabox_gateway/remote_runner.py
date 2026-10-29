"""Small standalone Linux task runner, copied to the selected workspace. No credentials."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


def atomic(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def identity(pid):
    try:
        if Path("/proc/self/stat").exists():
            stat = (Path("/proc") / str(pid) / "stat").read_text()
            tail = stat[stat.rfind(")") + 2:].split()
            if tail[0] == "Z":
                return None
            return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + tail[19]
        out = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True, check=True)
        return out.stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def path_in(root, path):
    value = Path(path)
    value = (value if value.is_absolute() else root / value).resolve()
    if value != root and root not in value.parents:
        raise ValueError("workspace_path_denied")
    return value


def git_info(root):
    if not (root / ".git").exists():
        return None
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    def git(*argv):
        return subprocess.run(["git", "-C", str(root), *argv], env=env, capture_output=True,
                              text=True, check=True, timeout=10).stdout.strip()
    if Path(git("rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("workspace_git_mismatch")
    common = Path(git("rev-parse", "--git-common-dir"))
    common = (root / common).resolve() if not common.is_absolute() else common.resolve()
    # External worktree Git directories need a separate, explicit registration.
    if root not in common.parents and common != root:
        raise ValueError("external_git_directory_not_registered")
    dirty = []
    budget = 16 * 1024 * 1024
    names = git("ls-files", "-m", "-o", "--exclude-standard", "-z").split("\0")
    for name in names[:1000]:
        if not name or name.startswith(".igw/"):
            continue
        path = path_in(root, name)
        size = path.stat().st_size if path.is_file() else 0
        digest = None
        if path.is_file() and size <= budget:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            budget -= size
        dirty.append({"path": name, "size": size, "sha256": digest})
    return {"root": str(root), "commit": git("rev-parse", "HEAD"), "dirtyInputs": dirty,
            "inputInventoryComplete": len(names) <= 1000 and all(p["sha256"] for p in dirty),
            "status": git("status", "--porcelain=v1", "--untracked-files=normal")[:65536],
            "branch": git("rev-parse", "--abbrev-ref", "HEAD")}


def checked_root(value):
    root = Path(value["root"])
    resolved = root.resolve()
    base = Path(value["fileRoot"]).resolve()
    if str(resolved) != str(root) or base not in resolved.parents:
        raise ValueError("workspace_root_denied")
    if value.get("create"):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not root.is_dir():
        raise ValueError("workspace_missing")
    return root


def worker(task):
    with (task / "worker.lock").open("a+") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state_path = task / "state.json"
        with (task / "control.lock").open("a+") as control:
            fcntl.flock(control, fcntl.LOCK_EX)
            state = json.loads(state_path.read_text())
            state.update(status="running", pid=os.getpid(), identity=identity(os.getpid()), startedAt=time.time())
            atomic(state_path, state)
        spec = json.loads((task / "spec.json").read_text())
        child = None
        def stop(*_):
            (task / "cancel").touch()
        signal.signal(signal.SIGTERM, stop)
        try:
            if not state["identity"]:
                raise ValueError("process_identity_unavailable")
            root = checked_root(spec)
            cwd = path_in(root, spec.get("cwd", "."))
            before = git_info(root)
            expected = spec.get("expectedCommit")
            if expected and (not before or before["commit"] != expected):
                raise ValueError("workspace_commit_changed")
            if spec.get("expectedInputHash"):
                observed = hashlib.sha256(json.dumps({"commit": before["commit"], "dirtyInputs": before["dirtyInputs"]}, sort_keys=True).encode()).hexdigest() if before else None
                if observed != spec["expectedInputHash"]:
                    raise ValueError("workspace_inputs_changed")
            state["gitBefore"] = before
            env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            env.update(spec.get("envs", {}))
            stages = [("run", spec["argv"])]
            if spec.get("verifyArgv"):
                stages.append(("verify", spec["verifyArgv"]))
            for phase, argv in stages:
                state.update(phase=phase, verification="pending" if phase == "verify" else "not_requested")
                atomic(state_path, state)
                with (task / "stdout.log").open("ab") as out, (task / "stderr.log").open("ab") as err:
                    child = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                                             stdout=out, stderr=err, start_new_session=True)
                    while child.poll() is None:
                        if (task / "cancel").exists():
                            os.killpg(child.pid, signal.SIGTERM)
                            try:
                                child.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                os.killpg(child.pid, signal.SIGKILL)
                                child.wait()
                            # A child can exit on TERM while other group members remain.
                            with contextlib.suppress(ProcessLookupError):
                                os.killpg(child.pid, signal.SIGKILL)
                            break
                        time.sleep(.1)
                    state["exitCode"] = child.returncode
                if (task / "cancel").exists():
                    state.update(status="cancelled", terminationScope="owned_process_group_only", externalResourcesUnverified=True)
                    break
                if child.returncode:
                    state.update(status="failed", verification="failed" if phase == "verify" else "not_requested")
                    break
                if phase == "verify":
                    state["verification"] = "verified"
            else:
                state["status"] = "succeeded"
            state["gitAfter"] = git_info(root)
        except Exception as exc:
            state.update(status="blocked", error=str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
        finally:
            if child is not None and child.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            state["finishedAt"] = time.time()
            atomic(state_path, state)


def main(action, value):
    root = checked_root(value)
    if action == "watch-status":
        path = path_in(root, value["statusPath"])
        with path.open("rb") as stream:
            raw = stream.read(262145)
        if len(raw) > 262144:
            raise ValueError("external_status_too_large")
        external = json.loads(raw)
        status = external.get("status", external.get("phase"))
        status = {"verified": "succeeded", "completed": "succeeded", "stopped": "cancelled"}.get(status, status)
        if status not in ("pending", "running", "succeeded", "failed", "cancelled", "blocked"):
            raise ValueError("external_task_state_unknown")
        return {"status": status, "verification": "verified" if external.get("status") == "verified" else "not_requested",
                "verificationEvidence": "producer_reported", "phase": external.get("phase", external.get("checkpoint")),
                "exitCode": external.get("exit_code", external.get("workerExitCode"))}
    if action == "watch-logs":
        channel = value.get("channel", "stdout")
        if channel not in ("stdout", "stderr") or not value.get(channel + "Path"):
            raise ValueError("external_log_not_configured")
        path = path_in(root, value[channel + "Path"])
        with path.open("rb") as stream:
            stream.seek(value.get("offset", 0))
            data = stream.read(65536)
        return {"channel": channel, "offset": value.get("offset", 0) + len(data), "text": data.decode("utf-8", "replace")}
    if action == "probe":
        cwd = path_in(root, value.get("cwd", "."))
        if not cwd.is_dir():
            raise ValueError("workspace_cwd_missing")
        result = {"root": str(root), "cwd": str(cwd), "git": git_info(root)}
        if "path" in value:
            result["path"] = str(path_in(root, value["path"]))
        return result
    task = root / ".igw" / value["taskId"]
    if not value["taskId"].replace("_", "").isalnum():
        raise ValueError("invalid_task_id")
    if task.resolve() != task:
        raise ValueError("workspace_path_denied")
    if action == "start":
        task.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (task / "control.lock").open("a+") as control:
            fcntl.flock(control, fcntl.LOCK_EX)
            state_path = task / "state.json"
            if state_path.exists():
                return main("status", value)
            atomic(task / "spec.json", value)
            atomic(state_path, {"status": "pending", "verification": "not_requested", "taskId": value["taskId"], "launchedAt": time.time()})
            with (task / "runner.log").open("ab") as out:
                process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "worker", str(task)],
                                           stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
            atomic(state_path, {"status": "pending", "verification": "not_requested", "taskId": value["taskId"],
                                "launchPid": process.pid, "launchIdentity": identity(process.pid), "launchedAt": time.time()})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = json.loads(state_path.read_text())
            if state["status"] != "pending":
                return state
            if process.poll() is not None:
                break
            time.sleep(.05)
        return main("status", value)
    state = json.loads((task / "state.json").read_text()) if (task / "state.json").exists() else {"status": "blocked", "error": "task_not_found"}
    if action == "cancel":
        if state["status"] in ("pending", "running"):
            (task / "cancel").touch()
        return state
    if action == "logs":
        channel = value.get("channel", "stdout")
        if channel not in ("stdout", "stderr"):
            raise ValueError("invalid_log_channel")
        path = path_in(root, str(task / (channel + ".log")))
        offset = value.get("offset", 0)
        data = b""
        if path.exists():
            with path.open("rb") as stream:
                stream.seek(offset)
                data = stream.read(65536)
        return {"channel": channel, "offset": offset + len(data), "text": data.decode("utf-8", "replace")}
    if state["status"] == "running" and identity(state["pid"]) != state["identity"]:
        state.update(status="blocked", error="worker_missing_reconcile_required")
        atomic(task / "state.json", state)
    if state["status"] == "pending" and time.time() - state.get("launchedAt", time.time()) > 30:
        if not state.get("launchPid") or not state.get("launchIdentity") or identity(state["launchPid"]) != state["launchIdentity"]:
            state.update(status="blocked", error="worker_not_started_reconcile_required")
            atomic(task / "state.json", state)
    return state


if __name__ == "__main__":
    if sys.argv[1] == "worker":
        worker(Path(sys.argv[2]))
    else:
        try:
            print(json.dumps(main(sys.argv[1], json.load(sys.stdin))))
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            print(json.dumps({"runnerError": str(exc) if isinstance(exc, ValueError) else type(exc).__name__}))
            sys.exit(1)
