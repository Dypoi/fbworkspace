# Panduan Git: pertama kali & pembaruan berikutnya

Panduan ini untuk pengguna Windows yang baru memakai Git.
Git dipakai **hanya untuk kode & dokumentasi**, bukan untuk data atau sesi login.

---

## 1. Yang tidak boleh masuk Git

Sudah diatur di `.gitignore`, tetapi Anda harus tahu alasannya:

| File/folder | Alasan |
|---|---|
| `.env` | konfigurasi lokal, bisa memuat info sensitif |
| `data/*.db` | berisi postingan & catatan Anda |
| `data/browser_profile/` | **cookie/sesi Facebook** — rahasia |
| `data/business_profile.json` | data bisnis pribadi |
| `logs/`, `*.log` | log bisa memuat detail aktivitas |
| `models/`, `*.gguf` | file model besar |
| `.venv/` | dependensi, bisa dibuat ulang |

Periksa `.gitignore` sebelum menambah file baru.

---

## 2. Pertama kali di laptop Anda

### 2.1 Cek identitas Git
```powershell
git config --global user.name "Nama Anda"
git config --global user.email "email-anda@example.com"
git config --global core.autocrlf true
```

`core.autocrlf true` membantu konsistensi baris baru antara Windows dan Linux.

### 2.2 Clone repository
```powershell
cd $HOME\Documents
git clone https://github.com/Dypoi/fbworkspace.git
cd fbworkspace
```

Saat diminta login, gunakan browser (Git Credential Manager) atau Personal Access
Token yang Anda buat sendiri di GitHub. **Jangan** pernah menempelkan token ke
dalam chat, issue, atau file yang di-commit.

### 2.3 Lihat cabang
```powershell
git branch -a
git status
```

Jika kode terbaru ada di cabang pengembangan (mis. `arena/...`), ada dua cara:
- **Disarankan:** gabungkan lewat Pull Request di GitHub, lalu di laptop:
  ```powershell
  git checkout main
  git pull --ff-only
  ```
- Alternatif tanpa merge: ambil cabang itu langsung
  ```powershell
  git fetch origin
  git checkout -b nama-lokal origin/arena/81d0e57c-fbworkspace
  ```

Gunakan `main` untuk pemakaian sehari-hari setelah perubahan digabung.

### 2.4 Verifikasi tidak ada file sensitif
```powershell
git status --ignored
```

Pastikan `.env`, `data\*.db`, dan `data\browser_profile\` muncul sebagai *ignored*,
bukan *untracked*.

---

## 3. Alur harian

```powershell
cd $HOME\Documents\fbworkspace
git status                 # apa yang berubah
git pull --ff-only         # ambil pembaruan, tanpa merge commit aneh
```

Lalu jalankan aplikasi:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_windows.ps1
```

Setelah selesai, periksa lagi:

```powershell
git status
```

Jika muncul `.env`, `*.db`, atau profil browser sebagai *untracked*, **jangan**
`git add .` secara membabi buta. Tambahkan pola ke `.gitignore` lebih dulu.

---

## 4. Menyimpan perubahan Anda sendiri

Jika Anda mengubah kode/dokumentasi dan ingin menyimpannya:

```powershell
git checkout -b perubahan-saya
git add README.md docs\INSTALL_WINDOWS.md
git commit -m "Perjelas langkah instalasi Playwright"
git push -u origin perubahan-saya
```

Lalu buat Pull Request di GitHub. Hindari `git add -A` tanpa memeriksa
`git status` terlebih dahulu.

### Jika tidak sengaja menambahkan file sensitif
Sebelum commit:
```powershell
git restore --staged .env
```
Sudah terlanjur commit tetapi belum push:
```powershell
git reset --soft HEAD~1
git restore --staged .env
```
Sudah terlanjur push: hapus dari riwayat (mis. `git filter-repo`) **dan** anggap
rahasianya bocor — ganti kata sandi/logout sesi Facebook, hapus file profil browser.
Jangan hanya mengandalkan `git rm`.

---

## 5. Memperbarui dependensi

Jika `requirements.txt` berubah setelah `git pull`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

---

## 6. Memeriksa riwayat

```powershell
git log --oneline -10
git diff
git diff --stat
```

Untuk melihat siapa mengubah baris tertentu:
```powershell
git log -p -- app.py
```

---

## 7. Konflik sederhana

Jika `git pull` menghasilkan konflik:

```powershell
git status
# buka file yang ditandai, selesaikan bagian <<<<<<< ======= >>>>>>>
git add nama-file
git commit
```

Untuk perubahan lokal yang belum siap, simpan sementara:
```powershell
git stash push -m "pekerjaan saya"
git pull --ff-only
git stash pop
```

---

## 8. Kesalahan umum

| Gejala | Penyebab | Solusi |
|---|---|---|
| `Authentication failed` | kredensial kedaluwarsa | Login ulang lewat Git Credential Manager atau buat PAT baru |
| `Updates were rejected` | ada commit lokal | `git pull --rebase` lalu push, atau buat cabang baru |
| Semua file tampak berubah | line endings | set `core.autocrlf true`, lalu `git checkout -- .` |
| `fatal: not a git repository` | salah folder | `cd` ke folder proyek |
| File besar ditolak | > 100 MB | jangan commit model/DB; gunakan penyimpanan lokal |
| `.env` terlihat di `git status` | belum di-ignore | pastikan `.gitignore` berisi `.env` |

---

## 9. Ringkasan perintah yang aman dihafal

```powershell
git status                 # selalu mulai dari sini
git pull --ff-only         # ambil pembaruan
git add <file-tertentu>    # jangan git add -A tanpa cek
git commit -m "pesan"
git push
git log --oneline -10
```

Itu cukup untuk pemakaian pribadi.
