# FB Workspace

Alat bantu **lokal** (Windows) untuk menemukan dan menganalisis postingan tentang
iPhone di grup Facebook publik yang Anda akses sendiri secara sah.

Fokusnya bukan "memanen" grup, melainkan:

1. membaca **jumlah kecil** postingan yang memang tampil untuk akun Anda,
2. menyimpannya di SQLite lokal,
3. memfilter kata kunci, lokasi, dan rentang harga,
4. mengekstrak kategori/model/harga/budget/lokasi dengan aturan yang bisa dijelaskan,
5. membuat draf balasan netral yang **Anda** periksa dan kirim manual di Facebook.

Tidak ada auto-post, auto-comment, auto-chat, atau unggahan data ke cloud.

---

## 1. Status jujur proyek ini

Ini penting supaya Anda tidak salah harap.

| Bagian | Status |
|---|---|
| Dashboard Streamlit (3 langkah: Mulai · Hasil · Pengaturan) | ✅ Berjalan & diuji (termasuk smoke test otomatis) |
| Hitungan Semua/Lolos/Tersembunyi + "Terapkan ulang filter" | ✅ Diuji otomatis (data & UI) |
| Penyaring komentar/reply + pembersih baris lama | ✅ Diuji otomatis (122 baris ekspor nyata terdeteksi) |
| Ekstraksi isi postingan dari tautan `/posts/` (bukan komentar) | ✅ Unit + uji DOM (jsdom); ⚠️ tetap perlu POC di akun Anda |
| SQLite, deduplikasi, status, penghapusan data | ✅ Diuji otomatis |
| Filter kata kunci/lokasi/harga, mode longgar & ketat | ✅ Diuji otomatis |
| Analisis aturan iPhone (kategori, model, harga vs budget, detail) | ✅ Diuji otomatis |
| Ekspor CSV aman formula injection | ✅ Diuji otomatis |
| Draf respons netral (tanpa klaim karangan) | ✅ Diuji otomatis |
| Klien Ollama (timeout, validasi JSON, offline) | ✅ Diuji otomatis **tanpa model nyata** |
| Deteksi halaman login/checkpoint/penolakan | ✅ Diuji dengan teks contoh |
| **Pembacaan grup Facebook sungguhan** | ⚠️ **BELUM diverifikasi** — selector perlu Anda uji lewat POC |
| **Login manual di jendela Chromium** | ⚠️ **BELUM diverifikasi** di laptop Anda. Alur menunggu-login & deteksi cookie sudah diuji dengan simulasi, bukan akun nyata |
| **Helper login (`scripts/login_helper.py`)** | ✅ Diuji tanpa browser; ⚠️ perlu uji di laptop |
| **Kualitas & kecepatan Ollama di laptop Anda** | ⚠️ **BELUM diukur** |

> Jangan menganggap selector `div[role="article"]` pasti bekerja. Struktur halaman
> Facebook berubah dan berbeda antar akun/bahasa. Skrip POC disediakan justru untuk
> memastikan hal ini **sebelum** Anda memakai sistem penuh.

---

## 2. Prinsip kepatuhan (tidak bisa dinegosiasikan)

- Tautan grup publik **tidak menjamin** seluruh isi grup bisa dibaca. Facebook bisa
  meminta login, membatasi tampilan, atau menampilkan tantangan keamanan.
- Bila muncul login/challenge/pembatasan/akses ditolak: aplikasi **berhenti** dan
  menampilkan status. Tidak ada percobaan menembusnya.
- Tidak ada bypass CAPTCHA, stealth/anti-detection, proxy rotation, atau trik
  menghindari kontrol akses.
- Jika login diperlukan, **Anda** login sendiri di browser yang terbuka.
  Aplikasi tidak meminta, membaca, atau menyimpan kata sandi/cookie Anda.
- Tidak ada pengambilan profil anggota, komentar, atau data pribadi yang tidak perlu.
- Teks postingan diperlakukan sebagai **data tidak tepercaya**, bukan instruksi.
  Tidak ada `eval`, tidak ada eksekusi perintah dari teks, dan AI tidak pernah
  mengendalikan browser.

---

## 3. Fitur MVP

