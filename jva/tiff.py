"""TIFF structure: checking (check_tiffs.py) and repairing (repair_tiffs.py).

check_tiff() reads a TIFF's header and directories -- not the image -- and lists structural
problems: directories or image data outside the file, and pointers to the Exif, GPS,
Interoperability or sub-image directories that lead past the end of the file or into the
image data (the fault that stops macOS Preview and Quick Look from opening a file).
plan_fixes() works out how to remove just the bad pointer entries; apply_fixes() and
undo_fixes() write those few bytes in place, keeping the file's date; move_checksum()
carries a repaired file's previews and category over to its new checksum.
"""
import hashlib
import os
import struct

from jva.catalog import has_table

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


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def plan_fixes(path):
    """-> [(offset, old_bytes, new_bytes, [what was removed])] -- the directories to rewrite."""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        def read(off, n):
            if off < 0 or off + n > size:
                return None
            fh.seek(off)
            b = fh.read(n)
            return b if len(b) == n else None

        head = read(0, 8)
        if not head or head[:2] not in (b"II", b"MM"):
            return []
        e = "<" if head[:2] == b"II" else ">"
        if struct.unpack(e + "H", head[2:4])[0] != 42:
            return []

        def directory(off):
            """-> (entries [(tag, typ, cnt, value, raw12)], next, raw block) or None if not one"""
            if not off or off >= size:
                return None
            nb = read(off, 2)
            if nb is None:
                return None
            n = struct.unpack(e + "H", nb)[0]
            if n == 0 or n > MAX_ENTRIES:
                return None
            raw = read(off, 2 + n * 12 + 4)
            if raw is None:
                return None
            ents = []
            for i in range(n):
                r12 = raw[2 + i * 12: 14 + i * 12]
                tag, typ, cnt, v = struct.unpack(e + "HHII", r12)
                ents.append((tag, typ, cnt, v, r12))
            return ents, raw[2 + n * 12:], raw

        def targets(typ, cnt, v):
            sz = TYPE_SIZE.get(typ, 1) * cnt
            if typ not in (4, 13) or cnt == 0:
                return [v] if cnt == 1 else None
            data = struct.pack(e + "I", v) if sz <= 4 else read(v, sz)
            return list(struct.unpack(e + "I" * cnt, data)) if data else None

        def fix(off, ents, nxt, raw, pointer_tags):
            keep, removed = [], []
            for tag, typ, cnt, v, r12 in ents:
                if tag in pointer_tags:
                    ts = targets(typ, cnt, v)
                    if ts is None or any(directory(t) is None for t in ts):
                        removed.append(f"{pointer_tags[tag]} pointer")
                        continue
                keep.append(r12)
            if not removed:
                return None
            new = struct.pack(e + "H", len(keep)) + b"".join(keep) + nxt
            new += b"\0" * (len(raw) - len(new))
            return off, raw, new, removed

        fixes, off, seen = [], struct.unpack(e + "I", head[4:8])[0], set()
        while off and off not in seen and len(seen) < 64:
            seen.add(off)
            d = directory(off)
            if d is None:
                break
            ents, nxt, raw = d
            f = fix(off, ents, nxt, raw, POINTER_TAGS)
            if f:
                fixes.append(f)
            for tag, typ, cnt, v, _ in ents:                  # inside a sound Exif directory:
                if tag == 34665 and not (f and "Exif pointer" in f[3]):   # its Interoperability pointer
                    ex = directory(v)
                    if ex:
                        g = fix(v, ex[0], ex[1], ex[2], {40965: "Interoperability"})
                        if g:
                            fixes.append(g)
            off = struct.unpack(e + "I", nxt[:4])[0]
    return fixes


def apply_fixes(path, fixes):
    st = os.stat(path)
    with open(path, "r+b") as fh:
        for off, _old, new, _ in fixes:
            fh.seek(off)
            fh.write(new)
        fh.flush()
        os.fsync(fh.fileno())
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))      # keep the file's date


def undo_fixes(path, fixes):
    st = os.stat(path)
    with open(path, "r+b") as fh:
        for off, old, _new, _ in reversed(fixes):
            fh.seek(off)
            fh.write(old)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def move_checksum(db, old, new):
    """Copies whose content changed from `old` to `new`: carry the preview images and the
    category over to the new checksum (kept for the old one while copies with it remain)."""
    still_old = db.execute("SELECT 1 FROM files WHERE sha256=? LIMIT 1", (old,)).fetchone() is not None
    for table in ("derivatives", "content_class"):
        if not has_table(db, table):
            continue
        if db.execute(f"SELECT 1 FROM {table} WHERE sha256=?", (new,)).fetchone():
            continue
        if still_old:
            cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
            sel = ", ".join("?" if c == "sha256" else c for c in cols)
            db.execute(f"INSERT INTO {table} ({', '.join(cols)}) SELECT {sel} FROM {table} WHERE sha256=?",
                       (new, old))
        else:
            db.execute(f"UPDATE {table} SET sha256=? WHERE sha256=?", (new, old))
    if not still_old:
        db.execute("UPDATE unique_files SET sha256=? WHERE sha256=?", (new, old))
    return still_old
