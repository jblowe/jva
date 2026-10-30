"""identify_archive.py: trying unreadable files again, and stopping when the drive goes away."""
import shutil
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers as H  # noqa: E402


class Retry(unittest.TestCase):
    def setUp(self):
        self.root, self.config = H.make_archive()
        self.db = H.connect(self.config)

    def test_unreadable_files_are_tried_again(self):
        self.db.execute("UPDATE files SET sha256=NULL, hash_error='No such file or directory' "
                        "WHERE name='sherd.tif'")
        self.db.commit()
        r = H.run_script("identify_archive", "-a", "ARCH1", "--skip-identify", config=self.config)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("trying again 1 files", r.stderr)
        sha, err = self.db.execute("SELECT sha256, hash_error FROM files WHERE name='sherd.tif'").fetchone()
        self.assertTrue(sha)
        self.assertIsNone(err)
        # and the distinct file is one entry with its checksum, not an "unreadable" one
        self.assertEqual(self.db.execute("SELECT checksum FROM unique_files WHERE name='sherd.tif'").fetchall(),
                         [("checksummed",)])

    def test_drive_gone_records_nothing(self):
        IA = H.load_script("identify_archive")
        self.db.execute("UPDATE files SET sha256=NULL WHERE archive='ARCH1'")
        self.db.commit()
        drive = self.root / "Volumes" / "ARCH1"
        shutil.move(drive, self.root / "unplugged")            # the drive drops out
        with self.assertRaises(IA.DriveGone):
            IA.hash_files(self.db, "ARCH1", drive)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM files WHERE hash_error IS NOT NULL")
                         .fetchone()[0], 0)

    def test_drive_gone_during_identify(self):
        IA = H.load_script("identify_archive")
        drive = self.root / "Volumes" / "ARCH1"

        def fake_sf(sf, root, rows):                # Siegfried can't find anything
            shutil.move(drive, self.root / "unplugged")
            return {fid: ("", "", "", "", "", "", "siegfried", f"[FATAL] file access error for {rel}")
                    for fid, rel in rows}
        IA._run_sf = fake_sf
        with mock.patch.object(IA.shutil, "which", return_value="/usr/local/bin/sf"), \
                self.assertRaises(IA.DriveGone):
            IA.identify(self.db, "ARCH1", drive)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM files WHERE id_error IS NOT NULL")
                         .fetchone()[0], 0)

    def test_access_errors_are_tried_again(self):
        IA = H.load_script("identify_archive")
        self.db.execute("UPDATE files SET id_tool='siegfried', id_error='[FATAL] file access error for x' "
                        "WHERE archive='ARCH1'")
        self.db.commit()
        IA._run_sf = lambda sf, root, rows: {fid: ("fmt/43", "JPEG", "", "image/jpeg", "", "", "siegfried", None)
                                             for fid, _ in rows}
        with mock.patch.object(IA.shutil, "which", return_value="/usr/local/bin/sf"):
            IA.identify(self.db, "ARCH1", self.root / "Volumes" / "ARCH1")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM files WHERE archive='ARCH1' AND id_error IS NOT NULL")
                         .fetchone()[0], 0)
        self.assertGreater(self.db.execute("SELECT COUNT(*) FROM files WHERE puid='fmt/43'").fetchone()[0], 0)


class SystemFolder(unittest.TestCase):
    def test_system_folder_is_clutter(self):
        from jva.catalog import is_clutter
        self.assertTrue(is_clutter("Finder", 10, "Bill Fitts/System Drive/System Folder/Finder"))
        self.assertTrue(is_clutter("x.pdf", 10, "a/System Folder 9.2/Extensions/x.pdf"))
        self.assertFalse(is_clutter("notes.doc", 10, "a/System Drive/Documents/notes.doc"))
        self.assertFalse(is_clutter("x.doc", 10, "a/Filing System Folders/x.doc"))
        self.assertTrue(is_clutter("01 Highway Blues.M4A", 10, "My Music/01 Highway Blues.M4A"))
        self.assertTrue(is_clutter("settings.XML", 10, "a/settings.XML"))
        self.assertTrue(is_clutter("sld012.htm", 10, "a/talk_files/sld012.htm"))
        root, config = H.make_archive()
        sysf = root / "Volumes" / "ARCH1" / "CumulativeArchive" / "OLD MAC" / "System Folder" / "Extensions"
        sysf.mkdir(parents=True)
        (sysf / "QuickTime").write_bytes(b"not content" * 10)
        talk = root / "Volumes" / "ARCH1" / "CumulativeArchive" / "LECTURES" / "talk_files"
        talk.mkdir(parents=True)
        for n in ("sld001.htm", "tsld001.htm", "master03.htm", "img001.jpg"):
            (talk / n).write_bytes(n.encode() * 20)
        (talk.parent / "notes.htm").write_bytes(b"<p>my own page</p>" * 5)
        db = H.connect(config)
        # a row catalogued before the rule existed
        db.execute("INSERT INTO files(archive, unit, drive, path, name, ext, size, mtime, junk) VALUES "
                   "('ARCH2', 'CumulativeArchive', 'X', 'CumulativeArchive/X/System Folder/Finder', 'Finder', '', 9, 0, 0)")
        db.execute("INSERT INTO files(archive, unit, drive, path, name, ext, size, mtime, junk) VALUES "
                   "('ARCH2', 'CumulativeArchive', 'X', 'CumulativeArchive/X/song.m4a', 'song.m4a', '.m4a', 9, 0, 0)")
        db.commit()
        r = H.run_script("identify_archive", "-a", "ARCH1", "--skip-identify", config=config)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("System Folders now count as clutter", r.stderr)
        self.assertIn("1 files of clutter types (.m4a, .xml)", r.stderr)
        self.assertIn("2 files in PowerPoint web exports", r.stderr)     # the slide pages themselves at the scan
        rows = db.execute("SELECT name, junk FROM files WHERE path LIKE '%talk_files/%' ORDER BY name").fetchall()
        self.assertEqual(rows, [("img001.jpg", 1), ("master03.htm", 1), ("sld001.htm", 1), ("tsld001.htm", 1)])
        self.assertEqual(db.execute("SELECT junk FROM files WHERE name = 'notes.htm'").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT junk FROM files WHERE name = 'song.m4a'").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT name, junk, unique_id FROM files WHERE path LIKE '%System Folder%' "
                                    "ORDER BY name").fetchall(), [("Finder", 1, None), ("QuickTime", 1, None)])


if __name__ == "__main__":
    unittest.main()
