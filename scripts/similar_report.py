#!/usr/bin/env python3
"""
similar_report.py -- try out the second level of grouping (jva/similar.py) on the catalogue
and write what it would do to similar_files.xlsx, without changing the catalogue.

    python3 scripts/similar_report.py                     # writes similar_files.xlsx in the data folder
    python3 scripts/similar_report.py --catalogue copy.sqlite -o ~/Desktop/similar.xlsx

Level 1 is what the catalogue has now: one entry per distinct file (identical SHA-256).
Level 2 groups those entries further:
  pass 1  identical names, except made-up ones (IMG_0001.JPG, Scan 3.jpg, 001.jpg ...)
  pass 2  the rules in jva/similar.py: names (or folders) that name the same object

Sheets: Summary; Pass 1 groups; Made-up names (what pass 1 refused to group); Pass 2 rules;
Pass 2 groups; Big groups (the widest-spread groups, to check for wrong merges); Name shapes
(names left on their own, with digits as #, to find more patterns). Needs openpyxl.
"""
import argparse
import importlib.util
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # the repository, for jva
from jva.config import CONFIG  # noqa: E402
from jva.catalog import connect, log  # noqa: E402
from jva import similar as S  # noqa: E402

TOP = 1000      # rows in the group listings


def analyse(info):
    items = [(uid, d["name"], d["folder"], d["path"]) for uid, d in info.items()]
    t0 = time.time()
    res, labels = S.group(items)
    log(f"grouped {len(items):,} distinct files in {time.time() - t0:.0f}s")
    # pass 1 alone, for the summary
    p1 = defaultdict(list)
    for uid, d in info.items():
        k = S.pass1_key(d["name"], d["path"])
        p1[k or f"#{uid}"].append(uid)
    groups = defaultdict(list)
    for uid, (g, how, key) in res.items():
        groups[g].append(uid)
    return res, labels, p1, groups


