"""
MarketingApp Worker — Ethgent'i "worker" olarak calistiran giris noktasi.

`python -m MarketingApp.main` (terminal.py) Ethgent'i interaktif bir insan
oturumu olarak ayaga kaldirir. Bu modul onun yaninda, insansiz calisan iki
farkli giris noktasini BIRLIKTE baslatir:

  1. Queue-poller: `agent_jobs` Supabase tablosunu (bkz.
     migrations/001_agent_jobs.sql) periyodik olarak yoklar, 'queued'
     satirlari claim edip EthgentAgent.run() ile isler, sonucu tabloya yazar.
     App / scheduler_worker / baska bir sistem sadece satir eklemekle gorevi
     kuyruga atmis olur — social-media-worker'in publish_jobs'u nasil
     calisiyorsa aynen oyle (ayni Supabase projesi, SUPABASE_URL /
     SUPABASE_SECRET_KEY).
  2. MCP sunucusu (Streamable HTTP, POST /mcp): mcp_worker ile ayni protokol
     ve auth deseniyle (Authorization: Bearer <AGENT_WORKER_MCP_TOKEN>) ayni
     ajani senkron/on-demand cagirmak isteyen herhangi bir MCP client'a acar.

Ikisi de AYNI tek EthgentAgent ornegini paylasir (agent_api.py'nin "tek
process / tek workspace" kisitiyla tutarli) ve AutomationCoordinator ile
serilestirilir — boylece queue-poller ile MCP cagrisi ayni anda
BaseModel/browser oturumuna dokunamaz.

BILINEN SINIR: Bu koordinasyon sadece BU process icindir. Ayni workspace_dir
ile ayni anda `python -m MarketingApp.main` (terminal/heartbeat/telegram)
calistirmak desteklenmez — iki ayri process, iki ayri AutomationCoordinator
ornegi demektir ve paylasimli browser oturumunu koruyamazlar (agent_api.py
"tek process" notuyla ayni kisit). Uretimde ya terminal ya worker calistirin.

Kullanim:
    python -m MarketingApp.worker
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

import uvicorn
from dotenv import load_dotenv

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.append(_PROJECT_ROOT)

load_dotenv(dotenv_path=os.path.join(_PROJECT_ROOT, ".env"))
load_dotenv(dotenv_path=os.path.join(_PROJECT_ROOT, ".env.local"), override=True)
load_dotenv(dotenv_path=os.path.join(_PROJECT_ROOT, ".env.model"), override=True)
load_dotenv(dotenv_path=os.path.join(_PROJECT_ROOT, ".env.secrets"), override=True)

from MarketingApp.agent_api import EthgentAgent
from MarketingApp.environments.approval_runtime import approve_if_granted_by_job, job_approvals
from MarketingApp.environments import agent_job_queue
from MarketingApp.environments.agent_mcp_server import AgentMcpServer, build_auth_app
from MarketingApp.environments.automation_runtime import (
    release_automation,
    try_acquire_automation,
)

_DEFAULT_POLL_MS = 4000
_DEFAULT_MCP_HOST = "127.0.0.1"
_DEFAULT_MCP_PORT = 8091
_BUSY_RETRY_ATTEMPTS = 5
_BUSY_RETRY_DELAY_SECONDS = 2.0


class AutomationBusyError(RuntimeError):
    """AutomationCoordinator baska bir sahiple mesgulken firlatilir."""


def _agent_result_to_dict(result) -> dict:
    return {
        "text": result.text,
        "direct_texts": result.direct_texts,
        "step_texts": result.step_texts,
    }


async def _execute_task(
    ethgent_agent: EthgentAgent,
    task: str,
    context: str,
    *,
    owner: str,
    job_id: str = "",
    label: str = "",
) -> dict:
    """AutomationCoordinator ile serilestirilmis tek bir gorev calistirir."""
    acquired, snapshot = await try_acquire_automation(
        owner, job_id=job_id, label=label or task[:80], source=owner
    )
    if not acquired:
        busy_owner = snapshot.get("owner") or "bilinmeyen"
        busy_label = snapshot.get("label") or snapshot.get("job_id") or "aktif gorev"
        raise AutomationBusyError(f"Otomasyon mesgul: {busy_owner} / {busy_label}")
    try:
        result = await ethgent_agent.run(task, context=context)
        return _agent_result_to_dict(result)
    finally:
        await release_automation(owner, job_id=job_id)


async def _execute_task_with_busy_retry(ethgent_agent: EthgentAgent, task: str, context: str, *, job_id: str, label: str) -> dict:
    """Queue-poller icin: MCP cagrisi mesgulse birkac kez kisa aralikla yeniden dener."""
    last_exc: Exception | None = None
    for attempt in range(1, _BUSY_RETRY_ATTEMPTS + 1):
        try:
            return await _execute_task(
                ethgent_agent, task, context, owner="agent_worker_queue", job_id=job_id, label=label
            )
        except AutomationBusyError as exc:
            last_exc = exc
            if attempt < _BUSY_RETRY_ATTEMPTS:
                await asyncio.sleep(_BUSY_RETRY_DELAY_SECONDS)
    raise last_exc  # type: ignore[misc]


async def _queue_poll_loop(ethgent_agent: EthgentAgent, poll_ms: int) -> None:
    if not agent_job_queue.is_configured():
        print("ℹ️ [Worker] SUPABASE_URL/SUPABASE_SECRET_KEY tanimsiz; agent_jobs kuyrugu dinlenmiyor.")
        return

    print(f"📥 [Worker] agent_jobs kuyrugu dinleniyor ({poll_ms}ms araliklarla).")
    while True:
        try:
            job = await asyncio.to_thread(agent_job_queue.claim_next_agent_job)
        except agent_job_queue.AgentJobQueueError as exc:
            print(f"⚠️ [Worker] Kuyruk yoklama hatasi: {exc}")
            job = None

        if not job:
            await asyncio.sleep(poll_ms / 1000)
            continue

        job_id = str(job.get("id"))
        payload = job.get("payload") or {}
        task = str(payload.get("task") or "").strip()
        context = str(payload.get("context") or "")
        owner_ref = str(job.get("owner_ref") or "")

        print(f"📥 [Worker] Is alindi: {job_id} ({task[:80]!r})")
        if not task:
            await asyncio.to_thread(agent_job_queue.fail_agent_job, job_id, "payload.task bos olamaz.")
            continue

        try:
            # Onay isi baslatandan gelir (payload.approved_tools), modelden degil.
            with job_approvals(payload.get("approved_tools")):
                result = await _execute_task_with_busy_retry(ethgent_agent, task, context, job_id=job_id, label=owner_ref)
            await asyncio.to_thread(agent_job_queue.finish_agent_job, job_id, result)
            print(f"✅ [Worker] Is tamamlandi: {job_id}")
        except AutomationBusyError as exc:
            # Tum retry denemeleri de mesgul bulduysa: basarisiz sayma, tekrar
            # kuyruga koy — sonraki poll'de (ya da baska bir replica) yeniden denensin.
            print(f"⏳ [Worker] Is tekrar kuyruga alindi (mesgul): {job_id} -> {exc}")
            await asyncio.to_thread(agent_job_queue.requeue_agent_job, job_id)
        except Exception as exc:
            await asyncio.to_thread(agent_job_queue.fail_agent_job, job_id, str(exc))
            print(f"❌ [Worker] Is hatasi: {job_id} -> {exc}")


def _build_mcp_run_task(ethgent_agent: EthgentAgent):
    async def _mcp_run_task(task: str, context: str) -> dict:
        job_row = None
        if agent_job_queue.is_configured():
            try:
                job_row = await asyncio.to_thread(
                    agent_job_queue.insert_agent_job,
                    {"task": task, "context": context},
                    owner_ref="mcp",
                    status="processing",
                    # Her MCP cagrisi ayri bir olaydir (retry yok, bkz.
                    # insert_agent_job docstring'i) -- ayni task/context'le
                    # yapilan ONCEKI bir cagriya yanlislikla eslesip audit
                    # satirinin ustune yazilmasin diye her zaman benzersiz.
                    idempotency_key=uuid.uuid4().hex,
                )
            except agent_job_queue.AgentJobQueueError as exc:
                print(f"⚠️ [Worker] MCP audit-trail insert atlandi: {exc}")

        job_id = str(job_row.get("id")) if job_row else ""
        try:
            result = await _execute_task(
                ethgent_agent, task, context, owner="agent_worker_mcp", job_id=job_id, label=task[:80]
            )
        except Exception as exc:
            if job_row:
                await asyncio.to_thread(agent_job_queue.fail_agent_job, job_id, str(exc))
            raise

        if job_row:
            await asyncio.to_thread(agent_job_queue.finish_agent_job, job_id, result)
        return result

    return _mcp_run_task


async def _run_mcp_server(ethgent_agent: EthgentAgent, host: str, port: int, token: str) -> None:
    mcp_server = AgentMcpServer(_build_mcp_run_task(ethgent_agent))
    app = build_auth_app(mcp_server.asgi_app(), token)

    if not token:
        print("⚠️ [Worker] AGENT_WORKER_MCP_TOKEN tanimli degil; MCP sunucusu TUM istekleri reddedecek.")

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    print(f"🔌 [Worker] MCP sunucusu: http://{host}:{port}/mcp")
    await server.serve()


async def main() -> None:
    print("🚀 [Worker] Ethgent worker baslatiliyor...")

    ethgent_agent = EthgentAgent(approval_handler=approve_if_granted_by_job)

    poll_ms = max(1000, int(os.getenv("AGENT_WORKER_POLL_MS") or _DEFAULT_POLL_MS))
    mcp_host = os.getenv("AGENT_WORKER_MCP_HOST") or _DEFAULT_MCP_HOST
    mcp_port = int(os.getenv("AGENT_WORKER_MCP_PORT") or _DEFAULT_MCP_PORT)
    mcp_token = (os.getenv("AGENT_WORKER_MCP_TOKEN") or "").strip()

    tasks = [
        asyncio.create_task(_queue_poll_loop(ethgent_agent, poll_ms), name="agent-worker-queue"),
        asyncio.create_task(_run_mcp_server(ethgent_agent, mcp_host, mcp_port, mcp_token), name="agent-worker-mcp"),
    ]

    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Worker] kapatildi.")
