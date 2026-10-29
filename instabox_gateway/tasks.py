"""Fixed-target task submission, reconciliation and one local monitor loop."""
import hashlib
import json
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time

from .errors import GatewayError
from .gateway import fields, require, text
from .store import now
from .workspaces import RUNNER

TERMINAL = ("succeeded", "failed", "cancelled", "blocked")


def desktop_notify(task):
    # Only safe generated metadata, never command text, raw output or credentials.
    title = "InstaBox Gateway"
    message = task["id"] + " · " + task["status"] + " · " + task.get("verification", "not_requested")
    if task.get("machineId"):
        message += " · " + task["machineId"]
    try:
        if sys.platform == "darwin":
            script = "on run argv\ndisplay notification (item 1 of argv) with title (item 2 of argv)\nend run"
            result = subprocess.run(["osascript", "-e", script, message, title], capture_output=True, timeout=5)
        elif sys.platform.startswith("linux") and shutil.which("notify-send"):
            result = subprocess.run(["notify-send", title, message], capture_output=True, timeout=5)
        else:
            return "unavailable"
        return "submitted" if result.returncode == 0 else "unavailable"
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


class Tasks:
    def __init__(self, gateway):
        self.g = gateway
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.RLock()

    def start_monitor(self):
        self.thread = threading.Thread(target=self._monitor, name="igw-task-monitor", daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=40)

    def _monitor(self):
        while not self.stop_event.is_set():
            for row in self.g.store.rows("task"):
                if self.stop_event.is_set():
                    return
                if row["status"] not in TERMINAL:
                    try:
                        self.refresh(row["id"])
                    except GatewayError:
                        pass  # refresh persists unavailable; never replay a write.
                else:
                    self.notify(row)
            self.stop_event.wait(10)

    def get(self, ident):
        row = self.g.store.get("task", text(ident, 100))
        require(row is not None, "task_not_found", 404)
        return row

    def public(self, row):
        value = {k: v for k, v in row.items() if k not in ("encryptedSpec", "submissionHash")}
        if row["status"] not in TERMINAL and row.get("observation") == "available" and time.time() - row.get("observedAt", 0) > 30:
            value["observation"] = "stale"
        return value

    def call(self, row, action, **extra):
        spec = json.loads(self.g.store.decrypt(row["encryptedSpec"], row["id"]))
        workspace = self.g.workspaces.get(row["workspaceId"])
        argv = ["python3", row["runner"], action]
        if row.get("source") == "external":
            require(action in ("status", "logs"), "external_task_readonly", 409)
            argv = ["python3", "-c", RUNNER, "watch-" + action]
        result = self.g.execute({"target": {"bindingId": row["bindingId"]},
                                 "argv": argv, "timeoutSeconds": 15,
                                 "stdin": json.dumps({**spec, "root": workspace["remoteRoot"],
                                                       "fileRoot": self.g.settings()["fileRoot"], **extra})})
        require(result["outcome"] == "SUCCEEDED" and not result["result"]["truncated"], "task_query_failed", 502)
        try:
            value = json.loads(result["result"]["stdout"])
        except ValueError:
            raise GatewayError("task_query_failed", 502) from None
        require(isinstance(value, dict) and "runnerError" not in value, "task_query_failed", 502)
        return value

    def watch(self, body, submission_id):
        fields(body, ("workspaceId", "statusPath", "stdoutPath", "stderrPath"))
        require(isinstance(submission_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{8,100}", submission_id), "submission_id_required")
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        with self.lock:
            previous = next((t for t in self.g.store.rows("task") if t.get("submissionId") == submission_id), None)
            if previous:
                require(previous["submissionHash"] == digest, "idempotency_conflict", 409)
                return self.public(previous)
            workspace = self.g.workspaces.get(body.get("workspaceId"))
            text(body.get("statusPath"))
            for key in ("statusPath", "stdoutPath", "stderrPath"):
                if key in body:
                    self.g.workspaces.file_path(workspace["id"], body[key])
            ident = "t_" + secrets.token_hex(12)
            row = dict(id=ident, workspaceId=workspace["id"], bindingId=workspace["bindingId"], source="external",
                       mode="background_task", status="pending", verification="not_requested", createdAt=now(),
                       submissionId=submission_id, submissionHash=digest, dispatch="confirmed", runner=None,
                       encryptedSpec=self.g.store.encrypt(json.dumps({**body, "taskId": ident}), ident))
            binding = self.g.store.get("binding", workspace["bindingId"])
            row.update(accountId=binding["accountId"], machineId=binding["machineId"])
            self.g.store.put("task", ident, row)
            return self.refresh(ident)

    def submit(self, body, submission_id):
        with self.lock:
            return self._submit(body, submission_id)

    def _submit(self, body, submission_id):
        fields(body, ("workspaceId", "argv", "preflightArgv", "verifyArgv", "resumeArgv", "cwd", "expectedCommit", "mode"))
        require(body.get("mode", "background_task") == "background_task", "invalid_task_mode")
        require(isinstance(submission_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{8,100}", submission_id), "submission_id_required")
        for key in ("argv", "preflightArgv", "verifyArgv", "resumeArgv"):
            if key in body:
                argv = body[key]
                require(isinstance(argv, list) and 0 < len(argv) <= 1024 and sum(len(text(a, 65536, empty=True).encode()) + 1 for a in argv) < 65536)
                require(bool(argv[0]))
        require("argv" in body)
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        with self.lock:
            previous = next((t for t in self.g.store.rows("task") if t.get("submissionId") == submission_id), None)
            if previous:
                require(previous["submissionHash"] == digest, "idempotency_conflict", 409)
                return self.public(previous)  # GET status to reconcile, never repeat start.
            workspace = self.g.workspaces.get(body.get("workspaceId"))
            probe = self.g.workspaces.probe(workspace, cwd=body.get("cwd", "."))
            if "expectedCommit" in body:
                text(body["expectedCommit"], 64)
                require(probe["git"] and probe["git"]["commit"] == body["expectedCommit"], "workspace_commit_changed", 409)
            ident = "t_" + secrets.token_hex(12)
            runner = workspace["remoteRoot"] + "/.igw/runner-" + hashlib.sha256(RUNNER.encode()).hexdigest()[:16] + ".py"
            spec = {k: v for k, v in body.items() if k not in ("mode", "workspaceId")}
            spec.update(taskId=ident)
            if probe["git"]:
                spec.setdefault("expectedCommit", probe["git"]["commit"])
                if probe["git"]["inputInventoryComplete"]:
                    spec["expectedInputHash"] = hashlib.sha256(json.dumps({"commit": probe["git"]["commit"], "dirtyInputs": probe["git"]["dirtyInputs"]}, sort_keys=True).encode()).hexdigest()
            row = dict(id=ident, workspaceId=workspace["id"], bindingId=workspace["bindingId"],
                       mode="background_task", status="pending", verification="not_requested", createdAt=now(),
                       submissionId=submission_id, submissionHash=digest, runner=runner,
                       observation="available", dispatch="preparing", gitBefore=probe["git"],
                       resumeConfigured="resumeArgv" in spec, verifyConfigured="verifyArgv" in spec,
                       preflight={"passed": True, "checkedAt": now(), "workspace": probe},
                       encryptedSpec=self.g.store.encrypt(json.dumps(spec), ident))
            binding = self.g.store.get("binding", workspace["bindingId"])
            row.update(accountId=binding["accountId"], machineId=binding["machineId"])
            self.g.store.put("task", ident, row)
        try:
            if "preflightArgv" in spec:
                result = self.g.workspaces.execute(workspace["id"], {"argv": spec["preflightArgv"], "cwd": spec.get("cwd", ".")})
                row["preflight"]["exitCode"] = result["result"]["exitCode"]
                row["preflight"]["passed"] = result["outcome"] == "SUCCEEDED" and not result["result"]["truncated"]
                require(row["preflight"]["passed"], "task_preflight_failed", 409)
            self.g.execute({"target": {"bindingId": row["bindingId"]}, "argv": ["python3", "-c",
                            "import pathlib,sys; r=pathlib.Path(sys.argv[1]); p=r/'.igw'; assert p.resolve()==p; p.mkdir(exist_ok=True,mode=0o700)", workspace["remoteRoot"]]})
            self.g.workspaces.probe(workspace, path=runner)
            self.g.file({"bindingId": row["bindingId"]}, runner, RUNNER.encode())
            row.update(dispatch="unknown")
            self.g.store.put("task", ident, row)  # Persist before the non-idempotent request.
            result = self.call(row, "start")
            row.update({k: v for k, v in result.items() if k in ("status", "verification", "phase", "exitCode", "gitBefore", "gitAfter", "startedAt", "finishedAt", "error")})
            row.update(dispatch="confirmed", lastObservedAt=now(), observedAt=time.time())
        except GatewayError as exc:
            row.update(observation="unavailable", error=exc.code)
            if row["dispatch"] == "preparing":
                row.update(status="blocked", dispatch="not_started")
        self.g.store.put("task", ident, row)
        return self.public(row)

    def refresh(self, ident):
        # Serialize short control calls with notifications and cancellation.
        with self.lock:
            row = self.get(ident)
            if row["status"] in TERMINAL:
                self.notify(row)
                return self.public(row)
            try:
                state = self.call(row, "status")
                require(state.get("status") in ("pending", "running", *TERMINAL), "invalid_task_state", 502)
                row.pop("error", None)  # A recovered observation must not retain a transient network failure.
                row.update({k: v for k, v in state.items() if k in ("status", "verification", "verificationEvidence", "phase", "exitCode", "gitBefore", "gitAfter", "startedAt", "finishedAt", "error", "terminationScope", "externalResourcesUnverified")})
                row.update(observation="available", lastObservedAt=now(), observedAt=time.time())
                if state.get("error") != "task_not_found":
                    row["dispatch"] = "confirmed"
            except GatewayError as exc:
                row.update(observation="unavailable", error=exc.code)
            self.g.store.put("task", ident, row)
            self.notify(row)
            return self.public(row)

    def notify(self, row):
        if row["status"] not in TERMINAL:
            return
        with self.lock:
            if self.g.store.get("task_event", row["id"]):
                return
            event = dict(id=row["id"], status=row["status"], createdAt=now(), read=False, desktop="pending")
            self.g.store.put("task_event", row["id"], event)
            event["desktop"] = desktop_notify(row)
            self.g.store.put("task_event", row["id"], event)

    def control(self, ident, action, submission_id=None):
        row = self.get(ident)
        require(row.get("source") != "external", "external_task_readonly", 409)
        if action == "cancel":
            self.call(row, "cancel")
            return self.refresh(ident)
        spec = json.loads(self.g.store.decrypt(row["encryptedSpec"], ident))
        key = "resumeArgv" if action == "resume" else "verifyArgv"
        require(key in spec, "checkpoint_resume_not_configured" if action == "resume" else "verification_not_configured", 409)
        require(row["status"] in TERMINAL, "task_still_running", 409)
        body = dict(workspaceId=row["workspaceId"], argv=spec[key], cwd=spec.get("cwd", "."))
        if action == "verify":
            body.update(argv=["python3", "-c", "pass"], verifyArgv=spec[key])
        if "expectedCommit" in spec:
            body["expectedCommit"] = spec["expectedCommit"]
        if "preflightArgv" in spec:
            body["preflightArgv"] = spec["preflightArgv"]
        if action == "resume" and "verifyArgv" in spec:
            body["verifyArgv"] = spec["verifyArgv"]
        if action == "resume":
            body["resumeArgv"] = spec[key]
        result = self.submit(body, submission_id)
        child = self.get(result["id"])
        child.update(parentTaskId=ident, attempt=row.get("attempt", 1) + 1)
        self.g.store.put("task", child["id"], child)
        return self.public(child)
