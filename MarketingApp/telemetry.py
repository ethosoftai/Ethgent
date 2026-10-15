"""Kalici operasyon kaydi: olay logu, calisma (run) gecmisi ve LLM token kullanimi.

Neden var
---------
``BaseModel.log_message`` sadece ``print`` + 100 elemanlik bir bellek listesiydi;
heartbeat'in ``job_runtime`` tablosu ise her gorev icin TEK satirdi (son durum).
Ikisi de "gece ne oldu?" sorusuna cevap vermiyordu: restart'ta loglar siliniyor,
onceki calismalar ust uste yaziliyordu, token harcamasi hic olculmuyordu.

Tasarim kurallari
-----------------
* Yazma fonksiyonlari (``record_*``, ``start_run``, ``finish_run``) ASLA exception
  firlatmaz: gozlemlenebilirlik katmani is akisini bozmamali. Hata olursa
  ``health()`` icinde gorunur.
* Her sey yerel SQLite'ta (``workspace/runtime/``, gitignore'da) durur. ``ETHGENT_TELEMETRY_EXPORT``
  tanimliysa ayni kayitlar ayrica JSON satirlari olarak disari da yazilir: ``stdout`` ya da bir dosya
  yolu. Bir log toplayici (Loki, Datadog, CloudWatch, ...) bunlari oradan okur.
* Bir "run", ``run_scope`` ile acilir ve ContextVar ile tasinir; boylece ayni run
  icindeki LLM cagrilari ve log satirlari otomatik olarak o run'a baglanir
  (``asyncio.to_thread`` ve ``wait_for`` de context'i tasir).
* Zamanlar UTC (``...Z``) saklanir, gosterimde yerel saate cevrilir.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import sys
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from MarketingApp.paths import config_path, workspace_path

try:  # pricing.yaml opsiyonel; PyYAML yoksa maliyet tahmini kapali kalir
    import yaml
except Exception:  # pragma: no cover
    yaml = None

DB_PATH = workspace_path("runtime", "telemetry.sqlite")

_MAX_MESSAGE_CHARS = 2000
_MAX_SUMMARY_CHARS = 1000
_MAX_ERROR_CHARS = 600
_PRUNE_INTERVAL_SECONDS = 24 * 3600
_DEFAULT_RETENTION_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    job_id TEXT,
    label TEXT,
    detail TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    error TEXT,
    summary TEXT,
    duration_ms INTEGER,
    llm_calls INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_job ON runs(job_id);

CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    run_id TEXT,
    agent TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON llm_usage(ts);
CREATE INDEX IF NOT EXISTS idx_usage_run ON llm_usage(run_id);
"""

_initialized_path: str | None = None
_last_error: str | None = None
_last_prune_monotonic: float | None = None

_run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("ethgent_run_id", default=None)
_source_var: contextvars.ContextVar[str] = contextvars.ContextVar("ethgent_source", default="direct")

GROUP_BY_CHOICES = ("agent", "model", "source", "day", "run")


# --------------------------------------------------------------------------- zaman

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_ts(value: str | None, *, with_date: bool = True) -> str:
    """Saklanan UTC zamani yerel saatle okunur hale getirir."""
    if not value:
        return "-"
    try:
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone()
    except ValueError:
        return value
    return moment.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S")


def parse_since(value: str | None) -> str | None:
    """'24h', '7d', '30m', 'today', '2026-09-01' veya '2026-09-01 08:30' -> UTC ISO esik degeri."""
    text = (value or "").strip().lower()
    if not text:
        return None

    relative = re.fullmatch(r"(\d+)\s*([smhdw])", text)
    if relative:
        unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[relative.group(2)]
        return _iso(_now() - timedelta(**{unit: int(relative.group(1))}))

    if text in {"today", "bugun"}:
        local_midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        return _iso(local_midnight)

    for pattern in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            naive = datetime.strptime(value.strip(), pattern)
        except ValueError:
            continue
        return _iso(naive.astimezone())  # naive deger yerel saat sayilir

    raise ValueError(f"Gecersiz zaman: '{value}'. Ornekler: 30m, 24h, 7d, today, 2026-09-01, '2026-09-01 08:30'")


def retention_days() -> int:
    raw = os.getenv("ETHGENT_TELEMETRY_RETENTION_DAYS", "").strip()
    if not raw:
        return _DEFAULT_RETENTION_DAYS
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_RETENTION_DAYS


# ------------------------------------------------------------------------ baglanti
#
# TEK kalici baglanti (kilit korumali). Her cagrida baglanti acip kapamak WAL modunda her
# kapanista checkpoint + fsync tetikliyor: olculen maliyet ~17 ms/yazma. log_message event
# loop uzerinde senkron cagrildigi icin bu kabul edilemezdi; kalici baglantida ~0.1 ms.

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_path: str | None = None


