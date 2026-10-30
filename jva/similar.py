"""Second-level grouping of distinct files: files that are not identical (different SHA-256)
but are versions of the same thing. Two passes over unique_files:

  pass 1  same name     identical file names (ignoring case and extension: x.jpg = x.TIF),
                        wherever they are -- except
                        names a camera, scanner or program makes up (IMG_0001.JPG, DSC_0090.JPG,
                        Untitled 3.jpg, 001.jpg ...), which say nothing about the content
  pass 2  same subject  names, or the folder they are in, that name the same object: e.g.
                        "10091.JPG", "10091 another 3.JPG" and "10091 NPW/DSC_0575.JPG" are all
                        object 10091

Each rule is a regular expression on the file name without its extension (or on the folder's
name), and the group's key comes from what it matches. All matching is case-insensitive, on
the Unicode-normalised name. group() returns, for every unique file, its group and how it got
there, so identify_archive.py can store it and the search app can show identical copies and
similar versions separately.
"""
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass

I = re.IGNORECASE
SITES = r"(?:NPW|NML|NKH|TK|PL|PF|BNW|BVM|KTW)"

# Made-up names: never grouped by name alone (pass 1), nor by the name rules of pass 2.
GENERIC = [
    ("camera: IMG_1234, IMG1234", r"img[_\-]?\d+"),
    ("camera: DSC_1234, DSCN1234, DSCF1234, DSC01234", r"dsc[nf_]?\d+"),
    ("camera: P1010001, PICT0001, CIMG0001, DCP_0001, MVC-001, IMAG0001",
     r"(p\d{6,8}|pict\d+|cimg\d+|dcp[_\-]?\d+|mvc[_\-]?\d+[a-z]?|imag\d+|sam_\d+|gopr\d+|mvi_\d+|mov_?\d+|_mg_\d+|_dsc\d+)"),
    ("camera: 100_0001, 2003-01-22-0026", r"(\d{3}_\d{4}|\d{4}-\d\d-\d\d[\-_ ]\d+)"),
    ("scanner or program: Image1, image001, Picture 3, Scan 12, Untitled-2, Document1 ...",
     r"(image|img|picture|pic|photo|scan|scanned|untitled|document|doc|new document|copy|file|"
     r"clip_image|screen ?shot|screenshot|capture|snapshot|thumb|thumbnail)[ _\-]*\(?\d*\)?( copy( \d+)?)?"),
    ("plain small number: 1, 01, 001, 0042", r"\d{1,3}|0\d{3}"),
    ("web page parts: index, default, home, spacer, bullet, logo, banner",
     r"(index|default|home|main|spacer|bullet|blank|clear|dot|arrow|logo|banner|header|footer|"
     r"style|styles|readme|read me)[_\-]?\d*"),
    ("PowerPoint web pages: sld001.htm, tsld002.htm, slide0001, next.gif, outline ...",
     r"(t?sld\d+|slide\d+|slide\d+_image\d+|master\d+|outline|fullscreen|frame|script|next|prev|first|last|"
     r"text|space|info|expand|collapse|activep|activem|ielogo|nextarrow|prevarrow|buttons?|"
     r"pres|filelist|preview|thumbnail)"),
    ("DVD and scanner files: VIDEO_TS.VOB, VTS_01_1.VOB, Page 1.pdf, clientname.txt",
     r"(video_ts|vts_\d\d_\d|page[ _]?\d+|clientname|usermanual|userguide|user guide|manual|eula|wdeula|license|licence)"),
    ("program files: INFO.TXT, FILEID.DAT, Desktop, Icon, data, contents ...",
     r"(info|fileid|data|contents|desktop|icon|version|properties|meta|manifest|settings|"
     r"index\.apxl|document|metadata|buildversionhistory|preview-micro|preview-web)"),
]
# Files inside these folders are parts of something else (a Keynote or Pages document, a web
# page saved with its pictures, an application): their names are never grouped.
PART_OF = re.compile(r"(\.(key|pages|numbers|rtfd|app|lrdata|fcpbundle|photoslibrary|band|lpdf)/|"
                     r"_files/|/(Contents|Resources|QuickLook|Data|Index)/)", I)
GENERIC_RX = [(label, re.compile(rf"^(?:{rx})$", I)) for label, rx in GENERIC]

IMAGE_EXT = {".jpg", ".jpeg", ".jpe", ".tif", ".tiff", ".png", ".psd", ".bmp", ".gif", ".pct", ".pict",
             ".nef", ".cr2", ".crw", ".dng", ".heic", ".jp2"}


