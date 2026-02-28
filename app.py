#!/usr/bin/env python3
"""
Music Grabber - A self-hosted music acquisition service
Searches YouTube, downloads best quality audio with optional conversion to FLAC, drops into Navidrome library
"""

import json
import os
import re
import sqlite3
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import httpx

import base64

from constants import (
    VERSION, MUSIC_DIR, DB_PATH, COOKIES_FILE,
    MONOCHROME_API_URL, TIMEOUT_MONOCHROME_API,
    TIMEOUT_YTDLP_INFO,
    TIMEOUT_YTDLP_PREVIEW,
    TIMEOUT_SLSKD_SEARCH,
    WATCHED_PLAYLIST_CHECK_HOURS,
    SEARCH_LOG_RETENTION_DAYS,
)
from db import db_conn, init_db, start_stale_job_monitor, cleanup_stale_jobs, cleanup_old_search_logs
from settings import (
    get_setting, get_setting_bool, set_setting, get_singles_dir,
    SETTINGS_SCHEMA, SENSITIVE_SETTINGS, _get_typed_setting, _is_env_override,
)
from models import (
    SearchRequest, DownloadRequest, PlaylistFetchRequest,
    AsyncBulkImportRequest, WatchedPlaylistRequest, WatchedPlaylistUpdate,
    SettingsUpdate, SearchResult, BlacklistRequest,
    TestSlskdRequest, TestNavidromeRequest, TestJellyfinRequest, TestYouTubeCookiesRequest,
)
from middleware import AuthMiddleware
from youtube import (
    _has_valid_cookie_entries, _cookie_lines_for_domain_check, _sync_cookies_file,
    _ytdlp_base_args, _is_ytdlp_403, parse_duration,
)
from search import search_source, search_all, get_available_sources, SOURCE_REGISTRY
from slskd import slskd_enabled, search_slskd
from downloads import (
    process_download, process_playlist_download, process_slskd_download,
)
from bulk_import import clean_bulk_import_line, start_bulk_import_for_tracks, process_bulk_import_worker
from amazon import fetch_amazon_playlist
from watched_playlists import (
    detect_playlist_platform, fetch_playlist_tracks, refresh_watched_playlist,
    start_scheduler, _fetch_spotify_playlist_embed,
)
from utils import hash_track, is_valid_youtube_id, spawn_daemon_thread, subsonic_auth_params

URL_BASED_SOURCES = {"soundcloud", "monochrome"}

# =============================================================================
# Application Setup
# =============================================================================

app = FastAPI(title="Music Grabber", version=VERSION)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Ensure directories exist
get_singles_dir().mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Initialise database and start background monitors
init_db()
cleanup_old_search_logs(SEARCH_LOG_RETENTION_DAYS)
start_stale_job_monitor()

# Sync cookies file from settings at startup
_sync_cookies_file()

# Register middleware
app.add_middleware(AuthMiddleware)


# =============================================================================
# Basic Routes
# =============================================================================

@app.get("/", response_class=HTMLResponse)
def root():
    """Serve the main UI"""
    return FileResponse("static/index.html")

def _is_volume_mounted() -> bool:
    """Check if MUSIC_DIR appears to be a mounted volume.

    Compares device IDs - if /music is on a different device than /,
    it's likely a mounted volume. This helps detect misconfigured setups
    where users forgot to mount their music directory.
    """
    try:
        root_stat = os.stat("/")
        music_stat = os.stat(MUSIC_DIR)
        # Different device ID means it's a mount point
        return root_stat.st_dev != music_stat.st_dev
    except OSError:
        # Can't stat, assume it's fine
        return True


@app.get("/api/config")
def get_config():
    """Expose server configuration and version for the UI"""
    api_key = get_setting("api_key", "")
    return {
        "version": VERSION,
        "default_convert_to_flac": get_setting_bool("default_convert_to_flac", True),
        "auth_required": bool(api_key),
        "volume_mounted": _is_volume_mounted()
    }


# =============================================================================
# Music directory listing (for subfolder picker)
# =============================================================================

