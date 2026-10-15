"""
Agent MCP Server — EthgentAgent'i istek/yanit (on-demand) olarak disariya acan
Streamable HTTP MCP sunucusu. `mcp_worker` ile ayni protokol/transport'u
(POST /mcp, `Authorization: Bearer <token>`) kullanir, ama Kara Tahta yerine
dogrudan bu ajani (Ethgent) calistirir.

Uzun surebilecek gorevler icin (orn. browser_agent tool'u sinirsiz suredir,
bkz. BaseModel._get_tool_timeout_seconds) `mcp_worker`'in
generate_lesson_video / check_lesson_status deseni tekrarlanir:
`run_marketing_task` en fazla `AGENT_WORKER_WAIT_MS` kadar bekler; is
bitmezse arka planda calismaya devam eder ve `{status:"running", jobId}`
doner, `check_marketing_task` ile sonradan sorgulanir. Is bellekte (process
icinde) izlenir -- worker yeniden baslarsa yarim kalan jobId'ler kaybolur,
ama gorevin kendisi `agent_jobs` tablosuna da yaziliyorsa (bkz. worker.py)
o tablo kalici audit-trail'dir.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from mcp.server.mcpserver import MCPServer

_DEFAULT_WAIT_MS = 45000
_JOB_RETENTION_SECONDS = 3600  # bellekteki bitmis is kayitlarini bu sureden sonra unut

RunTaskFn = Callable[[str, str], Awaitable[dict[str, Any]]]


@dataclass
class _TrackedJob:
    status: str = "running"  # running | done | failed
    result: dict[str, Any] | None = None
    error: str | None = None
    finished_at: float | None = None


class AgentMcpServer:
    """EthgentAgent'i saran, tool cagrilarini calistiran MCP sunucusu."""

    def __init__(self, run_task: RunTaskFn):
        """
        Args:
            run_task: `async def(task: str, context: str) -> dict` — gorevi
                gercekten calistiran fonksiyon (bkz. worker.py'deki
                `_build_mcp_run_task`; AutomationCoordinator'i ve varsa
                agent_jobs audit-trail'ini o katman yonetir).
        """
        self._run_task = run_task
        self._jobs: dict[str, _TrackedJob] = {}
        self.server = MCPServer(
            name="marketing-agent-worker",
            instructions=(
                "Ethgent marketing-agent'ini gorev/gorev+baglam olarak calistirir. "
                "run_marketing_task ile bir gorev baslat; hemen bitmezse donen "
                "jobId ile check_marketing_task kullanarak sonucu sorgula."
            ),
        )
        self._register_tools()

    def _register_tools(self) -> None:
        @self.server.tool()
        async def run_marketing_task(task: str, context: str = "") -> dict[str, Any]:
            """
            Ethgent marketing-agent'ine dogal dilde bir gorev gonderir ve calistirir.
            Kisa gorevlerde dogrudan sonucu (status: done) dondurur; gorev
            AGENT_WORKER_WAIT_MS (varsayilan 45sn) icinde bitmezse
            {status: "running", jobId} doner ve gorev arka planda calismaya
            devam eder — sonucu check_marketing_task(job_id) ile al.

            Args:
                task: Calistirilacak gorev (dogal dil).
                context: Modelin gorev oncesi gorecegi ek baglam (opsiyonel).
            """
            return await self._start_and_wait(task, context)

        @self.server.tool()
        async def check_marketing_task(job_id: str) -> dict[str, Any]:
            """
            run_marketing_task'in dondurdugu jobId icin guncel durumu/sonucu sorgular.

            Args:
                job_id: run_marketing_task'ten donen jobId.
            """
            return self._check(job_id)

    async def _start_and_wait(self, task: str, context: str) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        wait_ms = max(1000, int(os.getenv("AGENT_WORKER_WAIT_MS") or _DEFAULT_WAIT_MS))

        asyncio_task = asyncio.create_task(self._run_task(task, context))
        self._jobs[job_id] = _TrackedJob()
        asyncio_task.add_done_callback(lambda t: self._record_finished(job_id, t))

        try:
            result = await asyncio.wait_for(asyncio.shield(asyncio_task), timeout=wait_ms / 1000)
        except asyncio.TimeoutError:
            return {"status": "running", "jobId": job_id}
        except Exception as exc:
            return {"status": "failed", "jobId": job_id, "error": str(exc)}

        return {"status": "done", "jobId": job_id, **(result or {})}

    def _record_finished(self, job_id: str, task_obj: "asyncio.Task") -> None:
        tracked = self._jobs.get(job_id)
        if tracked is None:
            return
        tracked.finished_at = time.monotonic()
        try:
            tracked.result = task_obj.result()
            tracked.status = "done"
        except asyncio.CancelledError:
            tracked.status = "failed"
            tracked.error = "Gorev iptal edildi."
        except Exception as exc:
            tracked.status = "failed"
            tracked.error = str(exc)
        self._prune_finished_jobs()

    def _check(self, job_id: str) -> dict[str, Any]:
        tracked = self._jobs.get(job_id)
        if tracked is None:
            return {
                "status": "unknown",
                "jobId": job_id,
                "error": "Bilinmeyen jobId (worker yeniden baslatilmis olabilir).",
            }
        if tracked.status == "running":
            return {"status": "running", "jobId": job_id}
        if tracked.status == "done":
            return {"status": "done", "jobId": job_id, **(tracked.result or {})}
        return {"status": "failed", "jobId": job_id, "error": tracked.error or "Bilinmeyen hata."}

    def _prune_finished_jobs(self) -> None:
        now = time.monotonic()
        stale = [
            jid
            for jid, job in self._jobs.items()
            if job.status != "running" and job.finished_at and now - job.finished_at > _JOB_RETENTION_SECONDS
        ]
        for jid in stale:
            self._jobs.pop(jid, None)

    def asgi_app(self):
        return self.server.streamable_http_app(stateless_http=True)


def build_auth_app(inner_app, token: str):
    """`inner_app`'i basit bir Bearer-token kontroluyle sarar.

    mcp_worker (server.js) ile ayni davranis: token tanimsizsa TUM istekler
    500 alir (sunucu ayaktadir ama yapilandirilmamistir); token yanlis/eksikse
    401 doner.
    """

    async def app(scope, receive, send):
        if scope["type"] != "http":
            await inner_app(scope, receive, send)
            return

        if not token:
            await send(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"error":"AGENT_WORKER_MCP_TOKEN tanimli degil."}',
                }
            )
            return

        headers = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization", b"").decode("latin-1")
        if auth_header != f"Bearer {token}":
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"error":"Gecersiz veya eksik Authorization: Bearer <token>."}',
                }
            )
            return

        await inner_app(scope, receive, send)

    return app
