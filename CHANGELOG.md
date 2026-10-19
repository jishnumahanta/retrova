# Changelog

## Unreleased

- **Security: the administrator helper no longer gets the session token on its command line.** The token used to be visible to other processes on the machine while the helper ran. It's now passed through a private one-time file (owner-only, deleted as soon as the helper starts).
- **Fixed: removing files on a drive without a Recycle Bin (USB stick, memory card, network share) on Windows.** Windows can delete from those permanently. Retrova now refuses and says so, instead of moving to a Recycle Bin that isn't there.
- **Corrected the descriptions of what Retrova does:** it doesn't browse APFS snapshots, deep scan recognises 45 file types, and PDFs are searched by their text layer (scanned PDF pages aren't OCR'd). APFS compressed files (zlib, LZVN, LZFSE) are read.

## 1.0.2 (19 October 2026)

Windows fixes. If you're on Windows, please update.

- **Fixed: on Windows, Retrova opened in a browser window and buttons like "Choose folder" didn't work.** A missing WebView2 helper made the real app window fail to start, so Retrova fell back to an Edge window without native dialogs. It now opens in its own window with the normal Windows folder picker.
- **Fixed: on Windows, Space, Duplicates, Space saver and Cleanup only looked at the top level of a folder** and skipped everything in subfolders. They now scan the whole folder.
- **Fixed:** the drive list can no longer hang silently on Windows, and helper timeouts are now written to the log.
- **New: Check for updates.** A button in the sidebar and in the Support panel asks GitHub for the latest version and, if there's a newer one, offers the right download for your computer. Retrova only goes online when you click it; nothing about you is sent.

## 1.0.1 (18 October 2026)

**Retrova is now open source** under the GNU AGPL-3.0. The full source, build scripts and tests are in this repository.

- **New: Support Retrova.** A sidebar entry with ways to chip in (UPI, Wise, GitHub Sponsors) and the licence notice. After a successful recovery or a clean verify there's a quiet "Support Retrova" button. Paying unlocks nothing; every feature stays free. Links open in your own browser, and the app still makes no network connections.
- **Fixed:** the drive-health summary on the home screen said "All 4 drives look healthy" when some drives (such as disk images) can't report health. It now only counts drives that actually reported.
- The app, installer and Windows executable now carry the copyright and AGPL-3.0 notice, and the licence texts ship inside the app.
- Tests no longer trigger the administrator password prompt when run on a Mac or Windows PC.

## 1.0.0 (17 October 2026)

First release: Recover, Verify, Space, Duplicates, Cleanup, Find Text and System for macOS 11+ and Windows 10/11.
