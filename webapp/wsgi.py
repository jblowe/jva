"""The search app for gunicorn (or any WSGI server):

    gunicorn --chdir /path/to/jva/webapp wsgi:app

It reads config.json at the top of the repository, or the file named by $JVA_CONFIG.
On the web server that file sets "mode": "public" (see deploy/server-config.example.json).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import app  # noqa: E402,F401

application = app
