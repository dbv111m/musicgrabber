"""
MusicGrabber - Settings Management

Environment variable > DB value > default hierarchy.
Per-user settings layer sits between env vars and global DB values.
"""

import contextlib
import os
import shutil
from pathlib import Path

from constants import (
    BOT_BACKOFF_MIN_SECONDS, BOT_BACKOFF_MAX_SECONDS,
    TIMEOUT_SPOTIFY_BROWSER, SPOTIFY_BROWSER_STALL_SECONDS,
    MUSIC_DIR, DB_PATH,
    MONOCHROME_HIFI_API_URL,
    QBDLX_FALLBACK_ENABLED, MONOCHROME_BROWSER_FALLBACK_ENABLED,
)
from db import db_conn


# Settings whose environment variable has been renamed. The old spelling still
# works so nobody's docker-compose quietly changes behaviour on upgrade; the
# current spelling takes precedence when both are present.
LEGACY_ENV_ALIASES = {
    "default_convert_audio": "DEFAULT_CONVERT_TO_FLAC",
}


def get_setting(key: str, default: str = "", user_id: str | None = None) -> str:
    """Get a setting value.

    Lookup order:
    1. Environment variable (always wins)
    2. user_settings table (if user_id provided and key is a user-scoped setting)
    3. Global settings table
    4. Schema default / provided default
    """
    # Check environment variable first (uppercase, with underscores)
    env_key = key.upper().replace(".", "_")
    env_value = os.getenv(env_key)
    if env_value is not None:
        return env_value

    # Then the pre-rename name, so a compose file written for an older release
    # keeps doing what its author intended. The current name wins if both are set.
    legacy_env_key = LEGACY_ENV_ALIASES.get(key)
    if legacy_env_key:
        env_value = os.getenv(legacy_env_key)
        if env_value is not None:
            return env_value

    # Per-user setting (only for user-scoped keys when a user_id is given)
    if user_id and key in USER_SETTINGS_KEYS:
        try:
            with db_conn() as conn:
                row = conn.execute(
                    "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
                    (user_id, key)
                ).fetchone()
            if row and row[0] is not None:
                return row[0]
        except Exception:
            pass
        # Private keys don't inherit from global settings; new users start blank.
        if key in USER_PRIVATE_KEYS:
            return default

    # Fall back to global database
    try:
        with db_conn() as conn:
            cursor = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
        if row and row[0] is not None:
            return row[0]
    except Exception:
        pass

    return default


def get_setting_bool(key: str, default: bool = False, user_id: str | None = None) -> bool:
    value = get_setting(key, str(default).lower(), user_id=user_id)
    return value.lower() in ("true", "1", "yes", "on")


def get_setting_int(key: str, default: int = 0, user_id: str | None = None) -> int:
    value = get_setting(key, str(default), user_id=user_id)
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def set_setting(key: str, value: str) -> None:
    """Set a global setting value in the database."""
    with db_conn() as conn:
        conn.execute("""
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = CURRENT_TIMESTAMP
        """, (key, value, value))
        conn.commit()


def set_user_setting(user_id: str, key: str, value: str) -> None:
    """Set a per-user setting in the user_settings table."""
    with db_conn() as conn:
        conn.execute("""
            INSERT INTO user_settings (user_id, key, value, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, key) DO UPDATE SET value = ?, updated_at = CURRENT_TIMESTAMP
        """, (user_id, key, value, value))
        conn.commit()


def get_user_settings(user_id: str) -> dict:
    """Get all settings for a specific user from user_settings table."""
    with db_conn() as conn:
        cursor = conn.execute(
            "SELECT key, value FROM user_settings WHERE user_id = ?", (user_id,)
        )
        return {row[0]: row[1] for row in cursor.fetchall()}


def get_all_settings(user_id: str | None = None) -> dict:
    """Get all settings from the database. If user_id given, user_settings override global ones."""
    with db_conn() as conn:
        cursor = conn.execute("SELECT key, value FROM settings")
        result = {row[0]: row[1] for row in cursor.fetchall()}

    if user_id:
        with db_conn() as conn:
            cursor = conn.execute(
                "SELECT key, value FROM user_settings WHERE user_id = ?", (user_id,)
            )
            for row in cursor.fetchall():
                result[row[0]] = row[1]

    return result


