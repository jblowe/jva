#!/usr/bin/env python3
"""
delete_files.py -- delete files from the archive drives and keep the catalogue up to date.

  python3 scripts/delete_files.py to_delete.txt                      # shows what would go, asks, deletes
  python3 scripts/delete_files.py to_delete.txt --reason "personal"  # recorded with each deletion
  python3 scripts/delete_files.py to_delete.txt --yes                # no question

The list is a text file with one path per line (blank lines and lines starting with # are
ignored), or a CSV file with a "path" column (and optionally an "archive" column). Each path
can be written any of these ways:
  /Volumes/TAP 4T 2019/CumulativeArchive/JUDY 3/old stuff/letter.doc
  TAP 4T 2019: CumulativeArchive/JUDY 3/old stuff/letter.doc       (as the search app shows it)
  CumulativeArchive/JUDY 3/old stuff/letter.doc                      (if only one drive has it)
A FOLDER deletes everything in it. The archive drive(s) must be plugged in.

Deleting a file also deletes its "._" companion (exFAT's Mac metadata), and folders that are
left empty (or hold only .DS_Store). The catalogue is updated at once: the copies disappear
from `files` and from the search app; a file whose last copy is gone disappears entirely,
with its preview images. Each deletion is recorded in the `deleted_files` table (what, when,
why), so drive_inventory.py --redo does not copy it back.

Before deleting, it writes delete_plan.csv: every file, its size and category, and how many
copies of it remain elsewhere -- "last copy" means the archive will no longer have it at all.
The search app's Delete buttons use the same code.
"""
import argparse
import csv
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # the repository, for jva
from jva.config import CONFIG  # noqa: E402
from jva.catalog import human, log  # noqa: E402
from jva.deletion import SCHEMA, delete_copies, plan, read_list, resolve, targets_for, volume_problem  # noqa: E402

PROJECT_DIR = CONFIG.data_dir             # catalogue, reports and drive records
VOLUMES = CONFIG.volumes_root


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("list", help="text or CSV file of paths to delete")
    ap.add_argument("--reason", default="", help="recorded with each deletion")
    ap.add_argument("--yes", action="store_true", help="delete without asking")
    ap.add_argument("-o", "--out", default=str(PROJECT_DIR), help=argparse.SUPPRESS)
    ap.add_argument("--volumes", default=VOLUMES, help=argparse.SUPPRESS)      # for testing
    a = ap.parse_args()
    out = Path(a.out)
    db = sqlite3.connect(out / "archive_catalog.sqlite", timeout=60)
    db.executescript(SCHEMA)

    targets, problems = [], []
    for archive, text in read_list(a.list):
        arch, path, why = resolve(db, archive, text, a.volumes)
        if why:
            problems.append((text, why))
            continue
        rows = targets_for(db, arch, path)
        if not rows:
            problems.append((text, "not in the catalogue"))
        targets += rows
    seen, uniq = set(), []
    for t in targets:                          # a file listed twice (or inside a listed folder)
        if t[0] not in seen:
            seen.add(t[0])
            uniq.append(t)
    files, clutter = plan(db, uniq)
    for text, why in problems:
        log(f"   skipped: {text} -- {why}")
    if not files and not clutter:
        sys.exit("Nothing to delete.")

    with open(out / "delete_plan.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["archive", "path", "bytes", "size", "category", "copies_left", "last_copy"])
        for f in files:
            w.writerow([f["archive"], f["path"], f["size"], human(f["size"]), f["category"],
                        f["copies_left"], "yes" if f["copies_left"] == 0 else ""])
    last = [f for f in files if f["copies_left"] == 0]
    total = sum(f["size"] for f in files)
    drives = sorted({f["archive"] for f in files} | {t[1] for t in clutter})
    print(f"{len(files):,} files, {human(total)}, on {', '.join(drives)} (list: delete_plan.csv)")
    print(f"   {len(last):,} of them ({human(sum(f['size'] for f in last))}) are the LAST copy in the archive"
          if last else "   every one of them has another copy in the archive that stays")
    if clutter:
        print(f"   plus {len(clutter):,} clutter/._ files in the same places")
    for arch in drives:
        why = volume_problem(db, arch, a.volumes)
        if why:
            sys.exit(f"Can't delete: {why}.")
    if not a.yes:
        if input("Delete them? Type yes: ").strip().lower() != "yes":
            sys.exit("Nothing deleted.")
    t0 = time.time()
    clutter_files = [{"id": t[0], "archive": t[1], "path": t[2], "name": t[3], "size": t[4],
                      "sha256": t[5], "unique_id": t[6], "junk": 1} for t in clutter]
    done, failed, freed = delete_copies(db, files + clutter_files, a.reason, "delete_files.py", a.volumes,
                                        progress=lambda i, n: log(f"   {i:,}/{n:,} ..."))
    for p, why in failed[:20]:
        log(f"   failed: {p} -- {why}")
    if len(failed) > 20:
        log(f"   ... and {len(failed) - 20:,} more failures")
    db.execute("PRAGMA wal_checkpoint(PASSIVE)")
    db.close()
    print(f"Deleted {done:,} files ({human(freed)}) in {time.time() - t0:.0f} s"
          + (f"; {len(failed):,} could not be deleted" if failed else "")
          + ". The catalogue and the search app are up to date.")


if __name__ == "__main__":
    main()
