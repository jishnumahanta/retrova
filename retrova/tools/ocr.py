"""Find text in images and PDFs: on-device OCR into a local full-text index.

Engines (first available wins): macOS Vision (pyobjc), Windows.Media.Ocr (winrt),
the `tesseract` command line tool. PDF text layers: macOS PDFKit, else `pdftotext`.
Index: sqlite FTS5 in the app-data folder (LIKE search when FTS5 is missing).
Nothing leaves the computer.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import threading

from ..jobs import JOBS
from ..server import route, ApiError
from ..util import IS_MAC, IS_WIN
from .common import ToolJob, app_data, clean_paths, plural

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp", ".gif", ".bmp"}
PDF_EXT = {".pdf"}
SKIP_DIRS = {"node_modules", ".git", "Library", "AppData", "__pycache__", ".Trash", "$RECYCLE.BIN", ".venv", "venv"}
MAX_IMAGE = 60 * 1024 * 1024


# ---------------------------------------------------------------- engines
def _mac_vision_ocr(path: str) -> str:
    import Quartz  # type: ignore
    import Vision  # type: ignore
    from Foundation import NSURL  # type: ignore
    url = NSURL.fileURLWithPath_(path)
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        return ""
    img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if img is None:
        return ""
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(True)
    try:
        req.setAutomaticallyDetectsLanguage_(True)
    except Exception:
        pass
    ok = handler.performRequests_error_([req], None)
    if isinstance(ok, tuple):
        ok = ok[0]
    lines = []
    for obs in (req.results() or []):
        cands = obs.topCandidates_(1)
        if cands:
            lines.append(str(cands[0].string()))
    return "\n".join(lines)


def _mac_pdf_text(path: str) -> str:
    import Quartz  # type: ignore
    from Foundation import NSURL  # type: ignore
    doc = Quartz.PDFDocument.alloc().initWithURL_(NSURL.fileURLWithPath_(path))
    if doc is None:
        return ""
    return str(doc.string() or "")


def _win_ocr(path: str) -> str:
    import asyncio

    async def go():
        from winrt.windows.graphics.imaging import BitmapDecoder  # type: ignore
        from winrt.windows.media.ocr import OcrEngine  # type: ignore
        from winrt.windows.storage import FileAccessMode, StorageFile  # type: ignore
        f = await StorageFile.get_file_from_path_async(os.path.abspath(path))
        stream = await f.open_async(FileAccessMode.READ)
        dec = await BitmapDecoder.create_async(stream)
        bmp = await dec.get_software_bitmap_async()
        eng = OcrEngine.try_create_from_user_profile_languages()
        if eng is None:
            return ""
        res = await eng.recognize_async(bmp)
        return res.text or ""
    return asyncio.run(go())


def _tesseract(path: str) -> str:
    r = subprocess.run([shutil.which("tesseract"), path, "stdout"], capture_output=True, text=True, timeout=120,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout if r.returncode == 0 else ""


def _pdftotext(path: str) -> str:
    r = subprocess.run([shutil.which("pdftotext"), "-q", path, "-"], capture_output=True, text=True, timeout=120,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout if r.returncode == 0 else ""


_ENGINE = {}


def engines() -> dict:
    """{'image': (name, fn) | None, 'pdf': (name, fn) | None} - detected once."""
    if _ENGINE:
        return _ENGINE
    img = pdf = None
    if IS_MAC:
        try:
            import Vision  # noqa: F401  # type: ignore
            import Quartz  # noqa: F401  # type: ignore
            img = ("macOS Vision", _mac_vision_ocr)
            pdf = ("macOS PDFKit", _mac_pdf_text)
        except Exception:
            pass
    if IS_WIN and img is None:
        try:
            from winrt.windows.media.ocr import OcrEngine  # noqa: F401  # type: ignore
            img = ("Windows OCR", _win_ocr)
        except Exception:
            pass
    if img is None and shutil.which("tesseract"):
        img = ("Tesseract", _tesseract)
    if pdf is None and shutil.which("pdftotext"):
        pdf = ("pdftotext", _pdftotext)
    _ENGINE.update({"image": img, "pdf": pdf})
    return _ENGINE


# ---------------------------------------------------------------- index
_DB_LOCK = threading.Lock()


def db_path() -> str:
    return os.path.join(app_data(), "ocr.sqlite")


def connect():
    con = sqlite3.connect(db_path(), timeout=30, check_same_thread=False)
    con.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, size INTEGER, mtime REAL, kind TEXT, chars INTEGER)")
    try:
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(path UNINDEXED, text, tokenize='unicode61 remove_diacritics 2')")
        fts = True
    except sqlite3.OperationalError:
        con.execute("CREATE TABLE IF NOT EXISTS docs(path TEXT PRIMARY KEY, text TEXT)")
        fts = False
    con.execute("CREATE TABLE IF NOT EXISTS roots(path TEXT PRIMARY KEY, files INTEGER, updated REAL)")
    return con, fts


def _store(con, path, size, mtime, kind, text):
    con.execute("DELETE FROM docs WHERE path=?", (path,))
    if text.strip():
        con.execute("INSERT INTO docs(path, text) VALUES(?, ?)", (path, text))
    con.execute("INSERT OR REPLACE INTO files(path, size, mtime, kind, chars) VALUES(?,?,?,?,?)",
                (path, size, mtime, kind, len(text)))


class OcrJob(ToolJob):
    kind = "ocr"
    title = "Reading text in images"

    def run(self):
        eng = engines()
        if not eng["image"] and not eng["pdf"]:
            raise RuntimeError("Text recognition isn't available on this system.")
        roots = self.params["paths"]
        self.phase = "Finding images and PDFs…"
        todo = []
        for root in roots:
            if os.path.isfile(root):
                todo.append(root)
                continue
            for dp, dns, fns in os.walk(root):
                self.checkpoint()
                dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
                for f in fns:
                    ext = os.path.splitext(f)[1].lower()
                    if (ext in IMAGE_EXT and eng["image"]) or (ext in PDF_EXT and eng["pdf"]):
                        todo.append(os.path.join(dp, f))
                self.stats["found"] = len(todo)
        with _DB_LOCK:
            con, _ = connect()
            known = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path, size, mtime FROM files")}
            con.close()
        self.stats.update({"files_total": len(todo), "files_done": 0, "indexed": 0, "skipped": 0,
                           "with_text": 0})
        self.phase = f"Reading text with {(eng['image'] or eng['pdf'])[0]}…"
        con, _ = connect()
        try:
            for i, p in enumerate(todo):
                self.checkpoint()
                try:
                    st = os.stat(p)
                except OSError as ex:
                    self.add_error(p, ex)
                    continue
                if known.get(p) == (st.st_size, st.st_mtime):
                    self.stats["skipped"] += 1
                else:
                    ext = os.path.splitext(p)[1].lower()
                    kind = "pdf" if ext in PDF_EXT else "image"
                    text = ""
                    try:
                        if kind == "image" and st.st_size <= MAX_IMAGE:
                            text = eng["image"][1](p)
                        elif kind == "pdf":
                            text = eng["pdf"][1](p)
                    except Exception as ex:
                        self.add_error(p, ex)
                    with _DB_LOCK:
                        _store(con, p, st.st_size, st.st_mtime, kind, text or "")
                    self.stats["indexed"] += 1
                    if (text or "").strip():
                        self.stats["with_text"] += 1
                    if self.stats["indexed"] % 20 == 0:
                        con.commit()
                self.stats["files_done"] = i + 1
                self.progress = (i + 1) / max(1, len(todo))
            with _DB_LOCK:
                for r in roots:
                    con.execute("INSERT OR REPLACE INTO roots(path, files, updated) VALUES(?,?,strftime('%s','now'))",
                                (r, len(todo)))
                con.commit()
        finally:
            con.close()
        self.message = (f"Indexed {plural(self.stats['indexed'], 'file')} "
                        f"({self.stats['with_text']} with text); {self.stats['skipped']} unchanged.")

    def result(self, q):
        return {"stats": self.stats}


# ---------------------------------------------------------------- endpoints
@route("GET", "/api/ocr/status")
def status(q, body):
    eng = engines()
    try:
        with _DB_LOCK:
            con, fts = connect()
            n = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            withtext = con.execute("SELECT COUNT(*) FROM files WHERE chars>0").fetchone()[0]
            roots = [{"path": r[0], "files": r[1], "updated": r[2]} for r in
                     con.execute("SELECT path, files, updated FROM roots ORDER BY updated DESC")]
            con.close()
    except sqlite3.Error:
        n = withtext = 0
        roots, fts = [], False
    return {"available": bool(eng["image"] or eng["pdf"]),
            "engine": (eng["image"] or ("", None))[0], "pdf_engine": (eng["pdf"] or ("", None))[0],
            "indexed": n, "with_text": withtext, "roots": roots, "fts": fts,
            "message": None if (eng["image"] or eng["pdf"]) else
            "Text recognition isn't available on this system."}


@route("POST", "/api/ocr/index")
def index(q, body):
    paths = clean_paths(body.get("paths"), "folder")
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        raise ApiError("That folder isn't there - is the drive connected?", 404)
    eng = engines()
    if not eng["image"] and not eng["pdf"]:
        raise ApiError("Text recognition isn't available on this system.", 409)
    if any(j.kind == "ocr" and j.live() for j in JOBS.jobs.values()):
        raise ApiError("Retrova is already reading text from images - wait for it to finish.", 409)
    job = OcrJob({"paths": paths})
    JOBS.start(job)
    return job.status()


def _fts_query(q: str) -> str:
    words = re.findall(r"\w+", q, flags=re.UNICODE)
    return " ".join(f'"{w}"*' for w in words)


@route("GET", "/api/ocr/search")
def search(q, body):
    text = (q.get("q") or "").strip()
    limit = max(1, min(int(q.get("limit") or 50), 500))
    if not text:
        return {"results": [], "total": 0}
    with _DB_LOCK:
        con, fts = connect()
        try:
            rows = []
            if fts and _fts_query(text):
                try:
                    rows = con.execute(
                        "SELECT d.path, snippet(docs, 1, '[[', ']]', ' … ', 14), f.kind FROM docs d "
                        "LEFT JOIN files f ON f.path = d.path WHERE docs MATCH ? ORDER BY rank LIMIT ?",
                        (_fts_query(text), limit)).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            if not rows:
                like = f"%{text}%"
                raw = con.execute("SELECT d.path, d.text, f.kind FROM docs d LEFT JOIN files f ON f.path=d.path "
                                  "WHERE d.text LIKE ? LIMIT ?", (like, limit)).fetchall()
                rows = []
                for p, t, k in raw:
                    i = t.lower().find(text.lower())
                    a, b = max(0, i - 60), i + len(text) + 60
                    rows.append((p, ("… " if a else "") + t[a:i] + "[[" + t[i:i + len(text)] + "]]" + t[i + len(text):b]
                                 + (" …" if b < len(t) else ""), k))
        finally:
            con.close()
    results = [{"path": p, "name": os.path.basename(p), "snippet": " ".join((s or "").split()), "kind": k or "image",
                "exists": os.path.exists(p)} for p, s, k in rows]
    return {"results": results, "total": len(results)}


@route("POST", "/api/ocr/forget")
def forget(q, body):
    """Remove a folder (or everything) from the text index. Only the index is touched."""
    root = body.get("path")
    with _DB_LOCK:
        con, _ = connect()
        if root:
            like = root.rstrip("/\\") + "%"
            con.execute("DELETE FROM docs WHERE path LIKE ?", (like,))
            con.execute("DELETE FROM files WHERE path LIKE ?", (like,))
            con.execute("DELETE FROM roots WHERE path = ?", (root,))
        else:
            for t in ("docs", "files", "roots"):
                con.execute(f"DELETE FROM {t}")
        con.commit()
        con.close()
    return {"ok": True}
