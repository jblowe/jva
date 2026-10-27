#!/usr/bin/env python3
"""
make_derivatives.py -- web-friendly preview images ("derivatives") for the archive.

  python3 scripts/make_derivatives.py -a "TAP 4T 2019" -d "/Volumes/TAP 4T 2019/derivatives"

  -a, --archive NAME   archive volume whose files to work from (required); one per run,
                       like the other scripts. Files whose copies are all on other archive
                       drives are done when you run it with those drives.
  -d, --dest PATH      the derivatives folder (required). It can be on any drive, e.g. next
                       to the archived folders on an archive drive; identify_archive.py
                       recognizes it and never catalogues it.

For every DISTINCT file (identical copies share one derivative) it makes two WebP images:
  thumb    480 px on the long side  (search results)
  preview 1200 px on the long side  (detail view)
How:
  images       ImageMagick (JPEG, TIFF, PSD, GIF, PNG, PICT, camera raw, ...); falls back to
               macOS `sips`, then Quick Look
  PDFs         first page: ImageMagick + Ghostscript, falling back to Quick Look
  PostScript   Illustrator files (creator ART3/ART5/ARTZ..., or .ai), EPS and PostScript:
               told apart by their first bytes. PostScript (Illustrator 8 and earlier, EPS)
               goes to Ghostscript, on a page the size of the drawing's bounding box;
               PDF-based ones (Illustrator 9 and later) are treated as PDFs. Quick Look is
               never used for PostScript: it shows the PostScript code as text.
  drawings     old Mac Canvas drawings (type drw2/drw5/drwt, creator DAD2/DAD5, or .cvs,
               .cv5, .canvas, ...): LibreOffice (headless) -> PDF -> ImageMagick, with the
               blank page margins trimmed off
  old Mac     Word 4/5/6 (type WDBN/W6BN/W8BN), MacWrite, ClarisWorks, old Excel and
  documents    PowerPoint, mostly with no extension or a made-up one: first page via
               LibreOffice -> PDF (Quick Look can't read them, and hangs trying).
               For old Microsoft Word files, the whole document is also kept as a PDF
               and as an RTF (readable by any word processor today):
                 <dest>/pdf/3f/<slug>.pdf   <dest>/rtf/3f/<slug>.rtf
  documents,   first page / first frame from macOS Quick Look (Word, Excel, PowerPoint,
  video, ...   Pages, Keynote, Numbers, RTF, text, HTML, video, ...); documents Quick Look
               can't do are tried in LibreOffice
  the rest     no image of its own: a shared placeholder per kind (document, audio,
               archive, font, application, unknown, ...)

  --only TAP   only files classify_archive.py put in these categories (comma-separated)

Layout (all names are web-safe):
  <dest>/thumb/3f/<slug>-<first 10 of sha256>.webp
  <dest>/preview/3f/<slug>-<first 10 of sha256>.webp       (3f = first 2 of sha256)
  <dest>/placeholder/<kind>.svg
where <slug> is the file name made lower-case ASCII with dashes, e.g.
  "Anvils 2 for Jake #3 (10/31/08).tif"  ->  anvils-2-for-jake-3-10-31-08-3f9a2c7b1e.webp

The `derivatives` table in archive_catalog.sqlite relates each sha256 (the same key as in
`files` and `unique_files`) to its slug, image paths, size, method and status. Reruns only
do what's missing (add --retry-failed to try failures again). Files need a checksum first:
run identify_archive.py on the drive before this.

Needs:  brew install imagemagick ghostscript       (qlmanage and sips come with macOS)
        brew install --cask libreoffice            (Canvas drawings, old Mac Word files)
Files that got a placeholder in an earlier run, but which a newer version of this script
can render (e.g. the Canvas drawings), are picked up again automatically. So are Illustrator
and EPS files that an earlier version sent to Quick Look (a picture of their code).
Progress goes to STDERR; a one-line result goes to STDOUT. Standard library only.
"""
import argparse
import concurrent.futures as cf
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # the repository, for jva
from jva.config import CONFIG  # noqa: E402
from jva import volumes  # noqa: E402
from jva.catalog import DERIVATIVES  # noqa: E402
from jva.kinds import (DRAWING_EXT, DRAWING_MAC, IMAGE_EXT, IMAGE_MAC, OLD_DOC_MAC, PS_EXT,  # noqa: E402
                       PS_MAC, WORD_MAC)

PROJECT_DIR = str(CONFIG.data_dir)       # catalogue, reports and drive records
MARKER = ".archive-derivatives"          # identify_archive.py skips folders containing this
THUMB_PX, PREVIEW_PX = 480, 1200
THUMB_Q, PREVIEW_Q = 75, 80
TIMEOUT = 300                             # seconds per tool call

