# Instalasi di Windows (PowerShell) — langkah demi langkah

Panduan ini ditulis untuk Windows 10/11, RAM 16 GB, tanpa langganan software.
Semua yang dipakai gratis dan berjalan lokal.

Perkiraan kebutuhan ruang disk:

| Komponen | Ukuran |
|---|---|
| Python + dependensi Python | ± 300 MB |
| Chromium untuk Playwright | ± 150–300 MB |
| Database & log | beberapa MB |
| Ollama + model 3B (opsional) | ± 2–4 GB |

---

## 0. Buka PowerShell di folder yang Anda inginkan

Tekan `Win + X` → **Terminal (Admin)** atau **Windows PowerShell**.
Untuk instalasi ini Anda **tidak perlu** Administrator, kecuali kebijakan
perusahaan membatasinya.

```powershell
cd $HOME\Documents
```

---

## 1. Pasang Python 3.11 atau 3.12

1. Unduh dari <https://www.python.org/downloads/windows/>.
2. Saat memasang, **centang** `Add python.exe to PATH`.
3. Tutup dan buka ulang PowerShell, lalu cek:

```powershell
py --version
python --version
```

Yang diharapkan: `Python 3.11.x` atau `Python 3.12.x`.
Jika `py` tidak ada tetapi `python` ada, gunakan `python` di semua perintah berikut.

> Hindari Python dari Microsoft Store jika Anda mengalami masalah path.
> Python dari python.org lebih dapat diprediksi.

---

## 2. Pasang Git

Unduh dari <https://git-scm.com/download/win> dan pasang dengan opsi default.

```powershell
git --version
```

---

## 3. Ambil kode proyek

```powershell
git clone https://github.com/Dypoi/fbworkspace.git
cd fbworkspace
```

Jika Anda menerima pembaruan dari sesi pengembangan, lihat
[docs/GIT_GUIDE.md](GIT_GUIDE.md) untuk cara `pull` dengan benar.

---

## 4. Jalankan setup otomatis

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1
```

Script ini akan:
1. mencari Python 3.10+,
2. membuat virtual environment `.venv`,
3. memasang `requirements.txt`,
4. mengunduh Chromium untuk Playwright,
5. membuat `.env` dari `.env.example` (tanpa rahasia),
6. membuat folder `data/` dan `logs/`.

### Jika PowerShell memblokir script

Gejala: `... cannot be loaded because running scripts is disabled on this system`.

Pakai cara di atas (`-ExecutionPolicy Bypass -File ...`) yang hanya berlaku untuk
satu perintah itu. Jangan mengubah kebijakan global mesin Anda hanya untuk ini.

### Setup manual (jika script gagal)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
New-Item -ItemType Directory -Force -Path data, logs
```

Jika `Activate.ps1` diblokir, jalankan langsung tanpa aktivasi:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

---

## 5. Periksa konfigurasi

Buka `.env` (Notepad/VSCode). Nilai default sudah aman:

```
FBW_HOST=127.0.0.1
FBW_PORT=8501
FBW_DB_PATH=data/fbworkspace.db
FBW_BROWSER_HEADLESS=false
FBW_BROWSER_PROFILE_DIR=data/browser_profile
FBW_OLLAMA_URL=http://127.0.0.1:11434
FBW_OLLAMA_MODEL=qwen2.5:3b
FBW_OLLAMA_ENABLED=false
```

**Jangan** mengubah `FBW_HOST` menjadi `0.0.0.0` untuk laptop pribadi: itu membuat
dashboard bisa diakses perangkat lain di jaringan yang sama.
**Jangan** mengisi password/cookie/token Facebook di `.env` — aplikasi tidak
memerlukannya.

---

## 6. Jalankan dashboard

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_windows.ps1
```

Atau klik dua kali `run_windows.bat`.

Browser default akan terbuka di `http://127.0.0.1:8501`.
Untuk berhenti: `Ctrl + C` di jendela PowerShell.

---

## 7. Verifikasi tanpa Facebook

1. Klik **Muat data DEMO (sintetis)**.
2. Tab **Postingan** harus menampilkan 9 baris berlabel `DEMO`.
3. Buka satu detail → cek kategori/model/harga/lokasi/skor.
4. Klik **Ekspor CSV**, buka di Excel. Sel yang berisi `=SUM(...)` harus tampil
   sebagai teks dengan awalan `'`, bukan dihitung.
5. Tab **Input Manual** → tempel contoh → simpan → pastikan masuk database.
6. Tab **Pengaturan & Data** → coba hapus data, lalu muat ulang DEMO.

Jika semua langkah ini berhasil, bagian lokal sudah sehat.

---

## 8. POC pembacaan grup (wajib sebelum pemakaian serius)

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\poc_windows.ps1 `
  -Url "https://www.facebook.com/groups/ID-GRUP-ANDA/" `
  -MaxPosts 5 -MaxSeconds 90
