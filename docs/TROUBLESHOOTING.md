# Troubleshooting

Start with **Settings → Diagnostics** in the web UI. It shows the four things
that cause almost every problem:

| Row | What it means |
| --- | --- |
| `Devices visible` | which of `/dev/sr0`, `/dev/sg0`, ... exist in the container |
| `sr* in /sys`, `sg* in /sys` | what the kernel exposes |
| `cdparanoia` / `ffmpeg` | both must say *installed* |
| `Free space` | space left in the destination folder |

Logs are in **the activity button (bottom right) → full log**, or:

```sh
docker logs -f ripcd
```

Add `-e RIPCD_DEBUG=1` and restart for verbose logging.

---

## One command that answers most questions

```sh
docker exec -it ripcd ripcd-diagnose
```

It prints who the app runs as, which device nodes exist inside the container,
what the kernel knows about the drive, the raw drive ioctls and RipCD's own
diagnosis - enough to tell a missing device passthrough from a permission
problem. The rest of this file explains what to do with the output.

## "The host sees /dev/sr0 but the app does not"

`lsblk` on the host lists `sr0`, the web UI shows the drive, but no disc ever
appears or every rip fails. Run the command above - the output tells you which
of these three it is:

| What you see in the output | What it means | Fix |
| --- | --- | --- |
| no `/dev/sr*` inside the container | the drive was not passed through | add it to `devices:` (see below) |
| `open failed -> [Errno 13] Permission denied` | the container user may not open the device | run as root (`PUID=0`, `PGID=0`) or add `group_add` |
| `DRIVE_STATUS = 1 (CDS_NO_INFO)` | the drive does not answer the status command properly | update RipCD - from 1.0.0 it reads the TOC anyway |
| `DRIVE_STATUS failed -> [Errno 25] Inappropriate ioctl` | you bind mounted the device (`-v /dev/sr0:/dev/sr0`) instead of passing it | use `devices:` |
| `/dev/sr0` exists, `/dev/sg*` does not | cdparanoia has no way to read audio | pass the `/dev/sg*` device too |

The three device settings, in `docker-compose.yml`:

```yaml
    devices:
      - /dev/sr0:/dev/sr0
      - /dev/sg0:/dev/sg0
    # group_add:
    #   - "24"          # gid of the device on the host, `ls -l /dev/sr0`
    environment:
      - PUID=0          # 0 = root: always allowed to read the drive
      - PGID=0
```

Everything changed in those blocks only takes effect after the container is
recreated - `docker compose up -d --force-recreate` (Docker cannot add a device
to a running container).

## The app says "no optical drive found"

The container cannot see the drive.

```sh
# on the host
lsscsi -g                       # -> [0:0:0:0] cd/dvd ... /dev/sr0 /dev/sg1
docker inspect -f '{{.HostConfig.Devices}}' ripcd
```

The `Devices` list must contain both devices. In compose:

```yaml
    devices:
      - /dev/sr0:/dev/sr0
      - /dev/sg1:/dev/sg1        # note: the sg number can differ from sr!
```

If the drive appears and disappears (USB), re-run `lsscsi -g` after plugging it
in and update the compose file, then `docker compose up -d` to recreate the
container - adding a device to a *running* container is not possible.

## "unreadable disc", or the rip stops at track 1

99 % of the time the SCSI generic device (`/dev/sg*`) is missing or not
readable. cdparanoia needs it; the block device alone is only enough to read
the table of contents.

1. Make sure the matching `/dev/sg*` is in `devices:` (see above).
2. Check the permissions *inside* the container:

   ```sh
   docker exec -it ripcd ls -l /dev/sr* /dev/sg*
   docker exec -it ripcd id          # which user is the app running as?
   ```

3. If the app runs as a normal user (`PUID`/`PGID` set), that user has to be in
   the group that owns the device. RipCD's entrypoint does this automatically
   when the group exists inside the container; if not, add the group id:

   ```yaml
       group_add:
         - "24"          # the gid `ls -l /dev/sr0` shows on the host
   ```

4. Last resort for USB enclosures that need raw SCSI access: run the container
   privileged.

   ```yaml
       privileged: true
   ```

   It works, but it gives the container full access to the host - prefer the
   two options above.

## "no permission to open /dev/sr0"

The device node is inside the container but the user RipCD runs as may not open
it. Two options:

1. Simplest - run as root and use the web UI normally:

   ```yaml
       environment:
         - PUID=0
         - PGID=0
   ```

