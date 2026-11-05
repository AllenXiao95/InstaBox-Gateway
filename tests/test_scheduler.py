"""Offline scheduler tests; no InstaBox credentials or live machines."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from instabox_gateway.errors import GatewayError
from instabox_gateway.scheduler import Scheduler


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = {}

    def rows(self, kind):
        return list(self.data.get(kind, {}).values())

    def get(self, kind, ident):
        return self.data.get(kind, {}).get(ident)

    def put(self, kind, ident, value):
        self.data.setdefault(kind, {})[ident] = value.copy()


class Gateway:
    def __init__(self):
        self.store = Store()
        self.lock = self.store.lock
        self.account_rows = [dict(id="a_one", priority=5, enabled=True, scopes=None),
                             dict(id="a_two", priority=10, enabled=True, scopes=None)]
        self.machine_rows = [dict(accountId="a_one", complete=True, machines=[
                                 dict(id="m_one", state="running", protected=False),
                                 dict(id="m_two", state="running", protected=False)]),
                             dict(accountId="a_two", complete=True, machines=[
                                 dict(id="m_three", state="running", protected=False)])]
        self.exec_result = {"outcome": "SUCCEEDED", "result": {"timedOut": False}}

    def accounts(self):
        return self.account_rows

    def inventory(self):
        return {"accounts": self.machine_rows, "complete": True}

    def context(self, ident):
        return SimpleNamespace(account=ident, version="v1", user="u", org="o")

    def _current(self, ctx, machine_id):
        return None

    def execute(self, request):
        return self.exec_result


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.g = Gateway()
        self.s = Scheduler(self.g)
        self.probe = patch.object(Scheduler, "_probe", return_value={
            "idle": True, "reason": "idle", "metrics": {
                "cpuPercent": 5, "loadPerCpu": .1, "memoryAvailablePercent": 75, "gpuBusy": None}})
        self.probe.start()
        self.addCleanup(self.probe.stop)

    def test_account_priority_and_secondary_selection(self):
        first = self.s.allocate({})
        self.assertEqual(first["lease"]["accountId"], "a_one")
        self.assertEqual(first["lease"]["machineId"], "m_one")
        second = self.s.allocate({})
        self.assertEqual(second["lease"]["machineId"], "m_two")
        third = self.s.allocate({})
        self.assertEqual(third["lease"]["accountId"], "a_two")
        with self.assertRaises(GatewayError) as error:
            self.s.allocate({})
        self.assertEqual(error.exception.code, "no_idle_machine")

    def test_concurrent_allocations_are_distinct(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.s.allocate({}), range(2)))
        self.assertEqual(len({r["lease"]["machineId"] for r in results}), 2)

    def test_account_filter_and_release(self):
        lease = self.s.allocate({"accountIds": ["a_two"]})["lease"]
        self.assertEqual(lease["machineId"], "m_three")
        self.s.release({"leaseId": lease["id"]})
        self.assertEqual(self.s.allocate({"accountIds": ["a_two"]})["lease"]["machineId"], "m_three")

    def test_busy_task_and_protected_machine(self):
        self.g.store.put("task", "t_1", {"accountId": "a_one", "machineId": "m_one", "status": "running"})
        self.g.machine_rows[0]["machines"][1]["protected"] = True
        chosen = self.s.allocate({})
        self.assertEqual(chosen["lease"]["machineId"], "m_three")
        resources = self.s.resources()["resources"]
        self.assertEqual(resources[0]["reason"], "reserved_or_task_active")
        self.assertEqual(resources[1]["reason"], "protected")

    def test_running_not_equivalent_to_idle(self):
        self.probe.stop()
        with patch.object(Scheduler, "_probe", return_value={"idle": False, "reason": "cpu_busy", "metrics": None}):
            with self.assertRaises(GatewayError):
                self.s.allocate({})

    def test_expired_lease_and_renewal(self):
        lease = self.s.allocate({})["lease"]
        renewed = self.s.renew({"leaseId": lease["id"], "leaseSeconds": 1200})
        self.assertGreater(renewed["expiresAt"], lease["expiresAt"])
        renewed["expiresAt"] = 0
        self.g.store.put("scheduler_lease", lease["id"], renewed)
        with self.assertRaises(GatewayError):
            self.s.renew({"leaseId": lease["id"], "leaseSeconds": 1200})
        self.assertEqual(self.s.allocate({})["lease"]["machineId"], "m_one")

    def test_exec_releases_on_success_but_not_timeout(self):
        result = self.s.execute({"argv": ["echo", "hi"]})
        self.assertTrue(result["scheduling"]["leaseReleased"])
        self.assertEqual(self.s.allocate({})["lease"]["machineId"], "m_one")
        self.g.exec_result = {"outcome": "TIMED_OUT", "result": {"timedOut": True}}
        result = self.s.execute({"argv": ["sleep", "10"]})
        self.assertFalse(result["scheduling"]["leaseReleased"])
        self.assertEqual(self.g.store.get("scheduler_lease", result["scheduling"]["leaseId"])["status"], "uncertain")

    def test_invalid_exec_does_not_claim_lease(self):
        with self.assertRaises(GatewayError):
            self.s.execute({"argv": []})
        self.assertEqual(self.g.store.rows("scheduler_lease"), [])

    def test_probe_rejects_unknown_and_detects_gpu_process(self):
        self.probe.stop()
        self.g._call = lambda *args, **kwargs: (200, {"exitCode": 0, "timedOut": False,
                             "truncated": False, "stdout": '{"cpuPercent":2,"loadPerCpu":0.1,"memoryAvailablePercent":80,"gpuBusy":true}'})
        self.assertEqual(self.s._probe("a_one", "m_one")["reason"], "gpu_process_active")
        self.g._call = lambda *args, **kwargs: (200, {"exitCode": 0, "timedOut": False,
                             "truncated": False, "stdout": 'not json'})
        self.assertEqual(self.s._probe("a_one", "m_one")["reason"], "probe_unavailable")

    def test_scheduler_binding_requires_active_lease(self):
        from instabox_gateway.gateway import Gateway as RealGateway
        allocated = self.s.allocate({})
        binding = allocated["binding"]
        resolver = RealGateway.__new__(RealGateway)
        resolver.store = self.g.store
        resolver.lock = self.g.lock
        resolver.context = self.g.context
        resolver._current = self.g._current
        self.assertEqual(resolver._resolve({"bindingId": binding["id"]})[1], binding["machineId"])
        self.s.release({"leaseId": allocated["lease"]["id"]})
        with self.assertRaises(GatewayError) as error:
            resolver._resolve({"bindingId": binding["id"]})
        self.assertEqual(error.exception.code, "scheduler_lease_inactive")

    def test_failed_inventory_not_scheduled(self):
        self.g.machine_rows[0]["complete"] = False
        self.assertEqual(self.s.allocate({})["lease"]["accountId"], "a_two")


if __name__ == "__main__":
    unittest.main()
