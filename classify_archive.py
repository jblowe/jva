#!/usr/bin/env python3
"""
classify_archive.py -- sort every file in the catalogue into TAP / SE Asian Archaeology /
Other Archaeology / Class / Museum / Other / Unclassified, using the rules in classify_rules.csv.

  python3 classify_archive.py            # classify everything in archive_catalog.sqlite
  python3 classify_archive.py --rules my_rules.csv

No drive needs to be mounted: it works from the catalogue only, so rerun it whenever you
edit the rules or after identify_archive.py has catalogued more.

How a file (one path) is classified -- see the comments in classify_rules.csv:
  1. a strong TAP match anywhere in its folders or name      -> TAP, unless a folder below
     it matches a "nottap" rule (a place outside Thailand, e.g. ".../World Trip/China/...")
  2. otherwise the deepest folder/file name with a strong match decides
  3. otherwise the deepest weak match decides
  4. otherwise                                               -> Unclassified
  (the source-drive folder itself, e.g. "JUDY 3", is not used)
A distinct file (all identical copies together) is TAP if ANY copy is TAP; otherwise the
first of Class, SE Asian Archaeology, Other Archaeology, Museum, Other found among its copies.

Writes to the catalogue:
  file_class(archive, path, category, rule, matched)    one row per file (path)
  content_class(sha256, category, copies_by_category)   one row per distinct checksummed file
  unique_files.category                                  filled in (identify_archive.py keeps it
                                                         up to date when it rebuilds the table)
and to this folder:
  classification_summary.csv   files / distinct files / GB per category, per archive drive
  classification_explain.csv   for each folder name that decided a category: the rule, files,
                               GB, where it is, and the biggest subfolders inside it -- the
                               place to check what a category (e.g. TAP) really contains
  classification_review.csv    folders (2 levels below each source drive) by size, with how
                               their files were classified and the rule that decided most
                               of them -- the place to look for rules to add or fix
"""
import argparse
import csv
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_DIR = "/Users/johnlowe/Claude/Projects/Voelker"
CATEGORIES = ["TAP", "Class", "SE Asian Archaeology", "Other Archaeology", "Museum", "Other",
              "Unclassified"]
RANK = {c: i for i, c in enumerate(CATEGORIES)}       # lower = wins when copies disagree
STRENGTHS = ("override", "nottap", "strong", "broad", "weak")
# folder names that are really a computer / drive / backup, not a subject: a TAP match in such
# a name only counts as weak ("TAP Voelker mac laptop", "TAP - KEEP", "TAP from Museum wd")
CONTAINER = re.compile(r"(?i)\b(desktop|laptop|computer|pc|i?mac|macbook|dell|wd|drive|jump ?drive|"
                       r"thumb ?drives?|backups?|backed up|back up|keep)\b")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def load_rules(path):
    rules = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        lines = [l for l in fh if l.strip() and not l.lstrip().startswith("#")]
    for i, r in enumerate(csv.DictReader(lines), 2):
        pat, cat = (r.get("pattern") or "").strip(), (r.get("category") or "").strip()
        strength = (r.get("strength") or "strong").strip().lower()
        if not pat:
            continue
        if strength not in STRENGTHS:
            sys.exit(f"{path}: unknown strength {strength!r} for pattern {pat!r} "
                     f"(use one of: {', '.join(STRENGTHS)})")
        if strength == "nottap" and cat == "TAP":
            sys.exit(f"{path}: pattern {pat!r}: a nottap rule needs a category other than TAP")
        if cat not in CATEGORIES[:-1]:
            sys.exit(f"{path}: unknown category {cat!r} for pattern {pat!r} "
                     f"(use one of: {', '.join(CATEGORIES[:-1])})")
        try:
            rules.append((re.compile(pat, re.I), cat, strength, pat))
        except re.error as e:
            sys.exit(f"{path}: bad pattern {pat!r}: {e}")
    return rules


def copied_drives(outdir):
    """Folder names drive_inventory.py copied source drives into (their names are just drive
    labels, so they're only used as a last resort)."""
    names = set()
    for f, col in (("archive_map.csv", "archive_name"), ("drives.csv", "archive_name")):
        p = outdir / f
        if p.exists():
            with open(p, newline="", encoding="utf-8-sig") as fh:
                names |= {r[col] for r in csv.DictReader(fh) if r.get(col)}
    return names


def source_parts(path, drive, archive, copied, volumes=frozenset()):
    """-> (names to classify on, drive label used only as a fallback or None).
    For a drive_inventory copy, that's everything below the drive's folder; for any other
    folder on the volume (e.g. "TAP Images and Slide Scan Project"), the whole path."""
    parts = path.split("/")
    for v in volumes:                     # e.g. "CumulativeArchive/TAP 4T 2019/...": the archive
        if v in parts[:3]:                # drive's own name says nothing about the subject
            return parts[parts.index(v) + 1:], None
    if drive in copied and drive in parts[:3]:
        return parts[parts.index(drive) + 1:], drive
    if parts and parts[0] == "CumulativeArchive":
        parts = parts[1:]
    return parts, None