# Define which settings are sensitive (should be masked in GET response)
SENSITIVE_SETTINGS = {
    "slskd_pass", "navidrome_pass", "jellyfin_api_key", "plex_token", "lidarr_api_key",
    "smtp_pass", "telegram_webhook_url", "api_key", "youtube_cookies",
    "spotify_cookies", "apple_music_user_token",
}

# Settings that belong to each user (stored in user_settings table)
USER_SETTINGS_KEYS = {
    "singles_subdir", "playlists_subdir", "albums_subdir", "organise_by_artist", "include_track_number_in_filename", "auto_album_singles", "auto_album_singles_use_albums_dir", "playlist_album_as_name",
    "audio_format", "mp3_bitrate", "opus_bitrate", "alac_bitrate", "normalise_lossy_audio", "auto_import_dir",
    "enable_replaygain", "replaygain_replace_existing",
    "navidrome_url", "navidrome_user", "navidrome_pass", "navidrome_dupe_check",
    "jellyfin_url", "jellyfin_api_key",
    "plex_url", "plex_token",
    "lidarr_url", "lidarr_api_key",
    "notify_on", "telegram_webhook_url", "apprise_url",
    "smtp_host", "smtp_port", "smtp_user", "smtp_pass", "smtp_from", "smtp_to", "smtp_tls",
    "youtube_cookies",
    "spotify_cookies", "spotify_cookies_expired",
    "apple_music_user_token",
    "webhook_url",
}

# These user-scoped keys are personal credentials; a new user with no explicit value
# should get a blank default rather than inheriting whatever the global setting says.
# (Navidrome/Jellyfin/Plex are NOT in this set: shared server, shared library.)
USER_PRIVATE_KEYS = {
    "notify_on", "telegram_webhook_url", "apprise_url",
    "smtp_host", "smtp_port", "smtp_user", "smtp_pass", "smtp_from", "smtp_to", "smtp_tls",
    "webhook_url",
    "youtube_cookies",
    "spotify_cookies", "spotify_cookies_expired",
    "apple_music_user_token",
}

