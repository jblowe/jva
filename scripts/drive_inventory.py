#!/usr/bin/env python3
"""
drive_inventory.py -- inventory one external drive and copy it onto an archive drive (macOS).

Plug in ONE source drive (the archive drive stays mounted too), then:
  python3 drive_inventory.py -a "ARCHIVE VOLUME"                 # finds the source drive itself
  python3 drive_inventory.py -a "ARCHIVE VOLUME" /Volumes/Foo    # or name it explicitly

  -a, --archive NAME   the archive volume to copy onto (required): a name in /Volumes or a path.
                       Copies go to the root of that volume:  /Volumes/NAME/<drive name>/

Options:
  --folder SUB  put copies in a subfolder of the archive volume instead of its root
                (e.g. --folder CumulativeArchive on TAP 4T 2019)
  --name NAME   archive folder name to use (skips the duplicate-name prompt; reuses it if it exists)
  --no-copy     inventory only, don't copy
  --redo        process a drive again even if it was already archived (only missing or changed
                files get copied)
  --reserve-gb  free space to leave on the archive drive (default 5)
  --magic       also sniff each file's real type with `file` (slower)
  --all         include macOS housekeeping dirs (.Spotlight-V100, .fseventsd, .Trashes, ...)
  --summary-only  just rebuild drive_summary.csv from drives.csv (no drive needed)
  --no-eject    leave the source drive mounted when finished (by default it is ejected)
  --no-readonly don't remount the source drive read-only (by default it is, so nothing --
                not even Spotlight or Finder -- can write to the original)
  --speedtest   measure source read speed and archive write speed separately, then stop

What it does, per run:
  0. Remounts the source drive read-only (unless --no-readonly), so nothing can write to it.
  1. Picks the archive folder name: the volume name, or, if that name is already used (on any
     archive drive, or in the destination folder), asks you for a different one.
  2. Inventories the drive (read-only) into  <project>/<name>.csv : one row per file, plus a
     row for anything that couldn't be read (same name as the archive folder; a rerun replaces it)
  3. Checks there's room on the archive drive. If there isn't, it refuses to copy.
  4. Copies everything into  /Volumes/<archive>/[<folder>/]<name>/  keeping
     dates, permissions, extended attributes and resource forks (macOS copyfile).
     Files that can't be copied are added to <name>.csv as COPY ERROR rows; the copy continues.
     If interrupted, just rerun: files already copied (same size and date) are skipped.
  5. Appends a row to <project>/drives.csv, updates <project>/archive_map.csv, and rebuilds
     <project>/drive_summary.csv (one row per drive + TOTAL; all number columns are plain numbers).
  6. Ejects the source drive (unless --no-eject), so you can unplug it and plug in the next.
  7. Prints one line to STDOUT: free GB on the archive drive, GB copied this run, copy time
     in seconds and MB/s.
     All other messages go to STDERR.
Standard library only.
"""
import argparse
import csv
import ctypes
import ctypes.util
import datetime as dt
import errno
import mimetypes
import os
import plistlib
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

# archive_map.csv rows written before archive drives were recorded all went here:
LEGACY_ARCHIVE = ("TAP 4T 2019", "CumulativeArchive")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # the repository, for jva
from jva.config import CONFIG  # noqa: E402
from jva.catalog import connect, has_table  # noqa: E402

PROJECT_DIR = str(CONFIG.data_dir)       # catalogue, reports and drive records

SKIP_DIRS = {".Spotlight-V100", ".fseventsd", ".Trashes", ".DocumentRevisions-V100",
             ".TemporaryItems", ".MobileBackups", "$RECYCLE.BIN", "System Volume Information"}

DRIVE_FIELDS = [
    "inventoried_at", "mount_point", "volume_name", "volume_uuid", "device",
    "filesystem", "read_only", "physical_disk", "model", "bus", "solid_state", "removable", "encrypted",
    "physical_disk_bytes", "physical_disk_gb", "capacity_bytes", "used_bytes", "free_bytes",
    "capacity_gb", "used_gb", "free_gb", "pct_used",
    "file_count", "dir_count", "top_level_dirs", "top_level_files", "hidden_files",
    "total_file_bytes", "total_file_gb", "largest_file", "largest_file_gb",
    "top_type_1", "top_type_1_gb", "top_type_2", "top_type_2_gb", "top_type_3", "top_type_3_gb",
    "top_type_4", "top_type_4_gb", "top_type_5", "top_type_5_gb",
    "oldest_modified", "newest_modified", "errors", "scan_seconds", "top_level_dir_names",
    "files_csv",
    "archive_volume", "archive_name", "archive_dir", "copy_status", "files_to_copy", "copied_files",
    "copied_bytes", "copied_gb", "skipped_existing", "copy_errors", "copy_seconds", "copy_mb_per_s",
    "archive_free_gb_after",
]

# drive_summary.csv: one row per drive (latest run), plus a TOTAL row
SUMMARY_FIELDS = [
    "volume_name", "archive_volume", "archive_name", "copy_status", "copied_gb", "copy_errors",
    "model", "disk_type", "bus", "format", "encrypted",
    "disk_size_gb", "space_used_gb", "space_free_gb", "pct_used",
    "file_count", "folder_count", "top_level_dirs", "top_level_files", "hidden_files",
    "total_file_gb", "read_errors", "largest_file", "largest_file_gb",
    "top_type_1", "top_type_1_gb", "top_type_2", "top_type_2_gb", "top_type_3", "top_type_3_gb",
    "top_type_4", "top_type_4_gb", "top_type_5", "top_type_5_gb",
    "oldest_modified", "newest_modified", "physical_disk_gb", "top_level_dir_names",
    "volume_uuid", "inventoried_at", "files_csv",
]

