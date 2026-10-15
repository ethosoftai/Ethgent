"""Small shared Supabase REST helper for the agent's newer queue clients.

Same key policy as worker_yayinlama_araclari.py / agent_job_queue.py: prefer the
scoped SUPABASE_AGENT_KEY (a role that can only touch the queue tables), fall
back to the full-access SUPABASE_SECRET_KEY with a one-time warning.
"""

from __future__ import annotations

import os

_warned = False


def config() -> tuple[str, str] | str:
    """(base_url, key), or a user-readable error string when nothing is configured."""
    global _warned
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    scoped = (os.getenv("SUPABASE_AGENT_KEY") or "").strip()
    broad = (os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not url or not (scoped or broad):
        return "❌ Hata: SUPABASE_URL ve (SUPABASE_AGENT_KEY veya SUPABASE_SECRET_KEY) tanimli degil."
    if scoped:
        return url, scoped
    if not _warned:
        print(
            "⚠️ [Supabase] SUPABASE_AGENT_KEY tanimli degil; daraltilmamis SUPABASE_SECRET_KEY "
            "kullaniliyor (tum projeye erisimi var). Bkz. AGENT.md 'Supabase anahtarini daraltma'."
        )
        _warned = True
    return url, broad


def headers(key: str, *, prefer: str | None = None) -> dict[str, str]:
    result = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if prefer:
        result["Prefer"] = prefer
    return result
