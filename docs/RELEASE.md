# Panduan Rilis Standar — SWARARIA AUDIO LAB

Dokumen ini menjelaskan alur rilis standar (dari pengembangan harian hingga rilis publik Windows Installer) agar proses bugfix tetap cepat di lokal, sementara rilis publik tetap bersih, teruji, dan aman bagi pengguna.

---

## 1. Aturan Branch & Penomoran Versi (SemVer)

- **`main`** selalu menjadi sumber rilis yang bersih dan lolos semua tes.
- Kerjakan fitur atau perbaikan bug di branch terpisah (`feature/...` atau `fix/...`), lalu merge ke `main` setelah CI hijau.
- Gunakan format **Semantic Versioning**:
  - `v1.0.1` — **Patch**: perbaikan bug tanpa perubahan besar.
  - `v1.1.0` — **Minor**: fitur baru yang tetap kompatibel.
  - `v2.0.0` — **Major**: perubahan arsitektur / breaking change.
  - `v1.0.1-rc.1` / `v1.1.0-beta.1` — **Pre-release / Release Candidate** untuk uji coba pra-rilis.

### 3 File Versi yang Harus Selalu Sinkron
Sebelum membuat tag rilis baru, pastikan versi di 3 file berikut **identik** (tanpa awalan `v`):

1. `pyproject.toml` → `version = "1.0.1"`
2. `swararia/server/state.py` → `APP_VERSION = "1.0.1"`
3. `packaging/installer.iss` → `#define MyAppVersion "1.0.1"`

Verifikasi sinkronisasi versi di lokal sebelum push tag:
```bash
.venv/bin/python packaging/build_desktop.py --check-version --release-tag v1.0.1-rc.1
```

> **Mengapa ini penting?** Fitur pembaruan otomatis (*in-app update checker*) membandingkan `APP_VERSION` dengan tag rilis terbaru di GitHub Releases. Jika tidak cocok, aplikasi akan terus menawarkan pembaruan berulang kali.

---

## 2. Alur Harian (Bugfix Cepat Tanpa Tunggu CI)

- **Uji logika & UI harian:** jalankan langsung `.venv/bin/python -m swararia` (buka `http://127.0.0.1:8765`) atau `python -m swararia.desktop` di Python Windows.
- **Jalankan seluruh test suite sebelum merge ke `main`:**
  ```bash
  .venv/bin/python tests/python/run_tests.py
  for f in tests/js/*.test.mjs; do node "$f"; done
  ```
- **Hindari memakai GitHub Actions Artifacts untuk iterasi bugfix kecil** — cukup gunakan pipeline GitHub saat akan membuat **Pre-release (RC)** atau **Stable Release**.

---

## 3. Langkah Rilis: Pre-release (RC) → QA → Stable Release

### Tahap A — Siapkan Commit Rilis di `main`
1. Pastikan seluruh perubahan sudah di-merge ke `main` dan semua tes lulus.
2. Perbarui versi di 3 file (`pyproject.toml`, `swararia/server/state.py`, `packaging/installer.iss`), misal ke `1.0.1`.
3. Cek sinkronisasi versi dan tes lokal:
   ```bash
   .venv/bin/python packaging/build_desktop.py --check-version --release-tag v1.0.1-rc.1
   .venv/bin/python tests/python/run_tests.py
   for f in tests/js/*.test.mjs; do node "$f"; done
   ```
4. Commit dan push ke `main`.

### Tahap B — Publikasikan Kandidat Rilis (`-rc.1`)
```bash
git tag v1.0.1-rc.1
git push origin v1.0.1-rc.1
```

Workflow `.github/workflows/release-windows.yml` akan otomatis:
1. Menjalankan **Release Quality Gate** (validasi versi + Python/JS tests).
2. Mem-build `SwarariaAudioLab.exe` + FFmpeg Shared, melakukan **smoke test** binary & bundle, lalu mengompilasi `SwarariaAudioLab_Setup.exe` + `.sha256`.
3. Mem-publish ke **GitHub Releases** dengan tanda **Pre-release**.
   - Unduhan lewat halaman **Releases** menggunakan CDN cepat GitHub (jauh lebih cepat dari Actions Artifacts).
   - **Auto-updater pengguna stabil tidak terpicu**, karena GitHub `/releases/latest` mengabaikan Pre-release.

### Tahap C — Checklist QA Manual (5 Menit di Windows / Windows Sandbox)
Unduh `SwarariaAudioLab_Setup.exe` dari halaman Pre-release dan uji:
- [ ] **Fresh install / Upgrade:** Wizard berjalan mulus, shortcut terbuat, dan upgrade tidak menghapus koleksi di `Music\SwarariaAudioLab` maupun pengaturan di `AppData\Local\SwarariaAudioLab`.
- [ ] **Ingest → FOH Player:** Unduh 1 lagu tunggal dan 1 playlist dari Ingest → lagu otomatis muncul di FOH Player tanpa perlu refresh manual.
- [ ] **Playback & DSP:** Play, Pause, Stop, Fade Out, dan Cue Pad berjalan normal tanpa error audio.
- [ ] **Remote (opsional):** Koneksi HP Browser Remote / Android Native Remote berjalan normal.
- [ ] **Uninstall bersih:** Uninstall via Windows Settings menghapus aplikasi tanpa menghapus koleksi audio pengguna.

> Jika ditemukan bug saat QA RC, perbaiki di branch → merge ke `main` → buat `v1.0.1-rc.2` dan ulangi verifikasi.

### Tahap D — Publikasikan Rilis Stabil ke Publik (`v1.0.1`)
Setelah RC lolos semua checklist QA:
```bash
git tag v1.0.1
git push origin v1.0.1
```

Pipeline akan mem-build dari commit `main` yang bersih dan mem-publish **GitHub Release (Latest)**. Pengguna versi sebelumnya (`v1.0.0`) akan otomatis melihat notifikasi pembaruan di header aplikasi.

---

## 4. Build Lokal Opsional di Windows

Jika Anda ingin membangun `.exe` atau installer langsung di mesin Windows lokal (tanpa mengunduh dari GitHub):

```powershell
# Build portable app saja (cepat untuk uji lokal):
python packaging/build_desktop.py --verify-bundle

# Build lengkap + Setup Wizard (butuh Inno Setup 6):
python packaging/build_desktop.py --verify-bundle --installer
```

Catatan: Jika `.vendor/bin/` sudah berisi `ffmpeg.exe`, `ffprobe.exe`, dan DLL FFmpeg Shared, tidak perlu menambahkan `--fetch-ffmpeg`.