PDF_EXT = {".pdf"}
# Quick Look can render these (first page / first frame)
QL_EXT = {".doc", ".docx", ".dot", ".dotx", ".rtf", ".txt", ".text", ".md", ".csv", ".tsv",
          ".xls", ".xlsx", ".ppt", ".pptx", ".pps", ".ppsx", ".pages", ".key", ".numbers",
          ".odt", ".ods", ".odp", ".html", ".htm", ".xml", ".json", ".eml", ".vcf",
          ".mov", ".mp4", ".m4v", ".avi", ".mpg", ".mpeg", ".3gp", ".mts", ".m2ts", ".wmv",
          ".mkv", ".dv", ".usdz", ".stl", ".obj", ".epub"}
QL_MAC = {"TEXT", "WDBN", "W8BN", "W6BN", "XLS8", "XLS5", "SLD8", "PPT3", "MooV", "RTF "}
# Canvas drawings (DRAWING_*) are opened by LibreOffice (its libmwaw import filter); so are
# old Mac documents (OLD_DOC_MAC), which Quick Look mostly can't read -- qlmanage then hangs.
# document extensions LibreOffice can try when Quick Look fails
LO_DOC_EXT = {".doc", ".docx", ".dot", ".rtf", ".xls", ".xlsx", ".ppt", ".pptx", ".pps", ".odt",
              ".ods", ".odp", ".wpd", ".wps", ".mcw", ".wri", ".sxw", ".txt", ".text"}
QL_TIMEOUT = 45     # seconds; Quick Look either renders a file quickly or hangs on it
# Illustrator/PostScript (PS_*): the first bytes of the file decide how it is drawn (sniff()).

PLACEHOLDER_KIND = [
    ("drawing", DRAWING_EXT),
    ("audio", {".mp3", ".m4a", ".aif", ".aiff", ".wav", ".flac", ".ogg", ".wma", ".aac", ".mid", ".midi"}),
    ("archive", {".zip", ".sit", ".sitx", ".sea", ".hqx", ".gz", ".tgz", ".bz2", ".tar", ".rar",
                 ".7z", ".dmg", ".iso", ".cpt", ".bin", ".img", ".toast"}),
    ("font", {".ttf", ".otf", ".dfont", ".fon", ".suit", ".pfb", ".pfm", ".afm", ".bmap", ".ffil"}),
    ("database", {".fp5", ".fp7", ".fmp12", ".mdb", ".accdb", ".db", ".sqlite", ".dbf", ".fmp"}),
    ("application", {".app", ".exe", ".dll", ".pkg", ".mpkg", ".msi", ".jar", ".dylib", ".so", ".plugin"}),
    ("code", {".py", ".pl", ".js", ".css", ".c", ".h", ".java", ".sh", ".php", ".rb", ".sql"}),
    ("document", {".doc", ".docx", ".rtf", ".txt", ".pages", ".odt", ".wpd", ".wps", ".tex",
                  ".xls", ".xlsx", ".numbers", ".ods", ".ppt", ".pptx", ".key", ".odp", ".pdf",
                  ".html", ".htm", ".xml", ".csv", ".eml"}),
    ("video", {".mov", ".mp4", ".m4v", ".avi", ".mpg", ".mpeg", ".3gp", ".mts", ".wmv", ".mkv"}),
    ("image", IMAGE_EXT),
]

SCHEMA = DERIVATIVES


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def slugify(name, maxlen=60):
    stem = os.path.splitext(name)[0] if not name.startswith(".") else name
    s = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if len(s) > maxlen:
        s = s[:maxlen].rsplit("-", 1)[0] or s[:maxlen]
    return s or "file"


def is_illustrator(creator):
    return (creator or "").startswith("ART")


def category(ext, mime, mac_type, puid, creator=""):
    ext, mime, mac_type = (ext or "").lower(), (mime or "").lower(), mac_type or ""
    if ext in PDF_EXT or mime == "application/pdf" or mac_type == "PDF ":
        return "pdf"
    if mime != "multipart/appledouble" and (
            is_illustrator(creator) or mac_type in PS_MAC or ext in PS_EXT or mime == "application/postscript"):
        return "postscript"
    if mac_type in DRAWING_MAC or (ext in DRAWING_EXT and mime != "multipart/appledouble"):
        return "drawing"
    if mac_type in OLD_DOC_MAC:
        return "olddoc"
    if ext in IMAGE_EXT or mime.startswith("image/") or mac_type in IMAGE_MAC:
        return "image"
    if ext in QL_EXT or mac_type in QL_MAC or mime.startswith("video/") or mime.startswith("text/"):
        return "quicklook"
    return "placeholder"