# Define all configurable settings with their types and defaults
SETTINGS_SCHEMA = {
    # General
    "music_dir": {"type": "str", "default": "/music", "env": "MUSIC_DIR"},
    "enable_musicbrainz": {"type": "bool", "default": True, "env": "ENABLE_MUSICBRAINZ"},
    "enable_deezer_metadata": {"type": "bool", "default": True, "env": "ENABLE_DEEZER_METADATA"},
    "enable_lyrics": {"type": "bool", "default": True, "env": "ENABLE_LYRICS"},
    "default_convert_audio": {"type": "bool", "default": False, "env": "DEFAULT_CONVERT_AUDIO"},
    "audio_format": {"type": "str", "default": "opus", "env": "AUDIO_FORMAT"},
    "mp3_bitrate": {"type": "str", "default": "v2", "env": "MP3_BITRATE"},
    "opus_bitrate": {"type": "str", "default": "256k", "env": "OPUS_BITRATE"},
    "alac_bitrate": {"type": "str", "default": "lossless", "env": "ALAC_BITRATE"},
    "min_audio_bitrate": {"type": "int", "default": 0, "env": "MIN_AUDIO_BITRATE"},
    # Reject live versions. Off by default; opt-in, and deliberately so since it's
    # destructive (bins a confidently-identified live recording and tries again).
    "reject_live_versions": {"type": "bool", "default": False, "env": "REJECT_LIVE_VERSIONS"},
    # Bake EBU R128 loudness normalisation into downloads from lossy web sources
    # (YouTube et al are all over the shop volume-wise). Lossless sources are never
    # touched; their masters stay exactly as mastered. Off by default.
    "normalise_lossy_audio": {"type": "bool", "default": False, "env": "NORMALISE_LOSSY_AUDIO"},
    # Write ReplayGain 2.0 tags on new downloads. Unlike the setting above this
    # never touches a single audio sample; it measures the file and writes the
    # numbers, leaving the player to do the actual turning-down. Off by default.
    "enable_replaygain": {"type": "bool", "default": False, "env": "ENABLE_REPLAYGAIN"},
    # By default existing ReplayGain tags are left well alone, on the grounds
    # that whoever wrote them probably meant it. Turn this on to overwrite.
    "replaygain_replace_existing": {"type": "bool", "default": False, "env": "REPLAYGAIN_REPLACE_EXISTING"},
    # Copy each finished download into this folder (e.g. a mounted macOS Music
    # "Automatically Add to Music" folder) so it imports itself. Empty = off.
    "auto_import_dir": {"type": "str", "default": "", "env": "AUTO_IMPORT_DIR"},
    # Stamp watched-playlist names into the COMMENT tag so macOS Music can build
    # smart playlists off them. Off by default; it writes to every playlist file.
    "playlist_comment_tagging": {"type": "bool", "default": False, "env": "PLAYLIST_COMMENT_TAGGING"},
    # Watched playlists and artists re-fetch a track whose file has vanished from
    # the library. Grand when you have binned a duff rip on purpose; considerably
    # less grand when an external organiser (MusicBee, beets and friends) moves or
    # renames files out from under us, and every refresh dutifully downloads them
    # all over again, forever. Turn this off and a track downloaded once stays
    # downloaded; the Missing panel's per-track Retry is still there if you want
    # one back. On by default, so nothing changes unless you say so.
    "redownload_missing_files": {"type": "bool", "default": True, "env": "REDOWNLOAD_MISSING_FILES"},
    # Track upgrades (Lidarr-style). Off by default; opt-in. The scan flags library
    # files sitting below the quality you already download at, ready for a manual upgrade.
    "enable_track_upgrades": {"type": "bool", "default": False, "env": "ENABLE_TRACK_UPGRADES"},
    "upgrade_scan_interval_hours": {"type": "int", "default": 24, "env": "UPGRADE_SCAN_INTERVAL_HOURS"},
    "singles_subdir": {"type": "str", "default": "Singles", "env": "SINGLES_SUBDIR"},
    "playlists_subdir": {"type": "str", "default": "", "env": "PLAYLISTS_SUBDIR"},
    "albums_subdir": {"type": "str", "default": "Albums", "env": "ALBUMS_SUBDIR"},
    "organise_by_artist": {"type": "bool", "default": True, "env": "ORGANISE_BY_ARTIST"},
    "include_track_number_in_filename": {"type": "bool", "default": False, "env": "INCLUDE_TRACK_NUMBER_IN_FILENAME"},
    "auto_album_singles": {"type": "bool", "default": False, "env": "AUTO_ALBUM_SINGLES"},
    "auto_album_singles_use_albums_dir": {"type": "bool", "default": False, "env": "AUTO_ALBUM_SINGLES_USE_ALBUMS_DIR"},
    "playlist_album_as_name": {"type": "bool", "default": False, "env": "PLAYLIST_ALBUM_AS_NAME"},
    "singles_only_mode": {"type": "bool", "default": False, "env": "SINGLES_ONLY_MODE"},
    "file_permissions": {"type": "str", "default": "666", "env": "FILE_PERMISSIONS"},
    # Soulseek/slskd
    "slskd_url": {"type": "str", "default": "", "env": "SLSKD_URL"},
    "slskd_user": {"type": "str", "default": "", "env": "SLSKD_USER"},
    "slskd_pass": {"type": "str", "default": "", "env": "SLSKD_PASS", "sensitive": True},
    "slskd_downloads_path": {"type": "str", "default": "", "env": "SLSKD_DOWNLOADS_PATH"},
    # Off by default: taking the file out of slskd's completed folder also takes it
    # out of what you share back to Soulseek, which is a decision for the user, not us.
    "slskd_move_completed": {"type": "bool", "default": False, "env": "SLSKD_MOVE_COMPLETED"},
    # Navidrome
    "navidrome_url": {"type": "str", "default": "", "env": "NAVIDROME_URL"},
    "navidrome_user": {"type": "str", "default": "", "env": "NAVIDROME_USER"},
    "navidrome_pass": {"type": "str", "default": "", "env": "NAVIDROME_PASS", "sensitive": True},
    # Jellyfin
    "jellyfin_url": {"type": "str", "default": "", "env": "JELLYFIN_URL"},
    "jellyfin_api_key": {"type": "str", "default": "", "env": "JELLYFIN_API_KEY", "sensitive": True},
    # Plex
    "plex_url": {"type": "str", "default": "", "env": "PLEX_URL"},
    "plex_token": {"type": "str", "default": "", "env": "PLEX_TOKEN", "sensitive": True},
    # Lidarr
    "lidarr_url": {"type": "str", "default": "", "env": "LIDARR_URL"},
    "lidarr_api_key": {"type": "str", "default": "", "env": "LIDARR_API_KEY", "sensitive": True},
    # Duplicate checking
    "skip_dupes": {"type": "bool", "default": True, "env": "SKIP_DUPES"},
    "navidrome_dupe_check": {"type": "bool", "default": True, "env": "NAVIDROME_DUPE_CHECK"},
    # Notifications
    "notify_on": {"type": "str", "default": "playlists,bulk,errors", "env": "NOTIFY_ON"},
    "telegram_webhook_url": {"type": "str", "default": "", "env": "TELEGRAM_WEBHOOK_URL", "sensitive": True},
    "apprise_url": {"type": "str", "default": "", "env": "APPRISE_URL"},
    "smtp_host": {"type": "str", "default": "", "env": "SMTP_HOST"},
    "smtp_port": {"type": "int", "default": 587, "env": "SMTP_PORT"},
    "smtp_user": {"type": "str", "default": "", "env": "SMTP_USER"},
    "smtp_pass": {"type": "str", "default": "", "env": "SMTP_PASS", "sensitive": True},
    "smtp_from": {"type": "str", "default": "", "env": "SMTP_FROM"},
    "smtp_to": {"type": "str", "default": "", "env": "SMTP_TO"},
    "smtp_tls": {"type": "bool", "default": True, "env": "SMTP_TLS"},
    # AcoustID fingerprinting
    "acoustid_api_key": {"type": "str", "default": "0NILMQojj4", "env": "ACOUSTID_API_KEY"},
    # Search sources
    "source_youtube_enabled": {"type": "bool", "default": True, "env": "SOURCE_YOUTUBE_ENABLED"},
    "source_mp3phoenix_enabled": {"type": "bool", "default": False, "env": "SOURCE_MP3PHOENIX_ENABLED"},
    "source_soundcloud_enabled": {"type": "bool", "default": True, "env": "SOURCE_SOUNDCLOUD_ENABLED"},
    "source_zvu4no_enabled": {"type": "bool", "default": True, "env": "SOURCE_ZVU4NO_ENABLED"},
    "source_freemp3cloud_enabled": {"type": "bool", "default": True, "env": "SOURCE_FREEMP3CLOUD_ENABLED"},
    "source_soulseek_enabled": {"type": "bool", "default": False, "env": "SOURCE_SOULSEEK_ENABLED"},
    "source_monochrome_enabled": {"type": "bool", "default": True, "env": "SOURCE_MONOCHROME_ENABLED"},
    "source_offline_fallback": {"type": "bool", "default": True, "env": "SOURCE_OFFLINE_FALLBACK"},
    "youtube_requested_video_fallback": {"type": "bool", "default": False, "env": "YOUTUBE_REQUESTED_VIDEO_FALLBACK"},
    "source_health_checks_enabled": {"type": "bool", "default": True, "env": "SOURCE_HEALTH_CHECKS_ENABLED"},
    "source_health_check_interval_minutes": {"type": "int", "default": 10, "env": "SOURCE_HEALTH_CHECK_INTERVAL_MINUTES"},
    "source_health_cooldown_minutes": {"type": "int", "default": 10, "env": "SOURCE_HEALTH_COOLDOWN_MINUTES"},
    "search_concurrency": {"type": "int", "default": 1, "env": "SEARCH_CONCURRENCY"},
    "monochrome_hifi_api_url": {"type": "str", "default": MONOCHROME_HIFI_API_URL, "env": "MONOCHROME_HIFI_API_URL"},
    "monochrome_qbdlx_fallback_enabled": {"type": "bool", "default": QBDLX_FALLBACK_ENABLED, "env": "QBDLX_FALLBACK_ENABLED"},
    "monochrome_browser_fallback_enabled": {"type": "bool", "default": MONOCHROME_BROWSER_FALLBACK_ENABLED, "env": "MONOCHROME_BROWSER_FALLBACK_ENABLED"},
    # YouTube
    "youtube_cookies": {"type": "str", "default": "", "env": "YOUTUBE_COOKIES", "sensitive": True},
    "youtube_bot_backoff_min": {"type": "int", "default": BOT_BACKOFF_MIN_SECONDS, "env": "YOUTUBE_BOT_BACKOFF_MIN"},
    # Spotify
    "spotify_cookies": {"type": "str", "default": "", "sensitive": True},
    "spotify_cookies_expired": {"type": "bool", "default": False},
    # Apple Music
    "apple_music_user_token": {"type": "str", "default": "", "sensitive": True},
    "youtube_bot_backoff_max": {"type": "int", "default": BOT_BACKOFF_MAX_SECONDS, "env": "YOUTUBE_BOT_BACKOFF_MAX"},
    "spotify_browser_timeout_seconds": {
        "type": "int",
        "default": TIMEOUT_SPOTIFY_BROWSER,
        "env": "SPOTIFY_BROWSER_TIMEOUT_SECONDS",
    },
    "spotify_browser_stall_seconds": {
        "type": "int",
        "default": SPOTIFY_BROWSER_STALL_SECONDS,
        "env": "SPOTIFY_BROWSER_STALL_SECONDS",
    },
    # Webhooks
    "webhook_url": {"type": "str", "default": "", "env": "WEBHOOK_URL"},
    # Downloads
    "max_concurrent_downloads": {"type": "int", "default": 3, "env": "MAX_CONCURRENT_DOWNLOADS"},
    # Security
    "api_key": {"type": "str", "default": "", "env": "API_KEY", "sensitive": True},
}


