"""publish.py: the public catalogue, and the upload (to a stand-in "server" folder)."""
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers as H  # noqa: E402
from test_webapp import load_app  # noqa: E402


def setUpModule():
    global ROOT, CONFIG, PUB, DERIV, SHA
    ROOT, CONFIG = H.make_archive()
    cfg = json.loads(CONFIG.read_text())
    cfg["rules"] = str(Path(__file__).resolve().parent / "rules_fixture.csv")
    CONFIG.write_text(json.dumps(cfg))
    for script in ("classify_archive", "prepare_search"):
        r = H.run_script(script, config=CONFIG)
        assert r.returncode == 0, r.stderr
    DERIV = ROOT / "derivatives"
    db = H.connect(CONFIG)
    SHA = dict(db.execute("SELECT name, sha256 FROM unique_files"))
    from jva.catalog import DELETED_FILES, DERIVATIVES, REMOVED_COPIES
    db.executescript(DERIVATIVES + REMOVED_COPIES + DELETED_FILES)
    for name, files, pdf in (("Burial 2.jpg", True, None),    # TAP: published
                             ("sherd.tif", False, None),      # TAP, but its images are missing
                             ("lecture.ppt", True, "doc/le/lecture.pdf")):   # Class: left out
        sha = SHA[name]
        rel = {k: f"{k}/{sha[:2]}/{name}.webp" for k in ("thumb", "preview")}
        if files:
            for p in list(rel.values()) + ([pdf] if pdf else []):
                (DERIV / p).parent.mkdir(parents=True, exist_ok=True)
                (DERIV / p).write_bytes(b"RIFF....WEBP")
        db.execute("INSERT INTO derivatives(sha256, slug, kind, status, root, thumb, preview, pdf) "
                   "VALUES (?, ?, 'image', 'made', ?, ?, ?, ?)",
                   (sha, name, str(DERIV), rel["thumb"], rel["preview"], pdf))
    db.execute("INSERT INTO removed_copies(archive, path) VALUES ('ARCH1', 'gone.jpg')")
    db.execute("INSERT INTO deleted_files(archive, path) VALUES ('ARCH1', 'deleted.jpg')")
    db.commit()
    db.close()
    r = H.run_script("publish", "-d", str(DERIV), config=CONFIG)
    assert r.returncode == 0, r.stdout + r.stderr
    PUB = sqlite3.connect(ROOT / "public" / "archive_public.sqlite")


