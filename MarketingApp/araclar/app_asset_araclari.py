"""
App Asset Araclari — future_work.md'nin "Planned worker environment
interface" #1 maddesindeki `collect_assets` yetenegi icin ilk somut adim:
App'in onayladigi medya varliklarini (schemas/asset.schema.json) okuyup
ajana getirir.

Onemli sinirlar (future_work.md sorumluluk haritasiyla tutarli):
  - Bu araclar SADECE OKUR. App'e yazma, onay durumu degistirme veya medya
    yukleme yapmazlar — o App katmaninin sorumlulugunda kalir.
  - Platform token'lari, DB credential'lari veya imzali URL'ler bu araclar
    uzerinden asla LLM prompt'una girmez: App'in dondurdugu URL'nin sorgu
    kismi (imza) ciktida ve onbellekte MASKELENIR (`https://host/yol?…`).
    Yayin icin gereken gercek URL'yi model gormez; `medya_hazirla` onu bir
    `media_ref` kimligiyle saklar ve publish tool'lari ondan cozer.
  - future_work.md'deki App HTTP yuzeyi bu yazildigi anda henuz canli
    degil (bkz. AGENT.md "honest scope"). APP_INTERNAL_URL tanimsizsa
    araclar net bir yapilandirma hatasi doner, sessizce basarisiz olmaz —
    App bu sozlesmeyi uyguladigi anda ayarlari eklemek yeterli olur.

Beklenen App sozlesmesi (tam metin: marketing-agent-assets/asset-pool/README.md):
  GET {APP_INTERNAL_URL}{APP_ASSETS_PATH}?kind=&tag=&limit=
      -> {"assets": [Asset, ...]}  (veya dogrudan [Asset, ...])
  GET {APP_INTERNAL_URL}{APP_ASSETS_PATH}/{id}
      -> Asset  (veya {"asset": Asset})
  Auth: Authorization: Bearer {APP_INTERNAL_TOKEN}
  APP_ASSETS_PATH varsayilan "/api/assets"; bir app havuzu baska bir yola
  mount ediyorsa (orn. "/asset-pool/v1") sadece bu degeri degistirmek yeterli.
  Ayni sozlesmeyi uygulayan HERHANGI bir app baglanabilir; bkz. asset-pool/.

  Asset alanlari schemas/asset.schema.json ile birebir: id, kind
  (video|image|audio|document), url (https), contentType, title, altText,
  expiresAt, metadata.
"""

from __future__ import annotations

import json
import os
from urllib.parse import quote

import requests

from MarketingApp.environments.media_registry import mask_url
from MarketingApp.paths import workspace_path

_ALLOWED_ASSET_KINDS = {"video", "image", "audio", "document"}
_APP_TIMEOUT_SECONDS = 15
_CATALOG_PATH = workspace_path("assets", "app_asset_catalog.json")


def _app_config() -> tuple[str, str] | str:
    """(base_url, token) dondurur; eksikse kullanici-okur bir hata metni dondurur."""
    url = (os.getenv("APP_INTERNAL_URL") or "").strip().rstrip("/")
    token = (os.getenv("APP_INTERNAL_TOKEN") or "").strip()
    if not url:
        return (
            "❌ Hata: APP_INTERNAL_URL tanimli degil. App'in asset koleksiyonu "
            "henuz bu ajana baglanmadi; App hazir oldugunda .env dosyasina "
            "APP_INTERNAL_URL (ve varsa APP_INTERNAL_TOKEN) ekleyin."
        )
    return url, token


def assets_path() -> str:
    """Havuzun App uzerindeki yolu (APP_ASSETS_PATH, varsayilan /api/assets)."""
    raw = (os.getenv("APP_ASSETS_PATH") or "/api/assets").strip()
    return "/" + raw.strip("/")


def app_request(method: str, path: str, *, params: dict | None = None, json_body: dict | None = None,
                headers: dict | None = None, timeout: int = _APP_TIMEOUT_SECONDS):
    """App'e bir istek atar. Dondurur: (status_code | None, json | None, hata_metni | None).

    Ag/yapilandirma hatasinda status None ve hata metni dolu; HTTP hatalarinda
    (4xx/5xx) durum kodu ve varsa JSON govde DONER, firlatilmaz -- cagiran taraf
    404/422/501 gibi kodlari kendisi yorumlar."""
    config = _app_config()
    if isinstance(config, str):
        return None, None, config

    base_url, token = config
    request_headers = {"Accept": "application/json", **(headers or {})}
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.request(
            method,
            f"{base_url}{path}",
            headers=request_headers,
            params=params or None,
            json=json_body,
            timeout=timeout,
        )
    except Exception as exc:
        return None, None, f"❌ App'e ulasilamadi ({path}): {exc}"
    try:
        data = response.json()
    except ValueError:
        data = None
    return response.status_code, data, None


def _error_detail(status: int, data) -> str:
    if isinstance(data, dict) and data.get("error"):
        return str(data["error"])[:500]
    return f"HTTP {status}"


def _app_get(path: str, params: dict | None = None):
    """GET; basarisizsa kullanici-okur bir hata metni (str) dondurur, basariliysa JSON."""
    status, data, error = app_request("GET", path, params=params)
    if error:
        return error
    if status >= 400:
        return f"❌ App istegi basarisiz ({path}): {_error_detail(status, data)}"
    if data is None:
        return f"❌ App istegi basarisiz ({path}): yanit JSON degil."
    return data


