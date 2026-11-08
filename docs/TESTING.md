# Pengujian: apa yang sudah diuji dan apa yang belum

Dokumen ini mencatat hasil pengujian yang benar-benar dijalankan, bukan klaim.
Terakhir diperbarui: **6 November 2026**.

---

## 1. Cara menjalankan

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Atau dengan ringkasan lebih detail:

```powershell
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m pytest --collect-only -q
```

Lint (opsional, butuh `ruff`):

```powershell
.\.venv\Scripts\pip.exe install ruff
.\.venv\Scripts\ruff.exe check . --select E,F,W,I
```

---

## 2. Hasil terakhir

**Lingkungan:** Linux (sandbox pengembangan), Python 3.11.2, Streamlit 1.67.0,
pytest 8.x. Windows belum dijalankan dari lingkungan ini.

```
471 passed in ~4s
ruff check . --select E,F,W,I  ->  All checks passed!
```

| Berkas tes | Jumlah | Cakupan |
|---|---:|---|
| `tests/test_normalize.py` | 52 | normalisasi teks, validasi tautan grup, ID postingan, dedupe key, harga Indonesia, waktu posting |
| `tests/test_analyzer.py` | 48 | kategori, model iPhone, harga vs budget, detail, lokasi, skor & batasnya, teks tidak tepercaya |
| `tests/test_db.py` | 21 | simpan, dedupe, status, analisis upsert, filter, job, event, penghapusan, statistik |
| `tests/test_llm_validation.py` | 25 | validasi keluaran AI, anti-halusinasi, prompt, payload `think`/`num_predict`, offline, blokir endpoint non-lokal |
| `tests/test_login.py` | 22 | cookie sesi `c_user`, penantian login (deteksi/timeout/berhenti), file marker status |
| `tests/test_filters.py` | 29 | batas kata, frasa, inklusi/eksklusi, lokasi, harga, mode ketat, varian `ip x`/`iphonex` |
| `tests/test_page_state.py` | 21 | deteksi login/checkpoint/penolakan/rate limit/404, pengecualian penanda login saat sesi ada |
| `tests/test_demo_and_worker.py` | 14 | data DEMO, pemilihan adapter, pemetaan status job, batas |
| `tests/test_export.py` | 14 | formula injection, BOM, header, detail & alasan, truncation |
| `tests/test_pipeline.py` | 12 | parsing input manual, penyimpanan, dedupe, batch |
| `tests/test_drafts.py` | 11 | draf netral, larangan klaim, placeholder, profil |
| `tests/test_app_smoke.py` | 8 | dashboard dirender (kosong & setelah DEMO), tepat 4 tab, segmentasi Semua/Lolos/Tersembunyi, tombol filter ulang, hapus baris komentar, tab Balas bawaannya uji kering |
| `tests/test_post_extraction.py` | 37 | kartu dari tautan `/posts/` **dan** kartu feed `[aria-posinset]`, dedupe, buang komentar, blok `dir=auto`, pembersihan label "Facebook"/huruf berjauhan, fallback selector |
| `tests/test_text_quality.py` | 26 | pola komentar/reply (contoh asli dari ekspor), postingan sah tidak ikut terbuang, adapter melewati komentar, `skipped_comment`, pembersihan data lama |
| `tests/test_ui_data.py` | 12 | hitungan per status filter, hapus per sumber (DEMO), pratinjau filter, "terapkan ulang filter" tanpa duplikasi |
| `tests/test_pricelist.py` | 29 | nama model baku (`iphone se 2020` → `iphone se gen2`), format rupiah, parsing baris harga (`iphone 11 64gb 2,4jt`), pencarian per model/storage, stok |
| `tests/test_replies.py` | 43 | harga hanya dari daftar, tanya storage bila lebih dari satu varian, ready/lokasi/WA, "nego" → eskalasi, model tak dikenal & nomor pembeli → diam + eskalasi, sapaan → diam tanpa eskalasi, batas 400 karakter |
| `tests/test_notify.py` | 12 | baris pertama pesan WA, isi Chat/Isi/Alasan/Link, pemotongan 900 karakter, normalisasi nomor `08…`/`+62…` → `62…`, URL `web.whatsapp.com/send` |
| `tests/test_reply_style.py` | 22 | prompt variasi, gaya diterima, **angka berubah/lokasi hilang/nomor WA hilang/klaim terlarang → ditolak**, Ollama mati tetap memakai teks template |
| `tests/test_messenger_worker.py` | 12 | uji kering tidak mengirim, balasan nyata memakai harga, **"nego" memicu notifikasi WA**, dedup eskalasi, cooldown per chat, batas balasan, `perlu_login` → job diblokir, gagal kirim dihitung, variasi AI hanya saat Ollama aktif |

### Pengujian non-unit yang juga dijalankan