def _get_typed_setting(key: str, user_id: str | None = None):
    """Get a setting with proper type conversion based on schema."""
    schema = SETTINGS_SCHEMA.get(key, {"type": "str", "default": ""})
    default = schema["default"]
    if schema["type"] == "bool":
        return get_setting_bool(key, default, user_id=user_id)
    elif schema["type"] == "int":
        return get_setting_int(key, default, user_id=user_id)
    return get_setting(key, default, user_id=user_id)


def _is_env_override(key: str) -> bool:
    """Check if a setting is being overridden by an environment variable."""
    schema = SETTINGS_SCHEMA.get(key, {})
    env_key = schema.get("env", key.upper())
    if os.getenv(env_key) is not None:
        return True
    # A renamed setting is still locked when only the old env var is set, otherwise
    # the UI would offer an editable field that the environment silently overrules.
    legacy_env_key = LEGACY_ENV_ALIASES.get(key)
    return bool(legacy_env_key and os.getenv(legacy_env_key) is not None)


def _join_within_music_dir(music_dir: Path, subdir: str, label: str) -> Path:
    """Join a user-supplied subfolder onto the music root, and insist it stays there.

    Every one of these subdir strings is typed by a user, so "Singles" and
    "../../etc" arrive through exactly the same door. pathlib is no help on its
    own: joining an absolute path throws the root away entirely, and ".." hops
    are cheerfully honoured.

    Both sides are resolved before comparing, because a lexical prefix check
    believes whatever a symlink tells it. The path handed back is the unresolved
    join, though, so installs where /music is itself a symlink keep the paths
    they already have in the database.

    Raises ValueError rather than quietly rewriting the value; silently writing
    somewhere other than where you were asked to is how libraries go missing.
    """
    candidate = music_dir / subdir
    root_resolved = music_dir.resolve()
    candidate_resolved = candidate.resolve()
    if candidate_resolved != root_resolved and root_resolved not in candidate_resolved.parents:
        raise ValueError(
            f"{label} must stay inside the music directory "
            f"({subdir!r} lands at {candidate_resolved})"
        )
    return candidate


