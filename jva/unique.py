"""unique_files: one row per distinct file (identical copies together), built from `files`.

build_unique() rebuilds the whole table (identify_archive.py, after every run);
refresh_unique() brings one distinct file up to date after some of its copies were deleted
or changed (delete_files.py, the search app). Both describe a file the same way (summary()).
build_unique() then groups similar files (jva/similar.py: build_groups).
"""
import os
import sqlite3
import time

from jva.catalog import FTS_COLUMNS, UNIQUE, UNIQUE_COLUMNS, UNIQUE_INDEXES, UNIQUE_SEARCH, has_table, human, log

MAX_LOCATIONS = 100
# the columns of `files` summary() needs, in this order
COPY_COLUMNS = ("id", "sha256", "hash_error", "archive", "drive", "path", "name", "ext", "size", "mtime",
                "modified", "created", "format", "puid", "format_version", "mime", "mac_type", "mac_creator")


def summary(copies, max_locations=MAX_LOCATIONS):
    """The unique_files values (all but id and category) for a group of identical copies,
    each a dict with COPY_COLUMNS. The representative copy is the oldest (then shortest path)."""
    rep = min(copies, key=lambda r: (r["mtime"] or 0, len(r["path"])))
    names = sorted({r["name"] for r in copies} - {rep["name"]}, key=str.lower)
    mods = sorted(r["modified"] for r in copies if r["modified"])
    locs = sorted(f"{r['archive']}: {r['path']}" for r in copies)
    if len(locs) > max_locations:
        locs = locs[:max_locations] + [f"... and {len(locs) - max_locations} more"]
    drives = sorted({f"{r['archive']}: {r['drive']}" for r in copies}, key=str.lower)
    folder = os.path.dirname(rep["path"])
    sha = rep["sha256"]
    return {
        "sha256": sha or "", "name": rep["name"], "other_names": "; ".join(names), "ext": rep["ext"],
        "format": rep["format"] or "", "puid": rep["puid"] or "", "format_version": rep["format_version"] or "",
        "mime": rep["mime"] or "", "mac_type": rep["mac_type"] or "", "mac_creator": rep["mac_creator"] or "",
        "size_bytes": rep["size"], "size": human(rep["size"]),
        "created": min((r["created"] for r in copies if r["created"]), default=""),
        "modified": mods[0] if mods else "", "modified_latest": mods[-1] if mods else "",
        "year": int(mods[0][:4]) if mods else None,
        "copies": len(copies), "drives": len(drives), "volumes": len({r["archive"] for r in copies}),
        "archive": rep["archive"], "path": rep["path"], "folder": f"{rep['archive']}: {folder or '/'}",
        "locations": "; ".join(locs), "drive_list": "; ".join(drives),
        "checksum": "checksummed" if sha else "unreadable" if rep["hash_error"] else "not yet checksummed",
    }


def build_unique(db, max_locations=MAX_LOCATIONS):
    """Rebuild unique_files from files, and point files.unique_id at it. Clutter
    (.DS_Store, empty files, "._" files, ...) is left out; those files get unique_id NULL."""
    t0 = time.time()
    db.executescript(UNIQUE)
    cur = db.execute(f"SELECT COALESCE(sha256, 'f' || id) k, {', '.join(COPY_COLUMNS)} "
                     f"FROM files WHERE junk=0 ORDER BY k")
    db.execute("UPDATE files SET unique_id=NULL")
    cols = [c for c in UNIQUE_COLUMNS if c != "category"]
    insert = f"INSERT INTO unique_files ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    uid, out, links = 0, [], []

    def flush():
        db.executemany(insert, out)
        db.executemany("UPDATE files SET unique_id=? WHERE id=?", links)
        out.clear()
        links.clear()

    def emit(group):
        nonlocal uid
        uid += 1
        s = summary(group, max_locations)
        out.append([uid] + [s[c] for c in cols[1:]])
        links.extend((uid, r["id"]) for r in group)
        if len(out) >= 5000:
            flush()

    group, key = [], None
    for row in cur:
        r = dict(zip(COPY_COLUMNS, row[1:]))
        if row[0] != key and group:
            emit(group)
            group = []
        key = row[0]
        group.append(r)
    if group:
        emit(group)
    flush()
    # subject category, if classify_archive.py has been run
    if has_table(db, "content_class") and has_table(db, "file_class"):
        db.execute("""UPDATE unique_files SET category = COALESCE(
                        (SELECT c.category FROM content_class c WHERE c.sha256 = unique_files.sha256),
                        (SELECT fc.category FROM file_class fc
                          WHERE fc.archive = unique_files.archive AND fc.path = unique_files.path),
                        'Unclassified')""")
    db.executescript(UNIQUE_INDEXES)
    try:
        db.executescript(UNIQUE_SEARCH)
        fts = "with full-text index unique_search"
    except sqlite3.OperationalError as e:
        fts = f"(no full-text index: {e})"
    db.commit()
    n_files = db.execute("SELECT COUNT(*) FROM files WHERE junk=0").fetchone()[0]
    log(f"   unique_files: {uid:,} distinct files (from {n_files:,} non-clutter files) {fts}, "
        f"{time.time() - t0:.0f}s")
    from jva.similar import build_groups          # second level: similar files together
    build_groups(db, log=log)
    return uid


