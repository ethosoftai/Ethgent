"""Terminal yonetim komutlari: ajan / tool / heartbeat / pack / gozlemlenebilirlik."""

import _env  # noqa: F401  (her seyden once)

import asyncio
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

import _support as S
from _env import CONFIG_DIR, REPO, ROOT, WORKSPACE_DIR
from _support import Term


def studio():
    from MarketingApp.llms import agent_studio

    return agent_studio


def agent_entry(name):
    return next((a for a in studio().load_agents_config()["agents"] if a["name"] == name), None)


def tool_entry(name):
    return next((t for t in studio().load_custom_tools_config()["custom_tools"] if t["name"] == name), None)


class TerminalCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from MarketingApp.environments import automation_runtime

        S.reset_state()
        await automation_runtime.reset_automation_runtime()
        self.addCleanup(S.patch_model_env())
        manager = S.fresh_telemetry()
        self.t = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        self.term = Term()
        self.src = Path(tempfile.mkdtemp(dir=ROOT))

    async def run_cmd(self, line, term=None):
        return await (term or self.term).run(line)

    def write_source(self, name, text):
        path = self.src / name
        path.write_text(text, encoding="utf-8")
        return path


# --------------------------------------------------------------------------- ajanlar

class AgentCommandTests(TerminalCase):
    async def test_legacy_toggle_form_still_works(self):
        self.term.base.active_agents["sosyal_medya_agent"] = True
        self.assertIn("pasif", await self.run_cmd("/agent sosyal_medya_agent off"))
        self.assertIn("aktif", await self.run_cmd("/agent sosyal_medya_agent on"))
        self.assertIn("bulunamadi", await self.run_cmd("/agent yok_boyle on"))

    async def test_create_config_agent_persists_everything(self):
        out = await self.run_cmd('/agent create rapor_ajani --tools workspace_oku,workspace_yaz --desc "Rapor" --prompt "Sen bir ajansin." --model gemma-x')
        self.assertIn("olusturuldu", out)
        agent = agent_entry("rapor_ajani")
        self.assertEqual(agent["tools"], ["workspace_oku", "workspace_yaz"])
        self.assertEqual((agent["type"], agent["model"], agent["description"], agent["system_prompt"]),
                         ("config", "gemma-x", "Rapor", "Sen bir ajansin."))

    async def test_create_rejects_duplicates_and_bad_names(self):
        await self.run_cmd("/agent create ilk_ajan --tools workspace_oku")
        self.assertIn("zaten var", await self.run_cmd("/agent create ilk_ajan"))
        self.assertIn("kucuk harf", await self.run_cmd("/agent create BuyukHarf"))
        self.assertIsNone(agent_entry("BuyukHarf"))

    async def test_create_without_tools_warns_that_config_agents_get_no_default_set(self):
        out = await self.run_cmd("/agent create bos_ajan")
        self.assertIn("tool'suz", out)
        self.assertIn("hicbir tool kullanamaz", out)

    async def test_unknown_tools_are_warned_not_blocked(self):
        out = await self.run_cmd("/agent create x_ajani --tools olmayan_tool_xyz")
        self.assertIn("kayitli olmayan", out)
        self.assertEqual(agent_entry("x_ajani")["tools"], ["olmayan_tool_xyz"])

    async def test_tool_category_and_group_expand_to_flat_name_lists(self):
        await self.run_cmd("/agent create memo_ajani --tool-category memory")
        self.assertTrue({"bellek_oku", "bellek_yaz", "rol_oku"} <= set(agent_entry("memo_ajani")["tools"]))
        await self.run_cmd("/agent create arastir_ajani --tool-group arastirma_agent")
        self.assertIn("web_arama", agent_entry("arastir_ajani")["tools"])
        self.assertIn("Bilinmeyen tool grubu", await self.run_cmd("/agent create z_ajani --tool-group yok_grup"))
        self.assertIn("Bilinmeyen tool kategorisi", await self.run_cmd("/agent create z_ajani --tool-category yok_kat"))
        self.assertIsNone(agent_entry("z_ajani"))

    async def test_dry_run_saves_nothing(self):
        out = await self.run_cmd("/agent create deneme_ajan --tool-category memory --dry-run")
        self.assertIn("dry-run", out)
        self.assertIsNone(agent_entry("deneme_ajan"))
        await self.run_cmd("/agent create var_olan --tools workspace_oku")
        out = await self.run_cmd("/agent edit var_olan --tool-category memory --dry-run")
        self.assertIn("Tool sayisi 1 ->", out)
        self.assertEqual(agent_entry("var_olan")["tools"], ["workspace_oku"])

    async def test_edit_add_remove_and_flags(self):
        await self.run_cmd("/agent create e_ajani --tools workspace_oku,workspace_yaz")
        await self.run_cmd("/agent edit e_ajani --add-tools web_arama")
        self.assertEqual(agent_entry("e_ajani")["tools"], ["workspace_oku", "workspace_yaz", "web_arama"])
        await self.run_cmd("/agent edit e_ajani --remove-tools workspace_yaz --disable --model yeni-model")
        agent = agent_entry("e_ajani")
        self.assertEqual((agent["tools"], agent["enabled"], agent["model"]), (["workspace_oku", "web_arama"], False, "yeni-model"))
        await self.run_cmd("/agent edit e_ajani --tool-category memory")
        self.assertIn("bellek_oku", agent_entry("e_ajani")["tools"])
        await self.run_cmd("/agent edit e_ajani --remove-tool-category memory --enable")
        agent = agent_entry("e_ajani")
        self.assertNotIn("bellek_oku", agent["tools"])
        self.assertTrue(agent["enabled"])

    async def test_edit_usage_and_unknown_agent(self):
        self.assertIn("Kullanim", await self.run_cmd("/agent edit e_ajani"))
        self.assertIn("bulunamadi", await self.run_cmd("/agent edit yok_ajan --model x"))
        self.assertIn("icin deger verilmedi", await self.run_cmd("/agent create x_ajan --model"))

    async def test_editing_a_builtin_keeps_its_implicit_default_tools(self):
        # Regresyon: builtin'in bos tool listesi 'kendi grubu' demektir; tek tool eklemek onu silmemeli.
        self.assertEqual(agent_entry("content_creator_agent")["tools"], [])
        out = await self.run_cmd("/agent edit content_creator_agent --add-tools web_arama")
        self.assertIn("varsayilan setinden", out)
        tools = agent_entry("content_creator_agent")["tools"]
        self.assertGreaterEqual(len(tools), 20)
        self.assertIn("pexels_fotograf_ara", tools)
        self.assertEqual(tools.count("web_arama"), 1)

    async def test_delete_flow(self):
        await self.run_cmd("/agent create silinecek --tools workspace_oku")
        term = Term(answers=["hayir"])
        self.assertIn("iptal", await self.run_cmd("/agent delete silinecek", term))
        self.assertIsNotNone(agent_entry("silinecek"))
        term = Term(answers=["e"])
        self.assertIn("silindi", await self.run_cmd("/agent delete silinecek", term))
        self.assertIsNone(agent_entry("silinecek"))
        self.assertIn("silinemez", await self.run_cmd("/agent delete sistem_agent --yes"))
        self.assertIn("bulunamadi", await self.run_cmd("/agent delete yok_ajan --yes"))

    async def test_copy_resolves_the_implicit_tool_set_of_a_builtin(self):
        out = await self.run_cmd("/agent copy content_creator_agent klon_ajan")
        self.assertIn("kopyalandi", out)
        clone = agent_entry("klon_ajan")
        self.assertEqual(clone["type"], "config")
        self.assertGreaterEqual(len(clone["tools"]), 20)
        self.assertIn("zaten var", await self.run_cmd("/agent copy content_creator_agent klon_ajan"))
        self.assertIn("bulunamadi", await self.run_cmd("/agent copy yok_ajan baska_klon"))

    async def test_show_and_list(self):
        await self.run_cmd('/agent create goster_ajan --tools workspace_oku --desc "Aciklama"')
        out = await self.run_cmd("/agent show goster_ajan")
        self.assertIn("Ajan: goster_ajan", out)
        self.assertIn("Aciklama", out)
        self.assertIn("bulunamadi", await self.run_cmd("/agent show yok_ajan"))


