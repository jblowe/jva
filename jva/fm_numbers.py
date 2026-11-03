"""
fm_numbers.py -- numbers out of the FileMaker CSVs' measurement columns.

FileMaker number fields accept any text, and the people entering TAP's data often wrote the
unit, or a qualifier, with the number: "60%", "2.5cm", "≈ 12.3 cm", ">4.42", "80-85%",
"3.20 cm (nc)", "189+25", "2-3 years". So a column of measurements can't be sorted or
summed as it stands.

fix_csv() rewrites one CSV so that each such column holds the best guess at a plain number,
in the column's own unit, and a new column <column>_original, just after it, holds what was
written. Only measurement columns are touched, and only when at least one value differs:

  * a measurement column is one whose name says so (weight, length, width, height,
    thickness, diameter, count, % complete, age, ...) or whose values carry units, and in
    which at least 80% of the values with something in them read as a number (50% when the
    name says it's a measurement; "---", "na", "?" and a repeated heading don't count). Identifier
    columns (bag, lot, level, layer, spit, op, feature, burial, catalogue, T#, page, box,
    area, ...) are left alone, though "123A", "2/3" or "B12" look half numeric: those are
    names, not quantities.
  * the column's unit is the unit written most often in it. A value written in another unit
    of the same sort is converted to it ("450 g" in a kg column -> 0.45), unless the
    conversion would make it wildly out of line with the rest of the column, when the unit
    is taken as a slip ("4 mg" among kilograms -> 4). A unit of another sort ("12%" in a
    weight column) leaves the number empty.
  * the best guess: a range ("80-85", "2 to 3", "33/35") -> its middle; a sum ("189+25")
    -> the total; "<20", ">4.42", "40+", "≈12", "~5", "c. 3", "3.2?", "(nc)", "(approx)" and
    other notes in brackets -> the number as written (the qualifier stays in _original);
    "months" in an age column -> years. Obvious typing slips are mended: "1..4" -> 1.4,
    "6 .50" -> 6.50, "1.6 0" -> 1.60, "kkg" -> kg.
  * anything else ("---", "na", "adult", "missing rim", a repeated heading) -> empty.

Running it again on a CSV it has fixed recomputes the numbers from the _original columns.
"""
import csv
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

csv.field_size_limit(10 ** 9)

SUFFIX = "_original"

# unit -> (sort of quantity, size in that sort's base unit)
UNITS = {
    "mm": ("length", Decimal("0.001")), "cm": ("length", Decimal("0.01")), "m": ("length", Decimal(1)),
    "in": ("length", Decimal("0.0254")),
    "mg": ("mass", Decimal("0.000001")), "g": ("mass", Decimal("0.001")), "kg": ("mass", Decimal(1)),
    "%": ("percent", Decimal(1)),
    "years": ("age", Decimal(1)), "months": ("age", Decimal(1) / Decimal(12)),
    "weeks": ("age", Decimal(1) / Decimal(52)), "days": ("age", Decimal(1) / Decimal(365)),
}
ALIASES = {
    "cms": "cm", "mms": "mm", "gm": "g", "gms": "g", "gr": "g", "grams": "g", "kgs": "kg", "kkg": "kg",
    "kilos": "kg", "percent": "%", "pct": "%", "year": "years", "yrs": "years", "yr": "years", "y": "years",
    "yo": "years", "month": "months", "mos": "months", "mo": "months", "mths": "months", "m.o.": "months",
    "week": "weeks", "wks": "weeks", "wk": "weeks", "day": "days", "cmm": "cm",
}

MEASURE = re.compile(r"weight|wt(?![a-z])|length|width|height|thick|diam|depth|count|complete|amount|percent|"
                     r"%|(?<![a-z])age(?![a-z])|size|volume|mass|circumf|breadth|elev|(?<![a-z])(len|ht|wd|dia)(?![a-z])", re.I)
