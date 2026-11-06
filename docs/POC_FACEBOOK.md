# Proof of Concept (POC): apakah grup ini bisa dibaca?

Dokumen ini menjelaskan cara menguji **kelayakan** pembacaan grup Facebook
sebelum memakai sistem penuh. Tujuannya menghindari dua kesalahan umum:

1. Membangun sistem besar di atas asumsi bahwa tautan publik = isi bebas dibaca.
2. Menyimpulkan "berhasil" hanya karena halaman terbuka, padahal 0 postingan terbaca.

---

## 1. Yang diuji POC

- Apakah halaman grup terbuka untuk akun Anda.
- Apakah muncul login / checkpoint / penolakan / pembatasan.
- Apakah elemen postingan bisa ditemukan dengan selector konservatif
  `div[role="article"]`.
- Seperti apa teks yang berhasil dibaca (utuh atau terpotong).

POC **tidak** menyimpan ke database, tidak mengirim apa pun ke Facebook, dan
tidak mencoba melewati hambatan.

---

## 2. Jalankan

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\poc_windows.ps1 `
  -Url "https://www.facebook.com/groups/ID-GRUP-ANDA/" `
  -MaxPosts 5 -MaxSeconds 90
```

Atau langsung:

```powershell
.\.venv\Scripts\python.exe scripts\poc_facebook.py `
  --url "https://www.facebook.com/groups/ID-GRUP-ANDA/" `
  --max-posts 5 --max-seconds 90 --login-wait 180
