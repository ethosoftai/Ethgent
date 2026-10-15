"""
Araçlar — Tüm core tool'ların merkezi import noktası.

Tool grupları:
  BASE_ARACLAR      → BaseModel'in doğrudan kullandığı
  SISTEM_ARACLARI   → sistem_agent (workspace, terminal, bellek)
  ARAMA_ARACLARI    → arastirma_agent (web araması + workspace)
  KOD_ARACLARI      → kod_agent (terminal, workspace)
  BROWSER_ARACLARI  → browser_agent (Selenium web etkileşimi)
"""

from .sistem_araclari import (
    get_system_status as sistem_get_system_status,
    terminal_komut_calistir,
)
from .workspace_araclari import (
    context_aksiyon_kaydet,
    context_paketi_oku,
    workspace_yaz,
    workspace_oku,
    workspace_sonunu_oku,
    workspace_listele,
    workspace_sil,
    workspace_ekle,
)
from .bellek_araclari import bellek_yaz, bellek_oku, bellek_sil, rol_oku, rol_guncelle
from .content_creator_araclari import (
    website_icerik_cikar,
    website_iceriginden_post_paketi_uret,
    pexels_fotograf_ara,
    pexels_curated_fotograflar,
    pexels_fotograf_detay,
    pexels_video_ara,
    pexels_populer_videolar,
    video_post_olustur_ve_mp4_kaydet,
    html_css_post_olustur_ve_png_kaydet,
)
from .worker_yayinlama_araclari import (
    worker_video_yayinla,
    worker_instagram_carousel_yayinla,
    worker_yayin_durumu_sorgula,
)
from .app_asset_araclari import (
    app_baglanti_durumu,
    app_asset_listele,
    app_asset_detay,
)
from .medya_araclari import (
    medya_dogrula,
    medya_hazirla,
)
from .video_uretim_araclari import (
    video_uretimi_iste,
    video_uretimi_durumu,
)
from .platform_veri_araclari import (
    platform_veri_eylemleri,
    platform_veri_topla,
    platform_veri_durumu,
)


def _missing_tool(tool_name: str, error: Exception):
    """Opsiyonel bağımlılık eksik olduğunda yer tutucu tool üretir."""

    def _tool(*args, **kwargs):
        return f"❌ '{tool_name}' aracı kullanılamıyor: {error}"

    _tool.__name__ = tool_name
    _tool.__doc__ = f"Opsiyonel bağımlılık eksik olduğu için geçici olarak devre dışı: {error}"
    return _tool


try:
    from .arama_araclari import web_arama
except Exception as arama_import_error:
    web_arama = _missing_tool("web_arama", arama_import_error)


try:
    from .vlm_araclari import (
        take_screenshot,
        click_mouse,
        type_text,
        press_key,
        scroll_up,
        scroll_down,
        double_click_mouse,
        right_click_mouse,
        get_system_status as vlm_get_system_status,
        get_screenshot_bytes,
        look_closer,
        hover_mouse,
        get_pixel_color,
    )
except Exception as vlm_import_error:
    take_screenshot = _missing_tool("take_screenshot", vlm_import_error)
    click_mouse = _missing_tool("click_mouse", vlm_import_error)
    type_text = _missing_tool("type_text", vlm_import_error)
    press_key = _missing_tool("press_key", vlm_import_error)
    scroll_up = _missing_tool("scroll_up", vlm_import_error)
    scroll_down = _missing_tool("scroll_down", vlm_import_error)
    double_click_mouse = _missing_tool("double_click_mouse", vlm_import_error)
    right_click_mouse = _missing_tool("right_click_mouse", vlm_import_error)
    vlm_get_system_status = _missing_tool("get_system_status", vlm_import_error)
    get_screenshot_bytes = _missing_tool("get_screenshot_bytes", vlm_import_error)
    look_closer = _missing_tool("look_closer", vlm_import_error)
    hover_mouse = _missing_tool("hover_mouse", vlm_import_error)
    get_pixel_color = _missing_tool("get_pixel_color", vlm_import_error)


