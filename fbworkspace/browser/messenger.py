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
import os
import re
import subprocess
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
    "customise chat",
    "customize chat",
    "media and files",
    "media, files",
    "privacy & support",
    "privasi & dukungan",
    "mute",
    "bisukan",
    "search in conversation",
    "cari dalam percakapan",
    "add people",
    "tambah orang",
    "create group",
    "buat grup",
    "leave chat",
    "keluar dari chat",
    "archive",
    "arsipkan",
    "delete chat",
    "hapus chat",
    "block",
    "blokir",
    "report",
    "laporkan",
    "open profile",
    "buka profil",
    "theme",
    "tema",
    "emoji",
    "nicknames",
    "nama panggilan",
    "edit nicknames",
)

# Ekstraksi berbasis blok teks (bukan role=row): di Messenger, isi pesan hidup di
# elemen dir="auto", sedangkan role=row dipakai untuk sidebar/panel detail.
EXTRACT_JS = """
() => {
  const SEL = 'div[dir="auto"], span[dir="auto"]';
  const composer = document.querySelector(
    'div[contenteditable="true"][role="textbox"], div[role="textbox"][contenteditable="true"], div[contenteditable="true"]'
  );
  const box = (el) => {
    const r = el.getBoundingClientRect();
    return {x: r.x, y: r.y, w: r.width, h: r.height};
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const textOf = (el) => (el.innerText || el.textContent || '').trim();
  const leaf = (el) => !el.querySelector(SEL);

  // Cari wadah percakapan: leluhur terdekat dari kotak tulis yang memuat
  // beberapa blok teks lain. Ini membuang bilah atas ("Find friends"), sidebar,
  // dan panel detail tanpa menebak label satu per satu.
  let container = null;
  if (composer) {
    let node = composer.parentElement;
    for (let i = 0; i < 15 && node; i++) {
      const inside = Array.from(node.querySelectorAll(SEL)).filter(
        (el) => !composer.contains(el) && visible(el) && textOf(el) && leaf(el)
      );
      const r = node.getBoundingClientRect();
      if (inside.length >= 2 && r.height > 200) { container = node; break; }
      node = node.parentElement;
    }
  }

  const scope = container || document.body;

  // Facebook menandai pesan yang BELUM dibaca lewat aria-label gelembung/baris
  // ("Pesan belum dibaca", "Unread message"). Penanda ini yang paling dapat
  // dipercaya: tidak bergantung pada posisi kiri/kanan atau riwayat database.
  const unreadFlag = (el) => {
    let node = el;
    for (let i = 0; i < 6 && node; i++) {
      const label = (node.getAttribute && (node.getAttribute('aria-label') || '')) || '';
      const low = label.toLowerCase();
      if (low.includes('belum dibaca') || low.includes('unread')) return true;
      if (low.includes('dibaca oleh') || low.includes('seen by') || low.includes('delivered to')) {
        return false;
      }
      node = node.parentElement;
    }
    return false;
  };

  let nodes = Array.from(scope.querySelectorAll(SEL))
    .filter((el) => !(composer && (composer.contains(el) || el.contains(composer))))
    .filter((el) => textOf(el) && leaf(el))
    .map((el) => Object.assign(
      {
        text: textOf(el),
        kind: 'text',
        unread: unreadFlag(el),
        hidden: !visible(el),
      },
      box(el)
    ));

  // Facebook menuliskan baris ini di DOM walau elemennya tidak terlihat:
  //   "Enter, Message sent 18:10 by Pak Bagus: Berapa ya"
  //   "Enter, Message sent 18:10 by You: Ready kak"
  // Ini menyebut ARAH dan ISI pesan tanpa perlu menebak posisi sama sekali.
  const parseSent = (raw) => {
    const low = raw.toLowerCase();
    const marker = low.includes('message sent')
      ? 'message sent'
      : (low.includes('pesan terkirim') ? 'pesan terkirim' : '');
    if (!marker) return null;
    const after = raw.slice(low.indexOf(marker) + marker.length);
    const byIndex = after.toLowerCase().lastIndexOf(' by ');
    if (byIndex < 0) return null;
    const rest = after.slice(byIndex + 4);
    const colon = rest.indexOf(':');
    if (colon < 0) return null;
    const author = rest.slice(0, colon).trim();
    const text = rest.slice(colon + 1).split(/[ \t\r\n]+/).join(' ').trim();
    if (!text) return null;
    return {author: author, text: text};
  };
  const labels = [];
  const seenLabel = {};
  for (const el of Array.from(document.querySelectorAll('[aria-label]'))) {
    const parsed = parseSent(el.getAttribute('aria-label') || '');
    if (!parsed) continue;
    const key = parsed.author + '|' + parsed.text;
    if (seenLabel[key]) continue;
    seenLabel[key] = true;
    labels.push(Object.assign(parsed, {visible: visible(el)}));
  }

  // Placeholder media ("You sent a photo") kadang hanya ada di aria-label.
  const media = Array.from(scope.querySelectorAll('[aria-label*="sent a photo" i], [aria-label*="mengirim foto" i], [aria-label*="sent an attachment" i]'))
    .filter((el) => visible(el))
    .map((el) => Object.assign(
      {text: (el.getAttribute('aria-label') || '').trim(), kind: 'media'},
      box(el)
    ));
  nodes = nodes.concat(media);

  // Petunjuk struktur: apakah ini "Messenger baru" (input berupa contenteditable
  // di dalam panel) atau halaman klasik (input berupa <textarea> di form)?
  const textarea = document.querySelector('textarea[name="message"], form textarea, textarea');
  const editableInForm = document.querySelector('form div[contenteditable="true"]');
  let variant = 'tidak_diketahui';
  if (composer) variant = 'baru';
  else if (textarea || editableInForm) variant = 'klasik';

  return {
    nodes: nodes,
    labels: labels,
    composer: composer ? box(composer) : null,
    container: container ? box(container) : null,
    found_container: !!container,
    variant: variant,
    has_textarea: !!textarea,
    editable_count: document.querySelectorAll('[contenteditable="true"]').length,
    url: location.href,
    title: document.title,
    vw: window.innerWidth,
  };
}
"""

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
    # Teks cuplikan yang terlihat di daftar chat (belum tentu pesan masuk).
    preview: str = ""


