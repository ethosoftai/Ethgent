"""Ethgent icin interaktif terminal yonetim arayuzu."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import re
import shlex
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from MarketingApp.environments.automation_runtime import (
    get_automation_snapshot,
    release_automation,
    try_acquire_automation,
)
from MarketingApp.environments.heartbeat import (
    HeartbeatConfigError,
    add_task_to_content,
    get_heartbeat_jobs_snapshot,
    get_heartbeat_status_snapshot,
    parse_config_content,
    pause_heartbeat_job,
    read_config_content,
    reload_heartbeat_service,
    remove_task_from_content,
    resume_heartbeat_job,
    run_heartbeat_job,
    set_enabled_in_content,
    write_config_content,
)


from MarketingApp import telemetry
from MarketingApp.paths import workspace_path


def _studio():
    """agent_studio'yu tembel yukle; import aninda llms paketini ayaga kaldirmamak icin."""
    from MarketingApp.llms import agent_studio

    return agent_studio


def _runtime_config():
    """runtime_config'i tembel yukle; import aninda llms paketini ayaga kaldirmamak icin."""
    from MarketingApp.llms import runtime_config

    return runtime_config


def _bellek():
    """bellek_araclari'ni tembel yukle; import aninda araclar paketini ayaga kaldirmamak icin."""
    from MarketingApp.araclar import bellek_araclari

    return bellek_araclari


_HISTORY_FILE = workspace_path(".system", "terminal_chat_history.json")
_MAX_HISTORY = 30
_MAX_CONTEXT_MESSAGES = 12
_MAX_CONTEXT_CHARS = 5000
_AGENT_SWITCH_WORDS = {"on", "off", "toggle", "ac", "kapat", "aktif", "pasif", "degistir"}
_AFFIRMATIVE_ANSWERS = {"e", "evet", "y", "yes"}