def classify(parts, rules, fallback=None):
    """-> (category, rule pattern, matched name). Order of precedence:
    1. the deepest OVERRIDE match anywhere (your exceptions)
    2. TAP anywhere: a strong TAP match, or a broad TAP match not contradicted by a strong
       match of another category in the same folder name; TAP in a computer/drive/backup-like
       folder name only counts as weak. A FOLDER name matching a "nottap" rule (a place
       outside Thailand) cancels any TAP match above it; a TAP match in that same folder name
       or deeper still counts. ("nottap" in a file name doesn't cancel anything: a TAP
       folder's "TAP94.export.rome1" or "vietnam-bronze.htm" stays TAP.)
    3. the deepest strong match   4. the deepest weak match   5. the drive's label"""
    override = tap = best_strong = best_weak = None
    last = len(parts) - 1
    for i, name in enumerate(parts):
        hits = [(cat, st, pat) for rx, cat, st, pat in rules if rx.search(name)]
        if not hits:
            continue
        if i < last and any(st == "nottap" for _, st, _ in hits) and \
                not any(cat == "TAP" and st == "strong" for cat, st, _ in hits):
            tap = None                                           # ".../World Trip/China/..."
        ov = sorted((h for h in hits if h[1] == "override"), key=lambda h: RANK[h[0]])
        if ov:
            override = (ov[0][0], ov[0][2], name)
            continue
        container = bool(CONTAINER.search(name))
        other_strong = [h for h in hits if h[0] != "TAP" and h[1] in ("strong", "broad", "nottap")]
        strong, weak = [], []
        for h in hits:
            cat, st, pat = h
            if cat == "TAP" and container:
                weak.append(h)                                   # "TAP Voelker mac laptop"
            elif cat == "TAP" and st == "broad" and other_strong:
                continue                                         # "Thiel ... Scanning Project"
            elif cat == "TAP" and st in ("strong", "broad"):
                if tap is None:
                    tap = ("TAP", pat, name)
            elif st in ("strong", "broad", "nottap"):
                strong.append(h)
            else:
                weak.append(h)
        if strong:
            h = min(strong, key=lambda h: RANK[h[0]])
            best_strong = (h[0], h[2], name)                     # deeper replaces
        if weak:
            h = min(weak, key=lambda h: RANK[h[0]])
            best_weak = (h[0], h[2], name)
    if override or tap or best_strong or best_weak:
        return override or tap or best_strong or best_weak
    if fallback:                       # nothing in the path itself: try the drive's label
        cat, pat, name = classify([fallback], rules)
        if cat != "Unclassified":
            return cat, f"{pat} (drive name)", name
    return "Unclassified", "", ""


