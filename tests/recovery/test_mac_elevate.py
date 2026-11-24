"""The Mac administrator helper can only start when Retrova lives where root may read it."""
import os

from retrova.recovery import api_platform as ap


def test_applications_is_fine(tmp_path):
    home = tmp_path / "home"
    assert ap._mac_location_problem("/Applications/Retrova.app/Contents/Resources/app", str(home)) == ""


def test_protected_folders_are_refused(tmp_path):
    home = tmp_path / "home"
    for sub in ("Downloads", "Desktop", "Documents", os.path.join("Library", "Mobile Documents", "x")):
        root = home / sub / "Retrova.app" / "Contents" / "Resources" / "app"
        assert ap._mac_location_problem(str(root), str(home)) == ap.MOVE_TO_APPLICATIONS, sub


def test_running_from_the_disk_image_is_refused(tmp_path):
    assert ap._mac_location_problem("/Volumes/Retrova/Retrova.app/Contents/Resources/app",
                                    str(tmp_path)) == ap.MOVE_TO_APPLICATIONS


def test_similar_names_are_not_confused(tmp_path):
    home = tmp_path / "home"
    root = home / "DownloadsArchive" / "Retrova.app"
    assert ap._mac_location_problem(str(root), str(home)) == ""
