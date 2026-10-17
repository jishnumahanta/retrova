# Security and Privacy Policy

Retrova was engineered from the ground up for strict privacy, data integrity, and safe physical drive recovery.

---

## Security Guarantees

### 1. 100% Offline / Zero Telemetry
* **No Network Calls**: Retrova contains zero analytics, tracking scripts, crash-reporting daemons, or telemetry libraries.
* **Local HTTP Loopback**: The backend API listens exclusively on `127.0.0.1` (localhost).
* **Cryptographic Token Verification**: Every API interaction requires an ephemeral session token generated when the app starts. Cross-origin requests and non-local Host headers are rejected with HTTP 401/403.

### 2. Read-Only Source Drive Access
* Physical devices and disk images being recovered are opened strictly with read-only flags (`O_RDONLY` / `GENERIC_READ`).
* The recovery subsystem contains no code paths that write to, format, repartition, or modify the source disk.

### 3. Non-Destructive File Management
* Deletion operations never use permanent removal commands (`rm` / `unlink`).
* Removals are routed through your operating system's native Trash (`send2trash` / macOS Finder Trash / Windows Recycle Bin), allowing any removed file to be restored.
* Files written to output directories are staged as `.part` files and renamed upon verification to prevent corrupt or partial overwrites.

### 4. Local OCR Processing
* Text recognition in photos, screenshots, and PDFs is computed entirely on-device using native OS frameworks (Apple Vision on macOS, Windows.Media.Ocr on Windows).
* The search index is stored in a local SQLite FTS5 database on your physical hard drive and is never uploaded anywhere.

---

## Reporting a Security Issue

If you discover a security vulnerability or unexpected write behavior, please report it privately:

* **Email**: Contact Jishnu Mahanta directly via [jishnumahanta.in](https://jishnumahanta.in) or by opening a private security advisory on GitHub.
* Please provide:
  * Operating system and Retrova version.
  * Steps to reproduce the issue.
  * Relevant logs (from `~/Library/Application Support/Retrova` or `%LOCALAPPDATA%\Retrova`).

We take security and data preservation seriously and will investigate and patch verified vulnerabilities promptly.