try:
    from .social_browser_workflow import (
        launch_social_browser,
        close_social_browser,
        open_social_page,
        launch_x_browser,
        close_x_browser,
        open_x_page,
        get_x_queue,
        get_browser_status,
        scan_x_page,
        scan_x_notifications,
        snapshot_x_feed,
        save_x_market_snapshot,
        search_x_posts,
        get_x_trends,
        inspect_x_profile,
        inspect_x_post,
        like_x_post,
        bookmark_x_post,
        repost_x_post,
        quote_x_post,
        follow_x_account,
        engage_with_x_post,
        update_queue_item,
        send_x_reply,
        publish_x_post,
        publish_x_post_with_media,
        submit_current_x_composer,
        verify_current_x_submission,
        resolve_recent_x_status_url,
        publish_x_thread,
        reply_to_x_post,
        mark_queue_item,
        inspect_instagram_profile,
        inspect_instagram_post,
        like_instagram_post,
        follow_instagram_account,
        comment_instagram_post,
        search_youtube_videos,
        inspect_youtube_channel,
        inspect_youtube_video,
        like_youtube_video,
        subscribe_youtube_channel,
    )
except Exception as social_import_error:
    launch_social_browser = _missing_tool("launch_social_browser", social_import_error)
    close_social_browser = _missing_tool("close_social_browser", social_import_error)
    open_social_page = _missing_tool("open_social_page", social_import_error)
    launch_x_browser = _missing_tool("launch_x_browser", social_import_error)
    close_x_browser = _missing_tool("close_x_browser", social_import_error)
    open_x_page = _missing_tool("open_x_page", social_import_error)
    get_x_queue = _missing_tool("get_x_queue", social_import_error)
    get_browser_status = _missing_tool("get_browser_status", social_import_error)
    scan_x_page = _missing_tool("scan_x_page", social_import_error)
    scan_x_notifications = _missing_tool("scan_x_notifications", social_import_error)
    snapshot_x_feed = _missing_tool("snapshot_x_feed", social_import_error)
    save_x_market_snapshot = _missing_tool("save_x_market_snapshot", social_import_error)
    search_x_posts = _missing_tool("search_x_posts", social_import_error)
    get_x_trends = _missing_tool("get_x_trends", social_import_error)
    inspect_x_profile = _missing_tool("inspect_x_profile", social_import_error)
    inspect_x_post = _missing_tool("inspect_x_post", social_import_error)
    like_x_post = _missing_tool("like_x_post", social_import_error)
    bookmark_x_post = _missing_tool("bookmark_x_post", social_import_error)
    repost_x_post = _missing_tool("repost_x_post", social_import_error)
    quote_x_post = _missing_tool("quote_x_post", social_import_error)
    follow_x_account = _missing_tool("follow_x_account", social_import_error)
    engage_with_x_post = _missing_tool("engage_with_x_post", social_import_error)
    update_queue_item = _missing_tool("update_queue_item", social_import_error)
    send_x_reply = _missing_tool("send_x_reply", social_import_error)
    publish_x_post = _missing_tool("publish_x_post", social_import_error)
    publish_x_post_with_media = _missing_tool("publish_x_post_with_media", social_import_error)
    submit_current_x_composer = _missing_tool("submit_current_x_composer", social_import_error)
    verify_current_x_submission = _missing_tool("verify_current_x_submission", social_import_error)
    resolve_recent_x_status_url = _missing_tool("resolve_recent_x_status_url", social_import_error)
    publish_x_thread = _missing_tool("publish_x_thread", social_import_error)
    reply_to_x_post = _missing_tool("reply_to_x_post", social_import_error)
    mark_queue_item = _missing_tool("mark_queue_item", social_import_error)
    inspect_instagram_profile = _missing_tool("inspect_instagram_profile", social_import_error)
    inspect_instagram_post = _missing_tool("inspect_instagram_post", social_import_error)
    like_instagram_post = _missing_tool("like_instagram_post", social_import_error)
    follow_instagram_account = _missing_tool("follow_instagram_account", social_import_error)
    comment_instagram_post = _missing_tool("comment_instagram_post", social_import_error)
    search_youtube_videos = _missing_tool("search_youtube_videos", social_import_error)
    inspect_youtube_channel = _missing_tool("inspect_youtube_channel", social_import_error)
    inspect_youtube_video = _missing_tool("inspect_youtube_video", social_import_error)
    like_youtube_video = _missing_tool("like_youtube_video", social_import_error)
    subscribe_youtube_channel = _missing_tool("subscribe_youtube_channel", social_import_error)


