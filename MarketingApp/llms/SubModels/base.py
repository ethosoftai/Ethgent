import json
import inspect
from abc import ABC, abstractmethod

from openai import AsyncOpenAI

from MarketingApp import telemetry


AUTO_CONTEXT_LOG_TOOLS = {
    "save_x_market_snapshot",
    "update_queue_item",
    "send_x_reply",
    "publish_x_post",
    "publish_x_post_with_media",
    "submit_current_x_composer",
    "publish_x_thread",
    "reply_to_x_post",
    "mark_queue_item",
    "like_x_post",
    "bookmark_x_post",
    "repost_x_post",
    "quote_x_post",
    "follow_x_account",
    "engage_with_x_post",
    "inspect_instagram_profile",
    "inspect_instagram_post",
    "like_instagram_post",
    "follow_instagram_account",
    "comment_instagram_post",
    "search_youtube_videos",
    "inspect_youtube_channel",
    "inspect_youtube_video",
    "like_youtube_video",
    "subscribe_youtube_channel",
    "website_iceriginden_post_paketi_uret",
    "html_css_post_olustur_ve_png_kaydet",
    "video_post_olustur_ve_mp4_kaydet",
    "worker_video_yayinla",
    "worker_instagram_carousel_yayinla",
    "video_uretimi_iste",
}


