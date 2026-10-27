#!/usr/bin/env python3
"""
free_space.py -- free space on an archive drive by removing extra copies of identical files,
keeping one copy of each on that drive and a record of every copy removed.

  python3 free_space.py -a "TAP 4T 2019" --target-gb 100            # dry run: shows the plan
  python3 free_space.py -a "TAP 4T 2019" --target-gb 100 --apply    # actually remove

  -a, --archive NAME   the archive drive (required)
  --target-gb N        remove the largest extra copies until N GB are freed
  --all                remove every extra copy (instead of --target-gb)
  --min-mb N           ignore copies smaller than N MB (default 1)
  --apply              without it, nothing is touched: you only get the plan

Which copy stays (one per set of identical files on this drive), in order of preference:
  1. a copy classified TAP (by classify_archive.py, if it has been run)
  2. a copy NOT in a throwaway-looking folder ("Delete when Finished", "dup", "old",
     "Recycled", "already copied", ...), and then preferably not in a "backup" folder
  3. the oldest (by date modified), then the shortest path
Every other copy on the same drive is an "extra" copy.

Safety, for every copy before it's removed:
  * both files must still be there, with the size and date in the catalogue
  * the two files are compared byte for byte (this reads both, so allow time)
  * classic Mac resource forks must match too -- the checksums only cover the main data,
    and on exFAT the resource fork lives in a "._" file next to it. Copies whose resource
    fork differs from the kept copy's are skipped and listed.
In place of each removed copy it leaves a STUB: a tiny text file with exactly the same name,
whose content is the full path of the copy that was kept, e.g.
  /Volumes/TAP 4T 2019/CumulativeArchive/JUDY 3/Voelker TAP Videos/clip.m4v
(--no-stubs leaves nothing). identify_archive.py and the other scripts recognize stubs (via
the record below) and treat them as clutter, never as real files.
Removing a copy also removes its "._" companion. Each removal is recorded in the catalogue
table removed_copies (path, checksum, size, dates, category, and which copy was kept), so
the search app can still show where a file used to be, and drive_inventory.py --redo won't
copy it back. Removed copies of drives copied by drive_inventory.py are also still on the
original source drive.

Writes  free_space_plan.csv  (the plan, or what was done) to this folder.
Don't run it while another script is using the same drive.
"""
import argparse
import csv
import ctypes
import ctypes.util
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_DIR = "/Users/johnlowe/Claude/Projects/Voelker"
CHUNK = 8 * 1024 * 1024

REMOVED_SCHEMA = """
CREATE TABLE IF NOT EXISTS removed_copies(
  archive TEXT, path TEXT, sha256 TEXT, size INTEGER, modified TEXT, created TEXT,
  drive TEXT, category TEXT, kept_path TEXT, removed_at TEXT, reason TEXT,
  PRIMARY KEY(archive, path));
"""


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def make_xattr_reader():
    if sys.platform == "darwin":
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        fn = libc.getxattr
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                       ctypes.c_uint32, ctypes.c_int]
        fn.restype = ctypes.c_ssize_t

        def get(path, name):
            p, n = os.fsencode(path), name.encode()
            size = fn(p, n, None, 0, 0, 1)
            if size < 0:
                return None
            buf = ctypes.create_string_buffer(size)
            got = fn(p, n, buf, size, 0, 1)
            return buf.raw[:got] if got >= 0 else None
        return get

    def get(path, name):                     # Linux, for testing
        try:
            return os.getxattr(path, "user." + name, follow_symlinks=False)
        except (OSError, AttributeError):
            return None
    return get


def same_bytes(a, b):
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            x, y = fa.read(CHUNK), fb.read(CHUNK)
            if x != y:
                return False
            if not x:
                return True


# folder names that suggest a throwaway copy (the file name itself isn't considered)
THROWAWAY = re.compile(r"(?i)\b(delete|deleted|dups?|duplicates?|recycle[dr]?|trash|old|copied|"
                       r"copy of|to delete|temp|tmp|already copied|can delete|do not use)\b")
