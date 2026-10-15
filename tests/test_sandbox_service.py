"""Sandbox service (ETHGENT_SANDBOX=remote): an agent that cannot start containers sends pack tools to it.

The service runs each call as a limited subprocess with only the variables the agent sent, behind a
token. The agent's side keeps the same rules as the local modes: reserved names are withheld before
anything is sent, and a tool that needs approval is not sent without it.
"""

import _env  # noqa: F401  (her seyden once)

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from _env import ROOT

from MarketingApp.environments import approval_runtime as gate
from MarketingApp.sandbox import executor, proxy, service

TOOL = '''
import os
import time


def echo(text: str, times: int = 1):
    """Repeats text."""
    return text * times


def env_seen(name: str):
    """Returns one variable, as the tool sees it."""
    return os.environ.get(name)


def snooze(seconds: float = 30):
    """Sleeps."""
    time.sleep(seconds)
    return "woke"
'''


class _Service(unittest.TestCase):
    token = "test-token-123"

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=ROOT))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.folder, ignore_errors=True))
        self.tool = self.folder / "tools_demo.py"
        self.tool.write_text(TOOL, encoding="utf-8")
        conf = {**executor.settings(), "timeout": 4.0, "memory_mb": 512}
        self.server = service.build_server("127.0.0.1", 0, self.token, max_concurrent=2, conf=conf)
        self.calls = 0
        original = service.run_request

        def counting(request, conf):
            self.calls += 1
            return original(request, conf)

        patcher = mock.patch.object(service, "run_request", counting)
        patcher.start()
        self.addCleanup(patcher.stop)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        host, port = self.server.server_address[:2]
        self.url = f"http://{host}:{port}"
        self.conf = {**executor.settings(), "mode": "remote", "url": self.url, "token": self.token,
                     "timeout": 4.0, "startup_grace": 10.0}

    def post(self, body, token=None):
        data = json.dumps(body).encode()
        request = urllib.request.Request(self.url + "/run", data=data, method="POST",
                                         headers={"Authorization": f"Bearer {token or self.token}"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class RemoteRunTests(_Service):
    def test_a_call_runs_in_the_service_and_comes_back(self):
        out = executor.run_tool(self.tool, "echo", {"times": 3}, positional=["ab"], conf=self.conf)
        self.assertEqual((out["ok"], out["result"], out["mode"]), (True, "ababab", "remote"))
        self.assertEqual(self.calls, 1)

    def test_only_declared_values_travel_and_reserved_names_stay_home(self):
        with mock.patch.dict(os.environ, {"WEATHER_KEY": "w-1", "OPENAI_API_KEY": "sk-agent"}):
            seen = executor.run_tool(self.tool, "env_seen", {"name": "WEATHER_KEY"}, ["WEATHER_KEY"], {}, conf=self.conf)
            key = executor.run_tool(self.tool, "env_seen", {"name": "OPENAI_API_KEY"}, ["OPENAI_API_KEY"], {}, conf=self.conf)
            undeclared = executor.run_tool(self.tool, "env_seen", {"name": "WEATHER_KEY"}, [], {}, conf=self.conf)
        self.assertEqual(seen["result"], "w-1")
        self.assertIsNone(key["result"])
        self.assertEqual(key["withheld_env"], ["OPENAI_API_KEY"])
        self.assertIsNone(undeclared["result"])

    def test_the_services_own_environment_is_not_visible(self):
        with mock.patch.dict(os.environ, {"SERVICE_ONLY_SECRET": "s"}):
            status, out = self.post({"source": TOOL, "filename": "t.py", "function": "env_seen",
                                     "args": {"name": "SERVICE_ONLY_SECRET"}})
        self.assertEqual((status, out["ok"], out["result"]), (200, True, None))

    def test_the_services_time_limit_applies(self):
        out = executor.run_tool(self.tool, "snooze", {"seconds": 30}, conf=self.conf)
        self.assertFalse(out["ok"])
        self.assertTrue("timed out" in out["error"] or "wall-clock" in out["error"], out["error"])

    def test_auto_picks_remote_when_a_url_is_set(self):
        self.assertEqual(executor.resolve_mode({**self.conf, "mode": "auto"}), "remote")
        with self.assertRaises(executor.SandboxError):
            executor.resolve_mode({**self.conf, "mode": "remote", "url": ""})


class ServiceGuardTests(_Service):
    def test_wrong_or_missing_token_is_refused(self):
        status, out = self.post({"source": TOOL, "filename": "t.py", "function": "echo", "args": {"text": "a"}}, token="nope")
        self.assertEqual(status, 401)
        out = executor.run_tool(self.tool, "echo", {"text": "a"}, conf={**self.conf, "token": "nope"})
        self.assertFalse(out["ok"])
        self.assertIn("401", out["error"])
        self.assertEqual(self.calls, 0)

    def test_bad_requests_are_refused_before_anything_runs(self):
        for body in (
            {"source": TOOL, "filename": "../escape.py", "function": "echo"},
            {"source": TOOL, "filename": "t.py", "function": "os.system"},
            {"source": "", "filename": "t.py", "function": "echo"},
            {"source": TOOL, "filename": "t.py", "function": "echo", "env": {"A": 1}},
            ["not", "an", "object"],
        ):
            status, _ = self.post(body)
            self.assertEqual(status, 400, body)
        self.assertEqual(self.calls, 0)

    def test_health_needs_no_token(self):
        with urllib.request.urlopen(self.url + "/health", timeout=10) as response:
            self.assertEqual(json.loads(response.read()), {"ok": True})

    def test_no_token_no_service(self):
        with mock.patch.dict(os.environ, {"ETHGENT_SANDBOX_TOKEN": ""}):
            self.assertEqual(service.main(), 2)
        with self.assertRaises(ValueError):
            service.build_server("127.0.0.1", 0, "")


class ProxyTests(_Service):
    def test_an_unreachable_service_is_a_readable_error_not_a_crash(self):
        with mock.patch.dict(os.environ, {"ETHGENT_SANDBOX": "remote", "ETHGENT_SANDBOX_URL": "http://127.0.0.1:9",
                                          "ETHGENT_SANDBOX_TOKEN": "x"}):
            tool = proxy.make_sandboxed_tool("echo", self.tool)
            self.assertIn("[Sandbox hata]", tool("a"))


class ApprovalTests(_Service, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _Service.setUp(self)
        self.addCleanup(gate.register_approval_handler, gate.get_registered_approval_handler())
        patcher = mock.patch.dict(os.environ, {"ETHGENT_SANDBOX": "remote", "ETHGENT_SANDBOX_URL": self.url,
                                               "ETHGENT_SANDBOX_TOKEN": self.token})
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_without_approval_nothing_is_sent(self):
        tool = proxy.make_sandboxed_tool("echo", self.tool, requires_approval=True)

        async def no(action_id, description):
            return False

        gate.register_approval_handler(no)
        self.assertIn("Onay yok", await tool("a"))
        self.assertEqual(self.calls, 0)

        async def yes(action_id, description):
            return True

        gate.register_approval_handler(yes)
        self.assertEqual(await tool("a", times=2), "aa")
        self.assertEqual(self.calls, 1)


class DependencyPathTests(unittest.TestCase):
    def test_pythonpath_folders_reach_the_runner(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            with mock.patch.dict(os.environ, {"PYTHONPATH": os.pathsep.join([folder, "/does/not/exist"])}):
                paths = executor._dependency_paths()
        self.assertIn(folder, paths)
        self.assertNotIn("/does/not/exist", paths)


if __name__ == "__main__":
    unittest.main()