@app.get("/api/music-dirs")
def list_music_dirs(path: str = "", recursive: bool = False, max_depth: int | None = None):
    """List subdirectories of MUSIC_DIR (or a subpath) for the subfolder picker.

    Returns directory names/paths only — no hidden/system dirs, sorted alphabetically.
    Filters out dotfiles and @-prefixed system dirs (Synology, etc.).
    The path parameter lets users browse deeper into the tree.
    Set recursive=true to list descendants as full paths relative to MUSIC_DIR.
    Optionally pass max_depth to cap recursive traversal depth.
    """
    # Sanitise: normalise separators, strip edges, reject traversal segments
    clean = path.strip().replace("\\", "/").strip("/")
    segments = [segment for segment in clean.split("/") if segment and segment != "."]
    if any(segment == ".." for segment in segments):
        raise HTTPException(status_code=400, detail="Path traversal not allowed")
    clean = "/".join(segments)

    target = MUSIC_DIR / clean if clean else MUSIC_DIR
    target_resolved = target.resolve()
    music_root = MUSIC_DIR.resolve()
    # Make sure we haven't escaped MUSIC_DIR
    try:
        target.resolve().relative_to(music_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path outside music directory")

    if max_depth is not None and max_depth < 1:
        raise HTTPException(status_code=400, detail="max_depth must be >= 1")

    try:
        if recursive:
            dirs = []
            if target.is_dir():
                for dirpath, dirnames, _ in os.walk(target):
                    # Prune hidden/system directories at each level.
                    dirnames[:] = [
                        name for name in dirnames
                        if not name.startswith((".", "@"))
                    ]
                    rel_from_target = Path(dirpath).resolve().relative_to(target_resolved)
                    depth = len(rel_from_target.parts)
                    if max_depth is not None and depth >= max_depth:
                        dirnames[:] = []
                    rel = Path(dirpath).resolve().relative_to(music_root).as_posix()
                    if rel in {".", clean}:
                        continue
                    if max_depth is not None and depth > max_depth:
                        continue
                    dirs.append(rel)
            dirs.sort(key=str.casefold)
        else:
            dirs = sorted(
                [
                    d.name for d in target.iterdir()
                    if d.is_dir() and not d.name.startswith((".", "@"))
                ],
                key=str.casefold,
            )
    except FileNotFoundError:
        dirs = []

    return {
        "path": clean,
        "directories": dirs,
    }


@app.get("/api/check-file")
def check_existing_file(artist: str, title: str):
    """Check if a track already exists in the library.

    Returns the file path if found, along with metadata from the jobs database.
    This allows Telegram bot to send existing files instead of re-downloading.
    """
    from utils import check_duplicate

    existing_file = check_duplicate(artist, title)
    if not existing_file:
        return {"exists": False, "file": None}

    # Get job metadata if available
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        job = conn.execute(
            "SELECT * FROM jobs WHERE artist = ? AND title = ? AND status = 'completed' ORDER BY completed_at DESC LIMIT 1",
            (artist, title)
        ).fetchone()

    return {
        "exists": True,
        "file": str(existing_file.relative_to(MUSIC_DIR)),
        "full_path": str(existing_file),
        "filename": existing_file.name,
        "size": existing_file.stat().st_size,
        "metadata": dict(job) if job else None
    }


@app.get("/api/file/{file_path:path}")
def serve_music_file(file_path: str):
    """Serve a music file for Telegram URL-based upload.
    
    Telegram will download the file from this URL instead of us pushing it.
    This is more reliable than direct upload which can timeout.
    """
    from fastapi import Header
    from fastapi.responses import FileResponse
    
    file_path_clean = file_path.replace("..", "")
    full_path = MUSIC_DIR / file_path_clean
    
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    if not full_path.is_file():
        raise HTTPException(status_code=400, detail="Not a file")
    
    # Determine content type
    suffix = full_path.suffix.lower()
    content_types = {
        '.mp3': 'audio/mpeg',
        '.flac': 'audio/flac',
        '.m4a': 'audio/mp4',
        '.ogg': 'audio/ogg',
        '.opus': 'audio/ogg',
        '.wav': 'audio/wav',
    }
    content_type = content_types.get(suffix, 'audio/mpeg')
    
    return FileResponse(
        full_path,
        media_type=content_type,
        filename=full_path.name
    )


# =============================================================================
# Settings API
# =============================================================================

@app.get("/api/settings")
def get_settings():
    """Get all settings. Sensitive values are masked unless empty."""
    settings = {}
    env_overrides = []

    for key, schema in SETTINGS_SCHEMA.items():
        value = _get_typed_setting(key)
        is_sensitive = schema.get("sensitive", False)

        # Track which settings are locked by env vars
        if _is_env_override(key):
            env_overrides.append(key)

        # Mask sensitive values (show that something is set, but not what)
        if is_sensitive and value:
            settings[key] = "••••••••"
        else:
            settings[key] = value

    return {
        "settings": settings,
        "env_overrides": env_overrides,  # Frontend can disable these fields
        "sensitive_fields": list(SENSITIVE_SETTINGS)
    }


@app.put("/api/settings")
def update_settings(updates: SettingsUpdate):
    """Update settings. Only non-None values are updated. Returns updated settings."""
    updated_keys = []

    for key, value in updates.model_dump(exclude_none=True).items():
        if key not in SETTINGS_SCHEMA:
            continue

        # Don't allow updating settings that are locked by env vars
        if _is_env_override(key):
            continue

        # Convert booleans to string for storage
        if isinstance(value, bool):
            value = "true" if value else "false"
        else:
            value = str(value)

        # Validate singles_subdir to keep writes under MUSIC_DIR.
        if key == "singles_subdir":
            raw = value.strip().replace("\\", "/")
            if raw == ".":
                value = "."
            else:
                parts = [
                    part.strip()
                    for part in raw.split("/")
                    if part.strip() and part.strip() != "."
                ]
                if any(part == ".." for part in parts):
                    raise HTTPException(status_code=400, detail="Invalid singles subfolder path")
                value = "/".join(parts) or "Singles"
                try:
                    (MUSIC_DIR / value).resolve().relative_to(MUSIC_DIR.resolve())
                except ValueError:
                    raise HTTPException(status_code=400, detail="Singles subfolder must stay within music directory")

        # Validate cookie format before saving
        if key == "youtube_cookies" and value.strip() and not _has_valid_cookie_entries(value):
            raise HTTPException(
                status_code=400,
                detail="Invalid cookies format. Paste Netscape-format cookies.txt content."
            )

        set_setting(key, value)
        updated_keys.append(key)

    # Sync cookies file if YouTube cookies were updated
    if "youtube_cookies" in updated_keys:
        _sync_cookies_file()

    return {
        "updated": updated_keys,
        "settings": get_settings()["settings"]
    }


# =============================================================================
# Settings Test Endpoints
# =============================================================================

@app.post("/api/settings/test/slskd")
def test_slskd_connection(request: TestSlskdRequest = None):
    """Test connection to slskd server. Uses form values if provided, otherwise saved settings."""
    url = (request.url if request and request.url else None) or _get_typed_setting("slskd_url")
    user = (request.username if request and request.username else None) or _get_typed_setting("slskd_user")
    password = (request.password if request and request.password else None) or _get_typed_setting("slskd_pass")

    if not url:
        return {"success": False, "message": "slskd URL not configured"}

    try:
        with httpx.Client(timeout=10) as client:
            auth_response = client.post(
                f"{url.rstrip('/')}/api/v0/session",
                json={"username": user, "password": password}
            )
            if auth_response.status_code == 200:
                return {"success": True, "message": "Connected to slskd successfully"}
            else:
                return {"success": False, "message": f"Authentication failed: {auth_response.status_code}"}
    except httpx.TimeoutException:
        return {"success": False, "message": "Connection timed out"}
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {str(e)}"}


@app.post("/api/settings/test/navidrome")
def test_navidrome_connection(request: TestNavidromeRequest = None):
    """Test connection to Navidrome server. Uses form values if provided, otherwise saved settings."""
    url = (request.url if request and request.url else None) or _get_typed_setting("navidrome_url")
    user = (request.username if request and request.username else None) or _get_typed_setting("navidrome_user")
    password = (request.password if request and request.password else None) or _get_typed_setting("navidrome_pass")

    if not url:
        return {"success": False, "message": "Navidrome URL not configured"}

    try:
        params = subsonic_auth_params(user, password)

        with httpx.Client(timeout=10) as client:
            response = client.get(
                f"{url.rstrip('/')}/rest/ping",
                params=params
            )
            if response.status_code == 200:
                data = response.json()
                if data.get("subsonic-response", {}).get("status") == "ok":
                    return {"success": True, "message": "Connected to Navidrome successfully"}
                else:
                    return {"success": False, "message": "Authentication failed"}
            else:
                return {"success": False, "message": f"Connection failed: {response.status_code}"}
    except httpx.TimeoutException:
        return {"success": False, "message": "Connection timed out"}
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {str(e)}"}


@app.post("/api/settings/test/jellyfin")
def test_jellyfin_connection(request: TestJellyfinRequest = None):
    """Test connection to Jellyfin server. Uses form values if provided, otherwise saved settings."""
    url = (request.url if request and request.url else None) or _get_typed_setting("jellyfin_url")
    api_key = (request.api_key if request and request.api_key else None) or _get_typed_setting("jellyfin_api_key")

    if not url:
        return {"success": False, "message": "Jellyfin URL not configured"}
    if not api_key:
        return {"success": False, "message": "Jellyfin API key not configured"}

    try:
        with httpx.Client(timeout=10) as client:
            response = client.get(
                f"{url.rstrip('/')}/System/Info",
                headers={"X-Emby-Token": api_key}
            )
            if response.status_code == 200:
                data = response.json()
                server_name = data.get("ServerName", "Jellyfin")
                return {"success": True, "message": f"Connected to {server_name} successfully"}
            elif response.status_code == 401:
                return {"success": False, "message": "Invalid API key"}
            else:
                return {"success": False, "message": f"Connection failed: {response.status_code}"}
    except httpx.TimeoutException:
        return {"success": False, "message": "Connection timed out"}
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {str(e)}"}


@app.post("/api/settings/test/youtube-cookies")
def test_youtube_cookies(request: TestYouTubeCookiesRequest = None):
    """Test YouTube cookies by fetching info for a known public video.
    Uses form value if provided, otherwise the saved cookies."""
    cookies_text = (request.cookies if request and request.cookies else None)
    if cookies_text is None:
        cookies_text = get_setting("youtube_cookies", "")

    if not cookies_text.strip():
        return {"success": False, "message": "No cookies provided"}

    # Basic format validation
    if not _has_valid_cookie_entries(cookies_text):
        return {"success": False, "message": "No cookie entries found (only comments or blank lines)"}

    lines = _cookie_lines_for_domain_check(cookies_text)
    has_youtube_cookie = any(".youtube.com" in l or ".google.com" in l for l in lines)
    if not has_youtube_cookie:
        return {"success": False, "message": "No YouTube or Google cookies found. Export cookies from youtube.com."}

    # Write to a temp file and test with yt-dlp
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(cookies_text)
            tmp_path = f.name

        # Use a short, well-known public video (Rick Astley - official)
        test_cmd = [
            "yt-dlp",
            "--cookies", tmp_path,
            "--dump-json",
            "--no-warnings",
            "--no-download",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        ]

        result = subprocess.run(test_cmd, capture_output=True, text=True, timeout=TIMEOUT_YTDLP_INFO)

        if result.returncode == 0:
            try:
                info = json.loads(result.stdout)
                title = info.get("title", "Unknown")
                return {"success": True, "message": f"Cookies valid — fetched: {title}"}
            except json.JSONDecodeError:
                return {"success": True, "message": "Cookies appear valid (got a response)"}
        else:
            stderr = result.stderr
            if _is_ytdlp_403(stderr):
                return {"success": False, "message": "Cookies rejected by YouTube (403). They may be expired — try re-exporting."}
            return {"success": False, "message": f"yt-dlp failed: {stderr[:200]}"}

    except subprocess.TimeoutExpired:
        return {"success": False, "message": "Test timed out"}
    except Exception as e:
        return {"success": False, "message": f"Test failed: {str(e)}"}
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


@app.get("/api/settings/youtube-cookies/status")
def youtube_cookies_status():
    """Return non-sensitive status for the cookies file."""
    cookies_text = get_setting("youtube_cookies", "")
    has_setting = bool(cookies_text.strip())
    file_exists = COOKIES_FILE.exists()
    file_size = COOKIES_FILE.stat().st_size if file_exists else 0
    file_mtime = COOKIES_FILE.stat().st_mtime if file_exists else None
    return {
        "has_setting": has_setting,
        "file_exists": file_exists,
        "file_size": file_size,
        "file_mtime": file_mtime,
        "file_has_valid_entries": _has_valid_cookie_entries(cookies_text) if has_setting else False
    }


# =============================================================================
# Statistics API
# =============================================================================

def _extract_search_artist(query: str) -> str | None:
    """Best-effort artist extraction from common search query formats."""
    q = (query or "").strip()
    if not q:
        return None

    # "Artist - Song", "Artist – Song", "Artist — Song"
    split_match = re.split(r"\s*[-–—]\s*", q, maxsplit=1)
    if len(split_match) == 2 and split_match[0].strip():
        return split_match[0].strip()[:120]

    # Fallback: first 3 words is usually artist-ish, avoids giant free text blobs
    words = q.split()
    if not words:
        return None
    return " ".join(words[:3])[:120]


def _log_search(query: str, result_count: int, source: str = "youtube") -> str:
    """Log search requests for dashboard analytics and return tracking token."""
    artist = _extract_search_artist(query)
    search_token = uuid.uuid4().hex
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO search_logs (query, artist, result_count, source, search_token) VALUES (?, ?, ?, ?, ?)",
            (query.strip(), artist, int(result_count), source, search_token)
        )
        conn.commit()
    return search_token