| Uji | Cara | Hasil |
|---|---|---|
| Worker end-to-end (mode manual) | `python -m fbworkspace.worker --job-id 1` | Job `selesai`, event log lengkap, counter JSON benar |
| Menunggu login (simulasi) | `wait_for_login` dengan jam & cookie palsu | Deteksi, timeout, dan berhenti bekerja tanpa menggantung |
| Helper login tanpa Chromium | `scripts/login_helper.py` | Gagal dengan pesan + saran `playwright install chromium`, status ditulis ke marker |
| Penurunan tautan postingan | `resolve_post_url` dengan tautan foto `set=pcb./gm.` dan tautan komentar | ID postingan terbaca; tautan disusun dari ID grup; tanpa petunjuk → None |
| Selector dari PowerShell | `_selector_is_default` + isi `poc_windows.ps1` | `div[role=article]` (kutip hilang) tetap dianggap default; wrapper tidak mengirim `--selector` |
| Logika DOM ekstraksi (jsdom) | `npm install jsdom` lalu `node scripts/check_extraction_dom.cjs` (Python: `FBW_PYTHON`) | 9/9 pemeriksaan lulus: kartu postingan terdeteksi, komentar dibuang, postingan lain tidak ikut |
| Penyaring komentar pada data nyata | `looks_like_comment` atas ekspor 122 baris pengguna | 122/122 terdeteksi; 9 teks DEMO & postingan contoh tidak ikut tertandai |
| POC tanpa Chromium terpasang | `python scripts/poc_facebook.py --yes` | Gagal dengan pesan jelas + saran `playwright install chromium`, diagnostik JSON tersimpan, exit code kesalahan |
| Server Streamlit | `streamlit run app.py` + `curl /_stcore/health` | `200 ok` |
| Render dashboard | `streamlit.testing.v1.AppTest` | Tanpa exception; tepat 4 tab; segmentasi Semua/Lolos/Tersembunyi ada; DEMO & filter ulang berjalan; tab Balas bawaannya uji kering |
| Worker balas DM tanpa browser | `run()` dengan adapter + `MemorySender` palsu | Uji kering mencatat tanpa mengirim; "nego" mengirim notifikasi `cek facebook ada chat yang bukan ranah saya`; dedup & cooldown bekerja |
| Penyusun balasan (18 pesan nyata) | `compose_reply` manual | "iphone 8 berapa?" tidak menjawab harga (harus tanya storage); "ada yang lain?" tidak dijawab "Ready"; nomor pembeli → eskalasi |

---

## 3. Yang BELUM diuji (wajib Anda validasi)

| Hal | Mengapa belum | Cara memvalidasi |
|---|---|---|
| Membuka grup Facebook nyata | Lingkungan pengembangan tidak punya akses/akun Facebook | Jalankan POC ([POC_FACEBOOK.md](POC_FACEBOOK.md)) |
| Login manual di jendela Chromium | Butuh akun nyata | Jalankan `scripts/login_helper.py`; pastikan status `selesai`, lalu POC tidak lagi `perlu_login` |
| Penantian login di halaman nyata | Butuh halaman login nyata | Jalankan pembacaan tanpa login; jendela harus tetap terbuka sampai `FBW_LOGIN_WAIT_SECONDS` |
| Selector `div[role="article"]` pada akun Anda | Perlu halaman nyata | POC: periksa jumlah postingan & kualitas teks |
| Selector Messenger (`div[role="listitem"]`, kotak tulis) | Butuh akun nyata | Tab Balas → uji kering → periksa log: chat terbaca & balasan yang AKAN dikirim |
| Pengiriman WhatsApp Web nyata | Butuh scan QR di ponsel Anda | `scripts/wa_login.py`, lalu **Kirim tes WhatsApp** di tab Balas |
| Deteksi checkpoint nyata | Butuh halaman challenge nyata | Hentikan jika muncul; laporkan teks penandanya bila tidak terdeteksi |
| Kecepatan & kualitas Ollama | Model tidak diunduh di sandbox | Pengaturan → Ollama → Cek koneksi → analisis 1–2 postingan |
| JSON `qwen3:4b` dengan `think:false` | Butuh model nyata | Set model di kotak "Model untuk sesi ini", analisis 1 postingan |
| Instalasi Windows (PowerShell) | Lingkungan uji Linux | Ikuti [INSTALL_WINDOWS.md](INSTALL_WINDOWS.md) |
| Tampilan di Chrome/Edge/Firefox | Tidak ada browser di sandbox | Buka dashboard, cek tabel & tombol salin |
| Ekspor CSV di Excel nyata | Excel tidak tersedia | Buka hasil ekspor, pastikan `=SUM(...)` tampil sebagai teks |

---

## 4. Checklist penerimaan manual (di laptop Anda)

Jalankan berurutan. Beri tanda centang hanya jika benar-benar terlihat.

**A. Instalasi**
- [ ] `setup_windows.ps1` selesai tanpa error.
- [ ] `.env` ada dan `FBW_HOST=127.0.0.1`.
- [ ] `git status --ignored` menunjukkan `.env`, `*.db`, `data/browser_profile/` diabaikan.

