"""Konfigurasi aplikasi FB Workspace.

Semua nilai dibaca dari environment / file .env (lokal, tidak di-commit).
Tidak ada rahasia yang disimpan di kode.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Root proyek = folder yang berisi package fbworkspace/
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Muat .env kalau ada (tidak error kalau tidak ada)
load_dotenv(PROJECT_ROOT / ".env")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "ya", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def resolve_path(value: str) -> Path:
    """Path relatif diartikan relatif terhadap root proyek."""
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()


@dataclass(frozen=True)
class Settings:
    """Konfigurasi yang aman untuk laptop pribadi."""

    # --- Dashboard ---
    # Default 127.0.0.1: hanya bisa diakses dari laptop sendiri (localhost).
    host: str = field(default_factory=lambda: os.getenv("FBW_HOST", "127.0.0.1").strip())
    port: int = field(default_factory=lambda: _env_int("FBW_PORT", 8501))

    # --- Database ---
    db_path: Path = field(
        default_factory=lambda: resolve_path(os.getenv("FBW_DB_PATH", "data/fbworkspace.db"))
    )

    # --- Batas pengujian ---
    default_max_posts: int = field(default_factory=lambda: _env_int("FBW_MAX_POSTS", 10))
    default_max_minutes: int = field(default_factory=lambda: _env_int("FBW_MAX_MINUTES", 3))
    hard_max_posts: int = field(default_factory=lambda: _env_int("FBW_HARD_MAX_POSTS", 100))

    # --- Browser ---
    browser_headless: bool = field(
        default_factory=lambda: _env_bool("FBW_BROWSER_HEADLESS", False)
    )
    browser_profile_dir: Path = field(
        default_factory=lambda: resolve_path(
            os.getenv("FBW_BROWSER_PROFILE_DIR", "data/browser_profile")
        )
    )
    browser_slow_mo_ms: int = field(
        default_factory=lambda: _env_int("FBW_BROWSER_SLOW_MO_MS", 120)
    )
    # Berapa lama browser DIBIARKAN TERBUKA menunggu Anda login saat Facebook
    # meminta login. 0 = langsung berhenti (perilaku lama).
    login_wait_seconds: int = field(
        default_factory=lambda: _env_int("FBW_LOGIN_WAIT_SECONDS", 180)
    )

    # --- Ollama (opsional) ---
    ollama_url: str = field(
        default_factory=lambda: os.getenv("FBW_OLLAMA_URL", "http://127.0.0.1:11434").strip()
    )
    ollama_model: str = field(
        default_factory=lambda: os.getenv("FBW_OLLAMA_MODEL", "qwen2.5:3b").strip()
    )
    ollama_timeout: float = field(
        default_factory=lambda: _env_float("FBW_OLLAMA_TIMEOUT", 60.0)
    )
    ollama_max_input_chars: int = field(
        default_factory=lambda: _env_int("FBW_OLLAMA_MAX_INPUT_CHARS", 2500)
    )
    ollama_enabled: bool = field(
        default_factory=lambda: _env_bool("FBW_OLLAMA_ENABLED", False)
    )
    # Qwen3 dan sebagian model lain punya mode "thinking". Untuk ekstraksi JSON
    # pendek, thinking justru menghabiskan kuota token -> matikan secara default.
    ollama_think: bool = field(
        default_factory=lambda: _env_bool("FBW_OLLAMA_THINK", False)
    )
    ollama_num_predict: int = field(
        default_factory=lambda: _env_int("FBW_OLLAMA_NUM_PREDICT", 512)
    )

    # --- Balas chat (Messenger) ---
    # Nomor pemilik untuk notifikasi eskalasi (format 62...).
    wa_phone: str = field(default_factory=lambda: os.getenv("FBW_WA_PHONE", "081513839973").strip())
    wa_profile_dir: Path = field(
        default_factory=lambda: resolve_path(os.getenv("FBW_WA_PROFILE_DIR", "data/wa_profile"))
    )
    messenger_max_threads: int = field(
        default_factory=lambda: _env_int("FBW_MESSENGER_MAX_THREADS", 8)
    )
    messenger_max_replies: int = field(
        default_factory=lambda: _env_int("FBW_MESSENGER_MAX_REPLIES", 10)
    )
    # Jeda antar balasan (detik) — pengaman agar tidak terlihat seperti spam.
    messenger_gap_seconds: int = field(
        default_factory=lambda: _env_int("FBW_MESSENGER_GAP_SECONDS", 45)
    )
    # Jangan balas chat yang sama lebih sering dari ini.
    messenger_cooldown_seconds: int = field(
        default_factory=lambda: _env_int("FBW_MESSENGER_COOLDOWN_SECONDS", 600)
    )
    # Bawaannya uji kering: tidak ada pesan yang benar-benar dikirim.
    messenger_dry_run: bool = field(
        default_factory=lambda: _env_bool("FBW_MESSENGER_DRY_RUN", True)
    )
    # Variasi gaya bahasa balasan dengan Ollama (fakta tetap dari template).
    # Hanya jalan bila Ollama juga diaktifkan (FBW_OLLAMA_ENABLED=true).
    messenger_ai_style: bool = field(
        default_factory=lambda: _env_bool("FBW_MESSENGER_AI_STYLE", True)
    )

    # --- Folder ---
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")
    log_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "logs")

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def describe_safety(self) -> dict[str, str]:
        """Ringkasan singkat untuk ditampilkan di dashboard."""
        local_only = self.host in {"127.0.0.1", "localhost", "::1"}
        return {
            "bind": f"{self.host}:{self.port}",
            "lokal_saja": "ya" if local_only else "TIDAK (hati-hati!)",
            "database": str(self.db_path),
            "profil_browser": str(self.browser_profile_dir),
            "tunggu_login": f"{self.login_wait_seconds} detik",
            "ollama": f"{self.ollama_url} (model: {self.ollama_model})",
            "ollama_aktif": "ya" if self.ollama_enabled else "tidak",
        }


def get_settings() -> Settings:
    """Ambil settings terbaru (dibaca ulang setiap dipanggil).

    Sengaja tidak di-cache supaya perubahan .env cukup dengan refresh dashboard.
    """
    return Settings()