def write(path, info, res, labels, p1, groups):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter as col

    F = "Arial"
    plain, bold = Font(name=F, size=10), Font(name=F, size=10, bold=True)
    head_fill = PatternFill("solid", fgColor="DDE4EE")
    thin = Side(style="thin", color="999999")
    wb = Workbook()

    def sheet(title, heads, rows, widths, first=None):
        ws = wb.active if first else wb.create_sheet(title)
        ws.title = title
        for i, (h, w) in enumerate(zip(heads, widths), 1):
            c = ws.cell(1, i, h)
            c.font, c.fill, c.border = bold, head_fill, Border(bottom=thin)
            c.alignment = Alignment(wrap_text=True, vertical="center")
            ws.column_dimensions[col(i)].width = w
        for r, row in enumerate(rows, 2):
            for i, v in enumerate(row, 1):
                c = ws.cell(r, i, v)
                c.font = plain
                if isinstance(v, int):
                    c.number_format = "#,##0"
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = f"A1:{col(len(heads))}{len(rows) + 1}"
        return ws

    n = len(info)
    p1_groups = [v for v in p1.values() if len(v) > 1]
    final_multi = [v for v in groups.values() if len(v) > 1]
    how_count = Counter(h for _, h, _ in res.values() if h)

    # ---- Summary (counts; the derived numbers are formulas)
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = "Second-level grouping of the catalogue: a trial run"
    ws["A1"].font = Font(name=F, size=13, bold=True)
    ws["A2"] = (f"{time.strftime('%Y-%m-%d %H:%M')}. Nothing in the catalogue was changed. Rules: "
                "jva/similar.py.")
    ws["A2"].font = Font(name=F, size=9, italic=True)
    rows = [
        ("Distinct files now (level 1: identical SHA-256 together)", n),
        ("After pass 1 (same name)", len(p1)),
        ("   files in a same-name group", sum(len(v) for v in p1_groups)),
        ("   same-name groups", len(p1_groups)),
        ("After pass 2 (same object, by the rules)", len(groups)),
        ("   files in a group of 2 or more", sum(len(v) for v in final_multi)),
        ("   groups of 2 or more", len(final_multi)),
    ]
    for i, (k, v) in enumerate(rows, 4):
        ws.cell(i, 1, k).font = plain
        c = ws.cell(i, 2, v)
        c.font, c.number_format = plain, "#,##0"
    ws.cell(4, 3, "fewer entries").font = bold
    for r in (5, 8):
        c = ws.cell(r, 3, f"=1-B{r}/$B$4")
        c.number_format, c.font = "0.0%", plain
    r = 13
    ws.cell(r, 1, "How each file got into its group").font = Font(name=F, size=11, bold=True)
    for j, h in enumerate(["", "Files"], 1):
        c = ws.cell(r + 1, j, h)
        c.font, c.fill = bold, head_fill
    hows = [("same name (pass 1)", how_count.get("same name", 0))] + \
           [(rule.name, how_count.get(rule.name, 0)) for rule in S.RULES] + \
           [("in no group", n - sum(how_count.values()))]
    for i, (k, v) in enumerate(hows, r + 2):
        ws.cell(i, 1, k).font = plain
        c = ws.cell(i, 2, v)
        c.font, c.number_format = plain, "#,##0"
    r = r + 2 + len(hows) + 2
    ws.cell(r, 1, "Group sizes after pass 2").font = Font(name=F, size=11, bold=True)
    for j, h in enumerate(["Files in the group", "Groups", "Files"], 1):
        c = ws.cell(r + 1, j, h)
        c.font, c.fill = bold, head_fill
    bands = [(1, 1), (2, 2), (3, 5), (6, 10), (11, 25), (26, 100), (101, 10 ** 9)]
    sizes = Counter(len(v) for v in groups.values())
    for i, (lo, hi) in enumerate(bands, r + 2):
        label = str(lo) if lo == hi else (f"{lo} or more" if hi > 10 ** 6 else f"{lo}–{hi}")
        ws.cell(i, 1, label).font = plain
        for j, v in ((2, sum(c for s, c in sizes.items() if lo <= s <= hi)),
                     (3, sum(s * c for s, c in sizes.items() if lo <= s <= hi))):
            c = ws.cell(i, j, v)
            c.font, c.number_format = plain, "#,##0"
    ws.column_dimensions["A"].width = 70
    ws.column_dimensions["B"].width = 14
    ws.column_dimensions["C"].width = 14

    def examples(uids, k=3):
        return " | ".join(info[u]["location"] for u in sorted(uids)[:k])

    # ---- Pass 1 groups
    rows = []
    for k, uids in sorted(((k, v) for k, v in p1.items() if len(v) > 1), key=lambda kv: -len(kv[1]))[:TOP]:
        folders = set().union(*(info[u]["folders"] for u in uids))
        rows.append((info[uids[0]]["name"], len(uids), len(folders),
                     ", ".join(sorted({info[u]["kind"] for u in uids})),
                     sum(info[u]["size"] for u in uids), examples(uids)))
    sheet("Pass 1 groups", ["Name", "Distinct files", "Folders", "Kinds", "Bytes", "Examples"], rows,
          [40, 10, 9, 18, 14, 140])

    # ---- Made-up names
    by_label = defaultdict(list)
    for uid, d in info.items():
        g = S.generic(d["name"])
        if g:
            by_label[g].append(uid)
    rows = []
    for label, uids in sorted(by_label.items(), key=lambda kv: -len(kv[1])):
        names = Counter(S.norm(info[u]["name"]) for u in uids)
        rows.append((label, len(uids), len(names), sum(c for c in names.values() if c > 1),
                     ", ".join(n for n, _ in names.most_common(12))))
    sheet("Made-up names", ["Pattern (never grouped by name)", "Distinct files", "Different names",
                            "Files whose name repeats (kept apart)", "Commonest names"], rows,
          [60, 12, 12, 18, 120])

    # ---- Pass 2 rules
    rule_files, rule_keys = Counter(), defaultdict(set)
    for uid, (g, how, key) in res.items():
        if key and how != "same name":
            rule_files[how] += 1
            rule_keys[how].add(key)
    rows = []
    for rule in S.RULES:
        sample = [u for u, (g, h, k) in res.items() if h == rule.name][:5]
        rows.append((rule.name, rule.on, rule.rx.pattern, "images only" if rule.images_only else "all",
                     rule_files[rule.name], len(rule_keys[rule.name]),
                     " | ".join(f"{info[u]['name']} -> {res[u][2]}" for u in sample)))
    sheet("Pass 2 rules", ["Rule", "Looks at", "Pattern", "Files", "Files it grouped",
                           "Keys (objects)", "Examples"], rows, [50, 9, 50, 11, 12, 12, 100])

    # ---- Pass 2 groups and Big groups
    def group_row(g, uids):
        hows = Counter(res[u][1] or "-" for u in uids)
        folders = set().union(*(info[u]["folders"] for u in uids))
        return (labels[g], len(uids), len(folders), hows.get("same name", 0),
                sum(c for h, c in hows.items() if h not in ("same name", "-")),
                ", ".join(sorted({info[u]["kind"] for u in uids})),
                ", ".join(sorted({info[u]["name"] for u in uids})[:15]), examples(uids))
    heads = ["Group", "Distinct files", "Folders", "By same name", "By a pass-2 rule", "Kinds",
             "Names (first 15)", "Examples"]
    widths = [22, 10, 9, 10, 10, 18, 90, 120]
    p2 = [(g, v) for g, v in groups.items() if len(v) > 1 and any(res[u][2] for u in v)]
    sheet("Pass 2 groups", heads, [group_row(g, v) for g, v in sorted(p2, key=lambda gv: -len(gv[1]))[:TOP]],
          widths)
    wide = sorted(final_multi, key=lambda v: -len(set().union(*(info[u]["folders"] for u in v))))[:300]
    sheet("Big groups", heads, [group_row(res[v[0]][0], v) for v in wide], widths)

    # ---- Site check: object numbers written with more than one site
    by_obj = defaultdict(lambda: defaultdict(list))
    for uid, d in info.items():
        folder = d["folder"]
        r, k = S.pass2_key(d["name"], folder, path=d["path"])
        if r and r.name in S.OBJECT_RULES:
            for site in S.sites_named(d["name"], folder) or {"(none)"}:
                by_obj[k][site].append(uid)
    rows = []
    for k, sites in by_obj.items():
        named = {s: v for s, v in sites.items() if s != "(none)"}
        if len(named) > 1:
            rows.append((k, ", ".join(sorted(x.upper() for x in named)), sum(len(v) for v in sites.values()),
                         "; ".join(f"{x.upper()}: {len(v)}" for x, v in sorted(named.items())),
                         " | ".join(f"{x.upper()}: {info[v[0]]['location']}" for x, v in sorted(named.items()))))
    rows.sort(key=lambda r: -r[2])
    site_rows = rows
    sheet("Site check", ["Object number", "Sites named", "Distinct files", "Files per site",
                         "An example for each site"], rows, [14, 16, 10, 30, 160])
    ws = wb["Summary"]
    ws["E4"] = "Object numbers written with more than one site (see Site check)"
    ws["E4"].font = bold
    ws["F4"] = len(site_rows)
    ws["E5"] = "Object numbers with a site written somewhere"
    ws["E5"].font = plain
    ws["F5"] = sum(1 for sites in by_obj.values() if set(sites) - {"(none)"})
    ws.column_dimensions["E"].width = 62
    for c in ("F4", "F5"):
        ws[c].number_format, ws[c].font = "#,##0", plain

    # ---- Name shapes of files left alone
    alone = [u for u, (g, h, k) in res.items() if len(groups[g]) == 1]
    shapes, ex = Counter(), {}
    for u in alone:
        s = re.sub(r"\d+", "#", S.norm(info[u]["name"]))
        shapes[s] += 1
        ex.setdefault(s, info[u]["location"])
    sheet("Name shapes", ["Shape of the name (digits as #)", "Distinct files left on their own",
                          "Example"], [(s, c, ex[s]) for s, c in shapes.most_common(500)], [60, 14, 120])
    wb.save(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalogue", default=str(CONFIG.catalogue))
    ap.add_argument("-o", "--output", help="the spreadsheet (default: similar_files.xlsx in the data folder)")
    a = ap.parse_args()
    if importlib.util.find_spec("openpyxl") is None:
        sys.exit("This needs openpyxl:  python3 -m pip install openpyxl")
    out = Path(a.output).expanduser() if a.output else CONFIG.data_dir / "similar_files.xlsx"
    db = connect(a.catalogue, readonly=True)
    info = S.catalogue_items(db)
    db.close()
    res, labels, p1, groups = analyse(info)
    write(out, info, res, labels, p1, groups)
    print(f"Wrote {out}: {len(info):,} distinct files -> {len(p1):,} after pass 1 -> {len(groups):,} after pass 2")


if __name__ == "__main__":
    main()
