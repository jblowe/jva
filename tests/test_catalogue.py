"""Building the catalogue's distinct-file table, classifying, and preparing for search."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers as H  # noqa: E402


def setUpModule():
    global ROOT, CONFIG, DB
    ROOT, CONFIG = H.make_archive()
    cfg = json.loads(CONFIG.read_text())
    cfg["rules"] = str(Path(__file__).resolve().parent / "rules_fixture.csv")
    CONFIG.write_text(json.dumps(cfg))
    r = H.run_script("classify_archive", config=CONFIG)
    assert r.returncode == 0, r.stderr
    r = H.run_script("prepare_search", config=CONFIG)
    assert r.returncode == 0, r.stderr
    DB = H.connect(CONFIG)


class Unique(unittest.TestCase):
    def test_distinct_files_and_copies(self):
        n, copies = DB.execute("SELECT COUNT(*), SUM(copies) FROM unique_files").fetchone()
        self.assertEqual((n, copies), (8, 10))            # 12 files, 2 of them clutter
        c, vols, locs = DB.execute("SELECT copies, volumes, locations FROM unique_files "
                                   "WHERE name = 'Burial 2.jpg'").fetchone()
        self.assertEqual((c, vols), (3, 2))
        self.assertIn("ARCH2: CumulativeArchive/JUDY/NPW 1986/Burial 2.jpg", locs)

    def test_clutter_left_out(self):
        names = {r[0] for r in DB.execute("SELECT name FROM unique_files")}
        self.assertFalse(any(n.startswith(".") for n in names))
        self.assertEqual(DB.execute("SELECT COUNT(*) FROM files WHERE junk = 1 AND unique_id IS NOT NULL")
                         .fetchone()[0], 0)

    def test_full_text_search(self):
        hits = DB.execute("SELECT u.name FROM unique_search s JOIN unique_files u ON u.id = s.rowid "
                          "WHERE unique_search MATCH 'burial*'").fetchall()
        self.assertEqual([h[0] for h in hits], ["Burial 2.jpg"])


class Classification(unittest.TestCase):
    def cat(self, name):
        return DB.execute("SELECT category FROM unique_files WHERE name = ?", (name,)).fetchone()[0]

    def test_categories(self):
        self.assertEqual(self.cat("Burial 2.jpg"), "TAP")                 # NPW
        self.assertEqual(self.cat("sherd.tif"), "TAP")                    # TAP 94 NML
        self.assertEqual(self.cat("lecture.ppt"), "Class")                # ANT 318 beats Courses
        self.assertEqual(self.cat("case 3.jpg"), "Museum")                # Museum displays
        self.assertEqual(self.cat("notes.txt"), "TAP")                    # THAI 2008 drive label, last resort

    def test_nottap_place_folder(self):
        self.assertEqual(self.cat("img1.jpg"), "SE Asian Archaeology")    # JUDY/World Trip/China 1

    def test_classify_function(self):
        C = H.load_script("classify_archive")
        rules = C.load_rules(Path(__file__).resolve().parent / "rules_fixture.csv")
        cases = {"Slide Scans/TAP/World Trip/China 1/x.jpg": "SE Asian Archaeology",
                 "TAP Images/China/TAP NPW comparisons/a.tif": "TAP",
                 "TAP Export Records/TAP94.export.china1": "TAP",
                 "Courses/ANT 318/TAP slides/x.jpg": "TAP"}
        for path, want in cases.items():
            self.assertEqual(C.classify(path.split("/"), rules)[0], want, path)


class SearchPreparation(unittest.TestCase):
    def test_kinds_sites_seasons(self):
        kind = dict(DB.execute("SELECT name, kind FROM unique_files"))
        self.assertEqual(kind["Burial 2.jpg"], "Image")
        self.assertEqual(kind["lecture.ppt"], "Presentation")
        tags = DB.execute("SELECT u.name, t.facet, t.value FROM unique_tags t JOIN unique_files u ON u.id = t.unique_id "
                          "ORDER BY 1, 2, 3").fetchall()
        self.assertIn(("Burial 2.jpg", "site", "npw"), tags)
        self.assertIn(("sherd.tif", "season", "94"), tags)
        self.assertIn(("sherd.tif", "site", "nml"), tags)

    def test_meta_matches(self):
        meta = dict(DB.execute("SELECT key, value FROM search_meta"))
        self.assertEqual(meta["unique_count"], "8")

    def test_kinds_module(self):
        from jva.kinds import kind_of, tags_of
        self.assertEqual(kind_of("", "", "TEXT", "ART5"), "Image")         # Illustrator PostScript
        self.assertEqual(kind_of(".pdf", "", "", ""), "PDF")
        self.assertEqual(kind_of("", "", "WDBN", "MSWD"), "MS Word")
        self.assertEqual(sorted(tags_of("NPW86 sherds/TAP94.export")),
                         [("season", "86"), ("season", "94"), ("site", "npw")])

    def test_placeholders(self):
        from jva.kinds import PLACEHOLDERS, placeholder_for as pf
        self.assertEqual(pf(".fp5", "", ""), "filemaker")
        self.assertEqual(pf("", "", "FMPR", "FMPR"), "filemaker")                   # old Mac, no extension
        self.assertEqual(pf("", "", "", "", "FileMaker Pro Database"), "filemaker")
        self.assertEqual(pf(".mdb", "", ""), "database")
        self.assertEqual(pf(".xls", "", ""), "spreadsheet")
        self.assertEqual(pf("", "", "WDBN", "MSWD"), "word")
        self.assertEqual(pf(".ppt", "", ""), "presentation")
        self.assertEqual(pf("", "", "drw5", "DAD5"), "drawing")                      # Canvas
        self.assertEqual(pf("", "", "TEXT", "ART5"), "image")                        # Illustrator PostScript
        self.assertEqual(pf(".pmd", "", ""), "layout")
        self.assertEqual(pf("", "", "OEDB", "MSNM"), "email")
        self.assertEqual(pf(".zip", "", ""), "archive")
        self.assertEqual(pf(".plist", "", ""), "settings")
        self.assertEqual(pf(".js", "", ""), "web")
        self.assertEqual(pf(".xyz", "", ""), "unknown")
        here = Path(__file__).resolve().parents[1] / "webapp" / "static" / "placeholder"
        self.assertEqual([n for n in PLACEHOLDERS if not (here / f"{n}.svg").exists()], [])


if __name__ == "__main__":
    unittest.main()