class AgentTestCommandTests(TerminalCase):
    async def test_runs_the_agent_and_records_a_run(self):
        async def runner(gorev):
            self.t.record_usage("rapor", "m", SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15))
            return f"islendi: {gorev}"

        self.term.base._submodel_func_map["rapor"] = runner
        out = await self.run_cmd('/agent test rapor "haftalik ozet"')
        self.assertIn("islendi: haftalik ozet", out)
        run = self.t.recent_runs(1)[0]
        self.assertEqual((run["status"], run["source"], run["label"], run["total_tokens"]), ("ok", "terminal", "agent test: rapor", 15))

    async def test_the_runners_error_string_marks_the_run_as_failed(self):
        async def runner(gorev):
            return "[SISTEM_MESAJI_GIZLI] rapor hatasi: rate limit"

        self.term.base._submodel_func_map["rapor"] = runner
        await self.run_cmd('/agent test rapor "x"')
        run = self.t.recent_runs(1)[0]
        self.assertEqual(run["status"], "error")
        self.assertIn("rate limit", run["error"])

    async def test_unknown_agent_usage_and_busy_system(self):
        self.assertIn("bulunamadi", await self.run_cmd('/agent test yok "x"'))
        self.assertIn("Kullanim", await self.run_cmd("/agent test rapor"))
        from MarketingApp.environments import automation_runtime

        async def runner(gorev):
            return "asla"

        self.term.base._submodel_func_map["rapor"] = runner
        await automation_runtime.try_acquire_automation("heartbeat", job_id="j", label="post", source="heartbeat")
        self.assertIn("mesgul", await self.run_cmd('/agent test rapor "x"'))
        self.assertEqual(self.t.recent_runs(5), [])

    async def test_source_can_be_overridden_for_remote_channels(self):
        from MarketingApp.environments.terminal import TerminalManager

        async def runner(gorev):
            return "ok"

        base = S.FakeBase()
        base._submodel_func_map["rapor"] = runner
        out = []
        manager = TerminalManager(base, input_func=lambda p: "", output_func=out.append, source="telegram",
                                  history_file=str(ROOT / "h.json"))
        await manager.handle_line('/agent test rapor "x"')
        self.assertEqual(self.t.recent_runs(1)[0]["source"], "telegram")


# ------------------------------------------------------------------------------ pack

