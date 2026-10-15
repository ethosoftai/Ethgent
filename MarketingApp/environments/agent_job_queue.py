"""
Agent Job Queue — `agent_jobs` Supabase tablosuna karsi ince bir REST istemcisi.

`MarketingApp/worker.py`'nin iki girdi noktasi da (queue-poller ve MCP
on-demand) bu tabloyu kullanir; `social-media-worker/adapters/supabase.js` +
`services/jobQueue.js`'in Python karsiligidir, ayni Supabase projesi ve ayni
SUPABASE_URL/SUPABASE_SECRET_KEY degiskenleri uzerinden calisir (bkz.
`MarketingApp/araclar/worker_yayinlama_araclari.py`).

Tasarim gerekcesi: bu modul SADECE queue-poller'i besler ve MCP tool'unun
audit-trail insert'ini yapar; asil is (`EthgentAgent.run`) burada degil
`MarketingApp/worker.py`'de calisir.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

import requests

_TIMEOUT_SECONDS = 20
_warned_broad_key = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentJobQueueError(RuntimeError):
    """Supabase istegi basarisiz oldugunda veya yapilandirma eksikse firlatilir."""


def is_configured() -> bool:
    return bool((os.getenv("SUPABASE_URL") or "").strip()) and bool(
        (os.getenv("SUPABASE_AGENT_KEY") or os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    )


def _config() -> tuple[str, str]:
    """(base_url, key) dondurur. `SUPABASE_AGENT_KEY` (daraltilmis role/anahtar
    — bkz. migrations/002'deki ornek GRANT/POLICY) varsa o tercih edilir;
    yoksa tam service-role yetkili `SUPABASE_SECRET_KEY`'e dusulur ve BU
    process icin bir kez uyari loglanir."""
    global _warned_broad_key

    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    scoped_key = (os.getenv("SUPABASE_AGENT_KEY") or "").strip()
    broad_key = (os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()

    if not url or not (scoped_key or broad_key):
        raise AgentJobQueueError(
            "SUPABASE_URL / (SUPABASE_AGENT_KEY veya SUPABASE_SECRET_KEY) tanimli degil; "
            "agent_jobs kuyrugu kullanilamaz."
        )

    if scoped_key:
        return url, scoped_key

    if not _warned_broad_key:
        print(
            "⚠️ [Agent Job Queue] SUPABASE_AGENT_KEY tanimli degil; daraltilmamis "
            "SUPABASE_SECRET_KEY kullaniliyor (tum projeye erisimi var). Bkz. "
            "AGENT.md 'Supabase anahtarini daraltma'."
        )
        _warned_broad_key = True
    return url, broad_key


def _idempotency_key(payload: dict[str, Any]) -> str:
    normalized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:48]


def _headers(key: str, *, prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def claim_next_agent_job() -> dict[str, Any] | None:
    """`claim_next_agent_job()` RPC'sini cagirir; is yoksa None dondurur."""
    base_url, key = _config()
    try:
        response = requests.post(
            f"{base_url}/rest/v1/rpc/claim_next_agent_job",
            headers=_headers(key),
            json={},
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise AgentJobQueueError(f"claim_next_agent_job basarisiz: {exc}") from exc

    data = response.json()
    if not data or not isinstance(data, dict) or not data.get("id"):
        return None
    return data


def _lookup_job_by_idempotency_key(base_url: str, key_value: str, headers: dict[str, str]) -> dict[str, Any] | None:
    try:
        response = requests.get(
            f"{base_url}/rest/v1/agent_jobs",
            headers=headers,
            params={"idempotency_key": f"eq.{key_value}", "select": "*", "limit": "1"},
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        rows = response.json()
        return rows[0] if isinstance(rows, list) and rows else None
    except Exception:
        return None


def insert_agent_job(
    payload: dict[str, Any],
    *,
    owner_ref: str = "",
    status: str = "queued",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Yeni bir agent_jobs satiri ekler; eklenen (veya idempotency_key
    eslesirse VAR OLAN) satiri dondurur.

    status='processing' MCP on-demand cagrilarinin kendi isini hemen kendisi
    calistirip audit-trail birakmasi icindir (claim RPC'sini atlamak gerekir,
    cunku is zaten bu process tarafindan sahipli).

    Varsayilan olarak `payload` icerigine dayali deterministik bir anahtar
    uretilir: ayni payload'la iki kez cagrilirsa (network retry, cagiran
    tarafin ayni istegi tekrar denemesi) ikinci deneme yeni bir satir
    olusturmaz, ilk satiri dondurur (bkz.
    migrations/002_agent_jobs_idempotency_key.sql). Cagiran taraf "bu her
    zaman YENI bir kayit olmali, ayni gorev metniyle bile" istiyorsa (orn.
    worker.py'nin MCP audit-trail insert'i — orada zaten retry yok, her
    cagri ayri bir olay) `idempotency_key` parametresiyle kendi benzersiz
    degerini verebilir.
    """
    base_url, key = _config()
    key_value = idempotency_key or _idempotency_key(payload)
    headers = _headers(key, prefer="return=representation")
    body: dict[str, Any] = {"payload": payload, "status": status, "idempotency_key": key_value}
    if owner_ref:
        body["owner_ref"] = owner_ref
    if status == "processing":
        body["attempts"] = 1

    try:
        response = requests.post(
            f"{base_url}/rest/v1/agent_jobs",
            headers=headers,
            json=body,
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 409:
            existing = _lookup_job_by_idempotency_key(base_url, key_value, _headers(key))
            if existing:
                return existing
        raise AgentJobQueueError(f"agent_jobs kaydi eklenemedi: {exc}") from exc
    except requests.RequestException as exc:
        raise AgentJobQueueError(f"agent_jobs kaydi eklenemedi: {exc}") from exc

    rows = response.json()
    if not isinstance(rows, list) or not rows:
        raise AgentJobQueueError("agent_jobs insert bos yanit dondurdu.")
    return rows[0]


def _update_job(job_id: str, patch: dict[str, Any]) -> None:
    base_url, key = _config()
    try:
        response = requests.patch(
            f"{base_url}/rest/v1/agent_jobs",
            headers=_headers(key),
            params={"id": f"eq.{job_id}"},
            json=patch,
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise AgentJobQueueError(f"agent_jobs guncellenemedi ({job_id}): {exc}") from exc


def finish_agent_job(job_id: str, results: dict[str, Any]) -> None:
    now = _now_iso()
    _update_job(
        job_id,
        {
            "status": "done",
            "results": results,
            "error": None,
            "completed_at": now,
            "updated_at": now,
        },
    )


def requeue_agent_job(job_id: str) -> None:
    """Bir isi 'queued' durumuna geri dondurur (basarisiz sayilmadan tekrar denensin diye).

    Ornegin: worker.py bu process icinde otomasyon mesgulken claim edilmis bir
    isi birkac kez denedikten sonra hala mesgulse, isi 'failed' olarak
    isaretlemek yerine burayi cagirip sonraki poll'e (ya da baska bir
    replica'ya) birakir.
    """
    _update_job(
        job_id,
        {
            "status": "queued",
            "started_at": None,
            "error": None,
            "updated_at": _now_iso(),
        },
    )


def fail_agent_job(job_id: str, error: str, results: dict[str, Any] | None = None) -> None:
    now = _now_iso()
    _update_job(
        job_id,
        {
            "status": "failed",
            "results": results or {},
            "error": str(error)[:2000],
            "completed_at": now,
            "updated_at": now,
        },
    )
