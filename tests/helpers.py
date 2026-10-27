"""A small synthetic archive for the tests: two archive "drives" (folders standing in for
/Volumes/<name>), duplicate photos, a Courses folder, a World Trip/China folder, Mac clutter
and "._" companions, and TIFFs with and without a broken Exif pointer. make_archive() builds
it in a temporary folder with a catalogue, a config.json, and the unique_files table."""
import hashlib
import importlib.util
import json
import os
import sqlite3
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(REPO))


def load_script(name):
    """Import scripts/<name>.py as a module (it reads its settings from $JVA_CONFIG)."""
    spec = importlib.util.spec_from_file_location(f"script_{name}", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPTS))
    spec.loader.exec_module(mod)
    return mod


def run_script(name, *args, config, input_text=None, env=None):
    """Run scripts/<name>.py with this config; -> CompletedProcess (text)."""
    env = dict(env or os.environ, JVA_CONFIG=str(config), JVA_NO_WARM="1")
    return subprocess.run([sys.executable, str(SCRIPTS / f"{name}.py"), *args], env=env, text=True,
                          capture_output=True, input=input_text, timeout=300)


def tiff_bytes(exif="ok", w=40, h=30, shade=7):
    """A small, valid little-endian RGB TIFF with an Exif directory (and an Interoperability
    directory inside it). exif="eof" makes the Exif pointer point past the end of the file,
    "pixels" into the image data."""
    e = "<"
    pix = bytes((x * shade) % 256 for x in range(w * h * 3))
    data = bytearray(b"II" + struct.pack(e + "HI", 42, 0))
    strip = len(data)
    data += pix
    interop = len(data)
    data += struct.pack(e + "H", 1) + struct.pack(e + "HHI4s", 1, 2, 4, b"R98\0") + struct.pack(e + "I", 0)
    exif_off = len(data)
    data += struct.pack(e + "H", 2) + struct.pack(e + "HHII", 0x9000, 7, 4, 0x30323230)
    data += struct.pack(e + "HHII", 40965, 4, 1, interop) + struct.pack(e + "I", 0)
    ents = [(256, 3, 1, w), (257, 3, 1, h), (258, 3, 1, 8), (259, 3, 1, 1), (262, 3, 1, 2),
            (273, 4, 1, strip), (277, 3, 1, 3), (278, 3, 1, h), (279, 4, 1, len(pix)),
            (34665, 4, 1, {"ok": exif_off, "eof": 10 ** 8, "pixels": strip + 100}[exif])]
    ifd = len(data)
    data += struct.pack(e + "H", len(ents)) + b"".join(struct.pack(e + "HHII", *x) for x in ents)
    data += struct.pack(e + "I", 0)
    struct.pack_into(e + "I", data, 4, ifd)
    return bytes(data)


# (archive drive, path on it, content); identical content = identical copies
PHOTO = b"\xff\xd8\xff\xe0 photo of burial 2 at Non Pa Wai " * 50
LAYOUT = [
    ("ARCH1", "CumulativeArchive/JUDY/NPW 1986/Burial 2.jpg", PHOTO),
    ("ARCH1", "CumulativeArchive/JUDY/NPW 1986/._Burial 2.jpg", b"\x00\x05\x16\x07 appledouble"),
    ("ARCH1", "CumulativeArchive/JUDY/NPW 1986/.DS_Store", b"ds store"),
    ("ARCH1", "CumulativeArchive/JUDY/World Trip/China 1/img1.jpg", b"\xff\xd8\xff great wall " * 40),
    ("ARCH1", "CumulativeArchive/JUDY/Courses/ANT 318/lecture.ppt", b"\xd0\xcf\x11\xe0 slides " * 30),
    ("ARCH1", "CumulativeArchive/THAI 2008/TAP 94 NML/sherd.tif", tiff_bytes("ok", shade=7)),
    ("ARCH1", "CumulativeArchive/THAI 2008/TAP 94 NML/bad exif.tif", tiff_bytes("eof", shade=9)),
    ("ARCH1", "CumulativeArchive/THAI 2008/TAP 94 NML/bad pixels.tif", tiff_bytes("pixels", shade=11)),
    ("ARCH1", "CumulativeArchive/THAI 2008/copies/Burial 2 copy.jpg", PHOTO),
    ("ARCH1", "CumulativeArchive/THAI 2008/misc/notes.txt", b"field notes, NKH season 90\n" * 20),
    ("ARCH2", "CumulativeArchive/JUDY/NPW 1986/Burial 2.jpg", PHOTO),
    ("ARCH2", "CumulativeArchive/JUDY/Museum displays/case 3.jpg", b"\xff\xd8\xff museum case " * 40),
]


def make_archive(tmp=None, app=None):
    """-> (root folder, config.json path). Builds the drives, the catalogue (files and
    unique_files, like identify_archive.py) and a config pointing at them."""
    root = Path(tmp or tempfile.mkdtemp(prefix="jva-test-"))
    vols, data = root / "Volumes", root / "data"
    data.mkdir(parents=True, exist_ok=True)
    for archive, path, content in LAYOUT:
        p = vols / archive / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        os.utime(p, (1_210_000_000, 1_210_000_000))        # 2008-05-05
    cfg = {"data_dir": str(data), "volumes_root": str(vols),
           "derivatives_dir": str(root / "derivatives"),
           "app": dict({"mode": "local", "warm_cache": False, "derivatives_roots": [str(root / "derivatives")]},
                       **(app or {})),
           "publish": {"out_dir": str(root / "public")}}
    config = root / "config.json"
    config.write_text(json.dumps(cfg, indent=1))
    build_catalogue(data / "archive_catalog.sqlite", vols)
    return root, config


def build_catalogue(cat, vols):
    os.environ["JVA_CONFIG"] = str(cat.parent.parent / "config.json")
    ia = load_script("identify_archive")
    db = sqlite3.connect(cat)
    db.executescript(ia.SCHEMA)
    for archive in sorted({a for a, _, _ in LAYOUT}):
        db.execute("INSERT INTO archives(name, uuid, last_scan) VALUES (?, '', '2026-10-27')", (archive,))
    for archive, path, content in LAYOUT:
        name = path.rsplit("/", 1)[-1]
        full = vols / archive / path
        st = full.stat()
        ext = os.path.splitext(name)[1].lower()
        tif = ext == ".tif"
        db.execute("""INSERT INTO files(archive, unit, drive, path, name, ext, size, mtime, modified, junk,
                                        sha256, puid, format, mime)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (archive, "CumulativeArchive", path.split("/")[1], path, name, ext, st.st_size, st.st_mtime,
                    "2008-05-05 15:06:40", int(ia.is_clutter(name, st.st_size)),
                    hashlib.sha256(content).hexdigest(), "fmt/353" if tif else "", "Tagged Image File Format"
                    if tif else "", "image/tiff" if tif else ("image/jpeg" if ext == ".jpg" else "")))
    db.commit()
    build_unique(db)
    db.close()


def build_unique(db):
    try:
        from jva.unique import build_unique as b
    except ImportError:
        b = load_script("identify_archive").build_unique
    b(db)


def connect(config):
    return sqlite3.connect(Path(json.loads(Path(config).read_text())["data_dir"]) / "archive_catalog.sqlite")
