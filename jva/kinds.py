"""What sort of file something is, from its extension, MIME type and classic Mac type and
creator codes; and the Site and Season a file belongs to, from its paths.

The format lists here are the only ones: the search app's Kind (kind_of), the search tables
(prepare_search.py) and the preview images (make_derivatives.py) all use them."""
import re


KINDS = ["Image", "PDF", "MS Word", "Presentation", "Spreadsheet", "Database", "Text", "Web page",
         "Video", "Audio", "Other"]

IMAGE_EXT = {".jpg", ".jpeg", ".jpe", ".tif", ".tiff", ".png", ".gif", ".bmp", ".psd", ".psb",
             ".heic", ".heif", ".webp", ".jp2", ".j2k", ".pct", ".pict", ".pic", ".tga",
             ".nef", ".cr2", ".cr3", ".crw", ".dng", ".orf", ".arw", ".raf", ".rw2", ".raw",
             ".eps", ".ai", ".svg", ".ico", ".icns", ".xcf", ".pcx", ".sgi", ".exr"}
# The kinds of documents. Each is recognised by extension, by classic Mac type code, or by the
# format name Siegfried gave it (for files with no extension, or a meaningless one).
WORD_EXT = {".doc", ".docx", ".dot", ".dotx", ".docm", ".dotm", ".rtf", ".mcw", ".word"}
WORD_DOC_MAC = {"WDBN", "W8BN", "W6BN", "WXBN", "W8TN", "W6TN", "WDTN", "WTMP", "RTF "}
WORD_FORMAT = re.compile(r"microsoft word|rich text format", re.I)
SLIDE_EXT = {".ppt", ".pptx", ".pps", ".ppsx", ".pptm", ".pot", ".potx", ".key", ".odp"}
SLIDE_MAC = {"SLD3", "SLD8", "SLD0", "SLDS", "PPT3"}
SLIDE_FORMAT = re.compile(r"powerpoint|keynote|presentation", re.I)
SHEET_EXT = {".xls", ".xlsx", ".xlsm", ".xlt", ".xltx", ".csv", ".tsv", ".tab", ".numbers", ".ods",
             ".excel", ".wks", ".wk1"}
SHEET_MAC = {"XLS3", "XLS4", "XLS5", "XLS8", "XLBN", "XLSX", "CWSS"}
SHEET_FORMAT = re.compile(r"excel|separated values|spreadsheet|worksheet|lotus 1-2-3", re.I)
DB_EXT = {".fp3", ".fp5", ".fp7", ".fmp12", ".fmp", ".mdb", ".accdb", ".dbf"}
DB_MAC = {"FMPR", "FMP3", "FMP5", "FMP7", "CWDB"}
DB_FORMAT = re.compile(r"filemaker|access database|dbase", re.I)
WEB_EXT = {".htm", ".html", ".mht", ".mhtml", ".xhtml", ".css", ".url", ".webloc", ".asp", ".aspx",
           ".php", ".js", ".jsp"}
WEB_FORMAT = re.compile(r"hypertext|style sheet|internet shortcut|office file list|vector markup", re.I)
TEXT_EXT = {".txt", ".text", ".md", ".tex", ".log", ".wpd", ".wp", ".wp5", ".wps", ".pages", ".odt",
            ".cwk", ".pub", ".eml", ".ltr", ".memo", ".fax"}
TEXT_MAC = {"TEXT", "ttro", "WORD", "MWII", "MW2D", "CWWP", "nX^d", "WPD0", "WPD1", "WPD2", "WPD3"}
TEXT_FORMAT = re.compile(r"plain text|wordperfect|works word|macwrite|appleworks word|clarisworks word|"
                         r"opendocument text|iwork pages|publisher|text file", re.I)
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".mpg", ".mpeg", ".3gp", ".mts", ".m2ts", ".wmv",
             ".mkv", ".dv", ".vob", ".flv"}
AUDIO_EXT = {".mp3", ".m4a", ".aif", ".aiff", ".wav", ".flac", ".ogg", ".wma", ".aac", ".mid", ".midi"}
IMAGE_MAC = {"JPEG", "TIFF", "PICT", "GIFf", "PNGf", "8BPS", "EPSF", "EPSP", "EPSI", "BMP ", "BMPf"}

