"""Kontrak adapter browser.

Adapter TIDAK boleh:
- memecahkan CAPTCHA / tantangan keamanan,
- menyamarkan otomasi (stealth, UA palsu, sidik jari palsu),
- memutar proxy untuk menghindari pembatasan,
- mencoba ulang tanpa batas setelah diblokir.

Adapter WAJIB berhenti dan melaporkan status bila menemui login/challenge/penolakan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..models import AccessStatus, CollectLimits, RawPost

ProgressCallback = Callable[[str, str, dict], None]
# (level, message, counters) -> None ; level: info|warning|error


@dataclass
class CollectTarget:
    """Apa yang mau dibaca."""

    group_urls: list[str]
    limits: CollectLimits
    note: str = ""


@dataclass
class CollectOutcome:
    """Hasil satu sesi pembacaan."""

    status: AccessStatus
    posts: list[RawPost] = field(default_factory=list)
    message: str = ""
    diagnostics: dict = field(default_factory=dict)
    # Postingan yang sudah terkumpul sebelum berhenti tetap dipertahankan,
    # tetapi statusnya jujur: proses tidak selesai normal.

    @property
    def stopped_normally(self) -> bool:
        return self.status in {AccessStatus.OK, AccessStatus.OK_EMPTY}


class BrowserAdapter(Protocol):
    """Interface minimal agar adapter bisa ditukar."""

    name: str

    def is_available(self) -> tuple[bool, str]:
        """Apakah adapter siap dipakai di mesin ini."""
        ...

    def collect(
        self,
        target: CollectTarget,
        should_stop: Callable[[], bool] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> CollectOutcome:
        ...
