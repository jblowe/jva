"""copy_originals.py: one checked copy of each distinct document in the derivatives folder."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers as H  # noqa: E402


class CopyOriginals(unittest.TestCase):
    def setUp(self):
        self.root, self.config = H.make_archive()
        r = H.run_script("prepare_search", config=self.config)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.deriv = self.root / "derivatives"
        self.deriv.mkdir()

    def run_copy(self, *args):
        r = H.run_script("copy_originals", "-d", str(self.deriv), "--kinds", "Text,Presentation,Image", *args,
                         config=self.config)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_copies(self):
        db = H.connect(self.config)
        n = db.execute("SELECT COUNT(*) FROM unique_files WHERE kind IN ('Text', 'Presentation', 'Image')").fetchone()[0]
        out = self.run_copy("--dry-run")
        self.assertIn(f"would copy {n} files", out)
        self.assertFalse((self.deriv / "originals").exists())
        vols = self.root / "Volumes"
        # the catalogue's own copy has changed: another copy of the same file is read instead,
        # and still put at the catalogue's path
        burial = db.execute("SELECT archive, path FROM unique_files WHERE name = 'Burial 2.jpg'").fetchone()
        src = vols / burial[0] / burial[1]
        good = src.read_bytes()
        src.write_bytes(good[::-1])
        out = self.run_copy()
        self.assertIn(f"copied {n} files", out)
        rows = dict(db.execute("SELECT rel, size FROM originals"))
        self.assertEqual(len(rows), n)
        for rel, size in rows.items():
            self.assertTrue(rel.startswith("originals/ARCH"), rel)
            self.assertEqual((self.deriv / rel).stat().st_size, size)
        self.assertEqual((self.deriv / "originals" / burial[0] / burial[1]).read_bytes(), good)
        notes = self.deriv / "originals/ARCH1/CumulativeArchive/THAI 2008/misc/notes.txt"
        self.assertEqual(notes.stat().st_mtime, 1_210_000_000)          # the date is kept
        self.assertIn(f"{n} were already there", self.run_copy())         # nothing done again
        # a file with no good copy anywhere is listed, not copied
        (vols / "ARCH1" / "CumulativeArchive/THAI 2008/misc/notes.txt").write_text("changed")
        notes.unlink()
        out = self.run_copy()
        self.assertIn("1 not copied", out)
        self.assertFalse(notes.exists())
        self.assertEqual(list(self.deriv.rglob("*.copying")), [])


if __name__ == "__main__":
    unittest.main()