class PublicCatalogue(unittest.TestCase):
    def test_categories_left_out(self):
        cats = {r[0] for r in PUB.execute("SELECT DISTINCT COALESCE(category, 'Unclassified') FROM unique_files")}
        self.assertTrue(cats)
        self.assertFalse(cats & {"Class", "Unclassified"})
        names = {r[0] for r in PUB.execute("SELECT name FROM files")}
        self.assertNotIn("lecture.ppt", names)
        self.assertIn("Burial 2.jpg", names)
        self.assertFalse(any(n.startswith(".") for n in names))       # no clutter rows

    def test_everything_consistent(self):
        self.assertEqual(PUB.execute("SELECT COUNT(*) FROM files WHERE unique_id NOT IN "
                                     "(SELECT id FROM unique_files)").fetchone()[0], 0)
        self.assertEqual(PUB.execute("SELECT COUNT(*) FROM file_class fc WHERE NOT EXISTS (SELECT 1 FROM files f "
                                     "WHERE f.archive = fc.archive AND f.path = fc.path)").fetchone()[0], 0)
        hits = PUB.execute("SELECT COUNT(*) FROM unique_search WHERE unique_search MATCH 'lecture'").fetchone()[0]
        self.assertEqual(hits, 0)
        hits = PUB.execute("SELECT COUNT(*) FROM unique_search WHERE unique_search MATCH 'burial'").fetchone()[0]
        self.assertEqual(hits, 1)

    def test_housekeeping_dropped(self):
        tables = {r[0] for r in PUB.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(tables & {"removed_copies", "deleted_files", "tiff_repairs"})
        self.assertTrue({"unique_tags", "unique_drives", "search_meta"} <= tables)
        self.assertEqual(PUB.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertEqual(PUB.execute("SELECT COUNT(*) FROM derivatives WHERE root <> ''").fetchone()[0], 0)

    def test_derivatives(self):
        st = dict(PUB.execute("SELECT sha256, status FROM derivatives"))
        self.assertEqual(st, {SHA["Burial 2.jpg"]: "made", SHA["sherd.tif"]: "missing"})
        listed = set((ROOT / "public" / "derivatives_files.txt").read_text().splitlines())
        b = SHA["Burial 2.jpg"][:2]
        self.assertEqual(listed, {f"preview/{b}/Burial 2.jpg.webp", f"thumb/{b}/Burial 2.jpg.webp"})

    def test_app_on_public_catalogue(self):
        cfg = json.loads(CONFIG.read_text())
        cfg["catalogue"] = str(ROOT / "public" / "archive_public.sqlite")
        cfg["app"] = {"mode": "public", "derivatives_url": "/derivatives", "warm_cache": False}
        pc = ROOT / "server-config.json"
        pc.write_text(json.dumps(cfg))
        A = load_app(pc)
        c = A.app.test_client()
        for url in ["/", "/search", "/search?view=list&sort=site", "/browse", "/browse?a=ARCH1",
                    f"/file/{SHA['Burial 2.jpg']}", "/search?q=burial"]:
            self.assertEqual(c.get(url).status_code, 200, url)
        self.assertEqual(c.get(f"/file/{SHA['lecture.ppt']}").status_code, 404)
        self.assertIn("/derivatives/thumb/", c.get("/search").get_data(as_text=True))


@unittest.skipUnless(shutil.which("rsync"), "needs rsync")
class Upload(unittest.TestCase):
    """The upload, with a fake ssh that runs the "remote" commands on this machine."""

    def test_unreachable_server_stops_before_building(self):
        bin_dir = Path(tempfile.mkdtemp())
        ssh = bin_dir / "ssh"
        ssh.write_text('#!/bin/sh\necho "ssh: Could not resolve hostname nowhere" >&2\nexit 255\n')
        ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
        out = ROOT / "public-unreachable"
        r = H.run_script("publish", "--upload", "-d", str(DERIV), "--server", "nowhere", "--out", str(out),
                         config=CONFIG, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Could not resolve hostname", r.stderr)        # ssh's own message is shown
        self.assertIn("ssh couldn't connect", r.stderr)
        self.assertFalse(out.exists())                               # nothing was built

    def test_upload_and_prune(self):
        bin_dir = Path(tempfile.mkdtemp())
        ssh = bin_dir / "ssh"
        ssh.write_text('#!/bin/sh\nwhile [ "$1" = -o ] || [ "$1" = -i ]; do echo "$1 $2" >> "$SSH_LOG"; shift 2; done\nshift\nexec sh -c "$*"\n')
        ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
        images = ROOT / "mnt" / "images"                  # shared with other sites
        (images / "other_site" / "pics").mkdir(parents=True)
        (images / "other_site" / "pics" / "keep.jpg").write_bytes(b"not ours")
        log = bin_dir / "ssh.log"
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", SSH_LOG=str(log))
        key = bin_dir / "server key.pem"
        key.write_text("not really a key")
        key.chmod(0o644)

        def upload(images_dir, *extra):
            return H.run_script("publish", "--upload-only", "-d", str(DERIV), "--server", "fakehost", *extra,
                                "--remote-data-dir", str(images / "jva_data"),
                                "--remote-images-dir", str(images_dir), config=CONFIG, env=env)

        # pointed at the shared parent by mistake: refused, nothing touched
        r = upload(images)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("isn't marked", r.stdout + r.stderr)
        self.assertTrue((images / "other_site" / "pics" / "keep.jpg").exists())

        # a key others can read is refused before anything happens, as ssh itself would
        r = upload(images / "jva_images", "--ssh-key", str(key))
        self.assertIn("chmod 600", r.stderr)
        self.assertFalse((images / "jva_images").exists())
        key.chmod(0o600)

        r = upload(images / "jva_images", "--ssh-key", str(key))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"-i {key}", log.read_text())          # ssh and rsync both used the key
        self.assertGreaterEqual(log.read_text().count(f"-i {key}"), 5)
        self.assertTrue((images / "jva_data" / "archive_public.sqlite").exists())
        self.assertFalse((images / "jva_data" / "archive_public.sqlite.new").exists())
        b = SHA["Burial 2.jpg"][:2]
        self.assertTrue((images / "jva_images" / "thumb" / b / "Burial 2.jpg.webp").exists())
        self.assertTrue((images / "jva_images" / ".jva_images").exists())

        # a later upload prunes files the catalogue no longer uses, and only in jva_images
        thumb = images / "jva_images" / "thumb" / b / "Burial 2.jpg.webp"
        thumb.chmod(0o600)                                  # unreadable by Apache
        (images / "jva_images" / "thumb").chmod(0o700)
        (images / "jva_images" / "thumb" / "zz").mkdir()
        (images / "jva_images" / "thumb" / "zz" / "old.webp").write_bytes(b"old")
        r = upload(images / "jva_images")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((images / "jva_images" / "thumb" / "zz").exists())
        self.assertEqual(thumb.stat().st_mode & 0o044, 0o044)                      # readable again
        self.assertEqual((images / "jva_images" / "thumb").stat().st_mode & 0o055, 0o055)
        self.assertTrue((images / "jva_images" / ".jva_images").exists())
        self.assertTrue((images / "other_site" / "pics" / "keep.jpg").exists())
        self.assertFalse(list(images.rglob("lecture*")))


if __name__ == "__main__":
    unittest.main()
