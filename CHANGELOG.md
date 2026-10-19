# Changelog

## 1.1.0 (20 October 2026)

A big free update: deleted files come back with their names, encrypted and older Mac drives open, failing drives are imaged more carefully, and deep scan knows 72 file types.

- **New: careful imaging of failing drives.** Creating a disk image now works the way GNU ddrescue does. A first pass saves all the healthy data and skips ahead when the drive reports errors (so a weak drive isn't pushed into the bad spots first), then Retrova trims the edges of the damaged areas, reads what's left sector by sector and tries the unreadable sectors again (one extra try by default; you can change it). The window shows which step it is on, how much is saved, how much is unreadable and the speed. Stop keeps everything, including after closing the app.
- **New: the image comes with a `.map` file** in GNU ddrescue's format, so you can carry on with `ddrescue` if you want to keep trying. `.unreadable.txt` is still written. A complete image with nothing unreadable also gets a `.sha256` fingerprint file.
- **New: Retrova can read Mac OS Extended (HFS+ and HFSX) drives.** That covers older Macs, many Time Machine drives and older external drives. Folders, files, symlinks, hard links, resource forks and compressed files are read, names with accents and emoji come out right, and Time Machine backup folders can be browsed like any other folder. Apple Partition Map disks (old PowerPC Macs) are recognised too. Not covered: the drive's journal isn't replayed, so changes made just before a drive was pulled out without ejecting may be missing (Retrova says so when it sees that).
- **New: recover deleted files with their original names and folders (NTFS, FAT12/16/32, exFAT).** In Browse files, turn on "Show deleted files": deleted files appear in the folders they were in, and a "Deleted files" folder at the top of the drive holds them all (files whose folder can't be traced are under "Lost folders"). Each shows how likely its data is intact: Good, Possibly overwritten (another file now uses some of its space) or May be incomplete (FAT cannot say whether a file was split into pieces; Retrova reads it as one unbroken stretch). You can preview, copy and Verify them like any other file. On FAT, the first letter of a name that has no long name is lost and shows as "_". Nothing is ever written to the drive. Deep scan is still there for files whose records are gone.
- **New: open encrypted drives with your key.** BitLocker (password or recovery key), LUKS 1/2 (passphrase) and FileVault / encrypted APFS volumes (password or recovery key). Locked drives are marked "Locked"; Retrova asks for the key, shows a plain error if it's wrong so you can retry, then browses the files inside as usual. Copy, Verify, Create image and Deep scan work on the unlocked contents. Keys stay in memory only (never written, logged or sent back to the screen) and are forgotten when you leave the drive. Drives locked to a Mac's own security chip can't be unlocked elsewhere, and Retrova says so.
  Limits: Deep scan and Create image on an unlocked APFS (FileVault) volume still read the drive's encrypted bytes (only BitLocker and LUKS are decrypted for those); decryption runs at roughly 100 MB/s, so a 1 TB drive takes a few hours; BitLocker startup-key (.bek) files and TPM-only drives aren't supported.
- **Deep scan recognises 72 file types (was 45).** New: camera RAW from Nikon Coolpix (NRW), Olympus (ORF), Fujifilm (RAF) and Pentax (PEF); JPEG XL, ICO, SVG and Photoshop large documents (PSB); 3G2 and Windows Media video (WMV); AAC, AIFF, Windows Media audio (WMA) and MIDI; Outlook PST/OST mail files, RTF, multipart EML emails and Apple Pages, Numbers and Keynote files; TrueType, OpenType and WOFF/WOFF2 fonts, Google Earth KMZ and SQLite write-ahead logs. Each one is found by walking its real structure to the exact end of the file, and rejected if it is cut short or damaged.
- **Fixed: AVIF photos were saved as .heic** when they also listed the generic HEIF brand, which most AVIF encoders do.
- **Security: the administrator helper no longer gets the session token on its command line.** The token used to be visible to other processes on the machine while the helper ran. It's now passed through a private one-time file (owner-only, deleted as soon as the helper starts).
- **Fixed: removing files on a drive without a Recycle Bin (USB stick, memory card, network share) on Windows.** Windows can delete from those permanently. Retrova now refuses and says so, instead of moving to a Recycle Bin that isn't there.
- **Corrected the README:** Retrova doesn't browse APFS snapshots, and PDFs are searched by their text layer (scanned PDF pages aren't OCR'd). APFS compressed files (zlib, LZVN, LZFSE) are read.
- **Fixed (Windows): recovered links to folders** are now created as folder links, so they open in File Explorer.
- Known limits: EML is only recovered for multipart messages (the closing boundary is the only reliable end marker), RTF files that contain raw 8-bit bytes instead of escapes are skipped, and Panasonic RW2, MBOX and Affinity files are not recovered because their end cannot be found reliably.

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
