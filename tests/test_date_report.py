"""date_report.py: creation dates from the drive CSVs, and the spreadsheet."""
import csv
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers as H  # noqa: E402


@unittest.skipUnless(__import__("importlib").util.find_spec("openpyxl"), "needs openpyxl")
class DateReport(unittest.TestCase):
    def test_report(self):
        import openpyxl
        root, config = H.make_archive()
        db = H.connect(config)
        # copies on the archive drive are dated by the copying; ARCH2's drive CSV has the real date
        db.execute("UPDATE files SET created='2026-10-15 12:00:00'")
        db.commit()
        data = root / "data"
        with open(data / "archive_map.csv", "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["volume_uuid", "volume_name", "archive_volume", "archive_dir", "archive_name", "status", "updated"])
            w.writerow(["X", "JUDY", "ARCH2", "CumulativeArchive/JUDY", "JUDY", "copied", ""])
        with open(data / "JUDY.csv", "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["path", "name", "status", "created", "modified"])
            w.writerow(["NPW 1986/Burial 2.jpg", "Burial 2.jpg", "file", "1999-03-04 05:06:07", "2008-05-05 15:06:40"])
            w.writerow(["Museum displays/case 3.jpg", "case 3.jpg", "file", "2008-05-05 15:06:40", "2008-05-05 15:06:40"])
        out = root / "dates.xlsx"
        r = H.run_script("date_report", "-o", str(out), config=config)
        self.assertEqual(r.returncode, 0, r.stderr)
        ws = openpyxl.load_workbook(out).active
        rows = {row[0]: row for row in ws.iter_rows(min_row=6, values_only=True) if row[0]}
        self.assertEqual(rows["1999"][1], 1)          # all files: the copy dated by its source drive
        self.assertEqual((rows["2008"][1], rows["2008"][3], rows["2008"][4]), (1, 1, 0))   # created = modified
        self.assertEqual(rows["2026"][1], 8)          # the rest: the archive drive's date
        self.assertEqual(rows["1999"][7], 1)          # unique: Burial 2's earliest creation date
        self.assertEqual(rows["2026"][7], 6)
        self.assertTrue(str(rows["Total"][1]).startswith("=SUM("))


if __name__ == "__main__":
    unittest.main()
