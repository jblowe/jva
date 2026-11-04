"""Offloading files that are already in S3 Glacier: stubs on the drives, rows kept in the
catalogue, and the other scripts leaving offloaded copies alone."""
import csv
import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers as H  # noqa: E402

PHOTO_SHA = hashlib.sha256(H.PHOTO).hexdigest()
SHERD = "CumulativeArchive/THAI 2008/TAP 94 NML/sherd.tif"
BURIAL = "CumulativeArchive/JUDY/NPW 1986/Burial 2.jpg"
BURIAL_COPY = "CumulativeArchive/THAI 2008/copies/Burial 2 copy.jpg"


class Offload(unittest.TestCase):
    def setUp(self):
        self.root, self.config = H.make_archive()
        H.run_script("prepare_search", config=self.config)
        self.vols = self.root / "Volumes"
        db = H.connect(self.config)
        sherd_sha = db.execute("SELECT sha256 FROM files WHERE path=?", (SHERD,)).fetchone()[0]
        self.manifest = self.root / "manifest.csv"
        with open(self.manifest, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["unique_id", "sha256", "size_bytes", "archive", "path", "s3_key"])
            w.writerow([1, PHOTO_SHA, len(H.PHOTO), "ARCH1", BURIAL, f"ARCH1/{BURIAL}"])
            w.writerow([2, sherd_sha, 0, "ARCH1", SHERD, f"ARCH1/{SHERD}"])
        self.checked = self.root / "checked"
        self.checked.mkdir()
        (self.checked / "matched_ARCH1.txt").write_text(BURIAL + "\n")      # the sherd isn't verified

    def offload(self, *extra):
        return H.run_script("offload_files", str(self.manifest), "--bucket", "test-bucket",
                            "--verified", str(self.checked), "--volumes", str(self.vols), *extra,
                            config=self.config)

    def test_dry_run_changes_nothing(self):
        r = self.offload()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Would offload 3 copies", r.stdout)
        self.assertEqual((self.vols / "ARCH1" / BURIAL).read_bytes(), H.PHOTO)
        self.assertEqual(H.connect(self.config).execute("SELECT COUNT(*) FROM offloaded").fetchone()[0], 0)

    def test_offload_every_copy_keeps_catalogue(self):
        r = self.offload("--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Offloaded 3 copies", r.stdout)
        for a, p in (("ARCH1", BURIAL), ("ARCH1", BURIAL_COPY), ("ARCH2", BURIAL)):
            stub = (self.vols / a / p).read_text()
            self.assertTrue(stub.startswith(f"s3://test-bucket/ARCH1/{BURIAL}\n"), stub)
        self.assertEqual((self.vols / "ARCH1" / SHERD).read_bytes()[:2], b"II")        # not verified: untouched
        db = H.connect(self.config)
        self.assertEqual(db.execute("SELECT copies FROM unique_files WHERE sha256=?", (PHOTO_SHA,)).fetchone()[0], 3)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM offloaded WHERE sha256=?", (PHOTO_SHA,)).fetchone()[0], 3)
        self.assertEqual(db.execute("SELECT size FROM files WHERE archive='ARCH1' AND path=?", (BURIAL,)).fetchone()[0],
                         len(H.PHOTO))
        r = self.offload("--apply")                                                # again: nothing to do
        self.assertIn("Offloaded 0 copies", r.stdout)

    def test_changed_copy_is_skipped(self):
        (self.vols / "ARCH2" / BURIAL).write_bytes(b"something else")
        r = self.offload("--apply")
        self.assertIn("Offloaded 2 copies", r.stdout)
        self.assertIn("changed since it was catalogued", r.stdout)
        self.assertEqual((self.vols / "ARCH2" / BURIAL).read_bytes(), b"something else")

    def test_rescan_keeps_rows_and_notices_restore(self):
        self.offload("--apply")
        r = H.run_script("identify_archive", "-a", str(self.vols / "ARCH1"), "--skip-identify", config=self.config)
        self.assertEqual(r.returncode, 0, r.stderr)
        db = H.connect(self.config)
        row = db.execute("SELECT size, sha256, junk FROM files WHERE archive='ARCH1' AND path=?", (BURIAL,)).fetchone()
        self.assertEqual(row, (len(H.PHOTO), PHOTO_SHA, 0))
        (self.vols / "ARCH1" / BURIAL).write_bytes(H.PHOTO)                          # restored from S3
        r = H.run_script("identify_archive", "-a", str(self.vols / "ARCH1"), "--skip-identify", config=self.config)
        self.assertEqual(r.returncode, 0, r.stderr)
        db = H.connect(self.config)
        self.assertIsNone(db.execute("SELECT 1 FROM offloaded WHERE archive='ARCH1' AND path=?", (BURIAL,)).fetchone())
        self.assertEqual(db.execute("SELECT COUNT(*) FROM offloaded").fetchone()[0], 2)

    def test_other_scripts_leave_offloaded_copies_alone(self):
        self.offload("--apply")
        di = H.load_script("drive_inventory")
        self.assertIn(BURIAL, di.removed_copies(Path(self.root / "data"), "ARCH1"))
        fs = H.load_script("free_space")
        db = H.connect(self.config)
        self.assertEqual(fs.plan(db, "ARCH1", 0, {}), [])           # its two copies there are both offloaded

    def test_deleting_a_stub_drops_its_record(self):
        self.offload("--apply")
        lst = self.root / "list.txt"
        lst.write_text(f"ARCH1: {BURIAL_COPY}\n")
        r = H.run_script("delete_files", str(lst), "--yes", config=self.config)
        self.assertEqual(r.returncode, 0, r.stderr)
        db = H.connect(self.config)
        self.assertIsNone(db.execute("SELECT 1 FROM offloaded WHERE path=?", (BURIAL_COPY,)).fetchone())
        self.assertEqual(db.execute("SELECT copies FROM unique_files WHERE sha256=?", (PHOTO_SHA,)).fetchone()[0], 2)

    def test_search_app_shows_where_it_is(self):
        self.offload("--apply")
        from test_webapp import load_app
        c = load_app(self.config).app.test_client()
        page = c.get(f"/file/{PHOTO_SHA}").get_data(as_text=True)
        self.assertIn(f"s3://test-bucket/ARCH1/{BURIAL}", page)
        r = c.get(f"/original/{PHOTO_SHA}")
        self.assertEqual(r.status_code, 404)
        self.assertIn("restored", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
