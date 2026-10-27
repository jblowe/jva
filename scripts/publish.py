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
  1. the images and PDF/RTF copies -> "remote_images_dir" (/mnt/images/jva_images), by rsync:
     only what changed is sent
  2. the catalogue -> "remote_data_dir"/archive_public.sqlite (/mnt/images/jva_data), swapped
     in at once, so the site
     never sees half a file; the running app notices the new catalogue by itself
  3. derivative files on the server the new catalogue no longer uses are deleted
     (--keep-old to skip this)
The derivatives folder (derivatives_dir, or -d) must be readable: plug in its drive.
"""
import argparse
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # the repository, for jva
from jva.config import CONFIG  # noqa: E402
from jva.publish import LIST_NAME, PUBLIC_NAME, build_public  # noqa: E402
from jva.catalog import human  # noqa: E402

MARKER = ".jva_images"     # marks the server's images folder as publish.py's own (see upload)
RSYNC_PERMS = ["--chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r"]      # readable by Apache


SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]   # fail at once rather than prompt


class UploadFailed(Exception):
    pass


def run(cmd, output=False, **kw):
    """Run a command, showing it; its errors go straight to the terminal. output=True returns
    what it printed. A failure raises UploadFailed with a readable explanation."""
    if cmd[0] == "ssh":
        cmd = ["ssh", *SSH_OPTS, *cmd[1:]]
    elif cmd[0] == "rsync":                       # rsync logs in the same way
        cmd = ["rsync", "-e", shlex.join(["ssh", *SSH_OPTS]), *cmd[1:]]
    print("  $ " + " ".join(cmd if len(" ".join(cmd)) < 200 else cmd[:8] + ["..."]), flush=True)
    try:
        r = subprocess.run(cmd, check=True, stdout=subprocess.PIPE if output else None, **kw)
    except FileNotFoundError:
        raise UploadFailed(f"{cmd[0]} isn't installed here.")
    except subprocess.CalledProcessError as e:
        if cmd[0] == "ssh" and e.returncode == 255:
            raise UploadFailed("ssh couldn't connect or log in (see its message above).")
        raise UploadFailed(f"{cmd[0]} stopped with exit status {e.returncode} (see its message above).")
    return r.stdout if output else r


def check_server(server):
    """Before spending minutes on a build: can we log in without a password?"""
    print(f"Checking that ssh to {server} works")
    try:
        run(["ssh", server, "true"])
    except UploadFailed as e:
        key = next((SSH_OPTS[k + 1] for k, o in enumerate(SSH_OPTS) if o == "-i"), None)
        by_hand = f"ssh -i '{key}' {server} true" if key else f"ssh {server} true"
        sys.exit(f"{e}\nTry it by hand:  {by_hand}\n"
                 "It must log in without asking anything. If the server needs a key file (an AWS .pem),\n"
                 "set \"ssh_key\" in config.json's \"publish\" section (or use --ssh-key); a Host alias\n"
                 "from ~/.ssh/config works as \"server\" too. Check, too, that the name resolves (the\n"
                 "DNS record for a new site name may not exist yet).")


def upload(out_dir, derivatives_dir, server, data_dir, images_dir, keep_old=False, dry_run=False):
    cat, listing = out_dir / PUBLIC_NAME, out_dir / LIST_NAME
    if not cat.exists() or not listing.exists():
        sys.exit(f"Nothing to upload in {out_dir}: run publish.py without --upload-only first.")
    wanted = set(listing.read_text(encoding="utf-8").split("\n")) - {""}
    dd, im = data_dir.rstrip("/"), images_dir.rstrip("/")
    dry = ["--dry-run"] if dry_run else []
    print(f"Uploading to {server}: catalogue to {dd}, images to {im}")
    # Step 3 deletes files in the images folder, so it must be this archive's own folder, never a
    # shared parent such as /mnt/images. A new or empty folder is marked as ours; a folder with
    # other things in it and no mark is refused.
    state = run(["ssh", server, f"mkdir -p '{dd}' '{im}' && cd '{im}' && "
                 f"if [ -e {MARKER} ]; then echo ours; elif [ -z \"$(ls -A)\" ]; then touch {MARKER} && echo ours; "
                 f"else echo other; fi"], output=True).decode().strip()
    if state != "ours":
        sys.exit(f"{server}:{im} already holds other files, and isn't marked as this archive's images "
                 f"folder ({MARKER}). Check remote_images_dir in config.json: it must be a folder of its "
                 f"own, e.g. /mnt/images/jva_images. If it really is the right one, create {im}/{MARKER} "
                 f"on the server and run this again.")
    print(f"1. {len(wanted):,} derivative files (only new or changed ones are sent)")
    run(["rsync", "-rt", "--stats", *RSYNC_PERMS, *dry, f"--files-from={listing}",
         f"{derivatives_dir}/", f"{server}:{im}/"])
    print(f"2. the catalogue ({human(cat.stat().st_size)})")
    run(["rsync", "-t", "--stats", *RSYNC_PERMS, *dry, str(cat), f"{server}:{dd}/{PUBLIC_NAME}.new"])
    if not dry_run:
        run(["ssh", server, f"mv -f '{dd}/{PUBLIC_NAME}.new' '{dd}/{PUBLIC_NAME}'"])
    if keep_old:
        return
    print(f"3. removing files in {im} the site no longer uses")
    there = run(["ssh", server, f"cd '{im}' && find . -type f -print0"],
                output=True).decode("utf-8", "surrogateescape").split("\0")
    stale = sorted(p[2:] for p in there if p.startswith("./") and p[2:] not in wanted and p[2:] != MARKER)
    print(f"   {len(stale):,} to remove")
    if stale and not dry_run:
        run(["ssh", server, f"cd '{im}' && xargs -0 rm -f --"],
            input="\0".join(stale).encode("utf-8", "surrogateescape"))
        run(["ssh", server, f"find '{im}' -mindepth 1 -type d -empty -delete"])


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
    ap.add_argument("--ssh-key", default=P.get("ssh_key") or "",
                    help="the private key ssh logs in with (default: publish.ssh_key in config.json)")
    ap.add_argument("--remote-data-dir", default=P["remote_data_dir"])
    ap.add_argument("--remote-images-dir", default=P["remote_images_dir"])
    a = ap.parse_args()
    out = Path(a.out).expanduser()
    deriv = Path(a.derivatives).expanduser() if a.derivatives else None
    if deriv is None or not deriv.is_dir():
        sys.exit(f"The derivatives folder isn't there ({deriv or 'not set'}): plug in its drive, or use -d.")
    if (a.upload or a.upload_only) and not a.server:
        sys.exit('Say which server to upload to: "publish": {"server": "user@host"} in config.json, or --server.')

    if a.ssh_key:
        key = Path(a.ssh_key).expanduser()
        if not key.is_file():
            sys.exit(f"The ssh key isn't there: {key}")
        if key.stat().st_mode & 0o077:
            sys.exit(f"ssh won't use {key} while others can read it. Fix with:  chmod 600 '{key}'")
        SSH_OPTS[:0] = ["-i", str(key), "-o", "IdentitiesOnly=yes"]
    if a.upload or a.upload_only:
        check_server(a.server)
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
        try:
            upload(out, deriv, a.server, a.remote_data_dir, a.remote_images_dir, keep_old=a.keep_old,
                   dry_run=a.dry_run)
        except UploadFailed as e:
            sys.exit(f"Upload stopped: {e}\nThe build is kept in {out}; once the problem is fixed, "
                     f"send it with  python3 scripts/publish.py --upload-only")
        print("Done." + (" (dry run: nothing was changed on the server)" if a.dry_run else ""))


if __name__ == "__main__":
    main()
