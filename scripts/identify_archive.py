#!/usr/bin/env python3
"""
identify_archive.py -- catalogue, de-duplicate and identify the formats of everything on
the archive drives, one drive at a time, into ONE cumulative catalogue.

  python3 scripts/identify_archive.py -a "TAP 4T 2019"             # scan, hash, identify, reports
  python3 scripts/identify_archive.py --reports                    # just rebuild unique_files + reports

  -a, --archive NAME   the archive volume to process (required, except with --reports):
                       a name in /Volumes, or a path. Everything on the volume, from its root,
                       is catalogued.

Read-only on the archive. Everything goes into this folder:
  archive_catalog.sqlite     one row per file, for ALL archive drives: archive volume,
                             source drive, path, size, dates, SHA-256, PRONOM format ID
                             (Siegfried), MIME type, classic Mac type/creator codes,
                             resource-fork size. Open with DB Browser for SQLite or Datasette.
  archive_by_volume.csv      per archive drive: files, GB, what exists only there, what is
                             also on another archive drive, files not yet checksummed
  archive_by_drive.csv       per source drive (within each archive drive): files, GB, how
                             much exists nowhere else, how much is duplicated elsewhere
  archive_duplicates.csv     one row per set of identical files, biggest waste first
  archive_formats.csv        one row per file format: count, GB, drives, example
  appledouble_orphans.csv    "._" files whose own file or folder isn't there (on exFAT, a
                             file's Mac metadata lives in "._<name>" next to it; a "._" file
                             alone can mean the file itself didn't make it onto the drive;
                             an empty folder shows up here too, since only files are listed)

"Source drive" = the folder drive_inventory.py copied a drive into (known from drives.csv
and archive_map.csv); anything else is grouped by its top-level folder on the volume.

Steps (each picks up where it left off if interrupted; later runs only do new or changed
files; a run only ever changes catalogue entries for the archive drive given):
  1. scan      list every file on the volume; read classic Mac type/creator codes and
               resource-fork sizes (old Mac files often have no extension)
  2. hash      SHA-256 of every file on this volume that doesn't have one yet (files that
               couldn't be read last time are tried again). Used to
               find identical files across all archive drives, and as a fixity record
               (to check later that nothing has changed or decayed).
  3. identify  Siegfried (PRONOM) format identification from file contents.
               Install:  brew install richardlehane/digipres/siegfried
               Without it, falls back to `file --mime-type` (MIME type only). Files it
               couldn't open last time are tried again.
If the drive is unplugged or drops out during steps 2-3, the run stops without marking the
files it couldn't reach as unreadable; run it again once the drive is back.
  4. unique    rebuild the unique_files table: one row per distinct file (identical copies
               share one row; `copies` says how many there are, `locations` lists them),
               with a full-text search index (unique_search). files.unique_id links each
               copy to its row.
  5. reports   the CSVs above, covering every archive drive in the catalogue. All number
               columns are plain numbers; sizes in decimal GB.
Progress goes to STDERR; a one-line result goes to STDOUT. Standard library only.
"""
import argparse
import csv
import hashlib
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # the repository, for jva
from jva.config import CONFIG  # noqa: E402
from jva.catalog import (CLUTTER_EXT, FILE_COLS, FILES as SCHEMA, gb, is_clutter, log,  # noqa: E402
                        mark_clutter_folders, mark_clutter_types, write_csv)
from jva.search_prep import prepare  # noqa: E402
from jva.unique import build_unique  # noqa: E402
from jva import volumes  # noqa: E402
from jva.volumes import diskutil  # noqa: E402
from jva.macfiles import mac_codes, make_xattr_reader  # noqa: E402

PROJECT_DIR = str(CONFIG.data_dir)       # catalogue, reports and drive records
# catalogues made before archive drives were tracked: all files were on this volume,
# with paths relative to this folder
LEGACY_ARCHIVE = ("TAP 4T 2019", "CumulativeArchive")

SKIP_DIRS = {".Spotlight-V100", ".fseventsd", ".Trashes", ".DocumentRevisions-V100",
             ".TemporaryItems", ".MobileBackups", "$RECYCLE.BIN", "System Volume Information",
             ".PKInstallSandboxManager", ".PKInstallSandboxManager-SystemSoftware"}