def placeholder_kind(ext, mime, mac_type=""):
    ext, mime = (ext or "").lower(), (mime or "").lower()
    if (mac_type or "") in DRAWING_MAC:
        return "drawing"
    for kind, exts in PLACEHOLDER_KIND:
        if ext in exts:
            return kind
    for prefix, kind in (("audio/", "audio"), ("video/", "video"), ("image/", "image"),
                         ("text/", "document"), ("font/", "font")):
        if mime.startswith(prefix):
            return kind
    return "unknown"


def write_placeholders(dest):
    d = dest / "placeholder"
    d.mkdir(parents=True, exist_ok=True)
    kinds = sorted({k for k, _ in PLACEHOLDER_KIND} | {"unknown"})
    for k in kinds:
        p = d / f"{k}.svg"
        if not p.exists():
            p.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="480" viewBox="0 0 480 480">'
                '<rect width="480" height="480" rx="24" fill="#eceae4"/>'
                '<path d="M170 110h100l60 60v200H170z" fill="#fff" stroke="#9a968c" stroke-width="6"/>'
                '<path d="M270 110v60h60" fill="none" stroke="#9a968c" stroke-width="6"/>'
                f'<text x="240" y="420" font-family="Helvetica,Arial,sans-serif" font-size="34" '
                f'fill="#6b675e" text-anchor="middle">{k.upper()}</text></svg>', encoding="utf-8")
    return kinds


# ---------- tools ----------

class Tools:
    def __init__(self):
        self.magick = shutil.which("magick") or shutil.which("convert")
        self.im7 = bool(shutil.which("magick"))
        self.gs = shutil.which("gs")
        self.sips = shutil.which("sips")
        self.ql = shutil.which("qlmanage")
        self.soffice = shutil.which("soffice") or next(
            (p for p in ("/Applications/LibreOffice.app/Contents/MacOS/soffice",
                         os.path.expanduser("~/Applications/LibreOffice.app/Contents/MacOS/soffice"))
             if os.path.exists(p)), None)
        # LibreOffice allows one running instance per user profile, so each worker thread
        # gets a profile of its own (made on first use, removed by cleanup())
        self._lo_base = tempfile.mkdtemp(prefix="deriv-lo-")
        self._lo_local = threading.local()
        self._lo_count = 0
        self._lo_lock = threading.Lock()
        self.webp = False
        if self.magick:
            out = subprocess.run([self.magick, "-list", "format"], capture_output=True, text=True).stdout
            self.webp = bool(re.search(r"^\s*WEBP\*?\s+\S+\s+[r-]w", out, re.M))

    def report(self):
        log(f"   tools: ImageMagick {'yes' if self.magick else 'NO'} (WebP {'yes' if self.webp else 'NO'}), "
            f"Ghostscript {'yes' if self.gs else 'no -> PDFs via Quick Look, no Illustrator/EPS'}, "
            f"sips {'yes' if self.sips else 'no'}, Quick Look {'yes' if self.ql else 'no'}, "
            f"LibreOffice {'yes' if self.soffice else 'no -> Canvas drawings, old Mac Word files get placeholders'}")

    def lo_profile(self):
        """file:// URL of this thread's own LibreOffice profile."""
        p = getattr(self._lo_local, "profile", None)
        if p is None:
            with self._lo_lock:
                self._lo_count += 1
                n = self._lo_count
            p = self._lo_local.profile = Path(self._lo_base) / f"profile{n}"
        return p.as_uri()

    def cleanup(self):
        shutil.rmtree(self._lo_base, ignore_errors=True)


def run(cmd, timeout=TIMEOUT):
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    if p.returncode != 0:
        lines = [l for l in (p.stderr or p.stdout).strip().splitlines() if l.strip()]
        raise RuntimeError(lines[0][:300] if lines else f"exit {p.returncode}")
    return p.stdout


def im_make(tools, src, thumb, preview, prefix="", pdf=False, trim=False, density=150):
    """One ImageMagick call: first frame/page -> preview (1200) and thumb (480) WebPs.
    trim: cut away the blank page around a drawing (leaving a small white margin)."""
    cmd = [tools.magick]
    if pdf:
        cmd += ["-density", str(density)]
    else:
        cmd += ["-define", f"jpeg:size={PREVIEW_PX * 2}x{PREVIEW_PX * 2}"]   # fast JPEG decode
    cmd += [f"{prefix}{src}[0]", "-auto-orient", "-colorspace", "sRGB", "-strip",
            "-background", "white", "-alpha", "remove", "-alpha", "off"]
    if trim:
        cmd += ["-fuzz", "2%", "-trim", "+repage", "-bordercolor", "white", "-border", "3%"]
    cmd += ["-resize", f"{PREVIEW_PX}x{PREVIEW_PX}>", "-quality", str(PREVIEW_Q),
            "-print", "%w %h\\n", "-write", str(preview),
            "-resize", f"{THUMB_PX}x{THUMB_PX}>", "-quality", str(THUMB_Q), str(thumb)]
    out = run(cmd)
    w, h = (out.split() + ["", ""])[:2]
    return int(w), int(h)