def _validated_search_token(search_token: str | None) -> str | None:
    """Only accept server-issued search tokens that exist in search_logs."""
    token = (search_token or "").strip().lower()
    if not token or not re.fullmatch(r"[0-9a-f]{32}", token):
        return None

    with db_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM search_logs WHERE search_token = ? LIMIT 1",
            (token,)
        ).fetchone()
    return token if row else None


@app.get("/api/stats")
def get_stats():
    """Return download statistics for the dashboard."""
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        # Overall counts by status
        rows = conn.execute(
            "SELECT status, COUNT(*) as count FROM jobs GROUP BY status"
        ).fetchall()
        status_counts = {r["status"]: r["count"] for r in rows}

        # Total completed tracks
        total = sum(status_counts.values())
        completed = status_counts.get("completed", 0) + status_counts.get("completed_with_errors", 0)
        failed = status_counts.get("failed", 0)

        # Source breakdown (youtube vs soulseek)
        source_rows = conn.execute(
            "SELECT COALESCE(source, 'youtube') as source, COUNT(*) as count "
            "FROM jobs WHERE status IN ('completed', 'completed_with_errors') "
            "GROUP BY source"
        ).fetchall()
        sources = {r["source"]: r["count"] for r in source_rows}

        # Downloads completed per day (last 30 days)
        daily_rows = conn.execute(
            "SELECT DATE(completed_at) as day, COUNT(*) as count "
            "FROM jobs WHERE status IN ('completed', 'completed_with_errors') "
            "AND completed_at IS NOT NULL "
            "AND completed_at >= DATE('now', '-30 days') "
            "GROUP BY day ORDER BY day"
        ).fetchall()
        daily = [{"day": r["day"], "count": r["count"]} for r in daily_rows]

        # Top artists (by completed downloads) — case-insensitive grouping,
        # display the most popular casing variant for each artist
        artist_rows = conn.execute(
            "SELECT artist, total_count as count FROM ("
            "  SELECT artist, "
            "    SUM(COUNT(*)) OVER (PARTITION BY LOWER(artist)) as total_count, "
            "    ROW_NUMBER() OVER (PARTITION BY LOWER(artist) ORDER BY COUNT(*) DESC) as rn "
            "  FROM jobs "
            "  WHERE status IN ('completed', 'completed_with_errors') "
            "  AND artist IS NOT NULL AND artist != '' "
            "  GROUP BY artist"
            ") WHERE rn = 1 "
            "ORDER BY count DESC LIMIT 10"
        ).fetchall()
        top_artists = [{"artist": r["artist"], "count": r["count"]} for r in artist_rows]

        # Recent downloads (last 10 completed)
        recent_rows = conn.execute(
            "SELECT title, artist, source, completed_at "
            "FROM jobs WHERE status IN ('completed', 'completed_with_errors') "
            "ORDER BY completed_at DESC LIMIT 10"
        ).fetchall()
        recent = [
            {"title": r["title"], "artist": r["artist"], "source": r["source"], "completed_at": r["completed_at"]}
            for r in recent_rows
        ]

        # Search history stats
        search_summary = conn.execute(
            "SELECT COUNT(*) as total_searches, "
            "SUM(CASE WHEN result_count > 0 THEN 1 ELSE 0 END) as successful_searches "
            "FROM search_logs"
        ).fetchone()
        total_searches = int(search_summary["total_searches"] or 0)
        successful_searches = int(search_summary["successful_searches"] or 0)

        # Case-insensitive grouping — display the most popular casing variant
        searched_artist_rows = conn.execute(
            "SELECT artist, total_count as count, total_successful as successful_searches FROM ("
            "  SELECT artist, "
            "    SUM(COUNT(*)) OVER (PARTITION BY LOWER(artist)) as total_count, "
            "    SUM(SUM(CASE WHEN result_count > 0 THEN 1 ELSE 0 END)) OVER (PARTITION BY LOWER(artist)) as total_successful, "
            "    ROW_NUMBER() OVER (PARTITION BY LOWER(artist) ORDER BY COUNT(*) DESC) as rn "
            "  FROM search_logs "
            "  WHERE artist IS NOT NULL AND artist != '' "
            "  GROUP BY artist"
            ") WHERE rn = 1 "
            "ORDER BY count DESC, artist ASC "
            "LIMIT 10"
        ).fetchall()
        top_searched_artists = [
            {
                "artist": r["artist"],
                "count": int(r["count"]),
                "successful_searches": int(r["successful_searches"] or 0),
            }
            for r in searched_artist_rows
        ]

        search_to_download = conn.execute(
            "SELECT COUNT(*) as converted_searches "
            "FROM search_logs s "
            "WHERE EXISTS ("
            "  SELECT 1 FROM jobs j "
            "  WHERE j.search_token = s.search_token "
            "  AND j.status IN ('completed', 'completed_with_errors')"
            ")"
        ).fetchone()
        converted_searches = int(search_to_download["converted_searches"] or 0)

        # Storage usage
        storage_bytes = 0
        file_count = 0
        try:
            for f in get_singles_dir().rglob("*"):
                if f.is_file() and f.suffix.lower() in ('.flac', '.opus', '.m4a', '.mp3', '.ogg', '.webm'):
                    storage_bytes += f.stat().st_size
                    file_count += 1
        except OSError:
            pass

    return {
        "total_jobs": total,
        "completed": completed,
        "failed": failed,
        "sources": sources,
        "daily": daily,
        "top_artists": top_artists,
        "total_searches": total_searches,
        "successful_searches": successful_searches,
        "converted_searches": converted_searches,
        "top_searched_artists": top_searched_artists,
        "recent": recent,
        "storage_bytes": storage_bytes,
        "file_count": file_count,
    }


