# Development

Everything needed to work on RipCD. No CD drive is required - the test suite and
the demo mode never touch hardware.

## Layout

```
RipCd-Docker/
├── Dockerfile, docker-compose.yml, .env.example   # deployment
├── scripts/entrypoint.sh                          # container entrypoint
├── docs/                                          # this documentation
└── app/
    ├── ripcd/                 # the application package
    │   ├── __main__.py        # python -m ripcd
    │   ├── server.py          # FastAPI app, HTTP API, SSE
    │   ├── config.py          # settings, formats, quality levels, persistence
    │   ├── optical.py         # CD-ROM ioctls, TOC, MusicBrainz disc id
    │   ├── drives.py          # drive discovery and selection
    │   ├── scanner.py         # background disc scanner (+ auto-rip)
    │   ├── extract.py         # cdparanoia invocation and progress parsing
    │   ├── encoder.py         # ffmpeg command lines, tags, covers, progress
    │   ├── proc.py            # cancel-safe subprocess helpers
    │   ├── ripper.py          # the rip pipeline and job state
    │   ├── manager.py         # job queue, history
    │   ├── metadata.py        # MusicBrainz client, matching, caching
    │   ├── artwork.py         # cover art cache
    │   ├── paths.py           # path sandbox, templates, file browser
    │   ├── events.py          # event bus, ring log, SSE encoding
    │   ├── security.py        # token auth, login page
    │   ├── system.py          # tool probing, system info
    │   └── mock.py            # demo mode (drive, discs, metadata, covers)
    ├── static/                # the single page web UI (no build step)
    └── tests/                 # pytest suite
```

The UI is plain HTML/CSS/JS with no bundler and no dependencies: `static/`
is served as-is, which keeps the container small and the upgrade path simple.

## Run from source

```sh
cd app
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

RIPCD_MOCK=1 python -m ripcd        # http://localhost:8080, simulated discs
python -m ripcd                     # real drive (/dev/sr0)
```

Useful variables while developing:

```sh
RIPCD_MOCK=1                     # demo mode: no drive, no network needed
RIPCD_DEBUG=1                    # debug logs
RIPCD_MEDIA_ROOTS=/tmp/music     # where the UI may write
RIPCD_OUTPUT_DIR=/tmp/music/rips
RIPCD_CONFIG_DIR=/tmp/ripcd-cfg  # keep the repo clean
RIPCD_POLL_INTERVAL=2            # faster scans while testing
RIPCD_TOKEN=secret               # exercise the password path
```

## Demo mode

`RIPCD_MOCK=1` replaces the drive, MusicBrainz and the cover art with local
fakes, so the whole application flow (scan → metadata → rip → history) works
offline. It is also what the test suite uses.

Four simulated discs:

| Key | Content |
| --- | --- |
| `chromatic-night` | 12 audio tracks ("The Demo Band") |
| `short-ep` | 3 audio tracks ("Tape Machine") |
| `mixed-cd` | 4 audio tracks + 1 data track (CD-Extra) |
| `data-disc` | data only - must be refused |

Drive control through the API:

```sh
curl -X POST localhost:8080/api/mock/insert \
     -H 'content-type: application/json' \
     -d '{"disc":"mixed-cd","track_seconds":3,"generation_seconds":0.5}'
curl -X POST localhost:8080/api/mock/eject
curl -X POST localhost:8080/api/mock/close
curl localhost:8080/api/mock/status
```

`track_seconds` sets the fake length of every track, `generation_seconds` how
long the fake "read" takes - both make the demo finish in a second or two.

## Tests

```sh
cd app
pytest -q                       # 149 tests, ~15 s
pytest tests/test_optical.py -q # just the drive layer
pytest -k "disc_id" -q
```

| File | Covers |
| --- | --- |
| `test_optical.py` | MSF maths, TOC parsing, disc ids (libdiscid reference vectors), drive discovery |
| `test_extract.py` | cdparanoia command line, progress parsing, WAV verification |
| `test_encoder.py` | ffmpeg command lines for all 7 formats, tag mapping, covers, progress |
| `test_paths.py` | path sandbox, filename sanitising, templates, file browser |
| `test_metadata.py` | release parsing, release selection, overrides, caching |
| `test_ripper.py` | job state, album/track naming, preview, job serialisation |
| `test_api.py` | the whole HTTP API against the demo drive, including a real rip |

`test_api.py` really encodes audio: install `ffmpeg` (the suite skips the rip
tests when it is missing). `cdparanoia` is not needed - the demo drive
generates the WAVs itself.

When adding a feature, prefer a test in the relevant engine file over a HTTP
test; the API tests are deliberately coarse.

## Disc ids

`optical.musicbrainz_disc_id()` implements what libdiscid does:

```
payload = "%02X%02X" % (first, last) + "".join("%08X" % offset
          for offset in [leadout] + tracks)   # padded with zeros up to 100 slots
disc_id = base64(sha1(payload)).replace("+", ".").replace("/", "_").replace("=", "-")
```

Two reference vectors from libdiscid's own test suite are checked on every test
run, so a regression cannot slip through unnoticed:

```
offsets 150,9700,...,288750 with lead-out 303602 (22 tracks)
  -> xUp1F2NkfP8s8jaeFn_Av3jNEI4-
```

Offsets are **absolute MSF frame addresses** (track 1 starts at 150, i.e.
`00:02.00`), not MMC/LBA sectors. `TrackInfo.sector` converts when needed.

## Adding an output format

1. Add the encoder command to `encoder.py` (`codec_arguments()` and, when the
   container needs special handling, `mux_arguments()`).
2. Register it in `config.FORMATS` with label, extension and quality levels.
3. Add the extension to the tag mapping in `encoder.tag_arguments()` if the
   container is not one of the known ones.
4. Extend `tests/test_encoder.py` with the expected command line.
5. Verify against a real ffmpeg: `pytest tests/test_encoder.py -q` plus one
   manual rip in demo mode.

## Trying the real thing without a CD drive

Not possible by design - that is what demo mode is for. If you need to poke at
the ioctl layer, `optical.OpticalDrive` is small enough to be replaced: see
`mock.MockOpticalDrive`, which subclasses it and overrides `_read_toc_ioctl()`,
`_read_toc_cdparanoia()`, `status()`, `disc_status()` and `extract_track()`.

## Building the image

```sh
docker build -t ripcd .
docker run --rm -p 8080:8080 -e RIPCD_MOCK=1 -v $PWD/app:/app/app ripcd
```

The image is `python:3.11-slim-bookworm` plus `cdparanoia`, `ffmpeg`, `eject`
and the two Python packages from `app/requirements.txt`. GitHub Actions builds
`linux/amd64` and `linux/arm64` and pushes to `ghcr.io/delgame880/ripcd-docker`
(`.github/workflows/docker-publish.yml`); a smoke test starts the built image in
demo mode and checks the health endpoint, so a broken image never gets the
`latest` tag.

## Conventions

* Python 3.10+, standard library first. The only runtime dependencies are
  FastAPI and uvicorn - keep it that way.
* Type hints on public functions, dataclasses for data, `logging` (never
  `print`).
* Long-running work is a subprocess or a background thread; the HTTP layer never
  blocks on the drive.
* Every subprocess goes through `proc.py`, so cancellation and process groups
  are handled in one place.
* The API returns JSON only; the UI is served from `static/`.
