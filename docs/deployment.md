# Putting the archive on the web (jva.johnblowe.com)

The web server gets the public part of the archive only: a copy of the catalogue without
the Class and Unclassified files, the preview images, and the PDF/RTF copies of old Word
files. The originals stay on the archive drives. The search app runs in public mode there,
so Reveal in Finder, Delete and the original files don't exist on the site at all.

```
Mac                                         EC2 (Ubuntu 24.04, Apache2)
---                                         ---------------------------
archive_catalog.sqlite --publish.py-->      /mnt/images/jva_data/archive_public.sqlite
_derivatives/ (images, PDF, RTF) --rsync--> /mnt/images/jva_images/
~/GitHub/jva  --git push / git pull-->      ~/jva on the server (code, venv, config.json)

browser --> Apache :443 jva.johnblowe.com
              /derivatives/...  served from /mnt/images/jva_images
              everything else   proxied to gunicorn on 127.0.0.1:8007 (systemd unit "jva")
```

## Once: on the Mac

The repository lives at `~/GitHub/jva` on the Mac and `~/jva` on the server; GitHub
(https://github.com/jblowe/jva) is how code gets from one to the other.

```sh
git clone ~/Claude/Projects/Voelker/jva.bundle ~/GitHub/jva     # or from GitHub, once pushed
cd ~/GitHub/jva
git remote set-url origin https://github.com/jblowe/jva.git
cp config.example.json config.json            # check data_dir, derivatives_dir and "publish"
python3 scripts/prepare_search.py             # once, for this version
git push -u origin main                       # so the server can clone it
```

## Once: set up the server

1. **DNS.** Add an A record for `jva.johnblowe.com` pointing at the EC2 instance's address
   (the same one the other sites use). Ports 80 and 443 are presumably already open.

2. **Code and Python.**

   ```sh
   sudo apt install python3.12-venv          # if venv isn't there yet
   git clone https://github.com/jblowe/jva.git ~/jva
   cd ~/jva
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   cp deploy/server-config.example.json config.json
   ```

   The server's `config.json` says where the public catalogue is
   (`/mnt/images/jva_data/archive_public.sqlite`), where the images are
   (`/mnt/images/jva_images`, which Apache serves as `/derivatives`), and that the app runs in
   public mode. The settings are described in `search-app.md`.

3. **The two folders publish.py uploads to**, beside the other image collections in
   `/mnt/images`: `jva_images` for the derivatives and `jva_data` for the catalogue.

   ```sh
   mkdir -p /mnt/images/jva_images /mnt/images/jva_data
   ```

   If `/mnt/images` belongs to root, use `sudo mkdir` and then
   `sudo chown ubuntu:ubuntu /mnt/images/jva_images /mnt/images/jva_data`: publish.py logs in
   as `ubuntu` and must be able to write to both.

   publish.py also creates them if it can, and puts a marker file (`.jva_images`) in the empty
   `jva_images` folder on its first upload. It deletes images the site no longer uses only in
   a folder with that marker, so a mistyped `remote_images_dir` (say, `/mnt/images` itself)
   is refused instead of emptied.

   Apache must be able to read `/mnt/images/jva_images`, as it presumably already reads the
   other collections there.

   Check there is room: `df -h /mnt/images`. The derivatives folder on the Mac tells you how much
   is needed (`du -sh "/Volumes/TAP PROJECT/_derivatives"`); the public share is a bit less.

4. **Publish from the Mac** (next section) so there is something to show.

5. **The app under gunicorn.** Port 8007 is in `deploy/gunicorn.conf.py`; if
   `sudo ss -ltnp | grep 8007` shows it taken, pick another and use it in the Apache file too.

   ```sh
   sudo cp deploy/jva.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now jva
   curl -sI http://127.0.0.1:8007/ | head -1      # HTTP/1.1 200 OK
   journalctl -u jva -n 20                         # if not
   ```

   If the server's user isn't `ubuntu` or the clone is elsewhere, change the paths in
   `jva.service` before copying it.

6. **Apache.**

   ```sh
   sudo cp deploy/jva.johnblowe.com.conf /etc/apache2/sites-available/
   sudo a2enmod proxy proxy_http headers          # already on: no harm
   sudo a2ensite jva.johnblowe.com
   sudo apache2ctl configtest && sudo systemctl reload apache2
   sudo certbot --apache -d jva.johnblowe.com      # HTTPS, and redirect http to it
   ```

   Then open https://jva.johnblowe.com/.

## Every time: publish from the Mac

In `config.json` on the Mac, the `publish` section says what and where:

```json
"publish": {
  "exclude_categories": ["Class", "Unclassified"],
  "server": "ubuntu@jva.johnblowe.com",
  "remote_data_dir": "/mnt/images/jva_data",
  "remote_images_dir": "/mnt/images/jva_images"
}
```

`server` is anything `ssh` accepts, so a Host alias from `~/.ssh/config` (with the EC2 key)
works too. Check that `ssh ubuntu@jva.johnblowe.com true` works without a password prompt.

Plug in the drive with the derivatives folder, then:

```sh
cd ~/GitHub/jva
python3 scripts/publish.py                   # build data/public/archive_public.sqlite; look at it
python3 scripts/publish.py --upload          # build and upload
python3 scripts/publish.py --upload-only --dry-run   # what would be sent, changing nothing
```

To try the public catalogue on the Mac before uploading, point a second config at it:
`"catalogue": ".../public/archive_public.sqlite"` and `"app": {"mode": "public"}`, then
`JVA_CONFIG=that.json python3 webapp/app.py`.

The upload sends only new or changed images (the first one takes a while), swaps the new
catalogue in at once, and then deletes images on the server that the new catalogue no
longer uses (files you deleted, or files that moved into an excluded category). The
running app picks up the new catalogue by itself; no restart.

## Updating the code

On the Mac: commit, then `git push`. On the server:

```sh
cd ~/jva && git pull
venv/bin/pip install -r requirements.txt     # only if requirements.txt changed
sudo systemctl restart jva
```

## Restricting access later

The site is open to everyone. To put a password on it, use the commented `<Location />`
block in the Apache file (on the server, in both `jva.johnblowe.com.conf` and the
`-le-ssl.conf` certbot made). An `.htaccess` file isn't enough on its own: it would only
cover the images under `/derivatives/`, not the pages the app makes.

## If something goes wrong

| What you see | Where to look |
|---|---|
| 502 or 503 from Apache | the app isn't running: `systemctl status jva`, `journalctl -u jva -n 50` |
| The app says the catalogue isn't there | publish with `--upload` first; check `catalogue` in `~/jva/config.json` |
| Pages work but images are broken (403/404) | `ls -l /mnt/images/jva_images` (Apache must be able to read it); `/var/log/apache2/jva_error.log` |
| Icons instead of previews for some files | their images weren't in the derivatives folder when you published; publish.py says how many |
| publish.py stops: "isn't marked" | `remote_images_dir` points at a folder holding other things, see step 3 above |
| `ssh` asks for a password or is refused | set up the EC2 key for `server` in `~/.ssh/config` on the Mac |
