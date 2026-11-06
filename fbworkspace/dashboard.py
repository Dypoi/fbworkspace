"""Dashboard Streamlit FB Workspace.

Semua fungsi di sini hanya membangun tampilan dan memanggil modul inti.
Logika bisnis ada di db.py, filters.py, analyzer.py, pipeline.py, export.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from . import jobs
from .analyzer import analyze_text
from .browser.page_state import detect_page_state
from .browser.playwright_fb import PlaywrightFacebookAdapter
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
from .pipeline import ingest_manual_entries, parse_manual_input

PROFILE_PATH = PROJECT_ROOT / "data" / "business_profile.json"
ACTIVE_STATUSES = {JobStatus.QUEUED.value, JobStatus.RUNNING.value}


# ---------------------------------------------------------------------------
# Setup & helper
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


def _status_badge(row: dict) -> str:
    if row.get("source") == "demo":
        return "DEMO"
    if row.get("source") == "manual":
        return "Manual"
    return "Browser"


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def render_sidebar(settings: Settings, db: Database) -> FilterConfig:
    with st.sidebar:
        st.markdown("### 📱 FB Workspace")
        st.caption("Alat bantu analisis lokal. Bukan crawler, bukan bot otomatis.")

        safety = settings.describe_safety()
        st.caption(f"Dashboard: `{safety['bind']}` · lokal: {safety['lokal_saja']}")
        st.caption(f"Database: `{Path(safety['database']).name}`")

        stats = db.stats()
        st.metric("Postingan tersimpan", stats["total"])

        st.divider()
        st.markdown("#### 🎯 Filter")
        st.caption("Dipakai saat menilai postingan (browser maupun input manual).")

        st.text_area(
            "Kata kunci inklusi",
            key="fbw_include",
            height=70,
            placeholder="iphone, iphone 13, cari iphone",
            help="Pisahkan dengan koma atau baris baru. Kosongkan bila tidak perlu.",
        )
        st.text_area(
            "Kata kunci eksklusi",
            key="fbw_exclude",
            height=70,
            placeholder="rusak, mati, hilang, KW",
            help="Postingan yang memuat salah satu kata ini langsung digugurkan.",
        )
        st.text_input(
            "Lokasi (opsional)",
            key="fbw_locations",
            placeholder="Tangerang, Jakarta Selatan",
            help="Hanya mencocokkan nama lokasi yang tertulis di teks.",
        )

        price_enabled = st.checkbox("Batasi rentang harga", key="fbw_price_on", value=False)
        price_min, price_max = None, None
        if price_enabled:
            c1, c2 = st.columns(2)
            with c1:
                min_val = st.number_input(
                    "Min (Rp)", min_value=0, value=0, step=500_000,
                    format="%d", key="fbw_price_min",
                )
            with c2:
                max_val = st.number_input(
                    "Maks (Rp)", min_value=0, value=0, step=500_000,
                    format="%d", key="fbw_price_max",
                )
            price_min = int(min_val) if min_val > 0 else None
            price_max = int(max_val) if max_val > 0 else None
            if price_min and price_max and price_min > price_max:
                st.error("Harga minimal lebih besar dari maksimal.")

        st.checkbox(
            "Wajib menyebut iPhone",
            key="fbw_require_iphone",
            value=True,
            help="Matikan hanya bila Anda ingin menyimpan semua postingan.",
        )
        st.checkbox(
            "Mode ketat (tolak jika harga/lokasi tidak diketahui)",
            key="fbw_strict",
            value=False,
        )
        st.caption(
            "Mode longgar: postingan tanpa harga/lokasi tetap lolos dengan catatan. "
            "Mode ketat: postingan seperti itu digugurkan."
        )

        st.divider()
        st.caption("Tidak ada data yang dikirim ke internet. Ollama & browser tetap lokal.")

    return config_from_ui(
        include_raw=st.session_state.get("fbw_include", ""),
        exclude_raw=st.session_state.get("fbw_exclude", ""),
        locations_raw=st.session_state.get("fbw_locations", ""),
        price_min=price_min,
        price_max=price_max,
        require_iphone=st.session_state.get("fbw_require_iphone", True),
    )


def strict_mode() -> bool:
    return bool(st.session_state.get("fbw_strict", False))


# ---------------------------------------------------------------------------
# Panel job (langsung/otomatis diperbarui)
# ---------------------------------------------------------------------------

def _render_job_content(db: Database, settings: Settings, job_id: int, live: bool) -> None:
    job = db.get_job(job_id)
    if not job:
        st.info("Belum ada sesi pembacaan.")
        return

    status = job.get("status") or JobStatus.QUEUED.value
    counters = job.get("counters") or {}

    label = {
        JobStatus.QUEUED.value: "⏳ Antre",
        JobStatus.RUNNING.value: "🔄 Berjalan",
        JobStatus.COMPLETED.value: "✅ Selesai",
        JobStatus.STOPPED.value: "🛑 Dihentikan",
        JobStatus.FAILED.value: "❌ Gagal",
        JobStatus.BLOCKED.value: "⛔ Diblokir / tidak diizinkan",
    }.get(status, status)

    cols = st.columns(5)
    cols[0].metric("Status", label)
    cols[1].metric("Terbaca", counters.get("read", 0))
    cols[2].metric("Baru", counters.get("inserted", 0))
    cols[3].metric("Duplikat", counters.get("duplicates", 0))
    cols[4].metric("Tidak lolos filter", counters.get("filtered_out", 0))

    if job.get("message"):
        st.info(job["message"])

    age = _age_seconds(job.get("heartbeat_at"))
    if status in ACTIVE_STATUSES and age is not None and age > 120:
        st.warning(
            f"Tidak ada pembaruan dari worker selama {int(age)} detik. "
            "Proses mungkin macet. Periksa log di bawah, lalu hentikan paksa bila perlu."
        )

    if status in ACTIVE_STATUSES:
        b1, b2, b3 = st.columns(3)
        if b1.button("🛑 Minta berhenti", key=f"stop_{job_id}"):
            db.request_stop(job_id)
            st.toast("Permintaan berhenti dikirim. Worker akan berhenti di batas aman.")
        if b2.button("⛔ Hentikan paksa", key=f"force_{job_id}"):
            msg = jobs.force_stop(db, job_id, st.session_state.get("worker_proc"))
            st.toast(msg)
            st.rerun()
        if b3.button("🩺 Tandai gagal (jika macet)", key=f"fail_{job_id}"):
            jobs.mark_failed(db, job_id, "Ditandai gagal manual oleh pengguna karena proses macet.")
            st.rerun()

    events = db.get_events(job_id, limit=120)
    if events:
        with st.expander(f"Log kejadian ({len(events)})", expanded=status in ACTIVE_STATUSES):
            icons = {"info": "ℹ️", "warning": "⚠️", "error": "❌"}
            for event in events[-40:]:
                ts = human_time(event.get("ts"))
                # Pesan bisa memuat teks dari halaman Facebook -> selalu escape.
                safe_message = _escape_html(str(event.get("message", "")))
                st.markdown(
                    f"<div style='font-size:0.82rem;margin:2px 0'>"
                    f"<span style='opacity:.6'>{ts}</span> "
                    f"{icons.get(event.get('level'), '•')} {safe_message}</div>",
                    unsafe_allow_html=True,
                )

    log_text = jobs.tail_log(job_id, settings, max_lines=120)
    if log_text:
        with st.expander("Log teknis worker (untuk diagnosa)"):
            st.code(log_text, language="text")

    if live:
        st.caption("Panel ini diperbarui otomatis setiap 2 detik.")


@st.fragment(run_every="2s")
def live_job_panel(db: Database, settings: Settings, job_id: int) -> None:
    _render_job_content(db, settings, job_id, live=True)


def render_job_section(db: Database, settings: Settings, job_id: int | None) -> None:
    st.subheader("Status proses")
    if not job_id:
        latest = db.latest_job()
        job_id = latest["id"] if latest else None
    if job_id is None:
        st.info("Belum ada sesi pembacaan. Jalankan POC atau pakai input manual.")
        return
    job = db.get_job(job_id)
    if job and job.get("status") in ACTIVE_STATUSES:
        live_job_panel(db, settings, job_id)
    else:
        _render_job_content(db, settings, job_id, live=False)


# ---------------------------------------------------------------------------
# Tab 1 - Jalankan
# ---------------------------------------------------------------------------

def render_run_tab(db: Database, settings: Settings, filter_config: FilterConfig) -> None:
    st.subheader("Jalankan pembacaan (Proof of Concept)")
    st.warning(
        "**Baca dulu.** Tautan grup publik tidak menjamin seluruh isi grup bisa dibaca. "
        "Facebook dapat meminta login, membatasi tampilan, atau menampilkan tantangan keamanan. "
        "Aplikasi akan **berhenti dan melaporkan status** bila itu terjadi — tidak ada upaya menembusnya. "
        "Gunakan batas kecil sampai Anda yakin hasilnya layak."
    )

    left, right = st.columns([3, 2])

    with left:
        st.markdown("#### 1) Tautan grup")
        raw_urls = st.text_area(
            "Satu tautan per baris",
            key="fbw_group_urls",
            height=110,
            placeholder="https://www.facebook.com/groups/123456789/",
            label_visibility="collapsed",
        )
        validations = parse_group_urls(raw_urls)
        valid_urls: list[str] = []
        if validations:
            for item in validations:
                if item.ok:
                    valid_urls.append(item.normalized)
                    st.success(f"✅ {item.normalized} — {item.message}")
                    if item.warning:
                        st.warning(f"⚠️ {item.warning}")
                else:
                    st.error(f"❌ {item.message}")
        st.caption(
            "Validasi hanya memeriksa **bentuk** tautan, bukan apakah isi grup boleh dibaca. "
            "Aplikasi tidak mengumpulkan data anggota dan tidak membuka profil siapa pun."
        )

        st.markdown("#### 2) Batas pengujian")
        c1, c2 = st.columns(2)
        with c1:
            max_posts = st.slider(
                "Maksimum postingan per sesi", 1,
                max(5, settings.hard_max_posts), min(5, settings.hard_max_posts),
                help=f"Pengaman maksimum: {settings.hard_max_posts} postingan.",
            )
        with c2:
            max_minutes = st.slider("Batas waktu (menit)", 1, 15, 3)

        st.caption(
            "Tidak ada pengulangan otomatis. Setelah sesi ini selesai, proses berhenti. "
            "Anda yang memutuskan mau menjalankannya lagi."
        )

    with right:
        st.markdown("#### 3) Mode")
        mode = st.radio(
            "Cara memperoleh data",
            options=["browser", "manual"],
            format_func=lambda v: {
                "browser": "Buka browser lokal (Playwright POC)",
                "manual": "Tanpa browser — pakai Input Manual",
            }[v],
            key="fbw_mode",
            help="Mode manual tetap berguna kalau pembacaan otomatis tidak layak.",
        )

        if mode == "browser":
            available, message = PlaywrightFacebookAdapter(
                profile_dir=settings.browser_profile_dir,
                headless=settings.browser_headless,
            ).is_available()
            if available:
                st.info(message)
            else:
                st.error(message)
            st.caption(
                "Jendela Chromium akan terbuka dan terlihat. Bila Facebook meminta login, "
                "login sendiri di jendela itu. Aplikasi tidak menyimpan kata sandi, cookie "
                "tidak pernah diunggah, dan profil browser tidak masuk Git."
            )
        else:
            st.info(
                "Mode manual tidak membuka Facebook. Tempelkan teks postingan di tab "
                "**Input Manual**."
            )

        st.markdown("#### 4) Opsional")
        analyze_ollama = st.checkbox(
            "Analisis otomatis dengan Ollama setelah membaca",
            value=False,
            help="Hanya jika Ollama berjalan lokal dan model sudah diunduh. Job tetap sukses "
                 "walau Ollama gagal.",
        )

        consent = st.checkbox(
            "Saya memahami kemungkinan gagal/dibatasi dan akan mematuhi Ketentuan Facebook.",
            value=False,
            key="fbw_consent",
        )

    st.divider()

    active = db.active_job()
    start_disabled = (
        mode != "browser"
        or not valid_urls
        or not consent
        or active is not None
    )
    cta, hint = st.columns([1, 3])
    with cta:
        if st.button("▶️ Mulai pembacaan", type="primary", disabled=start_disabled):
            config = jobs.worker_config(
                mode=mode,
                max_posts=max_posts,
                max_seconds=max_minutes * 60,
                filter_json=config_to_json(filter_config),
                strict_unknown=strict_mode(),
                analyze_ollama=analyze_ollama,
            )
            job_id, proc = jobs.start_job(db, settings, group_urls=valid_urls, config=config)
            st.session_state["job_id"] = job_id
            st.session_state["worker_proc"] = proc
            st.rerun()
    with hint:
        reasons = []
        if mode != "browser":
            reasons.append("mode manual tidak memulai browser")
        if not valid_urls:
            reasons.append("belum ada tautan grup yang valid")
        if not consent:
            reasons.append("centang pernyataan kesesuaian")
        if active:
            reasons.append(f"masih ada job aktif (#{active['id']})")
        st.caption("Tombol aktif bila: " + "; ".join(reasons) + "." if reasons else "Siap dijalankan.")

    if active and not st.session_state.get("job_id"):
        st.session_state["job_id"] = active["id"]

    st.divider()
    render_job_section(db, settings, st.session_state.get("job_id"))


# ---------------------------------------------------------------------------
# Tab 2 - Input manual
# ---------------------------------------------------------------------------

_MANUAL_EXAMPLE = """URL: https://www.facebook.com/groups/123456789/posts/987654321/
GRUP: https://www.facebook.com/groups/123456789/
WAKTU: 3 jam lalu