try:
    from .browser_araclari import (
        browser_baslat,
        browser_baglan,
        browser_chrome_baslat,
        browser_git,
        browser_dom_oku,
        browser_hizli_durum_oku,
        browser_ilgili_bolumleri_getir,
        browser_bul,
        browser_click_text,
        browser_click_role,
        browser_click_id,
        browser_click_css,
        browser_type_placeholder,
        browser_type_id,
        browser_type_css,
        browser_enter_bas,
        browser_geri,
        browser_scroll,
        browser_select_sec,
        browser_deger_ata,
        browser_eleman_bekle,
        browser_bekle,
        browser_cookie_kaydet,
        browser_cookie_yukle,
        browser_yeni_sekme,
        browser_sekme_degistir,
        browser_sekme_listele,
        browser_sekme_kapat,
        browser_dosya_yukle,
        browser_screenshot,
        browser_vlm_yardim,
        browser_kapat,
    )
except Exception as browser_import_error:
    browser_baslat = _missing_tool("browser_baslat", browser_import_error)
    browser_baglan = _missing_tool("browser_baglan", browser_import_error)
    browser_chrome_baslat = _missing_tool("browser_chrome_baslat", browser_import_error)
    browser_git = _missing_tool("browser_git", browser_import_error)
    browser_dom_oku = _missing_tool("browser_dom_oku", browser_import_error)
    browser_hizli_durum_oku = _missing_tool("browser_hizli_durum_oku", browser_import_error)
    browser_ilgili_bolumleri_getir = _missing_tool("browser_ilgili_bolumleri_getir", browser_import_error)
    browser_bul = _missing_tool("browser_bul", browser_import_error)
    browser_click_text = _missing_tool("browser_click_text", browser_import_error)
    browser_click_role = _missing_tool("browser_click_role", browser_import_error)
    browser_click_id = _missing_tool("browser_click_id", browser_import_error)
    browser_click_css = _missing_tool("browser_click_css", browser_import_error)
    browser_type_placeholder = _missing_tool("browser_type_placeholder", browser_import_error)
    browser_type_id = _missing_tool("browser_type_id", browser_import_error)
    browser_type_css = _missing_tool("browser_type_css", browser_import_error)
    browser_enter_bas = _missing_tool("browser_enter_bas", browser_import_error)
    browser_geri = _missing_tool("browser_geri", browser_import_error)
    browser_scroll = _missing_tool("browser_scroll", browser_import_error)
    browser_select_sec = _missing_tool("browser_select_sec", browser_import_error)
    browser_deger_ata = _missing_tool("browser_deger_ata", browser_import_error)
    browser_eleman_bekle = _missing_tool("browser_eleman_bekle", browser_import_error)
    browser_bekle = _missing_tool("browser_bekle", browser_import_error)
    browser_cookie_kaydet = _missing_tool("browser_cookie_kaydet", browser_import_error)
    browser_cookie_yukle = _missing_tool("browser_cookie_yukle", browser_import_error)
    browser_yeni_sekme = _missing_tool("browser_yeni_sekme", browser_import_error)
    browser_sekme_degistir = _missing_tool("browser_sekme_degistir", browser_import_error)
    browser_sekme_listele = _missing_tool("browser_sekme_listele", browser_import_error)
    browser_sekme_kapat = _missing_tool("browser_sekme_kapat", browser_import_error)
    browser_dosya_yukle = _missing_tool("browser_dosya_yukle", browser_import_error)
    browser_screenshot = _missing_tool("browser_screenshot", browser_import_error)
    browser_vlm_yardim = _missing_tool("browser_vlm_yardim", browser_import_error)
    browser_kapat = _missing_tool("browser_kapat", browser_import_error)