IDENT = re.compile(r"bag|lot|level|layer|spit|(?<![a-z])op(?![a-z])|feature|(?<![a-z])fea(?![a-z])|burial|"
                   r"(?<![a-z])bur(?![a-z])|cat|(?<![a-z])id(?![a-z])|tag|#|page|box|season|square|"
                   r"(?<![a-z])sq(?![a-z])|unit|(?<![a-z])no\.?(?![a-z])|date|year|number|(?<![a-z])num|roll|"
                   r"frame|neg|photo|slide|sample|accession|record|serial|time|plate|section|grid|context|locus|"
                   r"trench|(?<![a-z])pit(?![a-z])|area|strat|prov|drawn|draw|form|phase|reg|obj|film|exp|"
                   r"phone|zip|code", re.I)

KIND_BY_NAME = [            # (pattern in the column name, sort of quantity; None = a plain count)
    (re.compile(r"count|(?<![a-z])(n|qty|quantity)(?![a-z])", re.I), None),
    (re.compile(r"weight|(?<![a-z])wt(?![a-z])|mass", re.I), "mass"),
    (re.compile(r"(?<![a-z])age(?![a-z])", re.I), "age"),
    (re.compile(r"complete|amount|percent|%", re.I), "percent"),
    (re.compile(r"length|width|height|thick|diam|depth|size|breadth|circumf|(?<![a-z])(len|ht|wd|dia)(?![a-z])",
                re.I), "length"),
]
NO_KIND = object()

NUMBER = r"\d+(?:\.\d*)?|\.\d+"
PLACEHOLDER = re.compile(r"[-–—_.?*~ ]*|n/?a|none|nil|nd|n\.d\.|nc|missing|unknown|unk|void", re.I)
QUALIFIER = re.compile(r"≈|~|≥|≤|\+/-|(?<![a-z])(?:c\.|ca\.?|circa|approx\.?|approximately|app\.?|about|est\.?)"
                       r"(?![a-z])|(?<![a-z])nc(?![a-z])|\?", re.I)
WORD = re.compile(r"[a-z%][a-z%.]*", re.I)
TOKEN = re.compile(rf"\s*({NUMBER}|to|and|plus|[-–—/+&<>,])\s*", re.I)
RANGE = {"-", "–", "—", "to", "/"}
SUM = {"+", "&", "and", "plus"}


def unit_of(word):
    w = word.lower().rstrip(".")
    w = ALIASES.get(w, ALIASES.get(word.lower(), w))
    return w if w in UNITS else None


def parse(text):
    """One value as written -> (Decimal or None, unit or None, how, the number's digits as typed when there
    is just one number). how is '' for a plain number."""
    s = text.strip()
    if not s or PLACEHOLDER.fullmatch(s):
        return None, None, "no number", None
    notes = []
    if re.search(r"\(.*?\)|\[.*?\]", s):
        notes.append("note in brackets dropped")
        s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = s.replace("`", "").replace("'", "")
    if re.search(r"±|\+/-", s):                                     # 26.8 cm ±.5 cm: the tolerance goes
        notes.append("tolerance dropped")
        s = re.sub(rf"(?:±|\+/-)\s*(?:{NUMBER})\s*[a-z]*", " ", s, flags=re.I)
    if re.search(r"(?<![a-z])w/o?\s*dirt?|(?<![a-z])with(?:out)? dirt", s, re.I):   # 1.8 kg w/dirt
        notes.append("note dropped")
        s = re.sub(r"(?<![a-z])w/o?\s*dirt?\w*|(?<![a-z])with(?:out)? dirt\w*", " ", s, flags=re.I)
    s = re.sub(r"(?<![a-z])k\s+g(?![a-z])", "kg", s, flags=re.I)        # 0.01k g
    s = re.sub(r"(?<=\d\.) (?=\d)", "", s)                         # 12. 0 kg -> 12.0
    s = re.sub(r"(?<=\d)\.\.+(?=\d)", ".", s)                       # 1..4 -> 1.4
    s = re.sub(r"(?<=\d) (?=\.\d)", "", s)                          # 6 .50 -> 6.50
    s = re.sub(r"(\d+\.\d+) (\d+)(?=\s*[a-z%]|\s*$)", r"\1\2", s, flags=re.I)   # 1.6 0 cm -> 1.60 cm
    s = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", s)                   # 1,234 -> 1234
    if QUALIFIER.search(s):
        notes.append("qualifier dropped")
        s = QUALIFIER.sub(" ", s)
    units = []

    def take(m):
        u = unit_of(m.group(0))
        if u:
            units.append(u)
            return " "
        if m.group(0).lower() in ("to", "and", "plus"):
            return f" {m.group(0)} "
        raise ValueError(m.group(0))
    try:
        s = WORD.sub(take, s)
    except ValueError:
        return None, None, "not a number", None
    if len(set(units)) > 1:
        return None, None, "two different units", None
    toks, pos = [], 0
    s = s.strip()
    while pos < len(s):
        m = TOKEN.match(s, pos)
        if not m:
            return None, None, "not a number", None
        toks.append(m.group(1).lower())
        pos = m.end()
    if toks and toks[0] in ("<", ">", "+", "plus"):
        notes.append("qualifier dropped")
        toks = toks[1:]
    while toks and toks[-1] in ("-", "–", "—", "/", ","):        # "33.90 cm - nc"
        toks = toks[:-1]
    if toks and toks[-1] == "+":
        notes.append("qualifier dropped")
        toks = toks[:-1]
    nums, ops = toks[0::2], toks[1::2]
    if not nums or not all(re.fullmatch(NUMBER, n) for n in nums) or len(toks) % 2 == 0:
        return None, None, "not a number", None
    try:
        vals = [Decimal(n) for n in nums]
    except InvalidOperation:
        return None, None, "not a number", None
    if not ops:
        value = vals[0]
    elif len(vals) == 2 and ops[0] in RANGE:
        value = (vals[0] + vals[1]) / 2
        notes.append("middle of a range")
    elif all(o in SUM for o in ops):
        value = sum(vals)
        notes.append("sum")
    else:
        return None, None, "not a number", None
    return value, (units[0] if units else None), "; ".join(dict.fromkeys(notes)), (nums[0] if not ops else None)


