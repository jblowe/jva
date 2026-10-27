#!/usr/bin/env python3
"""
check_tiffs.py -- find damaged TIFF files in the archive.

  python3 scripts/check_tiffs.py                    # every TIFF on the archive drives plugged in
  python3 scripts/check_tiffs.py -a "TAP PROJECT"   # only copies on this archive drive
  python3 scripts/check_tiffs.py --file x.tif       # just one file, with details

For each distinct TIFF in the catalogue (identical copies are checked once, using a copy on a
drive that is plugged in) it reads the file's header and directory -- not the image itself,
so it is quick -- and checks that:
  * the header and every directory ("IFD") are where they say, inside the file
  * the image data (strips or tiles) lies inside the file
  * the pointers to the Exif, GPS, Interoperability and sub-image directories lead to a real
    directory, not into the image data or past the end of the file. A bad pointer of this kind
    is what stops macOS Preview and Quick Look from opening a file that other programs read
    fine (e.g. "Tap 86 Rol 25 #13 Sq A.tif": its Exif pointer lands in the pixel data).
Writes tiff_check.csv here: one row per TIFF with a problem (what is wrong, where the copy
is, how many copies there are), and prints a summary. Reads only; changes nothing.
"""
import argparse
import csv
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # the repository, for jva
from jva.config import CONFIG  # noqa: E402
from jva.tiff import check_tiff  # noqa: E402
from jva import volumes  # noqa: E402
PROJECT_DIR = CONFIG.data_dir             # catalogue, reports and drive records
VOLUMES = CONFIG.volumes_root


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-a", "--archive", help="only copies on this archive drive")
    ap.add_argument("--file", help="check just this one file and show the result")
    ap.add_argument("-o", "--out", default=str(PROJECT_DIR), help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.file:
        pr = check_tiff(a.file)
        print("\n".join(pr) if pr else "OK: no structural problems found")
        sys.exit(1 if pr else 0)

    out = Path(a.out)
    db = sqlite3.connect(f"file:{out / 'archive_catalog.sqlite'}?mode=ro", uri=True)
    mounted = {r[0] for r in db.execute("SELECT name FROM archives") if volumes.is_mounted(r[0])}
    if a.archive:
        mounted &= {a.archive}
    if not mounted:
        sys.exit("No archive drive from the catalogue is plugged in." if not a.archive
                 else f'"{a.archive}" is not plugged in.')
    rows = db.execute("""
        SELECT f.sha256, f.archive, f.path, f.size, u.copies, COALESCE(u.category, '')
        FROM files f LEFT JOIN unique_files u ON u.id = f.unique_id
        WHERE f.junk = 0 AND f.sha256 IS NOT NULL AND substr(f.name, 1, 2) <> '._'
          AND (lower(f.ext) IN ('.tif', '.tiff') OR f.mac_type = 'TIFF' OR f.mime = 'image/tiff'
               OR f.puid IN ('fmt/353', 'fmt/152', 'fmt/153', 'fmt/154', 'fmt/155', 'fmt/156',
                             'x-fmt/387', 'x-fmt/388', 'x-fmt/399'))
        ORDER BY f.archive, f.path""").fetchall()
    todo = {}
    for sha, archive, path, size, copies, cat in rows:      # one copy per distinct file
        if archive in mounted and sha not in todo:
            todo[sha] = (archive, path, size, copies or 1, cat)
    skipped = len({r[0] for r in rows}) - len(todo)
    print(f"{len(todo):,} distinct TIFFs to check on {', '.join(sorted(mounted))}"
          + (f" ({skipped:,} more have no copy on a drive that's plugged in)" if skipped else ""))

    bad, missing, t0, last = [], 0, time.time(), time.time()
    for i, (sha, (archive, path, size, copies, cat)) in enumerate(todo.items(), 1):
        full = os.path.join(VOLUMES, archive, path)
        try:
            pr = check_tiff(full)
        except FileNotFoundError:
            missing += 1
            continue
        except OSError as x:
            pr = [f"could not be read: {x}"]
        if pr:
            bad.append([archive, path, size, copies, cat, "; ".join(pr), sha])
        if time.time() - last > 20:
            last = time.time()
            print(f"   {i:,}/{len(todo):,} checked, {len(bad):,} with problems ...", flush=True)
    bad.sort(key=lambda r: (r[0], r[1]))
    with open(out / "tiff_check.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["archive_volume", "path", "bytes", "copies", "category", "problem", "sha256"])
        w.writerows(bad)
    kinds = {}
    for r in bad:
        k = r[5].split(":")[0]
        kinds[k] = kinds.get(k, 0) + 1
    print(f"{len(todo) - missing:,} checked in {time.time() - t0:.0f} s: {len(bad):,} with problems"
          + (f", {missing:,} no longer on the drive" if missing else "") + " -> tiff_check.csv")
    for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]):
        print(f"   {n:6,}  {k}")


if __name__ == "__main__":
    main()