Dijual iPhone 13 128GB, BH 88%, mulus, garansi habis. Harga 8,5jt COD Tangerang.

---

Cari iPhone 12 mini 64GB budget 4jt, area Jakarta. DM ya.
"""


def render_manual_tab(db: Database, settings: Settings, filter_config: FilterConfig) -> None:
    st.subheader("Input manual")
    st.caption(
        "Jalur yang selalu tersedia, bahkan jika pembacaan otomatis tidak layak. "
        "Salin teks postingan yang memang boleh Anda lihat, lalu tempel di sini. "
        "Postingan disimpan sebagai data tidak tepercaya dan tidak pernah dieksekusi."
    )

    with st.expander("Format input & contoh", expanded=False):
        st.markdown(
            "Pisahkan beberapa postingan dengan baris `---`. Baris opsional di awal blok:\n"
            "- `URL: <tautan postingan>`\n- `GRUP: <tautan grup>`\n- `WAKTU: <teks waktu posting>`\n\n"
            "Sisanya dianggap teks postingan."
        )
        st.code(_MANUAL_EXAMPLE, language="text")

    if st.button("Isi contoh DEMO", key="fill_manual_example"):
        st.session_state["fbw_manual_text"] = _MANUAL_EXAMPLE

    raw = st.text_area(
        "Tempel satu atau beberapa postingan",
        key="fbw_manual_text",
        height=260,
        placeholder="Dijual iPhone 13 128GB ...\n\n---\n\nCari iPhone 11 budget 4jt ...",
    )

    uploaded = st.file_uploader(
        "Atau unggah file teks (.txt / .json)",
        type=["txt", "json"],
        help="File JSON harus berbentuk {\"posts\": [{\"text\": \"...\", ...}]}. "
             "File tidak diunggah ke internet; dibaca lokal oleh aplikasi.",
    )

    entries = parse_manual_input(raw) if raw else []
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
            st.caption(f"File `{uploaded.name}` terbaca: {len(entries)} blok.")

    st.caption(
        f"Filter aktif: {len(filter_config.include_keywords)} inklusi, "
        f"{len(filter_config.exclude_keywords)} eksklusi, "
        f"{len(filter_config.locations)} lokasi, "
        f"harga {'—' if filter_config.price_min is None and filter_config.price_max is None else 'dibatasi'}. "
        "Ubah di sidebar."
    )

    c1, c2 = st.columns(2)
    save = c1.button("💾 Simpan ke database", type="primary", disabled=not entries)
    save_ai = c2.button("💾 Simpan + analisis Ollama", disabled=not entries)

    if save or save_ai:
        if not entries:
            st.warning("Tidak ada teks yang bisa disimpan.")
        else:
            summary = ingest_manual_entries(
                db,
                entries,
                filter_config=filter_config,
                strict_unknown=strict_mode(),
                max_posts=50,
            )
            st.success(
                f"Selesai: {summary.inserted} baru, {summary.duplicates} duplikat, "
                f"{summary.filtered_out} tidak lolos filter, {summary.errors} gagal."
            )
            for message in summary.messages[:5]:
                st.warning(message)
            if summary.inserted == 0 and summary.duplicates:
                st.info(
                    "Semua postingan sudah ada di database. Deduplikasi memakai ID tautan, "
                    "atau hash teks bila tautan tidak ada."
                )
            if save_ai and summary.inserted:
                _run_ollama_batch(
                    db,
                    settings,
                    limit=min(summary.inserted, 20),
                    only_new=True,
                    ids=None,
                )

    st.divider()
    st.markdown("#### Cek cepat satu teks (tanpa menyimpan)")
    preview_text = st.text_area(
        "Teks untuk dianalisis", height=110, key="fbw_preview_text",
        placeholder="Dijual iPhone 13 Pro Max 256GB Rp 12.500.000 COD Tangerang",
    )
    if preview_text.strip():
        result = analyze_text(preview_text)
        cols = st.columns(5)
        cols[0].metric("Kategori", result.category)
        cols[1].metric("Model", result.iphone_model)
        cols[2].metric("Harga/Budget", _fmt_price(result.price_amount))
        cols[3].metric("Jenis", result.price_kind)
        cols[4].metric("Skor", result.relevance_score)
        if result.details:
            st.write("Detail disebutkan:", result.details)
        st.write("Alasan skor:")
        for reason in result.reasons:
            st.markdown(f"- {reason}")
        st.caption(
            "Skor adalah penjumlahan kriteria yang bisa dijelaskan, bukan probabilitas kebenaran. "
            "Field kosong ditandai 'tidak diketahui', bukan ditebak."
        )


# ---------------------------------------------------------------------------
# Tab 3 - Postingan
# ---------------------------------------------------------------------------

def render_posts_tab(db: Database, settings: Settings) -> None:
    st.subheader("Postingan tersimpan")
    stats = db.stats()
    top = st.columns(4)
    top[0].metric("Total", stats["total"])
    top[1].metric("Sudah dianalisis", stats["analysed"])
    top[2].metric("DEMO", stats["by_source"].get("demo", 0))
    top[3].metric("Relevan (manual)", stats["by_status"].get("relevan", 0))

    f1, f2, f3, f4 = st.columns([2, 1, 1, 1])
    search = f1.text_input("Cari teks / tautan", key="fbw_search")
    category = f2.selectbox(
        "Kategori", ["semua", "mencari", "menjual", "diskusi", "tidak relevan"],
        key="fbw_filter_category",
    )
    status_filter = f3.selectbox(
        "Status", ["semua"] + list(WORKFLOW_STATUS_LABELS.keys()),
        format_func=lambda v: WORKFLOW_STATUS_LABELS.get(v, "Semua"),
        key="fbw_filter_status",
    )
    only_passed = f4.checkbox("Hanya lolos filter", value=True, key="fbw_only_passed")

    rows = db.list_posts(
        search=search,
        workflow_status=None if status_filter == "semua" else status_filter,
        category=None if category == "semua" else category,
        filter_passed=True if only_passed else None,
        limit=500,
    )

    if not rows:
        st.info(
            "Belum ada postingan yang cocok. Muat data DEMO di tab **Pengaturan & Data** "
            "atau tambahkan lewat **Input Manual**."
        )
        return

    table = [
        {
            "ID": row["id"],
            "Sumber": _status_badge(row),
            "Waktu ditemukan": human_time(row["found_at"]),
            "Kategori": row.get("category") or "-",
            "Model": row.get("iphone_model") or UNKNOWN,
            "Nominal": _fmt_price(row.get("price_amount")),
            "Jenis": row.get("price_kind") or UNKNOWN,
            "Lokasi": row.get("location") or UNKNOWN,
            "Skor": row.get("relevance_score", 0),
            "Status": WORKFLOW_STATUS_LABELS.get(row["workflow_status"], row["workflow_status"]),
            "Cuplikan": (row["text"][:110] + "…") if len(row["text"]) > 110 else row["text"],
        }
        for row in rows
    ]
    st.dataframe(table, hide_index=True, height=360)

    st.download_button(
        "⬇️ Ekspor CSV (aman dari formula injection)",
        data=rows_to_csv_bytes(rows),
        file_name=suggested_filename(),
        mime="text/csv",
        help="Sel yang dimulai dengan = + - @ diberi awalan apostrof agar tidak dieksekusi Excel.",
    )
    st.caption(
        "Tabel menampilkan maksimum 500 baris terbaru; ekspor CSV juga mengikuti hasil filter ini. "
        f"Database: `{db.path}`"
    )

    st.divider()
    st.markdown("#### Detail postingan")
    id_options = [row["id"] for row in rows]
    selected_id = st.selectbox(
        "Pilih ID postingan",
        options=id_options,
        format_func=lambda pid: f"#{pid} — {next((r['text'][:60] for r in rows if r['id'] == pid), '')}…",
    )
    if selected_id:
        _render_post_detail(db, int(selected_id))


def _render_post_detail(db: Database, post_id: int) -> None:
    post = db.get_post(post_id)
    if not post:
        st.error("Postingan tidak ditemukan.")
        return

    if post["source"] == "demo":
        st.warning("🟡 Data DEMO (sintetis). Bukan hasil pembacaan Facebook.")

    meta = st.columns(4)
    meta[0].metric("Kategori", post.get("category") or "belum dianalisis")
    meta[1].metric("Model", post.get("iphone_model") or UNKNOWN)
    meta[2].metric("Nominal", _fmt_price(post.get("price_amount")))
    meta[3].metric("Skor relevansi", post.get("relevance_score") or 0)

    with st.expander("Teks postingan lengkap", expanded=True):
        st.markdown(
            "<div style='white-space:pre-wrap;background:rgba(127,127,127,.08);"
            "padding:12px;border-radius:8px'>"
            + _escape_html(post["text"])
            + "</div>",
            unsafe_allow_html=True,
        )
        st.caption(
            f"Sumber data: {post['source']} · ditemukan: {human_time(post['found_at'])} · "
            f"waktu posting: {human_time(post.get('posted_at'))} "
            f"({post.get('posted_at_precision') or UNKNOWN})"
        )

    links = st.columns(2)
    if post.get("post_url"):
        links[0].markdown(f"🔗 [Buka postingan di Facebook]({post['post_url']})")
    else:
        links[0].caption("Tautan postingan: tidak diketahui (hanya teks yang tersedia).")
    if post.get("group_url"):
        links[1].markdown(f"👥 [Tautan grup]({post['group_url']})")
    else:
        links[1].caption("Tautan grup: tidak diketahui.")

    if post.get("filter_reasons"):
        with st.expander("Kenapa lolos/tidak lolos filter"):
            for reason in post["filter_reasons"]:
                st.markdown(f"- {reason}")

    analysis = db.get_analysis(post_id)
    if analysis:
        with st.expander("Detail analisis", expanded=True):
            detail_cols = st.columns(2)
            with detail_cols[0]:
                st.write("**Jenis nominal:**", analysis.get("price_kind") or UNKNOWN)
                st.write("**Lokasi:**", analysis.get("location") or UNKNOWN)
                st.write("**Mesin analisis:**", analysis.get("engine"))
                st.write("**Waktu analisis:**", human_time(analysis.get("analyzed_at")))
            with detail_cols[1]:
                details = analysis.get("details") or {}
                if details:
                    st.write("**Detail yang disebutkan:**")
                    for key, value in details.items():
                        st.markdown(f"- {key}: {value}")
                else:
                    st.write("**Detail yang disebutkan:** tidak ada yang eksplisit.")
            st.write("**Alasan skor:**")
            for reason in analysis.get("reasons") or []:
                st.markdown(f"- {reason}")
            if analysis.get("snippet"):
                st.caption(f"Cuplikan pendukung: “{analysis['snippet']}”")

    st.markdown("##### Status pengerjaan")
    current = post["workflow_status"]
    status_cols = st.columns(4)
    for column, (value, label) in zip(status_cols, WORKFLOW_STATUS_LABELS.items()):
        if column.button(
            ("✅ " if current == value else "") + label,
            key=f"status_{post_id}_{value}",
            use_container_width=True,
        ):
            db.set_workflow_status(post_id, value)
            st.toast(f"Status diubah menjadi {label}.")
            st.rerun()

    st.markdown("##### Draf respons (tidak dikirim otomatis)")
    profile = _load_profile()
    draft = build_draft(post, analysis, profile)
    if draft is None:
        st.info("Kategori 'tidak relevan': tidak ada draf yang disarankan.")
    else:
        st.caption(draft.title)
        st.code(draft.body, language=None)
        st.caption("Klik ikon salin di kanan atas blok. Periksa dan sunting sebelum mengirim manual.")
        for note in draft.notes:
            st.caption(f"• {note}")
        warnings_found = contains_forbidden_claim(draft.body)
        if warnings_found:
            st.error(f"Draf memuat klaim yang tidak boleh dikarang: {', '.join(warnings_found)}")
        profile_hint = "lengkap" if profile.is_filled() else "belum lengkap (isi di Pengaturan)"
        st.caption(f"Profil bisnis: {profile_hint} — {profile.nama_toko} · {profile.area}")

    with st.expander("Hapus postingan ini"):
        confirm = st.checkbox("Saya yakin ingin menghapus permanen.", key=f"del_confirm_{post_id}")
        if st.button("🗑️ Hapus", key=f"del_{post_id}", disabled=not confirm):
            db.delete_post(post_id)
            st.toast("Postingan dihapus.")
            st.rerun()


def _escape_html(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# Tab 4 - Ollama
# ---------------------------------------------------------------------------

def _run_ollama_batch(
    db: Database,
    settings: Settings,
    *,
    limit: int,
    only_new: bool = False,
    ids: list[int] | None = None,
) -> None:
    config = OllamaConfig.from_settings(settings)
    status = check_connection(config)
    if not status.ok:
        st.error(status.message)
        return

    if ids:
        posts = [db.get_post(pid) for pid in ids]
        posts = [p for p in posts if p]
    else:
        rows = db.list_posts(filter_passed=True, limit=5000)
        posts = [
            row for row in rows
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
        st.success(f"{ok_count} postingan dianalisis dengan model `{config.model}`.")
    if failures:
        st.warning("Sebagian gagal:\n" + "\n".join(f"- {item}" for item in failures[:6]))
    if ok_count:
        st.caption("Hasil Ollama menggantikan analisis aturan untuk postingan tersebut. Periksa ulang sebelum dipakai.")


def render_ollama_tab(db: Database, settings: Settings) -> None:
    st.subheader("Analisis Ollama (opsional)")
    st.caption(
        "Aplikasi tetap berfungsi penuh tanpa AI: filter kata kunci dan analisis aturan "
        "sudah berjalan lokal. Ollama hanya menambah ekstraksi bahasa."
    )

    config = OllamaConfig.from_settings(settings)
    info = summarize_config(config)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Endpoint", info["endpoint"].replace("http://", ""))
    c2.metric("Model", info["model"])
    c3.metric("Timeout", info["timeout"])
    c4.metric("Lokal", info["lokal"])

    if st.button("🔍 Cek koneksi Ollama"):
        status = check_connection(config)
        st.session_state["fbw_ollama_status"] = {
            "ok": status.ok,
            "message": status.message,
            "models": status.models,
        }

    cached = st.session_state.get("fbw_ollama_status")
    if cached:
        if cached["ok"]:
            st.success(cached["message"])
        else:
            st.error(cached["message"])
        if cached["models"]:
            st.caption("Model terpasang: " + ", ".join(cached["models"][:12]))

    st.warning(
        "**Kejujuran soal performa.** RAM 16 GB cukup untuk model 3B–4B terkuantisasi, "
        "tetapi kecepatan sangat bergantung pada CPU/GPU. Tanpa GPU, satu postingan bisa "
        "memakan puluhan detik. Aplikasi memakai timeout dan analisis aturan sebagai fallback."
    )

    with st.expander("Model kecil yang disarankan (ukuran & lisensi)"):
        st.dataframe(
            [
                {
                    "Model": m["name"],
                    "Unduhan": m["size"],
                    "Lisensi": m["license"],
                    "Catatan": m["note"],
                }
                for m in SUGGESTED_MODELS
            ],
            hide_index=True,
        )
        st.code(
            "ollama pull qwen2.5:3b\nollama serve   # biasanya sudah jalan otomatis di Windows",
            language="powershell",
        )
        st.caption(
            "Perintah di atas dijalankan di PowerShell Anda, bukan oleh aplikasi. "
            "Endpoint dan model diatur lewat .env (FBW_OLLAMA_URL, FBW_OLLAMA_MODEL)."
        )

    st.divider()
    st.markdown("#### Jalankan analisis")
    rows = db.list_posts(filter_passed=True, limit=500)
    pending = [row for row in rows if row["analysis_status"] != "dianalisis (Ollama)"]
    st.caption(f"{len(pending)} postingan belum dianalisis Ollama (maksimum 20 per klik).")

    batch_limit = st.slider("Jumlah postingan per klik", 1, 20, min(5, max(1, len(pending))))
    if st.button("🧠 Analisis sekarang", disabled=not pending):
        _run_ollama_batch(db, settings, limit=batch_limit)

    if pending:
        with st.expander("Pilih postingan tertentu"):
            options = {row["id"]: f"#{row['id']} — {row['text'][:70]}" for row in pending[:50]}
            chosen = st.multiselect(
                "ID postingan", options=list(options.keys()),
                format_func=lambda k: options[k], key="fbw_ollama_ids",
            )
            if st.button("Analisis pilihan", disabled=not chosen):
                _run_ollama_batch(db, settings, limit=len(chosen), ids=chosen)

    st.divider()
    st.markdown("#### Batasan yang perlu diketahui")
    st.markdown(
        "- Model kecil bisa salah membaca model iPhone atau harga. Selalu periksa hasilnya.\n"
        "- Teks postingan adalah data tidak tepercaya; model diinstruksikan mengabaikan perintah di dalamnya, "
        "tetapi prompt injection tidak bisa dijamin 100%.\n"
        "- Keluaran divalidasi ketat (kategori terbatas, skor 0–100, input dipotong). "
        "Jika tidak valid, hasilnya ditolak dan status 'analisis gagal'.\n"
        "- Tidak ada embeddings/vector database pada MVP ini."
    )


# ---------------------------------------------------------------------------
# Tab 5 - Pengaturan & data
# ---------------------------------------------------------------------------

def render_settings_tab(db: Database, settings: Settings) -> None:
    st.subheader("Pengaturan & data")

    st.markdown("#### Keamanan & lokasi file")
    safety = settings.describe_safety()
    st.dataframe(
        [{"Pengaturan": key, "Nilai": value} for key, value in safety.items()],
        hide_index=True,
    )
    if safety["lokal_saja"] != "ya":
        st.error(
            "Dashboard TIDAK terikat ke localhost. Ini berbahaya untuk laptop pribadi. "
            "Set FBW_HOST=127.0.0.1 di .env."
        )
    else:
        st.success("Dashboard terikat ke localhost (hanya bisa diakses dari laptop ini).")

    st.markdown("#### Profil bisnis (dipakai di draf)")
    st.caption(
        "Isi hanya identitas umum. Draf tidak boleh memuat klaim stok/harga/garansi; "
        "semua placeholder harus Anda ganti sendiri."
    )
    profile = _load_profile()
    p1, p2, p3 = st.columns(3)
    nama = p1.text_input("Nama toko / identitas", value=profile.nama_toko, key="fbw_prof_nama")
    area = p2.text_input("Area", value=profile.area, key="fbw_prof_area")
    jam = p3.text_input("Jam respons", value=profile.jam_respons, key="fbw_prof_jam")
    if st.button("💾 Simpan profil lokal"):
        _save_profile(BusinessProfile(nama, area, jam))
        st.success("Profil disimpan di data/business_profile.json (masuk .gitignore).")

    st.divider()
    st.markdown("#### Data DEMO")
    st.caption(
        "Data sintetis untuk mencoba alur tanpa Facebook. Semua diberi label [DEMO] "
        "dan tautan grup palsu."
    )
    if st.button("📥 Muat data DEMO"):
        result = load_demo_posts(db, None)
        (st.success if result.get("ok") else st.error)(result.get("message"))

    st.divider()
    st.markdown("#### Database")
    stats = db.stats()
    st.write(
        f"Lokasi: `{stats['db_path']}` · ukuran {stats['db_size_kb']} KB · "
        f"{stats['total']} postingan"
    )
    if stats["by_source"]:
        st.caption("Per sumber: " + ", ".join(f"{k}={v}" for k, v in stats["by_source"].items()))

    st.markdown("#### Hapus data")
    st.warning(
        "Penghapusan bersifat permanen dan lokal. Tidak ada salinan di cloud. "
        "File database juga tidak pernah diunggah ke GitHub."
    )
    h1, h2, h3 = st.columns(3)

    with h1:
        confirm_posts = st.checkbox("Hapus semua postingan", key="del_posts_confirm")
        if st.button("🗑️ Hapus postingan", disabled=not confirm_posts):
            deleted = db.delete_all_posts()
            st.success(f"{deleted} postingan dihapus.")
            st.rerun()

    with h2:
        confirm_logs = st.checkbox("Hapus log & riwayat job", key="del_logs_confirm")
        if st.button("🧹 Hapus log", disabled=not confirm_logs):
            db.delete_jobs_and_events()
            st.success("Log dan riwayat job dihapus.")
            st.rerun()

    with h3:
        confirm_profile = st.checkbox("Hapus profil browser (cookie/sesi)", key="del_profile_confirm")
        if st.button("🔐 Hapus profil browser", disabled=not confirm_profile):
            import shutil

            target = settings.browser_profile_dir
            if target.exists() and target.is_dir():
                try:
                    shutil.rmtree(target)
                    st.success(f"Profil browser dihapus: {target}")
                except OSError as exc:
                    st.error(f"Gagal menghapus: {exc}. Tutup semua jendela Chromium lalu coba lagi.")
            else:
                st.info("Folder profil browser belum ada.")
            st.caption(
                "Menghapus profil berarti Anda perlu login manual lagi di Facebook. "
                "Cookie/sesi tidak pernah disimpan di Git."
            )

    st.divider()
    st.markdown("#### Berkas yang tidak boleh masuk Git")
    st.code(
        ".env\ndata/*.db\ndata/browser_profile/\nlogs/\n*.gguf",
        language="text",
    )
    st.caption("Semua sudah ada di .gitignore. Jangan melakukan force-add untuk file-file ini.")


# ---------------------------------------------------------------------------
# Tab 6 - Bantuan
# ---------------------------------------------------------------------------

def render_help_tab(settings: Settings) -> None:
    st.subheader("Bantuan, keterbatasan, dan prinsip kerja")

    st.markdown(
        """
