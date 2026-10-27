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
        self.assertIn("1 distinct file", t)
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


class Public(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root, cls.config = H.make_archive(app={"mode": "public", "derivatives_url": "/derivatives/"})
        H.run_script("prepare_search", config=cls.config)
        cls.A = load_app(cls.config)
        cls.c = cls.A.app.test_client()
        cls.db = H.connect(cls.config)

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
        self.assertIn(self.c.post("/delete", data={"uid": 1}).status_code, (403, 404, 405))
        self.assertIn(self.c.post("/reveal", data={"id": 1}).status_code, (403, 404, 405))


if __name__ == "__main__":
    unittest.main()
