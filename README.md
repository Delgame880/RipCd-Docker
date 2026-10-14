# RipCD

Rip your audio CDs to your NAS from the browser. No terminal, no desktop
application: drop a CD into the drive, open the web UI, and RipCD reads the
table of contents, looks the album up on MusicBrainz, tags everything correctly
and writes it into a folder you choose.

Built for **ZimaOS / CasaOS** and any other Docker host.

```
        +----------------+       +------------------------------+
        |  CD/DVD drive  | ----> |  RipCD container             |
        |  /dev/sr0      |       |  cdparanoia + ffmpeg + web UI |
        +----------------+       +---------------+--------------+
                                               |
                                     /media/Music/<Album Artist>/<Album>/
```

## What it does

* **Automatic disc detection** - a background scanner notices when you insert a
  CD, nothing to click. The album appears in the UI within a few seconds.
* **Metadata from MusicBrainz** - exact disc id lookup first, then a fuzzy TOC
  search, then the barcode. Cover art comes from the Cover Art Archive.
* **You choose where the music goes** - the destination folder is a setting, and
  the built-in folder browser lets you navigate everything mounted into the
  container.
* **7 output formats** - FLAC, ALAC, WAV (lossless), MP3, Ogg Vorbis, Opus, AAC.
* **Readable structure** - `{album_artist}/{album}` for folders and
  `{track:02d} - {title}` for files; both can be customised with templates.
* **Correct tagging** - titles, artists, album artist, track/disc numbers, year,
  genre, label, catalogue number, barcode, plus embedded cover art and ReplayGain
  friendly album tags. A `.m3u8` playlist and a `.ripcd.json` sidecar are written
  next to the files.
* **Rip history and live progress** - per track percentages while ripping,
  activity log, retry/cancel, and an optional auto-rip that even ejects the disc
  when it is done.
* **Optional password** - set `RIPCD_TOKEN` and the UI asks for it once.
* **Demo mode** - `RIPCD_MOCK=1` runs the whole app with simulated discs, handy
  for trying it out without a drive.

Under the hood: Python 3 + FastAPI, `cdparanoia` for reading and `ffmpeg` for
encoding. No other runtime dependencies, nothing is phoned home except
MusicBrainz.

## Quick start

You need a CD/DVD drive attached to the machine (internal SATA or USB) and its
device names.

### 1. Find the device names on the host

```sh
lsscsi -g                 # -> [0:0:0:0] cd/dvd HL-DT-ST ... /dev/sr0 /dev/sg0
ls -l /dev/sr* /dev/sg*   # if lsscsi is not installed
```

Write down both names, for example `/dev/sr0` and `/dev/sg0`.

### 2. Start the container

```yaml
services:
  ripcd:
    image: ghcr.io/delgame880/ripcd-docker:latest
    container_name: ripcd
    restart: unless-stopped
    devices:
      - /dev/sr0:/dev/sr0
      - /dev/sg0:/dev/sg0
    ports:
      - "8080:8080"
    environment:
      - TZ=UTC
      - RIPCD_OUTPUT_DIR=/media/Music
      - PUID=1000
      - PGID=1000
    volumes:
      - /DATA/AppData/ripcd:/config
      - /DATA/Media:/media
```

Or with plain `docker run`:

```sh
docker run -d --name ripcd \
  --restart unless-stopped \
  --device /dev/sr0 --device /dev/sg0 \
  -p 8080:8080 \
  -e PUID=1000 -e PGID=1000 \
  -v /DATA/AppData/ripcd:/config \
  -v /DATA/Media:/media \
  ghcr.io/delgame880/ripcd-docker:latest
```

Then open `http://<your-server>:8080`.

