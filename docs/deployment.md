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
~/GitHub/jva  --git push / git pull-->      ~/jva on the server (code, config.json)

browser --https--> AWS load balancer (holds the certificate)
                     --http--> Apache :80, vhost jva.johnblowe.com
                                 /derivatives/...  served from /mnt/images/jva_images
                                 everything else   proxied to gunicorn on 127.0.0.1:8007 (unit "jva")
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

1. **DNS and the load balancer.** Point `jva.johnblowe.com` at the AWS load balancer, the
   same way as the other sites on this server. The load balancer handles https with its own
   certificate and passes plain http to Apache, so there is no certificate to set up on the
   server. Check that its certificate covers `jva.johnblowe.com` (e.g. `*.johnblowe.com`), and
   if its listener picks a target by host name, add a rule for `jva.johnblowe.com`.

2. **Code and Python.**

   ```sh
   git clone https://github.com/jblowe/jva.git ~/jva
   cd ~/jva
   ~/venv/bin/pip install -r requirements.txt     # the venv the server's Python apps share
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

4. **Fill the two folders, from the Mac.** This is what `publish.py --upload` does (details in
   the next section); nothing is copied by hand:

   ```sh
   cd ~/GitHub/jva
   python3 scripts/publish.py --upload
   ```

   It builds the public catalogue on the Mac, then copies it to `/mnt/images/jva_data` and
   the preview images and PDF/RTF copies to `/mnt/images/jva_images`, over ssh with rsync.
   Afterwards, on the server, `ls /mnt/images/jva_data` shows `archive_public.sqlite`, and
   `ls /mnt/images/jva_images` shows `thumb` and `preview` folders, plus `pdf` and `rtf`
   for the old Word files. The first upload sends every image, so it takes a while.

5. **The app under gunicorn.** Port 8007 is in `deploy/gunicorn.conf.py`; if
   `sudo ss -ltnp | grep 8007` shows it taken, pick another and use it in the Apache file too.

   ```sh
   sudo cp deploy/jva.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now jva
   curl -sI http://127.0.0.1:8007/ | head -1      # HTTP/1.1 200 OK
   journalctl -u jva -n 20                         # if not
   ```

   If the server's user isn't `ubuntu` or the clone or the venv is elsewhere, change the paths in
   `jva.service` before copying it.

6. **Apache.**

   ```sh
   sudo cp deploy/jva.johnblowe.com.conf /etc/apache2/sites-available/
   sudo a2enmod proxy proxy_http headers          # already on: no harm
   sudo a2ensite jva.johnblowe.com
   sudo apache2ctl configtest && sudo systemctl reload apache2
   curl -sI -H 'Host: jva.johnblowe.com' http://localhost/ | head -1   # HTTP/1.1 200 OK
   ```

   No certbot: the load balancer does https. Then open https://jva.johnblowe.com/.

## Every time: publish from the Mac

In `config.json` on the Mac, the `publish` section says what and where:

```json
"publish": {
  "exclude_categories": ["Class", "Unclassified"],
  "server": "ubuntu@EC2-ADDRESS",
  "ssh_key": "~/.ssh/jblowe.pem",
  "remote_data_dir": "/mnt/images/jva_data",
  "remote_images_dir": "/mnt/images/jva_images"
}
```

`server` is the EC2 instance itself, by its public IP address (put it in place of
`EC2-ADDRESS`), not `jva.johnblowe.com`: that name leads to the load balancer, which only
passes on web traffic.

`ssh_key` is the AWS key file ssh logs in with; publish.py passes it to every ssh and rsync
it runs. Keep it in `~/.ssh` rather than Downloads, readable only by you (ssh refuses it
otherwise):

```sh
mkdir -p ~/.ssh && mv ~/Downloads/jblowe.pem ~/.ssh/ && chmod 600 ~/.ssh/jblowe.pem
ssh -i ~/.ssh/jblowe.pem ubuntu@EC2-ADDRESS true    # must return without asking anything
```

(Answer "yes" if that first login asks whether to trust the server; publish.py can't.)
Alternatively, leave `ssh_key` empty and set `server` to a Host alias in `~/.ssh/config`
that names the key with `IdentityFile`.

Plug in the drive with the derivatives folder, then:

```sh
cd ~/GitHub/jva
python3 scripts/publish.py                   # build only, on the Mac; nothing is sent
python3 scripts/publish.py --upload          # build, then fill jva_data and jva_images on the server
python3 scripts/publish.py --upload-only     # send the last build again, without rebuilding
python3 scripts/publish.py --upload-only --dry-run   # what would be sent, changing nothing
```

Without `--upload`, publish.py only builds, in the `public` folder inside the data folder
(`~/Claude/Projects/Voelker/public`): `archive_public.sqlite`, and `derivatives_files.txt`,
the list of the images that catalogue uses. With `--upload` it then sends, in this order:

| What | From (Mac) | To (server) |
|---|---|---|
| the images and PDF/RTF copies in the list | the derivatives folder (`derivatives_dir`, or `-d`) | `/mnt/images/jva_images` |
| the public catalogue | `public/archive_public.sqlite` | `/mnt/images/jva_data/archive_public.sqlite` |

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
~/venv/bin/pip install -r requirements.txt   # only if requirements.txt changed
sudo systemctl restart jva
```

## Restricting access later

The site is open to everyone. To put a password on it, use the commented `<Location />`
block in the Apache file (`/etc/apache2/sites-available/jva.johnblowe.com.conf` on the
server). An `.htaccess` file isn't enough on its own: it would only
cover the images under `/derivatives/`, not the pages the app makes.

## If something goes wrong

| What you see | Where to look |
|---|---|
| 502 or 503 from Apache | the app isn't running: `systemctl status jva`, `journalctl -u jva -n 50` |
| The app says the catalogue isn't there | publish with `--upload` first; check `catalogue` in `~/jva/config.json` |
| Pages work but images are broken (403 Forbidden) | `sudo tail /var/log/apache2/jva_error.log`. "Permission denied" or "search permissions are missing": Apache can't read the files or enter a folder; `namei -l /mnt/images/jva_images/thumb/...` shows which, and `chmod -R u+rwX,go+rX /mnt/images/jva_images` fixes it (publish.py now does this after each upload). "client denied by server configuration": the request isn't reaching the `<Directory /mnt/images/jva_images>` block; check `sudo apache2ctl -S` that the jva vhost is the one answering, and that it was reloaded after copying |
| Icons instead of previews for some files | their images weren't in the derivatives folder when you published; publish.py says how many |
| publish.py stops: "isn't marked" | `remote_images_dir` points at a folder holding other things, see step 3 above |
| publish.py: "ssh couldn't connect or log in" | try `ssh -i ~/.ssh/jblowe.pem ubuntu@EC2-ADDRESS true`; ssh's message says why: "Permission denied" is the key (`ssh_key`), a timeout is the EC2 security group or the wrong address (`server` must be the instance, not the site name) |
| The site gives the load balancer's error page | the load balancer doesn't send `jva.johnblowe.com` to this server (its listener rules or target group), or Apache isn't answering on port 80 |