# Adobe Illustrator and other PostScript. On the old Macs, Illustrator 5-8 saved plain
# PostScript with Mac type TEXT and creator ART3/ART5/ARTZ..., often with no extension.
PS_EXT = {".ai", ".eps", ".epsf", ".epsi", ".ps"}
PS_MAC = {"EPSF", "EPSP", "EPSI"}
# Deneba Canvas drawings. On the old drives these mostly have no extension, or a meaningless
# one ("T_16560", "fig.1", ...), so the Mac type code is what identifies them.
DRAWING_MAC = {"drw2", "drw3", "drw5", "drwt"}
DRAWING_EXT = {".cvs", ".cv5", ".cv6", ".cv7", ".cv8", ".cv9", ".cvx", ".canvas", ".can"}
# Old Mac word-processor, spreadsheet and presentation files, by Mac type code: Word 4/5
# (WDBN), Word 6 (W6BN), Word 98 (W8BN), MacWrite (MACA "WORD", MWII), ClarisWorks/AppleWorks
# (CWWP/CWSS/CWDB/CWGR), WriteNow (nX^d), old Excel and PowerPoint.
OLD_DOC_MAC = {"WDBN", "W6BN", "W8BN", "WORD", "MWII", "MW2D", "CWWP", "CWSS", "CWDB", "CWGR",
               "nX^d", "XLS3", "XLS4", "XLS5", "XLS8", "XLBN", "SLD3", "SLD8", "PPT3"}
# old Microsoft Word for Mac: Word 4-5 (WDBN), 6 (W6BN), 98 (W8BN); Word 1-3 is WORD/MSWD
WORD_MAC = {"WDBN", "W6BN", "W8BN"}


# Bump this whenever kind_of() changes: the search app then ignores the kinds stored by
# prepare_search.py until it is run again.
RULES_VERSION = 5


def kind_of(ext, mime, mac_type, creator="", fmt=""):
    """Image, PDF, MS Word, Presentation, Spreadsheet, Database, Text, Web page, Video, Audio or
    Other, from a file's extension, MIME type, classic Mac type and creator codes, and the format
    name Siegfried identified (fmt)."""
    ext, mime, mac_type, fmt = (ext or "").lower(), (mime or "").lower(), mac_type or "", fmt or ""
    # Adobe Illustrator (creator ART3/ART5/ARTZ...) and other PostScript are drawings, even
    # though the old Macs typed Illustrator's PostScript files as TEXT or PDF
    if (creator or "").startswith("ART") or ext in PS_EXT or mime in ("application/postscript",
                                                                         "application/illustrator"):
        return "Image"
    if ext == ".pdf" or mime == "application/pdf" or mac_type == "PDF ":
        return "PDF"
    if ext in IMAGE_EXT or mime.startswith("image/") or mac_type in IMAGE_MAC:
        return "Image"
    if ext in VIDEO_EXT or mime.startswith("video/") or mac_type == "MooV":
        return "Video"
    if ext in AUDIO_EXT or mime.startswith("audio/"):
        return "Audio"
    if ext in DB_EXT or mac_type in DB_MAC or DB_FORMAT.search(fmt):
        return "Database"
    if ext in SLIDE_EXT or mac_type in SLIDE_MAC or SLIDE_FORMAT.search(fmt):
        return "Presentation"
    if ext in SHEET_EXT or mac_type in SHEET_MAC or SHEET_FORMAT.search(fmt):
        return "Spreadsheet"
    if ext in WEB_EXT or mime == "text/html" or WEB_FORMAT.search(fmt):
        return "Web page"
    if ext in WORD_EXT or mac_type in WORD_DOC_MAC or WORD_FORMAT.search(fmt) or \
            (mac_type == "WORD" and creator == "MSWD"):
        return "MS Word"
    if ext in TEXT_EXT or mac_type in TEXT_MAC or mime.startswith("text/") or TEXT_FORMAT.search(fmt):
        return "Text"
    return "Other"


# ---- Site and Season, from the words in a file's paths (all its copies) ----
SITES = ["npw", "nml", "tk", "nkh", "pl", "pf"]        # shown in this order
SEASONS = ["85", "86", "90", "92", "94"]                 # TAP field seasons
_WORDS = re.compile(r"[a-z]+|[0-9]+")


def tags_of(path):
    """{("site", "npw"), ("season", "86"), ...} for one path. A path is split into words at
    spaces, punctuation and letter/digit changes ("NPW86 sherds/TAP94.export" -> npw, 86,
    sherds, tap, 94, export); a word that is exactly a site code or season counts."""
    words = set(_WORDS.findall(path.lower()))
    return {("site", w) for w in SITES if w in words} | {("season", w) for w in SEASONS if w in words}
