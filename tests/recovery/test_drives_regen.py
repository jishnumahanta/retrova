from retrova.recovery import drives
from retrova.recovery.regen import DEV_JUNK, REGEN_RULES, classify_regen, venv_verdict


def test_parse_mac():
    lst = {"AllDisksAndPartitions": [
        {"DeviceIdentifier": "disk0", "Size": 500107862016, "Content": "GUID_partition_scheme",
         "Partitions": [{"DeviceIdentifier": "disk0s1", "Size": 314572800, "Content": "EFI", "VolumeName": "EFI"},
                        {"DeviceIdentifier": "disk0s2", "Size": 499763888128, "Content": "Apple_APFS"}]},
        {"DeviceIdentifier": "disk3", "Size": 499763888128, "Content": "EF57347C-0000-11AA-AA11-00306543ECAC",
         "APFSPhysicalStores": [{"DeviceIdentifier": "disk0s2"}],
         "APFSVolumes": [{"DeviceIdentifier": "disk3s1", "VolumeName": "Macintosh HD", "MountPoint": "/nonexistent"},
                         {"DeviceIdentifier": "disk3s5", "VolumeName": "Macintosh HD - Data",
                          "MountPoint": "/System/Volumes/Data"},
                         {"DeviceIdentifier": "disk3s2", "VolumeName": "Preboot"}]},
        {"DeviceIdentifier": "disk4", "Size": 500107862016, "Content": "GUID_partition_scheme",
         "Partitions": [{"DeviceIdentifier": "disk4s2", "Size": 499763888128, "Content": "Apple_APFS"}]},
        {"DeviceIdentifier": "disk5", "APFSPhysicalStores": [{"DeviceIdentifier": "disk4s2"}], "APFSVolumes": [
            {"DeviceIdentifier": "disk5s1", "VolumeName": "MacStorage"}]},
    ]}
    infos = {
        "disk0": {"MediaName": "APPLE SSD AP0512Q", "Internal": True, "BusProtocol": "Apple Fabric",
                  "SMARTStatus": "Verified", "TotalSize": 500107862016, "VirtualOrPhysical": "Physical"},
        "disk0s2": {"PartitionMapPartitionOffset": 314613760},
        "disk3": {"VirtualOrPhysical": "Virtual"},
        "disk4": {"MediaName": "Samsung Portable SSD T5", "Internal": False, "BusProtocol": "USB",
                  "SMARTStatus": "Not Supported", "VirtualOrPhysical": "Physical", "Ejectable": True},
        "disk4s2": {"PartitionMapPartitionOffset": 209735680},
        "disk5": {"VirtualOrPhysical": "Virtual"},
    }
    r = drives.parse_mac(lst, infos)
    names = [d["id"] for d in r["disks"]]
    assert names == ["disk0", "disk4"]            # APFS containers are not separate disks
    d0, d4 = r["disks"]
    assert d0["health"]["state"] == "ok" and d0["internal"] and not d0["removable"]
    assert d4["health"]["state"] == "unknown" and d4["removable"] and d4["protocol"] == "USB"
    p = d4["partitions"][0]
    assert (p["device"], p["offset"], p["fs"], p["label"], p["mount"]) == (
        "/dev/rdisk4s2", 209735680, "apfs", "MacStorage", "")
    p = d0["partitions"][1]
    assert p["fs"] == "apfs" and p["label"] == "Macintosh HD, Macintosh HD - Data"
    assert p["mount"] == "/System/Volumes/Data"
    assert {v["mount"] for v in r["volumes"]} == {"/nonexistent", "/System/Volumes/Data"}


def test_parse_windows():
    data = {
        "disks": [{"Number": 0, "FriendlyName": "Samsung SSD 970", "Model": "Samsung SSD 970", "Size": 1000,
                   "BusType": 17, "PartitionStyle": 2, "HealthStatus": 0},
                  {"Number": 1, "FriendlyName": "Samsung Portable SSD T5", "Size": 2000, "BusType": 7,
                   "PartitionStyle": 2, "HealthStatus": 1}],
        "parts": [{"DiskNumber": 0, "PartitionNumber": 3, "Offset": 135266304, "Size": 900, "DriveLetter": "C",
                   "Type": "Basic"},
                  {"DiskNumber": 1, "PartitionNumber": 2, "Offset": 209735680, "Size": 1900, "DriveLetter": None,
                   "Type": "Unknown", "AccessPaths": ["\\\\?\\Volume{abc}\\"]}],
        "vols": {"DriveLetter": "C", "FileSystemLabel": "Windows", "FileSystem": "NTFS", "Size": 900,
                 "SizeRemaining": 100, "Path": "\\\\?\\Volume{c}\\"},
        "phys": [{"DeviceId": "1", "HealthStatus": "Warning", "OperationalStatus": ["OK"]}],
    }
    r = drives.parse_windows(data)
    d0, d1 = r["disks"]
    assert d0["device"] == "\\\\.\\PhysicalDrive0" and d0["protocol"] == "NVMe" and d0["health"]["state"] == "ok"
    assert d0["partitions"][0]["fs"] == "ntfs" and d0["partitions"][0]["mount"] == "C:\\"
    assert d1["removable"] and d1["health"]["state"] == "warn"
    assert d1["partitions"][0]["offset"] == 209735680 and d1["partitions"][0]["fs"] is None
    assert r["volumes"] == [{"mount": "C:\\", "label": "Windows", "fs": "ntfs", "size": 900, "free": 100,
                             "device": "\\\\?\\Volume{c}\\"}]