@dataclass
class ChatMessage:
    key: str
    direction: str  # "in" | "out"
    text: str
    author: str = ""
    unread: bool = False  # ditandai Facebook sebagai belum dibaca
    side: str = ""  # "kiri" (lawan bicara) | "kanan" (kita) | "" (tidak diketahui)


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


# Pola teks yang BUKAN isi pesan (data nyata dari akun pengguna, 8 Nov).
_NOISE_PATTERNS = (
    re.compile(r"^\d{1,2}[.:]\d{2}$"),                        # "16:50"
    re.compile(r"^(senin|selasa|rabu|kamis|jumat|jum'at|sabtu|minggu)\b.{0,12}\d{1,2}[.:]\d{2}$"),
    re.compile(r"^(yesterday|kemarin|today|hari ini)\b.{0,12}\d{1,2}[.:]\d{2}$"),
    re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}\b.{0,10}\d{1,2}[.:]\d{2}$"),  # "8/11/2026 16.50"
    re.compile(r"^(enter|masuk|tekan enter)[,\s].*$"),          # "Enter, Conversation details"
    re.compile(r"^message sent\b.*$"),
    re.compile(r"^pesan terkirim\b.*$"),
    re.compile(r"\bend-to-end encryption\b"),
    re.compile(r"\bterenkripsi\b.*\bpanggilan\b"),
    re.compile(r"\bonly people in this chat can read\b"),
    re.compile(r"^·?\s*\d+[hmd]\b$"),                        # "· 1h"
    # "Sat 17:50" / "Yesterday 09:12": cap waktu gaya baru yang muncul sendiri.
    re.compile(r"^(mon|tue|wed|thu|fri|sat|sun)\b.{0,14}\d{1,2}[.:]\d{2}$"),
    re.compile(
        r"^(?:\d+\s+)?(?:pesan\s+)?(?:belum\s+dibaca|unread)"
        r"(?:\s+messages?)?(?:\s+dari|\s+from)?\s*$"
    ),  # "2 pesan belum dibaca dari"
    re.compile(r"^\d{1,2}\s+(?:menit|jam|hari)\s+lalu$"),      # "5 menit lalu"
)


_UNREAD_NAME_RE = re.compile(
    r"^\s*(?:\d+\s+)?(?:pesan\s+)?(?:belum\s+dibaca|unread)(?:\s+messages?)?"
    r"(?:\s+dari|\s+from)?\s*[:,]?\s*(?P<name>.*)$",
    re.IGNORECASE,
)
# Ekor metadata pada aria-label: ", 15:40" / ", Sat 17:50" / ", Yesterday 09:12" / ", · 1h"
_NAME_TIME_RE = re.compile(
    r"\s*[,·|]\s*(?:"
    r"(?:mon|tue|wed|thu|fri|sat|sun|senin|selasa|rabu|kamis|jumat|jum'at|sabtu|minggu)"
    r"|(?:yesterday|kemarin|today|hari ini)"
    r"|\d{1,2}[./]\d{1,2}[./]\d{2,4}"
    r"|\d{1,2}[.:]\d{2}"
    r"|·?\s*\d+[hmd]\b"
    r").*$",
    re.IGNORECASE,
)


def _chat_name_from(label: str) -> str:
    """Ambil nama lawan bicara dari aria-label tautan chat.

    Contoh nyata: "2 pesan belum dibaca dari Budi, 15:40" -> "Budi";
    "Budi, 16:50" -> "Budi"; "Budi" -> "Budi".
    """
    text = " ".join((label or "").split())
    if not text:
        return ""
    match = _UNREAD_NAME_RE.match(text)
    if match:
        text = match.group("name")
    text = _NAME_TIME_RE.sub("", text)
    text = text.strip(" ,·|:-")
    # Nama dari elemen pencarian/aksi ("Find friends", "Cari teman") bukan chat.
    if not text or _is_ui_noise(text):
        return ""
    if any(
        marker in text.lower()
        for marker in ("find friends", "cari teman", "cari orang", "search messages", "cari pesan")
    ):
        return ""
    return text[:80]