2. Cleaner - keep your user, give it the drive's group. Find the group on the
   host (`ls -l /dev/sr0` -> `brw-rw---- root cdrom`, so the group id of
   `cdrom`, usually 24) and pass it:

   ```yaml
       group_add:
         - "24"
   ```

   RipCD's entrypoint also tries to add the container user to the group
   automatically; the log line `added ripcd to group cdrom (gid 24) for
   /dev/sr0` means it worked.

## "Cannot open the device" / "Permission denied" while writing files

The container user may not write into the music share.

```sh
docker exec -it ripcd id                       # uid=1000(ripcd) gid=1000 ...
docker exec -it ripcd touch /media/Music/.test && docker exec -it ripcd rm /media/Music/.test
```

Fix by setting the owner:

```yaml
    environment:
      - PUID=1000     # your ZimaOS user, see `id` on the host
      - PGID=1000
```

or by giving the share to that user: `sudo chown -R 1000:1000 /DATA/Media`.

RipCD also refuses paths outside `RIPCD_MEDIA_ROOTS` on purpose - that is what
keeps a mis-click from writing into `/etc`. Add the folder to the variable (and
mount it) if you want to use it.

## My data CD / DVD is not ripped

Correct: RipCD only rips **audio** CDs. A pure data disc is reported as `data`
and refused. An enhanced CD (CD-Extra, with a data track at the end) is
reported as `mixed`; the audio tracks are ripped and the data track is skipped.

## No metadata / wrong album

* Check the internet connection of the container:
  `docker exec -it ripcd python -c "import urllib.request;print(urllib.request.urlopen('https://musicbrainz.org/ws/2/release/?query=test&fmt=json', timeout=10).status)"`
* Set `RIPCD_MUSICBRAINZ_CONTACT` to your e-mail - MusicBrainz throttles
  anonymous clients more aggressively.
* Very new, very obscure or self-made CDs are simply not in the database. Use
  the **Edit tags** button in the disc view to type the album/artist/year
  yourself; those values are used for that rip.
* If the lookup timed out, press **Refresh metadata** - the result is cached in
  `/config/cache` afterwards.

## No cover art

The Cover Art Archive has no image for that release, or the release has been
matched but its art was added after the cache entry was written. Force a new
lookup with **Refresh metadata**, or clear the cache:

```sh
docker exec -it ripcd rm -rf /config/cache/covers
docker restart ripcd
```

## Ripper is slow, or the disc sounds bad

Ripping at maximum paranoia settings is *slower than playback* on purpose - it
reads every sector at least twice. Settings → *Paranoia level*:

* **Maximum** - default, repairs scratched discs, slowest
* **Reduced** - no intra-read verification, roughly 2x faster
* **None** - raw read, only for pristine discs

Reading speed can be limited too (*Read speed*), which helps with drives that
rattle or with old CDs that the drive cannot read faster than 4x.

## The disc is not detected until I press refresh

* Settings → *Autoscan* must be on.
* *Poll interval* is the delay between two checks (5 seconds is a good value;
  it is read from the settings on every loop).
* Some USB bridges only report a disc after the drive has spun up. Give it a few
  seconds, the scanner will pick it up.

## Eject does not work

RipCD first uses the `CDROMEJECT` ioctl, then falls back to the `eject`
command. If both fail, the drive is likely a USB bridge that does not implement
it - eject with the physical button. Ripping does not depend on it.

## The web UI asks for a token

`RIPCD_TOKEN` is set. Open `http://<server>:8080/?token=<value>` once; the app
then stores it in a cookie. To remove the password, unset the variable and
restart. `/api/health` never requires the token, so container health checks keep
working.

## Container restarts in a loop

```sh
docker logs ripcd --tail 50
```

* `PermissionError` on `/config` - the bind mount is not writable by the user
  from `PUID`/`PGID`.
* `Address already in use` - change `WEBUI_PORT`.
* `Error: No such device` when starting - the device names in `devices:` do not
  exist on the host (drive unplugged, or it is `/dev/sr1`).

## I want to test the drive outside the container

```sh
cdparanoia -Q -d /dev/sr0      # prints the TOC if the drive and disc are fine
eject /dev/sr0                 # opens the tray
cat /proc/sys/dev/cdrom/info   # what the kernel thinks
```

If these fail on the host, the container cannot do better.

## Resetting everything

```sh
docker compose down
sudo rm -rf /DATA/AppData/ripcd      # settings, history, caches
docker compose up -d
```

Your music files are never touched by RipCD, only added.