@dataclass
class Rule:
    name: str          # shown in the reports
    rx: re.Pattern     # matched against the name without extension (or the folder's name)
    key: str           # template for the group key, e.g. r"\1"
    on: str = "name"   # "name" or "folder"
    images_only: bool = True

    def match(self, text):
        m = self.rx.match(text)
        return m.expand(self.key).strip().lower() if m else None


NUM = r"(\d{4,6}(?:\.\d{1,2})?)"            # catalogue numbers: 10091, 39903.4
YEAR = r"(?:19[5-9]\d|20[0-2]\d)"           # 4-digit numbers that are much more often years
SITE_WORDS = ("npw", "nml", "nkh", "tk", "pl", "pf", "bnw", "ktw")
RULES = [
    # a letter after the number is part of it (18536e is one piece of lot 18536); a number
    # followed by "_17" or "-3" is a date or event code (1404_17_Celebration_046.jpg), not an object
    Rule("object number at the start of the name: 10091, 10091 another 3, 10091 side, 39903.4b",
         re.compile(rf"^(?!{YEAR}(?:\D|$))(\d{{4,6}}(?:\.\d{{1,2}})?[a-z]?)(?=$|[ (,]|[_\-](?!\d))", I), r"\1",
         images_only=False),
    Rule("museum accession number: 2006-12, 2006-12 side view, 2018-1 another 5",
         re.compile(r"^((?:19[5-9]\d|20[0-2]\d)-\d{1,4})(?=$|[ _(,a-z])", I), r"\1", images_only=False),
    Rule("BNW catalogue number: BNW cat 12.3, BNW Cat. 7",
         re.compile(r"^bnw[ _]cat\.?[ _]?(\d+(?:\.\d+)?)(?=$|[ _\-(,a-z])", I), r"bnw cat \1", images_only=False),
    Rule("BNW number: BNW 1234",
         re.compile(r"^bnw[ _](\d{2,6}(?:\.\d+)?)(?=$|[ _\-(,a-z])", I), r"bnw \1", images_only=False),
    Rule("catalogue number: cat 12.3",
         re.compile(r"^cat\.?[ _](\d+(?:\.\d+)?)(?=$|[ _\-(,a-z])", I), r"cat \1", images_only=False),
    Rule("TAP database sketch: T 31415",
         re.compile(r"^t[ _](\d{4,6})$", I), r"t \1", images_only=False),
    Rule("TAP slide scan: TAP 85 Rol 25 #13 ..., TAP92 R12 NPW 34",
         re.compile(r"^tap[ _]?(\d\d)[ _]?(?:rol|roll|r)[ _]?(\d+)[ _]+(?:(?:" + "|".join(SITE_WORDS) +
                    r")[ _]+)?#?(\d+)(?=$|[ _\-,a-z])", I), r"tap\1 r\2 f\3", images_only=False),
    Rule("Phu Lon slide scan: PL_R12_34, PL_L11_32",
         re.compile(r"^pl_([a-z])(\d+)_(\d+)$", I), r"pl \1\2 f\3", images_only=False),
    Rule("China slide: China 3 Box 2 Picture 14",
         re.compile(r"^china (\d+) box (\d+) picture (\d+)", I), r"china \1 box \2 picture \3", images_only=False),
    Rule("object folder: files in '10091 NPW', '5273  NKH', '16115 NPW Done Cloud'",
         re.compile(rf"^(?!{YEAR}\s){NUM}\s+{SITES}\b", I), r"\1", on="folder", images_only=False),
]
# the rules whose key is an object number (checked for numbers used at more than one site)
OBJECT_RULES = {RULES[0].name, RULES[-2].name}


def sites_named(name, folder=""):
    """The site abbreviations written in a file's name and its folder's name, e.g.
    '20952 NML Stone Adze.jpg' -> {'nml'}."""
    words = set(re.findall(r"[a-z]+", split(name)[0] + " " + norm(folder)))
    return words & set(SITE_WORDS)


def norm(s):
    return unicodedata.normalize("NFC", s or "").strip().casefold()


def split(name):
    """'10091 another 3.JPG' -> ('10091 another 3', '.jpg')"""
    n = norm(name)
    stem, dot, ext = n.rpartition(".")
    if not dot or not stem or len(ext) > 5 or " " in ext:
        return n, ""
    return stem, "." + ext