def via_png(tools, make_png, thumb, preview):
    """Run a tool that produces a PNG, then ImageMagick for the two WebPs."""
    with tempfile.TemporaryDirectory(prefix="deriv-") as td:
        png = make_png(Path(td))
        if not png or not png.exists():
            raise RuntimeError("no image produced")
        return im_make(tools, png, thumb, preview)


def im_trimmed(tools, make_png, thumb, preview):
    """Like via_png, with the blank margins trimmed (Ghostscript renders the whole page)."""
    with tempfile.TemporaryDirectory(prefix="deriv-") as td:
        png = make_png(Path(td))
        if not png or not png.exists() or png.stat().st_size == 0:
            raise RuntimeError("no image produced")
        return im_make(tools, png, thumb, preview, trim=True)


def sips_png(tools, src):
    def make(td):
        out = td / "sips.png"
        run([tools.sips, "-s", "format", "png", "-Z", str(PREVIEW_PX), str(src), "--out", str(out)])
        return out
    return make


def ql_png(tools, src):
    def make(td):
        run([tools.ql, "-t", "-s", str(PREVIEW_PX), "-o", str(td), str(src)], timeout=QL_TIMEOUT)
        pngs = list(td.glob("*.png"))
        return pngs[0] if pngs else None
    return make


def sniff(src):
    """What the file really is, from its first bytes: 'ps' (PostScript, EPS, Illustrator 8 and
    earlier), 'dos-eps' (EPS with a binary DOS header and preview), 'pdf' (Illustrator 9 and
    later save PDF), or ''."""
    try:
        with open(src, "rb") as fh:
            head = fh.read(1024)
    except OSError:
        return ""
    if head[:4] == b"\xc5\xd0\xd3\xc6":
        return "dos-eps"
    head = head.lstrip(b"\x04\r\n\t ")      # some old PostScript starts with ^D or blank lines
    if head.startswith(b"%!"):
        return "ps"
    if head.startswith(b"%PDF") or b"%PDF-" in head[:1024]:
        return "pdf"
    return ""


def ps_bbox(src, kind):
    """(llx, lly, urx, ury) in points from the PostScript header (%%HiResBoundingBox if given),
    or None. A DOS EPS keeps its PostScript at an offset given in its header."""
    try:
        with open(src, "rb") as fh:
            start = 0
            if kind == "dos-eps":
                hdr = fh.read(12)
                start = int.from_bytes(hdr[4:8], "little")
            fh.seek(start)
            head = fh.read(65536)
    except OSError:
        return None
    text = head.replace(b"\r", b"\n").decode("latin-1")
    end = text.find("%%EndComments")
    text = text[:end] if end > 0 else text
    for key in ("%%HiResBoundingBox:", "%%BoundingBox:"):
        m = re.search(re.escape(key) + r"\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)", text)
        if m:
            llx, lly, urx, ury = (float(v) for v in m.groups())
            if urx - llx >= 1 and ury - lly >= 1:
                return llx, lly, urx, ury
    return None


def gs_png(tools, src, kind):
    """PostScript -> PNG with Ghostscript, on a page exactly the size of the drawing's bounding
    box (Illustrator's art often lies partly off its page), at about 2400 px on the long side."""
    def make(td):
        out = td / "gs.png"
        cmd = [tools.gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=png16m",
               "-dTextAlphaBits=4", "-dGraphicsAlphaBits=4", "-dFirstPage=1", "-dLastPage=1",
               f"-sOutputFile={out}"]
        bb = ps_bbox(src, kind)
        if bb:
            llx, lly, urx, ury = bb
            w, h = urx - llx, ury - lly
            dpi = max(36, min(600, round(PREVIEW_PX * 2 * 72 / max(w, h))))
            cmd += [f"-r{dpi}", f"-dDEVICEWIDTHPOINTS={w:.2f}", f"-dDEVICEHEIGHTPOINTS={h:.2f}",
                    "-dFIXEDMEDIA",      # shifted with Install: it survives the file's own initgraphics
                    "-c", f"<</Install {{{-llx:.2f} {-lly:.2f} translate}}>> setpagedevice", "-f"]
        else:
            cmd += ["-r150"]
        cmd += [str(src)]
        run(cmd)
        return out
    return make