# <name>.csv: one row per file. status is "file", or "READ ERROR" / "COPY ERROR" (see error column)
FILE_FIELDS = ["path", "name", "ext", "status", "mime_guess", "magic_type", "size_bytes",
               "created", "modified", "accessed", "perms", "hidden", "error"]

# archive_dir is relative to the root of archive_volume
MAP_FIELDS = ["volume_uuid", "volume_name", "archive_volume", "archive_dir", "archive_name",
              "status", "updated"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------- helpers ----------

def diskutil(target):
    """diskutil info as a dict ({} if unavailable)."""
    try:
        out = subprocess.run(["diskutil", "info", "-plist", str(target)],
                             capture_output=True, check=True).stdout
        return plistlib.loads(out)
    except Exception:
        return {}


def ts(t):
    return dt.datetime.fromtimestamp(t).isoformat(sep=" ", timespec="seconds") if t else ""


def gb(n):
    return round(float(n or 0) / 1e9, 6)


def to_int(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def top_types(by_ext, n=5):
    """The n extensions using the most space, as top_type_i / top_type_i_gb columns."""
    out = {}
    ranked = [(e, b) for e, b in by_ext.most_common(n) if b]
    for i in range(1, n + 1):
        e, b = ranked[i - 1] if i <= len(ranked) else ("", None)
        out[f"top_type_{i}"] = e
        out[f"top_type_{i}_gb"] = gb(b) if b is not None else ""
    return out


def kind_of(mode):
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def physical_disk_info(vol_info):
    """Follow APFS volume -> container -> physical store -> whole disk."""
    stores = vol_info.get("APFSPhysicalStores") or []
    if stores:
        store = stores[0].get("APFSPhysicalStore", "")
        whole = diskutil(store).get("ParentWholeDisk", store) if store else ""
    else:
        whole = vol_info.get("ParentWholeDisk", "")
    return whole, (diskutil(whole) if whole else {})


def find_source(archive_vol, other_archives=()):
    """The single external volume that isn't the archive drive (or another known archive drive)."""
    arch_disk = physical_disk_info(diskutil(archive_vol))[0]
    cands = []
    for p in sorted(Path("/Volumes").iterdir()):
        if p.is_symlink() or p.resolve() == archive_vol.resolve() or p.name in other_archives:
            continue
        info = diskutil(p)
        if not info or info.get("Internal", True):
            continue
        if arch_disk and physical_disk_info(info)[0] == arch_disk:
            continue        # another partition of the archive drive
        cands.append(p)
    if not cands:
        sys.exit(f"No source drive found (only {archive_vol} is mounted?).")
    if len(cands) > 1:
        sys.exit("More than one source drive mounted:\n  " + "\n  ".join(map(str, cands)) +
                 "\nEject the extras, or name one: python3 drive_inventory.py -a ... '/Volumes/...'")
    return cands[0]


def drive_id(uuid, name):
    """How a drive is recognized: its volume UUID (by name only if it has none)."""
    return uuid or "name:" + (name or "")


def read_csv(path):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def plain(row):
    """Write floats as plain decimals (no 5e-06 style), so spreadsheets read them as numbers."""
    return {k: (f"{v:.6f}".rstrip("0").rstrip(".") if isinstance(v, float) else v)
            for k, v in row.items()}


def append_row(path, row, fields):
    """Append to a CSV; if an older file has different columns, rewrite it with the union."""
    row = plain(row)
    if path.exists():
        with open(path, newline="", encoding="utf-8-sig") as fh:
            header = next(csv.reader(fh), [])
        if header != fields:
            rows = read_csv(path) + [row]
            allf = fields + [h for h in header if h not in fields]
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.DictWriter(fh, fieldnames=allf, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
            return
        with open(path, "a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore").writerow(row)
    else:
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerow(row)


def load_map(outdir):
    amap = {}
    for r in read_csv(outdir / "archive_map.csv"):
        if not r.get("archive_volume"):               # written before archive drives were recorded
            r["archive_volume"] = LEGACY_ARCHIVE[0]
            r["archive_dir"] = f"{LEGACY_ARCHIVE[1]}/{r.get('archive_name', '')}"
        amap[drive_id(r.get("volume_uuid"), r.get("volume_name"))] = r
    return amap


def archive_volume_path(arg):
    """'TAP 4T 2019' -> /Volumes/TAP 4T 2019 ; a full path is taken as is."""
    return Path(arg) if arg.startswith("/") else Path("/Volumes") / arg


def save_map(outdir, amap):
    with open(outdir / "archive_map.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=MAP_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(amap.values())


def name_ok(name):
    return bool(name) and "/" not in name and ":" not in name and not name.startswith(".") \
        and len(name) <= 200


def choose_archive_name(vol_name, amap, archive_root):
    # names must be unique across ALL archive drives (they share one catalogue), and must
    # not clash with anything already in the destination folder
    taken = {r["archive_name"].lower() for r in amap.values() if r.get("archive_name")}
    if archive_root.is_dir():
        taken |= {p.name.lower() for p in archive_root.iterdir()}
    name = vol_name.strip()
    while True:
        if not name_ok(name):
            log(f'"{name}" can\'t be used as a folder name (no "/", ":" or leading ".").')
        elif name.lower() in taken:
            where = [f'{r["archive_volume"]}: {r["archive_dir"]}' for r in amap.values()
                     if r.get("archive_name", "").lower() == name.lower()]
            log(f'\nDUPLICATE: "{name}" is already used ' +
                (f'({"; ".join(where)})' if where else f"in {archive_root}"))
        else:
            return name
        sys.stderr.write("Enter a different name for this drive's archive folder: ")
        sys.stderr.flush()
        line = sys.stdin.readline()
        if not line:
            sys.exit("No name given; nothing copied.")
        name = line.strip()


def walk(root, include_all):
    """Yield (path, stat_or_None, error_or_None); stays on one filesystem, no symlink following."""
    root_dev = os.lstat(root).st_dev
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError as e:
            yield d, None, e
            continue
        with it:
            while True:
                try:
                    entry = next(it)
                except StopIteration:
                    break
                except OSError as e:          # listing died mid-folder (bad sectors etc.)
                    yield d, None, e
                    break
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError as e:
                    yield entry.path, None, e
                    continue
                yield entry.path, st, None
                if (stat.S_ISDIR(st.st_mode) and st.st_dev == root_dev
                        and (include_all or entry.name not in SKIP_DIRS)):
                    stack.append(entry.path)


def add_magic(rows, root):
    """Fill magic_type for regular files in a batch using `file`."""
    targets = [r for r in rows if r["status"] == "file"]
    if not targets:
        return
    paths = [os.path.join(root, r["path"]) for r in targets]
    try:
        out = subprocess.run(["file", "-b", "--mime-type", "--", *paths],
                             capture_output=True, text=True, errors="replace").stdout.splitlines()
    except Exception:
        out = []
    if len(out) != len(targets):
        out = []
        for p in paths:
            try:
                out.append(subprocess.run(["file", "-b", "--mime-type", "--", p],
                                          capture_output=True, text=True,
                                          errors="replace").stdout.strip())
            except Exception:
                out.append("")
    for r, m in zip(targets, out):
        r["magic_type"] = m


# ---------- inventory ----------

def inventory(mount, info, files_csv, magic, include_all):
    """Write files_csv; return (drive stats, folder list, symlink list)."""
    started = time.time()
    dirs, links = [], []
    n_files = n_dirs = n_err = total_bytes = 0
    oldest = newest = None
    top_dirs, top_files, hidden_files = [], 0, 0
    largest, largest_size = "", 0
    by_ext = Counter()
    buf = []

    with open(files_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FILE_FIELDS)
        w.writeheader()

        def flush():
            if magic:
                add_magic(buf, mount)
            w.writerows(buf)
            buf.clear()

        for i, (path, st, err) in enumerate(walk(mount, include_all), 1):
            rel = os.path.relpath(path, mount)
            name = os.path.basename(path)
            row = {"path": rel, "name": name}
            if err is not None:
                n_err += 1
                row.update(status="READ ERROR", error=f"{err.__class__.__name__}: {err.strerror or err}")
                log(f"   READ ERROR {rel}: {err.strerror or err}")
                buf.append(row)
            else:
                k = kind_of(st.st_mode)
                ext = os.path.splitext(name)[1].lower() if k == "file" else ""
                hidden = name.startswith(".") or bool(getattr(st, "st_flags", 0) & stat.UF_HIDDEN)
                top = os.sep not in rel
                if k == "symlink":
                    links.append(rel)
                elif k == "file":
                    row.update(
                        ext=ext, status="file",
                        mime_guess=mimetypes.guess_type(name)[0] or "",
                        size_bytes=st.st_size,
                        created=ts(getattr(st, "st_birthtime", 0)),
                        modified=ts(st.st_mtime), accessed=ts(st.st_atime),
                        perms=stat.filemode(st.st_mode), hidden="Y" if hidden else "",
                    )
                    buf.append(row)
                    n_files += 1
                    total_bytes += st.st_size
                    oldest = st.st_mtime if oldest is None else min(oldest, st.st_mtime)
                    newest = st.st_mtime if newest is None else max(newest, st.st_mtime)
                    by_ext[ext or "(none)"] += st.st_size
                    if st.st_size > largest_size or not largest:
                        largest, largest_size = rel, st.st_size
                    if top:
                        top_files += 1
                    if hidden:
                        hidden_files += 1
                elif k == "dir":
                    n_dirs += 1
                    dirs.append(rel)
                    if top and not hidden:
                        top_dirs.append(rel)
            if len(buf) >= 500:
                flush()
            if i % 25000 == 0:
                log(f"   {i:,} entries...")
        flush()

    usage = shutil.disk_usage(mount)
    whole, wd = physical_disk_info(info)
    phys = to_int(wd.get("TotalSize") or wd.get("Size"))
    drive = {
        "mount_point": str(mount),
        "read_only": "Y" if is_readonly(mount) else "N",
        "device": info.get("DeviceIdentifier", ""),
        "filesystem": info.get("FilesystemName") or info.get("FilesystemType", ""),
        "physical_disk": whole,
        "model": wd.get("MediaName") or info.get("MediaName", ""),
        "bus": wd.get("BusProtocol") or info.get("BusProtocol", ""),
        "solid_state": wd.get("SolidState", info.get("SolidState", "")),
        "removable": wd.get("RemovableMediaOrExternalDevice", info.get("RemovableMediaOrExternalDevice", "")),
        "encrypted": info.get("Encryption", info.get("FileVault", "")),
        "physical_disk_bytes": phys,
        "physical_disk_gb": gb(phys) if phys else "",
        "capacity_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "capacity_gb": gb(usage.total),
        "used_gb": gb(usage.used),
        "free_gb": gb(usage.free),
        "pct_used": round(100 * usage.used / usage.total, 1) if usage.total else 0,
        "file_count": n_files,
        "dir_count": n_dirs,
        "top_level_dirs": len(top_dirs),
        "top_level_files": top_files,
        "hidden_files": hidden_files,
        "total_file_bytes": total_bytes,
        "total_file_gb": gb(total_bytes),
        "largest_file": largest,
        "largest_file_gb": gb(largest_size),
        "oldest_modified": ts(oldest),
        "newest_modified": ts(newest),
        "errors": n_err,
        "scan_seconds": round(time.time() - started, 1),
        "top_level_dir_names": "; ".join(sorted(top_dirs, key=str.lower))[:1000],
        "files_csv": str(files_csv),
    }
    drive.update(top_types(by_ext))
    log(f"   inventory: {n_files:,} files, {n_dirs:,} folders, {gb(total_bytes)} GB, "
        f"{n_err} read errors, {drive['scan_seconds']}s -> {files_csv}")
    return drive, dirs, links


# ---------- copy ----------

def make_copier():
    """macOS copyfile(3) with COPYFILE_ALL (data, dates, perms, ACLs, xattrs, resource forks),
    like Finder/cp. Falls back to shutil.copy2 elsewhere."""
    if sys.platform != "darwin":
        return shutil.copy2
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    fn = libc.copyfile
    fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint32]
    fn.restype = ctypes.c_int
    COPYFILE_ALL = 0x0F

    def cp(src, dst):
        if fn(os.fsencode(src), os.fsencode(dst), None, COPYFILE_ALL) != 0:
            e = ctypes.get_errno()
            raise OSError(e, os.strerror(e), src)
    return cp


def _rm(p):
    try:
        os.unlink(p)
    except OSError:
        pass


def copy_one(copier, s, d):
    try:
        copier(s, d)
    except OSError as e:
        _rm(d)                              # never leave a partial file behind
        if copier is shutil.copy2 or e.errno == errno.EIO:
            raise
        try:                                # e.g. archive drive rejects some metadata
            shutil.copy2(s, d)
        except OSError:
            _rm(d)
            raise e


def already_there(d, row):
    try:
        st = os.stat(d)
    except OSError:
        return False
    return str(st.st_size) == row["size_bytes"] and ts(st.st_mtime) == row["modified"]


def file_rows(files_csv):
    with open(files_csv, newline="", encoding="utf-8-sig") as fh:
        yield from csv.DictReader(fh)


def removed_copies(outdir, archive):
    """Paths (relative to the archive volume) that free_space.py removed as extra copies, or
    that delete_files.py / the search app deleted -- a --redo must not copy them back."""
    p = outdir / "archive_catalog.sqlite"
    if not p.exists():
        return set()
    out = set()
    try:
        with connect(p, readonly=True) as db:
            for table in ("removed_copies", "deleted_files"):
                if has_table(db, table):
                    out |= {r[0] for r in db.execute(f"SELECT path FROM {table} WHERE archive=?", (archive,))}
    except sqlite3.Error:
        pass
    return out


def copy_drive(src, dest, files_csv, dirs, links, archive_vol, reserve_gb, removed=frozenset()):
    res = {"files_to_copy": 0, "copied_files": 0, "copied_bytes": 0,
           "skipped_existing": 0, "copy_errors": 0}
    dest_rel = os.path.relpath(dest, archive_vol)

    def was_removed(rel):
        return bool(removed) and os.path.join(dest_rel, rel) in removed

    # 1. how much is still to copy (handles resumed runs)
    need = 0
    for r in file_rows(files_csv):
        if r["status"] == "file" and not was_removed(r["path"]) and \
                not already_there(os.path.join(dest, r["path"]), r):
            res["files_to_copy"] += 1
            need += -(-int(r["size_bytes"] or 0) // 4096) * 4096    # round up to 4K blocks
    free = shutil.disk_usage(archive_vol).free
    log(f"   to copy: {res['files_to_copy']:,} files, {gb(need)} GB; "
        f"archive drive has {gb(free)} GB free (keeping {reserve_gb} GB in reserve)")
    if need + reserve_gb * 1e9 > free:
        res["copy_status"] = (f"REFUSED - needs {gb(need)} GB + {reserve_gb:g} GB reserve, "
                              f"only {gb(free)} GB free")
        log(f"\n*** NOT COPYING: {res['copy_status']} ***")
        return res

    # 2. copy
    copier = make_copier()
    dest.mkdir(parents=True, exist_ok=True)
    started = last = time.time()
    last_bytes = 0
    errs = []

    def fail(rel, e):
        res["copy_errors"] += 1
        msg = f"{e.__class__.__name__}: {e.strerror or e}"
        errs.append({"path": rel, "name": os.path.basename(rel), "status": "COPY ERROR", "error": msg})
        log(f"   COPY ERROR {rel}: {msg}")

    try:
        for rel in dirs:                       # folders first, so empty ones are kept too
            try:
                os.makedirs(os.path.join(dest, rel), exist_ok=True)
            except OSError as e:
                fail(rel, e)

        for r in file_rows(files_csv):
            if r["status"] != "file":
                continue
            rel = r["path"]
            s, d = os.path.join(src, rel), os.path.join(dest, rel)
            if was_removed(rel):
                res["skipped_removed"] = res.get("skipped_removed", 0) + 1
                continue            # free_space.py removed this extra copy on purpose
            try:
                if already_there(d, r):
                    res["skipped_existing"] += 1
                else:
                    os.makedirs(os.path.dirname(d), exist_ok=True)
                    copy_one(copier, s, d)
                    res["copied_files"] += 1
                    res["copied_bytes"] += int(r["size_bytes"] or 0)
            except OSError as e:
                fail(rel, e)
            now = time.time()
            if now - last > 30:
                cur = (res["copied_bytes"] - last_bytes) / 1e6 / (now - last)
                avg = res["copied_bytes"] / 1e6 / max(now - started, 1)
                left = (need - res["copied_bytes"]) / 1e6 / cur / 3600 if cur > 0 else 0
                log(f"   copied {res['copied_files']:,}/{res['files_to_copy']:,} files, "
                    f"{res['copied_bytes'] / 1e9:.1f}/{need / 1e9:.1f} GB  "
                    f"(now {cur:.0f} MB/s, average {avg:.0f} MB/s, ~{left:.1f} h left)")
                last, last_bytes = now, res["copied_bytes"]

        for rel in links:
            d = os.path.join(dest, rel)
            try:
                if not os.path.lexists(d):
                    os.makedirs(os.path.dirname(d), exist_ok=True)
                    os.symlink(os.readlink(os.path.join(src, rel)), d)
            except OSError as e:
                fail(rel, e)
    finally:                                   # record copy errors even if interrupted
        if errs:
            with open(files_csv, "a", newline="", encoding="utf-8") as fh:
                csv.DictWriter(fh, fieldnames=FILE_FIELDS, extrasaction="ignore").writerows(errs)

    # folder dates (adding files changed them)
    for rel in dirs:
        try:
            shutil.copystat(os.path.join(src, rel), os.path.join(dest, rel))
        except OSError:
            pass

    elapsed = time.time() - started
    res["copy_seconds"] = round(elapsed, 1)
    res["copy_mb_per_s"] = round(res["copied_bytes"] / 1e6 / elapsed, 1) if elapsed > 0 else 0
    res["copy_status"] = "copied" if not res["copy_errors"] else f"copied with {res['copy_errors']} errors"
    log(f"   copy: {res['copied_files']:,} files, {gb(res['copied_bytes'])} GB in "
        f"{res['copy_seconds']}s; {res['skipped_existing']:,} already there; "
        f"{res['copy_errors']} errors")
    return res


# ---------- summary ----------

def disk_type(solid):
    s = str(solid).strip().lower()
    return "SSD" if s == "true" else "HDD" if s == "false" else ""


def find_files_csv(d, outdir):
    p = Path(d.get("files_csv", ""))
    an, vn = d.get("archive_name", ""), d.get("volume_name", "")
    cands = [p, outdir / p.name, outdir / f"{an}.csv", outdir / f"{vn}.csv",
             # layouts from earlier versions: <folder>/files.csv
             outdir / p.parent.name / "files.csv", outdir / an / "files.csv", outdir / vn / "files.csv"]
    for c in cands:
        if c.suffix == ".csv" and c.name not in ("drives.csv", "drive_summary.csv",
                                                 "archive_map.csv") and c.is_file():
            return c
    return None


def legacy_stats(files_csv):
    """Top-level/largest/type stats for drives.csv rows written by older versions."""
    top_dirs, top_files, hidden = [], 0, 0
    largest, largest_size = "", 0
    by_ext = Counter()
    for r in file_rows(files_csv):
        kind, rel = r.get("kind"), r.get("path", "")
        top = "/" not in rel
        if kind == "dir" and top and not r.get("hidden"):
            top_dirs.append(rel)
        elif kind == "file":
            size = to_int(r.get("size_bytes"))
            top_files += top
            hidden += bool(r.get("hidden"))
            by_ext[r.get("ext") or "(none)"] += size
            if size > largest_size or not largest:
                largest, largest_size = rel, size
    s = {"top_level_dirs": len(top_dirs), "top_level_files": top_files, "hidden_files": hidden,
         "largest_file": largest, "largest_file_gb": gb(largest_size),
         "top_level_dir_names": "; ".join(sorted(top_dirs, key=str.lower))[:1000]}
    s.update(top_types(by_ext))
    return s


def _volume_of(path):
    """'/Volumes/TAP 4T 2019/CumulativeArchive/X' -> 'TAP 4T 2019' (for older drives.csv rows)."""
    parts = Path(path).parts
    return parts[2] if len(parts) > 2 and parts[1] == "Volumes" else ""


def write_summary(outdir):
    """Rebuild drive_summary.csv from drives.csv: latest run per drive, plus TOTAL.
    Every *_gb / count / pct column holds a plain number."""
    latest = {}
    for d in read_csv(outdir / "drives.csv"):
        latest[drive_id(d.get("volume_uuid"), d.get("volume_name"))] = d
    rows = []
    for d in sorted(latest.values(), key=lambda d: d.get("volume_name", "").lower()):
        fc = find_files_csv(d, outdir)
        if d.get("top_level_dirs", "") == "" and fc:
            d = {**d, **legacy_stats(fc)}
        r = {
            "volume_name": d.get("volume_name", ""),
            "archive_name": d.get("archive_name", ""),
            "archive_volume": d.get("archive_volume") or _volume_of(d.get("archive_dir", "")),
            "copy_status": d.get("copy_status", ""),
            "copied_gb": gb(d.get("copied_bytes")),
            "copy_errors": to_int(d.get("copy_errors")),
            "model": d.get("model", ""),
            "disk_type": disk_type(d.get("solid_state")),
            "bus": d.get("bus", ""),
            "format": d.get("filesystem", ""),
            "encrypted": d.get("encrypted", ""),
            "disk_size_gb": gb(d.get("capacity_bytes")),
            "space_used_gb": gb(d.get("used_bytes")),
            "space_free_gb": gb(d.get("free_bytes")),
            "pct_used": round(to_float(d.get("pct_used")), 1),
            "file_count": to_int(d.get("file_count")),
            "folder_count": to_int(d.get("dir_count")),
            "top_level_dirs": to_int(d.get("top_level_dirs")),
            "top_level_files": to_int(d.get("top_level_files")),
            "hidden_files": to_int(d.get("hidden_files")),
            "total_file_gb": gb(d.get("total_file_bytes")),
            "read_errors": to_int(d.get("errors")),
            "largest_file": d.get("largest_file", ""),
            "largest_file_gb": round(to_float(d.get("largest_file_gb")), 6),
            "oldest_modified": d.get("oldest_modified", ""),
            "newest_modified": d.get("newest_modified", ""),
            "physical_disk_gb": gb(d["physical_disk_bytes"]) if to_int(d.get("physical_disk_bytes")) else "",
            "top_level_dir_names": d.get("top_level_dir_names", ""),
            "volume_uuid": d.get("volume_uuid", ""),
            "inventoried_at": d.get("inventoried_at", ""),
            "files_csv": str(fc) if fc else "MISSING: " + d.get("files_csv", ""),
        }
        for i in range(1, 6):
            r[f"top_type_{i}"] = d.get(f"top_type_{i}", "")
            v = d.get(f"top_type_{i}_gb", "")
            r[f"top_type_{i}_gb"] = round(to_float(v), 6) if v != "" else ""
        rows.append(r)

    def tot(k):
        return sum(r[k] for r in rows if isinstance(r.get(k), (int, float)))
    cap = sum(to_int(d.get("capacity_bytes")) for d in latest.values())
    used = sum(to_int(d.get("used_bytes")) for d in latest.values())
    total = {"volume_name": f"TOTAL ({len(rows)} drives)"}
    for k in ("copied_gb", "disk_size_gb", "space_used_gb", "space_free_gb", "total_file_gb",
              "physical_disk_gb"):
        total[k] = round(tot(k), 6)
    for k in ("copy_errors", "file_count", "folder_count", "top_level_dirs", "top_level_files",
              "hidden_files", "read_errors"):
        total[k] = tot(k)
    total["pct_used"] = round(100 * used / cap, 1) if cap else 0
    total["oldest_modified"] = min((r["oldest_modified"] for r in rows if r["oldest_modified"]), default="")
    total["newest_modified"] = max((r["newest_modified"] for r in rows if r["newest_modified"]), default="")
    rows.append(total)

    out = outdir / "drive_summary.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(plain(r) for r in rows)
    log(f"   summary of {len(rows) - 1} drives -> {out}")


# ---------- read-only mount ----------

def is_readonly(path):
    try:
        return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except OSError:
        return False


def remount_readonly(src):
    """Unmount the source drive and mount it again read-only, so neither this script nor
    Spotlight/Finder can write anything to the original. Returns the (new) mount point."""
    if is_readonly(src):
        log("   source drive is mounted read-only")
        return src
    dev = diskutil(src).get("DeviceIdentifier")
    if not dev:
        log("   (couldn't get the device name from diskutil; leaving the drive as mounted)")
        return src
    log(f"   remounting {dev} read-only so nothing gets written to the original ...")
    for attempt in range(5):
        r = subprocess.run(["diskutil", "unmount", dev], capture_output=True, text=True)
        if r.returncode == 0:
            break
        time.sleep(3)                      # often Spotlight or a Finder window has it open
    else:
        sys.exit(f"Could not unmount {src} to remount it read-only:\n  "
                 f"{(r.stderr or r.stdout).strip()}\n"
                 "Close Finder windows/apps using the drive and try again, "
                 "or run with --no-readonly to use it as it is.")
    r = subprocess.run(["diskutil", "mount", "readOnly", dev], capture_output=True, text=True)
    mp = diskutil(dev).get("MountPoint") if r.returncode == 0 else ""
    if not mp or not is_readonly(mp):
        subprocess.run(["diskutil", "mount", dev], capture_output=True)   # don't leave it unmounted
        sys.exit(f"Could not mount {dev} read-only ({(r.stderr or r.stdout).strip()}).\n"
                 "It has been mounted normally again. Run with --no-readonly to use it as it is.")
    log(f"   mounted read-only at {mp}")
    return Path(mp)


def eject(src, archive_vol):
    """Eject the source drive when done, so it can be unplugged. Never touches the archive drive."""
    info = diskutil(src)
    dev = info.get("DeviceIdentifier")
    if not dev:
        log("   (couldn't get the device name from diskutil; not ejecting)")
        return False
    src_disk = physical_disk_info(info)[0]
    arch_disk = physical_disk_info(diskutil(archive_vol))[0]
    # same physical disk as the archive (another partition): only unmount this volume
    cmd = ["diskutil", "unmount", dev] if src_disk and src_disk == arch_disk else ["diskutil", "eject", dev]
    for attempt in range(5):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            log(f"   {src} ejected -- safe to unplug")
            return True
        time.sleep(3)
    log(f"   WARNING: couldn't eject {src}: {(r.stderr or r.stdout).strip()}  (eject it in Finder)")
    return False


# ---------- speed test ----------

def _nocache(fd):
    """Bypass the macOS file cache so we measure the disk, not RAM."""
    try:
        import fcntl
        fcntl.fcntl(fd, getattr(fcntl, "F_NOCACHE", 48), 1)
    except Exception:
        pass


def _bigfiles(root, n=3, min_size=100_000_000, max_entries=200_000):
    """A few large files on the source drive (quick, bounded search)."""
    found, best = [], (0, None)
    for i, (path, st, err) in enumerate(walk(str(root), False)):
        if i > max_entries or len(found) >= n:
            break
        if st is not None and stat.S_ISREG(st.st_mode):
            if st.st_size >= min_size:
                found.append(path)
            elif st.st_size > best[0]:
                best = (st.st_size, path)
    return found or ([best[1]] if best[1] else [])


def _rate(nbytes, secs):
    return nbytes / 1e6 / max(secs, 1e-6)


def speedtest(src, archive_vol, mb=1024):
    chunk = 8 * 1024 * 1024
    print(f"\nSpeed test (each side measured separately, file cache bypassed)\n")
    for label, p in (("source", src), ("archive", archive_vol)):
        i = diskutil(p)
        whole, wd = physical_disk_info(i)
        print(f"  {label:8} {p}\n           {wd.get('MediaName') or i.get('MediaName', '?')} | "
              f"bus {wd.get('BusProtocol') or i.get('BusProtocol', '?')} | "
              f"{'SSD' if wd.get('SolidState') else 'HDD' if wd else '?'} | "
              f"{i.get('FilesystemName', '?')}")

    # 1. read big files from the source
    files = _bigfiles(src)
    total = 0
    t0 = time.time()
    for f in files:
        try:
            fd = os.open(f, os.O_RDONLY)
        except OSError:
            continue
        _nocache(fd)
        try:
            while total < mb * 1e6:
                b = os.read(fd, chunk)
                if not b:
                    break
                total += len(b)
        except OSError as e:
            print(f"  read error in {f}: {e.strerror}")
        os.close(fd)
        if total >= mb * 1e6:
            break
    rd = _rate(total, time.time() - t0) if total else 0
    print(f"\n  READ  from source : {rd:6.0f} MB/s   ({total / 1e6:.0f} MB from {len(files)} large file(s))")

    # 2. write to the archive drive
    tmp = archive_vol / ".speedtest.tmp"
    buf = os.urandom(chunk)
    written = 0
    t0 = time.time()
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        _nocache(fd)
        while written < mb * 1e6:
            written += os.write(fd, buf)
        os.fsync(fd)
        os.close(fd)
        wr = _rate(written, time.time() - t0)
    except OSError as e:
        wr = 0
        print(f"  write error: {e.strerror}")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    print(f"  WRITE to archive  : {wr:6.0f} MB/s   ({written / 1e6:.0f} MB)")

    # 3. other things that slow a copy down
    try:
        md = subprocess.run(["mdutil", "-s", str(archive_vol)], capture_output=True,
                            text=True).stdout.strip().splitlines()[-1].strip()
        print(f"  Spotlight on archive: {md}")
    except Exception:
        pass

    print()
    if rd and wr:
        slow, other = ("source drive", wr) if rd < wr else ("archive drive", rd)
        print(f"  Bottleneck: the {slow} ({min(rd, wr):.0f} MB/s; the other side manages {other:.0f}).")
        if min(rd, wr) < 40:
            print("  Under ~40 MB/s usually means a USB 2.0 connection (max ~35 MB/s in practice),\n"
                  "  an old/failing disk, or a slow SMR portable drive.")
    print("  Copies of many small files run slower than these figures: each file costs\n"
          "  a few disk seeks on both drives, whatever its size.")


# ---------- main ----------

def status_line(archive_vol, copied_bytes, copy_seconds, rate, note):
    free = shutil.disk_usage(archive_vol).free
    print(f"{archive_vol.name}: {free / 1e9:.2f} GB available | copied: {copied_bytes / 1e9:.2f} GB "
          f"in {copy_seconds:.1f} s ({rate:.1f} MB/s) | {note}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Inventory one external drive and copy it to the archive.")
    ap.add_argument("volume", nargs="?", help="source mount point (default: the one external drive)")
    ap.add_argument("-a", "--archive",
                    help="archive volume to copy onto (required): name in /Volumes, or a path")
    ap.add_argument("--folder", default="",
                    help="subfolder of the archive volume to copy into (default: its root)")
    ap.add_argument("--name", help="archive folder name (no prompt; reuses an existing folder)")
    ap.add_argument("--no-copy", action="store_true", help="inventory only")
    ap.add_argument("--redo", action="store_true", help="process a drive already archived")
    ap.add_argument("--reserve-gb", type=float, default=5, help="GB to keep free on the archive drive")
    ap.add_argument("--magic", action="store_true", help="sniff real file types with `file`")
    ap.add_argument("--all", action="store_true", help="include macOS housekeeping dirs")
    ap.add_argument("--no-eject", action="store_true",
                    help="leave the source drive mounted when finished")
    ap.add_argument("--no-readonly", action="store_true",
                    help="don't remount the source drive read-only")
    ap.add_argument("--speedtest", action="store_true",
                    help="measure read speed of the source and write speed of the archive drive, then stop")
    ap.add_argument("--summary-only", action="store_true",
                    help="just rebuild drive_summary.csv (no drive needed)")
    ap.add_argument("-o", "--out", default=PROJECT_DIR, help=argparse.SUPPRESS)
    a = ap.parse_args()

    outdir = Path(a.out).expanduser()
    if a.summary_only:
        write_summary(outdir)
        return

    if not a.archive:
        ap.error('the archive volume is required, e.g.  -a "TAP 4T 2019"')
    archive_vol = archive_volume_path(a.archive)
    if not os.path.ismount(archive_vol):       # never write into /Volumes on the boot disk
        sys.exit(f"Archive drive {archive_vol} is not mounted.")
    folder = a.folder.strip("/")
    archive_root = archive_vol / folder if folder else archive_vol
    archive_root.mkdir(exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)

    amap = load_map(outdir)
    other_archives = {r["archive_volume"] for r in amap.values()} - {archive_vol.name}
    src = Path(a.volume).resolve() if a.volume else find_source(archive_vol, other_archives).resolve()
    if not src.is_dir():
        sys.exit(f"{src} is not a directory.")
    if src == archive_vol.resolve() or archive_vol.resolve() in src.parents:
        sys.exit("That's the archive drive -- refusing to copy it into itself.")
    if not a.no_readonly:
        src = remount_readonly(src).resolve()

    if a.speedtest:
        speedtest(src, archive_vol)
        return

    info = diskutil(src)
    vol_name = info.get("VolumeName") or src.name or "volume"
    uuid = info.get("VolumeUUID") or info.get("DiskUUID") or ""
    key = drive_id(uuid, vol_name)
    log(f"\n== {vol_name}  ({src})")

    prev = amap.get(key)
    if prev and prev.get("status", "").startswith("copied") and not a.redo and not a.no_copy:
        log(f'   already archived on {prev["archive_volume"]} as "{prev["archive_dir"]}" '
            f'(use --redo to process again)')
        write_summary(outdir)
        if not a.no_eject:
            eject(src, archive_vol)
        status_line(archive_vol, 0, 0, 0, f"{vol_name}: skipped, already archived")
        return

    if a.name:
        archive_name = a.name.strip()
        if not name_ok(archive_name):
            sys.exit(f'"{archive_name}" can\'t be used as a folder name.')
        # a name belonging to a DIFFERENT drive would overwrite that drive's <name>.csv
        owner = [r for k, r in amap.items()
                 if k != key and r.get("archive_name", "").lower() == archive_name.lower()]
        if owner:
            sys.exit(f'"{archive_name}" already belongs to another drive ("{owner[0]["volume_name"]}", '
                     f'{owner[0]["archive_volume"]}: {owner[0]["archive_dir"]}). Choose a different --name.')
        archive_dir = f"{folder}/{archive_name}" if folder else archive_name
    elif prev and not a.no_copy and prev["archive_volume"] != archive_vol.name:
        sys.exit(f'This drive was already started on another archive drive: '
                 f'{prev["archive_volume"]}: {prev["archive_dir"]} ({prev["status"]}).\n'
                 f'Mount that drive and run with -a "{prev["archive_volume"]}" to continue there, '
                 f'or add --name NAME to start a separate copy on {archive_vol.name}.')
    elif prev and prev["archive_volume"] == archive_vol.name:
        archive_name, archive_dir = prev["archive_name"], prev["archive_dir"]
        log(f'   continuing into existing archive folder "{archive_dir}"')
    else:
        archive_name = choose_archive_name(vol_name, amap, archive_root)
        archive_dir = f"{folder}/{archive_name}" if folder else archive_name
    dest = archive_vol / archive_dir
    log(f"   archive folder: {dest}")

    now = dt.datetime.now()
    files_csv = outdir / f"{archive_name}.csv"   # same name as the archive folder; a rerun replaces it

    if not a.no_copy:
        amap[key] = {"volume_uuid": uuid, "volume_name": vol_name, "archive_volume": archive_vol.name,
                     "archive_dir": archive_dir, "archive_name": archive_name, "status": "in progress", "updated": now.isoformat(sep=" ", timespec="seconds")}
        save_map(outdir, amap)

    drive, dirs, links = inventory(src, info, files_csv, a.magic, a.all)
    drive.update(inventoried_at=now.isoformat(sep=" ", timespec="seconds"),
                 volume_name=vol_name, volume_uuid=uuid, archive_name=archive_name,
                 archive_volume=archive_vol.name)

    res = {"files_to_copy": 0, "copied_files": 0, "copied_bytes": 0, "skipped_existing": 0,
           "copy_errors": 0, "copy_seconds": 0, "copy_mb_per_s": 0}
    if a.no_copy:
        res["copy_status"] = "not copied (--no-copy)"
    else:
        res.update(copy_drive(src, dest, files_csv, dirs, links, archive_vol, a.reserve_gb,
                              removed_copies(outdir, archive_vol.name)))
        drive["archive_dir"] = str(dest)
    drive.update(res)
    drive["copied_gb"] = gb(res.get("copied_bytes", 0))
    drive["archive_free_gb_after"] = gb(shutil.disk_usage(archive_vol).free)
    append_row(outdir / "drives.csv", drive, DRIVE_FIELDS)

    if not a.no_copy:
        amap[key].update(status=res["copy_status"],
                         updated=dt.datetime.now().isoformat(sep=" ", timespec="seconds"))
        save_map(outdir, amap)

    log(f"   drives.csv updated: {outdir / 'drives.csv'}")
    write_summary(outdir)
    if not a.no_eject:
        eject(src, archive_vol)
    status_line(archive_vol, res["copied_bytes"], res["copy_seconds"], res["copy_mb_per_s"],
                f"{vol_name} -> {archive_name}: {res['copy_status']}")


if __name__ == "__main__":
    main()