ROOT_UNIT = ""                     # "unit" of files lying directly in the volume root
# make_derivatives.py puts this file in its output folder; such folders are never catalogued
DERIV_MARKER = ".archive-derivatives"

# ---------- catalogue setup / migration ----------

def open_catalogue(outdir):
    db = sqlite3.connect(outdir / "archive_catalog.sqlite")
    cols = [r[1] for r in db.execute("PRAGMA table_info(files)")]
    if cols and "archive" not in cols:
        backup = outdir / "archive_catalog.before-multi-archive.sqlite"
        log(f"   saving a copy of the current catalogue first: {backup.name}")
        with sqlite3.connect(backup) as b:
            db.backup(b)
        migrate_legacy(db)
        cols = [r[1] for r in db.execute("PRAGMA table_info(files)")]
    for c in ("created", "unique_id"):            # added after the first versions
        if cols and c not in cols:
            db.execute(f"ALTER TABLE files ADD COLUMN {c} {'TEXT' if c == 'created' else 'INTEGER'}")
    db.executescript(SCHEMA)
    db.execute("PRAGMA journal_mode=WAL")
    # dot-files ("._" files, ._.DS_Store, .BridgeSort, ...) count as clutter since this version
    n = db.execute("UPDATE files SET junk=1 WHERE junk=0 AND substr(name, 1, 1)='.'").rowcount
    if n:
        log(f"   {n:,} hidden dot-files (mostly Mac \"._\" files) now count as clutter")
    n = mark_clutter_folders(db)
    if n:
        log(f"   {n:,} files in Mac OS System Folders now count as clutter")
    n = mark_clutter_types(db)
    if n:
        log(f"   {n:,} files of clutter types ({', '.join(sorted(CLUTTER_EXT))}) now count as clutter")
    db.commit()
    return db


def migrate_legacy(db):
    """Catalogue from the single-archive version: tag every row as LEGACY_ARCHIVE and make
    paths relative to the volume root. Checksums and format results are kept."""
    vol, sub = LEGACY_ARCHIVE
    n = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    log(f"   converting the existing catalogue ({n:,} files) to multi-archive form: "
        f'all assigned to "{vol}", paths now start with "{sub}/" ...')
    keep = ("drive, name, ext, size, mtime, modified, junk, mac_type, mac_creator, rsrc_bytes, "
            "sha256, hash_error, puid, format, format_version, mime, id_basis, id_warning, "
            "id_tool, id_error")
    db.executescript(f"""
        DROP INDEX IF EXISTS i_size; DROP INDEX IF EXISTS i_sha; DROP INDEX IF EXISTS i_drive;
        DROP INDEX IF EXISTS i_puid;
        CREATE TABLE files_new({FILE_COLS});
        INSERT INTO files_new (archive, unit, path, {keep})
          SELECT '{vol}', '{sub}', '{sub}/' || path, {keep} FROM files;
        DROP TABLE files;
        ALTER TABLE files_new RENAME TO files;
        CREATE TABLE IF NOT EXISTS archives(name TEXT PRIMARY KEY, uuid TEXT, last_scan TEXT);
        INSERT OR IGNORE INTO archives(name, uuid, last_scan) VALUES ('{vol}', '', '');
    """)
    db.commit()
    log("   done")


def register_archive(db, name, vol):
    """Remember the volume's UUID, and refuse if a different drive with the same name was
    catalogued before (it would mix up two drives' files)."""
    uuid = diskutil(vol).get("VolumeUUID", "")
    row = db.execute("SELECT uuid FROM archives WHERE name=?", (name,)).fetchone()
    if row and row[0] and uuid and row[0] != uuid:
        sys.exit(f'A different drive named "{name}" (UUID {row[0]}) is already in the catalogue.\n'
                 f"This one is {uuid}. Rename one of the two volumes, then run again.")
    db.execute("INSERT INTO archives(name, uuid, last_scan) VALUES (?,?,?) "
               "ON CONFLICT(name) DO UPDATE SET uuid=COALESCE(NULLIF(excluded.uuid,''), uuid), "
               "last_scan=excluded.last_scan",
               (name, uuid, time.strftime("%Y-%m-%d %H:%M:%S")))
    db.commit()


