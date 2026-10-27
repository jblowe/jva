# Putting the archive on the web (jva.johnblowe.com)

The web server gets the public part of the archive only: a copy of the catalogue without
the Class and Unclassified files, the preview images, and the PDF/RTF copies of old Word
files. The originals stay on the archive drives. The search app runs in public mode there,
so Reveal in Finder, Delete and the original files don't exist on the site at all.

```
Mac                                         EC2 (Ubuntu 24.04, Apache2)
---                                         ---------------------------
archive_catalog.sqlite --publish.py-->      /srv/jva-data/archive_public.sqlite
_derivatives/ (images, PDF, RTF) --rsync--> /srv/jva-data/derivatives/
~/GitHub/jva  --git push / git pull-->      /home/ubuntu/GitHub/jva   (code, venv, config.json)

browser --> Apache :443 jva.johnblowe.com
              /derivatives/...  served from /srv/jva-data/derivatives
              everything else   proxied to gunicorn on 127.0.0.1:8007 (systemd unit "jva")
```

## Once: set up the server

1. **DNS.** Add an A record for `jva.johnblowe.com` pointing at the EC2 instance's address
   (the same one the other sites use). Ports 80 and 443 are presumably already open.

2. **Code and Python.**

   ```sh
   sudo apt install python3.12-venv          # if venv isn't there yet
   mkdir -p ~/GitHub && cd ~/GitHub
   git clone https://github.com/jblowe/jva.git
   cd jva
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   cp deploy/server-config.example.json config.json
   ```

3. **The data folder**, which publish.py uploads to:

   ```sh
   sudo mkdir -p /srv/jva-data
   sudo chown ubuntu:ubuntu /srv/jva-data
   ```

   Check there is room: `df -h /srv`. The derivatives folder on the Mac tells you how much
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
  "remote_dir": "/srv/jva-data"
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
cd ~/GitHub/jva && git pull
venv/bin/pip install -r requirements.txt     # only if requirements.txt changed
sudo systemctl restart jva
```

## Restricting access later

The site is open to everyone. To put a password on it, use the commented `<Location />`
block in the Apache file (on the server, in both `jva.johnblowe.com.conf` and the
`-le-ssl.conf` certbot made). An `.htaccess` file isn't enough on its own: it would only
cover the images under `/derivatives/`, not the pages the app makes.
