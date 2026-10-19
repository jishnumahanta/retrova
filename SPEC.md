# Retrova — build spec (shared contract for all contributors)

Retrova is a local-only desktop app for macOS (Apple Silicon + Intel) and Windows 10/11 that
grew out of a one-off rescue script written to save the files on a dead portable SSD. It recovers files from damaged or unmountable
drives, verifies and repairs copies, and helps people take care of their disks: space
analyser, duplicate finder, "find text in images" (OCR), cleanup, sparse-file reclaim,
memory/system check. Tagline: **Recover. Verify. Reclaim.**

## Non-negotiable safety rules
1. Source drives being recovered are opened **read-only**. No code path writes to them.
2. Nothing is ever permanently deleted by the app. Removal = move to Trash / Recycle Bin
   (`retrova.util.to_trash`). Every destructive action is user-confirmed in the UI and listed first.
3. Never overwrite a different existing file: write `name.part` then rename; collisions -> `name.retrova-N`.
4. System locations (`/System`, `/usr`, `/bin`, `/sbin`, `/private/var/db`, `C:\Windows`,
   `C:\Program Files*`, `/Library` except caches listed explicitly) are never offered for removal.
5. Server listens on 127.0.0.1 only; every API call needs the session token. No network calls
   to the internet anywhere in the app (no telemetry, no automatic update checks). The single
   exception is GET /api/update/check, run only when the user clicks "Check for updates".
6. Pure-Python where possible. Allowed third-party deps: dissect.* (apfs, ntfs, fat, extfs, xfs,
   btrfs, cstruct, util), send2trash, psutil, xxhash (optional, fall back to hashlib.blake2b),
   pywebview (+ pyobjc on mac, pythonnet on win), pyobjc-framework-Vision/Quartz (mac OCR),
   winrt-Windows.Media.Ocr (win OCR). Every optional dep must be imported lazily and the feature
   must degrade gracefully ("OCR isn't available on this system") if it is missing.
7. Python 3.10+ syntax is fine (bundled runtime is 3.12). Must also run on Linux for tests.

## Layout (who owns what)
```
retrova/                     project root
  retrova/__init__.py        version                                  [lead]
  retrova/__main__.py        entry: server + native window            [lead]
  retrova/server.py          HTTP server, @route, Reply, ApiError     [lead, done]
  retrova/jobs.py            Job base class + JOBS manager             [lead, done]
  retrova/util.py            helpers: human, long_path, to_trash, reveal, is_admin, give_to_user [lead, done]
  retrova/recovery/          ALL disk-recovery code                   [agent: core]
  retrova/tools/             analyser, dupes, sparse, cleaner, system, ocr, transfer, fs browse [agent: tools]
  retrova/ui/                the single-page app (index.html, css/, js/) [agent: ui]
  packaging/                build scripts -> dist/*.dmg, dist/*.exe   [agent: packaging]
  website/                  marketing site (index.html + assets)     [agent: website]
  tests/                    pytest-style tests per module
```
Test APFS images: `tests/apfs_images/`.

## Backend conventions
* Endpoints: `from retrova.server import route, Reply, ApiError` and decorate plain functions
  `fn(q: dict, body: dict)`. `q` holds single-valued query params plus `q["_range"]` (HTTP Range header).
  Return dict/list (JSON) or `Reply(...)`. Raise `ApiError(msg, code)` for user-facing errors —
  messages are shown verbatim in the UI, so write them in plain friendly English.
* Each package exposes `register()` in its `__init__.py` that imports its endpoint modules
  (so `retrova/__main__.py` just calls `recovery.register(); tools.register()`).
* Long work = subclass `retrova.jobs.Job` (`kind`, `title`, `run()`, `checkpoint()` often,
  set `phase`, `progress` 0..1, `stats`), start with `JOBS.start(job)`, return `job.status()`.
  Disk-reading recovery jobs set `exclusive = True`.
  Generic endpoints already exist: `GET /api/jobs?kind=`, `GET /api/job?id=`,
  `GET /api/job/result?id=&...` (calls `job.result(q)`), `POST /api/job/control {id, action: pause|resume|stop}`.
