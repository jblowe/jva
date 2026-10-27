#!/usr/bin/env python3
"""
webapp/app.py -- search and browse the archive catalogue (archive_catalog.sqlite).

    python3 -m pip install -r requirements.txt     # once
    python3 webapp/app.py                          # then open http://127.0.0.1:5000

Settings: the "app" section of config.json at the top of the repository (see
config.example.json and docs/search-app.md); the catalogue is the one named there.
  derivatives_roots  folders to look in for the preview images, in order. The derivatives
                     table stores image paths relative to the derivatives folder, so when the
                     folder moves, list its new location here. The location each image was
                     made in is always tried last.
  mode               "local": Reveal in Finder, drive status, originals, Delete (this Mac only)
                     "public": none of those (the routes don't exist) -- for the web server
  derivatives_url    e.g. "/derivatives": pages link the preview images straight to this URL,
                     where the web server (Apache) serves the derivatives folder itself, instead
                     of through /img/ in the app
  hide_categories    categories never shown, e.g. ["Other", "Class"]
  host, port         default 127.0.0.1 and 5000 (local only)
  title              shown in the page header

The catalogue is opened read-only (deleting, in local mode, uses scripts/delete_files.py).
On a server it runs under gunicorn: see deploy/ and docs/deployment.md.
"""
import functools
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from urllib.parse import quote

from flask import Flask, abort, g, jsonify, render_template, request, send_file, url_for

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
from jva.config import CONFIG  # noqa: E402


def load_config():
    cfg = dict(CONFIG.app)
    cfg["catalogue"] = str(CONFIG.catalogue)
    cfg["volumes_root"] = CONFIG.volumes_root
    return cfg


CFG = load_config()
LOCAL = CFG["mode"] == "local"
app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

from jva import volumes  # noqa: E402
from jva.catalog import DISPLAY_CATEGORIES as CATEGORIES, human  # noqa: E402
from jva.kinds import KINDS, SEASONS, SITES, kind_of  # noqa: E402
from jva.search_prep import is_prepared as _tables_ready  # noqa: E402


PLACEHOLDER_FOR_KIND = {"Image": "image", "PDF": "document", "Document": "document",
                        "Video": "video", "Audio": "audio", "Other": "unknown"}


# ---------- database ----------

