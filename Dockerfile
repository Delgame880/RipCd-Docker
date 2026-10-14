# syntax=docker/dockerfile:1
#
# RipCD - rip audio CDs into your music share from a web UI.
#
# Build:
#   docker build -t ghcr.io/delgame880/ripcd-docker:latest .
#
# Run (both device nodes matter, see README):
#   docker run -d --name ripcd -p 8080:8080 \
#     --device /dev/sr0 --device /dev/sg0 \
#     -v /DATA/AppData/ripcd:/config -v /DATA/Media:/media \
#     ghcr.io/delgame880/ripcd-docker:latest

# --------------------------------------------------------------------------- #
# Build stage: resolve the Python dependencies once so the runtime layer stays
# small and rebuilds are fast.
# --------------------------------------------------------------------------- #
FROM python:3.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY app/requirements.txt ./requirements.txt
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# --------------------------------------------------------------------------- #
# Runtime stage
# --------------------------------------------------------------------------- #
FROM python:3.11-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="RipCD" \
      org.opencontainers.image.description="Self-hosted web app that rips audio CDs to FLAC/MP3/AAC with MusicBrainz metadata and cover art" \
      org.opencontainers.image.source="https://github.com/Delgame880/RipCd-Docker" \
      org.opencontainers.image.url="https://github.com/Delgame880/RipCd-Docker" \
      org.opencontainers.image.documentation="https://github.com/Delgame880/RipCd-Docker/blob/main/README.md" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.vendor="Delgame880"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:$PATH" \
    RIPCD_CONFIG_DIR=/config \
    RIPCD_MEDIA_ROOTS=/media \
    RIPCD_OUTPUT_DIR=/media/Music \
    RIPCD_HOST=0.0.0.0 \
    RIPCD_PORT=8080 \
    UMASK=022

# cdparanoia : reads the audio (jitter correction, scratch repair)
# ffmpeg     : encodes, tags and embeds cover art in every supported format
# eject      : fallback for drives whose CDROMEJECT ioctl is not implemented
# gosu       : lets the entrypoint drop root when PUID/PGID are set
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        cdparanoia \
        eject \
        ffmpeg \
        gosu \
        passwd \
        util-linux \
    ; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY app/ /app/
COPY scripts/entrypoint.sh /usr/local/bin/ripcd-entrypoint
RUN chmod 0755 /usr/local/bin/ripcd-entrypoint \
 && mkdir -p /config /media /data

VOLUME ["/config"]
EXPOSE 8080

# The health endpoint is deliberately unauthenticated so this keeps working
# when RIPCD_TOKEN is set.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)" || exit 1

ENTRYPOINT ["/usr/local/bin/ripcd-entrypoint"]
CMD ["python", "-m", "ripcd"]
