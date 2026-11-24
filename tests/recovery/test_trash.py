"""Trash while the engine runs as root on a Mac: items must land in the real user's Trash (not root's
hidden one), by a rename on the same volume, keeping their contents and owner."""
import os

import pytest

from retrova import util


@pytest.fixture
def as_root_on_mac(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(util, "IS_MAC", True)
    monkeypatch.setattr(util, "IS_WIN", False)
    monkeypatch.setattr(util, "is_admin", lambda: True)
    monkeypatch.setattr(util, "REAL_UID", os.getuid() or 501)
    monkeypatch.setattr(util, "REAL_GID", os.getgid())
    monkeypatch.setattr(util, "REAL_HOME", str(home))
    return home


def test_goes_to_users_trash_by_rename(tmp_path, as_root_on_mac):
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"jpeg data")
    ino = f.stat().st_ino
    r = util.to_trash([str(f)])
    assert r == {"moved": [str(f)], "failed": []}
    moved = as_root_on_mac / ".Trash" / "photo.jpg"
    assert not f.exists()
    assert moved.read_bytes() == b"jpeg data"
    assert moved.stat().st_ino == ino               # renamed, not copied


def test_same_name_twice_keeps_both(tmp_path, as_root_on_mac):
    for content in (b"one", b"two"):
        f = tmp_path / "notes.txt"
        f.write_bytes(content)
        assert util.to_trash([str(f)])["failed"] == []
    trash = as_root_on_mac / ".Trash"
    assert (trash / "notes.txt").read_bytes() == b"one"
    assert (trash / "notes 2.txt").read_bytes() == b"two"


def test_folder_moves_whole(tmp_path, as_root_on_mac):
    d = tmp_path / "node_modules"
    (d / "pkg").mkdir(parents=True)
    (d / "pkg" / "index.js").write_text("x")
    assert util.to_trash([str(d)])["failed"] == []
    assert (as_root_on_mac / ".Trash" / "node_modules" / "pkg" / "index.js").read_text() == "x"


def test_missing_file_is_reported_not_raised(tmp_path, as_root_on_mac):
    r = util.to_trash([str(tmp_path / "gone")])
    assert r["moved"] == [] and len(r["failed"]) == 1


def test_mount_point_of_tmp_is_a_parent(tmp_path):
    mp = util._mount_point(str(tmp_path))
    assert str(tmp_path).startswith(mp)
    assert os.lstat(mp).st_dev == os.lstat(str(tmp_path)).st_dev
