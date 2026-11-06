"""FB Workspace - dashboard lokal.

Jalankan dari folder proyek:
    streamlit run app.py

Default hanya bisa diakses dari laptop sendiri (127.0.0.1).
"""

from __future__ import annotations

import streamlit as st

from fbworkspace.dashboard import (
    get_context,
    render_help_tab,
    render_manual_tab,
    render_ollama_tab,
    render_posts_tab,
    render_run_tab,
    render_settings_tab,
    render_sidebar,
)
from fbworkspace.demo import load_demo_posts

st.set_page_config(
    page_title="FB Workspace",
    page_icon="📱",
    layout="wide",
    initial_sidebar_state="expanded",
)

settings, db = get_context()

if "worker_proc" not in st.session_state:
    st.session_state["worker_proc"] = None

filter_config = render_sidebar(settings, db)

st.title("📱 FB Workspace")
st.caption(
    "Alat bantu lokal untuk menemukan & menganalisis postingan iPhone di grup Facebook "
    "yang Anda akses secara sah. Tidak ada auto-post, auto-comment, atau auto-chat."
)

if db.count_posts() == 0:
    st.info(
        "Database masih kosong. Untuk mencoba tanpa Facebook, muat **data DEMO sintetis** "
        "lebih dulu."
    )
    if st.button("📥 Muat data DEMO (sintetis)", type="primary", key="load_demo_main"):
        result = load_demo_posts(db, filter_config)
        (st.success if result.get("ok") else st.error)(result.get("message"))
        st.rerun()

tabs = st.tabs(
    [
        "🚀 Jalankan",
        "📥 Input Manual",
        "📋 Postingan",
        "🧠 Analisis Ollama",
        "⚙️ Pengaturan & Data",
        "ℹ️ Bantuan",
    ]
)

with tabs[0]:
    render_run_tab(db, settings, filter_config)

with tabs[1]:
    render_manual_tab(db, settings, filter_config)

with tabs[2]:
    render_posts_tab(db, settings)

with tabs[3]:
    render_ollama_tab(db, settings)

with tabs[4]:
    render_settings_tab(db, settings)

with tabs[5]:
    render_help_tab(settings)