class PackTests(TerminalCase):
    async def _build_setup(self):
        src = self.write_source("kripto.py", 'def fiyat_getir(s="BTC"):\n    """Fiyat."""\n    return s\n\n\ndef hacim_getir(s="BTC"):\n    """Hacim."""\n    return s\n')
        await self.run_cmd(f"/tool create --file {src} --all --env COINGECKO_KEY")
        await self.run_cmd('/agent create kripto_ajani --tools fiyat_getir,hacim_getir,workspace_yaz --desc "Kripto takip" --prompt "Sen kripto izlersin."')

    async def test_export_then_install_on_a_clean_setup_reproduces_the_agent(self):
        await self._build_setup()
        pack = ROOT / "cikti" / "kripto_paketim"
        out = await self.run_cmd(f"/agent pack export kripto_paketim --agents kripto_ajani --out {pack}")
        self.assertIn("Pack olusturuldu", out)
        for expected in ("plugin.yaml", "agents/kripto_ajani.yaml", "prompts/kripto_ajani.md", "tools/kripto.py", "env.example", "README.md"):
            self.assertTrue((pack / expected).exists(), expected)
        self.assertEqual(len(list((pack / "tools").glob("*.py"))), 1)  # iki tool tek dosyayi paylasir

        S.reset_state()  # temiz makine
        self.assertIsNone(agent_entry("kripto_ajani"))
        out = await self.run_cmd(f"/agent pack install {pack} --yes")
        self.assertIn("Pack kuruldu", out)
        agent = agent_entry("kripto_ajani")
        self.assertEqual(agent["tools"], ["fiyat_getir", "hacim_getir", "workspace_yaz"])
        self.assertEqual((agent["description"], agent["system_prompt"].strip()), ("Kripto takip", "Sen kripto izlersin."))
        loaded = studio().load_custom_tools()["tools"]
        self.assertEqual((loaded["fiyat_getir"]("ETH"), loaded["hacim_getir"]("ETH")), ("ETH", "ETH"))

    async def test_env_example_carries_names_only_never_values(self):
        src = self.write_source("gizli.py", 'def gizli_tool():\n    """x"""\n    return 1\n')
        await self.run_cmd(f"/tool create gizli_tool --file {src} --desc x --env SUPER_SECRET=hunter2-cok-gizli")
        pack = ROOT / "cikti" / "gizli_paket"
        await self.run_cmd(f"/agent pack export gizli_paket --tools gizli_tool --out {pack}")
        blob = "\n".join(p.read_text(encoding="utf-8") for p in pack.rglob("*") if p.is_file())
        self.assertNotIn("hunter2", blob)
        self.assertEqual((pack / "env.example").read_text(encoding="utf-8").strip(), "SUPER_SECRET=")

    async def test_export_refuses_builtin_tools_but_allows_builtin_agents(self):
        self.assertIn("hazir bir builtin tool", await self.run_cmd("/agent pack export paket_b --tools workspace_yaz"))
        self.assertIn("Kullanim", await self.run_cmd("/agent pack export"))
        pack = ROOT / "cikti" / "sistem_paketi"
        out = await self.run_cmd(f"/agent pack export sistem_paketi --agents sistem_agent --out {pack}")
        self.assertIn("Pack olusturuldu", out)
        self.assertTrue((pack / "agents/sistem_agent.yaml").exists())
        self.assertFalse((pack / "submodels").exists())  # orijinal builtin: kaynak gomulmez
        await self._build_setup()
        pack = ROOT / "cikti" / "tekrar"
        await self.run_cmd(f"/agent pack export tekrar_paket --agents kripto_ajani --out {pack}")
        self.assertIn("zaten var", await self.run_cmd(f"/agent pack export tekrar_paket --agents kripto_ajani --out {pack}"))
        self.assertIn("Pack olusturuldu", await self.run_cmd(f"/agent pack export tekrar_paket --agents kripto_ajani --out {pack} --overwrite"))

    async def test_export_warns_when_a_referenced_custom_tool_is_left_out(self):
        await self._build_setup()
        pack = ROOT / "cikti" / "eksik"
        out = await self.run_cmd(f"/agent pack export eksik_paket --agents kripto_ajani --tools fiyat_getir --out {pack}")
        self.assertIn("pakete alinmadi: hacim_getir", out)

    async def test_export_and_install_a_scaffolded_builtin_onto_a_clean_setup(self):
        from MarketingApp.llms.SubModels import base as submodel_base

        init_path = studio().SUBMODELS_INIT_PATH
        original_init = init_path.read_text(encoding="utf-8")
        scaffold_path = studio()._builtin_module_path("ozel_ajanim")

        def cleanup():
            scaffold_path.unlink(missing_ok=True)
            init_path.write_text(original_init, encoding="utf-8")
            sys.modules.pop("MarketingApp.llms.SubModels.ozel_ajanim", None)
            submodel_base._SUBMODEL_REGISTRY.pop("ozel_ajanim", None)

        self.addCleanup(cleanup)

        await self.run_cmd('/agent create ozel_ajanim --builtin --desc "Ozel builtin" --prompt "Sen ozel bir ajansin."')
        self.assertTrue(scaffold_path.exists())

        pack = ROOT / "cikti" / "ozel_paket"
        out = await self.run_cmd(f"/agent pack export ozel_paket --agents ozel_ajanim --out {pack}")
        self.assertIn("Pack olusturuldu", out)
        self.assertTrue((pack / "submodels/ozel_ajanim.py").exists())

        # "temiz makine" simulasyonu: config'i VE scaffold dosyasini/import'unu tamamen sil
        S.reset_state()
        scaffold_path.unlink()
        init_path.write_text(original_init, encoding="utf-8")
        sys.modules.pop("MarketingApp.llms.SubModels.ozel_ajanim", None)
        submodel_base._SUBMODEL_REGISTRY.pop("ozel_ajanim", None)
        self.assertIsNone(agent_entry("ozel_ajanim"))

        # Python kodlu ajan ana process'te calisir: --trust olmadan kurulmaz.
        out = await self.run_cmd(f"/agent pack install {pack} --yes")
        self.assertNotIn("Pack kuruldu", out)
        self.assertIn("--trust", out)
        self.assertFalse(scaffold_path.exists())

        out = await self.run_cmd(f"/agent pack install {pack} --yes --trust")
        self.assertIn("Pack kuruldu", out)
        self.assertTrue(scaffold_path.exists())
        self.assertIn("ozel_ajanim", init_path.read_text(encoding="utf-8"))
        agent = agent_entry("ozel_ajanim")
        self.assertEqual(agent["type"], "builtin")
        self.assertEqual(agent["description"], "Ozel builtin")
        self.assertIn("ozel_ajanim", submodel_base._SUBMODEL_REGISTRY)

    async def test_export_and_install_config_overlay_for_a_shipped_builtin(self):
        await self.run_cmd('/agent edit content_creator_agent --model gemma-test-model --prompt "Ozel marka sesi."')
        pack = ROOT / "cikti" / "marka_paketi"
        out = await self.run_cmd(f"/agent pack export marka_paketi --agents content_creator_agent --out {pack}")
        self.assertIn("Pack olusturuldu", out)
        self.assertFalse((pack / "submodels").exists())
        self.assertIn("gomulu bir builtin", out)

        S.reset_state()  # content_creator_agent varsayilana doner: model=default, prompt=''
        self.assertEqual(agent_entry("content_creator_agent")["model"], "default")

        # sevk edilen builtin'ler agents.yaml'da HER ZAMAN varsayilan haliyle onceden var;
        # overlay uygulamak icin --overwrite gerekir.
        out = await self.run_cmd(f"/agent pack install {pack} --yes --overwrite")
        self.assertIn("Pack kuruldu", out)
        agent = agent_entry("content_creator_agent")
        self.assertEqual(agent["model"], "gemma-test-model")
        self.assertEqual(agent["system_prompt"].strip(), "Ozel marka sesi.")

    async def test_github_spec_clones_then_installs_agent_and_tool(self):
        await self._build_setup()
        pack = ROOT / "cikti" / "gh_paket"
        await self.run_cmd(f"/agent pack export gh_paket --agents kripto_ajani --out {pack}")
        repo = ROOT / "uzak_repo"
        (repo / "paketler").mkdir(parents=True)
        subprocess.run(["cp", "-r", str(pack), str(repo / "paketler" / "gh_paket")], check=True)
        for cmd in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
            subprocess.run(["git", "-C", str(repo), *cmd], check=True)

        real_run = subprocess.run

        def fake_run(cmd, *a, **kw):
            if cmd[:2] == ["git", "clone"]:  # github URL'sini yerel depoya yonlendir
                cmd = [*cmd[:-2], str(repo), cmd[-1]]
            return real_run(cmd, *a, **kw)

        S.reset_state()
        self.assertIsNone(agent_entry("kripto_ajani"))
        with unittest.mock.patch.object(studio().subprocess, "run", fake_run):
            out = await self.run_cmd("/agent pack install github:kullanici/repo#paketler/gh_paket --yes")
        self.assertIn("Pack kuruldu", out)
        self.assertEqual(agent_entry("kripto_ajani")["tools"], ["fiyat_getir", "hacim_getir", "workspace_yaz"])
        self.assertEqual(studio().load_custom_tools()["tools"]["fiyat_getir"]("ETH"), "ETH")
        self.assertEqual(studio().load_agent_packs_config()["installed_packs"][0]["source_path"],
                         "github:kullanici/repo#paketler/gh_paket")

    async def test_github_spec_rejects_malformed_and_escaping_sources(self):
        for bad in ("github:x", "github:a/b#../etc", "github:a/b@-oops", "github:a/b c"):
            out = await self.run_cmd(f"/agent pack preview {bad}")
            self.assertNotIn("Kurulabilir", out, bad)

    async def test_preview_and_list(self):
        example = REPO / "MarketingApp" / "workspace" / "agent_packs" / "ornek_haber_bundle"
        self.assertIn("Kurulabilir: evet", await self.run_cmd(f"/agent pack preview {example}"))
        self.assertIn("Kurulu pack yok", await self.run_cmd("/agent pack list"))
        await self.run_cmd(f"/agent pack install {example} --yes")
        self.assertIn("ornek_haber_bundle", await self.run_cmd("/agent pack list"))