**Alur UI tiga langkah** (menggantikan 6 tab lama)
- Tab **Mulai**: ① sumber (Grup Facebook / Tempel manual / DEMO) → ② filter →
  ③ batas + mulai. Ringkasan satu baris menunjukkan apa yang akan dijalankan.
- Tab **Hasil**: hitungan **Semua / Lolos filter / Tersembunyi**, klik baris untuk
  detail, ekspor CSV, dan tombol **Terapkan ulang filter** untuk data lama.
- Tab **Pengaturan**: Ollama, profil bisnis, data (termasuk hapus DEMO), keamanan,
  bantuan. Sidebar hanya ringkasan — tidak ada filter ganda.

**Dashboard lokal**
- Helper login manual sekali (`scripts/login_helper.py` + tombol di dashboard).
- Saat login wall muncul, browser dibiarkan terbuka (tidak auto-close) sampai batas waktu.
- Input satu/beberapa tautan grup + validasi bentuk tautan.
- Kata kunci inklusi & eksklusi, filter lokasi dan rentang harga.
- Batas kecil postingan dan durasi; tidak ada pengulangan otomatis.
- Tombol mulai & berhenti; log kejadian yang mudah dipahami; pesan error jelas.

**Data postingan**
- ID internal, tautan grup, tautan postingan (bila ada), teks, waktu ditemukan,
  waktu posting + presisi (`tepat`/`perkiraan`/`tidak diketahui`).
- Sumber data: `browser`, `manual`, `demo`.
- Status analisis dan status pengerjaan: **baru / diperiksa / relevan / diabaikan**.
- Deduplikasi: ID tautan → fallback hash teks ternormalisasi (batasnya dijelaskan).
- Penanda `tidak diketahui` untuk data yang tidak tersedia (bukan tebakan).
- Isi postingan diambil dari **kartu postingan** yang ditemukan lewat tautan
  `/posts/` (bukan dari `div[role="article"]`, yang di grup nyata justru komentar).
  Komentar di dalam kartu dibuang, lalu teks dipotong di bilah `Like · Comment · Share`.
- Komentar/reply tetap ditolak sebagai jaring pengaman: teks berpola `… Reply`/
  `LikeReplyShare` dihitung sebagai "komentar dilewati" dan tidak disimpan.

**Analisis iPhone (tanpa AI)**
- Kategori: `mencari`, `menjual`, `diskusi`, `tidak relevan`.
- Model iPhone, harga vs budget (dibedakan), lokasi.
- Storage, battery health, garansi, kondisi — **hanya jika disebutkan**.
- Skor relevansi 0–100 berbasis kriteria tertulis + alasan + cuplikan pendukung.

**Hasil**
- Tabel yang bisa dicari/difilter, detail postingan + tautan sumber.
- Ekspor CSV yang aman dari formula injection (aman dibuka di Excel).
- Draf respons dengan tombol salin; tidak pernah dikirim otomatis.

**Ollama (opsional)**
- Aplikasi tetap 100% berfungsi tanpa AI.
- Endpoint lokal saja (default `http://127.0.0.1:11434`), timeout, validasi JSON,
  input dipotong, penanganan offline.
- Model bisa diganti per sesi di dashboard; `think` dan `num_predict` dapat diatur
  (penting untuk `qwen3:4b` yang punya mode thinking).

---

## 4. Struktur proyek