> **Both device nodes matter.** The block device (`/dev/sr0`) is used to read
> the table of contents, while `cdparanoia` needs the SCSI generic device
> (`/dev/sg0`) to read the audio. Passing only `/dev/sr0` is the number one
> cause of "unreadable disc" / "no CD drive found" - see
> [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

### 3. Rip something

1. Insert an audio CD. The disc shows up by itself (Settings → *Poll interval*
   controls how fast the scan is, 5 seconds by default).
2. Check the tracks, tick the ones you want, fix any title you like.
3. Choose the destination folder (Settings → *Save music in*, or click the
   folder button to browse).
4. Press **Rip**. Progress is shown per track, and the finished album appears in
   the history with a link to its folder.

## Installing on ZimaOS

1. Open the ZimaOS dashboard → **App Store** → **Install a custom app**.
2. Paste the compose file from [docker-compose.yml](docker-compose.yml). The
   `x-casaos` block at the bottom is what makes it look like a native app
   (icon, description, port).
3. Adjust the device names, the music share and `PUID`/`PGID` to your user
   before installing - see the *before install* tips in the app dialog.

A copy/paste ready file with comments is in
[docs/INSTALL-ZIMAOS.md](docs/INSTALL-ZIMAOS.md), including how to add RipCD to
the ZimaOS app store later on.

The default values in `docker-compose.yml`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CD_DEVICE` | `/dev/sr0` | the CD/DVD block device |
| `CD_SCSI_DEVICE` | `/dev/sg0` | its SCSI generic sibling |
| `WEBUI_PORT` | `8080` | published web port |
| `MEDIA_DIR` | `/DATA/Media` | host folder mounted as `/media` |
| `APPDATA_DIR` | `/DATA/AppData/ripcd` | host folder mounted as `/config` |
| `PUID` / `PGID` | `0` / `0` | owner of the ripped files (`0` = root) |
| `RIPCD_FORMAT` | `flac` | default output format |
| `RIPCD_TOKEN` | empty | optional password for the UI |
| `RIPCD_MOCK` | `0` | `1` = demo mode, no drive needed |
| `TZ` | `UTC` | timezone for log timestamps |

## Environment variables

Everything can also be configured in the UI; these are the boot defaults.

| Variable | Default | Purpose |
| --- | --- | --- |
| `RIPCD_CONFIG_DIR` | `/config` | settings, history, metadata cache |
| `RIPCD_MEDIA_ROOTS` | `/media` | comma separated list of folders the browser may show |
| `RIPCD_OUTPUT_DIR` | `/media/Music` | where rips are written by default |
| `RIPCD_FORMAT` | `flac` | `flac`, `alac`, `wav`, `mp3`, `ogg`, `opus`, `aac` |
| `RIPCD_POLL_INTERVAL` | `5` | seconds between disc scans |
| `RIPCD_MUSICBRAINZ_CONTACT` | empty | e-mail/URL sent in the MusicBrainz user agent |
| `RIPCD_TOKEN` | empty | password for the web UI (see below) |
| `RIPCD_TEMP_DIR` | empty | working folder for the intermediate WAVs |
| `RIPCD_HOST` / `RIPCD_PORT` | `0.0.0.0` / `8080` | web server binding |
| `RIPCD_LOG_LEVEL` / `RIPCD_DEBUG` | `info` / off | log verbosity |
| `RIPCD_MOCK` (`RIPCD_DEMO`) | off | demo mode with simulated discs |

**Password.** Set `RIPCD_TOKEN` to any value and every request has to carry it:
open `http://server:8080/?token=yourvalue` once, the app then stores a cookie.
The health endpoint stays open so container health checks keep working.

## Using it well

**Folder templates.** `{album_artist}/{album}` (folders) and
`{track:02d} - {title}` (files) are the defaults. Available fields:
`album_artist`, `artist`, `album`, `title`, `track`, `disc`, `year`, `genre`,
`label`, `catalog_number`, `barcode`, `format`, `quality`, `disc_id`.

**Auto-rip.** Enable it in Settings and a detected audio CD starts ripping after
a configurable delay. Perfect for a headless server: insert the CD, walk away,
the disc is ripped and ejected, and files appear in your share.

**Overrides.** If MusicBrainz has the wrong data (or nothing at all), edit the
album artist / album / year / genre fields in the disc view before ripping -
the change applies to that rip only. Per track titles can be edited the same way.

**Paranoia levels.** *Maximum* verifies every sector twice and repairs scratches
(slowest, recommended for old or damaged CDs). *Reduced* turns the intra-read
verification off and is noticeably faster. *None* is a raw read for pristine
discs.

**Reading data discs.** Enhanced CDs (CD-Extra) carry a data track at the end;
RipCD lists the audio tracks and ignores the data part. A pure data CD is
reported as "data" and refused - there is nothing to rip.

## Updating

```sh
docker compose pull && docker compose up -d
```

Settings, history and the metadata cache live in `/config` and survive updates.
The music folder is yours - RipCD only ever adds and never deletes files it did
not create.

## Troubleshooting

The short version is in [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).
Settings → **Diagnostics** in the UI already tells you the most common things:
which devices the container can see, whether `cdparanoia` and `ffmpeg` are
installed, and how much space is left in the destination folder.

Run `docker exec -it ripcd ripcd-diagnose` inside the container for a full
report: who the app runs as, which devices it can see, the raw drive ioctls and
what RipCD makes of them.

| Symptom | First thing to check |
| --- | --- |
| "no optical drive found" | `/dev/sr0` and `/dev/sg0` are passed to the container |
| "unreadable disc" while ripping | the `/dev/sg*` device is missing, or the container user is not in the drive's group |
| Disc not detected automatically | Settings → *Poll interval* and *Autoscan* |
| Files owned by root | set `PUID`/`PGID` to your user |
| No cover art | the release has no art in the Cover Art Archive; add it there or use overrides |
| Host sees `/dev/sr0` but the app never shows a disc | pass both devices with `devices:` (not `-v`), and run as root or add the drive's group |
| Container restarts in a loop | the config volume is not writable |

## Development

Run it from source, no drive needed:

```sh
cd app
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
RIPCD_MOCK=1 python -m ripcd          # demo mode on http://localhost:8080
pytest -q                             # 149 tests, no hardware required
```

Demo mode simulates four discs (`chromatic-night`, `short-ep`, `mixed-cd`,
`data-disc`) and can insert/eject them through the API. Architecture notes are
in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Building the image

```sh
docker build -t ripcd .
docker run --rm -p 8080:8080 -e RIPCD_MOCK=1 ripcd   # demo mode, no drive
```

The image is Debian slim plus `cdparanoia`, `ffmpeg` and the frozen Python
requirements - nothing is downloaded at runtime. Multi-arch images (amd64,
arm64) are built by `.github/workflows/docker.yml` and published to
`ghcr.io/delgame880/ripcd-docker`.

## API

The UI is a static single page app that talks to the same HTTP API you can use
from scripts. Highlights:

| Endpoint | What it does |
| --- | --- |
| `GET /api/state` | everything the UI needs: drives, disc, jobs, settings, logs |
| `GET /api/disc`, `POST /api/disc/refresh` | current disc snapshot (TOC + metadata) |
| `POST /api/preview` | show the folder/file names a rip would produce |
| `POST /api/jobs` | start a rip (`format`, `quality`, `tracks`, `overrides`, `output_dir`) |
| `GET /api/jobs/{id}`, `POST /api/jobs/{id}/cancel`, `POST /api/jobs/{id}/retry` | job handling |
| `GET /api/events` | server sent events: disc, metadata, progress, log |
| `POST /api/drive/eject`, `POST /api/drive/close` | tray control |
| `GET /api/browse?path=/media` | folder browser (confined to `RIPCD_MEDIA_ROOTS`) |

Full list: open `http://server:8080/docs` (the generated OpenAPI page).

## Credits and license

RipCD is a thin, opinionated wrapper around tools that do the hard part:
[cdparanoia](https://www.xiph.org/paranoia/) for reading audio CDs without
clicks, [FFmpeg](https://ffmpeg.org/) for encoding and tagging, and
[MusicBrainz](https://musicbrainz.org/) plus the
[Cover Art Archive](https://coverartarchive.org/) for metadata. The disc id
algorithm is a faithful reimplementation of
[libdiscid](https://github.com/metabrainz/libdiscid) and is verified against its
reference values in the test suite.

MIT licensed - see [LICENSE](LICENSE). MusicBrainz asks API users to identify
themselves; set `RIPCD_MUSICBRAINZ_CONTACT` to your e-mail or homepage and RipCD
will include it in the user agent.
