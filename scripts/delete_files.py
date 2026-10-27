#!/usr/bin/env python3
"""
delete_files.py -- delete files from the archive drives and keep the catalogue up to date.

  python3 delete_files.py to_delete.txt                      # shows what would go, asks, deletes
  python3 delete_files.py to_delete.txt --reason "personal"  # recorded with each deletion
  python3 delete_files.py to_delete.txt --yes                # no question

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
import os
import plistlib
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # the repository, for jva
from jva.config import CONFIG  # noqa: E402
PROJECT_DIR = CONFIG.data_dir             # catalogue, reports and drive records
VOLUMES = CONFIG.volumes_root
CLUTTER = {".DS_Store", "._.DS_Store"}
MAX_LOCATIONS = 100

SCHEMA = """
CREATE TABLE IF NOT EXISTS deleted_files(
  archive TEXT, path TEXT,          -- where the deleted copy was
  name TEXT, size INTEGER, sha256 TEXT, category TEXT,
  copies_left INTEGER,              -- copies of the same file still in the archive afterwards
  reason TEXT, deleted_at TEXT, deleted_by TEXT,   -- deleted_by: delete_files.py or search app
  PRIMARY KEY (archive, path));
"""


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def human(n):
    n = float(n or 0)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{int(n)} bytes" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1000


def has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


# ---------- checks ----------

_uuid_cache = {}


def volume_problem(db, archive, volumes_root=VOLUMES):
    """None if the archive drive is plugged in and is the drive the catalogue knows (by its
    volume UUID, so a different drive with the same name can't be touched); else why not."""
    root = Path(volumes_root) / archive
    if not os.path.ismount(root) and not (root.is_dir() and volumes_root != VOLUMES):
        return f'"{archive}" is not plugged in'
    if sys.platform != "darwin":
        return None
    known = db.execute("SELECT uuid FROM archives WHERE name=?", (archive,)).fetchone()
    if not known or not known[0]:
        return None
    if archive not in _uuid_cache:
        try:
            out = subprocess.run(["diskutil", "info", "-plist", str(root)], capture_output=True,
                                 timeout=30).stdout
            _uuid_cache[archive] = plistlib.loads(out).get("VolumeUUID", "")
        except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException):
            _uuid_cache[archive] = ""
    if _uuid_cache[archive] and _uuid_cache[archive] != known[0]:
        return f'the drive plugged in as "{archive}" is not the one in the catalogue'
    return None


# ---------- choosing what to delete ----------

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


def resolve(db, archive, text, volumes_root=VOLUMES):
    """One line of the list -> (archive, archive-relative path, None) or (None, None, problem)."""
    archives = [r[0] for r in db.execute("SELECT name FROM archives")]
    t = text.strip().rstrip("/")
    vr = volumes_root.rstrip("/") + "/"
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


def remove_empty_folders(db, archive, rel_dir, volumes_root=VOLUMES):
    """Remove folders left empty (or holding only .DS_Store / ._.DS_Store), walking up, but
    never a drive's top-level folders (e.g. "CumulativeArchive/JUDY 3")."""
    root = Path(volumes_root) / archive
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


def delete_copies(db, files, reason="", by="delete_files.py", volumes_root=VOLUMES, progress=None):
    """Delete these catalogued files (rows of `files`: dicts with id/archive/path/...; clutter
    included) from disk with their "._" companions, then update the catalogue. Returns
    (deleted, failed: [(path, why)], bytes freed)."""
    db.executescript(SCHEMA)
    prepared_before = _search_meta_matches(db)
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
        full = Path(volumes_root) / archive / path
        parent, name = os.path.split(path)
        companion = f"{parent}/._{name}" if parent else f"._{name}"
        try:
            existed = _unlink(full)
            _unlink(Path(volumes_root) / archive / companion)
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
        refresh_unique(db, uid)
    if prepared_before:
        _search_meta_update(db)
    db.commit()
    return done, failed, freed


# ---------- keeping unique_files and the search tables right ----------

def _search_meta_matches(db):
    if not has_table(db, "search_meta"):
        return False
    meta = dict(db.execute("SELECT key, value FROM search_meta").fetchall())
    n, mx = db.execute("SELECT COUNT(*), MAX(id) FROM unique_files").fetchone()
    return meta.get("unique_count") == str(n) and meta.get("unique_max_id") == str(mx)


def _search_meta_update(db):
    n, mx = db.execute("SELECT COUNT(*), MAX(id) FROM unique_files").fetchone()
    db.executemany("INSERT OR REPLACE INTO search_meta VALUES (?, ?)",
                   [("unique_count", str(n)), ("unique_max_id", str(mx))])


