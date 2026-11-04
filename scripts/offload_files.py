#!/usr/bin/env python3
"""
offload_files.py -- free space on the archive drives by offloading files that are already
safely in Amazon S3 Glacier, keeping them in the catalogue and the search app.

  python3 scripts/offload_files.py MANIFEST --bucket NAME --verified FOLDER           # dry run
  python3 scripts/offload_files.py MANIFEST --bucket NAME --verified FOLDER --apply   # do it

  MANIFEST            CSV, one row per distinct file uploaded: columns sha256, archive, path,
                      s3_key (the copy that was uploaded and its key in the bucket), e.g.
                      ~/Claude/Projects/Voelker/glacier/tiff_manifest.csv
  --verified FOLDER   where `rclone check --match` wrote its lists: matched_<archive>.txt
                      (spaces in the archive name as "_"), one path per line, relative to the
                      archive drive. ONLY files listed there are offloaded.
  --bucket NAME       the S3 bucket (recorded as s3://NAME/<s3_key>)
  --storage-class C   recorded with each copy (default DEEP_ARCHIVE)
  --reason TEXT       recorded with each copy
  --limit N           only the first N distinct files (a trial run)
  --apply             without it, nothing is touched: you only get the plan

For each verified distinct file, EVERY copy of it in the catalogue (all identical, by
checksum, on any archive drive) is offloaded, after these checks:
  * the copy's archive drive is plugged in and is the drive the catalogue knows
  * the copy is still there with the size and date in the catalogue
Then the file is replaced by a STUB: a short text file of the same name giving its S3
location (written beside it first, then renamed over it). Its "._" companion (exFAT's Mac
metadata) is left in place. The copy is recorded in the table `offloaded`; its row in
`files` stays as it is, so the file stays in the search app with its preview images.
Copies on drives that aren't plugged in, or that changed since they were catalogued, are
skipped and listed: plug the drive in and run again. Rerunning is safe: copies already
offloaded are skipped.

Writes offload_plan.csv (the plan, or what was done) to the data folder.
Don't run it while another script is using the same drives.
"""
import argparse
import csv
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # the repository, for jva
from jva.config import CONFIG  # noqa: E402
from jva import volumes  # noqa: E402
from jva.catalog import human, log  # noqa: E402
from jva.offload import SCHEMA, stub_text  # noqa: E402

PROJECT_DIR = str(CONFIG.data_dir)


def read_manifest(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [r for r in csv.DictReader(fh)]


def read_verified(folder, archives):
    """{(archive, path)} listed as identical in S3 by rclone check."""
    out = set()
    for a in archives:
        p = Path(folder) / f"matched_{a.replace(' ', '_')}.txt"
        if p.exists():
            with open(p, encoding="utf-8", newline="") as fh:
                out |= {(a, line) for line in fh.read().split("\n") if line}
    return out


def plan(db, manifest, verified, bucket, limit, volumes_root=None):
    """-> list of dicts (one per copy): archive, path, size, sha256, location, action, why"""
    done = {(a, p) for a, p in db.execute("SELECT archive, path FROM offloaded")}
    problems, out, n = {}, [], 0
    for r in manifest:
        sha, a, p, key = r.get("sha256", ""), r["archive"], r["path"], r.get("s3_key") or f"{r['archive']}/{r['path']}"
        loc = f"s3://{bucket}/{key}"
        if not sha:
            out.append(dict(archive=a, path=p, size="", sha256="", location=loc, action="skip",
                            why="never checksummed: its copies can't be matched safely"))
            continue
        if (a, p) not in verified:
            out.append(dict(archive=a, path=p, size="", sha256=sha, location=loc, action="skip",
                            why="not verified in S3 (not in the rclone check matched list)"))
            continue
        if limit and n >= limit:
            break
        n += 1
        copies = db.execute("SELECT archive, path, size, mtime FROM files WHERE sha256=? AND junk=0 "
                            "ORDER BY archive, path", (sha,)).fetchall()
        for ca, cp, size, mtime in copies:
            row = dict(archive=ca, path=cp, size=size, sha256=sha, location=loc, action="offload", why="")
            if (ca, cp) in done:
                row.update(action="skip", why="already offloaded")
            else:
                if ca not in problems:
                    problems[ca] = volumes.problem(db, ca, volumes_root)
                if problems[ca]:
                    row.update(action="skip", why=problems[ca])
                else:
                    full = volumes.path_of(ca, volumes_root) / cp
                    try:
                        st = os.stat(full)
                        if st.st_size != size or abs(st.st_mtime - (mtime or 0)) > 2:
                            row.update(action="skip", why="changed since it was catalogued")
                    except OSError as e:
                        row.update(action="skip", why=f"can't read it: {e.strerror or e}")
            out.append(row)
    return out


def offload(db, rows, storage_class, reason, volumes_root=None):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    done = freed = 0
    todo = [r for r in rows if r["action"] == "offload"]
    for i, r in enumerate(todo, 1):
        full = volumes.path_of(r["archive"], volumes_root) / r["path"]
        tmp = full.with_name(f".{full.name}.offload-stub")
        meta = db.execute("SELECT mtime, modified FROM files WHERE archive=? AND path=?",
                          (r["archive"], r["path"])).fetchone() or (None, None)
        try:
            tmp.write_text(stub_text(r["location"], storage_class, now), encoding="utf-8")
            os.replace(tmp, full)
        except OSError as e:
            r.update(action="failed", why=str(e))
            try:
                tmp.unlink()
            except OSError:
                pass
            continue
        db.execute("INSERT OR REPLACE INTO offloaded VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (r["archive"], r["path"], r["sha256"], r["size"], meta[0], meta[1], r["location"],
                    storage_class, now, reason or None))
        r["action"] = "offloaded"
        done += 1
        freed += r["size"] or 0
        if i % 200 == 0:
            db.commit()
        if i % 1000 == 0:
            log(f"   {i:,} of {len(todo):,} copies offloaded, {human(freed)} freed")
    db.commit()
    return done, freed


