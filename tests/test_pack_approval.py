"""Pack'in `requires_approval` diye isaretledigi tool'lar: onayi tool degil, ajan (host) sorar.

Onay yoksa sandbox'a hicbir sey gitmez; onay varsa tool calisir. `--trust` ile ana process'te
calisan bir pack tool'u da ayni kurala uyar.
"""

import _env  # noqa: F401  (her seyden once)

import asyncio
import inspect
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support as S
from _env import ROOT

import yaml

from MarketingApp.environments import approval_runtime as gate
from MarketingApp.sandbox import executor

PLUGIN = """name: odeme_paketi
version: 0.1.0
type: tool_pack
description: test
tools:
  - name: odeme_gonder
    file: tools/odeme.py
    requires_approval: true
  - name: bakiye_oku
    file: tools/odeme.py
  - name: fabrika_akisi
    file: tools/fabrika.py
"""

TOOLS = '''
def odeme_gonder(alici: str, tutar: int = 1):
    """Para gonderir (dis dunyayi degistirir)."""
    return f"gonderildi:{alici}:{tutar}"


def bakiye_oku():
    """Bakiyeyi okur."""
    return 100
'''

# The workflow factory's gated tools ask the agent app's gate from inside their own code.
FACTORY_TOOL = '''
def _host_gate():
    try:
        from MarketingApp.environments.approval_runtime import request_tool_approval
    except Exception:
        return None
    return request_tool_approval


async def fabrika_akisi(metin: str = "x"):
    """Fabrikadan gelen, kendi icinde onay soran akis."""
    gate = _host_gate()
    if gate is None or not await gate("fabrika_akisi", "dis dunyaya yazar"):
        return "reddedildi"
    return "yazildi:" + metin
'''


