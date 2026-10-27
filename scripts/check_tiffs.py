#!/usr/bin/env python3
"""
check_tiffs.py -- find damaged TIFF files in the archive.

  python3 check_tiffs.py                    # every TIFF on the archive drives plugged in
  python3 check_tiffs.py -a "TAP PROJECT"   # only copies on this archive drive
  python3 check_tiffs.py --file x.tif       # just one file, with details

For each distinct TIFF in the catalogue (identical copies are checked once, using a copy on a
drive that is plugged in) it reads the file's header and directory -- not the image itself,
so it is quick -- and checks that:
  * the header and every directory ("IFD") are where they say, inside the file
  * the image data (strips or tiles) lies inside the file
  * the pointers to the Exif, GPS, Interoperability and sub-image directories lead to a real
    directory, not into the image data or past the end of the file. A bad pointer of this kind
    is what stops macOS Preview and Quick Look from opening a file that other programs read
    fine (e.g. "Tap 86 Rol 25 #13 Sq A.tif": its Exif pointer lands in the pixel data).
Writes tiff_check.csv here: one row per TIFF with a problem (what is wrong, where the copy
is, how many copies there are), and prints a summary. Reads only; changes nothing.
"""
import argparse
import csv
import os
import sqlite3
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # the repository, for jva
from jva.config import CONFIG  # noqa: E402
PROJECT_DIR = CONFIG.data_dir             # catalogue, reports and drive records
VOLUMES = CONFIG.volumes_root
TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}
POINTER_TAGS = {34665: "Exif", 34853: "GPS", 40965: "Interoperability", 330: "SubIFD"}
MAX_ENTRIES = 1000            # more entries than this in one directory means it isn't one


class Bad(Exception):
    pass