def _has_unread_badge(anchor) -> bool:
    """Cari penanda belum-dibaca di dalam tautan chat (tanpa menebak teks chat)."""
    try:
        found = anchor.query_selector_all(
            '[aria-label*="belum dibaca"], [aria-label*="Belum dibaca"], '
            '[aria-label*="unread"], [aria-label*="Unread"]'
        )
    except Exception:  # noqa: BLE001
        return False
    return bool(found)


def _is_ui_noise(text: str) -> bool:
    """True bila teks hanyalah label antarmuka / metadata (bukan isi pesan)."""
    cleaned = " ".join((text or "").lower().split())
    stripped = cleaned.strip(" .:·—–-\n\t")
    if not stripped:
        return True
    if stripped in UI_NOISE_LABELS:
        return True
    if any(cleaned == label or cleaned.startswith(label + " ") for label in UI_NOISE_LABELS):
        return True
    return any(pattern.search(cleaned) for pattern in _NOISE_PATTERNS)


def _clean_message_text(text: str) -> str:
    """Buang baris label UI; sisakan isi pesan sebenarnya."""
    lines = [line.strip() for line in (text or "").split("\n")]
    kept = [line for line in lines if line and not _is_ui_noise(line)]
    if not kept:
        return ""
    return "\n".join(kept).strip()


def _row_text(row) -> str:
    """Teks mentah satu baris (untuk catatan diagnosa, bukan untuk disimpan).

    ``inner_text`` didahulukan karena elemen baris sungguhan di Messenger berisi
    teks; pembungkus tanpa teks sendiri tetap punya teks dari anak-anaknya.
    """
    text = _text_of(row)
    if not text:
        for selector in MESSAGE_TEXT_SELECTORS:
            try:
                nodes = row.query_selector_all(selector)
            except Exception:  # noqa: BLE001
                nodes = []
            if nodes:
                text = " ".join(_text_of(node) for node in nodes if _text_of(node))
                break
    return " ".join((text or "").split())


LOCK_NAME = ".fbworkspace.lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import os

        if os.name == "nt":
            import subprocess

            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def acquire_profile_lock(profile_dir: Path) -> str:
    """Catat proses yang memakai profil browser. Kembalikan peringatan (bila ada).

    Chromium menolak dua proses memakai folder profil yang sama. Daripada gagal
    dengan pesan membingungkan, kita deteksi lebih awal dan beri tahu langkahnya.
    """
    lock = Path(profile_dir) / LOCK_NAME
    try:
        if lock.exists():
            try:
                pid = int(lock.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if pid and pid != os.getpid() and _pid_alive(pid):
                return (
                    f"Profil browser kemungkinan masih dipakai proses PID {pid} "
                    "(worker lain atau jendela Chromium yang belum tertutup). "
                    "Tutup jendela itu atau hentikan job yang berjalan, lalu ulangi."
                )
        lock.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    return ""


def _powershell() -> str:
    import shutil

    return shutil.which("powershell") or shutil.which("pwsh") or ""


def list_profile_browsers(profile_dir: Path) -> list[dict]:
    """Daftar proses Chromium yang memakai folder profil aplikasi ini."""
    marker = str(profile_dir)
    found: list[dict] = []
    if os.name == "nt":
        powershell = _powershell()
        if not powershell:
            return found
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' OR Name='chromium.exe'\" "
            "| Where-Object { $_.CommandLine -like '*" + marker.replace("'", "''") + "*' } "
            "| Select-Object ProcessId, Name, CommandLine | ConvertTo-Json -Compress"
        )
        try:
            out = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            ).stdout.strip()
            if out:
                data = json.loads(out)
                found = data if isinstance(data, list) else [data]
        except Exception:  # noqa: BLE001
            return found
    else:
        try:
            out = subprocess.run(
                ["pgrep", "-af", marker], capture_output=True, text=True, timeout=15, check=False
            ).stdout
            for line in out.splitlines():
                pid, _, rest = line.partition(" ")
                if pid.isdigit():
                    found.append({"ProcessId": int(pid), "CommandLine": rest[:200]})
        except Exception:  # noqa: BLE001
            return found
    return found


def inspect_browsers(profile_dir: Path) -> list[dict]:
    """Daftar proses chrome/chromium/msedge + penanda apakah milik aplikasi ini.

    Penting: Chromium Playwright juga bernama ``chrome.exe``, sama seperti Chrome
    pribadi pengguna. Karena itu pencocokan memakai baris perintah (folder profil),
    bukan nama proses.
    """
    marker = str(profile_dir)
    rows: list[dict] = []
    if os.name != "nt":
        return [
            {"pid": p.get("ProcessId"), "ours": True, "cmd": p.get("CommandLine", "")}
            for p in list_profile_browsers(profile_dir)
        ]

    powershell = _powershell()
    if not powershell:
        return rows
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' OR Name='chromium.exe' "
        "OR Name='msedge.exe'\" | Select-Object ProcessId, Name, CreationDate, CommandLine "
        "| ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=40,
            check=False,
        ).stdout.strip()
        data = json.loads(out) if out else []
    except Exception:  # noqa: BLE001
        return rows
    for item in data if isinstance(data, list) else [data]:
        cmd = str(item.get("CommandLine") or "")
        rows.append(
            {
                "pid": item.get("ProcessId"),
                "name": item.get("Name"),
                "started": str(item.get("CreationDate") or "")[:19],
                "ours": marker.lower() in cmd.lower(),
                "profile_hint": _profile_hint(cmd),
            }
        )
    rows.sort(key=lambda r: (not r["ours"], str(r.get("started") or "")))
    return rows