```
fbworkspace/
├── app.py                      # entry dashboard Streamlit
├── fbworkspace/
│   ├── dashboard.py            # seluruh tampilan UI
│   ├── config.py               # .env → Settings (localhost default)
│   ├── db.py                   # SQLite: postingan, analisis, job, event
│   ├── models.py               # konstanta, enum, dataclass
│   ├── normalize.py            # normalisasi teks/tautan, harga, waktu
│   ├── filters.py              # filter kata kunci/lokasi/harga
│   ├── analyzer.py             # analisis aturan + validasi keluaran AI
│   ├── pipeline.py             # alur simpan + input manual
│   ├── export.py               # CSV anti formula injection
│   ├── drafts.py               # template draf netral
│   ├── ollama_client.py        # klien Ollama lokal (opsional)
│   ├── demo.py                 # pemuat data DEMO sintetis
│   ├── jobs.py                 # start/stop worker dari dashboard
│   ├── worker.py               # proses pembacaan browser terpisah
│   └── browser/
│       ├── base.py             # kontrak adapter
│       ├── manual.py           # adapter manual (tanpa browser)
│       ├── page_state.py       # deteksi login/checkpoint/penolakan
│       └── playwright_fb.py    # adapter Playwright (POC, belum terverifikasi)
├── scripts/
│   ├── login_helper.py         # login manual sekali (profil browser)
│   ├── poc_facebook.py         # POC satu grup, batas kecil
│   ├── setup_windows.ps1       # setup otomatis Windows
│   ├── run_windows.ps1         # menjalankan dashboard
│   ├── poc_windows.ps1         # pembungkus POC
│   └── check_extraction_dom.cjs # uji logika ekstraksi DOM (Node + jsdom, opsional)
│   └── load_demo.py            # muat data DEMO via CLI
├── data/demo_posts_DEMO.json   # data sintetis berlabel DEMO
├── docs/                       # instalasi, POC, batasan, Git, testing
├── tests/                      # 290 tes otomatis
├── requirements.txt
├── .env.example
└── .gitignore
```

---

## 5. Instalasi cepat (Windows)

Panduan lengkap: **[docs/INSTALL_WINDOWS.md](docs/INSTALL_WINDOWS.md)**

```powershell
git clone https://github.com/Dypoi/fbworkspace.git
cd fbworkspace
powershell -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\run_windows.ps1
```

Atau klik dua kali `setup_windows.bat` lalu `run_windows.bat`.

Dashboard terbuka di `http://127.0.0.1:8501` dan **hanya** bisa diakses dari laptop itu.

---

## 6. Alur pemakaian yang disarankan

### Langkah A — Coba tanpa Facebook (5 menit)
1. Jalankan dashboard.
2. Tab **Mulai** → ① pilih sumber **Data DEMO** → **Muat data DEMO**.
   (Saat database masih kosong, kartu onboarding menawarkan tombol yang sama.)
3. Buka tab **Hasil**: perhatikan **Semua (N) · Lolos filter (N) · Tersembunyi (N)**.
4. Klik satu baris untuk detail, ubah status, coba draf dan ekspor CSV.
5. Kembali ke tab **Mulai** → ① **Tempel manual** untuk mencoba teks Anda sendiri.
6. Ubah filter di langkah ②, lalu klik **Terapkan ulang filter** di tab Hasil:
   status data lama dihitung ulang tanpa menambah baris.

### Langkah B — Login sekali (opsional tapi disarankan)

```powershell
.\venv\Scripts\python.exe scripts\login_helper.py
```
Jendela Chromium terbuka. Login sendiri di sana (termasuk OTP/checkpoint), lalu
tutup jendela browser. Sesi tersimpan di `data/browser_profile/` (tidak masuk Git).
Bisa juga lewat dashboard: tab **Mulai → ① Sumber data → 🔐 Login Facebook**.

Kalau login belum dilakukan dan saat pembacaan Facebook meminta login, **browser
tidak langsung ditutup**: jendela dibiarkan terbuka sampai `FBW_LOGIN_WAIT_SECONDS`
(default 180 detik) agar Anda bisa login saat itu juga. Waktu menunggu tidak
dihitung sebagai waktu pembacaan.

### Langkah C — Uji kelayakan pembacaan (POC, wajib sebelum sistem penuh)
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\poc_windows.ps1 `
  -Url "https://www.facebook.com/groups/ID-GRUP-ANDA/"
```
Baca **[docs/POC_FACEBOOK.md](docs/POC_FACEBOOK.md)** untuk cara menafsirkan hasil.
Kalau POC tidak berhasil, gunakan ① **Tempel manual** di tab Mulai — aplikasi tetap berguna.

### Langkah D — Jalankan dari dashboard (batas kecil)
Tab **Mulai** → ① pilih **Grup Facebook** dan tempel tautan → ② atur filter →
③ centang pernyataan → **Mulai pembacaan**. Panel status job muncul di bawahnya.
Batas awal disarankan: 3–5 postingan, 2–3 menit.

