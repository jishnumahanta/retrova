# Retrova

<p align="center">
  <img src="assets/logo-64.png" width="80" height="80" alt="Retrova Logo" />
</p>

<h3 align="center">Recover. Verify. Reclaim.</h3>

<p align="center">
  A free, open-source, local-only desktop app for macOS and Windows that gets your files off damaged or unmountable drives without ever writing to them, proves every copy is byte-for-byte identical, and helps you reclaim disk space.
</p>

<p align="center">
  <a href="https://github.com/jishnumahanta/retrova/releases/tag/v1.0.1"><img src="https://img.shields.io/badge/Release-v1.0.1-2BD9A0?style=flat-square" alt="Release v1.0.1" /></a>
  <a href="#downloads"><img src="https://img.shields.io/badge/Platform-macOS%20%7C%20Windows-blue?style=flat-square" alt="Platform" /></a>
  <img src="https://img.shields.io/badge/Offline-100%25%20Local-17A877?style=flat-square" alt="100% Local" />
  <img src="https://img.shields.io/badge/Source%20Drives-Strictly%20Read--Only-success?style=flat-square" alt="Read Only" />
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0-2BD9A0?style=flat-square" alt="License: AGPL-3.0" /></a>
  <a href="#-support-retrova"><img src="https://img.shields.io/badge/Sponsor-%E2%9D%A4-ff69b4?style=flat-square" alt="Sponsor" /></a>
</p>

---

## 📸 Screenshots

<p align="center">
  <img src="assets/rescue.jpg" width="100%" alt="Retrova Rescue UI" />
  <br>
  <em>Rescue Dashboard: Drive health monitor, APFS snapshot explorer, recovery stats, and byte-for-byte verification.</em>
</p>

<p align="center">
  <img src="assets/tools.jpg" width="100%" alt="Retrova Tools UI" />
  <br>
  <em>Disk Care & Reclaim: Interactive space treemap, APFS-clone duplicate finder, smart cleanup, system monitor, and local OCR search.</em>
</p>

---

## ⬇️ Downloads (v1.0.1)

