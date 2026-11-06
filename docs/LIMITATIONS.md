# Keterbatasan & batas penggunaan

Dokumen ini sengaja dibuat tidak nyaman dibaca. Tujuannya agar Anda tidak
mengambil keputusan bisnis berdasarkan kemampuan yang tidak dimiliki alat ini.

---

## 1. Platform & kepatuhan

### Tautan grup publik bukan jaminan akses
Grup "publik" di Facebook tetap bisa:
- mewajibkan login untuk melihat isi,
- hanya menampilkan sebagian postingan kepada non-anggota,
- membatasi jumlah tampilan per akun,
- menampilkan checkpoint/CAPTCHA ketika aktivitas dianggap tidak biasa.

Aplikasi **tidak** mencoba melewati hal-hal tersebut. Jika terjadi, proses berhenti
dan status dilaporkan. Alternatif yang sah: **Input Manual**.

### Bukan alat scraping massal
Desainnya sengaja kecil: batas postingan, batas waktu, tanpa retry otomatis, tanpa
rotasi proxy, tanpa stealth. Jika kebutuhan Anda adalah memanen ribuan postingan,
alat ini bukan jawabannya — dan kemungkinan besar juga melanggar Ketentuan Facebook.

### Tanggung jawab tetap pada Anda
Kepatuhan pada Ketentuan Facebook, aturan grup, dan hukum (termasuk UU PDP) tetap
tanggung jawab pengguna. Aplikasi hanya membantu Anda melihat dan menganalisis
konten yang Anda akses sendiri.

---

## 2. Pembacaan otomatis (Playwright)

| Hal | Kenyataan |
|---|---|
| Selector | `div[role="article"]` adalah **tebakan konservatif**, belum diverifikasi pada akun nyata |
| Perubahan halaman | Facebook dapat mengubah struktur kapan saja tanpa pemberitahuan |
| Bahasa/akun | Tampilan berbeda per bahasa, per akun, per perangkat |
| Teks terpotong | Tombol "Lihat selengkapnya" **tidak** diklik otomatis, jadi teks bisa terpotong |
| Gambar | Tidak dibaca sama sekali (tidak ada OCR) |
| Komentar | Tidak diambil (di luar kebutuhan & mengurangi data pribadi) |
| Nama anggota | Tidak diambil, sengaja |
| Waktu posting | Hanya jika terlihat di teks; sering tidak terbaca |
| Tautan postingan | Diambil dari anchor dalam elemen; bisa tidak ditemukan |

Artinya: **hasil 0 postingan bukan bug otomatis**, melainkan sinyal bahwa grup
tersebut tidak dapat dibaca dengan cara ini. Lihat [POC_FACEBOOK.md](POC_FACEBOOK.md).

---

## 3. Deduplikasi

Prioritas: ID postingan dari tautan → fallback hash teks ternormalisasi.

Keterbatasan:
- Tanpa ID tautan, dua postingan berbeda dengan teks identik dianggap sama.
- Tanpa ID tautan, teks yang diedit sedikit dianggap postingan **baru**.
- Berbeda grup dengan teks identik (mis. cross-post) akan dianggap duplikat.
- Hash tidak mengenali parafrase atau teks yang ditambah emoji berbeda.

Untuk grup yang menampilkan tautan postingan, deduplikasi cukup andal. Untuk
tampilan yang tidak menampilkan tautan, anggap deduplikasi sebagai *best effort*.

---

## 4. Parsing harga

- Konvensi Indonesia: `12.500.000` = dua belas juta lima ratus ribu; `12,5jt` = 12,5 juta.
- Format Inggris `12,500` dibaca sebagai 12.500 (bukan 12,5).
- Angka yang benar-benar ambigu dikembalikan sebagai `tidak diketahui`, bukan ditebak.
- "iPhone 11" tidak pernah dianggap harga.
- Nominal di luar rentang wajar (di bawah Rp10.000 atau di atas Rp500 juta) diabaikan.
- Harga dalam gambar/tanggal di poster tidak terbaca.

Pembeda harga vs budget memakai kata kunci (`cari`, `budget`, `maksimal` vs
`dijual`, `harga`, `nego`). Teks campuran ("dijual, kalau ada yang minat budget 5jt")
bisa salah diklasifikasi. Periksa manual untuk keputusan penting.

---

## 5. Waktu posting

- Tanggal eksplisit dengan jam → presisi `tepat`.
- Tanggal tanpa jam → `perkiraan` (jam diisi 00:00).
- Teks relatif ("3 jam lalu") → `perkiraan`, dihitung dari waktu ditemukan.
- "kemarin", "baru saja", "minggu lalu" tanpa angka → `tidak diketahui`.
- Zona waktu: disimpan UTC, ditampilkan waktu lokal.

