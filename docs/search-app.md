# Archive search app

A small web app for searching and browsing `archive_catalog.sqlite`. It opens the catalogue
read-only and never changes it, so it is safe to leave running while other scripts work.
It does pick up a fresh catalogue automatically after `identify_archive.py` or
`classify_archive.py` runs.

## Run it on this Mac

```
cd ~/GitHub/jva
python3 -m pip install -r requirements.txt     # once
python3 webapp/app.py
```

Then open the address it prints (the port is `port` in `config.json`; 5000 clashes with
AirPlay on recent macOS, so the example config uses 5001). Stop it with Ctrl-C.

## Keep it fast: prepare_search.py

```
python3 scripts/prepare_search.py
```

**After updating to the reorganised repository, run this once**: the lists of file kinds were
merged, so the stored kinds are rebuilt (until then the app runs in its slower mode).

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
python3 scripts/time_queries.py                  # the Museum category page
python3 scripts/time_queries.py --cat ""         # All files
python3 scripts/time_queries.py --cat TAP --q anvil
```

## What it shows

- **All files**: one entry per distinct file (the `unique_files` table), so a photo kept on
  five drives appears once, marked "5 copies". Versions of the same thing -- files with the
  same name, or the same object number (see "Similar files" in `archive-workflow.md`) -- are
  shown together as one entry, marked "12 versions", by the group's main file (a picture,
  preferably one named just `10091.JPG`). **Versions together / One by one** above the results
  switches between that and every distinct file on its own. The filter counts on the left
  always count distinct files. Search matches words in names and folder paths.
  Words are matched as prefixes (`anvil` finds "Anvils"), and `"quoted phrases"` must appear
  together. Filters on the left narrow by site, season, category, kind (Image, PDF, MS Word --
  including RTF --, Presentation, Spreadsheet, Database -- nearly all FileMaker --, Text,
  Web page, Video, Audio, Other; see `kind_of` in `jva/kinds.py`), archive drive, source
  drive, copies and whether a preview exists. Results can be shown as slides or as a list,
  sorted by name (the default), site, season, folder, kind, size, copies, category or date
  modified -- from the **Sort by** menu, or by clicking a column heading in the list (click
  again to reverse). With search words, **Best match** is offered too.
  **Site** (NPW, NML, TK, NKH, PL, PF) and **Season** (85, 86, 90, 92, 94) come from the words
  in a file's paths, all its copies: a path is split into words at spaces, punctuation and
  where letters meet digits, so `NPW86 sherds/TAP94.export` counts for NPW, 86 and 94. The
  lists are `SITES` and `SEASONS` in `jva/kinds.py`; after changing them, run `scripts/prepare_search.py`.
- **A file's page**: the preview, its facts (format, size, dates, checksum), why it got its
  category, and every copy (from the `files` table) with **Reveal in Finder** and **Copy path**.
  It also lists copies that `free_space.py` removed and other files in the same folder.
  Under **Where it is**, **Other versions** lists the rest of the file's group, coloured:
  green for identical (this file's own copies, and other files with the same name), copper
  for similar (other versions of the same object), with the reason each one is there.
  **Open** / **Download** under the preview fetch the original file itself from whichever
  archive drive with a copy is plugged in (PDFs, pictures and plain text open in the browser;
  everything else downloads). Old Word files also get **Open PDF** / **Download RTF**: whole-
  document copies made by `make_derivatives.py`. **Delete** (on each copy) and **Delete all
  copies** remove files from the archive drives after you confirm, the same way as
  `scripts/delete_files.py` (see `archive-workflow.md`): the catalogue is updated at once. They show only in local mode, for
  copies on drives that are plugged in.
- **Folders**: browse each archive drive's folders the way they sit on disk, with a bar
  showing each folder's mix of categories.
- **Overview**: totals by category and by archive drive, and, on the Mac, whether each drive is plugged in.

Mac `._` files (AppleDouble metadata) are left out everywhere.

## Settings: config.json

The app reads the `"app"` section of `config.json` at the top of the repository (or the file
`$JVA_CONFIG` names). The catalogue is the one the rest of `config.json` names (`catalogue`,
or `archive_catalog.sqlite` in `data_dir`), and `volumes_root` is shared with the scripts.

| Setting | Meaning |
|---|---|
| `mode` | `"local"`: Reveal in Finder, drive status, originals, Delete. `"public"`: none of those -- the routes don't exist (the web server) |
| `derivatives_roots` | folders to look in for preview images, in order. The location each image was made in is always tried last. **When you move the derivatives folder, add its new location here.** |
| `derivatives_url` | public mode: the URL the web server serves the derivatives folder at (`/derivatives`). Pages then link images straight there instead of through the app |
| `hide_categories` | categories never shown, e.g. `["Other", "Class"]` (the public catalogue leaves its excluded categories out altogether, see `publish.py`) |
| `host`, `port` | `127.0.0.1` and `5000` unless set |
| `title` | shown in the page header |
| `page_size` | results per page (60) |
| `warm_cache` | `true`: work out the category pages' counts in the background at startup |
| `allow_delete` | Delete buttons. Unset or `true`: shown in local mode; `false`: never. Never in public mode |
| `serve_originals` | open/download original files. Unset or `true`: yes in local mode; `false`: no. Never in public mode |

Previews need the drive holding the derivatives folder to be plugged in. Without it, files
show a placeholder icon. Files with no preview of their own (FileMaker databases, archives,
email, ...) show a placeholder for their sort of file, with a caption: "FileMaker database",
"Spreadsheet", "Word document", and so on (`jva/kinds.py`, `placeholder_for`). Reveal in Finder needs the archive drive holding that copy to be
plugged in.

## On the web server

See `deployment.md`: `scripts/publish.py` makes and uploads the public catalogue and the
images, and `deploy/` has the gunicorn, systemd and Apache files. In public mode the Reveal,
Delete, Open and Download buttons and the drive status are gone (the PDF/RTF copies of old
Word files stay), and paths are shown as text. Visitors can only read; nothing on the server
can be changed through the app.
