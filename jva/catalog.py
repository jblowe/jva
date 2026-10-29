"""The catalogue (archive_catalog.sqlite): opening it, its tables, and small shared helpers.

Tables, and which script fills them:
  files, archives          identify_archive.py   every copy of every file on every archive drive
  unique_files, unique_search   identify_archive.py (jva.unique)   one row per distinct file
  file_class, content_class     classify_archive.py   subject categories
  derivatives              make_derivatives.py   preview images, PDF/RTF copies
  removed_copies           free_space.py         extra copies removed to save space
  deleted_files            delete_files.py, the search app   files deleted on purpose
  tiff_repairs             repair_tiffs.py       bytes changed in damaged TIFFs (for --undo)
  file_groups              jva/similar.py (with unique_files)   similar files grouped together
  unique_archives, unique_drives, unique_tags, search_meta, unique_files.kind
                           prepare_search.py (jva.search_prep)   for a fast search app
"""
import csv
import re
import sqlite3
import sys
from pathlib import Path

from jva.config import CONFIG

FILE_COLS = """
  id INTEGER PRIMARY KEY,
  archive TEXT, unit TEXT, drive TEXT, path TEXT, name TEXT, ext TEXT,
  size INTEGER, mtime REAL, modified TEXT, junk INTEGER,
  mac_type TEXT, mac_creator TEXT, rsrc_bytes INTEGER,
  sha256 TEXT, hash_error TEXT,
  puid TEXT, format TEXT, format_version TEXT, mime TEXT, id_basis TEXT, id_warning TEXT,
  id_tool TEXT, id_error TEXT, created TEXT, unique_id INTEGER,
  UNIQUE(archive, path)"""
FILES = f"""
CREATE TABLE IF NOT EXISTS files({FILE_COLS});
CREATE INDEX IF NOT EXISTS i_size ON files(size);
CREATE INDEX IF NOT EXISTS i_sha ON files(sha256);
CREATE INDEX IF NOT EXISTS i_unit ON files(archive, unit);
CREATE INDEX IF NOT EXISTS i_drive ON files(archive, drive);
CREATE INDEX IF NOT EXISTS i_puid ON files(puid);
CREATE INDEX IF NOT EXISTS i_unique ON files(unique_id);
CREATE TABLE IF NOT EXISTS archives(name TEXT PRIMARY KEY, uuid TEXT, last_scan TEXT);
"""

UNIQUE_COLUMNS = ("id", "sha256", "name", "other_names", "ext", "format", "puid", "format_version",
                  "mime", "mac_type", "mac_creator", "size_bytes", "size", "created", "modified",
                  "modified_latest", "year", "copies", "drives", "volumes", "archive", "path", "folder",
                  "locations", "drive_list", "checksum", "category")
UNIQUE = """
DROP TABLE IF EXISTS unique_search;
DROP TABLE IF EXISTS unique_files;
CREATE TABLE unique_files(
  id INTEGER PRIMARY KEY,           -- files.unique_id points here
  sha256 TEXT,                      -- empty if the file was never checksummed (see checksum)
  name TEXT,                        -- name of the representative (oldest) copy
  other_names TEXT,                 -- other names the same content goes by, "; "-separated
  ext TEXT, format TEXT, puid TEXT, format_version TEXT, mime TEXT,
  mac_type TEXT, mac_creator TEXT,
  size_bytes INTEGER, size TEXT,    -- size TEXT is human-readable, e.g. "1.2 MB"
  created TEXT,                     -- earliest date created among the copies
  modified TEXT,                    -- earliest date modified among the copies
  modified_latest TEXT,
  year INTEGER,                     -- year of `modified`
  copies INTEGER,                   -- 1 = unique; N = N identical copies in the archive
  drives INTEGER, volumes INTEGER,  -- how many source drives / archive drives they're on
  archive TEXT, path TEXT, folder TEXT,   -- where the representative copy is
  locations TEXT,                   -- every copy as "archive: path", "; "-separated (first 100)
  drive_list TEXT,                  -- "archive: drive" for each drive holding a copy
  checksum TEXT,                    -- checksummed | not yet checksummed | unreadable
  category TEXT,                    -- from classify_archive.py: TAP, Class, ... (if run)
  group_id INTEGER,                 -- jva/similar.py: the id of the group's main file (itself if alone)
  group_how TEXT,                   -- why it is in that group: "same name", a rule's name, or ""
  group_key TEXT                    -- the object (etc.) the rule found, e.g. "10091"
);
"""
UNIQUE_INDEXES = """
CREATE INDEX IF NOT EXISTS i_u_category ON unique_files(category);
CREATE INDEX IF NOT EXISTS i_u_sha ON unique_files(sha256);
CREATE INDEX IF NOT EXISTS i_u_name ON unique_files(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS i_u_copies ON unique_files(copies);
CREATE INDEX IF NOT EXISTS i_u_format ON unique_files(format);
CREATE INDEX IF NOT EXISTS i_u_year ON unique_files(year);
"""
# full-text index over the human-searchable columns of unique_files (SQLite FTS5)
FTS_COLUMNS = ("name", "other_names", "folder", "locations", "format", "ext")
UNIQUE_SEARCH = f"""
CREATE VIRTUAL TABLE unique_search USING fts5(
  {', '.join(FTS_COLUMNS)},
  content='unique_files', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
INSERT INTO unique_search(unique_search) VALUES('rebuild');
"""

