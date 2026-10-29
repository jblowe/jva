"""The public copy of the catalogue, for the web server (scripts/publish.py).

build_public() copies the catalogue and, in the copy only:
  * removes the distinct files in the excluded categories (Class and Unclassified unless
    config.json says otherwise), with all their copies, classification rows and derivatives
  * removes clutter rows (.DS_Store, "._" files, empty files)
  * drops the housekeeping tables: removed_copies, deleted_files, tiff_repairs
  * forgets the drives' volume UUIDs and the Mac paths of the derivatives folder
  * marks a preview as missing if its image file isn't in the derivatives folder, so the
    site shows the placeholder icon instead of a broken image
  * rebuilds the full-text index and the search helper tables, and compacts the file
    (rollback journal, not WAL: the server only ever reads it)
and lists the derivative files the public catalogue uses (derivatives_files.txt), which is
what gets uploaded. The original catalogue is only read.
"""
import os
import sqlite3
from pathlib import Path

from jva.catalog import connect, has_table
from jva.search_prep import prepare

PUBLIC_NAME = "archive_public.sqlite"
LIST_NAME = "derivatives_files.txt"
DROP_TABLES = ("removed_copies", "deleted_files", "tiff_repairs",
               "unique_archives", "unique_drives", "unique_tags", "search_meta")   # helpers rebuilt
DERIVATIVE_FILES = ("thumb", "preview", "pdf", "rtf")


def build_public(src, out_dir, exclude, derivatives_dir, step=lambda msg: None):
    """-> dict of counts. Writes <out_dir>/archive_public.sqlite and derivatives_files.txt."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    final, tmp = out_dir / PUBLIC_NAME, out_dir / (PUBLIC_NAME + ".tmp")
    for p in (tmp, Path(str(tmp) + "-journal")):
        if p.exists():
            p.unlink()
    s = connect(src, readonly=True)
    d = sqlite3.connect(tmp)
    s.backup(d)                       # a consistent snapshot, even while other scripts write
    s.close()
    d.execute("PRAGMA journal_mode=DELETE")
    step("copied the catalogue")

    counts = {}
    marks = ", ".join("?" * len(exclude)) or "NULL"
    with d:
        counts["excluded"] = d.execute(
            f"SELECT COUNT(*) FROM unique_files WHERE COALESCE(category, 'Unclassified') IN ({marks})",
            list(exclude)).fetchone()[0]
        if has_table(d, "unique_search"):
            d.execute("DROP TABLE unique_search")       # rebuilt below; faster than updating it
        d.execute(f"DELETE FROM unique_files WHERE COALESCE(category, 'Unclassified') IN ({marks})",
                  list(exclude))
        d.execute("""DELETE FROM files WHERE junk <> 0 OR unique_id IS NULL
                     OR unique_id NOT IN (SELECT id FROM unique_files)""")
        if has_table(d, "file_class"):
            d.execute("""DELETE FROM file_class WHERE NOT EXISTS
                         (SELECT 1 FROM files f WHERE f.archive = file_class.archive AND f.path = file_class.path)""")
        keep_sha = "(SELECT sha256 FROM unique_files WHERE sha256 <> '')"
        if has_table(d, "content_class"):
            d.execute(f"DELETE FROM content_class WHERE sha256 NOT IN {keep_sha}")
        if has_table(d, "derivatives"):
            d.execute(f"DELETE FROM derivatives WHERE sha256 NOT IN {keep_sha}")
        for t in DROP_TABLES:
            d.execute(f"DROP TABLE IF EXISTS {t}")
        if has_table(d, "archives"):
            d.execute("UPDATE archives SET uuid = ''")
    step(f"left out {counts['excluded']:,} distinct files in {', '.join(exclude) or 'no categories'}, and the clutter")

    from jva.similar import build_groups           # regroup what is left
    build_groups(d)
    step("grouped similar files")

    files = check_derivatives(d, derivatives_dir, counts)
    step(f"checked the derivatives: {len(files):,} files to publish, "
         f"{counts['missing_previews']:,} previews missing")

    from jva.catalog import UNIQUE_SEARCH
    try:
        d.executescript(UNIQUE_SEARCH)
    except sqlite3.OperationalError as e:
        step(f"(no full-text index: {e})")
    counts["distinct"] = prepare(d, step=step)
    d.execute("VACUUM")
    counts["copies"] = d.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    d.close()
    os.replace(tmp, final)
    (out_dir / LIST_NAME).write_text("".join(f + "\n" for f in sorted(files)), encoding="utf-8")
    counts["bytes"] = final.stat().st_size
    counts["derivative_files"] = len(files)
    step(f"wrote {final}")
    return counts


def check_derivatives(d, derivatives_dir, counts):
    """The derivative files (relative paths) the public catalogue refers to. Rows whose
    images aren't in the folder are marked status 'missing'; missing PDF/RTF copies are cleared."""
    counts["missing_previews"] = counts["missing_docs"] = 0
    if not has_table(d, "derivatives"):
        return set()
    root = Path(derivatives_dir) if derivatives_dir else None
    have = set()
    rows = d.execute("SELECT sha256, status, thumb, preview, pdf, rtf FROM derivatives").fetchall()
    with d:
        d.execute("UPDATE derivatives SET root = ''")
        for sha, status, *rels in rows:
            rels = dict(zip(DERIVATIVE_FILES, rels))
            found = {k: bool(v) and not v.startswith("placeholder/") and root is not None and (root / v).is_file()
                     for k, v in rels.items()}
            if status == "made" and not (found["thumb"] and found["preview"]):
                d.execute("UPDATE derivatives SET status = 'missing' WHERE sha256 = ?", (sha,))
                counts["missing_previews"] += 1
            elif status == "made":
                have.update((rels["thumb"], rels["preview"]))
            for k in ("pdf", "rtf"):
                if rels[k] and found[k]:
                    have.add(rels[k])
                elif rels[k]:
                    d.execute(f"UPDATE derivatives SET {k} = NULL WHERE sha256 = ?", (sha,))
                    counts["missing_docs"] += 1
    return have