try:
    from .skill_loader import load_skills, get_active_skills, list_skills
except Exception as skill_loader_import_error:
    _SKILL_LOADER_IMPORT_ERROR = skill_loader_import_error

    def load_skills() -> dict[str, dict]:
        print(f"⚠️ [Skill Loader] Devre dışı: {_SKILL_LOADER_IMPORT_ERROR}")
        return {}

    def get_active_skills(agent: str = None) -> list:
        return []

    def list_skills() -> list[dict]:
        return []


BASE_ARACLAR = [
    terminal_komut_calistir,
    context_paketi_oku,
    context_aksiyon_kaydet,
    workspace_yaz,
    workspace_oku,
    workspace_sonunu_oku,
    workspace_listele,
    rol_oku,
    rol_guncelle,
]

SOSYAL_MEDYA_ARACLARI = [
    # --- Tarayici yonetimi ---
    launch_social_browser,
    close_social_browser,
    open_social_page,
    # --- X (Twitter) ---
    launch_x_browser,
    close_x_browser,
    open_x_page,
    get_browser_status,
    get_x_queue,
    scan_x_page,
    scan_x_notifications,
    snapshot_x_feed,
    save_x_market_snapshot,
    search_x_posts,
    get_x_trends,
    inspect_x_profile,
    inspect_x_post,
    like_x_post,
    bookmark_x_post,
    repost_x_post,
    quote_x_post,
    follow_x_account,
    engage_with_x_post,
    update_queue_item,
    send_x_reply,
    publish_x_post,
    publish_x_post_with_media,
    submit_current_x_composer,
    verify_current_x_submission,
    resolve_recent_x_status_url,
    publish_x_thread,
    reply_to_x_post,
    mark_queue_item,
    # --- Instagram ---
    inspect_instagram_profile,
    inspect_instagram_post,
    like_instagram_post,
    follow_instagram_account,
    comment_instagram_post,
    # --- YouTube ---
    search_youtube_videos,
    inspect_youtube_channel,
    inspect_youtube_video,
    like_youtube_video,
    subscribe_youtube_channel,
    # --- Worker uzerinden video/carousel yayinlama (social-media-worker) ---
    worker_video_yayinla,
    worker_instagram_carousel_yayinla,
    worker_yayin_durumu_sorgula,
    # --- Yayin oncesi medya hazirlama (media_ref uretir; imzali URL modele gosterilmez) ---
    medya_dogrula,
    medya_hazirla,
    # --- Workspace (strateji/log dosyalari icin) ---
    context_paketi_oku,
    context_aksiyon_kaydet,
    workspace_oku,
    workspace_sonunu_oku,
    workspace_yaz,
    workspace_ekle,
    workspace_listele,
    # --- Browser fallback (son post/reply tusu, DOM teyidi, kurtarma) ---
    browser_baglan,
    browser_git,
    browser_dom_oku,
    browser_hizli_durum_oku,
    browser_ilgili_bolumleri_getir,
    browser_bul,
    browser_click_text,
    browser_click_role,
    browser_click_id,
    browser_click_css,
    browser_type_placeholder,
    browser_type_id,
    browser_type_css,
    browser_enter_bas,
    browser_scroll,
    browser_eleman_bekle,
    browser_bekle,
    browser_sekme_listele,
    browser_sekme_degistir,
    browser_screenshot,
]

SISTEM_ARACLARI = [
    terminal_komut_calistir,
    bellek_yaz,
    bellek_oku,
    bellek_sil,
    context_paketi_oku,
    context_aksiyon_kaydet,
    workspace_yaz,
    workspace_oku,
    workspace_sonunu_oku,
    workspace_ekle,
    workspace_listele,
    rol_oku,
    sistem_get_system_status,
]

ARAMA_ARACLARI = [
    web_arama,
    context_paketi_oku,
    context_aksiyon_kaydet,
    workspace_oku,
    workspace_sonunu_oku,
    workspace_yaz,
    workspace_ekle,
    workspace_listele,
]

