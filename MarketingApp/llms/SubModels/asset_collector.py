"""Asset Collector Agent SubModel — App'in onayli medyasini listeleyen, yayina hazirlayan ve video uretimi isteyen uzman."""

from __future__ import annotations

import json
import re

from .base import SubModel, SubModelRateLimitError, register_submodel
from MarketingApp.araclar import ASSET_COLLECTOR_ARACLARI
from MarketingApp.llms.runtime_config import (
    get_model_api_key,
    get_model_api_keys,
    get_openai_compat_base_url,
    get_provider_display_name,
    get_submodel_model_name,
    get_submodel_reasoning_effort,
)


DEFAULT_SYSTEM_PROMPT = (
    "Sen App'in onayli medyasini yoneten bir uzmansin: varliklari listelersin, yayina "
    "hazirlarsin ve gerekirse App'ten yeni video uretimi istersin. content_creator_agent ve "
    "sosyal_medya_agent senin hazirladigin medyayi kullanir.\n\n"
    "CALISMA PRENSIPLERI:\n"
    "1. App'e ICERIK yazma, onay durumu degistirme veya medya yukleme yapma: onay App'in kararidir.\n"
    "2. Once `app_baglanti_durumu` ile App'in bagli olup olmadigini kontrol et; bagli degilse acikca soyle, "
    "veri uretmis gibi davranma.\n"
    "3. Varlik listelemek icin `app_asset_listele`, tek varligin detayi icin `app_asset_detay` kullan.\n"
    "4. Bir varligi YAYINA hazirlamak icin `medya_hazirla(asset_ids, aksiyon, platformlar)` kullan; "
    "basariliysa dondurdugu `media_ref`'i son yanitina AYNEN yaz. Imzali URL'leri asla tahmin etme, "
    "uretme ya da bir yere yazma: sana zaten gosterilmezler. `medya_dogrula` sadece App disi bir HTTPS "
    "URL'yi dogrulamak icindir.\n"
    "5. Bir medya `hazir: false` donerse sorunlari (JPEG degil, Content-Length yok, TikTok 64 MB siniri...) "
    "oldugu gibi raporla; kendi basina 'duzelttim' iddia etme.\n"
    "6. Yeni bir video gerekiyorsa ONCE havuzda ayni isi gorecek onayli bir varlik ara. Bulamazsan "
    "`video_uretimi_iste` kullan: bu UCRETLI olabilir, onay ister ve gunluk siniri vardir. Brief duz "
    "aciklayici bir metin olsun (konu, ton, kitle, akis) — komut ya da kod degil. Ayni brief'i tekrar tekrar gonderme.\n"
    "7. Uretim uzun surer: `video_uretimi_durumu` ile seyrek kontrol et. Biten videonun varligi bir TASLAKTIR; "
    "App onaylayip havuza koymadan (`app_asset_detay` basarili olmadan) yayinlanamaz — bunu acikca belirt.\n"
    "8. Onemli bir is bitince `context_aksiyon_kaydet` ile kisa bir iz birak.\n"
    "9. Yanitlarini Turkce ver.\n"
)


class AssetCollectorAgentSubModel(SubModel):
    """App'in onayli video/gorsel/ses/dokuman varliklarini listeleyip detaylandiran ajan."""

    def __init__(self):
        api_keys = get_model_api_keys()
        api_key = api_keys[0] if api_keys else get_model_api_key()
        self.provider_name = get_provider_display_name()
        self.reasoning_effort = get_submodel_reasoning_effort()
        if not api_key:
            print(f"⚠️  UYARI: {self.provider_name} API anahtari bulunamadi!")

        super().__init__(
            name="asset_collector_agent",
            description=(
                "App'in onayli medya varliklarini (video, gorsel, ses, dokuman) listeleme ve "
                "detaylandirma, bir varligi yayin hedefi icin hazirlama/dogrulama (`media_ref` uretir) "
                "ve App'ten yeni video uretimi isteme uzmani. Yayinlama yapmaz."
            ),
            model_id=get_submodel_model_name(),
            api_key=api_key,
            tools=ASSET_COLLECTOR_ARACLARI,
        )
        self._configure_openai_client(get_openai_compat_base_url(), api_keys)

    def _strip_thought_blocks(self, text: str) -> str:
        return re.sub(r"<thought>.*?</thought>", "", text or "", flags=re.DOTALL | re.IGNORECASE).strip()

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
            return self._strip_thought_blocks("\n".join(part.strip() for part in texts if part and part.strip()))
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

    def _parse_tool_args(self, raw_args) -> dict:
        if isinstance(raw_args, dict):
            return raw_args
        if not raw_args:
            return {}
        try:
            return json.loads(raw_args)
        except Exception:
            return {}

    async def run(self, gorev: str) -> str:
        print(f"\n📦 [{self.name}] Asset toplama gorevi baslatiliyor: {gorev[:120]}...")

        messages = [
            {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
            {"role": "user", "content": gorev},
        ]
        final_response = "Tamamlandi"

        try:
            for _ in range(12):
                create_kwargs = {
                    "model": self.model_id,
                    "messages": messages,
                    "tools": self._build_tool_schemas(),
                    "tool_choice": "auto",
                }
                if self.reasoning_effort:
                    create_kwargs["reasoning_effort"] = self.reasoning_effort

                completion = await self._create_chat_completion_with_failover(create_kwargs)
                message = completion.choices[0].message
                current_text = self._extract_message_text(message)
                tool_calls = getattr(message, "tool_calls", None) or []

                if tool_calls:
                    messages.append(self._assistant_message_payload(message))
                    if current_text:
                        final_response = current_text

                    for call in tool_calls:
                        args = self._parse_tool_args(call.function.arguments)
                        result = await self._execute_tool(call.function.name, args)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "name": call.function.name,
                                "content": json.dumps({"result": str(result)[:6000]}, ensure_ascii=False),
                            }
                        )
                    continue

                if current_text:
                    final_response = current_text
                break
        except Exception as e:
            err = str(e)
            if "429" in err or "quota" in err.lower() or "limit" in err.lower():
                print(f"  ⚠️ [{self.name}] {self.provider_name} limit hatasi! BaseModel'e devrediliyor.")
                raise SubModelRateLimitError(self.name, self.tools)
            print(f"  ❌ [{self.name}] API Hatasi: {e}")
            return f"Asset Collector Agent Hatasi: {e}"

        print(f"  ✅ [{self.name}] Gorev tamamlandi.")
        return final_response


register_submodel(AssetCollectorAgentSubModel())
