"""jva/fm_numbers.py: plain numbers in the FileMaker CSVs' measurement columns."""
import csv
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from jva.fm_numbers import convert_column, fix_csv  # noqa: E402


def column(name, values):
    r = convert_column(name, values)
    return None if r is None else (r[0], r[1])


class Values(unittest.TestCase):
    def test_units_and_qualifiers(self):
        unit, new = column("WEIGHT", ["0.70 kg", ".41", "≈1.2 kg", ">2 kg", "1.8 kg w/dirt", "450 g", "---", "WHT", ""])
        self.assertEqual(unit, "kg")
        self.assertEqual(new, ["0.70", ".41", "1.2", "2", "1.8", "0.45", "", "", ""])

    def test_ranges_sums_and_slips(self):
        _, new = column("AMOUNT_PRESENT", ["100%", "<20%", "85-90%", "95% (rim fray missing)", "just base", "60%"])
        self.assertEqual(new, ["100", "20", "87.5", "95", "", "60"])
        _, new = column("Plain_Count", ["35 + 174", "12", "55 (historic)", "1.8 kg", "---"])
        self.assertEqual(new, ["209", "12", "55", "", ""])         # a weight in a count column: empty
        _, new = column("Rim_Weight", ["1.0 mg", "0.5 kg", "1.2 kg", ".3", "1..4", "12. 0 kg", "0.01k g"])
        self.assertEqual(new, ["1", "0.5", "1.2", ".3", "1.4", "12.0", "0.01"])   # 1.0 mg: a slip for kg
        _, new = column("Height", ["3.20cm (nc)", "26.8 cm ±.5 cm", "6 .50 cm", "1.6 0 cm", "45 mm", "2cm"])
        self.assertEqual(new, ["3.20", "26.8", "6.50", "1.60", "4.5", "2"])
        _, new = column("Age", ["30-39 years", "18 months", "40+ years", "Neonate", "? 13 years", "28 years"])
        self.assertEqual(new, ["34.5", "1.5", "40", "", "13", "28"])

    def test_left_alone(self):
        self.assertIsNone(column("Level", ["2-3", "4", "5/6", "7"]))             # identifiers aren't quantities
        self.assertIsNone(column("CATALOG", ["123A", "124", "125B"]))
        self.assertIsNone(column("Weight", ["12", ".5", "3.25"]))               # plain numbers already
        self.assertIsNone(column("Comments", ["broken", "2 pieces", "red slip"]))
        self.assertIsNotNone(column("Bag_Weight", ["1.2 kg", "3"]))             # a weight, though "bag"


class Files(unittest.TestCase):
    def test_fix_csv_twice(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "finds.csv"
            with open(f, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh, lineterminator="\r\n")
                w.writerows([["Lot", "Weight", "Note"], ["12a", "5g", "x"], ["13", "<2 g", "y"], ["14", "7", ""]])
            first = fix_csv(f)
            text = f.read_text(encoding="utf-8-sig")
            self.assertEqual([c[0] for c in first], ["Weight"])
            with open(f, newline="", encoding="utf-8-sig") as fh:
                rows = list(csv.reader(fh))
            self.assertEqual(rows, [["Lot", "Weight", "Weight_original", "Note"], ["12a", "5", "5g", "x"],
                                    ["13", "2", "<2 g", "y"], ["14", "7", "7", ""]])
            fix_csv(f)                                                          # again: the same file
            self.assertEqual(f.read_text(encoding="utf-8-sig"), text)

    def test_script(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.csv").write_text("Width\r\n2cm\r\n3 cm\r\n", encoding="utf-8-sig")
            r = subprocess.run([sys.executable, str(REPO / "scripts" / "fm_numbers.py"), "-d", d],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("1 CSV files with 1 measurement columns", r.stdout)
            with open(Path(d) / "_numbers.csv", newline="", encoding="utf-8-sig") as fh:
                rep = list(csv.DictReader(fh))
            self.assertEqual((rep[0]["column"], rep[0]["unit"], rep[0]["changed"]), ("Width", "cm", "2"))


if __name__ == "__main__":
    unittest.main()
