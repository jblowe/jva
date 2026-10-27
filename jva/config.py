"""Settings shared by every script and the web app.

They come from one JSON file: $JVA_CONFIG if set, otherwise config.json at the top of the
repository (not in git; copy config.example.json). Every key is optional:

  data_dir         the folder for the catalogue, the reports (CSV) and the drive records
                   (archive_map.csv, drives.csv, one CSV per source drive). On the Mac:
                   /Users/johnlowe/Claude/Projects/Voelker. Default: data/ in the repository
  catalogue        the catalogue file. Default: <data_dir>/archive_catalog.sqlite
  rules            the classification rules. Default: rules/classify_rules.csv here
  volumes_root     where the archive drives are mounted (/Volumes)
  derivatives_dir  the preview-image folder (make_derivatives.py's -d, when not given)
  app              the web app's settings (see webapp/app.py and docs/search-app.md)
  publish          what publish.py publishes and where to (see docs/deployment.md)
"""
import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

APP_DEFAULTS = {
    "mode": "local",             # "local": the Mac with the archive drives; "public": a web server
    "derivatives_roots": [],     # where to find preview images, in order (local mode)
    "derivatives_url": "",       # public mode: URL prefix the web server serves the images at
    "hide_categories": [],
    "host": "127.0.0.1",
    "port": 5000,
    "title": "Voelker Archive",
    "page_size": 60,
    "warm_cache": True,
    "serve_originals": None,     # None: yes in local mode, no in public mode
    "allow_delete": None,        # None: yes in local mode; never in public mode
}

PUBLISH_DEFAULTS = {
    "exclude_categories": ["Class", "Unclassified"],
    "out_dir": None,             # default <data_dir>/public
    "server": "",                # e.g. "ubuntu@jva.johnblowe.com" (ssh destination for rsync)
    "remote_data_dir": "/mnt/images/jva_data",       # the public catalogue goes here
    "remote_images_dir": "/mnt/images/jva_images",   # the derivatives (images, PDF/RTF) go here
}


class Config:
    def __init__(self, values, source=None):
        self.source = source
        v = dict(values)
        self.data_dir = Path(os.path.expanduser(v.get("data_dir") or REPO / "data"))
        self.catalogue = Path(os.path.expanduser(v.get("catalogue") or self.data_dir / "archive_catalog.sqlite"))
        self.rules = Path(os.path.expanduser(v.get("rules") or REPO / "rules" / "classify_rules.csv"))
        self.volumes_root = v.get("volumes_root") or "/Volumes"
        d = v.get("derivatives_dir")
        self.derivatives_dir = Path(os.path.expanduser(d)) if d else None
        self.app = {**APP_DEFAULTS, **(v.get("app") or {})}
        self.publish = {**PUBLISH_DEFAULTS, **(v.get("publish") or {})}
        if not self.publish["out_dir"]:
            self.publish["out_dir"] = str(self.data_dir / "public")
        self.raw = v

    def __repr__(self):
        return f"<Config {self.source or '(defaults)'}: data {self.data_dir}>"


def load(path=None):
    p = Path(path or os.environ.get("JVA_CONFIG") or REPO / "config.json")
    if p.exists():
        return Config(json.loads(p.read_text(encoding="utf-8")), p)
    return Config({}, None)


CONFIG = load()