BACKUPISH = re.compile(r"(?i)\b(backups?|backed up|back up)\b")


def keeper_rank(path, mtime, catmap):
    """Lower = better copy to keep: TAP-classified first, then not in a throwaway-looking
    folder (then not in a backup-looking one), then the oldest, then the shortest path."""
    folders = "/".join(path.split("/")[:-1])
    return (catmap.get(path) != "TAP",
            2 if THROWAWAY.search(folders) else 1 if BACKUPISH.search(folders) else 0,
            mtime or 0, len(path), path)


def plan(db, archive, min_bytes, catmap):
    """Extra copies on this drive, largest first:
    (size, sha, path, mtime, modified, created, drive, keep_path)"""
    rows = db.execute("""
        SELECT f.sha256, f.size, f.path, f.mtime, f.modified, f.created, f.drive
        FROM files f
        WHERE f.archive=? AND f.junk=0 AND f.size >= ? AND substr(f.name, 1, 2) <> '._'
          AND f.sha256 IN (SELECT sha256 FROM files WHERE archive=? AND junk=0 AND sha256 IS NOT NULL
                           AND substr(name, 1, 2) <> '._' GROUP BY sha256 HAVING COUNT(*) > 1)""",
                      (archive, min_bytes, archive)).fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(r[0], []).append(r)
    extra = []
    for sha, g in groups.items():
        g.sort(key=lambda r: keeper_rank(r[2], r[3], catmap))
        keep = g[0][2]
        for _, size, path, mtime, modified, created, drive in g[1:]:
            extra.append((size, sha, path, mtime, modified, created, drive, keep))
    extra.sort(key=lambda e: -e[0])
    return extra