KOD_ARACLARI = [
    terminal_komut_calistir,
    context_paketi_oku,
    context_aksiyon_kaydet,
    workspace_yaz,
    workspace_oku,
    workspace_sonunu_oku,
    workspace_listele,
]

VLM_ARACLARI = [
    take_screenshot,
    press_key,
    scroll_up,
    scroll_down,
    vlm_get_system_status,
    get_screenshot_bytes,
    look_closer,
    hover_mouse,
    get_pixel_color,
]

CONTENT_CREATOR_ARACLARI = [
    website_icerik_cikar,
    website_iceriginden_post_paketi_uret,
    pexels_fotograf_ara,
    pexels_curated_fotograflar,
    pexels_fotograf_detay,
    pexels_video_ara,
    pexels_populer_videolar,
    video_post_olustur_ve_mp4_kaydet,
    html_css_post_olustur_ve_png_kaydet,
    rol_oku,
    bellek_oku,
    web_arama,
    context_paketi_oku,
    context_aksiyon_kaydet,
    workspace_oku,
    workspace_sonunu_oku,
    workspace_yaz,
    workspace_ekle,
    workspace_listele,
]

ASSET_COLLECTOR_ARACLARI = [
    app_baglanti_durumu,
    app_asset_listele,
    app_asset_detay,
    medya_dogrula,
    medya_hazirla,
    video_uretimi_iste,
    video_uretimi_durumu,
    context_paketi_oku,
    context_aksiyon_kaydet,
    workspace_oku,
    workspace_sonunu_oku,
    workspace_yaz,
    workspace_ekle,
    workspace_listele,
]

PLATFORM_DATA_ARACLARI = [
    platform_veri_eylemleri,
    platform_veri_topla,
    platform_veri_durumu,
    context_paketi_oku,
    context_aksiyon_kaydet,
    workspace_oku,
    workspace_sonunu_oku,
    workspace_yaz,
    workspace_ekle,
    workspace_listele,
]

GENERIC_BROWSER_TOOLS_ACTIVE = True

BROWSER_ARACLARI = [] if not GENERIC_BROWSER_TOOLS_ACTIVE else [
    browser_baslat,
    browser_baglan,
    browser_chrome_baslat,
    browser_git,
    browser_dom_oku,
    browser_hizli_durum_oku,
    browser_ilgili_bolumleri_getir,
    browser_bul,
    browser_click_text,
    browser_click_role,
    browser_click_id,
    browser_click_css,
    browser_type_placeholder,
    browser_type_id,
    browser_type_css,
    browser_enter_bas,
    browser_geri,
    browser_scroll,
    browser_select_sec,
    browser_deger_ata,
    browser_eleman_bekle,
    browser_bekle,
    browser_cookie_kaydet,
    browser_cookie_yukle,
    browser_yeni_sekme,
    browser_sekme_degistir,
    browser_sekme_listele,
    browser_sekme_kapat,
    browser_dosya_yukle,
    browser_screenshot,
    browser_vlm_yardim,
    browser_kapat,
]

TUM_ARACLAR = BASE_ARACLAR

_loaded_skills = load_skills()

_AGENT_TOOL_MAP = {
    "base": BASE_ARACLAR,
    "sosyal_medya_agent": SOSYAL_MEDYA_ARACLARI,
    "content_creator_agent": CONTENT_CREATOR_ARACLARI,
    "asset_collector_agent": ASSET_COLLECTOR_ARACLARI,
    "platform_data_agent": PLATFORM_DATA_ARACLARI,
    "sistem_agent": SISTEM_ARACLARI,
    "arastirma_agent": ARAMA_ARACLARI,
    "kod_agent": KOD_ARACLARI,
    "vlm_agent": VLM_ARACLARI,
    "browser_agent": BROWSER_ARACLARI if GENERIC_BROWSER_TOOLS_ACTIVE else None,
}

for agent_name, tool_list in _AGENT_TOOL_MAP.items():
    if tool_list is None:
        continue
    skills = get_active_skills(agent=agent_name)
    if skills:
        tool_list.extend(skills)
        print(f"🧩 [Boot] {agent_name} için {len(skills)} skill enjekte edildi.")
