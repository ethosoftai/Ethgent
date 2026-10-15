"""Running the worker unattended: stalled jobs are recovered, MCP callers approve only what the operator
allows, and telemetry can be exported as JSON lines."""

import _env  # noqa: F401  (her seyden once)

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _env import ROOT

from MarketingApp.environments import agent_job_queue as q
from MarketingApp.environments import approval_runtime as gate

QUEUE_ENV = {"SUPABASE_URL": "http://queue.test", "SUPABASE_AGENT_KEY": "k"}


class _Response:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class StaleRecoveryTests(unittest.TestCase):
    def run_recovery(self, rows, updated=True):
        patches = []

        def fake_patch(url, headers=None, params=None, json=None, timeout=None):
            patches.append((params, json))
            return _Response([{"id": params["id"][3:]}] if updated else [])

        with mock.patch.dict(os.environ, QUEUE_ENV), \
                mock.patch.object(q.requests, "get", return_value=_Response(rows)) as get, \
                mock.patch.object(q.requests, "patch", side_effect=fake_patch):
            outcome = q.recover_stale_jobs(300, 3)
        return outcome, get.call_args.kwargs["params"], patches

    def test_a_queue_job_under_the_limit_is_requeued_and_others_fail(self):
        rows = [
            {"id": "a1", "attempts": 1, "owner_ref": "order-1"},
            {"id": "b2", "attempts": 3, "owner_ref": "order-2"},
            {"id": "c3", "attempts": 1, "owner_ref": "mcp"},
        ]
        outcome, query, patches = self.run_recovery(rows)
        self.assertEqual(outcome, {"requeued": ["a1"], "failed": ["b2", "c3"]})
        self.assertEqual(query["status"], "eq.processing")
        self.assertTrue(query["updated_at"].startswith("lt."))
        by_id = {params["id"]: body for params, body in patches}
        self.assertEqual(by_id["eq.a1"]["status"], "queued")
        self.assertIsNone(by_id["eq.a1"]["started_at"])
        self.assertEqual(by_id["eq.b2"]["status"], "failed")
        self.assertEqual(by_id["eq.c3"]["status"], "failed")
        # The update repeats the condition, so a worker that came back in between keeps its job.
        for params, _ in patches:
            self.assertEqual(params["status"], "eq.processing")
            self.assertTrue(params["updated_at"].startswith("lt."))

    def test_a_job_that_changed_in_between_is_not_counted(self):
        outcome, _, _ = self.run_recovery([{"id": "a1", "attempts": 1, "owner_ref": ""}], updated=False)
        self.assertEqual(outcome, {"requeued": [], "failed": []})

    def test_heartbeat_touches_only_a_running_job(self):
        with mock.patch.dict(os.environ, QUEUE_ENV), \
                mock.patch.object(q.requests, "patch", return_value=_Response([])) as patch:
            q.heartbeat_agent_job("a1")
        params = patch.call_args.kwargs["params"]
        self.assertEqual(params, {"id": "eq.a1", "status": "eq.processing"})
        self.assertIn("updated_at", patch.call_args.kwargs["json"])


class HeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_long_job_keeps_beating_and_stops_when_done(self):
        from MarketingApp import worker

        beats = []
        with mock.patch.dict(os.environ, {"AGENT_WORKER_HEARTBEAT_SECONDS": "0.05"}), \
                mock.patch.object(worker.agent_job_queue, "heartbeat_agent_job", side_effect=beats.append):
            result = await worker._with_heartbeat("job-1", asyncio.sleep(0.23, result="done"))
            count = len(beats)
            await asyncio.sleep(0.15)
        self.assertEqual(result, "done")
        self.assertGreaterEqual(count, 3)
        self.assertEqual(len(beats), count)  # no beats after the job finished
        self.assertEqual(set(beats), {"job-1"})


class _FakeAgent:
    def __init__(self):
        self.answers = []

    async def run(self, task, context=""):
        approved = await gate.request_tool_approval("publish_x_post", "post")
        self.answers.append(approved)
        return type("R", (), {"text": f"approved={approved}", "direct_texts": [], "step_texts": []})()


class McpApprovalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.addCleanup(gate.register_approval_handler, gate.get_registered_approval_handler())
        gate.register_approval_handler(gate.approve_if_granted_by_job)

    async def call(self, approvable, requested):
        from MarketingApp import worker

        agent = _FakeAgent()
        run = worker._build_mcp_run_task(agent)
        env = {"AGENT_WORKER_MCP_APPROVABLE_TOOLS": approvable, "SUPABASE_URL": "", "SUPABASE_AGENT_KEY": "", "SUPABASE_SECRET_KEY": ""}
        with mock.patch.dict(os.environ, env):
            result = await run("post it", "", requested)
        return result["text"]

    async def test_nothing_is_approved_unless_the_operator_allows_it(self):
        self.assertEqual(await self.call("", ["publish_x_post"]), "approved=False")

    async def test_the_caller_gets_only_tools_on_the_operators_list(self):
        self.assertEqual(await self.call("publish_x_post, like_x_post", ["publish_x_post"]), "approved=True")
        self.assertEqual(await self.call("like_x_post", ["publish_x_post"]), "approved=False")
        self.assertEqual(await self.call("publish_x_post", []), "approved=False")

    async def test_the_mcp_tool_passes_approved_tools_through(self):
        from MarketingApp.environments.agent_mcp_server import AgentMcpServer

        seen = []

        async def run_task(task, context, approved_tools):
            seen.append(approved_tools)
            return {"text": "ok"}

        server = AgentMcpServer(run_task)
        result = await server._start_and_wait("t", "", ["publish_x_post"])
        self.assertEqual((result["status"], seen), ("done", [["publish_x_post"]]))


class TelemetryExportTests(unittest.TestCase):
    def test_records_are_written_as_json_lines(self):
        from MarketingApp import telemetry

        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            target = Path(folder) / "events.jsonl"
            with mock.patch.dict(os.environ, {"ETHGENT_TELEMETRY_EXPORT": str(target)}):
                run_id = telemetry.start_run("worker", "a task", job_id="j-1")
                telemetry.record_event("tool", "publish_x_post refused", run_id=run_id)
                telemetry.record_usage("orchestrator", "deepseek-chat", {"prompt_tokens": 10, "completion_tokens": 5}, run_id=run_id)
                telemetry.finish_run(run_id, "ok", duration_ms=12)
            records = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["kind"] for r in records], ["run_start", "event", "llm_usage", "run_end"])
        self.assertEqual(records[0]["job_id"], "j-1")
        self.assertEqual(records[2]["total_tokens"], 15)
        self.assertTrue(all(r["service"] == "ethgent" and r["ts"] for r in records))

    def test_off_by_default_and_never_raises(self):
        from MarketingApp import telemetry

        with mock.patch.dict(os.environ, {"ETHGENT_TELEMETRY_EXPORT": ""}), mock.patch("builtins.open") as opened:
            telemetry._export("event", message="x")
        opened.assert_not_called()
        with mock.patch.dict(os.environ, {"ETHGENT_TELEMETRY_EXPORT": "/proc/not/writable/x.jsonl"}):
            telemetry._export("event", message="x")  # no exception


if __name__ == "__main__":
    unittest.main()
