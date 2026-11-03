#!/usr/bin/env python3
"""
fm_numbers.py -- plain numbers in the measurement columns of the FileMaker CSVs.

    python3 scripts/fm_numbers.py              # the CSVs in ~/jva_fm
    python3 scripts/fm_numbers.py -d ~/Desktop/fm

Values such as "60%", "2.5 cm", "≈12 cm", ">4.42", "80-85%" or "3.20 cm (nc)" become 60, 2.5,
12, 4.42, 82.5 and 3.20; what was written is kept in a new column <column>_original beside
each column changed. filemaker_to_csv.py does this itself as it writes the CSVs; this script
does it to CSVs already written (it is safe to run again). The rules are in jva/fm_numbers.py.

Two reports are written beside the CSVs: _numbers.csv (each column changed, its unit, how many
values changed and how many were placeholders or other text left empty) and _numbers_values.csv (each distinct value
changed, what it became and why), for checking.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # the repository, for jva
from jva.fm_numbers import fix_csv, summary, write_reports  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-d", "--dir", default="~/jva_fm", help="the folder of CSVs (default ~/jva_fm)")
    a = ap.parse_args()
    folder = Path(a.dir).expanduser()
    results = {}
    for f in sorted(folder.glob("*.csv"), key=lambda p: p.name.lower()):
        if f.name.startswith("_"):
            continue
        r = fix_csv(f)
        if r:
            results[f.name] = r
    write_reports(folder, results)
    print(f"{summary(results)}; see _numbers.csv and _numbers_values.csv in {folder}")


if __name__ == "__main__":
    main()