def generic(name):
    """The GENERIC label a made-up name matches, or None."""
    stem, _ = split(name)
    stem = re.sub(r"( copy( \d+)?| \(\d+\)|-\d)$", "", stem)        # "IMG_0001 copy", "Scan (2)"
    for label, rx in GENERIC_RX:
        if rx.match(stem):
            return label
    return None


def pass1_key(name, path=""):
    """Pass 1: the name without its extension -- so 'Burial 2.jpg', 'Burial 2.tif' and
    'Burial 2.JPG' are the same name -- unless it is a made-up one or part of something else."""
    return None if generic(name) or PART_OF.search(path or "") else split(name)[0]


def same_name(a, b):
    """True when two file names are the same apart from case and extension."""
    return split(a)[0] == split(b)[0]


def pass2_key(name, folder, rules=RULES, path=""):
    """Pass 2: (rule, key) of the first rule that applies, or (None, None)."""
    if PART_OF.search(path or ""):
        return None, None
    stem, ext = split(name)
    is_image = ext in IMAGE_EXT
    made_up = generic(name)
    for r in rules:
        if r.images_only and not is_image:
            continue
        if r.on == "folder":
            k = r.match(norm(folder))
        elif made_up:
            continue
        else:
            k = r.match(stem)
        if k:
            return r, k
    return None, None


class _Union:
    def __init__(self):
        self.p = {}

    def find(self, x):
        p = self.p
        p.setdefault(x, x)
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.p[max(a, b)] = min(a, b)


def group(items, rules=RULES):
    """items: iterable of (unique_id, name, folder, path) -- folder = the last folder in the path.
    -> ({unique_id: (group_id, how, key)}, {group_id: label}). how is "same name", the name
    of the pass-2 rule that grouped it, or "" (in no group); key is the pass-2 key if any.
    group_id is the smallest unique_id in the group; the label is the commonest pass-2 key
    in the group (e.g. "10091"), else the commonest name."""
    items = list(items)
    u = _Union()
    by_name, by_key, how, key_of = defaultdict(list), defaultdict(list), {}, {}
    for uid, name, folder, path in items:
        u.find(uid)
        k = pass1_key(name, path)
        if k:
            by_name[k].append(uid)
    for k, uids in by_name.items():
        if len(uids) > 1:
            for x in uids[1:]:
                u.union(uids[0], x)
            for x in uids:
                how[x] = "same name"
    for uid, name, folder, path in items:
        r, k = pass2_key(name, folder, rules, path)
        if r:
            by_key[k].append((uid, r))
    for k, members in by_key.items():
        if len(members) < 2:
            continue
        for x, r in members[1:]:
            u.union(members[0][0], x)
        for x, r in members:
            how.setdefault(x, r.name)
            key_of[x] = k
    out, keys, names = {}, defaultdict(Counter), defaultdict(Counter)
    for uid, name, folder, path in items:
        g = u.find(uid)
        out[uid] = (g, how.get(uid, ""), key_of.get(uid, ""))
        names[g][name] += 1
        if uid in key_of:
            keys[g][key_of[uid]] += 1
    labels = {g: (keys[g].most_common(1)[0][0] if keys[g] else names[g].most_common(1)[0][0])
              for g in names}
    return out, labels


# ---------- in the catalogue ----------

FILE_GROUPS = """
DROP TABLE IF EXISTS file_groups;
CREATE TABLE file_groups(
  group_id INTEGER PRIMARY KEY,     -- = unique_files.id of the group's main file
  label TEXT,                       -- what the group is: "10091", "tap85 r25 f13", or the shared name
  members INTEGER,                  -- distinct files in the group
  same_name INTEGER,                -- of them, grouped by identical name (pass 1)
  similar INTEGER);                 -- of them, grouped by a pass-2 rule
"""
GROUP_COLUMNS = (("group_id", "INTEGER"), ("group_how", "TEXT"), ("group_key", "TEXT"))