def test_parse_linux():
    data = {"blockdevices": [
        {"name": "sda", "path": "/dev/sda", "size": 1000, "type": "disk", "model": "Disk", "vendor": "ACME",
         "rm": False, "hotplug": False, "tran": "sata", "rota": "1", "children": [
             {"name": "sda1", "kname": "sda1", "path": "/dev/sda1", "size": 500, "type": "part", "fstype": "ext4",
              "label": "root", "mountpoint": None}]},
        {"name": "sdb", "path": "/dev/sdb", "size": 64, "type": "disk", "rm": "1", "tran": "usb",
         "fstype": "exfat", "label": "CARD", "mountpoint": None},
        {"name": "loop0", "path": "/dev/loop0", "size": 10, "type": "loop"},
    ]}
    r = drives.parse_linux(data, {}, lambda k: 1048576)
    a, b = r["disks"]
    assert a["partitions"][0]["offset"] == 1048576 and a["partitions"][0]["fs"] == "ext4"
    assert not a["removable"] and a["rotational"] and a["model"] == "ACME Disk"
    assert b["removable"] and b["partitions"][0] == {"id": "sdb", "device": "/dev/sdb", "offset": 0, "size": 64,
                                                     "fs": "exfat", "label": "CARD", "mount": "", "free": None}


def test_list_drives_never_raises():
    r = drives.list_drives()
    assert "disks" in r and "volumes" in r


def test_regen_api_unchanged():
    assert any(r[0] == "node_modules" for r in REGEN_RULES) and "node_modules" in DEV_JUNK
    assert classify_regen("node_modules", ["package.json", "package-lock.json"], lambda: [])[0] == "safe"
    assert classify_regen("node_modules", ["package.json"], lambda: [])[0] == "review"
    assert classify_regen("node_modules", ["package.json"], lambda: [], lock_above=True)[0] == "safe"
    assert classify_regen("build", ["build.gradle"], lambda: []) == ("safe", "Gradle/Android build output",
                                                                     "./gradlew build")
    assert classify_regen("build", [], lambda: ["CMakeCache.txt"])[0] == "safe"
    assert classify_regen(".git", ["package.json"], lambda: []) is None
    assert classify_regen("venv", ["requirements.txt"], lambda: ["pyvenv.cfg"])[0] == "safe"
    assert classify_regen("venv", [], lambda: ["bin"]) is None
    assert venv_verdict(".venv", [])[0] == "review"


def test_output_on_the_source_drive_is_refused(monkeypatch, tmp_path):
    from retrova.recovery import api_recover
    from retrova.server import ApiError
    import pytest
    fake = {"disks": [
        {"device": "/dev/rdisk4", "partitions": [{"mount": str(tmp_path / "SourceVol"), "mounts": []}]},
        {"device": "/dev/rdisk0", "partitions": [{"mount": "/", "mounts": ["/", str(tmp_path)]}]},
    ], "volumes": []}
    monkeypatch.setattr(api_recover, "list_drives", lambda: fake)
    with pytest.raises(ApiError, match="drive you are recovering from"):
        api_recover.check_not_on_source_drive(str(tmp_path / "SourceVol" / "out"), "/dev/rdisk4s2")
    api_recover.check_not_on_source_drive(str(tmp_path / "elsewhere"), "/dev/rdisk4s2")     # other drive: fine
    with pytest.raises(ApiError):
        api_recover.check_not_on_source_drive(str(tmp_path / "x"), "/dev/disk0s2")
    api_recover.check_not_on_source_drive(str(tmp_path / "x"), str(tmp_path / "image.img"))  # images: no check