# ------------------------------------------------------------------------------ tool'lar

class ToolFilterTests(TerminalCase):
    async def test_group_category_and_risk_filters(self):
        out = await self.run_cmd("/tools --group arastirma_agent")
        self.assertIn("web_arama", out)
        self.assertNotIn("browser_click_css", out)
        self.assertIn("bellek_oku", await self.run_cmd("/tools --category memory"))
        high = await self.run_cmd("/tools --risk high")
        self.assertIn("publish_x_post", high)
        self.assertIn("risk=high", high)
        self.assertNotIn("risk=low", high)

    async def test_combined_filters_and_search_text(self):
        out = await self.run_cmd("/tools workspace --category workspace")
        self.assertIn("workspace_oku", out)
        self.assertNotIn("bellek_oku", out)

    async def test_invalid_filter_values_are_errors_not_empty_results(self):
        self.assertIn("Bilinmeyen tool grubu", await self.run_cmd("/tools --group yok"))
        self.assertIn("Bilinmeyen tool kategorisi", await self.run_cmd("/tools --category yok"))
        self.assertIn("low, medium veya high", await self.run_cmd("/tools --risk cok_yuksek"))

    async def test_list_groups_reports_groups_categories_and_risk(self):
        out = await self.run_cmd("/tools --list-groups")
        for expected in ("Tool gruplari", "Tool kategorileri", "Risk dagilimi", "sosyal_medya_agent"):
            self.assertIn(expected, out)

    async def test_legacy_tool_toggle(self):
        self.term.base.active_tools["web_arama"] = True
        self.assertIn("pasif", await self.run_cmd("/tool web_arama off"))
        self.assertIn("bulunamadi", await self.run_cmd("/tool yok_tool on"))