def check_tiff(path):
    """-> list of problems (strings); [] if the structure is sound."""
    problems = []
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        def read(off, n):
            if off < 0 or off + n > size:
                raise Bad(f"needs bytes {off:,}-{off + n:,} but the file is {size:,} bytes")
            fh.seek(off)
            b = fh.read(n)
            if len(b) < n:
                raise Bad("file ends early (read error)")
            return b

        head = read(0, 8) if size >= 8 else b""
        if head[:2] == b"II":
            e = "<"
        elif head[:2] == b"MM":
            e = ">"
        else:
            return ["not a TIFF (no II/MM byte-order mark)"]
        magic = struct.unpack(e + "H", head[2:4])[0]
        if magic == 43:
            return []                                    # BigTIFF: not checked (rare here)
        if magic != 42:
            return [f"bad TIFF signature ({magic})"]

        def ifd(off, what):
            """-> {tag: (type, count, value_or_offset, inline_bytes)}"""
            if off == 0 or off >= size:
                raise Bad(f"{what} directory offset {off:,} is outside the file ({size:,} bytes)")
            n = struct.unpack(e + "H", read(off, 2))[0]
            if n == 0 or n > MAX_ENTRIES:
                raise Bad(f"{what} directory at {off:,} has {n:,} entries: it is not a directory")
            raw = read(off + 2, n * 12 + 4)
            tags = {}
            for i in range(n):
                tag, typ, cnt = struct.unpack_from(e + "HHI", raw, i * 12)
                inline = raw[i * 12 + 8:i * 12 + 12]
                tags[tag] = (typ, cnt, struct.unpack(e + "I", inline)[0], inline)
            nxt = struct.unpack_from(e + "I", raw, n * 12)[0]
            return tags, nxt

        def values(tags, tag):
            typ, cnt, v, inline = tags[tag]
            sz = TYPE_SIZE.get(typ, 1)
            if typ not in (3, 4, 16):
                return []
            data = inline if sz * cnt <= 4 else read(v, sz * cnt)
            f = {3: "H", 4: "I", 16: "Q"}[typ]
            return list(struct.unpack(e + f * cnt, data[:sz * cnt]))

        data_ranges = []
        images, off, seen = 0, struct.unpack(e + "I", head[4:8])[0], set()
        pointers = []
        while off and images < 64:
            if off in seen:
                problems.append("the directories loop back on themselves")
                break
            seen.add(off)
            try:
                tags, nxt = ifd(off, "image" if images == 0 else f"image {images + 1}")
            except Bad as x:
                problems.append(str(x))
                break
            images += 1
            for tag, (typ, cnt, v, _) in tags.items():          # out-of-line values inside the file
                n = TYPE_SIZE.get(typ, 1) * cnt
                if n > 4 and v + n > size:
                    problems.append(f"tag {tag} data ({n:,} bytes at {v:,}) runs past the end of the file")
            for o_tag, c_tag, what in ((273, 279, "strip"), (324, 325, "tile")):
                if o_tag in tags:
                    try:
                        offs, counts = values(tags, o_tag), values(tags, c_tag) if c_tag in tags else []
                    except Bad as x:
                        problems.append(f"{what} table unreadable: {x}")
                        continue
                    if len(counts) != len(offs):
                        problems.append(f"{len(offs):,} {what} offsets but {len(counts):,} {what} sizes")
                    past = sum(1 for o, c in zip(offs, counts) if o + c > size)
                    if past:
                        problems.append(f"{past:,} of {len(offs):,} {what}s lie past the end of the file "
                                        f"(the file is cut short)")
                    if offs and counts:
                        data_ranges.append((min(offs), max(o + c for o, c in zip(offs, counts))))
            if 273 not in tags and 324 not in tags and images == 1:
                problems.append("the first image has no image data (no strips or tiles)")
            for tag, what in POINTER_TAGS.items():
                if tag in tags:
                    try:
                        for p in values(tags, tag):
                            pointers.append((what, p))
                    except Bad as x:
                        problems.append(f"{what} pointer unreadable: {x}")
            off = nxt

        def inside_data(p):
            return any(lo <= p < hi for lo, hi in data_ranges)

        for what, p in pointers:
            where = " (it points into the image data)" if inside_data(p) else ""
            try:
                tags, _ = ifd(p, what)
                if what == "Exif" and 40965 in tags:
                    pointers.append(("Interoperability", tags[40965][2]))
            except Bad as x:
                problems.append(f"bad {what} pointer: {x}{where}")
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-a", "--archive", help="only copies on this archive drive")
    ap.add_argument("--file", help="check just this one file and show the result")
    ap.add_argument("-o", "--out", default=str(PROJECT_DIR), help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.file:
        pr = check_tiff(a.file)
        print("\n".join(pr) if pr else "OK: no structural problems found")
        sys.exit(1 if pr else 0)

    out = Path(a.out)
    db = sqlite3.connect(f"file:{out / 'archive_catalog.sqlite'}?mode=ro", uri=True)
    mounted = {r[0] for r in db.execute("SELECT name FROM archives")
               if os.path.ismount(os.path.join(VOLUMES, r[0]))}
    if a.archive:
        mounted &= {a.archive}
    if not mounted:
        sys.exit("No archive drive from the catalogue is plugged in." if not a.archive
                 else f'"{a.archive}" is not plugged in.')
    rows = db.execute("""
        SELECT f.sha256, f.archive, f.path, f.size, u.copies, COALESCE(u.category, '')
        FROM files f LEFT JOIN unique_files u ON u.id = f.unique_id
        WHERE f.junk = 0 AND f.sha256 IS NOT NULL AND substr(f.name, 1, 2) <> '._'
          AND (lower(f.ext) IN ('.tif', '.tiff') OR f.mac_type = 'TIFF' OR f.mime = 'image/tiff'
               OR f.puid IN ('fmt/353', 'fmt/152', 'fmt/153', 'fmt/154', 'fmt/155', 'fmt/156',
                             'x-fmt/387', 'x-fmt/388', 'x-fmt/399'))
        ORDER BY f.archive, f.path""").fetchall()
    todo = {}
    for sha, archive, path, size, copies, cat in rows:      # one copy per distinct file
        if archive in mounted and sha not in todo:
            todo[sha] = (archive, path, size, copies or 1, cat)
    skipped = len({r[0] for r in rows}) - len(todo)
    print(f"{len(todo):,} distinct TIFFs to check on {', '.join(sorted(mounted))}"
          + (f" ({skipped:,} more have no copy on a drive that's plugged in)" if skipped else ""))

    bad, missing, t0, last = [], 0, time.time(), time.time()
    for i, (sha, (archive, path, size, copies, cat)) in enumerate(todo.items(), 1):
        full = os.path.join(VOLUMES, archive, path)
        try:
            pr = check_tiff(full)
        except FileNotFoundError:
            missing += 1
            continue
        except OSError as x:
            pr = [f"could not be read: {x}"]
        if pr:
            bad.append([archive, path, size, copies, cat, "; ".join(pr), sha])
        if time.time() - last > 20:
            last = time.time()
            print(f"   {i:,}/{len(todo):,} checked, {len(bad):,} with problems ...", flush=True)
    bad.sort(key=lambda r: (r[0], r[1]))
    with open(out / "tiff_check.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["archive_volume", "path", "bytes", "copies", "category", "problem", "sha256"])
        w.writerows(bad)
    kinds = {}
    for r in bad:
        k = r[5].split(":")[0]
        kinds[k] = kinds.get(k, 0) + 1
    print(f"{len(todo) - missing:,} checked in {time.time() - t0:.0f} s: {len(bad):,} with problems"
          + (f", {missing:,} no longer on the drive" if missing else "") + " -> tiff_check.csv")
    for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]):
        print(f"   {n:6,}  {k}")


if __name__ == "__main__":
    main()