def source_dirs(outdir, archive):
    """{folder relative to the volume root: source-drive name} for folders that
    drive_inventory.py copied drives into on this archive volume."""
    m = {}
    prefix = f"/Volumes/{archive}/"
    for f in ("drives.csv",):
        p = outdir / f
        if p.exists():
            with open(p, newline="", encoding="utf-8-sig") as fh:
                for r in csv.DictReader(fh):
                    d = r.get("archive_dir") or ""
                    if d.startswith(prefix):
                        rel = d[len(prefix):].strip("/")
                        m[rel] = r.get("archive_name") or Path(rel).name
    p = outdir / "archive_map.csv"
    if p.exists():
        with open(p, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                vol = r.get("archive_volume") or LEGACY_ARCHIVE[0]
                d = r.get("archive_dir") or f"{LEGACY_ARCHIVE[1]}/{r.get('archive_name', '')}"
                if vol == archive and d.strip("/"):
                    m[d.strip("/")] = r.get("archive_name") or Path(d).name
    return m


def drive_label(rel, sdirs):
    parts = rel.split("/")
    for k in (1, 2, 3):
        if len(parts) > k and "/".join(parts[:k]) in sdirs:
            return sdirs["/".join(parts[:k])]
    return parts[0] if len(parts) > 1 else "(volume root)"


def relabel(db, archive, sdirs):
    """Apply the current source-drive folders to rows catalogued earlier."""
    for prefix, label in sdirs.items():
        db.execute("UPDATE files SET drive=? WHERE archive=? AND path >= ? AND path < ? AND drive <> ?",
                   (label, archive, prefix + "/", prefix + "0", label))   # '0' sorts right after '/'
    db.commit()


# ---------- classic Mac metadata (Finder type/creator, resource fork) ----------

def walk_files(top, recursive=True):
    stack = [top]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError as e:
            log(f"   can't read {d}: {e.strerror}")
            continue
        with it:
            while True:
                try:
                    e = next(it)
                except StopIteration:
                    break
                except OSError as err:
                    log(f"   listing failed in {d}: {err.strerror}")
                    break
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    if (recursive and e.name not in SKIP_DIRS
                            and not os.path.exists(os.path.join(e.path, DERIV_MARKER))):
                        stack.append(e.path)
                elif stat.S_ISREG(st.st_mode):
                    yield e.path, e.name, st


def removed_paths(db, archive):
    """Paths where free_space.py removed an extra copy (and left a small stub file)."""
    try:
        return {p for (p,) in db.execute("SELECT path FROM removed_copies WHERE archive=?", (archive,))}
    except sqlite3.OperationalError:
        return set()


STUB_MAX = 4096        # a stub only holds a path


def scan(db, archive, root, sdirs):
    xattr = make_xattr_reader()
    stubs = removed_paths(db, archive)
    units = sorted(p.name for p in root.iterdir()
                   if p.is_dir() and not p.is_symlink() and p.name not in SKIP_DIRS
                   and not (p / DERIV_MARKER).exists())
    total_new = total_gone = 0
    for unit in [ROOT_UNIT] + units:
        t0 = time.time()
        known, no_created = {}, set()
        for p, s, m, c in db.execute("SELECT path, size, mtime, created FROM files "
                                     "WHERE archive=? AND unit=?", (archive, unit)):
            known[p] = (s, m)
            if not c:
                no_created.add(p)
        seen, batch, n_new, fill = set(), [], 0, []
        top = str(root / unit) if unit else str(root)
        for full, name, st in walk_files(top, recursive=bool(unit)):
            rel = os.path.relpath(full, root)
            seen.add(rel)
            created = _created(st)
            if known.get(rel) == (st.st_size, st.st_mtime):  # unchanged since last run
                if rel in no_created and created:
                    fill.append((created, archive, rel))    # catalogued before dates-created were kept
                    if len(fill) >= 5000:
                        _fill_created(db, fill)
                continue
            t, c, rs = mac_codes(xattr, full)
            batch.append((archive, unit, drive_label(rel, sdirs), rel, name,
                          os.path.splitext(name)[1].lower(), st.st_size, st.st_mtime,
                          time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                          int(is_clutter(name, st.st_size, rel) or
                              (rel in stubs and st.st_size <= STUB_MAX)), t, c, rs, created))
            n_new += 1
            if len(batch) >= 5000:
                _insert(db, batch)
                log(f"   {unit or '(volume root)'}: {n_new:,} new/changed files so far ...")
        _insert(db, batch)
        _fill_created(db, fill)
        gone = [(archive, p) for p in known if p not in seen]
        db.executemany("DELETE FROM files WHERE archive=? AND path=?", gone)
        db.commit()
        total_new += n_new
        total_gone += len(gone)
        if seen or gone:
            log(f"   {unit or '(volume root)'}: {len(seen):,} files ({n_new:,} new/changed, "
                f"{len(gone):,} removed) in {time.time() - t0:.0f}s")
    # top-level folders that are gone from THIS volume (other volumes are never touched)
    for (u,) in db.execute("SELECT DISTINCT unit FROM files WHERE archive=?", (archive,)).fetchall():
        if u != ROOT_UNIT and u not in units:
            db.execute("DELETE FROM files WHERE archive=? AND unit=?", (archive, u))
            log(f"   {u}: no longer on {archive}, removed from catalogue")
    db.commit()
    return total_new, total_gone


def _created(st):
    t = getattr(st, "st_birthtime", 0)                  # macOS: the file's creation date
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""


def _fill_created(db, fill):
    if fill:
        db.executemany("UPDATE files SET created=? WHERE archive=? AND path=?", fill)
        db.commit()
        fill.clear()


def _insert(db, batch):
    if batch:
        # INSERT OR REPLACE resets hash and format fields, so changed files get redone
        db.executemany("""INSERT OR REPLACE INTO files
            (archive, unit, drive, path, name, ext, size, mtime, modified, junk,
             mac_type, mac_creator, rsrc_bytes, created)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", batch)
        db.commit()
        batch.clear()


# ---------- 2. hash ----------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(4 * 1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


class DriveGone(Exception):
    """The archive drive went away (unplugged, or it dropped off the bus) during a run."""


def check_drive(root):
    """Raise DriveGone unless the archive drive is still there."""
    if not (os.path.ismount(root) if str(root).startswith("/Volumes/") else Path(root).is_dir()):
        raise DriveGone(f"{root} is no longer there: the drive was unplugged or dropped out. "
                        "Nothing was recorded for the files it couldn't reach; plug it in and run "
                        "this again to carry on.")


def hash_files(db, archive, root):
    # Files that couldn't be read on an earlier run are tried again: the usual cause is the
    # drive dropping out for a moment, and then they read fine.
    retry = db.execute("UPDATE files SET hash_error=NULL WHERE archive=? AND hash_error IS NOT NULL "
                       "AND sha256 IS NULL", (archive,)).rowcount
    db.commit()
    if retry:
        log(f"   trying again {retry:,} files that couldn't be read last time")
    where = "archive=? AND sha256 IS NULL AND hash_error IS NULL AND junk=0"
    todo_n, todo_b = db.execute(f"SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE {where}",
                                (archive,)).fetchone()
    log(f"   {todo_n:,} files to checksum, {todo_b / 1e9:.3f} GB")
    done_n = done_b = 0
    t0 = last = time.time()
    while True:
        rows = db.execute(f"SELECT id, path, size FROM files WHERE {where} ORDER BY id LIMIT 500",
                          (archive,)).fetchall()
        if not rows:
            break
        for fid, rel, size in rows:
            try:
                db.execute("UPDATE files SET sha256=? WHERE id=?", (sha256_file(root / rel), fid))
            except OSError as e:
                check_drive(root)              # the whole drive gone: stop, record nothing
                db.execute("UPDATE files SET hash_error=? WHERE id=?", (e.strerror or str(e), fid))
                log(f"   can't read {rel}: {e.strerror}")
            done_n += 1
            done_b += size
            if time.time() - last > 30:
                last = time.time()
                log(f"   hashed {done_n:,}/{todo_n:,} files, {done_b / 1e9:.3f}/{todo_b / 1e9:.3f} GB "
                    f"({done_b / 1e6 / (last - t0):.0f} MB/s)")
        db.commit()
    log(f"   hashed {done_n:,} files, {done_b / 1e9:.3f} GB in {time.time() - t0:.0f}s")
    return done_n


# ---------- 3. identify ----------

def identify(db, archive, root, batch=2000):
    sf = shutil.which("sf")
    # With Siegfried, also redo files that only got a MIME type from `file` on an earlier run,
    # or that `file` gave no answer for. (Siegfried's own failures aren't retried in a loop.)
    # Files Siegfried couldn't open last time ("file access error") are tried again, as for hashing.
    retry = db.execute(f"UPDATE files SET id_error=NULL, id_tool=NULL WHERE archive=? AND {ACCESS_ERROR}",
                       (archive,)).rowcount
    db.commit()
    if retry:
        log(f"   trying again {retry:,} files that couldn't be opened last time")
    if sf:
        where = ("archive=? AND junk=0 AND (id_tool IS NULL OR id_tool='file') AND "
                 "(id_error IS NULL OR id_error IN ('no result from file', 'no result from identifier'))")
    else:
        where = "archive=? AND junk=0 AND id_tool IS NULL AND id_error IS NULL"
    tool = "siegfried" if sf else "file"
    todo = db.execute(f"SELECT COUNT(*) FROM files WHERE {where}", (archive,)).fetchone()[0]
    log(f"   {todo:,} files to identify with " +
        ("Siegfried" if sf else "`file` (Siegfried not found -- brew install richardlehane/digipres/siegfried)"))
    done, t0, last = 0, time.time(), time.time()
    while True:
        rows = db.execute(f"SELECT id, path FROM files WHERE {where} ORDER BY id LIMIT ?",
                          (archive, batch)).fetchall()
        if not rows:
            break
        bad = [(fid,) for fid, rel in rows if "\n" in rel or "\r" in rel]
        db.executemany("UPDATE files SET id_error='newline in file name' WHERE id=?", bad)
        rows = [(fid, rel) for fid, rel in rows if "\n" not in rel and "\r" not in rel]
        check_drive(root)
        results = (_run_sf(sf, root, rows) if sf else _run_file(root, rows))
        if any(_access_error(v[7]) for v in results.values()):
            check_drive(root)                  # every file "missing" because the drive went
        got = set()
        for fid, vals in results.items():
            db.execute("""UPDATE files SET puid=?, format=?, format_version=?, mime=?, id_basis=?,
                          id_warning=?, id_tool=?, id_error=? WHERE id=?""", (*vals, fid))
            got.add(fid)
        missing = [(fid,) for fid, _ in rows if fid not in got]
        db.executemany(f"UPDATE files SET id_error='no result from {tool}' WHERE id=?", missing)
        db.commit()
        done += len(rows) + len(bad)
        if time.time() - last > 30:
            last = time.time()
            log(f"   identified {done:,}/{todo:,} files ({done / (last - t0):.0f}/s)")
    log(f"   identified {done:,} files in {time.time() - t0:.0f}s")
    return done


ACCESS_ERROR = "id_error LIKE '%file access error%'"


def _access_error(err):
    return bool(err) and "file access error" in err


def _run_sf(sf, root, rows):
    """Siegfried over a batch of files. Returns {id: (puid, format, version, mime, basis,
    warning, tool, error)}."""
    by_path = {str(root / rel): fid for fid, rel in rows}
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as lf:
        lf.write("\n".join(by_path) + "\n")
        listfile = lf.name
    try:
        p = subprocess.run([sf, "-csv", "-multi", "8", "-f", listfile],
                           capture_output=True, text=True, errors="replace")
    finally:
        os.unlink(listfile)
    reader = csv.DictReader(p.stdout.splitlines())
    fields = [f.lower() for f in (reader.fieldnames or [])]
    if "filename" not in fields or "id" not in fields:
        sys.exit("Siegfried's CSV output didn't have the expected 'filename' and 'id' columns.\n"
                 f"  columns: {reader.fieldnames}\n  stderr: {p.stderr.strip()[:500]}")
    out = {}
    for r in reader:
        r = {k.lower(): (v or "") for k, v in r.items() if k}
        fid = by_path.get(r["filename"])
        if fid is None or fid in out:       # several matches for one file: keep the first
            continue
        out[fid] = (r.get("id", ""), r.get("format", ""), r.get("version", ""), r.get("mime", ""),
                    r.get("basis", ""), r.get("warning", ""), "siegfried", r.get("errors") or None)
    return out


def _run_file(root, rows):
    paths = [str(root / rel) for _, rel in rows]
    p = subprocess.run(["file", "-b", "--mime-type", "--", *paths],
                       capture_output=True, text=True, errors="replace")
    lines = p.stdout.splitlines()
    if len(lines) != len(rows):
        return {}
    return {fid: ("", "", "", m.strip(), "", "", "file", None) for (fid, _), m in zip(rows, lines)}


# ---------- unique files (one row per distinct file) ----------

# ---------- 4. reports (all archive drives) ----------

PENDING = "f.sha256 IS NULL AND f.hash_error IS NULL AND f.junk=0"     # not yet checksummed


def reports(db, outdir):
    db.execute("DROP TABLE IF EXISTS temp.groups")
    db.execute("""CREATE TEMP TABLE groups AS
                  SELECT sha256, COUNT(*) n, COUNT(DISTINCT archive || char(31) || drive) nd,
                         COUNT(DISTINCT archive) na
                  FROM files WHERE sha256 IS NOT NULL AND junk=0 GROUP BY sha256 HAVING n > 1""")
    db.execute("CREATE INDEX temp.i_g ON groups(sha256)")

    # per archive volume
    vols = db.execute(f"""
        SELECT f.archive, a.uuid, a.last_scan, COUNT(*), SUM(f.size),
          SUM(CASE WHEN f.junk=0 AND (g.sha256 IS NULL OR g.na = 1) THEN f.size ELSE 0 END),
          SUM(CASE WHEN g.na > 1 THEN f.size ELSE 0 END),
          SUM({PENDING}), COUNT(DISTINCT f.drive)
        FROM files f LEFT JOIN temp.groups g ON f.sha256 = g.sha256
        LEFT JOIN archives a ON a.name = f.archive
        GROUP BY f.archive ORDER BY f.archive COLLATE NOCASE""").fetchall()
    write_csv(outdir / "archive_by_volume.csv",
              ["archive_volume", "volume_uuid", "last_scanned", "files", "total_gb",
               "only_on_this_volume_gb", "also_on_other_volumes_gb", "not_yet_checksummed",
               "drives"],
              [[v, u or "", ls or "", n, gb(b), gb(ob), gb(ab), p or 0, nd]
               for v, u, ls, n, b, ob, ab, p, nd in vols])

    # per source drive
    rows = db.execute(f"""
        SELECT f.archive, f.drive, COUNT(*), SUM(f.size),
          SUM(f.junk=0 AND g.sha256 IS NULL), SUM(CASE WHEN f.junk=0 AND g.sha256 IS NULL THEN f.size ELSE 0 END),
          SUM(g.nd > 1), SUM(CASE WHEN g.nd > 1 THEN f.size ELSE 0 END),
          SUM(g.nd = 1), SUM(CASE WHEN g.nd = 1 THEN f.size ELSE 0 END),
          SUM(f.junk), SUM(f.junk=0 AND (f.puid = 'UNKNOWN' OR (f.puid = '' AND f.id_tool = 'siegfried'))),
          SUM(f.mac_type <> ''), SUM(f.rsrc_bytes > 0), SUM({PENDING})
        FROM files f LEFT JOIN temp.groups g ON f.sha256 = g.sha256
        GROUP BY f.archive, f.drive
        ORDER BY f.archive COLLATE NOCASE, f.drive COLLATE NOCASE""").fetchall()
    out = []
    for r in rows:
        av, d, n, b, un, ub, on, ob, wn, wb, junk, unk, mt, rf, pend = [x if x is not None else 0
                                                                         for x in r]
        out.append([av, d, n, gb(b), un, gb(ub), round(100 * ub / b, 1) if b else 0,
                    on, gb(ob), wn, gb(wb), junk, unk, mt, rf, pend])
    write_csv(outdir / "archive_by_drive.csv",
              ["archive_volume", "drive", "files", "total_gb", "only_copy_files", "only_copy_gb",
               "pct_only_copy", "also_on_other_drives_files", "also_on_other_drives_gb",
               "duplicated_within_drive_files", "duplicated_within_drive_gb",
               "junk_or_empty_files", "unidentified_files", "mac_type_coded_files",
               "files_with_resource_fork", "not_yet_checksummed"], out)

    # duplicate sets
    groups = db.execute("""
        SELECT g.sha256, MIN(f.size), g.n, g.nd, g.na,
               GROUP_CONCAT(DISTINCT f.archive || ': ' || f.drive)
        FROM temp.groups g JOIN files f ON f.sha256 = g.sha256
        GROUP BY g.sha256 ORDER BY MIN(f.size) * (g.n - 1) DESC""").fetchall()
    dup_rows = []
    for sha, size, n, nd, na, drives in groups:
        paths = [f"{a}: {p}" for a, p in db.execute(
            "SELECT archive, path FROM files WHERE sha256=? ORDER BY archive, path LIMIT 6", (sha,))]
        dup_rows.append([sha, size, gb(size), n, nd, na, gb(size * (n - 1)),
                         drives.replace(",", "; "), paths[0], "; ".join(paths[1:])])
    write_csv(outdir / "archive_duplicates.csv",
              ["sha256", "size_bytes", "size_gb", "copies", "drives_count", "volumes_count",
               "redundant_gb", "drives", "first_path", "other_paths_up_to_5"], dup_rows)

    # formats (unidentified files are split by their classic Mac type/creator codes)
    fmt = db.execute("""
        SELECT COALESCE(puid,''), COALESCE(format,''), COALESCE(format_version,''), COALESCE(mime,''),
          CASE WHEN COALESCE(puid,'') IN ('', 'UNKNOWN') THEN COALESCE(mac_type,'') ELSE '' END mt,
          CASE WHEN COALESCE(puid,'') IN ('', 'UNKNOWN') THEN COALESCE(mac_creator,'') ELSE '' END mc,
          COUNT(*), SUM(size), COUNT(DISTINCT archive || char(31) || drive), MIN(archive || ': ' || path),
          GROUP_CONCAT(DISTINCT ext)
        FROM files WHERE junk=0
        GROUP BY 1, 2, 3, 4, 5, 6 ORDER BY COUNT(*) DESC""").fetchall()
    fmt_rows = []
    for puid, f, v, m, mt, mc, n, b, nd, ex, exts in fmt:
        exts = ", ".join(sorted(set((exts or "").split(",")) - {""}))[:200]
        fmt_rows.append([puid, f, v, m, mt, mc, n, gb(b), nd, exts, ex])
    write_csv(outdir / "archive_formats.csv",
              ["puid", "format", "version", "mime", "mac_type", "mac_creator", "files",
               "total_gb", "drives", "extensions_seen", "example_path"], fmt_rows)

    orphans = appledouble_orphans(db)
    write_csv(outdir / "appledouble_orphans.csv",
              ["archive_volume", "drive", "appledouble_file", "bytes", "missing_file_or_folder"], orphans)

    t = db.execute("SELECT COUNT(*), COALESCE(SUM(size),0), COUNT(DISTINCT archive), "
                   "COUNT(DISTINCT archive || char(31) || drive) FROM files").fetchone()
    red = db.execute("SELECT COALESCE(SUM(f.size),0) FROM files f JOIN temp.groups g ON f.sha256=g.sha256").fetchone()[0]
    keep = db.execute("SELECT COALESCE(SUM(sz),0) FROM (SELECT MIN(f.size) sz FROM files f "
                      "JOIN temp.groups g ON f.sha256=g.sha256 GROUP BY f.sha256)").fetchone()[0]
    pending = sum((v[7] or 0) for v in vols)
    log(f"   reports written: archive_by_volume.csv, archive_by_drive.csv, "
        f"archive_duplicates.csv ({len(dup_rows):,} sets), archive_formats.csv ({len(fmt_rows):,} formats), "
        f"appledouble_orphans.csv ({len(orphans):,})")
    for v in vols:
        if v[7]:
            log(f'   {v[7]:,} files on "{v[0]}" not yet checksummed -- run with -a "{v[0]}" '
                f'when it is mounted')
    return {"volumes": t[2], "drives": t[3], "files": t[0], "gb": gb(t[1]), "dup_sets": len(dup_rows),
            "redundant_gb": gb(red - keep), "formats": len(fmt_rows), "pending": pending}


def appledouble_orphans(db):
    """"._X" files with no X (file or folder) beside them."""
    db.execute("DROP TABLE IF EXISTS temp.ad")
    db.execute("""CREATE TEMP TABLE ad AS
                  SELECT archive, drive, path, size,
                         substr(path, 1, length(path) - length(name)) || substr(name, 3) partner
                  FROM files WHERE substr(name, 1, 2) = '._' AND name <> '._.DS_Store'""")
    rows = db.execute("""
        SELECT a.archive, a.drive, a.path, a.size, a.partner FROM temp.ad a
        WHERE NOT EXISTS (SELECT 1 FROM files b WHERE b.archive = a.archive AND b.path = a.partner)
          AND NOT EXISTS (SELECT 1 FROM files b WHERE b.archive = a.archive
                          AND b.path >= a.partner || '/' AND b.path < a.partner || '0')
          AND a.partner NOT LIKE '%/.Trashes' AND a.partner <> '.Trashes'
        ORDER BY a.archive, a.path""").fetchall()
    db.execute("DROP TABLE temp.ad")
    return [list(r) for r in rows]


# ---------- main ----------

def prepare_for_search(outdir):
    """Keep the search app fast: rebuild its helper tables (jva.search_prep, as
    prepare_search.py does), which also refreshes SQLite's statistics."""
    log("6. prepare for searching")
    db = sqlite3.connect(Path(outdir) / "archive_catalog.sqlite", timeout=60)
    try:
        n = prepare(db, step=lambda m: log(f"   {m}"))
        log(f"   {n:,} distinct files ready for searching")
    except Exception as e:                     # never lose the run's results over this
        log(f"   preparing for searching failed ({e}); refreshing statistics only")
        db.execute("ANALYZE")
    db.close()


def main():
    ap = argparse.ArgumentParser(description="Catalogue, de-duplicate and identify archive drives.")
    ap.add_argument("-a", "--archive",
                    help="archive volume to process (required): name in /Volumes, or a path")
    ap.add_argument("--hash-all", action="store_true",
                    help=argparse.SUPPRESS)   # every file is checksummed now; kept so old commands work
    ap.add_argument("--reports", action="store_true", help="only rewrite the reports")
    ap.add_argument("--skip-identify", action="store_true", help="skip format identification")
    ap.add_argument("-o", "--out", default=PROJECT_DIR, help=argparse.SUPPRESS)
    a = ap.parse_args()

    outdir = Path(a.out).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)
    if not a.reports:
        if not a.archive:
            ap.error('the archive volume is required, e.g.  -a "TAP 4T 2019"')
        vol = volumes.path_of(a.archive)
        if not volumes.is_mounted(a.archive):
            sys.exit(f"{vol} is not mounted.")
        archive = vol.name
    db = open_catalogue(outdir)
    t0 = time.time()

    if not a.reports:
        register_archive(db, archive, vol)
        sdirs = source_dirs(outdir, archive)
        log(f"== {archive}  ({vol})")
        log("1. scan");     scan(db, archive, vol, sdirs); relabel(db, archive, sdirs)
        try:
            log("2. hash");     hash_files(db, archive, vol)
            if not a.skip_identify:
                log("3. identify"); identify(db, archive, vol)
        except DriveGone as e:
            db.commit()
            db.close()
            sys.exit(f"Stopped: {e}")
    log("4. unique files");  n_unique = build_unique(db)
    log("5. reports")
    s = reports(db, outdir)
    db.close()
    prepare_for_search(outdir)
    print(f"{s['volumes']} archive volumes, {s['drives']} drives, {s['files']:,} files "
          f"({n_unique:,} distinct), {s['gb']:.2f} GB | "
          f"{s['dup_sets']:,} sets of duplicates, {s['redundant_gb']:.2f} GB redundant | "
          f"{s['formats']:,} formats | {s['pending']:,} not yet checksummed | {time.time() - t0:.0f} s",
          flush=True)


if __name__ == "__main__":
    main()
