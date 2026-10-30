#!/usr/bin/env python3
"""
filemaker_to_csv.py -- get the data out of the archive's FileMaker Pro databases, as CSV files.

    python3 scripts/filemaker_to_csv.py                 # into ~/jva_fm
    python3 scripts/filemaker_to_csv.py -o ~/Desktop/fm

Every distinct FileMaker database in the catalogue (kind Database: .fp3, .fp5, .fp7, .fmp12,
or the old Macs' FMPR/FMP3 files with no extension) is read from whichever archive drive with
a copy is plugged in, and each of its tables is written as a CSV file:

  TAP92 Bags.fp7          -> TAP92 Bags.csv
  a database with tables  -> <database> - <table>.csv   (FileMaker 7 and later)
  the same name again     -> TAP92 Bags (2).csv          (a different database, same name)

_index.csv says which CSV came from which database and copy, how many records and fields
it has, and which databases couldn't be read and why. Tables with no records (only field
names) get no CSV, just a line in _index.csv; so do tables with exactly the same fields and
records as one already written (many databases were copied and renamed): the note names the
CSV that has their data. --keep-duplicates writes them all. Running it again replaces the CSVs it
made last time (those listed in _index.csv) and leaves anything else in the folder alone.

The CSVs are UTF-8 (with a byte-order mark, so Excel shows accents right), quoted where
needed by Python's csv module. FileMaker's line breaks inside a field (carriage returns)
become ordinary line breaks inside the quoted value; other control characters become spaces.
Pictures in container fields are not extracted.

Not readable: FileMaker Pro 1 and 2 files (older than the fp3 format), and databases with a
password. Those are listed in _index.csv; old FileMaker (3 to 6) can convert the first kind.

The reading is done by FMP Tools (third_party/fmptools, MIT licence), which this script
compiles the first time, with the Mac's C compiler (xcode-select --install, if it's missing).
"""
import argparse
import csv
import hashlib
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # the repository, for jva
from jva.config import CONFIG  # noqa: E402
from jva.catalog import columns, connect, log  # noqa: E402
from jva.kinds import is_filemaker  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
FMPTOOLS = REPO / "third_party" / "fmptools"
TOOL = FMPTOOLS / "build" / "fmp2sqlite"
INDEX = "_index.csv"
FM_EXT = re.compile(r"\.(fp3|fp5|fp7|fmp12|fmp)$", re.I)
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
ERRORS = {"6": "an older FileMaker format (FileMaker Pro 1 or 2) that the reader doesn't understand",
          "14": "password-protected", "1": "couldn't be opened"}


# ---------- the reader ----------

def build_tool():
    """Compile FMP Tools' fmp2sqlite into third_party/fmptools/build (once). -> its path."""
    if TOOL.exists():
        return TOOL
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if not cc:
        sys.exit("No C compiler found. On a Mac, install the command-line tools:  xcode-select --install")
    src = FMPTOOLS / "src"
    TOOL.parent.mkdir(parents=True, exist_ok=True)
    cmd = [cc, "-O2", "-w", "-DHAVE_STRPTIME", "-DHAVE_FMEMOPEN", "-D_GNU_SOURCE", '-DVERSION="jva"',
           f"-I{src}", "-o", str(TOOL),
           *[str(p) for p in sorted(src.glob("*.c"))], str(src / "bin" / "fmp2sqlite.c"),
           str(src / "bin" / "usage.c"), "-lsqlite3"]
    if platform.system() == "Darwin":
        cmd.append("-liconv")
    log("compiling FMP Tools (once) ...")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        hint = ""
        if "sqlite3.h" in r.stderr:
            hint = ("\nSQLite's header file is missing: on a Mac, xcode-select --install; "
                    "on Ubuntu, sudo apt install libsqlite3-dev.")
        sys.exit("Couldn't compile FMP Tools:\n" + r.stderr[-2000:] + hint)
    return TOOL


