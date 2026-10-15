"""
Worker Yayinlama Araclari — social-media-worker'in `publish_jobs` kuyruguna
sema-uyumlu bir is (job) ekler ve durumunu geri okur.

Bu modul, bu ajanin (marketing-agent) `marketing-agent-assets/schemas/publish_request.schema.json`
sozlesmesine uydugu TEK gercek entegrasyon noktasidir: worker su an sadece
`video.publish` ve `instagram.carousel` aksiyonlarini destekliyor (bkz.
`marketing-agent-assets/future_work.md` — collect_assets/execute_publish gibi digerleri henuz
planlama asamasinda, gercek bir HTTP capability olarak yok).

Onemli sinirlar:
  - `videoUrl`/`imageUrls` HTTPS URL olmali; bu arac yerel dosya YUKLEMEZ.
    Uretilen medyanin (video_post_olustur_ve_mp4_kaydet,
    html_css_post_olustur_ve_png_kaydet) once erisilebilir bir HTTPS
    adrese barindirilmis olmasi gerekir — barindirma bu ajanin degil,
    "App" katmaninin sorumlulugu (bkz. future_work.md sorumluluk tablosu).
  - Bu araclar SADECE video/carousel yayinlamayi kapsar. Begeni, takip,
    yorum, tweet atma gibi islemler worker'da karsiligi olmadigi icin
    Ethgent'in kendi browser tabanli sosyal medya araclarinda (supervised
    local) kalmaya devam eder.
  - Gercek bir yayin (`worker_video_yayinla`/`worker_instagram_carousel_yayinla`)
    kuyruga girmeden once `environments.approval_runtime.request_tool_approval`
    ile onay ister — future_work.md'nin "Present external-write actions for
    approval when required" kuralinin agent-tarafi karsiligi. Onaylanmazsa
    (ya da onaylayacak kimse yoksa — bkz. approval_runtime) is HIC
    kuyruga girmez.
  - Ayni istegin iki kez kuyruga girmesini (network retry, LLM'in "sonucu
    goremedim tekrar deneyeyim" refleksi) onlemek icin payload icerigine
    dayali bir `idempotency_key` gonderilir (bkz.
    marketing-agent-assets/social-media-worker/migrations/002_publish_jobs_idempotency_key.sql).
    Ayni anahtarla ikinci insert denemesi, VAR OLAN isin durumunu dondurur;
    yeni bir is olusturmaz.
"""

from __future__ import annotations

import hashlib
import json
import os

import requests

from MarketingApp.environments.approval_runtime import request_tool_approval
from MarketingApp.environments.media_registry import mask_url, resolve as resolve_media_ref

_ALLOWED_VIDEO_PLATFORMS = {"instagram", "youtube", "tiktok"}
_SUPABASE_TIMEOUT_SECONDS = 15


def _supabase_config() -> tuple[str, str] | str:
    """(base_url, key) dondurur; eksikse kullanici-okur bir hata metni dondurur.

    `SUPABASE_AGENT_KEY` (publish_jobs uzerinde sadece insert/select yapabilen,
    daraltilmis bir role/anahtar — bkz. migrations/002'deki ornek GRANT/POLICY)
    varsa o tercih edilir. Yoksa `SUPABASE_SECRET_KEY` (tam service-role
    yetkisi) kullanilir; bu durumda tek seferlik bir uyari loglanir, cunku bu
    anahtar sadece bu tabloya degil, projedeki her tabloya erisebilir.
    """
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    scoped_key = (os.getenv("SUPABASE_AGENT_KEY") or "").strip()
    broad_key = (os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()

    if not url or not (scoped_key or broad_key):
        return (
            "❌ Hata: SUPABASE_URL ve (SUPABASE_AGENT_KEY veya SUPABASE_SECRET_KEY) tanimli degil. "
            "social-media-worker'in kullandigi ayni Supabase projesine ait "
            "degerleri .env dosyasina ekleyin."
        )

    if scoped_key:
        return url, scoped_key

    print(
        "⚠️ [Worker Yayinlama] SUPABASE_AGENT_KEY tanimli degil; daraltilmamis "
        "SUPABASE_SECRET_KEY kullaniliyor (tum projeye erisimi var). Daha az "
        "yetkili bir anahtar icin AGENT.md'deki 'Supabase anahtarini daraltma' "
        "bolumune bakin."
    )
    return url, broad_key


def _is_https_url(value: str) -> bool:
    return bool(value) and value.strip().lower().startswith("https://")


def _idempotency_key(payload: dict) -> str:
    """Payload icerigine dayali, deterministik bir anahtar uretir.

    Ayni action + ayni alanlarla iki kez cagrilirsa AYNI anahtar cikar ->
    ikinci insert denemesi (retry) yeni bir is degil, var olan isi dondurur.
    """
    normalized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:48]