* Job status JSON (all jobs): `{id, kind, title, state, phase, message, error, progress, stats, elapsed, params, ...extra}`
  where state ∈ queued|running|paused|stopping|stopped|done|error. Use `stats` for the numbers the UI shows,
  with these conventional keys when they apply: `files_done, files_total, bytes_done, bytes_total,
  speed (bytes/s), eta (seconds), found, failed, skipped`.

## API contract
All responses JSON unless noted. (job) = returns a job status object.

### App / platform  [core]
- `GET /api/info` -> `{version, platform: mac|windows|linux, admin: bool, can_elevate: bool, user, home, app_data}`
- `POST /api/elevate` -> `{url}`: relaunch the backend with administrator rights (macOS: osascript
  "with administrator privileges"; Windows: ShellExecute runas), same token, new port; the UI navigates to `url`.
  The elevated server exits when the parent (non-elevated) process exits. Ownership of files it writes
  goes back to the real user (pass SUDO_UID/SUDO_GID env on mac).
- `GET /api/drives` -> `{disks: [{id, name, model, device, size, internal: bool, removable: bool, protocol,
   health: {state: ok|warn|bad|unknown, detail}, partitions: [{id, device, offset, size, fs, label, mount, free}]}],
   volumes: [{mount, label, fs, size, free, device}]}`  (mac: diskutil -plist, win: PowerShell Get-Disk/Get-Partition/Get-Volume
   or WMI, linux: lsblk -J). Needs no admin.

### Recovery  [core]  (generalised rescue script)
- `GET /api/recover/sources` -> `{sources: [{source, label, size, fs, detail, recommended: bool, needs_admin: bool}]}` —
  candidates: every partition/whole disk with a recognised filesystem; flag ones the OS can't mount or that look damaged.
- `POST /api/recover/open {source, offset?: int, volume?: str}` -> `{sid, fs, label, volumes: [str], size, source, notes: [str]}`
  source = raw device (`/dev/rdiskN`, `/dev/diskNsM`, `\\.\PhysicalDriveN`, `/dev/sdX`) or a disk-image file path.
  Auto-detects GPT/MBR/Apple Partition Map partitions and filesystems: APFS (via legacy engine incl. all its patches), NTFS, FAT12/16/32,
  exFAT, ext2/3/4, XFS, Btrfs, HFS+/HFSX (own reader, `recovery/hfsplus.py`). Read-only always.
- `GET /api/recover/ls?sid=&path=/` -> `{path, entries: [{name, path, kind: dir|file|link|other, size, mtime (ms), error?}]}`
  Add `&deleted=1` (NTFS, FAT, exFAT; the open reply has `can_list_deleted`) to also list deleted files: each such row has
  `deleted: true`, files carry `recover: good|overwritten|incomplete` and `recover_note`, its `path` is under `/Deleted files/...`
  (the same path works for preview, size, copy and verify), the root gets a `Deleted files` folder, and the reply may carry
  `deleted_notes: [str]`. `/Deleted files/...` can also be browsed directly without the flag.
- `GET /api/recover/preview?sid=&path=` -> raw file bytes with Range support (images, video, pdf, text).
- `POST /api/recover/size {sid, paths: [], patterns: []}` -> `{sizes: {path: {state: queued|running|done|error, bytes, files, errors}}}`
  non-blocking; queues unknown paths and returns what is known; the UI calls again to poll.
- `POST /api/recover/regen {sid, paths}` -> (job kind "regen")  result: `{found: [{path, what, regen, confidence: safe|review}]}`
- `POST /api/recover/copy {sid, paths, excluded, patterns, out, reserve_gb, workers, skipped_info}` -> (job "copy")
  status extra like legacy CopyJob (copied, skipped, failed, renamed, current[], errors[], free, out_root).