```

Baca hasil dan interpretasinya di **[POC_FACEBOOK.md](POC_FACEBOOK.md)**.
Jangan lanjut ke dashboard jika POC belum memberi hasil yang bisa Anda pahami.

---

## 9. Ollama (opsional, lakukan terakhir)

1. Pasang Ollama dari <https://ollama.com/download/windows>.
2. Buka PowerShell:

```powershell
ollama pull qwen2.5:3b
ollama list
```

3. Cek server lokal: <http://127.0.0.1:11434> (hanya dari laptop Anda).
4. Ubah `.env`:

```
FBW_OLLAMA_ENABLED=true
FBW_OLLAMA_MODEL=qwen2.5:3b
FBW_OLLAMA_TIMEOUT=60
```

5. Restart dashboard, masuk tab **Analisis Ollama** → **Cek koneksi Ollama**.

Catatan performa: 3B terkuantisasi biasanya butuh ~3–4 GB RAM saat berjalan.
Dengan RAM 16 GB seharusnya cukup, tetapi kecepatan bergantung CPU/GPU.
Tanpa GPU, satu postingan bisa puluhan detik. Aplikasi tetap berfungsi tanpa AI.

---

## 10. Menjalankan tes

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Harus muncul ringkasan tes lulus. Jika ada yang gagal, jangan lanjut ke POC;
lihat pesan error terlebih dahulu.

---

## 11. Memperbarui aplikasi

```powershell
cd $HOME\Documents\fbworkspace
git status
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Jika ada perubahan dependensi atau Playwright, jalankan ulang:

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

Database dan profil browser **tidak** tersentuh oleh `git pull` karena tidak
dilacak Git.

---

## 12. Pemecahan masalah

| Gejala | Penyebab umum | Solusi |
|---|---|---|
| `python` tidak dikenali | Python tidak di PATH | Pasang ulang, centang "Add to PATH", buka PowerShell baru |
| `running scripts is disabled` | Execution policy | Gunakan `powershell -ExecutionPolicy Bypass -File ...` |
| `pip install` gagal SSL/proxy | Jaringan kantor | Coba jaringan lain; jangan matikan verifikasi SSL |
| `playwright install chromium` lambat/gagal | Unduhan besar | Ulangi; periksa ruang disk dan antivirus |
| POC: `Executable doesn't exist ... chrome` | Chromium belum diunduh | `.\.venv\Scripts\python.exe -m playwright install chromium` |
| POC: 0 postingan padahal halaman terbuka | Selector/keanggotaan/grup butuh join | Lihat [POC_FACEBOOK.md](POC_FACEBOOK.md) bagian "0 postingan" |
| Browser terbuka lalu langsung tertutup | Profil sedang dipakai proses lain | Tutup semua Chromium, hapus `data/browser_profile` jika perlu |
| `database is locked` | Dua proses menulis bersamaan | Tutup dashboard lain; tunggu beberapa detik; ulangi |
| Port 8501 dipakai | Streamlit lain berjalan | Ubah `FBW_PORT=8502` di `.env` |
| Halaman dashboard kosong | Cache browser | `Ctrl + F5`; coba mode incognito |
| Ollama: connection refused | Server belum jalan | Jalankan Ollama, tes <http://127.0.0.1:11434> |
| Ollama: model tidak ada | Belum `pull` | `ollama pull qwen2.5:3b` |
| Antivirus menandai script | Script PowerShell buatan sendiri | Periksa isinya (dokumentasi ini), kecualikan folder proyek bila Anda yakin |

---

## 13. Checklist keamanan setelah instalasi

- [ ] `.env` ada dan hanya berisi konfigurasi, tanpa rahasia.
- [ ] `FBW_HOST=127.0.0.1`.
- [ ] `git status` tidak menampilkan `.env`, `*.db`, `data/browser_profile/`, `logs/`.
- [ ] Dashboard tidak bisa diakses dari HP di jaringan yang sama.
- [ ] Anda tahu cara menghapus data di tab **Pengaturan & Data**.
- [ ] Anda belum menjalankan POC sebelum membaca [POC_FACEBOOK.md](POC_FACEBOOK.md).

---

## 14. Jangan meniru konfigurasi "preview"

Jika Anda melihat dokumentasi/screenshot dari lingkungan pengembangan yang memakai
`--server.address 0.0.0.0`, itu hanya agar halaman bisa dipratinjau lewat proxy.
Untuk laptop pribadi, pertahankan `127.0.0.1`. File `.streamlit/config.toml` dan
`scripts/run_windows.ps1` di repo ini sudah aman; jangan mengubahnya menjadi
`0.0.0.0` kecuali Anda benar-benar paham konsekuensinya (dashboard bisa diakses
perangkat lain di jaringan yang sama).

---

## 15. Menghapus instalasi

```powershell
# dari folder proyek
Remove-Item -Recurse -Force .venv
Remove-Item -Recurse -Force data\browser_profile
Remove-Item -Recurse -Force logs
Remove-Item .env
```

Database ada di `data\fbworkspace.db` — hapus hanya jika Anda yakin tidak
memerlukan datanya. Setelah itu folder proyek bisa dihapus seluruhnya.