def _lookup_job_by_idempotency_key(base_url: str, key: str, headers: dict) -> dict | None:
    try:
        response = requests.get(
            f"{base_url}/rest/v1/publish_jobs",
            headers=headers,
            params={"idempotency_key": f"eq.{key}", "select": "*", "limit": "1"},
            timeout=_SUPABASE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        rows = response.json()
        return rows[0] if isinstance(rows, list) and rows else None
    except Exception:
        return None


def _format_job_status(job: dict) -> str:
    job_id = job.get("id", "bilinmiyor")
    status = job.get("status", "bilinmiyor")
    results = job.get("results") or {}
    error = job.get("error")
    lines = [f"job_id={job_id} status={status}"]
    if results:
        lines.append(f"results={json.dumps(results, ensure_ascii=False)}")
    if error:
        lines.append(f"error={error}")
    return " | ".join(lines)


def _insert_publish_job(payload: dict, owner_ref: str = "") -> str:
    config = _supabase_config()
    if isinstance(config, str):
        return config

    base_url, key = config
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

    key_value = _idempotency_key(payload)
    body = {"payload": payload, "idempotency_key": key_value}
    if owner_ref:
        body["owner_ref"] = owner_ref

    try:
        response = requests.post(
            f"{base_url}/rest/v1/publish_jobs",
            headers=headers,
            json=body,
            timeout=_SUPABASE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        rows = response.json()
        job_id = rows[0].get("id") if isinstance(rows, list) and rows else None
        return (
            f"✅ Yayin isi kuyruga eklendi (job_id={job_id or 'bilinmiyor'}). "
            "social-media-worker bu isi kendi dongusunde alip yayinlayacak; "
            "durumu `worker_yayin_durumu_sorgula` ile takip edin."
        )
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code == 409:
            existing = _lookup_job_by_idempotency_key(base_url, key_value, headers)
            if existing:
                return (
                    "ℹ️ Bu istek daha once kuyruga eklenmis (idempotency_key eslesti); "
                    "yeni bir is olusturulmadi. " + _format_job_status(existing)
                )
        detay = exc.response.text[:500] if exc.response is not None else str(exc)
        return f"❌ Supabase isteği basarisiz: {detay}"
    except Exception as exc:
        return f"❌ Yayin isi eklenemedi: {exc}"


async def _require_publish_approval(action_id: str, description: str) -> str | None:
    """Onay istenir; reddedilirse/kimse yoksa kullanici-okur bir ret metni dondurur, aksi halde None."""
    approved = await request_tool_approval(action_id, description)
    if approved:
        return None
    return (
        "❌ Onay reddedildi (ya da bu surecte onaylayacak biri yok): "
        f"{description}. Is kuyruga EKLENMEDI."
    )


def worker_yayin_durumu_sorgula(job_id: str) -> str:
    """
    `worker_video_yayinla` veya `worker_instagram_carousel_yayinla`'nin
    dondurdugu job_id icin `publish_jobs` tablosundaki guncel durumu
    (status: queued/processing/done/failed, results, error) sorgular.
    Bir yayin isteginin gercekten basarili olup olmadigini SADECE bu tool
    ile ogrenebilirsin — kuyruga ekleme mesaji ("✅ kuyruga eklendi") henuz
    yayinlandigi anlamina gelmez.

    Args:
        job_id: Kuyruga ekleme sirasinda donen job_id.
    """
    clean_id = (job_id or "").strip()
    if not clean_id:
        return "❌ Hata: job_id bos olamaz."

    config = _supabase_config()
    if isinstance(config, str):
        return config
    base_url, key = config

    try:
        response = requests.get(
            f"{base_url}/rest/v1/publish_jobs",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            params={"id": f"eq.{clean_id}", "select": "*", "limit": "1"},
            timeout=_SUPABASE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            return f"❌ '{clean_id}' id'li bir yayin isi bulunamadi."
        return _format_job_status(rows[0])
    except requests.HTTPError as exc:
        detay = exc.response.text[:500] if exc.response is not None else str(exc)
        return f"❌ Supabase isteği basarisiz: {detay}"
    except Exception as exc:
        return f"❌ Durum sorgulanamadi: {exc}"


async def worker_video_yayinla(
    video_url: str = "",
    platforms: str = "",
    baslik: str = "",
    aciklama: str = "",
    caption: str = "",
    gizlilik: str = "private",
    credential_ref: str = "",
    media_ref: str = "",
) -> str:
    """
    Zaten HTTPS uzerinde barindirilan bir videoyu social-media-worker
    araciligiyla Instagram/YouTube/TikTok'a yayinlamak icin kuyruga bir
    `video.publish` isi ekler. Worker'in kendisi gercekten yayinlar; bu arac
    sadece isi olusturur (ve once onay ister — bkz. modul docstring'i).

    Args:
        video_url: Videonun HTTPS adresi (yerel dosya yolu KABUL EDILMEZ).
            App varliklari icin bunu KULLANMA; `medya_hazirla`'nin dondurdugu
            media_ref'i ver (imzali URL'ler modele hic gosterilmez).
        platforms: Virgulle ayrilmis platform listesi. Gecerli degerler:
            instagram, youtube, tiktok (orn: "instagram,youtube").
        baslik: Video basligi (YouTube icin zorunlu, en fazla 100 karakter).
        aciklama: Uzun aciklama metni (en fazla 5000 karakter).
        caption: Kisa altyazi/caption metni (en fazla 2200 karakter).
        gizlilik: private, unlisted veya public (varsayilan private).
        credential_ref: Coklu-hesap kurulumlarinda hangi hesabin kullanilacagini
            belirten referans (opsiyonel).
        media_ref: `medya_hazirla`'nin dondurdugu hazir-medya kimligi (video_url yerine).
    """
    platform_listesi = [p.strip().lower() for p in (platforms or "").split(",") if p.strip()]
    if not platform_listesi:
        return "❌ Hata: en az bir platform belirtin (instagram, youtube, tiktok)."
    gecersiz = [p for p in platform_listesi if p not in _ALLOWED_VIDEO_PLATFORMS]
    if gecersiz:
        return f"❌ Hata: gecersiz platform(lar): {', '.join(gecersiz)}. Izin verilenler: instagram, youtube, tiktok."
    platform_listesi = list(dict.fromkeys(platform_listesi))  # uniqueItems

    if media_ref.strip():
        if video_url.strip():
            return "❌ Hata: video_url ve media_ref birlikte verilemez; yalnizca birini kullan."
        resolved, reason = resolve_media_ref(media_ref, action="video.publish", platforms=platform_listesi)
        if reason:
            return f"❌ Hata: {reason}"
        if len(resolved) != 1:
            return "❌ Hata: media_ref tek bir video icermeli."
        video_url = resolved[0]
    if not _is_https_url(video_url):
        return "❌ Hata: video_url https:// ile baslayan bir adres olmali (ya da media_ref ver)."

    if gizlilik not in {"private", "unlisted", "public"}:
        return "❌ Hata: gizlilik 'private', 'unlisted' veya 'public' olmali."
    if "youtube" in platform_listesi and not baslik.strip():
        return "❌ Hata: YouTube icin baslik zorunlu."
    if len(baslik) > 100:
        return "❌ Hata: baslik en fazla 100 karakter olabilir."
    if len(aciklama) > 5000:
        return "❌ Hata: aciklama en fazla 5000 karakter olabilir."
    if len(caption) > 2200:
        return "❌ Hata: caption en fazla 2200 karakter olabilir."

    payload = {
        "action": "video.publish",
        "videoUrl": video_url.strip(),
        "platforms": platform_listesi,
        "privacyStatus": gizlilik,
    }
    if baslik.strip():
        payload["title"] = baslik.strip()
    if aciklama.strip():
        payload["description"] = aciklama.strip()
    if caption.strip():
        payload["caption"] = caption.strip()
    if credential_ref.strip():
        payload["credentialRef"] = credential_ref.strip()

    rejection = await _require_publish_approval(
        action_id=f"worker_video_yayinla:{_idempotency_key(payload)}",
        description=(
            f"Video yayinla — platformlar: {', '.join(platform_listesi)}; "
            f"gizlilik: {gizlilik}; baslik: '{baslik.strip() or '-'}'; url: {mask_url(video_url.strip())}"
        ),
    )
    if rejection:
        return rejection

    return _insert_publish_job(payload, owner_ref=credential_ref.strip())


async def worker_instagram_carousel_yayinla(
    image_urls: str = "",
    caption: str = "",
    credential_ref: str = "",
    media_ref: str = "",
) -> str:
    """
    Zaten HTTPS uzerinde barindirilan 2-10 JPEG gorseli social-media-worker
    araciligiyla Instagram carousel olarak yayinlamak icin kuyruga bir
    `instagram.carousel` isi ekler (ve once onay ister — bkz. modul docstring'i).

    Args:
        image_urls: Virgulle ayrilmis, https:// ile baslayan 2-10 gorsel
            adresi (orn: "https://cdn.example.com/1.jpg,https://cdn.example.com/2.jpg").
            App varliklari icin bunu KULLANMA; `medya_hazirla`'nin media_ref'ini ver.
        caption: Carousel altyazisi (en fazla 2200 karakter).
        credential_ref: Coklu-hesap kurulumlarinda hangi hesabin kullanilacagini
            belirten referans (opsiyonel).
        media_ref: `medya_hazirla`'nin dondurdugu hazir-medya kimligi (image_urls yerine).
    """
    if media_ref.strip():
        if image_urls.strip():
            return "❌ Hata: image_urls ve media_ref birlikte verilemez; yalnizca birini kullan."
        resolved, reason = resolve_media_ref(media_ref, action="instagram.carousel", platforms=["instagram"])
        if reason:
            return f"❌ Hata: {reason}"
        image_urls = ",".join(resolved)
    url_listesi = [u.strip() for u in (image_urls or "").split(",") if u.strip()]
    if len(url_listesi) < 2 or len(url_listesi) > 10:
        return "❌ Hata: image_urls 2 ile 10 arasinda https gorsel adresi icermeli."
    gecersiz = [u for u in url_listesi if not _is_https_url(u)]
    if gecersiz:
        return f"❌ Hata: https:// ile baslamayan adres(ler): {', '.join(gecersiz)}"
    if len(caption) > 2200:
        return "❌ Hata: caption en fazla 2200 karakter olabilir."

    payload = {
        "action": "instagram.carousel",
        "imageUrls": url_listesi,
    }
    if caption.strip():
        payload["caption"] = caption.strip()
    if credential_ref.strip():
        payload["credentialRef"] = credential_ref.strip()

    rejection = await _require_publish_approval(
        action_id=f"worker_instagram_carousel_yayinla:{_idempotency_key(payload)}",
        description=f"Instagram carousel yayinla — {len(url_listesi)} gorsel ({mask_url(url_listesi[0])} …); caption: '{caption.strip() or '-'}'",
    )
    if rejection:
        return rejection

    return _insert_publish_job(payload, owner_ref=credential_ref.strip())
