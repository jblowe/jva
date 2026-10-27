# Voelker drive archiving

Scripts for consolidating a few dozen old external hard drives onto one or more
**archive drives**. Each source drive is plugged in, inventoried and copied onto the
archive drive you name, at the root of that volume:

    /Volumes/<archive drive>/<drive name>/

(On TAP 4T 2019, the drives archived so far are in `CumulativeArchive/`; use
`--folder CumulativeArchive` to keep adding them there.)

All inventories and logs are kept here, in `/Users/johnlowe/Claude/Projects/Voelker`.

Both scripts are plain Python 3 (standard library only, nothing to install) and are
written for macOS.

## Files

| File | What it is |
|---|---|
| `search_app/` | The search app: search and browse the catalogue in a web browser, with previews (see "Searching the archive" and `search_app/README.md`) |
| `classify_archive.py` + `classify_rules.csv` | Sorts files into TAP / SE Asian Archaeology / Other Archaeology / Class / Museum / Other (see "Classifying by subject") |
| `check_tiffs.py`, `repair_tiffs.py` | Find TIFFs with a damaged directory (the kind macOS Preview won't open) and repair them (see "Damaged TIFFs") |
| `delete_files.py` | Deletes files you list (or delete in the search app) from the archive drives, and updates the catalogue (see "Deleting files") |
| `free_space.py` | Removes extra copies of identical files on an archive drive, with a record of each (see "Freeing space") |
| `make_derivatives.py` | Makes WebP thumbnails and previews for the search app, recorded in the catalogue (see "Preview images" below) |
| `identify_archive.py` | Catalogues, de-duplicates and identifies the formats of everything on the archive drives, one drive at a time, into one shared catalogue (see below) |
| `drive_inventory.py` | Inventories one source drive, copies it onto an archive drive, and updates the summary |
| `drive_summary.csv` | **The main spreadsheet:** one row per drive, plus a TOTAL row. Rebuilt after every run |
| `drives.csv` | One row per run (so a drive may appear more than once): full details |
| `archive_map.csv` | Which source drive (by volume UUID) went into which folder on which archive drive. **Don't delete it.** |
| `<drive name>.csv` | Every file on that drive: path, type, size, dates, permissions, plus a row for anything that couldn't be read or copied (see below). Named like the drive's archive folder; rerunning a drive replaces it |
| `old/` | Earlier versions of the scripts, including the retired `summarize_drives.py`. Safe to delete |

## One-time setup

Stop Spotlight from indexing each archive drive. Indexing slows copies down and isn't
needed there:

    sudo mdutil -i off "/Volumes/TAP 4T 2019"
    sudo mdutil -i off "/Volumes/<other archive drive>"

## Archiving a drive

1. Make sure the **archive drive** is mounted.
2. Plug in **one** drive to be archived. Don't plug in any other external drives
   (other archive drives the script already knows about are ignored).
3. Run, naming the archive drive (required):

       cd /Users/johnlowe/Claude/Projects/Voelker
       python3 drive_inventory.py -a "TAP 4T 2019" --folder CumulativeArchive >> archive_log.txt
       python3 drive_inventory.py -a "<other archive drive>" >> archive_log.txt

   Progress shows in the terminal. The `>> archive_log.txt` part saves the one-line
   result of each run, giving you a running log.
4. Folder names must be unique across **all** archive drives, since they share one
   catalogue. If the drive's name is already used, the script prints
   `DUPLICATE: "<name>" is already used (<archive drive>: <folder>)` and asks for a
   different name. That name becomes the drive's folder. You're asked before the long
   scan starts.
5. When it finishes, the script ejects the drive and prints "safe to unplug". Unplug it
   and plug in the next one.

When the run ends, the script prints one line, for example:

    TAP 4T 2019: 1834.27 GB available | copied: 412.66 GB in 18757.3 s (22.0 MB/s) | Voelker 2008 -> Voelker 2008: copied

## What the script does

- **Finds the drive.** It picks the single external drive that isn't the archive drive
  (or another archive drive it knows about). If it finds none, or more than one, it stops
  and tells you.
- **Protects the original.** It unmounts the source drive and mounts it again
  **read-only**, so nothing can write to it: not the script, not Spotlight (no
  `.Spotlight-V100` index), and not Finder (no `.DS_Store` files). If the drive can't be
  unmounted, usually because a Finder window or app has it open, it says so and stops.
  Close the window and run again. `drives.csv` records `read_only` = Y/N for each run.
- **Inventories it (read-only).** It writes `<drive name>.csv`, with one row per file
  (folders aren't listed).
- **Checks space.** If what still needs copying, plus a 5 GB reserve, won't fit on the
  archive drive, it **refuses to copy**. The inventory is still saved.
- **Copies.** It uses the Mac's own copy routine, the same one Finder uses, so dates,
  permissions, extended attributes and old-style resource forks are kept. macOS
  housekeeping folders (`.Spotlight-V100`, `.Trashes`, `.fseventsd`, …) are left out.
- **Handles bad sectors.** Problems go into the same `<drive name>.csv`, in its `status`
  column: `READ ERROR` for a file or folder that couldn't be read during the inventory,
  and `COPY ERROR` for a file that couldn't be copied. The `error` column says why. The
  copy carries on regardless. Empty folders and symlinks are copied too.
- **Records the result.** It adds a row to `drives.csv`, updates `archive_map.csv`, and
  rebuilds `drive_summary.csv`.
- **Ejects the source drive** so it can be unplugged. It also ejects a drive it skipped
  as already archived. It never ejects the archive drive. If the eject fails, it prints a
  warning; eject the drive in Finder.

## Interruptions, repeats, problems

- **Interrupted copy** (Ctrl-C, cable pulled, Mac slept): just run it again with the same
  archive drive. It goes back into the same archive folder and skips files already copied
  with the same size and date. If you name a *different* archive drive for a drive that
  was started elsewhere, it stops and tells you where it was started. Add `--name NAME`
  if you really want a second copy on the other archive drive.
- **Drive already archived:** it's skipped. Use `--redo` to go over it again; only missing
  or changed files are copied.
- **Not enough space:** free some room on the archive drive, use another archive drive,
  or lower the reserve with `--reserve-gb 1`, then run again.
- **Two drives with the same name:** drives are told apart by their volume UUID, so a
  second "Untitled" drive gets the duplicate-name prompt. It is not mistaken for the first one.
  The UUID is in the `volume_uuid` column of `drives.csv` and `archive_map.csv`.
- **`Input/output error`** usually means bad sectors on the source drive. Those files end
  up as `READ ERROR` / `COPY ERROR` rows in `<drive name>.csv`. If a drive has many of them, image it with `ddrescue`
  (`brew install ddrescue`) rather than rescanning it over and over.

## If copying is slow

Run this with the source drive plugged in, while nothing else is copying:

    python3 drive_inventory.py -a "TAP 4T 2019" --speedtest

It reads about 1 GB of large files from the source drive, writes 1 GB to the archive drive, and
reports both speeds, so you can see which drive is holding things up. It deletes its test
file afterwards. Typical causes:

- **Under ~35 MB/s on the source:** usually a USB 2.0 enclosure, common on older drives.
  Taking the bare disk out and putting it in a USB 3 SATA dock typically gives
  80–150 MB/s.
- **Slow writes to the archive drive:** many 4 TB portable drives use SMR recording, which
  drops to tens of MB/s on long writes. A short test can look fine while long copies
  crawl. Turning Spotlight off for it helps a little:
  `sudo mdutil -i off "/Volumes/TAP 4T 2019"`.
- **Lots of small files:** each file costs disk seeks on both drives, so folders of small
  files copy far below the drives' rated speeds.

## Options

    python3 drive_inventory.py -a "ARCHIVE DRIVE" [/Volumes/SomeDrive] [options]

| Option | Effect |
|---|---|
| `-a`, `--archive NAME` | **Required.** The archive drive to copy onto: its name in `/Volumes`, or a full path |
| `--folder SUB` | Copy into this folder on the archive drive instead of its root (e.g. `CumulativeArchive` on TAP 4T 2019) |
| `/Volumes/SomeDrive` | Archive this drive instead of detecting it automatically |
| `--name NAME` | Use this archive folder name; no prompt. Reuses the folder if it exists |
| `--no-copy` | Inventory only |
| `--redo` | Process a drive that was already archived |
| `--reserve-gb N` | GB to leave free on the archive drive (default 5) |
| `--magic` | Also detect each file's real type with `file` (slower) |
| `--all` | Include the macOS housekeeping folders |
| `--summary-only` | Just rebuild `drive_summary.csv`; no drive needed |
| `--no-eject` | Leave the source drive mounted when finished |
| `--no-readonly` | Use the source drive as mounted, without remounting it read-only |
| `--speedtest` | Measure the source drive's read speed and the archive drive's write speed separately, then stop |

## Identifying and de-duplicating the archive

    brew install richardlehane/digipres/siegfried   # once; the format identifier
    python3 identify_archive.py -a "TAP 4T 2019"
    python3 identify_archive.py -a "<other archive drive>"

The archive drive is required: the script processes one archive drive per run, reading
**everything on that volume from its root** (it never writes to it). All archive drives
go into one shared catalogue, and a run only ever changes the entries for the drive you
named, so archive drives that aren't plugged in are left alone. Run it whenever you like;
afterwards it only processes what's new or changed. Don't run it while
`drive_inventory.py` is copying to the same archive drive: the two slow each other down
badly.

Each file is attributed to its **source drive**, the folder `drive_inventory.py` copied it
into. Anything else on the volume is grouped by its top-level folder (files directly in
the volume root show as "(volume root)").

Five steps, with progress on screen:

1. **Scan.** Lists every file. It also reads each file's classic Mac **type/creator**
   codes and **resource-fork** size. Pre-OS X files often have no extension, and these
   codes are frequently the only clue to what they are (e.g. `TEXT`/`MSWD` is a Word
   file).
2. **Checksum.** Computes a SHA-256 checksum for every file that doesn't have one yet.
   Checksums identify identical files across all archive drives, and they're a fixity
   record: they let you check later that no file has changed or decayed. This step reads
   every byte of every new file, so it's the slowest part of a first run on a new archive
   drive. Later runs only checksum new or changed files.
3. **Identify.** Runs Siegfried on each file's contents to get its PRONOM format ID
   (e.g. `fmt/353` = TIFF). Without Siegfried it falls back to MIME types from `file`,
   and redoes those files properly once Siegfried is installed.
4. **Unique files.** Rebuilds the `unique_files` table and its search index (see below).
5. **Reports.** Writes these files to this folder:

| File | What it is |
|---|---|
| `archive_catalog.sqlite` | The master catalogue: every file on every archive drive (`files`), one row per distinct file (`unique_files`), and a search index; see "The catalogue's tables" below |
| `archive_by_volume.csv` | Per archive drive: files, GB, how much exists only on that archive drive, how much is also on another, files not yet checksummed, when it was last scanned |
| `archive_by_drive.csv` | Per source drive (with its archive drive): files, GB, how much exists **only** on that drive, how much is also on other drives, duplicates within the drive, unidentified files, files with Mac type codes or resource forks, files not yet checksummed |
| `archive_duplicates.csv` | One row per set of identical files, ordered by wasted space, with where the copies are (shown as *archive drive: path*) |
| `archive_formats.csv` | One row per format: files, GB, number of drives, extensions seen, an example. Unidentified files are grouped by their Mac type/creator codes |
| `appledouble_orphans.csv` | `._` files with nothing beside them: the file or folder they belong to isn't on the archive drive. Usually that file didn't make it onto the drive, or an empty folder (only files are listed) |

System clutter (`.DS_Store`, `Thumbs.db`, empty files, and every hidden dot-file, above
all the `._` files) is catalogued but treated as clutter: it isn't checksummed, never counts
as a duplicate, and is left out of `unique_files`, the search app, the preview images and
the subject classification. On exFAT drives each Mac file's type and creator codes and
resource fork live in a `._<name>` file beside it; the scan reads those into the file's
own row (`mac_type`, `mac_creator`, `rsrc_bytes`), so nothing is lost by not treating the
`._` files as content. They stay listed in `files` because they are on the drive: the
totals then match what's there, `free_space.py` and `drive_inventory.py --redo` look after
them, and `appledouble_orphans.csv` uses them to spot files that didn't get copied. Use `--reports` to just rebuild the `unique_files` table and the CSVs (no
archive drive needed), and `--skip-identify` to skip step 3.

### The catalogue's tables

`archive_catalog.sqlite` holds two linked tables:

- **`files`**: every copy of every file on every archive drive (one row per path).
  Each row's `unique_id` points to its row in `unique_files`.
- **`unique_files`**: one row per *distinct* file. Identical copies share one row.
  It's rebuilt at the end of every run, so it always matches `files`. Columns: `name`
  (the oldest copy's name), `other_names` (other names the same content goes by), `ext`,
  `format`, `puid`, `mime`, `mac_type`, `mac_creator`, `size_bytes` and `size` ("1.2 MB"),
  `created` and `modified` (earliest among the copies), `modified_latest`, `year`,
  **`copies`** (1 = unique, N = N identical copies), `drives` and `volumes` (how many
  source drives / archive drives hold a copy), `archive`, `path` and `folder` of the
  representative copy, **`locations`** (every copy as *archive drive: path*), `drive_list`,
  `sha256`, and `checksum` (`checksummed`, `not yet checksummed` or `unreadable`).
- **`unique_search`**: a full-text index over `unique_files` (names, other names, folders,
  locations, format, extension), for fast word searches.
- **`unique_archives`**, **`unique_drives`**, `unique_files.kind` and `search_meta`: helper
  tables for the search app, rebuilt by `search_app/prepare_search.py`, which
  `identify_archive.py` runs at the end of every run (step 6).

Clutter files are not in `unique_files`; their `unique_id` is empty. Some examples:

```sql
-- all copies of one distinct file
SELECT archive, path FROM files WHERE unique_id = 123;

-- word search: files whose name, folder or path mentions "anvil" (and "anvils", ...)
SELECT u.name, u.size, u.copies, u.folder FROM unique_search s
JOIN unique_files u ON u.id = s.rowid WHERE unique_search MATCH 'anvil*' ORDER BY rank;

-- the most-duplicated files
SELECT name, size, copies, drives FROM unique_files ORDER BY copies DESC LIMIT 20;
```

`created` (the file's creation date) was added in this version. The next run on each
archive drive fills it in for files already in the catalogue, without re-reading them.

The first run of this version converts the existing catalogue automatically: everything
already in it is assigned to TAP 4T 2019, under `CumulativeArchive/`, keeping all
checksums and format results. The first TAP 4T 2019 run then also catalogues whatever
else is on that volume outside `CumulativeArchive`. Each archive drive is identified by
its volume UUID as well as its name, so two different drives with the same name can't
be mixed up; the script refuses and asks you to rename one.

## Preview images ("derivatives")

    brew install imagemagick ghostscript      # once (qlmanage and sips come with macOS)
    brew install --cask libreoffice           # once, for Canvas drawings and old Mac Word files
    python3 make_derivatives.py -a "TAP 4T 2019" -d "/Volumes/TAP 4T 2019/derivatives"

Makes web-friendly preview images for the archive, for the search app. Like the other
scripts it works on one archive drive at a time (`-a`, required). `-d` (required) is
where the images go; any drive works, e.g. a `derivatives` folder next to the archived
folders. `identify_archive.py` recognizes that folder (by a `.archive-derivatives` marker
file inside it) and never catalogues it. Run `identify_archive.py` on a drive first:
derivatives are keyed on checksums.

- **One per distinct file.** Identical copies share one set of images.
- **Two WebP images each:** `thumb` (480 px on the long side) for result lists, and
  `preview` (1,200 px) for the detail view.
- **Images** (JPEG, TIFF, PSD, PNG, GIF, PICT, camera raw, …) are converted by ImageMagick,
  falling back to macOS `sips`, then Quick Look. **PDFs:** first page, by ImageMagick with
  Ghostscript, falling back to Quick Look. **Documents and video** (Word, Excel, PowerPoint,
  Pages, Keynote, RTF, text, HTML, movies, …): first page or frame, from macOS Quick Look.
- **Canvas drawings** (the old Mac Deneba Canvas files, mostly TAP object drawings such as
  `T_16560`): recognized by their Mac type code (`drw2`, `drw5`, `drwt`), since most
  have no extension or a meaningless one, or by `.cvs`, `.cv5`, `.canvas`, … LibreOffice,
  run in the background, opens each one, and the script fixes two things before it
  makes a PDF: text boxes are let grow to fit their text, because LibreOffice reads
  them slightly too narrow and labels wrapped ("T# / 16560", "5 c / m"); and old Mac
  fonts that macOS no longer has (Chicago, New York, …) are swapped for the nearest
  (`FONT_MAP` in the script). The drawings mostly use Monaco and Geneva, which macOS
  still has. ImageMagick then makes the WebPs with the blank page around the drawing
  trimmed off (`method` = `libreoffice`), about 3–4 seconds a drawing. Without
  LibreOffice they get a `drawing` placeholder until a later run with it installed.
- **Old Mac documents**: Microsoft Word 4/5 (Mac type `WDBN`), Word 6 (`W6BN`),
  Word 98 (`W8BN`), MacWrite, ClarisWorks, WriteNow, old Excel and PowerPoint, mostly
  with no extension or a made-up one (`.final`, `.rev`, `.ltr`). Quick Look can't read
  them and hangs trying, so they go to LibreOffice, which makes a PDF; its first page
  becomes the preview (`method` = `libreoffice`, under a second a file). For the **old
  Microsoft Word files** the whole document is also kept, as a PDF and as an RTF that
  any word processor opens today, in `pdf/` and `rtf/` next to `thumb/` and `preview/`
  (columns `pdf` and `rtf`). The search app links to both from a file's page. One
  caveat: LibreOffice updates Word's automatic date fields, so a letter whose date was
  such a field shows the date of conversion in the PDF.
  Documents that failed in Quick Look in earlier runs are tried in LibreOffice
  automatically; other documents Quick Look can't do are tried there too. Quick Look now
  gets 45 seconds a file instead of 2 minutes.
- **Adobe Illustrator, EPS and PostScript** (`.ai`, `.eps`, `.ps`, or Illustrator's creator
  code `ART3`/`ART5`/`ARTZ`): told apart by the file's first bytes, because the old Macs
  typed Illustrator 5–8 files `TEXT`, often with no extension (e.g. the NML traced
  sections). PostScript goes to Ghostscript, on a page the size of the drawing's
  `%%BoundingBox` (Illustrator art often lies partly off its page), with the blank
  margins trimmed (`method` = `ghostscript`). PDF-based Illustrator files (version 9 on)
  are drawn like PDFs. Quick Look is never used for these: it shows PostScript as text.
- **Everything else** gets a shared placeholder per kind (document, audio, archive, font,
  application, database, code, drawing, unknown).
- **Reruns** only do what's missing, but files that got a placeholder in an earlier run
  and that the script can now render (the Canvas drawings, before it knew them) are
  redone automatically, as are Illustrator/EPS files that an earlier version previewed
  through Quick Look as a picture of their PostScript code.

Names are web-safe: a slug of the file name plus the first 10 characters of its
checksum, in folders named by the first 2 characters (so no folder gets huge):

    derivatives/thumb/1b/anvils-2-for-jake-3-10-31-08-1ba0ca91f8.webp
    derivatives/preview/1b/anvils-2-for-jake-3-10-31-08-1ba0ca91f8.webp
    derivatives/pdf/1b/...pdf  and  derivatives/rtf/1b/...rtf    (old Word files only)
    derivatives/placeholder/document.svg

The `derivatives` table in `archive_catalog.sqlite` has one row per distinct file:
`sha256` (the same key as in `files` and `unique_files`), `slug`, `kind`, `method`
(imagemagick / sips / libreoffice / ghostscript / quicklook / placeholder), `status` (made / placeholder / failed),
`root` (the derivatives folder), `thumb` and `preview` (paths relative to it), the preview's
`width` and `height`, `pdf` and `rtf` (old Word files: the whole document), which copy it was made from, and the error if it failed. For example:

```sql
SELECT u.name, u.copies, d.root || '/' || d.thumb AS thumbnail
FROM unique_files u JOIN derivatives d USING (sha256) WHERE u.name LIKE '%anvil%';
```

Add `--only TAP` (or e.g. `--only "TAP,SE Asian Archaeology"`) to make images only for
those categories; run `classify_archive.py` first.

Reruns only do what's missing, so you can stop it any time. `--retry-failed` tries
failures again, `--workers N` sets how many files are processed at once (default 4), and
`--limit 200` does a short trial run. For the whole archive, expect roughly 240,000
images, about 50 GB, and 10–20 hours, mostly spent reading the original images off the
drive. Don't run it while another script is using the same drive.

## Classifying by subject (TAP vs. the rest)

    python3 classify_archive.py

Sorts every file in the catalogue into **TAP**, **SE Asian Archaeology** (other than TAP),
**Other Archaeology**, **Class** (course materials), **Museum** (exhibits, displays,
collections work, museum photography), **Other**, or **Unclassified**. It
works from the catalogue alone (no drive needed), so rerun it whenever you edit the rules
or after cataloguing more.

The rules are in **`classify_rules.csv`**, which you can edit. Each line is a pattern (matched
against folder and file names, ignoring case), a category, and a strength:
**strong** (a specific name: a site, project or course number), **broad** (a wide TAP net
such as "Thai", "slide scan" or "notebook" that loses to a strong match of another category
in the same folder name), **nottap** (a place outside Thailand: China, Cambodia, Laos,
Istanbul, World Trip, …; like strong, and see step 2), **weak** (a general word), or
**override** (your exceptions; see below). For each file:

1. An **override** match anywhere in its path decides.
2. Otherwise a TAP match anywhere makes it TAP, so `ANT194 2011/Non Pa Wai slides/IMG_1.jpg`
   is TAP. In a computer, drive or backup-like folder name ("TAP Voelker mac laptop",
   "TAP - KEEP", "TAP from Museum wd") TAP only counts as weak, so the folders inside decide.
   A **folder** matching a **nottap** rule cancels any TAP match above it, so
   `Slide Scanning Project/World Trip/China/img.jpg` is Other, not TAP. A TAP match in that
   same folder name or deeper still counts (`TAP Images/China/TAP NPW comparisons/…` is TAP),
   and a place in a *file* name cancels nothing (`TAP Export Records/TAP 94/TAP94.export.rome1`
   stays TAP). "Pigott" is a broad TAP match, so `Abydos Egypt Pigott slides` is not TAP.
3. Otherwise the **deepest** folder (or the file name) with a strong match decides, so
   `ANT194 2011/week 3/lecture.ppt` is Class.
4. Otherwise the deepest weak match decides; otherwise it's Unclassified.

The whole path counts, so `IMG_287.TIF` gets its category from its folders. A copied
drive's own label ("JUDY 3", "THAI 2008") is used only as a last resort, and an archive
drive's name ("TAP 4T 2019", e.g. in `CumulativeArchive/TAP 4T 2019/…`) never. A distinct
file is TAP if **any** of its copies is TAP; otherwise the first of Class, SE Asian
Archaeology, Other Archaeology, Museum, Other found among its copies. The search app's
"Why TAP" line explains it with the copy that made it TAP.

**Exceptions.** To take a folder out of TAP (or put it anywhere else) whatever surrounds it,
add an override line to the rules file, then rerun:

    ^Voelker Sabbatical 2013 material$,Other,override,

Results go into the catalogue (`file_class` per path, `content_class` per distinct file,
and `unique_files.category`) and into two CSVs:
- `classification_summary.csv`: files and GB per category, per archive drive.
- `classification_explain.csv`: **what each category really contains.** For every folder
  name that decided a category: the rule, files, GB, where it is, and the biggest
  subfolders inside it. Filter on `category` = TAP and read down the list to find non-TAP
  material hiding in TAP folders, then add override lines for it.
- `classification_review.csv`: folders by size with how their files were classified and
  the rule that decided most of them. Look here for big Unclassified or mis-classified
  folders, then add or adjust a rule.

`make_derivatives.py --only TAP` then makes preview images for TAP material only.

## Freeing space: removing extra copies

    python3 free_space.py -a "TAP 4T 2019" --target-gb 100            # dry run: the plan
    python3 free_space.py -a "TAP 4T 2019" --target-gb 100 --apply    # do it

Removes extra copies of identical files on one archive drive, largest first, until the
target is reached (or `--all`). On each drive it keeps one copy of every file, chosen in
this order: a copy classified **TAP** (if `classify_archive.py` has been run); then one
**not** in a throwaway-looking folder ("Delete when Finished", "dup", "old", "Recycled",
"already copied", …), and preferably not in a "backup" folder; then the oldest. Without `--apply` it only writes the plan
to `free_space_plan.csv`. Files under 1 MB are ignored (`--min-mb` changes that).

Before removing each copy it checks that both files still match the catalogue and
compares them byte for byte. It also checks that their classic Mac resource forks are the
same (checksums only cover the main data); copies whose resource fork differs are skipped
and listed. A copy's `._` companion is removed with it.

In place of each removed copy it leaves a **stub**: a tiny text file with exactly the same
name, whose content is the full path of the copy that was kept, e.g.

    /Volumes/TAP 4T 2019/CumulativeArchive/JUDY 3/Voelker TAP Videos/clip.m4v

So the folder still shows the file, and the stub says where the real one is. (It will
look like a broken image or video if opened in its usual app, since only the name is
real.) The scripts recognize stubs through the record below and treat them as clutter,
never as real files. `--no-stubs` leaves nothing behind instead. Every removal is recorded in the
catalogue table `removed_copies` (path, checksum, size, dates, category, and which copy was
kept), so the search app can still show where a file used to be, and
`drive_inventory.py --redo` won't copy it back. Copies that came from a source drive are
still on that source drive. Afterwards, run `python3 identify_archive.py --reports` to
refresh `unique_files` and the reports.

## Deleting files

    python3 delete_files.py to_delete.txt --reason "not part of the archive"

`to_delete.txt` lists what to delete, one path per line (or a CSV file with a `path` column),
written as the search app shows it (`TAP 4T 2019: CumulativeArchive/JUDY 3/...`), as a full
path (`/Volumes/TAP 4T 2019/CumulativeArchive/...`), or relative to the drive if only one
drive has it. A folder deletes everything in it. The drive must be plugged in.

It first lists what will go in `delete_plan.csv` and says how many files, how much space,
and how many are the **last copy** in the archive; then it asks (`--yes` skips the
question). Each file goes together with its `._` companion, and folders left empty (or with
only `.DS_Store`) go too. The catalogue is updated straight away: the copies leave `files`
and the search app; a file whose last copy is gone leaves `unique_files` with its preview
images. Each deletion is recorded in the `deleted_files` table (path, size, category, copies
left, reason, date, and whether it was this script or the search app), and
`drive_inventory.py --redo` never copies those files back. The search app's **Delete**
buttons do the same, one copy or all copies at a time. Deleting is permanent: exFAT drives
have no Trash.

## Damaged TIFFs

    python3 check_tiffs.py        # reads only; writes tiff_check.csv
    python3 repair_tiffs.py       # repairs the files listed there (asks first)

`check_tiffs.py` reads the header and directory of every TIFF on the archive drives that are
plugged in (one copy of each distinct file), not the images, and lists in `tiff_check.csv`
those whose structure is broken. The usual fault: a pointer to the Exif (or GPS or
Interoperability) block that leads past the end of the file or into the picture. Other
programs ignore it; macOS Preview and Quick Look won't open the file. `--file x.tif` checks
one file.

`repair_tiffs.py` removes just that bad entry from the file's directory, in place, in every
copy on the drives plugged in: the picture, colour profile, Photoshop data and the file's date
are unchanged (typically 6-17 bytes differ). Each file is checked before and after, and the
original bytes are kept in the catalogue (table `tiff_repairs`), so `--undo` puts every file
back exactly. Because the files' contents change, so do their checksums; the catalogue
follows (copies, preview images, categories). Copies on drives that weren't plugged in can be
repaired later by running both scripts again with that drive. Files listed as "not a TIFF"
(mostly Photoshop files named `.tif`) are left alone.

## Searching the archive

The search app in `search_app/` lets you search and browse the catalogue in a web browser:

    python3 -m pip install flask          # once
    cd search_app
    python3 prepare_search.py             # once; identify_archive.py repeats it after each run
    python3 archive_search.py             # then open http://127.0.0.1:5000

`search_app/README.md` has the details, the settings, and notes on running it on a server.

## Summary spreadsheet

`drive_summary.csv` is rebuilt automatically at the end of every run. It has one row per
drive, using its latest run, and a TOTAL row. Columns:

- **Archive:** archive drive, archive folder, copy status, GB copied, copy errors
- **Disk:** model, disk type (SSD/HDD), bus, format, encrypted, physical disk size
- **Space:** disk size, space used, space free (GB), and % used
- **Contents:** files, folders, top-level folders, top-level files, hidden files, total
  GB of files, read errors, the largest file, the five file types using the most space
  (each with its GB), oldest and newest file dates, and the names of the top-level folders

Every number column holds a plain number: sizes are decimal GB (1 GB = 1,000,000,000
bytes, as Finder reports), with no units or text, so they sort and add up in Excel or
Numbers. To rebuild it without a drive plugged in:

    python3 drive_inventory.py --summary-only

## Notes

- An earlier version of the script wrote its inventories to `~/drive_inventory`. Move
  `drives.csv` and the per-drive folders here if you want them in the summary. The script
  merges the old columns in automatically. Those drives show no copy results, since they
  weren't copied.
- TAP 4T 2019 has itself shown read errors in the past. Run Disk Utility → First Aid on
  the archive drives now and then, and watch the `copy_errors` counts in `drive_summary.csv`.

- A drive is mounted read-write for a moment when you plug it in, before the script
  remounts it, so macOS may already have written its own housekeeping folders to it in
  that window. To prevent even that, an app such as the open-source Disk Arbitrator can
  make new disks mount read-only from the start.