@app.delete("/api/stats")
def reset_stats(confirm: bool = False):
    """Reset dashboard stats without touching active queue items."""
    if not confirm:
        raise HTTPException(status_code=400, detail="Confirmation required (use ?confirm=true)")

    # Keep active work intact, but clear historical job/search data used by stats
    cleanup_stale_jobs()

    with db_conn() as conn:
        deleted_jobs = conn.execute(
            "DELETE FROM jobs WHERE status IN ('completed', 'completed_with_errors', 'failed')"
        ).rowcount
        deleted_searches = conn.execute("DELETE FROM search_logs").rowcount
        conn.commit()

    return {"deleted_jobs": deleted_jobs, "deleted_searches": deleted_searches}


# =============================================================================
# Search API
# =============================================================================

@app.get("/api/preview/{video_id}")
def get_preview_url(video_id: str, source: str = "youtube", url: str = None):
    """Get a streamable audio URL for preview playback."""
    try:
        # Monochrome: fetch an AAC stream URL from the API — no yt-dlp needed,
        # and browsers play MP4/AAC natively without any fuss
        if source == "monochrome":
            with httpx.Client(timeout=TIMEOUT_MONOCHROME_API) as client:
                resp = client.get(
                    f"{MONOCHROME_API_URL}/track/",
                    params={"id": video_id, "quality": "HIGH"},
                )
            resp.raise_for_status()
            data = resp.json().get("data") or {}
            if not data.get("manifest"):
                raise HTTPException(status_code=404, detail="No stream available for this track")
            manifest = json.loads(base64.b64decode(data["manifest"]))
            urls = manifest.get("urls") or []
            if not urls:
                raise HTTPException(status_code=404, detail="No audio stream found")
            return {"url": urls[0], "video_id": video_id}

        if source == "youtube":
            if not is_valid_youtube_id(video_id):
                raise HTTPException(status_code=400, detail="Invalid YouTube video ID")
            target_url = f"https://www.youtube.com/watch?v={video_id}"
            base_args = _ytdlp_base_args()
        elif source in URL_BASED_SOURCES:
            if not url:
                raise HTTPException(status_code=400, detail=f"{source.capitalize()} preview requires url parameter")
            target_url = url
            base_args = []  # No cookies needed for URL-based non-YouTube sources
        else:
            raise HTTPException(status_code=400, detail=f"Preview not supported for source: {source}")

        # SoundCloud returns HLS (.m3u8) for bestaudio which browsers can't
        # play natively — prefer the direct HTTP MP3 stream for previews.
        # Format IDs vary by track: older ones use http_mp3_1_0, newer ones
        # use http_mp3_standard. Both resolve to a direct .mp3 on cf-media.sndcdn.com.
        if source == "soundcloud":
            fmt = "http_mp3_1_0/http_mp3_standard/bestaudio[protocol=http]/best"
        else:
            fmt = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"

        cmd = [
            "yt-dlp",
            *base_args,
            "-f", fmt,
            "-g",  # Get URL only, don't download
            "--no-warnings",
            target_url,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_YTDLP_PREVIEW)

        if result.returncode != 0:
            raise HTTPException(status_code=500, detail="Failed to get preview URL")

        audio_url = result.stdout.strip()

        if not audio_url:
            raise HTTPException(status_code=404, detail="No audio stream found")

        return {"url": audio_url, "video_id": video_id}

    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Preview request timed out")
    except Exception as e:
        print(f"preview error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get preview URL")


@app.get("/api/sources")
def list_sources():
    """Return available search sources for the frontend source selector."""
    return {"sources": get_available_sources()}


