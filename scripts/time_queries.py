#!/usr/bin/env python3
"""
time_queries.py -- time each query the search page runs, using the app's own code.

    python3 scripts/time_queries.py                     # the Museum click
    python3 scripts/time_queries.py --cat ""            # the unfiltered All files page
    python3 scripts/time_queries.py --cat TAP --q anvil

Runs the page's queries twice: the first run is closer to a fresh start, the second shows the
effect of the disk cache. Prints SQLite's plan for each query. Works without Flask installed.
"""
import argparse
import os
import re
import sqlite3
import sys
import time
import types

os.environ["JVA_NO_WARM"] = "1"


class _Stub:
    def __init__(self, *a, **k):
        self.config = {}
        self.jinja_env = types.SimpleNamespace(filters={}, globals={})

    def __getattr__(self, name):
        if name == "app_context":
            return lambda: _Ctx()
        return lambda *a, **k: (lambda f: f)


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


try:
    import flask  # noqa: F401
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False
    fake = types.ModuleType("flask")
    fake.Flask = _Stub
    for n in ("abort", "jsonify", "render_template", "request", "send_file", "url_for"):
        setattr(fake, n, lambda *a, **k: None)
    fake.g = types.SimpleNamespace()
    sys.modules["flask"] = fake

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp"))
import app as A  # noqa: E402


class Timed:
    """A connection that records how long each query takes."""
    def __init__(self, con):
        self.con, self.log = con, []

    def execute(self, sql, params=()):
        t = time.perf_counter()
        rows = self.con.execute(sql, params).fetchall()
        self.log.append((sql, list(params), time.perf_counter() - t, len(rows)))
        return Result(rows)


class Result(list):
    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


def label(sql):
    s = " ".join(sql.split())
    if "unique_archives" in s and "GROUP BY" in s or "f.archive, COUNT" in s:
        return "filter: archive drive"
    if "unique_drives" in s and "GROUP BY" in s or "f.archive || ': ' || f.drive, COUNT" in s:
        return "filter: source drive"
    for key, name in (("u.category,", "filter: category"), ("SELECT COALESCE(u.category", "filter: category"),
                      ("facet = 'site'", "filter: site"), ("facet = 'season'", "filter: season"),
                      ("unique_tags WHERE facet", "filter: site/season"), ("u.copies > 1 THEN", "filter: copies"),
                      ("d.status = 'made' THEN", "filter: preview"), ("LIMIT ? OFFSET", "first 60 results"),
                      ("SELECT COUNT(*) FROM", "result count")):
        if key in s:
            return name
    if re.match(r"SELECT (u\.kind|kind\()", s):
        return "filter: kind"
    return s[:40]


def page_queries(q, filters):
    frm, where, params = A.build(q, filters)
    sort = "relevance" if q else "name"
    A.db().execute(f"SELECT COUNT(*) FROM {frm}{where}", params)
    A.db().execute(f"""SELECT u.id, u.name, {A.kind_sql()} kind FROM {frm}{where}
                       ORDER BY {A.order_by(sort, A.SORTS[sort][2])} LIMIT ? OFFSET ?""", params + [60, 0])
    A.facet_counts.__wrapped__(q, tuple(sorted(filters.items())), None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cat", default="Museum")
    ap.add_argument("--q", default="")
    ap.add_argument("--catalogue", default=A.CFG["catalogue"])
    a = ap.parse_args()
    raw = sqlite3.connect(f"file:{a.catalogue}?mode=ro", uri=True)
    raw.row_factory = sqlite3.Row
    raw.create_function("kind", 5, A.kind_of, deterministic=True)
    con = Timed(raw)
    A.db = lambda: con
    A.has_table = lambda t: raw.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone() is not None
    A.CFG["catalogue"] = a.catalogue
    if not HAVE_FLASK:
        A.app = _Stub()
    prep = A.prepared.__wrapped__(None)
    A.is_prepared = lambda: prep
    print("SQLite", sqlite3.sqlite_version, "|", a.catalogue)
    print("prepare_search.py tables:", "present and up to date" if prep else "missing or stale")

    filters = {k: () for k in A.FILTERS}
    if a.cat:
        filters["cat"] = (a.cat,)
    runs = []
    for _ in range(2):
        con.log = []
        page_queries(a.q, filters)
        runs.append(con.log)
    first, again = runs
    print(f"\n{'query':24s} {'first':>8s} {'again':>8s}  rows")
    for (sql, p, t1, n), (_, _, t2, _) in zip(first, again):
        print(f"{label(sql):24s} {t1:7.2f}s {t2:7.2f}s  {n}")
    print(f"{'TOTAL':24s} {sum(x[2] for x in first):7.2f}s {sum(x[2] for x in again):7.2f}s")
    print("\nQuery plans:")
    for sql, p, _, _ in first:
        print(f"  {label(sql)}:")
        for r in raw.execute("EXPLAIN QUERY PLAN " + sql, p):
            print("     ", r[3])


if __name__ == "__main__":
    main()
