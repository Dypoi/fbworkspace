"""Adapter baca/kirim DM Facebook (Messenger) lewat browser yang sudah login.

Batas yang sama seperti adapter feed:
- tidak menembus CAPTCHA/challenge, tidak menyamarkan otomasi;
- berhenti dan melaporkan status bila diminta login;
- hanya membalas percakapan yang SUDAH ada (tidak menyapa orang baru).

Selector Facebook sering berubah dan **belum terverifikasi** pada akun nyata.
Karena itu modul ini:

- memakai beberapa selector berlapis untuk inbox dan kotak tulis,
- memberi waktu halaman (SPA) memuat sebelum menyimpulkan "tidak ada chat",
- menyimpan ``last_diagnostics`` berisi apa yang benar-benar terlihat
  (URL, judul, jumlah tautan chat, ada/tidak kotak tulis, cuplikan teks),
- mengembalikan status yang bisa dibedakan: ``ok``/``perlu_login``/
  ``tidak_ada_chat``/``pemblokiran``/``kesalahan``.

Ingin melihat apa yang terbaca tanpa menjalankan worker:

    .\\.venv\\Scripts\\python.exe -m fbworkspace.browser.messenger --diagnose
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..chat_pin import decide as decide_pin
from ..chat_pin import fill_pin, looks_like_pin_prompt, mask

INBOX_URL = "https://www.facebook.com/messages/"

# --- Selector berlapis (urutan = prioritas) --------------------------------
THREAD_SELECTORS = (
    'a[href*="/messages/t/"]',
    'div[role="listitem"] a[href*="/t/"]',
    'div[role="row"] a[href]',
)
MESSAGE_ROW_SELECTORS = (
    'div[role="row"]',
    'div[role="listitem"]',
    'div[aria-label*="Anda kirim"], div[aria-label*="You sent"]',
)
MESSAGE_TEXT_SELECTORS = ('div[dir="auto"]', 'span[dir="auto"]', 'div[role="none"] > span')
COMPOSER_SELECTORS = (
    'div[contenteditable="true"][role="textbox"]',
    'div[role="textbox"][contenteditable="true"]',
    'div[contenteditable="true"][data-lexical-editor="true"]',
    'div[aria-label*="Pesan"][contenteditable="true"]',
    'div[aria-label*="Message"][contenteditable="true"]',
    'div[contenteditable="true"]',
)
# Baris pesan di dalam percakapan (bukan daftar chat di sidebar).
CONVERSATION_ROW_SELECTORS = (
    'div[role="main"] div[role="row"]',
    'div[role="main"] div[role="listitem"]',
    'div[role="log"] div[role="row"]',
    'div[role="log"] [role="listitem"]',
    'div[aria-label*="Pesan"][role="main"] div[role="row"]',
)

# Label antarmuka yang sering terbaca sebagai "pesan" — harus dibuang.
UI_NOISE_LABELS = (
    "chat info",
    "info chat",
    "detail percakapan",
    "conversation details",
    "lihat profil",
    "view profile",
    "ketik pesan",
    "type a message",
    "tulis pesan",
    "kirim pesan",
    "send message",
    "pesan ini",
    "emoji",
    "lampiran",
    "attachment",
    "pesan suara",
    "voice message",
    "balas",
    "reply",
    "teruskan",
    "forward",
    "suka",
    "like",
    "reaksi",
    "react",
    "opsi",
    "options",
    "tutup",
    "close",
    "kembali",
    "back",
    "lainnya",
    "more",
    "lalu",
    "next",
    "sebelumnya",
    "previous",
    "muat lebih",
    "load more",
    "aktif sekarang",
    "active now",
    "terkirim",
    "sent",
    "dibaca",
    "seen",
    "delivered",
)

LOGIN_MARKERS = (
    'input[name="email"]',
    "#email",
    'form[action*="login"]',
    'input[name="pass"]',
)
# Penanda halaman yang berarti proses harus BERHENTI, bukan diulang.
BLOCK_MARKERS = (
    "checkpoint",
    "security check",
    "konfirmasi identitas",
    "verify your identity",
    "akun anda telah dinonaktifkan",
    "your account has been locked",
    "temporarily blocked",
    "sementara diblokir",
)
CHECKPOINT_WORDS = (
    "checkpoint",
    "konfirmasi identitas",
    "verifikasi identitas",
    "security check",
    "verify your identity",
)


@dataclass(frozen=True)
class Thread:
    """Satu percakapan di daftar inbox."""

    key: str
    name: str
    url: str
    unread: bool = False


@dataclass
class ChatMessage:
    key: str
    direction: str  # "in" | "out"
    text: str
    author: str = ""


@dataclass
class ThreadRead:
    messages: list[ChatMessage] = field(default_factory=list)
    status: str = "ok"
    note: str = ""


@dataclass
class ThreadList:
    """Hasil daftar chat + diagnostik mentah (untuk laporan pengguna)."""

    threads: list[Thread] = field(default_factory=list)
    status: str = "ok"
    diagnostics: dict = field(default_factory=dict)


def _text_of(element) -> str:
    try:
        return (element.inner_text() or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _safe_attr(element, name: str) -> str:
    try:
        return element.get_attribute(name) or ""
    except Exception:  # noqa: BLE001
        return ""


def _safe_body_text(page, limit: int = 400) -> str:
    try:
        raw = page.inner_text("body")
    except Exception:  # noqa: BLE001
        return ""
    return " ".join(str(raw).split())[:limit]


def _is_ui_noise(text: str) -> bool:
    """True bila teks hanyalah label antarmuka (bukan isi pesan)."""
    cleaned = " ".join((text or "").lower().split())
    cleaned = cleaned.strip(" .:·—–-\n\t")
    if not cleaned:
        return True
    if cleaned in UI_NOISE_LABELS:
        return True
    # "Chat Info" + nama, dsb.
    return any(cleaned == label or cleaned.startswith(label + " ") for label in UI_NOISE_LABELS)


def _clean_message_text(text: str) -> str:
    """Buang baris label UI; sisakan isi pesan sebenarnya."""
    lines = [line.strip() for line in (text or "").split("\n")]
    kept = [line for line in lines if line and not _is_ui_noise(line)]
    if not kept:
        return ""
    return "\n".join(kept).strip()


def _msg_key(thread_key: str, index: int, text: str) -> str:
    digest = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:10]
    return f"{thread_key}:{index}:{digest}"


def _thread_key(url: str) -> str:
    """ID percakapan, baik dari tautan ``/messages/t/<id>`` maupun ``/t/<id>``."""
    for marker in ("/messages/t/", "/t/"):
        if marker in url:
            return url.split(marker)[1].split("/")[0].split("?")[0]
    return url


def _direction(row, text: str, known_out: list[str]) -> str:
    """Tentukan arah pesan: 'out' (kita) atau 'in' (pembeli)."""
    for attr in ("aria-label", "data-scope"):
        label = _safe_attr(row, attr).lower()
        if not label:
            continue
        if "anda kirim" in label or "you sent" in label or "sent by you" in label:
            return "out"
        if "diterima" in label or "received" in label:
            return "in"
    try:
        marker = row.query_selector(
            '[aria-label*="Anda kirim"], [aria-label*="You sent"], '
            '[aria-label*="Anda mengirim"], [aria-label*="Sent by you"]'
        )
        if marker is not None:
            return "out"
    except Exception:  # noqa: BLE001
        pass
    normalized = " ".join(text.lower().split())
    for sent in known_out:
        if sent and normalized and normalized[:60] == " ".join(sent.lower().split())[:60]:
            return "out"
    return "in"


def _first_match(page, selectors: tuple[str, ...]):
    """Kembalikan (selector, elemen pertama) atau (None, None)."""
    for selector in selectors:
        try:
            element = page.query_selector(selector)
        except Exception:  # noqa: BLE001
            element = None
        if element is not None:
            return selector, element
    return None, None


def _first_match_all(page, selectors: tuple[str, ...]) -> tuple[str | None, list]:
    """Coba semua selector dan GABUNGkan hasilnya (tanpa duplikat).

    Penting: jangan berhenti di selector pertama yang menemukan sesuatu. Pada akun
    nyata, ``a[href*="/messages/t/"]`` hanya menemukan 1 tautan sementara daftar
    chat sebenarnya memakai bentuk ``/t/...`` (13 tautan). Berhenti di selector
    pertama membuat inbox terlihat kosong.
    """
    collected: list = []
    seen_ids: set[int] = set()
    used: list[str] = []
    for selector in selectors:
        try:
            elements = page.query_selector_all(selector)
        except Exception:  # noqa: BLE001
            elements = []
        if not elements:
            continue
        used.append(selector)
        for element in elements:
            handle = getattr(element, "_impl_obj", None) or id(element)
            marker = ("h", repr(handle)) if handle is not None else ("o", id(element))
            if marker in seen_ids:
                continue
            seen_ids.add(marker)
            collected.append(element)
    return (" + ".join(used) or None), collected


class MessengerAdapter:
    name = "messenger"

    def __init__(
        self,
        profile_dir: str | Path,
        *,
        headless: bool = False,
        slow_mo_ms: int = 120,
        timeout_ms: int = 45_000,
        chat_pin: str = "",
        pin_auto: bool = True,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self.timeout_ms = timeout_ms
        # PIN disimpan hanya di memori proses ini; tidak pernah dicetak.
        self.chat_pin = chat_pin or ""
        self.pin_auto = pin_auto
        self.pin_attempts = 0
        self.pin_status = "tidak diminta"
        self.last_diagnostics: dict = {}

    # ------------------------------------------------------------------ util
    def is_available(self) -> tuple[bool, str]:
        try:
            import playwright  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            return False, f"Playwright tidak tersedia: {exc.__class__.__name__}"
        return True, ""

    def _launch(self):
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            return sync_playwright().start()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "Playwright gagal dijalankan "
                f"({exc.__class__.__name__}). Jalankan sekali: "
                "playwright install chromium"
            ) from exc

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        """Terjemahkan error Playwright yang sering muncul jadi langkah konkret."""
        raw = f"{exc.__class__.__name__}: {exc}"
        lowered = raw.lower()
        if "executable doesn't exist" in lowered or "looks like playwright" in lowered:
            return (
                "Chromium Playwright belum terpasang. Jalankan sekali: "
                "playwright install chromium"
            )
        if "target page, context or browser has been closed" in lowered:
            return "Jendela browser tertutup di tengah proses."
        if "profile" in lowered and "in use" in lowered:
            return (
                "Profil browser sedang dipakai proses lain. Tutup jendela Chromium "
                "lain milik aplikasi ini, lalu coba lagi."
            )
        if "timeout" in lowered:
            return f"Halaman tidak selesai dimuat dalam batas waktu ({raw[:160]})"
        return raw[:300]

    @staticmethod
    def _close(playwright, context) -> None:
        """Tutup browser & Playwright; kegagalan menutup tidak boleh menutupi hasil."""
        if context is not None:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:  # noqa: BLE001
                pass

    def _context(self, playwright):
        return playwright.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=self.headless,
            slow_mo=self.slow_mo_ms,
        )

    # --------------------------------------------------------------- penanda
    def _login_required(self, page) -> bool:
        for selector in LOGIN_MARKERS:
            try:
                if page.query_selector(selector) is not None:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    @staticmethod
    def _blocked(page, diagnostics: dict) -> bool:
        blob = f"{diagnostics.get('url', '')} {diagnostics.get('title', '')} " \
               f"{diagnostics.get('text', '')}".lower()
        return any(word in blob for word in BLOCK_MARKERS)

    @staticmethod
    def _checkpoint_word(page, diagnostics: dict) -> str:
        blob = f"{diagnostics.get('url', '')} {diagnostics.get('title', '')} " \
               f"{diagnostics.get('text', '')}".lower()
        for word in CHECKPOINT_WORDS:
            if word in blob:
                return word
        return ""

    def _inspect(self, page) -> dict:
        """Rekam apa yang benar-benar terlihat di halaman (tanpa menebak)."""
        diagnostics: dict = {
            "url": "",
            "title": "",
            "text": "",
            "thread_links": 0,
            "chat_links": 0,
            "rows": 0,
            "listitems": 0,
            "composer": "",
            "login_form": False,
        }
        try:
            diagnostics["url"] = page.url
        except Exception:  # noqa: BLE001
            pass
        try:
            diagnostics["title"] = (page.title() or "")[:120]
        except Exception:  # noqa: BLE001
            pass
        diagnostics["text"] = _safe_body_text(page)
        for selector, key in (
            ('a[href*="/messages/t/"]', "thread_links"),
            ('a[href*="/t/"]', "chat_links"),
            ('div[role="row"]', "rows"),
            ('div[role="listitem"]', "listitems"),
            ('div[role="main"] div[role="row"]', "main_rows"),
        ):
            try:
                diagnostics[key] = len(page.query_selector_all(selector))
            except Exception:  # noqa: BLE001
                diagnostics[key] = 0
        selector, _ = _first_match(page, COMPOSER_SELECTORS)
        diagnostics["composer"] = selector or ""
        diagnostics["login_form"] = self._login_required(page)
        return diagnostics

    # ------------------------------------------------------------------- PIN
    def _handle_pin(self, page, *, stage: str) -> str:
        """Isi PIN HANYA di layar permintaan PIN pemulihan chat.

        Kembalikan status: ``tidak_diminta`` / ``terisi`` / ``perlu_pin``.
        """
        diagnostics = {
            "url": "",
            "title": "",
            "text": _safe_body_text(page, limit=600),
        }
        try:
            diagnostics["url"] = page.url
        except Exception:  # noqa: BLE001
            pass
        try:
            diagnostics["title"] = (page.title() or "")[:120]
        except Exception:  # noqa: BLE001
            pass

        if not looks_like_pin_prompt(" ".join(diagnostics.values())):
            return "tidak_diminta"

        decision = decide_pin(
            page_text=diagnostics["text"],
            url=diagnostics["url"],
            title=diagnostics["title"],
            pin=self.chat_pin,
            auto=self.pin_auto,
        )
        if not decision.allowed:
            self.pin_status = f"perlu_pin ({stage}): {decision.reason}"
            self.last_diagnostics = dict(
                self.last_diagnostics, pin=decision.reason, pin_stage=stage
            )
            return "perlu_pin"

        if self.pin_attempts >= 2:
            self.pin_status = "perlu_pin: PIN sudah dicoba 2 kali tanpa berhasil"
            return "perlu_pin"

        self.pin_attempts += 1
        ok, note = fill_pin(page, self.chat_pin, timeout_ms=self.timeout_ms)
        self.pin_status = f"{'terisi' if ok else 'gagal'}: {note}" if ok else note
        self.last_diagnostics = dict(
            self.last_diagnostics,
            pin=f"{note} (percobaan {self.pin_attempts}, {mask(self.chat_pin)})",
            pin_stage=stage,
        )
        if not ok:
            return "perlu_pin"
        try:
            page.wait_for_load_state("networkidle", timeout=min(15_000, self.timeout_ms))
        except Exception:  # noqa: BLE001
            pass
        return "terisi"

    # ------------------------------------------------------------- daftar chat
    def list_threads_detailed(self, limit: int = 8) -> ThreadList:
        """Seperti :meth:`list_threads`, tetapi mengembalikan diagnostik juga."""
        playwright = None
        context = None
        try:
            playwright = self._launch()
            context = self._context(playwright)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(INBOX_URL, wait_until="domcontentloaded", timeout=self.timeout_ms)
            except Exception as exc:  # noqa: BLE001
                diagnostics = {
                    "status": "kesalahan",
                    "stage": "buka_halaman",
                    "url": INBOX_URL,
                    "error": self._friendly_error(exc),
                }
                self.last_diagnostics = diagnostics
                return ThreadList(status="kesalahan", diagnostics=diagnostics)

            # Beri waktu SPA memuat; jangan langsung menyimpulkan "kosong".
            try:
                page.wait_for_load_state("networkidle", timeout=min(15_000, self.timeout_ms))
            except Exception:  # noqa: BLE001
                pass

            selector, anchors = _first_match_all(page, THREAD_SELECTORS)
            attempts = 0
            while not anchors and attempts < 3:
                attempts += 1
                try:
                    page.wait_for_timeout(1500)
                    page.mouse.wheel(0, 500)
                except Exception:  # noqa: BLE001
                    page.wait_for_timeout(1000)
                selector, anchors = _first_match_all(page, THREAD_SELECTORS)

            pin_state = self._handle_pin(page, stage="daftar_chat")
            if pin_state == "terisi":
                selector, anchors = _first_match_all(page, THREAD_SELECTORS)

            diagnostics = self._inspect(page)
            diagnostics["thread_selector"] = selector or ""
            diagnostics["attempts"] = attempts
            diagnostics["pin"] = self.pin_status

            if pin_state == "perlu_pin":
                diagnostics["status"] = "perlu_pin"
                self.last_diagnostics = diagnostics
                return ThreadList(status="perlu_pin", diagnostics=diagnostics)

            if self._login_required(page):
                diagnostics["status"] = "perlu_login"
                self.last_diagnostics = diagnostics
                return ThreadList(status="perlu_login", diagnostics=diagnostics)

            word = self._checkpoint_word(page, diagnostics)
            if word or self._blocked(page, diagnostics):
                diagnostics["status"] = "pemblokiran"
                diagnostics["reason"] = word or "halaman pembatasan"
                self.last_diagnostics = diagnostics
                return ThreadList(status="pemblokiran", diagnostics=diagnostics)

            threads: list[Thread] = []
            seen: set[str] = set()
            candidates = 0
            for anchor in anchors:
                href = _safe_attr(anchor, "href")
                if "/t/" not in href:
                    continue
                candidates += 1
                key = _thread_key(href)
                if not key or key in seen:
                    continue
                seen.add(key)
                url = href if href.startswith("http") else f"https://www.facebook.com{href}"
                label = _safe_attr(anchor, "aria-label").strip()
                name = label or _text_of(anchor).split("\n")[0][:80]
                unread = "belum dibaca" in label.lower() or "unread" in label.lower()
                threads.append(Thread(key=key, name=name or key, url=url, unread=unread))
                if len(threads) >= max(1, limit):
                    break

            diagnostics["threads"] = len(threads)
            diagnostics["candidates"] = candidates
            diagnostics["status"] = "ok" if threads else "tidak_ada_chat"
            self.last_diagnostics = diagnostics
            return ThreadList(
                threads=threads,
                status=diagnostics["status"],
                diagnostics=diagnostics,
            )
        except Exception as exc:  # noqa: BLE001
            diagnostics = {
                "status": "kesalahan",
                "stage": "siapkan_browser",
                "error": self._friendly_error(exc),
            }
            self.last_diagnostics = diagnostics
            return ThreadList(status="kesalahan", diagnostics=diagnostics)
        finally:
            self._close(playwright, context)

    def list_threads(self, limit: int = 8) -> tuple[list[Thread], str]:
        result = self.list_threads_detailed(limit=limit)
        return result.threads, result.status

    # ------------------------------------------------------------- baca isi chat
    def read_thread(self, url: str, *, known_out: list[str] | None = None) -> ThreadRead:
        thread_key = _thread_key(url)
        playwright = None
        context = None
        try:
            playwright = self._launch()
            context = self._context(playwright)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(
                self.conversation_url(url), wait_until="domcontentloaded", timeout=self.timeout_ms
            )
            try:
                page.wait_for_load_state("networkidle", timeout=min(15_000, self.timeout_ms))
            except Exception:  # noqa: BLE001
                pass
            diagnostics = self._inspect(page)
            if pin_state := self._handle_pin(page, stage="baca_chat"):
                if pin_state == "perlu_pin":
                    return ThreadRead(
                        status="perlu_pin",
                        note="Messenger meminta PIN pemulihan chat; worker tidak mengisinya",
                    )
            if self._login_required(page):
                return ThreadRead(status="perlu_login")
            word = self._checkpoint_word(page, diagnostics)
            if word or self._blocked(page, diagnostics):
                return ThreadRead(status="pemblokiran", note=word or "halaman pembatasan")

            selector, rows = _first_match_all(page, CONVERSATION_ROW_SELECTORS)
            attempts = 0
            while not rows and attempts < 2:
                attempts += 1
                page.wait_for_timeout(1500)
                selector, rows = _first_match_all(page, CONVERSATION_ROW_SELECTORS)
            if not rows:
                self.last_diagnostics = dict(
                    diagnostics, status="tidak_ada_pesan", conversation_selector=""
                )
                return ThreadRead(
                    status="tidak_ada_pesan",
                    note=(
                        "tidak ada baris pesan di area percakapan "
                        f"(baris di halaman: {diagnostics.get('rows', 0)})"
                    ),
                )

            text_selector = MESSAGE_TEXT_SELECTORS[0]
            for candidate in MESSAGE_TEXT_SELECTORS:
                try:
                    if page.query_selector(candidate) is not None:
                        text_selector = candidate
                        break
                except Exception:  # noqa: BLE001
                    continue

            messages: list[ChatMessage] = []
            for index, row in enumerate(rows):
                text = ""
                try:
                    nodes = row.query_selector_all(text_selector)
                    if nodes:
                        text = "\n".join(_text_of(node) for node in nodes if _text_of(node))
                except Exception:  # noqa: BLE001
                    text = ""
                if not text:
                    text = _text_of(row)
                text = _clean_message_text(text)
                if not text:
                    continue
                messages.append(
                    ChatMessage(
                        key=_msg_key(thread_key, index, text),
                        direction=_direction(row, text, known_out or []),
                        text=text[:2000],
                    )
                )
            if not messages:
                self.last_diagnostics = dict(
                    diagnostics,
                    status="tidak_ada_pesan",
                    rows=len(rows),
                    messages=0,
                    conversation_selector=selector or "",
                )
                return ThreadRead(
                    status="tidak_ada_pesan",
                    note=f"{len(rows)} baris di area percakapan, semuanya label antarmuka",
                )

            self.last_diagnostics = dict(
                diagnostics,
                status="ok",
                rows=len(rows),
                messages=len(messages),
                conversation_selector=selector or "",
            )
            return ThreadRead(messages=messages, status="ok", note=f"{len(rows)} baris")
        except Exception as exc:  # noqa: BLE001
            return ThreadRead(status="kesalahan", note=self._friendly_error(exc))
        finally:
            self._close(playwright, context)

    # ------------------------------------------------------------------ kirim
    @staticmethod
    def conversation_url(url: str) -> str:
        """URL percakapan langsung (bukan tautan sidebar yang bisa tanpa ID)."""
        key = _thread_key(url)
        if key and key.isdigit():
            return f"https://www.facebook.com/messages/t/{key}/"
        return url

    def _open_conversation(self, page, url: str, *, stage: str) -> tuple[bool, str]:
        """Buka URL percakapan langsung lalu pastikan kotak tulis muncul."""
        target = self.conversation_url(url)
        try:
            if page.url.rstrip("/") != target.rstrip("/"):
                page.goto(target, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception as exc:  # noqa: BLE001
            return False, self._friendly_error(exc)
        try:
            page.wait_for_load_state("networkidle", timeout=min(15_000, self.timeout_ms))
        except Exception:  # noqa: BLE001
            pass
        self._handle_pin(page, stage=stage)
        selector, composer = _first_match(page, COMPOSER_SELECTORS)
        if composer is None:
            return False, "kotak tulis tidak ditemukan di halaman percakapan"
        return True, ""

    def send_message(self, url: str, text: str) -> tuple[bool, str]:
        if not text.strip():
            return False, "teks kosong"
        playwright = None
        context = None
        try:
            playwright = self._launch()
            context = self._context(playwright)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(
                self.conversation_url(url), wait_until="domcontentloaded", timeout=self.timeout_ms
            )
            diagnostics = self._inspect(page)
            if self._login_required(page):
                return False, "perlu_login"
            word = self._checkpoint_word(page, diagnostics)
            if word or self._blocked(page, diagnostics):
                return False, f"pemblokiran: {word or 'halaman pembatasan'}"
            if self._handle_pin(page, stage="kirim") == "perlu_pin":
                return False, "perlu_pin"

            selector, composer = _first_match(page, COMPOSER_SELECTORS)
            if composer is None:
                try:
                    composer = page.wait_for_selector(COMPOSER_SELECTORS[0], timeout=self.timeout_ms)
                except Exception:  # noqa: BLE001
                    return False, "kotak tulis tidak ditemukan (selector berubah)"
            composer.click()
            page.keyboard.type(text, delay=25)
            page.keyboard.press("Enter")
            page.wait_for_timeout(2500)
            needle = " ".join(text.split())[:40]
            if needle and needle.lower() in " ".join(page.inner_text("body").split()).lower():
                return True, "terkirim"
            return False, "tidak terverifikasi terkirim"
        except Exception as exc:  # noqa: BLE001
            return False, self._friendly_error(exc)
        finally:
            self._close(playwright, context)


def diagnose(profile_dir: str | Path, *, headless: bool = False) -> dict:
    """Buka inbox, laporkan apa yang terlihat. Tidak mengirim apa pun."""
    adapter = MessengerAdapter(profile_dir=profile_dir, headless=headless)
    ok, why = adapter.is_available()
    if not ok:
        return {"ok": False, "error": why}
    result = adapter.list_threads_detailed(limit=10)
    payload = dict(result.diagnostics)
    payload["status"] = result.status
    payload["threads"] = [
        {"key": item.key, "name": item.name, "unread": item.unread} for item in result.threads
    ]
    return payload


def _save_report(payload: dict, settings=None) -> Path | None:
    """Simpan hasil pemeriksaan ke data/diagnostics/ supaya mudah dikirim."""
    try:
        root = Path(__file__).resolve().parent.parent.parent
        if settings is not None:
            base = Path(settings.data_dir)
        else:
            base = root / "data"
        folder = base / "diagnostics"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = folder / f"messenger-{stamp}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
    except OSError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Periksa inbox Messenger (tanpa mengirim apa pun)"
    )
    parser.add_argument("--profile", default="", help="Folder profil browser")
    parser.add_argument(
        "--headless", action="store_true", help="Jalankan tanpa jendela (untuk laporan)"
    )
    parser.add_argument(
        "--show", action="store_true", help="Tampilkan jendela browser (bisa Anda lihat)"
    )
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent.parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from fbworkspace.config import get_settings  # noqa: PLC0415
    from fbworkspace.normalize import collapse_ws  # noqa: PLC0415

    settings = get_settings()
    profile = args.profile or str(settings.browser_profile_dir)

    started = time.time()
    payload = diagnose(profile, headless=not args.show)
    payload["profile"] = str(profile)
    payload["detik"] = round(time.time() - started, 1)
    report = _save_report(payload, settings)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if report is not None:
        print(f"\nLaporan disimpan: {collapse_ws(str(report))}")
    return 0 if payload.get("status") == "ok" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