def show(d):
    """A Decimal as plain text: 0.84, 12, 2.5 (no exponent, no trailing zeros after a sum or conversion)."""
    t = format(d.quantize(Decimal("0.000001")) if d.as_tuple().exponent < -6 else d, "f")
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    return t or "0"


def is_header_echo(value, column):
    k = lambda x: re.sub(r"[\W_]+", "", x).lower()
    return k(value) == k(column)


def convert_column(column, values):
    """-> None if this isn't a measurement column worth changing, else
    (unit, [new value per row], [(original, new, how) per changed row])."""
    if IDENT.search(column) and not MEASURE.search(column):
        return None
    parsed, tried, ok, with_unit = [], 0, 0, 0
    for v in values:
        t = v.strip()
        if not t:
            parsed.append(None)
            continue
        p = parse(t)
        parsed.append(p)
        if p[2] == "no number" or is_header_echo(t, column):
            continue
        tried += 1
        if p[0] is not None:
            ok += 1
            with_unit += p[1] is not None
    named = bool(MEASURE.search(column))
    if tried < 1 or ok / tried < (0.5 if named else 0.8) or not (named or with_unit):
        return None
    kind = next((k for rx, k in KIND_BY_NAME if rx.search(column)), NO_KIND)
    counts = Counter(p[1] for p in parsed if p and p[0] is not None and p[1]
                     and (kind is NO_KIND or UNITS[p[1]][0] == kind))
    unit = counts.most_common(1)[0][0] if counts else None
    plain = sorted(p[0] for p in parsed if p and p[0] is not None and (p[1] in (None, unit)))
    median = plain[len(plain) // 2] if plain else None
    new, changes = [], []
    for v, p in zip(values, parsed):
        if p is None:
            new.append("")
            continue
        value, u, how, typed = p
        out = ""
        if value is not None:
            if u and kind is None:
                value, how = None, f"a value in {u} in a column of counts"
            elif u and kind not in (NO_KIND, None) and UNITS[u][0] != kind:
                value, how = None, f"unit {u} doesn't fit a column of {kind}"
            elif u and unit and u != unit:
                if UNITS[u][0] != UNITS[unit][0]:
                    value, how = None, f"unit {u} doesn't fit a column in {unit}"
                else:
                    conv = value * UNITS[u][1] / UNITS[unit][1]
                    def odd(x):
                        return median and median > 0 and x > 0 and not (median / 50 <= x <= median * 50)
                    if UNITS[u][0] in ("mass", "length") and odd(conv) and not odd(value):
                        how = "; ".join(filter(None, [how, f"{u} taken as a slip for {unit}"]))
                    else:
                        value = conv
                        how = "; ".join(filter(None, [how, f"converted from {u}"]))
            if value is not None:
                simple = not how or how in ("qualifier dropped", "note in brackets dropped",
                                            "qualifier dropped; note in brackets dropped",
                                            "note in brackets dropped; qualifier dropped")
                out = typed if simple and typed else show(value)
        new.append(out)
        if out != v.strip() and not (p[2] == "" and p[1] is None and out and re.fullmatch(NUMBER, v.strip())):
            changes.append((v, out, how or ("unit dropped" if u else "tidied")))
    if not changes:
        return None
    return unit, new, changes


def fix_csv(path):
    """Rewrite one CSV in place.
    -> [(column, unit, values, changed, placeholders emptied, text emptied, changes)] for the columns changed."""
    path = Path(path)
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return []
    header, body = rows[0], rows[1:]
    width = len(header)
    body = [r + [""] * (width - len(r)) for r in body]
    names = set(header)
    report, plan = [], {}
    for i, col in enumerate(header):
        if col.endswith(SUFFIX) and col[:-len(SUFFIX)] in names:
            continue
        has_orig = col + SUFFIX in names
        src = header.index(col + SUFFIX) if has_orig else i
        values = [r[src] for r in body]
        res = convert_column(col, values)
        if res is None:
            if has_orig:                                   # no longer converted: put the original back
                plan[i] = (values, src, True)
            continue
        unit, new, changes = res
        plan[i] = (new, src if has_orig else None, False)
        placeholders = sum(1 for o, n, h in changes if h == "no number")
        emptied = sum(1 for o, n, h in changes if not n and h != "no number")
        report.append((col, unit or "", sum(1 for v in values if v.strip()), len(changes), placeholders, emptied,
                       changes))
    if not plan:
        return report
    out_header, cols = [], []
    for i, col in enumerate(header):
        if col.endswith(SUFFIX) and col[:-len(SUFFIX)] in names:
            continue                                       # placed beside its column below
        if i in plan:
            new, src, restore = plan[i]
            out_header.append(col)
            cols.append(("new", new))
            if not restore:
                out_header.append(col + SUFFIX)
                cols.append(("orig", [r[src if src is not None else i] for r in body]))
        else:
            out_header.append(col)
            cols.append(("keep", [r[i] for r in body]))
            if col + SUFFIX in names:
                out_header.append(col + SUFFIX)
                cols.append(("keep", [r[header.index(col + SUFFIX)] for r in body]))
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
        w.writerow(out_header)
        for n in range(len(body)):
            w.writerow([c[1][n] for c in cols])
    tmp.replace(path)
    return report


REPORT = "_numbers.csv"
VALUES = "_numbers_values.csv"


def summary(results):
    """-> one line about what fix_csv() did to these CSVs."""
    cols = [c for r in results.values() for c in r]
    return (f"{len(results):,} CSV files with {len(cols):,} measurement columns made plain numbers: "
            f"{sum(c[3] for c in cols):,} values changed ({sum(c[4] for c in cols):,} placeholders such as --- "
            f"and {sum(c[5] for c in cols):,} other values that aren't numbers left empty)")


def write_reports(folder, results):
    """results: {csv name: fix_csv() result} -> writes _numbers.csv and _numbers_values.csv."""
    folder = Path(folder)
    with open(folder / REPORT, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow(["csv_file", "column", "unit", "values", "changed", "placeholders emptied (---, na, ?)",
                    "text emptied (not a number)"])
        for name in sorted(results, key=str.lower):
            for col, unit, n, changed, placeholders, emptied, _ in results[name]:
                w.writerow([name, col, unit, n, changed, placeholders, emptied])
    with open(folder / VALUES, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow(["csv_file", "column", "unit", "as written", "number", "how", "times"])
        for name in sorted(results, key=str.lower):
            for col, unit, *_, changes in results[name]:
                for (o, n, h), k in sorted(Counter(changes).items(), key=lambda x: (-x[1], x[0][0])):
                    w.writerow([name, col, unit, o, n, h, k])