def _safe_library_dir(music_dir: Path, subdir: str, label: str, default: str) -> Path:
    """Contained join for a *settings*-derived folder, falling back if it escapes.

    These three run at import time (app.py makes the Singles directory before it
    does anything else), so raising here would stop a container booting over a
    setting nobody can reach the UI to fix. A stored value that escapes is a
    misconfiguration rather than an attack in progress, so we grumble loudly and
    use the default, which is still safely inside the music root.

    A per-playlist custom folder is different: that one raises, because it
    arrives with a specific job and that job should fail rather than land
    somewhere else entirely.
    """
    try:
        return _join_within_music_dir(music_dir, subdir, label)
    except ValueError as e:
        print(f"[settings] {e}; using the default {default!r} instead")
        return music_dir / default


def get_singles_dir(user_id: str | None = None) -> Path:
    """Get the singles download directory for a user (or global default).

    A value of "." means the music root itself (no subfolder).
    """
    music_dir = Path(get_setting("music_dir", str(MUSIC_DIR), user_id=user_id))
    subdir = get_setting("singles_subdir", "Singles", user_id=user_id).strip() or "Singles"
    if subdir == ".":
        return music_dir
    return _safe_library_dir(music_dir, subdir, "Singles folder", "Singles")


def resolve_custom_subdir(custom_subdir: str, user_id: str | None = None) -> Path:
    """Resolve a per-playlist custom subdir string to an absolute path under music_dir."""
    music_dir = Path(get_setting("music_dir", str(MUSIC_DIR), user_id=user_id))
    subdir = custom_subdir.strip()
    if subdir in ("", "."):
        return music_dir
    return _join_within_music_dir(music_dir, subdir, "Custom playlist folder")


