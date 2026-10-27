"""kinds.py -- the broad kind of a file (Image, PDF, Document, Video, Audio, Other), worked out from
its extension, MIME type and classic Mac type code; and the Site and Season a file belongs
to, from its paths. Shared by archive_search.py and prepare_search.py."""
import re


KINDS = ["Image", "PDF", "Document", "Video", "Audio", "Other"]

IMAGE_EXT = {".jpg", ".jpeg", ".jpe", ".tif", ".tiff", ".png", ".gif", ".bmp", ".psd", ".psb",
             ".heic", ".heif", ".webp", ".jp2", ".pct", ".pict", ".pic", ".tga", ".nef", ".cr2",
             ".cr3", ".crw", ".dng", ".orf", ".arw", ".raf", ".rw2", ".raw", ".eps", ".ai", ".svg",
             ".ico", ".icns"}
DOC_EXT = {".doc", ".docx", ".dot", ".rtf", ".txt", ".text", ".md", ".pages", ".odt", ".wpd",
           ".wps", ".xls", ".xlsx", ".csv", ".tsv", ".numbers", ".ods", ".ppt", ".pptx", ".pps",
           ".key", ".odp", ".html", ".htm", ".xml", ".eml", ".tex", ".fp5", ".fp7", ".fmp12"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".mpg", ".mpeg", ".3gp", ".mts", ".m2ts", ".wmv",
             ".mkv", ".dv", ".vob", ".flv"}
AUDIO_EXT = {".mp3", ".m4a", ".aif", ".aiff", ".wav", ".flac", ".ogg", ".wma", ".aac", ".mid"}
IMAGE_MAC = {"JPEG", "TIFF", "PICT", "GIFf", "PNGf", "8BPS", "EPSF", "EPSP", "EPSI"}
DOC_MAC = {"TEXT", "WDBN", "W8BN", "W6BN", "XLS8", "XLS5", "SLD8", "PPT3", "RTF "}


# Bump this whenever kind_of() changes: the search app then ignores the kinds stored by
# prepare_search.py until it is run again.
RULES_VERSION = 3

PS_EXT = {".ai", ".eps", ".epsf", ".epsi", ".ps"}


def kind_of(ext, mime, mac_type, creator=""):
    ext, mime, mac_type = (ext or "").lower(), (mime or "").lower(), mac_type or ""
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
    if ext in DOC_EXT or mime.startswith("text/") or mac_type in DOC_MAC:
        return "Document"
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
