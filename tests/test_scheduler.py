"""Offline scheduler tests; no InstaBox credentials or live machines."""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

from instabox_gateway.auth import BrowserAuth
from instabox_gateway.client import main as client_main
from instabox_gateway.errors import GatewayError
from instabox_gateway.gateway import Gateway as RealGateway
from instabox_gateway.http import Handler
from instabox_gateway.scheduler import PROBE, Scheduler
from instabox_gateway.store import Store as RealStore


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

    def _execute(self, target, payload):
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


class ExecutionUpstream:
    """Synthetic upstream; all validation, state and HTTP handling stay real."""
    def __init__(self):
        self.calls = []
        self.commands = []
        self.error = None
        self.result = {"exitCode": 0, "timedOut": False, "truncated": False, "stdout": "ok", "stderr": ""}

    def request(self, key, method, path, **kwargs):
        self.calls.append((method, path))
        machine = {"id": "m_test", "state": "running"}
        if method == "GET" and path == "/v1/me":
            return 200, {"userId": "test-user", "orgId": None, "scopes": ["machines:write"]}
        if method == "GET" and path == "/v1/machines":
            return 200, {"machines": [machine], "nextCursor": None}
        if method == "GET" and path == "/v1/machines/m_test":
            return 200, machine
        if method == "POST" and path == "/v1/machines/m_test/exec":
            payload = kwargs["body"]
            if payload.get("argv") == ["python3", "-c", PROBE]:
                return 200, {**self.result, "exitCode": 0, "timedOut": False,
                             "stdout": json.dumps({"cpuPercent": 2, "memoryAvailablePercent": 80,
                                                   "loadPerCpu": .1, "gpuBusy": None})}
            self.commands.append(payload)
            if self.error:
                raise self.error
            return 200, self.result.copy()
        raise AssertionError((method, path))


class HttpConnection:
    """Feed HTTP bytes to the real Handler without opening a listening socket."""
    def __init__(self, data):
        self.input = io.BytesIO(data)
        self.output = bytearray()

    def makefile(self, *args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)

    def settimeout(self, value):
        pass


class SchedulerExecutionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="igw-scheduler-test-")
        self.addCleanup(directory.cleanup)
        self.store = RealStore(directory.name, "test-only-passphrase", create=True)
        self.addCleanup(self.store.close)
        self.upstream = ExecutionUpstream()
        self.g = RealGateway(self.store, self.upstream)
        account = self.g.save_account({"alias": "test", "apiKey": "ibk_synthetic"})
        self.target = {"accountId": account["id"], "machineId": "m_test"}
        self.upstream.calls.clear()

    def http(self, method, path, body=None):
        data = b"" if body is None else json.dumps(body).encode()
        headers = [f"{method} {path} HTTP/1.0", "Host: 127.0.0.1:18799",
                   "Authorization: Bearer " + self.store.token("execution"),
                   "Content-Type: application/json", f"Content-Length: {len(data)}"]
        connection = HttpConnection(("\r\n".join(headers) + "\r\n\r\n").encode() + data)
        server = SimpleNamespace(gateway=self.g, server_address=("127.0.0.1", 18799), auth=BrowserAuth())
        Handler(connection, ("127.0.0.1", 1), server)
        head, raw = bytes(connection.output).split(b"\r\n\r\n", 1)
        return int(head.split()[1]), json.loads(raw)

    def cli_error(self, status, response):
        error = urllib.error.HTTPError("http://127.0.0.1:18799/api/v1/scheduler/exec", status,
                                       "synthetic", {}, io.BytesIO(json.dumps(response).encode()))
        opener = SimpleNamespace(open=Mock(side_effect=error))
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"IGW_TOKEN": "synthetic-execution-token"}), \
                patch("instabox_gateway.client.urllib.request.build_opener", return_value=opener), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = client_main(["--data-dir", str(self.store.directory), "--url", "http://127.0.0.1:18799",
                                "schedule-exec", "--", "echo", "ok"])
        self.assertEqual(code, 1)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Gateway HTTP " + str(status), stderr.getvalue())
        return json.loads(stderr.getvalue().splitlines()[-1])

    def test_invalid_command_fields_have_no_side_effects(self):
        invalid = [{"cwd": 123}, {"cwd": ""}, {"envs": []}, {"envs": {"bad-name": "value"}},
                   {"envs": {"VALUE": 123}}, {"stdin": {}}, {"stdin": "x" * 1048577},
                   {"argv": []}, {"cmd": "echo ok"}, {"timeoutSeconds": True}, {"extra": True}]
        for changes in invalid:
            for path in ("/api/v1/exec", "/api/v1/scheduler/exec"):
                with self.subTest(fields=list(changes), path=path):
                    body = {"argv": ["echo", "ok"], **changes}
                    if path == "/api/v1/exec":
                        body["target"] = self.target
                    status, reply = self.http("POST", path, body)
                    self.assertEqual(status, 400)
                    self.assertEqual(reply["error"]["code"], "invalid_request")
                    self.assertNotIn("scheduling", reply["error"])
                    self.assertEqual(self.upstream.calls, [])
                    for kind in ("binding", "scheduler_lease", "request"):
                        self.assertEqual(self.store.rows(kind), [])
        status, result = self.http("POST", "/api/v1/scheduler/exec", {"argv": ["echo", "ok"]})
        self.assertEqual(status, 200)
        self.assertTrue(result["scheduling"]["leaseReleased"])
        self.assertEqual(self.upstream.commands, [{"argv": ["echo", "ok"], "timeoutSeconds": 60}])

    def test_valid_options_reach_upstream_for_both_entry_points(self):
        for path in ("/api/v1/exec", "/api/v1/scheduler/exec"):
            for command in ({"argv": ["echo", "ok"]}, {"cmd": "printf ok"}):
                with self.subTest(path=path, command=command):
                    expected = {**command, "cwd": "/data", "envs": {"EMPTY": ""}, "stdin": "", "timeoutSeconds": 30}
                    body = dict(expected)
                    if path == "/api/v1/exec":
                        body["target"] = self.target
                    status, result = self.http("POST", path, body)
                    self.assertEqual(status, 200)
                    self.assertEqual(result["outcome"], "SUCCEEDED")
                    self.assertEqual(self.upstream.commands[-1], expected)
        self.assertEqual(len(self.upstream.commands), 4)

    def test_unknown_command_returns_lease_for_renewal_and_release(self):
        self.upstream.error = GatewayError("upstream_network_error", 502, outcome="UNKNOWN")
        status, reply = self.http("POST", "/api/v1/scheduler/exec", {"argv": ["echo", "ok"]})
        self.assertEqual(status, 502)
        error = reply["error"]
        self.assertEqual(error["code"], "upstream_network_error")
        self.assertEqual(error["outcome"], "UNKNOWN")
        self.assertIsNone(error["upstreamStatus"])
        self.assertEqual(error["requestId"], self.store.rows("request")[0]["id"])
        scheduling = error["scheduling"]
        lease_id = scheduling["leaseId"]
        self.assertEqual(scheduling, {"leaseId": lease_id, "bindingId": self.store.get("scheduler_lease", lease_id)["bindingId"],
                                      "leaseReleased": False})
        self.assertEqual(self.store.get("scheduler_lease", lease_id)["status"], "uncertain")
        self.assertEqual(self.cli_error(status, reply), {k: error[k] for k in ("code", "outcome", "requestId", "scheduling")})
        status, renewed = self.http("POST", "/api/v1/scheduler/renew", {"leaseId": lease_id, "leaseSeconds": 300})
        self.assertEqual(status, 200)
        self.assertEqual(renewed["status"], "uncertain")
        status, unavailable = self.http("POST", "/api/v1/scheduler/allocate", {})
        self.assertEqual((status, unavailable["error"]["code"]), (409, "no_idle_machine"))
        status, inactive = self.http("GET", "/api/v1/connection-info?bindingId=" + scheduling["bindingId"])
        self.assertEqual((status, inactive["error"]["code"]), (409, "scheduler_lease_inactive"))
        status, released = self.http("POST", "/api/v1/scheduler/release", {"leaseId": lease_id})
        self.assertEqual((status, released["status"]), (200, "released"))
        status, allocated = self.http("POST", "/api/v1/scheduler/allocate", {})
        self.assertEqual(status, 201)
        self.assertEqual(allocated["lease"]["machineId"], "m_test")
        self.assertEqual(len(self.upstream.commands), 1)  # Never replay the uncertain command.

    def test_upstream_rejection_preserves_error_contract(self):
        self.upstream.error = GatewayError("rate_limited", 429, upstream_status=429)
        status, reply = self.http("POST", "/api/v1/scheduler/exec", {"argv": ["echo", "ok"]})
        self.assertEqual(status, 429)
        self.assertEqual({k: reply["error"][k] for k in ("code", "outcome", "upstreamStatus")},
                         {"code": "rate_limited", "outcome": "FAILED", "upstreamStatus": 429})
        self.assertFalse(reply["error"]["scheduling"]["leaseReleased"])
        self.assertEqual(len(self.upstream.commands), 1)

    def test_unexpected_error_keeps_lease_without_exposing_exception(self):
        self.upstream.error = RuntimeError("private-test-canary")
        status, reply = self.http("POST", "/api/v1/scheduler/exec", {"argv": ["echo", "ok"]})
        self.assertEqual(status, 500)
        self.assertEqual(reply["error"]["code"], "internal_error")
        self.assertEqual(reply["error"]["outcome"], "UNKNOWN")
        self.assertNotIn("private-test-canary", json.dumps(reply))
        lease = self.store.get("scheduler_lease", reply["error"]["scheduling"]["leaseId"])
        self.assertEqual(lease["status"], "uncertain")
        self.assertEqual(len(self.upstream.commands), 1)

    def test_success_nonzero_and_timeout_keep_existing_lease_behavior(self):
        for exit_code, timed_out, outcome, released in ((0, False, "SUCCEEDED", True),
                (17, False, "NONZERO", True), (124, True, "TIMED_OUT", False)):
            with self.subTest(outcome=outcome):
                self.upstream.result.update(exitCode=exit_code, timedOut=timed_out)
                status, result = self.http("POST", "/api/v1/scheduler/exec", {"argv": ["echo", "ok"]})
                self.assertEqual(status, 200)
                self.assertEqual(result["outcome"], outcome)
                self.assertEqual(result["result"]["exitCode"], exit_code)
                self.assertEqual(result["scheduling"]["leaseReleased"], released)
                lease_id = result["scheduling"]["leaseId"]
                self.assertEqual(self.store.get("scheduler_lease", lease_id)["status"], "released" if released else "uncertain")
                self.http("POST", "/api/v1/scheduler/release", {"leaseId": lease_id})
        self.assertEqual(len(self.upstream.commands), 3)

    def test_plain_execution_error_does_not_gain_lease_metadata(self):
        self.upstream.error = GatewayError("upstream_network_error", 502, outcome="UNKNOWN")
        status, reply = self.http("POST", "/api/v1/exec", {"target": self.target, "argv": ["echo", "ok"]})
        self.assertEqual(status, 502)
        self.assertNotIn("scheduling", reply["error"])
        self.assertEqual(self.store.rows("scheduler_lease"), [])

    def test_cli_only_displays_valid_scheduling_fields(self):
        scheduling = {"leaseId": "l_test", "bindingId": "b_test", "leaseReleased": False}
        base = {"code": "upstream_network_error", "outcome": "UNKNOWN"}
        shown = self.cli_error(502, {"error": {**base, "scheduling": {**scheduling, "private": "private-test-canary"}}})
        self.assertEqual(shown, {**base, "scheduling": scheduling})
        for changes in ({"leaseId": "l_test\nprivate-test-canary"}, {"bindingId": []}, {"leaseReleased": "false"}):
            with self.subTest(fields=list(changes)):
                shown = self.cli_error(502, {"error": {**base, "scheduling": {**scheduling, **changes}}})
                self.assertEqual(shown, base)


if __name__ == "__main__":
    unittest.main()
