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
249 passed in ~5s
ruff check . --select E,F,W,I  ->  All checks passed!
```

| Berkas tes | Jumlah | Cakupan |
|---|---:|---|
| `tests/test_normalize.py` | 40 | normalisasi teks, validasi tautan grup, ID postingan, dedupe key, harga Indonesia, waktu posting |
| `tests/test_analyzer.py` | 33 | kategori, model iPhone, harga vs budget, detail, lokasi, skor & batasnya, teks tidak tepercaya |
| `tests/test_db.py` | 21 | simpan, dedupe, status, analisis upsert, filter, job, event, penghapusan, statistik |
| `tests/test_llm_validation.py` | 25 | validasi keluaran AI, anti-halusinasi, prompt, payload `think`/`num_predict`, offline, blokir endpoint non-lokal |
| `tests/test_login.py` | 22 | cookie sesi `c_user`, penantian login (deteksi/timeout/berhenti), file marker status |
| `tests/test_filters.py` | 18 | batas kata, frasa, inklusi/eksklusi, lokasi, harga, mode ketat |
| `tests/test_page_state.py` | 21 | deteksi login/checkpoint/penolakan/rate limit/404, pengecualian penanda login saat sesi ada |
| `tests/test_demo_and_worker.py` | 14 | data DEMO, pemilihan adapter, pemetaan status job, batas |
| `tests/test_export.py` | 14 | formula injection, BOM, header, detail & alasan, truncation |
| `tests/test_pipeline.py` | 12 | parsing input manual, penyimpanan, dedupe, batch |
| `tests/test_drafts.py` | 11 | draf netral, larangan klaim, placeholder, profil |
| `tests/test_app_smoke.py` | 6 | dashboard dirender (kosong & setelah DEMO), hanya 3 tab, segmentasi Semua/Lolos/Tersembunyi, tombol filter ulang |
| `tests/test_ui_data.py` | 12 | hitungan per status filter, hapus per sumber (DEMO), pratinjau filter, "terapkan ulang filter" tanpa duplikasi |

### Pengujian non-unit yang juga dijalankan

| Uji | Cara | Hasil |
|---|---|---|
| Worker end-to-end (mode manual) | `python -m fbworkspace.worker --job-id 1` | Job `selesai`, event log lengkap, counter JSON benar |
| Menunggu login (simulasi) | `wait_for_login` dengan jam & cookie palsu | Deteksi, timeout, dan berhenti bekerja tanpa menggantung |
| Helper login tanpa Chromium | `scripts/login_helper.py` | Gagal dengan pesan + saran `playwright install chromium`, status ditulis ke marker |
| POC tanpa Chromium terpasang | `python scripts/poc_facebook.py --yes` | Gagal dengan pesan jelas + saran `playwright install chromium`, diagnostik JSON tersimpan, exit code kesalahan |
| Server Streamlit | `streamlit run app.py` + `curl /_stcore/health` | `200 ok` |
| Render dashboard | `streamlit.testing.v1.AppTest` | Tanpa exception; hanya 3 tab; segmentasi Semua/Lolos/Tersembunyi ada; DEMO & filter ulang berjalan |

---

## 3. Yang BELUM diuji (wajib Anda validasi)

| Hal | Mengapa belum | Cara memvalidasi |
|---|---|---|
| Membuka grup Facebook nyata | Lingkungan pengembangan tidak punya akses/akun Facebook | Jalankan POC ([POC_FACEBOOK.md](POC_FACEBOOK.md)) |
| Login manual di jendela Chromium | Butuh akun nyata | Jalankan `scripts/login_helper.py`; pastikan status `selesai`, lalu POC tidak lagi `perlu_login` |
| Penantian login di halaman nyata | Butuh halaman login nyata | Jalankan pembacaan tanpa login; jendela harus tetap terbuka sampai `FBW_LOGIN_WAIT_SECONDS` |
| Selector `div[role="article"]` pada akun Anda | Perlu halaman nyata | POC: periksa jumlah postingan & kualitas teks |
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

**D. Ollama (hanya jika dipakai)**
- [ ] `ollama pull qwen2.5:3b` selesai.
- [ ] Pengaturan → Ollama: "Cek koneksi" hijau.
- [ ] 1 postingan dianalisis; bandingkan hasil AI vs analisis aturan.
- [ ] Matikan Ollama lalu analisis lagi → aplikasi tidak crash, pesan jelas.

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
