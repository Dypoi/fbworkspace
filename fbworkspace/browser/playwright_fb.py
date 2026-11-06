"""Adapter Playwright untuk membaca postingan grup Facebook.

STATUS: PROOF OF CONCEPT (POC). Baca sebelum memakai:

1. SELECTOR TIDAK DIJAMIN. Struktur halaman Facebook berubah kapan saja dan
   berbeda antar akun/bahasa. Selector di sini (`div[role="article"]`) adalah
   tebakan konservatif yang HARUS Anda uji sendiri lewat scripts/poc_facebook.py.
   Kalau hasilnya 0 postingan padahal halaman terbuka, berarti selector perlu
   disesuaikan secara manual - bukan berarti kode ini "sudah bekerja".

2. TIDAK ADA STEALTH / ANTI-DETECTION. Browser dijalankan apa adanya, terlihat,
   dengan profil persisten supaya Anda bisa login sendiri. Tidak ada UA palsu,
   tidak ada penyembunyian webdriver, tidak ada proxy rotation.

3. TIDAK ADA CAPTCHA SOLVING. Begitu penanda checkpoint/login/penolakan muncul,
   proses langsung berhenti dan status dilaporkan.

4. TIDAK ADA AUTO-RETRY. Satu halaman dicoba satu kali per grup.

5. Hanya teks postingan + tautan postingan (bila terlihat) yang diambil.
   Nama/URL profil anggota tidak diambil. Komentar dibuang: elemen `article`
   bersarang (komentar) dilewati, dan teks berpola komentar ("... Reply",
   "LikeReplyShare") ditolak sebelum disimpan.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable

from ..models import AccessStatus, RawPost
from ..normalize import (
    canonical_post_url,
    collapse_ws,
    extract_post_ids_from_url,
    looks_like_comment,
    normalize_text,
    strip_invisible_noise,
    truncate,
)
from .base import CollectOutcome, CollectTarget, ProgressCallback
from .page_state import detect_page_state
from .session import has_fb_session, wait_for_login

DEFAULT_ARTICLE_SELECTOR = 'div[role="article"]'
# Tautan postingan dipakai sebagai titik masuk: lebih stabil daripada atribut
# role yang berubah antar versi antarmuka Facebook.
POST_LINK_SELECTOR = 'a[href*="/posts/"], a[href*="/permalink/"]'
# Di sebagian grup, postingan tidak punya tautan sama sekali. Kartu postingan
# bisa ditemukan dari feed; isinya diambil dari blok teks `div[dir="auto"]`.
FEED_SELECTOR = 'div[role="feed"]'
FEED_CARD_SELECTOR = '[aria-posinset]'
POST_BLOCK_SELECTOR = 'div[dir="auto"]'
MIN_BLOCK_CHARS = 12
MAX_TEXT_PER_POST = 6000

# Naik dari tautan postingan ke kontainer terdekat yang memuat SATU postingan
# saja (berhenti begitu ada tautan postingan kedua). Hasilnya elemen "kartu
# postingan" tanpa bergantung pada nama class acak.
_CONTAINER_JS = """(a) => {
  let node = a;
  let best = a;
  for (let i = 0; i < 14 && node; i++) {
    node = node.parentElement;
    if (!node) break;
    const links = Array.from(
      node.querySelectorAll('a[href*="/posts/"], a[href*="/permalink/"]')
    )
      .filter((x) => !x.href.includes('comment_id='));
    const ids = new Set(
      links.map((x) => (x.href.match(/\\/(?:posts|permalink)\\/(\\d+)/) || [])[1]).filter(Boolean)
    );
    if (ids.size > 1) break;
    best = node;
  }
  return best;
}"""

# Ambil HANYA isi postingan: buang komentar (article bersarang, daftar berisi
# tautan comment_id, blok ber-aria-label komentar).
_POST_BODY_JS = """(el) => {
  const clone = el.cloneNode(true);
  clone.querySelectorAll('div[role="article"]').forEach((n) => n.remove());
  clone.querySelectorAll('ul').forEach((n) => {
    if (n.querySelector('a[href*="comment_id="]')) n.remove();
  });
  clone.querySelectorAll('[aria-label]').forEach((n) => {
    const label = (n.getAttribute('aria-label') || '').toLowerCase();
    const isComment = label.includes('comment') && !label.includes('most relevant');
    const isCommentId = label.includes('komentar') || label.includes('balasan');
    if ((isComment || isCommentId) && (n.innerText || '').length < 1500) n.remove();
  });
  return clone.innerText || '';
}"""

_ACTION_BAR_RE = re.compile(
    r"(?im)^\s*(?:like|suka)\s*[\u00b7|.]?\s*(?:comment|komentar)"
    r"\s*[\u00b7|.]?\s*(?:share|bagikan)\s*$"
)


class PlaywrightFacebookAdapter:
    name = "playwright"

    def __init__(
        self,
        *,
        profile_dir: str | Path,
        headless: bool = False,
        slow_mo_ms: int = 120,
        article_selector: str = DEFAULT_ARTICLE_SELECTOR,
        scroll_pause_ms: int = 1500,
        page_timeout_ms: int = 30_000,
        login_wait_seconds: int = 180,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self.article_selector = article_selector
        self.scroll_pause_ms = scroll_pause_ms
        self.page_timeout_ms = page_timeout_ms
        # Saat Facebook meminta login: biarkan jendela terbuka selama ini
        # supaya pengguna bisa login sendiri. 0 = langsung berhenti.
        self.login_wait_seconds = max(0, int(login_wait_seconds))
        # Sumber elemen pada putaran terakhir: "post-links" atau "article-selector".
        self._last_source = "unknown"
        # Kunci kartu kosong yang sudah dihitung, agar tidak berlipat tiap gulir.
        self._empty_card_keys: set[str] = set()

    # ------------------------------------------------------------------ setup
    def is_available(self) -> tuple[bool, str]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return False, (
                "Paket 'playwright' belum terpasang. Jalankan: "
                "pip install -r requirements.txt"
            )
        try:
            with sync_playwright() as pw:
                executable = Path(pw.chromium.executable_path)
            if not executable.exists():
                return False, (
                    "Chromium untuk Playwright belum diunduh. Jalankan: "
                    "playwright install chromium"
                )
        except Exception:  # noqa: BLE001 - pemeriksaan best-effort, jangan gagalkan UI
            pass
        if self.headless:
            return (
                True,
                "PERINGATAN: headless=true. Anda tidak bisa login sendiri dengan aman. "
                "Sebaiknya gunakan FBW_BROWSER_HEADLESS=false.",
            )
        return True, "Playwright siap (browser harus sudah diunduh: playwright install chromium)."

    # ------------------------------------------------------------------ utama
    def collect(
        self,
        target: CollectTarget,
        should_stop: Callable[[], bool] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> CollectOutcome:
        from playwright.sync_api import Error as PWError
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright

        limits = target.limits
        started = time.monotonic()
        posts: list[RawPost] = []
        # Kunci teks yang sudah diketahui berupa komentar, agar hitungan tidak
        # membengkak saat elemen yang sama terbaca di setiap putaran gulir.
        skipped_keys: set[str] = set()
        seen_keys: set[str] = set()
        diagnostics: dict = {
            "adapter": self.name,
            "article_selector": self.article_selector,
            "groups": [],
            "selector_verified": False,
            "login_wait_seconds": self.login_wait_seconds,
            "notes": [
                "Selector belum divalidasi untuk akun Anda. Periksa hasil POC.",
                "Teks bisa terpotong karena tombol 'Lihat selengkapnya' tidak diklik otomatis.",
            ],
        }
        counters = {"read": 0, "inserted": 0, "duplicates": 0}

        def progress(level: str, message: str) -> None:
            if on_progress:
                on_progress(level, message, dict(counters))

        # Waktu menunggu login tidak dihitung sebagai waktu pembacaan.
        paused_seconds = [0.0]

        def stop_now() -> bool:
            if should_stop and should_stop():
                return True
            elapsed = time.monotonic() - started - paused_seconds[0]
            return elapsed > limits.max_seconds

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        if not self.headless:
            progress(
                "info",
                "Membuka jendela Chromium. Kalau diminta login, login sendiri di jendela itu. "
                "Aplikasi tidak menyimpan kata sandi Anda.",
            )

        with sync_playwright() as pw:
            context = None
            try:
                try:
                    context = pw.chromium.launch_persistent_context(
                        user_data_dir=str(self.profile_dir),
                        headless=self.headless,
                        slow_mo=self.slow_mo_ms,
                        args=["--start-maximized"],
                        viewport=None,
                    )
                except PWError as exc:
                    detail = str(exc)
                    if "Executable doesn't exist" in detail or "playwright install" in detail:
                        message = (
                            "Chromium untuk Playwright belum terunduh. Jalankan: "
                            "playwright install chromium"
                        )
                    elif "SingletonLock" in detail or "ProcessSingleton" in detail:
                        message = (
                            "Profil browser sedang dipakai proses lain (mungkin jendela login "
                            "yang masih terbuka). Tutup jendela itu lalu jalankan lagi."
                        )
                    else:
                        message = f"Gagal membuka Chromium: {detail[:300]}"
                    return CollectOutcome(
                        status=AccessStatus.ERROR,
                        posts=[],
                        message=message,
                        diagnostics=diagnostics,
                    )

                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(self.page_timeout_ms)

                for group_url in target.group_urls:
                    if stop_now():
                        return CollectOutcome(
                            AccessStatus.STOPPED, posts,
                            "Batas waktu tercapai atau pengguna menghentikan proses.",
                            diagnostics,
                        )
                    group_diag = {"url": group_url, "status": "", "articles_seen": 0}
                    diagnostics["groups"].append(group_diag)

                    progress("info", f"Membuka grup: {group_url}")
                    try:
                        page.goto(group_url, wait_until="domcontentloaded", timeout=self.page_timeout_ms)
                    except PWTimeout:
                        return CollectOutcome(
                            AccessStatus.ERROR, posts,
                            "Halaman tidak selesai dimuat dalam batas waktu. "
                            "Periksa koneksi internet Anda.",
                            diagnostics,
                        )
                    except PWError as exc:
                        return CollectOutcome(
                            AccessStatus.ERROR, posts, f"Browser error: {exc}", diagnostics
                        )

                    page.wait_for_timeout(2000)
                    if page.is_closed():
                        return CollectOutcome(
                            AccessStatus.STOPPED, posts, "Jendela browser ditutup.", diagnostics
                        )

                    state = self._read_page_state(page, context, PWError)
                    session = has_fb_session(_safe_cookies(context))
                    group_diag["status"] = state.status.value
                    group_diag["detail"] = state.detail
                    group_diag["session_cookie"] = session

                    # --- Login wall: beri kesempatan pengguna login sendiri ---
                    if (
                        state.status == AccessStatus.LOGIN_REQUIRED
                        and not session
                        and self.login_wait_seconds > 0
                    ):
                        progress(
                            "warning",
                            "Facebook meminta login. Jendela TIDAK ditutup: silakan login "
                            f"sendiri di jendela Chromium (maks {self.login_wait_seconds} detik). "
                            "Aplikasi tidak melihat kata sandi Anda.",
                        )
                        wait_started = time.monotonic()
                        waited = wait_for_login(
                            cookie_provider=lambda: _safe_cookies(context),
                            timeout_seconds=self.login_wait_seconds,
                            should_stop=stop_now,
                            on_tick=lambda remaining: progress(
                                "info",
                                f"Menunggu login di jendela browser… sisa {remaining} detik.",
                            ),
                        )
                        paused_seconds[0] += time.monotonic() - wait_started
                        diagnostics["login_wait"] = {
                            "enabled": True,
                            "seconds": self.login_wait_seconds,
                            "detected": waited.detected,
                            "reason": waited.reason,
                        }
                        if not waited.detected:
                            progress("warning", waited.message())
                            return CollectOutcome(
                                status=AccessStatus.LOGIN_REQUIRED,
                                posts=posts,
                                message=(
                                    _status_message(AccessStatus.LOGIN_REQUIRED, state.detail)
                                    + " "
                                    + waited.message()
                                ),
                                diagnostics=diagnostics,
                            )

                        # Login berhasil -> muat ulang grup sekali, lalu lanjut.
                        try:
                            window_closed = page.is_closed() or not context.pages
                        except Exception:  # noqa: BLE001 - jendela bisa sudah tertutup
                            window_closed = True
                        if window_closed:
                            return CollectOutcome(
                                AccessStatus.STOPPED,
                                posts,
                                "Login terdeteksi, tetapi jendela browser ditutup sebelum "
                                "pembacaan dilanjutkan. Sesi sudah tersimpan; jalankan ulang "
                                "dan jangan tutup jendela sampai proses selesai.",
                                diagnostics,
                            )
                        progress("info", "Login terdeteksi. Memuat ulang halaman grup…")
                        try:
                            page.goto(
                                group_url,
                                wait_until="domcontentloaded",
                                timeout=self.page_timeout_ms,
                            )
                            page.wait_for_timeout(2000)
                        except PWError as exc:
                            return CollectOutcome(
                                AccessStatus.ERROR, posts,
                                f"Gagal memuat ulang grup setelah login: {exc}", diagnostics,
                            )
                        state = self._read_page_state(page, context, PWError)
                        group_diag["status_after_login"] = state.status.value
                        if state.status != AccessStatus.OK:
                            progress("warning", state.detail)
                            return CollectOutcome(
                                status=state.status,
                                posts=posts,
                                message=_status_message(state.status, state.detail),
                                diagnostics=diagnostics,
                            )

                    if state.status != AccessStatus.OK:
                        progress("warning", state.detail)
                        return CollectOutcome(
                            status=state.status,
                            posts=posts,
                            message=_status_message(state.status, state.detail),
                            diagnostics=diagnostics,
                        )

                    # Gulir terbatas sampai batas postingan/waktu tercapai
                    idle_rounds = 0
                    while len(posts) < limits.max_posts and not stop_now():
                        if page.is_closed():
                            return CollectOutcome(
                                AccessStatus.STOPPED, posts, "Jendela browser ditutup.", diagnostics
                            )
                        try:
                            elements = self._candidate_elements(page)
                        except PWError as exc:
                            return CollectOutcome(
                                AccessStatus.ERROR, posts, f"Gagal membaca elemen: {exc}", diagnostics
                            )

                        group_diag["articles_seen"] = max(group_diag["articles_seen"], len(elements))
                        group_diag["extraction"] = self._last_source
                        diagnostics["extraction"] = self._last_source
                        before = len(posts)

                        for element in elements:
                            if len(posts) >= limits.max_posts or stop_now():
                                break
                            raw = self._extract_post(element, group_url, diagnostics, skipped_keys)
                            if raw is None:
                                continue
                            key = self._post_key(raw)
                            if key in seen_keys:
                                counters["duplicates"] += 1
                                continue
                            seen_keys.add(key)
                            posts.append(raw)
                            counters["read"] = len(posts)
                            progress("info", f"Terkumpul {len(posts)} postingan (batas {limits.max_posts}).")

                        if len(posts) == before:
                            idle_rounds += 1
                        else:
                            idle_rounds = 0

                        if idle_rounds >= 3:
                            group_diag["stop_reason"] = "tidak ada postingan baru setelah 3 kali gulir"
                            break

                        try:
                            page.mouse.wheel(0, 2600)
                            page.wait_for_timeout(self.scroll_pause_ms)
                        except PWError as exc:
                            if "closed" in str(exc).lower():
                                return CollectOutcome(
                                    AccessStatus.STOPPED, posts, "Jendela browser ditutup.", diagnostics
                                )
                            break

                    if len(posts) >= limits.max_posts:
                        break

                elapsed = round(time.monotonic() - started, 1)
                diagnostics["elapsed_seconds"] = elapsed
                diagnostics["duplicates_skipped"] = counters["duplicates"]

                skipped = int(diagnostics.get("skipped_comment") or 0)
                diagnostics["skipped_comment"] = skipped

                if not posts:
                    if skipped:
                        message = (
                            f"Tidak ada postingan yang layak disimpan. {skipped} elemen yang "
                            "terbaca ternyata komentar/reply, bukan isi postingan. Selector "
                            "perlu diperbaiki sebelum dipakai lagi; lihat contoh teks di "
                            "scripts/poc_facebook.py (jalankan POC)."
                        )
                    else:
                        message = (
                            "Halaman terbuka tanpa penanda pemblokiran, tetapi TIDAK ada postingan "
                            "yang bisa dibaca dengan selector saat ini. Ini bisa berarti: grup "
                            "memerlukan keanggotaan, konten belum dimuat, atau selector perlu "
                            "diperbaiki manual. Lihat scripts/poc_facebook.py."
                        )
                    return CollectOutcome(AccessStatus.OK_EMPTY, [], message, diagnostics)

                message = f"Selesai membaca {len(posts)} postingan dalam {elapsed} detik."
                if skipped:
                    message += (
                        f" {skipped} komentar/reply dilewati karena bukan isi postingan."
                    )
                    diagnostics.setdefault("notes", []).append(
                        "Sebagian elemen yang terbaca adalah komentar. Kalau angka ini besar, "
                        "periksa hasil POC sebelum memakai datanya."
                    )
                return CollectOutcome(AccessStatus.OK, posts, message, diagnostics)
            except PWError as exc:
                return CollectOutcome(
                    AccessStatus.ERROR, posts, f"Kesalahan Playwright: {exc}", diagnostics
                )
            finally:
                try:
                    if context is not None:
                        context.close()
                except Exception:  # noqa: BLE001 - penutupan browser tidak boleh menutupi error asli
                    pass

    # -------------------------------------------------------------- halaman
    @staticmethod
    def _read_page_state(page, context, pw_error_class):
        """Baca teks halaman + tentukan status, dengan sadar-sesi (cookie)."""
        try:
            body_text = page.inner_text("body")[:20000]
        except pw_error_class:
            body_text = ""
        session = has_fb_session(_safe_cookies(context))
        return detect_page_state(page.url, body_text, has_session=session)

    # ------------------------------------------------------------- ekstraksi
    def _containers_from_links(self, page) -> list:
        """Kumpulkan kartu postingan dari tautan `/posts/` (bukan komentar)."""
        try:
            anchors = page.query_selector_all(POST_LINK_SELECTOR)
        except Exception:  # noqa: BLE001 - halaman bisa berubah saat dipindai
            return []

        containers: list = []
        seen_ids: set[str] = set()
        for anchor in anchors:
            try:
                href = anchor.get_attribute("href") or ""
            except Exception:  # noqa: BLE001
                continue
            if not href or "comment_id=" in href:
                continue
            match = re.search(r"/(?:posts|permalink)/(\d+)", href)
            if not match or match.group(1) in seen_ids:
                continue
            try:
                handle = anchor.evaluate_handle(_CONTAINER_JS)
                container = handle.as_element()
            except Exception:  # noqa: BLE001
                container = None
            if container is None:
                continue
            seen_ids.add(match.group(1))
            containers.append(container)
        return containers

    def _feed_cards(self, page) -> list:
        """Kartu postingan dari feed, untuk grup yang tidak memasang tautan."""
        try:
            cards = page.query_selector_all(FEED_CARD_SELECTOR)
        except Exception:  # noqa: BLE001 - halaman bisa berubah saat dipindai
            return []
        return list(cards)

    def _candidate_elements(self, page) -> list:
        """Elemen kandidat: tautan postingan, kartu feed, lalu selector lama."""
        if self.article_selector != DEFAULT_ARTICLE_SELECTOR:
            self._last_source = "custom-selector"
            return page.query_selector_all(self.article_selector)

        containers = self._containers_from_links(page)
        if containers:
            self._last_source = "post-links"
            return containers

        cards = self._feed_cards(page)
        if cards:
            self._last_source = "feed-cards"
            return cards

        self._last_source = "article-selector"
        return page.query_selector_all(self.article_selector)

    def _blocks_text(self, element) -> str:
        """Gabungkan blok teks postingan (`div[dir="auto"]`), tanpa label UI."""
        try:
            blocks = element.query_selector_all(POST_BLOCK_SELECTOR)
        except Exception:  # noqa: BLE001
            return ""
        texts: list[str] = []
        for block in blocks:
            try:
                raw = block.inner_text()
            except Exception:  # noqa: BLE001
                continue
            cleaned = _clean_ui_noise(strip_invisible_noise(normalize_text(raw)))
            flat = collapse_ws(cleaned)
            if len(flat) < MIN_BLOCK_CHARS:
                continue
            if looks_like_comment(flat):
                continue
            texts.append(cleaned)
        return "\n".join(dict.fromkeys(texts))

    def _note_empty_card(
        self,
        element,
        diagnostics: dict | None,
        skipped_keys: set[str] | None,
    ) -> None:
        """Bedakan kartu kosong (skeleton) dari kartu yang isinya komentar."""
        if diagnostics is None:
            return
        try:
            position = element.get_attribute("aria-posinset")
        except Exception:  # noqa: BLE001 - elemen tanpa atribut/get_attribute
            position = None
        if position:
            key = f"pos:{position}"
            if key in self._empty_card_keys:
                return
            self._empty_card_keys.add(key)
        raw = ""
        try:
            raw = strip_invisible_noise(normalize_text(element.inner_text()))
        except Exception:  # noqa: BLE001
            raw = ""
        if looks_like_comment(raw):
            self._note_skipped(
                diagnostics, skipped_keys if skipped_keys is not None else set(), raw
            )
        else:
            diagnostics["empty_cards"] = int(diagnostics.get("empty_cards") or 0) + 1

    def _post_body(self, element) -> str:
        """Teks isi postingan saja (komentar dibuang di dalam DOM)."""
        try:
            body = element.evaluate(_POST_BODY_JS)
            if isinstance(body, str):
                return body
        except Exception:  # noqa: BLE001 - fallback ke inner_text
            pass
        try:
            return element.inner_text()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _is_nested_article(element) -> bool:
        """True bila elemen ini `article` di dalam `article` lain (komentar/reply)."""
        try:
            return bool(
                element.evaluate(
                    "(el) => { const parent = el.parentElement;"
                    " return !!(parent && parent.closest('div[role=\"article\"]')); }"
                )
            )
        except Exception:  # noqa: BLE001 - elemen bisa hilang saat halaman berubah
            return False

    def _note_skipped(
        self, diagnostics: dict | None, skipped_keys: set[str], text: str
    ) -> None:
        if diagnostics is None:
            return
        snippet = collapse_ws(text)[:140]
        key = snippet.lower()[:120]
        if not key or key in skipped_keys:
            return
        skipped_keys.add(key)
        diagnostics["skipped_comment"] = int(diagnostics.get("skipped_comment") or 0) + 1
        samples = diagnostics.setdefault("sample_texts", [])
        if len(samples) < 5:
            samples.append(snippet)

    def _extract_post(
        self,
        element,
        group_url: str,
        diagnostics: dict | None = None,
        skipped_keys: set[str] | None = None,
    ) -> RawPost | None:
        if self._is_nested_article(element):
            return None

        if self._last_source == "feed-cards":
            text = normalize_text(self._blocks_text(element))
            if len(collapse_ws(text)) < 15:
                # Kartu bisa kosong (skeleton/label UI) atau isinya hanya komentar.
                self._note_empty_card(element, diagnostics, skipped_keys)
                return None
        else:
            text = normalize_text(self._post_body(element))
            if len(collapse_ws(text)) < 15:
                return None

        text = strip_invisible_noise(text)
        # Potong di bilah aksi "Like · Comment · Share" (sesudahnya komentar).
        text = _cut_at_action_bar(text)
        # Buang baris antarmuka khas Facebook yang bukan isi postingan.
        text = _clean_ui_noise(text)
        if len(collapse_ws(text)) < 15:
            return None

        # Jangan simpan teks yang isinya hanya nama + tombol aksi.

        hrefs: list[str] = []
        try:
            anchors = element.query_selector_all(
                "a[href*='/posts/'], a[href*='/permalink/'], a[href*='story_fbid=']"
            )
            for anchor in anchors[:6]:
                href = anchor.get_attribute("href") or ""
                if not href:
                    continue
                if href.startswith("/"):
                    href = f"https://www.facebook.com{href}"
                if "facebook.com" in href:
                    hrefs.append(href)
        except Exception:  # noqa: BLE001
            hrefs = []

        # Tautan komentar memuat comment_id=; utamakan tautan postingan murni.
        hrefs.sort(key=lambda href: "comment_id=" in href)
        comment_only_links = bool(hrefs) and all("comment_id=" in href for href in hrefs)

        if looks_like_comment(text) or (
            comment_only_links and len(collapse_ws(text)) < 300
        ):
            self._note_skipped(
                diagnostics, skipped_keys if skipped_keys is not None else set(), text
            )
            return None

        post_url = None
        for href in hrefs:
            post_url = canonical_post_url(href) or href
            break

        return RawPost(
            text=truncate(text, MAX_TEXT_PER_POST),
            group_url=group_url,
            post_url=post_url,
            source="browser",
        )

    @staticmethod
    def _post_key(post: RawPost) -> str:
        ids = extract_post_ids_from_url(post.post_url)
        if ids:
            return f"id:{ids[0]}"
        return "text:" + collapse_ws(post.text).lower()[:400]


def _safe_cookies(context) -> list[dict]:
    """Ambil cookie Facebook saja. Hanya nama yang dipakai, nilai tidak disimpan."""
    try:
        return list(context.cookies(["https://www.facebook.com", "https://www.facebook.com/"]))
    except Exception:  # noqa: BLE001 - jendela bisa ditutup pengguna
        try:
            return list(context.cookies())
        except Exception:  # noqa: BLE001
            return []


_UI_NOISE_PATTERNS = (
    r"^like$",
    r"^suka$",
    r"^comment$",
    r"^komentar$",
    r"^share$",
    r"^bagikan$",
    r"^see more$",
    r"^lihat selengkapnya$",
    r"^\d+ (comment|komentar|share|bagikan|reaction|reaksi)s?$",
    r"^(follow|ikuti|join group|gabung grup)$",
    r"^most relevant$",
    r"^paling relevan$",
    r"^sort group feed by.*$",
    r"^facebook$",
    r"^all reactions:$",
)


def _cut_at_action_bar(text: str) -> str:
    """Potong teks di bilah aksi postingan; komentar berada setelahnya."""
    match = _ACTION_BAR_RE.search(text)
    return text[: match.start()] if match else text


def _clean_ui_noise(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if any(re.match(pattern, stripped, re.I) for pattern in _UI_NOISE_PATTERNS):
            continue
        lines.append(stripped)
    return normalize_text("\n".join(lines))


def _status_message(status: AccessStatus, detail: str) -> str:
    from ..models import ACCESS_STATUS_MESSAGES

    base = ACCESS_STATUS_MESSAGES.get(status, "Proses dihentikan.")
    return f"{base} Detail: {detail}" if detail else base
