"""Opt-in, best-effort idle probes and cross-account scheduling.

A lease only coordinates callers going through this scheduler. It is not an OS
lock and does not stop direct /exec, SSH or upstream activity.
"""
import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor

from .errors import GatewayError
from .gateway import fields, require, text
from .store import now

# Executed on the selected machine via InstaBox's existing /exec API.
# No file writes, machine wake, service installation or credentials.
PROBE = r'''import json, os, subprocess, time

def cpu():
    with open('/proc/stat', encoding='ascii') as f:
        parts = [int(x) for x in f.readline().split()[1:]]
    return sum(parts), parts[3] + (parts[4] if len(parts) > 4 else 0)

first = cpu()
time.sleep(0.3)
last = cpu()
delta = max(1, last[0] - first[0])
cpu_pct = 100.0 * (1.0 - (last[1] - first[1]) / delta)
with open('/proc/meminfo', encoding='ascii') as f:
    memory = {s.split(':')[0]: int(s.split(':')[1].split()[0]) for s in f if ':' in s}
free_pct = 100.0 * memory['MemAvailable'] / max(1, memory['MemTotal'])
cores = max(1, os.cpu_count() or 1)
load = os.getloadavg()[0] / cores
gpu_busy = None
try:
    result = subprocess.run(['nvidia-smi', '--query-compute-apps=pid', '--format=csv,noheader,nounits'],
                            capture_output=True, text=True, timeout=3, check=False)
    if result.returncode == 0:
        gpu_busy = any(line.strip().isdigit() for line in result.stdout.splitlines())
except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
    pass
print(json.dumps({'cpuPercent': round(max(0, min(100, cpu_pct)), 1),
                  'memoryAvailablePercent': round(free_pct, 1),
                  'loadPerCpu': round(load, 3), 'gpuBusy': gpu_busy}))
'''

CPU_MAX = 35.0
MEMORY_AVAILABLE_MIN = 15.0
LOAD_PER_CPU_MAX = 0.75
TERMINAL_TASKS = frozenset(("succeeded", "failed", "cancelled", "blocked"))


