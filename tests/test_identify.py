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


if __name__ == "__main__":
    unittest.main()
