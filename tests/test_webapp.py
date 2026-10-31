"""The search app, in local mode (the Mac) and public mode (the web server)."""
import importlib
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers as H  # noqa: E402


def load_app(config):
    os.environ["JVA_CONFIG"] = str(config)
    os.environ["JVA_NO_WARM"] = "1"
    import jva.config
    importlib.reload(jva.config)
    sys.path.insert(0, str(H.REPO / "webapp"))
    import app
    return importlib.reload(app)


class Local(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root, cls.config = H.make_archive()
        H.run_script("prepare_search", config=cls.config)
        cls.A = load_app(cls.config)
        cls.c = cls.A.app.test_client()
        cls.db = H.connect(cls.config)

    def key(self, name):
        return self.db.execute("SELECT sha256 FROM unique_files WHERE name=?", (name,)).fetchone()[0]

    def test_pages(self):
        for url in ["/", "/search", "/search?view=list", "/search?q=burial", "/search?site=npw",
                    "/search?view=list&sort=season&dir=desc", "/browse", "/browse?a=ARCH1",
                    f"/file/{self.key('Burial 2.jpg')}"]:
            self.assertEqual(self.c.get(url).status_code, 200, url)
        self.assertEqual(self.c.get("/file/nope").status_code, 404)

    def test_search_and_facets(self):
        t = self.c.get("/search?q=burial").get_data(as_text=True)
        self.assertIn("1 result", t)
        t = self.c.get("/search").get_data(as_text=True)
        self.assertIn('name="site" value="npw"', t)

    def test_sorting(self):
        t = self.c.get("/search?view=list&sort=name").get_data(as_text=True)
        names = re.findall(r'<td><a href="/file/[^"]+">([^<]+)</a>', t)
        self.assertEqual(names, sorted(names, key=str.lower))

    def test_local_tools(self):
        t = self.c.get(f"/file/{self.key('Burial 2.jpg')}").get_data(as_text=True)
        self.assertIn("Reveal in Finder", t)
        self.assertIn('class="delete"', t)
        r = self.c.get(f"/original/{self.key('notes.txt')}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"field notes", r.data)


class Versions(unittest.TestCase):
    """Similar files (jva/similar.py) shown together, and on a file's page."""

    @staticmethod
    def archive():
        root, config = H.make_archive()
        d = root / "Volumes" / "ARCH1" / "CumulativeArchive" / "THAI 2008"
        for rel, data in [("10091 NPW/10091.JPG", b"\xff\xd8 front" * 30),
                          ("10091 NPW/10091 another 3.JPG", b"\xff\xd8 back" * 30),
                          ("10091 NPW/DSC_0575.JPG", b"\xff\xd8 detail" * 30),
                          ("old/10091.JPG", b"\xff\xd8 front, older edit" * 30)]:
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_bytes(data)
        r = H.run_script("identify_archive", "-a", "ARCH1", "--skip-identify", config=config)
        assert r.returncode == 0, r.stderr
        return root, config

    @classmethod
    def setUpClass(cls):
        cls.root, cls.config = cls.archive()
        cls.A = load_app(cls.config)
        cls.c = cls.A.app.test_client()
        cls.db = H.connect(cls.config)

    def test_grouped_in_the_catalogue(self):
        rows = self.db.execute("SELECT name, group_how, group_key FROM unique_files WHERE group_key = '10091' "
                               "ORDER BY name").fetchall()
        self.assertEqual(len(rows), 4)
        self.assertEqual(self.db.execute("SELECT label, members FROM file_groups").fetchall(), [("10091", 4)])

    def test_search_together_and_apart(self):
        t = self.c.get("/search?q=10091").get_data(as_text=True)
        self.assertIn("1 result", t)
        self.assertIn("4 versions", t)
        t = self.c.get("/search?q=10091&similar=apart").get_data(as_text=True)
        self.assertIn("4 distinct files", t)          # DSC_0575 matches through its folder
        all_together = self.c.get("/search").get_data(as_text=True)
        all_apart = self.c.get("/search?similar=apart").get_data(as_text=True)
        self.assertIn("9 results", all_together)      # 8 + 4 versions - 3
        self.assertIn("12 distinct files", all_apart)

    def test_file_page(self):
        sha = self.db.execute("SELECT sha256 FROM unique_files WHERE path LIKE '%10091 NPW/10091.JPG'").fetchone()[0]
        t = self.c.get(f"/file/{sha}").get_data(as_text=True)
        self.assertIn("Other versions", t)
        self.assertEqual(t.count('class="rel-same"'), 1)        # old/10091.JPG: same name
        self.assertEqual(t.count('class="rel-similar"'), 2)     # another 3, DSC_0575
        self.assertIn("object folder", t)

    def test_deleting_the_main_file_keeps_the_group(self):
        from jva import deletion as D
        root, config = self.archive()                 # its own copy: this test deletes files
        con = H.connect(config)
        main = con.execute("SELECT group_id FROM file_groups").fetchone()[0]
        D.delete_unique(con, main, "test", "test", str(root / "Volumes"))
        con.commit()
        g = con.execute("SELECT group_id, members FROM file_groups").fetchone()
        self.assertNotEqual(g[0], main)
        self.assertEqual(g[1], 3)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM unique_files WHERE group_id = ?", (g[0],)).fetchone()[0], 3)


class Public(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root, cls.config = H.make_archive(app={"mode": "public", "derivatives_url": "/derivatives/"})
        H.run_script("prepare_search", config=cls.config)
        cls.db = H.connect(cls.config)
        sha = cls.db.execute("SELECT sha256 FROM unique_files WHERE name='Burial 2.jpg'").fetchone()[0]
        cls.db.executescript(__import__("jva.catalog", fromlist=["x"]).DERIVATIVES)
        cls.db.execute("INSERT INTO derivatives(sha256, slug, kind, status, root, thumb, preview) "
                       "VALUES (?, 'burial-2', 'image', 'made', '/nowhere', ?, ?)",
                       (sha, f"thumb/{sha[:2]}/burial 2.webp", f"preview/{sha[:2]}/burial 2.webp"))
        cls.db.commit()
        cls.A = load_app(cls.config)
        cls.c = cls.A.app.test_client()

    def test_direct_image_links(self):
        sha = self.db.execute("SELECT sha256 FROM unique_files WHERE name='Burial 2.jpg'").fetchone()[0]
        t = self.c.get("/search").get_data(as_text=True)
        self.assertIn(f'src="/derivatives/thumb/{sha[:2]}/burial%202.webp"', t)
        self.assertIn("/static/placeholder/presentation.svg", t)    # files without previews: by sort
        self.assertIn("/static/placeholder/text.svg", t)
        self.assertNotIn("/img/", t)
        t = self.c.get(f"/file/{sha}").get_data(as_text=True)
        self.assertIn(f"/derivatives/preview/{sha[:2]}/burial%202.webp", t)

    def test_pages(self):
        k = self.db.execute("SELECT sha256 FROM unique_files WHERE name='Burial 2.jpg'").fetchone()[0]
        for url in ["/", "/search", "/search?view=list", "/browse", f"/file/{k}"]:
            self.assertEqual(self.c.get(url).status_code, 200, url)
        t = self.c.get(f"/file/{k}").get_data(as_text=True)
        self.assertNotIn("Reveal in Finder", t)
        self.assertNotIn('class="delete"', t)

    def test_local_routes_gone(self):
        k = self.db.execute("SELECT sha256 FROM unique_files WHERE name='notes.txt'").fetchone()[0]
        self.assertEqual(self.c.get(f"/original/{k}").status_code, 404)
        self.assertEqual(self.c.post("/delete", data={"uid": 1}).status_code, 404)
        self.assertEqual(self.c.post("/reveal", data={"id": 1}).status_code, 404)
        self.assertEqual(H.connect(self.config).execute("SELECT COUNT(*) FROM files").fetchone()[0], 12)


if __name__ == "__main__":
    unittest.main()
