# jva

Code for cataloguing, de-duplicating, classifying, previewing and publishing Prof. Judy
Voelker's archive (TAP, the Thailand Archaeometallurgy Project, and related materials).

- `scripts/` -- the commands (drive inventory, cataloguing, classification, previews,
  clean-up, publishing)
- `jva/` -- code shared by the scripts and the web app
- `webapp/` -- the search app (Flask)
- `rules/` -- the classification rules
- `deploy/` -- gunicorn, systemd and Apache files for the web server
- `tests/` -- tests on a small made-up archive
- `docs/` -- how it all works

## Getting started

```sh
git clone https://github.com/jblowe/jva.git ~/GitHub/jva
cd ~/GitHub/jva
cp config.example.json config.json      # then set data_dir, derivatives_dir, ...
python3 -m pip install -r requirements.txt
python3 scripts/prepare_search.py
python3 webapp/app.py
```

`config.json` says where the data is: the catalogue, the drive records and the reports live
in `data_dir`, never in this repository. Scripts run from the top of the repository, e.g.
`python3 scripts/identify_archive.py -a "TAP 4T 2019"`.

## Documentation

- [docs/archive-workflow.md](docs/archive-workflow.md) -- archiving drives, the catalogue,
  classification, previews, freeing space, deleting files, damaged TIFFs
- [docs/search-app.md](docs/search-app.md) -- the search app and its settings
- [docs/deployment.md](docs/deployment.md) -- the public site, jva.johnblowe.com

## Tests

```sh
python3 -m unittest discover -s tests
```
