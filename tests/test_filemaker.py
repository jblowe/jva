"""filemaker_to_csv.py: FileMaker databases to CSV (needs a C compiler and SQLite's headers)."""
import csv
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers as H  # noqa: E402

SAMPLE = Path(__file__).resolve().parent / "data" / "governmenttypes.FP3"


def can_build():
    """A C compiler and SQLite's header file (sqlite3.h; on a Mac they come with the command-line tools)."""
    cc = shutil.which("cc") or shutil.which("gcc")
    if not cc:
        return False
    r = subprocess.run([cc, "-x", "c", "-fsyntax-only", "-"], input="#include <sqlite3.h>\n",
                       capture_output=True, text=True)
    return r.returncode == 0


@unittest.skipUnless(can_build(), "needs a C compiler and SQLite's headers")
class FileMakerToCSV(unittest.TestCase):
    def test_export(self):
        root, config = H.make_archive()
        d = root / "Volumes" / "ARCH1" / "CumulativeArchive" / "DATABASES"
        d.mkdir(parents=True)
        shutil.copyfile(SAMPLE, d / "Government Types.fp3")
        (d / "old one.fp3").write_bytes(b"\x00\x01\x00\x00\x00\x09 not a FileMaker 3 file" * 50)
        (d / "notes.doc").write_bytes(b"\xd0\xcf\x11\xe0 not a database" * 20)
        r = H.run_script("identify_archive", "-a", "ARCH1", "--skip-identify", config=config)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = root / "fm"
        (out).mkdir()
        (out / "mine.txt").write_text("keep me")
        for _ in range(2):                                  # a second run replaces the first
            r = H.run_script("filemaker_to_csv", "-o", str(out), config=config)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("from 1 of 2 FileMaker databases", r.stdout)
        rows = list(csv.reader(open(out / "Government Types.csv", encoding="utf-8-sig", newline="")))
        self.assertGreater(len(rows), 1)
        self.assertEqual(len({len(x) for x in rows}), 1)  # every record has every field
        index = list(csv.DictReader(open(out / "_index.csv", encoding="utf-8-sig", newline="")))
        self.assertEqual([x["database"] for x in index], ["Government Types.fp3", "old one.fp3"])
        self.assertEqual(index[0]["csv_file"], "Government Types.csv")
        self.assertIn("FileMaker Pro 1 or 2", index[1]["note"])
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["Government Types.csv", "_index.csv", "mine.txt"])


if __name__ == "__main__":
    unittest.main()
