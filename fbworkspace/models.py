"""Konstanta & tipe data bersama untuk FB Workspace."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

# Penanda untuk data yang tidak tersedia. JANGAN diganti string kosong,
# agar field kosong tidak tertukar dengan "memang tidak ada".
UNKNOWN = "tidak diketahui"


class Source(str, Enum):
    BROWSER = "browser"
    MANUAL = "manual"
    DEMO = "demo"
    FILE = "file"


class WorkflowStatus(str, Enum):
    NEW = "baru"
    REVIEWED = "diperiksa"
    RELEVANT = "relevan"
    IGNORED = "diabaikan"


WORKFLOW_STATUS_LABELS = {
    WorkflowStatus.NEW.value: "Baru",
    WorkflowStatus.REVIEWED.value: "Diperiksa",
    WorkflowStatus.RELEVANT.value: "Relevan",
    WorkflowStatus.IGNORED.value: "Diabaikan",
}


class AnalysisStatus(str, Enum):
    NONE = "belum dianalisis"
    RULE = "dianalisis (aturan)"
    OLLAMA = "dianalisis (Ollama)"
    FAILED = "analisis gagal"


class Category(str, Enum):
    BUYING = "mencari"
    SELLING = "menjual"
    DISCUSSION = "diskusi"
    IRRELEVANT = "tidak relevan"


class PriceKind(str, Enum):
    ASKING = "harga"
    BUDGET = "budget"
    UNKNOWN = "tidak diketahui"


class JobStatus(str, Enum):
    QUEUED = "antre"
    RUNNING = "berjalan"
    COMPLETED = "selesai"
    STOPPED = "dihentikan"
    FAILED = "gagal"
    BLOCKED = "diblokir/tidak diizinkan"


class AccessStatus(str, Enum):
    """Hasil pemeriksaan akses halaman grup."""

    OK = "ok"
    OK_EMPTY = "ok_tanpa_postingan"
    LOGIN_REQUIRED = "perlu_login"
    CHECKPOINT = "tantangan_keamanan"
    PERMISSION_DENIED = "akses_ditolak"
    RATE_LIMITED = "dibatasi_sementara"
    NOT_FOUND = "halaman_tidak_ditemukan"
    ERROR = "kesalahan"
    STOPPED = "dihentikan_pengguna"


ACCESS_STATUS_MESSAGES = {
    AccessStatus.OK: "Halaman grup bisa dibaca.",
    AccessStatus.OK_EMPTY: "Halaman terbuka, tetapi tidak ada postingan yang bisa dibaca.",
    AccessStatus.LOGIN_REQUIRED: (
        "Facebook meminta login. Silakan login sendiri di jendela browser yang terbuka, "
        "lalu jalankan ulang. Aplikasi tidak mengumpulkan kredensial Anda."
    ),
    AccessStatus.CHECKPOINT: (
        "Facebook menampilkan tantangan keamanan (checkpoint/CAPTCHA). Proses dihentikan. "
        "Aplikasi tidak mencoba melewatinya. Selesaikan secara manual atau hentikan penggunaan."
    ),
    AccessStatus.PERMISSION_DENIED: (
        "Akses ke grup ditolak atau grup tidak publik bagi akun ini. Proses dihentikan."
    ),
    AccessStatus.RATE_LIMITED: (
        "Facebook membatasi permintaan sementara. Proses dihentikan, tidak ada percobaan ulang otomatis."
    ),
    AccessStatus.NOT_FOUND: "Halaman grup tidak ditemukan. Periksa kembali tautannya.",
    AccessStatus.ERROR: "Terjadi kesalahan saat membuka halaman. Lihat detail teknis.",
    AccessStatus.STOPPED: "Proses dihentikan oleh pengguna.",
}


def utcnow_iso() -> str:
    """Waktu UTC dalam format ISO-8601 (untuk penyimpanan)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class RawPost:
    """Postingan mentah dari adapter browser / input pengguna (belum divalidasi)."""

    text: str
    group_url: str = ""
    post_url: str | None = None
    posted_at_raw: str | None = None
    source: str = Source.BROWSER.value


@dataclass
class FilterConfig:
    """Kriteria filter kata kunci & atribut."""

    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    price_min: int | None = None
    price_max: int | None = None
    require_iphone: bool = True

    def is_empty(self) -> bool:
        return not (
            self.include_keywords
            or self.exclude_keywords
            or self.locations
            or self.price_min is not None
            or self.price_max is not None
            or self.require_iphone
        )


@dataclass
class FilterResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


@dataclass
class AnalysisResult:
    """Hasil analisis satu postingan (aturan atau Ollama)."""

    category: str = Category.IRRELEVANT.value
    iphone_model: str = UNKNOWN
    price_amount: int | None = None
    price_kind: str = PriceKind.UNKNOWN.value
    location: str = UNKNOWN
    details: dict[str, str] = field(default_factory=dict)
    relevance_score: int = 0
    reasons: list[str] = field(default_factory=list)
    snippet: str = ""
    engine: str = "rule"

    def as_dict(self) -> dict:
        return {
            "category": self.category,
            "iphone_model": self.iphone_model,
            "price_amount": self.price_amount,
            "price_kind": self.price_kind,
            "location": self.location,
            "details": self.details,
            "relevance_score": self.relevance_score,
            "reasons": self.reasons,
            "snippet": self.snippet,
            "engine": self.engine,
        }


@dataclass
class CollectLimits:
    """Batas pengaman untuk satu sesi pembacaan."""

    max_posts: int = 10
    max_seconds: int = 180

    def clamp(self, hard_max: int = 100) -> "CollectLimits":
        return CollectLimits(
            max_posts=max(1, min(int(self.max_posts), hard_max)),
            max_seconds=max(10, min(int(self.max_seconds), 3600)),
        )
