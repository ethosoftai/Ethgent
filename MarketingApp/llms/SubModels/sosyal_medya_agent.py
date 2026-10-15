"""
Sosyal Medya Agent SubModel — X (Twitter), Instagram ve YouTube otomasyon uzmani.

OpenAI-compatible endpoint uzerinde tool-calling ile calisir.
Tum sosyal medya gorevlerini (post yayinlama, yorum, begeni, takip,
bildirim tarama, piyasa analizi vb.) tek merkezden yonetir.
"""

from __future__ import annotations

import json
import re
import traceback

from openai import AsyncOpenAI

from .base import SubModel, SubModelRateLimitError, register_submodel
from MarketingApp.araclar import SOSYAL_MEDYA_ARACLARI
from MarketingApp.llms.runtime_config import (
    get_model_api_key,
    get_model_api_keys,
    get_openai_compat_base_url,
    get_provider_display_name,
    get_submodel_model_name,
    get_submodel_reasoning_effort,
)


DEFAULT_SYSTEM_PROMPT = (
    "Sen bir sosyal medya otomasyon uzmansin. X (Twitter), Instagram ve YouTube "
    "platformlarinda icerik uretimi, etkilesim ve analiz gorevlerini yonetirsin.\n\n"
    "CALISMA PRENSIPLERI:\n"
    "1. Once gorev tanimimdaki tum talimatlari dikkatlice oku.\n"
    "2. Gerekli bilgileri toplamak icin uygun araclari kullan "
    "(snapshot_x_feed, get_x_queue, scan_x_notifications vb.).\n"
    "2b. Karar vermeden once `context_paketi_oku` ile persona, market_state, idea_pool ve son aksiyon ozetini oku; tum workspace'i modele yigma.\n"
    "3. Icerik uretirken:\n"
    "   - Her post/yorum tek bir ana fikir tasisin.\n"
    "   - Maksimum 240 karakter sinirini asma.\n"
    "   - Ayni kalibi veya aciyi tekrarlama.\n"
    "   - Spam, manipulatif dil veya bos icerik uretme.\n"
    "4. Aksiyon adimlarini sirayla yap; once durumu oku, sonra karar ver, sonra uygula.\n"
    "5. Her basarili veya basarisiz aksiyondan sonra `context_aksiyon_kaydet` ile "
    "social/automation_log.md ve social/recent_actions.md dosyalarina standart kayit dus.\n"
    "6. Basarisiz bir islem olursa hata mesajini raporla, gereksiz tekrarlardan kacin.\n"
    "7. X tarayicisi acik degilse once `launch_x_browser()` veya `launch_social_browser()` cagir.\n"
    "8. Tarayici durumunu `get_browser_status()` ile kontrol edebilirsin.\n"
    "9. Elinde yerel bir PNG/JPG dosya yolu varsa ve gorselli post isteniyorsa `publish_x_post_with_media` ile once drafti hazirla; bu arac son Post tusuna basmaz.\n"
    "10. Draft hazir olduktan sonra `submit_current_x_composer` ile son Post/Reply tusuna bas.\n"
    "11. Submit sonrasinda `verify_current_x_submission` ile dogrulama yap; dogrulama basariliysa `resolve_recent_x_status_url` ile tweet URL'sini ayri coz.\n"
    "12. Composer ekrani hazir ama son Post/Reply tusuna basamadiysan `submit_current_x_composer` kurtarma aracini kullan.\n"
    "13. Yuksek seviyeli X araclari takilirsa ayni oturumda browser fallback araclarini kullanabilirsin: "
    "`browser_hizli_durum_oku`, `browser_ilgili_bolumleri_getir`, `browser_bul`, "
    "`browser_click_text`, `browser_click_role`, `browser_click_css`, `browser_click_id`, "
    "`browser_eleman_bekle`, `browser_bekle`, `browser_screenshot`.\n"
    "14. X'te gorselli post attiysan final yaniyta media dosya yolunu ve cozulen tweet URL'sini yaz.\n"
    "15. `worker_video_yayinla`/`worker_instagram_carousel_yayinla` sadece kuyruga IS EKLER; "
    "'kuyruga eklendi' mesaji YAYINLANDI anlamina gelmez ve onay reddedilirse is hic eklenmez. "
    "Bu iki tool'u kullandiktan sonra bir sure bekleyip `worker_yayin_durumu_sorgula(job_id)` ile "
    "gercek sonucu (done/failed) kontrol et; `context_aksiyon_kaydet`'e 'basarili' yazmadan once "
    "bu dogrulamayi yap.\n"
    "16. Gorev tamamlandiginda kisa ve net bir Turkce ozet ver.\n"
)