def catalogue_items(db):
    """Every distinct file as a dict: uid, name, path, archive, location, ext, size, category,
    kind, and folders (the last folder of every copy, since any copy can be in an object
    folder) and folder (the one pass 2 looks at: an object folder if any copy is in one)."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(unique_files)")}
    kind = "COALESCE(kind, '')" if "kind" in cols else "''"
    info = {}
    for uid, name, kd, ext, mime, mtype, mcreator, size, cat, archive, path in db.execute(
            f"SELECT id, name, {kind}, lower(ext), mime, mac_type, mac_creator, size_bytes, "
            "COALESCE(category, 'Unclassified'), archive, path FROM unique_files"):
        if not kd:
            from jva.kinds import kind_of
            kd = kind_of(ext, mime, mtype, mcreator)
        info[uid] = {"uid": uid, "name": name, "kind": kd, "ext": ext, "size": size or 0, "category": cat,
                     "archive": archive, "path": path, "location": f"{archive}: {path}", "folders": set()}
    for uid, path in db.execute("SELECT unique_id, path FROM files WHERE unique_id IS NOT NULL AND junk = 0"):
        if uid in info:
            info[uid]["folders"].add(path.rsplit("/", 2)[-2] if "/" in path else "")
    folder_rule = next(r for r in RULES if r.on == "folder")
    for d in info.values():
        fs = sorted(d["folders"])
        d["folder"] = next((f for f in fs if folder_rule.match(norm(f))), fs[0] if fs else "")
    return info


def main_file(members, label):
    """The file that stands for a group: a picture over anything else, then one whose name is
    just the group's label (10091.JPG), then the shortest name."""
    return min(members, key=lambda d: (d["kind"] != "Image", split(d["name"])[0] != label,
                                       len(d["name"]), d["uid"]))


def build_groups(db, rules=RULES, log=lambda msg: None):
    """(Re)compute the groups for the whole catalogue: fills unique_files.group_id/how/key and
    the file_groups table. Called after unique_files is rebuilt and by publish.py."""
    import time
    t0 = time.time()
    cols = {r[1] for r in db.execute("PRAGMA table_info(unique_files)")}
    for c, typ in GROUP_COLUMNS:
        if c not in cols:
            db.execute(f"ALTER TABLE unique_files ADD COLUMN {c} {typ}")
    info = catalogue_items(db)
    res, labels = group([(d["uid"], d["name"], d["folder"], d["path"]) for d in info.values()], rules)
    members = defaultdict(list)
    for uid, (g, how, key) in res.items():
        members[g].append(info[uid])
    updates, groups = [], []
    for g, ms in members.items():
        main = main_file(ms, labels[g]) if len(ms) > 1 else ms[0]
        hows = Counter(res[d["uid"]][1] for d in ms)
        if len(ms) > 1:
            groups.append((main["uid"], labels[g], len(ms), hows.get("same name", 0),
                           len(ms) - hows.get("same name", 0) - hows.get("", 0)))
        for d in ms:
            _, how, key = res[d["uid"]]
            updates.append((main["uid"], how if len(ms) > 1 else "", key if len(ms) > 1 else "", d["uid"]))
    db.executemany("UPDATE unique_files SET group_id = ?, group_how = ?, group_key = ? WHERE id = ?", updates)
    db.executescript(FILE_GROUPS)
    db.executemany("INSERT INTO file_groups VALUES (?, ?, ?, ?, ?)", groups)
    db.execute("CREATE INDEX IF NOT EXISTS i_u_group ON unique_files(group_id)")
    db.commit()
    log(f"   similar files: {len(info):,} distinct files in {len(members):,} groups "
        f"({len(groups):,} of two or more), {time.time() - t0:.0f}s")
    return len(members)


def leave_group(db, uid):
    """Before distinct file uid is removed from unique_files: if it is its group's main file,
    hand the group to another member, and keep the group's counts right."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(unique_files)")}
    if "group_id" not in cols:
        return
    r = db.execute("SELECT group_id, group_how FROM unique_files WHERE id = ?", (uid,)).fetchone()
    if not r or r[0] is None:
        return
    g, how = r
    rest = [x for (x,) in db.execute("SELECT id FROM unique_files WHERE group_id = ? AND id <> ? ORDER BY id",
                                     (g, uid))]
    has_table = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'file_groups'").fetchone()
    if g == uid and rest:
        db.execute("UPDATE unique_files SET group_id = ? WHERE group_id = ?", (rest[0], uid))
        if has_table:
            db.execute("UPDATE file_groups SET group_id = ? WHERE group_id = ?", (rest[0], uid))
        g = rest[0]
    if has_table:
        if len(rest) <= 1:
            db.execute("DELETE FROM file_groups WHERE group_id = ?", (g,))
            db.execute("UPDATE unique_files SET group_how = '', group_key = '' WHERE id IN (?, ?)",
                       (g, rest[0] if rest else g))
        else:
            db.execute("UPDATE file_groups SET members = members - 1, same_name = same_name - ?, "
                       "similar = similar - ? WHERE group_id = ?",
                       (1 if how == "same name" else 0, 1 if how and how != "same name" else 0, g))