### Langkah E — Ollama (opsional, terakhir)
Buka tab **Pengaturan → 🧠 Ollama** dan bagian 8 di bawah.

---

## 7. Cara kerja filter & skor relevansi

**Kata kunci**: kata tunggal memakai batas kata (`ip` tidak cocok dengan `tips`);
frasa dicocokkan apa adanya. Eksklusi selalu menang atas inklusi.

**Lokasi**: hanya nama lokasi yang tertulis di teks dan ada di
`fbworkspace/locations.py`. Tidak ada simpulan dari nomor telepon atau nama.

**Harga/budget**:
- `Rp 12.500.000`, `12,5jt`, `850rb`, `8 juta` dikenali.
- Konvensi: titik = ribuan, koma = desimal. Angka ambigu → `tidak diketahui`.
- "iPhone 11" tidak pernah dianggap harga.
- Pembeda: kata `cari/budget/maksimal` → **budget**; `dijual/harga/nego` → **harga**.

**Mode filter**:
- *Longgar* (default): postingan tanpa harga/lokasi tetap lolos dengan catatan.
- *Ketat*: postingan tanpa harga/lokasi digugurkan.

**Postingan tersembunyi**: data yang tidak lolos filter tetap disimpan dan bisa
dilihat lewat pilihan **Tersembunyi** di tab Hasil. Setelah mengubah kriteria,
klik **Terapkan ulang filter** — kolom filter dihitung ulang dari teks tersimpan
(tanpa baris baru dan tanpa mengubah hasil analisis).

**Skor relevansi** (dijumlahkan, maksimum 100):

| Kriteria | Poin |
|---|---|
| Menyebut iPhone | +40 |
| Model teridentifikasi | +15 |
| Kategori mencari/menjual | +20 |
| Kategori diskusi | +5 |
| Nominal harga/budget terbaca | +10 |
| Lokasi dikenali | +10 |
| Sinyal transaksi tambahan (COD/nego/DM/stok/garansi) | +5 |
| Tanpa penyebutan iPhone | skor dibatasi ≤ 20 |

Skor ini **bukan probabilitas kebenaran** dan bukan penilaian kualitas penjual.

---

## 8. Ollama (opsional)

Model **tidak wajib** `qwen2.5:3b` — itu hanya default di `.env.example`.
Anda bisa memakai model apa pun yang terpasang di Ollama, misalnya `qwen3:4b`.
Rekomendasi awal: `qwen2.5:3b` (Apache-2.0, ±1,9 GB unduhan).
Alternatif: `llama3.2:3b` (±2,0 GB), `gemma3:4b` (±3,3 GB, lebih berat).

```powershell
ollama list          # lihat nama persis yang terpasang
ollama pull qwen2.5:3b   # atau model lain, mis. qwen3:4b
```

Lalu atur di `.env`:

```
FBW_OLLAMA_ENABLED=true
FBW_OLLAMA_URL=http://127.0.0.1:11434
FBW_OLLAMA_MODEL=qwen3:4b      # samakan dengan `ollama list`
FBW_OLLAMA_TIMEOUT=120
FBW_OLLAMA_THINK=false         # penting untuk qwen3: matikan thinking agar JSON tidak terpotong
FBW_OLLAMA_NUM_PREDICT=512     # naikkan bila JSON sering terpotong
```

Model juga bisa diganti sementara tanpa restart lewat kotak **"Model untuk sesi ini"**
di tab **Pengaturan → 🧠 Ollama**.

**Kejujuran soal performa:** RAM 16 GB cukup untuk memuat model 3B–4B terkuantisasi,
tetapi kecepatan sangat bergantung pada CPU/GPU. Tanpa GPU, satu postingan bisa
memakan puluhan detik sampai lebih dari satu menit. Karena itu:
- setiap panggilan punya timeout,
- job tetap sukses walau Ollama gagal,
- analisis aturan selalu tersedia sebagai fallback,
- tidak ada embeddings/vector DB pada MVP.

---

## 9. Data, privasi, dan apa yang tidak boleh masuk Git

