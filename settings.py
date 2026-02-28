"""
MusicGrabber - Settings Management

Environment variable > DB value > default hierarchy.
"""

import os
from pathlib import Path

from constants import BOT_BACKOFF_MIN_SECONDS, BOT_BACKOFF_MAX_SECONDS, MUSIC_DIR
from db import db_conn


def get_setting(key: str, default: str = "") -> str:
    """Get a setting value. Environment variable takes precedence over DB value."""
    # Check environment variable first (uppercase, with underscores)
    env_key = key.upper().replace(".", "_")
    env_value = os.getenv(env_key)
    if env_value is not None:
        return env_value

    # Fall back to database
    try:
        with db_conn() as conn:
            cursor = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
        if row and row[0] is not None:
            return row[0]
    except Exception:
        pass

    return default


def get_setting_bool(key: str, default: bool = False) -> bool:
    """Get a boolean setting value."""
    value = get_setting(key, str(default).lower())
    return value.lower() in ("true", "1", "yes", "on")


def get_setting_int(key: str, default: int = 0) -> int:
    """Get an integer setting value."""
    value = get_setting(key, str(default))
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def set_setting(key: str, value: str) -> None:
    """Set a setting value in the database."""
    with db_conn() as conn:
        conn.execute("""
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = CURRENT_TIMESTAMP
        """, (key, value, value))
        conn.commit()


def get_all_settings() -> dict:
    """Get all settings from the database."""
    with db_conn() as conn:
        cursor = conn.execute("SELECT key, value FROM settings")
        return {row[0]: row[1] for row in cursor.fetchall()}


# Define which settings are sensitive (should be masked in GET response)
SENSITIVE_SETTINGS = {
    "slskd_pass", "navidrome_pass", "jellyfin_api_key",
    "smtp_pass", "telegram_webhook_url", "api_key", "youtube_cookies"
}

# Define all configurable settings with their types and defaults
SETTINGS_SCHEMA = {
    # General
    "music_dir": {"type": "str", "default": "/music", "env": "MUSIC_DIR"},
    "enable_musicbrainz": {"type": "bool", "default": True, "env": "ENABLE_MUSICBRAINZ"},
    "enable_lyrics": {"type": "bool", "default": True, "env": "ENABLE_LYRICS"},
    "default_convert_to_flac": {"type": "bool", "default": True, "env": "DEFAULT_CONVERT_TO_FLAC"},
    "min_audio_bitrate": {"type": "int", "default": 0, "env": "MIN_AUDIO_BITRATE"},
    "singles_subdir": {"type": "str", "default": "Singles", "env": "SINGLES_SUBDIR"},
    "organise_by_artist": {"type": "bool", "default": True, "env": "ORGANISE_BY_ARTIST"},
    # Soulseek/slskd
    "slskd_url": {"type": "str", "default": "", "env": "SLSKD_URL"},
    "slskd_user": {"type": "str", "default": "", "env": "SLSKD_USER"},
    "slskd_pass": {"type": "str", "default": "", "env": "SLSKD_PASS", "sensitive": True},
    "slskd_downloads_path": {"type": "str", "default": "", "env": "SLSKD_DOWNLOADS_PATH"},
    # Navidrome
    "navidrome_url": {"type": "str", "default": "", "env": "NAVIDROME_URL"},
    "navidrome_user": {"type": "str", "default": "", "env": "NAVIDROME_USER"},
    "navidrome_pass": {"type": "str", "default": "", "env": "NAVIDROME_PASS", "sensitive": True},
    # Jellyfin
    "jellyfin_url": {"type": "str", "default": "", "env": "JELLYFIN_URL"},
    "jellyfin_api_key": {"type": "str", "default": "", "env": "JELLYFIN_API_KEY", "sensitive": True},
    # Notifications
    "notify_on": {"type": "str", "default": "playlists,bulk,errors", "env": "NOTIFY_ON"},
    "telegram_webhook_url": {"type": "str", "default": "", "env": "TELEGRAM_WEBHOOK_URL", "sensitive": True},
    "telegram_convert_to_mp3": {"type": "bool", "default": True, "env": "TELEGRAM_CONVERT_TO_MP3"},
    "smtp_host": {"type": "str", "default": "", "env": "SMTP_HOST"},
    "smtp_port": {"type": "int", "default": 587, "env": "SMTP_PORT"},
    "smtp_user": {"type": "str", "default": "", "env": "SMTP_USER"},
    "smtp_pass": {"type": "str", "default": "", "env": "SMTP_PASS", "sensitive": True},
    "smtp_from": {"type": "str", "default": "", "env": "SMTP_FROM"},
    "smtp_to": {"type": "str", "default": "", "env": "SMTP_TO"},
    "smtp_tls": {"type": "bool", "default": True, "env": "SMTP_TLS"},
    # YouTube
    "youtube_cookies": {"type": "str", "default": "", "env": "YOUTUBE_COOKIES", "sensitive": True},
    "youtube_bot_backoff_min": {"type": "int", "default": BOT_BACKOFF_MIN_SECONDS, "env": "YOUTUBE_BOT_BACKOFF_MIN"},
    "youtube_bot_backoff_max": {"type": "int", "default": BOT_BACKOFF_MAX_SECONDS, "env": "YOUTUBE_BOT_BACKOFF_MAX"},
    # Webhooks
    "webhook_url": {"type": "str", "default": "", "env": "WEBHOOK_URL"},
    # Security
    "api_key": {"type": "str", "default": "", "env": "API_KEY", "sensitive": True},
}


def _get_typed_setting(key: str):
    """Get a setting with proper type conversion based on schema."""
    schema = SETTINGS_SCHEMA.get(key, {"type": "str", "default": ""})
    default = schema["default"]
    if schema["type"] == "bool":
        return get_setting_bool(key, default)
    elif schema["type"] == "int":
        return get_setting_int(key, default)
    return get_setting(key, default)


def _is_env_override(key: str) -> bool:
    """Check if a setting is being overridden by an environment variable."""
    schema = SETTINGS_SCHEMA.get(key, {})
    env_key = schema.get("env", key.upper())
    return os.getenv(env_key) is not None


def get_singles_dir() -> Path:
    """Get the singles download directory. Reads the setting at runtime so changes take effect immediately.

    A value of "." means the music root itself (no subfolder).
    """
    subdir = get_setting("singles_subdir", "Singles").strip() or "Singles"
    if subdir == ".":
        return MUSIC_DIR
    return MUSIC_DIR / subdir


def get_download_dir(artist: str) -> Path:
    """Get the download directory for a track, respecting the organise-by-artist setting.

    When organise_by_artist is True (default):  /music/Singles/Artist Name/
    When organise_by_artist is False:            /music/Singles/
    """
    from utils import sanitize_filename
    base = get_singles_dir()
    if get_setting_bool("organise_by_artist", True):
        return base / sanitize_filename(artist)
    return base