DERIVATIVES = """
CREATE TABLE IF NOT EXISTS derivatives(
  sha256 TEXT PRIMARY KEY,      -- same key as files.sha256 / unique_files.sha256
  slug TEXT,                    -- web-safe name, e.g. anvils-2-for-jake-3f9a2c7b1e
  kind TEXT,                    -- image | pdf | postscript | drawing | olddoc | quicklook | placeholder
  method TEXT,                  -- imagemagick | sips | libreoffice | ghostscript | quicklook | placeholder
  status TEXT,                  -- made | placeholder | failed
  root TEXT,                    -- the derivatives folder (absolute path) when made
  thumb TEXT,                   -- path relative to root, e.g. thumb/3f/<slug>.webp
  preview TEXT,                 -- path relative to root
  width INTEGER, height INTEGER,-- of the preview
  pdf TEXT, rtf TEXT,           -- old Word files: whole document as PDF and RTF (relative paths)
  source_archive TEXT, source_path TEXT,   -- the copy it was made from
  error TEXT, made_at TEXT);
CREATE INDEX IF NOT EXISTS i_der_status ON derivatives(status);
"""

REMOVED_COPIES = """
CREATE TABLE IF NOT EXISTS removed_copies(
  archive TEXT, path TEXT, sha256 TEXT, size INTEGER, modified TEXT, created TEXT,
  drive TEXT, category TEXT, kept_path TEXT, removed_at TEXT, reason TEXT,
  PRIMARY KEY(archive, path));
"""

DELETED_FILES = """
CREATE TABLE IF NOT EXISTS deleted_files(
  archive TEXT, path TEXT,          -- where the deleted copy was
  name TEXT, size INTEGER, sha256 TEXT, category TEXT,
  copies_left INTEGER,              -- copies of the same file still in the archive afterwards
  reason TEXT, deleted_at TEXT, deleted_by TEXT,   -- deleted_by: delete_files.py or search app
  PRIMARY KEY (archive, path));
"""

TIFF_REPAIRS = """
CREATE TABLE IF NOT EXISTS tiff_repairs(
  archive TEXT, path TEXT,
  old_sha256 TEXT, new_sha256 TEXT,
  offset INTEGER, old_bytes BLOB, new_bytes BLOB,   -- one directory rewritten (several rows if several)
  removed TEXT,                                     -- e.g. "Exif pointer"
  repaired_at TEXT);
CREATE INDEX IF NOT EXISTS i_tr_path ON tiff_repairs(archive, path);
"""

# separate statements, so classify_archive.py can run them inside one transaction
CLASSIFICATION = (
    "DROP TABLE IF EXISTS file_class",
    """CREATE TABLE file_class(archive TEXT, path TEXT, category TEXT, rule TEXT, matched TEXT,
                              PRIMARY KEY(archive, path))""",
    "DROP TABLE IF EXISTS content_class",
    "CREATE TABLE content_class(sha256 TEXT PRIMARY KEY, category TEXT, copies_by_category TEXT)",
)

# catalogued, but never treated as content (system clutter)
JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", "Icon\r", ".localized", "ehthumbs.db"}


# Folders whose whole contents are clutter: the classic Mac OS "System Folder" (also
# "System Folder 9.2", "System Folder (old)", "System Folder copy", ...)
CLUTTER_FOLDERS = re.compile(r"(^|/)System Folder(?: [^/]*)?/", re.I)


def is_clutter(name, size, path=""):
    """Catalogued, but never treated as content: system files, empty files, every hidden
    dot-file -- above all the "._" AppleDouble files exFAT drives keep next to Mac files -- and
    everything in a CLUTTER_FOLDERS folder. A "._" file holds the Mac type/creator codes and
    resource fork of the file it sits beside; the scan reads those into that file's own row
    (mac_type, mac_creator, rsrc_bytes)."""
    return name in JUNK_NAMES or size == 0 or name.startswith(".") or bool(CLUTTER_FOLDERS.search(path))


def mark_clutter_folders(db):
    """Mark files already catalogued in a CLUTTER_FOLDERS folder as clutter. -> how many."""
    rows = db.execute("SELECT id, path FROM files WHERE junk = 0 AND path LIKE '%System Folder%/%'").fetchall()
    ids = [(i,) for i, p in rows if CLUTTER_FOLDERS.search(p)]
    db.executemany("UPDATE files SET junk = 1 WHERE id = ?", ids)
    return len(ids)


# subject categories, in order: a distinct file takes the first one any of its copies has
CATEGORIES = ["TAP", "Class", "SE Asian Archaeology", "Other Archaeology", "Museum", "Other",
              "Unclassified"]
# the order the search app lists them in
DISPLAY_CATEGORIES = ["TAP", "SE Asian Archaeology", "Other Archaeology", "Museum", "Class", "Other",
                      "Unclassified"]


def connect(path=None, readonly=False, timeout=60):
    """The catalogue (default: the one in config.json). readonly opens it so nothing can be
    changed, which also lets other programs write to it meanwhile (it is in WAL mode)."""
    p = Path(path or CONFIG.catalogue)
    if readonly:
        return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=timeout, check_same_thread=False)
    return sqlite3.connect(p, timeout=timeout)


def has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def columns(db, table):
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


def human(n):
    """1234567 -> '1.2 MB' (decimal units, as the Finder shows them)."""
    n = float(n or 0)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{int(n)} bytes" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1000


def gb(n):
    return round((n or 0) / 1e9, 6)


def plain(v):
    return f"{v:.6f}".rstrip("0").rstrip(".") if isinstance(v, float) else v


def write_csv(path, header, rows):
    """A CSV that Excel and Numbers open cleanly (UTF-8 with BOM, plain numbers)."""
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in rows:
            w.writerow([plain(v) for v in r])


def log(*a):
    print(*a, file=sys.stderr, flush=True)