- `POST /api/recover/image {sid | source, out, reserve_gb, retries?: 1, reverse?: false}` -> (job "image") full sector image, sparse, resumable.
  Passes: copy (skip ahead on errors) -> trim -> scrape -> `retries` retry passes (`reverse`: scrape/retry from the end).
  Progress is a ddrescue-compatible mapfile `<out>.map`; also `<out>.unreadable.txt` (generated from it) and, only for a
  complete image with nothing unreadable, `<out>.sha256`. Status extra: `phase_id` (copy|trim|scrape|retry|done), `pass`,
  `rescued`, `bad_bytes`, `error_areas`, `map_path`, `bad_log`, `sha256`, `stop_reason`; stats add `speed`, `eta`.
  result(q: offset, limit) -> `{areas: [{offset, length, status}], total}` (status `*` `/` `-`).
- `POST /api/recover/verify {sid, from_job?, paths, excluded, patterns, out, mode: quick|sample|full, workers, resume, show_hidden}`
  -> (job "verify")  result(q: status, q, limit) -> `{items, more, total}`; `POST /api/recover/repair {job, workers}` -> (job "verify").
  `GET /api/recover/verify_csv?id=&status=&q=&t=` -> CSV download. (Port legacy VerifyJob.)
- `POST /api/recover/carve {source, out, types: [jpg,png,gif,heic,mp4,mov,pdf,zip,docx,xlsx,pptx,mp3,wav,sqlite,...], start?, end?}`
  -> (job "carve") signature-based deep scan of a raw device/image for deleted or lost files (PhotoRec-style),
  writes found files to `out/<type>/retrova-<offset>.<ext>`; result -> `{counts: {type: n}, items: [{path, type, size, offset}]}`.

### Tools  [tools]
- Local file browsing for in-app pickers: `GET /api/fs/places` -> `{places: [{name, path, icon}]}` (home, Desktop, Documents,
  Downloads, Pictures, each mounted volume); `GET /api/fs/ls?path=` -> `{path, parent, entries: [{name, path, kind, size, mtime}]}`;
  `GET /api/file?path=` -> local file bytes with Range (for previews); `GET /api/thumb?path=&size=256` -> JPEG/PNG thumbnail
  (mac: `sips`/`qlmanage`; else serve original if browser-renderable, else 404); `POST /api/reveal {path}`.
- Space analyser: `POST /api/analyse {path}` -> (job "analyse").
  result(q.view): `summary` -> `{total, files, dirs, biggest_dirs, types: [{type, bytes, files}], findings: [...]}`;
  `tree&node=<path>` -> `{node, size, children: [{name, path, size, files, kind}]}` (sorted desc);
  `largest` -> top 200 files; `variants` -> copy-suffix twins (`name copy.ext`, `name (1).ext`, `.t5r-N`, `.restore-N`,
  `.retrova-N`, `.INCOMPLETE`, `.part`) with their originals; `dupe_dirs` -> folders that are identical copies of each other
  (by name+size signature, confirmed by sampled hashes); `sparse` -> big files mostly zeros (VM disks).
  findings = plain-English cards: `{id, title, detail, bytes, action: dupes|sparse|clean|reveal|none, paths}`.
- Duplicates: `POST /api/dupes {paths, min_size, include_hidden}` -> (job "dupes"): size -> 64KB head/tail hash -> full hash
  (xxhash or blake2b). result -> `{groups: [{id, size, hash, files: [{path, mtime, keep: bool}]}], reclaimable, scanned}`
  (keep = smart suggestion: prefer shortest path / oldest / not in Downloads/Trash/cache).
  `POST /api/dupes/apply {action: trash|clone, remove: [paths], keep_map: {removed_path: kept_path}}` -> (job "dupes_apply").
  clone = macOS APFS clonefile (keeps both names, shares space) — verify bytes equal right before acting.
- Sparse reclaim: `POST /api/sparse/scan {paths}` -> (job "sparse_scan") result `{files: [{path, size, allocated, zero_bytes, reclaimable}]}`;
  `POST /api/sparse/apply {files}` -> (job "sparse_apply"): rewrite as sparse copy (seek over zero blocks), verify hash,
  atomically replace, keep mtime/mode. Skip files that are open/locked. Windows: mark sparse via FSCTL_SET_SPARSE.