class Scheduler:
    def __init__(self, gateway):
        self.g = gateway

    @staticmethod
    def _accounts_filter(account_ids):
        require(account_ids is None or isinstance(account_ids, list)
                and all(isinstance(a, str) and a.startswith('a_') for a in account_ids)
                and len(account_ids) <= 100 and len(set(account_ids)) == len(account_ids),
                "invalid_account_filter")
        return account_ids

    def _active_targets(self):
        """Caller holds the Gateway's shared store lock."""
        tick = time.time()
        busy = set()
        for lease in self.g.store.rows("scheduler_lease"):
            if lease["status"] in ("active", "uncertain") and lease["expiresAt"] > tick:
                busy.add((lease["accountId"], lease["machineId"]))
        for task in self.g.store.rows("task"):
            if task.get("status") not in TERMINAL_TASKS:
                busy.add((task.get("accountId"), task.get("machineId")))
        return busy

    def _probe(self, account_id, machine_id):
        try:
            ctx = self.g.context(account_id)
            with self.g.lock:
                self.g._current(ctx, machine_id)
            _, result = self.g._call(ctx, "POST", "/v1/machines/" + machine_id + "/exec",
                                     body={"argv": ["python3", "-c", PROBE], "timeoutSeconds": 12},
                                     timeout=18)
            require(isinstance(result, dict) and result.get("exitCode") == 0
                    and result.get("timedOut") is False and result.get("truncated") is False
                    and isinstance(result.get("stdout"), str), "idle_probe_failed", 502)
            metrics = json.loads(result["stdout"])
            require(isinstance(metrics, dict), "idle_probe_failed", 502)
            for name in ("cpuPercent", "memoryAvailablePercent", "loadPerCpu"):
                value = metrics.get(name)
                require(type(value) in (int, float) and 0 <= value <= (100 if name != "loadPerCpu" else 1e6),
                        "idle_probe_failed", 502)
            require(metrics.get("gpuBusy") is None or type(metrics["gpuBusy"]) is bool,
                    "idle_probe_failed", 502)
            if metrics["gpuBusy"] is True:
                return {"idle": False, "reason": "gpu_process_active", "metrics": metrics}
            if metrics["cpuPercent"] > CPU_MAX or metrics["loadPerCpu"] > LOAD_PER_CPU_MAX:
                return {"idle": False, "reason": "cpu_busy", "metrics": metrics}
            if metrics["memoryAvailablePercent"] < MEMORY_AVAILABLE_MIN:
                return {"idle": False, "reason": "memory_low", "metrics": metrics}
            return {"idle": True, "reason": "idle", "metrics": metrics}
        except (GatewayError, ValueError, KeyError, TypeError, OverflowError):
            # A missing, malformed, timed-out or unauthorized probe is never idle.
            return {"idle": False, "reason": "probe_unavailable", "metrics": None}

    def resources(self, account_ids=None):
        account_ids = self._accounts_filter(account_ids)
        accounts = {a["id"]: a for a in self.g.accounts()}
        inventory = self.g.inventory()
        resources = []
        probe_jobs = []
        for row in inventory["accounts"]:
            account = accounts.get(row["accountId"])
            if not account or (account_ids is not None and account["id"] not in account_ids):
                continue
            for machine in row["machines"]:
                item = {"accountId": account["id"], "machineId": machine["id"],
                        "accountPriority": account["priority"], "state": machine["state"],
                        "idle": False, "metrics": None, "reason": "not_running"}
                resources.append(item)
                if not row["complete"]:
                    item["reason"] = "inventory_unavailable"
                elif machine["protected"]:
                    item["reason"] = "protected"
                elif not account["enabled"]:
                    item["reason"] = "account_disabled"
                elif account["scopes"] is not None and not {"machines:exec", "machines:write"}.intersection(account["scopes"]):
                    item["reason"] = "exec_scope_missing"
                elif machine["state"] == "running":
                    probe_jobs.append(item)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda m: self._probe(m["accountId"], m["machineId"]), probe_jobs))
        with self.g.lock:
            occupied = self._active_targets()
            for item, probe in zip(probe_jobs, results):
                item.update(probe)
                if (item["accountId"], item["machineId"]) in occupied:
                    item.update(idle=False, reason="reserved_or_task_active")
        return {"resources": resources, "complete": inventory["complete"],
                "policy": {"cpuMaxPercent": CPU_MAX, "memoryAvailableMinPercent": MEMORY_AVAILABLE_MIN,
                           "loadPerCpuMax": LOAD_PER_CPU_MAX}}

    def allocate(self, body):
        fields(body, ("accountIds", "leaseSeconds"))
        account_ids = self._accounts_filter(body.get("accountIds"))
        lease_seconds = body.get("leaseSeconds", 900)
        require(type(lease_seconds) is int and 30 <= lease_seconds <= 3600, "invalid_lease_seconds")
        scanned = self.resources(account_ids)["resources"]
        options = sorted((r for r in scanned if r["idle"]),
                         key=lambda r: (r["accountPriority"], r["metrics"]["cpuPercent"],
                                        r["accountId"], r["machineId"]))
        with self.g.lock:
            occupied = self._active_targets()  # Recheck atomically after potentially slow probes.
            for item in options:
                target = (item["accountId"], item["machineId"])
                if target in occupied:
                    continue
                try:
                    ctx = self.g.context(item["accountId"])
                    self.g._current(ctx, item["machineId"])
                except GatewayError:
                    continue
                binding_id = "b_" + secrets.token_hex(12)
                lease_id = "l_" + secrets.token_hex(12)
                binding = {"id": binding_id, "accountId": ctx.account,
                           "machineId": item["machineId"], "version": ctx.version,
                           "userId": ctx.user, "orgId": ctx.org,
                           "createdAt": now(), "reason": "idle_scheduler"}
                lease = {"id": lease_id, "accountId": ctx.account,
                         "machineId": item["machineId"], "bindingId": binding_id,
                         "createdAt": now(), "expiresAt": time.time() + lease_seconds,
                         "status": "active"}
                self.g.store.put("binding", binding_id, binding)
                self.g.store.put("scheduler_lease", lease_id, lease)
                return {"lease": lease, "binding": binding, "metrics": item["metrics"]}
        raise GatewayError("no_idle_machine", 409)

    def release(self, body):
        fields(body, ("leaseId",))
        lease_id = text(body.get("leaseId"), 100)
        with self.g.lock:
            lease = self.g.store.get("scheduler_lease", lease_id)
            require(lease is not None, "lease_not_found", 404)
            if lease["status"] != "released":
                lease.update(status="released", releasedAt=now())
                self.g.store.put("scheduler_lease", lease_id, lease)
            return lease

    def renew(self, body):
        fields(body, ("leaseId", "leaseSeconds"))
        lease_id = text(body.get("leaseId"), 100)
        seconds = body.get("leaseSeconds")
        require(type(seconds) is int and 30 <= seconds <= 3600, "invalid_lease_seconds")
        with self.g.lock:
            lease = self.g.store.get("scheduler_lease", lease_id)
            require(lease is not None, "lease_not_found", 404)
            require(lease["status"] in ("active", "uncertain") and lease["expiresAt"] > time.time(),
                    "lease_expired", 409)
            lease["expiresAt"] = time.time() + seconds
            self.g.store.put("scheduler_lease", lease_id, lease)
            return lease

    def _uncertain(self, lease_id):
        with self.g.lock:
            lease = self.g.store.get("scheduler_lease", lease_id)
            if lease and lease["status"] == "active":
                lease["status"] = "uncertain"
                self.g.store.put("scheduler_lease", lease_id, lease)

    def execute(self, body):
        fields(body, ("accountIds", "argv", "cmd", "cwd", "envs", "stdin", "timeoutSeconds"))
        timeout = body.get("timeoutSeconds", 60)
        require(type(timeout) is int and 1 <= timeout <= 120, "invalid_request")
        chosen = self.allocate({"accountIds": body.get("accountIds"), "leaseSeconds": max(180, timeout + 60)})
        lease_id = chosen["lease"]["id"]
        request = {k: v for k, v in body.items() if k != "accountIds"}
        request["target"] = {"bindingId": chosen["binding"]["id"]}
        try:
            result = self.g.execute(request)
            if result["outcome"] in ("SUCCEEDED", "NONZERO") and not result["result"]["timedOut"]:
                self.release({"leaseId": lease_id})
                released = True
            else:
                self._uncertain(lease_id)
                released = False
            return {**result, "scheduling": {"leaseId": lease_id,
                    "bindingId": chosen["binding"]["id"],
                    "leaseReleased": released}}
        except Exception:
            # A failed HTTP response cannot prove the remote command never started.
            self._uncertain(lease_id)
            raise
