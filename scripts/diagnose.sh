#!/bin/sh
# RipCD diagnostics - print everything needed to find out why the CD drive is
# not usable inside the container.  Run it with:
#
#     docker exec -it ripcd ripcd-diagnose
#
# Send the whole output when asking for help.

set -eu

line() { printf '\n=== %s ===\n' "$*"; }

line "who we are"
id
printf 'python: %s\n' "$(python -c 'import sys; print(sys.version.split()[0])')"

line "files in /dev"
for pattern in /dev/sr* /dev/scd* /dev/cdrom* /dev/dvd* /dev/sg*; do
    for device in $pattern; do
        [ -e "$device" ] || continue
        ls -l "$device"
    done
done
if ! ls /dev/sr* /dev/sg* >/dev/null 2>&1; then
    printf '%s\n' 'no /dev/sr* or /dev/sg* device node in the container'
    printf '%s\n' '-> the drive was not passed through:'
    printf '%s\n' '   devices:' '     - /dev/sr0:/dev/sr0' '     - /dev/sg0:/dev/sg0'
fi

line "what the kernel knows"
if [ -r /proc/sys/dev/cdrom/info ]; then
    cat /proc/sys/dev/cdrom/info
else
    printf '/proc/sys/dev/cdrom/info is not available\n'
fi
printf 'sr* in /sys: %s\n' "$(ls /sys/class/block/sr* 2>/dev/null | tr '\n' ' ' || printf none)"
printf 'sg* in /sys: %s\n' "$(ls /sys/class/scsi_generic/sg* 2>/dev/null | tr '\n' ' ' || printf none)"

line "tools"
for tool in cdparanoia ffmpeg eject; do
    if command -v "$tool" >/dev/null 2>&1; then
        printf '%-12s %s\n' "$tool" "$(command -v "$tool")"
    else
        printf '%-12s MISSING\n' "$tool"
    fi
done

line "talking to the drives (raw ioctls)"
python - <<'PY'
import errno, fcntl, os

CDROM_DRIVE_STATUS = 0x5326
CDROM_DISC_STATUS = 0x5327
STATUS = {1: "CDS_NO_INFO", 2: "CDS_NO_DISC", 3: "CDS_TRAY_OPEN",
          4: "CDS_DRIVE_NOT_READY", 5: "CDS_DISC_OK"}
DISC = {100: "CDS_NO_INFO", 101: "data", 102: "data", 103: "audio",
        104: "mixed", 105: "mixed", 106: "mixed"}

found = False
for path in ("/dev/sr0", "/dev/sr1", "/dev/sg0", "/dev/sg1"):
    if not os.path.exists(path):
        continue
    found = True
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        print(f"{path}: open failed -> {exc}")
        continue
    for name, request, names in (("DRIVE_STATUS", CDROM_DRIVE_STATUS, STATUS),
                                 ("DISC_STATUS", CDROM_DISC_STATUS, DISC)):
        try:
            value = fcntl.ioctl(fd, request, 0)
            print(f"{path}: {name} = {value} ({names.get(value, 'unknown')})")
        except OSError as exc:
            print(f"{path}: {name} failed -> {exc}")
    os.close(fd)

if not found:
    print("no device node to test")
PY

line "what RipCD makes of it"
python - <<'PY'
import json
try:
    from ripcd.optical import (
        OpticalDrive, device_diagnosis, device_tree_snapshot, find_device_paths,
    )
except ImportError as exc:
    print(f"cannot import ripcd: {exc}")
    raise SystemExit(0)

drives = []
for path in find_device_paths():
    drive = OpticalDrive(path)
    status = drive.status()
    drives.append({"device": path, "status": status, "has_disc": status == "disc_ok"})
    print(f"{path}: status={status}"
          + (f" ({drive.last_error})" if drive.last_error else ""))
    if status in {"disc_ok", "unknown"}:
        toc = drive.read_toc()
        if toc is None:
            print(f"{path}: no table of contents (drive.last_error={drive.last_error!r})")
        else:
            print(f"{path}: {len(toc.tracks)} track(s) via {toc.source}, "
                  f"{toc.total_time} total, kind={toc.kind}")

print()
print(json.dumps(device_diagnosis(device_tree_snapshot(), drives), indent=2))
PY

line "the app's own view"
python - <<'PY'
import json, urllib.request
for path in ("/api/health", "/api/drives"):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8080{path}", timeout=5) as response:
            data = json.load(response)
    except Exception as exc:
        print(f"{path}: {exc}")
        continue
    if path.endswith("health"):
        print(f"health: {data.get('status')} missing_tools={data.get('missing_tools')}")
    else:
        print("drives:", json.dumps(data.get("drives"), indent=2))
        print("diagnosis:", json.dumps(data.get("diagnosis"), indent=2))
PY

printf '\nDone. If the drive does not show up above, the problem is the container\n'
printf 'configuration, not the application.\n'