def get_playlists_dir(user_id: str | None = None) -> Path | None:
    """Get the playlists download directory for a user, or None if disabled."""
    music_dir = Path(get_setting("music_dir", str(MUSIC_DIR), user_id=user_id))
    subdir = get_setting("playlists_subdir", "", user_id=user_id).strip()
    if not subdir:
        return None  # Feature disabled, fall back to Singles behaviour
    if subdir == ".":
        return music_dir
    try:
        return _join_within_music_dir(music_dir, subdir, "Playlists folder")
    except ValueError as e:
        # This one is opt-in, so the safe default is "off" rather than a guess
        # at which folder was meant.
        print(f"[settings] {e}; treating the playlists folder as disabled")
        return None


TRASH_ROOT = DB_PATH.parent / ".trash"


def get_trash_dir(user_id: str | None = None) -> Path:
    """Get the trash directory for a user.

    Lives under /data rather than the music volume to avoid FUSE/mergerfs
    filesystem quirks that prevent directory listing on some setups.

    Each account gets its own bin. It used to be one shared heap, which meant
    anyone with an account could browse, play, restore or permanently delete
    somebody else's deleted music; restoring was the really cheeky one, since it
    dropped their file into your library. In single-user mode there is no
    user_id and no one to hide from, so the root itself is used, exactly as
    before. migrate_trash_to_per_user() sorts out installs that grew a second
    account after the fact.
    """
    if not user_id:
        return TRASH_ROOT
    return TRASH_ROOT / str(user_id)


