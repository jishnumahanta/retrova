"""Disk recovery: read-only access to damaged/unmountable drives and disk images
(APFS, NTFS, FAT, exFAT, ext, XFS, Btrfs), copy / image / verify / repair jobs,
signature carving ("deep scan") and the platform endpoints (info, elevate, drives)."""


def register():
    """Import the endpoint modules (each registers its routes with retrova.server)."""
    from . import api_platform, api_recover
    from .apfs_engine import patch_dissect
    patch_dissect()
    return [api_platform.__name__, api_recover.__name__]


__all__ = ["register"]
