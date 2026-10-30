# Notes for Claude: the jva project (Judy Voelker archive)

Read this first in a new conversation about the project. It is the hand-off from earlier
sessions; docs/ has the full user documentation.

## What it is

Code for cataloguing Prof. Judy Voelker's archive (TAP, the Thailand Archaeometallurgy
Project, and related material) copied from a few dozen old drives onto two archive drives,
and a Flask search app over it, published read-only at https://jva.johnblowe.com.

## Where things are

| | Mac (JB's) | EC2 server |
|---|---|---|
| Code | `~/GitHub/jva` (this repo; GitHub jblowe/jva, public) | `~/jva` |
| Settings | `config.json` here (not in git; see config.example.json) | `~/jva/config.json` (deploy/server-config.example.json) |
| Catalogue | `~/Claude/Projects/Voelker/archive_catalog.sqlite` (= data_dir; also all reports/CSVs) | `/mnt/images/jva_data/archive_public.sqlite` |
| Archive drives | `/Volumes/TAP 4T 2019` (most of it, under CumulativeArchive/), `/Volumes/TAP PROJECT` | — |
| Previews (derivatives) | `/Volumes/TAP PROJECT/_derivatives` | `/mnt/images/jva_images` (served by Apache as /derivatives/) |
| Python | `python3` (miniconda env "main", 3.11) | shared venv `~/venv` (3.12) |

Server: Ubuntu 24.04, Apache2 vhost `deploy/jva.johnblowe.com.conf` (port 80 only; the AWS load
balancer has the certificate), gunicorn on 127.0.0.1:8007 via systemd unit `jva`
(`deploy/jva.service`). publish.py logs in with `publish.server` = ubuntu@<instance IP> and
`publish.ssh_key` = ~/.ssh/jblowe.pem (both in JB's config.json).

## Layout

- `jva/` shared library: catalog.py (schemas, clutter rules: "System Folder", .m4a, .xml, PowerPoint web exports), unique.py
  (unique_files), similar.py (second-level grouping: THE one place for the rules), search_prep.py,
  kinds.py (file kinds incl. MS Word/Presentation/Spreadsheet/Database..., SITES, SEASONS, RULES_VERSION), volumes.py, deletion.py, tiff.py, publish.py
- `scripts/`: drive_inventory, identify_archive (scan, hash, Siegfried, unique, groups, reports,
  prepare), classify_archive (+ rules/classify_rules.csv), make_derivatives, free_space,
  delete_files, check_tiffs, repair_tiffs, prepare_search, publish, time_queries, and reports:
  date_report (creation_dates.xlsx), word_report (word_documents.xlsx), similar_report
  (similar_files.xlsx), filemaker_to_csv (~/jva_fm; vendored third_party/fmptools); find_documents (finds documents by their title pages, searches in
  rules/searches/, e.g. catalogs.csv; spreadsheet and copies go to ~/jva_found, its text cache is
  document_text.sqlite in the data folder)
- `webapp/`: app.py (modes local/public), wsgi.py, templates/, static/
- `tests/`: `python3 -m unittest discover -s tests` (synthetic archive in tests/helpers.py)

## The usual cycle (on the Mac, from ~/GitHub/jva)

identify_archive.py -a "<drive>" -> classify_archive.py -> make_derivatives.py -a "<drive>"
-> publish.py --upload. Drive names: "TAP 4T 2019", "TAP PROJECT" (singular). The local app
picks up catalogue changes by itself; the server app picks up a new catalogue by itself;
code changes need `git pull && sudo systemctl restart jva` on the server.

## How JB works (rules for Claude)

- Commit, never push: JB pushes himself. Commits are authored "John B. Lowe" and end with the
  Co-Authored-By / Claude-Session trailers. Don't rewrite or force-push history.
- Work directly in `~/GitHub/jva` on JB's Mac (through the linked computer) and commit there. No
  git bundles, no sandbox copies of the repo: the old `jva.bundle` hand-off is retired.
- More than one conversation may work on the repo. So: run `git status` and `git log -3` before
  starting and again just before committing; `git add` only the files you changed (never `git add
  -A`, never commit someone else's uncommitted work); if HEAD moved under you, look at the new
  commits before going on. Untracked scratch files at the top (notes, logs, redo.sh) are JB's.
- git needs to delete its own .git lock files, so ask once per session for delete permission on
  the repo folder (device_request_delete_permission) before the first commit; delete nothing else
  with it.
- Never open the live catalogue with SQLite from the linked-computer VM while JB's scripts may
  be using it (WAL -shm across systems caused a SIGBUS). Copy the file (`cp`) and work on the copy.
- Ask before deleting anything from the archive; prefer lists for delete_files.py, reviewed by JB.
- Write plainly; explain causes; keep docs/ in step with code changes.

## State (end of October 2026)

- Catalogue: ~932k non-clutter files, ~222k distinct files, ~137k after grouping similar files.
- Tidy-up done (35,593 junk files deleted); tidy lists in the Voelker folder; 20 locked
  Adobe Illustrator PPD files and the tidy_review.csv items (44 recycle-bin-only files, 3
  Outlook Express mailboxes) still await JB.
- Open items: Word page counts (word_report.py) still need the LibreOffice recount of ~800
  doubtful documents; 3 unreadable files on TAP PROJECT (real I/O errors); ~44 damaged TIFFs
  (check_tiffs/repair_tiffs); 111 object numbers written with two sites (similar_files.xlsx,
  Site check) to review.
- Site abbreviations: NPW Non Pa Wai, NML Non Mak La, NKH, TK, PL Phu Lon, PF (SE Asian, not
  TAP), BNW Ban Non Wat (TAP). Object numbers are unique across sites.
