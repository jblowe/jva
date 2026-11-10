#!/usr/bin/env python3
"""
copy_originals.py -- put one copy of each distinct document (MS Word, PDF, spreadsheet,
database) in the derivatives folder, so the web server can offer the original for download.

    python3 scripts/copy_originals.py                  # into <derivatives_dir>/originals
    python3 scripts/copy_originals.py -d "/Volumes/TAP PROJECT/_derivatives"
    python3 scripts/copy_originals.py --dry-run        # only count what would be copied

Each distinct file of those kinds (clutter is never a distinct file) is copied to

    <derivatives folder>/originals/<archive drive>/<its path on that drive>

using the copy the catalogue names for it (unique_files.archive / path), so the folders are
those of the archive. If that drive isn't plugged in, or that copy has changed, another copy
of the same file is read instead, but it is still put at the catalogue's path. Each copy is
checked against the catalogue's SHA-256 as it is written, and keeps its modification date.
It is recorded in the catalogue table `originals` (checksum -> path in the derivatives
folder); publish.py takes the ones in public categories to the web server, and the search
app offers them as "Download" on the file's page.

Rerunning is safe: files already copied (same size, recorded) are skipped, so it can be run
again after plugging in the other drive, or after adding drives to the archive. Files
offloaded to S3 (stubs on the drives) can't be copied and are listed at the end.
"""
import argparse
import hashlib
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # the repository, for jva
from jva.config import CONFIG  # noqa: E402
from jva.catalog import ORIGINALS, connect, human, log  # noqa: E402
from jva.offload import not_offloaded  # noqa: E402

KINDS = ("MS Word", "PDF", "Spreadsheet", "Database")
FOLDER = "originals"
CHUNK = 4 << 20


class DestGone(Exception):
    """The derivatives folder went away (its drive unplugged): stop."""


def wanted(db, kinds):
    """The distinct files to copy: rows (id, sha256, size, archive, path, name)."""
    marks = ", ".join("?" * len(kinds))
    return db.execute(f"""SELECT id, sha256, size_bytes, archive, path, name FROM unique_files
                          WHERE kind IN ({marks}) AND sha256 IS NOT NULL AND sha256 <> ''
                          ORDER BY archive, path""", list(kinds)).fetchall()


def sources(db, uid, archive, path, size, volumes_root):
    """Readable copies of a distinct file, the catalogue's own copy first."""
    rows = db.execute(f"""SELECT f.archive, f.path FROM files f WHERE f.unique_id = ? AND f.junk = 0
                          {not_offloaded(db, 'f')}
                          ORDER BY (f.archive = ? AND f.path = ?) DESC, f.archive, f.path""",
                      (uid, archive, path)).fetchall()
    for a, p in rows:
        full = Path(volumes_root) / a / p
        try:
            if full.is_file() and full.stat().st_size == size:
                yield full
        except OSError:
            continue


def copy_checked(src, dest, sha256, root):
    """Copy src to dest, checking the SHA-256 as it goes. -> None, or why it failed."""
    tmp = dest.with_name(dest.name + ".copying")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        if not root.is_dir():
            raise DestGone(str(e))
        return f"can't make the folder: {e}"
    h = hashlib.sha256()
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                b = fi.read(CHUNK)
                if not b:
                    break
                h.update(b)
                fo.write(b)
    except OSError as e:
        if not root.is_dir():
            raise DestGone(str(e))
        try:
            tmp.unlink()
        except OSError:
            pass
        return f"read or write error: {e}"
    if h.hexdigest() != sha256:
        tmp.unlink()
        return "the copy read doesn't match the catalogue's checksum"
    try:
        shutil.copystat(src, tmp)        # keep the modification date
    except OSError:
        pass
    os.replace(tmp, dest)
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-d", "--derivatives", default=str(CONFIG.derivatives_dir or ""),
                    help="the derivatives folder (default: derivatives_dir in config.json)")
    ap.add_argument("--catalogue", default=str(CONFIG.catalogue))
    ap.add_argument("--kinds", default=",".join(KINDS), help=f"comma-separated (default {','.join(KINDS)})")
    ap.add_argument("--dry-run", action="store_true", help="only count what would be copied")
    a = ap.parse_args()
    if not a.derivatives:
        sys.exit("Which derivatives folder? Give -d, or set derivatives_dir in config.json.")
    root = Path(a.derivatives)
    if not root.is_dir():
        sys.exit(f"The derivatives folder isn't there: {root} (is its drive plugged in?)")
    kinds = [k.strip() for k in a.kinds.split(",") if k.strip()]
    db = connect(a.catalogue)
    db.executescript(ORIGINALS)
    done = {sha: (rel, size) for sha, rel, size in db.execute("SELECT sha256, rel, size FROM originals")}
    rows = wanted(db, kinds)
    total = sum(r[2] or 0 for r in rows)
    log(f"{len(rows):,} distinct files ({', '.join(kinds)}), {human(total)}")
    n_copied = n_kept = 0
    bytes_copied = 0
    unavailable, failed = [], []
    t0 = last = time.time()
    try:
        for i, (uid, sha, size, archive, path, name) in enumerate(rows, 1):
            rel = f"{FOLDER}/{archive}/{path}"
            dest = root / rel
            if done.get(sha) == (rel, size) and dest.is_file() and dest.stat().st_size == size:
                n_kept += 1
                continue
            if a.dry_run:
                n_copied += 1
                bytes_copied += size or 0
                continue
            why = "no copy on a plugged-in drive (or every copy has changed, or is offloaded)"
            for src in sources(db, uid, archive, path, size, CONFIG.volumes_root):
                why = copy_checked(src, dest, sha, root)
                if why is None:
                    break
            if why is None:
                db.execute("INSERT OR REPLACE INTO originals(sha256, rel, size, copied_at) "
                           "VALUES (?, ?, ?, datetime('now'))", (sha, rel, size))
                n_copied += 1
                bytes_copied += size or 0
            elif why.startswith("no copy"):
                unavailable.append((archive, path))
            else:
                failed.append((archive, path, why))
            if time.time() - last > 30:
                db.commit()
                last = time.time()
                log(f"  {i:,} of {len(rows):,}: {n_copied:,} copied ({human(bytes_copied)}), "
                    f"{n_kept:,} already there, {len(unavailable) + len(failed):,} not copied")
    except DestGone as e:
        db.commit()
        sys.exit(f"The derivatives folder went away ({e}): is its drive still plugged in? "
                 f"What was copied is recorded; run this again to go on.")
    except KeyboardInterrupt:
        db.commit()
        sys.exit("Stopped. What was copied is recorded; run this again to go on.")
    db.commit()
    db.close()
    verb = "would copy" if a.dry_run else "copied"
    print(f"{verb} {n_copied:,} files ({human(bytes_copied)}) in {time.time() - t0:.0f}s; "
          f"{n_kept:,} were already there; into {root / FOLDER}")
    if unavailable:
        drives = sorted({x[0] for x in unavailable})
        print(f"{len(unavailable):,} not copied: no readable copy (drives: {', '.join(drives)}). "
              f"Plug in the drive and run this again.")
    if failed:
        print(f"{len(failed):,} failed:")
        for archive, path, why in failed[:20]:
            print(f"  {archive}: {path}: {why}")


if __name__ == "__main__":
    main()