class CustomToolTests(TerminalCase):
    async def test_create_from_file_copies_it_under_the_tool_name(self):
        src = self.write_source("baska_ad.py", 'def fiyat_getir(s="BTC"):\n    """Doc."""\n    return f"{s}!"\n')
        out = await self.run_cmd(f'/tool create fiyat_getir --file {src} --desc "Fiyat" --params \'{{"s": "BTC"}}\'')
        self.assertIn("olusturuldu", out)
        self.assertIn("Yukleme testi: basarili", out)
        self.assertTrue((WORKSPACE_DIR / "custom_tools" / "fiyat_getir.py").exists())
        self.assertTrue(src.exists())  # kaynak dokunulmadan kalir
        self.assertEqual(studio().load_custom_tools()["tools"]["fiyat_getir"]("ETH"), "ETH!")
        self.assertEqual(tool_entry("fiyat_getir")["description"], "Fiyat")

    async def test_the_file_is_a_copy_not_a_reference_and_edit_reloads_it(self):
        src = self.write_source("k.py", 'def sayac():\n    """d"""\n    return 1\n')
        await self.run_cmd(f"/tool create sayac --file {src} --desc d")
        src.write_text('def sayac():\n    """d"""\n    return 2\n', encoding="utf-8")
        self.assertEqual(studio().load_custom_tools()["tools"]["sayac"](), 1)
        await self.run_cmd(f"/tool edit sayac --file {src}")
        self.assertEqual(studio().load_custom_tools()["tools"]["sayac"](), 2)

    async def test_inline_code(self):
        out = await self.run_cmd('/tool create satir_ici --code "def satir_ici():\n    return 42" --desc "Inline"')
        self.assertIn("Yukleme testi: basarili", out)
        self.assertEqual(studio().load_custom_tools()["tools"]["satir_ici"](), 42)

    async def test_invalid_code_is_rejected_before_anything_is_written(self):
        bozuk = self.write_source("bozuk.py", "def bozuk(:\n  pass\n")
        self.assertIn("Kod derlenemedi", await self.run_cmd(f"/tool create bozuk --file {bozuk} --desc x"))
        yanlis = self.write_source("yanlis.py", "def baska_ad():\n    return 1\n")
        self.assertIn("adinda bir fonksiyon tanimi bulunamadi", await self.run_cmd(f"/tool create rapor_al --file {yanlis} --desc x"))
        notes = self.write_source("notlar.txt", "merhaba")
        self.assertIn(".py uzantili", await self.run_cmd(f"/tool create notlar --file {notes} --desc x"))
        self.assertIn("Dosya bulunamadi", await self.run_cmd("/tool create hayalet --file /yok/dosya.py --desc x"))
        for name in ("bozuk", "rapor_al", "notlar", "hayalet"):
            self.assertIsNone(tool_entry(name), name)

    async def test_duplicates_and_missing_targets(self):
        src = self.write_source("a.py", 'def ikiz():\n    """d"""\n    return 1\n')
        await self.run_cmd(f"/tool create ikiz --file {src} --desc d")
        self.assertIn("zaten kayitli", await self.run_cmd(f"/tool create ikiz --file {src}"))
        self.assertIn("bulunamadi", await self.run_cmd("/tool edit yok_boyle --desc x"))
        self.assertIn("--file", await self.run_cmd("/tool create yeni_tool --desc x"))

    async def test_edit_flags_and_show(self):
        src = self.write_source("d.py", 'def duzenle():\n    """d"""\n    return 1\n')
        await self.run_cmd(f"/tool create duzenle --file {src} --desc eski")
        await self.run_cmd("/tool edit duzenle --desc yeni --disable")
        entry = tool_entry("duzenle")
        self.assertEqual((entry["description"], entry["enabled"]), ("yeni", False))
        out = await self.run_cmd("/tool show duzenle --code")
        self.assertIn("pasif", out)
        self.assertIn("return 1", out)
        self.assertNotIn("return 1", await self.run_cmd("/tool show duzenle"))

    async def test_delete_removes_entry_and_file_and_warns_about_users(self):
        src = self.write_source("s.py", 'def silinen():\n    """d"""\n    return 1\n')
        await self.run_cmd(f"/tool create silinen --file {src} --desc d")
        await self.run_cmd("/agent create kullanici --tools silinen")
        out = await self.run_cmd("/tool delete silinen --yes")
        self.assertIn("kullanici", out)
        self.assertIsNone(tool_entry("silinen"))
        self.assertFalse((WORKSPACE_DIR / "custom_tools" / "silinen.py").exists())
        src2 = self.write_source("s2.py", 'def korunan():\n    """d"""\n    return 1\n')
        await self.run_cmd(f"/tool create korunan --file {src2} --desc d")
        await self.run_cmd("/tool delete korunan --yes --keep-file")
        self.assertTrue((WORKSPACE_DIR / "custom_tools" / "korunan.py").exists())

    async def test_env_flag_writes_values_to_env_model_but_names_alone_do_not(self):
        src = self.write_source("e.py", 'def envli():\n    """d"""\n    return 1\n')
        await self.run_cmd(f"/tool create envli --file {src} --desc d --env ANAHTAR_A,ANAHTAR_B=deger-b")
        self.assertEqual(tool_entry("envli")["env_vars"], ["ANAHTAR_B"])  # yalniz degeri verilen .env.model'e yazilir
        env_text = studio().MODEL_ENV_PATH.read_text(encoding="utf-8")
        self.assertIn("ANAHTAR_B=deger-b", env_text)
        self.assertNotIn("ANAHTAR_A", env_text)