# Old Mac bitmap fonts that modern macOS no longer has -> the nearest it does have.
# (Monaco, Geneva, Helvetica, Times, Courier, Palatino, Symbol are all still there.)
FONT_MAP = {"Chicago": "Geneva", "New York": "Times", "Athens": "Times", "Toronto": "Geneva",
            "London": "Times", "Venice": "Apple Chancery", "Los Angeles": "Marker Felt",
            "San Francisco": "Geneva", "Cairo": "Zapf Dingbats", "Mobile": "Zapf Dingbats"}


def fix_drawing_xml(xml):
    """Undo two things that go wrong when LibreOffice opens Canvas drawings:
    - text boxes come in a little narrower than Canvas drew them, so labels wrap onto an
      extra line ("TAP 86 NPW T#" / "16560", "5 c" / "m"). Canvas never wrapped them,
      so the boxes are set to grow to fit their text, anchored at the left edge.
    - fonts that no longer exist are replaced by the nearest (FONT_MAP)."""
    text_styles = set(re.findall(
        r'<draw:frame draw:style-name="([^"]+)"[^>]*>\s*<draw:text-box', xml))

    def grow(m):
        if m.group(1) not in text_styles:
            return m.group(0)
        return m.group(0).replace(
            "<style:graphic-properties",
            '<style:graphic-properties draw:auto-grow-width="true" fo:wrap-option="no-wrap" '
            'draw:textarea-horizontal-align="left"', 1)
    xml = re.sub(r'<style:style style:name="([^"]+)" style:family="graphic".*?</style:style>',
                 grow, xml, flags=re.S)
    for old, new in FONT_MAP.items():
        xml = re.sub(r'((?:style:name|style:font-name(?:-asian|-complex)?|svg:font-family)=")'
                     + re.escape(old) + '"', lambda m: f'{m.group(1)}{new}"', xml)
    return xml


def lo_make(tools, src, thumb, preview):
    """Canvas drawing -> LibreOffice drawing (.fodg), text boxes and fonts fixed -> PDF
    -> trimmed WebPs (ImageMagick + Ghostscript). Two LibreOffice runs, both headless."""
    with tempfile.TemporaryDirectory(prefix="deriv-") as td:
        # LibreOffice names its output after the input, and some of these files have odd
        # "extensions" ("fig.1 sqa b5"); a plainly named link sidesteps that and keeps
        # the original's resource fork reachable
        link = Path(td) / "drawing"
        os.symlink(src, link)
        lo = [tools.soffice, f"-env:UserInstallation={tools.lo_profile()}", "--headless",
              "--norestore", "--nolockcheck", "--convert-to"]
        run(lo + ["fodg", "--outdir", td, str(link)], timeout=180)
        fodg = Path(td) / "drawing.fodg"
        if not fodg.exists() or fodg.stat().st_size == 0:
            raise RuntimeError("LibreOffice could not open it")
        fixed = Path(td) / "fixed.fodg"
        fixed.write_text(fix_drawing_xml(fodg.read_text(encoding="utf-8")), encoding="utf-8")
        run(lo + ["pdf", "--outdir", td, str(fixed)], timeout=180)
        pdf = Path(td) / "fixed.pdf"
        if not pdf.exists() or pdf.stat().st_size == 0:
            raise RuntimeError("LibreOffice could not make a PDF of it")
        # 300 dpi so a small drawing on a big page is still sharp after trimming
        return im_make(tools, pdf, thumb, preview, pdf=True, trim=True, density=300)


def keep_copy(tmp, final):
    """Copy a finished file into the derivatives folder (via a temporary name, so a stopped
    run never leaves half a file under the real name)."""
    final.parent.mkdir(parents=True, exist_ok=True)
    part = final.with_name(final.name + ".part")
    shutil.copyfile(tmp, part)
    os.replace(part, final)


def lo_doc(tools, src, thumb, preview, keep=None):
    """A document -> PDF with LibreOffice (headless) -> first page as WebPs.
    keep: {"pdf": path, "rtf": path} to also keep the whole document as PDF and RTF; on
    return it says which were made (an RTF that fails doesn't stop the rest)."""
    with tempfile.TemporaryDirectory(prefix="deriv-") as td:
        link = Path(td) / "document"          # plain name: see lo_make
        os.symlink(src, link)
        lo = [tools.soffice, f"-env:UserInstallation={tools.lo_profile()}", "--headless",
              "--norestore", "--nolockcheck", "--convert-to"]
        run(lo + ["pdf", "--outdir", td, str(link)], timeout=180)
        pdf = Path(td) / "document.pdf"
        if not pdf.exists() or pdf.stat().st_size == 0:
            raise RuntimeError("LibreOffice could not open it")
        w, h = im_make(tools, pdf, thumb, preview, pdf=True)
        if keep:
            keep_copy(pdf, keep["pdf"])
            keep["made"] = ["pdf"]
            try:
                run(lo + ["rtf", "--outdir", td, str(link)], timeout=180)
                rtf = Path(td) / "document.rtf"
                if rtf.exists() and rtf.stat().st_size > 0:
                    keep_copy(rtf, keep["rtf"])
                    keep["made"].append("rtf")
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                pass
        return w, h