def main():
    ap = argparse.ArgumentParser(description="Classify archive files by subject.")
    ap.add_argument("--rules", default=None, help="rules file (default: classify_rules.csv here)")
    ap.add_argument("-o", "--out", default=PROJECT_DIR, help=argparse.SUPPRESS)
    a = ap.parse_args()
    outdir = Path(a.out).expanduser()
    rules = load_rules(Path(a.rules) if a.rules else outdir / "classify_rules.csv")
    copied = copied_drives(outdir)
    t0 = time.time()

    db = sqlite3.connect(outdir / "archive_catalog.sqlite", timeout=60)
    # an archive drive's own name (e.g. a "CumulativeArchive/TAP 4T 2019" folder holding that
    # drive's original contents) is a label, not a subject
    volumes = {n for (n,) in db.execute("SELECT name FROM archives")}
    log(f"   {len(rules)} rules; {len(copied)} copied-drive labels; archive drives {sorted(volumes)}")
    # One transaction from here to the commit below: until the new classification is complete,
    # everyone else (the search app) keeps seeing the old one, and a run that is stopped
    # half-way leaves the old one in place.
    db.execute("BEGIN")
    for stmt in ("DROP TABLE IF EXISTS file_class",
                 """CREATE TABLE file_class(archive TEXT, path TEXT, category TEXT, rule TEXT,
                                            matched TEXT, PRIMARY KEY(archive, path))""",
                 "DROP TABLE IF EXISTS content_class",
                 """CREATE TABLE content_class(sha256 TEXT PRIMARY KEY, category TEXT,
                                               copies_by_category TEXT)"""):
        db.execute(stmt)
    per_sha = defaultdict(Counter)
    review = defaultdict(lambda: [Counter(), Counter(), 0, 0])     # folder -> cats, rules, n, bytes
    summary = defaultdict(lambda: [0, 0])                          # (archive, cat) -> n, bytes
    # (category, deciding folder name, rule) -> files, bytes, subfolder bytes, an example location
    explain = defaultdict(lambda: [0, 0, Counter(), ""])
    batch = []
    n = 0
    for archive, drive, path, sha, size, junk in db.execute(
            "SELECT archive, drive, path, sha256, size, junk FROM files"):
        parts, fallback = source_parts(path, drive, archive, copied, volumes)
        cat, pat, matched = classify(parts, rules, fallback)
        batch.append((archive, path, cat, pat, matched))
        if sha and not junk:
            per_sha[sha][cat] += 1
        if not junk:
            key = (archive, drive, "/".join(parts[:2]) if len(parts) > 2 else "/".join(parts[:-1]) or "(top)")
            rv = review[key]
            rv[0][cat] += 1
            rv[1][pat or "-"] += 1
            rv[2] += 1
            rv[3] += size or 0
            summary[(archive, cat)][0] += 1
            summary[(archive, cat)][1] += size or 0
            ex = explain[(cat, matched, pat)]
            ex[0] += 1
            ex[1] += size or 0
            if matched and matched in parts:
                i = parts.index(matched)
                ex[2][parts[i + 1] if i + 2 < len(parts) else "(files directly in it)"] += size or 0
                if not ex[3]:
                    ex[3] = f"{archive}: " + path[:path.find(matched) + len(matched)]
        n += 1
        if len(batch) >= 20000:
            db.executemany("INSERT INTO file_class VALUES (?,?,?,?,?)", batch)
            batch.clear()
            log(f"   {n:,} files ...")
    db.executemany("INSERT INTO file_class VALUES (?,?,?,?,?)", batch)

    rows = []
    for sha, c in per_sha.items():
        cat = min(c, key=lambda k: RANK[k])
        rows.append((sha, cat, "; ".join(f"{k}: {v}" for k, v in sorted(c.items(), key=lambda x: RANK[x[0]]))))
    db.executemany("INSERT INTO content_class VALUES (?,?,?)", rows)

    # unique_files.category (identify_archive.py refreshes it on every rebuild)
    cols = [r[1] for r in db.execute("PRAGMA table_info(unique_files)")]
    if cols:
        if "category" not in cols:
            db.execute("ALTER TABLE unique_files ADD COLUMN category TEXT")
        db.execute("""UPDATE unique_files SET category = COALESCE(
                        (SELECT c.category FROM content_class c WHERE c.sha256 = unique_files.sha256),
                        (SELECT fc.category FROM file_class fc
                          WHERE fc.archive = unique_files.archive AND fc.path = unique_files.path),
                        'Unclassified')""")
        db.execute("CREATE INDEX IF NOT EXISTS i_u_category ON unique_files(category)")
    db.commit()
    db.execute("ANALYZE")      # fresh statistics, so the search app's queries stay fast

    # summary per archive and category, with distinct-file counts
    dist = defaultdict(lambda: [0, 0])
    if cols:
        for archive, cat, k, b in db.execute(
                "SELECT archive, category, COUNT(*), SUM(size_bytes) FROM unique_files GROUP BY 1, 2"):
            dist[(archive, cat)] = [k, b or 0]
    with open(outdir / "classification_summary.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["archive_volume", "category", "files", "gb", "distinct_files", "distinct_gb"])
        for (av, cat) in sorted(summary, key=lambda k: (k[0], RANK[k[1]])):
            nf, b = summary[(av, cat)]
            dk, db_ = dist.get((av, cat), [0, 0])
            w.writerow([av, cat, nf, round(b / 1e9, 3), dk, round(db_ / 1e9, 3)])
    with open(outdir / "classification_explain.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["category", "decided_by_folder", "rule", "files", "gb", "where",
                    "biggest_subfolders_gb"])
        for (cat, matched, pat), (nf, b, subs, where) in sorted(
                explain.items(), key=lambda x: (RANK[x[0][0]], -x[1][1])):
            w.writerow([cat, matched or "(nothing matched)", pat, nf, round(b / 1e9, 3), where,
                        "; ".join(f"{k} ({v / 1e9:.1f})" for k, v in subs.most_common(8))])
    with open(outdir / "classification_review.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["archive_volume", "drive", "folder", "files", "gb", "main_category", "pct_main",
                    *[f"{c}_files" for c in CATEGORIES], "main_rule"])
        for (av, d, f), (cats, rls, nf, b) in sorted(review.items(), key=lambda x: -x[1][3]):
            main, mn = cats.most_common(1)[0]
            w.writerow([av, d, f, nf, round(b / 1e9, 3), main, round(100 * mn / nf, 1),
                        *[cats.get(c, 0) for c in CATEGORIES], rls.most_common(1)[0][0]])

    tot = Counter()
    for (av, cat), (nf, b) in summary.items():
        tot[cat] += b
    db.close()
    log(f"   wrote classification_summary.csv, classification_review.csv ({len(review):,} folders) "
        f"and classification_explain.csv")
    print(f"{n:,} files classified in {time.time() - t0:.0f} s | " +
          " | ".join(f"{c} {tot[c] / 1e9:,.0f} GB" for c in CATEGORIES if tot[c]), flush=True)


if __name__ == "__main__":
    main()