class BatchToolTests(TerminalCase):
    SOURCE = ('"""Kripto."""\n\n'
              'def fiyat_getir(s="BTC"):\n    """Fiyati doner."""\n    return f"{s}:1"\n\n'
              'def hacim_getir(s="BTC"):\n    """Hacmi doner."""\n    return f"{s}:2"\n\n'
              'def trend_getir(s="BTC"):\n    """Trendi doner."""\n    return f"{s}:3"\n\n'
              'def _dahili():\n    return "tool degil"\n')

    async def test_names_registers_only_the_named_functions_sharing_one_file(self):
        src = self.write_source("kripto.py", self.SOURCE)
        out = await self.run_cmd(f"/tool create --file {src} --names fiyat_getir,hacim_getir")
        self.assertIn("2 tool kaydedildi", out)
        self.assertEqual(tool_entry("fiyat_getir")["file"], tool_entry("hacim_getir")["file"])
        self.assertIsNone(tool_entry("trend_getir"))
        self.assertEqual(len(list((WORKSPACE_DIR / "custom_tools").glob("kripto*.py"))), 1)
        self.assertEqual(tool_entry("fiyat_getir")["description"], "Fiyati doner.")  # docstring'den
        tools = studio().load_custom_tools()["tools"]
        self.assertEqual((tools["fiyat_getir"]("X"), tools["hacim_getir"]("X")), ("X:1", "X:2"))

    async def test_all_skips_underscore_helpers_and_needs_overwrite_for_existing(self):
        src = self.write_source("kripto.py", self.SOURCE)
        await self.run_cmd(f"/tool create --file {src} --names fiyat_getir")
        self.assertIn("--overwrite", await self.run_cmd(f"/tool create --file {src} --all"))
        out = await self.run_cmd(f"/tool create --file {src} --all --overwrite")
        self.assertIn("3 tool kaydedildi", out)
        self.assertIsNone(tool_entry("_dahili"))

    async def test_error_paths(self):
        src = self.write_source("kripto.py", self.SOURCE)
        self.assertIn("Dosyada bulunmayan fonksiyon: yok_boyle", await self.run_cmd(f"/tool create --file {src} --names yok_boyle"))
        self.assertIn("Toplu kayitta tool adi verilmez", await self.run_cmd(f"/tool create fiyat_getir --file {src} --names hacim_getir"))
        self.assertIn("--file <yol.py> gerekli", await self.run_cmd("/tool create --names a,b"))
        bos = self.write_source("bos.py", "x = 1\n")
        self.assertIn("ust seviye fonksiyon yok", await self.run_cmd(f"/tool create --file {bos} --all"))
        bozuk = self.write_source("bozuk.py", "def a(:\n  pass\n")
        self.assertIn("Kod derlenemedi", await self.run_cmd(f"/tool create --file {bozuk} --all"))

    async def test_a_builtin_tool_name_cannot_be_shadowed_without_overwrite(self):
        src = self.write_source("golge.py", 'def web_arama(q=""):\n    """d"""\n    return "sahte"\n')
        self.assertIn("--overwrite", await self.run_cmd(f"/tool create --file {src} --names web_arama"))
        self.assertIsNone(tool_entry("web_arama"))


# ----------------------------------------------------------------------------- heartbeat