#### Apa yang alat ini lakukan
- Menyimpan postingan iPhone yang **Anda lihat sendiri** (otomatis terbatas atau input manual).
- Menormalkan teks, menghapus duplikat, memfilter kata kunci, lalu mengekstrak
  kategori/model/harga/budget/lokasi dengan aturan yang bisa dijelaskan.
- Menyusun draf balasan netral yang **Anda** periksa dan kirim manual.

#### Apa yang TIDAK dilakukan
- Tidak menembus CAPTCHA, tidak menyamarkan otomasi, tidak memakai proxy untuk menghindari batasan.
- Tidak mengumpulkan data anggota/profil, komentar, atau data pribadi yang tidak perlu.
- Tidak mengirim komentar, tidak posting, tidak chat otomatis.
- Tidak mengunggah data ke hosting/API AI. Ollama berjalan di laptop Anda.
- Tidak menjalankan perintah yang tertulis di teks postingan.

#### Keterbatasan penting
1. **Tautan grup publik bukan jaminan.** Facebook bisa meminta login, membatasi
   tampilan, atau menampilkan tantangan. Bila itu terjadi, aplikasi berhenti dan
   melaporkan status — bukan mencari akal.
2. **Selector browser belum tentu cocok.** `div[role="article"]` adalah tebakan
   konservatif. Kalau POC menghasilkan 0 postingan, itu berarti selector/halaman
   perlu diperiksa manual, bukan "berhasil".
