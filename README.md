# jva

Code for cataloguing, de-duplicating, classifying, previewing and publishing Prof. Judy
Voelker's archive (TAP, the Thailand Archaeometallurgy Project, and related materials).

- `scripts/` -- the commands (drive inventory, cataloguing, classification, previews, clean-up)
- `jva/` -- code shared by the scripts and the web app
- `webapp/` -- the search app (Flask)
- `rules/` -- the classification rules
- `docs/` -- how it all works

Settings: copy `config.example.json` to `config.json` and set `data_dir` to the folder that
holds the catalogue and the drive records. Data never goes in this repository.