class TerminalManager:
    """Sohbeti ve temel runtime islemlerini tek terminal dongusunde yonetir."""

    def __init__(
        self,
        base_model,
        *,
        telegram_enabled: bool = False,
        discord_enabled: bool = False,
        input_func: Callable[[str], Any] = input,
        output_func: Callable[[str], Any] = print,
        history_file: str = _HISTORY_FILE,
        source: str = "terminal",
    ):
        self.source = source
        self.base_model = base_model
        self.telegram_enabled = bool(telegram_enabled)
        self.discord_enabled = bool(discord_enabled)
        self.input_func = input_func
        self.output_func = output_func
        self.history_file = history_file
        self.history = self._load_history()
        self._original_request_approval = getattr(base_model, "request_approval", None)
        self._use_color = output_func is print and sys.stdout.isatty() and not os.getenv("NO_COLOR")

    async def run(self) -> None:
        self.base_model.request_approval = self._request_terminal_approval
        self._print_banner()
        try:
            while True:
                try:
                    line = await self._read_line(self._color("sen> ", "cyan"))
                except (EOFError, KeyboardInterrupt):
                    self._emit("\nTerminal oturumu kapatiliyor.")
                    return

                if line is None:
                    return
                if not await self.handle_line(str(line)):
                    return
        finally:
            if self._original_request_approval is not None:
                self.base_model.request_approval = self._original_request_approval

    async def handle_line(self, raw_line: str) -> bool:
        line = (raw_line or "").strip()
        if not line:
            return True
        if line.startswith("/"):
            return await self._handle_command(line)
        await self._chat(line)
        return True

    async def _read_line(self, prompt: str) -> str:
        if self.input_func is input:
            return await asyncio.to_thread(input, prompt)
        result = self.input_func(prompt)
        if inspect.isawaitable(result):
            return await result
        return result

    def _emit(self, message: str = "") -> None:
        self.output_func(str(message))

    def _color(self, text: str, color: str) -> str:
        if not self._use_color:
            return text
        codes = {
            "cyan": "\033[96m",
            "green": "\033[92m",
            "yellow": "\033[93m",
            "red": "\033[91m",
            "bold": "\033[1m",
        }
        return f"{codes.get(color, '')}{text}\033[0m"

    def _print_banner(self) -> None:
        model = getattr(self.base_model, "model", "bilinmiyor")
        provider = getattr(self.base_model, "provider_name", "bilinmiyor")
        self._emit("")
        self._emit(self._color("ETHGENT TERMINAL", "bold"))
        self._emit(f"Model: {model} | Saglayici: {provider}")
        self._emit(
            f"Telegram: {'aktif' if self.telegram_enabled else 'kapali'} | "
            f"Discord: {'aktif' if self.discord_enabled else 'kapali'}"
        )
        self._emit("Mesaj yaz veya komutlari gormek icin /help kullan. Cikmak icin /exit.")
        if self.history:
            self._emit(f"Onceki terminal sohbetinden {len(self.history)} mesaj yuklendi.")
        errors = self._studio_errors()
        if errors:
            self._emit(self._color(f"UYARI: {len(errors)} config hatasi var. /errors ile bak.", "yellow"))
        self._emit("")

    async def _handle_command(self, line: str) -> bool:
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            self._emit(self._color(f"Komut okunamadi: {exc}", "red"))
            return True

        command = parts[0].lower()
        args = parts[1:]

        if command in {"/exit", "/quit", "/q"}:
            self._emit("Ethgent kapatiliyor...")
            return False
        if command in {"/help", "/?"}:
            self._print_help()
        elif command == "/status":
            self._print_status()
        elif command == "/agents":
            self._print_agents()
        elif command == "/agent":
            await self._manage_agent(args)
        elif command == "/tools":
            self._print_tools(args)
        elif command == "/tool":
            await self._manage_tool(args)
        elif command == "/logs":
            self._print_logs(args)
        elif command == "/errors":
            self._print_errors(args)
        elif command == "/runs":
            self._print_runs(args)
        elif command == "/run":
            self._show_run(args)
        elif command == "/usage":
            self._print_usage(args)
        elif command == "/history":
            self._print_history()
        elif command == "/clear":
            self._clear_history()
        elif command == "/reload":
            self._reload_agents()
        elif command == "/heartbeat":
            await self._manage_heartbeat(args)
        elif command == "/provider":
            self._manage_provider(args)
        elif command == "/memory":
            self._manage_memory(args)
        elif command == "/prompt":
            self._manage_prompt(args)
        else:
            self._emit(self._color(f"Bilinmeyen komut: {command}. /help ile listeyi gorebilirsin.", "yellow"))
        return True

    def _print_help(self) -> None:
        self._emit(
            """
Komutlar
  /status                         Sistem ve kanal durumunu goster
  /agents                         Ajanlari listele
  /agent <ad> on|off|toggle       Ajan durumunu degistir
  /agent show <ad>                Ajan detayini goster
  /agent create <ad> [bayrak]     Yeni ajan olustur
  /agent edit <ad> [bayrak]       Var olan ajani duzenle
  /agent delete <ad> [--yes]      Config ajanini sil
  /agent copy <kaynak> <hedef>    Ajani tool listesiyle birlikte klonla
  /agent test <ad> "gorev"        Tek ajani dogrudan calistir
  /agent pack list                Kurulu agent pack'leri listele
  /agent pack preview <yol>       Pack'i kurmadan incele
  /agent pack install <yol>       Pack kur (--overwrite, --yes, --trust: sandbox'siz)
                                  <yol> yerine github:kullanici/repo[@dal][#alt/klasor] da olur
  /agent pack export <ad>         Kurulumunu paylasilabilir pakete cikar
  /tools [arama]                  Tool'lari listele veya filtrele
  /tools --group <ad>             Bir gruba ait tool'lari listele
  /tools --category <ad>          Bir kategorideki tool'lari listele
  /tools --risk high              Riske gore filtrele (low|medium|high)
  /tools --list-groups            Grup, kategori ve risk dagilimini say
  /tool <ad> on|off|toggle        Tool durumunu degistir
  /tool list                      Custom tool'lari listele
  /tool show <ad> [--code]        Custom tool detayini (ve kodunu) goster
  /tool create <ad> --file <yol>  Yeni custom tool ekle (--code ile satir ici)
  /tool create --file <yol> --names a,b   Tek dosyadan birden fazla tool kaydet
  /tool create --file <yol> --all         Dosyadaki tum fonksiyonlari tool yap
  /tool edit <ad> [bayrak]        Custom tool'u guncelle
  /tool delete <ad> [--yes]       Custom tool'u sil (--keep-file dosyayi birakir)
  /logs [adet] [--since 24h] [--type T] [--grep metin] [--run id]   Kalici loglar (--memory: sadece bellek)
  /runs [adet] [--source S] [--status S] [--since 24h]    Kosu gecmisi (terminal, heartbeat, Telegram...)
  /run <id>                       Bir kosunun detayi: sure, hata, token kirilimi, olaylar
  /usage [--since 24h] [--by agent|model|source|day|run]  Token kullanimi (ve varsa maliyet tahmini)
  /errors [arama]                 Config/runtime hatalarini goster
  /heartbeat                      Zamanlayici ve gorev durumunu goster
  /heartbeat show <id>            Gorev detayini goster
  /heartbeat log [id] [adet]      Gorevin gecmis kosulari: ne zaman, ne kadar surdu, ne uretti
  /heartbeat add --cron X --gorev "..."   Yeni zamanli gorev ekle
  /heartbeat remove <id> [--yes]  Zamanli gorevi sil
  /heartbeat run <id>             Bir heartbeat gorevini simdi calistir
  /heartbeat pause|resume <id>    Gorevi duraklat veya devam ettir
  /heartbeat on|off               Heartbeat config'ini aktif/pasif yap
  /heartbeat reload               Config'i diskten yeniden yukle
  /reload                         Ajan ve custom tool config'ini yenile
  /provider                       Aktif provider/model ve pinlenmis ajanlari goster
  /provider set <ad> [bayrak]     Provider/model degistir (.env.model'e yazar)
  /memory [kategori] [anahtar]    Uzun vadeli bellegi goster
  /memory search <metin>          Bellekte metin ara
  /memory delete <kategori> <anahtar> [--yes]   Bellekten bir kayit sil
  /prompt                         Orkestratorun aktif system prompt'unu goster (ozel mi, varsayilan mi)
  /prompt default                 Kod icindeki varsayilan orkestrator promptunu goster
  /prompt set "..."               Orkestrator promptunu degistir (config/orchestrator_prompt.md)
  /prompt reset                   Ozel promptu sil, varsayilana don
  /history                        Terminal sohbet gecmisini goster
  /clear                          Terminal sohbet gecmisini temizle
  /exit                           Uygulamayi guvenli sekilde kapat

Ajan bayraklari
  --model <ad>              Ajanin kullanacagi model (varsayilan: default)
  --tools a,b,c             Tool listesini komple ayarla
  --tool-group g1,g2        Bir grubun tamamini ekle (bkz. /tools --list-groups)
  --tool-category c1,c2     Bir kategorinin tamamini ekle
  --add-tools a,b           Mevcut listeye tool ekle (sadece edit)
  --remove-tools a,b        Mevcut listeden tool cikar (sadece edit)
  --remove-tool-group g1    Bir grubun tamamini cikar (sadece edit)
  --remove-tool-category c1 Bir kategorinin tamamini cikar (sadece edit)
  --tool-mode default|custom
  --prompt "..."            System prompt
  --desc "..."              Aciklama
  --builtin                 SubModels altinda gercek .py dosyasi uret (sadece create)
  --disabled                Pasif olarak olustur (sadece create)
  --enable / --disable      Ajani aktif/pasif yap (sadece edit)
  --dry-run                 Kaydetmeden sonucu goster

Custom tool bayraklari
  --file <yol.py>           Tool kodunu dosyadan al
  --code "..."              Tool kodunu satir ici ver
  --desc "..."              Model bu tool'u ne zaman cagiracagini buradan anlar
  --params "..."            Parametre notu (ornek JSON)
  --env A,B                 Gerekli env degiskenleri (.env.model dosyasina yazilir)
  --names a,b               Tek dosyadan kaydedilecek fonksiyon adlari (toplu kayit)
  --all                     Dosyadaki '_' ile baslamayan tum fonksiyonlari kaydet
  --overwrite               Var olan tool kayitlarinin uzerine yaz (toplu kayit)

Provider bayraklari (/provider set icin)
  --base-model M             BASE_MODEL_NAME
  --submodel-model M         SUBMODEL_MODEL_NAME
  --browser-model M          BROWSER_AGENT_MODEL
  --base-url URL             OPENAI_COMPAT_BASE_URL (OpenAI-uyumlu endpoint)
  --api-key K                Yeni provider'in API anahtari (gemini->GEMINI_API_KEY, moonshot->MOONSHOT_API_KEY, digerleri->{PROVIDER}_API_KEY)
  --reset-pins                agents.yaml'daki somut model isimlerini default sentinaline sifirla
  --dry-run                   Kaydetmeden neyin degisecegini goster

NOT: config tipi ajanlar varsayilan tool setini almaz; tool vermezsen ajan
tool'suz calisir. Varsayilan set fallback'i sadece builtin ajanlarda vardir.

Ornekler
  /agent create rapor_ajani --tool-category workspace,memory \\
      --desc "Haftalik rapor derleyici" --prompt "Sen rapor derleyen bir ajansin."
  /agent edit rapor_ajani --tool-group browser_agent --dry-run
  /agent copy sosyal_medya_agent test_sosyal
  /agent test rapor_ajani "Bu haftanin ozetini cikar"
  /tool create fiyat_getir --file ~/fiyat_getir.py --desc "Kripto fiyati doner"
  /tool create --file ~/kripto.py --names fiyat_getir,hacim_getir
  /agent pack export kripto_paketim --agents kripto_ajani --out ~/paketim
  /heartbeat add --cron "*/30" --gorev "Market snapshot al" --name "Market"
  /provider set deepseek --base-url https://api.deepseek.com --base-model deepseek-chat --reset-pins
  /memory search "ton"

Slash ile baslamayan her satir Ethgent'e mesaj olarak gonderilir.
""".strip()
        )

    def _print_status(self) -> None:
        uptime = max(0, int(time.time() - getattr(self.base_model, "start_time", time.time())))
        heartbeat = get_heartbeat_status_snapshot()
        automation = get_automation_snapshot()
        self._emit(self._color("Sistem durumu", "bold"))
        self._emit(f"  Model      : {getattr(self.base_model, 'model', '-')}")
        self._emit(f"  Saglayici  : {getattr(self.base_model, 'provider_name', '-')}")
        self._emit(f"  Uptime     : {self._format_duration(uptime)}")
        self._emit(f"  Telegram   : {'aktif' if self.telegram_enabled else 'kapali'}")
        self._emit(f"  Discord    : {'aktif' if self.discord_enabled else 'kapali'}")
        self._emit(
            f"  Heartbeat  : {'calisiyor' if heartbeat.get('running') else 'kapali'} "
            f"({heartbeat.get('job_count', 0)} gorev)"
        )
        if automation.get("busy"):
            self._emit(
                f"  Otomasyon  : mesgul - {automation.get('owner') or '-'} / "
                f"{automation.get('label') or automation.get('job_id') or '-'}"
            )
        else:
            self._emit("  Otomasyon  : hazir")

        errors = self._studio_errors()
        if errors:
            self._emit(
                self._color(
                    f"  Config     : {len(errors)} hata - detay icin /errors",
                    "yellow",
                )
            )
        else:
            self._emit("  Config     : temiz")

        self._print_telemetry_status()

    def _studio_errors(self, name: str = "") -> list[dict[str, Any]]:
        """BaseModel'in topladigi agent studio hatalari; istege bagli olarak tek ajana filtreli.

        Bu liste runtime'da doluyor ama hicbir yerde gosterilmiyordu: bilinmeyen tool,
        registry'de bulunamayan submodel, bozuk YAML girdisi hep sessizce yutuluyordu.
        """
        errors = list(getattr(self.base_model, "agent_studio_errors", []) or [])
        if not name:
            return errors
        return [item for item in errors if item.get("name") == name]

    def _print_errors(self, args: list[str]) -> None:
        errors = self._studio_errors()
        needle = " ".join(args).strip().lower()
        if needle:
            errors = [item for item in errors if needle in json.dumps(item, ensure_ascii=False).lower()]

        self._emit(self._color(f"Config hatalari ({len(errors)})", "bold"))
        if not errors:
            self._emit(self._color("  Hata yok.", "green"))
            return
        for item in errors:
            scope = item.get("scope") or "genel"
            target = item.get("name") or ""
            header = f"  [{scope}]" + (f" {target}" if target else "")
            self._emit(self._color(header, "yellow"))
            self._emit(f"      {item.get('message') or '-'}")
            if item.get("entry"):
                self._emit(f"      girdi: {json.dumps(item['entry'], ensure_ascii=False)[:200]}")

    def _print_agents(self) -> None:
        agents = self.base_model.get_hierarchy().get("submodels", [])
        self._emit(self._color(f"Ajanlar ({len(agents)})", "bold"))
        for agent in agents:
            state = "ON " if agent.get("active") else "OFF"
            self._emit(
                f"  [{state}] {agent.get('name')} | {agent.get('model') or '-'} | "
                f"{agent.get('tool_count', len(agent.get('tools') or []))} tool"
            )

    async def _manage_agent(self, args: list[str]) -> None:
        if not args:
            self._emit("Kullanim: /agent <ad> on|off|toggle  ya da  /agent create|edit|delete|show|list|pack ...")
            return

        # Eski kullanim once: /agent <ad> on|off|toggle
        if len(args) == 2 and args[1].lower() in _AGENT_SWITCH_WORDS:
            self._toggle_agent(args[0], args[1].lower())
            return

        studio = _studio()
        action = args[0].lower()
        rest = args[1:]
        try:
            if action == "list":
                self._print_agents()
            elif action == "show":
                self._show_agent(rest)
            elif action == "create":
                self._create_agent(rest)
            elif action == "edit":
                self._edit_agent(rest)
            elif action == "delete":
                await self._delete_agent(rest)
            elif action == "copy":
                self._copy_agent(rest)
            elif action == "test":
                await self._test_agent(rest)
            elif action == "pack":
                await self._manage_agent_pack(rest)
            else:
                self._emit("Kullanim: /agent <ad> on|off|toggle  ya da  /agent create|edit|delete|show|list|pack ...")
        except studio.AgentStudioError as exc:
            self._emit(self._color(f"Agent Studio hatasi: {exc}", "red"))
        except ValueError as exc:
            self._emit(self._color(str(exc), "red"))
        except Exception as exc:
            self._emit(self._color(f"Ajan islemi basarisiz: {exc}", "red"))

    def _toggle_agent(self, name: str, action: str) -> None:
        current = getattr(self.base_model, "active_agents", {}).get(name)
        if current is None:
            self._emit(self._color(f"Ajan bulunamadi: {name}", "red"))
            return
        try:
            target = self._resolve_switch(action, current)
            active = self.base_model.set_agent_active(name, target)
            self._emit(self._color(f"{name}: {'aktif' if active else 'pasif'}", "green"))
        except ValueError as exc:
            self._emit(str(exc))

    def _show_agent(self, args: list[str]) -> None:
        if len(args) != 1:
            self._emit("Kullanim: /agent show <ad>")
            return
        name = _studio().validate_agent_name(args[0])
        entry = self._find_agent_entry(name)
        if entry is None:
            self._emit(self._color(f"Ajan bulunamadi: {name}", "red"))
            return
        self._describe_agent(entry)

    def _create_agent(self, args: list[str]) -> None:
        positional, flags = self._parse_flags(args, bool_flags={"builtin", "disabled", "dry_run"})
        if len(positional) != 1:
            self._emit('Kullanim: /agent create <ad> [--model M] [--tools a,b] [--tool-group g1,g2] [--tool-category c1,c2] [--prompt "..."] [--desc "..."] [--tool-mode default|custom] [--builtin] [--disabled]')
            return

        name = _studio().validate_agent_name(positional[0])
        tools = self._split_name_list(flags.get("tools"))
        expanded = self._expand_tool_selectors(flags, group_key="tool_group", category_key="tool_category")
        if expanded:
            self._emit(f"Grup/kategori secimi {len(expanded)} tool'a genisletildi.")
            tools = list(dict.fromkeys(tools + expanded))
        tool_mode = str(flags.get("tool_mode") or ("custom" if tools else "default")).lower()
        entry = {
            "name": name,
            "type": "builtin" if flags.get("builtin") else "config",
            "enabled": not flags.get("disabled"),
            "description": flags.get("desc") or flags.get("description") or "",
            "model": flags.get("model") or "default",
            "tool_mode": tool_mode,
            "system_prompt": flags.get("prompt") or "",
            "tools": tools,
        }
        self._warn_unknown_tools(tools)
        if not flags.get("builtin") and not tools:
            self._emit(
                self._color(
                    "Uyari: config ajanlari varsayilan tool setini almaz; tool verilmedigi icin bu ajan "
                    "tool'suz calisacak. --tools / --tool-group / --tool-category ile ekleyebilirsin.",
                    "yellow",
                )
            )

        if flags.get("dry_run"):
            self._emit(self._color("[dry-run] Kaydedilmedi. Olusacak ajan:", "yellow"))
            self._describe_agent(entry)
            return

        if flags.get("builtin"):
            result = _studio().create_builtin_agent_scaffold(entry)
            saved = result["agent"]
            self._emit(self._color(f"Builtin ajan olusturuldu: {saved['name']}", "green"))
            self._emit(f"  Submodel dosyasi: {result['path']}")
        else:
            saved = _studio().upsert_agent_config(entry, create=True)
            self._emit(self._color(f"Config ajani olusturuldu: {saved['name']}", "green"))

        self._describe_agent(saved)
        self._reload_agents()

    def _edit_agent(self, args: list[str]) -> None:
        positional, flags = self._parse_flags(args, bool_flags={"enable", "disable", "dry_run"})
        if len(positional) != 1 or not flags:
            self._emit('Kullanim: /agent edit <ad> [--model M] [--tools a,b] [--add-tools a,b] [--remove-tools a,b] [--tool-group g1,g2] [--remove-tool-group g1] [--tool-category c1] [--remove-tool-category c1] [--tool-mode default|custom] [--prompt "..."] [--desc "..."] [--enable|--disable]')
            return

        name = _studio().validate_agent_name(positional[0])
        current = self._find_agent_entry(name)
        if current is None:
            self._emit(self._color(f"Ajan bulunamadi: {name}", "red"))
            return

        merged = dict(current)
        tools_changed = False

        if "model" in flags:
            merged["model"] = flags["model"]
        if "desc" in flags or "description" in flags:
            merged["description"] = flags.get("desc") or flags.get("description") or ""
        if "prompt" in flags:
            merged["system_prompt"] = flags["prompt"]

        # --tools listeyi komple degistirir; digerleri mevcut listeyi baz alir. Varsayilan
        # moddaki builtin ajanin bos listesi "hicbir tool" degil "kendi grubu" demektir,
        # o yuzden ekleme/cikarma oncesi grubu somutlastir.
        if "tools" in flags:
            merged["tools"] = self._split_name_list(flags["tools"])
            tools_changed = True
        elif any(
            key in flags
            for key in ("add_tools", "remove_tools", "tool_group", "tool_category", "remove_tool_group", "remove_tool_category")
        ):
            merged["tools"] = self._materialize_default_tools(merged)

        if "add_tools" in flags:
            additions = self._split_name_list(flags["add_tools"])
            merged["tools"] = list(dict.fromkeys(list(merged.get("tools") or []) + additions))
            tools_changed = True
        group_additions = self._expand_tool_selectors(flags, group_key="tool_group", category_key="tool_category")
        if group_additions:
            self._emit(f"Grup/kategori secimi {len(group_additions)} tool'a genisletildi.")
            merged["tools"] = list(dict.fromkeys(list(merged.get("tools") or []) + group_additions))
            tools_changed = True

        group_removals = self._expand_tool_selectors(
            flags, group_key="remove_tool_group", category_key="remove_tool_category"
        )
        if group_removals:
            self._emit(f"Grup/kategori cikarmasi {len(group_removals)} tool'a genisletildi.")
            dropped = set(group_removals)
            merged["tools"] = [item for item in (merged.get("tools") or []) if item not in dropped]
            tools_changed = True

        if "remove_tools" in flags:
            removals = set(self._split_name_list(flags["remove_tools"]))
            merged["tools"] = [item for item in (merged.get("tools") or []) if item not in removals]
            tools_changed = True

        if tools_changed:
            self._warn_unknown_tools(merged.get("tools") or [])
            merged["tool_mode"] = "custom"
        if "tool_mode" in flags:
            merged["tool_mode"] = str(flags["tool_mode"]).lower()

        if flags.get("enable"):
            merged["enabled"] = True
        if flags.get("disable"):
            merged["enabled"] = False

        if flags.get("dry_run"):
            before = len(current.get("tools") or [])
            after = len(merged.get("tools") or [])
            self._emit(self._color(f"[dry-run] Kaydedilmedi. Tool sayisi {before} -> {after}. Sonuc:", "yellow"))
            self._describe_agent(merged)
            return

        saved = _studio().upsert_agent_config(merged, create=False)
        self._emit(self._color(f"Ajan guncellendi: {saved['name']}", "green"))
        self._describe_agent(saved)
        self._reload_agents()

    def _copy_agent(self, args: list[str]) -> None:
        positional, flags = self._parse_flags(args, bool_flags={"disabled"})
        if len(positional) != 2:
            self._emit('Kullanim: /agent copy <kaynak> <hedef> [--model M] [--desc "..."] [--disabled]')
            return

        source_name = _studio().validate_agent_name(positional[0])
        target_name = _studio().validate_agent_name(positional[1])
        source = self._find_agent_entry(source_name)
        if source is None:
            self._emit(self._color(f"Kaynak ajan bulunamadi: {source_name}", "red"))
            return
        if self._find_agent_entry(target_name) is not None:
            raise ValueError(f"'{target_name}' zaten var.")

        # Builtin kaynagin ortuk grubu klona tasinmaz; kopya her zaman config tipi olur.
        entry = {
            "name": target_name,
            "type": "config",
            "enabled": not flags.get("disabled"),
            "description": flags.get("desc") or flags.get("description") or source.get("description") or "",
            "model": flags.get("model") or source.get("model") or "default",
            "tool_mode": "custom",
            "system_prompt": source.get("system_prompt") or "",
            "tools": self._materialize_default_tools(source),
        }
        saved = _studio().upsert_agent_config(entry, create=True)
        self._emit(self._color(f"'{source_name}' -> '{target_name}' olarak kopyalandi.", "green"))
        self._describe_agent(saved)
        self._reload_agents()

    async def _test_agent(self, args: list[str]) -> None:
        positional, _ = self._parse_flags(args)
        if len(positional) != 2:
            self._emit('Kullanim: /agent test <ad> "gorev metni"')
            return

        name, gorev = positional[0], positional[1]
        runner = getattr(self.base_model, "_submodel_func_map", {}).get(name)
        if runner is None:
            self._emit(self._color(f"Calisan ajan bulunamadi: {name}. /agents ile aktif olanlara bak.", "red"))
            return

        job_id = f"terminal-agent-test-{time.time_ns()}"
        acquired, snapshot = await try_acquire_automation(
            "terminal", job_id=job_id, label=f"Ajan testi: {name}", source="terminal"
        )
        if not acquired:
            owner = snapshot.get("owner") or "otomasyon"
            self._emit(self._color(f"Sistem mesgul: {owner} / {snapshot.get('label') or '-'}", "yellow"))
            return

        self._emit(self._color(f"{name} calistiriliyor...", "yellow"))
        started = time.monotonic()
        try:
            with telemetry.run_scope(self.source, f"agent test: {name}", detail="agent_test") as run:
                answer = await runner(gorev)
                run.set_summary(answer)
                if str(answer).startswith("[SISTEM_MESAJI_GIZLI]"):
                    # BaseModel'in runner'i hatalari exception yerine bu on ekli metne cevirir.
                    run.fail(answer)
        except Exception as exc:
            self._emit(self._color(f"Ajan hatasi: {exc}", "red"))
            return
        finally:
            await release_automation("terminal", job_id=job_id)

        self._emit(self._color(f"{name}> ({time.monotonic() - started:.1f}sn)", "green"))
        self._emit(str(answer))

    async def _delete_agent(self, args: list[str]) -> None:
        positional, flags = self._parse_flags(args, bool_flags={"yes"})
        if len(positional) != 1:
            self._emit("Kullanim: /agent delete <ad> [--yes]")
            return

        name = _studio().validate_agent_name(positional[0])
        entry = self._find_agent_entry(name)
        if entry is None:
            self._emit(self._color(f"Ajan bulunamadi: {name}", "red"))
            return
        if entry.get("type") == "builtin":
            self._emit(self._color("Builtin ajan silinemez; /agent <ad> off ile pasife alabilirsin.", "yellow"))
            return

        if not flags.get("yes") and not await self._confirm(f"  {name} agents.yaml icinden silinecek. Onayliyor musun? [e/H]: "):
            self._emit("Silme iptal edildi.")
            return

        deleted = _studio().delete_agent_config(name)
        self._emit(self._color(f"Ajan silindi: {deleted['name']}", "green"))
        self._reload_agents()

    async def _manage_agent_pack(self, args: list[str]) -> None:
        if not args:
            self._emit("Kullanim: /agent pack list|preview <yol>|install <yol> [--overwrite] [--yes]")
            return

        action = args[0].lower()
        rest = args[1:]

        if action == "list":
            packs = _studio().load_agent_packs_config()["installed_packs"]
            self._emit(self._color(f"Kurulu pack'ler ({len(packs)})", "bold"))
            if not packs:
                self._emit("  Kurulu pack yok.")
            for pack in packs:
                self._emit(f"  {pack['name']} v{pack['version']} | {pack['type']}")
                self._emit(
                    f"    Ajan: {', '.join(pack['installed_agents']) or '-'} | "
                    f"Tool: {', '.join(pack['installed_tools']) or '-'}"
                )
            return

        if action == "export":
            self._export_agent_pack(rest)
            return

        if action not in {"preview", "install"}:
            self._emit(
                "Kullanim: /agent pack list|preview <yol>|install <yol> [--overwrite] [--yes]|"
                "export <ad> [--agents a,b] [--tools c,d] [--all] [--out <klasor>]"
            )
            return

        positional, flags = self._parse_flags(rest, bool_flags={"overwrite", "yes", "trust"})
        if len(positional) != 1:
            self._emit(f"Kullanim: /agent pack {action} <yol>" + (" [--overwrite] [--yes] [--trust]" if action == "install" else ""))
            return

        source = positional[0]
        with _studio().fetched_pack(source) as path_value:
            await self._preview_or_install_pack(action, source, path_value, flags)

    async def _preview_or_install_pack(self, action: str, source: str, path_value: str, flags: dict[str, Any]) -> None:
        preview = _studio().preview_agent_pack(path_value)
        self._print_pack_preview(preview)

        if action == "preview":
            return
        if not preview["installable"]:
            self._emit(self._color("Pack kurulabilir durumda degil; kurulum yapilmadi.", "red"))
            return
        trusted = bool(flags.get("trust"))
        self._emit_sandbox_plan(trusted)
        if not flags.get("yes") and not await self._confirm(f"  {preview['name']} kurulacak. Onayliyor musun? [e/H]: "):
            self._emit("Kurulum iptal edildi.")
            return

        label = source if _studio().is_github_pack_spec(source) else None
        result = _studio().install_agent_pack(
            path_value, overwrite=bool(flags.get("overwrite")), source_label=label, trusted=trusted
        )
        pack = result["pack"]
        self._emit(self._color(f"Pack kuruldu: {pack['name']} v{pack['version']}", "green"))
        self._emit(f"  Konum: {pack['installed_path']}")
        self._emit(f"  Ajanlar: {', '.join(pack['installed_agents']) or '-'}")
        self._emit(f"  Tool'lar: {', '.join(pack['installed_tools']) or '-'}")
        self._reload_agents()

    def _export_agent_pack(self, args: list[str]) -> None:
        positional, flags = self._parse_flags(args, bool_flags={"all", "overwrite"})
        if len(positional) != 1:
            self._emit(
                'Kullanim: /agent pack export <pack_adi> [--agents a,b] [--tools c,d] [--all] '
                '[--out <klasor>] [--version 0.1.0] [--desc "..."] [--overwrite]'
            )
            self._emit("   --agents verilmezse ajanlarin kullandigi custom tool'lar otomatik toplanir.")
            return

        result = _studio().export_agent_pack(
            positional[0],
            out_dir=str(flags.get("out") or ""),
            agents=self._split_name_list(flags.get("agents")),
            tools=self._split_name_list(flags.get("tools")),
            include_all=bool(flags.get("all")),
            version=str(flags.get("version") or "0.1.0"),
            description=str(flags.get("desc") or flags.get("description") or ""),
            overwrite=bool(flags.get("overwrite")),
        )

        self._emit(self._color(f"Pack olusturuldu: {result['name']} v{result['version']} ({result['type']})", "green"))
        self._emit(f"  Konum   : {result['path']}")
        self._emit(f"  Ajanlar : {', '.join(result['agents']) or '-'}")
        self._emit(f"  Tool'lar: {', '.join(result['tools']) or '-'}")
        if result["env_vars"]:
            self._emit(f"  Env     : {', '.join(result['env_vars'])} (env.example'a sadece adlar yazildi)")
        self._emit("  Dosyalar:")
        for item in result["files"]:
            self._emit(f"    {item}")
        for warning in result["warnings"]:
            self._emit(self._color(f"  Uyari: {warning}", "yellow"))
        self._emit(f"  Kurmak icin: /agent pack install {result['path']}")

    def _emit_sandbox_plan(self, trusted: bool) -> None:
        """Says before install where the pack's tools will run."""
        if trusted:
            self._emit(self._color("  --trust: pack tool'lari ana process'te, ajanin anahtarlariyla birlikte calisacak.", "yellow"))
            return
        from MarketingApp import sandbox

        try:
            mode = sandbox.resolve_mode()
        except sandbox.SandboxError as exc:
            self._emit(self._color(f"  Sandbox: {exc}", "red"))
            return
        if mode == "container":
            self._emit("  Sandbox: pack tool'lari her cagrida ayri, sinirli bir Docker container'inda calisacak.")
        else:
            self._emit(self._color(
                "  Sandbox: Docker yok; pack tool'lari ayri bir process'te, anahtarsiz ama ayni dosya sistemi "
                "ve agla calisacak.", "yellow"))

    def _print_pack_preview(self, preview: dict[str, Any]) -> None:
        self._emit(self._color(f"Pack: {preview['name']} v{preview['version']} ({preview['type']})", "bold"))
        if preview.get("description"):
            self._emit(f"  Aciklama: {preview['description']}")
        self._emit(f"  Konum: {preview['path']}")
        self._emit(f"  Ajanlar ({len(preview['agents'])}):")
        for agent in preview["agents"]:
            self._emit(f"    {agent['name']} | {agent['type']} | {len(agent.get('tools') or [])} tool")
        self._emit(f"  Tool'lar ({len(preview['tools'])}):")
        for tool in preview["tools"]:
            state = "OK " if tool.get("export_ok") else "HATA"
            gate = " (calismadan once onay ister)" if tool.get("requires_approval") else ""
            self._emit(f"    [{state}] {tool['name']}{gate}")
        for warning in preview["warnings"]:
            self._emit(self._color(f"  Uyari: {warning}", "yellow"))
        for error in preview["errors"]:
            self._emit(self._color(f"  Hata: {error}", "red"))
        self._emit(
            self._color(
                f"  Kurulabilir: {'evet' if preview['installable'] else 'hayir'}",
                "green" if preview["installable"] else "red",
            )
        )

    def _describe_agent(self, entry: dict[str, Any]) -> None:
        self._emit(self._color(f"Ajan: {entry['name']}", "bold"))
        self._emit(f"  Tip       : {entry.get('type') or 'config'}")
        self._emit(f"  Durum     : {'aktif' if entry.get('enabled') else 'pasif'}")
        self._emit(f"  Model     : {entry.get('model') or 'default'}")
        self._emit(f"  Tool modu : {entry.get('tool_mode') or 'default'}")
        if entry.get("description"):
            self._emit(f"  Aciklama  : {entry['description']}")
        tools = entry.get("tools") or []
        if tools:
            self._emit(f"  Tool'lar  : {', '.join(tools)} ({len(tools)} adet)")
        elif entry.get("type") == "builtin" and entry.get("tool_mode") != "custom":
            self._emit(f"  Tool'lar  : (varsayilan set: '{entry['name']}' grubu)")
        else:
            # Config ajanlari icin tool_mode yok sayilir; bos liste = gercekten tool'suz.
            self._emit(self._color("  Tool'lar  : (bos - bu ajan hicbir tool kullanamaz)", "yellow"))
        prompt = str(entry.get("system_prompt") or "").strip()
        if prompt:
            first_line = prompt.splitlines()[0]
            suffix = "..." if len(prompt) > len(first_line) else ""
            self._emit(f"  Prompt    : {first_line[:90]}{suffix} ({len(prompt)} karakter)")
        for error in self._studio_errors(entry["name"]):
            self._emit(self._color(f"  Hata      : {error.get('message')}", "yellow"))

    @staticmethod
    def _find_agent_entry(name: str) -> dict[str, Any] | None:
        agents = _studio().load_agents_config()["agents"]
        return next((item for item in agents if item["name"] == name), None)

    @staticmethod
    def _tool_taxonomy() -> list[dict[str, Any]]:
        """Tool kayit defteri: her tool icin ad, kategori ve ait oldugu gruplar."""
        return _studio().build_tool_registry()["tools"]

    def _materialize_default_tools(self, entry: dict[str, Any]) -> list[str]:
        """Varsayilan moddaki builtin ajanin ortuk tool setini acik listeye cevirir.

        BaseModel builtin ajanlar icin bos listeyi 'ajan adiyla ayni gruba ait tum
        tool'lar' diye yorumluyor. Ekleme/cikarma yapmadan once bunu somutlastirmazsak
        tek tool eklemek ajanin butun varsayilan setini silmis olur.
        """
        tools = list(entry.get("tools") or [])
        if tools or entry.get("type") != "builtin" or entry.get("tool_mode") == "custom":
            return tools
        try:
            registry = self._tool_taxonomy()
        except Exception:
            return tools
        defaults = sorted(
            tool["name"] for tool in registry if entry["name"] in (tool.get("groups") or [])
        )
        if defaults:
            self._emit(
                f"'{entry['name']}' varsayilan setinden {len(defaults)} tool acik listeye alindi."
            )
        return defaults

    @staticmethod
    def _assert_known_taxonomy(registry: list[dict[str, Any]], groups, categories) -> None:
        """Bilinmeyen grup/kategori adlarini sessizce bos sonuca dusurmek yerine hata verir."""
        known_groups = sorted({group for tool in registry for group in tool.get("groups") or []})
        known_categories = sorted({tool["category"] for tool in registry})

        unknown_groups = [item for item in groups if item not in known_groups]
        if unknown_groups:
            raise ValueError(
                f"Bilinmeyen tool grubu: {', '.join(unknown_groups)}. Gecerli gruplar: {', '.join(known_groups)}"
            )
        unknown_categories = [item for item in categories if item not in known_categories]
        if unknown_categories:
            raise ValueError(
                f"Bilinmeyen tool kategorisi: {', '.join(unknown_categories)}. "
                f"Gecerli kategoriler: {', '.join(known_categories)}"
            )

    def _expand_tool_selectors(self, flags: dict[str, Any], *, group_key: str, category_key: str) -> list[str]:
        """--tool-group / --tool-category degerlerini tool adlarina cevirir."""
        wanted_groups = self._split_name_list(flags.get(group_key))
        wanted_categories = self._split_name_list(flags.get(category_key))
        if not wanted_groups and not wanted_categories:
            return []

        registry = self._tool_taxonomy()
        self._assert_known_taxonomy(registry, wanted_groups, wanted_categories)

        selected = [
            tool["name"]
            for tool in registry
            if any(group in wanted_groups for group in tool.get("groups") or [])
            or tool["category"] in wanted_categories
        ]
        return sorted(dict.fromkeys(selected))

    def _warn_unknown_tools(self, tools: list[str]) -> None:
        if not tools:
            return
        try:
            known = set(_studio().load_available_tools(include_custom=True)["tools"].keys())
        except Exception:
            return
        unknown = [tool for tool in tools if tool not in known]
        if unknown:
            self._emit(
                self._color(
                    f"Uyari: kayitli olmayan tool'lar yok sayilacak: {', '.join(unknown)}",
                    "yellow",
                )
            )

    async def _confirm(self, prompt: str) -> bool:
        try:
            answer = await self._read_line(prompt)
        except (EOFError, KeyboardInterrupt):
            return False
        return (answer or "").strip().lower() in _AFFIRMATIVE_ANSWERS

    @staticmethod
    def _split_name_list(value: Any) -> list[str]:
        if not value:
            return []
        raw = str(value).replace(",", " ").split()
        return list(dict.fromkeys(item.strip() for item in raw if item.strip()))

    @staticmethod
    def _parse_flags(args: list[str], *, bool_flags: set[str] | frozenset[str] = frozenset()) -> tuple[list[str], dict[str, Any]]:
        positional: list[str] = []
        flags: dict[str, Any] = {}
        index = 0
        while index < len(args):
            token = args[index]
            if token.startswith("--"):
                key = token[2:].strip().lower().replace("-", "_")
                if not key:
                    raise ValueError(f"Gecersiz bayrak: {token}")
                if key in bool_flags:
                    flags[key] = True
                    index += 1
                    continue
                if index + 1 >= len(args):
                    raise ValueError(f"--{key} icin deger verilmedi.")
                flags[key] = args[index + 1]
                index += 2
                continue
            positional.append(token)
            index += 1
        return positional, flags

    def _all_tools(self) -> list[dict]:
        hierarchy = self.base_model.get_hierarchy()
        tools = list(hierarchy.get("tools", []))
        for agent in hierarchy.get("submodels", []):
            tools.extend(agent.get("tools", []))
        unique = {}
        for tool in tools:
            unique.setdefault(tool.get("name"), tool)
        return [tool for name, tool in sorted(unique.items()) if name]

    def _print_tools(self, args: list[str]) -> None:
        try:
            positional, flags = self._parse_flags(args, bool_flags={"list_groups"})
        except ValueError as exc:
            self._emit(self._color(str(exc), "red"))
            return

        if flags.get("list_groups"):
            self._print_tool_taxonomy()
            return

        needle = " ".join(positional).strip().lower()
        wanted_groups = set(self._split_name_list(flags.get("group")))
        wanted_categories = set(self._split_name_list(flags.get("category")))
        wanted_risks = set(self._split_name_list(flags.get("risk")))
        if wanted_risks - {"low", "medium", "high"}:
            self._emit(self._color("--risk sadece low, medium veya high olabilir.", "red"))
            return

        taxonomy: dict[str, dict[str, Any]] = {}
        if wanted_groups or wanted_categories or wanted_risks:
            try:
                registry = self._tool_taxonomy()
                self._assert_known_taxonomy(registry, wanted_groups, wanted_categories)
                taxonomy = {tool["name"]: tool for tool in registry}
            except ValueError as exc:
                self._emit(self._color(str(exc), "red"))
                return
            except Exception as exc:
                self._emit(self._color(f"Tool taksonomisi okunamadi: {exc}", "red"))
                return

        tools = []
        for tool in self._all_tools():
            name = tool.get("name", "")
            if needle and needle not in name.lower() and needle not in tool.get("desc", "").lower():
                continue
            if taxonomy:
                meta = taxonomy.get(name)
                if not meta:
                    continue
                if wanted_groups and not (wanted_groups & set(meta.get("groups") or [])):
                    continue
                if wanted_categories and meta.get("category") not in wanted_categories:
                    continue
                if wanted_risks and meta.get("risk") not in wanted_risks:
                    continue
            tools.append(tool)

        self._emit(self._color(f"Tool'lar ({len(tools)})", "bold"))
        if not tools:
            self._emit("  Eslesen tool yok.")
            return
        for tool in tools:
            state = "ON " if tool.get("active", True) else "OFF"
            meta = taxonomy.get(tool.get("name", ""))
            suffix = f" | {meta['category']} | risk={meta['risk']}" if meta else ""
            self._emit(f"  [{state}] {tool.get('name')}{suffix}")

    def _print_tool_taxonomy(self) -> None:
        try:
            registry = self._tool_taxonomy()
        except Exception as exc:
            self._emit(self._color(f"Tool taksonomisi okunamadi: {exc}", "red"))
            return

        group_counts: dict[str, int] = {}
        category_counts: dict[str, int] = {}
        for tool in registry:
            for group in tool.get("groups") or []:
                group_counts[group] = group_counts.get(group, 0) + 1
            category_counts[tool["category"]] = category_counts.get(tool["category"], 0) + 1

        self._emit(self._color(f"Tool gruplari ({len(group_counts)})", "bold"))
        for name, count in sorted(group_counts.items(), key=lambda item: (-item[1], item[0])):
            self._emit(f"  {name:24} {count} tool")
        self._emit(self._color(f"Tool kategorileri ({len(category_counts)})", "bold"))
        for name, count in sorted(category_counts.items(), key=lambda item: (-item[1], item[0])):
            self._emit(f"  {name:24} {count} tool")
        risk_counts: dict[str, int] = {}
        for tool in registry:
            risk_counts[tool["risk"]] = risk_counts.get(tool["risk"], 0) + 1
        self._emit(self._color("Risk dagilimi", "bold"))
        for level in ("high", "medium", "low"):
            if level in risk_counts:
                self._emit(f"  {level:24} {risk_counts[level]} tool")
        self._emit(f"Toplam {len(registry)} tool.")
        self._emit("Kullanim: /tools --group <ad> | --category <ad> | --risk high")

    async def _manage_tool(self, args: list[str]) -> None:
        if not args:
            self._emit("Kullanim: /tool <ad> on|off|toggle  ya da  /tool create|edit|show|delete ...")
            return

        # Eski kullanim once: /tool <ad> on|off|toggle
        if len(args) == 2 and args[1].lower() in _AGENT_SWITCH_WORDS:
            self._toggle_tool(args[0], args[1].lower())
            return

        action = args[0].lower()
        rest = args[1:]
        studio = _studio()
        try:
            if action == "create":
                self._upsert_custom_tool(rest, create=True)
            elif action == "edit":
                self._upsert_custom_tool(rest, create=False)
            elif action == "show":
                self._show_custom_tool(rest)
            elif action == "list":
                self._print_custom_tools()
            elif action == "delete":
                await self._delete_custom_tool(rest)
            else:
                self._emit("Kullanim: /tool <ad> on|off|toggle  ya da  /tool create|edit|show|list|delete ...")
        except studio.AgentStudioError as exc:
            self._emit(self._color(f"Agent Studio hatasi: {exc}", "red"))
        except ValueError as exc:
            self._emit(self._color(str(exc), "red"))
        except Exception as exc:
            self._emit(self._color(f"Tool islemi basarisiz: {exc}", "red"))

    def _toggle_tool(self, name: str, action: str) -> None:
        current = getattr(self.base_model, "active_tools", {}).get(name)
        if current is None:
            self._emit(self._color(f"Tool bulunamadi: {name}", "red"))
            return
        try:
            target = self._resolve_switch(action, current)
            active = self.base_model.set_tool_active(name, target)
            self._emit(self._color(f"{name}: {'aktif' if active else 'pasif'}", "green"))
        except ValueError as exc:
            self._emit(str(exc))

    @staticmethod
    def _find_custom_tool(name: str) -> dict[str, Any] | None:
        entries = _studio().load_custom_tools_config()["custom_tools"]
        return next((item for item in entries if item["name"] == name), None)

    def _print_custom_tools(self) -> None:
        entries = _studio().load_custom_tools_config()["custom_tools"]
        self._emit(self._color(f"Custom tool'lar ({len(entries)})", "bold"))
        if not entries:
            self._emit("  Kayitli custom tool yok.")
            return
        for entry in entries:
            state = "ON " if entry.get("enabled") else "OFF"
            self._emit(f"  [{state}] {entry['name']} | {entry.get('file') or '-'}")
            if entry.get("description"):
                self._emit(f"        {entry['description']}")

    def _parse_env_flag(self, value: Any) -> dict[str, str]:
        """--env AD,BASKA_AD=deger -> {AD: '', BASKA_AD: 'deger'}.

        Degeri verilen degiskenler .env.model dosyasina yazilir; degersiz olanlar
        sadece tool'un gereksinim listesine kaydedilir.
        """
        result: dict[str, str] = {}
        for item in self._split_name_list(value):
            key, _, raw_value = item.partition("=")
            key = key.strip().upper()
            if not key:
                continue
            result[key] = raw_value.strip()
        return result

    def _read_tool_code(self, flags: dict[str, Any]) -> str | None:
        """--file veya --code bayragindan tool kaynak kodunu okur."""
        if "code" in flags:
            return str(flags["code"])
        if "file" in flags:
            path = Path(str(flags["file"])).expanduser()
            if not path.is_file():
                raise ValueError(f"Dosya bulunamadi: {path}")
            if path.suffix != ".py":
                raise ValueError("Tool dosyasi .py uzantili olmali.")
            return path.read_text(encoding="utf-8")
        return None

    def _upsert_custom_tool(self, args: list[str], *, create: bool) -> None:
        positional, flags = self._parse_flags(
            args, bool_flags={"disabled", "enable", "disable", "all", "overwrite"}
        )
        label = "create" if create else "edit"

        # Toplu kayit: tek dosyadaki birden fazla fonksiyonu ayri tool olarak ekle.
        if create and ("names" in flags or flags.get("all")):
            if positional:
                raise ValueError("Toplu kayitta tool adi verilmez; adlar --names ile ya da --all ile belirlenir.")
            self._create_tools_from_file(flags)
            return

        if len(positional) != 1:
            self._emit(
                f'Kullanim: /tool {label} <ad> [--file <yol.py>] [--code "..."] [--desc "..."] '
                '[--params "..."] [--env A,B]' + (" [--disabled]" if create else " [--enable|--disable]")
            )
            if create:
                self._emit('   Toplu: /tool create --file <yol.py> --names a,b   ya da   --all')
            return

        name = _studio().validate_tool_name(positional[0])
        existing = self._find_custom_tool(name)
        if create and existing:
            raise ValueError(f"'{name}' zaten kayitli. Guncellemek icin /tool edit kullan.")
        if not create and not existing:
            raise ValueError(f"'{name}' bulunamadi. Olusturmak icin /tool create kullan.")

        code = self._read_tool_code(flags)
        if create and code is None:
            raise ValueError("Yeni tool icin --file <yol.py> ya da --code \"...\" vermelisin.")
        if code is None:
            code = _studio().read_custom_tool_code(existing)

        # Kaydetmeden once derle: bozuk kod runtime'a girmesin.
        try:
            compile(code, f"<custom_tool:{name}>", "exec")
        except SyntaxError as exc:
            raise ValueError(f"Kod derlenemedi (satir {exc.lineno}): {exc.msg}") from exc
        if not re.search(rf"(?m)^\s*(?:async\s+)?def\s+{re.escape(name)}\s*\(", code):
            raise ValueError(f"Kod icinde '{name}' adinda bir fonksiyon tanimi bulunamadi.")

        base = existing or {}
        description = flags.get("desc") or flags.get("description") or base.get("description") or ""
        params_note = flags.get("params") or flags.get("params_note") or base.get("params_note") or ""
        env_vars = self._parse_env_flag(flags.get("env")) or {
            name: "" for name in base.get("env_vars") or []
        }
        enabled = bool(base.get("enabled", True))
        if flags.get("disabled") or flags.get("disable"):
            enabled = False
        if flags.get("enable"):
            enabled = True

        saved = _studio().upsert_custom_tool(
            name,
            description,
            code,
            enabled=enabled,
            params_note=params_note,
            env_vars=env_vars,
        )
        self._emit(self._color(f"Custom tool {'olusturuldu' if create else 'guncellendi'}: {name}", "green"))
        self._emit(f"  Dosya: {saved['path']}")

        # Import edilebiliyor mu? Kayit sonrasi gercek yukleme denemesi.
        func, error = _studio().load_custom_tool_callable(saved, include_disabled=True)
        if error or func is None:
            self._emit(self._color(f"  Uyari: tool yuklenemedi -> {error or 'bilinmeyen hata'}", "yellow"))
        else:
            self._emit(self._color("  Yukleme testi: basarili", "green"))
        if saved.get("env_vars"):
            self._emit(f"  Env: {', '.join(saved['env_vars'])} (.env.model dosyasina eklendi)")
        if not description:
            self._emit(self._color("  Uyari: aciklama bos; model bu tool'u ne zaman cagiracagini bilemez.", "yellow"))

        self._reload_agents()

    @staticmethod
    def _module_functions(code: str) -> list[tuple[str, str]]:
        """Modulun ust seviye fonksiyonlarini (ad, docstring ilk satiri) olarak dondurur."""
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            raise ValueError(f"Kod derlenemedi (satir {exc.lineno}): {exc.msg}") from exc
        functions = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = (ast.get_docstring(node) or "").strip()
                functions.append((node.name, doc.splitlines()[0] if doc else ""))
        return functions

    def _create_tools_from_file(self, flags: dict[str, Any]) -> None:
        """Tek dosyadan birden fazla tool kaydeder.

        Dosya kaynak adiyla bir kez kopyalanir ve her tool kaydi ayni dosyayi
        gosterir; runtime tool'u modulden adiyla cekiyor (getattr), bu yuzden bir
        modul birden fazla tool tasiyabilir. Agent pack kurulumu da ayni sekilde
        calisir.
        """
        studio = _studio()
        if "file" not in flags:
            raise ValueError("Toplu kayit icin --file <yol.py> gerekli.")

        source = Path(str(flags["file"])).expanduser()
        if not source.is_file():
            raise ValueError(f"Dosya bulunamadi: {source}")
        if source.suffix != ".py":
            raise ValueError("Tool dosyasi .py uzantili olmali.")

        code = source.read_text(encoding="utf-8")
        functions = self._module_functions(code)
        available = {name for name, _ in functions}
        docs = dict(functions)

        if flags.get("all"):
            names = [name for name, _ in functions if not name.startswith("_")]
            if not names:
                raise ValueError("Dosyada '_' ile baslamayan ust seviye fonksiyon yok.")
        else:
            names = [studio.validate_tool_name(item) for item in self._split_name_list(flags["names"])]
            if not names:
                raise ValueError("--names bos.")
            missing = [name for name in names if name not in available]
            if missing:
                raise ValueError(
                    f"Dosyada bulunmayan fonksiyon: {', '.join(missing)}. "
                    f"Mevcut: {', '.join(sorted(available)) or '(yok)'}"
                )

        config = studio.load_custom_tools_config()
        entries = list(config["custom_tools"])
        by_name = {entry["name"]: entry for entry in entries}
        builtin_names = {
            item["name"] for item in studio.build_tool_registry()["tools"] if item.get("source") != "custom"
        }

        clashes = [name for name in names if name in by_name or name in builtin_names]
        if clashes and not flags.get("overwrite"):
            raise ValueError(f"Zaten kayitli tool: {', '.join(clashes)}. Uzerine yazmak icin --overwrite kullan.")

        target = Path(studio.CUSTOM_TOOLS_DIR) / source.name
        if target.exists() and target.resolve() != source.resolve() and not flags.get("overwrite"):
            existing_users = [entry["name"] for entry in entries if entry.get("file") == source.name]
            if existing_users:
                raise ValueError(
                    f"{source.name} zaten kullanimda ({', '.join(existing_users)}). --overwrite ile guncelle."
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

        env_vars = self._parse_env_flag(flags.get("env"))
        env_names = studio.update_model_env_vars(env_vars) or list(env_vars)
        shared_desc = flags.get("desc") or flags.get("description") or ""
        enabled = not flags.get("disabled")

        for name in names:
            entry = {
                "name": name,
                "enabled": enabled,
                "description": docs.get(name) or shared_desc,
                "file": target.name,
                "params_note": flags.get("params") or flags.get("params_note") or "",
                "env_vars": [item.upper() for item in env_names],
            }
            entries = [item for item in entries if item["name"] != name]
            entries.append(entry)

        studio.save_custom_tools_config(entries)
        self._emit(self._color(f"{len(names)} tool kaydedildi: {', '.join(names)}", "green"))
        self._emit(f"  Dosya: {target} (tek kopya, {len(names)} kayit paylasiyor)")

        by_name = {entry["name"]: entry for entry in studio.load_custom_tools_config()["custom_tools"]}
        for name in names:
            func, error = studio.load_custom_tool_callable(by_name[name], include_disabled=True)
            if error or func is None:
                self._emit(self._color(f"  [HATA] {name}: {error or 'yuklenemedi'}", "red"))
            else:
                self._emit(self._color(f"  [OK  ] {name}: {by_name[name]['description'] or '(aciklama yok)'}", "green"))

        skipped = sorted(available - set(names))
        if skipped:
            self._emit(f"  Kaydedilmeyen fonksiyonlar: {', '.join(skipped)}")
        self._reload_agents()

    def _show_custom_tool(self, args: list[str]) -> None:
        positional, flags = self._parse_flags(args, bool_flags={"code"})
        if len(positional) != 1:
            self._emit("Kullanim: /tool show <ad> [--code]")
            return
        name = _studio().validate_tool_name(positional[0])
        entry = self._find_custom_tool(name)
        if entry is None:
            self._emit(self._color(f"Custom tool bulunamadi: {name}", "red"))
            return

        self._emit(self._color(f"Custom tool: {entry['name']}", "bold"))
        self._emit(f"  Durum     : {'aktif' if entry.get('enabled') else 'pasif'}")
        self._emit(f"  Dosya     : {entry.get('file') or '-'}")
        self._emit(f"  Aciklama  : {entry.get('description') or '-'}")
        if entry.get("params_note"):
            self._emit(f"  Parametre : {entry['params_note']}")
        if entry.get("env_vars"):
            self._emit(f"  Env       : {', '.join(entry['env_vars'])}")
        self._emit(f"  Calisma   : {'sandbox (pack tool)' if entry.get('sandbox') else 'ana process'}")
        if entry.get("requires_approval"):
            self._emit("  Onay      : her calismadan once (pack beyan etti)")

        func, error = _studio().load_custom_tool_callable(entry, include_disabled=True)
        if error or func is None:
            self._emit(self._color(f"  Yukleme   : HATA -> {error or 'bilinmeyen'}", "red"))
        else:
            self._emit(self._color("  Yukleme   : basarili", "green"))

        if flags.get("code"):
            code = _studio().read_custom_tool_code(entry)
            self._emit(self._color("--- kod ---", "bold"))
            for line_no, line in enumerate(code.splitlines(), start=1):
                self._emit(f"  {line_no:3} | {line}")

    async def _delete_custom_tool(self, args: list[str]) -> None:
        positional, flags = self._parse_flags(args, bool_flags={"yes", "keep_file"})
        if len(positional) != 1:
            self._emit("Kullanim: /tool delete <ad> [--yes] [--keep-file]")
            return
        name = _studio().validate_tool_name(positional[0])
        entry = self._find_custom_tool(name)
        if entry is None:
            self._emit(self._color(f"Custom tool bulunamadi: {name}", "red"))
            return

        users = [
            agent["name"]
            for agent in _studio().load_agents_config()["agents"]
            if name in (agent.get("tools") or [])
        ]
        if users:
            self._emit(self._color(f"Uyari: bu tool su ajanlarda kayitli: {', '.join(users)}", "yellow"))

        if not flags.get("yes") and not await self._confirm(f"  {name} silinecek. Onayliyor musun? [e/H]: "):
            self._emit("Silme iptal edildi.")
            return

        studio = _studio()
        entries = [item for item in studio.load_custom_tools_config()["custom_tools"] if item["name"] != name]
        studio.save_custom_tools_config(entries)

        if not flags.get("keep_file"):
            path = Path(studio.CUSTOM_TOOLS_DIR) / (entry.get("file") or f"{name}.py")
            try:
                path.unlink()
                self._emit(f"  Dosya silindi: {path}")
            except FileNotFoundError:
                pass
            except Exception as exc:
                self._emit(self._color(f"  Dosya silinemedi: {exc}", "yellow"))

        self._emit(self._color(f"Custom tool silindi: {name}", "green"))
        self._reload_agents()

    def _print_telemetry_status(self) -> None:
        health = telemetry.health()
        if not health["ok"]:
            self._emit(self._color(f"  Kayit      : HATA - {health['last_error']}", "red"))
            return
        self._emit(
            f"  Kayit      : {health.get('events', 0)} olay, {health.get('runs', 0)} kosu "
            f"({health['retention_days']} gun saklanir)" if health["retention_days"] > 0 else
            f"  Kayit      : {health.get('events', 0)} olay, {health.get('runs', 0)} kosu (sinirsiz saklanir)"
        )
        try:
            today = telemetry.usage_summary(telemetry.parse_since("today"), group_by="agent")["totals"]
        except Exception:
            return
        if today["calls"]:
            self._emit(f"  Bugun      : {today['calls']} LLM cagrisi, {self._fmt_tokens(today['total'])} token")

    @staticmethod
    def _fmt_tokens(value: int | float) -> str:
        value = int(value or 0)
        if value >= 1_000_000:
            return f"{value / 1_000_000:.1f}M"
        if value >= 10_000:
            return f"{value / 1_000:.0f}k"
        if value >= 1_000:
            return f"{value / 1_000:.1f}k"
        return str(value)

    @staticmethod
    def _fmt_duration_ms(value: int | None) -> str:
        if value is None:
            return "-"
        seconds = value / 1000
        if seconds >= 60:
            return f"{int(seconds // 60)}dk{int(seconds % 60):02d}sn"
        return f"{seconds:.1f}sn"

    def _print_logs(self, args: list[str]) -> None:
        """Kalici olay logu. --memory eski davranisi (sadece bu process'in son 100 satiri) verir."""
        positional, flags = self._parse_flags(args, bool_flags={"memory"})
        try:
            count = min(500, max(1, int(positional[0]))) if positional else 15
        except ValueError:
            self._emit("Kullanim: /logs [adet] [--type T] [--since 24h] [--grep metin] [--run <id>] [--memory]")
            return

        if not flags.get("memory"):
            try:
                since = telemetry.parse_since(flags.get("since"))
                run_id = None
                if flags.get("run"):
                    matches = telemetry.find_runs(str(flags["run"]))
                    if len(matches) != 1:
                        self._emit(self._color("--run icin tek bir kosu eslesmeli; /runs ile id'ye bak.", "red"))
                        return
                    run_id = matches[0]["run_id"]
                events = telemetry.recent_events(
                    count, type_=flags.get("type"), since=since, grep=flags.get("grep"), run_id=run_id
                )
            except ValueError as exc:
                self._emit(self._color(str(exc), "red"))
                return
            except Exception as exc:
                self._emit(self._color(f"Kalici log okunamadi ({exc}); bellekteki loglar gosteriliyor.", "yellow"))
            else:
                self._emit(self._color(f"Loglar ({len(events)})", "bold"))
                today = datetime.now().strftime("%Y-%m-%d")
                for item in events:
                    stamp = telemetry.format_ts(item["ts"])
                    stamp = stamp[11:] if stamp.startswith(today) else stamp
                    self._emit(f"  {stamp} [{item['type']}] {item['message']}")
                if not events:
                    self._emit("  Eslesen kayit yok.")
                return

        logs = list(getattr(self.base_model, "logs", []))[-count:]
        self._emit(self._color(f"Bellekteki loglar ({len(logs)})", "bold"))
        for item in logs:
            self._emit(f"  {item.get('time', '--:--:--')} [{item.get('type', 'log')}] {item.get('message', '')}")

    def _format_run_line(self, run: dict[str, Any]) -> str:
        started = telemetry.format_ts(run["started_at"])[5:]  # MM-DD HH:MM:SS
        tokens = f"{self._fmt_tokens(run['total_tokens']):>6} tok" if run["total_tokens"] else "         -"
        return (
            f"  {run['run_id']}  {started}  {run['source']:<10} {run['status']:<11} "
            f"{self._fmt_duration_ms(run['duration_ms']):>8} {run['llm_calls']:>3} cagri {tokens}  {run['label'] or '-'}"
        )

    def _print_runs(self, args: list[str], *, job_id: str | None = None, title: str = "Kosular") -> None:
        positional, flags = self._parse_flags(args)
        try:
            count = min(200, max(1, int(positional[0]))) if positional else 15
        except ValueError:
            self._emit("Kullanim: /runs [adet] [--source S] [--status S] [--job ID] [--since 24h]")
            return
        try:
            runs = telemetry.recent_runs(
                count,
                source=flags.get("source"),
                job_id=job_id or flags.get("job"),
                status=flags.get("status"),
                since=telemetry.parse_since(flags.get("since")),
            )
        except ValueError as exc:
            self._emit(self._color(str(exc), "red"))
            return
        except Exception as exc:
            self._emit(self._color(f"Kosu gecmisi okunamadi: {exc}", "red"))
            return

        self._emit(self._color(f"{title} ({len(runs)})", "bold"))
        if not runs:
            self._emit("  Eslesen kosu yok.")
            return
        for run in runs:
            self._emit(self._format_run_line(run))
            if run["error"]:
                self._emit(self._color(f"      ! {run['error']}", "red" if run["status"] == "error" else "yellow"))
            elif job_id and run["summary"]:
                self._emit(f"      > {run['summary'][:200]}")

    def _show_run(self, args: list[str]) -> None:
        if len(args) != 1:
            self._emit("Kullanim: /run <id>   (id'nin bir kismi yeterli; /runs ile bak)")
            return
        matches = telemetry.find_runs(args[0])
        if not matches:
            self._emit(self._color(f"Kosu bulunamadi: {args[0]}", "red"))
            return
        if len(matches) > 1:
            self._emit(self._color("Birden fazla kosu eslesti, daha uzun bir id ver:", "yellow"))
            for run in matches:
                self._emit(self._format_run_line(run))
            return

        run = matches[0]
        self._emit(self._color(f"Kosu {run['run_id']}", "bold"))
        self._emit(f"  Kaynak    : {run['source']}" + (f" ({run['detail']})" if run["detail"] else ""))
        self._emit(f"  Etiket    : {run['label'] or '-'}")
        if run["job_id"]:
            self._emit(f"  Job       : {run['job_id']}")
        self._emit(f"  Durum     : {run['status']}")
        self._emit(f"  Baslangic : {telemetry.format_ts(run['started_at'])}")
        self._emit(f"  Sure      : {self._fmt_duration_ms(run['duration_ms'])}")
        self._emit(
            f"  LLM       : {run['llm_calls']} cagri, {self._fmt_tokens(run['total_tokens'])} token "
            f"(girdi {self._fmt_tokens(run['prompt_tokens'])} / cikti {self._fmt_tokens(run['completion_tokens'])})"
        )
        if run["error"]:
            self._emit(self._color(f"  Hata      : {run['error']}", "red"))
        if run["summary"]:
            self._emit(f"  Ozet      : {run['summary']}")

        usage = telemetry.run_usage(run["run_id"])
        if usage:
            self._emit("  Ajan/model kirilimi:")
            for row in usage:
                self._emit(
                    f"    {row['agent']:<24} {row['model']:<32} {row['calls']:>3} cagri  "
                    f"{self._fmt_tokens(row['total_tokens']):>6} tok"
                )
        events = telemetry.recent_events(40, run_id=run["run_id"])
        if events:
            self._emit(f"  Olaylar ({len(events)}):")
            for item in events:
                self._emit(f"    {telemetry.format_ts(item['ts'], with_date=False)} [{item['type']}] {item['message']}")

    def _print_usage(self, args: list[str]) -> None:
        _, flags = self._parse_flags(args)
        group_by = str(flags.get("by") or "agent").lower()
        since_text = str(flags.get("since") or "24h")
        try:
            report = telemetry.usage_summary(telemetry.parse_since(since_text), group_by=group_by)
        except ValueError as exc:
            self._emit(self._color(str(exc), "red"))
            self._emit("Kullanim: /usage [--since 24h|7d|today|2026-09-01] [--by agent|model|source|day|run]")
            return
        except Exception as exc:
            self._emit(self._color(f"Kullanim verisi okunamadi: {exc}", "red"))
            return

        rows, totals = report["rows"], report["totals"]
        self._emit(self._color(f"Token kullanimi (son {since_text}, {group_by} bazinda)", "bold"))
        if not rows:
            self._emit("  Bu aralikta kayitli LLM cagrisi yok.")
            return

        show_cost = report["pricing_loaded"]
        header = f"  {group_by:<26}{'cagri':>6}{'girdi':>9}{'cikti':>9}{'toplam':>9}" + (f"{'~USD':>9}" if show_cost else "")
        self._emit(header)
        for row in rows[:40]:
            cost = f"{row['cost']:>9.4f}" if show_cost and row["priced"] else (f"{'-':>9}" if show_cost else "")
            self._emit(
                f"  {str(row['key'])[:25]:<26}{row['calls']:>6}{self._fmt_tokens(row['prompt']):>9}"
                f"{self._fmt_tokens(row['completion']):>9}{self._fmt_tokens(row['total']):>9}{cost}"
            )
        if len(rows) > 40:
            self._emit(f"  ... {len(rows) - 40} satir daha")
        total_cost = f"{totals['cost']:>9.4f}" if show_cost else ""
        self._emit(
            f"  {'TOPLAM':<26}{totals['calls']:>6}{self._fmt_tokens(totals['prompt']):>9}"
            f"{self._fmt_tokens(totals['completion']):>9}{self._fmt_tokens(totals['total']):>9}{total_cost}"
        )

        if totals["unknown_calls"]:
            self._emit(f"  Not: {totals['unknown_calls']} cagrida saglayici kullanim bilgisi dondurmedi (token sayilamadi).")
        if group_by == "agent" and any(str(row["key"]) in {"arastirma_agent", "sistem_agent", "vlm_agent"} for row in rows):
            self._emit("  Not: Gemini Live ajanlari (arastirma/sistem/vlm) oturum basina tek, yaklasik bir kayit uretir.")
        if not show_cost:
            self._emit("  Maliyet tahmini icin config/pricing.yaml olustur:  models: {model_adi: {input: 0.30, output: 2.50}}  (USD / 1M token)")
        elif report["unpriced_models"]:
            self._emit(f"  Not: fiyati tanimli olmayan modeller maliyete dahil degil: {', '.join(report['unpriced_models'])}")

    def _print_history(self) -> None:
        if not self.history:
            self._emit("Sohbet gecmisi bos.")
            return
        self._emit(self._color(f"Sohbet gecmisi ({len(self.history)})", "bold"))
        for item in self.history:
            label = "Sen" if item.get("role") == "user" else "Ethgent"
            self._emit(f"  {item.get('time', '--:--')} {label}: {item.get('content', '')}")

    def _clear_history(self) -> None:
        self.history = []
        self._save_history()
        self._emit(self._color("Terminal sohbet gecmisi temizlendi.", "green"))

    def _reload_agents(self) -> None:
        hierarchy = self.base_model.reload_agent_studio()
        self._emit(
            self._color(
                f"Runtime yenilendi: {len(hierarchy.get('submodels', []))} ajan, "
                f"{len(hierarchy.get('tools', []))} base tool.",
                "green",
            )
        )

    _MODEL_SENTINELS = {"default", "base_default", "browser_default"}

    def _pinned_agents(self) -> list[tuple[str, str]]:
        """agents.yaml'da somut (sentinel olmayan) model adi tasiyan ajanlar.

        Bunlar provider/model env degiskenlerini degistirmenin ETKILEMEDIGI
        ajanlardir; cunku BaseModel sadece "default"/"base_default"/"browser_default"
        degerlerini env'den yeniden cozer (bkz. BaseModel.py:171-173).
        """
        agents = _studio().load_agents_config()["agents"]
        return [
            (agent["name"], agent.get("model"))
            for agent in agents
            if str(agent.get("model") or "").strip() not in self._MODEL_SENTINELS
        ]

    def _manage_provider(self, args: list[str]) -> None:
        if not args or args[0].lower() == "show":
            self._print_provider_status()
            return
        sub = args[0].lower()
        if sub == "set":
            self._set_provider(args[1:])
            return
        self._emit(self._color(f"Bilinmeyen /provider komutu: {sub}. Kullanim: /provider [show] | /provider set <ad> [bayrak]", "red"))

    def _print_provider_status(self) -> None:
        rc = _runtime_config()
        self._emit(self._color("Provider", "bold"))
        self._emit(f"  Provider        : {rc.get_provider_display_name()} ({rc.get_model_provider()})")
        self._emit(f"  Base URL        : {rc.get_openai_compat_base_url()}")
        self._emit(f"  BASE_MODEL_NAME     : {rc.get_base_model_name()}")
        self._emit(f"  SUBMODEL_MODEL_NAME : {rc.get_submodel_model_name()}")
        self._emit(f"  BROWSER_AGENT_MODEL : {rc.get_browser_model_name()}")
        self._emit(f"  API anahtari    : {'tanimli' if rc.get_model_api_key() else self._color('TANIMSIZ', 'yellow')}")

        pinned = self._pinned_agents()
        if pinned:
            self._emit(self._color(f"\n{len(pinned)} ajan somut model adina pinlenmis (provider/model env degisince TAKIP ETMEZ):", "yellow"))
            for name, model in pinned:
                self._emit(f"  - {name}: {model}")
            self._emit("  /provider set ... --reset-pins ile default'a sifirlanabilir.")
        else:
            self._emit(self._color("\nTum ajanlar default sentinel'ini kullaniyor; provider degisince hepsi takip eder.", "green"))

    def _set_provider(self, args: list[str]) -> None:
        try:
            positional, flags = self._parse_flags(args, bool_flags={"dry_run", "reset_pins"})
        except ValueError as exc:
            self._emit(self._color(str(exc), "red"))
            return
        if len(positional) != 1:
            self._emit('Kullanim: /provider set <ad> [--base-model M] [--submodel-model M] [--browser-model M] [--base-url URL] [--api-key K] [--reset-pins] [--dry-run]')
            return

        provider_name = positional[0].strip().lower()
        if not provider_name:
            self._emit(self._color("Provider adi bos olamaz.", "red"))
            return

        env_vars: dict[str, str] = {"MODEL_PROVIDER": provider_name}
        if "base_model" in flags:
            env_vars["BASE_MODEL_NAME"] = flags["base_model"]
        if "submodel_model" in flags:
            env_vars["SUBMODEL_MODEL_NAME"] = flags["submodel_model"]
        if "browser_model" in flags:
            env_vars["BROWSER_AGENT_MODEL"] = flags["browser_model"]
        if "base_url" in flags:
            env_vars["OPENAI_COMPAT_BASE_URL"] = flags["base_url"]
        if "api_key" in flags:
            env_vars[_runtime_config().provider_api_key_env_name(provider_name)] = flags["api_key"]

        reset_pins = bool(flags.get("reset_pins"))
        pinned = self._pinned_agents() if reset_pins else []

        if flags.get("dry_run"):
            self._emit(self._color("[dry-run] Kaydedilmedi. Yazilacak env degiskenleri:", "yellow"))
            for key, value in env_vars.items():
                shown = "***" if "KEY" in key else value
                self._emit(f"  {key}={shown}")
            if reset_pins:
                if pinned:
                    self._emit(self._color(f"{len(pinned)} ajanin model pini 'default'a sifirlanacak:", "yellow"))
                    for name, model in pinned:
                        self._emit(f"  - {name}: {model} -> default")
                else:
                    self._emit("Sifirlanacak pinlenmis ajan yok.")
            return

        updated = _studio().update_model_env_vars(env_vars)
        self._emit(self._color(f"Yazildi ({len(updated)} degisken): {', '.join(updated)}", "green"))

        if reset_pins and pinned:
            config = _studio().load_agents_config()
            for agent in config["agents"]:
                if str(agent.get("model") or "").strip() in self._MODEL_SENTINELS:
                    continue
                agent["model"] = "browser_default" if agent.get("name") == "browser_agent" else "default"
            _studio().save_agents_config(config["agents"], config.get("global_disabled_tools"))
            self._emit(self._color(f"{len(pinned)} ajanin model pini 'default'a sifirlandi.", "green"))

        self._reload_agents()
        self._emit(
            self._color(
                "NOT: model adi degisikligi hemen etkili oldu. Ancak base_url/API anahtari "
                "degisikligi (gercek provider degisimi) mevcut oturumdaki HTTP client'lari "
                "yeniden olusturmaz -- tam etkisi icin uygulamayi yeniden baslat.",
                "yellow",
            )
        )

    def _manage_memory(self, args: list[str]) -> None:
        bellek = _bellek()
        if not args:
            self._emit(bellek.bellek_oku())
            return
        sub = args[0].lower()
        if sub == "show" and len(args) == 1:
            self._emit(bellek.bellek_oku())
            return
        if sub == "search":
            if len(args) != 2:
                self._emit('Kullanim: /memory search <metin>')
                return
            self._print_memory_search(args[1])
            return
        if sub == "delete":
            if len(args) not in (3, 4) or (len(args) == 4 and args[3] != "--yes"):
                self._emit('Kullanim: /memory delete <kategori> <anahtar> --yes')
                return
            if len(args) != 4:
                self._emit(self._color("Bellek kalici siliniyor; onaylamak icin --yes ekle.", "yellow"))
                return
            self._emit(bellek.bellek_sil(args[1], args[2]))
            return
        # /memory <kategori> [anahtar] kisayolu
        self._emit(bellek.bellek_oku(sub, args[1] if len(args) > 1 else ""))

    def _print_memory_search(self, query: str) -> None:
        bellek = _bellek()
        data = bellek.bellek_raw()
        needle = query.strip().lower()
        hits: list[str] = []
        for kategori, icerik in data.items():
            if isinstance(icerik, dict):
                for k, v in icerik.items():
                    val = v.get("deger") if isinstance(v, dict) else v
                    if needle in k.lower() or needle in str(val).lower():
                        hits.append(f"[{kategori}] {k}: {val}")
            elif isinstance(icerik, list):
                for item in icerik:
                    k = item.get("anahtar", "")
                    val = item.get("deger", "")
                    if needle in k.lower() or needle in str(val).lower():
                        hits.append(f"[{kategori}] {k}: {val}")
        if not hits:
            self._emit(f"'{query}' icin bellekte sonuc bulunamadi.")
            return
        self._emit(self._color(f"{len(hits)} sonuc:", "bold"))
        for hit in hits:
            self._emit(f"  • {hit}")

    def _default_orchestrator_prompt(self) -> str:
        getter = getattr(self.base_model, "default_system_instruction", None)
        return getter() if callable(getter) else ""

    def _manage_prompt(self, args: list[str]) -> None:
        if not args or args[0].lower() == "show":
            self._print_prompt_status()
            return
        sub = args[0].lower()
        if sub == "default":
            self._emit(self._color("Varsayilan orkestrator promptu (kod icinde, SYSTEM_INSTRUCTION):", "bold"))
            self._emit(self._default_orchestrator_prompt())
            return
        if sub == "set":
            if len(args) != 2 or not args[1].strip():
                self._emit('Kullanim: /prompt set "yeni prompt metni"')
                return
            _studio().write_orchestrator_prompt(args[1])
            self._emit(self._color("Orkestrator promptu guncellendi; bir sonraki mesajda hemen etkili olur.", "green"))
            return
        if sub == "reset":
            _studio().write_orchestrator_prompt("")
            self._emit(self._color("Orkestrator promptu varsayilana sifirlandi.", "green"))
            return
        self._emit(self._color(f"Bilinmeyen /prompt komutu: {sub}. Kullanim: /prompt [show|default|set \"...\"|reset]", "red"))

    def _print_prompt_status(self) -> None:
        override = _studio().read_orchestrator_prompt()
        if override:
            self._emit(self._color("Orkestrator promptu: OZEL (config/orchestrator_prompt.md)", "bold"))
            self._emit(override)
        else:
            self._emit(self._color("Orkestrator promptu: VARSAYILAN (kod icinde)", "bold"))
            self._emit(self._default_orchestrator_prompt())
        self._emit(self._color("\n/prompt default ile varsayilani, /prompt set \"...\" ile ozellestirilmisini gorebilirsin.", "yellow"))

    async def _manage_heartbeat(self, args: list[str]) -> None:
        if not args:
            status = get_heartbeat_status_snapshot()
            jobs = get_heartbeat_jobs_snapshot()
            self._emit(self._color("Heartbeat", "bold"))
            self._emit(
                f"  Servis: {'calisiyor' if status.get('running') else 'kapali'} | "
                f"Config: {'aktif' if status.get('enabled') else 'pasif'} | {len(jobs)} gorev"
            )
            for job in jobs:
                self._emit(
                    f"  [{'RUN' if job.get('running') else 'ON ' if not job.get('paused') else 'OFF'}] "
                    f"{job.get('job_id')} | {job.get('name') or '-'} | sonraki: {job.get('next_run_at') or '-'}"
                )
            return

        action = args[0].lower()
        rest = args[1:]
        try:
            if action == "reload" and len(args) == 1:
                result = await reload_heartbeat_service(reason="terminal_reload")
            elif action in {"run", "pause", "resume"} and len(args) == 2:
                job_id = args[1]
                operations = {
                    "run": run_heartbeat_job,
                    "pause": pause_heartbeat_job,
                    "resume": resume_heartbeat_job,
                }
                result = await operations[action](job_id)
            elif action == "show" and len(rest) == 1:
                self._show_heartbeat_task(rest[0])
                return
            elif action == "log":
                positional, _flags = self._parse_flags(rest)
                job = positional[0] if positional and not positional[0].isdigit() else None
                extra = [item for item in rest if item != job]
                self._print_runs(
                    extra + (["--source", "heartbeat"] if not job else []),
                    job_id=job,
                    title=f"Heartbeat gecmisi{f' ({job})' if job else ''}",
                )
                return
            elif action == "add":
                await self._add_heartbeat_task(rest)
                return
            elif action == "remove":
                await self._remove_heartbeat_task(rest)
                return
            elif action in {"on", "off"} and not rest:
                await self._set_heartbeat_enabled(action == "on")
                return
            else:
                self._emit(
                    "Kullanim: /heartbeat [reload|run <id>|pause <id>|resume <id>|show <id>|log [id] [adet]|"
                    'add --cron X --gorev "..."|remove <id>|on|off]'
                )
                return
            self._emit(self._color(f"Heartbeat islemi tamamlandi: {result}", "green"))
        except HeartbeatConfigError as exc:
            self._emit(self._color(f"Heartbeat config hatasi: {exc}", "red"))
        except ValueError as exc:
            self._emit(self._color(str(exc), "red"))
        except Exception as exc:
            self._emit(self._color(f"Heartbeat hatasi: {exc}", "red"))

    def _show_heartbeat_task(self, task_id: str) -> None:
        tasks = parse_config_content(read_config_content())["tasks"]
        task = next((item for item in tasks if item.task_id == task_id), None)
        if task is None:
            self._emit(self._color(f"Heartbeat gorevi bulunamadi: {task_id}", "red"))
            return
        self._emit(self._color(f"Heartbeat gorevi: {task.task_id}", "bold"))
        self._emit(f"  Ad     : {task.name}")
        self._emit(f"  Cron   : {task.cron}")
        self._emit(f"  Durum  : {'aktif' if task.enabled else 'pasif'}")
        self._emit("  Gorev  :")
        for line in task.gorev.splitlines():
            self._emit(f"    {line}")

    async def _add_heartbeat_task(self, args: list[str]) -> None:
        positional, flags = self._parse_flags(args, bool_flags={"disabled", "dry_run"})
        if positional or "cron" not in flags or "gorev" not in flags:
            self._emit(
                'Kullanim: /heartbeat add --cron <startup|*/N|HH:MM> --gorev "..." '
                '[--id <id>] [--name "..."] [--disabled] [--dry-run]'
            )
            return

        content = read_config_content()
        updated, task_id = add_task_to_content(
            content,
            gorev=str(flags["gorev"]),
            cron=str(flags["cron"]),
            task_id=str(flags.get("id") or ""),
            name=str(flags.get("name") or ""),
            enabled=not flags.get("disabled"),
        )

        if flags.get("dry_run"):
            self._emit(self._color(f"[dry-run] Eklenecek gorev: {task_id}", "yellow"))
            self._emit("\n".join(f"  {line}" for line in updated.splitlines()[-8:]))
            return

        write_config_content(updated)
        self._emit(self._color(f"Heartbeat gorevi eklendi: {task_id}", "green"))
        await self._refresh_heartbeat("terminal_task_add")

    async def _remove_heartbeat_task(self, args: list[str]) -> None:
        positional, flags = self._parse_flags(args, bool_flags={"yes"})
        if len(positional) != 1:
            self._emit("Kullanim: /heartbeat remove <id> [--yes]")
            return

        task_id = positional[0]
        content = read_config_content()
        updated = remove_task_from_content(content, task_id)

        if not flags.get("yes") and not await self._confirm(f"  {task_id} gorevi silinecek. Onayliyor musun? [e/H]: "):
            self._emit("Silme iptal edildi.")
            return

        write_config_content(updated)
        self._emit(self._color(f"Heartbeat gorevi silindi: {task_id}", "green"))
        await self._refresh_heartbeat("terminal_task_remove")

    async def _refresh_heartbeat(self, reason: str) -> None:
        """Config yazildiktan sonra zamanlayiciyi tazeler.

        Servis henuz ayakta degilse bu bir hata degil: config diske yazildi ve
        heartbeat baslarken okunacak.
        """
        try:
            result = await reload_heartbeat_service(reason=reason)
        except Exception as exc:
            self._emit(self._color(f"  Config kaydedildi; zamanlayici tazelenemedi ({exc}).", "yellow"))
            return
        self._emit(f"  Zamanlayici yenilendi: {result}")

    async def _set_heartbeat_enabled(self, enabled: bool) -> None:
        updated = set_enabled_in_content(read_config_content(), enabled)
        write_config_content(updated)
        self._emit(self._color(f"Heartbeat config: {'aktif' if enabled else 'pasif'}", "green"))
        await self._refresh_heartbeat("terminal_toggle")

    async def _chat(self, user_text: str) -> None:
        job_id = f"terminal-chat-{time.time_ns()}"
        acquired, snapshot = await try_acquire_automation(
            "terminal",
            job_id=job_id,
            label="Terminal sohbet istegi",
            source="terminal",
        )
        if not acquired:
            owner = snapshot.get("owner") or "otomasyon"
            label = snapshot.get("label") or snapshot.get("job_id") or "aktif gorev"
            self._emit(self._color(f"Sistem mesgul: {owner} / {label}", "yellow"))
            return

        context = self._build_context()
        self._add_history("user", user_text)
        self._emit(self._color("Ethgent dusunuyor...", "yellow"))

        async def on_direct_text(text: str):
            cleaned = (text or "").strip()
            if cleaned:
                self._emit(f"  ↳ {cleaned}")

        try:
            with telemetry.run_scope(self.source, " ".join(user_text.split())[:80], detail="chat"):
                result = await self.base_model.text_query(
                    user_text,
                    context=context,
                    on_direct_text=on_direct_text,
                )
            answer = self._extract_result_text(result)
            self._add_history("assistant", answer)
            self._emit("")
            self._emit(self._color("Ethgent>", "green"))
            self._emit(answer)
            self._emit("")
        except Exception as exc:
            self._emit(self._color(f"Model hatasi: {exc}", "red"))
        finally:
            await release_automation("terminal", job_id=job_id)

    async def _request_terminal_approval(self, action_id: str, description: str) -> bool:
        self._emit("")
        self._emit(self._color("ONAY GEREKLI", "yellow"))
        self._emit(f"  {description}")
        try:
            answer = await self._read_line("  Onayliyor musun? [e/H]: ")
        except (EOFError, KeyboardInterrupt):
            return False
        approved = (answer or "").strip().lower() in {"e", "evet", "y", "yes"}
        self.base_model.log_message(
            "sistem",
            f"Terminal onayi: {action_id} -> {'onaylandi' if approved else 'reddedildi'}",
        )
        return approved

    def _build_context(self) -> str:
        selected = self.history[-_MAX_CONTEXT_MESSAGES:]
        rendered = []
        total_chars = 0
        for item in reversed(selected):
            role = "Kullanici" if item.get("role") == "user" else "Asistan"
            line = f"{role}: {item.get('content', '')}"
            if rendered and total_chars + len(line) > _MAX_CONTEXT_CHARS:
                break
            rendered.append(line)
            total_chars += len(line)
        if not rendered:
            return ""
        return "=== TERMINAL KONUSMA GECMISI ===\n" + "\n".join(reversed(rendered)) + "\n\n=== YENI MESAJ ==="

    @staticmethod
    def _extract_result_text(result: Any) -> str:
        if isinstance(result, tuple):
            if len(result) > 1 and str(result[1] or "").strip():
                return str(result[1]).strip()
            if len(result) > 3 and result[3]:
                return str(result[3][-1]).strip()
            if len(result) > 2 and result[2]:
                return str(result[2][-1]).strip()
        if isinstance(result, str) and result.strip():
            return result.strip()
        return "Islem tamamlandi ancak metin yaniti uretilmedi."

    @staticmethod
    def _resolve_switch(action: str, current: bool) -> bool:
        if action in {"on", "ac", "aktif"}:
            return True
        if action in {"off", "kapat", "pasif"}:
            return False
        if action in {"toggle", "degistir"}:
            return not current
        raise ValueError("Durum on, off veya toggle olmali.")

    @staticmethod
    def _format_duration(seconds: int) -> str:
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}s {minutes}dk {secs}sn"
        if minutes:
            return f"{minutes}dk {secs}sn"
        return f"{secs}sn"

    def _load_history(self) -> list[dict]:
        if not os.path.exists(self.history_file):
            return []
        try:
            with open(self.history_file, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, list):
                return []
            return [
                item for item in raw
                if isinstance(item, dict)
                and item.get("role") in {"user", "assistant"}
                and str(item.get("content") or "").strip()
            ][-_MAX_HISTORY:]
        except Exception:
            return []

    def _save_history(self) -> None:
        os.makedirs(os.path.dirname(self.history_file), exist_ok=True)
        temporary = f"{self.history_file}.tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(self.history[-_MAX_HISTORY:], handle, ensure_ascii=False, indent=2)
            os.replace(temporary, self.history_file)
        except Exception as exc:
            self._emit(self._color(f"Gecmis kaydedilemedi: {exc}", "yellow"))

    def _add_history(self, role: str, content: str) -> None:
        cleaned = (content or "").strip()
        if not cleaned:
            return
        self.history.append(
            {
                "role": role,
                "content": cleaned,
                "time": datetime.now().strftime("%H:%M"),
            }
        )
        self.history = self.history[-_MAX_HISTORY:]
        self._save_history()


async def run_terminal_manager(base_model, *, telegram_enabled: bool = False, discord_enabled: bool = False):
    manager = TerminalManager(
        base_model,
        telegram_enabled=telegram_enabled,
        discord_enabled=discord_enabled,
    )
    await manager.run()