def normalize_asset(raw) -> dict | None:
    """schemas/asset.schema.json'a gore temel dogrulama; gecersizse None dondurur."""
    if not isinstance(raw, dict):
        return None
    asset_id = str(raw.get("id") or "").strip()
    kind = str(raw.get("kind") or "").strip()
    url = str(raw.get("url") or "").strip()
    if not asset_id or kind not in _ALLOWED_ASSET_KINDS or not url.lower().startswith("https://"):
        return None
    metadata = raw.get("metadata")
    return {
        "id": asset_id,
        "kind": kind,
        "url": url,
        "contentType": str(raw.get("contentType") or "").strip(),
        "title": str(raw.get("title") or "").strip(),
        "altText": str(raw.get("altText") or "").strip(),
        "expiresAt": str(raw.get("expiresAt") or "").strip(),
        "metadata": metadata if isinstance(metadata, dict) else {},
    }


def _extract_asset_list(payload) -> list[dict]:
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, dict):
        raw_items = payload.get("assets") or payload.get("data") or []
    else:
        raw_items = []
    if not isinstance(raw_items, list):
        raw_items = []
    normalized = [normalize_asset(item) for item in raw_items]
    return [item for item in normalized if item is not None]


def _cache_catalog(assets: list[dict]) -> None:
    """Son basarili listeleme sonucunu workspace'e onbellekler (best-effort)."""
    try:
        os.makedirs(os.path.dirname(_CATALOG_PATH), exist_ok=True)
        safe = [{**asset, "url": mask_url(asset["url"])} for asset in assets]
        with open(_CATALOG_PATH, "w", encoding="utf-8") as f:
            json.dump({"assets": safe}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # onbellek hatasi asset listelemeyi bloklamamali


def _format_asset_line(asset: dict) -> str:
    parcalar = [f"- id={asset['id']} kind={asset['kind']}"]
    if asset["title"]:
        parcalar.append(f'title="{asset["title"]}"')
    parcalar.append(f"url={mask_url(asset['url'])}")
    if asset["expiresAt"]:
        parcalar.append(f"expiresAt={asset['expiresAt']}")
    return " ".join(parcalar)


def app_baglanti_durumu() -> str:
    """
    App'in asset koleksiyonu icin gerekli yapilandirmanin (APP_INTERNAL_URL /
    APP_INTERNAL_TOKEN) tanimli olup olmadigini kontrol eder. Gercek bir
    varlik istegi atmadan once hizli bir on-kontrol icin kullan.
    """
    config = _app_config()
    if isinstance(config, str):
        return config
    base_url, token = config
    return (
        f"✅ App baglanti bilgisi tanimli: {base_url} "
        f"(token: {'var' if token else 'YOK — App bunu zorunlu kilabilir'})."
    )


def app_asset_listele(kind: str = "", tag: str = "", limit: int = 20) -> str:
    """
    App'in onayladigi medya varliklarini (video/image/audio/document) listeler.
    Sonuclar schemas/asset.schema.json'a gore dogrulanir; sema disi/eksik
    veri iceren kayitlar sessizce elenir. App henuz baglanmadiysa
    (APP_INTERNAL_URL tanimsiz) net bir yapilandirma hatasi doner.
    Basarili sonuc ayrica workspace/assets/app_asset_catalog.json'a onbelleklenir.

    Args:
        kind: Filtre: video, image, audio veya document (bos = hepsi).
        tag: App tarafinda desteklenen ozgur metin/tag filtresi (opsiyonel).
        limit: Donecek maksimum varlik sayisi (varsayilan 20, en fazla 100).
    """
    if kind and kind not in _ALLOWED_ASSET_KINDS:
        return f"❌ Hata: kind '{kind}' gecersiz. Izin verilenler: {', '.join(sorted(_ALLOWED_ASSET_KINDS))}."

    try:
        limit = max(1, min(int(limit), 100))
    except Exception:
        limit = 20

    params = {"limit": limit}
    if kind:
        params["kind"] = kind
    if tag:
        params["tag"] = tag

    payload = _app_get(assets_path(), params=params)
    if isinstance(payload, str):
        return payload

    assets = _extract_asset_list(payload)
    if not assets:
        return "ℹ️ App bu filtrelerle onayli varlik dondurmedi (veya sema disi veri gorezlendi)."

    _cache_catalog(assets)

    lines = [
        f"📦 App'ten {len(assets)} onayli varlik alindi "
        "(workspace/assets/app_asset_catalog.json'a onbelleklendi):"
    ]
    lines.extend(_format_asset_line(asset) for asset in assets)
    return "\n".join(lines)


def app_asset_detay(asset_id: str) -> str:
    """
    Tek bir onayli App varliginin tam metadata'sini (contentType, altText,
    expiresAt, metadata) getirir.

    Args:
        asset_id: app_asset_listele'den donen varlik id'si.
    """
    clean_id = (asset_id or "").strip()
    if not clean_id:
        return "❌ Hata: asset_id bos olamaz."

    payload = _app_get(f"{assets_path()}/{quote(clean_id, safe='')}")
    if isinstance(payload, str):
        return payload

    raw = payload.get("asset") if isinstance(payload, dict) and "asset" in payload else payload
    asset = normalize_asset(raw)
    if not asset:
        return f"❌ Hata: '{clean_id}' icin gecersiz veya sema disi varlik verisi geldi."

    return json.dumps({**asset, "url": mask_url(asset["url"])}, ensure_ascii=False, indent=2)