class HeartbeatCommandTests(TerminalCase):
    def _config_text(self):
        return (CONFIG_DIR / "heartbeat_config.yaml").read_text(encoding="utf-8")

    async def test_add_show_remove_cycle_edits_the_real_config(self):
        original = self._config_text()
        out = await self.run_cmd('/heartbeat add --cron "*/45" --gorev "Market snapshot al" --name "Market" --id market_testi')
        self.assertIn("eklendi", out)
        self.assertIn("zamanlayici tazelenemedi", out)  # servis yokken hata degil, bilgi
        self.assertIn("market_testi", self._config_text())
        self.assertIn("&post_gorevi", self._config_text())  # anchor'lar korundu
        show = await self.run_cmd("/heartbeat show market_testi")
        self.assertIn("*/45", show)
        self.assertIn("Market snapshot al", show)
        await self.run_cmd("/heartbeat remove market_testi --yes")
        self.assertEqual(self._config_text().strip(), original.strip())

    async def test_dry_run_and_validation(self):
        original = self._config_text()
        self.assertIn("dry-run", await self.run_cmd('/heartbeat add --cron "08:30" --gorev "x" --dry-run'))
        self.assertEqual(self._config_text(), original)
        self.assertIn("Desteklenmeyen cron", await self.run_cmd('/heartbeat add --cron carsamba --gorev "x"'))
        self.assertIn("Kullanim", await self.run_cmd('/heartbeat add --gorev "x"'))
        self.assertEqual(self._config_text(), original)

    async def test_remove_asks_for_confirmation_and_reports_unknown(self):
        term = Term(answers=["hayir"])
        self.assertIn("iptal", await self.run_cmd("/heartbeat remove market_refresh_20m", term))
        self.assertIn("market_refresh_20m", self._config_text())
        self.assertIn("Task bulunamadi", await self.run_cmd("/heartbeat remove yok_boyle --yes"))

    async def test_on_off_toggle_the_enabled_line(self):
        await self.run_cmd("/heartbeat on")
        self.assertIn("enabled: true", self._config_text().split("tasks:")[0])
        await self.run_cmd("/heartbeat off")
        self.assertIn("enabled: false", self._config_text().split("tasks:")[0])

    async def test_log_shows_history_for_one_job_or_all(self):
        self.t.record_run("heartbeat", "Sabah post", status="ok", job_id="post_slot_0905", summary="Post yayinlandi")
        self.t.record_run("heartbeat", "Yorum", status="error", job_id="comment_20m", error="503")
        self.t.record_run("telegram", "sohbet", status="ok")
        one = await self.run_cmd("/heartbeat log post_slot_0905")
        self.assertIn("Sabah post", one)
        self.assertIn("Post yayinlandi", one)  # ozet gorev bazli gecmiste gosterilir
        self.assertNotIn("Yorum", one)
        every = await self.run_cmd("/heartbeat log")
        self.assertIn("Yorum", every)
        self.assertIn("! 503", every)
        self.assertNotIn("sohbet", every)  # yalnizca heartbeat kaynakli kosular
        self.assertIn("Kullanim", await self.run_cmd("/heartbeat nonsense"))


# --------------------------------------------------------------------- gozlemlenebilirlik