```

Jendela Chromium akan terbuka dan **terlihat**. Jika Facebook meminta login,
**jendela TIDAK ditutup** — Anda diberi waktu login sendiri di jendela itu
(default 180 detik, ubah dengan `FBW_LOGIN_WAIT_SECONDS` atau `--login-wait`).
Skrip tidak pernah membaca/menyimpan kredensial Anda. Profil browser disimpan di
`data/browser_profile/` (masuk `.gitignore`).

Kalau ingin login lebih dulu tanpa menjalankan POC:

```powershell
.\.venv\Scripts\python.exe scripts\login_helper.py
```

Setelah login terdeteksi (cookie sesi `c_user` ada), POC memuat ulang grup dan
melanjutkan. Status `perlu_login` baru dikembalikan bila waktu tunggu habis atau
`--login-wait 0`.

---

## 3. Membaca hasil

| Status | Arti | Tindakan |
|---|---|---|
| `ok` + ≥1 postingan | Ada elemen postingan terbaca | Periksa apakah teksnya utuh dan relevan |
| `ok` + 0 postingan | Halaman terbuka, tidak ada postingan terbaca | Lihat bagian 4 |
| `perlu_login` | Waktu tunggu login habis / login belum selesai | Jalankan `scripts\login_helper.py`, pastikan status `selesai`, lalu ulangi POC |
| `tantangan_keamanan` | Checkpoint/CAPTCHA | **Berhenti.** Selesaikan manual bila Anda mau, jangan dipaksa otomatis |
| `akses_ditolak` | Butuh keanggotaan / grup tidak terbuka untuk akun ini | Berhenti, atau gabung grup secara wajar bila Anda memang ingin |
| `dibatasi_sementara` | Rate limit | Berhenti. Jangan mengulang berkali-kali |
| `halaman_tidak_ditemukan` | Tautan salah/grup hilang | Periksa tautan |
| `kesalahan` | Masalah teknis (browser, jaringan) | Baca pesan detail |
| `dihentikan_pengguna` | Batas waktu / Ctrl+C | Ulangi dengan batas lebih kecil bila perlu |

Kode keluar skrip: `0` berhasil, `3` halaman OK tanpa postingan, `4` diblokir,
`5` kesalahan teknis, `6` dibatalkan.

Diagnostik disimpan di `logs/poc_YYYYMMDD_HHMMSS.json` (tidak masuk Git).
Berguna untuk membandingkan percobaan antar waktu tanpa menyimpan teks lengkap ke Git.

---

## 4. Jika hasilnya "ok" tetapi 0 postingan

Kemungkinan penyebab, dari yang paling umum:

1. **Grup memerlukan keanggotaan.** Banyak grup "publik" tetap hanya menampilkan
   sebagian isi untuk non-anggota. Pilihan Anda: gabung secara wajar (ikuti aturan
   grup) atau gunakan ① **Tempel manual** di tab Mulai.
2. **Konten dimuat lambat.** Ulangi dengan `-MaxSeconds 120` dan gulir manual sekali
   di jendela browser untuk memancing pemuatan.
3. **Selector tidak cocok.** Facebook bisa memakai struktur berbeda. Berikut cara
   memeriksa **tanpa mengubah kode dulu**:
   - Buka grup di Chromium yang sama.
   - Klik kanan pada teks sebuah postingan → **Inspect**.
   - Lihat elemen pembungkusnya. Jika memakai `role="article"`, selector seharusnya
     cocok. Jika tidak, catat atribut yang stabil (hindari class acak seperti
     `x1y2z3` yang berubah-ubah).
   - Uji selector baru tanpa mengubah kode:
     ```powershell
     .\.venv\Scripts\python.exe scripts\poc_facebook.py `
       --url "https://www.facebook.com/groups/ID/" `
       --selector 'div[role="feed"] > div' --max-posts 3
     ```
   - Jika selector baru bekerja, catat di catatan Anda. **Jangan** menganggapnya
     permanen; struktur bisa berubah lagi.
4. **Halaman menampilkan versi mobile/sederhana.** Coba tautan `m.facebook.com`
   secara manual di browser untuk melihat apakah isinya berbeda.

> Penting: POC ini tidak dirancang untuk "menemukan jalan masuk". Jika isi grup
> memang tidak ditampilkan untuk akun Anda, jalur yang benar adalah mode Tempel manual
> atau menjadi anggota sesuai aturan grup.

---

## 5. Batas etika & kepatuhan

- Gunakan batas kecil. Jangan menjalankan POC berulang-ulang dalam waktu singkat.
- Jangan menambahkan stealth, UA palsu, proxy rotation, atau solver CAPTCHA.
- Jangan mengumpulkan profil anggota/komentar.
- Berhenti saat muncul tantangan keamanan. Itu bukan hambatan teknis untuk diakali,
  melainkan kontrol platform.
- Kepatuhan pada Ketentuan Facebook dan hukum yang berlaku tetap tanggung jawab Anda.

---

## 6. Setelah POC berhasil

1. Catat: tautan grup, tanggal, status, jumlah postingan terbaca, kualitas teks.
2. Jalankan dari dashboard dengan batas serupa (3–5 postingan, 2–3 menit).
3. Bandingkan hasilnya dengan POC. Jika berbeda jauh, kemungkinan pembatasan atau
   perubahan halaman — berhenti dan catat.
4. Jangan menaikkan batas hanya karena "berhasil sekali". Naikkan bertahap dan
   hentikan bila muncul pembatasan.

---

## 7. Status pengujian POC ini

POC ini **belum diuji pada Facebook sungguhan** dari lingkungan pengembangan
(tidak ada akses jaringan ke Facebook dan tidak ada akun untuk diuji). Yang sudah
diuji otomatis:

- deteksi status halaman dari teks/URL (login, checkpoint, penolakan, rate limit, 404),
- pengecualian penanda login palsu bila cookie sesi `c_user` sudah ada,
- logika menunggu login (deteksi, timeout, berhenti) dengan jam dan cookie palsu,
- penulisan/pembacaan file status sesi login,
- penanganan kegagalan saat Chromium belum terpasang (pesan jelas, bukan crash),
- logika batas postingan/waktu dan penghentian.

Yang harus Anda validasi sendiri: pembukaan grup nyata, login manual nyata,
perilaku saat checkpoint muncul, dan kesesuaian selector.
