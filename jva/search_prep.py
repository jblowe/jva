"""Helper tables that make the search app fast (run by prepare_search.py, and by
identify_archive.py after each run):

  unique_files.kind   Image / PDF / Document / Video / Audio / Other, stored and indexed
  unique_archives     which archive drives each distinct file is on
  unique_drives       which source drives each distinct file came from
  unique_tags         Site and Season, from the words in each file's paths (jva.kinds)
  search_meta         what they were built from, so the app can tell when they are stale
  ANALYZE statistics  so SQLite chooses good query plans
"""
import sqlite3
import time

from jva.catalog import has_table
from jva.kinds import RULES_VERSION, kind_of, tags_of


def prepare(db, step=lambda msg: None):
    """Build or rebuild everything above; returns the number of distinct files."""
    db.create_function("kind_of", 4, kind_of, deterministic=True)
    cols = {r[1] for r in db.execute("PRAGMA table_info(unique_files)")}
    if not cols:
        raise RuntimeError("The catalogue has no unique_files table yet: run identify_archive.py first.")
    with db:
        if "kind" not in cols:
            db.execute("ALTER TABLE unique_files ADD COLUMN kind TEXT")
        db.execute("UPDATE unique_files SET kind = kind_of(ext, mime, mac_type, mac_creator)")
        db.execute("CREATE INDEX IF NOT EXISTS i_u_kind ON unique_files(kind)")
    step("stored each file's kind")

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
    step("listed which drives each file is on")

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
    step("found each file's Site and Season")

    db.execute("ANALYZE")
    step("collected statistics (ANALYZE)")

    with db:
        db.execute("CREATE TABLE IF NOT EXISTS search_meta (key TEXT PRIMARY KEY, value TEXT)")
        write_meta(db, prepared_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    return db.execute("SELECT COUNT(*) FROM unique_files").fetchone()[0]


def write_meta(db, **extra):
    n, mx = db.execute("SELECT COUNT(*), MAX(id) FROM unique_files").fetchone()
    db.executemany("INSERT OR REPLACE INTO search_meta VALUES (?, ?)",
                   [("version", str(RULES_VERSION)), ("unique_count", str(n)), ("unique_max_id", str(mx))]
                   + [(k, str(v)) for k, v in extra.items()])


def meta_matches(db):
    """True when the helper tables were built from the unique_files there now."""
    if not has_table(db, "search_meta"):
        return False
    meta = dict(db.execute("SELECT key, value FROM search_meta").fetchall())
    n, mx = db.execute("SELECT COUNT(*), MAX(id) FROM unique_files").fetchone()
    return meta.get("unique_count") == str(n) and meta.get("unique_max_id") == str(mx)


def is_prepared(db):
    """True when every helper table exists and matches the current unique_files and kind rules."""
    try:
        if "kind" not in {r[1] for r in db.execute("PRAGMA table_info(unique_files)")}:
            return False
        meta = dict(db.execute("SELECT key, value FROM search_meta").fetchall())
        db.execute("SELECT 1 FROM unique_drives LIMIT 1").fetchall()
        db.execute("SELECT 1 FROM unique_tags LIMIT 1").fetchall()
        return meta_matches(db) and meta.get("version") == str(RULES_VERSION)
    except sqlite3.OperationalError:
        return False