| Data | Lokasi | Masuk Git? |
|---|---|---|
| Database postingan | `data/fbworkspace.db` | ❌ |
| Profil browser (cookie/sesi) | `data/browser_profile/` | ❌ |
| Log | `logs/` | ❌ |
| Konfigurasi & rahasia | `.env` | ❌ (contoh: `.env.example`) |
| Profil bisnis | `data/business_profile.json` | ❌ |
| Model AI | `models/`, `*.gguf` | ❌ |
| Data DEMO sintetis | `data/demo_posts_DEMO.json` | ✅ |

Penghapusan data tersedia di tab **Pengaturan → 💾 Data** (postingan, log, profil browser), termasuk tombol **Hapus data DEMO** pada banner kuning di bagian atas.

---

## 10. Pengujian

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Hasil terakhir di lingkungan pengembangan: **290 tes lulus** (termasuk smoke test
dashboard Streamlit untuk 3 tab, segmentasi hasil, dan filter ulang), `ruff check` bersih. Detail: **[docs/TESTING.md](docs/TESTING.md)**.

Yang **belum** diuji dan harus Anda validasi sendiri:
1. Pembacaan grup Facebook nyata (selector, login, pembatasan).
2. Login manual nyata di jendela Chromium (alur menunggu-login sudah diuji dengan simulasi).
3. Perilaku saat Facebook menampilkan challenge di akun Anda.
4. Kecepatan/kualitas Ollama dengan model nyata di laptop Anda (termasuk `qwen3:4b`).
5. Tampilan dashboard di browser Anda (Chrome/Edge/Firefox).

---

## 11. Keterbatasan utama

Ringkasan (lengkap di **[docs/LIMITATIONS.md](docs/LIMITATIONS.md)**):

- Tautan publik ≠ isi bisa dibaca; login/keanggotaan sering diperlukan.
- Selector browser bisa berubah kapan saja; teks bisa terpotong.
- Deduplikasi tanpa ID tautan memakai hash teks (teks yang diedit = postingan baru).
- Waktu relatif ("3 jam lalu") hanya perkiraan.
- Model kecil bisa salah membaca model/harga; selalu periksa manual.
- Aplikasi tidak mengirim pesan; semua respons Anda kirim sendiri.
- Ekstraksi memakai tautan `/posts/` + naik ke kartu postingan. Postingan tanpa
  tautan (mis. anonim) bisa terlewat; komentar dalam kartu dibuang lewat DOM dan
  pemotongan bilah aksi, tetapi struktur Facebook bisa berubah kapan saja.
- Komentar yang lolos jaring pengaman bisa dibersihkan di **Pengaturan → 💾 Data**.

---

## 12. Git

Panduan pertama kali & update: **[docs/GIT_GUIDE.md](docs/GIT_GUIDE.md)**.

---

## 13. Catatan: konfigurasi preview pengembangan vs laptop Anda

Saat aplikasi ini dibangun, dashboard dijalankan di lingkungan pengembangan dengan
alamat `0.0.0.0` agar bisa dipratinjau lewat URL proxy. **Itu hanya untuk preview,
bukan untuk laptop Anda.**

| | Preview pengembangan | Laptop Windows Anda |
|---|---|---|
| Alamat bind | `0.0.0.0` (lewat argumen CLI sementara) | `127.0.0.1` (dari `.streamlit/config.toml` & `scripts/run_windows.ps1`) |
| Data | database kosong/terpisah | database lokal Anda |
| Akses Facebook | tidak tersedia | browser lokal Anda, login manual |
| Tujuan | memeriksa tampilan | pemakaian sebenarnya |

Di repo, konfigurasi yang tersimpan (`config.toml`, script) selalu `127.0.0.1`.
Argumen CLI hanya menimpa satu proses itu dan tidak mengubah file. Jangan menyalin
`--server.address 0.0.0.0` ke script laptop Anda.

---

## 14. Ketentuan platform

Gunakan alat ini hanya untuk konten yang memang boleh Anda akses, dan tetap
mematuhi Ketentuan Facebook serta hukum yang berlaku. Jika pembacaan otomatis
tidak diizinkan atau tidak stabil, gunakan ① **Tempel manual** di tab Mulai.
