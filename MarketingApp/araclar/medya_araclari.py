"""
Medya Araclari — future_work.md'nin `prepare_media` yetenegi: bir medyanin bir
yayin hedefi icin HAZIR olup olmadigini dogrular ve yayin araclarina guvenle
devreder.

Iki arac:
  medya_dogrula(url, aksiyon, platformlar)      -- rastgele bir HTTPS URL'yi dogrular
  medya_hazirla(asset_ids, aksiyon, platformlar, ttl_saniye)
                                                 -- App'in onayli bir varligini hedef icin
                                                    (varsa App'in `prepare` endpoint'iyle
                                                    tazeleyip/donusturup) hazirlar ve dogrular

Kurallar (worker'in gercek kisitlarindan; kaynaklar yorumlarda):
  * Kaynak https ve halka acik bir adreste olmali. Agent bu URL'leri kendisi
    fetch eder (sadece baslik okur) -- bu yuzden SSRF korumasi vardir: ozel /
    loopback / link-local adresler ve her yonlendirme adimi reddedilir.
  * `ALLOWED_VIDEO_HOSTS` (worker'la ayni degisken adi) tanimliysa host'un onda
    olmasi gerekir; yoksa worker isi kuyruktan aldiktan SONRA reddederdi.
  * video.publish: video/* olmali (mp4 onerilir); YouTube ve TikTok icin
    Content-Length sart (worker'in resumable/tek-parca yuklemesi bunu ister:
    youtubePublish.js / tiktokPublish.js); TikTok icin <= 64 MB (tek parca).
  * instagram.carousel: 2-10 gorsel ve HEPSI image/jpeg (instagramPublish.js:
    "Instagram only accepts JPEG here").

Imzali URL'ler LLM'e ASLA donmez: hazirlanan medya `media_ref` kimligiyle
saklanir (bkz. environments/media_registry.py) ve publish tool'lari onu kendisi cozer.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from urllib.parse import quote, urljoin, urlsplit

import requests

from MarketingApp.araclar.app_asset_araclari import app_request, assets_path, normalize_asset
from MarketingApp.environments.media_registry import mask_url, parse_dt, register

_ACTIONS = ("video.publish", "instagram.carousel")
_PLATFORMS = ("instagram", "youtube", "tiktok")
_TIKTOK_MAX_BYTES = 64 * 1024 * 1024  # socialPublisher/tiktokPublish.js: single-chunk limit
_MAX_REDIRECTS = 3
_PROBE_TIMEOUT = (5, 10)
_MIN_TTL, _MAX_TTL, _DEFAULT_TTL = 60, 86400, 21600


class MediaProbeError(RuntimeError):
    """Bir URL guvenle sorgulanamadi (SSRF korumasi, DNS, ag)."""


def _allowed_hosts() -> set[str]:
    return {h.strip().lower() for h in (os.getenv("ALLOWED_VIDEO_HOSTS") or "").split(",") if h.strip()}


def _assert_public_https(url: str) -> str:
    """URL'yi dogrular ve host'unu dondurur; guvenli degilse MediaProbeError."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise MediaProbeError("URL https:// ile baslamali.")
    host = (parts.hostname or "").lower()
    if not host:
        raise MediaProbeError("URL'de host yok.")
    if parts.username or parts.password:
        raise MediaProbeError("URL kullanici bilgisi (user:pass@) iceremez.")
    if parts.port not in (None, 443):
        raise MediaProbeError("Yalnizca 443 portu desteklenir.")
    allowed = _allowed_hosts()
    if allowed and host not in allowed:
        raise MediaProbeError(f"host '{host}' ALLOWED_VIDEO_HOSTS icinde degil; worker bu isi reddederdi.")
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise MediaProbeError(f"host cozumlenemedi: {exc}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise MediaProbeError("host halka acik olmayan bir adrese cozuluyor (SSRF korumasi).")
    return host


def _summarize(response) -> dict:
    content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    size = None
    content_range = response.headers.get("Content-Range") or ""
    total = content_range.rsplit("/", 1)[-1]
    if response.status_code == 206 and total.isdigit():
        size = int(total)
    else:
        length = response.headers.get("Content-Length") or ""
        if length.isdigit() and response.status_code == 200:
            size = int(length)
    return {"status": response.status_code, "content_type": content_type, "size": size}


def _probe(url: str) -> dict:
    """Sadece baslik okur (Range: bytes=0-0, govde okunmaz). Imzali S3/GCS URL'leri
    HEAD'i reddettigi icin GET+Range kullanilir. Her yonlendirme adimi yeniden dogrulanir.

    Not: DNS cozumu ile baglanti arasinda kisa bir TOCTOU penceresi vardir
    (DNS rebinding); bu arac yalnizca bilgi amaclidir, yayin islemini worker yapar."""
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        _assert_public_https(current)
        try:
            response = requests.get(
                current,
                headers={"Range": "bytes=0-0", "Accept": "*/*"},
                stream=True,
                allow_redirects=False,
                timeout=_PROBE_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise MediaProbeError(f"URL'ye ulasilamadi: {exc}") from exc
        try:
            location = response.headers.get("Location")
            if 300 <= response.status_code < 400 and location:
                current = urljoin(current, location)
                continue
            return _summarize(response)
        finally:
            response.close()
    raise MediaProbeError("cok fazla yonlendirme.")


def _parse_platforms(raw: str, action: str) -> list[str] | str:
    platforms = list(dict.fromkeys(p.strip().lower() for p in str(raw or "").split(",") if p.strip()))
    if not platforms:
        return "❌ Hata: en az bir platform belirtin (instagram, youtube, tiktok)."
    invalid = [p for p in platforms if p not in _PLATFORMS]
    if invalid:
        return f"❌ Hata: gecersiz platform(lar): {', '.join(invalid)}. Izin verilenler: {', '.join(_PLATFORMS)}."
    if action == "instagram.carousel" and platforms != ["instagram"]:
        return "❌ Hata: instagram.carousel yalnizca 'instagram' platformunu hedefleyebilir."
    return platforms


def _evaluate(action: str, platforms: list[str], urls: list[str]) -> dict:
    """URL'leri hedefin kisitlarina karsi kontrol eder. Aga cikar (baslik okur)."""
    issues: list[str] = []
    warnings: list[str] = []
    files: list[dict] = []

    if action == "instagram.carousel" and not 2 <= len(urls) <= 10:
        issues.append(f"Instagram carousel 2-10 gorsel ister ({len(urls)} verildi).")
    if action == "video.publish" and len(urls) != 1:
        issues.append(f"video.publish tam olarak 1 video ister ({len(urls)} verildi).")

    for index, url in enumerate(urls, start=1):
        label = f"#{index} {mask_url(url)}"
        try:
            info = _probe(url)
        except MediaProbeError as exc:
            issues.append(f"{label}: {exc}")
            files.append({"url": mask_url(url), "hata": str(exc)})
            continue
        files.append({"url": mask_url(url), "content_type": info["content_type"] or None, "boyut_bayt": info["size"]})

        if info["status"] not in (200, 206):
            issues.append(f"{label}: URL HTTP {info['status']} dondurdu (worker indiremez).")
            continue
        content_type = info["content_type"]
        size = info["size"]

        if action == "video.publish":
            if not content_type.startswith("video/"):
                issues.append(f"{label}: icerik turu '{content_type or 'bilinmiyor'}'; video/* olmali.")
            elif content_type != "video/mp4":
                warnings.append(f"{label}: video/mp4 degil ('{content_type}'); worker yine de mp4 olarak yukler.")
            if size is None and ({"youtube", "tiktok"} & set(platforms)):
                issues.append(f"{label}: sunucu Content-Length vermiyor; YouTube/TikTok yuklemesi bunu sart kosar.")
            if size is not None and "tiktok" in platforms and size > _TIKTOK_MAX_BYTES:
                issues.append(f"{label}: {size // (1024 * 1024)} MB; TikTok tek-parca yukleme siniri 64 MB.")
        else:  # instagram.carousel
            if content_type != "image/jpeg":
                issues.append(f"{label}: icerik turu '{content_type or 'bilinmiyor'}'; Instagram carousel yalnizca JPEG kabul eder.")

    return {"ok": not issues, "issues": issues, "warnings": warnings, "files": files}


def medya_dogrula(url: str, aksiyon: str = "video.publish", platformlar: str = "instagram,youtube,tiktok") -> str:
    """
    Bir medyanin (HTTPS URL) yayin hedefi icin uygun olup olmadigini dogrular:
    erisilebilirlik, icerik turu, boyut ve worker'in bekledigi kisitlar. Yayin
    yapmaz. URL'ler ciktida maskelenir.

    Args:
        url: Medya URL'si. instagram.carousel icin virgulle ayrilmis 2-10 gorsel URL'si.
        aksiyon: video.publish veya instagram.carousel.
        platformlar: Virgulle ayrilmis hedef platformlar (instagram, youtube, tiktok).
    """
    action = str(aksiyon or "").strip()
    if action not in _ACTIONS:
        return f"❌ Hata: aksiyon {' veya '.join(_ACTIONS)} olmali."
    platforms = _parse_platforms(platformlar, action)
    if isinstance(platforms, str):
        return platforms
    urls = [u.strip() for u in str(url or "").split(",") if u.strip()]
    if not urls:
        return "❌ Hata: url bos olamaz."
    if len(urls) > 10:
        return "❌ Hata: en fazla 10 URL dogrulanabilir."

    report = _evaluate(action, platforms, urls)
    return json.dumps(
        {"uygun": report["ok"], "aksiyon": action, "platformlar": platforms, "sorunlar": report["issues"], "uyarilar": report["warnings"], "dosyalar": report["files"]},
        ensure_ascii=False,
        indent=2,
    )


def _fetch_approved_asset(asset_id: str):
    """(asset | None, hata_metni | None). 404 = yok ya da App onaylamamis."""
    status, data, error = app_request("GET", f"{assets_path()}/{quote(asset_id, safe='')}")
    if error:
        return None, error
    if status == 404:
        return None, f"'{asset_id}' bulunamadi ya da App tarafindan onaylanmamis; onaysiz varlik yayinlanamaz."
    if status >= 400:
        detail = data.get("error") if isinstance(data, dict) else f"HTTP {status}"
        return None, f"❌ App istegi basarisiz ('{asset_id}'): {detail}"
    asset = normalize_asset(data.get("asset") if isinstance(data, dict) and "asset" in data else data)
    if not asset:
        return None, f"❌ '{asset_id}' icin gecersiz veya sema disi varlik verisi geldi."
    return asset, None


def medya_hazirla(asset_ids: str, aksiyon: str = "video.publish", platformlar: str = "youtube", ttl_saniye: int = _DEFAULT_TTL) -> str:
    """
    App'in ONAYLI bir varligini yayin icin hazirlar: varsa App'in prepare
    endpoint'iyle taze/kisa omurlu bir baglanti (ve gerekirse donusturulmus format)
    alir, sonra hedefin kisitlarina karsi dogrular. Basariliysa yayin araclarina
    verilecek bir `media_ref` dondurur (imzali URL modele ASLA gosterilmez).

    Args:
        asset_ids: app_asset_listele'den varlik id'si. instagram.carousel icin virgulle ayrilmis 2-10 gorsel id'si.
        aksiyon: video.publish veya instagram.carousel.
        platformlar: Virgulle ayrilmis hedef platformlar (video icin instagram,youtube,tiktok).
        ttl_saniye: Baglantinin istenen omru (60-86400). Worker isi gec alabilir; makul bir sure birak.
    """
    action = str(aksiyon or "").strip()
    if action not in _ACTIONS:
        return f"❌ Hata: aksiyon {' veya '.join(_ACTIONS)} olmali."
    platforms = _parse_platforms(platformlar, action)
    if isinstance(platforms, str):
        return platforms
    ids = list(dict.fromkeys(i.strip() for i in str(asset_ids or "").split(",") if i.strip()))
    if not ids:
        return "❌ Hata: asset_ids bos olamaz."
    if action == "video.publish" and len(ids) != 1:
        return "❌ Hata: video.publish tam olarak 1 video varligi ister."
    if action == "instagram.carousel" and not 2 <= len(ids) <= 10:
        return "❌ Hata: instagram.carousel 2-10 gorsel varligi ister."
    try:
        ttl = max(_MIN_TTL, min(int(ttl_saniye), _MAX_TTL))
    except (TypeError, ValueError):
        ttl = _DEFAULT_TTL

    wanted_kind = "image" if action == "instagram.carousel" else "video"
    target_platform = platforms[0]
    issues: list[str] = []
    notes: list[str] = []
    urls: list[str] = []
    expiries: list[str] = []

    for asset_id in ids:
        asset, error = _fetch_approved_asset(asset_id)
        if error:
            return error if error.startswith("❌") else f"❌ Hata: {error}"
        if asset["kind"] != wanted_kind:
            issues.append(f"'{asset_id}' bir {asset['kind']}; {action} icin {wanted_kind} gerekir.")
            continue

        status, data, request_error = app_request(
            "POST",
            f"{assets_path()}/{quote(asset_id, safe='')}/prepare",
            json_body={"target": {"platform": target_platform, "action": action}, "ttlSeconds": ttl},
        )
        if request_error:
            return request_error
        if status == 200:
            prepared = normalize_asset(data)
            if not prepared or prepared["kind"] != wanted_kind:
                issues.append(f"'{asset_id}': App gecersiz veya hedefe uymayan bir hazirlanmis varlik dondurdu.")
                continue
            asset = prepared
        elif status == 422:
            reasons = data.get("reasons") if isinstance(data, dict) else None
            fallback = data.get("error") if isinstance(data, dict) else None
            issues.append(f"'{asset_id}': App bu varligi hedef icin hazirlayamadi: " + ("; ".join(map(str, reasons)) if reasons else (fallback or "neden belirtilmedi")))
            continue
        elif status in (404, 405, 501):
            notes.append("App'in prepare endpoint'i yok; varlik listelendigi haliyle kullanildi.")
        else:
            detail = data.get("error") if isinstance(data, dict) else f"HTTP {status}"
            return f"❌ App istegi basarisiz (prepare, '{asset_id}'): {detail}"

        urls.append(asset["url"])
        if asset.get("expiresAt"):
            expiries.append(asset["expiresAt"])

    if issues:
        return json.dumps({"hazir": False, "aksiyon": action, "platformlar": platforms, "sorunlar": issues, "notlar": sorted(set(notes))}, ensure_ascii=False, indent=2)

    report = _evaluate(action, platforms, urls)
    if not report["ok"]:
        return json.dumps({"hazir": False, "aksiyon": action, "platformlar": platforms, "sorunlar": report["issues"], "uyarilar": report["warnings"], "notlar": sorted(set(notes)), "dosyalar": report["files"]}, ensure_ascii=False, indent=2)

    parsed_expiries = []
    for value in expiries:
        try:
            parsed_expiries.append(parse_dt(value))
        except ValueError:
            continue
    earliest = min(parsed_expiries).isoformat() if parsed_expiries else None
    ref = register(action=action, platforms=platforms, urls=urls, kind=wanted_kind, expires_at=earliest)

    hint = (
        f'worker_video_yayinla(media_ref="{ref}", platforms="{",".join(platforms)}", baslik=..., caption=...)'
        if action == "video.publish"
        else f'worker_instagram_carousel_yayinla(media_ref="{ref}", caption=...)'
    )
    return json.dumps(
        {
            "hazir": True,
            "media_ref": ref,
            "aksiyon": action,
            "platformlar": platforms,
            "dosya_sayisi": len(urls),
            "gecerlilik_bitisi": earliest,
            "uyarilar": report["warnings"],
            "notlar": sorted(set(notes)),
            "dosyalar": report["files"],
            "sonraki_adim": hint,
        },
        ensure_ascii=False,
        indent=2,
    )