class _Pack(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        S.reset_state()
        self.addCleanup(S.reset_state)
        self.addCleanup(S.patch_model_env())
        from MarketingApp.llms import agent_studio

        self.studio = agent_studio
        self.root = Path(tempfile.mkdtemp(dir=ROOT))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.pack = self.root / "odeme_paketi"
        (self.pack / "tools").mkdir(parents=True)
        (self.pack / "plugin.yaml").write_text(PLUGIN, encoding="utf-8")
        (self.pack / "tools" / "odeme.py").write_text(TOOLS, encoding="utf-8")
        (self.pack / "tools" / "fabrika.py").write_text(FACTORY_TOOL, encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"ETHGENT_SANDBOX": "subprocess"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(gate.register_approval_handler, gate.get_registered_approval_handler())
        self.asked = []

    def install(self, **kwargs):
        self.studio.install_agent_pack(str(self.pack), **kwargs)
        return self.studio.load_custom_tools()["tools"]

    def entry(self, name):
        return next(e for e in self.studio.load_custom_tools_config()["custom_tools"] if e["name"] == name)

    def answer(self, approved):
        async def handler(action_id, description):
            self.asked.append((action_id, description))
            return approved

        gate.register_approval_handler(handler)


class SandboxedApprovalTests(_Pack):
    async def test_the_declaration_is_recorded(self):
        self.install()
        self.assertTrue(self.entry("odeme_gonder")["requires_approval"])
        self.assertFalse(self.entry("bakiye_oku")["requires_approval"])

    async def test_without_approval_nothing_reaches_the_sandbox(self):
        tools = self.install()
        self.answer(False)
        with mock.patch.object(executor, "run_tool", wraps=executor.run_tool) as run:
            result = await tools["odeme_gonder"]("ayse", tutar=5)
        self.assertIn("Onay yok", result)
        run.assert_not_called()
        self.assertEqual(self.asked[0][0], "odeme_gonder")

    async def test_with_no_gate_registered_it_refuses(self):
        tools = self.install()
        gate.register_approval_handler(None)
        self.assertIn("Onay yok", await tools["odeme_gonder"]("ayse"))

    async def test_approved_call_runs_in_the_sandbox_and_the_person_sees_what_runs(self):
        tools = self.install()
        self.answer(True)
        self.assertEqual(await tools["odeme_gonder"]("ayse", tutar=5), "gonderildi:ayse:5")
        action, description = self.asked[0]
        self.assertEqual(action, "odeme_gonder")
        self.assertIn('"alici": "ayse"', description)
        self.assertIn('"tutar": 5', description)

    async def test_undeclared_tools_do_not_ask(self):
        tools = self.install()
        self.answer(False)
        self.assertEqual(tools["bakiye_oku"](), 100)
        self.assertEqual(self.asked, [])

    async def test_a_queued_job_approves_only_the_tools_it_lists(self):
        tools = self.install()
        gate.register_approval_handler(gate.approve_if_granted_by_job)
        with gate.job_approvals(["odeme_gonder"]):
            self.assertEqual(await tools["odeme_gonder"]("ali"), "gonderildi:ali:1")
        with gate.job_approvals(["baska_bir_tool"]):
            self.assertIn("Onay yok", await tools["odeme_gonder"]("ali"))

    async def test_factory_tool_inside_the_sandbox_gets_the_hosts_answer(self):
        tools = self.install()
        self.answer(True)
        # Not declared in plugin.yaml: the host does not ask, so the gate inside the sandbox says no.
        self.assertEqual(tools["fabrika_akisi"]("a"), "reddedildi")
        self.assertEqual(self.asked, [])

    async def test_declared_factory_tool_runs_only_when_the_host_approves(self):
        plugin = yaml.safe_load(PLUGIN)
        plugin["tools"][2]["requires_approval"] = True
        (self.pack / "plugin.yaml").write_text(yaml.safe_dump(plugin), encoding="utf-8")
        tools = self.install()
        self.answer(False)
        self.assertIn("Onay yok", await tools["fabrika_akisi"]("a"))
        self.answer(True)
        self.assertEqual(await tools["fabrika_akisi"]("a"), "yazildi:a")


class TrustedApprovalTests(_Pack):
    async def test_trusted_pack_tools_are_gated_too(self):
        tools = self.install(trusted=True)
        func = tools["odeme_gonder"]
        self.assertFalse(getattr(func, "ethgent_sandboxed", False))
        self.assertTrue(inspect.iscoroutinefunction(func))
        self.answer(False)
        self.assertIn("Onay yok", await func("ayse"))
        self.answer(True)
        self.assertEqual(await func("ayse", 3), "gonderildi:ayse:3")
        self.assertEqual(list(inspect.signature(func).parameters), ["alici", "tutar"])


class ExportTests(_Pack):
    async def test_export_keeps_the_declaration(self):
        self.install()
        out = self.root / "geri"
        self.studio.export_agent_pack("geri_paket", out_dir=str(out), tools=["odeme_gonder", "bakiye_oku"])
        exported = yaml.safe_load((out / "plugin.yaml").read_text(encoding="utf-8"))
        flags = {item["name"]: item.get("requires_approval", False) for item in exported["tools"]}
        self.assertEqual(flags, {"odeme_gonder": True, "bakiye_oku": False})


class SubModelLoopTests(_Pack):
    async def test_a_sub_agent_calls_a_gated_sandboxed_tool_through_its_tool_loop(self):
        from MarketingApp.llms.SubModels.base import SubModel

        tools = self.install()
        holder = type("Holder", (), {})()
        holder.name = "odeme_ajani"
        holder._tool_map = {"odeme_gonder": tools["odeme_gonder"]}
        holder._auto_log_context_action = lambda *a, **k: None
        schema = SubModel._build_tool_schemas(type("T", (), {"tools": [tools["odeme_gonder"]]})())[0]["function"]
        self.assertEqual(schema["parameters"]["required"], ["alici"])

        self.answer(True)
        result = await SubModel._execute_tool(holder, "odeme_gonder", {"alici": "can", "tutar": 2})
        self.assertEqual(result, "gonderildi:can:2")
        self.answer(False)
        result = await SubModel._execute_tool(holder, "odeme_gonder", {"alici": "can"})
        self.assertIn("Onay yok", result)


if __name__ == "__main__":
    unittest.main()
