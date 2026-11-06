"""FB Workspace - dashboard lokal (alur 3 langkah).

Jalankan dari folder proyek:
    streamlit run app.py

Default hanya bisa diakses dari laptop sendiri (127.0.0.1).
"""

from __future__ import annotations

import streamlit as st

from fbworkspace.dashboard import (
    build_filter_config,
    get_context,
    render_demo_banner,
    render_hasil,
    render_mulai,
    render_pengaturan,
    render_sidebar,
)

st.set_page_config(
    page_title="FB Workspace",
    page_icon="📱",
    layout="wide",
    initial_sidebar_state="expanded",
)

settings, db = get_context()
st.session_state.setdefault("worker_proc", None)

render_sidebar(settings, db)

st.title("📱 FB Workspace")
st.caption(
    "Alat bantu lokal untuk membaca & menganalisis postingan iPhone di grup Facebook "
    "yang Anda akses secara sah. Tidak ada auto-post, auto-comment, atau auto-chat."
)

render_demo_banner(db)

tabs = st.tabs(["🚀 Mulai", "📋 Hasil", "⚙️ Pengaturan"])

with tabs[0]:
    render_mulai(settings, db)

with tabs[1]:
    render_hasil(settings, db, build_filter_config())

with tabs[2]:
    render_pengaturan(settings, db)