def read_database(tool, path, workdir):
    """-> (sqlite path, None) or (None, why not)"""
    out = Path(workdir) / "db.sqlite"
    if out.exists():
        out.unlink()
    try:
        r = subprocess.run([str(tool), str(path), str(out)], capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return None, "took more than half an hour"
    if r.returncode != 0 or not out.exists():
        code = re.search(r"Error code: (\d+)", r.stderr or "")
        return None, ERRORS.get(code.group(1), f"couldn't be read (error {code.group(1)})") if code else \
            "couldn't be read"
    return out, None


def clean(v):
    if v is None:
        return ""
    if isinstance(v, bytes):
        return f"<binary data, {len(v)} bytes>"
    v = str(v).replace("\r\n", "\n").replace("\r", "\n").replace("\x0b", "\n")
    return CONTROL.sub(" ", v)


def write_table(db, table, dest):
    """One table of the converted database -> a CSV. -> (records, fields)"""
    cur = db.execute(f'SELECT * FROM "{table}"')
    names = [c[0] for c in cur.description]
    n = 0
    with open(dest, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
        w.writerow([clean(c) for c in names])
        for row in cur:
            w.writerow([clean(v) for v in row])
            n += 1
    return n, len(names)


# ---------- the archive's databases ----------

def filemaker_files(db):
    """Distinct FileMaker databases in the catalogue, as dicts."""
    kind = "AND kind = 'Database'" if "kind" in columns(db, "unique_files") else ""
    out = []
    for uid, sha, name, ext, mtype, fmt, copies in db.execute(
            f"SELECT id, sha256, name, ext, mac_type, format, copies FROM unique_files WHERE 1 {kind}"):
        if is_filemaker(ext, mtype, fmt):
            out.append({"uid": uid, "sha": sha or "", "name": name, "copies": copies})
    return sorted(out, key=lambda d: (d["name"].lower(), d["uid"]))


def copies_of(db, uid, volumes_root):
    """(path on the drive, "archive: path") of each copy, those on plugged-in drives first."""
    rows = db.execute("SELECT archive, path, size FROM files WHERE unique_id = ? AND junk = 0 ORDER BY archive, path",
                      (uid,)).fetchall()
    found = [(Path(volumes_root) / a / p, f"{a}: {p}") for a, p, s in rows
             if (Path(volumes_root) / a / p).is_file()]
    return found, (f"{rows[0][0]}: {rows[0][1]}" if rows else "")


def safe(name):
    return re.sub(r"[/:\x00-\x1f]", "-", name).strip() or "untitled"


def clear_previous(out):
    """Remove the CSVs the last run made (listed in its _index.csv)."""
    idx = out / INDEX
    if not idx.exists():
        return
    with open(idx, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            f = (r.get("csv_file") or "").strip()
            if f and "/" not in f and (out / f).is_file():
                (out / f).unlink()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", default="~/jva_fm", help="the folder for the CSVs (default ~/jva_fm)")
    ap.add_argument("--keep-duplicates", action="store_true",
                    help="write a CSV for every table, even when another has exactly the same data")
    ap.add_argument("--catalogue", default=str(CONFIG.catalogue))
    a = ap.parse_args()
    tool = build_tool()
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    clear_previous(out)
    db = connect(a.catalogue, readonly=True)
    docs = filemaker_files(db)
    log(f"{len(docs):,} distinct FileMaker databases in the catalogue")
    used, written, index, n_records, n_ok = {}, {}, [], 0, 0
    with tempfile.TemporaryDirectory() as work:
        for i, d in enumerate(docs, 1):
            paths, where = copies_of(db, d["uid"], CONFIG.volumes_root)
            base = safe(FM_EXT.sub("", d["name"]))
            row = [d["name"], where, d["copies"], d["sha"]]
            if not paths:
                index.append(["", *row[:1], "", "", "", *row[1:], "not read: no drive with a copy is plugged in"])
                continue
            path, where = paths[0]
            row[1] = where
            conv, why = read_database(tool, path, work)
            if conv is None:
                index.append(["", d["name"], "", "", "", *row[1:], f"not read: {why}"])
                continue
            cdb = sqlite3.connect(conv)
            tables = [t for (t,) in cdb.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY rowid")]
            for t in tables:
                tmp = Path(work) / "table.csv"
                recs, fields = write_table(cdb, t, tmp)
                if recs == 0:                       # field names only: no CSV, just a line in the index
                    index.append(["", d["name"], t, 0, fields, *row[1:], "no CSV: the table has no records"])
                    continue
                digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
                if digest in written and not a.keep_duplicates:     # the very same fields and records
                    index.append(["", d["name"], t, recs, fields, *row[1:],
                                  f"no CSV: the same data as {written[digest]}"])
                    continue
                stem = base if len(tables) == 1 or t == FM_EXT.sub("", d["name"]) else f"{base} - {safe(t)}"
                k = stem.lower()
                used[k] = used.get(k, 0) + 1
                fname = f"{stem}.csv" if used[k] == 1 else f"{stem} ({used[k]}).csv"
                shutil.move(str(tmp), out / fname)
                written[digest] = fname
                n_records += recs
                index.append([fname, d["name"], t, recs, fields, *row[1:], ""])
            cdb.close()
            n_ok += 1
            if i % 25 == 0:
                log(f"  {i:,} of {len(docs):,} databases")
    db.close()
    with open(out / INDEX, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow(["csv_file", "database", "table", "records", "fields", "where the database is (one copy)",
                    "copies", "sha256", "note"])
        w.writerows(index)
    n_csv = sum(1 for r in index if r[0])
    n_empty = sum(1 for r in index if r[-1] == "no CSV: the table has no records")
    n_dup = sum(1 for r in index if r[-1].startswith("no CSV: the same data"))
    print(f"Wrote {n_csv:,} CSV files ({n_records:,} records) from {n_ok:,} of {len(docs):,} FileMaker "
          f"databases into {out}; left out {n_empty:,} empty tables and {n_dup:,} tables with the same data "
          f"as another; {len(docs) - n_ok:,} databases couldn't be read (see {INDEX}).")


if __name__ == "__main__":
    main()
