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
    truncate,
)
from .base import CollectOutcome, CollectTarget, ProgressCallback
from .page_state import detect_page_state
from .session import has_fb_session, wait_for_login

DEFAULT_ARTICLE_SELECTOR = 'div[role="article"]'
MAX_TEXT_PER_POST = 6000


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
                            elements = page.query_selector_all(self.article_selector)
                        except PWError as exc:
                            return CollectOutcome(
                                AccessStatus.ERROR, posts, f"Gagal membaca elemen: {exc}", diagnostics
                            )

                        group_diag["articles_seen"] = max(group_diag["articles_seen"], len(elements))
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

        try:
            text = element.inner_text()
        except Exception:  # noqa: BLE001 - elemen bisa hilang saat halaman berubah
            return None
        text = normalize_text(text)
        if len(collapse_ws(text)) < 15:
            return None

        # Buang baris antarmuka khas Facebook yang bukan isi postingan.
        text = _clean_ui_noise(text)
        if len(collapse_ws(text)) < 15:
            return None

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

        # Tautan komentar memuat comment_id=; postingan tidak.
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
    r"^all reactions:$",
)


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