@app.post("/api/search")
def search(request: SearchRequest):
    """Search for music across configured sources."""
    try:
        source = request.source
        if source == "all":
            raw_results = search_all(request.query, request.limit)
        elif source in SOURCE_REGISTRY:
            raw_results = search_source(source, request.query, request.limit)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown source: {source}")

        final_results = []
        for item in raw_results[:request.limit]:
            final_results.append(SearchResult(
                video_id=item["video_id"],
                title=item["title"],
                artist=None,
                channel=item["channel"],
                duration=item["duration"],
                thumbnail=item["thumbnail"],
                is_playlist=item.get("is_playlist", False),
                video_count=item.get("video_count"),
                source=item["source"],
                source_url=item.get("source_url"),
                quality=item["quality"],
                quality_score=item["quality_score"],
                slskd_username=item["slskd_username"],
                slskd_filename=item["slskd_filename"],
                album=item.get("monochrome_album"),
            ))

        search_token = None
        try:
            search_token = _log_search(request.query, len(final_results), source=source)
        except Exception as log_error:
            print(f"search log error: {log_error}")

        return {"results": final_results, "slskd_enabled": slskd_enabled(), "search_token": search_token}

    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Search timed out")
    except HTTPException:
        raise
    except Exception as e:
        print(f"search error: {e}")
        raise HTTPException(status_code=500, detail="Search failed")


@app.post("/api/search/slskd")
def search_slskd_endpoint(request: SearchRequest):
    """Search Soulseek via slskd (slower, called separately)"""
    if not slskd_enabled():
        return {"results": [], "slskd_enabled": False}

    try:
        print(f"Searching slskd for: {request.query}")
        slskd_results = search_slskd(request.query, timeout_secs=TIMEOUT_SLSKD_SEARCH)
        print(f"slskd returned {len(slskd_results)} results")

        final_results = []
        for r in slskd_results[:request.limit]:
            final_results.append(SearchResult(
                video_id=r["id"],
                title=r["title"],
                artist=r["artist"],
                channel=r["channel"],
                duration=parse_duration(int(r["duration"])) if r["duration"].isdigit() else r["duration"],
                thumbnail="",
                is_playlist=False,
                video_count=None,
                source="soulseek",
                quality=r["quality"],
                quality_score=r["quality_score"],
                slskd_username=r["slskd_username"],
                slskd_filename=r["slskd_filename"],
            ))

        return {"results": final_results, "slskd_enabled": True}

    except Exception as e:
        print(f"slskd search error: {e}")
        return {"results": [], "slskd_enabled": True, "error": str(e)}


# =============================================================================
# Download API
# =============================================================================

@app.post("/api/download")
def download(request: DownloadRequest):
    """Queue a download job"""
    job_id = str(uuid.uuid4())[:8]

    # Extract artist/title if not provided
    artist = request.artist
    title = request.title

    # Create job record
    with db_conn() as conn:
        # Determine source type
        source = request.source or "youtube"
        if source == "soulseek" and not (request.slskd_username and request.slskd_filename):
            source = "youtube"  # Fallback if slskd fields missing

        # Validate based on source
        if source == "youtube":
            if not request.video_id or not is_valid_youtube_id(request.video_id):
                raise HTTPException(status_code=400, detail="Invalid YouTube video ID")
        elif source in URL_BASED_SOURCES:
            if not request.source_url:
                raise HTTPException(status_code=400, detail=f"{source.capitalize()} download requires source_url")

        # Build source URL for tracking
        if source == "soulseek":
            source_url = f"soulseek://{request.slskd_username}/{request.slskd_filename}" if request.slskd_username else None
        elif source in URL_BASED_SOURCES:
            source_url = request.source_url
        elif request.download_type == "playlist":
            source_url = f"https://www.youtube.com/playlist?list={request.video_id}" if request.video_id else None
        else:
            source_url = f"https://www.youtube.com/watch?v={request.video_id}" if request.video_id else None

        valid_search_token = _validated_search_token(request.search_token)

        if request.download_type == "playlist":
            conn.execute(
                """INSERT INTO jobs (id, video_id, title, status, download_type, playlist_name, source, convert_to_flac, source_url, search_token, telegram_chat_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, request.video_id, title, "queued", "playlist", title, "youtube", int(request.convert_to_flac), source_url, valid_search_token, request.telegram_chat_id)
            )
        else:
            conn.execute(
                """INSERT INTO jobs (id, video_id, title, artist, status, download_type, source, slskd_username, slskd_filename, convert_to_flac, source_url, search_token, telegram_chat_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, request.video_id, title, artist or "", "queued", "single", source,
                 request.slskd_username, request.slskd_filename, int(request.convert_to_flac), source_url, valid_search_token, request.telegram_chat_id)
            )
        conn.commit()

    # Queue the download based on source
    if request.download_type == "playlist":
        spawn_daemon_thread(process_playlist_download, job_id, request.video_id, title, request.convert_to_flac)
    elif source == "soulseek":
        spawn_daemon_thread(
            process_slskd_download,
            job_id,
            request.slskd_username,
            request.slskd_filename,
            artist or "",
            title,
            request.convert_to_flac
        )
    elif source in URL_BASED_SOURCES:
        spawn_daemon_thread(process_download, job_id, request.video_id, request.convert_to_flac, source_url=source_url)
    else:
        spawn_daemon_thread(process_download, job_id, request.video_id, request.convert_to_flac)

    return {"job_id": job_id, "status": "queued"}


# =============================================================================
# Job Management API
# =============================================================================

def _ensure_utc_suffix(timestamp: str | None) -> str | None:
    """Ensure timestamp has UTC indicator for proper JS parsing.

    SQLite's CURRENT_TIMESTAMP and datetime('now') return UTC but without
    timezone suffix. JavaScript's Date() treats such strings as local time.
    Appending 'Z' tells JS to interpret as UTC.
    """
    if not timestamp:
        return timestamp
    # Already has timezone info
    if timestamp.endswith('Z') or '+' in timestamp[-6:]:
        return timestamp
    # SQLite format uses space, ISO uses T
    return timestamp.replace(' ', 'T') + 'Z'


