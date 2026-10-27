"""Archive drives: where they are, whether they are plugged in, and whether a drive plugged in
under a catalogued name really is that drive."""
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from jva.config import CONFIG

MAC_VOLUMES = "/Volumes"


def root():
    return CONFIG.volumes_root


def path_of(archive, volumes_root=None):
    """'TAP 4T 2019' -> /Volumes/TAP 4T 2019; a full path is taken as it is."""
    if archive.startswith("/"):
        return Path(archive)
    return Path(volumes_root or root()) / archive


def is_mounted(archive, volumes_root=None):
    """Under /Volumes a drive must really be mounted (so nothing is ever written onto the boot
    disk by mistake); any other volumes_root (a test, a server copy) only needs the folder."""
    vr = volumes_root or root()
    p = path_of(archive, vr)
    if os.path.normpath(vr) == MAC_VOLUMES:
        return os.path.ismount(p)
    return p.is_dir()


def diskutil(target):
    """macOS `diskutil info` for a volume, as a dict ({} elsewhere or on failure)."""
    if sys.platform != "darwin":
        return {}
    try:
        out = subprocess.run(["diskutil", "info", "-plist", str(target)],
                             capture_output=True, check=True, timeout=30).stdout
        return plistlib.loads(out)
    except Exception:
        return {}


_uuid_cache = {}


def volume_uuid(archive, volumes_root=None):
    if archive not in _uuid_cache:
        _uuid_cache[archive] = diskutil(path_of(archive, volumes_root)).get("VolumeUUID", "")
    return _uuid_cache[archive]


def problem(db, archive, volumes_root=None):
    """None if the archive drive is plugged in and is the drive the catalogue knows (by its
    volume UUID, so a different drive with the same name can't be touched); else why not."""
    if not is_mounted(archive, volumes_root):
        return f'"{archive}" is not plugged in'
    known = db.execute("SELECT uuid FROM archives WHERE name=?", (archive,)).fetchone()
    if not known or not known[0]:
        return None
    u = volume_uuid(archive, volumes_root)
    if u and u != known[0]:
        return f'the drive plugged in as "{archive}" is not the one in the catalogue'
    return None
