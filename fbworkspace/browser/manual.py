"""Adapter 'manual': tidak membuka browser sama sekali.

Dipakai ketika pembacaan otomatis tidak layak/tidak diizinkan, atau saat
pengguna ingin menguji filter tanpa menyentuh Facebook. Pengguna menempelkan
teks postingan sendiri di tab Input Manual.
"""

from __future__ import annotations

from ..models import AccessStatus
from .base import CollectOutcome, CollectTarget, ProgressCallback


class ManualAdapter:
    name = "manual"

    def is_available(self) -> tuple[bool, str]:
        return True, "Mode manual: tidak ada akses otomatis ke Facebook."

    def collect(
        self,
        target: CollectTarget,
        should_stop=None,
        on_progress: ProgressCallback | None = None,
    ) -> CollectOutcome:
        if on_progress:
            on_progress(
                "info",
                "Mode manual dipilih. Tidak ada browser yang dijalankan.",
                {"read": 0, "inserted": 0},
            )
        return CollectOutcome(
            status=AccessStatus.OK_EMPTY,
            posts=[],
            message=(
                "Mode manual: tempelkan teks postingan di tab 'Input Manual'. "
                "Aplikasi tidak membuka Facebook."
            ),
            diagnostics={"adapter": "manual"},
        )
