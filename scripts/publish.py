#!/usr/bin/env python3
"""
publish.py -- put the public part of the archive on the web server.

    python3 scripts/publish.py              # build the public catalogue here, and stop
    python3 scripts/publish.py --upload     # build it, then upload it and the images
    python3 scripts/publish.py --upload-only    # upload what the last build made

What is public: every distinct file except those in the categories listed in config.json's
"publish": {"exclude_categories": [...]} (Class and Unclassified unless you say otherwise),
with its catalogue record, its folder paths, its preview images and, for old Word files, the
PDF and RTF copies. The originals never leave the Mac. See jva/publish.py for exactly what the
public catalogue leaves out.

Building writes, in "publish": {"out_dir"} (default <data_dir>/public):
    archive_public.sqlite     the public catalogue
    derivatives_files.txt     the derivative files it uses (paths in the derivatives folder)

Uploading (needs ssh access to "publish": {"server"}, e.g. ubuntu@jva.johnblowe.com):
  1. the images and PDF/RTF copies -> <remote_dir>/derivatives/   (rsync: only what changed)
  2. the catalogue -> <remote_dir>/archive_public.sqlite, swapped in at once, so the site
     never sees half a file; the running app notices the new catalogue by itself
  3. derivative files on the server the new catalogue no longer uses are deleted
     (--keep-old to skip this)
The derivatives folder (derivatives_dir, or -d) must be readable: plug in its drive.
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # the repository, for jva
from jva.config import CONFIG  # noqa: E402
from jva.publish import LIST_NAME, PUBLIC_NAME, build_public  # noqa: E402
from jva.catalog import human  # noqa: E402

RSYNC_PERMS = ["--chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r"]      # readable by Apache


def run(cmd, **kw):
    print("  $ " + " ".join(cmd if len(" ".join(cmd)) < 200 else cmd[:6] + ["..."]), flush=True)
    return subprocess.run(cmd, check=True, **kw)


def upload(out_dir, derivatives_dir, server, remote_dir, keep_old=False, dry_run=False):
    cat, listing = out_dir / PUBLIC_NAME, out_dir / LIST_NAME
    if not cat.exists() or not listing.exists():
        sys.exit(f"Nothing to upload in {out_dir}: run publish.py without --upload-only first.")
    wanted = set(listing.read_text(encoding="utf-8").split("\n")) - {""}
    rd = remote_dir.rstrip("/")
    dry = ["--dry-run"] if dry_run else []
    print(f"Uploading to {server}:{rd}")
    run(["ssh", server, f"mkdir -p '{rd}/derivatives'"])
    print(f"1. {len(wanted):,} derivative files (only new or changed ones are sent)")
    run(["rsync", "-rt", "--stats", *RSYNC_PERMS, *dry, f"--files-from={listing}",
         f"{derivatives_dir}/", f"{server}:{rd}/derivatives/"])
    print(f"2. the catalogue ({human(cat.stat().st_size)})")
    run(["rsync", "-t", "--stats", *RSYNC_PERMS, *dry, str(cat), f"{server}:{rd}/{PUBLIC_NAME}.new"])
    if not dry_run:
        run(["ssh", server, f"mv -f '{rd}/{PUBLIC_NAME}.new' '{rd}/{PUBLIC_NAME}'"])
    if keep_old:
        return
    print("3. removing derivative files the site no longer uses")
    there = run(["ssh", server, f"cd '{rd}/derivatives' && find . -type f -print0"],
                capture_output=True).stdout.decode("utf-8", "surrogateescape").split("\0")
    stale = sorted(p[2:] for p in there if p.startswith("./") and p[2:] not in wanted)
    print(f"   {len(stale):,} to remove")
    if stale and not dry_run:
        run(["ssh", server, f"cd '{rd}/derivatives' && xargs -0 rm -f --"],
            input="\0".join(stale).encode("utf-8", "surrogateescape"))
        run(["ssh", server, f"find '{rd}/derivatives' -mindepth 1 -type d -empty -delete"])


def main():
    P = CONFIG.publish
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--upload", action="store_true", help="build, then upload to the server")
    ap.add_argument("--upload-only", action="store_true", help="upload the last build")
    ap.add_argument("--dry-run", action="store_true", help="upload: show what rsync would send")
    ap.add_argument("--keep-old", action="store_true", help="upload: leave unused images on the server")
    ap.add_argument("-d", "--derivatives", default=str(CONFIG.derivatives_dir or ""),
                    help="the derivatives folder (default: derivatives_dir in config.json)")
    ap.add_argument("--catalogue", default=str(CONFIG.catalogue))
    ap.add_argument("--out", default=P["out_dir"], help="where to build (default: publish.out_dir)")
    ap.add_argument("--server", default=P["server"])
    ap.add_argument("--remote-dir", default=P["remote_dir"])
    a = ap.parse_args()
    out = Path(a.out).expanduser()
    deriv = Path(a.derivatives).expanduser() if a.derivatives else None
    if deriv is None or not deriv.is_dir():
        sys.exit(f"The derivatives folder isn't there ({deriv or 'not set'}): plug in its drive, or use -d.")
    if (a.upload or a.upload_only) and not a.server:
        sys.exit('Say which server to upload to: "publish": {"server": "user@host"} in config.json, or --server.')

    if not a.upload_only:
        if not Path(a.catalogue).exists():
            sys.exit(f"Catalogue not found: {a.catalogue}")
        excl = list(P["exclude_categories"])
        print(f"Building the public catalogue from {a.catalogue}\n  leaving out: {', '.join(excl) or 'nothing'}")
        t0 = time.time()
        c = build_public(a.catalogue, out, excl, deriv,
                         step=lambda m: print(f"  {m} ({time.time() - t0:.0f}s)", flush=True))
        print(f"Public catalogue: {c['distinct']:,} distinct files, {c['copies']:,} copies, "
              f"{human(c['bytes'])}; {c['derivative_files']:,} derivative files.")
        if c["missing_previews"] or c["missing_docs"]:
            print(f"  Not in {deriv}: previews for {c['missing_previews']:,} files, "
                  f"{c['missing_docs']:,} PDF/RTF copies (the site shows an icon instead).")
    if a.upload or a.upload_only:
        upload(out, deriv, a.server, a.remote_dir, keep_old=a.keep_old, dry_run=a.dry_run)
        print("Done." + (" (dry run: nothing was changed on the server)" if a.dry_run else ""))


if __name__ == "__main__":
    main()
