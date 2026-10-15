"""Config-driven agent, custom tool and agent pack helpers for Agent Studio."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
import re
import ast
import shutil
import subprocess
import sys
import tempfile
import types
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import yaml
except Exception:  # pragma: no cover - handled at runtime
    yaml = None


from MarketingApp.paths import CONFIG_DIR, WORKSPACE_DIR

# APP_DIR: bu paketin GERCEK kod konumu (workspace/config icin degil, kod-goreceli
# yollar -- SUBMODELS_DIR, MODEL_ENV_PATH, pack kok cozumleme -- icin kullanilir).
# Injectable workspace/config dizinleri icin MarketingApp.paths kullan.
APP_DIR = Path(__file__).resolve().parents[1]
CUSTOM_TOOLS_DIR = WORKSPACE_DIR / "custom_tools"
AGENT_PACKS_DIR = WORKSPACE_DIR / "agent_packs"
SUBMODELS_DIR = APP_DIR / "llms" / "SubModels"
SUBMODELS_INIT_PATH = SUBMODELS_DIR / "__init__.py"
AGENTS_CONFIG_PATH = CONFIG_DIR / "agents.yaml"
CUSTOM_TOOLS_CONFIG_PATH = CONFIG_DIR / "custom_tools.yaml"
AGENT_PACKS_CONFIG_PATH = CONFIG_DIR / "agent_packs.yaml"
ORCHESTRATOR_PROMPT_PATH = CONFIG_DIR / "orchestrator_prompt.md"
MODEL_ENV_PATH = APP_DIR.parent / ".env.model"

AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,80}$")
PACK_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
VALID_AGENT_TYPES = {"builtin", "config"}
VALID_TOOL_MODES = {"default", "custom"}
VALID_PACK_TYPES = {"tool_pack", "agent_bundle", "runtime_pack"}
RECOMMENDED_MEMORY_TOOLS = ["context_paketi_oku", "context_aksiyon_kaydet"]
AGENT_STUDIO_BUILTIN_MARKER = "# Agent Studio generated builtin submodel"

DEFAULT_BUILTIN_AGENTS = [
    {
        "name": "sosyal_medya_agent",
        "type": "builtin",
        "enabled": True,
        "description": "X, Instagram ve YouTube uzerinde yayinlama, etkilesim, bildirim tarama ve sosyal operasyon ajani.",
        "model": "default",
        "tool_mode": "default",
        "system_prompt": "",
        "tools": [],
    },
    {
        "name": "content_creator_agent",
        "type": "builtin",
        "enabled": True,
        "description": "Caption, medya arama, website icerigi, HTML/CSS PNG post ve MP4 video uretim ajani.",
        "model": "default",
        "tool_mode": "default",
        "system_prompt": "",
        "tools": [],
    },
    {
        "name": "browser_agent",
        "type": "builtin",
        "enabled": True,
        "description": "Selenium tabanli web gezinme, DOM okuma ve form etkilesimi ajani.",
        "model": "browser_default",
        "tool_mode": "default",
        "system_prompt": "",
        "tools": [],
    },
    {
        "name": "arastirma_agent",
        "type": "builtin",
        "enabled": False,
        "description": "Derinlemesine web arastirmasi ve kapsamli rapor yazma uzmani (Gemini Live API).",
        "model": "default",
        "tool_mode": "default",
        "system_prompt": "",
        "tools": [],
    },
    {
        "name": "sistem_agent",
        "type": "builtin",
        "enabled": False,
        "description": "Dosya yonetimi, sistem analizi ve kendini gelistirme uzmani (Gemini Live API).",
        "model": "default",
        "tool_mode": "default",
        "system_prompt": "",
        "tools": [],
    },
    {
        "name": "vlm_agent",
        "type": "builtin",
        "enabled": False,
        "description": "Ekran, klavye ve fare kontrol uzmani (Gemini Live API).",
        "model": "default",
        "tool_mode": "default",
        "system_prompt": "",
        "tools": [],
    },
]
BUILTIN_AGENT_NAMES = {item["name"] for item in DEFAULT_BUILTIN_AGENTS}


def _builtin_module_path(name: str) -> Path:
    return SUBMODELS_DIR / f"{validate_agent_name(name)}.py"


def _is_scaffolded_builtin_agent(name: str) -> bool:
    try:
        path = _builtin_module_path(name)
        return path.exists() and AGENT_STUDIO_BUILTIN_MARKER in path.read_text(encoding="utf-8")[:500]
    except Exception:
        return False


def is_known_builtin_agent_name(name: str) -> bool:
    return name in BUILTIN_AGENT_NAMES or _is_scaffolded_builtin_agent(name)


def _runtime_system_prompt_note() -> str:
    return (
        "\n\n[Not: Runtime sirasinda role.md marketing kisiligi uygunsa bu prompt'un sonuna "
        "dinamik olarak eklenir.]"
    )


def default_system_prompt_placeholder(agent: dict[str, Any] | None = None) -> str:
    name = (agent or {}).get("name") or ""
    agent_type = (agent or {}).get("type") or "config"
    try:
        if name == "content_creator_agent":
            from .SubModels.content_creator_agent import DEFAULT_SYSTEM_PROMPT

            return DEFAULT_SYSTEM_PROMPT + _runtime_system_prompt_note()
        if name == "sosyal_medya_agent":
            from .SubModels.sosyal_medya_agent import DEFAULT_SYSTEM_PROMPT

            return DEFAULT_SYSTEM_PROMPT + _runtime_system_prompt_note()
        if name == "browser_agent":
            from .SubModels.browser_agent import DEFAULT_SYSTEM_PROMPT

            return DEFAULT_SYSTEM_PROMPT + _runtime_system_prompt_note()
        if agent_type == "builtin" and _is_scaffolded_builtin_agent(name):
            module_name = f"MarketingApp.llms.SubModels.{name}"
            module = importlib.import_module(module_name)
            prompt = getattr(module, "DEFAULT_SYSTEM_PROMPT", "")
            if prompt:
                return str(prompt)
        if agent_type == "config":
            from .SubModels.generic_config_agent import build_generic_config_system_prompt

            return build_generic_config_system_prompt("")
    except Exception:
        pass
    return "Agent rolünü, karar kurallarını ve tool kullanım ilkelerini yaz."


class AgentStudioError(ValueError):
    """Raised for user-facing Agent Studio validation errors."""


def _yaml_available() -> bool:
    return yaml is not None


def _ensure_yaml():
    if not _yaml_available():
        raise AgentStudioError("PyYAML yuklu degil; Agent Studio YAML config okuyamiyor.")


def _read_yaml(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    _ensure_yaml()
    if not path.exists():
        return dict(fallback)
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        raise AgentStudioError(f"{path.name} okunamadi: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentStudioError(f"{path.name} kok verisi dict olmali.")
    return data


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    _ensure_yaml()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def _default_agents_config() -> dict[str, Any]:
    return {
        "version": 1,
        "global_disabled_tools": [],
        "agents": [dict(item) for item in DEFAULT_BUILTIN_AGENTS],
    }


def _default_custom_tools_config() -> dict[str, Any]:
    return {"version": 1, "custom_tools": []}


def _default_agent_packs_config() -> dict[str, Any]:
    return {"version": 1, "installed_packs": []}


def ensure_agent_studio_files() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CUSTOM_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    AGENT_PACKS_DIR.mkdir(parents=True, exist_ok=True)
    if not AGENTS_CONFIG_PATH.exists():
        _write_yaml(AGENTS_CONFIG_PATH, _default_agents_config())
    if not CUSTOM_TOOLS_CONFIG_PATH.exists():
        _write_yaml(CUSTOM_TOOLS_CONFIG_PATH, _default_custom_tools_config())
    if not AGENT_PACKS_CONFIG_PATH.exists():
        _write_yaml(AGENT_PACKS_CONFIG_PATH, _default_agent_packs_config())


def read_orchestrator_prompt() -> str:
    """Orkestratorun (BaseModel) system prompt override'ini okur; yoksa bos dondurur.

    Ayri bir dosyada tutulur (agents.yaml'in icinde degil) cunku save_agents_config()
    her ajan duzenlemesinde tum dosyayi yeniden yazar -- oraya eklenen bir alan,
    onu bilmeyen onlarca cagri noktasi tarafindan sessizce silinirdi.
    """
    if not ORCHESTRATOR_PROMPT_PATH.exists():
        return ""
    return ORCHESTRATOR_PROMPT_PATH.read_text(encoding="utf-8").strip()


def write_orchestrator_prompt(text: str) -> None:
    """Orkestrator prompt override'ini yazar; bos metin verilirse varsayilana doner (dosyayi siler)."""
    cleaned = (text or "").strip()
    if not cleaned:
        ORCHESTRATOR_PROMPT_PATH.unlink(missing_ok=True)
        return
    ORCHESTRATOR_PROMPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ORCHESTRATOR_PROMPT_PATH.write_text(cleaned, encoding="utf-8")


def validate_agent_name(name: str) -> str:
    normalized = (name or "").strip()
    if not AGENT_NAME_RE.fullmatch(normalized):
        raise AgentStudioError("Agent adi sadece kucuk harf, rakam ve '_' icerebilir; harfle baslamali.")
    return normalized


def validate_tool_name(name: str) -> str:
    normalized = (name or "").strip()
    if not TOOL_NAME_RE.fullmatch(normalized):
        raise AgentStudioError("Tool adi sadece kucuk harf, rakam ve '_' icerebilir; harfle baslamali.")
    return normalized


def validate_env_name(name: str) -> str:
    normalized = (name or "").strip().upper()
    if not ENV_NAME_RE.fullmatch(normalized):
        raise AgentStudioError("Env adi buyuk harf, rakam ve '_' icermeli; harfle baslamali.")
    return normalized


def validate_pack_name(name: str) -> str:
    normalized = (name or "").strip()
    if not PACK_NAME_RE.fullmatch(normalized):
        raise AgentStudioError("Pack adi sadece kucuk harf, rakam ve '_' icerebilir; harfle baslamali.")
    return normalized


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized = []
    seen = set()
    for item in value:
        text = str(item or "").strip()
        if text and text not in seen:
            normalized.append(text)
            seen.add(text)
    return normalized


def _normalize_env_var_names(value: Any) -> list[str]:
    names = []
    raw_items = value.keys() if isinstance(value, dict) else value
    if isinstance(raw_items, str):
        raw_items = [raw_items]
    if not isinstance(raw_items, list) and not hasattr(raw_items, "__iter__"):
        return []
    seen = set()
    for item in raw_items:
        try:
            name = validate_env_name(str(item))
        except AgentStudioError:
            continue
        if name not in seen:
            names.append(name)
            seen.add(name)
    return names


def normalize_agent_entry(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AgentStudioError("Agent entry dict olmali.")
    agent_type = str(raw.get("type") or "config").strip().lower()
    if agent_type not in VALID_AGENT_TYPES:
        raise AgentStudioError("Agent type 'builtin' veya 'config' olmali.")
    name = validate_agent_name(str(raw.get("name") or ""))
    if agent_type == "builtin" and not is_known_builtin_agent_name(name):
        raise AgentStudioError("Builtin agent sadece kodda kayitli hazir submodel icin kullanilabilir.")
    tool_mode = str(raw.get("tool_mode") or ("custom" if raw.get("tools") and agent_type == "config" else "default")).strip().lower()
    if tool_mode not in VALID_TOOL_MODES:
        tool_mode = "default"
    return {
        "name": name,
        "type": agent_type,
        "enabled": bool(raw.get("enabled", True)),
        "description": str(raw.get("description") or "").strip(),
        "model": str(raw.get("model") or "default").strip() or "default",
        "tool_mode": tool_mode,
        "system_prompt": str(raw.get("system_prompt") or ""),
        "tools": _normalize_string_list(raw.get("tools")),
    }


def load_agents_config() -> dict[str, Any]:
    ensure_agent_studio_files()
    errors: list[dict[str, Any]] = []
    try:
        data = _read_yaml(AGENTS_CONFIG_PATH, _default_agents_config())
    except AgentStudioError as exc:
        data = _default_agents_config()
        errors.append({"scope": "agents", "message": str(exc)})

    raw_agents = data.get("agents")
    if not isinstance(raw_agents, list):
        raw_agents = []
        errors.append({"scope": "agents", "message": "agents listesi bulunamadi; varsayilanlar kullaniliyor."})

    normalized: list[dict[str, Any]] = []
    seen = set()
    for raw in raw_agents:
        try:
            item = normalize_agent_entry(raw)
        except AgentStudioError as exc:
            errors.append({"scope": "agents", "message": str(exc), "entry": raw})
            continue
        if item["name"] in seen:
            errors.append({"scope": "agents", "message": f"Tekrarlanan agent atlandi: {item['name']}"})
            continue
        normalized.append(item)
        seen.add(item["name"])

    for default_agent in DEFAULT_BUILTIN_AGENTS:
        if default_agent["name"] not in seen:
            normalized.append(dict(default_agent))
            seen.add(default_agent["name"])

    disabled_tools = _normalize_string_list(data.get("global_disabled_tools"))
    return {
        "version": int(data.get("version") or 1),
        "global_disabled_tools": disabled_tools,
        "agents": normalized,
        "errors": errors,
        "path": str(AGENTS_CONFIG_PATH),
    }


def save_agents_config(agents: list[dict[str, Any]], global_disabled_tools: list[str] | None = None) -> None:
    normalized = [normalize_agent_entry(item) for item in agents]
    data = {
        "version": 1,
        "global_disabled_tools": _normalize_string_list(global_disabled_tools or []),
        "agents": normalized,
    }
    _write_yaml(AGENTS_CONFIG_PATH, data)


def _agent_class_name(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_") if part) + "SubModel"


def _builtin_scaffold_source(agent: dict[str, Any]) -> str:
    name = agent["name"]
    class_name = _agent_class_name(name)
    description = agent.get("description") or "Agent Studio ile uretilmis builtin alt ajan."
    model = agent.get("model") or "default"
    prompt = (agent.get("system_prompt") or "").strip() or (
        "Sen Agent Studio ile uretilmis dosya tabanli bir builtin alt ajansin. "
        "Sana baglanan araclari kullanarak gorevi denetlenebilir sekilde tamamla."
    )
    return f'''"""Agent Studio generated builtin submodel: {name}."""

from __future__ import annotations

{AGENT_STUDIO_BUILTIN_MARKER}

from MarketingApp.llms.SubModels.base import register_submodel
from MarketingApp.llms.SubModels.generic_config_agent import (
    GenericConfigAgent,
    build_generic_config_system_prompt,
)


DEFAULT_SYSTEM_PROMPT = {prompt!r}


class {class_name}(GenericConfigAgent):
    """{description}"""

    def __init__(self):
        super().__init__(
            {{
                "name": {name!r},
                "type": "builtin",
                "enabled": True,
                "description": {description!r},
                "model": {model!r},
                "tool_mode": "custom",
                "system_prompt": DEFAULT_SYSTEM_PROMPT,
                "tools": [],
            }},
            tools=[],
        )

    def _build_system_prompt(self) -> str:
        prompt = DEFAULT_SYSTEM_PROMPT
        try:
            from MarketingApp.llms.agent_studio import load_agents_config

            for agent in load_agents_config().get("agents", []):
                if agent.get("name") == self.name:
                    prompt = (agent.get("system_prompt") or "").strip() or DEFAULT_SYSTEM_PROMPT
                    break
        except Exception:
            pass
        return build_generic_config_system_prompt(prompt)


register_submodel({class_name}())
'''


def _ensure_submodels_init_import(name: str) -> None:
    SUBMODELS_INIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    import_line = f"from . import {name}  # Agent Studio builtin\n"
    if SUBMODELS_INIT_PATH.exists():
        text = SUBMODELS_INIT_PATH.read_text(encoding="utf-8")
    else:
        text = ""
    if import_line.strip() in text:
        return
    if text and not text.endswith("\n"):
        text += "\n"
    SUBMODELS_INIT_PATH.write_text(text + import_line, encoding="utf-8")


def import_scaffolded_builtin_submodel(name: str) -> None:
    normalized_name = validate_agent_name(name)
    if not _is_scaffolded_builtin_agent(normalized_name):
        raise AgentStudioError(f"{normalized_name} icin Agent Studio builtin dosyasi bulunamadi.")
    module_name = f"MarketingApp.llms.SubModels.{normalized_name}"
    importlib.invalidate_caches()
    if module_name in sys.modules:
        importlib.reload(sys.modules[module_name])
    else:
        importlib.import_module(module_name)


def import_scaffolded_builtin_submodels() -> list[str]:
    loaded = []
    if not SUBMODELS_DIR.exists():
        return loaded
    for path in sorted(SUBMODELS_DIR.glob("*.py")):
        name = path.stem
        if name.startswith("_") or name in {"base", "generic_config_agent"}:
            continue
        if _is_scaffolded_builtin_agent(name):
            import_scaffolded_builtin_submodel(name)
            loaded.append(name)
    return loaded


def create_builtin_agent_scaffold(entry: dict[str, Any]) -> dict[str, Any]:
    raw = dict(entry or {})
    raw["type"] = "builtin"
    name = validate_agent_name(str(raw.get("name") or ""))
    if name in BUILTIN_AGENT_NAMES:
        raise AgentStudioError("Hazir builtin agent yeniden olusturulamaz.")
    if _is_scaffolded_builtin_agent(name):
        raise AgentStudioError(f"{name} builtin submodel dosyasi zaten var.")

    config = load_agents_config()
    if any(item["name"] == name for item in config["agents"]):
        raise AgentStudioError(f"{name} zaten agents.yaml icinde var.")

    agent = {
        "name": name,
        "type": "builtin",
        "enabled": bool(raw.get("enabled", True)),
        "description": str(raw.get("description") or "Agent Studio ile uretilmis builtin alt ajan.").strip(),
        "model": str(raw.get("model") or "default").strip() or "default",
        "tool_mode": "custom",
        "system_prompt": str(raw.get("system_prompt") or "").strip(),
        "tools": _normalize_string_list(raw.get("tools")),
    }

    path = _builtin_module_path(name)
    if path.exists():
        raise AgentStudioError(f"{path.name} zaten var; var olan dosyanin ustune yazilmadi.")
    path.write_text(_builtin_scaffold_source(agent), encoding="utf-8")
    try:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
        _ensure_submodels_init_import(name)
        normalized = normalize_agent_entry(agent)
        save_agents_config(config["agents"] + [normalized], config["global_disabled_tools"])
        import_scaffolded_builtin_submodel(name)
    except Exception as exc:
        raise AgentStudioError(f"Builtin submodel scaffold olusturulamadi: {exc}") from exc

    return {
        "agent": normalized,
        "path": str(path),
        "init_path": str(SUBMODELS_INIT_PATH),
    }


def upsert_agent_config(entry: dict[str, Any], *, create: bool = False) -> dict[str, Any]:
    incoming = normalize_agent_entry(entry)
    if create and incoming["type"] == "builtin":
        raise AgentStudioError("Yeni builtin agent runtime'dan olusturulamaz; yeni ajanlar config tipinde olmali.")
    config = load_agents_config()
    agents = list(config["agents"])
    index = next((i for i, item in enumerate(agents) if item["name"] == incoming["name"]), None)
    if create and index is not None:
        raise AgentStudioError(f"{incoming['name']} zaten var.")
    if index is None:
        agents.append(incoming)
    else:
        existing_type = agents[index].get("type") or "config"
        if existing_type == "builtin" and incoming["type"] != "builtin":
            raise AgentStudioError("Builtin agent type degistirilemez.")
        agents[index] = {**agents[index], **incoming, "type": existing_type if existing_type == "builtin" else incoming["type"]}
    save_agents_config(agents, config["global_disabled_tools"])
    return incoming


def delete_agent_config(name: str) -> dict[str, Any]:
    normalized_name = validate_agent_name(name)
    config = load_agents_config()
    agents = list(config["agents"])
    target = next((item for item in agents if item["name"] == normalized_name), None)
    if not target:
        raise AgentStudioError(f"{normalized_name} bulunamadi.")
    if target.get("type") == "builtin":
        raise AgentStudioError("Builtin agent silinemez; pasife alabilirsin.")
    agents = [item for item in agents if item["name"] != normalized_name]
    save_agents_config(agents, config["global_disabled_tools"])
    return target


def set_agent_enabled(name: str, enabled: bool) -> bool:
    normalized_name = validate_agent_name(name)
    config = load_agents_config()
    changed = False
    for item in config["agents"]:
        if item["name"] == normalized_name:
            item["enabled"] = bool(enabled)
            changed = True
            break
    if not changed:
        raise AgentStudioError(f"{normalized_name} bulunamadi.")
    save_agents_config(config["agents"], config["global_disabled_tools"])
    return bool(enabled)


def set_global_tool_active(name: str, active: bool) -> bool:
    normalized_name = validate_tool_name(name)
    config = load_agents_config()
    disabled = set(config["global_disabled_tools"])
    if active:
        disabled.discard(normalized_name)
    else:
        disabled.add(normalized_name)
    save_agents_config(config["agents"], sorted(disabled))
    return bool(active)


def load_custom_tools_config() -> dict[str, Any]:
    ensure_agent_studio_files()
    errors: list[dict[str, Any]] = []
    try:
        data = _read_yaml(CUSTOM_TOOLS_CONFIG_PATH, _default_custom_tools_config())
    except AgentStudioError as exc:
        data = _default_custom_tools_config()
        errors.append({"scope": "custom_tools", "message": str(exc)})

    raw_tools = data.get("custom_tools")
    if not isinstance(raw_tools, list):
        raw_tools = []
        errors.append({"scope": "custom_tools", "message": "custom_tools listesi bulunamadi."})

    normalized = []
    seen = set()
    for raw in raw_tools:
        if not isinstance(raw, dict):
            errors.append({"scope": "custom_tools", "message": "Custom tool entry dict olmali.", "entry": raw})
            continue
        try:
            name = validate_tool_name(str(raw.get("name") or ""))
        except AgentStudioError as exc:
            errors.append({"scope": "custom_tools", "message": str(exc), "entry": raw})
            continue
        if name in seen:
            errors.append({"scope": "custom_tools", "message": f"Tekrarlanan custom tool atlandi: {name}"})
            continue
        file_name = os.path.basename(str(raw.get("file") or f"{name}.py"))
        normalized.append(
            {
                "name": name,
                "enabled": bool(raw.get("enabled", False)),
                "description": str(raw.get("description") or "").strip(),
                "file": file_name,
                "params_note": str(raw.get("params_note") or "").strip(),
                "env_vars": _normalize_env_var_names(raw.get("env_vars")),
                "sandbox": bool(raw.get("sandbox", False)),
                "requires_approval": bool(raw.get("requires_approval", False)),
            }
        )
        seen.add(name)

    return {
        "version": int(data.get("version") or 1),
        "custom_tools": normalized,
        "errors": errors,
        "path": str(CUSTOM_TOOLS_CONFIG_PATH),
    }


def save_custom_tools_config(entries: list[dict[str, Any]]) -> None:
    normalized = []
    for entry in entries:
        name = validate_tool_name(str(entry.get("name") or ""))
        normalized.append(
            {
                "name": name,
                "enabled": bool(entry.get("enabled", False)),
                "description": str(entry.get("description") or "").strip(),
                "file": os.path.basename(str(entry.get("file") or f"{name}.py")),
                "params_note": str(entry.get("params_note") or "").strip(),
                "env_vars": _normalize_env_var_names(entry.get("env_vars")),
                "sandbox": bool(entry.get("sandbox", False)),
                "requires_approval": bool(entry.get("requires_approval", False)),
            }
        )
    _write_yaml(CUSTOM_TOOLS_CONFIG_PATH, {"version": 1, "custom_tools": normalized})


def _custom_tool_path(file_name: str) -> Path:
    CUSTOM_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    candidate = (CUSTOM_TOOLS_DIR / os.path.basename(file_name)).resolve()
    root = CUSTOM_TOOLS_DIR.resolve()
    if os.path.commonpath([str(root), str(candidate)]) != str(root):
        raise AgentStudioError("Custom tool dosya yolu izin verilen dizin disinda.")
    return candidate


def read_model_env_vars() -> dict[str, str]:
    values: dict[str, str] = {}
    if not MODEL_ENV_PATH.exists():
        return values
    for raw_line in MODEL_ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = value.strip().strip('"').strip("'")
    return values


def load_model_env_file() -> dict[str, str]:
    values = read_model_env_vars()
    for key, value in values.items():
        os.environ[key] = value
    return values


def update_model_env_vars(env_vars: dict[str, Any] | None) -> list[str]:
    if not env_vars:
        return []
    existing = read_model_env_vars()
    updated_names = []
    for raw_key, raw_value in env_vars.items():
        key = validate_env_name(str(raw_key))
        value = str(raw_value or "")
        if value == "":
            continue
        existing[key] = value
        os.environ[key] = value
        updated_names.append(key)
    if updated_names:
        MODEL_ENV_PATH.write_text(
            "\n".join(f"{key}={value}" for key, value in sorted(existing.items())) + "\n",
            encoding="utf-8",
        )
    return updated_names


def upsert_custom_tool(
    name: str,
    description: str,
    code: str,
    *,
    enabled: bool = True,
    params_note: str = "",
    env_vars: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_name = validate_tool_name(name)
    file_name = f"{normalized_name}.py"
    path = _custom_tool_path(file_name)
    path.write_text(code or "", encoding="utf-8")
    env_var_names = update_model_env_vars(env_vars)

    config = load_custom_tools_config()
    entries = list(config["custom_tools"])
    index = next((i for i, item in enumerate(entries) if item["name"] == normalized_name), None)
    entry = {
        "name": normalized_name,
        "enabled": bool(enabled),
        "description": description.strip(),
        "file": file_name,
        "params_note": params_note.strip(),
        "env_vars": _normalize_env_var_names(env_var_names or env_vars or []),
        # A tool that came from an installed pack stays sandboxed when it is edited here.
        "sandbox": bool(entries[index].get("sandbox")) if index is not None else False,
        "requires_approval": bool(entries[index].get("requires_approval")) if index is not None else False,
    }
    if index is None:
        entries.append(entry)
    else:
        entries[index] = entry
    save_custom_tools_config(entries)
    return {**entry, "path": str(path)}


def read_custom_tool_code(entry: dict[str, Any]) -> str:
    try:
        return _custom_tool_path(entry.get("file") or f"{entry.get('name')}.py").read_text(encoding="utf-8")
    except Exception:
        return ""


def load_custom_tool_callable(entry: dict[str, Any], *, include_disabled: bool = False) -> tuple[Callable | None, str | None]:
    if not include_disabled and not entry.get("enabled", False):
        return None, "Custom tool pasif."
    try:
        name = validate_tool_name(entry["name"])
        path = _custom_tool_path(entry.get("file") or f"{name}.py")
        if not path.exists():
            return None, f"{path.name} bulunamadi."
        if entry.get("sandbox"):
            # Pack tool: build a stand-in from the source text; the code runs only in the sandbox,
            # and only with the env names it declared (never the agent's own keys).
            from MarketingApp.sandbox import make_sandboxed_tool

            func = make_sandboxed_tool(
                name,
                path,
                env_names=list(entry.get("env_vars") or []),
                env_values=read_model_env_vars,
                description=str(entry.get("description") or ""),
                requires_approval=bool(entry.get("requires_approval")),
            )
            return func, None
        load_model_env_file()
        # Kaynagi dogrudan derle: spec_from_file_location + exec_module __pycache__ kullanir ve .pyc
        # dogrulamasi mtime'i SANIYE cozunurlugunde + dosya boyutuyla yapar. Ayni saniyede yapilan ayni
        # boyutlu bir duzenleme (ornegin `return 1` -> `return 2`) sessizce eski bytecode'u calistirirdi.
        module_name = f"marketingapp_custom_tool_{name}_{path.stat().st_mtime_ns}"
        module = types.ModuleType(module_name)
        module.__file__ = str(path)
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
        func = getattr(module, name, None)
        if not callable(func):
            return None, f"{name} fonksiyonu export edilmemis."
        if not (func.__doc__ or "").strip() and entry.get("description"):
            func.__doc__ = entry["description"]
        if entry.get("requires_approval"):
            # The pack declared that this tool changes something outside: the host asks before it runs,
            # whatever the tool's own code does.
            from MarketingApp.environments.approval_runtime import gate_tool

            func = gate_tool(func, name, str(entry.get("description") or ""))
        return func, None
    except Exception as exc:
        return None, str(exc)


def load_custom_tools(*, include_disabled: bool = False) -> dict[str, Any]:
    config = load_custom_tools_config()
    tools: dict[str, Callable] = {}
    entries = []
    errors = list(config["errors"])
    for entry in config["custom_tools"]:
        func, error = load_custom_tool_callable(entry, include_disabled=include_disabled)
        status = {**entry, "path": str(_custom_tool_path(entry.get("file") or f"{entry['name']}.py")), "error": error, "code": read_custom_tool_code(entry)}
        entries.append(status)
        if error:
            errors.append({"scope": "custom_tools", "name": entry["name"], "message": error})
            continue
        if func:
            tools[entry["name"]] = func
    return {"tools": tools, "entries": entries, "errors": errors}


def load_agent_packs_config() -> dict[str, Any]:
    ensure_agent_studio_files()
    errors: list[dict[str, Any]] = []
    try:
        data = _read_yaml(AGENT_PACKS_CONFIG_PATH, _default_agent_packs_config())
    except AgentStudioError as exc:
        data = _default_agent_packs_config()
        errors.append({"scope": "agent_packs", "message": str(exc)})

    raw_packs = data.get("installed_packs")
    if not isinstance(raw_packs, list):
        raw_packs = []
        errors.append({"scope": "agent_packs", "message": "installed_packs listesi bulunamadi."})

    normalized = []
    seen = set()
    for raw in raw_packs:
        if not isinstance(raw, dict):
            errors.append({"scope": "agent_packs", "message": "Pack entry dict olmali.", "entry": raw})
            continue
        try:
            name = validate_pack_name(str(raw.get("name") or ""))
        except AgentStudioError as exc:
            errors.append({"scope": "agent_packs", "message": str(exc), "entry": raw})
            continue
        if name in seen:
            errors.append({"scope": "agent_packs", "message": f"Tekrarlanan pack atlandi: {name}"})
            continue
        pack_type = str(raw.get("type") or "agent_bundle").strip().lower()
        if pack_type not in VALID_PACK_TYPES:
            pack_type = "agent_bundle"
        normalized.append(
            {
                "name": name,
                "version": str(raw.get("version") or "0.1.0").strip() or "0.1.0",
                "type": pack_type,
                "description": str(raw.get("description") or "").strip(),
                "source_path": str(raw.get("source_path") or "").strip(),
                "installed_path": str(raw.get("installed_path") or "").strip(),
                "installed_agents": _normalize_string_list(raw.get("installed_agents")),
                "installed_tools": _normalize_string_list(raw.get("installed_tools")),
                "installed_at": str(raw.get("installed_at") or "").strip(),
                "trusted": bool(raw.get("trusted", False)),
            }
        )
        seen.add(name)

    return {
        "version": int(data.get("version") or 1),
        "installed_packs": normalized,
        "errors": errors,
        "path": str(AGENT_PACKS_CONFIG_PATH),
    }


def save_agent_packs_config(entries: list[dict[str, Any]]) -> None:
    normalized = []
    for entry in entries:
        name = validate_pack_name(str(entry.get("name") or ""))
        pack_type = str(entry.get("type") or "agent_bundle").strip().lower()
        if pack_type not in VALID_PACK_TYPES:
            raise AgentStudioError("Pack type 'tool_pack', 'agent_bundle' veya 'runtime_pack' olmali.")
        normalized.append(
            {
                "name": name,
                "version": str(entry.get("version") or "0.1.0").strip() or "0.1.0",
                "type": pack_type,
                "description": str(entry.get("description") or "").strip(),
                "source_path": str(entry.get("source_path") or "").strip(),
                "installed_path": str(entry.get("installed_path") or "").strip(),
                "installed_agents": _normalize_string_list(entry.get("installed_agents")),
                "installed_tools": _normalize_string_list(entry.get("installed_tools")),
                "installed_at": str(entry.get("installed_at") or "").strip(),
                "trusted": bool(entry.get("trusted", False)),
            }
        )
    _write_yaml(AGENT_PACKS_CONFIG_PATH, {"version": 1, "installed_packs": normalized})


_GITHUB_PACK_RE = re.compile(
    r"^(?:github:|https://github\.com/)"
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100}?)(?:\.git)?"
    r"(?:@(?P<ref>[A-Za-z0-9][A-Za-z0-9._/-]{0,99}))?"
    r"(?:#(?P<subdir>[A-Za-z0-9._][A-Za-z0-9._/-]{0,199}))?/?$"
)
GITHUB_CLONE_TIMEOUT_SECONDS = 120


def is_github_pack_spec(value: str) -> bool:
    text = str(value or "").strip()
    return text.startswith("github:") or text.startswith("https://github.com/")


@contextmanager
def fetched_pack(path_value: str):
    """Yield a local pack path; a github spec is shallow-cloned to a temp dir first.

    Spec: github:owner/repo[@branch-or-tag][#subdir]  (or a plain https://github.com/owner/repo URL).
    """
    text = str(path_value or "").strip()
    if not is_github_pack_spec(text):
        yield text
        return
    match = _GITHUB_PACK_RE.match(text)
    if not match:
        raise AgentStudioError("GitHub kaynagi gecersiz. Format: github:kullanici/repo[@dal][#alt/klasor]")
    subdir = match.group("subdir") or ""
    if ".." in Path(subdir).parts:
        raise AgentStudioError("Alt klasor '..' iceremez.")
    if shutil.which("git") is None:
        raise AgentStudioError("git bulunamadi; GitHub'dan pack cekmek icin git gerekli.")
    url = f"https://github.com/{match.group('owner')}/{match.group('repo')}.git"
    command = ["git", "clone", "--depth", "1", "--quiet"]
    if match.group("ref"):
        command.append(f"--branch={match.group('ref')}")
    with tempfile.TemporaryDirectory(prefix="ethgent_pack_") as tmp:
        dest = Path(tmp) / "repo"
        try:
            result = subprocess.run(
                [*command, url, str(dest)],
                capture_output=True,
                text=True,
                timeout=GITHUB_CLONE_TIMEOUT_SECONDS,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except subprocess.TimeoutExpired:
            raise AgentStudioError("GitHub klonlama zaman asimina ugradi.") from None
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            raise AgentStudioError(f"GitHub'dan cekilemedi: {detail[-1] if detail else 'bilinmeyen hata'}")
        shutil.rmtree(dest / ".git", ignore_errors=True)
        root = (dest / subdir).resolve() if subdir else dest.resolve()
        if dest.resolve() not in (root, *root.parents):
            raise AgentStudioError("Alt klasor repo disina cikiyor.")
        yield str(root)


def _resolve_pack_root(path_value: str) -> Path:
    candidate = Path(str(path_value or "").strip()).expanduser()
    if not candidate.is_absolute():
        candidate = (APP_DIR.parent / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if not candidate.exists():
        raise AgentStudioError("Pack yolu bulunamadi.")
    if not candidate.is_dir():
        raise AgentStudioError("Pack yolu klasor olmali.")
    return candidate


def _pack_manifest_path(root: Path) -> Path:
    return root / "plugin.yaml"


def _read_pack_manifest(root: Path) -> dict[str, Any]:
    manifest_path = _pack_manifest_path(root)
    if not manifest_path.exists():
        raise AgentStudioError("plugin.yaml bulunamadi.")
    data = _read_yaml(manifest_path, {})
    if not isinstance(data, dict):
        raise AgentStudioError("plugin.yaml kok verisi dict olmali.")
    return data


def _normalize_pack_type(value: Any) -> str:
    pack_type = str(value or "agent_bundle").strip().lower()
    if pack_type not in VALID_PACK_TYPES:
        raise AgentStudioError("Pack type 'tool_pack', 'agent_bundle' veya 'runtime_pack' olmali.")
    return pack_type


def _resolve_relative_path(root: Path, relative_value: str, label: str) -> Path:
    relative_text = str(relative_value or "").strip()
    if not relative_text:
        raise AgentStudioError(f"{label} bos olamaz.")
    candidate = (root / relative_text).resolve()
    if os.path.commonpath([str(root.resolve()), str(candidate)]) != str(root.resolve()):
        raise AgentStudioError(f"{label} pack dizini disina cikamaz.")
    if not candidate.exists():
        raise AgentStudioError(f"{label} bulunamadi: {relative_text}")
    return candidate


def _inspect_tool_source(path: Path) -> dict[str, Any]:
    try:
        source = path.read_text(encoding="utf-8")
    except Exception as exc:
        return {"source": "", "functions": [], "module_doc": "", "error": str(exc)}
    try:
        tree = ast.parse(source, filename=str(path))
    except Exception as exc:
        return {"source": source, "functions": [], "module_doc": "", "error": f"Python parse hatasi: {exc}"}
    functions = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(
                {
                    "name": node.name,
                    "doc": ast.get_docstring(node) or "",
                    "lineno": getattr(node, "lineno", 0),
                }
            )
    return {
        "source": source,
        "functions": functions,
        "module_doc": ast.get_docstring(tree) or "",
        "error": "",
    }


def _iter_pack_tools(root: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    declared = manifest.get("tools")
    if isinstance(declared, list) and declared:
        items = []
        for raw in declared:
            if not isinstance(raw, dict):
                raise AgentStudioError("plugin.yaml tools listesi sadece dict entry icermeli.")
            file_value = str(raw.get("file") or "").strip()
            if not file_value:
                raise AgentStudioError("Tool entry 'file' alani icermeli.")
            inferred_name = Path(file_value).stem
            name = validate_tool_name(str(raw.get("name") or inferred_name))
            items.append(
                {
                    "name": name,
                    "file": file_value,
                    "description": str(raw.get("description") or "").strip(),
                    "params_note": str(raw.get("params_note") or "").strip(),
                    "env_vars": _normalize_env_var_names(raw.get("env_vars")),
                    "requires_approval": bool(raw.get("requires_approval", False)),
                }
            )
        return items

    tools_dir = root / "tools"
    if not tools_dir.exists():
        return []
    items = []
    for path in sorted(tools_dir.glob("*.py")):
        name = validate_tool_name(path.stem)
        items.append(
            {
                "name": name,
                "file": str(path.relative_to(root)),
                "description": "",
                "params_note": "",
                "env_vars": [],
                "requires_approval": False,
            }
        )
    return items


def _load_agent_entries_from_file(path: Path) -> list[dict[str, Any]]:
    data = _read_yaml(path, {})
    if isinstance(data, dict) and isinstance(data.get("agents"), list):
        entries = data.get("agents") or []
    elif isinstance(data, dict):
        entries = [data]
    else:
        raise AgentStudioError(f"{path.name} agent manifesti dict veya agents listesi olmali.")
    if not all(isinstance(item, dict) for item in entries):
        raise AgentStudioError(f"{path.name} agent manifesti sadece dict entry icermeli.")
    return [dict(item) for item in entries]


def _iter_pack_agent_sources(root: Path, manifest: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    declared = manifest.get("agents")
    files: list[Path] = []
    if isinstance(declared, list) and declared:
        for raw in declared:
            if isinstance(raw, str):
                files.append(_resolve_relative_path(root, raw, "Agent manifesti"))
            elif isinstance(raw, dict):
                file_value = str(raw.get("file") or "").strip()
                if not file_value:
                    raise AgentStudioError("plugin.yaml agent entry 'file' alani icermeli.")
                files.append(_resolve_relative_path(root, file_value, "Agent manifesti"))
            else:
                raise AgentStudioError("plugin.yaml agents listesi string veya dict entry icermeli.")
    else:
        agents_dir = root / "agents"
        if agents_dir.exists():
            files = sorted(agents_dir.glob("*.yml")) + sorted(agents_dir.glob("*.yaml"))

    result: list[tuple[dict[str, Any], str]] = []
    for file_path in files:
        for entry in _load_agent_entries_from_file(file_path):
            result.append((entry, str(file_path.relative_to(root))))
    return result


def _materialize_pack_agent(root: Path, entry: dict[str, Any], source_file: str) -> dict[str, Any]:
    raw = dict(entry)
    prompt_file = str(raw.pop("system_prompt_file", "") or "").strip()
    if prompt_file:
        prompt_path = _resolve_relative_path(root, prompt_file, "System prompt dosyasi")
        raw["system_prompt"] = prompt_path.read_text(encoding="utf-8")
    elif not raw.get("system_prompt"):
        default_prompt = root / "prompts" / f"{Path(source_file).stem}.md"
        if default_prompt.exists():
            raw["system_prompt"] = default_prompt.read_text(encoding="utf-8")

    submodel_file = str(raw.pop("submodel_file", "") or "").strip()
    if submodel_file and str(raw.get("type") or "").strip().lower() == "builtin":
        # Pack kendi .py kaynagini tasiyor; hedefte henuz kayitli olmasa bile
        # gecerlidir -- normalize_agent_entry'nin "zaten kodda kayitli olmali"
        # kontrolunu (is_known_builtin_agent_name) burada BILEREK atlariz, cunku
        # install_agent_pack kurulum sirasinda dosyayi tam da bunun icin kopyalar.
        submodel_path = _resolve_relative_path(root, submodel_file, "Submodel dosyasi")
        return {
            "name": validate_agent_name(str(raw.get("name") or "")),
            "type": "builtin",
            "enabled": bool(raw.get("enabled", True)),
            "description": str(raw.get("description") or "").strip(),
            "model": str(raw.get("model") or "default").strip() or "default",
            "tool_mode": "custom",
            "system_prompt": str(raw.get("system_prompt") or ""),
            "tools": _normalize_string_list(raw.get("tools")),
            "source_file": source_file,
            "prompt_source": prompt_file or "",
            "submodel_file": submodel_file,
            "submodel_path": str(submodel_path),
        }

    normalized = normalize_agent_entry(raw)
    return {
        **normalized,
        "source_file": source_file,
        "prompt_source": prompt_file or "",
    }


def preview_agent_pack(path_value: str) -> dict[str, Any]:
    ensure_agent_studio_files()
    root = _resolve_pack_root(path_value)
    manifest = _read_pack_manifest(root)
    pack_name = validate_pack_name(str(manifest.get("name") or root.name))
    pack_type = _normalize_pack_type(manifest.get("type"))
    warnings: list[str] = []
    errors: list[str] = []

    tools_preview = []
    for item in _iter_pack_tools(root, manifest):
        try:
            tool_path = _resolve_relative_path(root, item["file"], "Tool dosyasi")
            if tool_path.suffix != ".py":
                raise AgentStudioError("Tool dosyasi .py olmali.")
            inspection = _inspect_tool_source(tool_path)
            function_names = [func["name"] for func in inspection["functions"]]
            export_ok = item["name"] in function_names
            error = inspection["error"]
            if not export_ok and not error:
                error = f"{item['name']} fonksiyonu export edilmemis."
            description = item["description"] or next(
                (func["doc"].strip().splitlines()[0] for func in inspection["functions"] if func["name"] == item["name"] and func["doc"].strip()),
                inspection["module_doc"].strip().splitlines()[0] if inspection["module_doc"].strip() else "",
            )
            tools_preview.append(
                {
                    **item,
                    "path": str(tool_path),
                    "export_ok": export_ok,
                    "function_names": function_names,
                    "description": description,
                    "error": error,
                }
            )
            if error:
                errors.append(f"Tool {item['name']}: {error}")
        except Exception as exc:
            tools_preview.append({**item, "path": "", "export_ok": False, "function_names": [], "error": str(exc)})
            errors.append(f"Tool {item['name']}: {exc}")

    agents_preview = []
    try:
        for raw_entry, source_file in _iter_pack_agent_sources(root, manifest):
            normalized = _materialize_pack_agent(root, raw_entry, source_file)
            agents_preview.append(normalized)
            if normalized["type"] == "builtin":
                if normalized.get("submodel_file"):
                    warnings.append(
                        f"{normalized['name']} builtin tipinde; pack kendi Python kaynagini tasiyor. Bu kod ana "
                        "process'te calisir ve sandbox'a alinamaz; yalnizca --trust ile kurulur."
                    )
                elif not is_known_builtin_agent_name(normalized["name"]):
                    warnings.append(
                        f"{normalized['name']} builtin tipinde ama bu kurulumda bilinmiyor ve pack Python "
                        "kaynagi tasimiyor; kurulum bu ajan icin basarisiz olacak."
                    )
    except Exception as exc:
        errors.append(str(exc))

    if pack_type == "runtime_pack":
        warnings.append("runtime_pack preview desteklenir; MVP kurulum akisi su an sadece tool_pack ve agent_bundle icin aktiftir.")
    if not tools_preview and pack_type in {"tool_pack", "agent_bundle"}:
        warnings.append("Pack icinde kurulum icin tool bulunamadi.")
    if pack_type == "agent_bundle" and not agents_preview:
        warnings.append("agent_bundle tipinde ama agents/ manifesti bulunamadi.")

    readme_path = root / "README.md"
    env_example_path = root / "env.example"
    installable = not errors and pack_type in {"tool_pack", "agent_bundle"}

    return {
        "name": pack_name,
        "version": str(manifest.get("version") or "0.1.0").strip() or "0.1.0",
        "type": pack_type,
        "description": str(manifest.get("description") or "").strip(),
        "path": str(root),
        "manifest_path": str(_pack_manifest_path(root)),
        "readme_path": str(readme_path) if readme_path.exists() else "",
        "env_example_path": str(env_example_path) if env_example_path.exists() else "",
        "agents": agents_preview,
        "tools": tools_preview,
        "warnings": warnings,
        "errors": errors,
        "installable": installable,
    }


def install_agent_pack(
    path_value: str,
    *,
    overwrite: bool = False,
    source_label: str | None = None,
    trusted: bool = False,
) -> dict[str, Any]:
    """Install a pack. Its tools run in the sandbox unless ``trusted`` is set.

    A pack can also carry Python source for a builtin sub-agent. That code would run in the
    agent's own process, so it is only installed when the pack is explicitly trusted.
    """
    preview = preview_agent_pack(path_value)
    if preview["errors"]:
        raise AgentStudioError("Pack preview hata verdi; kurulum yapilmadi.")
    python_agents = [agent["name"] for agent in preview["agents"] if agent["type"] == "builtin" and agent.get("submodel_path")]
    if python_agents and not trusted:
        raise AgentStudioError(
            f"Pack Python kodlu ajan tasiyor ({', '.join(python_agents)}); bu kod ana process'te calisir, "
            "sandbox'a alinamaz. Pack'e guveniyorsan --trust ile kur."
        )
    if preview["type"] not in {"tool_pack", "agent_bundle"}:
        raise AgentStudioError("MVP kurulum akisi su an sadece tool_pack ve agent_bundle destekliyor.")

    tool_registry = build_tool_registry()
    builtin_tool_names = {
        item["name"]
        for item in tool_registry["tools"]
        if item.get("source") != "custom"
    }
    custom_config = load_custom_tools_config()
    existing_custom_entries = {entry["name"]: dict(entry) for entry in custom_config["custom_tools"]}
    updated_custom_entries = list(custom_config["custom_tools"])
    tool_write_plan: list[tuple[dict[str, Any], Path]] = []

    for tool in preview["tools"]:
        name = tool["name"]
        if tool.get("error"):
            raise AgentStudioError(f"{name} tool hatali; once duzelt.")
        if name in builtin_tool_names:
            raise AgentStudioError(f"{name} hazir builtin tool ile cakisiyor.")
        source_path = Path(tool["path"])
        target_path = _custom_tool_path(source_path.name)
        existing_entry = existing_custom_entries.get(name)
        if target_path.exists() and not overwrite and not existing_entry:
            raise AgentStudioError(f"{target_path.name} zaten var; overwrite acilmadan kurulamaz.")
        if existing_entry and not overwrite:
            raise AgentStudioError(f"{name} isimli custom tool zaten kayitli; overwrite ile tekrar kur.")
        tool_write_plan.append((tool, target_path))

    agent_config = load_agents_config()
    existing_agents = {item["name"]: dict(item) for item in agent_config["agents"]}
    updated_agents = list(agent_config["agents"])
    installed_agent_names = []
    builtin_submodel_write_plan: list[tuple[str, Path, Path]] = []
    for agent in preview["agents"]:
        submodel_path = agent.get("submodel_path")
        if agent["type"] == "builtin" and submodel_path:
            target_path = _builtin_module_path(agent["name"])
            if target_path.exists() and not overwrite:
                raise AgentStudioError(f"{target_path.name} zaten var; overwrite acilmadan kurulamaz.")
            builtin_submodel_write_plan.append((agent["name"], Path(submodel_path), target_path))
        elif agent["type"] == "builtin" and not is_known_builtin_agent_name(agent["name"]):
            raise AgentStudioError(
                f"{agent['name']} bu kurulumda bilinen bir builtin degil ve pack Python kaynagi tasimiyor; "
                "kurulamaz."
            )
        existing = existing_agents.get(agent["name"])
        if existing and not overwrite:
            raise AgentStudioError(f"{agent['name']} zaten agents.yaml icinde var; overwrite ile tekrar kur.")

    installed_root = AGENT_PACKS_DIR / preview["name"]
    if installed_root.exists() and not overwrite:
        raise AgentStudioError(f"{installed_root.name} pack klasoru zaten var; overwrite ile guncelle.")

    for tool, target_path in tool_write_plan:
        name = tool["name"]
        source_path = Path(tool["path"])
        existing_entry = existing_custom_entries.get(name)
        shutil.copy2(source_path, target_path)
        entry = {
            "name": name,
            "enabled": True,
            "description": str(tool.get("description") or "").strip(),
            "file": target_path.name,
            "params_note": str(tool.get("params_note") or "").strip(),
            "env_vars": _normalize_env_var_names(tool.get("env_vars")),
            "sandbox": not trusted,
            "requires_approval": bool(tool.get("requires_approval")),
        }
        if existing_entry:
            updated_custom_entries = [item for item in updated_custom_entries if item["name"] != name]
        updated_custom_entries.append(entry)
        existing_custom_entries[name] = entry

    save_custom_tools_config(updated_custom_entries)

    for name, source_path, target_path in builtin_submodel_write_plan:
        source_text = source_path.read_text(encoding="utf-8")
        try:
            compile(source_text, str(target_path), "exec")
        except SyntaxError as exc:
            raise AgentStudioError(f"{name} icin submodel kaynagi hatali: {exc}") from exc
        target_path.write_text(source_text, encoding="utf-8")
        _ensure_submodels_init_import(name)

    for agent in preview["agents"]:
        existing = existing_agents.get(agent["name"])
        if existing:
            updated_agents = [item for item in updated_agents if item["name"] != agent["name"]]
        updated_agents.append(
            {
                "name": agent["name"],
                "type": agent["type"],
                "enabled": bool(agent.get("enabled", True)),
                "description": agent.get("description") or "",
                "model": agent.get("model") or "default",
                "tool_mode": agent.get("tool_mode") or "custom",
                "system_prompt": agent.get("system_prompt") or "",
                "tools": _normalize_string_list(agent.get("tools")),
            }
        )
        existing_agents[agent["name"]] = agent
        installed_agent_names.append(agent["name"])

    save_agents_config(updated_agents, agent_config["global_disabled_tools"])
    for name, _source_path, _target_path in builtin_submodel_write_plan:
        import_scaffolded_builtin_submodel(name)
    if installed_root.exists():
        shutil.rmtree(installed_root)
    shutil.copytree(preview["path"], installed_root)

    packs_config = load_agent_packs_config()
    entries = [item for item in packs_config["installed_packs"] if item["name"] != preview["name"]]
    entries.append(
        {
            "name": preview["name"],
            "version": preview["version"],
            "type": preview["type"],
            "description": preview["description"],
            "source_path": source_label or preview["path"],
            "installed_path": str(installed_root),
            "installed_agents": installed_agent_names,
            "installed_tools": [item["name"] for item in preview["tools"]],
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "trusted": bool(trusted),
        }
    )
    save_agent_packs_config(entries)

    return {
        "pack": {
            "name": preview["name"],
            "version": preview["version"],
            "type": preview["type"],
            "installed_path": str(installed_root),
            "installed_agents": installed_agent_names,
            "installed_tools": [item["name"] for item in preview["tools"]],
            "trusted": bool(trusted),
        },
        "catalog": load_agent_studio_catalog(),
    }


def _pack_readme_template(pack_name: str, description: str, agents: list[str], tools: list[str], env_names: list[str]) -> str:
    lines = [
        f"# {pack_name}",
        "",
        description or "Agent Studio ile disari aktarilmis paket.",
        "",
        "## Kurulum",
        "",
        "```",
        f"/agent pack preview <bu-klasorun-yolu>",
        f"/agent pack install <bu-klasorun-yolu>",
        "```",
        "",
    ]
    if agents:
        lines += ["## Ajanlar", ""] + [f"- `{item}`" for item in agents] + [""]
    if tools:
        lines += ["## Tool'lar", ""] + [f"- `{item}`" for item in tools] + [""]
    if env_names:
        lines += [
            "## Gerekli env degiskenleri",
            "",
            "`env.example` dosyasindaki degiskenleri kendi `.env` dosyana kopyalayip doldur.",
            "",
        ] + [f"- `{item}`" for item in env_names] + [""]
    return "\n".join(lines)


def export_agent_pack(
    name: str,
    *,
    out_dir: str = "",
    agents: list[str] | None = None,
    tools: list[str] | None = None,
    include_all: bool = False,
    version: str = "0.1.0",
    description: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    """Mevcut kurulumdan kurulabilir bir agent pack uretir (install_agent_pack'in tersi).

    Config tipi ajanlar, custom tool'lar VE builtin ajanlar paketlenebilir.
    Scaffold edilmis builtin'lerin (Agent Studio ile /agent create --builtin
    uretilmis) .py kaynagi pakete gomulur ve hedefte henuz yoksa kurulur.
    Repoyla gelen orijinal builtin'ler (sosyal_medya_agent, content_creator_agent,
    ...) sadece config olarak tasinir -- kaynagi gomulmez, hedefte zaten var
    olmasi beklenir. Builtin tool'lar zaten her kurulumda var, pakete girmez.
    Env degerleri ASLA pakete yazilmaz; sadece degisken adlari env.example'a girer.
    """
    ensure_agent_studio_files()
    pack_name = validate_pack_name(name)
    warnings: list[str] = []

    agent_entries = {item["name"]: item for item in load_agents_config()["agents"]}
    custom_entries = {item["name"]: item for item in load_custom_tools_config()["custom_tools"]}

    # --- ajan secimi ---
    # Not: builtin ajanlar da paketlenebilir. Scaffold edilmis (Agent Studio ile
    # /agent create --builtin ile uretilmis) builtin'lerin .py kaynagi pakete
    # gömülür, boylece hedef kurulumda henuz yoksa bile kurulabilir. Repoyla
    # birlikte gelen orijinal 6 builtin (sosyal_medya_agent, content_creator_agent, ...)
    # icin kaynak gömülmez -- hedefin zaten AYNI Ethgent kod tabanina sahip olmasi
    # beklenir; sadece config (model/tool/prompt) tasinir.
    if include_all and not agents:
        selected_agents = list(agent_entries.values())
    else:
        selected_agents = []
        for agent_name in agents or []:
            entry = agent_entries.get(validate_agent_name(agent_name))
            if entry is None:
                raise AgentStudioError(f"Ajan bulunamadi: {agent_name}")
            selected_agents.append(entry)

    # --- tool secimi ---
    if tools:
        selected_tools = []
        for tool_name in tools:
            normalized = validate_tool_name(tool_name)
            entry = custom_entries.get(normalized)
            if entry is None:
                if normalized in load_available_tools(include_custom=False)["tools"]:
                    raise AgentStudioError(
                        f"'{normalized}' hazir bir builtin tool; pakete girmez, hedef kurulumda zaten var."
                    )
                raise AgentStudioError(f"Custom tool bulunamadi: {normalized}")
            selected_tools.append(entry)
    elif include_all:
        selected_tools = list(custom_entries.values())
    else:
        referenced = {tool for item in selected_agents for tool in item.get("tools") or []}
        selected_tools = [custom_entries[item] for item in sorted(referenced & set(custom_entries))]

    if not selected_agents and not selected_tools:
        raise AgentStudioError("Pakete girecek ajan veya custom tool secilmedi.")

    exported_tool_names = {item["name"] for item in selected_tools}
    for agent in selected_agents:
        missing = [
            tool
            for tool in agent.get("tools") or []
            if tool in custom_entries and tool not in exported_tool_names
        ]
        if missing:
            warnings.append(
                f"{agent['name']} su custom tool'lara bagli ama pakete alinmadi: {', '.join(missing)}"
            )

    # --- hedef klasor ---
    root = Path(out_dir).expanduser() if out_dir else (WORKSPACE_DIR / "exports" / pack_name)
    root = root.resolve()
    if root.exists():
        if not overwrite:
            raise AgentStudioError(f"{root} zaten var; uzerine yazmak icin overwrite kullan.")
        if not root.is_dir():
            raise AgentStudioError(f"{root} bir klasor degil.")
        shutil.rmtree(root)
    root.mkdir(parents=True)

    # --- tool dosyalari (birden fazla tool ayni dosyayi paylasabilir) ---
    manifest_tools = []
    copied_files: set[str] = set()
    if selected_tools:
        (root / "tools").mkdir()
    for entry in selected_tools:
        file_name = entry.get("file") or f"{entry['name']}.py"
        source = _custom_tool_path(file_name)
        if not source.exists():
            raise AgentStudioError(f"{entry['name']} icin kaynak dosya bulunamadi: {source}")
        if file_name not in copied_files:
            shutil.copy2(source, root / "tools" / file_name)
            copied_files.add(file_name)
        manifest_tools.append(
            {
                "name": entry["name"],
                "file": f"tools/{file_name}",
                "description": entry.get("description") or "",
                "params_note": entry.get("params_note") or "",
                "env_vars": list(entry.get("env_vars") or []),
                **({"requires_approval": True} if entry.get("requires_approval") else {}),
            }
        )

    # --- ajan manifestleri, promptlar ve (varsa) builtin submodel kaynagi ---
    manifest_agents = []
    if selected_agents:
        (root / "agents").mkdir()
    for agent in selected_agents:
        is_builtin = agent.get("type") == "builtin"
        payload = {
            "name": agent["name"],
            "type": "builtin" if is_builtin else "config",
            "enabled": bool(agent.get("enabled", True)),
            "description": agent.get("description") or "",
            "model": agent.get("model") or "default",
            "tool_mode": agent.get("tool_mode") or "custom",
            "tools": list(agent.get("tools") or []),
        }
        if is_builtin and _is_scaffolded_builtin_agent(agent["name"]):
            source = _builtin_module_path(agent["name"])
            if not source.exists():
                raise AgentStudioError(f"{agent['name']} icin builtin submodel dosyasi bulunamadi: {source}")
            (root / "submodels").mkdir(exist_ok=True)
            shutil.copy2(source, root / "submodels" / f"{agent['name']}.py")
            payload["submodel_file"] = f"submodels/{agent['name']}.py"
        elif is_builtin:
            warnings.append(
                f"{agent['name']} bu Ethgent kod tabanina gomulu bir builtin; sadece ayni kod tabanina "
                "sahip baska bir kuruluma tasinabilir (Python kaynagi pakete girmedi)."
            )
        prompt = str(agent.get("system_prompt") or "").strip()
        if prompt:
            (root / "prompts").mkdir(exist_ok=True)
            (root / "prompts" / f"{agent['name']}.md").write_text(prompt, encoding="utf-8")
            payload["system_prompt_file"] = f"prompts/{agent['name']}.md"
        relative = f"agents/{agent['name']}.yaml"
        _write_yaml(root / relative, payload)
        manifest_agents.append(relative)

    # --- env.example: sadece adlar, deger yok ---
    env_names = sorted({item for entry in selected_tools for item in entry.get("env_vars") or []})
    if env_names:
        (root / "env.example").write_text(
            "\n".join(f"{item}=" for item in env_names) + "\n", encoding="utf-8"
        )

    manifest: dict[str, Any] = {
        "name": pack_name,
        "version": str(version or "0.1.0"),
        "type": "agent_bundle" if manifest_agents else "tool_pack",
        "description": description or f"{pack_name} paketi.",
    }
    if manifest_tools:
        manifest["tools"] = manifest_tools
    if manifest_agents:
        manifest["agents"] = manifest_agents
    _write_yaml(root / "plugin.yaml", manifest)

    (root / "README.md").write_text(
        _pack_readme_template(
            pack_name,
            manifest["description"],
            [item["name"] for item in selected_agents],
            [item["name"] for item in selected_tools],
            env_names,
        ),
        encoding="utf-8",
    )

    return {
        "name": pack_name,
        "version": manifest["version"],
        "type": manifest["type"],
        "path": str(root),
        "agents": [item["name"] for item in selected_agents],
        "tools": [item["name"] for item in selected_tools],
        "files": sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()),
        "env_vars": env_names,
        "warnings": warnings,
    }


def _tool_description(func: Callable) -> str:
    doc = (getattr(func, "__doc__", "") or "").strip().splitlines()
    return doc[0].strip() if doc else ""


def _category_for_tool(name: str, group: str, source: str = "builtin") -> str:
    if source == "custom":
        return "system"
    if name in RECOMMENDED_MEMORY_TOOLS or name.startswith("bellek_") or name.startswith("rol_"):
        return "memory"
    if name.startswith("workspace_"):
        return "workspace"
    if name.startswith("browser_") or name in {"click_mouse", "type_text", "press_key", "scroll_up", "scroll_down"}:
        return "browser"
    if name == "web_arama" or "website_" in name or name.startswith("pexels_"):
        return "web" if name == "web_arama" else "content"
    if "instagram" in name or "youtube" in name or "_x_" in name or name.startswith(("publish_x", "reply_to_x", "send_x", "scan_x", "get_x", "launch_x", "open_x", "close_x")):
        return "social"
    if group in {"content_creator_agent"}:
        return "content"
    if group in {"sosyal_medya_agent"}:
        return "social"
    if group in {"browser_agent", "vlm_agent"}:
        return "browser"
    if name.startswith("terminal_") or "system" in name or group in {"sistem_agent", "kod_agent"}:
        return "system"
    return "workspace"


def _risk_for_tool(name: str, category: str, source: str = "builtin") -> str:
    if source == "custom":
        return "high"
    high_markers = (
        "terminal",
        "sil",
        "publish_",
        "send_",
        "reply_to_",
        "follow_",
        "like_",
        "repost_",
        "quote_",
        "comment_",
        "subscribe_",
        "browser_click",
        "browser_type",
        "browser_deger",
        "browser_dosya",
        "browser_sekme_kapat",
        "browser_kapat",
    )
    if any(marker in name for marker in high_markers):
        return "high"
    medium_markers = ("workspace_yaz", "workspace_ekle", "rol_guncelle", "bellek_yaz", "html_css", "video_post", "website_iceriginden")
    if any(marker in name for marker in medium_markers):
        return "medium"
    if category in {"browser", "social", "web", "content"}:
        return "medium"
    return "low"


def _builtin_tool_groups() -> dict[str, list[Callable]]:
    from MarketingApp import araclar

    return {
        "base": list(araclar.BASE_ARACLAR),
        "sosyal_medya_agent": list(araclar.SOSYAL_MEDYA_ARACLARI),
        "content_creator_agent": list(araclar.CONTENT_CREATOR_ARACLARI),
        "sistem_agent": list(araclar.SISTEM_ARACLARI),
        "arastirma_agent": list(araclar.ARAMA_ARACLARI),
        "kod_agent": list(araclar.KOD_ARACLARI),
        "vlm_agent": list(araclar.VLM_ARACLARI),
        "browser_agent": list(araclar.BROWSER_ARACLARI),
    }


def load_available_tools(*, include_custom: bool = True) -> dict[str, Any]:
    from MarketingApp.environments.approval_runtime import gate_builtin_tool

    tools: dict[str, Callable] = {}
    groups_for_tool: dict[str, set[str]] = {}
    for group, funcs in _builtin_tool_groups().items():
        for func in funcs:
            name = getattr(func, "__name__", "")
            if not name:
                continue
            if name not in tools:
                # Dis dunyaya yazan yerlesik tool'lar calismadan once onay ister.
                tools[name] = gate_builtin_tool(func)
            groups_for_tool.setdefault(name, set()).add(group)

    errors: list[dict[str, Any]] = []
    custom_entries: list[dict[str, Any]] = []
    if include_custom:
        loaded_custom = load_custom_tools(include_disabled=False)
        errors.extend(loaded_custom["errors"])
        custom_entries = loaded_custom["entries"]
        for name, func in loaded_custom["tools"].items():
            # Yerlesik bir yazma tool'unun adini alan bir custom tool da kapidan gecer.
            tools[name] = gate_builtin_tool(func)
            groups_for_tool.setdefault(name, set()).add("custom")

    return {
        "tools": tools,
        "groups": {name: sorted(groups) for name, groups in groups_for_tool.items()},
        "errors": errors,
        "custom_entries": custom_entries,
    }


def build_tool_registry() -> dict[str, Any]:
    config = load_agents_config()
    loaded = load_available_tools(include_custom=True)
    disabled = set(config["global_disabled_tools"])
    registry = []
    for name, func in sorted(loaded["tools"].items()):
        groups = loaded["groups"].get(name, [])
        source = "custom" if "custom" in groups else "builtin"
        primary_group = next((group for group in groups if group != "custom"), groups[0] if groups else "base")
        category = _category_for_tool(name, primary_group, source)
        registry.append(
            {
                "name": name,
                "description": _tool_description(func),
                "category": category,
                "risk": _risk_for_tool(name, category, source),
                "source": source,
                "groups": groups,
                "active": name not in disabled,
                "recommended": name in RECOMMENDED_MEMORY_TOOLS,
                "signature": str(inspect.signature(func)),
            }
        )
    return {
        "tools": registry,
        "errors": loaded["errors"] + config["errors"],
        "recommended_memory_tools": list(RECOMMENDED_MEMORY_TOOLS),
    }


def load_agent_studio_catalog() -> dict[str, Any]:
    agents = load_agents_config()
    custom = load_custom_tools(include_disabled=True)
    packs = load_agent_packs_config()
    registry = build_tool_registry()
    agent_entries = [
        {**agent, "system_prompt_placeholder": default_system_prompt_placeholder(agent)}
        for agent in agents["agents"]
    ]
    return {
        "agents": agent_entries,
        "global_disabled_tools": agents["global_disabled_tools"],
        "tools": registry["tools"],
        "custom_tools": custom["entries"],
        "packs": packs["installed_packs"],
        "recommended_memory_tools": list(RECOMMENDED_MEMORY_TOOLS),
        "default_config_system_prompt_placeholder": default_system_prompt_placeholder({"type": "config"}),
        "paths": {
            "agents_config": str(AGENTS_CONFIG_PATH),
            "custom_tools_config": str(CUSTOM_TOOLS_CONFIG_PATH),
            "agent_packs_config": str(AGENT_PACKS_CONFIG_PATH),
            "custom_tools_dir": str(CUSTOM_TOOLS_DIR),
            "agent_packs_dir": str(AGENT_PACKS_DIR),
            "submodels_dir": str(SUBMODELS_DIR),
            "submodels_init": str(SUBMODELS_INIT_PATH),
            "model_env": str(MODEL_ENV_PATH),
        },
        "errors": agents["errors"] + registry["errors"] + custom["errors"] + packs["errors"],
    }
