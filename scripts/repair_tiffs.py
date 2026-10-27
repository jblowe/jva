#!/usr/bin/env python3
"""
repair_tiffs.py -- repair the TIFFs that check_tiffs.py found with a bad internal pointer.

  python3 repair_tiffs.py              # reads tiff_check.csv, shows what it will do, asks
  python3 repair_tiffs.py --yes        # no question
  python3 repair_tiffs.py --undo       # put every repaired file back exactly as it was

What is wrong with these files: a TIFF's directory has entries that point to further
directories -- Exif (camera/scanner details), GPS, Interoperability, sub-images. In these files
one such pointer leads past the end of the file or into the image data. Most programs ignore
it; macOS Preview and Quick Look refuse the whole file.

The repair removes just the bad entry from the directory, in place: the directory gets one
entry fewer (the bytes freed are zeroed), and nothing else in the file changes -- not the
image, colour profile, Photoshop data or XMP, nor the file's date or its "._" companion.
Every copy on the archive drives that are plugged in is repaired (identical copies get
identical repairs, so they stay identical). Each file is checked before and after; the
original bytes are kept in the catalogue (table tiff_repairs), so --undo can restore them.

Because a repaired file's content changes, so does its checksum: the catalogue is updated to
match (copies, preview images and categories move to the new checksum). Copies on drives that
aren't plugged in are left for a later run; until then, identify_archive.py --reports is run
at the end so the catalogue shows them as what they now are -- different files.

Files in tiff_check.csv with other problems ("not a TIFF": mostly Photoshop files named .tif)
are not touched.
"""
import argparse
import csv
import hashlib
import os
import sqlite3
import struct
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # the repository, for jva
from jva.config import CONFIG  # noqa: E402
PROJECT_DIR = CONFIG.data_dir             # catalogue, reports and drive records
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from check_tiffs import MAX_ENTRIES, POINTER_TAGS, TYPE_SIZE, check_tiff  # noqa: E402

VOLUMES = CONFIG.volumes_root
SCHEMA = """
CREATE TABLE IF NOT EXISTS tiff_repairs(
  archive TEXT, path TEXT,
  old_sha256 TEXT, new_sha256 TEXT,
  offset INTEGER, old_bytes BLOB, new_bytes BLOB,   -- one directory rewritten (several rows if several)
  removed TEXT,                                     -- e.g. "Exif pointer"
  repaired_at TEXT);
CREATE INDEX IF NOT EXISTS i_tr_path ON tiff_repairs(archive, path);
"""


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ---------- finding the bad entries ----------

def plan_fixes(path):
    """-> [(offset, old_bytes, new_bytes, [what was removed])] -- the directories to rewrite."""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        def read(off, n):
            if off < 0 or off + n > size:
                return None
            fh.seek(off)
            b = fh.read(n)
            return b if len(b) == n else None

        head = read(0, 8)
        if not head or head[:2] not in (b"II", b"MM"):
            return []
        e = "<" if head[:2] == b"II" else ">"
        if struct.unpack(e + "H", head[2:4])[0] != 42:
            return []

        def directory(off):
            """-> (entries [(tag, typ, cnt, value, raw12)], next, raw block) or None if not one"""
            if not off or off >= size:
                return None
            nb = read(off, 2)
            if nb is None:
                return None
            n = struct.unpack(e + "H", nb)[0]
            if n == 0 or n > MAX_ENTRIES:
                return None
            raw = read(off, 2 + n * 12 + 4)
            if raw is None:
                return None
            ents = []
            for i in range(n):
                r12 = raw[2 + i * 12: 14 + i * 12]
                tag, typ, cnt, v = struct.unpack(e + "HHII", r12)
                ents.append((tag, typ, cnt, v, r12))
            return ents, raw[2 + n * 12:], raw

        def targets(typ, cnt, v):
            sz = TYPE_SIZE.get(typ, 1) * cnt
            if typ not in (4, 13) or cnt == 0:
                return [v] if cnt == 1 else None
            data = struct.pack(e + "I", v) if sz <= 4 else read(v, sz)
            return list(struct.unpack(e + "I" * cnt, data)) if data else None

        def fix(off, ents, nxt, raw, pointer_tags):
            keep, removed = [], []
            for tag, typ, cnt, v, r12 in ents:
                if tag in pointer_tags:
                    ts = targets(typ, cnt, v)
                    if ts is None or any(directory(t) is None for t in ts):
                        removed.append(f"{pointer_tags[tag]} pointer")
                        continue
                keep.append(r12)
            if not removed:
                return None
            new = struct.pack(e + "H", len(keep)) + b"".join(keep) + nxt
            new += b"\0" * (len(raw) - len(new))
            return off, raw, new, removed

        fixes, off, seen = [], struct.unpack(e + "I", head[4:8])[0], set()
        while off and off not in seen and len(seen) < 64:
            seen.add(off)
            d = directory(off)
            if d is None:
                break
            ents, nxt, raw = d
            f = fix(off, ents, nxt, raw, POINTER_TAGS)
            if f:
                fixes.append(f)
            for tag, typ, cnt, v, _ in ents:                  # inside a sound Exif directory:
                if tag == 34665 and not (f and "Exif pointer" in f[3]):   # its Interoperability pointer
                    ex = directory(v)
                    if ex:
                        g = fix(v, ex[0], ex[1], ex[2], {40965: "Interoperability"})
                        if g:
                            fixes.append(g)
            off = struct.unpack(e + "I", nxt[:4])[0]
    return fixes


