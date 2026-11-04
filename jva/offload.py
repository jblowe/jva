"""Files moved to cold storage (Amazon S3 Glacier Deep Archive) but kept in the catalogue.

An offloaded copy keeps its row in `files` -- its checksum, size, format, category and
preview images stay, so it is still in the search app -- while the file on the archive drive
is replaced by a STUB: a short text file with the same name saying where the file is now.
Each offloaded copy is recorded in the table `offloaded` (scripts/offload_files.py does the
offloading). Everything that reads files from the drives leaves offloaded copies alone:
identify_archive.py keeps their rows as they are, drive_inventory.py --redo never copies them
back, free_space.py, make_derivatives.py, check_tiffs.py and repair_tiffs.py never use them,
and the search app shows where each one is instead of opening it. If the real file is put
back in place (restored from S3), identify_archive.py notices (the file has its catalogued
size again) and drops its record here.
"""
from jva.catalog import has_table

SCHEMA = """
CREATE TABLE IF NOT EXISTS offloaded(
  archive TEXT, path TEXT,          -- the copy on this archive drive (now a stub)
  sha256 TEXT, size INTEGER, mtime REAL, modified TEXT,   -- the file's, as catalogued
  location TEXT,                    -- where it is now: s3://<bucket>/<key>
  storage_class TEXT,               -- e.g. DEEP_ARCHIVE
  offloaded_at TEXT, reason TEXT,
  PRIMARY KEY (archive, path));
CREATE INDEX IF NOT EXISTS i_off_sha ON offloaded(sha256);
"""


def stub_text(location, storage_class, when):
    return (f"{location}\n\n"
            f"This file was moved to Amazon S3 ({storage_class}) on {when[:10]} to free space on this\n"
            f"drive. It is still in the archive catalogue and the search app (table `offloaded`).\n"
            f"Restoring it from {storage_class} takes 12 to 48 hours.\n")


def paths(db, archive):
    """Archive-relative paths of the offloaded copies on this archive drive."""
    if not has_table(db, "offloaded"):
        return set()
    return {p for (p,) in db.execute("SELECT path FROM offloaded WHERE archive=?", (archive,))}


def not_offloaded(db, alias="f"):
    """SQL to AND onto a query over `files` (as `alias`) that leaves out offloaded copies."""
    if not has_table(db, "offloaded"):
        return ""
    return (f" AND NOT EXISTS (SELECT 1 FROM offloaded o_ WHERE o_.archive = {alias}.archive"
            f" AND o_.path = {alias}.path)")