def db():
    if "db" not in g:
        if not os.path.exists(CFG["catalogue"]):
            abort(500, f"Catalogue not found: {CFG['catalogue']}")
        con = sqlite3.connect(f"file:{CFG['catalogue']}?mode=ro", uri=True, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.create_function("kind", 4, kind_of, deterministic=True)
        g.db = con
    return g.db


@app.teardown_appcontext
def _close(_exc):
    con = g.pop("db", None)
    if con is not None:
        con.close()


def has_table(name):
    return db().execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def catalogue_stamp():
    """Changes whenever the catalogue file does -- used to expire cached results."""
    out = []
    for suffix in ("", "-wal"):          # in WAL mode, recent changes live in the -wal file
        try:
            st = os.stat(CFG["catalogue"] + suffix)
            out += [st.st_mtime, st.st_size]
        except OSError:
            out += [0, 0]
    return tuple(out)


@functools.lru_cache(maxsize=4)
def prepared(_stamp):
    """True when prepare_search.py's tables exist and match the current unique_files."""
    with app.app_context():
        return _tables_ready(db())


def is_prepared():
    return prepared(catalogue_stamp())


def kind_sql():
    return "u.kind" if is_prepared() else "kind(u.ext, u.mime, u.mac_type, u.mac_creator)"


def cat_in(vals):
    """Category filter written so SQLite can use the category index."""
    cond = f"u.category IN ({','.join('?' * len(vals))})"
    return f"({cond} OR u.category IS NULL)" if "Unclassified" in vals else cond


def mounted(archive):
    if CFG["mode"] != "local":
        return False
    return volumes.is_mounted(archive, CFG["volumes_root"])


app.jinja_env.filters["human"] = human
app.jinja_env.filters["add_zero"] = lambda v: ("".join(v), 0)     # a ticked value with no matches
app.jinja_env.globals.update(CFG=CFG, KINDS=KINDS, CATEGORIES=CATEGORIES, SITES=SITES)


def file_key(row):
    return row["sha256"] or f"u{row['id']}"


app.jinja_env.globals["file_key"] = file_key


# ---------- search ----------

FILTERS = ("cat", "site", "season", "kind", "archive", "drive", "copies", "preview")
TAG_FACETS = {"site": SITES, "season": SEASONS}     # from prepare_search.py's unique_tags


def fts_query(q):
    """User text -> an FTS5 query: every word is a prefix match ("anvil" finds "Anvils"),
    "quoted phrases" stay phrases, all terms must match."""
    parts = []
    for tok in re.findall(r'"[^"]*"|\S+', q):
        if tok.startswith('"'):
            words = re.findall(r"\w+", tok)
            if words:
                parts.append('"' + " ".join(words) + '"')
        else:
            parts += [f'"{w}"*' for w in re.findall(r"\w+", tok)]
    return " AND ".join(parts)


def read_filters(args):
    return {k: tuple(v for v in args.getlist(k) if v) for k in FILTERS}


def build(q, filters, exclude=None):
    """-> (FROM ... clause, WHERE clause, params) for unique_files u (+ search index)"""
    frm, where, params = "unique_files u", ["substr(u.name, 1, 2) <> '._'"], []   # AppleDouble files: Mac metadata, not content
    fq = fts_query(q) if q else ""
    if fq:
        frm = "unique_search s JOIN unique_files u ON u.id = s.rowid"
        where.append("unique_search MATCH ?")
        params.append(fq)
    if CFG["hide_categories"]:
        where.append(f"NOT {cat_in(CFG['hide_categories'])}")
        params += CFG["hide_categories"]
    for k, vals in filters.items():
        if not vals or k == exclude:
            continue
        marks = ",".join("?" * len(vals))
        if k == "cat":
            where.append(cat_in(vals))
            params += vals
        elif k == "kind":
            where.append(f"{kind_sql()} IN ({marks})")
            params += vals
        elif k in TAG_FACETS:
            if is_prepared():            # needs prepare_search.py's unique_tags
                where.append(f"EXISTS (SELECT 1 FROM unique_tags t2 WHERE t2.unique_id = u.id "
                             f"AND t2.facet = '{k}' AND t2.value IN ({marks}))")
                params += vals
        elif k == "archive":
            t = "unique_archives" if is_prepared() else "files"
            where.append(f"EXISTS (SELECT 1 FROM {t} f2 WHERE f2.unique_id = u.id AND f2.archive IN ({marks}))")
            params += vals
        elif k == "drive":
            t = "unique_drives" if is_prepared() else "files"
            pairs = [v.split(": ", 1) for v in vals if ": " in v]
            if pairs:
                where.append(f"EXISTS (SELECT 1 FROM {t} f2 WHERE f2.unique_id = u.id AND ("
                             + " OR ".join(["(f2.archive = ? AND f2.drive = ?)"] * len(pairs)) + "))")
                params += [x for pr in pairs for x in pr]
            else:
                where.append("0")
        elif k == "copies":
            conds = [c for c in (("u.copies = 1" if "single" in vals else ""),
                                 ("u.copies > 1" if "dup" in vals else "")) if c]
            if conds:
                where.append("(" + " OR ".join(conds) + ")")
        elif k == "preview" and has_table("derivatives"):
            made = "EXISTS (SELECT 1 FROM derivatives d2 WHERE d2.sha256 = u.sha256 AND d2.status = 'made')"
            conds = [c for c in ((made if "yes" in vals else ""), (f"NOT {made}" if "no" in vals else "")) if c]
            if conds:
                where.append("(" + " OR ".join(conds) + ")")
    return frm, (" WHERE " + " AND ".join(where)) if where else "", params


# sort key -> (label, SQL expression, default direction). Files with no value (no site, no
# date...) always come last; ties are broken by name.
_SITE_RANK = "CASE t.value " + " ".join(f"WHEN '{s}' THEN {i}" for i, s in enumerate(SITES)) + " END"
SORTS = {
    "relevance": ("Best match", "s.rank", "asc"),
    "name": ("Name", "u.name COLLATE NOCASE", "asc"),
    "site": ("Site", f"(SELECT MIN({_SITE_RANK}) FROM unique_tags t WHERE t.unique_id = u.id AND t.facet = 'site')", "asc"),
    "season": ("Season", "(SELECT MIN(t.value) FROM unique_tags t WHERE t.unique_id = u.id AND t.facet = 'season')", "asc"),
    "folder": ("Folder", "u.folder COLLATE NOCASE", "asc"),
    "kind": ("Kind", "{kind}", "asc"),
    "size": ("Size", "u.size_bytes", "desc"),
    "copies": ("Copies", "u.copies", "desc"),
    "category": ("Category", "CASE COALESCE(u.category, 'Unclassified') "
                 + " ".join(f"WHEN '{c}' THEN {i}" for i, c in enumerate(CATEGORIES)) + " END", "asc"),
    "modified": ("Date modified", "NULLIF(u.modified, '')", "desc"),
}
OLD_SORTS = {"newest": ("modified", "desc"), "oldest": ("modified", "asc"), "largest": ("size", "desc")}


def order_by(sort, direction):
    expr = SORTS[sort][1].replace("{kind}", kind_sql())
    if sort == "relevance":
        return expr
    d = "DESC" if direction == "desc" else "ASC"
    tie = "" if sort == "name" else ", u.name COLLATE NOCASE"
    return f"({expr}) IS NULL, {expr} {d}{tie}"


def tags_for(ids):
    """{unique_id: {"site": "NPW, NML", "season": "86, 94"}} for the rows on this page."""
    out = {i: {"site": [], "season": []} for i in ids}
    if ids and is_prepared():
        for uid, facet, value in db().execute(
                f"SELECT unique_id, facet, value FROM unique_tags WHERE unique_id IN ({','.join('?' * len(ids))})",
                list(ids)):
            out[uid][facet].append(value)
    for t in out.values():
        t["site"] = ", ".join(s.upper() for s in SITES if s in t["site"])
        t["season"] = ", ".join(s for s in SEASONS if s in t["season"])
    return out


@functools.lru_cache(maxsize=256)
def facet_counts(q, filters_items, _stamp):
    """Counts for each filter, computed with all the OTHER filters applied (so you can see what
    ticking another box in the same group would add). Cached per query."""
    filters = dict(filters_items)
    out = {}
    with app.app_context():
        con = db()

        def run(sql, params):
            return [(r[0], r[1]) for r in con.execute(sql, params) if r[0] is not None]

        frm, where, p = build(q, filters, "cat")
        out["cat"] = run(f"SELECT COALESCE(u.category,'Unclassified'), COUNT(*) FROM {frm}{where} GROUP BY 1", p)
        frm, where, p = build(q, filters, "kind")
        out["kind"] = run(f"SELECT {kind_sql()}, COUNT(*) FROM {frm}{where} GROUP BY 1", p)
        frm, where, p = build(q, filters, "copies")
        out["copies"] = run(f"SELECT CASE WHEN u.copies > 1 THEN 'dup' ELSE 'single' END, COUNT(*) "
                            f"FROM {frm}{where} GROUP BY 1", p)
        if has_table("derivatives"):
            frm, where, p = build(q, filters, "preview")
            out["preview"] = run(f"SELECT CASE WHEN d.status = 'made' THEN 'yes' ELSE 'no' END, COUNT(*) "
                                 f"FROM {frm} LEFT JOIN derivatives d ON d.sha256 = u.sha256{where} GROUP BY 1", p)
        if is_prepared():
            # nothing narrowing the archive/drive counts: read them straight off the small tables
            unfiltered = lambda k: not q and not CFG["hide_categories"] and \
                not any(v for f, v in filters.items() if f != k)
            for k in TAG_FACETS:
                frm, where, p = build(q, filters, k)
                out[k] = run("SELECT value, COUNT(*) FROM unique_tags WHERE facet = ? GROUP BY 1", [k]) \
                    if unfiltered(k) else \
                    run(f"SELECT t.value, COUNT(*) FROM {frm} JOIN unique_tags t ON t.unique_id = u.id "
                        f"AND t.facet = '{k}'{where} GROUP BY 1", p)
            frm, where, p = build(q, filters, "archive")
            out["archive"] = run("SELECT archive, COUNT(*) FROM unique_archives GROUP BY 1 ORDER BY 1", []) \
                if unfiltered("archive") else \
                run(f"SELECT a.archive, COUNT(*) FROM {frm} JOIN unique_archives a ON a.unique_id = u.id"
                    f"{where} GROUP BY 1 ORDER BY 1", p)
            frm, where, p = build(q, filters, "drive")
            out["drive"] = run("SELECT archive || ': ' || drive, COUNT(*) FROM unique_drives "
                               "GROUP BY archive, drive ORDER BY 2 DESC LIMIT 40", []) \
                if unfiltered("drive") else \
                run(f"SELECT d.archive || ': ' || d.drive, COUNT(*) FROM {frm} JOIN unique_drives d "
                    f"ON d.unique_id = u.id{where} GROUP BY d.archive, d.drive ORDER BY 2 DESC LIMIT 40", p)
        else:
            frm, where, p = build(q, filters, "archive")
            out["archive"] = run(f"SELECT f.archive, COUNT(DISTINCT u.id) FROM {frm} "
                                 f"JOIN files f ON f.unique_id = u.id{where} GROUP BY 1 ORDER BY 1", p)
            frm, where, p = build(q, filters, "drive")
            out["drive"] = run(f"SELECT f.archive || ': ' || f.drive, COUNT(DISTINCT u.id) FROM {frm} "
                               f"JOIN files f ON f.unique_id = u.id{where} GROUP BY 1 ORDER BY 2 DESC LIMIT 40", p)
    order = {"cat": CATEGORIES, "kind": KINDS, **TAG_FACETS}
    for k, ref in order.items():
        out.setdefault(k, [])
        out[k].sort(key=lambda kv: ref.index(kv[0]) if kv[0] in ref else 99)
    return out


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    filters = read_filters(request.args)
    sort = request.args.get("sort") or "name"
    direction = request.args.get("dir")
    if sort in OLD_SORTS:
        sort, direction = OLD_SORTS[sort]
    if sort not in SORTS or (sort == "relevance" and not q) or \
            (sort in ("site", "season") and not is_prepared()):
        sort = "name"
    if direction not in ("asc", "desc"):
        direction = SORTS[sort][2]
    view = request.args.get("view", "grid")
    page = max(1, request.args.get("page", 1, type=int))
    size = CFG["page_size"]

    frm, where, params = build(q, filters)
    try:
        total = db().execute(f"SELECT COUNT(*) FROM {frm}{where}", params).fetchone()[0]
        rows = db().execute(
            f"""SELECT u.id, u.sha256, u.name, u.other_names, u.ext, u.mime, u.mac_type, u.size_bytes,
                       u.modified, u.copies, u.folder, COALESCE(u.category,'Unclassified') category,
                       {kind_sql()} kind
                FROM {frm}{where} ORDER BY {order_by(sort, direction)} LIMIT ? OFFSET ?""",
            params + [size, (page - 1) * size]).fetchall()
        tags = tags_for([r["id"] for r in rows])
        rows = [dict(r, sites=tags[r["id"]]["site"], seasons=tags[r["id"]]["season"]) for r in rows]
        facets = facet_counts(q, tuple(sorted(filters.items())), catalogue_stamp())
        error = None
    except sqlite3.OperationalError as e:
        total, rows, facets, error = 0, [], {}, f"That search couldn't be run ({e}). Try simpler words."
    sorts = {k: v[0] for k, v in SORTS.items()
             if (k != "relevance" or q) and (k not in ("site", "season") or is_prepared())}
    return render_template("search.html", q=q, filters=filters, sort=sort, sorts=sorts, direction=direction,
                           view=view, page=page, pages=max(1, -(-total // size)), total=total,
                           rows=rows, facets=facets, error=error)


# ---------- one file and its copies ----------

def unique_row(key):
    if key.startswith("u") and key[1:].isdigit():
        return db().execute("SELECT * FROM unique_files WHERE id=?", (int(key[1:]),)).fetchone()
    return db().execute("SELECT * FROM unique_files WHERE sha256=?", (key,)).fetchone()


@app.route("/file/<key>")
def file_page(key):
    u = unique_row(key)
    if u is None:
        abort(404, "No such file in the catalogue.")
    if (u["category"] or "Unclassified") in CFG["hide_categories"]:
        abort(404)
    con = db()
    copies = con.execute("""SELECT id, archive, drive, path, modified, created, size FROM files
                            WHERE unique_id = ? ORDER BY archive, path""", (u["id"],)).fetchall()
    why = None
    if has_table("file_class"):
        # explain with a copy that has the file's own category (a file is TAP if ANY copy is
        # in a TAP folder), preferring the copy shown above
        why = con.execute("""SELECT fc.category, fc.rule, fc.matched, fc.archive, fc.path,
                                    (fc.archive = ? AND fc.path = ?) shown
                             FROM files f JOIN file_class fc ON fc.archive = f.archive AND fc.path = f.path
                             WHERE f.unique_id = ?
                             ORDER BY fc.category = ? DESC, shown DESC LIMIT 1""",
                          (u["archive"], u["path"], u["id"], u["category"] or "Unclassified")).fetchone()
    by_copy = None
    if u["sha256"] and has_table("content_class"):
        r = con.execute("SELECT copies_by_category FROM content_class WHERE sha256=?", (u["sha256"],)).fetchone()
        by_copy = r[0] if r else None
    removed = []
    if u["sha256"] and has_table("removed_copies"):
        removed = con.execute("SELECT archive, path, removed_at, kept_path FROM removed_copies "
                              "WHERE sha256=? ORDER BY archive, path", (u["sha256"],)).fetchall()
    deriv = None
    if u["sha256"] and has_table("derivatives"):
        deriv = con.execute("SELECT * FROM derivatives WHERE sha256=?", (u["sha256"],)).fetchone()
    # neighbours: other files in the same folder as the representative copy
    folder = os.path.dirname(u["path"])
    lo = folder + "/" if folder else ""
    neighbours = con.execute(
        """SELECT u2.id, u2.sha256, u2.name, u2.ext, u2.mime, u2.mac_type, f.path
           FROM files f JOIN unique_files u2 ON u2.id = f.unique_id
           WHERE f.archive = ? AND f.path >= ? AND f.path < ? AND instr(substr(f.path, ?), '/') = 0
             AND f.junk = 0 AND f.path <> ? AND substr(f.name, 1, 2) <> '._'
           ORDER BY f.path LIMIT 48""",
        (u["archive"], lo, (folder + "0") if folder else "￿", len(lo) + 1, u["path"])).fetchall()
    vols = {c["archive"]: mounted(c["archive"]) for c in copies}
    orig = None
    if serves_originals():
        full, _ = original_copy(u)
        if full is not None:
            orig = {"inline": original_type(u) in INLINE_TYPES}
    return render_template("file.html", u=u, copies=copies, why=why, by_copy=by_copy, removed=removed,
                           deriv=deriv, neighbours=neighbours, vols=vols, folder=folder, orig=orig,
                           kind=kind_of(u["ext"], u["mime"], u["mac_type"], u["mac_creator"]))


def reveal():
    """Show a copy in the Finder (local mode only, catalogued files on mounted drives only)."""
    if CFG["mode"] != "local" or sys.platform != "darwin":
        return jsonify(ok=False, message="Reveal in Finder only works on the Mac running the archive."), 403
    fid = request.form.get("id", type=int)
    row = db().execute("SELECT archive, path FROM files WHERE id=?", (fid,)).fetchone()
    if row is None:
        return jsonify(ok=False, message="That copy isn't in the catalogue."), 404
    if not mounted(row["archive"]):
        return jsonify(ok=False, message=f'Plug in "{row["archive"]}" first.'), 409
    full = os.path.join(CFG["volumes_root"], row["archive"], row["path"])
    if not os.path.exists(full):
        return jsonify(ok=False, message="The file is no longer at that location."), 410
    subprocess.run(["open", "-R", full], check=False)
    return jsonify(ok=True, message="Shown in the Finder.")


def allows_delete():
    s = CFG.get("allow_delete")
    return LOCAL and (s is None or bool(s))


app.jinja_env.globals["allows_delete"] = allows_delete()


def delete():
    """Delete one copy (id = files.id) or every copy of a distinct file (uid = unique_files.id)
    from the archive drives, with their "._" companions, and update the catalogue. Uses
    jva/deletion.py (as scripts/delete_files.py does), which records each deletion."""
    if not allows_delete():
        return jsonify(ok=False, message="Deleting is switched off here."), 403
    from jva import deletion as D
    fid, uid = request.form.get("id", type=int), request.form.get("uid", type=int)
    reason = (request.form.get("reason") or "").strip()[:200]
    con = sqlite3.connect(CFG["catalogue"], timeout=30)
    try:
        if uid is None and fid is not None:
            r = con.execute("SELECT unique_id FROM files WHERE id=?", (fid,)).fetchone()
            if r is None:
                return jsonify(ok=False, message="That copy isn't in the catalogue any more."), 404
            uid_of = r[0]
            done, failed, freed = D.delete_copy(con, fid, reason, "search app", CFG["volumes_root"])
        elif uid is not None:
            if con.execute("SELECT 1 FROM unique_files WHERE id=?", (uid,)).fetchone() is None:
                return jsonify(ok=False, message="That file isn't in the catalogue any more."), 404
            uid_of = uid
            done, failed, freed = D.delete_unique(con, uid, reason, "search app", CFG["volumes_root"])
        else:
            return jsonify(ok=False, message="Nothing to delete."), 400
        left = con.execute("SELECT COUNT(*) FROM unique_files WHERE id=?", (uid_of,)).fetchone()[0] \
            if uid_of is not None else 0
    except sqlite3.OperationalError as e:
        return jsonify(ok=False, message=f"The catalogue is busy ({e}): try again in a minute."), 503
    finally:
        con.close()
    msg = f"Deleted {done} file{'' if done == 1 else 's'} ({human(freed)})."
    if failed:
        msg += " Not deleted: " + "; ".join(f"{p} ({why})" for p, why in failed[:3])
    return jsonify(ok=done > 0, message=msg, gone=not left), (200 if done else 409)


# ---------- images ----------

@app.route("/img/<size>/<key>")
def image(size, key):
    if size not in ("thumb", "preview"):
        abort(404)
    u = unique_row(key)
    if u is None:
        abort(404)
    placeholder = PLACEHOLDER_FOR_KIND[kind_of(u["ext"], u["mime"], u["mac_type"], u["mac_creator"])]
    if u["sha256"] and has_table("derivatives"):
        d = db().execute("SELECT status, root, thumb, preview FROM derivatives WHERE sha256=?",
                         (u["sha256"],)).fetchone()
        if d is not None:
            rel = d[size]
            if d["status"] == "made" and rel:
                for root in list(CFG["derivatives_roots"]) + [d["root"]]:
                    p = Path(root or "") / rel
                    if root and p.is_file():
                        return send_file(p, mimetype="image/webp", max_age=7 * 86400)
            elif rel and rel.startswith("placeholder/"):
                placeholder = Path(rel).stem
    p = HERE / "static" / "placeholder" / f"{placeholder}.svg"
    if not p.exists():
        p = HERE / "static" / "placeholder" / "unknown.svg"
    return send_file(p, mimetype="image/svg+xml", max_age=3600)


# originals the browser can show by itself; anything else is downloaded
INLINE_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp", "text/plain",
                "audio/mpeg", "audio/mp4", "video/mp4", "video/quicktime"}


def serves_originals():
    s = CFG.get("serve_originals")
    return LOCAL and (s is None or bool(s))


def original_copy(u):
    """(full path, copy row) of a copy of this distinct file that can be read now, or (None, None)."""
    for c in db().execute("SELECT archive, path, size FROM files WHERE unique_id = ? ORDER BY archive, path",
                          (u["id"],)):
        full = Path(CFG["volumes_root"]) / c["archive"] / c["path"]
        try:
            if full.is_file() and full.stat().st_size == c["size"]:
                return full, c
        except OSError:
            continue
    return None, None


def original_type(u):
    mime = (u["mime"] or "").split(";")[0].strip() or mimetypes.guess_type(u["name"])[0] or ""
    if not mime and (u["mac_type"] or "") == "PDF ":
        mime = "application/pdf"
    return mime or "application/octet-stream"


app.jinja_env.globals["serves_originals"] = serves_originals()


def original(key):
    """The original file itself, from whichever plugged-in archive drive has a copy.
    ?download=1 always downloads; otherwise PDFs, pictures, plain text and common audio/video
    open in the browser."""
    if not serves_originals():
        abort(404)
    u = unique_row(key)
    if u is None or (u["category"] or "Unclassified") in CFG["hide_categories"]:
        abort(404)
    full, _ = original_copy(u)
    if full is None:
        abort(404, "None of the archive drives with a copy of this file is plugged in.")
    mime = original_type(u)
    inline = mime in INLINE_TYPES and not request.args.get("download")
    return send_file(full, mimetype=mime, as_attachment=not inline, download_name=u["name"],
                     conditional=True, max_age=0)


@app.route("/doc/<fmt>/<key>")
def document(fmt, key):
    """The whole document as PDF or RTF (old Word files; made by make_derivatives.py)."""
    if fmt not in ("pdf", "rtf"):
        abort(404)
    u = unique_row(key)
    if u is None or not u["sha256"] or not has_table("derivatives") or \
            (u["category"] or "Unclassified") in CFG["hide_categories"]:
        abort(404)
    cols = {r[1] for r in db().execute("PRAGMA table_info(derivatives)")}
    if fmt not in cols:
        abort(404)
    d = db().execute(f"SELECT root, {fmt} rel FROM derivatives WHERE sha256=?", (u["sha256"],)).fetchone()
    if d is None or not d["rel"]:
        abort(404, "No copy in that format.")
    for root in list(CFG["derivatives_roots"]) + [d["root"]]:
        p = Path(root or "") / d["rel"]
        if root and p.is_file():
            stem = os.path.splitext(u["name"])[0] if "." in u["name"][1:] else u["name"]
            return send_file(p, mimetype="application/pdf" if fmt == "pdf" else "application/rtf",
                             as_attachment=(fmt == "rtf"), download_name=f"{stem}.{fmt}", max_age=86400)
    abort(404, "The drive with the derivatives folder isn't plugged in.")


# the routes that reach the archive drives exist only on the Mac that has them
if LOCAL:
    app.add_url_rule("/reveal", view_func=reveal, methods=["POST"])
    app.add_url_rule("/delete", view_func=delete, methods=["POST"])
    app.add_url_rule("/original/<key>", view_func=original)


def _field(r, k):
    try:
        return r[k]
    except (KeyError, IndexError):
        return None


def img_src(r, size="thumb"):
    """The URL of a file's thumbnail or preview. With derivatives_url set, straight to the image
    file under it (served by the web server); otherwise through /img/ in the app."""
    key = file_key(r)
    base = CFG.get("derivatives_url")
    if not base:
        return url_for("image", size=size, key=key)
    cache = g.setdefault("_deriv", {})
    sha = _field(r, "sha256")
    if sha and sha not in cache and has_table("derivatives"):
        cache[sha] = db().execute("SELECT status, thumb, preview FROM derivatives WHERE sha256=?",
                                  (sha,)).fetchone()
    d = cache.get(sha) if sha else None
    placeholder = None
    if d is not None and d[size]:
        if d["status"] == "made":
            return base.rstrip("/") + "/" + quote(d[size])
        if d[size].startswith("placeholder/"):
            placeholder = Path(d[size]).stem
    if placeholder is None:
        kind = _field(r, "kind") or kind_of(_field(r, "ext"), _field(r, "mime"), _field(r, "mac_type"),
                                            _field(r, "mac_creator"))
        placeholder = PLACEHOLDER_FOR_KIND.get(kind, "unknown")
    if not (HERE / "static" / "placeholder" / f"{placeholder}.svg").exists():
        placeholder = "unknown"
    return url_for("static", filename=f"placeholder/{placeholder}.svg")


app.jinja_env.globals["img_src"] = img_src


# ---------- browse by folder ----------

@functools.lru_cache(maxsize=128)
def folder_listing(archive, prefix, _stamp):
    """Subfolders (with files, bytes, category mix) and the files directly in a folder."""
    with app.app_context():
        con = db()
        lo = prefix + "/" if prefix else ""
        hi = prefix + "0" if prefix else "￿"
        subs, files = {}, []
        for path, size, uid, sha, name, ext, mime, mac, cat in con.execute(
                """SELECT f.path, f.size, u.id, u.sha256, f.name, f.ext, u.mime, f.mac_type,
                          COALESCE(u.category, 'Unclassified')
                   FROM files f LEFT JOIN unique_files u ON u.id = f.unique_id
                   WHERE f.archive = ? AND f.path >= ? AND f.path < ? AND f.junk = 0""",
                (archive, lo, hi)):
            rest = path[len(lo):]
            if rest.rsplit("/", 1)[-1].startswith("._"):
                continue                                         # AppleDouble metadata files
            if "/" in rest:
                s = subs.setdefault(rest.split("/", 1)[0], [0, 0, {}])
                s[0] += 1
                s[1] += size or 0
                s[2][cat] = s[2].get(cat, 0) + 1
            else:
                files.append({"id": uid, "sha256": sha, "name": name, "size": size, "ext": ext,
                              "mime": mime, "mac_type": mac, "category": cat})
        subs = sorted(([k, n, b, sorted(c.items(), key=lambda kv: -kv[1])] for k, (n, b, c) in subs.items()),
                      key=lambda s: s[0].lower())
        files.sort(key=lambda f: f["name"].lower())
        return subs, files


@app.route("/browse")
def browse():
    archive = request.args.get("a", "")
    prefix = request.args.get("p", "").strip("/")
    if not archive:
        vols = db().execute("""SELECT archive, COUNT(*) n, SUM(size) b FROM files WHERE junk = 0
                               GROUP BY archive ORDER BY archive""").fetchall()
        return render_template("browse.html", archive="", prefix="", vols=vols,
                               mounted={v["archive"]: mounted(v["archive"]) for v in vols},
                               subs=[], files=[], crumbs=[])
    subs, files = folder_listing(archive, prefix, catalogue_stamp())
    if CFG["hide_categories"]:
        files = [f for f in files if f["category"] not in CFG["hide_categories"]]
    crumbs, acc = [], []
    for part in prefix.split("/") if prefix else []:
        acc.append(part)
        crumbs.append((part, "/".join(acc)))
    return render_template("browse.html", archive=archive, prefix=prefix, vols=[], mounted={},
                           subs=subs, files=files[:600], more=max(0, len(files) - 600), crumbs=crumbs)


# ---------- overview ----------

@functools.lru_cache(maxsize=4)
def overview(_stamp):
    with app.app_context():
        con = db()
        cats = con.execute("""SELECT COALESCE(category,'Unclassified') c, COUNT(*) n, SUM(size_bytes) b
                              FROM unique_files GROUP BY 1""").fetchall()
        cats = sorted(cats, key=lambda r: CATEGORIES.index(r["c"]) if r["c"] in CATEGORIES else 99)
        vols = con.execute("SELECT name, last_scan FROM archives ORDER BY name").fetchall()
        counts = con.execute("SELECT archive, COUNT(*), SUM(size) FROM files WHERE junk=0 GROUP BY 1").fetchall()
        total = con.execute("SELECT COUNT(*), SUM(size_bytes), SUM(copies) FROM unique_files").fetchone()
        deriv = []
        if has_table("derivatives"):
            deriv = con.execute("SELECT status, COUNT(*) FROM derivatives GROUP BY 1 ORDER BY 2 DESC").fetchall()
        removed = con.execute("SELECT COUNT(*), SUM(size) FROM removed_copies").fetchone() \
            if has_table("removed_copies") else (0, 0)
        return ([dict(r) for r in cats], [dict(r) for r in vols], {r[0]: (r[1], r[2]) for r in counts},
                tuple(total), [tuple(r) for r in deriv], tuple(removed))


@app.route("/")
def home():
    cats, vols, counts, total, deriv, removed = overview(catalogue_stamp())
    if CFG["hide_categories"]:
        cats = [c for c in cats if c["c"] not in CFG["hide_categories"]]
    for v in vols:
        v["mounted"] = mounted(v["name"])
        v["files"], v["bytes"] = counts.get(v["name"], (0, 0))
    return render_template("home.html", cats=cats, vols=vols, total=total, deriv=deriv, removed=removed,
                           prepared=is_prepared())


@app.errorhandler(404)
def not_found(e):
    return render_template("message.html", title="Not found",
                           message=getattr(e, "description", "") or "That page doesn't exist."), 404


def warm_cache():
    """Work out the overview and the filter counts for the plain category pages in the background,
    so the first clicks after starting are quick too."""
    import threading

    def run():
        try:
            stamp = catalogue_stamp()
            overview(stamp)
            facet_counts("", tuple(sorted({k: () for k in FILTERS}.items())), stamp)
            for c in CATEGORIES:
                if c not in CFG["hide_categories"]:
                    f = {k: () for k in FILTERS}
                    f["cat"] = (c,)
                    facet_counts("", tuple(sorted(f.items())), stamp)
        except Exception as e:                       # never stop the app over this
            print(f"(Couldn't pre-compute counts: {e})")
    threading.Thread(target=run, daemon=True).start()


if CFG["warm_cache"] and os.path.exists(CFG["catalogue"]) and "JVA_NO_WARM" not in os.environ:
    warm_cache()


if __name__ == "__main__":
    print(f"Catalogue: {CFG['catalogue']}")
    if not is_prepared():
        print("Tip: run  python3 scripts/prepare_search.py  once to make searching faster.")
    print(f"Open http://{CFG['host']}:{CFG['port']}/   (Ctrl-C to stop)")
    app.run(host=CFG["host"], port=CFG["port"], debug=False, threaded=True)
