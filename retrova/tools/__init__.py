"""Disk-care tools: file browsing, space analyser, duplicates, sparse reclaim,
cleanup, system check, find-text (OCR) and transfer."""


def register():
    from . import fsbrowse, analyse, dupes, sparse, cleaner, system, ocr, transfer  # noqa: F401
    return ["fsbrowse", "analyse", "dupes", "sparse", "cleaner", "system", "ocr", "transfer"]


__all__ = ["register"]