def refresh_unique(db, uid, drop_derivative=None):
    """After copies of distinct file `uid` were deleted: drop it if none are left (and, via
    drop_derivative(db, sha256), its preview images), otherwise recompute its copies,
    locations, representative copy etc. Keeps the search helper tables right too."""
    fts_cols = ", ".join(FTS_COLUMNS)
    old = db.execute(f"SELECT {fts_cols}, sha256 FROM unique_files WHERE id=?", (uid,)).fetchone()
    if old is None:
        return
    fts = has_table(db, "unique_search")
    if fts:
        db.execute(f"INSERT INTO unique_search(unique_search, rowid, {fts_cols}) "
                   f"VALUES ('delete', ?, {', '.join('?' * len(FTS_COLUMNS))})", (uid, *old[:len(FTS_COLUMNS)]))
    sha = old[len(FTS_COLUMNS)]
    copies = [dict(zip(COPY_COLUMNS, r)) for r in db.execute(
        f"SELECT {', '.join(COPY_COLUMNS)} FROM files WHERE unique_id=? AND junk=0", (uid,))]
    for t in ("unique_archives", "unique_drives", "unique_tags"):
        if has_table(db, t):
            db.execute(f"DELETE FROM {t} WHERE unique_id=?", (uid,))
    if not copies:                                           # last copy gone
        from jva.similar import leave_group
        leave_group(db, uid)
        db.execute("DELETE FROM unique_files WHERE id=?", (uid,))
        if sha:
            if has_table(db, "content_class"):
                db.execute("DELETE FROM content_class WHERE sha256=?", (sha,))
            if drop_derivative and has_table(db, "derivatives"):
                drop_derivative(db, sha)
        return
    s = summary(copies)
    keep = [c for c in UNIQUE_COLUMNS if c not in ("id", "category")]
    db.execute(f"UPDATE unique_files SET {', '.join(c + '=?' for c in keep)} WHERE id=?",
               [s[c] for c in keep] + [uid])
    if "kind" in {r[1] for r in db.execute("PRAGMA table_info(unique_files)")}:
        from jva.kinds import kind_of
        db.execute("UPDATE unique_files SET kind=? WHERE id=?",
                   (kind_of(s["ext"], s["mime"], s["mac_type"], s["mac_creator"]), uid))
    if fts:
        new = db.execute(f"SELECT {fts_cols} FROM unique_files WHERE id=?", (uid,)).fetchone()
        db.execute(f"INSERT INTO unique_search(rowid, {fts_cols}) "
                   f"VALUES (?, {', '.join('?' * len(FTS_COLUMNS))})", (uid, *new))
    if has_table(db, "unique_drives"):
        db.executemany("INSERT OR IGNORE INTO unique_drives VALUES (?,?,?)",
                       {(uid, r["archive"], r["drive"]) for r in copies})
    if has_table(db, "unique_archives"):
        db.executemany("INSERT OR IGNORE INTO unique_archives VALUES (?,?)", {(uid, r["archive"]) for r in copies})
    if has_table(db, "unique_tags"):
        from jva.kinds import tags_of
        tags = set()
        for r in copies:
            tags |= tags_of(r["path"])
        db.executemany("INSERT OR IGNORE INTO unique_tags VALUES (?,?,?)", [(f, v, uid) for f, v in tags])