def apply_fixes(path, fixes):
    st = os.stat(path)
    with open(path, "r+b") as fh:
        for off, _old, new, _ in fixes:
            fh.seek(off)
            fh.write(new)
        fh.flush()
        os.fsync(fh.fileno())
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))      # keep the file's date


def undo_fixes(path, fixes):
    st = os.stat(path)
    with open(path, "r+b") as fh:
        for off, old, _new, _ in reversed(fixes):
            fh.seek(off)
            fh.write(old)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


# ---------- the catalogue ----------

def has_table(db, t):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone() is not None


def move_checksum(db, old, new):
    """Copies whose content changed from `old` to `new`: carry the preview images and the
    category over to the new checksum (kept for the old one while copies with it remain)."""
    still_old = db.execute("SELECT 1 FROM files WHERE sha256=? LIMIT 1", (old,)).fetchone() is not None
    for table in ("derivatives", "content_class"):
        if not has_table(db, table):
            continue
        if db.execute(f"SELECT 1 FROM {table} WHERE sha256=?", (new,)).fetchone():
            continue
        if still_old:
            cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
            sel = ", ".join("?" if c == "sha256" else c for c in cols)
            db.execute(f"INSERT INTO {table} ({', '.join(cols)}) SELECT {sel} FROM {table} WHERE sha256=?",
                       (new, old))
        else:
            db.execute(f"UPDATE {table} SET sha256=? WHERE sha256=?", (new, old))
    if not still_old:
        db.execute("UPDATE unique_files SET sha256=? WHERE sha256=?", (new, old))
    return still_old


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default=None, help="the list from check_tiffs.py (default: tiff_check.csv here)")
    ap.add_argument("--yes", action="store_true", help="repair without asking")
    ap.add_argument("--undo", action="store_true", help="restore every repaired file exactly as it was")
    ap.add_argument("-o", "--out", default=str(PROJECT_DIR), help=argparse.SUPPRESS)
    ap.add_argument("--volumes", default=VOLUMES, help=argparse.SUPPRESS)        # for testing
    a = ap.parse_args()
    out = Path(a.out)
    db = sqlite3.connect(out / "archive_catalog.sqlite", timeout=60)
    db.executescript(SCHEMA)
    mounted = lambda arch: os.path.isdir(os.path.join(a.volumes, arch)) and \
        (a.volumes != VOLUMES or os.path.ismount(os.path.join(a.volumes, arch)))

    if a.undo:
        return undo_all(db, a, mounted)

    rows = list(csv.DictReader(open(a.csv or out / "tiff_check.csv", encoding="utf-8-sig")))
    shas = [r["sha256"] for r in rows if "pointer" in r["problem"]]
    others = len(rows) - len(shas)
    work, absent = [], 0
    for sha in dict.fromkeys(shas):
        copies = db.execute("SELECT archive, path FROM files WHERE sha256=? AND junk=0 ORDER BY archive, path",
                            (sha,)).fetchall()
        here = [c for c in copies if mounted(c[0])]
        absent += len(copies) - len(here)
        if here:
            work.append((sha, here))
    n_copies = sum(len(h) for _, h in work)
    print(f"{len(work):,} damaged TIFFs to repair: {n_copies:,} copies on the drives plugged in"
          + (f" ({absent:,} more copies are on drives not plugged in: repair those later)" if absent else ""))
    if others:
        print(f"   ({others:,} files in the list have other problems and are left alone)")
    if not work:
        sys.exit("Nothing to repair.")
    if not a.yes and input("Repair them? Type yes: ").strip().lower() != "yes":
        sys.exit("Nothing changed.")

    t0, done, failed, partial = time.time(), 0, [], False
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for n, (old, copies) in enumerate(work, 1):
        new, first_fix = None, None
        for archive, path in copies:
            full = os.path.join(a.volumes, archive, path)
            try:
                fixes = plan_fixes(full)
                if not fixes:
                    failed.append((f"{archive}: {path}", "no bad pointer found (already repaired?)"))
                    continue
                apply_fixes(full, fixes)
                if check_tiff(full):                          # still not right: put it back
                    undo_fixes(full, fixes)
                    failed.append((f"{archive}: {path}", "still damaged after the repair; left as it was"))
                    continue
            except OSError as x:
                failed.append((f"{archive}: {path}", str(x)))
                continue
            same = first_fix is not None and [(f[0], f[1], f[2]) for f in fixes] == first_fix
            if new is None or not same:                       # identical copy + identical repair
                new = sha256_file(full)                       # -> identical result
                first_fix = [(f[0], f[1], f[2]) for f in fixes]
            db.executemany("INSERT INTO tiff_repairs VALUES (?,?,?,?,?,?,?,?,?)",
                           [(archive, path, old, new, off, ob, nb, ", ".join(what), now)
                            for off, ob, nb, what in fixes])
            db.execute("UPDATE files SET sha256=? WHERE archive=? AND path=?", (new, archive, path))
            done += 1
        if new:
            partial |= move_checksum(db, old, new)
        db.commit()
        if n % 20 == 0:
            log(f"   {n:,}/{len(work):,} files ...")
    for p, why in failed:
        log(f"   not repaired: {p} -- {why}")
    print(f"Repaired {done:,} copies in {time.time() - t0:.0f} s"
          + (f"; {len(failed):,} not repaired" if failed else "") + ".")
    db.close()
    if partial:
        print("Some damaged copies weren't plugged in, so repaired and unrepaired copies now differ; "
              "updating the catalogue's list of distinct files ...")
        subprocess.run([sys.executable, str(SCRIPTS / "identify_archive.py"), "--reports",
                        "-o", str(out)])
    print("Run check_tiffs.py again to confirm.")


