"""Onay isi baslatandan gelir: payload.approved_tools -> approve_if_granted_by_job."""

import _env  # noqa: F401  (her seyden once)

import asyncio
import unittest
from unittest import mock

from MarketingApp.environments import approval_runtime as ar


def run(coro):
    return asyncio.run(coro)


class CleanTests(unittest.TestCase):
    def test_only_a_short_list_of_sensible_strings_survives(self):
        self.assertEqual(ar.clean_approved_tools(["a", " b ", "", 3, None, "x" * 129]), frozenset({"a", "b"}))
        for junk in (None, "report_and_send", {"a": 1}, 5):
            self.assertEqual(ar.clean_approved_tools(junk), frozenset(), junk)
        many = [f"tool_{i}" for i in range(100)]
        self.assertEqual(len(ar.clean_approved_tools(many)), ar.MAX_JOB_APPROVALS)


class GateTests(unittest.TestCase):
    def test_nothing_is_approved_outside_a_job(self):
        self.assertFalse(run(ar.approve_if_granted_by_job("report_and_send", "d")))

    def test_only_the_listed_tools_are_approved_and_only_during_the_job(self):
        async def go():
            with ar.job_approvals(["report_and_send"]):
                inside = (await ar.approve_if_granted_by_job("report_and_send", "d"),
                          await ar.approve_if_granted_by_job("other_tool", "d"))
            after = await ar.approve_if_granted_by_job("report_and_send", "d")
            return inside, after
        inside, after = run(go())
        self.assertEqual(inside, (True, False))
        self.assertFalse(after)

    def test_a_garbage_list_approves_nothing(self):
        async def go():
            with ar.job_approvals("report_and_send"):  # a string, not a list
                return await ar.approve_if_granted_by_job("report_and_send", "d")
        self.assertFalse(run(go()))

    def test_tasks_started_inside_a_job_see_it_and_concurrent_jobs_do_not_mix(self):
        async def ask(name, approved):
            with ar.job_approvals(approved):
                await asyncio.sleep(0.01)
                child = asyncio.create_task(ar.approve_if_granted_by_job(name, "d"))
                return await child

        async def go():
            return await asyncio.gather(ask("a", ["a"]), ask("a", ["b"]), ask("b", ["b"]))
        self.assertEqual(run(go()), [True, False, True])

    def test_the_model_cannot_widen_it_through_the_gate_registry(self):
        # request_tool_approval goes to whatever handler the process registered
        async def go():
            ar.register_approval_handler(ar.approve_if_granted_by_job)
            try:
                with ar.job_approvals(["a"]):
                    return await ar.request_tool_approval("a", "d"), await ar.request_tool_approval("b", "d")
            finally:
                ar.register_approval_handler(None)
        self.assertEqual(run(go()), (True, False))


class WorkerLoopTests(unittest.TestCase):
    def test_the_poll_loop_runs_each_job_with_its_own_approvals(self):
        from MarketingApp import worker

        jobs = [
            {"id": "j1", "payload": {"task": "t1", "approved_tools": ["report_and_send"]}},
            {"id": "j2", "payload": {"task": "t2"}},
            {"id": "j3", "payload": {"task": "t3", "approved_tools": ["something_else"]}},
        ]
        seen, finished = {}, []

        async def fake_execute(agent, task, context, *, job_id, label):
            seen[job_id] = await ar.approve_if_granted_by_job("report_and_send", "d")
            return {"text": "ok"}

        def finish(job_id, result):
            finished.append(job_id)
            if len(finished) == len(jobs):
                raise asyncio.CancelledError  # the loop never ends by itself

        queue = iter(jobs)
        with mock.patch.object(worker.agent_job_queue, "is_configured", return_value=True), \
             mock.patch.object(worker.agent_job_queue, "claim_next_agent_job", side_effect=lambda: next(queue)), \
             mock.patch.object(worker.agent_job_queue, "finish_agent_job", side_effect=finish), \
             mock.patch.object(worker, "_execute_task_with_busy_retry", fake_execute):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(worker._queue_poll_loop(object(), 1000))
        self.assertEqual(seen, {"j1": True, "j2": False, "j3": False})


if __name__ == "__main__":
    unittest.main()
