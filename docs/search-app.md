# Archive search app

A small web app for searching and browsing `archive_catalog.sqlite`. It opens the catalogue
read-only and never changes it, so it is safe to leave running while other scripts work.
It does pick up a fresh catalogue automatically after `identify_archive.py` or
`classify_archive.py` runs.

## Run it on this Mac

```
python3 -m pip install flask          # once
cd /Users/johnlowe/Claude/Projects/Voelker/search_app
python3 archive_search.py
```

Then open <http://127.0.0.1:5000>. Stop it with Ctrl-C.

## Keep it fast: prepare_search.py

```
python3 prepare_search.py
```

adds a few helper tables and statistics to the catalogue (a stored "kind" for each file, which
drives each file is on, each file's Site and Season, and SQLite's `ANALYZE` statistics). With them, the filter counts beside
the results take well under a second instead of 10 seconds or more. Run it once now.
After that, `identify_archive.py` runs it for you at the end of every run, and
`classify_archive.py` refreshes the statistics. If the helper tables are missing or out of date,
the app still works, just more slowly and without the Site and Season filters, and the
Overview page says so.

When the app starts, it also works out the counts for the Overview's category links in the
background, so those first clicks are quick.

`time_queries.py` shows how long each query on the search page takes:

```
python3 time_queries.py                  # the Museum category page
python3 time_queries.py --cat ""         # All files
python3 time_queries.py --cat TAP --q anvil
```

## What it shows

- **All files**: one entry per distinct file (the `unique_files` table), so a photo kept on
  five drives appears once, marked "5 copies". Search matches words in names and folder paths.
  Words are matched as prefixes (`anvil` finds "Anvils"), and `"quoted phrases"` must appear
  together. Filters on the left narrow by site, season, category, kind, archive drive, source
  drive, copies and whether a preview exists. Results can be shown as slides or as a list,
  sorted by name (the default), site, season, folder, kind, size, copies, category or date
  modified -- from the **Sort by** menu, or by clicking a column heading in the list (click
  again to reverse). With search words, **Best match** is offered too.
  **Site** (NPW, NML, TK, NKH, PL, PF) and **Season** (85, 86, 90, 92, 94) come from the words
  in a file's paths, all its copies: a path is split into words at spaces, punctuation and
  where letters meet digits, so `NPW86 sherds/TAP94.export` counts for NPW, 86 and 94. The
  lists are `SITES` and `SEASONS` in `kinds.py`; after changing them, run `prepare_search.py`.
- **A file's page**: the preview, its facts (format, size, dates, checksum), why it got its
  category, and every copy (from the `files` table) with **Reveal in Finder** and **Copy path**.
  It also lists copies that `free_space.py` removed and other files in the same folder.
  **Open** / **Download** under the preview fetch the original file itself from whichever
  archive drive with a copy is plugged in (PDFs, pictures and plain text open in the browser;
  everything else downloads). Old Word files also get **Open PDF** / **Download RTF**: whole-
  document copies made by `make_derivatives.py`. **Delete** (on each copy) and **Delete all
  copies** remove files from the archive drives after you confirm, using `../delete_files.py`
  (see the main README): the catalogue is updated at once. They show only in local mode, for
  copies on drives that are plugged in.
- **Folders**: browse each archive drive's folders the way they sit on disk, with a bar
  showing each folder's mix of categories.
- **Overview**: totals by category and by archive drive, and whether each drive is plugged in.

Mac `._` files (AppleDouble metadata) are left out everywhere.

## Settings: config.json

| Setting | Meaning |
|---|---|
| `catalogue` | path to `archive_catalog.sqlite`; relative paths are from this folder (default `../archive_catalog.sqlite`) |
| `derivatives_roots` | folders to look in for preview images, in order. The location each image was made in is always tried last. **When you move the derivatives folder, add its new location here.** |
| `mode` | `"local"`: Reveal in Finder and drive status. `"public"`: neither (for EC2) |
| `hide_categories` | categories never shown, e.g. `["Other", "Class"]` |
| `volumes_root` | where archive drives are mounted (`/Volumes`) |
| `host`, `port` | `127.0.0.1` and `5000` |
| `title` | shown in the page header |
| `warm_cache` | `true`: work out the category pages' counts in the background at startup |
| `allow_delete` | Delete buttons. Unset or `true`: shown in local mode; `false`: never. Never in public mode |
| `serve_originals` | open/download original files. Unset: yes in local mode, no in public mode. On a server, `true` serves them from `volumes_root`/*archive drive*/*path* |

A different settings file can be used with `ARCHIVE_SEARCH_CONFIG=/path/to/config.json`.

Previews need the drive holding the derivatives folder to be plugged in. Without it, files
show a placeholder icon. Reveal in Finder needs the archive drive holding that copy to be
plugged in.

## Later: public use on EC2

1. Copy `search_app/`, `archive_catalog.sqlite` (after `prepare_search.py`) and the derivatives folder (`thumb/`,
   `preview/` and `placeholder/`) to the server.
2. Write a `config.json` with `"mode": "public"`, the server's paths for `catalogue` and
   `derivatives_roots`, and any `hide_categories` that should stay private.
3. Run it with gunicorn behind nginx:

   ```
   python3 -m pip install flask gunicorn
   gunicorn -w 3 -b 127.0.0.1:8000 archive_search:app
   ```

   and point nginx at `127.0.0.1:8000`. nginx can also serve the `thumb/` and `preview/`
   folders directly for speed.

In public mode the Reveal and Copy path buttons and drive status are gone, and paths are
shown only as text. Visitors can only read; nothing on the server can be changed through
the app.
