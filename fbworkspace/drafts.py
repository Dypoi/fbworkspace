"""Draf respons netral untuk diperiksa & dikirim MANUAL oleh pengguna.

Aturan keras modul ini:
1. TIDAK mengirim apa pun. Hanya menghasilkan teks.
2. TIDAK mengarang stok, harga, garansi, alamat, atau kemampuan layanan.
   Semua hal spesifik ditulis sebagai placeholder dalam kurung siku.
3. Nada sopan, singkat, tanpa tekanan/urgensi palsu.

Profil bisnis (opsional) hanya berisi identitas umum: nama toko, area, jam respons.
Jangan mengisi katalog harga di sini sebelum Anda benar-benar punya datanya.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import UNKNOWN

# Kata/frasa yang TIDAK boleh muncul dalam draf otomatis (klaim yang tidak boleh dikarang)
FORBIDDEN_CLAIMS = (
    "ready stock",
    "stok tersedia",
    "stok ready",
    "garansi resmi",
    "free garansi",
    "harga termurah",
    "termurah",
    "pasti original",
    "100% original",
    "pasti bisa",
    "dijamin",
    "cashback",
    "diskon",
)


@dataclass
class BusinessProfile:
    """Identitas bisnis minimal. Kosongkan bila belum ada."""

    nama_toko: str = "[NAMA TOKO]"
    area: str = "[AREA ANDA]"
    jam_respons: str = "[JAM RESPONS]"

    def is_filled(self) -> bool:
        return "[" not in self.nama_toko + self.area + self.jam_respons

    def as_dict(self) -> dict:
        return {
            "nama_toko": self.nama_toko,
            "area": self.area,
            "jam_respons": self.jam_respons,
        }


@dataclass
class Draft:
    title: str
    body: str
    notes: list[str] = field(default_factory=list)


_TEMPLATES = {
    "mencari": (
        "Halo, saya lihat postingannya tentang {model}.\n"
        "\n"
        "Boleh dibantu info lebih detail? Misalnya:\n"
        "- budget yang sudah disiapkan\n"
        "- kriteria yang paling penting (storage / kondisi / garansi)\n"
        "- area untuk COD\n"
        "\n"
        "Kalau cocok, saya bantu cek ketersediaan dan opsi yang sesuai. "
        "Belum bisa saya pastikan stok maupun harga sebelum dicek.\n"
        "\n"
        "Salam,\n"
        "{nama_toko} ({area})\n"
        "Balasan di jam {jam_respons}."
    ),
    "menjual": (
        "Halo, saya lihat postingan {model} yang dijual.\n"
        "\n"
        "Boleh info tambahan:\n"
        "- kondisi fisik dan fungsi saat ini\n"
        "- kelengkapan (unit, dus, charger, aksesori)\n"
        "- status garansi bila ada\n"
        "- area COD\n"
        "\n"
        "Kalau boleh, sebutkan juga harga yang diharapkan. Terima kasih.\n"
        "\n"
        "Salam,\n"
        "{nama_toko} ({area})"
    ),
    "diskusi": (
        "Halo, menarik topiknya tentang {model}.\n"
        "\n"
        "Saya juga penasaran pengalamannya. Kalau boleh tahu, bagian mana yang "
        "paling terasa bedanya untuk pemakaian sehari-hari?\n"
        "\n"
        "Terima kasih sudah berbagi.\n"
        "\n"
        "Salam,\n"
        "{nama_toko} ({area})"
    ),
}

_CATEGORY_TITLES = {
    "mencari": "Balasan untuk pencari iPhone",
    "menjual": "Balasan untuk penjual iPhone",
    "diskusi": "Balasan untuk diskusi",
}


def _clean_model(value: str | None) -> str:
    model = (value or "").strip()
    if not model or model == UNKNOWN or not re.search(r"iphone", model, re.I):
        return "iPhone"
    return model


def _fix_casing(text: str) -> str:
    """Rapikan 'postingan iphone' -> 'postingan iPhone'."""
    return re.sub(r"\biphone\b", "iPhone", text, flags=re.I)


def build_draft(
    post: dict,
    analysis: dict | None = None,
    profile: BusinessProfile | None = None,
) -> Draft | None:
    """Buat draf berdasarkan kategori. Kembalikan None kalau tidak relevan."""
    analysis = analysis or post
    category = (analysis.get("category") or "").strip()
    if category not in _TEMPLATES:
        return None

    profile = profile or BusinessProfile()
    model = _clean_model(analysis.get("iphone_model"))
    body = _TEMPLATES[category].format(
        model=model,
        nama_toko=profile.nama_toko,
        area=profile.area,
        jam_respons=profile.jam_respons,
    )
    body = _fix_casing(body)

    notes = [
        "Draf ini TIDAK dikirim otomatis. Salin, periksa, lalu kirim sendiri di Facebook.",
        "Placeholder dalam kurung siku wajib diganti dengan data Anda yang benar.",
        "Draf sengaja tidak menyebut stok, harga, atau garansi karena data itu belum tentu ada.",
    ]
    if not profile.is_filled():
        notes.append("Profil bisnis belum lengkap: isi di tab Pengaturan agar placeholder hilang.")

    return Draft(title=_CATEGORY_TITLES[category], body=body, notes=notes)


def contains_forbidden_claim(text: str) -> list[str]:
    """Periksa apakah teks memuat klaim yang dilarang (dipakai unit test & UI)."""
    lowered = (text or "").lower()
    return [claim for claim in FORBIDDEN_CLAIMS if claim in lowered]


def render_profile(profile: BusinessProfile) -> str:
    return (
        f"{profile.nama_toko} · area {profile.area} · respons {profile.jam_respons}"
    )
