"""Platform Data Agent SubModel — platformlardan salt-okunur, manifest-onayli veri toplayan ve yorumlayan uzman."""

from __future__ import annotations

import json
import re

from .base import SubModel, SubModelRateLimitError, register_submodel
from MarketingApp.araclar import PLATFORM_DATA_ARACLARI
from MarketingApp.llms.runtime_config import (
    get_model_api_key,
    get_model_api_keys,
    get_openai_compat_base_url,
    get_provider_display_name,
    get_submodel_model_name,
    get_submodel_reasoning_effort,
)


DEFAULT_SYSTEM_PROMPT = (
    "Sen platform verisi toplayan ve yorumlayan bir analistsin (YouTube, Instagram, TikTok, X, Reddit). "
    "Veriyi platform_data_worker uzerinden, manifest-onayli SALT-OKUNUR aksiyonlarla toplarsin.\n\n"
    "CALISMA PRENSIPLERI:\n"
    "1. Yalnizca OKU. Paylasim, yorum, begeni, takip gibi hicbir yazma islemi yapamazsin ve yapmaya calisma.\n"
    "2. Once `platform_veri_eylemleri` ile hangi aksiyonlarin var oldugunu ve parametrelerini oku; listede "
    "olmayan bir aksiyonu ya da bildirilmeyen bir parametreyi uydurma — reddedilir.\n"
    "3. Veriyi `platform_veri_topla` ile iste. Sonuc gecikirse `platform_veri_durumu` ile SEYREK kontrol et; "
    "worker calismiyorsa bunu acikca soyle.\n"
    "4. Yalnizca istenen veriyi topla (gereksiz sayfalama, gereksiz hesap tarama yapma). Saatlik limitler vardir.\n"
    "5. `kisisel_veri_icerir` olan aksiyonlar baskalarinin icerigini dondurur (yorum yazarlari, metinler): "
    "bunlari toplu halde alintilama, kalici olarak kaydetme, bir kisiyi hedef alan cikarim yapma; "
    "ozetle ve genel egilimleri raporla.\n"
    "6. Sadece donen veriye dayan. Veri yoksa ya da is basarisizsa bunu soyle; rakam/metrik uydurma. "
    "Sonucun kirpildigi (truncated) belirtildiyse yorumunun eksik veriye dayandigini belirt.\n"
    "7. Platform kimlik bilgilerini, token'lari ya da imzali URL'leri isteme, tahmin etme veya yazma.\n"
    "8. Onemli bir bulguyu `context_aksiyon_kaydet` ile kisa kaydet (kisisel veri OLMADAN).\n"
    "9. Yanitlarini Turkce ver; ana bulgulari, kullandigin aksiyonlari ve verinin sinirlarini yaz.\n"
)


class PlatformDataAgentSubModel(SubModel):
    """App'in onayli video/gorsel/ses/dokuman varliklarini listeleyip detaylandiran ajan."""

    def __init__(self):
        api_keys = get_model_api_keys()
        api_key = api_keys[0] if api_keys else get_model_api_key()
        self.provider_name = get_provider_display_name()
        self.reasoning_effort = get_submodel_reasoning_effort()
        if not api_key:
            print(f"⚠️  UYARI: {self.provider_name} API anahtari bulunamadi!")

        super().__init__(
            name="platform_data_agent",
            description=(
                "Platformlardan (YouTube, Instagram, TikTok, X, Reddit) metrik, yorum ve "
                "hesap/icerik verisini manifest-onayli, SALT-OKUNUR aksiyonlarla toplayan ve "
                "yorumlayan analist. Platform kimlik bilgilerine erisimi yoktur; yazma islemi yapmaz."
            ),
            model_id=get_submodel_model_name(),
            api_key=api_key,
            tools=PLATFORM_DATA_ARACLARI,
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
        print(f"\n📊 [{self.name}] Platform veri toplama gorevi baslatiliyor: {gorev[:120]}...")

        messages = [
            {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
            {"role": "user", "content": gorev},
        ]
        final_response = "Tamamlandi"

        try:
            for _ in range(8):
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
            return f"Platform Data Agent Hatasi: {e}"

        print(f"  ✅ [{self.name}] Gorev tamamlandi.")
        return final_response


register_submodel(PlatformDataAgentSubModel())