@app.get("/api/jobs")
def get_jobs(limit: int = 20):
    """Get recent jobs"""
    from utils import check_duplicate
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,)
        )
        jobs = []
        stale_ids = []  # Jobs that claim file exists but it doesn't
        for row in cursor.fetchall():
            job = dict(row)
            job['created_at'] = _ensure_utc_suffix(job.get('created_at'))
            job['completed_at'] = _ensure_utc_suffix(job.get('completed_at'))

            # Sync file_deleted flag with reality for completed jobs
            if (job.get('status') in ('completed', 'completed_with_errors')
                    and not job.get('file_deleted')
                    and job.get('artist') and job.get('title')):
                if not check_duplicate(job['artist'], job['title']):
                    job['file_deleted'] = 1
                    stale_ids.append(job['id'])

            jobs.append(job)

        # Batch-update any jobs whose files have gone walkabout
        if stale_ids:
            conn.executemany(
                "UPDATE jobs SET file_deleted = 1 WHERE id = ?",
                [(jid,) for jid in stale_ids]
            )
            conn.commit()

    return {"jobs": jobs}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    """Get a specific job"""
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    job = dict(row)
    job['created_at'] = _ensure_utc_suffix(job.get('created_at'))
    job['completed_at'] = _ensure_utc_suffix(job.get('completed_at'))
    return job


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str):
    """Retry a failed job"""
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Job not found")

        job = dict(row)

        # Allow retrying failed jobs or re-downloading completed jobs
        if job["status"] not in ("failed", "completed", "completed_with_errors"):
            raise HTTPException(status_code=400, detail="Only failed or completed jobs can be retried")

        # Reset job status
        conn.execute(
            "UPDATE jobs SET status = ?, error = NULL, completed_at = NULL, file_deleted = 0 WHERE id = ?",
            ("queued", job_id)
        )
        conn.commit()

    # Re-queue the job based on source type
    convert_to_flac = bool(job.get("convert_to_flac", 1))

    if job["download_type"] == "playlist":
        spawn_daemon_thread(process_playlist_download, job_id, job["video_id"], job["playlist_name"], convert_to_flac)
    elif job.get("source") == "soulseek" and job.get("slskd_username") and job.get("slskd_filename"):
        spawn_daemon_thread(
            process_slskd_download,
            job_id,
            job["slskd_username"],
            job["slskd_filename"],
            job.get("artist", ""),
            job.get("title", ""),
            convert_to_flac
        )
    elif job.get("source") in URL_BASED_SOURCES and job.get("source_url"):
        spawn_daemon_thread(process_download, job_id, job["video_id"], convert_to_flac, source_url=job["source_url"])
    else:
        spawn_daemon_thread(process_download, job_id, job["video_id"], convert_to_flac)

    return {"job_id": job_id, "status": "queued"}


@app.delete("/api/jobs/{job_id}/file")
def delete_job_file(job_id: str):
    """Delete the downloaded audio file (and lyrics) for a completed job."""
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    job = dict(row)
    if job["status"] not in ("completed", "completed_with_errors"):
        raise HTTPException(status_code=400, detail="Only completed jobs have files to delete")

    artist = job.get("artist")
    title = job.get("title")
    if not artist or not title:
        raise HTTPException(status_code=400, detail="Job has no artist/title metadata")

    from utils import sanitize_filename, check_duplicate
    existing = check_duplicate(artist, title)
    if not existing:
        # File already gone — just mark it as deleted and move on
        with db_conn() as conn:
            conn.execute("UPDATE jobs SET file_deleted = 1 WHERE id = ?", (job_id,))
            conn.commit()
        return {"deleted": [], "job_id": job_id}

    deleted_files = []
    try:
        # Delete audio file
        existing.unlink()
        deleted_files.append(existing.name)

        # Delete lyrics file if present
        lrc_file = existing.with_suffix(".lrc")
        if lrc_file.exists():
            lrc_file.unlink()
            deleted_files.append(lrc_file.name)

        # Remove empty artist directory
        artist_dir = existing.parent
        if artist_dir.exists() and not any(artist_dir.iterdir()):
            artist_dir.rmdir()

    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete: {e}")

    with db_conn() as conn:
        conn.execute("UPDATE jobs SET file_deleted = 1 WHERE id = ?", (job_id,))
        conn.commit()

    return {"deleted": deleted_files, "job_id": job_id}


@app.delete("/api/jobs/cleanup")
def cleanup_jobs(status: Optional[str] = None):
    """Delete completed, failed, or stale jobs"""
    # First, mark any stale jobs as failed so they get cleaned up
    cleanup_stale_jobs()

    with db_conn() as conn:
        if status == "completed":
            cursor = conn.execute("DELETE FROM jobs WHERE status IN ('completed', 'completed_with_errors')")
        elif status == "failed":
            cursor = conn.execute("DELETE FROM jobs WHERE status = 'failed'")
        elif status == "stale":
            cursor = conn.execute("DELETE FROM jobs WHERE status IN ('downloading', 'queued')")
        else:
            cursor = conn.execute("DELETE FROM jobs WHERE status IN ('completed', 'completed_with_errors', 'failed')")

        deleted_count = cursor.rowcount
        conn.commit()

    return {"deleted": deleted_count}


# =============================================================================
# Blacklist / Report API
# =============================================================================