**B. Fungsi lokal (tanpa Facebook)**
- [ ] Data DEMO termuat: 9 baris berlabel `DEMO`.
- [ ] Detail postingan menampilkan kategori, model, nominal, lokasi, skor, alasan.
- [ ] Ubah status menjadi `relevan`; setelah refresh tetap tersimpan.
- [ ] Filter di langkah ② tidak lagi berada di sidebar; sidebar hanya ringkasan.
- [ ] Mode ketat menggugurkan postingan tanpa harga.
- [ ] Ekspor CSV terbuka di Excel; sel `=SUM(1+1)` tampil sebagai teks berawalan `'`.
- [ ] Tab Mulai → ① Tempel manual menyimpan 1 postingan; duplikat tidak menambah baris.
- [ ] Ubah filter → **Terapkan ulang filter** mengubah hitungan tanpa menambah baris.
- [ ] Job baru: panel job menampilkan angka **Lolos filter** dan **komentar dilewati**.
- [ ] POC: `POSTINGAN TERBACA` > 0 dan contoh teksnya benar-benar kalimat postingan.
- [ ] POC: `METODE EKSTRAKSI` **bukan** `custom-selector` saat `-Selector` tidak diisi.
- [ ] Pengaturan → 💾 Data menampilkan peringatan + tombol hapus baris komentar bila ada.
- [ ] Draf respons muncul dan bisa disalin; placeholder `[NAMA TOKO]` terlihat.
- [ ] Hapus data → tabel kosong; muat DEMO lagi berhasil.

**C. Login & POC Facebook**
- [ ] `scripts\login_helper.py` membuka Chromium dan menulis status `selesai`.
- [ ] Setelah login, `data\browser_profile` tetap ada dan POC tidak lagi `perlu_login`.
- [ ] Tanpa login, jendela pembacaan tetap terbuka ±180 detik (tidak langsung tertutup).
- [ ] Chromium terbuka dan terlihat.
- [ ] Login manual berhasil (jika diminta) tanpa aplikasi meminta kredensial.
- [ ] Status POC dapat Anda tafsirkan dengan tabel di POC_FACEBOOK.md.
- [ ] Jika diblokir: proses berhenti, tidak ada percobaan ulang otomatis.

**D. Balas DM Messenger (mulai dari uji kering)**
- [ ] Tab **Balas** muncul dan bawaannya **Uji kering tercentang**; tombol nonsaktif sebelum pernyataan dicentang.
- [ ] Daftar harga tampil 9 baris sesuai pricelist Anda; ubah 1 harga → **Simpan daftar harga** berhasil.
- [ ] Jalankan uji kering → log menunjukkan chat terbaca dan balasan yang **akan** dikirim; tidak ada pesan terkirim ke pembeli.
- [ ] Chat berisi "nego" → muncul baris eskalasi di daftar **Eskalasi** (alasan `nego`) dan status `antre` (uji kering tidak mengirim WA).
- [ ] `scripts\wa_login.py` → scan QR → **Kirim tes WhatsApp** masuk ke nomor `081513839973`.
- [ ] Matikan uji kering untuk 1 chat uji: balasan pembeli sesuai harga di daftar; chat "nego" mengirim WA ke Anda.
- [ ] Bila WhatsApp Web logout: job tetap selesai, notifikasi tercatat `gagal`, dan chat tetap ada di daftar Eskalasi.

**E. Ollama (hanya jika dipakai)**
- [ ] `ollama pull qwen2.5:3b` selesai.
- [ ] Pengaturan → Ollama: "Cek koneksi" hijau.
- [ ] 1 postingan dianalisis; bandingkan hasil AI vs analisis aturan.
- [ ] Matikan Ollama lalu analisis lagi → aplikasi tidak crash, pesan jelas.

---

> Catatan jujur: tab Balas yang mengirim pesan **belum** diuji di akun Facebook nyata.
> Semua yang tercatat sebagai ✅ adalah tes otomatis dengan adapter/pengirim palsu.

---

## 5. Menambah tes

1. Buat/perluas berkas di `tests/`.
2. Gunakan fixture `db` dari `tests/conftest.py` untuk database sementara.
3. Jangan membuat tes yang memanggil Facebook atau Ollama sungguhan — tes harus
   berjalan offline dan deterministik.
4. Untuk logika halaman, uji `detect_page_state` dengan teks contoh, bukan browser.

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_filters.py -q
```

---

## 6. Catatan tentang lingkungan sandbox

Seluruh pembangunan dilakukan di lingkungan Linux tanpa akses jaringan ke Facebook
dan tanpa akun Facebook. Karena itu:

- tidak ada satu pun klaim "berhasil membaca grup Facebook" di proyek ini,
- adapter browser diuji pada perilaku gagal (browser belum terpasang, batas waktu),
  bukan pada halaman nyata,
- dokumentasi Windows ditulis berdasarkan API/perintah standar, bukan eksekusi nyata
  di Windows.
