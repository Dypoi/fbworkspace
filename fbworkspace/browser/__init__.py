"""Adapter browser.

Versi MVP hanya punya satu adapter nyata (Playwright) plus adapter manual.
Adapter dipisah dari dashboard agar mudah diganti/dimatikan tanpa mengubah
logika database, filter, dan analisis.
"""

from .base import BrowserAdapter, CollectLimits, CollectOutcome, CollectTarget, RawPost
from .manual import ManualAdapter

__all__ = [
    "BrowserAdapter",
    "CollectLimits",
    "CollectOutcome",
    "CollectTarget",
    "RawPost",
    "ManualAdapter",
]
