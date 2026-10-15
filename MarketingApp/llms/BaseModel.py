"""
BaseModel — ana orkestrator + browser_agent komutani.

Bu surum OpenAI-compatible chat completion + tool calling akisiyla
Gemini veya Moonshot/Kimi gibi saglayicilari kullanir.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time

import speech_recognition as sr
from openai import AsyncOpenAI, RateLimitError

from .SubModels import SubModelRateLimitError, get_all_submodels
from MarketingApp.environments.approval_runtime import register_approval_handler
from .runtime_config import (
    get_base_model_name,
    get_base_reasoning_effort,
    get_model_api_key,
    get_openai_compat_base_url,
    get_provider_display_name,
)
from MarketingApp.araclar import BASE_ARACLAR
from MarketingApp import telemetry


INPUT_RATE = 16000
SAMPLE_WIDTH = 2
CHANNELS = 1
PROVIDER_FAILURE_COOLDOWN_SECONDS = 10
DEFAULT_TOOL_TIMEOUT_SECONDS = 150.0

SYSTEM_INSTRUCTION = """
Sen "Ethgent" projesinin merkezi orkestratorusun.

CALISMA KURALLARI:
1. Yalnizca bu istekte tool semasinda gorunen aktif tool ve alt ajanlari kullan.
2. Pasif alt ajanlari veya pasif tool'lari asla cagirma; gorev onlara bagimliysa kullaniciya hangi ajanin/tool'un kapali oldugunu soyle.
3. Basit dosya ve workspace islemlerinde (okuma, yazma, listeleme) base tool'lari dogrudan kullan.
4. Sosyal medya, icerik uretimi veya proje hafizasi gerektiren islerde once `context_paketi_oku` ile role, market_state, idea_pool ve recent_actions ozetini al.
5. Onemli karar, dosya uretimi, post/reply denemesi veya basarili/basarisiz platform aksiyonu sonrasinda `context_aksiyon_kaydet` ile hafizaya iz dus.
6. `role.md` persona cekirdegidir; kullanici istemedikce bastan yazma, gerekirse sadece bilerek guncelle.
7. Uzun icerikleri sesli yanit gibi dusunme; metni `metinle_cevapla` veya `ekrana_yazdir` ile ilet.
8. Karmasik gorevlerde adim adim ilerle, gereksiz ajan cagrisi yapma.
9. Yanitlarini Turkce ver.
"""

SUBMODEL_ROUTING_RULES = {
    "sosyal_medya_agent": (
        "X (Twitter), Instagram ve YouTube yayinlama, yorum, begeni, takip, "
        "bildirim tarama ve platform ici operasyon gorevlerini bu alt ajana devret."
    ),
    "content_creator_agent": (
        "Caption, carousel, thread, web sitesinden icerik cikarip post paketine "
        "donusturme, kreatif kampanya fikri, gorsel/video medya arama, PNG post "
        "ve MP4 video uretimi gorevlerini bu alt ajana devret."
    ),
    "browser_agent": (
        "Genel web gezinme, DOM okuma, form doldurma ve Selenium tabanli tarayici "
        "isleri icin bu alt ajani kullan."
    ),
    "asset_collector_agent": (
        "App'in onayli medya varliklarini listeleme/detaylandirma, bir varligi yayina "
        "hazirlama ve dogrulama (`media_ref` uretir), ya da App'ten yeni bir video "
        "uretimi isteme gorevlerini bu alt ajana devret. Yayin araclari imzali URL "
        "yerine bu ajanin dondurdugu `media_ref` ile calisir."
    ),
    "platform_data_agent": (
        "Bir platformun (YouTube, Instagram, TikTok, X, Reddit) metriklerini, yorumlarini "
        "veya hesap/icerik verisini SALT-OKUNUR olarak toplama ve yorumlama gorevlerini "
        "bu alt ajana devret; platform kimlik bilgilerine erisimi yoktur."
    ),
}


def _process_stt(audio_data_bytes: bytes, recognizer: sr.Recognizer, rate: int = INPUT_RATE) -> str | None:
    audio_data = sr.AudioData(audio_data_bytes, rate, SAMPLE_WIDTH)
    try:
        return recognizer.recognize_google(audio_data, language="tr-TR")
    except sr.UnknownValueError:
        return None
    except sr.RequestError as e:
        print(f"\n[STT API Hatasi]: {e}")
        return None


class BaseModel:
    """OpenAI-compatible bir saglayici uzerinde calisan ana orchestrator."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or get_model_api_key()
        self.model = model or get_base_model_name()
        self.reasoning_effort = get_base_reasoning_effort()
        self.provider_name = get_provider_display_name()
        self._client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=get_openai_compat_base_url(),
        )
        self._current_image = None

        self.logs = []
        self.metrics = []
        self.pending_actions = {}
        self.start_time = time.time()
        self._provider_blocked_until = 0.0
        self._provider_block_message = ""
        self.agent_configs = []
        self.agent_config_by_name = {}
        self.agent_studio_errors = []
        self._agent_original_tools = {}
        self._runtime_tool_map = {}

        self._configure_agent_runtime()

        # araclar/ icindeki duz fonksiyonlarin (worker_video_yayinla gibi) onay
        # isteyebilmesi icin: self.request_approval'i HER cagride yeniden okuyan
        # dinamik bir kapanis kaydet -- boylece TerminalManager'in
        # base_model.request_approval'i degistirmesi otomatik yansir (bkz.
        # environments/approval_runtime.py).
        async def _dynamic_approval_handler(action_id: str, description: str) -> bool:
            return await self.request_approval(action_id, description)

        register_approval_handler(_dynamic_approval_handler)

        print(f"🧠 BaseModel başlatıldı: {self.model}")
        print(f"   Sağlayıcı: {self.provider_name}")
        if self.reasoning_effort:
            print(f"   Reasoning effort: {self.reasoning_effort}")
        active_submodels = [
            name
            for name in self._submodel_func_map
            if self._is_runtime_callable_active(name)
        ]
        inactive_submodels = [
            name
            for name in self._submodel_func_map
            if name not in active_submodels
        ]
        print(f"   Aktif SubModel tool'ları: {active_submodels}")
        if inactive_submodels:
            print(f"   Pasif SubModel'lar: {inactive_submodels}")

    def _configure_agent_runtime(self):
        from .SubModels.generic_config_agent import GenericConfigAgent
        from .agent_studio import import_scaffolded_builtin_submodels, load_agents_config, load_available_tools

        import_scaffolded_builtin_submodels()
        agents_config = load_agents_config()
        available_tools = load_available_tools(include_custom=True)
        tool_map = dict(available_tools["tools"])
        disabled_tools = set(agents_config.get("global_disabled_tools") or [])
        registered_submodels = {sm.name: sm for sm in get_all_submodels()}

        submodels = []
        original_tools = {}
        errors = list(agents_config.get("errors") or []) + list(available_tools.get("errors") or [])

        def resolve_tools_for_names(agent_name: str, tool_names: list[str]) -> tuple[list, list[str]]:
            selected_tools = []
            missing_tools = []
            seen_tools = set()
            for tool_name in tool_names:
                if tool_name not in tool_map:
                    missing_tools.append(tool_name)
                    continue
                if tool_name in seen_tools:
                    continue
                selected_tools.append(tool_map[tool_name])
                seen_tools.add(tool_name)
            return selected_tools, missing_tools

        def builtin_tool_names(agent_name: str) -> list[str]:
            return [
                tool_name
                for tool_name, groups in available_tools.get("groups", {}).items()
                if agent_name in groups
            ]

        for config in agents_config["agents"]:
            name = config["name"]
            if config.get("type") == "builtin":
                submodel = registered_submodels.get(name)
                if not submodel:
                    errors.append({"scope": "agents", "name": name, "message": "Builtin submodel registry'de bulunamadi."})
                    continue
                if config.get("description"):
                    submodel.description = config["description"]
                configured_model = str(config.get("model") or "").strip()
                if configured_model and configured_model not in {"default", "base_default", "browser_default"}:
                    submodel.model_id = configured_model
                selected_names = config.get("tools") or []
                if config.get("tool_mode") != "custom":
                    selected_names = builtin_tool_names(name)
                selected_tools, missing_tools = resolve_tools_for_names(name, selected_names)
                if missing_tools:
                    errors.append(
                        {
                            "scope": "agents",
                            "name": name,
                            "message": f"Bilinmeyen tool atlandi: {', '.join(missing_tools)}",
                        }
                    )
                submodels.append(submodel)
                original_tools[name] = list(selected_tools)
                continue

            selected_tools, missing_tools = resolve_tools_for_names(name, config.get("tools") or [])

            if missing_tools:
                errors.append(
                    {
                        "scope": "agents",
                        "name": name,
                        "message": f"Bilinmeyen tool atlandi: {', '.join(missing_tools)}",
                    }
                )

            submodel = GenericConfigAgent(config, selected_tools)
            submodels.append(submodel)
            original_tools[name] = list(selected_tools)

        self.submodels = submodels
        self.agent_configs = agents_config["agents"]
        self.agent_config_by_name = {item["name"]: item for item in self.agent_configs}
        self.agent_studio_errors = errors
        self._agent_original_tools = original_tools
        self._runtime_tool_map = tool_map
        self._submodel_funcs, self._submodel_func_map = self._build_submodel_functions(submodels)
        self.active_agents = {
            sm.name: bool(self.agent_config_by_name.get(sm.name, {}).get("enabled", True))
            for sm in submodels
        }
        self.active_tools = {
            name: name not in disabled_tools
            for name in tool_map
        }
        for func in self._submodel_func_map.values():
            self.active_tools[func.__name__] = bool(self.active_agents.get(func.__name__, True))

        self._apply_tool_activation_to_submodels()

    def _apply_tool_activation_to_submodels(self):
        for submodel in getattr(self, "submodels", []):
            original_tools = self._agent_original_tools.get(submodel.name, list(submodel.tools))
            if not self.active_agents.get(submodel.name, True):
                submodel.tools = []
                submodel._tool_map = {}
                continue
            filtered_tools = [
                func
                for func in original_tools
                if self.active_tools.get(func.__name__, True)
            ]
            submodel.tools = filtered_tools
            submodel._tool_map = {func.__name__: func for func in filtered_tools}

    def _is_runtime_callable_active(self, name: str) -> bool:
        if name in {"ekrana_yazdir", "metinle_cevapla"}:
            return True
        if name in self._submodel_func_map:
            return bool(self.active_agents.get(name, False)) and bool(self.active_tools.get(name, False))
        return bool(self.active_tools.get(name, True))

    def default_system_instruction(self) -> str:
        """Config override'i olmadan kullanilan, kod icindeki varsayilan orkestrator promptu."""
        return SYSTEM_INSTRUCTION.strip()

    def _build_runtime_system_instruction(self) -> str:
        from .agent_studio import read_orchestrator_prompt

        base_instruction = SYSTEM_INSTRUCTION
        try:
            override = read_orchestrator_prompt()
            if override:
                base_instruction = override
        except Exception:
            pass

        active_rules = []
        inactive_names = []

        for submodel in getattr(self, "submodels", []):
            is_active = bool(self.active_agents.get(submodel.name, True))
            if is_active:
                rule = SUBMODEL_ROUTING_RULES.get(submodel.name)
                if rule:
                    active_rules.append(f"- `{submodel.name}` aktif: {rule}")
                else:
                    active_rules.append(f"- `{submodel.name}` aktif: {submodel.description}")
            else:
                inactive_names.append(submodel.name)

        if active_rules:
            routing_block = "\nAKTIF ALT AJAN ROTASI:\n" + "\n".join(active_rules)
        else:
            routing_block = (
                "\nAKTIF ALT AJAN ROTASI:\n"
                "- Su anda aktif alt ajan yok. Gorevi sadece aktif base tool'lar ile yap; "
                "kapali alt ajan gerektiren islerde basari iddia etme."
            )

        if inactive_names:
            routing_block += (
                "\n\nPASIF ALT AJANLAR:\n"
                + "\n".join(f"- `{name}` pasif; bu ismi tool/ajan olarak cagirma." for name in inactive_names)
            )

        return base_instruction.rstrip() + "\n" + routing_block + "\n"

    def reload_agent_studio(self) -> dict:
        self._configure_agent_runtime()
        self.log_message("sistem", "Agent Studio config ve custom tool katalogu yeniden yuklendi.")
        return self.get_hierarchy()

    def _strip_thought_blocks(self, text: str) -> str:
        cleaned = re.sub(r"<thought>.*?</thought>", "", text or "", flags=re.DOTALL | re.IGNORECASE)
        return cleaned.strip()

    def _is_provider_quota_error(self, exc: Exception) -> bool:
        if isinstance(exc, RateLimitError):
            return True

        err = str(exc).lower()
        quota_markers = (
            "insufficient balance",
            "exceeded_current_quota",
            "quota",
            "rate limit",
            "429",
        )
        return any(marker in err for marker in quota_markers)

    def _format_provider_quota_message(self) -> str:
        return (
            f"⚠️ {self.provider_name} API şu anda kullanılamıyor. Kota, plan veya faturalama "
            "sınırına takılmış olabilir. Sağlayıcı hesabını ve API anahtarını kontrol edin."
        )

    def _is_thought_signature_error(self, exc: Exception) -> bool:
        err = str(exc).lower()
        return "thought_signature" in err or "missing a thought signature" in err

    def _format_thought_signature_message(self) -> str:
        return (
            "⚠️ Gemini tool-calling oturum imzasi eslesemedi. Uygulamayi yeniden baslatip "
            "istegi tekrar deneyin. Sorun surerse ayni oturumdaki eski model yanitlarini temizlemek gerekebilir."
        )

    def _mark_provider_temporarily_unavailable(self, exc: Exception):
        self._provider_blocked_until = time.time() + PROVIDER_FAILURE_COOLDOWN_SECONDS
        self._provider_block_message = self._format_provider_quota_message()
        self.log_message("sistem", f"Model sağlayıcı kota hatası: {exc}")

    def _get_provider_unavailable_message(self) -> str | None:
        remaining = self._provider_blocked_until - time.time()
        if remaining <= 0:
            return None
        if remaining < 60:
            remaining_text = f"yaklaşık {max(1, int(remaining + 0.999))} saniye"
        else:
            remaining_minutes = max(1, int((remaining + 59) // 60))
            remaining_text = f"yaklaşık {remaining_minutes} dakika"

        return (
            f"{self._provider_block_message} Sistem gereksiz tekrar denemeleri azaltmak için "
            f"{remaining_text} boyunca hızlıca bu uyarıyı dönecek."
        )

    def _build_submodel_functions(self, submodels):
        submodel_funcs = []
        submodel_func_map = {}

        for sm in submodels:
            def make_runner(submodel):
                async def runner(gorev: str) -> str:
                    try:
                        return await submodel.run(gorev)
                    except SubModelRateLimitError as e:
                        return f"[SISTEM_MESAJI_GIZLI] {e.submodel_name} rate limit verdi."
                    except Exception as e:
                        return f"[SISTEM_MESAJI_GIZLI] {submodel.name} hatasi: {e}"

                runner.__name__ = submodel.name
                runner.__doc__ = submodel.description + "\n\nArgs:\n    gorev: Bu ajana verilecek gorev aciklamasi."
                return runner

            func = make_runner(sm)
            submodel_funcs.append(func)
            submodel_func_map[sm.name] = func

        return submodel_funcs, submodel_func_map

    def _build_all_tools(self) -> list:
        def ekrana_yazdir(metin: str) -> str:
            """Uzun icerigi kullanici ekranina dogrudan iletir."""
            return "Metin basariyla kullanicinin ekranina gonderildi."

        def metinle_cevapla(cevap: str) -> str:
            """Kullaniciya metin yaniti gonderir."""
            return "Cevap basariyla metin olarak gonderildi."

        tools = [ekrana_yazdir, metinle_cevapla]
        for func in BASE_ARACLAR:
            if self._is_runtime_callable_active(func.__name__):
                tools.append(func)
        for func in self._submodel_funcs:
            if self._is_runtime_callable_active(func.__name__):
                tools.append(func)
        return tools

    def _build_full_tool_map(self) -> dict:
        full_map = {func.__name__: func for func in BASE_ARACLAR}
        full_map.update(self._submodel_func_map)
        return full_map

    def _schema_for_callable(self, func) -> dict:
        sig = inspect.signature(func)
        properties = {}
        required = []

        for param_name, param in sig.parameters.items():
            annotation = param.annotation
            param_type = "string"
            if annotation == int:
                param_type = "integer"
            elif annotation == float:
                param_type = "number"
            elif annotation == bool:
                param_type = "boolean"

            properties[param_name] = {"type": param_type}
            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        return {
            "type": "function",
            "function": {
                "name": func.__name__,
                "description": ((func.__doc__ or "").strip() or f"{func.__name__} aracini cagir"),
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def _build_tool_schemas(self) -> list[dict]:
        return [self._schema_for_callable(func) for func in self._build_all_tools()]

    def _get_tool_timeout_seconds(self, name: str):
        # Browser agent uzun cok-adimli gorevlerde sure sinirina takilmasin.
        if name == "browser_agent":
            return None
        return DEFAULT_TOOL_TIMEOUT_SECONDS

    def set_agent_active(self, name: str, active: bool) -> bool:
        if name not in self.active_agents:
            raise KeyError(name)
        self.active_agents[name] = bool(active)
        if name in self._submodel_func_map:
            self.active_tools[name] = bool(active)
        try:
            from .agent_studio import set_agent_enabled

            set_agent_enabled(name, bool(active))
        except Exception as exc:
            self.agent_studio_errors.append({"scope": "agents", "name": name, "message": str(exc)})
        state_label = "aktif" if self.active_agents[name] else "pasif"
        self.log_message("sistem", f"Alt ajan durumu guncellendi: {name} -> {state_label}")
        return self.active_agents[name]

    def toggle_agent(self, name: str) -> bool:
        if name not in self.active_agents:
            raise KeyError(name)
        return self.set_agent_active(name, not self.active_agents[name])

    def set_tool_active(self, name: str, active: bool) -> bool:
        if name not in self.active_tools:
            raise KeyError(name)
        self.active_tools[name] = bool(active)
        if name in self.active_agents:
            self.active_agents[name] = bool(active)
        try:
            from .agent_studio import set_global_tool_active

            set_global_tool_active(name, bool(active))
        except Exception as exc:
            self.agent_studio_errors.append({"scope": "tools", "name": name, "message": str(exc)})
        self._apply_tool_activation_to_submodels()
        state_label = "aktif" if self.active_tools[name] else "pasif"
        self.log_message("sistem", f"Tool durumu guncellendi: {name} -> {state_label}")
        return self.active_tools[name]

    def toggle_tool(self, name: str) -> bool:
        if name not in self.active_tools:
            raise KeyError(name)
        return self.set_tool_active(name, not self.active_tools[name])

    def get_hierarchy(self) -> dict:
        from MarketingApp.araclar import BASE_ARACLAR

        def tool_info(func_list):
            return [{
                "name": func.__name__,
                "desc": func.__doc__.split("\n")[0] if func.__doc__ else "",
                "active": self.active_tools.get(func.__name__, True),
            } for func in func_list]

        hierarchy = {
            "name": "BaseModel",
            "active": True,
            "tools": tool_info(BASE_ARACLAR),
            "submodels": [],
        }

        for sm in self.submodels:
            original_tools = self._agent_original_tools.get(sm.name, list(sm.tools))
            config = self.agent_config_by_name.get(sm.name, {})
            hierarchy["submodels"].append({
                "name": sm.name,
                "desc": sm.description,
                "model": sm.model_id,
                "type": config.get("type") or "builtin",
                "tool_mode": config.get("tool_mode") or "default",
                "active": self.active_agents.get(sm.name, True),
                "tool_count": len(original_tools),
                "tools": tool_info(original_tools),
            })

        return hierarchy

    def log_message(self, type: str, message: str):
        t = time.strftime("%H:%M:%S")
        log_entry = {"time": t, "type": type, "message": message}
        self.logs.append(log_entry)
        if len(self.logs) > 100:
            self.logs.pop(0)
        print(f"[{t}] [{type.upper()}] {message}")
        telemetry.record_event(type, message)

    async def request_approval(self, action_id: str, description: str):
        ev = asyncio.Event()
        self.pending_actions[action_id] = {
            "description": description,
            "event": ev,
            "status": "pending",
        }
        self.log_message("sistem", f"ONAY GEREKLI: {description}")

        try:
            await asyncio.wait_for(ev.wait(), timeout=300.0)
            status = self.pending_actions[action_id]["status"]
            del self.pending_actions[action_id]
            return status == "approved"
        except asyncio.TimeoutError:
            self.log_message("sistem", f"Zaman Asimi: {action_id} onay alinamadigi icin reddedildi.")
            if action_id in self.pending_actions:
                del self.pending_actions[action_id]
            return False

    def _parse_tool_args(self, arguments) -> dict:
        if isinstance(arguments, dict):
            return arguments
        if not arguments:
            return {}
        try:
            return json.loads(arguments)
        except Exception:
            return {}

    def _tool_repeat_key(self, name: str, args: dict) -> str:
        try:
            normalized_args = json.dumps(args or {}, sort_keys=True, ensure_ascii=False)
        except Exception:
            normalized_args = str(args or {})
        return f"{name}:{normalized_args}"

    def _extract_message_text(self, message) -> str:
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return self._strip_thought_blocks(content)
        if isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(item.get("text", ""))
                else:
                    maybe_text = getattr(item, "text", None)
                    if maybe_text:
                        texts.append(maybe_text)
            combined = "\n".join(part.strip() for part in texts if part and part.strip()).strip()
            return self._strip_thought_blocks(combined)
        return ""

    def _assistant_message_payload(self, message) -> dict:
        payload = {
            "role": "assistant",
            "content": self._extract_message_text(message),
        }
        tool_calls = []
        for call in getattr(message, "tool_calls", []) or []:
            if isinstance(call, dict):
                call_payload = dict(call)
            elif hasattr(call, "to_dict"):
                call_payload = call.to_dict()
            else:
                call_payload = {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments or "{}",
                    },
                }

            # Gemini OpenAI compatibility katmaninda function calling icin
            # tool_call.extra_content.google.thought_signature alanini oldugu
            # gibi geri dondurmek zorunludur.
            function_payload = call_payload.get("function") or {}
            function_payload["name"] = function_payload.get("name") or call.function.name
            function_payload["arguments"] = function_payload.get("arguments") or call.function.arguments or "{}"
            call_payload["function"] = function_payload
            call_payload["id"] = call_payload.get("id") or call.id
            call_payload["type"] = call_payload.get("type") or "function"
            tool_calls.append(call_payload)
        if tool_calls:
            payload["tool_calls"] = tool_calls
        return payload

    async def _execute_named_tool(self, name: str, args: dict, direct_texts: list, cevap_metinleri: list, on_direct_text=None, on_cevap_metni=None):
        if name == "ekrana_yazdir":
            metin = args.get("metin", "")
            if metin:
                direct_texts.append(metin)
                print("📠 [Sistem Call]: Ekrana yazdiriliyor...")
                if on_direct_text:
                    await on_direct_text(metin)
            return "Basariyla ekrana gonderildi."

        if name == "metinle_cevapla":
            cevap = args.get("cevap", "")
            if cevap:
                cevap_metinleri.append(cevap)
                print(f"💬 [Cevap Call]: Metin yaniti alindi ({len(cevap)} karakter)")
                if on_cevap_metni:
                    await on_cevap_metni(cevap)
            return "Cevap metin olarak gonderildi."

        func = self._build_full_tool_map().get(name)
        if not func:
            return f"[Hata]: {name} adinda bir tool veya submodel bulunamadi."

        is_submodel = name in self._submodel_func_map
        if not self._is_runtime_callable_active(name):
            kind = "Alt ajan" if is_submodel else "Tool"
            self.log_message("sistem", f"{kind} pasif oldugu icin cagri engellendi: {name}")
            return f"[Hata]: {kind} pasif: {name}. Terminalden aktiflestirilmeden calistirilamaz."

        emoji = "🤖" if is_submodel else "🔧"
        label = "submodel" if is_submodel else "tool"

        self.log_message(label, f"{name} cagriliyor... Argumanlar: {args}")
        start_time = time.time()
        start_offset = start_time - getattr(self, "_request_start_time", start_time)

        if on_direct_text:
            await on_direct_text(f"[+{start_offset:.1f}s] {emoji} {name} calistiriliyor...")

        try:
            if inspect.iscoroutinefunction(func):
                timeout_seconds = self._get_tool_timeout_seconds(name)
                if timeout_seconds is None:
                    result = await func(**args)
                else:
                    result = await asyncio.wait_for(func(**args), timeout=timeout_seconds)
            else:
                result = await asyncio.to_thread(func, **args)

            end_time = time.time()
            duration = end_time - start_time
            end_offset = end_time - getattr(self, "_request_start_time", end_time)

            if on_direct_text:
                await on_direct_text(f"[+{end_offset:.1f}s] ✅ {name} bitti ({duration:.1f}s)")

            self.log_message(label, f"{name} bitti ({duration:.1f}s)")
            self.metrics.append({"name": name, "duration": round(duration, 2), "time": time.strftime("%H:%M:%S")})
            if len(self.metrics) > 20:
                self.metrics.pop(0)
            return result
        except asyncio.TimeoutError:
            print(f"⌛ [Timeout]: {name} cok uzun surdugu icin kesildi!")
            timeout_seconds = self._get_tool_timeout_seconds(name) or DEFAULT_TOOL_TIMEOUT_SECONDS
            return f"[SISTEM_MESAJI_GIZLI] {name} araci veya ajani {int(timeout_seconds)}sn zaman asimina ugradi."
        except Exception as e:
            self.log_message(label, f"{name} hatasi: {e}")
            return f"[SISTEM_MESAJI_GIZLI] {name} hatasi: {e}"

    async def _run_chat_loop(self, messages: list[dict], on_direct_text=None, on_cevap_metni=None) -> tuple[bytes, str, list, list]:
        direct_texts = []
        cevap_metinleri = []
        tool_schemas = self._build_tool_schemas()
        self._request_start_time = time.time()
        final_text = ""
        tool_repeat_counts: dict[str, int] = {}

        blocked_message = self._get_provider_unavailable_message()
        if blocked_message:
            cevap_metinleri.append(blocked_message)
            if on_cevap_metni:
                await on_cevap_metni(blocked_message)
            return b"", blocked_message, direct_texts, cevap_metinleri

        for _ in range(12):
            try:
                create_kwargs = {
                    "model": self.model,
                    "messages": messages,
                    "tools": tool_schemas,
                    "tool_choice": "auto",
                }
                if self.reasoning_effort:
                    create_kwargs["reasoning_effort"] = self.reasoning_effort

                completion = await self._client.chat.completions.create(
                    **create_kwargs,
                )
            except Exception as e:
                if self._is_thought_signature_error(e):
                    final_text = self._format_thought_signature_message()
                    self.log_message("sistem", f"Thought signature hatasi: {e}")
                    cevap_metinleri.append(final_text)
                    if on_cevap_metni:
                        await on_cevap_metni(final_text)
                    return b"", final_text, direct_texts, cevap_metinleri
                if self._is_provider_quota_error(e):
                    self._mark_provider_temporarily_unavailable(e)
                    final_text = self._get_provider_unavailable_message() or self._format_provider_quota_message()
                    cevap_metinleri.append(final_text)
                    if on_cevap_metni:
                        await on_cevap_metni(final_text)
                    return b"", final_text, direct_texts, cevap_metinleri
                raise

            telemetry.record_usage("base", self.model, getattr(completion, "usage", None))

            message = completion.choices[0].message
            current_text = self._extract_message_text(message)
            tool_calls = getattr(message, "tool_calls", None) or []

            if tool_calls:
                messages.append(self._assistant_message_payload(message))
                if current_text:
                    final_text = current_text

                for call in tool_calls:
                    args = self._parse_tool_args(call.function.arguments)
                    repeat_key = self._tool_repeat_key(call.function.name, args)
                    tool_repeat_counts[repeat_key] = tool_repeat_counts.get(repeat_key, 0) + 1
                    repeat_limit = 1 if call.function.name == "context_paketi_oku" else 2
                    if tool_repeat_counts[repeat_key] > repeat_limit:
                        result = (
                            "[SISTEM_MESAJI_GIZLI] Ayni tool ayni argumanlarla bu istek icinde "
                            "zaten calisti. Sonucu tekrar isteme; mevcut baglamla devam et veya "
                            "gerekirse kullaniciya net blokaj bildir."
                        )
                        self.log_message("sistem", f"Tekrarlayan tool cagrisi engellendi: {call.function.name}")
                    else:
                        result = await self._execute_named_tool(
                            call.function.name,
                            args,
                            direct_texts,
                            cevap_metinleri,
                            on_direct_text=on_direct_text,
                            on_cevap_metni=on_cevap_metni,
                        )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.function.name,
                        "content": json.dumps({"result": str(result)[:4000]}, ensure_ascii=False),
                    })
                continue

            if current_text:
                final_text = current_text
                if not cevap_metinleri:
                    cevap_metinleri.append(current_text)
                    if on_cevap_metni:
                        await on_cevap_metni(current_text)
                return b"", final_text, direct_texts, cevap_metinleri

            break

        if cevap_metinleri:
            final_text = cevap_metinleri[-1]
        elif not final_text:
            final_text = "Yanıt üretilemedi."
            cevap_metinleri.append(final_text)
            if on_cevap_metni:
                await on_cevap_metni(final_text)

        return b"", final_text, direct_texts, cevap_metinleri

    async def text_query(self, user_text: str, context: str = "", image_bytes: bytes = None, on_direct_text=None, on_cevap_metni=None) -> tuple[bytes, str, list, list]:
        """Tum kanallarin (terminal, heartbeat, Telegram, Discord, embed API) gectigi tek nokta.

        Cagriyi telemetry'de bir 'run' olarak kaydeder; cagiran zaten bir run actiysa (ornegin
        heartbeat) ona katilir, boylece LLM kullanimi dogru run'a yazilir.
        """
        label = " ".join((user_text or "").split())[:80] or "(bos mesaj)"
        with telemetry.run_scope(telemetry.current_source(), label, detail="text_query") as run:
            result = await self._text_query_impl(user_text, context, image_bytes, on_direct_text, on_cevap_metni)
            if isinstance(result, tuple) and len(result) > 1:
                run.set_summary(result[1])
            return result

    async def _text_query_impl(self, user_text: str, context: str = "", image_bytes: bytes = None, on_direct_text=None, on_cevap_metni=None) -> tuple[bytes, str, list, list]:
        self._current_image = image_bytes
        try:
            from MarketingApp.araclar import rol_oku

            aktif_rol = rol_oku()
            system_instruction = self._build_runtime_system_instruction()
            if not aktif_rol.startswith("⚠️") and not aktif_rol.startswith("❌"):
                system_instruction += (
                    "\n\n=========== SENIN MARKETING KISILIGIN (ZORUNLU) ===========\n"
                    f"{aktif_rol}\n"
                    "========================================================\n"
                )

            user_parts = []
            if context:
                user_parts.append(context)
            if image_bytes:
                user_parts.append("Kullanici bir gorsel de gonderdi; bu gecici metin modunda gorsel bytes modele iletilmiyor.")
            user_parts.append(user_text)

            messages = [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": "\n\n".join(part for part in user_parts if part)},
            ]
            return await self._run_chat_loop(messages, on_direct_text=on_direct_text, on_cevap_metni=on_cevap_metni)
        finally:
            self._current_image = None

    async def audio_query(self, pcm_audio: bytes, context: str = "", image_bytes: bytes = None, on_direct_text=None, on_cevap_metni=None) -> tuple[bytes, str, list, list]:
        recognizer = sr.Recognizer()
        user_text = await asyncio.to_thread(_process_stt, pcm_audio, recognizer, INPUT_RATE)
        if not user_text:
            user_text = "Kullanicinin sesli mesaji net cozumlenemedi. Uygun ve kisa bir aciklama ile tekrar istemesini soyle."

        _audio, transcript, direct_texts, cevap_metinleri = await self.text_query(
            user_text=user_text,
            context=context,
            image_bytes=image_bytes,
            on_direct_text=on_direct_text,
            on_cevap_metni=on_cevap_metni,
        )
        return b"", transcript, direct_texts, cevap_metinleri