FTS_COLS = ("name", "other_names", "folder", "locations", "format", "ext")


def refresh_unique(db, uid):
    """After copies of distinct file `uid` were deleted: drop it if none are left, otherwise
    recompute its copies, locations, representative copy etc. (as identify_archive.py does)."""
    old = db.execute(f"SELECT {', '.join(FTS_COLS)}, sha256 FROM unique_files WHERE id=?", (uid,)).fetchone()
    if old is None:
        return
    fts = has_table(db, "unique_search")
    if fts:
        db.execute(f"INSERT INTO unique_search(unique_search, rowid, {', '.join(FTS_COLS)}) "
                   f"VALUES ('delete', ?, {', '.join('?' * len(FTS_COLS))})", (uid, *old[:len(FTS_COLS)]))
    sha = old[len(FTS_COLS)]
    rows = db.execute("SELECT archive, drive, path, name, size, mtime, modified, created FROM files "
                      "WHERE unique_id=? AND junk=0", (uid,)).fetchall()
    for t in ("unique_archives", "unique_drives", "unique_tags"):
        if has_table(db, t):
            db.execute(f"DELETE FROM {t} WHERE unique_id=?", (uid,))
    if not rows:                                             # last copy gone
        db.execute("DELETE FROM unique_files WHERE id=?", (uid,))
        if sha:
            if has_table(db, "content_class"):
                db.execute("DELETE FROM content_class WHERE sha256=?", (sha,))
            if has_table(db, "derivatives"):
                drop_derivative(db, sha)
        return
    rep = min(rows, key=lambda r: (r[5] or 0, len(r[2])))
    names = sorted({r[3] for r in rows} - {rep[3]}, key=str.lower)
    created = min((r[7] for r in rows if r[7]), default="")
    mods = sorted(r[6] for r in rows if r[6])
    locs = sorted(f"{r[0]}: {r[2]}" for r in rows)
    if len(locs) > MAX_LOCATIONS:
        locs = locs[:MAX_LOCATIONS] + [f"... and {len(locs) - MAX_LOCATIONS} more"]
    drives = sorted({f"{r[0]}: {r[1]}" for r in rows}, key=str.lower)
    folder = os.path.dirname(rep[2])
    db.execute("""UPDATE unique_files SET name=?, other_names=?, created=?, modified=?, modified_latest=?,
                  year=?, copies=?, drives=?, volumes=?, archive=?, path=?, folder=?, locations=?,
                  drive_list=? WHERE id=?""",
               (rep[3], "; ".join(names), created, mods[0] if mods else "", mods[-1] if mods else "",
                int(mods[0][:4]) if mods else None, len(rows), len(drives), len({r[0] for r in rows}),
                rep[0], rep[2], f"{rep[0]}: {folder or '/'}", "; ".join(locs), "; ".join(drives), uid))
    if fts:
        new = db.execute(f"SELECT {', '.join(FTS_COLS)} FROM unique_files WHERE id=?", (uid,)).fetchone()
        db.execute(f"INSERT INTO unique_search(rowid, {', '.join(FTS_COLS)}) "
                   f"VALUES (?, {', '.join('?' * len(FTS_COLS))})", (uid, *new))
    if has_table(db, "unique_drives"):
        db.executemany("INSERT OR IGNORE INTO unique_drives VALUES (?,?,?)", {(uid, r[0], r[1]) for r in rows})
    if has_table(db, "unique_archives"):
        db.executemany("INSERT OR IGNORE INTO unique_archives VALUES (?,?)", {(uid, r[0]) for r in rows})
    if has_table(db, "unique_tags"):
        try:
            from jva.kinds import tags_of
            tags = set()
            for r in rows:
                tags |= tags_of(r[2])
            db.executemany("INSERT OR IGNORE INTO unique_tags VALUES (?,?,?)", [(f, v, uid) for f, v in tags])
        except ImportError:
            pass


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


def delete_unique(db, uid, reason="", by="search app", volumes_root=VOLUMES):
    """All copies of one distinct file (used by the search app)."""
    rows = db.execute("SELECT id, archive, path, name, size, sha256, unique_id, junk FROM files "
                      "WHERE unique_id=?", (uid,)).fetchall()
    files, _ = plan(db, rows)
    return delete_copies(db, files, reason, by, volumes_root)


def delete_copy(db, file_id, reason="", by="search app", volumes_root=VOLUMES):
    """One copy (one row of `files`), used by the search app."""
    rows = db.execute("SELECT id, archive, path, name, size, sha256, unique_id, junk FROM files "
                      "WHERE id=?", (file_id,)).fetchall()
    files, _ = plan(db, rows)
    return delete_copies(db, files, reason, by, volumes_root)


# ---------- command line ----------

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
