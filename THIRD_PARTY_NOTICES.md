# Third-party notices

Retrova is Copyright (C) 2026 Jishnu Mahanta and is licensed under the
GNU Affero General Public License v3.0 (see `LICENSE`). The source code is at
<https://github.com/jishnumahanta/retrova>.

The release builds bundle the following components. Each keeps its own licence;
the full licence text of every Python package is in its `*.dist-info` folder inside
the app, and Python's own licence is in `python/lib/python3.12/LICENSE.txt` (macOS)
or `Lib/LICENSE.txt` / `LICENSE.txt` (Windows).

Test fixtures `tests/unlock_images/*` are copies of dissect.fve / dissect.apfs published test images (AGPL-3.0-or-later, Fox-IT).

## All platforms

| Component | Licence | Project |
| --- | --- | --- |
| CPython 3.12 (python-build-standalone) and the libraries it bundles (OpenSSL, SQLite, zlib, libffi, …) | PSF-2.0 and the licences of each library | https://github.com/astral-sh/python-build-standalone |
| dissect.apfs, dissect.ntfs, dissect.fat, dissect.extfs, dissect.xfs, dissect.btrfs, dissect.fve | AGPL-3.0-or-later | https://github.com/fox-it/dissect (Fox-IT, part of NCC Group) |
| dissect.cstruct, dissect.util | Apache-2.0 | https://github.com/fox-it/dissect |
| pywebview | BSD-3-Clause | https://github.com/r0x0r/pywebview |
| proxy_tools | MIT | https://github.com/jtushman/proxy_tools |
| bottle | MIT | https://github.com/bottlepy/bottle |
| typing_extensions | PSF-2.0 | https://github.com/python/typing_extensions |
| psutil | BSD-3-Clause | https://github.com/giampaolo/psutil |
| Send2Trash | BSD-3-Clause | https://github.com/arsenetar/send2trash |
| xxhash (Python binding and xxHash library) | BSD-2-Clause | https://github.com/ifduyue/python-xxhash |
| asn1crypto | MIT | https://github.com/wbond/asn1crypto |
| pycryptodome | BSD-2-Clause and Unlicense | https://github.com/Legrandin/pycryptodome |

## macOS only

| Component | Licence | Project |
| --- | --- | --- |
| PyObjC (core, Cocoa, Quartz, Security, WebKit, Vision, CoreML, UniformTypeIdentifiers) | MIT | https://github.com/ronaldoussoren/pyobjc |

## Windows only

| Component | Licence | Project |
| --- | --- | --- |
| pythonnet, clr_loader | MIT | https://github.com/pythonnet/pythonnet |
| cffi | MIT | https://github.com/python-cffi/cffi |
| pycparser | BSD-3-Clause | https://github.com/eliben/pycparser |
| PyWinRT (winrt-runtime and Windows.* projections) | MIT | https://github.com/pywinrt/pywinrt |
| Microsoft Edge WebView2 loader (shipped with pywebview) | Microsoft redistributable licence | https://developer.microsoft.com/microsoft-edge/webview2/ |

## Test data

`case_insensitive.bin`, `corrupt.bin`, `jhfs_converted.bin` and `snapshot.bin` in
`tests/apfs_images/` come from the dissect.apfs test suite (AGPL-3.0, Fox-IT).
`gptdisk.img` holds the same snapshot volume inside a GPT partition table.
