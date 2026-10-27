"""Deleting files (and keeping the catalogue right), and finding and repairing damaged TIFFs."""
import csv
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers as H  # noqa: E402


class Delete(unittest.TestCase):
    def setUp(self):
        self.root, self.config = H.make_archive()
        H.run_script("prepare_search", config=self.config)
        self.vols = self.root / "Volumes"

    def delete(self, *lines, yes=True):
        lst = self.root / "list.txt"
        lst.write_text("\n".join(lines) + "\n")
        return H.run_script("delete_files", str(lst), "--reason", "test", *(["--yes"] if yes else []),
                            config=self.config, input_text="no\n")

    def test_one_copy(self):
        r = self.delete("ARCH1: CumulativeArchive/THAI 2008/copies/Burial 2 copy.jpg")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.vols / "ARCH1/CumulativeArchive/THAI 2008/copies").exists())   # emptied folder
        db = H.connect(self.config)
        self.assertEqual(db.execute("SELECT copies FROM unique_files WHERE name='Burial 2.jpg'").fetchone()[0], 2)
        db.execute("INSERT INTO unique_search(unique_search) VALUES ('integrity-check')")
        self.assertEqual(db.execute("SELECT copies_left, reason FROM deleted_files").fetchall(), [(2, "test")])
        meta = dict(db.execute("SELECT key, value FROM search_meta"))
        self.assertEqual(meta["unique_count"], "8")                       # still counts as prepared

    def test_folder_with_companions_and_last_copy(self):
        r = self.delete(str(self.vols / "ARCH1/CumulativeArchive/JUDY/NPW 1986"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.vols / "ARCH1/CumulativeArchive/JUDY/NPW 1986").exists())   # ._ and .DS_Store too
        db = H.connect(self.config)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM files WHERE path LIKE '%NPW 1986%' AND archive='ARCH1'")
                         .fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT copies FROM unique_files WHERE name='Burial 2.jpg'").fetchone()[0], 2)

    def test_last_copy_leaves_catalogue(self):
        r = self.delete("ARCH1: CumulativeArchive/JUDY/Courses/ANT 318/lecture.ppt")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("LAST copy", r.stdout)
        db = H.connect(self.config)
        self.assertIsNone(db.execute("SELECT 1 FROM unique_files WHERE name='lecture.ppt'").fetchone())
        self.assertEqual(db.execute("SELECT COUNT(*) FROM unique_files").fetchone()[0],
                         db.execute("SELECT COUNT(*) FROM unique_search").fetchone()[0])

    def test_declining_changes_nothing(self):
        r = self.delete("ARCH1: CumulativeArchive/JUDY/Courses/ANT 318/lecture.ppt", yes=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue((self.vols / "ARCH1/CumulativeArchive/JUDY/Courses/ANT 318/lecture.ppt").exists())

    def test_redo_skips_deleted(self):
        self.delete("ARCH1: CumulativeArchive/JUDY/Courses/ANT 318/lecture.ppt")
        di = H.load_script("drive_inventory")
        self.assertIn("CumulativeArchive/JUDY/Courses/ANT 318/lecture.ppt",
                      di.removed_copies(H.Path(self.root / "data"), "ARCH1"))


class Tiffs(unittest.TestCase):
    def setUp(self):
        self.root, self.config = H.make_archive()
        self.vols = self.root / "Volumes"
        self.nml = self.vols / "ARCH1/CumulativeArchive/THAI 2008/TAP 94 NML"

    def test_check_one_file(self):
        self.assertEqual(H.run_script("check_tiffs", "--file", str(self.nml / "sherd.tif"), config=self.config)
                         .returncode, 0)
        r = H.run_script("check_tiffs", "--file", str(self.nml / "bad exif.tif"), config=self.config)
        self.assertIn("bad Exif pointer", r.stdout)

    def test_check_repair_undo(self):
        before = {p.name: p.read_bytes() for p in self.nml.iterdir()}
        r = H.run_script("check_tiffs", config=self.config)
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = list(csv.DictReader(open(self.root / "data/tiff_check.csv", encoding="utf-8-sig")))
        self.assertEqual(sorted(Path(x["path"]).name for x in rows), ["bad exif.tif", "bad pixels.tif"])
        r = H.run_script("repair_tiffs", "--yes", config=self.config)
        self.assertEqual(r.returncode, 0, r.stderr)
        for n in ("bad exif.tif", "bad pixels.tif"):
            self.assertEqual(H.run_script("check_tiffs", "--file", str(self.nml / n), config=self.config).returncode, 0)
            self.assertEqual(len(before[n]), len((self.nml / n).read_bytes()))
        db = H.connect(self.config)
        for path, sha in db.execute("SELECT path, sha256 FROM files WHERE name LIKE 'bad %'"):
            self.assertEqual(sha, H.hashlib.sha256((self.vols / "ARCH1" / path).read_bytes()).hexdigest())
        self.assertEqual(db.execute("SELECT COUNT(*) FROM unique_files u JOIN files f ON f.unique_id = u.id "
                                    "WHERE f.sha256 <> u.sha256").fetchone()[0], 0)
        r = H.run_script("repair_tiffs", "--undo", "--yes", config=self.config)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual({p.name: p.read_bytes() for p in self.nml.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
