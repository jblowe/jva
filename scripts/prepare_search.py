#!/usr/bin/env python3
"""
prepare_search.py -- add what the search app needs to be fast to archive_catalog.sqlite.

    python3 scripts/prepare_search.py                 # uses the catalogue named in config.json
    python3 scripts/prepare_search.py --catalogue /path/to/archive_catalog.sqlite

identify_archive.py does the same at the end of every run. Run it by hand only if that step
failed, or the first time. Without it the search app still works, just more slowly (it notices
and falls back).

What it adds (nothing existing is changed or removed):
  unique_files.kind   Image / PDF / Document / Video / Audio / Other, stored and indexed
  unique_archives     which archive drives each distinct file is on
  unique_drives       which source drives each distinct file came from
  unique_tags         each distinct file's Site (NPW, NML, TK, NKH, PL, PF) and Season (85, 86,
                      90, 92, 94), from the words in its copies' paths
  search_meta         what the above were built from, so the app can tell when they are stale
  ANALYZE statistics  so SQLite chooses good query plans

Mac "._" files (AppleDouble metadata) are left out of unique_archives and unique_drives, the
same as everywhere in the app. Takes a minute or two on the full catalogue; the search app
can stay running meanwhile.
"""
import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # the repository, for jva
from jva.config import CONFIG  # noqa: E402
from jva.search_prep import prepare  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalogue", default=str(CONFIG.catalogue))
    a = ap.parse_args()
    if not os.path.exists(a.catalogue):
        sys.exit(f"Catalogue not found: {a.catalogue}")
    print(f"Preparing {a.catalogue} for searching")
    db = sqlite3.connect(a.catalogue, timeout=60)
    db.execute("PRAGMA journal_mode=WAL")
    t0 = time.time()
    try:
        n = prepare(db, step=lambda m: print(f"  {m} ({time.time() - t0:.1f}s)", flush=True))
    except RuntimeError as e:
        sys.exit(str(e))
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()
    print(f"Done: {n:,} distinct files, {time.time() - t0:.0f}s. A running search app picks this up by itself.")


if __name__ == "__main__":
    main()
