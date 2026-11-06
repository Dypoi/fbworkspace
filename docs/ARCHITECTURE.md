# Arsitektur singkat

```
┌─────────────────────────┐
│  app.py (Streamlit)     │  UI lokal, 127.0.0.1
│  fbworkspace/dashboard  │
└───────────┬─────────────┘
            │ baca/tulis
            ▼
┌─────────────────────────┐        ┌──────────────────────┐
│  SQLite                 │◄───────┤ worker.py (proses    │
│  data/fbworkspace.db    │        │ terpisah)            │
│  posts/analyses/jobs/   │        │                      │
│  events                 │        └──────────┬───────────┘
└───────────▲─────────────┘                   │
            │                                 ▼
            │                      ┌──────────────────────┐
            │                      │ browser adapter      │
            │                      │ Playwright / manual  │
            │                      └──────────┬───────────┘
            │                                 │ halaman grup
            │                                 ▼
            │                      ┌──────────────────────┐
            │                      │ Facebook (lokal,     │
            │                      │ terlihat, tanpa      │
            │                      │ stealth/proxy)       │
            │                      └──────────────────────┘
            │
   ┌────────┴─────────┐   ┌──────────────────┐   ┌─────────────────┐
   │ pipeline.py      │──►│ filters.py       │──►│ analyzer.py     │
   │ normalisasi+dedupe│   │ kata/lokasi/harga│   │ aturan + skor   │
   └────────┬─────────┘   └──────────────────┘   └────────┬────────┘
            │                                             │ opsional
            ▼                                             ▼
   ┌─────────────────┐                          ┌─────────────────┐
   │ export.py (CSV) │                          │ ollama_client.py│
   │ drafts.py       │                          │ (localhost saja)│
   └─────────────────┘                          └─────────────────┘
```

## Prinsip desain

1. **Adapter browser terpisah.** `BrowserAdapter` adalah kontrak; Playwright bisa
   diganti/dimatikan tanpa mengubah UI, database, atau filter.
2. **Worker di proses terpisah.** Tombol Berhenti tidak bergantung pada siklus
   rerun Streamlit; status job hidup di SQLite (`stop_requested`, heartbeat).
3. **Satu jalur penyimpanan.** Input manual dan browser sama-sama melewati
   `pipeline.ingest_post`, sehingga normalisasi/dedupe/filter konsisten.
4. **AI tidak di jalur kritis.** Tanpa Ollama, semua fitur inti tetap bekerja.
   Hasil AI divalidasi sebelum disimpan dan tidak pernah mengendalikan browser.
5. **Data tidak tepercaya.** Teks postingan tidak pernah dieksekusi; ekspor CSV
   menetralkan formula; AI diberi aturan eksplisit untuk mengabaikan instruksi
   di dalam teks.
6. **Default aman.** Dashboard `127.0.0.1`, Ollama menolak endpoint non-lokal,
   profil browser & database masuk `.gitignore`.

## Alur satu sesi browser

1. Dashboard membuat baris `jobs` dengan konfigurasi (filter, batas, mode).
2. `jobs.start_job` menjalankan `python -m fbworkspace.worker --job-id N`.
3. Worker membuat adapter, memanggil `collect()` dengan callback progress.
4. Adapter membuka grup, memeriksa status halaman, menggulir terbatas, dan
   mengembalikan `CollectOutcome` (status + postingan + diagnostik).
5. Worker menyimpan tiap postingan lewat `pipeline.ingest_post`; filter dan
   analisis aturan dijalankan saat itu.
6. Bila diminta, Ollama menganalisis postingan yang lolos filter (dengan timeout).
7. Worker menutup job dengan status `selesai` / `dihentikan` / `diblokir` / `gagal`
   dan pesan yang bisa dibaca pengguna.
8. Dashboard menampilkan status, counter, event log, dan log teknis.
