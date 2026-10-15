"""Ethgent'in kendi yazma tool'lari (post, yanit, begeni, takip, yorum, abonelik) da onay kapisindan gecer.

Model bu tool'lari cagirdiginda tool calismadan once kayitli isleyiciye sorulur: terminalde insana,
gomulu API'de varsayilan olarak "hayir", kuyruk worker'inda isi baslatanin listesine.
"""

import _env  # noqa: F401  (her seyden once)

import inspect
import tempfile
import unittest
from pathlib import Path

from _env import ROOT

from MarketingApp.environments import approval_runtime as ar


def _fake(name, calls):
    def tool(tweet_url: str, message: str = "x"):
        """Dis dunyaya yazar."""
        calls.append((tweet_url, message))
        return {"status": "done"}

    tool.__name__ = name
    return tool


class ListTests(unittest.TestCase):
    def test_every_listed_name_is_a_real_builtin_tool(self):
        from MarketingApp.llms import agent_studio

        builtin = {f.__name__ for funcs in agent_studio._builtin_tool_groups().values() for f in funcs}
        self.assertEqual(ar.BUILTIN_EXTERNAL_WRITE_TOOLS - builtin, set())

    def test_drafts_reads_and_private_actions_are_not_listed(self):
        for name in ("publish_x_post_with_media", "bookmark_x_post", "inspect_x_post", "search_x_posts"):
            self.assertNotIn(name, ar.BUILTIN_EXTERNAL_WRITE_TOOLS)


class LoadedToolTests(unittest.TestCase):
    def setUp(self):
        from MarketingApp.llms import agent_studio

        self.tools = agent_studio.load_available_tools(include_custom=False)["tools"]

    def test_write_tools_come_out_gated_with_their_signature(self):
        from MarketingApp import araclar

        for name in ar.BUILTIN_EXTERNAL_WRITE_TOOLS:
            func = self.tools[name]
            self.assertTrue(getattr(func, "ethgent_requires_approval", False), name)
            self.assertTrue(inspect.iscoroutinefunction(func), name)
            self.assertEqual(
                list(inspect.signature(func).parameters),
                list(inspect.signature(getattr(araclar, name)).parameters),
                name,
            )

    def test_other_tools_are_untouched(self):
        from MarketingApp import araclar

        for name in ("inspect_x_post", "bookmark_x_post", "workspace_oku", "publish_x_post_with_media"):
            self.assertIs(self.tools[name], getattr(araclar, name), name)


class GateTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        ar.register_approval_handler(None)

    async def test_refused_call_never_runs_and_the_person_sees_the_arguments(self):
        calls, asked = [], []

        async def no(action_id, description):
            asked.append((action_id, description))
            return False

        ar.register_approval_handler(no)
        gated = ar.gate_builtin_tool(_fake("reply_to_x_post", calls))
        result = await gated("https://x.com/a/status/1", message="merhaba")
        self.assertIn("Onay yok", result)
        self.assertEqual(calls, [])
        self.assertEqual(asked[0][0], "reply_to_x_post")
        self.assertIn("merhaba", asked[0][1])

    async def test_embedded_default_and_no_handler_both_refuse(self):
        calls = []
        gated = ar.gate_builtin_tool(_fake("publish_x_post", calls))
        ar.register_approval_handler(None)
        self.assertIn("Onay yok", await gated("t"))
        ar.register_approval_handler(ar.reject_all_approvals)
        self.assertIn("Onay yok", await gated("t"))
        self.assertEqual(calls, [])

    async def test_a_queued_job_approves_only_what_it_lists(self):
        calls = []
        gated = ar.gate_builtin_tool(_fake("like_x_post", calls))
        ar.register_approval_handler(ar.approve_if_granted_by_job)
        with ar.job_approvals(["like_x_post"]):
            self.assertEqual(await gated("u"), {"status": "done"})
        with ar.job_approvals(["publish_x_post"]):
            self.assertIn("Onay yok", await gated("u"))
        self.assertEqual(calls, [("u", "x")])

    async def test_not_wrapped_twice_and_unlisted_names_pass_through(self):
        calls = []
        once = ar.gate_builtin_tool(_fake("follow_x_account", calls))
        self.assertIs(ar.gate_builtin_tool(once), once)
        plain = _fake("inspect_x_post", calls)
        self.assertIs(ar.gate_builtin_tool(plain), plain)


class SubModelWiringTests(unittest.IsolatedAsyncioTestCase):
    """Gercek kurulumda sosyal medya ajaninin elindeki tool kapili olandir."""

    def tearDown(self):
        ar.register_approval_handler(None)

    async def test_the_social_agent_holds_the_gated_tools(self):
        from MarketingApp.llms.BaseModel import BaseModel

        base = BaseModel(api_key="k", model="m")
        social = next(sm for sm in base.submodels if sm.name == "sosyal_medya_agent")
        for name in ("publish_x_post", "like_x_post", "reply_to_x_post"):
            if name in social._tool_map:
                self.assertTrue(getattr(social._tool_map[name], "ethgent_requires_approval", False), name)
        self.assertIn("publish_x_post", social._tool_map)

        async def no(action_id, description):
            return False

        ar.register_approval_handler(no)
        result = await social._execute_tool("publish_x_post", {"text": "merhaba"})
        self.assertIn("Onay yok", result)


class TerminalSessionTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        ar.register_approval_handler(None)

    async def test_o_approves_this_tool_for_the_rest_of_the_session(self):
        from MarketingApp.environments.terminal import TerminalManager
        from MarketingApp.llms.BaseModel import BaseModel

        base = BaseModel(api_key="k", model="m")
        replies = iter(["o", "h"])
        prompts = []

        def answer(prompt):
            prompts.append(prompt)
            return next(replies)

        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            term = TerminalManager(base, input_func=answer, output_func=lambda _: None,
                                   history_file=str(Path(folder) / "h.json"))
            base.request_approval = term._request_terminal_approval
            self.assertTrue(await ar.request_tool_approval("like_x_post", "d"))
            self.assertTrue(await ar.request_tool_approval("like_x_post", "d"))  # not asked again
            self.assertEqual(len(prompts), 1)
            self.assertFalse(await ar.request_tool_approval("publish_x_post", "d"))  # other tools still ask
            self.assertEqual(len(prompts), 2)


if __name__ == "__main__":
    unittest.main()