---

## 6. Analisis aturan

- Berbasis kata kunci dan pola. Bahasa gaul baru atau singkatan lokal yang belum
  ada di daftar tidak dikenali.
- Model iPhone hanya dikenali dalam bentuk umum (`iPhone 13 Pro Max`, `ip13pm`,
  `XR`, `SE`, `16e`, `Air`). Varian tidak lazim bisa jadi `tidak diketahui`.
- Detail (storage, BH, garansi, iCloud) hanya diambil bila tertulis eksplisit.
  "Mulus" dicatat sebagai kondisi, bukan jaminan kualitas.
- Lokasi hanya dari daftar kota di `fbworkspace/locations.py` + alias umum.
  Kecamatan/kelurahan yang tidak ada di daftar → `tidak diketahui`.
  Anda bisa menambahnya sendiri.
- Skor relevansi adalah penjumlahan kriteria, **bukan** probabilitas dan bukan
  rekomendasi investasi/kualitas.
- Kategori "diskusi" adalah kategori sisa bila ada iPhone tetapi tanpa sinyal
  transaksi yang jelas.

---

## 7. AI lokal (Ollama)

- Opsional. Tanpa AI, semua fitur inti tetap berjalan.
- Model 3B–4B bisa salah: model iPhone tertukar, harga salah baca, lokasi salah.
- Kecepatan bergantung CPU/GPU. RAM 16 GB cukup untuk memuat, tetapi belum tentu
  cepat. Tanpa GPU, satu postingan bisa puluhan detik atau lebih.
- Timeout membatasi waktu tunggu; job tetap sukses walau AI gagal.
- Keluaran divalidasi ketat: kategori harus salah satu dari empat nilai, skor
  di-clamp 0–100, harga di luar rentang diabaikan, field aneh dibuang.
- **Prompt injection tidak bisa dijamin 100%.** Instruksi di teks postingan
  diabaikan oleh aturan prompt, tetapi model kecil bisa tergoda. Karena itu:
  AI tidak pernah mengendalikan browser, tidak menjalankan perintah, dan hasilnya
  hanya ditampilkan sebagai data.
- Tidak ada embeddings/vector database. Tidak ada pencarian semantik.

---

## 8. Ekspor CSV

- Sel yang diawali `= + - @` diberi awalan apostrof agar Excel/Sheets tidak
  mengeksekusinya sebagai formula.
- File memakai BOM UTF-8 agar huruf beraksen/emoji tampil benar di Excel Windows.
- Tanda baca dan baris baru dipertahankan dalam tanda kutip CSV.
- Teks panjang dipotong sekitar 5.000 karakter per sel.

CSV tetap berisi teks dari internet. Jika dibuka di aplikasi lain yang memproses
markup (bukan Excel), tetap berhati-hati.

---

## 9. Keamanan lokal

- Dashboard default `127.0.0.1` — tidak dapat diakses perangkat lain.
- Aplikasi tidak membuka port lain selain dashboard.
- Ollama dipanggil lewat `127.0.0.1`; endpoint non-local **ditolak** oleh klien.
- Profil browser berisi cookie/sesi Facebook: diperlakukan sebagai rahasia,
  ada di `.gitignore`, dan bisa dihapus dari dashboard.
- Database SQLite tidak terenkripsi. Jika laptop dipakai bersama, gunakan akun
  Windows terpisah atau BitLocker.
- Tidak ada telemetri. Streamlit diatur `gatherUsageStats = false`.

---

## 10. Hal yang sengaja TIDAK dibuat

- Auto-comment, auto-post, auto-chat, auto-DM.
- Menyamar sebagai pengguna lain atau memalsukan perangkat.
- Mengumpulkan daftar anggota grup.
- Menyimpan kata sandi/cookie ke Git atau mengunggahnya.
- Memakai API AI berbayar atau hosting cloud.
- Menjanjikan stok/harga/garansi dalam draf (template memakai placeholder).
- Menjanjikan bahwa hasil analisis selalu benar.

---

## 11. Kapan sebaiknya berhenti memakai

- Muncul checkpoint/CAPTCHA atau peringatan pembatasan.
- Grup menampilkan pesan bahwa akses tidak diizinkan.
- Anda merasa perlu "trik" agar bisa membaca lebih banyak — itu tanda batas.
- Anda mulai menyimpan data pribadi yang tidak diperlukan untuk pekerjaan Anda.

Dalam kondisi tersebut: hentikan proses, gunakan Input Manual untuk postingan yang
memang Anda lihat, dan jangan mencari jalan memutar.
