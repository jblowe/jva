"""jva/similar.py: the second-level grouping rules."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jva import similar as S  # noqa: E402


class Rules(unittest.TestCase):
    def test_made_up_names_are_not_grouped(self):
        for n in ["IMG_0001.JPG", "DSC_0090.JPG", "DSCN2661.JPG", "P6180041.JPG", "001.jpg", "Image3.TIF",
                  "Untitled 3.jpg", "index.html", "sld003.htm", "VTS_01_1.VOB", "2003-01-22-0026.JPG"]:
            self.assertIsNotNone(S.generic(n), n)
            self.assertIsNone(S.pass1_key(n), n)
        for n in ["10091.JPG", "NKH Stone Tools.xls", "Burial 2.jpg"]:
            self.assertIsNone(S.generic(n), n)

    def test_object_numbers(self):
        for n, key in [("10091.JPG", "10091"), ("10091 another 3.JPG", "10091"), ("39903.4.JPG", "39903.4"),
                       ("20952 NML Stone Adze.jpg", "20952"), ("2006-12 side view.tif", "2006-12")]:
            self.assertEqual(S.pass2_key(n, "x")[1], key, n)
        self.assertEqual(S.pass2_key("DSC_0575.JPG", "16115 NPW Done Cloud")[1], "16115")
        self.assertIsNone(S.pass2_key("DSCN2537.JPG", "2004 PL TAP 1")[1])          # a year, not an object
        self.assertNotEqual(S.pass2_key("1994 NML page 4.jpg", "x")[1], "1994")
        self.assertIsNone(S.pass2_key("10091.doc", "x")[1])                         # images only, for now

    def test_group(self):
        items = [(1, "10091.JPG", "10091 NPW", "a/10091 NPW/10091.JPG"),
                 (2, "10091 another 3.JPG", "b", "b/10091 another 3.JPG"),
                 (3, "DSC_0575.JPG", "10091 NPW", "a/10091 NPW/DSC_0575.JPG"),
                 (4, "DSC_0575.JPG", "c", "c/DSC_0575.JPG"),
                 (5, "notes.doc", "a", "a/notes.doc"), (6, "notes.doc", "b", "b/notes.doc"),
                 (7, "sld001.htm", "x_files", "deck_files/sld001.htm"), (8, "sld001.htm", "y_files", "y_files/sld001.htm")]
        res, labels = S.group(items)
        self.assertEqual({res[i][0] for i in (1, 2, 3)}, {1})
        self.assertEqual(labels[1], "10091")
        self.assertEqual(res[4][0], 4)                       # camera name elsewhere: on its own
        self.assertEqual((res[5][0], res[6][0], res[6][1]), (5, 5, "same name"))
        self.assertNotEqual(res[7][0], res[8][0])            # parts of saved web pages


if __name__ == "__main__":
    unittest.main()
