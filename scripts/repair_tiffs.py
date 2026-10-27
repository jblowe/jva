#!/usr/bin/env python3
"""
repair_tiffs.py -- repair the TIFFs that check_tiffs.py found with a bad internal pointer.

  python3 scripts/repair_tiffs.py              # reads tiff_check.csv, shows what it will do, asks
  python3 scripts/repair_tiffs.py --yes        # no question
  python3 scripts/repair_tiffs.py --undo       # put every repaired file back exactly as it was

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
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # the repository, for jva
from jva.config import CONFIG  # noqa: E402
PROJECT_DIR = CONFIG.data_dir             # catalogue, reports and drive records
SCRIPTS = Path(__file__).resolve().parent
from jva.catalog import TIFF_REPAIRS  # noqa: E402
from jva.tiff import apply_fixes, check_tiff, move_checksum, plan_fixes, sha256_file, undo_fixes  # noqa: E402
from jva import volumes  # noqa: E402

VOLUMES = CONFIG.volumes_root
SCHEMA = TIFF_REPAIRS


def log(*a):
    print(*a, file=sys.stderr, flush=True)


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
    mounted = lambda arch: volumes.is_mounted(arch, a.volumes)

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