def _profile_hint(cmd: str) -> str:
    """Ambil penanda profil dari baris perintah (tanpa menampilkan path lengkap)."""
    lowered = cmd.lower()
    if "fbworkspace" in lowered:
        return "profil aplikasi"
    if "--remote-debugging-pipe" in lowered or "--enable-automation" in lowered:
        return "otomasi (Playwright/lainnya)"
    if "user-data-dir" in lowered:
        return "user-data-dir khusus"
    return "chrome pribadi"


def close_profile_browsers(profile_dir: Path, *, dry_run: bool = False) -> tuple[list[int], str]:
    """Tutup paksa Chromium yang memakai profil aplikasi ini (bukan Chrome pribadi).

    Hanya proses yang baris perintahnya memuat folder profil aplikasi yang
    disentuh, sehingga Chrome pribadi Anda tidak ikut tertutup.
    """
    processes = list_profile_browsers(profile_dir)
    pids = [int(p.get("ProcessId") or 0) for p in processes if p.get("ProcessId")]
    if not pids:
        return [], "Tidak ada jendela Chromium milik aplikasi ini yang berjalan."
    if dry_run:
        return pids, "Akan ditutup: PID " + ", ".join(str(p) for p in pids)
    errors: list[str] = []
    for pid in pids:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=20,
                    check=False,
                )
            else:
                os.kill(pid, 15)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{pid}: {exc.__class__.__name__}")
    release_profile_lock(profile_dir)
    note = "Ditutup: PID " + ", ".join(str(p) for p in pids)
    if errors:
        note += " (gagal: " + ", ".join(errors) + ")"
    return pids, note


def release_profile_lock(profile_dir: Path) -> None:
    lock = Path(profile_dir) / LOCK_NAME
    try:
        if lock.exists() and lock.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock.unlink()
    except OSError:
        pass


def _box(element) -> dict | None:
    """Kotak elemen di layar (None bila tidak terlihat)."""
    try:
        return element.bounding_box()
    except Exception:  # noqa: BLE001
        return None


def _conversation_area(page) -> dict | None:
    """Tentukan area percakapan dari kotak tulis.

    Ini menjawab masalah nyata: di halaman percakapan, elemen ``role="row"`` juga
    dipakai panel detail di KANAN ("Customise chat", "Media and files") dan
    sidebar di KIRI. Pesan sebenarnya berada di kolom yang sama dengan kotak tulis.
    """
    _, composer = _first_match(page, COMPOSER_SELECTORS)
    if composer is None:
        return None
    box = _box(composer)
    if not box or not box.get("width"):
        return None
    margin = 60
    return {
        "left": box["x"] - margin,
        "right": box["x"] + box["width"] + margin,
        "center": box["x"] + box["width"] / 2,
        "bottom": box["y"] + box.get("height", 0),
    }


def _in_area(box: dict | None, area: dict | None) -> bool:
    if box is None or area is None:
        return True  # tanpa informasi posisi, jangan buang (biar filter teks bekerja)
    center = box["x"] + box.get("width", 0) / 2
    return area["left"] <= center <= area["right"]


def _direction_by_position(box: dict | None, area: dict | None) -> str:
    """Di Messenger, pesan kita rata kanan, pesan pembeli rata kiri."""
    if box is None or area is None:
        return ""
    center = box["x"] + box.get("width", 0) / 2
    if center > area["center"] + 24:
        return "out"
    if center < area["center"] - 24:
        return "in"
    return ""