def undo_all(db, a, mounted):
    rows = db.execute("SELECT DISTINCT archive, path, old_sha256 FROM tiff_repairs").fetchall()
    todo = [r for r in rows if mounted(r[0])]
    print(f"{len(rows):,} repaired copies recorded; {len(todo):,} on drives plugged in")
    if not todo or (not a.yes and input("Undo those repairs? Type yes: ").strip().lower() != "yes"):
        sys.exit("Nothing changed.")
    for archive, path, old in todo:
        fixes = [(off, ob, nb, []) for off, ob, nb in db.execute(
            "SELECT offset, old_bytes, new_bytes FROM tiff_repairs WHERE archive=? AND path=? ORDER BY rowid",
            (archive, path))]
        full = os.path.join(a.volumes, archive, path)
        undo_fixes(full, fixes)
        new = db.execute("SELECT sha256 FROM files WHERE archive=? AND path=?", (archive, path)).fetchone()
        db.execute("UPDATE files SET sha256=? WHERE archive=? AND path=?", (old, archive, path))
        db.execute("DELETE FROM tiff_repairs WHERE archive=? AND path=?", (archive, path))
        if new and new[0]:
            move_checksum(db, new[0], old)
        db.commit()
    print(f"Restored {len(todo):,} copies. Run identify_archive.py --reports to refresh the catalogue.")


if __name__ == "__main__":
    main()
