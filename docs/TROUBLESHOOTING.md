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
