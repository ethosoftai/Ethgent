"""Onay kapisi (environments/approval_runtime.py): araclar/ icindeki duz fonksiyonlarin onay istemesi.

Kapi tek bir yerden gecer: kayitli islemci. Terminalde insana sorar, bassiz baglamda reddeder, ve bir isi
baslatan taraf onceden onayladiysa (payload.approved_tools) sadece o araca izin verir.
"""

import _env  # noqa: F401  (her seyden once)

import asyncio
import tempfile
import unittest
from pathlib import Path

from _env import ROOT

from MarketingApp.environments import approval_runtime as ar


def run(coro):
    return asyncio.run(coro)


class RegistryTests(unittest.TestCase):
    def tearDown(self):
        ar.register_approval_handler(None)

    def test_no_handler_means_no_approval(self):
        ar.register_approval_handler(None)
        self.assertFalse(run(ar.request_tool_approval("worker_video_yayinla", "d")))

    def test_the_headless_handler_refuses_everything(self):
        ar.register_approval_handler(ar.reject_all_approvals)
        self.assertFalse(run(ar.request_tool_approval("anything", "d")))

    def test_the_registered_handler_gets_the_action_and_decides(self):
        seen = []

        async def handler(action_id, description):
            seen.append((action_id, description))
            return action_id == "ok_tool"

        ar.register_approval_handler(handler)
        self.assertTrue(run(ar.request_tool_approval("ok_tool", "run it")))
        self.assertFalse(run(ar.request_tool_approval("other", "run it")))
        self.assertEqual(seen, [("ok_tool", "run it"), ("other", "run it")])


class JobApprovalTests(unittest.TestCase):
    """Onay, isi baslatan taraftan gelir; model ya da bir gorev metni bunu veremez."""

    def tearDown(self):
        ar.register_approval_handler(None)

    def test_only_a_short_list_of_sensible_strings_survives(self):
        self.assertEqual(ar.clean_approved_tools(["a", " b ", "", 3, None, "x" * 129]), frozenset({"a", "b"}))
        for junk in (None, "report_and_send", {"a": 1}, 5):
            self.assertEqual(ar.clean_approved_tools(junk), frozenset(), junk)
        self.assertEqual(len(ar.clean_approved_tools([f"t{i}" for i in range(100)])), ar.MAX_JOB_APPROVALS)

    def test_listed_tools_only_and_only_during_the_job(self):
        async def go():
            with ar.job_approvals(["report_and_send"]):
                inside = (await ar.approve_if_granted_by_job("report_and_send", "d"),
                          await ar.approve_if_granted_by_job("other_tool", "d"))
            return inside, await ar.approve_if_granted_by_job("report_and_send", "d")

        inside, after = run(go())
        self.assertEqual(inside, (True, False))
        self.assertFalse(after)

    def test_concurrent_jobs_do_not_share_approvals(self):
        async def ask(name, approved):
            with ar.job_approvals(approved):
                await asyncio.sleep(0.01)
                return await asyncio.create_task(ar.approve_if_granted_by_job(name, "d"))

        async def go():
            return await asyncio.gather(ask("a", ["a"]), ask("a", ["b"]), ask("b", ["b"]))

        self.assertEqual(run(go()), [True, False, True])


class BaseModelWiringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ar.register_approval_handler(None)
        from MarketingApp.llms.BaseModel import BaseModel

        self.base = BaseModel(api_key="k", model="m")

    def tearDown(self):
        ar.register_approval_handler(None)

    async def test_building_a_basemodel_registers_a_gate_that_follows_request_approval(self):
        self.assertIsNotNone(ar.get_registered_approval_handler())
        answers = []

        async def decide(action_id, description):
            answers.append(action_id)
            return True

        self.base.request_approval = decide  # what the terminal does: swap the method at run time
        self.assertTrue(await ar.request_tool_approval("worker_video_yayinla", "d"))

        async def refuse(action_id, description):
            return False

        self.base.request_approval = refuse  # swapped again: the gate must read the new one
        self.assertFalse(await ar.request_tool_approval("worker_video_yayinla", "d"))
        self.assertEqual(answers, ["worker_video_yayinla"])

    async def test_the_terminal_asks_the_person_and_only_yes_approves(self):
        from MarketingApp.environments.terminal import TerminalManager

        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            for reply, expected in (("e", True), ("evet", True), ("y", True), ("", False), ("hayir", False), ("h", False)):
                said = []
                term = TerminalManager(self.base, input_func=lambda prompt, r=reply: r,
                                       output_func=said.append, history_file=str(Path(folder) / "h.json"))
                self.base.request_approval = term._request_terminal_approval  # what TerminalManager.run() does
                got = await ar.request_tool_approval("worker_video_yayinla", "Videoyu yayinla")
                self.assertEqual(got, expected, reply)
                self.assertTrue(any("Videoyu yayinla" in line for line in said), "the person must be shown what is asked")


class AgentApiTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        ar.register_approval_handler(None)

    async def test_an_embedded_agent_refuses_by_default_and_takes_its_own_handler(self):
        from MarketingApp.agent_api import EthgentAgent

        EthgentAgent(api_key="k", model="m")
        self.assertFalse(await ar.request_tool_approval("worker_video_yayinla", "d"))

        async def mine(action_id, description):
            return action_id == "worker_video_yayinla"

        EthgentAgent(api_key="k", model="m", approval_handler=mine)
        self.assertTrue(await ar.request_tool_approval("worker_video_yayinla", "d"))
        self.assertFalse(await ar.request_tool_approval("other", "d"))


if __name__ == "__main__":
    unittest.main()
