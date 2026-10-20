# Installation and Setup Guide

This guide covers installing Retrova, configuring permissions for raw disk access, updating, and uninstallation.

---

## System Requirements

### macOS
* **Operating System**: macOS 11.0 (Big Sur) or later.
* **Architecture**: Apple Silicon (M1, M2, M3, M4+) or Intel 64-bit.
* **Dependencies**: Completely self-contained. Python is **not** required.

### Windows
* **Operating System**: Windows 10 or Windows 11 (64-bit).
* **Architecture**: x64.
* **Dependencies**: Completely self-contained. Python is **not** required.

---

## macOS Setup

### 1. Installation
1. Download `Retrova-1.1.2-mac-arm64.dmg` (for Apple Silicon) or `Retrova-1.1.2-mac-x64.dmg` (for Intel).
2. Double-click the `.dmg` file to mount it.
3. Drag **Retrova.app** into your `/Applications` folder.

### 2. First Launch (Gatekeeper "Open Anyway")
Because Retrova is independently distributed and ad-hoc signed:
1. Double-click **Retrova** in Applications. A message stating *"Retrova cannot be opened because the developer cannot be verified"* may appear. Click **Done** or **Cancel**.
2. Open **System Settings $\rightarrow$ Privacy & Security**.
3. Scroll down to the **Security** section. You will see:
   > *"Retrova was blocked from use because it is not from an identified developer."*
4. Click **Open Anyway** and enter your Mac password or Touch ID.
5. On the final prompt, click **Open**. This only needs to be performed once.

### 3. Granting Full Disk Access (Recommended for Raw Recovery)
To recover files from unmounted, raw, or damaged physical disks, macOS requires **Full Disk Access**:
1. Open **System Settings $\rightarrow$ Privacy & Security $\rightarrow$ Full Disk Access**.
2. Click the `+` button, navigate to `/Applications`, and select **Retrova**.
3. Toggle the switch to **On**.

---

## Windows Setup

### Option A: Standard Setup (Recommended)
1. Download `Retrova-1.1.2-win-x64-Setup.exe`.
2. Double-click to run the installer. It installs per-user into `%LOCALAPPDATA%\Programs\Retrova` (no administrator rights required to install).
3. If SmartScreen shows *"Windows protected your PC"*:
   * Click **More info**.
   * Click **Run anyway**.
4. Launch Retrova from your Start Menu.

### Option B: Portable Version (No Install)
1. Download `Retrova-1.1.2-win-x64-portable.zip`.
2. Extract the zip file to any folder or USB drive.
3. Double-click `Retrova.exe` to run.

---

## Updating Retrova

Retrova has no background automatic updaters or tracking daemons.
To update:
* **macOS**: Download the new `.dmg` and drag the new `Retrova.app` into `/Applications`, replacing the old version.
* **Windows**: Run the new setup `.exe` or replace the portable folder.

Your application settings and OCR index are stored separately in your user app data directory and will be preserved:
* macOS: `~/Library/Application Support/Retrova`
* Windows: `%LOCALAPPDATA%\Retrova`

---

## Uninstallation

Retrova stores all its data cleanly in standard user paths:
* **macOS**: Drag `Retrova.app` from `/Applications` to the Trash. To remove local OCR indexes and settings, delete `~/Library/Application Support/Retrova`.
* **Windows**: Open **Settings $\rightarrow$ Apps $\rightarrow$ Installed Apps**, select **Retrova**, and click **Uninstall**.
