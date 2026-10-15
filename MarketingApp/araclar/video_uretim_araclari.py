"""
Video Uretim Araclari — future_work.md'nin `request_video_generation` yetenegi:
App'in kendi video uretim/render kabiliyetinden, bir BRIEF vererek video ister.

Ajan komut degil, tarif verir ("the agent supplies a brief, not executable
infrastructure commands"): brief duz aciklayici metindir; App onu opak veri
olarak ele alir. Bu yuzden burada (ve App tarafinda, asset-pool kit'inde) kod
blogu / kontrol karakteri iceren brief'ler reddedilir.

Sozlesme (App'in uygulamasi gereken; tam metin asset-pool/README.md):
  POST {APP_INTERNAL_URL}{APP_VIDEO_REQUESTS_PATH}       govde: brief (+ baslik/sure/yonelim/dil)
       Idempotency-Key basligi  ->  202 {requestId, status, ...}
  GET  {APP_INTERNAL_URL}{APP_VIDEO_REQUESTS_PATH}/{id}  ->  200 {requestId, status, progress?, asset?, approval?, error?}
  Auth: Authorization: Bearer {APP_INTERNAL_TOKEN}

Uc guvenlik/maliyet kurali (video uretimi ucretli ve geri alinamaz):
  * Onay: uretim baslamadan once `approval_runtime.request_tool_approval` ile onay
    istenir (varsayilan acik; bilincli olarak `VIDEO_GENERATION_REQUIRES_APPROVAL=false`
    ile kapatilabilir -- bassiz calisan bir worker'in uretim yapabilmesi icin).
  * Gunluk sinir: `VIDEO_GENERATION_MAX_PER_DAY` (varsayilan 3) yeni istek/24 saat.
    Ayni istegin tekrari (ayni Idempotency-Key) sinira SAYILMAZ -- App zaten dedupe eder.
  * Idempotency: anahtar istek iceriginden turetilir; retry ikinci (ucretli) bir render baslatmaz.

`done` durumundaki `asset` bir TASLAKTIR: App onaylayana kadar yayinlanamaz. Onay
sinyali varligin havuzda gorunmesidir (`app_asset_detay(id)` 200 doner); o zaman
`medya_hazirla` ile yayina hazirlanir. Imzali URL'ler burada da maskelenir.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from MarketingApp.araclar.app_asset_araclari import app_request, normalize_asset
from MarketingApp.environments.approval_runtime import request_tool_approval
from MarketingApp.environments.media_registry import mask_url
from MarketingApp.paths import workspace_path

# Mirrors schemas/video_generation_request.schema.json (asset-pool/src/contracts.js).
_BRIEF_MIN, _BRIEF_MAX = 10, 2000
_TITLE_MAX = 100
_DURATION_MIN, _DURATION_MAX = 5, 1200
_ORIENTATIONS = ("horizontal", "vertical")
_LANGUAGE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2,4})?$")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_STATUSES = ("queued", "running", "done", "failed")
_LOG_PATH = workspace_path("assets", "video_requests_log.json")
_DEFAULT_DAILY_CAP = 3


def _video_path() -> str:
    raw = (os.getenv("APP_VIDEO_REQUESTS_PATH") or "/api/video-requests").strip()
    return "/" + raw.strip("/")


def _approval_required() -> bool:
    return (os.getenv("VIDEO_GENERATION_REQUIRES_APPROVAL") or "true").strip().lower() not in ("0", "false", "no", "off")


def _daily_cap() -> int:
    try:
        return max(0, int(os.getenv("VIDEO_GENERATION_MAX_PER_DAY") or _DEFAULT_DAILY_CAP))
    except ValueError:
        return _DEFAULT_DAILY_CAP


def _read_log() -> list[dict]:
    try:
        with open(_LOG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _recent(entries: list[dict]) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    fresh = []
    for entry in entries:
        try:
            if datetime.fromisoformat(entry["at"]) > cutoff:
                fresh.append(entry)
        except (KeyError, ValueError, TypeError):
            continue
    return fresh


def _write_log(entries: list[dict]) -> None:
    try:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        with open(_LOG_PATH, "w", encoding="utf-8") as handle:
            json.dump(entries[-200:], handle, ensure_ascii=False)
    except OSError:
        pass  # sayac yazilamazsa istek engellenmez; sinir bir sonraki basarili yazimda devam eder


def _build_request(brief: str, baslik: str, sure_saniye: int, yonelim: str, dil: str) -> dict | str:
    """Dogrulanmis istek govdesi ya da kullanici-okur hata metni."""
    text = str(brief or "").strip()
    if not _BRIEF_MIN <= len(text) <= _BRIEF_MAX:
        return f"❌ Hata: brief {_BRIEF_MIN}-{_BRIEF_MAX} karakter olmali ({len(text)} verildi)."
    if _CONTROL_CHARS.search(text):
        return "❌ Hata: brief kontrol karakteri iceremez."
    if "```" in text:
        return "❌ Hata: brief duz aciklayici metin olmali; kod blogu iceremez (brief komut degil, tariftir)."

    request: dict = {"brief": text}
    if str(baslik or "").strip():
        if len(baslik.strip()) > _TITLE_MAX:
            return f"❌ Hata: baslik en fazla {_TITLE_MAX} karakter olabilir."
        request["title"] = baslik.strip()
    try:
        duration = int(sure_saniye or 0)
    except (TypeError, ValueError):
        return "❌ Hata: sure_saniye bir tam sayi olmali."
    if duration:
        if not _DURATION_MIN <= duration <= _DURATION_MAX:
            return f"❌ Hata: sure_saniye {_DURATION_MIN}-{_DURATION_MAX} arasinda olmali."
        request["durationSeconds"] = duration
    if str(yonelim or "").strip():
        if yonelim.strip() not in _ORIENTATIONS:
            return f"❌ Hata: yonelim {' veya '.join(_ORIENTATIONS)} olmali."
        request["orientation"] = yonelim.strip()
    if str(dil or "").strip():
        if not _LANGUAGE.match(dil.strip()):
            return "❌ Hata: dil 'tr' veya 'pt-BR' gibi olmali."
        request["language"] = dil.strip()
    return request


def _idempotency_key(request: dict) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:48]


def _status_document(data) -> dict | None:
    if not isinstance(data, dict) or not isinstance(data.get("requestId"), str) or not data["requestId"]:
        return None
    if data.get("status") not in _STATUSES:
        return None
    return data


def _format_status(doc: dict) -> str:
    lines = [f"request_id={doc['requestId']}", f"durum={doc['status']}"]
    progress = doc.get("progress")
    if isinstance(progress, dict):
        parts = []
        if "current" in progress and "total" in progress:
            parts.append(f"{progress['current']}/{progress['total']}")
        if progress.get("message"):
            parts.append(str(progress["message"]))
        if parts:
            lines.append("ilerleme=" + " ".join(parts))
    if doc.get("error"):
        lines.append(f"hata={doc['error']}")

    asset = normalize_asset(doc.get("asset")) if doc.get("asset") else None
    if asset:
        lines.append(f"uretilen_varlik: id={asset['id']} kind={asset['kind']} url={mask_url(asset['url'])}")
    if doc["status"] == "done":
        if doc.get("approval") == "approved":
            lines.append("onay=onayli: medya_hazirla(asset_ids=<id>) ile yayina hazirlayabilirsin.")
        else:
            lines.append(
                "onay=BEKLIYOR: bu bir TASLAKTIR, App onaylayana kadar yayinlanamaz. Onaylandiginda "
                "varlik havuzda gorunur (app_asset_detay(id) basarili olur); o zaman medya_hazirla ile hazirla."
            )
    elif doc["status"] in ("queued", "running"):
        lines.append("sonraki_adim: birazdan video_uretimi_durumu(request_id) ile tekrar kontrol et (sik sik degil).")
    return "\n".join(lines)


async def video_uretimi_iste(brief: str, baslik: str = "", sure_saniye: int = 0, yonelim: str = "", dil: str = "") -> str:
    """
    App'in video uretim kabiliyetinden bir video ister. UCRETLI ve geri alinamaz
    olabilir: baslamadan once onay istenir ve gunluk sinir uygulanir. Brief
    duz aciklayici bir metindir (konu, ton, hedef kitle, akis); komut ya da
    kod DEGIL. Ayni brief tekrar gonderilirse ikinci bir uretim baslamaz.
    Sonuc uzun surebilir: video_uretimi_durumu(request_id) ile takip et.

    Args:
        brief: Istenen videonun tarifi (10-2000 karakter).
        baslik: Onerilen baslik (opsiyonel, en fazla 100 karakter).
        sure_saniye: Hedef sure, saniye (opsiyonel, 5-1200).
        yonelim: horizontal veya vertical (opsiyonel).
        dil: Anlatim dili, orn. tr veya en (opsiyonel).
    """
    request = _build_request(brief, baslik, sure_saniye, yonelim, dil)
    if isinstance(request, str):
        return request

    key = _idempotency_key(request)
    entries = _recent(_read_log())
    already_requested = any(entry.get("key") == key for entry in entries)
    if not already_requested:
        cap = _daily_cap()
        distinct_recent = len({entry.get("key") for entry in entries})
        if distinct_recent >= cap:
            return (
                f"❌ Gunluk video uretim siniri doldu ({distinct_recent}/{cap} son 24 saatte; "
                "VIDEO_GENERATION_MAX_PER_DAY). Yeni uretim istemeden once bekle ya da siniri bilincli olarak artir."
            )

    if _approval_required():
        preview = request["brief"][:120] + ("…" if len(request["brief"]) > 120 else "")
        approved = await request_tool_approval(
            f"video_uretimi_iste:{key}",
            f"Video uretimi iste (ucretli olabilir) — \"{preview}\""
            + (f"; sure: {request['durationSeconds']}sn" if "durationSeconds" in request else ""),
        )
        if not approved:
            return "❌ Onay reddedildi (ya da bu surecte onaylayacak biri yok): video uretimi ISTENMEDI."

    status, data, error = await asyncio.to_thread(
        lambda: app_request("POST", _video_path(), json_body=request, headers={"Idempotency-Key": key}, timeout=30)
    )
    if error:
        return error
    if status in (401, 403):
        return "❌ App istegi reddetti (yetki): APP_INTERNAL_TOKEN dogru mu?"
    if status in (404, 405, 501):
        return ("❌ App video uretimi endpoint'ini uygulamiyor "
                f"({_video_path()}). Sozlesme icin asset-pool/README.md'ye bak.")
    if status == 429:
        return f"❌ App uretimi reddetti (kota/oran siniri): {(data or {}).get('error', 'ayrinti yok') if isinstance(data, dict) else 'ayrinti yok'}"
    if status >= 400:
        detail = data.get("error") if isinstance(data, dict) else f"HTTP {status}"
        return f"❌ App video isteğini reddetti: {detail}"

    doc = _status_document(data)
    if not doc:
        return "❌ App gecersiz bir durum belgesi dondurdu (requestId/status eksik ya da bozuk)."

    if not already_requested:
        _write_log(_recent(_read_log()) + [{"at": datetime.now(timezone.utc).isoformat(), "key": key, "request_id": doc["requestId"]}])
    return ("✅ Video uretim istegi alindi.\n" if not already_requested else "ℹ️ Bu istek daha once gonderilmis; mevcut istek dondu.\n") + _format_status(doc)


def video_uretimi_durumu(request_id: str) -> str:
    """
    video_uretimi_iste'nin dondurdugu request_id icin uretimin durumunu sorgular.
    Bittiginde uretilen varlik bir TASLAKTIR (App onayi gerekir).

    Args:
        request_id: video_uretimi_iste'den donen request_id.
    """
    clean = str(request_id or "").strip()
    if not clean:
        return "❌ Hata: request_id bos olamaz."
    status, data, error = app_request("GET", f"{_video_path()}/{quote(clean, safe='')}")
    if error:
        return error
    if status == 404:
        return f"❌ '{clean}' id'li bir video istegi bulunamadi."
    if status in (401, 403):
        return "❌ App istegi reddetti (yetki): APP_INTERNAL_TOKEN dogru mu?"
    if status >= 400:
        detail = data.get("error") if isinstance(data, dict) else f"HTTP {status}"
        return f"❌ App istegi basarisiz: {detail}"
    doc = _status_document(data)
    if not doc:
        return "❌ App gecersiz bir durum belgesi dondurdu."
    return _format_status(doc)