class SosyalMedyaAgentSubModel(SubModel):
    """X (Twitter), Instagram ve YouTube uzerinde icerik uretimi, etkilesim ve analiz uzmani."""

    def __init__(self):
        api_keys = get_model_api_keys()
        api_key = api_keys[0] if api_keys else get_model_api_key()
        self.provider_name = get_provider_display_name()
        self.reasoning_effort = get_submodel_reasoning_effort()
        if not api_key:
            print(f"⚠️  UYARI: {self.provider_name} API anahtari bulunamadi!")

        super(SosyalMedyaAgentSubModel, self).__init__(
            name="sosyal_medya_agent",
            description=(
                "X (Twitter), Instagram ve YouTube uzerinde sosyal medya otomasyon uzmani. "
                "Post yayinlama, thread olusturma, yorum yapma, begeni/takip, bildirim tarama, "
                "piyasa snapshot'i alma, trend analizi, profil/post inceleme gibi tum sosyal medya "
                "gorevleri icin bu ajani kullan. Kripto, DeFi, NFT, blockchain icerik uretimi "
                "ve topluluk yonetimi konularinda uzmandir."
            ),
            model_id=get_submodel_model_name(),
            api_key=api_key,
            tools=SOSYAL_MEDYA_ARACLARI,
        )
        self._configure_openai_client(get_openai_compat_base_url(), api_keys)

    def _strip_thought_blocks(self, text: str) -> str:
        return re.sub(
            r"<thought>.*?</thought>", "", text or "", flags=re.DOTALL | re.IGNORECASE
        ).strip()

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
            combined = "\n".join(
                part.strip() for part in texts if part and part.strip()
            ).strip()
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

            function_payload = call_payload.get("function") or {}
            function_payload["name"] = (
                function_payload.get("name") or call.function.name
            )
            function_payload["arguments"] = (
                function_payload.get("arguments")
                or call.function.arguments
                or "{}"
            )
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

    def _looks_like_publish_success_log(self, tool_name: str, args: dict) -> bool:
        if tool_name != "context_aksiyon_kaydet":
            return False
        eylem = str(args.get("eylem", "")).strip().lower()
        sonuc = str(args.get("sonuc", "")).strip().lower()
        platform = str(args.get("platform", "")).strip().lower()
        return (
            eylem == "post_published"
            and sonuc == "success"
            and platform in {"x", "twitter", "x (twitter)"}
        )

    async def run(self, gorev: str) -> str:
        print(f"\n📱 [{self.name}] Sosyal medya gorevi baslatiliyor: {gorev[:120]}...")

        from MarketingApp.araclar import rol_oku

        aktif_rol = rol_oku()

        system_prompt = DEFAULT_SYSTEM_PROMPT

        if not aktif_rol.startswith("⚠️") and not aktif_rol.startswith("❌"):
            system_prompt += (
                "\n=========== MARKETING KISILIGI (ZORUNLU) ===========\n"
                f"{aktif_rol}\n"
                "===================================================\n"
            )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": gorev},
        ]
        final_response = "Tamamlandi"
        publish_flow_state = {
            "draft_ready": False,
            "submitted": False,
            "verified": False,
            "verification_state": "",
            "expected_text": "",
            "resolved_tweet_url": "",
        }

        try:
            for _ in range(16):
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
                        tool_name = call.function.name

                        if (
                            publish_flow_state["submitted"]
                            and not publish_flow_state["verified"]
                            and self._looks_like_publish_success_log(tool_name, args)
                        ):
                            result = {
                                "status": "blocked_until_verified",
                                "error": (
                                    "X postu success olarak kaydedilemez; once "
                                    "`verify_current_x_submission` ve sonra "
                                    "`resolve_recent_x_status_url` cagrilmalidir."
                                ),
                                "publish_flow_state": dict(publish_flow_state),
                            }
                        else:
                            result = await self._execute_tool(tool_name, args)

                        if tool_name == "publish_x_post_with_media" and isinstance(result, dict):
                            if result.get("status") == "draft_ready":
                                publish_flow_state["draft_ready"] = True
                                publish_flow_state["expected_text"] = (
                                    result.get("text", "") or publish_flow_state["expected_text"]
                                )
                        elif tool_name == "submit_current_x_composer" and isinstance(result, dict):
                            if result.get("status") == "submitted":
                                publish_flow_state["submitted"] = True
                                publish_flow_state["expected_text"] = (
                                    result.get("text", "") or publish_flow_state["expected_text"]
                                )
                        elif tool_name == "verify_current_x_submission" and isinstance(result, dict):
                            publish_flow_state["verified"] = bool(result.get("verified"))
                            publish_flow_state["verification_state"] = str(
                                result.get("verification_state", "") or ""
                            )
                            publish_flow_state["expected_text"] = (
                                result.get("expected_text", "") or publish_flow_state["expected_text"]
                            )
                        elif tool_name == "resolve_recent_x_status_url" and isinstance(result, dict):
                            publish_flow_state["resolved_tweet_url"] = str(
                                result.get("resolved_tweet_url", "") or ""
                            )

                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "name": tool_name,
                                "content": json.dumps(
                                    {"result": str(result)[:6000]},
                                    ensure_ascii=False,
                                ),
                            }
                        )
                    continue

                if publish_flow_state["submitted"] and not publish_flow_state["verified"]:
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "X publish akisi henuz tamamlanmadi. Final cevap verme ve "
                                "success kaydi dusme. Simdi "
                                "`verify_current_x_submission(expected_text=...)` cagir. "
                                "Dogrulama basariliysa hemen ardindan "
                                "`resolve_recent_x_status_url(expected_text=...)` cagir."
                            ),
                        }
                    )
                    continue

                if current_text:
                    final_response = current_text
                break

        except Exception as e:
            err = str(e)
            if "429" in err or "quota" in err.lower() or "limit" in err.lower():
                print(
                    f"  ⚠️ [{self.name}] {self.provider_name} limit hatasi! BaseModel'e devrediliyor."
                )
                raise SubModelRateLimitError(self.name, self.tools)
            print(f"  ❌ [{self.name}] API Hatasi ({type(e).__name__}): {e}")
            print(traceback.format_exc())
            return f"Sosyal Medya Agent Hatasi: {e}"

        print(f"  ✅ [{self.name}] Gorev tamamlandi.")
        return final_response


register_submodel(SosyalMedyaAgentSubModel())