def _drop_connection() -> None:
    global _conn, _conn_path, _initialized_path
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:  # noqa: BLE001
                pass
        _conn, _conn_path, _initialized_path = None, None, None


def close() -> None:
    """Baglantiyi kapatir (kapanis ve testler icin; sonraki cagri yeniden acar)."""
    _drop_connection()


def _connection() -> sqlite3.Connection:
    """Gecerli baglantiyi dondurur; ilk acilista semayi kurar ve yarim kalmis run'lari kapatir. _lock altinda cagrilmali."""
    global _conn, _conn_path, _initialized_path
    if _conn is not None and _conn_path == DB_PATH and _initialized_path == DB_PATH:
        return _conn

    _drop_connection()
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)

    conn = sqlite3.connect(DB_PATH, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        # Onceki process cokmusse 'running' kalan kayitlar sonsuza kadar acik gorunmesin.
        conn.execute(
            "UPDATE runs SET status = 'interrupted', finished_at = ? WHERE status = 'running'",
            (_iso(_now()),),
        )
        conn.commit()
    except Exception:
        conn.close()
        raise
    _conn, _conn_path, _initialized_path = conn, DB_PATH, DB_PATH
    return conn


def _write(sql: str, params: tuple = ()) -> None:
    global _last_error
    try:
        with _lock:
            conn = _connection()
            with conn:
                conn.execute(sql, params)
        _last_error = None
    except Exception as exc:  # noqa: BLE001 - yazma asla is akisini bozmamali
        _last_error = f"{type(exc).__name__}: {exc}"
        _drop_connection()  # bozuk baglanti bir sonraki cagrida yeniden kurulsun


def _export(kind: str, **fields: Any) -> None:
    """One JSON line per record for an external log collector. Never raises."""
    target = (os.getenv("ETHGENT_TELEMETRY_EXPORT") or "").strip()
    if not target:
        return
    record = {"ts": _iso(_now()), "kind": kind, "service": "ethgent"}
    record.update({k: v for k, v in fields.items() if v not in (None, "")})
    try:
        line = json.dumps(record, ensure_ascii=False, default=str)
        if target.lower() in {"stdout", "-"}:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        else:
            with _lock, open(target, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:  # noqa: BLE001 - disa aktarma is akisini bozmamali
        pass


def _read(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with _lock:
        conn = _connection()
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _clip(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ------------------------------------------------------------------------- context

def current_run_id() -> str | None:
    return _run_id_var.get()


def current_source() -> str:
    return _source_var.get()


def set_source(name: str) -> None:
    """Bu context'te (ornegin bir Telegram handler'i) acilacak run'larin kaynagini belirler."""
    _source_var.set((name or "direct").strip() or "direct")


class RunHandle:
    """run_scope icinden durumu elle isaretlemek icin: exception firlatmadan basarisizlik bildirir."""

    __slots__ = ("run_id", "owned", "status", "error", "summary")

    def __init__(self, run_id: str | None, owned: bool):
        self.run_id = run_id
        self.owned = owned
        self.status = "ok"
        self.error = ""
        self.summary = ""

    def fail(self, error: Any) -> None:
        self.status = "error"
        self.error = _clip(error, _MAX_ERROR_CHARS)

    def skip(self, reason: Any) -> None:
        self.status = "skipped"
        self.error = _clip(reason, _MAX_ERROR_CHARS)

    def set_summary(self, text: Any) -> None:
        self.summary = _clip(text, _MAX_SUMMARY_CHARS)


# --------------------------------------------------------------------------- yazma

def _maybe_prune() -> None:
    global _last_prune_monotonic
    now = time.monotonic()
    if _last_prune_monotonic is not None and now - _last_prune_monotonic < _PRUNE_INTERVAL_SECONDS:
        return
    _last_prune_monotonic = now
    try:
        prune()
    except Exception:  # noqa: BLE001
        pass


def record_event(type_: str, message: str, *, run_id: str | None = None) -> None:
    """Kalici log satiri. Aktif run varsa otomatik ona baglanir."""
    _maybe_prune()
    resolved_run = run_id or _run_id_var.get()
    _write(
        "INSERT INTO events (ts, type, message, run_id) VALUES (?, ?, ?, ?)",
        (_iso(_now()), _clip(type_ or "log", 40), _clip(message, _MAX_MESSAGE_CHARS), resolved_run),
    )
    _export("event", type=_clip(type_ or "log", 40), message=_clip(message, _MAX_MESSAGE_CHARS), run_id=resolved_run)


def start_run(source: str, label: str, *, job_id: str = "", detail: str = "") -> str | None:
    run_id = uuid.uuid4().hex[:12]
    _maybe_prune()
    _write(
        "INSERT INTO runs (run_id, source, job_id, label, detail, started_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, 'running')",
        (run_id, _clip(source or "direct", 40), job_id or None, _clip(label, 200), _clip(detail, 200), _iso(_now())),
    )
    _export("run_start", run_id=run_id, source=_clip(source or "direct", 40), job_id=job_id, label=_clip(label, 200))
    return None if _last_error else run_id


def finish_run(
    run_id: str | None,
    status: str,
    *,
    error: str = "",
    summary: str = "",
    duration_ms: int | None = None,
) -> None:
    if not run_id:
        return
    _write(
        "UPDATE runs SET finished_at = ?, status = ?, error = ?, summary = ?, duration_ms = ? WHERE run_id = ?",
        (
            _iso(_now()),
            status,
            _clip(error, _MAX_ERROR_CHARS) or None,
            _clip(summary, _MAX_SUMMARY_CHARS) or None,
            duration_ms,
            run_id,
        ),
    )
    _export(
        "run_end", run_id=run_id, status=status, error=_clip(error, _MAX_ERROR_CHARS),
        summary=_clip(summary, _MAX_SUMMARY_CHARS), duration_ms=duration_ms,
    )


def record_run(
    source: str,
    label: str,
    *,
    status: str,
    job_id: str = "",
    detail: str = "",
    error: str = "",
    summary: str = "",
) -> None:
    """Hic calismamis ama kayda gecmesi gereken bir olay icin (ornegin 'skipped')."""
    run_id = start_run(source, label, job_id=job_id, detail=detail)
    finish_run(run_id, status, error=error, summary=summary, duration_ms=0)


@contextmanager
def run_scope(source: str, label: str, *, job_id: str = "", detail: str = "") -> Iterator[RunHandle]:
    """Bir islemi 'run' olarak kaydeder. Zaten aktif bir run varsa ona katilir (ic ice cagri)."""
    parent = _run_id_var.get()
    if parent:
        yield RunHandle(parent, owned=False)
        return

    run_id = start_run(source, label, job_id=job_id, detail=detail)
    handle = RunHandle(run_id, owned=True)
    token = _run_id_var.set(run_id)
    started = time.monotonic()
    try:
        yield handle
    except BaseException as exc:
        cancelled = isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
        finish_run(
            run_id,
            "cancelled" if cancelled else "error",
            error=str(exc) or type(exc).__name__,
            summary=handle.summary,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise
    else:
        finish_run(
            run_id,
            handle.status,
            error=handle.error,
            summary=handle.summary,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    finally:
        _run_id_var.reset(token)


def _extract_usage(usage: Any) -> tuple[int, int, int] | None:
    """OpenAI uyumlu veya Gemini 'usage' nesnesi/dict'inden (prompt, completion, total) cikarir."""
    if usage is None:
        return None

    def pick(*names: str) -> int | None:
        for name in names:
            value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
            try:
                if value is not None:
                    return int(value)
            except (TypeError, ValueError):
                continue
        return None

    # OpenAI uyumlu (prompt_tokens...) ve Gemini (prompt_token_count...) alan adlari
    prompt = pick("prompt_tokens", "prompt_token_count", "input_tokens")
    completion = pick("completion_tokens", "response_token_count", "candidates_token_count", "output_tokens")
    total = pick("total_tokens", "total_token_count")
    if prompt is None and completion is None and total is None:
        return None
    prompt, completion = prompt or 0, completion or 0
    return prompt, completion, total if total is not None else prompt + completion


def record_usage(agent: str, model: str, usage: Any, *, run_id: str | None = None) -> None:
    """Bir LLM cagrisini kaydeder. Saglayici 'usage' dondurmediyse cagri yine sayilir (tokenlar NULL)."""
    resolved_run = run_id or _run_id_var.get()
    extracted = _extract_usage(usage)
    prompt, completion, total = extracted if extracted else (None, None, None)

    _write(
        "INSERT INTO llm_usage (ts, run_id, agent, model, prompt_tokens, completion_tokens, total_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_iso(_now()), resolved_run, _clip(agent or "?", 80), _clip(model or "?", 120), prompt, completion, total),
    )
    _export(
        "llm_usage", run_id=resolved_run, agent=_clip(agent or "?", 80), model=_clip(model or "?", 120),
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=total,
    )
    if resolved_run:
        _write(
            "UPDATE runs SET llm_calls = llm_calls + 1, prompt_tokens = prompt_tokens + ?, "
            "completion_tokens = completion_tokens + ?, total_tokens = total_tokens + ? WHERE run_id = ?",
            (prompt or 0, completion or 0, total or 0, resolved_run),
        )


class LiveUsageTracker:
    """Gemini Live oturumu icin: oturum boyunca gelen son usage_metadata'yi tutar, bitince TEK satir yazar.

    Live API'nin usage_metadata'sinin kumulatif mi mesaj-basi mi oldugu SDK belgelerinde
    belirtilmiyor. Her mesaji toplamak kumulatifse cok fazla sayardi; bu yuzden yalnizca
    sonuncusu kaydedilir: kumulatifse dogru, degilse eksik sayar -- asla fazla saymaz.
    Bu satirlar bu nedenle 'yaklasik' kabul edilmelidir.
    """

    __slots__ = ("agent", "model", "_last")

    def __init__(self, agent: str, model: str):
        self.agent = agent
        self.model = model
        self._last: Any = None

    def observe(self, message: Any) -> None:
        metadata = getattr(message, "usage_metadata", None)
        if metadata is not None:
            self._last = metadata

    def flush(self) -> None:
        record_usage(self.agent, self.model, self._last)


def prune(days: int | None = None) -> dict[str, int]:
    """Saklama suresini asan kayitlari siler. days<=0 sonsuz saklama demektir."""
    keep_days = retention_days() if days is None else days
    if keep_days <= 0:
        return {}
    cutoff = _iso(_now() - timedelta(days=keep_days))
    removed: dict[str, int] = {}
    with _lock:
        conn = _connection()
        with conn:
            removed["events"] = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
            removed["llm_usage"] = conn.execute("DELETE FROM llm_usage WHERE ts < ?", (cutoff,)).rowcount
            removed["runs"] = conn.execute(
                "DELETE FROM runs WHERE started_at < ? AND status != 'running'", (cutoff,)
            ).rowcount
    return removed


# --------------------------------------------------------------------------- okuma

def recent_events(
    limit: int = 50,
    *,
    type_: str | None = None,
    since: str | None = None,
    grep: str | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """En yeni 'limit' olay, eskiden yeniye sirali."""
    clauses, params = [], []
    if type_:
        clauses.append("type = ?")
        params.append(type_)
    if since:
        clauses.append("ts >= ?")
        params.append(since)
    if grep:
        clauses.append("message LIKE ? ESCAPE '\\'")
        params.append("%" + grep.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = _read(f"SELECT * FROM events {where} ORDER BY id DESC LIMIT ?", (*params, max(1, int(limit))))
    return list(reversed(rows))


def recent_runs(
    limit: int = 20,
    *,
    source: str | None = None,
    job_id: str | None = None,
    status: str | None = None,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """En yeni 'limit' run, yeniden eskiye sirali."""
    clauses, params = [], []
    if source:
        clauses.append("source = ?")
        params.append(source)
    if job_id:
        clauses.append("job_id = ?")
        params.append(job_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if since:
        clauses.append("started_at >= ?")
        params.append(since)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return _read(f"SELECT * FROM runs {where} ORDER BY started_at DESC, rowid DESC LIMIT ?", (*params, max(1, int(limit))))


def find_runs(prefix: str) -> list[dict[str, Any]]:
    """run_id'ye gore (onek destekli) arama; belirsizlik cagirana birakilir."""
    cleaned = re.sub(r"[^0-9a-f]", "", (prefix or "").lower())
    if not cleaned:
        return []
    return _read("SELECT * FROM runs WHERE run_id LIKE ? ORDER BY started_at DESC LIMIT 5", (cleaned + "%",))


def run_usage(run_id: str) -> list[dict[str, Any]]:
    return _read(
        "SELECT agent, model, COUNT(*) AS calls, "
        "SUM(COALESCE(prompt_tokens,0)) AS prompt_tokens, SUM(COALESCE(completion_tokens,0)) AS completion_tokens, "
        "SUM(COALESCE(total_tokens,0)) AS total_tokens "
        "FROM llm_usage WHERE run_id = ? GROUP BY agent, model ORDER BY total_tokens DESC",
        (run_id,),
    )


def load_pricing() -> dict[str, dict[str, float]]:
    """config/pricing.yaml -> {model: {input, output}} (USD / 1M token). Dosya yoksa bos.

    Fiyatlar bilerek koda gomulu degil: saglayici fiyatlari degisir ve model adlari
    kuruluma ozeldir. Format:

        models:
          gemini-2.5-flash: {input: 0.30, output: 2.50}
    """
    path = config_path("pricing.yaml")
    if yaml is None or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except Exception:  # noqa: BLE001
        return {}
    result: dict[str, dict[str, float]] = {}
    for name, entry in (data.get("models") or {}).items():
        try:
            result[str(name).strip().lower()] = {
                "input": float(entry.get("input")),
                "output": float(entry.get("output")),
            }
        except (AttributeError, TypeError, ValueError):
            continue
    return result


def estimate_cost(model: str, prompt: int, completion: int, total: int, pricing: dict[str, dict[str, float]]) -> float | None:
    """Fiyati tanimli modeller icin USD tahmini; tanimli degilse None.

    Cikti tokeni 'total - prompt' alinir: bazi saglayicilar dusunme (reasoning)
    tokenlarini completion_tokens'a saymaz ama total'e katar ve bunlar cikti
    fiyatindan faturalanir.
    """
    entry = pricing.get((model or "").strip().lower())
    if not entry:
        return None
    billable_output = max(total - prompt, completion)
    return prompt / 1_000_000 * entry["input"] + billable_output / 1_000_000 * entry["output"]


def usage_summary(since: str | None = None, *, group_by: str = "agent") -> dict[str, Any]:
    """Token kullanimini grupla. Donus: rows, totals, unpriced_models, priced (fiyat dosyasi var mi)."""
    if group_by not in GROUP_BY_CHOICES:
        raise ValueError(f"--by su degerlerden biri olmali: {', '.join(GROUP_BY_CHOICES)}")

    key_expr = {
        "agent": "u.agent",
        "model": "u.model",
        "source": "COALESCE(r.source, '(kosu disi)')",
        "day": "date(u.ts, 'localtime')",
        "run": "COALESCE(u.run_id, '(kosu disi)')",
    }[group_by]
    where, params = ("WHERE u.ts >= ?", (since,)) if since else ("", ())

    raw = _read(
        f"SELECT {key_expr} AS k, u.model AS model, COUNT(*) AS calls, "
        "SUM(COALESCE(u.prompt_tokens,0)) AS p, SUM(COALESCE(u.completion_tokens,0)) AS c, "
        "SUM(COALESCE(u.total_tokens,0)) AS t, SUM(CASE WHEN u.total_tokens IS NULL THEN 1 ELSE 0 END) AS unknown "
        f"FROM llm_usage u LEFT JOIN runs r ON r.run_id = u.run_id {where} GROUP BY k, u.model",
        params,
    )

    pricing = load_pricing()
    merged: dict[str, dict[str, Any]] = {}
    unpriced: set[str] = set()
    for row in raw:
        entry = merged.setdefault(
            row["k"],
            {"key": row["k"], "calls": 0, "prompt": 0, "completion": 0, "total": 0, "unknown_calls": 0, "cost": 0.0, "priced": False},
        )
        entry["calls"] += row["calls"]
        entry["prompt"] += row["p"]
        entry["completion"] += row["c"]
        entry["total"] += row["t"]
        entry["unknown_calls"] += row["unknown"]
        cost = estimate_cost(row["model"], row["p"], row["c"], row["t"], pricing)
        if cost is None:
            unpriced.add(row["model"])
        else:
            entry["cost"] += cost
            entry["priced"] = True

    rows = sorted(merged.values(), key=lambda item: (-item["total"], item["key"]))
    if group_by == "day":
        rows.sort(key=lambda item: item["key"], reverse=True)

    totals = {"calls": 0, "prompt": 0, "completion": 0, "total": 0, "unknown_calls": 0, "cost": 0.0}
    for entry in rows:
        for field in ("calls", "prompt", "completion", "total", "unknown_calls", "cost"):
            totals[field] += entry[field]
    return {
        "group_by": group_by,
        "rows": rows,
        "totals": totals,
        "pricing_loaded": bool(pricing),
        "unpriced_models": sorted(unpriced),
    }


def health() -> dict[str, Any]:
    """Depo sagligi: /status ve teshis icin. Yazma hatasi sessizce yutulmasin diye burada gorunur."""
    info: dict[str, Any] = {
        "ok": _last_error is None,
        "path": DB_PATH,
        "last_error": _last_error,
        "retention_days": retention_days(),
    }
    try:
        counts = _read(
            "SELECT (SELECT COUNT(*) FROM events) AS events, (SELECT COUNT(*) FROM runs) AS runs, "
            "(SELECT COUNT(*) FROM llm_usage) AS usage_rows"
        )[0]
        info.update(counts)
    except Exception as exc:  # noqa: BLE001
        info["ok"] = False
        info["last_error"] = f"{type(exc).__name__}: {exc}"
    return info
