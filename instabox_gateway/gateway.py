import hashlib
import http.client
import posixpath
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field

from .errors import GatewayError
from .store import now
from .transfer import FileBody


def require(condition, code="invalid_request", status=400):
    if not condition:
        raise GatewayError(code, status)


def text(value, maximum=4096, empty=False):
    require(isinstance(value, str) and (empty or bool(value)) and "\0" not in value)
    try:
        size = len(value.encode())
    except UnicodeEncodeError:
        raise GatewayError("invalid_unicode") from None
    require(size <= maximum)
    return value


def identifier(value, prefix):
    require(isinstance(value, str) and re.fullmatch(prefix + r"[A-Za-z0-9_-]{1,96}", value))
    return value


def fields(value, allowed):
    require(isinstance(value, dict) and not (set(value) - set(allowed)))


@dataclass(frozen=True)
class Context:
    account: str
    version: str
    user: str
    org: str
    key: str = field(repr=False)


class Gateway:
    def __init__(self, store, upstream):
        self.store, self.upstream = store, upstream
        self.lock = store.lock
        store.recover()
        from .workspaces import Workspaces
        from .tasks import Tasks
        self.workspaces = Workspaces(self)
        self.tasks = Tasks(self)
        from .drive import Drive
        self.drive = Drive(self)
        from .files import Files
        self.files = Files(self)

    def accounts(self):
        return [{k: v for k, v in a.items() if k != "encryptedKey"}
                for a in self.store.rows("account")]

    def _account(self, account_id):
        a = self.store.get("account", account_id)
        require(a is not None, "account_not_found", 404)
        return a

    def context(self, account_id):
        with self.lock:
            a = self._account(account_id)
            require(a["enabled"], "account_disabled", 409)
            return Context(a["id"], a["version"], a["userId"], a["orgId"],
                           self.store.decrypt(a["encryptedKey"], a["id"]))

    def _current(self, ctx, machine_id=None):
        a = self._account(ctx.account)
        require(a["enabled"], "account_disabled", 409)
        require(a["version"] == ctx.version, "credential_changed", 409)
        if machine_id:
            require(not self.store.get("protection", machine_id), "machine_protected", 403)

    def _call(self, ctx, method, path, **kwargs):
        return self.upstream.request(ctx.key, method, path, **kwargs)

    def _list(self, ctx):
        machines, ids, cursors, cursor = [], set(), set(), None
        for _ in range(100):
            query = {"limit": 100}
            if cursor:
                query["cursor"] = cursor
            _, data = self._call(ctx, "GET", "/v1/machines", query=query)
            require(isinstance(data.get("machines"), list) and "nextCursor" in data,
                    "invalid_machine_list", 502)
            for m in data["machines"]:
                self._machine_shape(m)
                require(m["id"] not in ids, "duplicate_machine_id", 502)
                ids.add(m["id"])
                machines.append(self._view(m))
            cursor = data["nextCursor"]
            if cursor is None:
                return machines
            require(isinstance(cursor, str) and 0 < len(cursor) <= 4096 and cursor not in cursors,
                    "invalid_pagination", 502)
            cursors.add(cursor)
        raise GatewayError("pagination_limit", 502)

    @staticmethod
    def _machine_shape(m):
        require(isinstance(m, dict) and isinstance(m.get("id"), str)
                and re.fullmatch(r"m_[A-Za-z0-9_-]{1,96}", m["id"])
                and isinstance(m.get("state"), str), "invalid_machine", 502)

    @staticmethod
    def _view(m):
        # Do not expose arbitrary metadata, Secrets, signed URLs or upstream errors.
        return {k: m[k] for k in ("id", "name", "state", "orgId", "operationId") if k in m}

    def save_account(self, body, account_id=None):
        fields(body, ("alias", "apiKey", "priority"))
        alias = text(body.get("alias"), 80)
        key = text(body.get("apiKey"), 256)
        require(re.fullmatch(r"ibk_[A-Za-z0-9_-]+", key))
        priority = body.get("priority", 100)
        require(type(priority) is int and -10000 <= priority <= 10000)
        _, me = self.upstream.request(key, "GET", "/v1/me")
        require(type(me.get("userId")) in (int, str) and str(me["userId"])
                and (me.get("orgId") is None or isinstance(me["orgId"], str)), "invalid_identity", 502)
        user, org = str(me["userId"]), me.get("orgId") or ""
        ident = account_id or "a_" + secrets.token_hex(12)
        version = secrets.token_hex(12)
        ctx = Context(ident, version, user, org, key)
        inventory = self._list(ctx)  # Empty accounts are valid; incomplete lists aren't.
        scopes = me.get("scopes")
        if scopes is not None:
            require(isinstance(scopes, list) and all(isinstance(s, str) for s in scopes), "invalid_identity", 502)
        with self.lock:
            enabled = True
            if account_id:
                old = self._account(account_id)
                require((old["userId"], old["orgId"]) == (user, org), "identity_mismatch", 409)
                enabled = old["enabled"]
            else:
                require(not any((a["userId"], a["orgId"]) == (user, org)
                                for a in self.accounts()), "duplicate_identity", 409)
            record = dict(id=ident, alias=alias, userId=user, orgId=org, version=version,
                          enabled=enabled, priority=priority, scopes=scopes, checkedAt=now(),
                          encryptedKey=self.store.encrypt(key, ident))
            self.store.put("account", ident, record)
            self.store.put("cache", ident, {"machines": inventory, "checkedAt": record["checkedAt"], "version": version})
        return next(a for a in self.accounts() if a["id"] == ident)

    def patch_account(self, ident, body):
        fields(body, ("alias", "enabled", "priority"))
        if "alias" in body:
            text(body["alias"], 80)
        if "enabled" in body:
            require(type(body["enabled"]) is bool)
        if "priority" in body:
            require(type(body["priority"]) is int and -10000 <= body["priority"] <= 10000)
        with self.lock:
            a = self._account(ident)
            a.update(body)
            self.store.put("account", ident, a)
        return next(a for a in self.accounts() if a["id"] == ident)

    def remove_account(self, ident):
        with self.lock:
            self._account(ident)
            self.store.delete("account", ident)
            self.store.delete("cache", ident)
        return {"removed": True}

    def inventory(self, account_id=None):
        accounts = self.accounts()
        if account_id:
            self._account(account_id)
            accounts = [a for a in accounts if a["id"] == account_id]

        def load(a):
            result = {"accountId": a["id"], "alias": a["alias"], "machines": [],
                      "complete": False, "cached": False, "checkedAt": None}
            try:
                ctx = self.context(a["id"])
                machines = self._list(ctx)
                with self.lock:
                    self._current(ctx)
                    timestamp = now()
                    self.store.put("cache", a["id"], {"machines": machines, "checkedAt": timestamp, "version": ctx.version})
                result.update(machines=machines, complete=True, checkedAt=timestamp)
            except GatewayError as exc:
                cached = self.store.get("cache", a["id"])
                if cached:
                    result.update(machines=cached["machines"], checkedAt=cached["checkedAt"], cached=True)
                result["error"] = exc.public()
            result["machines"] = [dict(m, protected=bool(self.store.get("protection", m["id"])))
                                  for m in result["machines"]]
            return result

        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(load, accounts))
        return {"accounts": rows, "complete": all(r["complete"] for r in rows)}

    def detail(self, account_id, machine_id, *, ctx=None):
        identifier(machine_id, "m_")
        ctx = ctx or self.context(account_id)
        _, m = self._call(ctx, "GET", "/v1/machines/" + machine_id)
        self._machine_shape(m)
        require(m["id"] == machine_id and (not ctx.org or m.get("orgId") == ctx.org),
                "machine_context_mismatch", 403)
        return dict(self._view(m), accountId=ctx.account,
                    protected=bool(self.store.get("protection", machine_id)))

    def settings(self, body=None):
        if body is not None:
            fields(body, ("fileRoot", "allowResume"))
            if "fileRoot" in body:
                root = text(body["fileRoot"])
                require(root.startswith("/") and root != "/" and posixpath.normpath(root) == root)
            if "allowResume" in body:
                require(type(body["allowResume"]) is bool)
            with self.lock:
                current = self.store.get("settings", "main")
                current.update(body)
                self.store.put("settings", "main", current)
        return self.store.get("settings", "main")

    def protect(self, machine_id, protected):
        identifier(machine_id, "m_")
        require(type(protected) is bool)
        with self.lock:
            if protected:
                self.store.put("protection", machine_id, {"id": machine_id})
            else:
                self.store.delete("protection", machine_id)
        return {"id": machine_id, "protected": protected}

    def _resolve(self, target):
        fields(target, ("bindingId", "accountId", "machineId"))
        if "bindingId" in target:
            require(set(target) == {"bindingId"})
            binding = self.store.get("binding", text(target["bindingId"], 100))
            require(binding is not None, "binding_not_found", 404)
            ctx = self.context(binding["accountId"])
            require(ctx.version == binding["version"], "binding_invalid", 409)
            machine_id = binding["machineId"]
        else:
            require(set(target) == {"accountId", "machineId"})
            ctx = self.context(text(target["accountId"], 100))
            machine_id = identifier(target["machineId"], "m_")
        with self.lock:
            self._current(ctx, machine_id)
        return ctx, machine_id

    def select(self, body):
        fields(body, ("target", "accountIds", "allowWake"))
        wake = body.get("allowWake", False)
        require(type(wake) is bool)
        if "target" in body:
            require("accountIds" not in body)
            ctx, mid = self._resolve(body["target"])
            machine = self.detail(ctx.account, mid, ctx=ctx)
            require(machine["state"] == "running" or wake and machine["state"] == "suspended",
                    "machine_not_running", 409)
            reason = "explicit"
        else:
            allowed = body.get("accountIds")
            require(allowed is None or isinstance(allowed, list) and all(isinstance(a, str) for a in allowed))
            candidates = []
            accounts = {a["id"]: a for a in self.accounts()}
            for row in self.inventory()["accounts"]:
                a = accounts.get(row["accountId"])
                if not a or not row["complete"] or (allowed is not None and a["id"] not in allowed):
                    continue
                if a["scopes"] is not None and not {"machines:write", "machines:exec"}.intersection(a["scopes"]):
                    continue
                for m in row["machines"]:
                    if not m["protected"] and (m["state"] == "running" or wake and m["state"] == "suspended"):
                        candidates.append((a["priority"], a["id"], m["id"], m))
            require(bool(candidates), "no_eligible_machine", 409)
            # Once a candidate is fixed, preflight/resume errors never cause reselection.
            _, aid, mid, _ = sorted(candidates, key=lambda c: c[:3])[0]
            ctx = self.context(aid)
            machine = self.detail(aid, mid, ctx=ctx)
            reason = "priority_then_account_id_then_machine_id"
        require(machine["state"] == "running" or wake and machine["state"] == "suspended",
                "machine_not_running", 409)
        if machine["state"] != "running":
            require(self.settings()["allowResume"], "resume_not_allowed", 403)
        with self.lock:
            self._current(ctx, mid)
            ident = "b_" + secrets.token_hex(12)
            binding = dict(id=ident, accountId=ctx.account, machineId=mid, version=ctx.version,
                           userId=ctx.user, orgId=ctx.org, createdAt=now(), reason=reason)
            self.store.put("binding", ident, binding)
        result = {"binding": binding, "machine": machine}
        if machine["state"] != "running":
            require(wake, "machine_not_running", 409)
            result["resume"] = self.resume({"bindingId": ident}, "select-" + ident, True)
        cached = self.store.get("connection", ctx.account + ":" + mid)
        result["ssh"] = (cached if cached and cached["version"] == ctx.version
                         and cached["status"] == "available"
                         else self.connection_info({"bindingId": ident}))
        return result

    def connection_info(self, target):
        """Discover metadata only; never create certificates, tokens or sessions."""
        ctx, mid = self._resolve(target)
        result = dict(accountId=ctx.account, machineId=mid, version=ctx.version, checkedAt=now())
        try:
            self.detail(ctx.account, mid, ctx=ctx)
            _, info = self._call(ctx, "GET", "/v1/machines/" + mid + "/connection-info")
            trust = info.get("hostTrust")
            require(info.get("machineId") == mid and info.get("username") == mid
                    and info.get("transport") == "ssh"
                    and isinstance(info.get("host"), str) and 0 < len(info["host"]) <= 253
                    and not any(c.isspace() or ord(c) < 33 for c in info["host"])
                    and type(info.get("port")) is int and 1 <= info["port"] <= 65535
                    and isinstance(trust, dict) and trust.get("type") == "ca"
                    and isinstance(trust.get("publicKey"), str) and 0 < len(trust["publicKey"]) <= 4096
                    and not any(c in trust["publicKey"] for c in "\r\n\0")
                    and isinstance(trust.get("principals"), list)
                    and all(isinstance(p, str) and 0 < len(p) <= 253
                            and not any(c.isspace() or ord(c) < 33 for c in p) for p in trust["principals"])
                    and isinstance(info.get("capabilities"), list)
                    and all(isinstance(c, str) for c in info["capabilities"]),
                    "invalid_connection_info", 502)
            result.update(status="available", transport="ssh", host=info["host"], port=info["port"],
                          username=mid, hostTrust={k: trust[k] for k in ("type", "publicKey", "principals")},
                          capabilities=[c for c in info["capabilities"] if c in ("pty", "exec", "port-forwarding", "mosh")],
                          loginCredentialsAvailable=False)
        except GatewayError as exc:
            result.update(status="unavailable", error=exc.public())
        with self.lock:
            self._current(ctx, mid)
            self.store.put("connection", ctx.account + ":" + mid, result)
        return result

    def _accept(self, ctx, mid, action):
        with self.lock:
            self._current(ctx, mid)
            ident = "r_" + secrets.token_hex(12)
            row = dict(id=ident, action=action, accountId=ctx.account, machineId=mid,
                       version=ctx.version, outcome="ACCEPTED", acceptedAt=now())
            self.store.put("request", ident, row)
            return row

    def _finish(self, row, outcome, **extra):
        row.update(outcome=outcome, finishedAt=now(), **extra)
        self.store.put("request", row["id"], row)

    def _running(self, target):
        ctx, mid = self._resolve(target)
        m = self.detail(ctx.account, mid, ctx=ctx)
        require(m["state"] == "running", "machine_not_running", 409)
        return ctx, mid

    def execute(self, body):
        fields(body, ("target", "argv", "cmd", "cwd", "envs", "stdin", "timeoutSeconds"))
        require(("argv" in body) != ("cmd" in body))
        if "argv" in body:
            argv = body["argv"]
            require(isinstance(argv, list) and 0 < len(argv) <= 4096)
            for arg in argv:
                text(arg, 65536, empty=True)
            require(bool(argv[0]) and sum(len(a.encode()) + 1 for a in argv) <= 65536)
        else:
            text(body["cmd"], 65536)
        if "cwd" in body:
            text(body["cwd"])
        if "stdin" in body:
            text(body["stdin"], 1048576, empty=True)
        if "envs" in body:
            require(isinstance(body["envs"], dict) and len(body["envs"]) <= 256)
            for k, v in body["envs"].items():
                require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k))
                text(v, 65536, empty=True)
        timeout = body.get("timeoutSeconds", 60)
        require(type(timeout) is int and 1 <= timeout <= 120)
        payload = {k: v for k, v in body.items() if k != "target"}
        payload["timeoutSeconds"] = timeout
        ctx, mid = self._running(body.get("target"))
        row = self._accept(ctx, mid, "exec")
        try:
            _, result = self._call(ctx, "POST", "/v1/machines/" + mid + "/exec", body=payload, timeout=timeout + 15)
            valid = (type(result.get("exitCode")) is int
                     and all(type(result.get(k)) is bool for k in ("timedOut", "truncated"))
                     and all(isinstance(result.get(k), str) for k in ("stdout", "stderr"))
                     and ("signal" not in result or isinstance(result["signal"], str)))
            require(valid, "invalid_exec_result", 502)
            result = {k: result[k] for k in ("exitCode", "timedOut", "truncated", "stdout", "stderr", "signal") if k in result}
            outcome = "TIMED_OUT" if result["timedOut"] else "SUCCEEDED" if result["exitCode"] == 0 else "NONZERO"
            self._finish(row, outcome, exitCode=result["exitCode"], timedOut=result["timedOut"], truncated=result["truncated"])
            return {"requestId": row["id"], "accountId": ctx.account, "machineId": mid,
                    "outcome": outcome, "result": result, "terminationGuaranteed": False}
        except GatewayError as exc:
            exc.request_id = row["id"]
            if exc.code == "invalid_exec_result":
                exc.outcome = "UNKNOWN"
            self._finish(row, exc.outcome, error=exc.code)
            raise

    def file_path(self, value):
        value = text(value)
        root = self.settings()["fileRoot"]
        require(value.startswith(root + "/") and posixpath.normpath(value) == value
                and not any(p in (".", "..", "") for p in value[1:].split("/")), "file_path_denied", 403)
        return value

    def file(self, target, path, data=None):
        path = self.file_path(path)
        require(data is None or isinstance(data, bytes))
        ctx, mid = self._running(target)
        upload = data is not None
        row = self._accept(ctx, mid, "upload" if upload else "download")
        try:
            status, result = self._call(ctx, "PUT" if upload else "GET", "/v1/machines/" + mid +
                                       ("/files" if upload else "/files/content"),
                                       query={"path": path, **({"parents": "true"} if upload else {})},
                                       body=data, binary=not upload, timeout=60)
            if upload:
                require(type(result.get("created")) is bool and type(result.get("size")) is int
                        and result["created"] == (status == 201) and result["size"] == len(data)
                        and result.get("sha256") == hashlib.sha256(data).hexdigest(), "invalid_upload_result", 502)
                result = {k: result[k] for k in ("created", "size", "sha256")}
            self._finish(row, "SUCCEEDED")
            return status, result, row["id"]
        except GatewayError as exc:
            exc.request_id = row["id"]
            if upload and exc.code == "invalid_upload_result":
                exc.outcome = "UNKNOWN"
            self._finish(row, exc.outcome, error=exc.code)
            raise

    def upload(self, target, path, source, length):
        path = self.file_path(path)
        ctx, mid = self._running(target)
        row = self._accept(ctx, mid, "upload")
        body = source if isinstance(source, FileBody) else FileBody(source, length)
        try:
            status, result = self._call(ctx, "PUT", "/v1/machines/" + mid + "/files",
                                        query={"path": path, "parents": "true"}, body=body, timeout=60)
            require(body.transferred == length and type(result.get("created")) is bool
                    and result["created"] == (status == 201) and type(result.get("size")) is int and result["size"] == length
                    and result.get("sha256") == body.sha256.hexdigest(), "invalid_upload_result", 502)
            self._finish(row, "SUCCEEDED", bytesTransferred=body.transferred)
            return status, {k: result[k] for k in ("created", "size", "sha256")}, row["id"]
        except GatewayError as exc:
            if exc.code == "invalid_upload_result":
                exc.outcome = "UNKNOWN"
            exc.request_id = row["id"]
            self._finish(row, exc.outcome, bytesTransferred=body.transferred, error=exc.code)
            raise

    def download(self, target, path):
        return self._download(target, self.file_path(path))

    @contextmanager
    def _download(self, target, path):
        """Open a bounded-memory download; the HTTP writer accounts for delivered bytes."""
        ctx, mid = self._running(target)
        row = self._accept(ctx, mid, "download")
        row["bytesTransferred"] = 0
        try:
            _, response = self._call(ctx, "GET", "/v1/machines/" + mid + "/files/content",
                                     query={"path": path}, binary=True, stream=True, timeout=60)
            with response:
                row["expectedBytes"] = int(response.headers["Content-Length"])
                yield response, row
                require(row["bytesTransferred"] == row["expectedBytes"], "incomplete_download", 502)
            self._finish(row, "SUCCEEDED")
        except GatewayError as exc:
            exc.request_id = row["id"]
            self._finish(row, "FAILED", error=exc.code)
            raise
        except (OSError, http.client.HTTPException):
            self._finish(row, "FAILED", error="download_interrupted")
            exc = GatewayError("download_interrupted", 502)
            exc.request_id = row["id"]
            raise exc from None
        except Exception:
            self._finish(row, "FAILED", error="download_failed")
            raise

    def resume(self, target, idempotency, allow_wake):
        require(allow_wake is True and self.settings()["allowResume"], "resume_not_allowed", 403)
        require(isinstance(idempotency, str) and re.fullmatch(r"[A-Za-z0-9_-]{8,128}", idempotency))
        ctx, mid = self._resolve(target)
        with self.lock:
            previous = self.store.get("resume", idempotency)
            if previous:
                require((previous["accountId"], previous["machineId"], previous["version"]) ==
                        (ctx.account, mid, ctx.version), "idempotency_conflict", 409)
                return previous  # Even UNKNOWN is never blindly re-sent.
        machine = self.detail(ctx.account, mid, ctx=ctx)
        require(machine["state"] == "suspended", "machine_not_suspended", 409)
        with self.lock:
            self._current(ctx, mid)
            previous = self.store.get("resume", idempotency)
            if previous:
                require((previous["accountId"], previous["machineId"], previous["version"]) ==
                        (ctx.account, mid, ctx.version), "idempotency_conflict", 409)
                return previous
            row = dict(id=idempotency, accountId=ctx.account, machineId=mid, version=ctx.version,
                       state="SENDING", operationId=None, createdAt=now(),
                       upstreamKey="igw-" + secrets.token_hex(16))
            self.store.put("resume", idempotency, row)
            audit = self._accept(ctx, mid, "resume")
            row["requestId"] = audit["id"]
            self.store.put("resume", idempotency, row)
        try:
            _, op = self._call(ctx, "POST", "/v1/machines/" + mid + "/resume",
                               body={"vault": False}, idempotency=row["upstreamKey"])
            self._validate_operation(op, mid)
            row.update(state=op["state"], operationId=op["id"])
            self.store.put("resume", idempotency, row)
            outcome = {"succeeded": "SUCCEEDED", "failed": "FAILED"}.get(op["state"], "PENDING")
            self._finish(audit, outcome, operationId=op["id"])
        except GatewayError as exc:
            row.update(state="UNKNOWN" if exc.code == "invalid_operation" else exc.outcome, error=exc.code)
            self._finish(audit, row["state"], error=exc.code)
        self.store.put("resume", idempotency, row)
        return row

    @staticmethod
    def _validate_operation(op, mid):
        require(isinstance(op.get("id"), str) and re.fullmatch(r"op_[A-Za-z0-9_-]{1,96}", op["id"])
                and op.get("machineId") == mid and op.get("type") == "resume"
                and op.get("state") in ("queued", "running", "succeeded", "failed"), "invalid_operation", 502)

    def operation(self, ident):
        row = self.store.get("resume", ident)
        require(row is not None, "resume_not_found", 404)
        ctx, mid = self._resolve({"accountId": row["accountId"], "machineId": row["machineId"]})
        require(ctx.version == row["version"], "credential_changed", 409)
        if row["operationId"]:
            _, op = self._call(ctx, "GET", "/v1/operations/" + row["operationId"])
            self._validate_operation(op, mid)
            require(op["id"] == row["operationId"], "operation_mismatch", 502)
            row.update(state=op["state"], checkedAt=now())
            self.store.put("resume", ident, row)
            if op["state"] in ("succeeded", "failed") and row.get("requestId"):
                audit = self.store.get("request", row["requestId"])
                if audit:
                    self._finish(audit, "SUCCEEDED" if op["state"] == "succeeded" else "FAILED")
        return row

    def requests(self):
        return sorted(self.store.rows("request"), key=lambda r: r["acceptedAt"], reverse=True)[:100]
