Retrova {version} for Mac ({arch_label})
Recover. Verify. Reclaim.

INSTALL
  Drag Retrova onto the Applications folder in this window, then eject this disk.

FIRST LAUNCH (Retrova is not notarized by Apple yet)
  macOS will say it can't check Retrova for malicious software. To open it once:
  - Open your Applications folder, Control-click (or right-click) Retrova and choose Open,
    then click Open in the dialog.
  - On macOS 15 Sequoia and later: double-click Retrova, click Done, then open
    System Settings > Privacy & Security, scroll down to "Retrova was blocked..."
    and click Open Anyway (enter your password if asked).
  After that, Retrova opens normally.
  (Terminal alternative: xattr -dr com.apple.quarantine /Applications/Retrova.app)

RECOVERING FROM A DISK
  Reading a damaged or unmountable drive needs administrator rights. Retrova asks for
  your password only when it needs it.
  To recover files that belong to other apps (Mail, Photos, Messages...) or from
  protected folders, give Retrova Full Disk Access:
    System Settings > Privacy & Security > Full Disk Access > + > choose Retrova
  then quit and reopen Retrova.

SAFETY
  Retrova opens source drives read-only and never deletes anything permanently:
  anything it removes goes to the Trash, so you can put it back.
  Everything happens on your Mac. Retrova makes no internet connections.

WHICH DOWNLOAD?
  This copy is for {arch_hint}.
  Apple menu > About This Mac shows "Chip: Apple M..." (Apple silicon) or
  "Processor: Intel...".

PROBLEMS?
  A log is kept at ~/Library/Logs/Retrova/retrova.log

LICENCE
  Retrova is free software: Copyright (C) 2026 Jishnu Mahanta, released under the
  GNU Affero General Public License v3.0, with NO WARRANTY. The full licence and the
  notices for bundled libraries are inside the app (Retrova.app/Contents/Resources).
  Source code: https://github.com/jishnumahanta/retrova
