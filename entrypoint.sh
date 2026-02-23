#!/bin/bash

# =============================================================================
# Music Grabber Entrypoint
# =============================================================================
# Supports PUID/PGID environment variables for running as a specific user,
# similar to Linuxserver.io containers and the *arr stack.
# =============================================================================

PUID=${PUID:-0}
PGID=${PGID:-0}

echo ""
echo "=========================================="
echo "  Music Grabber is starting..."
echo "  Listening on port 8080 (map to your desired host port in docker-compose)"

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

    # Check if Telegram bot should be started
    if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
        echo "  Telegram bot: ENABLED"
    else
        echo "  Telegram bot: DISABLED (set TELEGRAM_BOT_TOKEN to enable)"
    fi

    echo "=========================================="
    echo ""
    exec gosu musicgrabber uvicorn app:app --host 0.0.0.0 --port 8080
else
    echo "  Running as root (set PUID/PGID for custom user)"

    # Check if Telegram bot should be started
    if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
        echo "  Telegram bot: ENABLED"
    else
        echo "  Telegram bot: DISABLED (set TELEGRAM_BOT_TOKEN to enable)"
    fi

    echo "=========================================="
    echo ""
    exec uvicorn app:app --host 0.0.0.0 --port 8080
fi