| Platform | Download | Architecture | Size |
| :--- | :--- | :--- | :--- |
| **macOS** | [**`Retrova-1.0.1-mac-arm64.dmg`**](https://github.com/jishnumahanta/retrova/releases/download/v1.0.1/Retrova-1.0.1-mac-arm64.dmg) | Apple Silicon (M1, M2, M3, M4+) | 18 MB |
| **macOS** | [**`Retrova-1.0.1-mac-x64.dmg`**](https://github.com/jishnumahanta/retrova/releases/download/v1.0.1/Retrova-1.0.1-mac-x64.dmg) | Intel 64-bit | 18 MB |
| **Windows** | [**`Retrova-1.0.1-win-x64-Setup.exe`**](https://github.com/jishnumahanta/retrova/releases/download/v1.0.1/Retrova-1.0.1-win-x64-Setup.exe) | Windows 10 & 11 (Installer) | 14 MB |
| **Windows** | [**`Retrova-1.0.1-win-x64-portable.zip`**](https://github.com/jishnumahanta/retrova/releases/download/v1.0.1/Retrova-1.0.1-win-x64-portable.zip) | Windows 10 & 11 (Portable, no install) | 24 MB |

Check the official SHA-256 checksums in [**`SHA256SUMS`**](https://github.com/jishnumahanta/retrova/releases/download/v1.0.1/SHA256SUMS) to verify download integrity.

---

## 🛡️ Core Safety Invariants

1. **Strictly Read-Only**: Source drives are opened exclusively with read-only system handles. No recovery process ever writes, formats, partitions, or touches blocks on the damaged disk.
2. **Non-Destructive Deletions**: Deletions never execute permanent `rm`. All items cleaned or de-duplicated are moved to your operating system's Trash or Recycle Bin.
3. **Collision Safe**: Staged files are written as `.part` and renamed once verified. Collisions are preserved as `filename.retrova-N`.
4. **Zero Telemetry / Air-Gapped**: Retrova binds exclusively to `127.0.0.1` using a cryptographic session token. It never connects to the internet or sends analytics.

---

## ⚡ What Retrova Does

### 🛟 Rescue
* **Unmountable Drive Recovery**: Direct block-level parser for damaged or unmountable partitions:
  * **APFS** (with snapshot browsing, compressed forks `lzvn`/`zlib`, and corrupted catalog tree tolerance)
  * **NTFS**, **FAT12/16/32**, **exFAT**
  * **ext2/3/4**, **XFS**, **Btrfs**, and **GPT/MBR** partition table scanning.
* **Deep File Carving**: Extracts **47 file formats** directly from raw block devices without filesystem metadata. Header-and-structure validation rejects false positives.
* **Sector-Safe Disk Imaging**: Creates raw bit-for-bit `.img` disk images with bad-sector skipping and sparse zero-block detection.

### 🔍 Verify & Repair
* **Byte-for-Byte Comparison**: Compares recovered copies directly against the original drive blocks in **Quick** (name & size), **Sample** (64KB head/tail hash), or **Full** (byte-for-byte) mode.
* **Audit Manifests & One-Click Repair**: Generates CSV problem logs and SHA-256 manifests; automatically recopies mismatched files.

### 🧹 Reclaim
* **Sparse File Reclaim (Docker / Colima / VMs)**: Fixes the file-bloat issue where virtual disks expand to their maximum virtual limit upon recovery. Shrinks files back to their physical allocated size using native sparse filesystem holes.
* **APFS Duplicate Cloning**: Byte-exact duplicate finder. On macOS, duplicates can be turned into **APFS Clones** (Copy-on-Write) instantly freeing disk space while keeping both files visible in Finder.
* **Smart Dev Cleanup**: Identifies regenerable development caches (`node_modules`, `.pnpm-store`, `.venv`, `.next`, `DerivedData`) with exact commands to rebuild them.
* **Local OCR Search**: Apple Vision (macOS) and WinRT OCR (Windows) local text indexing into an offline SQLite FTS5 database to search screenshots, photos, and scanned PDFs by keyword.

---

## 🚀 Installation & First Launch

### macOS (Apple Silicon & Intel)
1. Download either [`Retrova-1.0.1-mac-arm64.dmg`](https://github.com/jishnumahanta/retrova/releases/download/v1.0.1/Retrova-1.0.1-mac-arm64.dmg) (Apple Silicon) or [`Retrova-1.0.1-mac-x64.dmg`](https://github.com/jishnumahanta/retrova/releases/download/v1.0.1/Retrova-1.0.1-mac-x64.dmg) (Intel).
2. Open the DMG and drag **Retrova.app** into `/Applications`.
3. Because Retrova is independently ad-hoc signed, macOS Gatekeeper may show a notice on first launch:
   * Double-click **Retrova** in Applications, then click **Done**.
   * Open **System Settings $\rightarrow$ Privacy & Security**.
   * Scroll down to the security section and click **Open Anyway**.
4. *(Recommended for raw disk recovery)*: In **System Settings $\rightarrow$ Privacy & Security $\rightarrow$ Full Disk Access**, enable Retrova to allow reading unmountable raw block devices (`/dev/rdisk*`).

### Windows 10 & 11
1. Download [`Retrova-1.0.1-win-x64-Setup.exe`](https://github.com/jishnumahanta/retrova/releases/download/v1.0.1/Retrova-1.0.1-win-x64-Setup.exe) (or the portable zip).
2. Run the installer.
3. If Windows SmartScreen displays a blue protection banner:
   * Click **More info**.
   * Click **Run anyway**.

---

## 🔐 Verifying Download Integrity

To verify that your downloaded file matches the official cryptographic release:

### On macOS / Linux:
```bash
shasum -a 256 Retrova-1.0.1-mac-arm64.dmg
```

### On Windows (PowerShell):
```powershell
Get-FileHash Retrova-1.0.1-win-x64-Setup.exe -Algorithm SHA256
```

Compare the output against the values in [`SHA256SUMS`](https://github.com/jishnumahanta/retrova/releases/download/v1.0.1/SHA256SUMS):
```text
4deb8ba912ba5e8627443fcafde45cf951c76b9b700b2a0d1bc713431b03830a  Retrova-1.0.1-mac-arm64.dmg
a396350ea8d531470720ecde7f345e93efa9277d8a3a6cf334e87b5eba899bc1  Retrova-1.0.1-mac-x64.dmg
b437cbaaadd0c7d1575608e59786998579ce7acb8e8eec30e2a53530e26b9213  Retrova-1.0.1-win-x64-Setup.exe
aa826c0d81829f31ed0f4f71872749d186511fb3fba825a906419cc6023eeea1  Retrova-1.0.1-win-x64-portable.zip
```

---

## 🧩 Build from source

Retrova is plain Python with a no-build-step web UI.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r packaging/requirements/common.txt -r packaging/requirements/mac.txt   # or win.txt
python -m retrova                                            # opens the app window
python -m retrova --serve-only --port 8930 --token dev       # engine only: http://127.0.0.1:8930/?t=dev
```

Add `&mock=1` to the URL to try the interface without touching any drive.

Run the tests with `pip install pytest pillow && python -m pytest tests`. The release builds (`packaging/build_all.sh`) need a Debian/Ubuntu x86_64 host. See [SPEC.md](SPEC.md) for how the engine and UI fit together.

---

## 💚 Support Retrova

Retrova is free for everyone rescuing their own files, and it will stay that way. If it saved your photos, your work or your weekend, you can help keep it going:

| | How |
| :--- | :--- |
| 🇮🇳 **UPI (India)** | Pay `8472066376-4@ybl` from any UPI app, or scan the QR code below |
| 🌍 **Wise (anywhere)** | [wise.com/pay/business/jishnumahanta](https://wise.com/pay/business/jishnumahanta): card or bank transfer in your currency |
| 💚 **GitHub Sponsors** | [github.com/sponsors/jishnumahanta](https://github.com/sponsors/jishnumahanta) |

<p><img src="retrova/ui/img/upi-qr.svg" width="160" height="160" alt="UPI QR code for Jishnu Mahanta" /></p>

The app has the same options under **Support Retrova** in the sidebar. Paying unlocks nothing: every feature is already free.

Starring the repo and telling one friend also helps more than you'd think.

---

## 📜 License

Retrova is free software: Copyright © 2026 Jishnu Mahanta, licensed under the [GNU Affero General Public License v3.0](LICENSE). You can use, study, share and modify it under those terms. It comes with **no warranty**.

The name "Retrova" and the Retrova logo are trademarks of Jishnu Mahanta. Forks are welcome, but please give them a different name and icon. Bundled libraries keep their own licences: see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

---

## 👤 Author

Designed and built by **Jishnu Mahanta** ([jishnumahanta.in](https://jishnumahanta.in)).

* Website: [getretrova.pages.dev](https://getretrova.pages.dev)
* Releases: [github.com/jishnumahanta/retrova/releases](https://github.com/jishnumahanta/retrova/releases)
* Privacy & Security: See [SECURITY.md](SECURITY.md)
* Installation Details: See [INSTALL.md](INSTALL.md)
