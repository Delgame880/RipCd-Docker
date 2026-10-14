# Installing RipCD on ZimaOS

ZimaOS runs Docker, so RipCD is a normal container - the only unusual part is
that the CD drive and the folder where the music should land have to be passed
through.

This guide uses the App Store's **Install a custom app** dialog, which is the
shortest path. If you prefer the terminal, the same file can be used with
`docker compose up -d`.

## 1. Find the drive

SSH into ZimaOS (or use the terminal in the dashboard) and run:

```sh
lsscsi -g
```

Example output:

```
[0:0:0:0]    cd/dvd  HL-DT-ST DVDRAM GP65NB60  PF00  /dev/sr0  /dev/sg0
```

The last two columns are what you need:

* `/dev/sr0` - the block device, used to read the table of contents
* `/dev/sg0` - the SCSI generic device, used by `cdparanoia` to read audio

If `lsscsi` is not installed:

```sh
ls -l /dev/sr* /dev/sg*
cat /proc/sys/dev/cdrom/info     # shows the drive name when the kernel has it
```

**USB drives** are usually `/dev/sr0` + `/dev/sg1` (the number of the generic
device does not have to match). Internal SATA drives are typically `sr0`/`sg0`.
If you have several optical drives, repeat the steps with `sr1`, `sg1`, and so
on.

> Tip: RipCD's *Settings → Diagnostics* panel shows which devices the container
> can see. Install it once with the drive passed through and check there.

## 2. Install the app

Dashboard → **App Store** → the **Install a custom app** button (or *+* in the
app store) → paste this file:

```yaml
name: ripcd

services:
  ripcd:
    image: ghcr.io/delgame880/ripcd-docker:latest
    container_name: ripcd
    restart: unless-stopped
    devices:
      - /dev/sr0:/dev/sr0
      - /dev/sg0:/dev/sg0
    ports:
      - target: 8080
        published: "8080"
        protocol: tcp
    environment:
      - TZ=Europe/Madrid
      - RIPCD_MEDIA_ROOTS=/media
      - RIPCD_OUTPUT_DIR=/media/Music
      - RIPCD_FORMAT=flac
      - PUID=1000
      - PGID=1000
    volumes:
      - /DATA/AppData/ripcd:/config
      - /DATA/Media:/media

x-casaos:
  id: org.delgame880.ripcd
  version: "1.0.0"
  architectures: [amd64, arm64]
  main: ripcd
  author: Delgame880
  developer: Delgame880
  category: Media
  icon: https://raw.githubusercontent.com/Delgame880/RipCd-Docker/main/app/static/icon.svg
  port_map: "8080"
  index: "/"
  title:
    en_US: RipCD
  tagline:
    en_US: Rip your audio CDs to the NAS straight from the browser.
  description:
    en_US: |
      RipCD turns audio CDs into properly tagged music files.
      Automatic disc detection, MusicBrainz metadata and cover art,
      FLAC/MP3/Opus/... output, folder browser, auto-rip and history.
  tips:
    before_install:
      en_US: |
        Pass your CD drive into the container: replace /dev/sr0 and /dev/sg0
        with the names from `lsscsi -g`. Set PUID/PGID to your ZimaOS user.
```

Change before installing:

| What | Why |
| --- | --- |
| `/dev/sr0`, `/dev/sg0` | must match your drive |
| `/DATA/Media` | the share where the music should be written |
| `/DATA/AppData/ripcd` | where the app keeps its settings |
| `PUID`, `PGID` | so the ripped files belong to you, not to root |
| `TZ` | so the activity log shows your local time |
| `published` port | if 8080 is already taken |

How to find your `PUID`/`PGID`: `id` in a ZimaOS terminal, e.g.
`uid=1000(zima) gid=1000(zima)`. ZimaOS uses `1000:1000` by default.

## 3. First run

1. Open `http://<zima-ip>:8080`.
2. Settings → *Save music in*: pick the folder inside `/media` you want
   (the folder browser only shows folders under `RIPCD_MEDIA_ROOTS`).
3. Insert an audio CD. The disc appears by itself after a few seconds.
4. Optional: turn on **Auto-rip** so every inserted CD is ripped and ejected
   without any interaction.

## Optional: password and HTTPS

* Password: add `- RIPCD_TOKEN=some-secret` to the environment and open
  `http://<zima-ip>:8080/?token=some-secret` once. The browser keeps a cookie
  afterwards.
* HTTPS: put RipCD behind the ZimaOS reverse proxy / your own proxy (e.g.
  Nginx Proxy Manager or Traefik; CasaOS/ZimaOS has a "Cloudflare tunnel"
  integration) and forward to `ripcd:8080`.

## Optional: publishing to the ZimaOS app store

If you want RipCD to appear in the store sidebar without pasting YAML:

1. Copy `docker-compose.yml` (the whole file, `x-casaos` block included) and the
   icon into a repository that looks like the
   [CasaOS app store](https://github.com/IceWhaleTech/CasaOS-AppStore):
   `Apps/RipCD/docker-compose.yml` plus a `store-config.json`.
2. Add the repository URL in ZimaOS → App Store → *More → Add source*.
3. Refresh; RipCD then installs like any other app.

## Updating RipCD

```sh
docker compose pull
docker compose up -d
```

In the ZimaOS UI: App Store → *Installed apps* → RipCD → *Update* (or simply
reinstall with the same compose file; the `/config` volume keeps your settings).

## Backups

Everything RipCD knows is in two places:

* `/config` (host: `/DATA/AppData/ripcd`) - settings, history, metadata cache
* your music share - the ripped files

Copy `/DATA/AppData/ripcd` to keep the app's memory, e.g. with
`rsync -a /DATA/AppData/ripcd /path/to/backup/`.