def write_plan(out, rows):
    p = Path(out) / "offload_plan.csv"
    with open(p, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["action", "archive", "path", "size", "sha256", "location", "why"])
        w.writeheader()
        w.writerows(rows)
    return p


def main():
    ap = argparse.ArgumentParser(description="Offload files already in Amazon S3 Glacier, keeping them "
                                             "in the catalogue.")
    ap.add_argument("manifest")
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--verified", required=True, help="folder with rclone check's matched_<archive>.txt")
    ap.add_argument("--storage-class", default="DEEP_ARCHIVE")
    ap.add_argument("--reason", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("-o", "--out", default=PROJECT_DIR, help=argparse.SUPPRESS)
    ap.add_argument("--volumes", default=None, help=argparse.SUPPRESS)      # for testing
    a = ap.parse_args()

    out = Path(a.out).expanduser()
    db = sqlite3.connect(out / "archive_catalog.sqlite", timeout=60)
    db.executescript(SCHEMA)
    manifest = read_manifest(a.manifest)
    verified = read_verified(a.verified, sorted({r["archive"] for r in manifest}))
    log(f"{len(manifest):,} files in the manifest, {len(verified):,} verified in S3")
    rows = plan(db, manifest, verified, a.bucket, a.limit, a.volumes)
    go = [r for r in rows if r["action"] == "offload"]
    by_drive = {}
    for r in go:
        n, b = by_drive.get(r["archive"], (0, 0))
        by_drive[r["archive"]] = (n + 1, b + (r["size"] or 0))
    for d, (n, b) in sorted(by_drive.items()):
        print(f"   {d}: {n:,} copies, {human(b)}")
    skips = {}
    for r in rows:
        if r["action"] == "skip":
            skips[r["why"]] = skips.get(r["why"], 0) + 1
    for why, n in sorted(skips.items(), key=lambda x: -x[1]):
        print(f"   skipped {n:,}: {why}")
    total = sum(r["size"] or 0 for r in go)
    if not a.apply:
        p = write_plan(out, rows)
        print(f"Would offload {len(go):,} copies, freeing {human(total)}. Plan: {p}")
        print("Nothing was changed. Add --apply to do it.")
        return
    done, freed = offload(db, rows, a.storage_class, a.reason, a.volumes)
    p = write_plan(out, rows)
    failed = sum(1 for r in rows if r["action"] == "failed")
    print(f"Offloaded {done:,} copies, freed {human(freed)}" + (f"; {failed:,} failed" if failed else "")
          + f". Details: {p}")


if __name__ == "__main__":
    main()