def main():
    ap = argparse.ArgumentParser(description="Remove extra copies of identical files on an archive drive.")
    ap.add_argument("-a", "--archive", required=True, help="archive drive: name in /Volumes, or a path")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--target-gb", type=float, help="free about this many GB, largest copies first")
    g.add_argument("--all", action="store_true", help="remove every extra copy")
    ap.add_argument("--min-mb", type=float, default=1, help="ignore copies smaller than this (default 1)")
    ap.add_argument("--apply", action="store_true", help="actually remove (default: dry run)")
    ap.add_argument("--no-stubs", action="store_true",
                    help="don't leave a stub file in place of each removed copy")
    ap.add_argument("-o", "--out", default=PROJECT_DIR, help=argparse.SUPPRESS)
    a = ap.parse_args()

    vol = Path(a.archive) if a.archive.startswith("/") else Path("/Volumes") / a.archive
    archive = vol.name
    outdir = Path(a.out).expanduser()
    db = sqlite3.connect(outdir / "archive_catalog.sqlite")
    db.executescript(REMOVED_SCHEMA)
    have = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    catmap = {}
    if "file_class" in have:
        catmap = {p: c for p, c in db.execute("SELECT path, category FROM file_class WHERE archive=?",
                                              (archive,))}

    extra = plan(db, archive, int(a.min_mb * 1e6), catmap)
    total_extra = sum(e[0] for e in extra)
    target = None if a.all else a.target_gb * 1e9
    chosen, acc = [], 0
    for e in extra:
        if target is not None and acc >= target:
            break
        chosen.append(e)
        acc += e[0]
    log(f"== {archive}: {len(extra):,} extra copies ({total_extra / 1e9:,.1f} GB) of files that "
        f"also have a kept copy on this drive")
    log(f"   plan: {len(chosen):,} copies, {acc / 1e9:,.1f} GB" +
        ("" if a.apply else "   (dry run -- add --apply to remove them)"))

    done = skipped = freed = 0
    results = []
    if a.apply:
        if not os.path.ismount(vol):
            sys.exit(f"{vol} is not mounted.")
        xattr = make_xattr_reader()
        free_before = os.statvfs(vol)
        t0 = last = time.time()
        for i, (size, sha, path, mtime, modified, created, drive, kept) in enumerate(chosen, 1):
            src, keep = vol / path, vol / kept
            why = None
            try:
                st, sk = os.stat(src), os.stat(keep)
                if st.st_size != size or abs(st.st_mtime - mtime) > 2:
                    why = "changed since it was catalogued"
                elif sk.st_size != size:
                    why = "kept copy changed since it was catalogued"
                elif xattr(src, "com.apple.ResourceFork") != xattr(keep, "com.apple.ResourceFork"):
                    why = "resource fork differs from the kept copy"
                elif not same_bytes(src, keep):
                    why = "contents differ from the kept copy"
            except FileNotFoundError as e:
                why = f"missing: {e.filename}"
            except OSError as e:
                why = f"can't read: {e.strerror}"
            if why:
                skipped += 1
                results.append((path, size, kept, "skipped: " + why))
                log(f"   skipped {path}: {why}")
                continue
            try:
                os.unlink(src)
                ad = src.with_name("._" + src.name)            # its exFAT metadata companion
                if ad.exists():
                    os.unlink(ad)
                    db.execute("DELETE FROM files WHERE archive=? AND path=?",
                               (archive, str(Path(path).with_name("._" + src.name))))
            except OSError as e:
                skipped += 1
                results.append((path, size, kept, f"skipped: can't remove: {e.strerror}"))
                continue
            stub_note = ""
            if not a.no_stubs:
                try:                # same name, containing the path of the copy that was kept
                    src.write_text(f"{vol / kept}\n", encoding="utf-8")
                except OSError as e:
                    stub_note = f" (stub not written: {e.strerror})"
                    log(f"   couldn't write stub for {path}: {e.strerror}")
            db.execute("INSERT OR REPLACE INTO removed_copies VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (archive, path, sha, size, modified, created, drive, catmap.get(path, ""),
                        kept, time.strftime("%Y-%m-%d %H:%M:%S"), "extra copy (free_space.py)"))
            db.execute("DELETE FROM files WHERE archive=? AND path=?", (archive, path))
            if "file_class" in have:
                db.execute("DELETE FROM file_class WHERE archive=? AND path=?", (archive, path))
            db.commit()
            done += 1
            freed += size
            results.append((path, size, kept, "removed" + ("" if a.no_stubs else ", stub left") + stub_note))
            if time.time() - last > 30:
                last = time.time()
                log(f"   {i:,}/{len(chosen):,}: removed {done:,} ({freed / 1e9:.1f} GB), skipped {skipped:,}")
        free_after = os.statvfs(vol)
        gained = (free_after.f_bavail - free_before.f_bavail) * free_after.f_frsize
        log(f"   done in {time.time() - t0:.0f} s; drive now has {gained / 1e9:,.1f} GB more free space")
    else:
        results = [(e[2], e[0], e[7], "planned") for e in chosen]

    with open(outdir / "free_space_plan.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "size_gb", "cumulative_gb", "remove_path", "kept_path", "category", "result"])
        c = 0
        for i, (p, s, k, r) in enumerate(results, 1):
            c += s if (r == "planned" or r.startswith("removed")) else 0
            w.writerow([i, round(s / 1e9, 6), round(c / 1e9, 3), p, k, catmap.get(p, ""), r])
    db.commit()
    db.close()
    if a.apply:
        print(f"{archive}: removed {done:,} extra copies, {freed / 1e9:,.1f} GB; skipped {skipped:,} "
              f"(see free_space_plan.csv). Run identify_archive.py --reports to "
              f"refresh unique_files.", flush=True)
    else:
        print(f"{archive}: dry run -- would remove {len(chosen):,} copies, {acc / 1e9:,.1f} GB "
              f"(see free_space_plan.csv)", flush=True)


if __name__ == "__main__":
    main()
