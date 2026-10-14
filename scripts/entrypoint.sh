#!/usr/bin/env bash
# RipCD container entrypoint.
#
# Responsibilities, in order:
#   1. keep the container alive when the app is not started by the user
#   2. apply UMASK so the ripped files get sane permissions
#   3. optionally drop root privileges (PUID/PGID) *while still being able to
#      read the CD drive*, which needs the user to be in the group that owns
#      /dev/sr* and /dev/sg* inside the container
#   4. complain loudly when the CD drive was not passed into the container -
#      that is by far the most common installation mistake
set -euo pipefail

log() { printf '[ripcd] %s\n' "$*" >&2; }
warn() { printf '[ripcd] WARNING: %s\n' "$*" >&2; }

umask "${UMASK:-022}"

# --------------------------------------------------------------------------- #
# 1. which devices can we see?
# --------------------------------------------------------------------------- #
shopt -s nullglob
SR_DEVICES=(/dev/sr* /dev/scd*)
SG_DEVICES=(/dev/sg*)
shopt -u nullglob

if [ "${#SR_DEVICES[@]}" -eq 0 ]; then
    warn "no CD/DVD block device found in the container."
    warn "add 'devices: [\"/dev/sr0:/dev/sr0\"]' (and \"/dev/sg0:/dev/sg0\") to your compose file"
    warn "or run: docker run --device /dev/sr0 --device /dev/sg0 ..."
elif [ "${#SG_DEVICES[@]}" -eq 0 ]; then
    warn "found ${SR_DEVICES[*]} but no /dev/sg* device."
    warn "cdparanoia needs the SCSI generic device as well, otherwise ripping fails"
    warn "with 'unreadable disc'. Pass the matching /dev/sg* into the container."
else
    log "found drives: ${SR_DEVICES[*]} (generic: ${SG_DEVICES[*]})"
fi

# --------------------------------------------------------------------------- #
# 2. optional privilege drop
# --------------------------------------------------------------------------- #
if [ -n "${PUID:-}" ] && [ -n "${PGID:-}" ] && [ "${PUID}" != "0" ]; then
    RUN_USER="ripcd"

    if ! getent group "${PGID}" >/dev/null; then
        groupadd -g "${PGID}" "${RUN_USER}"
    fi
    RUN_GROUP="$(getent group "${PGID}" | cut -d: -f1)"

    if ! getent passwd "${PUID}" >/dev/null; then
        useradd -u "${PUID}" -g "${RUN_GROUP}" -d /config -s /usr/sbin/nologin -M "${RUN_USER}"
    fi
    RUN_USER="$(getent passwd "${PUID}" | cut -d: -f1)"

    # The drive nodes keep the group of the host. Make sure that group exists
    # here and that our user is a member of it, otherwise reading the CD fails
    # with EACCES even though the device is present.
    for device in "${SR_DEVICES[@]}" "${SG_DEVICES[@]}"; do
        [ -e "${device}" ] || continue
        device_gid="$(stat -c '%g' "${device}")"
        if ! getent group "${device_gid}" >/dev/null; then
            groupadd -g "${device_gid}" "drive${device_gid}"
        fi
        device_group="$(getent group "${device_gid}" | cut -d: -f1)"
        usermod -aG "${device_group}" "${RUN_USER}"
        log "added ${RUN_USER} to group ${device_group} (gid ${device_gid}) for ${device}"
    done

    # /config has to stay writable for the settings and the metadata cache.
    if [ -d /config ] && [ "$(stat -c '%u' /config)" != "${PUID}" ]; then
        chown -R "${PUID}:${PGID}" /config 2>/dev/null || warn "cannot chown /config (bind mount without permissions?)"
    fi

    log "starting ${RUN_USER} (uid ${PUID}, gid ${PGID})"
    exec gosu "${RUN_USER}:${RUN_GROUP}" "$@"
fi

if [ "$(id -u)" = "0" ]; then
    log "starting as root - set PUID/PGID to write files as your own user"
fi
exec "$@"