def filter_text_nodes(
    payload: dict,
    *,
    known_out: list[str] | None = None,
    chat_name: str = "",
    our_side: str = "",
) -> tuple[list[dict], list[str], dict]:
    """Ubah blok teks mentah dari halaman menjadi daftar pesan kandidat.

    Aturan:
    - hanya blok yang berada di **kolom kotak tulis** (bukan sidebar/panel detail),
    - hanya blok **di atas** kotak tulis,
    - blok selebar/sepandekar halaman diabaikan (itu kontainer, bukan gelembung),
    - label antarmuka dibuang.

    Kembalikan (pesan, catatan_dibuang, ringkasan).
    """
    nodes = payload.get("nodes") or []
    composer = payload.get("composer") or None
    container = payload.get("container") or None
    viewport = float(payload.get("vw") or 1280)
    dropped: list[str] = []
    meta: dict = {
        "raw_nodes": len(nodes),
        "composer": bool(composer),
        "container": bool(container),
    }

    if composer:
        area = {
            "left": composer["x"] - 60,
            "right": composer["x"] + composer["w"] + 60,
            "center": composer["x"] + composer["w"] / 2,
            "bottom": composer["y"] + composer.get("h", 0),
        }
    else:
        # Tanpa kotak tulis: pakai blok teks terbanyak sebagai kolom percakapan.
        xs = sorted(n["x"] + n["w"] / 2 for n in nodes) or [viewport / 2]
        middle = xs[len(xs) // 2]
        area = {"left": middle - 320, "right": middle + 320, "center": middle, "bottom": 10**6}
        dropped.append("(kotak tulis tidak ditemukan -> kolom ditebak)")

    title = " ".join((chat_name or "").lower().split())
    known = {" ".join((t or "").lower().split())[:60] for t in (known_out or []) if t}
    # Node tanpa ukuran (0x0) atau "hidden": posisinya tidak bisa dipercaya, jadi
    # jangan dibuang karena posisi. Ini sumber hilangnya "Berapa ya" (8 Nov 19:17).
    unknown_box = [
        node
        for node in nodes
        if not node.get("w") or not node.get("h") or node.get("hidden")
    ]
    if unknown_box:
        meta["tanpa_ukuran"] = len(unknown_box)
    media_count = 0
    messages: list[dict] = []
    for node in nodes:
        text_raw = " ".join(str(node.get("text") or "").split())
        if not text_raw:
            continue
        if container and node.get("w") and node.get("h"):
            top = container["y"] - 4
            bottom_of_container = container["y"] + container.get("h", 0) + 4
            node_center_y = node.get("y", 0) + node.get("h", 0) / 2
            if not (top <= node_center_y <= bottom_of_container):
                dropped.append(f"(di luar wadah percakapan) {text_raw[:40]}")
                continue
        has_box = bool(node.get("w")) and bool(node.get("h"))
        center = node["x"] + node.get("w", 0) / 2
        if has_box and not (area["left"] <= center <= area["right"]):
            # Simpan baris ini supaya bisa dipakai bila ternyata pesan sungguhan.
            dropped.append(f"(di luar kolom) {text_raw[:40]}")
            continue
        if has_box and node.get("y", 0) + node.get("h", 0) > area["bottom"] + 10:
            dropped.append(f"(di bawah kotak tulis) {text_raw[:40]}")
            continue
        if has_box and (node.get("w", 0) > viewport * 0.75 or node.get("h", 0) > 400):
            dropped.append(f"(kontainer) {text_raw[:40]}")
            continue
        # Judul percakapan (nama pembeli) muncul di dalam wadah: bukan pesan.
        if title and " ".join(text_raw.lower().split()) == title:
            dropped.append(f"(judul chat) {text_raw[:40]}")
            continue
        text = _clean_message_text(str(node.get("text") or ""))
        if not text:
            dropped.append(f"(label UI) {text_raw[:40]}")
            continue
        # Placeholder media: tidak ada teks untuk dibaca -> hitung, jangan diproses.
        if node.get("kind") == "media" or "sent a photo" in text.lower() or (
            "mengirim foto" in text.lower()
        ):
            media_count += 1
            dropped.append(f"(media) {text_raw[:40]}")
            continue
        if len(text) > 1200:
            dropped.append(f"(kepanjangan) {text_raw[:40]}")
            continue
        # Pesan kita menempel ke sisi kanan kolom, pesan pembeli ke sisi kiri.
        # Memakai jarak tepi (bukan titik tengah) supaya gelembung pendek
        # seperti "coba" atau "up" tetap terbaca sebagai pesan kita.
        right_gap = area["right"] - (node["x"] + node.get("w", 0))
        left_gap = node["x"] - area["left"]
        if abs(right_gap - left_gap) <= 12:
            direction = "in"
            for sent in known_out or []:
                if sent and text_raw[:60].lower() == " ".join(sent.lower().split())[:60]:
                    direction = "out"
                    break
        else:
            direction = "out" if right_gap < left_gap else "in"
        # Penanda "belum dibaca" dari Facebook menang atas dugaan dari posisi.
        if node.get("unread"):
            direction = "in"
        has_box = bool(node.get("w")) and bool(node.get("h"))
        messages.append(
            {
                "text": text,
                "direction": direction,
                "y": node.get("y", 0) if has_box else None,
                "unread": bool(node.get("unread")),
                "side": _side_of(node, area) if has_box else "",
                "known": text[:60] in known,
                "no_box": not has_box,
            }
        )

    # Node tanpa ukuran tidak punya posisi yang bisa dipercaya. Pesan seperti itu
    # biasanya pesan terbaru (belum sempat dilayout saat dibaca), jadi diletakkan
    # di akhir sesuai urutan DOM — bukan di awal karena y=0.
    known_y = [msg["y"] for msg in messages if msg["y"] is not None]
    tail = (max(known_y) + 1) if known_y else 0
    for msg in messages:
        if msg["y"] is None:
            tail += 1
            msg["y"] = tail

    # Sisi kita = sisi yang memuat teks balasan yang kita catat pernah terkirim.
    # Ini jauh lebih andal daripada menebak dari lebar gelembung.
    counts = {"kiri": 0, "kanan": 0}
    for msg in messages:
        if msg.pop("known", False):
            counts[msg["side"]] += 1
    our_side = ""
    if counts["kanan"] != counts["kiri"]:
        our_side = "kanan" if counts["kanan"] > counts["kiri"] else "kiri"
    meta["our_side"] = our_side
    for msg in messages:
        if our_side and not msg.get("no_box"):
            msg["direction"] = "out" if msg["side"] == our_side else "in"
        if msg.get("unread"):
            msg["direction"] = "in"
        msg.pop("no_box", None)

    messages.sort(key=lambda m: m["y"])
    messages = _merge_labels(messages, payload.get("labels") or [])
    meta["labels"] = len(payload.get("labels") or [])
    meta["used"] = len(messages)
    meta["lines"] = [
        f"{msg['direction']}{'(belum dibaca)' if msg.get('unread') else ''}: {msg['text'][:40]}"
        for msg in messages[-8:]
    ]
    meta["media"] = media_count
    meta["found_container"] = bool(payload.get("found_container"))
    return messages, dropped, meta


_LABEL_AUTHOR_RE = re.compile(
    r"^(?:message\s+sent|pesan\s+terkirim)\b", re.IGNORECASE
)


def _same_text(left: str, right: str) -> bool:
    """Bandingkan dua teks pesan dengan toleransi cuplikan/emoji."""
    a = " ".join((left or "").lower().split())[:40]
    b = " ".join((right or "").lower().split())[:40]
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def _merge_labels(messages: list[dict], labels: list[dict], *, our_name: str = "") -> list[dict]:
    """Pakai aria-label "Message sent ... by X: ..." sebagai bukti arah & isi.

    Label ini berasal dari Facebook sendiri, jadi lebih dapat dipercaya daripada
    menebak dari posisi gelembung. Pesan yang hanya ada di label (tidak terlihat
    sebagai blok teks) ditambahkan, karena isinya nyata.
    """
    if not labels:
        return messages
    by_text: dict[str, dict] = {}
    for msg in messages:
        by_text.setdefault(" ".join(msg["text"].lower().split())[:40], msg)

    used: set[int] = set()
    for label in labels:
        author = str(label.get("author") or "").strip()
        text = str(label.get("text") or "").strip()
        if not text:
            continue
        ours = author.lower() in {"you", "anda", "kamu"}
        match = None
        for index, msg in enumerate(messages):
            if index in used:
                continue
            if _same_text(msg["text"], text):
                match = msg
                used.add(index)
                break
        if match is not None:
            # Penanda "belum dibaca" tetap menang: pesan yang belum dibaca pasti
            # pesan masuk, apa pun kata label.
            if not match.get("unread"):
                match["direction"] = "out" if ours else "in"
            match["from_label"] = True
            continue
        messages.append(
            {
                "text": text,
                "direction": "out" if ours else "in",
                "y": None,
                "unread": False,
                "side": "",
                "known": False,
                "no_box": True,
                "from_label": True,
            }
        )
    return messages


def _side_of(node: dict, area: dict) -> str:
    """Sisi gelembung: 'kiri' (lawan bicara) atau 'kanan' (kita)."""
    right_gap = area["right"] - (node["x"] + node.get("w", 0))
    left_gap = node["x"] - area["left"]
    return "kanan" if right_gap < left_gap else "kiri"


# Permintaan eksplisit ("berapa ya") vs sapaan/obrolan ("iyaa", "halo kak").
_REQUEST_WORDS = (
    "berapa", "harga", "hrg", "ready", "stok", "stock", "ada gak", "ada ga", "ada nggak",
    "masih ada", "cod", "lokasi", "alamat", "dimana", "di mana", "minta no", "nomor",
    "no wa", "wa nya", "wa-nya", "bisa", "boleh", "nego", "kurang", "diskon", "tawar",
    "tukar", "kirim", "ongkir", "mahal", "murah", "gan", "kak?",
)


def looks_like_request(text: str) -> bool:
    """True bila teks tampak seperti pertanyaan/permintaan nyata."""
    lowered = " ".join((text or "").lower().split())
    if not lowered:
        return False
    if "?" in lowered:
        return True
    return any(word in lowered for word in _REQUEST_WORDS)


def pick_answer_target(messages: list[dict], *, answered_texts: set[str] | None = None) -> dict | None:
    """Tentukan pesan lawan bicara yang harus dijawab.

    Urutan prioritas (jujur, tanpa menebak urutan waktu antar-sisi):
    1. pesan yang ditandai **belum dibaca** oleh Facebook;
    2. pesan lawan bicara yang **bukan** sekadar sapaan/ucapan terima kasih;
    3. tidak ada -> ``None`` (worker diam).
    """
    answered = answered_texts or set()

    def answered_(msg: dict) -> bool:
        return " ".join(str(msg.get("text") or "").lower().split())[:60] in answered

    lawan = [
        msg
        for msg in messages
        if msg.get("direction") == "in" and not answered_(msg)
    ]
    if not lawan:
        return None

    unread = [msg for msg in lawan if msg.get("unread")]
    if unread:
        return unread[-1]

    permintaan = [msg for msg in lawan if looks_like_request(msg.get("text", ""))]
    if permintaan:
        return permintaan[-1]
    return None


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
        # Peringatan bila profil tampaknya masih dipakai proses lain.
        self.lock_warning = ""
        # Cuplikan kandidat pesan yang dibuang/terpakai — untuk diagnosa.
        self.last_debug: dict = {}
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
        self.lock_warning = acquire_profile_lock(self.profile_dir)
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
            return (
                "Jendela Chromium tertutup sebelum selesai (sering karena jendela yang "
                "sama ditutup, atau profil sedang dipakai proses Chromium lain). "
                "Tutup semua jendela Chromium milik aplikasi ini lalu ulangi."
            )
        if "profile" in lowered and ("in use" in lowered or "lock" in lowered):
            return (
                "Profil browser sedang dipakai proses lain. Tutup jendela Chromium "
                "lain milik aplikasi ini, lalu coba lagi."
            )
        if "singleton" in lowered or "processsingleton" in lowered:
            return (
                "Profil browser dipakai jendela Chromium lain (ProcessSingleton). "
                "Tutup semua jendela Chromium milik aplikasi ini, lalu ulangi."
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

    def _chromium_executable(self, playwright) -> tuple[bool, str]:
        """Pastikan browser Chromium Playwright benar-benar ada sebelum dipakai."""
        try:
            path = playwright.chromium.executable_path
        except Exception as exc:  # noqa: BLE001
            return False, self._friendly_error(exc)
        if path and Path(path).exists():
            return True, ""
        return False, (
            "Chromium Playwright belum terpasang. Jalankan sekali di PowerShell: "
            ".\\.venv\\Scripts\\python.exe -m playwright install chromium"
        )

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
            ready, why = self._chromium_executable(playwright)
            if not ready:
                diagnostics = {"status": "kesalahan", "stage": "chromium", "error": why}
                self.last_diagnostics = diagnostics
                self._close(playwright, None)
                return ThreadList(status="kesalahan", diagnostics=diagnostics)
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

            payload_hint: dict = {}
            try:
                payload_hint = page.evaluate(EXTRACT_JS) or {}
            except Exception:  # noqa: BLE001
                payload_hint = {}

            pin_state = self._handle_pin(page, stage="daftar_chat")
            if pin_state == "terisi":
                selector, anchors = _first_match_all(page, THREAD_SELECTORS)

            diagnostics = self._inspect(page)
            if self.lock_warning:
                diagnostics["peringatan"] = self.lock_warning
            diagnostics["thread_selector"] = selector or ""
            diagnostics["attempts"] = attempts
            diagnostics["pin"] = self.pin_status
            diagnostics["variant"] = payload_hint.get("variant", "")
            diagnostics["has_textarea"] = payload_hint.get("has_textarea")

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
                unread = "belum dibaca" in label.lower() or "unread" in label.lower()
                if not unread:
                    unread = _has_unread_badge(anchor)
                # aria-label sering berbunyi "2 pesan belum dibaca dari Budi, 16:50"
                # atau "Budi, 16:50" -> ambil nama orangnya, bukan label utuh.
                name = _chat_name_from(label) or _text_of(anchor).split("\n")[0][:80]
                preview = _text_of(anchor).split("\n")[-1][:120] if _text_of(anchor) else ""
                threads.append(
                    Thread(
                        key=key,
                        name=name or key,
                        url=url,
                        unread=unread,
                        preview=preview,
                    )
                )
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
            release_profile_lock(self.profile_dir)

    def list_threads(self, limit: int = 8) -> tuple[list[Thread], str]:
        result = self.list_threads_detailed(limit=limit)
        return result.threads, result.status

    # ------------------------------------------------------------- baca isi chat
    def read_thread(
        self,
        url: str,
        *,
        known_out: list[str] | None = None,
        thread_name: str = "",
    ) -> ThreadRead:
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

            # Ekstraksi berbasis blok teks: role="row" di Messenger dipakai untuk
            # sidebar & panel detail, sedangkan isi pesan ada di elemen dir="auto".
            payload = {}
            try:
                payload = page.evaluate(EXTRACT_JS) or {}
            except Exception as exc:  # noqa: BLE001
                self.last_diagnostics = dict(
                    diagnostics, status="kesalahan", error=self._friendly_error(exc)
                )
                return ThreadRead(status="kesalahan", note="gagal membaca blok teks halaman")
            texts, dropped, meta = filter_text_nodes(
                payload, known_out=known_out, chat_name=thread_name
            )
            messages = [
                ChatMessage(
                    key=_msg_key(thread_key, index, item["text"]),
                    direction=item["direction"],
                    text=item["text"][:2000],
                    unread=bool(item.get("unread")),
                    side=str(item.get("side") or ""),
                )
                for index, item in enumerate(texts)
            ]
            selector = "dir=auto"
            self.last_debug = {
                "raw_nodes": meta.get("raw_nodes", 0),
                "used": len(messages),
                "dropped": dropped[:15],
                "composer": meta.get("composer", False),
                "lines": meta.get("lines", []),
                "our_side": meta.get("our_side", ""),
                "labels": meta.get("labels", 0),
                "tanpa_ukuran": meta.get("tanpa_ukuran", 0),
            }
            if not messages:
                self.last_diagnostics = dict(
                    diagnostics,
                    status="tidak_ada_pesan",
                    messages=0,
                    conversation_selector=selector,
                    debug=self.last_debug,
                )
                total = meta.get("raw_nodes", 0)
                return ThreadRead(
                    status="tidak_ada_pesan",
                    note=(
                        f"{total} blok teks, 0 pesan lolos "
                        f"(kotak tulis {'ada' if meta.get('composer') else 'tidak ditemukan'}, "
                        f"wadah {'ada' if meta.get('container') else 'tidak ditemukan'}, "
                        f"media {meta.get('media', 0)})"
                    ),
                )

            self.last_diagnostics = dict(
                diagnostics,
                status="ok",
                messages=len(messages),
                conversation_selector=selector,
                debug=self.last_debug,
            )
            return ThreadRead(
                messages=messages, status="ok", note=f"{len(messages)} pesan terbaca"
            )
        except Exception as exc:  # noqa: BLE001
            return ThreadRead(status="kesalahan", note=self._friendly_error(exc))
        finally:
            self._close(playwright, context)
            release_profile_lock(self.profile_dir)

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
            release_profile_lock(self.profile_dir)


def diagnose(profile_dir: str | Path, *, headless: bool = False) -> dict:
    """Buka inbox & percakapan PERTAMA, laporkan apa yang terlihat.

    Tidak mengirim apa pun. Selain keadaan halaman, fungsi ini melaporkan pesan
    yang benar-benar terbaca pada percakapan pertama, sehingga penyetelan selector
    bisa diverifikasi tanpa menjalankan worker.
    """
    adapter = MessengerAdapter(profile_dir=profile_dir, headless=headless)
    ok, why = adapter.is_available()
    if not ok:
        return {"ok": False, "error": why}
    result = adapter.list_threads_detailed(limit=10)
    payload = dict(result.diagnostics)
    payload["status"] = result.status
    if getattr(adapter, "lock_warning", ""):
        payload.setdefault("peringatan", adapter.lock_warning)
    payload["threads"] = [
        {"key": item.key, "name": item.name, "unread": item.unread, "preview": item.preview}
        for item in result.threads
    ]
    conversations = []
    for thread in result.threads[:5]:
        try:
            read = adapter.read_thread(thread.url, thread_name=thread.name)
        except Exception as exc:  # noqa: BLE001
            conversations.append(
                {"nama": thread.name, "status": "kesalahan", "catatan": str(exc)[:200]}
            )
            continue
        conversations.append(
            {
                "nama": thread.name,
                "status": read.status,
                "catatan": read.note,
                "jumlah_pesan": len(read.messages),
                "pesan": [
                    {
                        "arah": m.direction,
                        "teks": m.text[:120],
                        "belum_dibaca": bool(getattr(m, "unread", False)),
                    }
                    for m in read.messages[-10:]
                ],
                "debug": adapter.last_debug,
            }
        )
    if conversations:
        payload["percakapan"] = conversations
        payload["percakapan_pertama"] = conversations[0]
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
    parser.add_argument(
        "--list-browsers",
        action="store_true",
        help="Tampilkan proses chrome/msedge + mana yang milik aplikasi ini",
    )
    parser.add_argument(
        "--close-browsers",
        action="store_true",
        help="Tutup paksa jendela Chromium milik aplikasi ini (Chrome pribadi tidak disentuh)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Hanya tampilkan PID, tidak menutup apa pun"
    )
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent.parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from fbworkspace.config import get_settings  # noqa: PLC0415
    from fbworkspace.normalize import collapse_ws  # noqa: PLC0415

    settings = get_settings()
    profile = args.profile or str(settings.browser_profile_dir)

    if args.list_browsers:
        rows = inspect_browsers(Path(profile))
        if not rows:
            print("Tidak ada proses chrome/chromium/msedge yang terdeteksi.")
            return 0
        print(f"{'PID':>7}  {'milik aplikasi':<15} {'mulai':<19} keterangan")
        print("-" * 76)
        for row in rows:
            print(
                f"{str(row.get('pid')):>7}  "
                f"{('YA' if row.get('ours') else 'bukan'):<15} "
                f"{str(row.get('started'))[:19]:<19} {row.get('profile_hint', '')}"
            )
        ours = [r for r in rows if r.get("ours")]
        print()
        print(
            f"{len(ours)} proses milik aplikasi ini. Chrome pribadi TIDAK akan disentuh "
            "oleh --close-browsers."
        )
        return 0

    if args.close_browsers:
        pids, note = close_profile_browsers(Path(profile), dry_run=args.dry_run)
        print(note)
        return 0 if pids or "Tidak ada" in note else 1

    started = time.time()
    lock_note = acquire_profile_lock(Path(profile))
    if lock_note:
        print(f"⚠️  {lock_note}\n")
    payload = diagnose(profile, headless=not args.show)
    release_profile_lock(Path(profile))
    if lock_note:
        payload["peringatan"] = lock_note
    payload["profile"] = str(profile)
    payload["detik"] = round(time.time() - started, 1)
    report = _save_report(payload, settings)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if report is not None:
        print(f"\nLaporan disimpan: {collapse_ws(str(report))}")
    return 0 if payload.get("status") == "ok" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
