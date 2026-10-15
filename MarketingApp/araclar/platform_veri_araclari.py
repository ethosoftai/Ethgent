"""
Platform Veri Araclari — future_work.md'nin `collect_platform_data` yetenegi:
istenen metrik/yorum/hesap verisini, manifest-onayli SALT-OKUNUR API
aksiyonlari uzerinden toplar.

Mimari (platform token'lari agent'a ASLA girmez):

    agent ──insert──▶ platform_data_jobs ──claim──▶ platform_data_worker
                                                        │ manifest allowlist'ine karsi dogrular,
                                                        │ toolbox'i (kendi kimlik bilgileriyle) calistirir
    agent ◀──select── sonuc (results) ◀─────────────────┘

Agent yalnizca {platform, action, params} yazar ve sonucu okur. Neyin calisabilecegi
`toolboxes/<platform>/manifest.yaml` altindaki `data_collection.actions` allowlist'inde
yazilidir; listede olmayan (ve her yazma) aksiyon YOKTUR. Agent ayni kurallarla
hizli bir on-kontrol yapar ama yetkili olan worker'dir (yurutme aninda yeniden dogrular).
marketing-agent-assets/platform_data_worker/policy.py ile ayni kurallar: aralarindaki esitlik
marketing-agent-assets/platform_data_worker/tests/
test_agent_policy_parity.py ile test edilir.

Kisisel veri: `personal_data: true` aksiyonlar baskalarinin icerigini (yorum yazarlari,
gonderi metinleri) dondurur. Cikti buna gore isaretlenir; gerekmedikce alintilama,
kalici kaydetme ya da bir kisiyi hedef alan cikarim yapma.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests
import yaml

from MarketingApp.environments import supabase_rest
from MarketingApp.paths import workspace_path

_PLATFORMS = ("youtube", "instagram", "tiktok", "x", "reddit")
_TIMEOUT = 15
_MAX_WAIT = 60
_OUTPUT_CHARS = 6000
_JOB_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def _toolboxes_dir() -> Path:
    override = (os.getenv("TOOLBOXES_DIR") or "").strip()
    # Toolbox manifest'leri bu depoda degil, baglanti katmaninda (marketing-agent-assets/toolboxes).
    return Path(override) if override else Path.cwd() / "toolboxes"


def _load_catalog() -> dict[tuple[str, str], dict[str, Any]] | str:
    """{(platform, action): spec} ya da kullanici-okur hata metni."""
    root = _toolboxes_dir()
    catalog: dict[tuple[str, str], dict[str, Any]] = {}
    for platform in _PLATFORMS:
        path = root / platform / "manifest.yaml"
        if not path.is_file():
            continue
        try:
            manifest = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            return f"❌ Hata: {path} okunamadi: {exc}"
        actions = ((manifest.get("data_collection") or {}).get("actions")) or {}
        for action_id, spec in actions.items():
            if isinstance(spec, dict):
                catalog[(platform, str(action_id))] = spec
    if not catalog:
        return ("❌ Hata: manifest-onayli veri aksiyonu bulunamadi. toolboxes/*/manifest.yaml "
                "'data_collection' bolumunu ve TOOLBOXES_DIR degiskenini kontrol edin.")
    return catalog


def _present(value: Any) -> bool:
    return value not in (None, "", False)


def _coerce(name: str, spec: dict[str, Any], value: Any) -> Any:
    kind = spec.get("type")
    if kind == "string":
        if not isinstance(value, str):
            raise ValueError(f"param '{name}' must be a string")
        value = value.strip()
        if spec.get("max_length") is not None and len(value) > spec["max_length"]:
            raise ValueError(f"param '{name}' must be at most {spec['max_length']} characters")
        if spec.get("enum") and value not in spec["enum"]:
            raise ValueError(f"param '{name}' must be one of {', '.join(spec['enum'])}")
        if spec.get("pattern") and not re.fullmatch(spec["pattern"], value):
            raise ValueError(f"param '{name}' has an invalid format")
        return value
    if kind == "integer":
        if isinstance(value, bool):
            raise ValueError(f"param '{name}' must be an integer")
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
            value = int(value.strip())
        if not isinstance(value, int):
            raise ValueError(f"param '{name}' must be an integer")
        if spec.get("minimum") is not None and value < spec["minimum"]:
            raise ValueError(f"param '{name}' must be >= {spec['minimum']}")
        if spec.get("maximum") is not None and value > spec["maximum"]:
            raise ValueError(f"param '{name}' must be <= {spec['maximum']}")
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ValueError(f"param '{name}' must be a boolean")


def validate_request(catalog: dict, platform: str, action: str, params: Any) -> tuple[dict, dict]:
    """(spec, normalized params) ya da ValueError. platform_data_worker/policy.py ile ayni kurallar."""
    spec = catalog.get((str(platform), str(action)))
    if spec is None:
        raise ValueError(f"'{platform}.{action}' is not a manifest-approved data collection action")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    declared = spec.get("params") or {}
    unknown = sorted(set(params) - set(declared))
    if unknown:
        raise ValueError(f"unknown param(s) {unknown} for {platform}.{action}; allowed: {', '.join(sorted(declared)) or '(none)'}")
    clean: dict[str, Any] = {}
    for name, param in declared.items():
        if name not in params or params[name] in (None, ""):
            if param.get("required"):
                raise ValueError(f"param '{name}' is required for {platform}.{action}")
            continue
        clean[name] = _coerce(name, param, params[name])
    one_of = spec.get("require_one_of") or []
    if one_of and not any(_present(clean.get(name)) for name in one_of):
        raise ValueError(f"{platform}.{action} needs at least one of: {', '.join(one_of)}")
    return spec, clean


def platform_veri_eylemleri(platform: str = "") -> str:
    """
    Toplanabilecek platform verisi aksiyonlarinin (manifest-onayli, salt-okunur)
    listesini dondurur: her aksiyonun aciklamasi, parametreleri ve limitleri.
    Buradaki listenin disinda hicbir aksiyon calistirilamaz.

    Args:
        platform: Filtre: youtube, instagram, tiktok, x veya reddit (bos = hepsi).
    """
    catalog = _load_catalog()
    if isinstance(catalog, str):
        return catalog
    wanted = str(platform or "").strip().lower()
    if wanted and wanted not in _PLATFORMS:
        return f"❌ Hata: platform {', '.join(_PLATFORMS)} olmali."
    listing = []
    for (plat, action), spec in sorted(catalog.items()):
        if wanted and plat != wanted:
            continue
        listing.append({
            "eylem": f"{plat}.{action}",
            "aciklama": spec.get("description", ""),
            "kisisel_veri_icerir": bool(spec.get("personal_data")),
            "saatlik_limit": spec.get("rate_limit_per_hour"),
            "en_az_biri_gerekli": spec.get("require_one_of") or None,
            "parametreler": {
                name: {k: v for k, v in param.items() if k in ("type", "required", "enum", "minimum", "maximum", "max_length")}
                for name, param in (spec.get("params") or {}).items()
            },
        })
    return json.dumps({"eylemler": listing}, ensure_ascii=False, indent=2)


def _format_result(job: dict, spec: dict | None) -> str:
    status = job.get("status")
    results = job.get("results") or {}
    job_id = job.get("id")
    if status == "failed":
        reason = job.get("error") or results.get("error") or "bilinmeyen hata"
        return f"❌ Veri toplama basarisiz (job_id={job_id}): {reason}"
    if status != "done":
        return f"⏳ job_id={job_id} durum={status}; worker henuz bitirmedi. platform_veri_durumu(job_id) ile tekrar kontrol et."

    data = results.get("data")
    text = json.dumps(data, ensure_ascii=False, indent=1, default=str)
    lines = [f"✅ Veri toplandi (job_id={job_id}, {results.get('platform')}.{results.get('action')})."]
    if results.get("truncated"):
        lines.append("ℹ️ Worker sonucu boyut sinirina gore kirpti.")
    if spec and spec.get("personal_data"):
        lines.append("⚠️ Bu veri baskalarinin icerigini (yorum yazarlari/metinler) icerir: gerekmedikce alintilama, "
                     "kalici kaydetme ya da bir kisiyi hedef alan cikarim yapma.")
    if len(text) > _OUTPUT_CHARS:
        saved = None
        try:
            directory = Path(workspace_path("reports"))
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"platform_veri_{re.sub(r'[^0-9A-Za-z-]', '', str(job_id))}.json"
            target.write_text(text, encoding="utf-8")
            saved = f"reports/{target.name}"
        except OSError:
            pass
        lines.append(f"Sonuc {len(text)} karakter; ilk {_OUTPUT_CHARS} gosteriliyor" + (f" (tamami workspace/{saved})." if saved else "."))
        text = text[:_OUTPUT_CHARS] + "\n[... kirpildi ...]"
    lines.append(text)
    return "\n".join(lines)


def _get_job(base_url: str, key: str, job_id: str) -> dict | None:
    response = requests.get(
        f"{base_url}/rest/v1/platform_data_jobs",
        headers=supabase_rest.headers(key),
        params={"id": f"eq.{job_id}", "select": "id,status,results,error", "limit": "1"},
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    rows = response.json()
    return rows[0] if isinstance(rows, list) and rows else None


def platform_veri_topla(platform: str, eylem: str, parametreler: str = "{}", bekleme_saniye: int = 25) -> str:
    """
    Bir platformdan manifest-onayli, SALT-OKUNUR veri toplar (metrik, yorum,
    hesap/icerik bilgisi). Istegi platform_data_worker'a kuyruk uzerinden
    iletir; platform kimlik bilgilerini yalnizca worker tutar. Once
    platform_veri_eylemleri ile neyin toplanabildigine bak.

    Args:
        platform: youtube, instagram, tiktok, x veya reddit.
        eylem: platform_veri_eylemleri'ndeki eylem adi (orn. "comment_threads", "media_insights").
        parametreler: JSON nesnesi (orn. {"video_id_or_url": "abc", "max_results": 20}). Bildirilmeyen parametre reddedilir.
        bekleme_saniye: Sonucu en fazla kac saniye beklesin (0-60). Bitmezse job_id doner.
    """
    catalog = _load_catalog()
    if isinstance(catalog, str):
        return catalog
    try:
        params = json.loads(parametreler) if str(parametreler or "").strip() else {}
    except json.JSONDecodeError as exc:
        return f"❌ Hata: parametreler gecerli bir JSON nesnesi olmali ({exc.msg})."

    platform_name, action = str(platform or "").strip().lower(), str(eylem or "").strip()
    try:
        spec, clean = validate_request(catalog, platform_name, action, params)
    except ValueError as exc:
        return f"❌ Hata: {exc}"

    config = supabase_rest.config()
    if isinstance(config, str):
        return config
    base_url, key = config

    try:
        wait = max(0, min(int(bekleme_saniye), _MAX_WAIT))
    except (TypeError, ValueError):
        wait = 25

    try:
        response = requests.post(
            f"{base_url}/rest/v1/platform_data_jobs",
            headers=supabase_rest.headers(key, prefer="return=representation"),
            json={"payload": {"platform": platform_name, "action": action, "params": clean}, "owner_ref": "marketing-agent"},
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        rows = response.json()
        job_id = rows[0]["id"] if isinstance(rows, list) and rows else None
    except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
        return f"❌ Veri istegi kuyruga eklenemedi: {exc}"
    if not job_id:
        return "❌ Veri istegi kuyruga eklendi ama job_id alinamadi."

    deadline = time.monotonic() + wait
    job = None
    while True:
        try:
            job = _get_job(base_url, key, job_id)
        except (requests.RequestException, ValueError) as exc:
            return f"❌ job durumu okunamadi (job_id={job_id}): {exc}"
        if job and job.get("status") in ("done", "failed"):
            return _format_result(job, spec)
        if time.monotonic() >= deadline:
            break
        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
    return (f"⏳ Istek kuyrukta/isleniyor (job_id={job_id}, durum={(job or {}).get('status', 'queued')}). "
            "platform_data_worker calisiyor mu? platform_veri_durumu(job_id) ile tekrar kontrol et.")


def platform_veri_durumu(job_id: str) -> str:
    """
    platform_veri_topla'nin dondurdugu job_id icin sonucu/durumu sorgular.

    Args:
        job_id: platform_veri_topla'dan donen job_id.
    """
    clean = str(job_id or "").strip()
    if not _JOB_ID.match(clean):
        return "❌ Hata: job_id gecersiz."
    config = supabase_rest.config()
    if isinstance(config, str):
        return config
    base_url, key = config
    try:
        job = _get_job(base_url, key, clean)
    except (requests.RequestException, ValueError) as exc:
        return f"❌ job durumu okunamadi: {exc}"
    if not job:
        return f"❌ '{clean}' id'li bir veri toplama isi bulunamadi."
    catalog = _load_catalog()
    spec = None
    if isinstance(catalog, dict):
        results = job.get("results") or {}
        spec = catalog.get((str(results.get("platform")), str(results.get("action"))))
    return _format_result(job, spec)