class ObservabilityCommandTests(TerminalCase):
    def _seed(self):
        u = lambda p, c: SimpleNamespace(prompt_tokens=p, completion_tokens=c, total_tokens=p + c)
        with self.t.run_scope("heartbeat", "Sabah post", job_id="j1") as run:
            self.t.record_event("sistem", "context okundu")
            self.t.record_usage("base", "gemini-2.5-flash", u(12000, 800))
            self.t.record_usage("sosyal_medya_agent", "gemma", u(30000, 2000))
            run.set_summary("Post yayinlandi")
        try:
            with self.t.run_scope("heartbeat", "Yenileme", job_id="j2"):
                raise RuntimeError("503 overloaded")
        except RuntimeError:
            pass
        return self.t.recent_runs(10, job_id="j1")[0]["run_id"]

    async def test_errors_status_and_banner_surface_studio_errors(self):
        self.term.base.agent_studio_errors = [
            {"scope": "agents", "name": "rapor", "message": "Bilinmeyen tool atlandi: yok"},
            {"scope": "tools", "message": "Custom tool yuklenemedi: bozuk.py"},
        ]
        errors = await self.run_cmd("/errors")
        self.assertIn("Config hatalari (2)", errors)
        self.assertIn("Bilinmeyen tool atlandi: yok", errors)
        self.assertIn("Config hatalari (1)", await self.run_cmd("/errors bozuk"))
        self.assertIn("2 hata", await self.run_cmd("/status"))
        self.term.out.clear()
        self.term.manager._print_banner()
        self.assertIn("2 config hatasi", "\n".join(self.term.out))
        self.term.base.agent_studio_errors = []
        self.assertIn("Config     : temiz", await self.run_cmd("/status"))

    async def test_agent_show_lists_that_agents_errors(self):
        await self.run_cmd("/agent create hatali --tools workspace_oku")
        self.term.base.agent_studio_errors = [{"scope": "agents", "name": "hatali", "message": "Bilinmeyen tool atlandi: x"}]
        self.assertIn("Hata      : Bilinmeyen tool atlandi: x", await self.run_cmd("/agent show hatali"))

    async def test_status_reports_store_health_and_todays_usage(self):
        self._seed()
        out = await self.run_cmd("/status")
        self.assertIn("Kayit      : 1 olay, 2 kosu", out)
        self.assertIn("Bugun      : 2 LLM cagrisi", out)
        self.t.DB_PATH = "/proc/yok/telemetry.sqlite"
        self.t.close()
        self.t.record_event("x", "y")  # yazma basarisiz olur
        self.assertIn("Kayit      : HATA", await self.run_cmd("/status"))

    async def test_runs_lists_filters_and_shows_errors(self):
        self._seed()
        out = await self.run_cmd("/runs")
        self.assertIn("Kosular (2)", out)
        self.assertIn("! 503 overloaded", out)
        self.assertIn("Kosular (1)", await self.run_cmd("/runs --status error"))
        self.assertIn("Kosular (0)", await self.run_cmd("/runs --source telegram"))
        self.assertIn("Gecersiz zaman", await self.run_cmd("/runs --since dun"))
        self.assertIn("Kullanim", await self.run_cmd("/runs abc"))

    async def test_run_details_by_id_prefix(self):
        run_id = self._seed()
        out = await self.run_cmd(f"/run {run_id[:6]}")
        for expected in ("Kaynak    : heartbeat", "Job       : j1", "Ozet      : Post yayinlandi",
                         "45k token", "sosyal_medya_agent", "context okundu"):
            self.assertIn(expected, out)
        self.assertIn("bulunamadi", await self.run_cmd("/run ffffffffffff"))
        self.assertIn("Kullanim", await self.run_cmd("/run"))

    async def test_usage_groupings_pricing_and_notes(self):
        self._seed()
        out = await self.run_cmd("/usage")
        self.assertIn("agent bazinda", out)
        self.assertIn("sosyal_medya_agent", out)
        self.assertIn("config/pricing.yaml", out)  # fiyat dosyasi yokken nasil tanimlanacagini soyler
        self.assertNotIn("~USD", out)
        self.assertIn("model bazinda", await self.run_cmd("/usage --by model"))
        self.assertIn("(kosu disi)", await self._usage_outside_a_run())
        (CONFIG_DIR / "pricing.yaml").write_text("models:\n  gemini-2.5-flash: {input: 0.30, output: 2.50}\n", encoding="utf-8")
        priced = await self.run_cmd("/usage --by model")
        self.assertIn("~USD", priced)
        self.assertIn("fiyati tanimli olmayan modeller maliyete dahil degil: gemma", priced)
        self.assertIn("Gecersiz zaman", await self.run_cmd("/usage --since dun"))
        self.assertIn("agent|model|source|day|run", await self.run_cmd("/usage --by yok"))

    async def _usage_outside_a_run(self):
        self.t.record_usage("base", "m", SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2))
        return await self.run_cmd("/usage --by source")

    async def test_usage_empty_and_live_agent_note(self):
        self.assertIn("kayitli LLM cagrisi yok", await self.run_cmd("/usage"))
        self.t.record_usage("arastirma_agent", "gemini-live", SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2))
        self.assertIn("Gemini Live ajanlari", await self.run_cmd("/usage"))

    async def test_logs_persist_filter_and_fall_back_to_memory(self):
        self.t.record_event("sistem", "ilk")
        self.t.record_event("hata", "kirmizi alarm")
        self.t.record_event("sistem", "son")
        out = await self.run_cmd("/logs 2")
        self.assertIn("Loglar (2)", out)
        self.assertNotIn("ilk", out)
        self.assertIn("kirmizi alarm", await self.run_cmd("/logs --grep alarm"))
        self.assertNotIn("kirmizi", await self.run_cmd("/logs --type sistem"))
        self.assertIn("Eslesen kayit yok", await self.run_cmd("/logs --grep yokboyle"))
        self.term.base.logs.append({"time": "12:00:00", "type": "log", "message": "bellek satiri"})
        memory = await self.run_cmd("/logs --memory")
        self.assertIn("Bellekteki loglar", memory)
        self.assertIn("bellek satiri", memory)

    async def test_logs_can_be_scoped_to_one_run(self):
        run_id = self._seed()
        self.t.record_event("sistem", "kosu disi satir")
        out = await self.run_cmd(f"/logs --run {run_id[:6]}")
        self.assertIn("context okundu", out)
        self.assertNotIn("kosu disi satir", out)
        self.assertIn("tek bir kosu eslesmeli", await self.run_cmd("/logs --run ffffffffffff"))

    async def test_chat_is_recorded_as_a_terminal_run(self):
        async def text_query(user_text, **kwargs):
            self.t.record_usage("base", "m", SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15))
            return b"", "merhaba", [], ["merhaba"]

        self.term.base.text_query = text_query
        await self.run_cmd("bugun ne paylasalim?")
        run = self.t.recent_runs(1)[0]
        self.assertEqual((run["source"], run["status"], run["total_tokens"], run["label"]), ("terminal", "ok", 15, "bugun ne paylasalim?"))

    async def test_a_failing_chat_is_recorded_as_error_and_reported(self):
        async def text_query(user_text, **kwargs):
            raise RuntimeError("model coktu")

        self.term.base.text_query = text_query
        out = await self.run_cmd("merhaba")
        self.assertIn("Model hatasi: model coktu", out)
        run = self.t.recent_runs(1)[0]
        self.assertEqual((run["status"], run["error"]), ("error", "model coktu"))


class HelpAndImportTests(TerminalCase):
    async def test_help_documents_every_command_family(self):
        out = await self.run_cmd("/help")
        for expected in ("/agent create", "/agent copy", "/agent test", "/agent pack export", "/tool create", "--names", "--all",
                         "/heartbeat add", "/heartbeat log", "/errors", "/runs", "/run <id>", "/usage", "/logs", "--dry-run"):
            self.assertIn(expected, out, expected)

    async def test_unknown_command_and_bad_quoting_do_not_crash(self):
        self.assertIn("Bilinmeyen komut", await self.run_cmd("/nope"))
        self.assertIn("Komut okunamadi", await self.run_cmd('/agent create x --desc "acik tirnak'))

    def test_importing_the_terminal_module_does_not_boot_the_llm_package(self):
        import os

        code = "import sys, MarketingApp.environments.terminal; sys.exit(1 if 'MarketingApp.llms' in sys.modules else 0)"
        result = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=os.environ.copy(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
