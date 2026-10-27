#!/usr/bin/env python3
"""
prepare_search.py -- add what the search app needs to be fast to archive_catalog.sqlite.

    python3 prepare_search.py                 # uses the catalogue named in config.json
    python3 prepare_search.py --catalogue /path/to/archive_catalog.sqlite

identify_archive.py runs it for you at the end of every run. Run it by hand only if that step
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
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from kinds import RULES_VERSION, kind_of, tags_of  # noqa: E402



def default_catalogue():
    p = Path(os.environ.get("ARCHIVE_SEARCH_CONFIG", HERE / "config.json"))
    cat = "../archive_catalog.sqlite"
    if p.exists():
        cat = json.loads(p.read_text(encoding="utf-8")).get("catalogue", cat)
    return str(cat if os.path.isabs(cat) else (HERE / cat).resolve())


def step(msg, t0):
    print(f"  {msg} ({time.time() - t0:.1f}s)", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalogue", default=default_catalogue())
    a = ap.parse_args()
    if not os.path.exists(a.catalogue):
        sys.exit(f"Catalogue not found: {a.catalogue}")
    print(f"Preparing {a.catalogue} for searching")
    db = sqlite3.connect(a.catalogue, timeout=60)
    db.execute("PRAGMA journal_mode=WAL")
    db.create_function("kind_of", 4, kind_of, deterministic=True)
    t0 = time.time()

    cols = {r[1] for r in db.execute("PRAGMA table_info(unique_files)")}
    if not cols:
        sys.exit("The catalogue has no unique_files table yet: run identify_archive.py first.")
    with db:
        if "kind" not in cols:
            db.execute("ALTER TABLE unique_files ADD COLUMN kind TEXT")
        db.execute("UPDATE unique_files SET kind = kind_of(ext, mime, mac_type, mac_creator)")
        db.execute("CREATE INDEX IF NOT EXISTS i_u_kind ON unique_files(kind)")
    step("stored each file's kind", t0)

    with db:
        db.executescript("""
            DROP TABLE IF EXISTS unique_archives;
            DROP TABLE IF EXISTS unique_drives;
            CREATE TABLE unique_archives (unique_id INTEGER, archive TEXT,
                                          PRIMARY KEY (unique_id, archive)) WITHOUT ROWID;
            CREATE TABLE unique_drives (unique_id INTEGER, archive TEXT, drive TEXT,
                                        PRIMARY KEY (unique_id, archive, drive)) WITHOUT ROWID;
            INSERT INTO unique_drives
              SELECT DISTINCT f.unique_id, f.archive, f.drive
              FROM files f JOIN unique_files u ON u.id = f.unique_id
              WHERE substr(u.name, 1, 2) <> '._';
            INSERT INTO unique_archives SELECT DISTINCT unique_id, archive FROM unique_drives;
            CREATE INDEX i_ua_archive ON unique_archives(archive, unique_id);
            CREATE INDEX i_ud_drive ON unique_drives(archive, drive, unique_id);""")
    step("listed which drives each file is on", t0)

    # Site and Season: from the words in every copy's path (see kinds.tags_of)
    with db:
        db.executescript("""
            DROP TABLE IF EXISTS unique_tags;
            CREATE TABLE unique_tags (facet TEXT, value TEXT, unique_id INTEGER,
                                      PRIMARY KEY (facet, value, unique_id)) WITHOUT ROWID;""")
        batch = set()
        for uid, path in db.execute("SELECT f.unique_id, f.path FROM files f WHERE f.unique_id IS NOT NULL"):
            for facet, value in tags_of(path):
                batch.add((facet, value, uid))
            if len(batch) > 50000:
                db.executemany("INSERT OR IGNORE INTO unique_tags VALUES (?, ?, ?)", batch)
                batch.clear()
        db.executemany("INSERT OR IGNORE INTO unique_tags VALUES (?, ?, ?)", batch)
        db.execute("CREATE INDEX i_ut_uid ON unique_tags(unique_id, facet, value)")
    step("found each file's Site and Season", t0)

    db.execute("ANALYZE")
    step("collected statistics (ANALYZE)", t0)

    n, mx = db.execute("SELECT COUNT(*), MAX(id) FROM unique_files").fetchone()
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS search_meta (key TEXT PRIMARY KEY, value TEXT)")
        db.executemany("INSERT OR REPLACE INTO search_meta VALUES (?, ?)", [
            ("version", str(RULES_VERSION)), ("unique_count", str(n)), ("unique_max_id", str(mx)),
            ("prepared_at", time.strftime("%Y-%m-%d %H:%M:%S"))])
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()
    print(f"Done: {n:,} distinct files, {time.time() - t0:.0f}s. A running search app picks this up by itself.")


if __name__ == "__main__":
    main()
