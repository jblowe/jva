"""Deleting files from the archive drives and keeping the catalogue right (used by
scripts/delete_files.py and the search app's Delete buttons).

A file goes together with its "._" companion (exFAT's Mac metadata), and folders left empty
(or holding only .DS_Store) go too. The catalogue is updated at once (files, unique_files,
the search tables, previews of files with no copies left), and each deletion is recorded in
deleted_files, so drive_inventory.py --redo never copies it back.
"""
import csv
import os
import time
from pathlib import Path

from jva import volumes
from jva.catalog import DELETED_FILES, has_table
from jva.search_prep import meta_matches, write_meta
from jva.unique import refresh_unique as _refresh

CLUTTER = {".DS_Store", "._.DS_Store"}
SCHEMA = DELETED_FILES


def volume_problem(db, archive, volumes_root=None):
    return volumes.problem(db, archive, volumes_root)


def read_list(path):
    """-> [(archive or None, path as written)]"""
    p = Path(path)
    with open(p, newline="", encoding="utf-8-sig") as fh:
        text = fh.read()
    lines = text.splitlines()
    first = lines[0] if lines else ""
    if p.suffix.lower() == ".csv" or first.lower().startswith(("path,", "archive,", '"path"', '"archive"')):
        rows = csv.DictReader(lines)
        return [((r.get("archive") or "").strip() or None, (r.get("path") or "").strip())
                for r in rows if (r.get("path") or "").strip()]
    return [(None, l.strip()) for l in lines if l.strip() and not l.lstrip().startswith("#")]


def resolve(db, archive, text, volumes_root=None):
    """One line of the list -> (archive, archive-relative path, None) or (None, None, problem)."""
    archives = [r[0] for r in db.execute("SELECT name FROM archives")]
    t = text.strip().rstrip("/")
    vr = (volumes_root or volumes.root()).rstrip("/") + "/"
    if t.startswith(vr):                                      # /Volumes/<archive>/<path>
        rest = t[len(vr):]
        for a in sorted(archives, key=len, reverse=True):
            if rest == a or rest.startswith(a + "/"):
                return a, rest[len(a) + 1:], None
        return None, None, "not on a catalogued archive drive"
    for a in sorted(archives, key=len, reverse=True):         # "<archive>: <path>"
        if t.startswith(a + ": "):
            return a, t[len(a) + 2:], None
    if archive:
        return archive, t, None
    have = [a for (a,) in db.execute(
        "SELECT DISTINCT archive FROM files WHERE path = ? OR (path >= ? AND path < ?)",
        (t, t + "/", t + "0"))]
    if len(have) == 1:
        return have[0], t, None
    if not have:
        return None, None, "not in the catalogue"
    return None, None, f"on more than one archive drive ({', '.join(have)}): write it as \"archive: path\""


def targets_for(db, archive, path):
    """Catalogued files for one path: the file itself, or everything under a folder."""
    rows = db.execute("SELECT id, archive, path, name, size, sha256, unique_id, junk FROM files "
                      "WHERE archive=? AND path=?", (archive, path)).fetchall()
    if rows:
        return rows
    return db.execute("SELECT id, archive, path, name, size, sha256, unique_id, junk FROM files "
                      "WHERE archive=? AND path >= ? AND path < ? ORDER BY path",
                      (archive, path + "/", path + "0")).fetchall()


def plan(db, targets):
    """-> list of dicts, one per file to delete (clutter and "._" companions are deleted with
    their files and not listed), with how many copies of it stay in the archive."""
    ids = {t[0] for t in targets}
    out = []
    for fid, archive, path, name, size, sha, uid, junk in targets:
        if junk or name.startswith("._"):
            continue
        left = 0
        cat = ""
        if uid is not None:
            left = sum(1 for (i,) in db.execute("SELECT id FROM files WHERE unique_id=? AND junk=0", (uid,))
                       if i not in ids)
            r = db.execute("SELECT category FROM unique_files WHERE id=?", (uid,)).fetchone()
            cat = (r[0] if r else "") or ""
        out.append({"id": fid, "archive": archive, "path": path, "name": name, "size": size or 0,
                    "sha256": sha or "", "unique_id": uid, "category": cat, "copies_left": left})
    clutter = [t for t in targets if t[7] or t[3].startswith("._")]
    return out, clutter


# ---------- deleting ----------

def _unlink(p):
    try:
        os.remove(p)
        return True
    except FileNotFoundError:
        return False


def _catalogue_rows_gone(db, archive, paths):
    db.executemany("DELETE FROM files WHERE archive=? AND path=?", [(archive, p) for p in paths])
    if has_table(db, "file_class"):
        db.executemany("DELETE FROM file_class WHERE archive=? AND path=?", [(archive, p) for p in paths])