def _move_trash_files(src: Path, dst: Path) -> int:
    """Move every file under src into the matching spot under dst, then tidy up.

    shutil.move on a directory would nest it inside an existing destination, and
    a trash bin full of `.trash/Singles/Singles/` helps nobody, so this walks
    files instead. Existing destination files win; a duplicate in the bin is not
    worth losing sleep, or the original, over.
    """
    moved = 0
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        target = dst / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            continue
        try:
            shutil.move(str(path), str(target))
            moved += 1
        except OSError as e:
            print(f"[trash] Could not migrate {path}: {e}")
    # Sweep up the empty shells left behind, deepest first.
    for path in sorted(src.rglob("*"), reverse=True):
        if path.is_dir():
            with contextlib.suppress(OSError):
                path.rmdir()
    return moved


def migrate_trash_to_per_user() -> None:
    """Reshape the trash bin to match how many accounts the install has.

    The bin used to be one shared pile. Now it is per-account, which leaves two
    installs needing a nudge, both idempotent and both cheap enough to run at
    every boot:

    * grew a second account: loose files at the root predate the split and have
      no owner recorded anywhere, so they go to the first admin. The alternative
      is leaving them on disk but invisible to every single user, which looks
      exactly like MusicGrabber ate them.
    * shrank back to one account: single-user mode has no user_id, so a bin
      tucked under one would be unreachable and restore paths would sprout a
      stray ID folder. Flatten it back to the root.
    """
    if not TRASH_ROOT.exists():
        return
    try:
        with db_conn() as conn:
            # Every account, active or not, deliberately: middleware decides
            # single-user mode on a plain COUNT(*), and if the two disagree we
            # would tidy the bin into a shape the request path cannot find.
            users = conn.execute(
                "SELECT id, role, is_active FROM users ORDER BY created_at"
            ).fetchall()
    except Exception as e:
        print(f"[trash] Skipping migration, could not read users: {e}")
        return

    user_ids = {str(row[0]) for row in users}

    if len(users) <= 1:
        # Single-user mode: everything belongs to the one account anyway.
        for child in TRASH_ROOT.iterdir():
            if child.is_dir() and child.name in user_ids:
                moved = _move_trash_files(child, TRASH_ROOT)
                with contextlib.suppress(OSError):
                    child.rmdir()
                if moved:
                    print(f"[trash] Flattened {moved} file(s) back to the shared bin")
        return

    # Prefer an admin who can actually log in to come and collect them.
    admin_id = next((str(row[0]) for row in users if row[1] == "admin" and row[2]), None)
    admin_id = admin_id or next((str(row[0]) for row in users if row[1] == "admin"), None)
    if not admin_id:
        return
    admin_dir = TRASH_ROOT / admin_id
    orphans = [
        child for child in TRASH_ROOT.iterdir()
        if child.name not in user_ids and not child.name.startswith(".")
    ]
    if not orphans:
        return
    admin_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    for child in orphans:
        if child.is_dir():
            total += _move_trash_files(child, admin_dir)
            with contextlib.suppress(OSError):
                child.rmdir()
        elif child.is_file():
            target = admin_dir / child.name
            if not target.exists():
                with contextlib.suppress(OSError):
                    shutil.move(str(child), str(target))
                    total += 1
    if total:
        print(f"[trash] Moved {total} unowned file(s) into the admin's bin")


def get_albums_dir(user_id: str | None = None) -> Path:
    """Get the albums download directory for a user.

    Files land at: albums_dir / Artist / Album / Track.flac
    Returns music_dir / albums_subdir (default "Albums").
    """
    music_dir = Path(get_setting("music_dir", str(MUSIC_DIR), user_id=user_id))
    subdir = get_setting("albums_subdir", "Albums", user_id=user_id).strip() or "Albums"
    if subdir == ".":
        return music_dir
    return _safe_library_dir(music_dir, subdir, "Albums folder", "Albums")


def get_download_dir(artist: str, user_id: str | None = None) -> Path:
    """Get the download directory for a track, respecting the organise-by-artist setting.

    When organise_by_artist is True (default):  /music/Singles/Artist Name/
    When organise_by_artist is False:            /music/Singles/
    """
    from utils import sanitize_filename
    base = get_singles_dir(user_id=user_id)
    if get_setting_bool("organise_by_artist", True, user_id=user_id):
        return base / sanitize_filename(artist)
    return base
