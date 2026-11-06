"""Dashboard Streamlit FB Workspace — alur tiga langkah.

Struktur UI sengaja dibuat sederhana:

    Tab 1 "Mulai"       : ① sumber data → ② filter → ③ jalankan
    Tab 2 "Hasil"       : tabel, detail, ekspor, terapkan ulang filter
    Tab 3 "Pengaturan"  : Ollama, profil bisnis, data, bantuan

Sidebar hanya berisi ringkasan & keamanan (bukan filter), supaya tidak ada dua
tempat pengaturan yang membingungkan.

Logika bisnis tetap di modul inti (db, filters, analyzer, pipeline, export).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from . import jobs
from .analyzer import analyze_text
from .browser.page_state import detect_page_state
from .browser.session import LOGIN_STATUS_LABELS, read_login_marker
from .config import PROJECT_ROOT, Settings, get_settings
from .db import Database, human_time
from .demo import load_demo_posts
from .drafts import BusinessProfile, build_draft, contains_forbidden_claim
from .export import rows_to_csv_bytes, suggested_filename
from .filters import config_from_ui, config_to_json
from .models import (
    ACCESS_STATUS_MESSAGES,
    UNKNOWN,
    WORKFLOW_STATUS_LABELS,
    FilterConfig,
    JobStatus,
)
from .normalize import parse_group_urls
from .ollama_client import (
    SUGGESTED_MODELS,
    OllamaConfig,
    analyze_post,
    check_connection,
    summarize_config,
)
from .pipeline import (
    ingest_manual_entries,
    parse_manual_input,
    preview_filter_counts,
    reapply_filters,
)

PROFILE_PATH = PROJECT_ROOT / "data" / "business_profile.json"
ACTIVE_STATUSES = {JobStatus.QUEUED.value, JobStatus.RUNNING.value}

FILTER_DEFAULTS: dict[str, object] = {
    "fbw_include": "",
    "fbw_exclude": "",
    "fbw_locations": "",
    "fbw_price_on": False,
    "fbw_price_min": 0,
    "fbw_price_max": 0,
    "fbw_require_iphone": True,
    "fbw_strict": False,
}

CATEGORY_BADGES = {
    "mencari": "🟢 mencari",
    "menjual": "🔵 menjual",
    "diskusi": "🟡 diskusi",
    "tidak relevan": "⚪ tidak relevan",
}
STATUS_BADGES = {
    "baru": "🆕 baru",
    "diperiksa": "👀 diperiksa",
    "relevan": "✅ relevan",
    "diabaikan": "🚫 diabaikan",
}
SOURCE_LABELS = {
    "browser": "Browser",
    "manual": "Manual",
    "demo": "DEMO",
    "file": "File",
}
JOB_STATUS_LABELS = {
    JobStatus.QUEUED.value: "⏳ Menunggu",
    JobStatus.RUNNING.value: "🔄 Berjalan",
    JobStatus.COMPLETED.value: "✅ Selesai",
    JobStatus.STOPPED.value: "🛑 Dihentikan",
    JobStatus.FAILED.value: "❌ Gagal",
    JobStatus.BLOCKED.value: "⛔ Diblokir / tidak diizinkan",
}


# ---------------------------------------------------------------------------
# Setup & util
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def _get_db(path_str: str) -> Database:
    return Database(Path(path_str))


def get_context() -> tuple[Settings, Database]:
    settings = get_settings()
    settings.ensure_dirs()
    return settings, _get_db(str(settings.db_path))


def _fmt_price(amount) -> str:
    if not amount:
        return UNKNOWN
    try:
        return "Rp" + f"{int(amount):,}".replace(",", ".")
    except (TypeError, ValueError):
        return UNKNOWN


def _escape_html(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _age_seconds(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _load_profile() -> BusinessProfile:
    if PROFILE_PATH.exists():
        try:
            data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
            return BusinessProfile(
                nama_toko=data.get("nama_toko") or "[NAMA TOKO]",
                area=data.get("area") or "[AREA ANDA]",
                jam_respons=data.get("jam_respons") or "[JAM RESPONS]",
            )
        except (json.JSONDecodeError, OSError):
            pass
    return BusinessProfile()


def _save_profile(profile: BusinessProfile) -> None:
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(
        json.dumps(profile.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def active_job_exists(db: Database) -> bool:
    return db.active_job() is not None


def login_helper_running() -> bool:
    proc = st.session_state.get("fbw_login_proc")
    return proc is not None and proc.poll() is None


def _ollama_config(settings: Settings) -> OllamaConfig:
    """Konfigurasi Ollama + model pilihan di tab Pengaturan (berlaku sesi ini)."""
    config = OllamaConfig.from_settings(settings)
    chosen = str(st.session_state.get("fbw_ollama_model_select") or "").strip()
    if chosen:
        config = replace(config, model=chosen)
    return config


def build_filter_config() -> FilterConfig:
    """Bangun FilterConfig dari nilai widget yang sedang aktif di tab Mulai."""
    price_on = bool(st.session_state.get("fbw_price_on", FILTER_DEFAULTS["fbw_price_on"]))
    pmin = int(st.session_state.get("fbw_price_min", 0) or 0)
    pmax = int(st.session_state.get("fbw_price_max", 0) or 0)
    return config_from_ui(
        include_raw=st.session_state.get("fbw_include", ""),
        exclude_raw=st.session_state.get("fbw_exclude", ""),
        locations_raw=st.session_state.get("fbw_locations", ""),
        price_min=pmin if price_on and pmin > 0 else None,
        price_max=pmax if price_on and pmax > 0 else None,
        require_iphone=bool(
            st.session_state.get("fbw_require_iphone", FILTER_DEFAULTS["fbw_require_iphone"])
        ),
    )


def strict_mode() -> bool:
    return bool(st.session_state.get("fbw_strict", FILTER_DEFAULTS["fbw_strict"]))


def _filter_summary(config: FilterConfig) -> str:
    parts = []
    if config.require_iphone:
        parts.append("wajib iPhone")
    if config.include_keywords:
        parts.append(f"{len(config.include_keywords)} kata inklusi")
    if config.exclude_keywords:
        parts.append(f"{len(config.exclude_keywords)} kata eksklusi")
    if config.locations:
        parts.append(f"lokasi: {', '.join(config.locations[:3])}")
    if config.price_min or config.price_max:
        batas = []
        if config.price_min:
            batas.append(f"≥ {_fmt_price(config.price_min)}")
        if config.price_max:
            batas.append(f"≤ {_fmt_price(config.price_max)}")
        parts.append("harga " + " dan ".join(batas))
    if not parts:
        return "tanpa filter"
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Sidebar — ringkasan saja, tanpa filter
# ---------------------------------------------------------------------------

def render_sidebar(settings: Settings, db: Database) -> None:
    stats = db.stats()
    with st.sidebar:
        st.markdown("### 📱 FB Workspace")
        st.caption("Alat bantu lokal · bukan bot")
        st.metric("Postingan tersimpan", stats["total"])
        demo_count = stats["by_source"].get("demo", 0)
        if demo_count:
            st.caption(f"Termasuk {demo_count} data DEMO sintetis")
        st.caption(f"Database: `{settings.db_path.name}`")
        banner = "ya" if settings.host in {"127.0.0.1", "localhost", "::1"} else "TIDAK"
        st.caption(f"Hanya lokal: {banner} · bind `{settings.host}:{settings.port}`")

        st.divider()
        st.markdown("**Alur pemakaian**")
        st.caption("1 · Mulai → 2 · Hasil → 3 · Pengaturan")
        st.caption("Filter ada di dalam langkah ②, bukan di sini.")

        st.divider()
        st.markdown("**Keamanan**")
        st.caption(
            "Data, log, dan sesi login tersimpan di laptop ini. Tidak ada "
            "unggahan ke cloud. Profil browser berisi cookie — jangan dibagikan."
        )
        with st.expander("Berkas dokumentasi"):
            for path in (
                "README.md",
                "docs/INSTALL_WINDOWS.md",
                "docs/POC_FACEBOOK.md",
                "docs/LIMITATIONS.md",
                "docs/GIT_GUIDE.md",
                "docs/TESTING.md",
            ):
                exists = (PROJECT_ROOT / path).exists()
                st.caption(("📄 " if exists else "⚠️ ") + path)

        st.divider()
        with st.expander("🗑️ Hapus data"):
            st.caption("Permanen dan hanya di laptop ini.")
            demo_n = stats["by_source"].get("demo", 0)
            if demo_n and st.button(
                f"Hapus {demo_n} data DEMO", key="sidebar_delete_demo"
            ):
                deleted = db.delete_posts_by_source("demo")
                st.toast(f"{deleted} postingan DEMO dihapus.")
                st.rerun()
            confirm = st.checkbox(
                "Saya yakin menghapus semua postingan", key="sidebar_confirm_all"
            )
            if st.button(
                "Hapus semua postingan",
                key="sidebar_delete_all",
                disabled=not confirm,
            ):
                deleted = db.delete_all_posts()
                st.toast(f"{deleted} postingan dihapus.")
                st.rerun()
            st.caption("Opsi lain: **Pengaturan → 💾 Data**.")

        st.caption("Butuh bantuan? Tab **Pengaturan → Bantuan**.")


# ---------------------------------------------------------------------------
# Banner DEMO
# ---------------------------------------------------------------------------

def render_demo_banner(db: Database) -> None:
    demo_count = db.count_posts(source="demo")
    if not demo_count:
        return
    cols = st.columns([5, 1])
    with cols[0]:
        st.warning(
            f"🟡 **{demo_count} postingan DEMO (sintetis)** ada di database ini. "
            "Data ini bukan hasil Facebook dan hanya untuk latihan."
        )
    with cols[1]:
        if st.button("Hapus DEMO", key="banner_delete_demo", help="Hapus semua data DEMO"):
            deleted = db.delete_posts_by_source("demo")
            st.toast(f"{deleted} postingan DEMO dihapus.")
            st.rerun()


# ---------------------------------------------------------------------------
# Login helper
# ---------------------------------------------------------------------------

@st.fragment(run_every="2s")
def login_live_panel(settings: Settings, marker_path: str) -> None:
    data = read_login_marker(marker_path)
    status = str(data.get("status") or "")
    label = LOGIN_STATUS_LABELS.get(status, "Membuka jendela browser…")
    if status in {"error", "timeout", "selesai_tanpa_login"}:
        st.warning(label + (f" — {data.get('error')}" if data.get("error") else ""))
    else:
        st.info(label)

    proc = st.session_state.get("fbw_login_proc")
    if proc is None or proc.poll() is not None:
        st.rerun()


def render_login_card(settings: Settings, *, job_active: bool, compact: bool = False) -> None:
    marker_path = str(jobs.login_marker_path(settings))
    proc = st.session_state.get("fbw_login_proc")
    alive = proc is not None and proc.poll() is None
    marker = read_login_marker(marker_path)

    with st.container(border=True):
        head, action = st.columns([3, 1])
        head.markdown("**🔐 Login Facebook**")
        head.caption(
            "Sekali saja. Login sendiri di jendela Chromium; sesi disimpan lokal "
            "di `data/browser_profile`."
        )
        if action.button(
            "Buka browser",
            key="fbw_open_login",
            disabled=alive or job_active,
            help="Jendela browser terbuka. Login, lalu tutup jendelanya.",
        ):
            path, new_proc = jobs.start_login_session(settings)
            st.session_state["fbw_login_proc"] = new_proc
            st.session_state["fbw_login_marker"] = str(path)
            st.rerun()

        if alive:
            login_live_panel(settings, st.session_state.get("fbw_login_marker") or marker_path)
        else:
            status = str(marker.get("status") or "")
            if status == "selesai" or marker.get("login_detected"):
                st.success("Sesi login tersimpan. Kalau Facebook minta login lagi, ulangi langkah ini.")
            elif status in {"error", "timeout", "selesai_tanpa_login"}:
                st.warning(
                    LOGIN_STATUS_LABELS.get(status, status)
                    + (f" — {marker.get('error')}" if marker.get("error") else "")
                )
            elif status:
                st.caption(LOGIN_STATUS_LABELS.get(status, status))
            else:
                st.caption("Belum pernah login dari dashboard ini.")

        if not compact:
            st.caption(
                f"Kalau pembacaan menemui login wall, jendela dibiarkan terbuka "
                f"{settings.login_wait_seconds} detik agar Anda bisa login saat itu juga."
            )


# ---------------------------------------------------------------------------
# Panel job
# ---------------------------------------------------------------------------

def _render_job_content(settings: Settings, db: Database, job_id: int, live: bool) -> None:
    job = db.get_job(job_id)
    if not job:
        st.info("Belum ada sesi pembacaan.")
        return

    status = job.get("status") or JobStatus.QUEUED.value
    counters = job.get("counters") or {}
    active = status in ACTIVE_STATUSES

    st.markdown(f"**Status sesi terakhir:** {JOB_STATUS_LABELS.get(status, status)}")
    if job.get("message"):
        st.info(job["message"])

    m = st.columns(4)
    m[0].metric("Postingan terbaca", counters.get("read", 0))
    m[1].metric("Baru disimpan", counters.get("inserted", 0))
    m[2].metric("Duplikat", counters.get("duplicates", 0))
    m[3].metric("Tidak lolos filter", counters.get("filtered_out", 0))

    if active:
        if st.button("🛑 Hentikan proses", type="primary", key=f"stop_{job_id}"):
            db.request_stop(job_id)
            st.toast("Permintaan berhenti dikirim. Worker akan berhenti dengan rapi.")
        st.caption("Proses akan berhenti di batas aman. Tidak ada pengulangan otomatis.")

    events = db.get_events(job_id, limit=200)
    if events:
        with st.expander("Aktivitas terakhir", expanded=active):
            icons = {"info": "ℹ️", "warning": "⚠️", "error": "❌"}
            for event in events[-12:]:
                ts = human_time(event.get("ts"))
                safe = _escape_html(str(event.get("message", "")))
                st.markdown(
                    f"<div style='font-size:0.82rem;margin:2px 0'>"
                    f"<span style='opacity:.6'>{ts}</span> "
                    f"{icons.get(event.get('level'), '•')} {safe}</div>",
                    unsafe_allow_html=True,
                )

    with st.expander("Diagnostik (hanya bila ada masalah)"):
        age = _age_seconds(job.get("heartbeat_at"))
        if active and age is not None and age > 120:
            st.warning(
                f"Tidak ada pembaruan dari worker selama {int(age)} detik. "
                "Proses mungkin macet."
            )
        dcols = st.columns(2)
        if active and dcols[0].button("⛔ Hentikan paksa", key=f"force_{job_id}"):
            st.toast(jobs.force_stop(db, job_id, st.session_state.get("worker_proc")))
            st.rerun()
        if active and dcols[1].button("🩺 Tandai gagal", key=f"fail_{job_id}"):
            jobs.mark_failed(db, job_id, "Ditandai gagal manual karena proses macet.")
            st.rerun()
        log_text = jobs.tail_log(job_id, settings, max_lines=120)
        if log_text:
            st.code(log_text, language="text")
        st.caption(f"Job #{job_id} · konfigurasi: " + jobs.config_preview(job.get("config") or {}))

    if live:
        st.caption("Panel ini diperbarui otomatis setiap 2 detik.")


@st.fragment(run_every="2s")
def job_live_panel(settings: Settings, db: Database, job_id: int) -> None:
    _render_job_content(settings, db, job_id, live=True)


def render_job_panel(settings: Settings, db: Database) -> None:
    job_id = st.session_state.get("job_id")
    if job_id is None:
        latest = db.latest_job()
        job_id = latest["id"] if latest else None
    if job_id is None:
        return

    job = db.get_job(job_id)
    if job and job.get("status") in ACTIVE_STATUSES:
        job_live_panel(settings, db, job_id)
    else:
        _render_job_content(settings, db, job_id, live=False)


# ---------------------------------------------------------------------------
# Tab 1 — Mulai
# ---------------------------------------------------------------------------

def _onboarding_card(settings: Settings, db: Database) -> None:
    if db.count_posts() > 0:
        return
    with st.expander("🚀 Baru di sini? Ikuti 4 langkah ini", expanded=True):
        st.markdown(
            "**1. Coba tanpa Facebook** — muat 9 postingan DEMO sintetis, lalu lihat tab **Hasil**."
        )
        if st.button("Muat data DEMO", key="onboarding_demo", type="secondary"):
            result = load_demo_posts(db, build_filter_config())
            (st.success if result.get("ok") else st.error)(result.get("message"))
            st.rerun()
        st.markdown("**2. Login Facebook sekali** (opsional, agar pembacaan tidak terhenti).")
        st.caption("Gunakan kartu 🔐 Login Facebook di bawah.")
        st.markdown("**3. Uji kelayakan grup dengan POC** — batas kecil, lihat hasilnya dulu.")
        st.code(
            'powershell -ExecutionPolicy Bypass -File .\\scripts\\poc_windows.ps1 '
            '-Url "https://www.facebook.com/groups/ID-GRUP/" -MaxPosts 3',
            language="powershell",
        )
        st.markdown("**4. Baru jalankan pembacaan** dari langkah ③ di bawah.")


def _source_group(settings: Settings, db: Database) -> list[str]:
    st.text_area(
        "Tautan grup (satu per baris)",
        key="fbw_group_urls",
        height=100,
        placeholder="https://www.facebook.com/groups/123456789/",
        label_visibility="collapsed",
    )
    validations = parse_group_urls(st.session_state.get("fbw_group_urls", ""))
    valid: list[str] = []
    for item in validations:
        if item.ok:
            valid.append(item.normalized)
            st.success(f"✅ {item.normalized}")
            if item.warning:
                st.caption(f"⚠️ {item.warning}")
        else:
            st.error(f"❌ {item.message}")
    if not validations:
        st.caption("Tempel tautan grup, contoh: https://www.facebook.com/groups/123456789/")
    st.caption(
        "Validasi hanya memeriksa bentuk tautan, bukan apakah isi grup boleh dibaca. "
        "Tidak ada data anggota yang diambil."
    )
    render_login_card(settings, job_active=active_job_exists(db))
    return valid


def _source_manual(settings: Settings, db: Database) -> None:
    st.caption(
        "Jalur yang selalu tersedia. Salin teks postingan yang boleh Anda lihat, "
        "lalu tempel di sini. Tidak ada browser yang dijalankan."
    )
    with st.expander("Format input"):
        st.markdown(
            "Pisahkan beberapa postingan dengan baris `---`. Baris opsional di awal blok: "
            "`URL:`, `GRUP:`, `WAKTU:`."
        )
        st.code(
            "URL: https://www.facebook.com/groups/123/posts/999/\n"
            "WAKTU: 3 jam lalu\n\n"
            "Dijual iPhone 13 128GB, BH 88%, mulus. Harga 8,5jt COD Tangerang.\n\n"
            "---\n\n"
            "Cari iPhone 12 mini 64GB budget 4jt, area Jakarta.",
            language="text",
        )

    if st.button("Isi contoh DEMO", key="fill_manual_example"):
        st.session_state["fbw_manual_text"] = (
            "URL: https://www.facebook.com/groups/DEMO/posts/1/\n\n"
            "Dijual iPhone 13 128GB, BH 88%, mulus. Harga 8,5jt COD Tangerang."
        )

    st.text_area(
        "Tempel postingan",
        key="fbw_manual_text",
        height=200,
        placeholder="Dijual iPhone 13 128GB ...",
    )
    uploaded = st.file_uploader(
        "Atau unggah file (.txt / .json)", type=["txt", "json"], key="fbw_manual_file"
    )

    entries = parse_manual_input(st.session_state.get("fbw_manual_text", ""))
    if uploaded is not None:
        try:
            content = uploaded.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Gagal membaca file: {exc}")
            content = ""
        if content:
            if uploaded.name.lower().endswith(".json"):
                try:
                    data = json.loads(content)
                    posts = data.get("posts") if isinstance(data, dict) else data
                    from .pipeline import ManualEntry

                    entries = [
                        ManualEntry(
                            text=str(item.get("text") or ""),
                            post_url=item.get("post_url"),
                            group_url=item.get("group_url") or "",
                            posted_at_raw=item.get("posted_at_raw"),
                        )
                        for item in (posts or [])
                        if isinstance(item, dict) and (item.get("text") or "").strip()
                    ]
                except json.JSONDecodeError as exc:
                    st.error(f"JSON tidak valid: {exc}")
            else:
                entries = parse_manual_input(content)

    st.caption(f"{len(entries)} postingan siap disimpan.")
    c1, c2 = st.columns(2)
    save = c1.button("💾 Simpan", type="primary", disabled=not entries, key="manual_save")
    save_ai = c2.button(
        "💾 Simpan + analisis AI", disabled=not entries, key="manual_save_ai"
    )

    if save or save_ai:
        summary = ingest_manual_entries(
            db,
            entries,
            filter_config=build_filter_config(),
            strict_unknown=strict_mode(),
            max_posts=50,
        )
        st.success(
            f"{summary.inserted} baru · {summary.duplicates} duplikat · "
            f"{summary.filtered_out} tidak lolos filter · {summary.errors} gagal"
        )
        for message in summary.messages[:5]:
            st.warning(message)
        if save_ai and summary.inserted:
            _run_ollama_batch(db, settings, limit=min(summary.inserted, 10))

    with st.expander("Cek cepat satu teks (tanpa menyimpan)"):
        preview_text = st.text_area(
            "Teks", height=100, key="fbw_preview_text", label_visibility="collapsed"
        )
        if preview_text.strip():
            result = analyze_text(preview_text)
            cols = st.columns(4)
            cols[0].metric("Kategori", result.category)
            cols[1].metric("Model", result.iphone_model)
            cols[2].metric("Nominal", _fmt_price(result.price_amount))
            cols[3].metric("Skor", result.relevance_score)
            st.caption("Detail hanya diambil bila disebut: " + (
                ", ".join(result.details) if result.details else "tidak ada"
            ))


def _source_demo(db: Database) -> None:
    st.caption(
        "9 postingan sintetis berlabel [DEMO]. Bukan hasil Facebook. Cocok untuk "
        "mencoba filter, analisis, ekspor, dan draf tanpa menyentuh Facebook."
    )
    if st.button("📥 Muat data DEMO", key="demo_source_load", type="secondary"):
        result = load_demo_posts(db, build_filter_config())
        (st.success if result.get("ok") else st.error)(result.get("message"))
        st.rerun()
    st.caption("Setelah dimuat, buka tab **Hasil**.")


def _run_controls(settings: Settings, db: Database, valid_urls: list[str]) -> None:
    c1, c2 = st.columns(2)
    max_posts = c1.slider(
        "Maksimum postingan",
        1,
        max(5, settings.hard_max_posts),
        min(5, settings.hard_max_posts),
        help=f"Pengaman: {settings.hard_max_posts}",
    )
    max_minutes = c2.slider("Batas waktu (menit)", 1, 15, 3)

    analyze_ollama = st.checkbox(
        "Analisis dengan Ollama setelah membaca",
        value=False,
        help="Butuh Ollama lokal yang berjalan. Job tetap sukses walau AI gagal.",
    )
    consent = st.checkbox(
        "Saya memahami kemungkinan gagal/dibatasi dan akan mematuhi Ketentuan Facebook.",
        value=False,
        key="fbw_consent",
    )

    active = db.active_job()
    st.info(
        f"**Akan dijalankan:** {len(valid_urls)} grup · maks {max_posts} postingan · "
        f"{max_minutes} menit · filter: {_filter_summary(build_filter_config())}"
        + (" · Ollama aktif" if analyze_ollama else "")
    )

    disabled_reasons: list[str] = []
    if not valid_urls:
        disabled_reasons.append("belum ada tautan grup valid")
    if not consent:
        disabled_reasons.append("centang pernyataan kesesuaian")
    if active:
        disabled_reasons.append(f"masih ada sesi aktif (#{active['id']})")
    if login_helper_running():
        disabled_reasons.append("jendela login masih terbuka")

    if st.button(
        "▶️ Mulai pembacaan",
        type="primary",
        disabled=bool(disabled_reasons),
        key="start_job",
    ):
        config = jobs.worker_config(
            mode="browser",
            max_posts=max_posts,
            max_seconds=max_minutes * 60,
            filter_json=config_to_json(build_filter_config()),
            strict_unknown=strict_mode(),
            analyze_ollama=analyze_ollama,
        )
        job_id, proc = jobs.start_job(db, settings, group_urls=valid_urls, config=config)
        st.session_state["job_id"] = job_id
        st.session_state["worker_proc"] = proc
        st.rerun()
    if disabled_reasons:
        st.caption("Tombol aktif bila: " + "; ".join(disabled_reasons) + ".")
    else:
        st.caption("Siap dijalankan.")


def _filter_step(db: Database) -> None:
    st.caption(
        "Filter dipakai saat menyimpan data baru. Untuk postingan yang sudah ada, "
        "pakai **Terapkan ulang filter** di tab Hasil."
    )
    c1, c2 = st.columns(2)
    c1.text_area(
        "Kata kunci inklusi (opsional)",
        key="fbw_include",
        height=80,
        placeholder="iphone, cari iphone, iphone 13",
        help="Pisahkan dengan koma atau baris baru.",
    )
    c2.text_area(
        "Kata kunci eksklusi (opsional)",
        key="fbw_exclude",
        height=80,
        placeholder="rusak, mati, hilang, KW",
        help="Postingan yang memuat kata ini digugurkan.",
    )

    c3, c4 = st.columns(2)
    c3.text_input(
        "Lokasi (opsional)",
        key="fbw_locations",
        placeholder="Tangerang, Jakarta Selatan",
        help="Hanya mencocokkan lokasi yang tertulis di teks.",
    )
    with c4:
        st.checkbox("Batasi rentang harga", key="fbw_price_on", value=False)
        if st.session_state.get("fbw_price_on"):
            p1, p2 = st.columns(2)
            p1.number_input(
                "Min (Rp)", min_value=0, value=0, step=500_000, format="%d",
                key="fbw_price_min",
            )
            p2.number_input(
                "Maks (Rp)", min_value=0, value=0, step=500_000, format="%d",
                key="fbw_price_max",
            )
            pmin = int(st.session_state.get("fbw_price_min") or 0)
            pmax = int(st.session_state.get("fbw_price_max") or 0)
            if pmin and pmax and pmin > pmax:
                st.error("Harga minimal lebih besar dari maksimal.")

    c5, c6 = st.columns(2)
    c5.checkbox(
        "Wajib menyebut iPhone",
        key="fbw_require_iphone",
        value=True,
        help="Matikan hanya bila ingin menyimpan semua postingan.",
    )
    c6.checkbox(
        "Mode ketat",
        key="fbw_strict",
        value=False,
        help="Tolak postingan yang harga/lokasinya tidak diketahui.",
    )

    total = db.count_posts()
    if total:
        config = build_filter_config()
        if total <= 300:
            passed, checked = preview_filter_counts(
                db, config, strict_unknown=strict_mode(), limit=300
            )
            st.caption(
                f"Dengan filter ini, **{passed} dari {checked}** postingan tersimpan "
                "akan lolos. Klik **Terapkan ulang filter** di tab Hasil untuk mengubahnya."
            )
        else:
            st.caption(
                f"{total} postingan tersimpan. Pratinjau filter dimatikan karena datanya "
                "banyak; gunakan tab Hasil → Terapkan ulang filter."
            )


def render_mulai(settings: Settings, db: Database) -> None:
    _onboarding_card(settings, db)

    st.markdown("### ① Sumber data")
    mode = st.radio(
        "Pilih sumber",
        options=["grup", "manual", "demo"],
        format_func=lambda value: {
            "grup": "Grup Facebook (browser)",
            "manual": "Tempel manual",
            "demo": "Data DEMO",
        }[value],
        horizontal=True,
        key="fbw_source_mode",
        label_visibility="collapsed",
    )

    valid_urls: list[str] = []
    if mode == "grup":
        valid_urls = _source_group(settings, db)
    elif mode == "manual":
        _source_manual(settings, db)
    else:
        _source_demo(db)

    st.divider()
    st.markdown("### ② Filter")
    _filter_step(db)

    st.divider()
    st.markdown("### ③ Jalankan")
    if mode == "grup":
        _run_controls(settings, db, valid_urls)
    elif mode == "manual":
        st.caption("Mode manual: gunakan tombol Simpan di langkah ①. Tidak ada job browser.")
    else:
        st.caption("Data DEMO sudah cukup; buka tab **Hasil** untuk melihat dan mengolahnya.")

    render_job_panel(settings, db)


# ---------------------------------------------------------------------------
# Tab 2 — Hasil
# ---------------------------------------------------------------------------

def render_hasil(settings: Settings, db: Database, filter_config: FilterConfig) -> None:
    stats = db.stats()
    counts = db.count_by_filter()
    m = st.columns(4)
    m[0].metric("Total", stats["total"])
    m[1].metric("Lolos filter", counts["passed"])
    m[2].metric("Tersembunyi", counts["hidden"])
    m[3].metric("Dianalisis AI", db.count_ai_analysed())

    c1, c2, c3, c4, c5 = st.columns([2.2, 2, 1.4, 1.3, 1.2])
    view = c1.radio(
        "Tampilkan",
        options=["all", "passed", "hidden"],
        format_func=lambda value: {
            "all": f"Semua ({stats['total']})",
            "passed": f"Lolos ({counts['passed']})",
            "hidden": f"Tersembunyi ({counts['hidden']})",
        }[value],
        horizontal=True,
        key="fbw_view",
    )
    search = c2.text_input("Cari", key="fbw_search", placeholder="teks / tautan")
    category = c3.selectbox(
        "Kategori",
        options=["semua", "mencari", "menjual", "diskusi", "tidak relevan"],
        key="fbw_filter_category",
    )
    status = c4.selectbox(
        "Status",
        options=["semua"] + list(WORKFLOW_STATUS_LABELS.keys()),
        format_func=lambda value: "Semua" if value == "semua" else WORKFLOW_STATUS_LABELS[value],
        key="fbw_filter_status",
    )
    source = c5.selectbox(
        "Sumber", options=["semua", "browser", "manual", "demo"], key="fbw_filter_source"
    )

    filter_passed = {"all": None, "passed": True, "hidden": False}[view]
    rows = db.list_posts(
        search=search,
        workflow_status=None if status == "semua" else status,
        category=None if category == "semua" else category,
        source=None if source == "semua" else source,
        filter_passed=filter_passed,
        limit=1000,
    )

    event = None
    if not rows:
        st.info(
            "Tidak ada postingan untuk tampilan ini. "
            "Coba pilih **Semua**, hapus kata pencarian, atau muat data DEMO di tab Mulai."
        )
    else:
        table = [
            {
                "ID": row["id"],
                "Sumber": SOURCE_LABELS.get(row["source"], row["source"]),
                "Kategori": CATEGORY_BADGES.get(
                    row.get("category") or "", row.get("category") or "—"
                ),
                "Model": row.get("iphone_model") or UNKNOWN,
                "Nominal": _fmt_price(row.get("price_amount")),
                "Lokasi": row.get("location") or UNKNOWN,
                "Skor": row.get("relevance_score") or 0,
                "Status": STATUS_BADGES.get(
                    row["workflow_status"], row["workflow_status"]
                ),
                "Ditemukan": human_time(row["found_at"]),
                "Cuplikan": (row["text"][:90] + "…") if len(row["text"]) > 90 else row["text"],
            }
            for row in rows
        ]
        event = st.dataframe(
            table,
            hide_index=True,
            height=380,
            on_select="rerun",
            selection_mode="single-row",
            key="fbw_results_table",
            column_config={
                "ID": st.column_config.NumberColumn(width="small"),
                "Skor": st.column_config.NumberColumn(width="small"),
                "Cuplikan": st.column_config.TextColumn(width="large"),
            },
        )

    st.caption(
        f"{len(rows)} baris ditampilkan"
        + (" (maksimum 1000)" if len(rows) >= 1000 else "")
        + " · klik satu baris untuk detail dan draf."
    )

    if rows:
        st.download_button(
            f"⬇️ Ekspor {len(rows)} baris yang tampil (CSV)",
            data=rows_to_csv_bytes(rows),
            file_name=suggested_filename(),
            mime="text/csv",
            help="Sel yang diawali = + - @ diberi apostrof agar tidak dieksekusi Excel.",
        )

    if rows:
        selected: list[int] = []
        try:
            selection = event.selection if event is not None else None
            selected = list(getattr(selection, "rows", []) or [])
        except Exception:  # noqa: BLE001 - kompatibilitas versi Streamlit
            selected = []
        if selected:
            index = selected[0]
            if 0 <= index < len(rows):
                _render_post_detail(db, int(rows[index]["id"]), settings)
        else:
            st.caption("Belum ada baris dipilih.")

    st.divider()
    with st.expander("🔁 Terapkan ulang filter ke data lama"):
        st.caption(
            "Menghitung ulang kolom filter dari teks yang tersimpan. Hasil analisis "
            "aturan/AI tidak diubah dan tidak ada postingan baru."
        )
        done = st.session_state.pop("fbw_reapply_result", None)
        if done:
            st.success(
                f"Selesai: {done['checked']} diperiksa, {done['passed']} lolos, "
                f"{done['changed']} status diubah."
                + (" Sebagian data lama belum diperiksa (batas 5000 baris)." if done.get("limited") else "")
            )
        total = db.count_posts()
        if total == 0:
            st.caption("Belum ada data.")
        else:
            if total <= 300:
                passed, checked = preview_filter_counts(
                    db, filter_config, strict_unknown=strict_mode(), limit=300
                )
                st.write(
                    f"Filter sekarang: **{passed} dari {checked}** postingan akan lolos "
                    f"(saat ini {counts['passed']} lolos)."
                )
            else:
                st.write(
                    f"{total} postingan tersimpan. Pratinjau dimatikan demi kecepatan; "
                    "tombol di bawah tetap memproses maksimum 5000 baris."
                )
            if st.button("Terapkan ulang sekarang", key="reapply_filters"):
                summary = reapply_filters(
                    db, filter_config, strict_unknown=strict_mode()
                )
                st.session_state["fbw_reapply_result"] = {
                    "checked": summary.checked,
                    "passed": summary.passed,
                    "changed": summary.changed,
                    "limited": summary.limited,
                }
                st.rerun()


def _render_post_detail(db: Database, post_id: int, settings: Settings) -> None:
    post = db.get_post(post_id)
    if not post:
        st.error("Postingan tidak ditemukan.")
        return

    with st.container(border=True):
        if post["source"] == "demo":
            st.caption("🟡 Data DEMO (sintetis), bukan hasil Facebook.")
        meta = st.columns(4)
        meta[0].metric("Kategori", post.get("category") or "belum dianalisis")
        meta[1].metric("Model", post.get("iphone_model") or UNKNOWN)
        meta[2].metric("Nominal", _fmt_price(post.get("price_amount")))
        meta[3].metric("Skor", post.get("relevance_score") or 0)

        st.markdown(
            "<div style='white-space:pre-wrap;background:rgba(127,127,127,.08);"
            "padding:12px;border-radius:8px;max-height:320px;overflow:auto'>"
            + _escape_html(post["text"])
            + "</div>",
            unsafe_allow_html=True,
        )
        st.caption(
            f"Sumber: {SOURCE_LABELS.get(post['source'], post['source'])} · "
            f"ditemukan {human_time(post['found_at'])} · "
            f"waktu posting {human_time(post.get('posted_at'))} "
            f"({post.get('posted_at_precision') or UNKNOWN})"
        )
        links = st.columns(2)
        if post.get("post_url"):
            links[0].markdown(f"🔗 [Buka postingan]({post['post_url']})")
        else:
            links[0].caption("Tautan postingan: tidak diketahui.")
        if post.get("group_url"):
            links[1].markdown(f"👥 [Tautan grup]({post['group_url']})")
        else:
            links[1].caption("Tautan grup: tidak diketahui.")

        if post.get("filter_reasons"):
            with st.expander("Kenapa lolos / tidak lolos filter"):
                for reason in post["filter_reasons"]:
                    st.markdown(f"- {reason}")

        analysis = db.get_analysis(post_id)
        if analysis:
            with st.expander("Detail analisis", expanded=False):
                left, right = st.columns(2)
                with left:
                    st.write("**Jenis nominal:**", analysis.get("price_kind") or UNKNOWN)
                    st.write("**Lokasi:**", analysis.get("location") or UNKNOWN)
                    st.write("**Mesin:**", analysis.get("engine"))
                with right:
                    details = analysis.get("details") or {}
                    if details:
                        st.write("**Detail disebutkan:**")
                        for key, value in details.items():
                            st.markdown(f"- {key}: {value}")
                    else:
                        st.write("**Detail disebutkan:** tidak ada yang eksplisit.")
                st.write("**Alasan skor:**")
                for reason in analysis.get("reasons") or []:
                    st.markdown(f"- {reason}")
                if analysis.get("snippet"):
                    st.caption(f"Cuplikan: “{analysis['snippet']}”")

        st.markdown("**Status pengerjaan**")
        current = post["workflow_status"]
        status_cols = st.columns(4)
        for column, (value, label) in zip(status_cols, WORKFLOW_STATUS_LABELS.items()):
            if column.button(
                ("✅ " if current == value else "") + label,
                key=f"status_{post_id}_{value}",
                use_container_width=True,
            ):
                db.set_workflow_status(post_id, value)
                st.toast(f"Status: {label}")
                st.rerun()

        st.markdown("**Draf respons** — tidak dikirim otomatis")
        draft = build_draft(post, analysis, _load_profile())
        if draft is None:
            st.info("Kategori 'tidak relevan': tidak ada draf yang disarankan.")
        else:
            st.code(draft.body, language=None)
            st.caption("Klik ikon salin di kanan atas. Periksa dan sunting sebelum mengirim manual.")
            if contains_forbidden_claim(draft.body):
                st.error("Draf memuat klaim yang tidak boleh dikarang.")
            for note in draft.notes:
                st.caption(f"• {note}")

        with st.expander("Hapus postingan ini"):
            confirm = st.checkbox("Saya yakin.", key=f"del_confirm_{post_id}")
            if st.button("🗑️ Hapus", key=f"del_{post_id}", disabled=not confirm):
                db.delete_post(post_id)
                st.toast("Postingan dihapus.")
                st.rerun()


# ---------------------------------------------------------------------------
# Tab 3 — Pengaturan
# ---------------------------------------------------------------------------

def _ollama_section(settings: Settings, db: Database) -> None:
    st.markdown("### 🧠 Ollama (opsional)")
    st.caption(
        "Tanpa AI, aplikasi tetap berfungsi penuh. Ollama hanya menambah ekstraksi bahasa."
    )
    config = _ollama_config(settings)
    info = summarize_config(config)

    metrics = st.columns(4)
    metrics[0].metric("Endpoint", info["endpoint"].replace("http://", ""))
    metrics[1].metric("Timeout", info["timeout"])
    metrics[2].metric("Thinking", info["think"])
    metrics[3].metric("Maks token", info["maks_token"])

    cached = st.session_state.get("fbw_ollama_status") or {}
    models: list[str] = list(cached.get("models") or [])
    current = str(st.session_state.get("fbw_ollama_model_select") or settings.ollama_model)
    options = list(dict.fromkeys([current, *models]))
    if st.session_state.get("fbw_ollama_model_select") not in options:
        st.session_state.pop("fbw_ollama_model_select", None)

    st.selectbox(
        "Model untuk sesi ini",
        options=options,
        index=options.index(current) if current in options else 0,
        key="fbw_ollama_model_select",
        help=(
            "Daftar diambil dari Ollama setelah 'Cek koneksi'. Untuk permanen, ubah "
            "FBW_OLLAMA_MODEL di .env. Untuk qwen3, thinking sebaiknya nonaktif "
            "(FBW_OLLAMA_THINK=false)."
        ),
    )

    c1, c2 = st.columns(2)
    if c1.button("🔍 Cek koneksi", key="ollama_check"):
        status = check_connection(_ollama_config(settings))
        st.session_state["fbw_ollama_status"] = {
            "ok": status.ok,
            "message": status.message,
            "models": status.models,
        }
        st.rerun()
    if c2.button("🧠 Analisis batch (maks 10)", key="ollama_batch"):
        _run_ollama_batch(db, settings, limit=10)

    status = st.session_state.get("fbw_ollama_status")
    if status:
        if status["ok"]:
            st.success(status["message"])
        else:
            st.error(status["message"])
        if status["models"]:
            st.caption("Model terpasang: " + ", ".join(status["models"][:10]))
        else:
            st.caption(
                "Belum ada model terpasang. Jalankan di PowerShell: "
                "`ollama pull qwen2.5:3b` lalu cek lagi."
            )

    with st.expander("Model kecil yang disarankan, biaya memori & lisensi"):
        st.dataframe(
            [
                {
                    "Model": model["name"],
                    "Unduhan": model["size"],
                    "Lisensi": model["license"],
                    "Catatan": model["note"],
                }
                for model in SUGGESTED_MODELS
            ],
            hide_index=True,
        )
        st.caption(
            "RAM 16 GB cukup untuk model 3B–4B terkuantisasi, tetapi kecepatan sangat "
            "bergantung CPU/GPU. Tanpa GPU, satu postingan bisa puluhan detik. Timeout "
            "membatasi waktu tunggu dan job tetap sukses walau AI gagal."
        )
        st.code(
            "ollama list\nollama pull qwen2.5:3b\nollama serve",
            language="powershell",
        )
        st.caption(
            "Ubah pengaturan lewat .env lalu restart dashboard: "
            "FBW_OLLAMA_MODEL, FBW_OLLAMA_TIMEOUT, FBW_OLLAMA_THINK, FBW_OLLAMA_NUM_PREDICT."
        )


def _run_ollama_batch(
    db: Database,
    settings: Settings,
    *,
    limit: int,
    only_new: bool = False,
    ids: list[int] | None = None,
) -> None:
    config = _ollama_config(settings)
    status = check_connection(config)
    if not status.ok:
        st.error(status.message)
        return

    if ids:
        posts = [db.get_post(pid) for pid in ids]
        posts = [post for post in posts if post]
    else:
        rows = db.list_posts(limit=5000)
        posts = [
            row
            for row in rows
            if (only_new and row["analysis_status"] == "belum dianalisis")
            or (not only_new and row["analysis_status"] != "dianalisis (Ollama)")
        ]
    posts = posts[:limit]
    if not posts:
        st.info("Tidak ada postingan yang perlu dianalisis.")
        return

    progress = st.progress(0.0, text="Menyiapkan…")
    ok_count = 0
    failures: list[str] = []
    for index, post in enumerate(posts, start=1):
        progress.progress(index / len(posts), text=f"Menganalisis #{post['id']} ({index}/{len(posts)})…")
        result = analyze_post(post["text"], config)
        if result.ok and result.analysis:
            db.save_analysis(post["id"], result.analysis, raw_output=result.raw)
            ok_count += 1
        else:
            db.set_analysis_status(post["id"], "analisis gagal")
            failures.append(f"#{post['id']}: {result.error}")
    progress.empty()
    if ok_count:
        st.success(f"{ok_count} postingan dianalisis dengan `{config.model}`.")
    if failures:
        st.warning("Sebagian gagal:\n" + "\n".join(f"- {item}" for item in failures[:6]))
    if ok_count:
        st.caption("Hasil AI menggantikan analisis aturan untuk postingan tersebut. Periksa ulang.")


def _data_section(settings: Settings, db: Database) -> None:
    st.markdown("### 💾 Data")
    stats = db.stats()
    st.write(
        f"Lokasi: `{stats['db_path']}` · {stats['db_size_kb']} KB · {stats['total']} postingan"
    )
    if stats["by_source"]:
        st.caption("Per sumber: " + ", ".join(f"{k}={v}" for k, v in stats["by_source"].items()))

    c1, c2 = st.columns(2)
    if c1.button("📥 Muat data DEMO", key="settings_load_demo"):
        result = load_demo_posts(db, build_filter_config())
        (st.success if result.get("ok") else st.error)(result.get("message"))
        st.rerun()
    if c2.button(
        "🧹 Hapus data DEMO",
        key="settings_delete_demo",
        disabled=stats["by_source"].get("demo", 0) == 0,
    ):
        deleted = db.delete_posts_by_source("demo")
        st.success(f"{deleted} postingan DEMO dihapus.")
        st.rerun()

    with st.expander("Hapus data (permanen, lokal)"):
        st.caption("Tidak ada salinan di cloud. Tindakan ini tidak bisa dibatalkan.")
        confirmed = st.checkbox("Saya yakin ingin menghapus.", key="del_all_confirm")
        c3, c4, c5 = st.columns(3)
        if c3.button("Semua postingan", key="del_all_posts", disabled=not confirmed):
            deleted = db.delete_all_posts()
            st.success(f"{deleted} postingan dihapus.")
            st.rerun()
        if c4.button("Log & riwayat job", key="del_logs", disabled=not confirmed):
            db.delete_jobs_and_events()
            st.success("Log dan riwayat job dihapus.")
            st.rerun()
        if c5.button("Profil browser (login)", key="del_profile", disabled=not confirmed):
            import shutil

            target = settings.browser_profile_dir
            if target.exists():
                try:
                    shutil.rmtree(target)
                    st.success("Profil browser dihapus. Anda perlu login ulang.")
                except OSError as exc:
                    st.error(f"Gagal menghapus: {exc}. Tutup semua Chromium lalu coba lagi.")
            else:
                st.info("Folder profil browser belum ada.")


def _security_section(settings: Settings, db: Database) -> None:
    st.markdown("### 🔒 Keamanan")
    safety = settings.describe_safety()
    st.dataframe(
        [{"Pengaturan": key, "Nilai": value} for key, value in safety.items()],
        hide_index=True,
    )
    if safety["lokal_saja"] == "ya":
        st.success("Dashboard hanya bisa diakses dari laptop ini (localhost).")
    else:
        st.error("Dashboard TIDAK terikat ke localhost. Set FBW_HOST=127.0.0.1 di .env.")

    st.caption(
        "Tidak ada data yang diunggah. Profil browser berisi cookie sesi — "
        "diperlakukan sebagai rahasia dan tidak masuk Git."
    )


def _profile_section() -> None:
    st.markdown("### 🏪 Profil bisnis (untuk draf)")
    st.caption(
        "Isi hanya identitas umum. Draf tidak memuat klaim stok/harga/garansi; "
        "placeholder wajib Anda ganti sendiri."
    )
    profile = _load_profile()
    c1, c2, c3 = st.columns(3)
    nama = c1.text_input("Nama toko", value=profile.nama_toko, key="fbw_prof_nama")
    area = c2.text_input("Area", value=profile.area, key="fbw_prof_area")
    jam = c3.text_input("Jam respons", value=profile.jam_respons, key="fbw_prof_jam")
    if st.button("💾 Simpan profil", key="save_profile"):
        _save_profile(BusinessProfile(nama, area, jam))
        st.success("Profil disimpan di data/business_profile.json (masuk .gitignore).")


def _help_section(settings: Settings) -> None:
    st.markdown("### ❓ Bantuan & keterbatasan")
    with st.expander("Apa yang alat ini lakukan / tidak lakukan"):
        st.markdown(
            "**Dilakukan:** menyimpan postingan yang Anda lihat sendiri, deduplikasi, "
            "filter kata kunci/lokasi/harga, analisis aturan iPhone, opsional AI lokal, "
            "ekspor CSV, draf balasan untuk dikirim manual.\n\n"
            "**Tidak dilakukan:** menembus CAPTCHA/checkpoint, stealth/proxy, auto-post, "
            "auto-comment, auto-chat, mengumpulkan profil anggota, mengunggah data ke cloud."
        )
    with st.expander("Kenapa ada postingan 'tersembunyi'?"):
        st.markdown(
            "Postingan tetap disimpan walau tidak lolos filter (ditandai tersembunyi). "
            "Pilih **Tersembunyi** di tab Hasil untuk melihatnya, atau longgarkan filter "
            "lalu klik **Terapkan ulang filter**."
        )
    with st.expander("Keterbatasan penting"):
        st.markdown(
            "1. Tautan grup publik bukan jaminan isi bisa dibaca.\n"
            "2. Selector browser belum tentu cocok; teks bisa terpotong.\n"
            "3. Deduplikasi tanpa ID tautan memakai hash teks.\n"
            "4. Waktu relatif ('3 jam lalu') hanya perkiraan.\n"
            "5. Model AI kecil bisa salah membaca model/harga.\n"
            "6. Teks mentah bisa memuat nama penulis/komentar karena diambil dari "
            "kontainer postingan.\n"
            "7. Semua respons dikirim manual oleh Anda."
        )
    with st.expander("Uji deteksi halaman (tanpa membuka Facebook)"):
        url = st.text_input("URL", value="https://www.facebook.com/groups/123456789/")
        body = st.text_area("Teks yang terlihat", height=80)
        if st.button("Periksa", key="check_page_state"):
            state = detect_page_state(url, body)
            st.write("Status:", state.status.value)
            st.write("Detail:", state.detail)
            st.info(ACCESS_STATUS_MESSAGES.get(state.status, ""))
    with st.expander("Sudah diuji otomatis vs perlu uji di laptop"):
        st.dataframe(
            [
                {"Bagian": "Normalisasi, harga, dedupe, filter", "Status": "Diuji"},
                {"Bagian": "Analisis aturan & validasi AI", "Status": "Diuji"},
                {"Bagian": "Ekspor CSV & draf netral", "Status": "Diuji"},
                {"Bagian": "SQLite, job, event, filter ulang", "Status": "Diuji"},
                {"Bagian": "Deteksi login/checkpoint & cookie", "Status": "Diuji dengan simulasi"},
                {"Bagian": "Pembacaan grup Facebook nyata", "Status": "BELUM — uji POC"},
                {"Bagian": "Login nyata & qwen3 nyata", "Status": "BELUM — uji di laptop"},
            ],
            hide_index=True,
        )


def render_pengaturan(settings: Settings, db: Database) -> None:
    _security_section(settings, db)
    st.divider()
    _ollama_section(settings, db)
    st.divider()
    _profile_section()
    st.divider()
    _data_section(settings, db)
    st.divider()
    _help_section(settings)