def _compact_for_log(value, limit: int = 700) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _pick_first(mapping: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = mapping.get(key)
        if value:
            return str(value)
    return ""


def _platform_for_tool(name: str) -> str:
    if "_x_" in name or name.startswith(("publish_x", "reply_to_x", "send_x", "submit_current_x")):
        return "X"
    if "instagram" in name:
        return "Instagram"
    if "youtube" in name:
        return "YouTube"
    if name == "worker_video_yayinla":
        return "worker (multi-platform)"
    if name == "video_uretimi_iste":
        return "content"
    if name in {"html_css_post_olustur_ve_png_kaydet", "video_post_olustur_ve_mp4_kaydet", "website_iceriginden_post_paketi_uret"}:
        return "content"
    return "workspace"


def _result_to_mapping(result) -> dict:
    if isinstance(result, dict):
        return result
    try:
        parsed = json.loads(result)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


class SubModelRateLimitError(Exception):
    """
    SubModel'in çağrısı sırasında (genellikle Groq API) rate limit,
    kota aşımı veya genel bağlantı hatası alındığında tetiklenir.
    BaseModel (Gemini) bu hatayı yakalayarak görevi bizzat kendisi sürdürür.
    """
    def __init__(self, submodel_name: str, tools: list):
        self.submodel_name = submodel_name
        self.tools = tools
        super().__init__(f"[{submodel_name}] API rate limitine veya kritik bır hataya takıldı.")


class SubModel(ABC):
    """
    Tüm SubModel'lerin uyması gereken soyut temel sınıf.
    Her SubModel bir 'mini ajan'dır — kendi tool'ları ve AI modeli vardır.
    """

    def __init__(self, name: str, description: str, model_id: str, api_key: str, tools: list = None):
        """
        Args:
            name: SubModel'in benzersiz adı (registry key).
            description: BaseModel'in bu SubModel'i ne zaman kullanacağını anlaması için açıklama.
            model_id: Kullanılacak AI modeli.
            api_key: API anahtarı.
            tools: Bu SubModel'in kullanabileceği fonksiyonların listesi.
        """
        self.name = name
        self.description = description
        self.model_id = model_id
        self.api_key = api_key
        self.tools = tools or []
        self._tool_map = {func.__name__: func for func in self.tools}
        self._api_keys = [api_key] if api_key else []
        self._api_key_index = 0
        self._openai_base_url = ""

    @abstractmethod
    async def run(self, gorev: str) -> str:
        """
        Görevi alır, kendi AI modeli + tool'ları ile çalıştırır, sonucu döndürür.
        Tool-calling loop implementasyonu alt sınıflarda yapılır.

        Args:
            gorev: BaseModel'den gelen görev açıklaması.

        Returns:
            Görevin sonucu (metin).
        """
        ...

    def _build_tool_schemas(self) -> list[dict]:
        """Tool fonksiyonlarından JSON schema üretir (function calling için)."""
        schemas = []
        for func in self.tools:
            sig = inspect.signature(func)
            properties = {}
            required = []

            for param_name, param in sig.parameters.items():
                # Parametre tipini al
                annotation = param.annotation
                param_type = "string"  # varsayılan
                if annotation == int:
                    param_type = "integer"
                elif annotation == float:
                    param_type = "number"
                elif annotation == bool:
                    param_type = "boolean"

                properties[param_name] = {"type": param_type}

                # Varsayılanı yok ise zorunlu
                if param.default is inspect.Parameter.empty:
                    required.append(param_name)

            schema = {
                "type": "function",
                "function": {
                    "name": func.__name__,
                    "description": (func.__doc__ or "").strip(),
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required
                    }
                }
            }
            schemas.append(schema)

        return schemas

    def _configure_openai_client(self, base_url: str, api_keys: list[str] | None = None):
        unique_keys = []
        for key in api_keys or self._api_keys or [self.api_key]:
            cleaned = str(key or "").strip()
            if cleaned and cleaned not in unique_keys:
                unique_keys.append(cleaned)

        self._api_keys = unique_keys
        self._api_key_index = 0
        self._openai_base_url = base_url
        self.api_key = self._api_keys[0] if self._api_keys else ""
        self._client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self._openai_base_url,
        )

    def _rotate_openai_client(self) -> bool:
        if self._api_key_index + 1 >= len(self._api_keys):
            return False

        self._api_key_index += 1
        self.api_key = self._api_keys[self._api_key_index]
        self._client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self._openai_base_url,
        )
        print(
            f"  🔁 [{self.name}] Yedek API anahtarina gecildi "
            f"({self._api_key_index + 1}/{len(self._api_keys)})"
        )
        return True

    def _is_retryable_provider_error(self, exc: Exception) -> bool:
        err = str(exc).lower()
        retryable_markers = (
            "internal error",
            "internal server error",
            "server had an error",
            "upstream error",
            "service unavailable",
            "temporarily unavailable",
            "overloaded",
            "backend error",
            "connection error",
            "connection reset",
            "remoteprotocolerror",
            "timed out",
            "timeout",
            "rate limit",
            "quota",
            "429",
            "500",
            "502",
            "503",
            "504",
        )
        return any(marker in err for marker in retryable_markers)

    async def _create_chat_completion_with_failover(self, create_kwargs: dict):
        attempts = max(1, len(self._api_keys) or 1)
        last_exc = None

        for attempt in range(attempts):
            try:
                response = await self._client.chat.completions.create(**create_kwargs)
                telemetry.record_usage(self.name, self.model_id, getattr(response, "usage", None))
                return response
            except Exception as exc:
                last_exc = exc
                can_retry = attempt < attempts - 1 and self._is_retryable_provider_error(exc)
                if can_retry and self._rotate_openai_client():
                    continue
                raise

        if last_exc:
            raise last_exc

    async def _execute_tool(self, name: str, arguments: dict):
        """İsme göre tool'u çalıştırır (Bloklamadan/Non-blocking)."""
        func = self._tool_map.get(name)
        if func:
            safe_args = arguments if isinstance(arguments, dict) else {}
            print(f"  🔧 [{self.name}] Tool çağrısı: {name}({safe_args})")
            
            if inspect.iscoroutinefunction(func):
                result = await func(**safe_args)
            else:
                import asyncio
                result = await asyncio.to_thread(func, **safe_args)

            self._auto_log_context_action(name, safe_args, result)
            print(f"  ✅ [{self.name}] Sonuç: {str(result)[:200]}...")
            return result
        else:
            return f"[Hata]: {name} adında bir tool bulunamadı."

    def _auto_log_context_action(self, tool_name: str, arguments: dict, result):
        if tool_name not in AUTO_CONTEXT_LOG_TOOLS:
            return

        result_map = _result_to_mapping(result)
        url = _pick_first(
            result_map,
            ("tweet_url", "post_url", "resolved_url", "url", "page_url", "pexels_url"),
        ) or _pick_first(arguments, ("tweet_url", "post_url", "url"))
        dosya = _pick_first(
            result_map,
            ("png_path", "mp4_path", "workspace_markdown_path", "output_path", "media_path", "file_path"),
        ) or _pick_first(arguments, ("media_path", "dosya", "file_path"))
        konu = _pick_first(arguments, ("konu", "topic", "query", "platform"))
        sonuc = str(result_map.get("status") or result_map.get("result") or "completed")
        ozet = (
            f"{tool_name} araci calisti. "
            f"Arguman ozeti: {_compact_for_log(arguments, 420)}. "
            f"Sonuc ozeti: {_compact_for_log(result, 520)}"
        )

        try:
            from MarketingApp.araclar.workspace_araclari import context_aksiyon_kaydet

            context_aksiyon_kaydet(
                ajan=self.name,
                eylem=f"tool:{tool_name}",
                ozet=ozet,
                sonuc=_compact_for_log(sonuc, 80),
                platform=_platform_for_tool(tool_name),
                konu=_compact_for_log(konu, 160),
                url=_compact_for_log(url, 500),
                dosya=_compact_for_log(dosya, 500),
            )
        except Exception as exc:
            print(f"  ⚠️ [{self.name}] Context auto-log atlandi: {exc}")

    def __repr__(self):
        tool_names = [f.__name__ for f in self.tools]
        return f"<SubModel name='{self.name}' model='{self.model_id}' tools={tool_names}>"


# ─── Global SubModel Registry ───────────────────────────────────────────────

_SUBMODEL_REGISTRY: dict[str, SubModel] = {}


def register_submodel(instance: SubModel):
    """Bir SubModel instance'ını registry'ye kaydeder."""
    _SUBMODEL_REGISTRY[instance.name] = instance
    print(f"  ✅ SubModel kaydedildi: {instance}")


def get_submodel(name: str) -> SubModel:
    """İsme göre kayıtlı SubModel'i döndürür."""
    if name not in _SUBMODEL_REGISTRY:
        available = ", ".join(_SUBMODEL_REGISTRY.keys()) or "(boş)"
        raise KeyError(
            f"'{name}' adında bir SubModel bulunamadı. "
            f"Kayıtlı modeller: {available}"
        )
    return _SUBMODEL_REGISTRY[name]


def list_submodels() -> dict[str, str]:
    """Kayıtlı tüm SubModel isimlerini ve açıklamalarını döndürür."""
    return {name: sm.description for name, sm in _SUBMODEL_REGISTRY.items()}


def get_all_submodels() -> list[SubModel]:
    """Kayıtlı tüm SubModel instance'larını döndürür."""
    return list(_SUBMODEL_REGISTRY.values())