3. **Teks bisa terpotong** karena tombol "Lihat selengkapnya" tidak diklik otomatis.
4. **Deduplikasi punya batas.** Tanpa ID tautan, dua postingan dengan teks identik
   dianggap sama. Tanpa ID, teks yang diedit sedikit akan dianggap berbeda.
5. **Harga/budget bisa salah baca.** Format seperti "12,5jt" ditafsirkan dengan
   konvensi Indonesia (titik ribuan, koma desimal). Angka ambigu dibiarkan kosong.
6. **"Tidak diketahui" berarti tidak diketahui.** Tidak ada tebakan model/lokasi/harga.
7. **Skor relevansi bukan probabilitas.** Itu penjumlahan kriteria yang dijelaskan
   pada setiap hasil.
8. **Waktu posting relatif** ("3 jam lalu") hanya perkiraan, ditandai `perkiraan`.
"""
    )

    st.divider()
    st.markdown("#### Sudah diuji otomatis vs. perlu Anda uji di laptop")
    st.dataframe(
        [
            {"Bagian": "Normalisasi teks & tautan", "Status": "Diuji (unit test)"},
            {"Bagian": "Parsing harga/budget Indonesia", "Status": "Diuji (unit test)"},
            {"Bagian": "Deduplikasi tautan/hash", "Status": "Diuji (unit test)"},
            {"Bagian": "Filter kata kunci/lokasi/harga", "Status": "Diuji (unit test)"},
            {"Bagian": "Analisis aturan iPhone", "Status": "Diuji (unit test)"},
            {"Bagian": "Validasi keluaran AI", "Status": "Diuji (unit test, tanpa model nyata)"},
            {"Bagian": "Ekspor CSV anti formula injection", "Status": "Diuji (unit test)"},
            {"Bagian": "SQLite simpan/dedupe/status", "Status": "Diuji (unit test)"},
            {"Bagian": "Deteksi halaman login/challenge", "Status": "Diuji dengan teks contoh"},
            {"Bagian": "Pembacaan grup Facebook sungguhan", "Status": "BELUM — harus Anda uji (POC)"},
            {"Bagian": "Login manual di jendela Chromium", "Status": "BELUM — harus Anda uji"},
            {"Bagian": "Kualitas analisis model Ollama", "Status": "BELUM — bergantung model & laptop"},
        ],
        hide_index=True,
    )

    with st.expander("Uji cepat deteksi status halaman (tanpa membuka Facebook)"):
        st.caption(
            "Tempel teks halaman yang Anda lihat untuk mengetahui bagaimana aplikasi "
            "akan menilainya. Ini tidak membuka Facebook."
        )
        url = st.text_input("URL halaman", value="https://www.facebook.com/groups/123456789/")
        body = st.text_area("Teks yang terlihat di halaman", height=90)
        if st.button("Periksa status halaman"):
            state = detect_page_state(url, body)
            st.write("Status:", state.status.value)
            st.write("Detail:", state.detail)
            st.info(ACCESS_STATUS_MESSAGES.get(state.status, ""))

    st.divider()
    st.markdown("#### Berkas dokumentasi")
    docs = [
        ("README.md", "Gambaran proyek & cara jalan singkat"),
        ("docs/INSTALL_WINDOWS.md", "Instalasi Windows PowerShell langkah demi langkah"),
        ("docs/POC_FACEBOOK.md", "Prosedur proof of concept & cara membaca hasilnya"),
        ("docs/LIMITATIONS.md", "Keterbatasan dan kepatuhan"),
        ("docs/GIT_GUIDE.md", "Git pertama kali & update berikutnya"),
        ("docs/TESTING.md", "Apa yang diuji, hasilnya, dan apa yang belum"),
        ("docs/ARCHITECTURE.md", "Struktur modul & alur data"),
    ]
    for path, description in docs:
        exists = (PROJECT_ROOT / path).exists()
        st.markdown(f"- {'📄' if exists else '⚠️'} `{path}` — {description}")

    st.caption(
        "Data dan sesi login tetap di laptop Anda. Repositori GitHub hanya berisi kode, "
        "dokumentasi, dan data DEMO sintetis."
    )
