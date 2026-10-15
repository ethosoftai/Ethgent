"""Prepared-media handles: how signed URLs reach a publish tool without reaching the LLM.

future_work.md: "private signed URLs never enter an LLM prompt". An asset URL from
an app is usually exactly that -- a short-lived signed link. So the tools that
handle assets never print it: `medya_hazirla` validates the media, stores the URL
here, and hands the model an opaque `media_ref`; `worker_video_yayinla` /
`worker_instagram_carousel_yayinla` resolve the ref back to the URL themselves.

The registry is a small JSON file in the agent workspace (so the queue/MCP worker
and the terminal share it). Entries expire with the URL they hold.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from MarketingApp.paths import workspace_path

_REGISTRY_PATH = workspace_path("assets", "prepared_media.json")
_DEFAULT_TTL = timedelta(hours=6)
_MAX_ENTRIES = 200
_lock = threading.Lock()


def mask_url(url: str) -> str:
    """`https://host/path` with credentials, query and fragment removed.

    Signed URLs carry their secret in the query string (and sometimes the path
    tail); the model only ever needs to recognise WHERE a file lives."""
    try:
        parts = urlsplit(str(url))
        host = parts.hostname or ""
        shown = f"{parts.scheme}://{host}{parts.path}"
        return shown + ("?…" if parts.query else "")
    except ValueError:
        return "[invalid url]"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load() -> dict[str, Any]:
    try:
        with open(_REGISTRY_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(entries: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(_REGISTRY_PATH), exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(_REGISTRY_PATH), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(entries, handle, ensure_ascii=False)
        os.chmod(temp_path, 0o600)  # the file holds live signed URLs
        os.replace(temp_path, _REGISTRY_PATH)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_path)
        raise


def parse_dt(value: Any) -> datetime:
    """ISO-8601 -> aware datetime (naive values are taken as UTC); raises ValueError."""
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _expiry(entry: dict[str, Any]) -> datetime:
    try:
        return parse_dt(entry.get("expires_at"))
    except ValueError:
        return _now()  # unreadable expiry: treat as already expired


def register(*, action: str, platforms: list[str], urls: list[str], kind: str, expires_at: str | None = None) -> str:
    """Stores prepared URL(s) and returns the opaque handle for them.

    The entry lives until `expires_at` (the URLs' own expiry) or six hours,
    whichever comes first."""
    ceiling = _now() + _DEFAULT_TTL
    try:
        expiry = parse_dt(expires_at) if expires_at else ceiling
    except ValueError:
        expiry = ceiling
    expiry = min(expiry, ceiling)

    ref = f"media_{secrets.token_hex(5)}"
    with _lock:
        entries = {k: v for k, v in _load().items() if _expiry(v) > _now()}
        if len(entries) >= _MAX_ENTRIES:
            for stale in sorted(entries, key=lambda k: _expiry(entries[k]))[: len(entries) - _MAX_ENTRIES + 1]:
                entries.pop(stale, None)
        entries[ref] = {
            "action": action,
            "platforms": sorted(set(platforms)),
            "kind": kind,
            "urls": list(urls),
            "expires_at": expiry.isoformat(),
        }
        _save(entries)
    return ref


def resolve(ref: str, *, action: str, platforms: list[str]) -> tuple[list[str] | None, str | None]:
    """Returns (urls, None) or (None, reason). Refuses expired refs and refs that
    were prepared for a different action or platform than the one being used."""
    ref = str(ref or "").strip()
    with _lock:
        entry = _load().get(ref)
    if not entry:
        return None, f"media_ref '{ref}' bulunamadi (yanlis ya da suresi dolmus). medya_hazirla ile yeniden hazirla."
    if _expiry(entry) <= _now():
        return None, f"media_ref '{ref}' suresi doldu. medya_hazirla ile yeniden hazirla."
    if entry.get("action") != action:
        return None, f"media_ref '{ref}' {entry.get('action')} icin hazirlandi, {action} icin kullanilamaz."
    missing = sorted(set(platforms) - set(entry.get("platforms") or []))
    if missing:
        return None, (
            f"media_ref '{ref}' su platform(lar) icin dogrulanmadi: {', '.join(missing)}. "
            "Bu platform(lar) icin medya_hazirla'yi yeniden calistir."
        )
    return list(entry.get("urls") or []), None