@app.post("/api/blacklist")
def add_blacklist_entry(request: BlacklistRequest):
    """Report a bad track and/or block an uploader."""
    if not request.video_id and not request.uploader:
        raise HTTPException(status_code=400, detail="Need at least a video_id or uploader to blacklist")

    entries_created = []

    with db_conn() as conn:
        # Blacklist the specific video
        if request.video_id:
            # Upsert — if the same video_id is already blacklisted, update the reason
            existing = conn.execute(
                "SELECT id FROM blacklist WHERE video_id = ?", (request.video_id,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE blacklist SET reason = ?, note = ?, job_id = COALESCE(?, job_id) WHERE id = ?",
                    (request.reason, request.note, request.job_id, existing[0])
                )
                entries_created.append({"type": "video", "id": existing[0], "updated": True})
            else:
                cursor = conn.execute(
                    "INSERT INTO blacklist (video_id, source, reason, note, job_id) VALUES (?, ?, ?, ?, ?)",
                    (request.video_id, request.source, request.reason, request.note, request.job_id)
                )
                entries_created.append({"type": "video", "id": cursor.lastrowid})

        # Optionally blacklist the uploader too
        if request.block_uploader and request.uploader:
            uploader_lower = request.uploader.lower()
            existing = conn.execute(
                "SELECT id FROM blacklist WHERE lower(uploader) = ? AND source = ? AND (video_id IS NULL OR video_id = '')",
                (uploader_lower, request.source)
            ).fetchone()
            if not existing:
                cursor = conn.execute(
                    "INSERT INTO blacklist (uploader, source, reason, note, job_id) VALUES (?, ?, ?, ?, ?)",
                    (request.uploader, request.source, request.reason, request.note, request.job_id)
                )
                entries_created.append({"type": "uploader", "id": cursor.lastrowid})

        conn.commit()

    return {"entries": entries_created}


@app.get("/api/blacklist")
def list_blacklist(limit: int = 100, offset: int = 0):
    """List all blacklist entries for the management UI."""
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM blacklist ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM blacklist").fetchone()[0]

    return {
        "entries": [dict(r) for r in rows],
        "total": total
    }


@app.delete("/api/blacklist/{entry_id}")
def remove_blacklist_entry(entry_id: int):
    """Remove a blacklist entry by ID."""
    with db_conn() as conn:
        cursor = conn.execute("DELETE FROM blacklist WHERE id = ?", (entry_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Blacklist entry not found")
        conn.commit()

    return {"deleted": entry_id}


# =============================================================================
# Bulk Import API
# =============================================================================

@app.post("/api/bulk-import-async")
def bulk_import_async(request: AsyncBulkImportRequest):
    """Start an async bulk import job"""
    lines = request.songs.strip().split('\n')
    import_id = str(uuid.uuid4())[:8]

    # Parse and validate all lines first
    tracks_to_import = []
    for line_num, line in enumerate(lines, 1):
        line = clean_bulk_import_line(line)

        if not line:
            continue

        if len(line) > 200:
            continue

        # Try to parse "Artist - Song" format
        match = re.match(r'^(.+?)\s*[-–—]\s*(.+)$', line)
        if not match:
            continue

        artist, song = match.groups()
        artist = artist.strip()
        song = song.strip()

        if not artist or not song:
            continue

        tracks_to_import.append({
            "line_num": line_num,
            "artist": artist,
            "song": song
        })

    if not tracks_to_import:
        raise HTTPException(status_code=400, detail="No valid tracks found in input")

    # Create bulk import record
    with db_conn() as conn:
        conn.execute(
            """INSERT INTO bulk_imports
               (id, status, total_tracks, create_playlist, playlist_name, convert_to_flac)
               VALUES (?, 'pending', ?, ?, ?, ?)""",
            (import_id, len(tracks_to_import), int(request.create_playlist),
             request.playlist_name, int(request.convert_to_flac))
        )

        # Insert all tracks
        for track in tracks_to_import:
            conn.execute(
                "INSERT INTO bulk_import_tracks (import_id, line_num, artist, song, status) VALUES (?, ?, ?, ?, 'pending')",
                (import_id, track["line_num"], track["artist"], track["song"])
            )

        conn.commit()

    # Start background worker for this import
    spawn_daemon_thread(process_bulk_import_worker, import_id)

    return {
        "import_id": import_id,
        "total_tracks": len(tracks_to_import),
        "status": "pending"
    }


@app.get("/api/bulk-import/{import_id}/status")
def get_bulk_import_status(import_id: str):
    """Get status of a bulk import job"""
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        cursor = conn.execute("SELECT * FROM bulk_imports WHERE id = ?", (import_id,))
        import_row = cursor.fetchone()

        if not import_row:
            raise HTTPException(status_code=404, detail="Import not found")

        import_row = dict(import_row)

        # Get recent track statuses for display
        cursor = conn.execute(
            """SELECT artist, song, status, error FROM bulk_import_tracks
               WHERE import_id = ?
               ORDER BY
                   CASE status
                       WHEN 'queued' THEN 0
                       WHEN 'failed' THEN 0
                       WHEN 'searching' THEN 1
                       ELSE 2
                   END,
                   line_num DESC
               LIMIT 10""",
            (import_id,)
        )
        recent_tracks = [dict(row) for row in cursor.fetchall()]

        # Count download statuses by joining bulk_import_tracks with jobs
        cursor = conn.execute(
            """SELECT
                   SUM(CASE WHEN j.status = 'completed' THEN 1 ELSE 0 END) as completed,
                   SUM(CASE WHEN j.status = 'failed' THEN 1 ELSE 0 END) as download_failed,
                   SUM(CASE WHEN j.status IN ('queued', 'downloading') THEN 1 ELSE 0 END) as still_queued
               FROM bulk_import_tracks t
               JOIN jobs j ON t.job_id = j.id
               WHERE t.import_id = ?""",
            (import_id,)
        )
        row = cursor.fetchone()
        completed_count = row[0] or 0
        download_failed_count = row[1] or 0
        still_queued_count = row[2] or 0

    total_failed = import_row["failed"] + download_failed_count
    search_done = import_row["status"] in ("completed", "error")
    all_done = search_done and still_queued_count == 0

    return {
        "import_id": import_id,
        "status": import_row["status"],
        "total_tracks": import_row["total_tracks"],
        "searched": import_row["searched"],
        "queued": still_queued_count,
        "completed": completed_count,
        "failed": total_failed,
        "skipped": import_row["skipped"],
        "rate_limited": import_row["rate_limited_until"] is not None,
        "error": import_row["error"],
        "recent_tracks": recent_tracks,
        "complete": all_done
    }


@app.get("/api/bulk-imports")
def list_bulk_imports(limit: int = 10):
    """List recent bulk imports"""
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        cursor = conn.execute(
            "SELECT * FROM bulk_imports ORDER BY created_at DESC LIMIT ?",
            (limit,)
        )
        imports = [dict(row) for row in cursor.fetchall()]

    return {"imports": imports}


# =============================================================================
# Playlist Fetch API (Spotify, Amazon Music)
# =============================================================================

@app.post("/api/fetch-playlist")
@app.post("/api/spotify-playlist")  # Backwards compat
def fetch_playlist(request: PlaylistFetchRequest):
    """Fetch track list from a public Spotify or Amazon Music playlist URL."""
    url = request.url.strip()

    # Route to the right scraper based on URL
    if re.match(r'https?://music\.amazon\.[a-z.]+/(user-playlists|playlists)/', url):
        return fetch_amazon_playlist(url)

    # Default: Spotify (the original behaviour)
    return _fetch_spotify_playlist_embed(url)


# =============================================================================
# Watched Playlists API
# =============================================================================

@app.post("/api/watched-playlists")
def add_watched_playlist(request: WatchedPlaylistRequest):
    """Add a new playlist to watch for new tracks"""
    platform, playlist_ext_id = detect_playlist_platform(request.url)

    with db_conn() as conn:
        # Check for duplicate
        conn.row_factory = sqlite3.Row

        existing = conn.execute(
            "SELECT id FROM watched_playlists WHERE url = ?", (request.url,)
        ).fetchone()

        if existing:
            raise HTTPException(status_code=409, detail="This playlist is already being watched")

        # Fetch playlist to get name and initial tracks
        try:
            tracks, playlist_name = fetch_playlist_tracks(request.url, platform)
        except HTTPException:
            raise

        # Create playlist record
        playlist_id = str(uuid.uuid4())[:8]

        conn.execute("""
            INSERT INTO watched_playlists
            (id, url, name, platform, refresh_interval_hours, convert_to_flac, last_track_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (playlist_id, request.url, playlist_name, platform,
              request.refresh_interval_hours, int(request.convert_to_flac), len(tracks)))

        # Insert all current tracks as "seen"
        for artist, title in tracks:
            track_hash = hash_track(artist, title)
            conn.execute("""
                INSERT OR IGNORE INTO watched_playlist_tracks
                (playlist_id, track_hash, artist, title)
                VALUES (?, ?, ?, ?)
            """, (playlist_id, track_hash, artist, title))

        conn.commit()

    import_id = None
    if tracks:
        import_id = start_bulk_import_for_tracks(
            tracks,
            request.convert_to_flac,
            watch_playlist_id=playlist_id
        )

    return {
        "id": playlist_id,
        "name": playlist_name,
        "platform": platform,
        "track_count": len(tracks),
        "refresh_interval_hours": request.refresh_interval_hours,
        "import_id": import_id,
        "message": f"Now watching '{playlist_name}' with {len(tracks)} tracks queued for download"
    }


@app.get("/api/watched-playlists")
def list_watched_playlists():
    """List all watched playlists"""
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        playlists = conn.execute("""
            SELECT
                wp.*,
                (SELECT COUNT(*) FROM watched_playlist_tracks wpt WHERE wpt.playlist_id = wp.id) as tracked_count,
                (SELECT COUNT(*) FROM watched_playlist_tracks wpt WHERE wpt.playlist_id = wp.id AND wpt.downloaded_at IS NOT NULL) as downloaded_count
            FROM watched_playlists wp
            ORDER BY wp.created_at DESC
        """).fetchall()

    return {
        "playlists": [dict(p) for p in playlists]
    }


@app.get("/api/watched-playlists/schedule")
def get_watched_schedule():
    """Get the current watched playlist check schedule"""
    return {
        "check_interval_hours": WATCHED_PLAYLIST_CHECK_HOURS,
        "enabled": WATCHED_PLAYLIST_CHECK_HOURS > 0
    }


@app.get("/api/watched-playlists/{playlist_id}")
def get_watched_playlist(playlist_id: str):
    """Get details of a watched playlist including track history"""
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        playlist = conn.execute(
            "SELECT * FROM watched_playlists WHERE id = ?", (playlist_id,)
        ).fetchone()

        if not playlist:
            raise HTTPException(status_code=404, detail="Watched playlist not found")

        tracks = conn.execute("""
            SELECT * FROM watched_playlist_tracks
            WHERE playlist_id = ?
            ORDER BY first_seen DESC
        """, (playlist_id,)).fetchall()

    return {
        "playlist": dict(playlist),
        "tracks": [dict(t) for t in tracks]
    }


@app.put("/api/watched-playlists/{playlist_id}")
def update_watched_playlist(playlist_id: str, request: WatchedPlaylistUpdate):
    """Update watched playlist settings"""
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        playlist = conn.execute(
            "SELECT * FROM watched_playlists WHERE id = ?", (playlist_id,)
        ).fetchone()

        if not playlist:
            raise HTTPException(status_code=404, detail="Watched playlist not found")

        # Build update query
        updates = []
        params = []

        if request.refresh_interval_hours is not None:
            updates.append("refresh_interval_hours = ?")
            params.append(request.refresh_interval_hours)

        if request.enabled is not None:
            updates.append("enabled = ?")
            params.append(int(request.enabled))

        if request.convert_to_flac is not None:
            updates.append("convert_to_flac = ?")
            params.append(int(request.convert_to_flac))

        if updates:
            params.append(playlist_id)
            conn.execute(
                f"UPDATE watched_playlists SET {', '.join(updates)} WHERE id = ?",
                params
            )
            conn.commit()

        # Fetch updated record
        updated = conn.execute(
            "SELECT * FROM watched_playlists WHERE id = ?", (playlist_id,)
        ).fetchone()

    return {"playlist": dict(updated)}


@app.delete("/api/watched-playlists/{playlist_id}")
def delete_watched_playlist(playlist_id: str):
    """Remove a watched playlist and its track history"""
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        playlist = conn.execute(
            "SELECT * FROM watched_playlists WHERE id = ?", (playlist_id,)
        ).fetchone()

        if not playlist:
            raise HTTPException(status_code=404, detail="Watched playlist not found")

        # Delete tracks first (FK constraint)
        conn.execute("DELETE FROM watched_playlist_tracks WHERE playlist_id = ?", (playlist_id,))
        conn.execute("DELETE FROM watched_playlists WHERE id = ?", (playlist_id,))
        conn.commit()

    return {"message": f"Deleted watched playlist '{playlist['name']}'"}


@app.post("/api/watched-playlists/{playlist_id}/refresh")
def refresh_single_playlist(playlist_id: str):
    """Force an immediate refresh of a specific watched playlist"""
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        playlist = conn.execute(
            "SELECT * FROM watched_playlists WHERE id = ?", (playlist_id,)
        ).fetchone()

        if not playlist:
            raise HTTPException(status_code=404, detail="Watched playlist not found")

    result = refresh_watched_playlist(playlist_id)
    return result


@app.post("/api/watched-playlists/check-all")
def check_all_watched_playlists():
    """Check all playlists due for refresh (called by cron)"""
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        playlists = conn.execute("""
            SELECT id, name FROM watched_playlists
            WHERE enabled = 1
            AND (last_checked IS NULL
                 OR datetime(last_checked, '+' || refresh_interval_hours || ' hours') < datetime('now'))
        """).fetchall()

    if not playlists:
        return {"checked": 0, "message": "No playlists due for refresh", "results": []}

    results = []
    for playlist in playlists:
        result = refresh_watched_playlist(playlist["id"])
        results.append(result)

    total_new = sum(r.get("new_tracks", 0) for r in results)
    total_queued = sum(r.get("queued", 0) for r in results)

    return {
        "checked": len(results),
        "total_new_tracks": total_new,
        "total_queued": total_queued,
        "results": results
    }


# =============================================================================
# Start Background Scheduler
# =============================================================================

start_scheduler()


# =============================================================================
# File System Sync Scheduler
# =============================================================================

def sync_file_system():
    """Sync database with actual files on disk.

    Runs periodically to update file paths when users manually move files.
    Mark jobs as file_deleted=True if the file no longer exists.
    """
    try:
        from utils import check_duplicate

        with db_conn() as conn:
            conn.row_factory = sqlite3.Row

            # Get all completed jobs
            jobs = conn.execute(
                "SELECT id, artist, title, file_deleted FROM jobs WHERE status = 'completed'"
            ).fetchall()

            updated_count = 0
            for job in jobs:
                artist = job["artist"] or ""
                title = job["title"] or ""

                # Skip if no artist/title
                if not artist or not title:
                    continue

                # Check if file still exists
                existing = check_duplicate(artist, title)

                if existing:
                    # File exists, make sure file_deleted is False
                    if job["file_deleted"]:
                        conn.execute(
                            "UPDATE jobs SET file_deleted = 0 WHERE id = ?",
                            (job["id"],)
                        )
                        updated_count += 1
                else:
                    # File doesn't exist, mark as deleted
                    if not job["file_deleted"]:
                        conn.execute(
                            "UPDATE jobs SET file_deleted = 1 WHERE id = ?",
                            (job["id"],)
                        )
                        updated_count += 1

            conn.commit()

        if updated_count > 0:
            print(f"File system sync: updated {updated_count} jobs")

    except Exception as e:
        print(f"File system sync error: {e}")


def start_file_system_sync():
    """Start periodic file system sync in background."""
    import threading
    import time

    def sync_worker():
        while True:
            try:
                sync_file_system()
            except Exception as e:
                print(f"File system sync worker error: {e}")
            # Sync every hour
            time.sleep(3600)

    thread = threading.Thread(target=sync_worker, daemon=True)
    thread.start()
    print("File system sync scheduler started (every hour)")


# Start file system sync
start_file_system_sync()


# =============================================================================
# Start Telegram Bot (if enabled)
# =============================================================================

try:
    from telegram_bot import run_bot_process
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        run_bot_process()
        print("Telegram bot started in background process")
    else:
        print("Telegram bot disabled (set TELEGRAM_BOT_TOKEN to enable)")
except ImportError:
    print("python-telegram-bot not installed, Telegram bot disabled")
except Exception as e:
    print(f"Failed to start Telegram bot: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
