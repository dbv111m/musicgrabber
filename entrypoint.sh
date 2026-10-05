#!/bin/bash

# =============================================================================
# Music Grabber Entrypoint
# =============================================================================
# Supports PUID/PGID environment variables for running as a specific user,
# similar to Linuxserver.io containers and the *arr stack.
# =============================================================================

PUID=${PUID:-0}
PGID=${PGID:-0}
LISTEN_ADDR=${LISTEN_ADDR:-0.0.0.0}
LISTEN_PORT=${LISTEN_PORT:-8080}

# Port zero asks the server for a random port, which makes published ports and
# the health check useless. Refuse it, and other invalid values, before boot work.
if ! [[ "$LISTEN_PORT" =~ ^[0-9]{1,5}$ ]] || (( 10#$LISTEN_PORT < 1 || 10#$LISTEN_PORT > 65535 )); then
    echo "Error: LISTEN_PORT must be a whole number from 1 to 65535 (got '$LISTEN_PORT')." >&2
    exit 1
fi

echo ""
echo "=========================================="
echo "  Music Grabber is starting..."
echo "  Configured bind: ${LISTEN_ADDR}:${LISTEN_PORT}"
echo "  Docker bridge: publish a host port to container port ${LISTEN_PORT}."
echo "  Shared VPN network: publish port ${LISTEN_PORT} on the VPN container, not MusicGrabber."
echo "  Set LISTEN_PORT in the container environment and recreate the container to change it."

# yt-dlp goes stale a great deal faster than this image does. YouTube swaps a
# player client, and a binary that was perfectly happy last month starts handing
# browsers media URLs they are not allowed to stream. So we bring it up to date
# at boot: bounded, so a sulking network delays the start rather than preventing
# it, and never fatal, because yesterday's yt-dlp still beats no MusicGrabber at
# all. Set YTDLP_AUTO_UPDATE=false if you would rather pin whatever shipped.
if [ "${YTDLP_AUTO_UPDATE:-true}" = "true" ]; then
    echo "  yt-dlp: $(yt-dlp --version 2>/dev/null || echo unknown), checking for a newer one..."
    if timeout 45 yt-dlp -U > /dev/null 2>&1; then
        echo "  yt-dlp: now on $(yt-dlp --version 2>/dev/null || echo unknown)"
    else
        echo "  yt-dlp: staying on the installed version (no network, or GitHub is having a moment)"
    fi
fi

# Only do user/group setup if not running as root (PUID/PGID specified)
if [ "$PUID" != "0" ] || [ "$PGID" != "0" ]; then
    echo "  Running as UID: $PUID / GID: $PGID"

    # Create group if it doesn't exist
    if ! getent group musicgrabber > /dev/null 2>&1; then
        groupadd -g "$PGID" musicgrabber
    fi

    # Create user if it doesn't exist
    if ! getent passwd musicgrabber > /dev/null 2>&1; then
        useradd -u "$PUID" -g "$PGID" -d /app -s /bin/bash musicgrabber
    fi

    # Ensure ownership of key directories
    chown -R "$PUID:$PGID" /app /data 2>/dev/null || true

    # Run as the specified user
    echo "=========================================="
    echo ""
    exec gosu musicgrabber uvicorn app:app --host "$LISTEN_ADDR" --port "$LISTEN_PORT"
else
    echo "  Running as root (set PUID/PGID for custom user)"
    echo "=========================================="
    echo ""
    exec uvicorn app:app --host "$LISTEN_ADDR" --port "$LISTEN_PORT"
fi