- Cleanup: `POST /api/clean/scan {}` -> (job "clean_scan") result `{categories: [{id, name, detail, risk: safe|review,
  size, items: [{path, size, mtime}]}]}` — user caches, logs, temp, Trash size (report only), Xcode DerivedData/
  simulators caches, browser caches, package-manager caches (npm/pip/brew/gradle), old installers in Downloads
  (.dmg/.pkg/.exe/.msi older than 30 days), regenerable dev folders (node_modules etc. — reuse legacy regen rules), crash reports.
  `POST /api/clean/apply {paths}` -> (job "clean_apply") moves to Trash; refuses anything outside the scanned item set.
- System: `GET /api/system` -> `{os, model, cpu: {percent, cores}, memory: {total, used, available, percent, pressure, swap_used},
  top: [{pid, name, rss, cpu}], disks: [{mount, total, used, free}], uptime}`;
  `POST /api/system/free_memory` -> `{ok, before, after, method, message, needs_admin?}` (mac: `purge` (admin);
  win: EmptyWorkingSet on accessible processes); `POST /api/system/quit {pid}` -> graceful terminate (not system processes).
- OCR (find text in images): `GET /api/ocr/status` -> `{available, engine, indexed, langs}`;
  `POST /api/ocr/index {paths}` -> (job "ocr") indexes images (jpg/png/heic/tiff/webp/gif/bmp) and PDFs (mac PDFKit text +
  OCR of scanned pages; elsewhere text layer only) into sqlite FTS5 in the app-data dir; incremental (skip unchanged by size+mtime).
  `GET /api/ocr/search?q=&limit=50` -> `{results: [{path, snippet, kind}]}`. Engines: mac Vision (pyobjc), Windows.Media.Ocr (winrt),
  fallback `tesseract` CLI if on PATH.
- Transfer (copy recovered files back, from the original restore script): `POST /api/transfer {src, dst, items, workers, fix_perms, restore_names, check}` -> (job "transfer").

## UI  [ui]
Single-page app in `retrova/ui/` (no build step, no CDN, everything local; ES modules under `js/`, styles in `css/`).
Loaded at `/?t=<token>`; send the token as `X-Token` header on every fetch. Shown inside a native window (pywebview),
so it must feel like a native app, not a website: no page scrolling of the whole shell, sidebar + content, keyboard
friendly, works from 900x600 up, light & dark (prefers-color-scheme), reduced motion respected.
* Home = problem-first: "What do you need?" cards → guided flows: A drive won't open / I deleted something /
  I'm running out of space / Find a photo or document by its text / My Mac (PC) feels slow / Check a copy is perfect.
  Plus a drive strip (all drives with health + free space).
* Sections (sidebar): Home, Recover, Verify, Space, Duplicates, Cleanup, Find Text, System, Jobs (activity). 
* Every destructive step shows exactly what will happen, sizes, and "moved to Trash — you can put it back".
* Jobs run in background; a global activity pill shows running jobs; each screen restores its job on revisit.

## Packaging  [packaging]
Outputs in `dist/`: `Retrova-<ver>-mac-arm64.dmg`, `Retrova-<ver>-mac-x64.dmg`, `Retrova-<ver>-win-x64-Setup.exe`,
`Retrova-<ver>-win-x64-portable.zip`. Self-contained: bundled CPython 3.12 (python-build-standalone from GitHub releases)
with all deps installed for the target platform via `pip install --target --platform ... --only-binary=:all:`.
No Python needed on the user's machine. App icon generated from one SVG/PNG master. Build script: `packaging/build_all.sh`.

## Website  [website]
`website/index.html` (+ `website/assets/`), a distinctive landing page for Retrova with download buttons linking to the
dist file names above, features, safety promises, how-it-works, FAQ. Fully responsive, light/dark, fast, no trackers.
