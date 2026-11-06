"""Daftar lokasi yang dikenali (kota/kabupaten umum di Indonesia).

Dipakai HANYA untuk mencocokkan nama tempat yang benar-benar tertulis di postingan.
Jika tempat tidak ada dalam daftar, hasilnya "tidak diketahui" — bukan tebakan.
Anda bisa menambah sendiri nama kota/kecamatan yang sering muncul di grup Anda.
"""

from __future__ import annotations

LOCATIONS: tuple[str, ...] = (
    # Jabodetabek
    "Jakarta", "Jakarta Pusat", "Jakarta Selatan", "Jakarta Barat", "Jakarta Timur",
    "Jakarta Utara", "Jaksel", "Jakpus", "Jakbar", "Jaktim", "Jakut",
    "Bogor", "Depok", "Tangerang", "Tangerang Selatan", "Tangsel", "Bekasi",
    "Cibubur", "Cikarang", "Serpong", "BSD", "Bintaro", "Karawang",
    # Jawa Barat
    "Bandung", "Cimahi", "Sukabumi", "Cirebon", "Tasikmalaya", "Garut", "Purwakarta",
    "Subang", "Sumedang", "Indramayu", "Majalengka", "Cianjur", "Banjar",
    # Jawa Tengah / DIY
    "Semarang", "Solo", "Surakarta", "Magelang", "Salatiga", "Klaten", "Purwokerto",
    "Tegal", "Pekalongan", "Kudus", "Jepara", "Wonosobo", "Yogyakarta", "Jogja",
    "Sleman", "Bantul", "Gunungkidul", "Kulon Progo",
    # Jawa Timur
    "Surabaya", "Malang", "Sidoarjo", "Gresik", "Kediri", "Jember", "Madiun",
    "Banyuwangi", "Probolinggo", "Pasuruan", "Mojokerto", "Blitar", "Tuban", "Batu",
    # Sumatera
    "Medan", "Binjai", "Deli Serdang", "Pematang Siantar", "Palembang", "Padang",
    "Pekanbaru", "Batam", "Tanjungpinang", "Bandar Lampung", "Lampung", "Bengkulu",
    "Jambi", "Banda Aceh", "Aceh", "Langsa", "Lhokseumawe", "Tebing Tinggi",
    "Tanjung Balai", "Prabumulih", "Lubuklinggau", "Dumai", "Rantau Prapat",
    # Kalimantan
    "Pontianak", "Singkawang", "Banjarmasin", "Banjarbaru", "Martapura",
    "Palangkaraya", "Samarinda", "Balikpapan", "Bontang", "Tanjung Selor", "Tarakan",
    # Sulawesi
    "Makassar", "Gowa", "Maros", "Parepare", "Palopo", "Manado", "Bitung",
    "Tomohon", "Palu", "Donggala", "Kendari", "Bau-Bau", "Gorontalo", "Mamuju",
    "Ambon", "Ternate", "Tidore", "Sorong", "Manokwari", "Jayapura", "Merauke",
    "Mataram", "Denpasar", "Badung", "Gianyar", "Singaraja", "Kupang", "Ende",
    "Labuan Bajo", "Maumere", "Waingapu", "Sumbawa", "Bima",
    # Umum
    "Indonesia", "Jabodetabek", "Pulau Jawa", "Luar Pulau Jawa",
)

# Singkatan/alias yang sering dipakai di grup jual-beli.
LOCATION_ALIASES: dict[str, str] = {
    "jkt": "Jakarta",
    "jogja": "Yogyakarta",
    "yogya": "Yogyakarta",
    "sby": "Surabaya",
    "bdg": "Bandung",
    "tgr": "Tangerang",
    "tng": "Tangerang",
    "bks": "Bekasi",
    "dpk": "Depok",
    "bgr": "Bogor",
    "smg": "Semarang",
    "mks": "Makassar",
    "mksr": "Makassar",
    "pku": "Pekanbaru",
    "plm": "Palembang",
    "mdr": "Madiun",
    "slo": "Solo",
    "batam": "Batam",
}


def known_locations() -> list[str]:
    return sorted(set(LOCATIONS), key=len, reverse=True)