def make_one(tools, job, dest):
    """Returns a dict for the derivatives table."""
    sha, archive, root, path, name, ext, mime, mac_type, puid, creator = job
    src = Path(root) / path
    kind = category(ext, mime, mac_type, puid, creator)
    slug = f"{slugify(name)}-{sha[:10]}"
    row = {"sha256": sha, "slug": slug, "kind": kind, "source_archive": archive, "source_path": path,
           "root": str(dest), "made_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if kind == "placeholder":
        pk = placeholder_kind(ext, mime, mac_type)
        row.update(method="placeholder", status="placeholder",
                   thumb=f"placeholder/{pk}.svg", preview=f"placeholder/{pk}.svg")
        return row
    rel_t, rel_p = f"thumb/{sha[:2]}/{slug}.webp", f"preview/{sha[:2]}/{slug}.webp"
    thumb, preview = dest / rel_t, dest / rel_p
    thumb.parent.mkdir(parents=True, exist_ok=True)
    preview.parent.mkdir(parents=True, exist_ok=True)

    attempts = []
    if kind == "image":
        prefix = "pict:" if (mac_type == "PICT" or (ext or "").lower() in (".pict", ".pct", ".pic")) else ""
        attempts.append(("imagemagick", lambda: im_make(tools, src, thumb, preview, prefix=prefix)))
        if tools.sips:
            attempts.append(("sips", lambda: via_png(tools, sips_png(tools, src), thumb, preview)))
    elif kind == "pdf" and tools.gs:
        attempts.append(("imagemagick", lambda: im_make(tools, src, thumb, preview, pdf=True)))
    elif kind == "olddoc":
        if not (tools.soffice and tools.gs):   # a document placeholder until LibreOffice is installed
            row.update(method="placeholder", status="placeholder",
                       thumb="placeholder/document.svg", preview="placeholder/document.svg")
            return row
        keep = None
        if mac_type in WORD_MAC or (mac_type == "WORD" and creator == "MSWD"):
            rel = {"pdf": f"pdf/{sha[:2]}/{slug}.pdf", "rtf": f"rtf/{sha[:2]}/{slug}.rtf"}
            keep = {k: dest / v for k, v in rel.items()}
        attempts.append(("libreoffice", lambda: lo_doc(tools, src, thumb, preview, keep)))
    elif kind == "postscript":
        real = sniff(src)
        if real in ("ps", "dos-eps"):
            if tools.gs:
                attempts.append(("ghostscript", lambda: im_trimmed(tools, gs_png(tools, src, real),
                                                                   thumb, preview)))
            if tools.sips:      # sips draws EPS too, if Ghostscript can't
                attempts.append(("sips", lambda: via_png(tools, sips_png(tools, src), thumb, preview)))
        elif real == "pdf":
            if tools.gs:
                attempts.append(("imagemagick", lambda: im_make(tools, src, thumb, preview, pdf=True)))
        else:               # an Illustrator-made file that isn't PostScript: any picture tool
            attempts.append(("imagemagick", lambda: im_make(tools, src, thumb, preview)))
            if tools.sips:
                attempts.append(("sips", lambda: via_png(tools, sips_png(tools, src), thumb, preview)))
        kind = {"ps": "postscript", "dos-eps": "postscript", "pdf": "pdf"}.get(real, "image")
        row["kind"] = kind
    elif kind == "drawing":
        if tools.soffice and tools.gs:
            attempts.append(("libreoffice", lambda: lo_make(tools, src, thumb, preview)))
        else:   # no LibreOffice yet: a drawing placeholder for now; a later run retries it
            row.update(method="placeholder", status="placeholder",
                       thumb="placeholder/drawing.svg", preview="placeholder/drawing.svg")
            return row
    if tools.ql and kind not in ("drawing", "postscript", "olddoc"):
        # Quick Look knows nothing of Canvas files or old Mac documents, and shows
        # PostScript as its code
        attempts.append(("quicklook", lambda: via_png(tools, ql_png(tools, src), thumb, preview)))
    if kind == "quicklook" and tools.soffice and tools.gs and \
            ((ext or "").lower() in LO_DOC_EXT or (mac_type or "") in QL_MAC - {"MooV"}):
        attempts.append(("libreoffice", lambda: lo_doc(tools, src, thumb, preview)))

    errors = []
    for method, fn in attempts:
        try:
            w, h = fn()
            row.update(method=method, status="made", thumb=rel_t, preview=rel_p, width=w, height=h)
            if kind == "olddoc" and keep:
                for k in keep.get("made", []):
                    row[k] = rel[k]
            return row
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as e:
            errors.append(f"{method}: {str(e)[:200]}")
    for p in (thumb, preview):          # never leave half-made images behind
        try:
            p.unlink()
        except OSError:
            pass
    pk = "image" if kind in ("image", "postscript") else "document" if kind == "olddoc" \
        else placeholder_kind(ext, mime, mac_type)
    row.update(method="none", status="failed", thumb=f"placeholder/{pk}.svg",
               preview=f"placeholder/{pk}.svg", error=" | ".join(errors) or "no tool available")
    return row


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description="Make WebP thumbnails/previews for archive files.")
    ap.add_argument("-a", "--archive", required=True,
                    help="archive volume to work from: name in /Volumes, or a path")
    ap.add_argument("-d", "--dest", required=CONFIG.derivatives_dir is None,
                    default=str(CONFIG.derivatives_dir) if CONFIG.derivatives_dir else None,
                    help="derivatives folder (default: derivatives_dir in config.json)")
    ap.add_argument("--workers", type=int, default=4, help="files processed in parallel (default 4)")
    ap.add_argument("--retry-failed", action="store_true", help="try earlier failures again")
    ap.add_argument("--limit", type=int, default=0, help="stop after N files (for a trial run)")
    ap.add_argument("--only", default="",
                    help='only these categories from classify_archive.py, e.g. --only TAP '
                         'or --only "TAP,SE Asian Archaeology"')
    ap.add_argument("-o", "--out", default=PROJECT_DIR, help=argparse.SUPPRESS)
    a = ap.parse_args()

    vol = volumes.path_of(a.archive)
    if not volumes.is_mounted(a.archive):
        sys.exit(f"{vol} is not mounted.")
    archive = vol.name
    dest = Path(a.dest).expanduser().resolve()
    if dest.parts[:2] == ("/", "Volumes") and len(dest.parts) > 2 and \
            not os.path.ismount(Path(*dest.parts[:3])):
        sys.exit(f"The drive for {dest} is not mounted.")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / MARKER).write_text("Derivative images made by make_derivatives.py. "
                               "identify_archive.py does not catalogue this folder.\n")
    write_placeholders(dest)

    tools = Tools()
    tools.report()
    if not tools.magick or not tools.webp:
        sys.exit("ImageMagick with WebP support is needed:  brew install imagemagick ghostscript")

    db = sqlite3.connect(Path(a.out).expanduser() / "archive_catalog.sqlite")
    db.executescript(SCHEMA)
    have = {r[1] for r in db.execute("PRAGMA table_info(derivatives)")}
    for col in ("pdf", "rtf"):                  # catalogues made by earlier versions
        if col not in have:
            db.execute(f"ALTER TABLE derivatives ADD COLUMN {col} TEXT")
    # placeholders are not skipped in SQL: those this version can now render (e.g. Canvas
    # drawings from before it knew them) are redone; the rest are filtered out below
    skip = "('made')" if a.retry_failed else "('made','failed')"
    only_sql, only_args = "", []
    if a.only:
        cats = [c.strip() for c in a.only.split(",") if c.strip()]
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='content_class'").fetchone():
            sys.exit("--only needs the categories from classify_archive.py: run that first.")
        only_sql = (f" AND f.sha256 IN (SELECT sha256 FROM content_class WHERE category IN "
                    f"({','.join('?' * len(cats))}))")
        only_args = cats
    # one copy per distinct file, among those on this archive drive; walked in path order
    # so the disk reads move steadily through the drive
    rows = db.execute(f"""
        SELECT sha256, archive, path, COALESCE(uname, name), ext, mime, mac_type, puid, creator,
               dstatus, dthumb, dmethod FROM (
          SELECT f.sha256, f.archive, MIN(f.path) path, f.name, f.ext, f.mime, f.mac_type, f.puid,
                 f.mac_creator creator,
                 (SELECT u.name FROM unique_files u WHERE u.sha256 = f.sha256) uname,
                 d.status dstatus, d.thumb dthumb, d.method dmethod
          FROM files f LEFT JOIN derivatives d ON d.sha256 = f.sha256
          WHERE f.archive=? AND f.junk=0 AND f.sha256 IS NOT NULL AND substr(f.name, 1, 2) <> '._'
            AND (d.status IS NULL OR d.status NOT IN {skip}
                 OR d.method = 'quicklook'
                 OR (d.status = 'failed' AND d.error LIKE 'quicklook:%')){only_sql}
          GROUP BY f.sha256)
        ORDER BY path""", (archive, *only_args)).fetchall()
    # an earlier placeholder is redone only if this version would now make an image, or a
    # better placeholder (a Canvas drawing filed as "unknown" before LibreOffice is installed)
    jobs, redo, redo_ps, redo_ql = [], 0, 0, 0
    for r in rows:
        k = category(r[4], r[5], r[6], r[7], r[8])
        if r[9] == "made":
            # made by Quick Look by an earlier version: redo only Illustrator/EPS/PostScript,
            # whose "preview" was a picture of their code
            if r[11] != "quicklook" or k != "postscript" or not tools.gs:
                continue
            redo_ps += 1
        elif r[9] == "placeholder":
            if k == "placeholder" or (k in ("drawing", "olddoc") and not tools.soffice):
                continue
            redo += 1
        elif r[9] == "failed" and not a.retry_failed:
            # Quick Look failed (old Mac Word files, mostly): LibreOffice now tries them
            if not tools.soffice:
                continue
            redo_ql += 1
        jobs.append(r[:9])
    no_sum = db.execute("SELECT COUNT(*) FROM files WHERE archive=? AND junk=0 AND sha256 IS NULL",
                        (archive,)).fetchone()[0]
    if a.limit:
        jobs = jobs[:a.limit]
    kinds = {}
    for j in jobs:
        k = category(j[4], j[5], j[6], j[7], j[8])
        kinds[k] = kinds.get(k, 0) + 1
    log(f"== {archive}: {len(jobs):,} distinct files to do" + (f" (only {a.only})" if a.only else "")
        + f" -> {dest}")
    log("   " + ", ".join(f"{k} {n:,}" for k, n in sorted(kinds.items())))
    if redo:
        log(f"   ({redo:,} of these had a placeholder from an earlier run and are redone)")
    if redo_ql:
        log(f"   ({redo_ql:,} documents Quick Look couldn't do are tried in LibreOffice)")
    if redo_ps:
        log(f"   ({redo_ps:,} Illustrator/EPS files previewed as text by an earlier run are redone)")
    if no_sum:
        log(f"   ({no_sum:,} files on this drive have no checksum yet and are skipped -- "
            f'run identify_archive.py -a "{archive}" first)')

    cols = ["sha256", "slug", "kind", "method", "status", "root", "thumb", "preview", "width",
            "height", "pdf", "rtf", "source_archive", "source_path", "error", "made_at"]
    counts = {"made": 0, "placeholder": 0, "failed": 0}
    t0 = last = time.time()
    done = 0
    with cf.ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = [ex.submit(make_one, tools, (j[0], j[1], str(vol), *j[2:]), dest) for j in jobs]
        try:
            for fut in cf.as_completed(futs):
                row = fut.result()
                db.execute(f"INSERT OR REPLACE INTO derivatives ({','.join(cols)}) "
                           f"VALUES ({','.join('?' * len(cols))})", [row.get(c) for c in cols])
                counts[row["status"]] += 1
                done += 1
                if row["status"] == "failed":
                    log(f"   failed: {row['source_path']}: {row['error'][:160]}")
                if done % 100 == 0:
                    db.commit()
                if time.time() - last > 30:
                    last = time.time()
                    rate = done / (last - t0)
                    left = (len(jobs) - done) / rate / 3600 if rate else 0
                    log(f"   {done:,}/{len(jobs):,}  made {counts['made']:,}, placeholder "
                        f"{counts['placeholder']:,}, failed {counts['failed']:,}  "
                        f"({rate:.1f}/s, ~{left:.1f} h left)")
        except KeyboardInterrupt:
            log("   stopping -- finishing the files in progress ...")
            for f in futs:
                f.cancel()
            raise
        finally:
            db.commit()
            tools.cleanup()
    total = db.execute("SELECT status, COUNT(*) FROM derivatives GROUP BY status").fetchall()
    db.close()
    print(f"{archive}: {done:,} done in {time.time() - t0:.0f} s -- made {counts['made']:,}, "
          f"placeholder {counts['placeholder']:,}, failed {counts['failed']:,} | catalogue now: "
          + ", ".join(f"{s} {n:,}" for s, n in sorted(total)), flush=True)


if __name__ == "__main__":
    main()