def remove_empty_folders(db, archive, rel_dir, volumes_root=None):
    """Remove folders left empty (or holding only .DS_Store / ._.DS_Store), walking up, but
    never a drive's top-level folders (e.g. "CumulativeArchive/JUDY 3")."""
    root = volumes.path_of(archive, volumes_root)
    while rel_dir:
        parts = rel_dir.split("/")
        if len(parts) < 2 or (parts[0] == "CumulativeArchive" and len(parts) < 3):
            return
        d = root / rel_dir
        try:
            names = set(os.listdir(d))
        except OSError:
            return
        if not names <= CLUTTER:
            return
        for n in names:
            _unlink(d / n)
        try:
            os.rmdir(d)
        except OSError:
            return
        parent, base = os.path.split(rel_dir)
        _unlink(root / parent / f"._{base}")                   # the folder's own "._" companion
        _catalogue_rows_gone(db, archive, [f"{rel_dir}/{n}" for n in names] +
                             [f"{parent}/._{base}" if parent else f"._{base}"])
        rel_dir = parent


def delete_copies(db, files, reason="", by="delete_files.py", volumes_root=None, progress=None):
    """Delete these catalogued files (rows of `files`: dicts with id/archive/path/...; clutter
    included) from disk with their "._" companions, then update the catalogue. Returns
    (deleted, failed: [(path, why)], bytes freed)."""
    db.executescript(SCHEMA)
    prepared_before = meta_matches(db)
    touched, done, failed, freed = set(), 0, [], 0
    folders = {}
    problems = {}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for i, f in enumerate(files):
        archive, path = f["archive"], f["path"]
        if archive not in problems:
            problems[archive] = volume_problem(db, archive, volumes_root)
        if problems[archive]:
            failed.append((f"{archive}: {path}", problems[archive]))
            continue
        full = volumes.path_of(archive, volumes_root) / path
        parent, name = os.path.split(path)
        companion = f"{parent}/._{name}" if parent else f"._{name}"
        try:
            existed = _unlink(full)
            _unlink(volumes.path_of(archive, volumes_root) / companion)
        except OSError as e:
            failed.append((f"{archive}: {path}", str(e)))
            continue
        if not name.startswith("._") and not f.get("junk"):
            db.execute("INSERT OR REPLACE INTO deleted_files VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (archive, path, name, f.get("size"), f.get("sha256") or None, f.get("category") or None,
                        f.get("copies_left"), reason or None, now, by))
            done += 1
            if existed:
                freed += f.get("size") or 0
        _catalogue_rows_gone(db, archive, [path, companion])
        if f.get("unique_id") is not None:
            touched.add(f["unique_id"])
        folders.setdefault(archive, set()).add(parent)
        if progress and i % 200 == 199:
            db.commit()
            progress(i + 1, len(files))
    for archive, dirs in folders.items():
        for d in sorted(dirs, key=lambda d: -d.count("/")):     # deepest first
            remove_empty_folders(db, archive, d, volumes_root)
    for uid in touched:
        _refresh(db, uid, drop_derivative)
    if prepared_before:
        write_meta(db)
    db.commit()
    return done, failed, freed


def drop_derivative(db, sha):
    """Preview images (and PDF/RTF copies) of a file no longer in the archive."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(derivatives)")}
    want = [c for c in ("root", "thumb", "preview", "pdf", "rtf") if c in cols]
    d = db.execute(f"SELECT {', '.join(want)} FROM derivatives WHERE sha256=?", (sha,)).fetchone()
    if d is None:
        return
    d = dict(zip(want, d))
    for k in ("thumb", "preview", "pdf", "rtf"):
        rel = d.get(k)
        if rel and not rel.startswith("placeholder/") and d.get("root"):
            try:
                _unlink(Path(d["root"]) / rel)
            except OSError:
                pass
    db.execute("DELETE FROM derivatives WHERE sha256=?", (sha,))


def delete_unique(db, uid, reason="", by="search app", volumes_root=None):
    """All copies of one distinct file (used by the search app)."""
    rows = db.execute("SELECT id, archive, path, name, size, sha256, unique_id, junk FROM files "
                      "WHERE unique_id=?", (uid,)).fetchall()
    files, _ = plan(db, rows)
    return delete_copies(db, files, reason, by, volumes_root)


def delete_copy(db, file_id, reason="", by="search app", volumes_root=None):
    """One copy (one row of `files`), used by the search app."""
    rows = db.execute("SELECT id, archive, path, name, size, sha256, unique_id, junk FROM files "
                      "WHERE id=?", (file_id,)).fetchall()
    files, _ = plan(db, rows)
    return delete_copies(db, files, reason, by, volumes_root)


