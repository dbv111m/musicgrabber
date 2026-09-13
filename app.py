#!/usr/bin/env python3
"""
Music Grabber - A self-hosted music acquisition service
Searches music sources, downloads best quality audio with optional conversion to FLAC/Opus/MP3, drops into Navidrome/Jellyfin library
"""

import csv
import io
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import httpx

from constants import (
    VERSION, MUSIC_DIR, DB_PATH, COOKIES_FILE, ROOT_PATH,
    TIMEOUT_YTDLP_INFO,
    TIMEOUT_YTDLP_PREVIEW,
    TIMEOUT_SLSKD_SEARCH,
    WATCHED_PLAYLIST_CHECK_HOURS,
    SEARCH_LOG_RETENTION_DAYS,
    STALE_JOB_TIMEOUT,
    DOWNLOAD_TOKEN_TTL_SECONDS,
    STREAM_TOKEN_TTL_SECONDS,
    AUDIO_EXTENSIONS,
    SEARCH_SLOT_WAIT_INTERACTIVE,
    SEARCH_SLOT_WAIT_AUTOMATED,
)
from db import (
    db_conn, init_db, start_stale_job_monitor, cleanup_stale_jobs,
    cleanup_old_search_logs, upsert_album_track_lock, save_search_decision,
    reconcile_deleted_library_files,
)
from settings import (
    get_setting, get_setting_bool, set_setting, set_user_setting, get_singles_dir, get_playlists_dir,
    get_albums_dir, migrate_trash_to_per_user,
    SETTINGS_SCHEMA, SENSITIVE_SETTINGS, USER_SETTINGS_KEYS, _get_typed_setting, _is_env_override,
)
from source_urls import is_valid_source_url, validate_source_url
from models import (
    SearchRequest, DownloadRequest, PlaylistFetchRequest,
    AsyncBulkImportRequest, WatchedPlaylistRequest, WatchedPlaylistUpdate,
    WatchedArtistRequest, WatchedArtistUpdate,
    SettingsUpdate, SearchResult, BlacklistRequest,
    TestSlskdRequest, TestNavidromeRequest, TestJellyfinRequest, TestPlexRequest, TestLidarrRequest, TestYouTubeCookiesRequest,
    TestAppriseRequest, TestEmailRequest, TestSpotifyCookiesRequest, RetryMissingTrackRequest, QueueMissingTrackCandidateRequest,
    QueueRescueCandidateRequest,
    OrphanMoveRequest, MaintenanceRequest,
    AlbumDownloadRequest, ReleaseGroupResolveRequest, ExploreRequest, PatchTagsRequest,
    LoginRequest, ChangePasswordRequest, CreateUserRequest,
    SetUserPasswordRequest, SetUserRoleRequest,
    DownloadTokenRequest,
    LongformDetectRequest, LongformSplitRequest,
)
from middleware import AuthMiddleware, invalidate_users_cache
from auth import (
    verify_password, create_session, delete_session, get_user_by_username,
    get_user_by_id, list_users, create_user, update_password, delete_user,
    clear_failed_login, create_download_token, create_stream_token, is_login_allowed,
    password_hash_for_timing, record_failed_login,
)
from youtube import (
    _has_valid_cookie_entries, _cookie_lines_for_domain_check, _sync_cookies_file,
    _ytdlp_base_args, _is_ytdlp_403, parse_duration,
    get_cookies_expiry, _parse_query_artist_title,
)
from search import (
    search_source, search_all, _search_all_events, get_available_sources,
    clear_automated_search_cache, SOURCE_REGISTRY, project_search_result,
    SourceSearchBusy,
)
from library_scans import snapshots, scan_playlists, scan_storage
import servicecheck
import monochrome_browser
from slskd import slskd_enabled, search_slskd
from downloads import (
    process_acquisition_cycle, process_download, process_playlist_download,
    process_slskd_download,
    rebuild_watched_playlist_m3u,
    trigger_navidrome_scan, trigger_jellyfin_scan, trigger_plex_scan,
    jellyfin_auth_headers,
    check_navidrome_duplicate, check_lidarr_duplicate,
    get_job_source_allowlist,
)
from acquisition import (
    acquisition_candidate_summary,
    attach_job_to_acquisition,
    begin_acquisition_cycle,
    ensure_acquisition_target,
    finish_acquisition_cycle,
    parse_allowed_sources,
    rank_automatic_candidates,
    stored_rescue_candidates,
)
from bulk_import import (
    clean_bulk_import_line,
    start_bulk_import_for_tracks,
    process_bulk_import_worker,
    request_bulk_import_cancellation,
)
from watched_playlists import (
    detect_playlist_platform, fetch_playlist_tracks, refresh_watched_playlist,
    fetch_listenbrainz_createdfor, start_scheduler, wake_scheduler,
)
from watched_artists import refresh_watched_artist, start_artist_scheduler, wake_artist_scheduler, seed_artist_albums
import discovery
import library_index
from library_index import start_library_index_scheduler
from upgrades import (
    start_upgrade_scheduler, run_scan_all,
    get_candidates_page, search_candidate, dismiss_candidate,
    perform_upgrade, perform_upgrade_all, get_upgrade_all_progress,
    mark_upgrade_all_starting,
    list_quarantine, restore_from_quarantine, delete_quarantine_entry,
    delete_orphaned_quarantine_file,
)
from audio_provenance import (
    get_audit_export,
    get_audit_files,
    get_audit_status,
    prioritise_over_audio_audit,
    start_audit_scan,
)
from metadata import (
    search_artist_mbid, fetch_artist_albums, fetch_album_tracks, resolve_album_release,
    apply_metadata_to_file, guess_musicbrainz_tags, MusicBrainzUnavailable,
    parse_musicbrainz_release_url, fetch_release_summary, search_release_groups,
)
from album_urls import resolve_album_url
from albums import (
    album_audio_file_count,
    album_track_status,
    queue_album_download,
    album_on_disk,
    watched_album_display_state,
    AlbumTracklistUnavailable,
    _normalise_album_match_text,
)
from utils import authoritative_audio_file, hash_track, is_valid_youtube_id, iter_library_audio_files, sanitize_filename, sanitize_playlist_name, set_file_permissions, spawn_daemon_thread, subsonic_auth_params
from coverart import fetch_cover_art_url
from notifications import send_test_email
from longform_split import (
    detect_longform_candidate, validate_segments, create_longform_split, cancel_longform_split,
)

URL_BASED_SOURCES = {"soundcloud", "mp3phoenix", "zvu4no", "freemp3cloud", "monochrome"}
DIRECT_PREVIEW_SOURCES = {"zvu4no", "freemp3cloud"}
MONOCHROME_PREVIEW_SOURCES = {"monochrome"}


def _request_root_path(request: Request | None = None) -> str:
    """Return the externally-visible app prefix, normalised like '/musicgrabber'."""
    raw = ""
    if request is not None:
        raw = (request.scope.get("root_path") or "").strip()
    raw = raw or ROOT_PATH
    if not raw or raw == "/":
        return ""
    return "/" + raw.strip("/")


def _app_path(path: str, request: Request | None = None) -> str:
    """Prefix an app-local path with the configured root path."""
    path = path if path.startswith("/") else f"/{path}"
    root_path = _request_root_path(request)
    return f"{root_path}{path}" if root_path else path


def _user_scope(user_id: str | None, is_admin: bool) -> tuple[str, tuple]:
    """Return a SQL WHERE fragment and params for scoping rows to the current user.

    Admins see their own rows plus legacy rows (user_id IS NULL) left over from
    single-user mode. Regular users see only their own rows; legacy rows are
    the admin's history, not theirs.

    Usage:
        frag, params = _user_scope(user_id, is_admin)
        conn.execute(f"SELECT ... FROM jobs WHERE {frag}", params)
    """
    if is_admin:
        return "(user_id = ? OR user_id IS NULL)", (user_id,)
    return "user_id = ?", (user_id,)


def _is_peon(request: Request) -> bool:
    user = getattr(request.state, "user", None) or {}
    return user.get("role") == "peon"


def _enforce_peon_format(request: Request, body) -> None:
    """Peons cannot toggle conversion on or off. Force their requests onto whatever
    the admin has set globally for the default conversion mode. Belt to the UI's braces:
    the toggle is hidden, but a peon with the dev tools open could still try to send
    their own value, so we overwrite it server-side."""
    if not _is_peon(request):
        return
    if hasattr(body, "convert_audio"):
        body.convert_audio = get_setting_bool("default_convert_audio", False)


# =============================================================================
# Application Setup
# =============================================================================

app = FastAPI(title="Music Grabber", version=VERSION, root_path=ROOT_PATH)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Ensure directories exist
get_singles_dir().mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Initialise database and start background monitors
init_db()
migrate_trash_to_per_user()  # Reshape the bin if the account count has changed
cleanup_old_search_logs(SEARCH_LOG_RETENTION_DAYS)
start_stale_job_monitor()
start_scheduler()
start_artist_scheduler()
start_upgrade_scheduler()
# The library index reconciles on its own, well after the schedulers above have
# settled, and nothing reads it yet except the storage tile. If it cannot open
# its database it says so once and everything carries on walking the disk.
start_library_index_scheduler()
servicecheck.start_health_checks()  # initial background health sweep at boot

# Warm Monochrome's browser session at boot so the first hover preview (or
# download) isn't the poor soul paying Chrome's start-up bill. Quietly does
# nothing unless Monochrome is an enabled source with the browser leg on.
spawn_daemon_thread(monochrome_browser.warm_browser_broker)

# Sync cookies file from settings at startup
_sync_cookies_file()

# Register middleware
app.add_middleware(AuthMiddleware)


# =============================================================================
# Basic Routes
# =============================================================================

def _static_cache_bust() -> str:
    """Cache-bust token for static assets.

    VERSION alone is not enough: within a (DEV) cycle the version stays put while
    app.js / index.html change repeatedly, so the browser would happily serve a
    stale cached app.js against the same ?v=. We fold in the newest static-file
    mtime (set fresh by the Docker COPY on every build) so any change to the
    served files busts the cache, version bump or not.
    """
    try:
        static_dir = Path("static")
        names = ("app.js", "index.html", "release-notes.js")
        mtimes = [(static_dir / n).stat().st_mtime for n in names if (static_dir / n).exists()]
        stamp = int(max(mtimes)) if mtimes else 0
        return f"{VERSION}.{stamp}"
    except Exception:
        return VERSION


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    """Serve the main UI"""
    html = Path("static/index.html").read_text(encoding="utf-8")
    root_path = _request_root_path(request)
    html = html.replace("__ROOT_PATH__", root_path)
    # Cache-bust static assets so browser fetches fresh files after an update
    html = html.replace("__CACHE_BUST__", _static_cache_bust())
    return HTMLResponse(content=html)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse("static/favicon.png", media_type="image/png")

def _is_volume_mounted() -> bool:
    """Check if the configured music directory appears to be a mounted volume.

    Compares device IDs: if the music dir is on a different device than /,
    it's likely a mounted volume. This helps detect misconfigured setups
    where users forgot to mount their music directory.
    """
    try:
        configured_music_dir = Path(get_setting("music_dir", str(MUSIC_DIR)))
        root_stat = os.stat("/")
        music_stat = os.stat(configured_music_dir)
        # Different device ID means it's a mount point
        return root_stat.st_dev != music_stat.st_dev
    except OSError:
        # Can't stat, assume it's fine
        return True


@app.get("/api/config")
def get_config(request: Request):
    """Expose server configuration and version for the UI"""
    api_key = get_setting("api_key", "")
    organise_by_artist = get_setting_bool("organise_by_artist", True)
    configured_music_dir = Path(get_setting("music_dir", str(MUSIC_DIR)))
    singles_dir = get_singles_dir()
    playlists_dir = get_playlists_dir()

    # Build human-readable example paths relative to the music root so the
    # frontend can show the user exactly where their files will land.
    try:
        singles_example = str(singles_dir.relative_to(configured_music_dir))
    except ValueError:
        singles_example = str(singles_dir)
    if organise_by_artist:
        singles_example += "/Artist Name"
    singles_example += "/Track Title.flac"

    playlists_example = None
    if playlists_dir:
        try:
            pl_rel = str(playlists_dir.relative_to(configured_music_dir))
        except ValueError:
            pl_rel = str(playlists_dir)
        playlists_example = f"{pl_rel}/Playlist Name/Artist - Title.flac"

    with db_conn() as conn:
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    # Multi-user mode kicks in only when there are 2+ accounts. A single-user
    # install with one account (the owner) stays login-free, same as no-account mode.
    users_exist = user_count >= 2

    default_convert_audio = get_setting_bool("default_convert_audio", False)
    return {
        "version": VERSION,
        "default_convert_audio": default_convert_audio,
        # Compatibility for pre-v4 browsers and API clients. The stored setting and
        # its column have both been renamed; this key is emitted purely so an older
        # cached frontend or a third-party script does not lose its footing.
        "default_convert_to_flac": default_convert_audio,
        "audio_format": get_setting("audio_format", "opus"),
        "playlists_subdir": get_setting("playlists_subdir", ""),
        "organise_by_artist": organise_by_artist,
        "singles_path_example": singles_example,
        "playlists_path_example": playlists_example,
        "music_dir": get_setting("music_dir", str(MUSIC_DIR)),
        "root_path": _request_root_path(request),
        "auth_required": bool(api_key),
        "auth_mode": "session",
        "users_exist": users_exist,
        "volume_mounted": _is_volume_mounted(),
        "singles_only_mode": get_setting_bool("singles_only_mode", False),
    }


# =============================================================================
# Music directory listing (for subfolder picker)
# =============================================================================

@app.get("/api/music-dirs")
@prioritise_over_audio_audit
def list_music_dirs(path: str = "", recursive: bool = False, max_depth: int | None = None):
    """List subdirectories of MUSIC_DIR (or a subpath) for the subfolder picker.

    Returns directory names/paths only  -  no hidden/system dirs, sorted alphabetically.
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

    configured_music_dir = Path(get_setting("music_dir", str(MUSIC_DIR)))
    target = configured_music_dir / clean if clean else configured_music_dir
    target_resolved = target.resolve()
    music_root = configured_music_dir.resolve()
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


# =============================================================================
# Playlist listing (watched + physical .m3u files)
# =============================================================================

@app.get("/api/playlists")
@prioritise_over_audio_audit
def list_playlists(http_request: Request):
    """List available playlists for the playlist routing selector.

    Returns watched playlists (from DB) and any physical .m3u files found in the
    Playlists directory. Watched playlists take priority if names collide.
    """
    user_id = getattr(http_request.state, "user_id", None)
    is_admin = getattr(http_request.state, "is_admin", False)
    results = {}

    # Physical .m3u files from Playlists dir (lowest priority)
    playlists_dir = get_playlists_dir(user_id=user_id)
    physical, scan_state = snapshots.get(
        ("playlists", user_id, str(playlists_dir)),
        lambda: scan_playlists(playlists_dir),
    )
    for name in physical or []:
        results[name] = {
            "name": name, "is_watched": False, "watched_id": None, "platform": None,
        }

    # Watched playlists from DB (higher priority  -  overwrite any same-named .m3u entry)
    _scope_frag, _scope_params = _user_scope(user_id, is_admin)
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT id, name, platform, sync_mode FROM watched_playlists WHERE {_scope_frag} ORDER BY name COLLATE NOCASE",
            _scope_params
        ).fetchall()

    for row in rows:
        results[row["name"]] = {
            "name": row["name"],
            "is_watched": True,
            "watched_id": row["id"],
            "platform": row["platform"],
            "sync_mode": row["sync_mode"] or "append",
        }

    return {"playlists": sorted(results.values(), key=lambda p: p["name"].casefold()),
            "scan_state": scan_state, "scan_ready": physical is not None}


# =============================================================================
# Auth & User Management Routes
# =============================================================================

@app.post("/api/auth/login")
def login(body: LoginRequest):
    allowed, _retry_after = is_login_allowed(body.username)
    if not allowed:
        # Keep the same generic response to avoid leaking account state.
        raise HTTPException(status_code=401, detail="Invalid username or password")

    user = get_user_by_username(body.username)
    password_ok = verify_password(body.password, password_hash_for_timing(user))
    if not user or not user.get("is_active") or not password_ok:
        record_failed_login(body.username)
        raise HTTPException(status_code=401, detail="Invalid username or password")

    clear_failed_login(body.username)
    token = create_session(user["id"])
    return {
        "token": token,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "force_password_change": bool(user["force_password_change"]),
        },
    }


@app.post("/api/auth/logout")
def logout(request: Request):
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token:
            delete_session(token)
    return {"ok": True}


@app.get("/api/auth/me")
def get_me(request: Request):
    if request.state.user_id is None:
        return {"id": None, "username": "admin", "role": "admin", "force_password_change": False}
    return request.state.user


@app.post("/api/auth/download-token")
def issue_download_token(request: Request, body: DownloadTokenRequest):
    """Issue a short-lived single-use token for downloading one specific job."""
    job_id = (body.job_id or "").strip()
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id is required")

    # Single-user mode has no session token in play; direct links are fine.
    if request.state.user_id is None:
        return {"url": _app_path(f"/api/jobs/{job_id}/download", request), "expires_in": 0}

    user_id = request.state.user_id
    is_admin = request.state.is_admin
    with db_conn() as conn:
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        row = conn.execute(
            f"""SELECT 1 FROM jobs
                WHERE id = ?
                  AND status IN ('completed', 'completed_with_errors')
                  AND COALESCE(file_deleted, 0) = 0
                  AND {_scope_frag}
                LIMIT 1""",
            (job_id, *_scope_params),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")

    token = create_download_token(user_id, job_id)
    return {
        "url": _app_path(f"/api/jobs/{job_id}/download?download_token={token}", request),
        "expires_in": DOWNLOAD_TOKEN_TTL_SECONDS,
    }


@app.post("/api/auth/stream-token")
def issue_stream_token(request: Request):
    """Issue a short-lived reusable token so an <audio> element can play a file.

    The browser cannot bolt an Authorization header onto `audio.src`, so without
    this every play button gets a cheerful 401 and the UI mutters "Could not play
    file". The token says who is asking and nothing more; the stream endpoints
    still scope what they will serve.
    """
    # Single-user mode has no session token in play; the endpoints are open anyway.
    if request.state.user_id is None:
        return {"token": "", "expires_in": 0}

    token = create_stream_token(request.state.user_id)
    return {"token": token, "expires_in": STREAM_TOKEN_TTL_SECONDS}


@app.put("/api/auth/password")
def change_own_password(request: Request, body: ChangePasswordRequest):
    if request.state.user_id is None:
        raise HTTPException(status_code=400, detail="Password change not available in single-user mode")
    user = get_user_by_id(request.state.user_id)
    if not user or not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password incorrect")
    if not body.new_password or len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    current_token = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        current_token = auth_header[7:].strip() or None
    update_password(request.state.user_id, body.new_password, keep_session_token=current_token)
    return {"ok": True}


@app.get("/api/users")
def get_users(request: Request):
    if not request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return {"users": list_users()}


@app.post("/api/users")
def create_new_user(request: Request, body: CreateUserRequest):
    if not request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    if body.role not in ("admin", "user", "peon"):
        raise HTTPException(status_code=400, detail="Role must be 'admin', 'user', or 'peon'")
    if not body.username:
        raise HTTPException(status_code=400, detail="Username cannot be empty")
    if not body.password or len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    # First user must be admin; they inherit the single-user instance
    with db_conn() as conn:
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if user_count == 0 and body.role != "admin":
        raise HTTPException(status_code=400, detail="The first account must be an admin")
    first_user = user_count == 0
    # Adding the 2nd account flips us from single-user to session-required mode.
    # The current admin has no session, so subsequent requests will 401; the
    # frontend uses this flag to bounce them to login instead of leaving the
    # user list stuck on the old single-user view.
    crosses_into_session_mode = user_count == 1
    try:
        new_id = create_user(body.username, body.password, body.role)
    except ValueError:
        raise HTTPException(status_code=409, detail="Username already exists")
    invalidate_users_cache()
    return {
        "ok": True,
        "user_id": new_id,
        "first_user": first_user,
        "requires_login": crosses_into_session_mode,
    }


@app.delete("/api/users/{user_id}")
def remove_user(request: Request, user_id: str):
    if not request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    if user_id == request.state.user_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    if not get_user_by_id(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    delete_user(user_id)
    invalidate_users_cache()
    return {"ok": True}


@app.put("/api/users/{user_id}/password")
def admin_set_password(request: Request, user_id: str, body: SetUserPasswordRequest):
    if not request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    if not get_user_by_id(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    if not body.new_password or len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    update_password(user_id, body.new_password)
    return {"ok": True}


@app.put("/api/users/{user_id}/role")
def set_user_role(request: Request, user_id: str, body: SetUserRoleRequest):
    if not request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    if body.role not in ("admin", "user", "peon"):
        raise HTTPException(status_code=400, detail="Role must be 'admin', 'user', or 'peon'")
    if not get_user_by_id(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    if user_id == request.state.user_id:
        raise HTTPException(status_code=400, detail="Cannot change your own role")
    with db_conn() as conn:
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (body.role, user_id))
        conn.commit()
    return {"ok": True}


@app.put("/api/users/{user_id}/force-password-change")
def flag_password_reset(request: Request, user_id: str):
    """Flag a user's account so they must set a new password on next login."""
    if not request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    if user_id == request.state.user_id:
        raise HTTPException(status_code=400, detail="Cannot force-reset your own password")
    if not get_user_by_id(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    with db_conn() as conn:
        conn.execute("UPDATE users SET force_password_change = 1 WHERE id = ?", (user_id,))
        # Also kill all their active sessions; they'll need to log in fresh and change immediately
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.commit()
    return {"ok": True}


# =============================================================================
# Settings API
# =============================================================================

def _clear_spotify_cookies_expired(user_id: str | None) -> None:
    """Reset the expired flag after fresh Spotify cookies are saved."""
    if user_id:
        set_user_setting(user_id, "spotify_cookies_expired", "false")
    else:
        set_setting("spotify_cookies_expired", "false")


@app.get("/api/settings")
def get_settings(request: Request):
    """Get settings for the current user.

    Admins get the full schema (global settings with their user_settings merged on top).
    Regular users get only their user-scoped keys, falling back to the global value.
    """
    settings = {}
    env_overrides = []

    # Determine which keys to expose for this request.
    # Admins see everything; regular users see only their own slice.
    if request.state.is_admin:
        keys_to_return = SETTINGS_SCHEMA.keys()
    else:
        keys_to_return = USER_SETTINGS_KEYS

    user_id = request.state.user_id

    for key in keys_to_return:
        schema = SETTINGS_SCHEMA.get(key, {"type": "str", "default": "", "sensitive": False})
        # Use user_id so per-user overrides are applied where relevant
        value = _get_typed_setting(key, user_id=user_id)
        is_sensitive = schema.get("sensitive", False)

        # Only expose env-override information for keys the caller can see
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
def update_settings(updates: SettingsUpdate, request: Request):
    """Update settings. Only non-None values are updated. Returns updated settings.

    Per-user keys are written to user_settings (or global when in single-user mode).
    Global keys can only be written by admins. Peons are read-only; they inherit
    the admin's globals for everything and have no Settings UI to begin with.
    """
    # Peons have no business writing settings; Settings tab is hidden, but the
    # API endpoint stays open to other roles, so this is the belt to the UI's braces.
    user = getattr(request.state, "user", None) or {}
    if user.get("role") == "peon":
        raise HTTPException(status_code=403, detail="Read-only account")

    updated_keys = []
    user_id = request.state.user_id

    for key, value in updates.model_dump(exclude_none=True).items():
        if key not in SETTINGS_SCHEMA:
            continue

        # Env-var overrides are immutable; skip silently
        if _is_env_override(key):
            continue

        # Convert booleans to string for storage
        if isinstance(value, bool):
            value = "true" if value else "false"
        else:
            value = str(value)

        # Validate subfolder settings to keep writes under MUSIC_DIR.
        if key in ("singles_subdir", "playlists_subdir", "albums_subdir"):
            raw = value.strip().replace("\\", "/")
            if raw == "." or raw == "":
                value = raw
            else:
                parts = [
                    part.strip()
                    for part in raw.split("/")
                    if part.strip() and part.strip() != "."
                ]
                if any(part == ".." for part in parts):
                    raise HTTPException(status_code=400, detail=f"Invalid {key.replace('_', ' ')} path")
                default_subdir = {
                    "singles_subdir": "Singles",
                    "playlists_subdir": "Playlists",
                    "albums_subdir": "Albums",
                }[key]
                value = "/".join(parts) or default_subdir
                try:
                    configured_music_dir = Path(get_setting("music_dir", str(MUSIC_DIR)))
                    (configured_music_dir / value).resolve().relative_to(configured_music_dir.resolve())
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"{key.replace('_', ' ')} must stay within music directory")

        # Only allow sensible chmod values; a free-text octal field is a footgun
        if key == "file_permissions" and value not in ("666", "777"):
            raise HTTPException(status_code=400, detail="file_permissions must be 666 or 777")
        if key == "search_concurrency" and not 1 <= int(value) <= 5:
            raise HTTPException(status_code=400, detail="search_concurrency must be between 1 and 5")

        # Validate cookie format before saving
        if key == "youtube_cookies" and value.strip() and not _has_valid_cookie_entries(value):
            raise HTTPException(
                status_code=400,
                detail="Invalid cookies format. Paste Netscape-format cookies.txt content."
            )
        if key == "spotify_cookies" and value.strip():
            if not _has_valid_cookie_entries(value):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid cookies format. Paste Netscape-format cookies.txt content."
                )
            lines = _cookie_lines_for_domain_check(value)
            has_sp_dc = any(
                len(l.split("\t")) >= 7 and l.split("\t")[5] == "sp_dc"
                for l in lines if not l.startswith("#")
            )
            if not has_sp_dc:
                raise HTTPException(
                    status_code=400,
                    detail="No sp_dc cookie found. Export cookies from open.spotify.com while logged in."
                )

        # Route the write: user-scoped keys go to user_settings (or global in single-user mode);
        # global keys require admin privileges.
        if key in USER_SETTINGS_KEYS:
            if user_id is None:
                # Single-user mode: no user table row, fall back to global store
                set_setting(key, value)
            else:
                set_user_setting(user_id, key, value)
        else:
            # Global setting: only admins may touch these
            if not request.state.is_admin:
                continue
            set_setting(key, value)

        updated_keys.append(key)

    # Sync cookies file if YouTube cookies were updated
    if "youtube_cookies" in updated_keys:
        _sync_cookies_file()

    # Clear the expired flag when fresh Spotify cookies are saved
    if "spotify_cookies" in updated_keys:
        _clear_spotify_cookies_expired(user_id)

    return {
        "updated": updated_keys,
        "settings": get_settings(request)["settings"]
    }


def _maintenance_orphan_import_ids(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0] for row in conn.execute(
            """SELECT bi.id
               FROM bulk_imports bi
               LEFT JOIN watched_playlists wp ON wp.id = bi.watch_playlist_id
               LEFT JOIN watched_artists wa ON wa.id = bi.watch_artist_id
               WHERE (bi.watch_playlist_id IS NOT NULL AND wp.id IS NULL)
                  OR (bi.watch_artist_id IS NOT NULL AND wa.id IS NULL)"""
        ).fetchall()
    ]


@app.get("/api/settings/maintenance")
def maintenance_preview(http_request: Request):
    """Preview database maintenance without changing anything."""
    if not http_request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    with db_conn() as conn:
        orphan_ids = _maintenance_orphan_import_ids(conn)
        counts = {
            status: conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = ?", (status,)
            ).fetchone()[0]
            for status in ("completed", "completed_with_errors", "failed", "cancelled")
        }
        indexed_paths = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE final_path IS NOT NULL AND final_path != ''"
        ).fetchone()[0]
    return {
        "orphan_imports": len(orphan_ids),
        "queue_history": counts,
        "indexed_paths": indexed_paths,
    }


def _purge_orphan_imports(conn: sqlite3.Connection) -> int:
    orphan_ids = _maintenance_orphan_import_ids(conn)
    for import_id in orphan_ids:
        target_ids = [
            row[0] for row in conn.execute(
                "SELECT acquisition_target_id FROM bulk_import_tracks WHERE import_id = ? AND acquisition_target_id IS NOT NULL",
                (import_id,),
            ).fetchall()
        ]
        conn.execute("DELETE FROM bulk_import_tracks WHERE import_id = ?", (import_id,))
        conn.execute("DELETE FROM bulk_imports WHERE id = ?", (import_id,))
        conn.executemany(
            "DELETE FROM acquisition_targets WHERE id = ?",
            [(target_id,) for target_id in target_ids],
        )
    return len(orphan_ids)


def _rebuild_library_index() -> int:
    """Add history rows for audio under configured roots which lacks an exact path."""
    roots = [get_singles_dir(), get_playlists_dir(), get_albums_dir()]
    with db_conn() as conn:
        known = {
            row[0] for row in conn.execute(
                "SELECT final_path FROM jobs WHERE final_path IS NOT NULL AND final_path != ''"
            ).fetchall()
        }
    rebuilt = []
    seen = set()
    for root in roots:
        if root is None or not root.exists():
            continue
        # The index already holds every audio file under this root, so an admin
        # rebuild no longer drags the entire library across the mount to be told
        # what it wrote down yesterday. A root it cannot vouch for falls back to
        # the walk: "I saw nothing" and "I looked at nothing" must never be the
        # same answer when the result becomes rows in the jobs table.
        audio_paths = library_index.paths_under(root)
        if audio_paths is None:
            audio_paths = [p for p, _st in iter_library_audio_files(root, AUDIO_EXTENSIONS)]
        # `final_path` is written resolved, so a library reached through a symlink
        # has to be compared resolved or every file already in the history gets a
        # second row. One resolve of the root does the whole job: no scan descends
        # a symlinked directory, so nothing beneath it can resolve anywhere else.
        try:
            base = root.resolve()
        except OSError:
            base = root
        for audio_path in audio_paths:
            try:
                relative = audio_path.relative_to(root)
                artist = relative.parts[-2] if len(relative.parts) > 1 else "Unknown"
                resolved = str(base / relative)
            except ValueError:
                artist = "Unknown"
                resolved = str(audio_path)
            if resolved in known or resolved in seen:
                continue
            seen.add(resolved)
            # Full UUID here, not the usual 8-char job ID. A rebuild can mint tens
            # of thousands of rows in one batch, and 32 bits of birthday paradox
            # says a collision around 50k is a coin toss  -  which would abort the
            # whole insert. Nobody types these IDs, so the extra characters cost us
            # nothing.
            rebuilt.append((str(uuid.uuid4()), audio_path.stem, artist, resolved))
    if rebuilt:
        with db_conn() as conn:
            conn.executemany(
                """INSERT INTO jobs
                   (id, title, artist, status, download_type, source, final_path,
                    file_deleted, created_at, completed_at)
                   VALUES (?, ?, ?, 'completed', 'single', 'library_rebuild', ?, 0,
                           datetime('now'), datetime('now'))""",
                rebuilt,
            )
            conn.commit()
    return len(rebuilt)


@app.post("/api/settings/maintenance")
def run_maintenance(body: MaintenanceRequest, http_request: Request):
    """Run an explicitly confirmed, audio-safe maintenance action."""
    if not http_request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    if body.action == "purge_orphans":
        with db_conn() as conn:
            changed = _purge_orphan_imports(conn)
            conn.commit()
        return {"orphan_imports_removed": changed}
    if body.action == "clear_history":
        allowed = {"completed", "completed_with_errors", "failed", "cancelled"}
        statuses = set(body.statuses) & allowed
        if not statuses:
            raise HTTPException(status_code=400, detail="Select at least one history status")
        placeholders = ",".join("?" for _ in statuses)
        with db_conn() as conn:
            changed = conn.execute(
                f"DELETE FROM jobs WHERE status IN ({placeholders})", tuple(sorted(statuses))
            ).rowcount
            conn.commit()
        return {"queue_rows_removed": changed}
    if body.action in {"reconcile", "clean_rebuild"}:
        marked_deleted, unlinked = reconcile_deleted_library_files(batch_size=1_000_000)
        orphan_count = 0
        rebuilt = 0
        if body.action == "clean_rebuild":
            with db_conn() as conn:
                orphan_count = _purge_orphan_imports(conn)
                conn.commit()
            rebuilt = _rebuild_library_index()
        return {
            "jobs_marked_deleted": marked_deleted,
            "watched_rows_unlinked": unlinked,
            "orphan_imports_removed": orphan_count,
            "library_rows_rebuilt": rebuilt,
        }
    raise HTTPException(status_code=400, detail="Unknown maintenance action")


# =============================================================================
# Settings Test Endpoints
# =============================================================================

@app.post("/api/settings/test/slskd")
def test_slskd_connection(http_request: Request, body: TestSlskdRequest = None):
    """Test connection to slskd server. Uses form values if provided, otherwise saved settings."""
    if not http_request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    url = (body.url if body and body.url else None) or _get_typed_setting("slskd_url")
    user = (body.username if body and body.username else None) or _get_typed_setting("slskd_user")
    password = (body.password if body and body.password else None) or _get_typed_setting("slskd_pass")
    downloads_path = (body.downloads_path if body and body.downloads_path else None) or _get_typed_setting("slskd_downloads_path")

    if not url:
        return {"success": False, "message": "slskd URL not configured"}

    try:
        with httpx.Client(timeout=10) as client:
            auth_response = client.post(
                f"{url.rstrip('/')}/api/v0/session",
                json={"username": user, "password": password}
            )
            if auth_response.status_code == 200:
                if not downloads_path:
                    return {
                        "success": True,
                        "warning": True,
                        "message": "Connected to slskd. Search works. For downloads to import, slskd and MusicGrabber must point at the same folder on your server: mount slskd's completed-downloads directory into MusicGrabber at the same path, then enter that path in the field below. See the README for a step-by-step example.",
                    }
                path = Path(downloads_path).expanduser()
                if not path.exists():
                    return {
                        "success": True,
                        "warning": True,
                        "message": f"Connected to slskd, but downloads path is not visible to MusicGrabber: {downloads_path}",
                    }
                if not path.is_dir():
                    return {
                        "success": True,
                        "warning": True,
                        "message": f"Connected to slskd, but downloads path is not a directory: {downloads_path}",
                    }
                return {"success": True, "message": "Connected to slskd and downloads path is accessible"}
            else:
                return {"success": False, "message": f"Authentication failed: {auth_response.status_code}"}
    except httpx.TimeoutException:
        return {"success": False, "message": "Connection timed out"}
    except Exception as e:
        print(f"slskd connection test error: {type(e).__name__}: {e}")
        return {"success": False, "message": "Connection failed  -  check server logs for details"}


@app.post("/api/settings/test/navidrome")
def test_navidrome_connection(http_request: Request, body: TestNavidromeRequest = None):
    """Test connection to Navidrome server. Uses form values if provided, otherwise saved settings."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    # Non-admins can only test their saved settings, not probe arbitrary URLs
    url = (body.url if body and body.url and is_admin else None) or _get_typed_setting("navidrome_url", user_id=user_id)
    user = (body.username if body and body.username and is_admin else None) or _get_typed_setting("navidrome_user", user_id=user_id)
    password = (body.password if body and body.password and is_admin else None) or _get_typed_setting("navidrome_pass", user_id=user_id)

    if not url:
        return {"success": False, "message": "Navidrome URL not configured"}

    try:
        params = subsonic_auth_params(user, password)

        with httpx.Client(timeout=10) as client:
            response = client.get(
                f"{url.rstrip('/')}/rest/ping",
                params=params
            )
            if response.status_code != 200:
                return {"success": False, "message": f"Connection failed: {response.status_code}"}

            data = response.json()
            if data.get("subsonic-response", {}).get("status") != "ok":
                return {"success": False, "message": "Authentication failed"}

            # Ping succeeded  -  now check and auto-enable real paths via Navidrome's native API.
            # Navidrome returns synthetic paths by default (Artist/Album/Track.mp3), which are
            # useless for M3U playlist entries. We auto-flip any MusicGrabber player records we
            # find that have reportRealPath=false so users don't have to do it manually.
            real_path_enabled = False
            real_path_auto_fixed = False
            try:
                auth_resp = client.post(
                    f"{url.rstrip('/')}/auth/login",
                    json={"username": user, "password": password}
                )
                if auth_resp.status_code == 200:
                    token = auth_resp.json().get("token")
                    if token:
                        nd_headers = {"X-ND-Authorization": f"Bearer {token}"}
                        players_resp = client.get(
                            f"{url.rstrip('/')}/api/player",
                            headers=nd_headers
                        )
                        if players_resp.status_code == 200:
                            for player in players_resp.json():
                                if "musicgrabber" not in (player.get("client") or "").lower():
                                    continue
                                if player.get("reportRealPath"):
                                    real_path_enabled = True
                                else:
                                    # Auto-enable real paths for this player
                                    updated = {**player, "reportRealPath": True}
                                    put_resp = client.put(
                                        f"{url.rstrip('/')}/api/player/{player['id']}",
                                        json=updated,
                                        headers=nd_headers
                                    )
                                    if put_resp.status_code == 200:
                                        real_path_enabled = True
                                        real_path_auto_fixed = True
            except Exception as e:
                print(f"Navidrome real-path check failed: {type(e).__name__}: {e}")  # Non-critical

            if real_path_enabled:
                msg = ("Connected to Navidrome successfully. Real file paths auto-enabled for MusicGrabber."
                       if real_path_auto_fixed else "Connected to Navidrome successfully")
                return {
                    "success": True,
                    "message": msg,
                    "real_path": True
                }
            return {
                "success": True,
                "message": "Connected to Navidrome successfully",
                "real_path": False,
                "real_path_hint": (
                    "Real file paths could not be enabled automatically. Without them, Navidrome "
                    "duplicate detection still works but M3U playlist entries cannot be populated "
                    "for Navidrome-only tracks. To fix this manually, add "
                    "ND_SUBSONIC_DEFAULTREPORTREALPATH=true to your Navidrome docker-compose "
                    "environment, or enable \"Report real path\" for MusicGrabber in "
                    "Navidrome admin > Players."
                )
            }
    except httpx.TimeoutException:
        return {"success": False, "message": "Connection timed out"}
    except Exception as e:
        print(f"Navidrome connection test error: {type(e).__name__}: {e}")
        return {"success": False, "message": "Connection failed  -  check server logs for details"}


@app.post("/api/settings/test/jellyfin")
def test_jellyfin_connection(http_request: Request, body: TestJellyfinRequest = None):
    """Test connection to Jellyfin server. Uses form values if provided, otherwise saved settings."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    # Non-admins can only test their saved settings, not probe arbitrary URLs
    url = (body.url if body and body.url and is_admin else None) or _get_typed_setting("jellyfin_url", user_id=user_id)
    api_key = (body.api_key if body and body.api_key and is_admin else None) or _get_typed_setting("jellyfin_api_key", user_id=user_id)

    if not url:
        return {"success": False, "message": "Jellyfin URL not configured"}
    if not api_key:
        return {"success": False, "message": "Jellyfin API key not configured"}

    try:
        with httpx.Client(timeout=10) as client:
            response = client.get(
                f"{url.rstrip('/')}/System/Info",
                headers=jellyfin_auth_headers(api_key)
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
        print(f"Jellyfin connection test error: {type(e).__name__}: {e}")
        return {"success": False, "message": "Connection failed  -  check server logs for details"}


@app.post("/api/settings/test/plex")
def test_plex_connection(http_request: Request, body: TestPlexRequest = None):
    """Test connection to Plex server. Uses form values if provided, otherwise saved settings."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    # Non-admins can only test their saved settings, not probe arbitrary URLs
    url = (body.url if body and body.url and is_admin else None) or _get_typed_setting("plex_url", user_id=user_id)
    token = (body.token if body and body.token and is_admin else None) or _get_typed_setting("plex_token", user_id=user_id)

    if not url:
        return {"success": False, "message": "Plex URL not configured"}
    if not token:
        return {"success": False, "message": "Plex token not configured"}

    try:
        with httpx.Client(timeout=10) as client:
            response = client.get(
                f"{url.rstrip('/')}/library/sections",
                headers={"X-Plex-Token": token, "Accept": "application/json"}
            )
            if response.status_code == 200:
                sections = response.json().get("MediaContainer", {}).get("Directory", [])
                music_sections = [s.get("title", "Music") for s in sections if s.get("type") == "artist"]
                if music_sections:
                    plural = "y" if len(music_sections) == 1 else "ies"
                    return {
                        "success": True,
                        "message": f"Connected to Plex  -  found {len(music_sections)} music librar{plural}: {', '.join(music_sections)}"
                    }
                return {
                    "success": True,
                    "message": "Connected to Plex, but no music library was found. Rescans will have nothing to refresh until one exists.",
                    "warning": True,
                }
            elif response.status_code == 401:
                return {"success": False, "message": "Invalid Plex token"}
            else:
                return {"success": False, "message": f"Connection failed: {response.status_code}"}
    except httpx.TimeoutException:
        return {"success": False, "message": "Connection timed out"}
    except Exception as e:
        print(f"Plex connection test error: {type(e).__name__}: {e}")
        return {"success": False, "message": "Connection failed  -  check server logs for details"}


@app.post("/api/settings/test/lidarr")
def test_lidarr_connection(http_request: Request, body: TestLidarrRequest = None):
    """Test connection to Lidarr. Uses form values if provided, otherwise saved settings."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    # Non-admins can only test their saved settings, not probe arbitrary URLs
    url = (body.url if body and body.url and is_admin else None) or _get_typed_setting("lidarr_url", user_id=user_id)
    api_key = (body.api_key if body and body.api_key and is_admin else None) or _get_typed_setting("lidarr_api_key", user_id=user_id)

    if not url:
        return {"success": False, "message": "Lidarr URL not configured"}
    if not api_key:
        return {"success": False, "message": "Lidarr API key not configured"}

    try:
        with httpx.Client(timeout=10) as client:
            response = client.get(
                f"{url.rstrip('/')}/api/v1/system/status",
                headers={"X-Api-Key": api_key}
            )
            if response.status_code == 200:
                data = response.json()
                version = data.get("version", "unknown")
                artist_resp = client.get(
                    f"{url.rstrip('/')}/api/v1/artist",
                    headers={"X-Api-Key": api_key}
                )
                artist_count = len(artist_resp.json()) if artist_resp.status_code == 200 else 0
                return {"success": True, "message": f"Connected to Lidarr v{version} ({artist_count} artists)"}
            elif response.status_code == 401:
                return {"success": False, "message": "Invalid API key"}
            else:
                return {"success": False, "message": f"Connection failed: {response.status_code}"}
    except httpx.TimeoutException:
        return {"success": False, "message": "Connection timed out"}
    except Exception as e:
        print(f"Lidarr connection test error: {type(e).__name__}: {e}")
        return {"success": False, "message": "Connection failed  -  check server logs for details"}


@app.post("/api/settings/test/monochrome")
def test_monochrome_downloads(http_request: Request):
    """Run the full Monochrome download diagnostic and report it step by step.

    Deliberately end-to-end: it searches, resolves through every leg, downloads
    a real track to a temp file, probes the bytes, then bins the lot. Slow, but
    the only way to answer "does Monochrome actually work here?" honestly.

    Tests the SAVED settings, not whatever is currently typed into the form, so
    save before testing. The hifi-api URL and fallback toggles are read deep
    inside the resolution ladder; smuggling unsaved values down there would mean
    threading overrides through a dozen call sites for very little gain.
    """
    if not http_request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    from monochrome import run_download_diagnostic
    return run_download_diagnostic()


@app.post("/api/settings/test/youtube-cookies")
def test_youtube_cookies(http_request: Request, body: TestYouTubeCookiesRequest = None):
    """Test YouTube cookies by fetching info for a known public video.
    Uses form value if provided, otherwise the saved cookies."""
    user_id = http_request.state.user_id
    cookies_text = (body.cookies if body and body.cookies else None)
    if cookies_text is None:
        cookies_text = get_setting("youtube_cookies", "", user_id=user_id)

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

        # Use the YouTube Music Charts page as the test target. It's public, globally
        # available, and yt-dlp extracts it as a flat playlist  -  so we get real output
        # to confirm yt-dlp + cookies are working together without needing age-restricted
        # content or account-specific URLs. If cookies are broken they'll cause a 403 here
        # too; if they're fine, we get tracks back and can report success confidently.
        # A plain public video would also work but gives no way to distinguish "cookies
        # loaded fine" from "cookies were silently ignored".
        TEST_URL = "https://music.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"

        test_cmd = [
            "yt-dlp",
            "--cookies", tmp_path,
            "--flat-playlist",
            "--dump-json",
            "--no-warnings",
            "--playlist-items", "1",
            TEST_URL,
        ]

        result = subprocess.run(test_cmd, capture_output=True, text=True, timeout=TIMEOUT_YTDLP_INFO)
        stderr = result.stderr.lower()

        if result.returncode == 0 and result.stdout.strip():
            return {"success": True, "message": "Cookies loaded and working"}

        if _is_ytdlp_403(result.stderr):
            return {"success": False, "message": "Cookies rejected by YouTube (403). They may be expired  -  try re-exporting."}
        if "sign in" in stderr or "login" in stderr or "not logged in" in stderr:
            return {"success": False, "message": "Cookies rejected  -  YouTube says you're not logged in. Re-export cookies while logged in."}

        # Test playlist unavailable or yt-dlp returned nothing  -  not necessarily a cookie problem
        print(f"Cookie test: test URL failed or returned no output. stderr={result.stderr[:300]!r}")
        return {"success": True, "message": "Cookies loaded (test inconclusive  -  real-world auth untested)"}

    except subprocess.TimeoutExpired:
        return {"success": False, "message": "Test timed out"}
    except Exception as e:
        print(f"YouTube cookie test error: {type(e).__name__}: {e}")
        return {"success": False, "message": "Cookie test failed  -  check server logs for details"}
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


@app.post("/api/settings/test/spotify-cookies")
def test_spotify_cookies(http_request: Request, body: TestSpotifyCookiesRequest = None):
    """Test Spotify cookies by hitting a known private-friendly endpoint.
    Uses the form value if provided, otherwise the saved cookies.
    """
    user_id = http_request.state.user_id
    cookies_text = (body.cookies if body and body.cookies else None)
    if cookies_text is None:
        cookies_text = get_setting("spotify_cookies", "", user_id=user_id)

    if not cookies_text.strip():
        return {"success": False, "message": "No cookies provided"}

    if not _has_valid_cookie_entries(cookies_text):
        return {"success": False, "message": "No cookie entries found (only comments or blank lines)"}

    lines = _cookie_lines_for_domain_check(cookies_text)
    sp_dc = next(
        (l.split("\t")[6].strip() for l in lines
         if not l.startswith("#") and len(l.split("\t")) >= 7 and l.split("\t")[5] == "sp_dc"),
        None
    )
    if not sp_dc:
        return {"success": False, "message": "No sp_dc cookie found. Export cookies from open.spotify.com while logged in."}

    # Test by fetching a small known public playlist embed with the cookie.
    # The embed endpoint is the same one used for real fetches, so if it works
    # here it'll work everywhere. Spotify's Varnish CDN blocks the internal
    # token exchange endpoint from non-browser origins, so the embed is the
    # reliable way to confirm the session is alive from a server context.
    # "Top 50 - Global" is stable, public, and always has tracks in the JSON blob.
    TEST_PLAYLIST_ID = "37i9dQZEVXbMDoHDwVN2tF"
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(
                f"https://open.spotify.com/embed/playlist/{TEST_PLAYLIST_ID}",
                cookies={"sp_dc": sp_dc},
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                }
            )
        if resp.status_code in (401, 403):
            return {"success": False, "message": "Cookies rejected by Spotify; they may have expired. Re-export from open.spotify.com."}
        if resp.status_code != 200:
            return {"success": False, "message": f"Spotify returned {resp.status_code}; cookies may be invalid"}

        # If the embed returned track data, the session is working
        import re as _re
        titles = _re.findall(r'"title":"([^"]+)"', resp.text)
        if len(titles) > 1:
            return {"success": True, "message": "Spotify cookies are valid and authenticated"}

        return {"success": False, "message": "Cookies loaded but Spotify returned no track data; they may be expired"}

    except Exception as e:
        print(f"Spotify cookie test error: {type(e).__name__}: {e}")
        return {"success": False, "message": "Cookie test failed; check server logs for details"}


@app.post("/api/settings/test/apprise")
def test_apprise_notification(http_request: Request, body: TestAppriseRequest = None):
    """Send a test notification via Apprise. Uses form URL if provided, otherwise saved setting."""
    user_id = http_request.state.user_id
    url = (body.url if body and body.url else None) or get_setting("apprise_url", "", user_id=user_id)

    if not url:
        return {"success": False, "message": "Apprise URL not configured"}

    try:
        import apprise
        a = apprise.Apprise()
        if not a.add(url):
            return {"success": False, "message": "Invalid Apprise URL  -  could not parse the notification service"}
        result = a.notify(title="MusicGrabber", body="Test notification from MusicGrabber. If you can see this, it works!")
        if result:
            return {"success": True, "message": "Test notification sent successfully"}
        return {"success": False, "message": "Apprise returned failure  -  check the URL and service configuration"}
    except ImportError:
        return {"success": False, "message": "Apprise library not installed  -  rebuild the Docker image"}
    except Exception as e:
        print(f"Apprise test error: {type(e).__name__}: {e}")
        return {"success": False, "message": "Notification failed  -  check server logs for details"}


@app.post("/api/settings/test/email")
def test_email_notification(http_request: Request, body: TestEmailRequest = None):
    """Send a test email using current form values and saved secret fallbacks."""
    user_id = http_request.state.user_id

    def supplied_or_saved(key: str):
        supplied = getattr(body, key, None) if body else None
        return supplied if supplied is not None else _get_typed_setting(key, user_id=user_id)

    success, message = send_test_email(
        smtp_host=supplied_or_saved("smtp_host"),
        smtp_port=supplied_or_saved("smtp_port"),
        smtp_user=supplied_or_saved("smtp_user"),
        smtp_pass=supplied_or_saved("smtp_pass"),
        smtp_from=supplied_or_saved("smtp_from"),
        smtp_to=supplied_or_saved("smtp_to"),
        smtp_tls=supplied_or_saved("smtp_tls"),
    )
    return {"success": success, "message": message}


@app.get("/api/settings/youtube-cookies/status")
def youtube_cookies_status(http_request: Request):
    """Return non-sensitive status for the cookies file."""
    user_id = http_request.state.user_id
    cookies_text = get_setting("youtube_cookies", "", user_id=user_id)
    has_setting = bool(cookies_text.strip())
    file_exists = COOKIES_FILE.exists()
    file_size = COOKIES_FILE.stat().st_size if file_exists else 0
    file_mtime = COOKIES_FILE.stat().st_mtime if file_exists else None
    expiry = get_cookies_expiry(cookies_text) if has_setting else None
    return {
        "has_setting": has_setting,
        "file_exists": file_exists,
        "file_size": file_size,
        "file_mtime": file_mtime,
        "file_has_valid_entries": _has_valid_cookie_entries(cookies_text) if has_setting else False,
        "auth_cookie_expiry": expiry,  # Unix timestamp of soonest auth cookie expiry, or null
    }


# =============================================================================
# Statistics API
# =============================================================================

def _extract_search_artist(query: str) -> str | None:
    """Best-effort artist extraction from common search query formats."""
    q = (query or "").strip()
    if not q:
        return None

    # "Artist - Song", "Artist – Song", "Artist  -  Song"
    split_match = re.split(r"\s*[-–—]\s*", q, maxsplit=1)
    if len(split_match) == 2 and split_match[0].strip():
        return split_match[0].strip()[:120]

    # Fallback: first 3 words is usually artist-ish, avoids giant free text blobs
    words = q.split()
    if not words:
        return None
    return " ".join(words[:3])[:120]


def _log_search(query: str, result_count: int, source: str = "youtube", user_id: str | None = None) -> str:
    """Log search requests for dashboard analytics and return tracking token."""
    artist = _extract_search_artist(query)
    search_token = uuid.uuid4().hex
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO search_logs (query, artist, result_count, source, search_token, user_id) VALUES (?, ?, ?, ?, ?, ?)",
            (query.strip(), artist, int(result_count), source, search_token, user_id)
        )
        conn.commit()
    return search_token


def _validated_search_token(search_token: str | None, user_id: str | None = None) -> str | None:
    """Only accept server-issued search tokens that exist in search_logs, scoped to the user."""
    token = (search_token or "").strip().lower()
    if not token or not re.fullmatch(r"[0-9a-f]{32}", token):
        return None

    with db_conn() as conn:
        if user_id is not None:
            row = conn.execute(
                "SELECT 1 FROM search_logs WHERE search_token = ? AND user_id = ? LIMIT 1",
                (token, user_id)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM search_logs WHERE search_token = ? LIMIT 1",
                (token,)
            ).fetchone()
    return token if row else None


def _search_duplicate_notice(query: str, user_id: str | None = None) -> dict | None:
    """Return a best-effort library duplicate notice for Artist - Title searches."""
    artist, title = _parse_query_artist_title(query)
    if not artist or not title:
        return None

    matches = []
    nav_path = check_navidrome_duplicate(artist, title, user_id=user_id)
    if nav_path:
        matches.append({"source": "Navidrome", "path": str(nav_path)})

    lidarr_path = check_lidarr_duplicate(artist, title, user_id=user_id)
    if lidarr_path:
        matches.append({"source": "Lidarr", "path": str(lidarr_path)})

    if not matches:
        return None

    sources = [m["source"] for m in matches]
    source_text = sources[0] if len(sources) == 1 else ", ".join(sources[:-1]) + f" and {sources[-1]}"

    return {
        "artist": artist,
        "title": title,
        "sources": matches,
        "message": (
            f"{artist} - {title} already appears to be in {source_text}. "
            "You can still download it again if you want another copy."
        ),
    }


@app.get("/api/stats")
@prioritise_over_audio_audit
def get_stats(http_request: Request):
    """Return download statistics for the dashboard."""
    if not http_request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
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

        # Top artists (by completed downloads)  -  case-insensitive grouping,
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

        # Case-insensitive grouping  -  display the most popular casing variant
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

    # Release the DB connection before any filesystem work is scheduled.
    directories = tuple(d for d in (get_singles_dir(), get_playlists_dir()) if d is not None)
    storage, scan_state = snapshots.get(
        ("storage", tuple(map(str, directories))), lambda: scan_storage(directories),
    )

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
        "storage_bytes": storage["storage_bytes"] if storage is not None else None,
        "file_count": storage["file_count"] if storage is not None else None,
        "storage_scan_state": scan_state,
    }


@app.delete("/api/stats")
def reset_stats(http_request: Request, confirm: bool = False):
    """Reset dashboard stats without touching active queue items."""
    if not http_request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    if not confirm:
        raise HTTPException(status_code=400, detail="Confirmation required (use ?confirm=true)")

    # Keep active work intact, but clear historical job/search data used by stats
    cleanup_stale_jobs()

    with db_conn() as conn:
        deleted_jobs = conn.execute(
            "DELETE FROM jobs WHERE status IN ('completed', 'completed_with_errors', 'failed')"
        ).rowcount
        deleted_searches = conn.execute("DELETE FROM search_logs").rowcount
        conn.execute("DELETE FROM search_decisions")
        conn.commit()

    return {"deleted_jobs": deleted_jobs, "deleted_searches": deleted_searches}


@app.get("/api/mismatches")
def get_mismatches(http_request: Request, limit: int = 200):
    """Return recent watched-track match mismatches for investigation."""
    if not http_request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT m.id, m.job_id, m.playlist_id, m.expected_artist, m.expected_title,
                      m.actual_artist, m.actual_title, m.exp_normalised, m.got_normalised,
                      m.created_at, wp.name AS playlist_name
               FROM watched_match_mismatches m
               LEFT JOIN watched_playlists wp ON wp.id = m.playlist_id
               ORDER BY m.created_at DESC
               LIMIT ?""",
            (min(limit, 500),),
        ).fetchall()
    return {"mismatches": [dict(r) for r in rows]}


@app.delete("/api/mismatches")
def clear_mismatches(http_request: Request):
    """Clear the mismatch log."""
    if not http_request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    with db_conn() as conn:
        deleted = conn.execute("DELETE FROM watched_match_mismatches").rowcount
        conn.commit()
    return {"deleted": deleted}


@app.post("/api/mismatches/{mismatch_id}/accept")
def accept_mismatch(mismatch_id: int, http_request: Request):
    """Force-accept a mismatched track: re-download it but skip the name comparison."""
    if not http_request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        mismatch = conn.execute(
            "SELECT * FROM watched_match_mismatches WHERE id = ?", (mismatch_id,)
        ).fetchone()
        if not mismatch:
            raise HTTPException(status_code=404, detail="Mismatch not found")

        job_id = mismatch["job_id"]
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        job = dict(job) if job else None

        user_id = (job.get("user_id") if job else None) or http_request.state.user_id

        if job:
            # Original job still exists, reset it with the skip flag
            conn.execute(
                "UPDATE jobs SET status = 'queued', error = NULL, completed_at = NULL, "
                "file_deleted = 0, skip_mismatch_check = 1, progress_at = datetime('now') "
                "WHERE id = ?",
                (job_id,)
            )
        else:
            # Job was cleaned up; create a fresh one from the mismatch record
            job_id = str(uuid.uuid4())[:8]
            artist = mismatch["expected_artist"] or ""
            title = mismatch["expected_title"] or ""
            convert_audio = get_setting_bool("default_convert_audio", False, user_id=user_id)
            conn.execute(
                """INSERT INTO jobs
                   (id, title, artist, status, download_type, source, convert_audio, skip_mismatch_check, user_id)
                   VALUES (?, ?, ?, 'queued', 'single', 'youtube', ?, 1, ?)""",
                (job_id, title, artist, int(convert_audio), user_id)
            )
            # Re-link the watched playlist track to this new job so the mismatch skip works
            if mismatch["playlist_id"]:
                track_hash = hash_track(artist, title)
                conn.execute(
                    "UPDATE watched_playlist_tracks SET job_id = ?, downloaded_at = NULL "
                    "WHERE playlist_id = ? AND track_hash = ?",
                    (job_id, mismatch["playlist_id"], track_hash)
                )
            job = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())

        # Clean up the mismatch record since we're dealing with it
        conn.execute("DELETE FROM watched_match_mismatches WHERE id = ?", (mismatch_id,))
        conn.commit()

    # Re-queue the download
    convert_audio = bool(job.get("convert_audio", 1))

    # Restore watched-playlist routing so the file lands in the right folder, not Singles.
    _pl_name, _use_pl_dir = None, False
    if mismatch.get("playlist_id"):
        with db_conn() as conn:
            conn.row_factory = sqlite3.Row
            pl_row = conn.execute(
                "SELECT name, use_playlists_dir FROM watched_playlists WHERE id = ?",
                (mismatch["playlist_id"],)
            ).fetchone()
            if pl_row and pl_row["use_playlists_dir"]:
                _pl_name = pl_row["name"]
                _use_pl_dir = True

    if job.get("source") == "soulseek" and job.get("slskd_username") and job.get("slskd_filename"):
        spawn_daemon_thread(
            process_slskd_download,
            job_id,
            job["slskd_username"],
            job["slskd_filename"],
            job.get("artist", ""),
            job.get("title", ""),
            convert_audio,
            user_id=user_id,
            override_dir=job.get("override_dir"),
            playlist_name=_pl_name,
            use_playlists_dir=_use_pl_dir,
            slskd_size=job.get("slskd_size"),
        )
    else:
        prior_id = job.get("video_id") or ""
        attempted = {prior_id} if prior_id else set()
        new_id = prior_id
        new_source_url = job.get("source_url")
        artist_hint = job.get("artist") or ""
        title_hint = job.get("title") or ""
        if artist_hint or title_hint:
            query = f"{artist_hint} - {title_hint}".strip(" -")
            try:
                for cand in search_all(query, limit=12,
                                       slot_wait=SEARCH_SLOT_WAIT_AUTOMATED)[0]:
                    cand_id = (cand.get("video_id") or "").strip()
                    if cand_id and cand_id not in attempted:
                        new_id = cand_id
                        new_source_url = cand.get("source_url")
                        break
            except Exception as e:
                print(f"Force-accept alternate search failed for job {job_id}: {e}")
        spawn_daemon_thread(
            process_download,
            job_id,
            new_id,
            convert_audio,
            source_url=new_source_url,
            playlist_name=_pl_name,
            use_playlists_dir=_use_pl_dir,
            user_id=user_id,
            override_dir=job.get("override_dir"),
            skip_dupe_check=bool(job.get("override_dir")),
            attempted_ids=attempted,
        )

    return {"status": "queued", "job_id": job_id}


@app.get("/api/jobs/{job_id}/score-rationale")
def get_score_rationale(job_id: str, http_request: Request):
    """Return the scoring rationale for an automated download decision.

    Shows why the scorer picked one candidate over its rivals, so users
    can debug bad picks without becoming intimate with Docker logs.
    """
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin

    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        # Verify the job belongs to this user (or they're admin)
        job = conn.execute("SELECT user_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if not is_admin and job["user_id"] and job["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="Not your job")

        row = conn.execute(
            "SELECT query, decision_json, created_at FROM search_decisions WHERE job_id = ? LIMIT 1",
            (job_id,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="No scoring data for this job")

    try:
        decision = json.loads(row["decision_json"])
    except (ValueError, TypeError):
        raise HTTPException(status_code=500, detail="Corrupt scoring data")

    return {
        "query": row["query"],
        "decision": decision,
        "created_at": row["created_at"],
    }


# =============================================================================
# Search API
# =============================================================================

@app.get("/api/preview/{video_id}")
def get_preview_url(http_request: Request, video_id: str, source: str = "youtube",
                    url: str = None, artist: str = "", title: str = ""):
    """Get a streamable audio URL for preview playback."""
    try:
        # Direct MP3 sources: the source_url is already streamable  -  hand it
        # straight to the browser, no yt-dlp round-trip needed.
        if source in DIRECT_PREVIEW_SOURCES:
            if not url:
                raise HTTPException(status_code=400, detail=f"{source.capitalize()} preview requires url parameter")
            return {"url": url, "video_id": video_id}

        if source in MONOCHROME_PREVIEW_SOURCES:
            if not url:
                raise HTTPException(status_code=400, detail="Monochrome preview requires url parameter")
            from monochrome import get_monochrome_preview_url
            from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs
            _parsed = _urlparse(url)
            if _parsed.scheme != "monochrome" or not _parsed.netloc:
                raise HTTPException(status_code=400, detail="Invalid Monochrome source URL")
            if not (_parse_qs(_parsed.query).get("isrc") or [""])[0]:
                raise HTTPException(status_code=400, detail="Monochrome source URL missing ISRC")
            cdn_url = get_monochrome_preview_url(
                url,
                artist_hint=artist,
                title_hint=title,
            )
            # Anything resolved through the browser leg arrives as CENC-encrypted
            # FLAC-in-MP4, which an <audio> element will stare at blankly. Decrypt
            # the opening seconds and hand back one of ours instead. The qbdlx and
            # Tidal legs return ordinary playable audio, so they pass straight through.
            from monochrome_browser import pop_decryption_key
            decryption_key = pop_decryption_key(cdn_url)
            if decryption_key:
                from preview_snippets import build_snippet
                token = build_snippet(
                    cdn_url, decryption_key, getattr(http_request.state, "user", None)
                )
                return {"url": f"/api/preview-audio/{token}", "video_id": video_id}
            return {"url": cdn_url, "video_id": video_id}

        if source == "mp3phoenix":
            if not url:
                raise HTTPException(status_code=400, detail="MP3Phoenix preview requires url parameter")
            try:
                validate_source_url(source, url)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            # Cloudflare will not serve an <audio> element directly, so the opening
            # seconds come down through the cleared session and go back out from
            # here. MP3 does not mind being cut off mid-flow, so no ffmpeg needed.
            from mp3phoenix import fetch_mp3phoenix_preview, is_mp3phoenix_url
            if not is_mp3phoenix_url(url):
                raise HTTPException(status_code=400, detail="Invalid MP3Phoenix download URL")
            from preview_snippets import store_snippet
            token = store_snippet(
                fetch_mp3phoenix_preview(url), getattr(http_request.state, "user", None)
            )
            return {"url": f"/api/preview-audio/{token}", "video_id": video_id}

        if source == "youtube":
            if not is_valid_youtube_id(video_id):
                raise HTTPException(status_code=400, detail="Invalid YouTube video ID")
            target_url = f"https://www.youtube.com/watch?v={video_id}"
            base_args = _ytdlp_base_args()
        elif source in URL_BASED_SOURCES:
            if not url:
                raise HTTPException(status_code=400, detail=f"{source.capitalize()} preview requires url parameter")
            # Same trust boundary as /api/download: preview fetches from inside
            # the container too, so it gets the same doorman.
            try:
                validate_source_url(source, url)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            target_url = url
            base_args = []  # No cookies needed for URL-based non-YouTube sources
        else:
            raise HTTPException(status_code=400, detail=f"Preview not supported for source: {source}")

        # SoundCloud returns HLS (.m3u8) for bestaudio which browsers can't
        # play natively  -  prefer the direct HTTP MP3 stream for previews.
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


@app.get("/api/preview-audio/{token}")
def get_preview_audio(token: str):
    """Serve a decrypted preview snippet prepared by /api/preview.

    Authenticated by the token itself, resolved back to its owner in the auth
    middleware, because an <audio> element cannot carry a Bearer header.
    """
    from preview_snippets import snippet_path
    path = snippet_path(token)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Preview snippet has expired")
    return FileResponse(path=str(path), media_type="audio/mpeg")


@app.get("/api/sources")
def list_sources():
    """Return available search sources for the frontend source selector."""
    return {"sources": get_available_sources()}


@app.get("/api/sources/health")
def sources_health():
    """Per-source health snapshot; parked sources carry their cooldown reason."""
    return {"sources": servicecheck.health_snapshot()}


@app.post("/api/sources/health/recheck")
def recheck_sources_health(http_request: Request):
    """Ask every source for a fresh verdict without holding the HTTP request open."""
    if not http_request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    spawn_daemon_thread(servicecheck.check_all_sources, True)
    return {"status": "checking"}


@app.post("/api/search")
def search(request: SearchRequest, http_request: Request):
    """Search for music across configured sources."""
    try:
        source = request.source
        album_suggestion = None
        if source == "all":
            raw_results, album_suggestion = search_all(
                request.query, request.limit, include_soulseek=False,
                slot_wait=SEARCH_SLOT_WAIT_INTERACTIVE,
            )
        elif source in SOURCE_REGISTRY:
            raw_results = search_source(source, request.query, request.limit)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown source: {source}")

        # One projection for both the blocking and streaming search endpoints.
        # These were duplicated, which is exactly how an artist fix could have
        # landed in one response path and silently missed the other.
        final_results = [
            SearchResult(**project_search_result(item))
            for item in raw_results[:request.limit]
        ]

        search_token = None
        try:
            search_token = _log_search(request.query, len(final_results), source=source, user_id=http_request.state.user_id)
        except Exception as log_error:
            print(f"search log error: {log_error}")

        # Surface any enabled-but-parked sources so the UI can explain the gaps.
        parked = [
            u for u in servicecheck.unavailable_sources()
            if get_setting_bool(
                f"source_{u['id']}_enabled",
                SOURCE_REGISTRY.get(u["id"], {}).get("default_enabled", True),
            )
        ]
        resp = {
            "results": final_results,
            "slskd_enabled": slskd_enabled(),
            "search_token": search_token,
            "unavailable_sources": parked,
            "duplicate_notice": _search_duplicate_notice(request.query, user_id=http_request.state.user_id),
        }
        if album_suggestion:
            resp["album_suggestion"] = album_suggestion
        return resp

    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Search timed out")
    except HTTPException:
        raise
    except SourceSearchBusy as e:
        # We waited and it is still occupied. Nothing has broken, so a 500 would
        # be a lie and an alarming one; 503 plus a Retry-After says what is
        # actually true, which is "ask me again in a moment".
        print(f"search busy: {e}")
        raise HTTPException(
            status_code=503,
            detail="That source is busy with another search. Try again in a moment.",
            headers={"Retry-After": "3"},
        )
    except Exception as e:
        print(f"search error: {e}")
        raise HTTPException(status_code=500, detail="Search failed")


@app.post("/api/search/stream")
def search_stream(request: SearchRequest, http_request: Request):
    """Live multi-source search: NDJSON stream, one event per line.

    Same inputs as /api/search, but instead of blocking until the slowest source
    answers, it streams progress as it happens: a 'start', a 'source' event per
    source as it lands (or times out / is skipped), an optional 'album_suggestion',
    and a terminal 'done' carrying the search_token and any parked sources. The
    browser renders results progressively and ticks off the source status panel.

    Soulseek rides along here like any other source (gated on its Search Sources
    toggle, as the Settings help has always advised), retiring the old bespoke
    slskd fetch-and-merge dance.
    """
    source = request.source
    if source != "all" and source not in SOURCE_REGISTRY:
        raise HTTPException(status_code=400, detail=f"Unknown source: {source}")

    # "all" gets the availability filter (park dead sources); an explicit single
    # source is honoured even if parked, mirroring the old single-source search.
    sources = None if source == "all" else [source]
    enforce_availability = source == "all"
    user_id = getattr(http_request.state, "user_id", None)

    def _event_lines():
        try:
            for ev in _search_all_events(
                request.query,
                request.limit,
                sources=sources,
                include_soulseek=True,
                enforce_availability=enforce_availability,
                slot_wait=SEARCH_SLOT_WAIT_INTERACTIVE,
            ):
                if ev["type"] == "source" and ev["status"] == "done":
                    ev = {**ev, "results": [project_search_result(r) for r in ev["results"]]}
                elif ev["type"] == "done":
                    search_token = None
                    try:
                        # result_count is informational on the log row; the client
                        # already counted what it rendered, so a rough total is fine.
                        search_token = _log_search(request.query, 0, source=source, user_id=user_id)
                    except Exception as log_error:
                        print(f"search log error: {log_error}")
                    parked = [
                        u for u in servicecheck.unavailable_sources()
                        if get_setting_bool(
                            f"source_{u['id']}_enabled",
                            SOURCE_REGISTRY.get(u["id"], {}).get("default_enabled", True),
                        )
                    ]
                    ev = {
                        **ev,
                        "search_token": search_token,
                        "unavailable_sources": parked,
                        "slskd_enabled": slskd_enabled(),
                        "duplicate_notice": _search_duplicate_notice(request.query, user_id=user_id),
                    }
                yield json.dumps(ev) + "\n"
        except Exception as e:
            print(f"search stream error: {e}")
            yield json.dumps({"type": "error", "detail": "Search failed"}) + "\n"

    return StreamingResponse(_event_lines(), media_type="application/x-ndjson")


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
                relevance_score=r["relevance_score"],
                slskd_username=r["slskd_username"],
                slskd_filename=r["slskd_filename"],
                slskd_size=r.get("slskd_size") or r.get("size"),
                album=r.get("album") or None,
                year=r.get("year"),
                size_bytes=r.get("size") or r.get("slskd_size"),
                bitrate=r.get("bitrate"),
                match_confidence=r.get("match_confidence"),
                queue_length=r.get("queue_length"),
            ))

        return {"results": final_results, "slskd_enabled": True}

    except Exception as e:
        print(f"slskd search error: {type(e).__name__}: {e}")
        return {"results": [], "slskd_enabled": True, "error": "Search failed  -  check server logs for details"}


@app.get("/api/search/artwork")
def search_artwork(artist: str, title: str = "", album: str = ""):
    """Return a cover art URL for display in search results.

    Pass ``album`` for a per-album cover; Soulseek results carry one parsed out
    of the shared file's path, so a page of results costs one lookup per album
    rather than one per row. Falls back to the track lookup when there is no
    album. Tries iTunes then Deezer, and the result is cached, so the second
    result from the same album is free.

    Returns the remote URL so the browser loads it directly; no image bytes are
    proxied through MusicGrabber.
    """
    artist = (artist or "").strip()
    title = (title or "").strip()
    album = (album or "").strip()
    if not artist or not (album or title):
        return {"url": None}
    url = fetch_cover_art_url(artist, album or title, album=bool(album))
    return {"url": url}


@app.post("/api/explore/similar")
def explore_similar(request: ExploreRequest):
    """Artists resembling the one asked for, for the Explore bar.

    The sourcing, ranking and identity resolution all live in `discovery`; this
    is deliberately just the door. It used to be sixty lines of MusicBrainz and
    ListenBrainz plumbing inline, which answered three of eight real seeds.
    """
    artist = request.artist.strip()
    if not artist:
        raise HTTPException(status_code=400, detail="Artist name required")

    names = discovery.similar_artists(artist, limit=max(1, min(request.limit, 25)))
    return {"artists": [{"artist": name} for name in names], "artist": artist}


# =============================================================================
# Download API
# =============================================================================

@app.post("/api/download")
def download(body: DownloadRequest, http_request: Request):
    """Queue a download job"""
    _enforce_peon_format(http_request, body)
    job_id = str(uuid.uuid4())[:8]
    user_id = http_request.state.user_id

    # Extract artist/title if not provided
    artist = body.artist
    title = body.title

    # Create job record
    with db_conn() as conn:
        # Determine source type
        source = body.source or "youtube"
        if source == "soulseek" and not (body.slskd_username and body.slskd_filename):
            source = "youtube"  # Fallback if slskd fields missing

        # Validate based on source
        if source == "youtube":
            if not body.video_id or not is_valid_youtube_id(body.video_id):
                raise HTTPException(status_code=400, detail="Invalid YouTube video ID")
        elif source in URL_BASED_SOURCES:
            if not body.source_url:
                raise HTTPException(status_code=400, detail=f"{source.capitalize()} download requires source_url")
            # The caller picks both the source and the URL, so the two agreeing is
            # something we check, not something we assume.
            try:
                validate_source_url(source, body.source_url)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

        # Build source URL for tracking
        if source == "soulseek":
            source_url = f"soulseek://{body.slskd_username}/{body.slskd_filename}" if body.slskd_username else None
        elif source in URL_BASED_SOURCES:
            source_url = body.source_url
        elif body.download_type == "playlist":
            source_url = f"https://www.youtube.com/playlist?list={body.video_id}" if body.video_id else None
        else:
            source_url = f"https://www.youtube.com/watch?v={body.video_id}" if body.video_id else None

        album_release_mbid = (body.album_release_mbid or "").strip() or None
        album_artist = (body.album_artist or "").strip() or None
        album_name = (body.album_name or "").strip() or None
        album_track_title = (body.album_track_title or "").strip() or None
        album_track_number = int(body.album_track_number) if body.album_track_number else None
        album_track_total = int(body.album_track_total) if body.album_track_total else None
        if album_track_number is not None and album_track_number <= 0:
            album_track_number = None
        if album_track_total is not None and album_track_total <= 0:
            album_track_total = None
        override_dir = None

        album_fields_present = any([album_release_mbid, album_artist, album_name, album_track_title, album_track_number, album_track_total])
        if album_fields_present:
            if body.download_type == "playlist":
                raise HTTPException(status_code=400, detail="Album routing cannot be used with playlist downloads")
            # Full album routing requires MBID + name + track title.
            # Exception: no_mbid folder-only routing just needs album_name + album_artist.
            if album_release_mbid and (not album_name or not album_track_title):
                raise HTTPException(status_code=400, detail="album_name and album_track_title are required when album_release_mbid is provided")
            if not album_release_mbid and not album_name:
                raise HTTPException(status_code=400, detail="album_name is required for album folder routing")
            route_artist = album_artist or (artist or "").strip()
            if not route_artist:
                raise HTTPException(status_code=400, detail="album_artist (or artist) is required for album routing")
            override_dir = str(get_albums_dir(user_id=user_id) / sanitize_filename(route_artist) / sanitize_filename(album_name))

        valid_search_token = _validated_search_token(body.search_token, user_id=user_id)
        # Only a server-issued search token turns this client-supplied value into
        # manual-result evidence. API callers without a preceding search cannot
        # accidentally make an automated download use the manual bypass/gate.
        selected_duration_secs = (
            float(body.selected_duration_secs)
            if valid_search_token and body.selected_duration_secs
            else None
        )

        if body.download_type == "playlist":
            conn.execute(
                """INSERT INTO jobs
                   (id, video_id, title, status, download_type, playlist_name, source, convert_audio, source_url, search_token, selected_duration_secs, user_id,
                    override_dir, album_release_mbid, album_name, album_track_title, album_track_number, album_track_total)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id, body.video_id, title, "queued", "playlist", title, "youtube", int(body.convert_audio), source_url, valid_search_token, None, user_id,
                    None, None, None, None, None, None,
                )
            )
        else:
            conn.execute(
                """INSERT INTO jobs
                   (id, video_id, title, artist, status, download_type, source, slskd_username, slskd_filename, slskd_size, convert_audio, source_url, search_token, selected_duration_secs, user_id,
                    override_dir, album_release_mbid, album_name, album_track_title, album_track_number, album_track_total)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id, body.video_id, title, artist or "", "queued", "single", source,
                    body.slskd_username, body.slskd_filename, body.slskd_size, int(body.convert_audio), source_url, valid_search_token, selected_duration_secs, user_id,
                    override_dir, album_release_mbid, album_name, album_track_title, album_track_number, album_track_total,
                )
            )
            if body.playlist_name:
                # The manual Search route routes the file correctly via
                # playlist_name/use_playlists_dir, but until now never linked
                # the job to its watched_playlist_tracks row, leaving that to
                # _mark_watched_track_downloaded's after-the-fact artist/title
                # healing once the download finished. Link it here instead, so
                # Retry, Replace and the M3U rebuild can all find this job by
                # job_id from the moment it's queued rather than waiting for
                # the download to finish and heal itself. A playlist_name with
                # no matching watched row (a one-off manual playlist) simply
                # matches nothing here, which is fine.
                track_hash = hash_track(artist or "", title)
                conn.execute(
                    """UPDATE watched_playlist_tracks
                       SET job_id = ?, downloaded_at = NULL
                       WHERE track_hash = ?
                         AND playlist_id IN (
                             SELECT id FROM watched_playlists WHERE user_id IS ? AND name = ?
                         )""",
                    (job_id, track_hash, user_id, body.playlist_name),
                )
        conn.commit()

    acquisition_target_id = None
    acquisition_cycle = None
    selected_candidate = None
    if body.download_type != "playlist":
        acquisition_target_id = ensure_acquisition_target(
            owner_type="manual",
            owner_key=job_id,
            mode="manual",
            artist=artist or "",
            title=title,
            user_id=user_id,
            allowed_sources={source},
            priority_source=source,
            convert_audio=body.convert_audio,
            destination={
                "playlist_name": body.playlist_name,
                "use_playlists_dir": bool(body.use_playlists_dir),
                "override_dir": override_dir,
                "album_release_mbid": album_release_mbid,
            },
        )
        acquisition_cycle = begin_acquisition_cycle(acquisition_target_id)
        attach_job_to_acquisition(job_id, acquisition_target_id, acquisition_cycle)
        selected_candidate = {
            "video_id": body.video_id,
            "title": title,
            "artist": artist or "",
            "channel": artist or "",
            "source": source,
            "source_url": source_url,
            "slskd_username": body.slskd_username,
            "slskd_filename": body.slskd_filename,
            "slskd_size": body.slskd_size,
        }

    # If this is an album-routed single track, register a lock so any retry
    # can skip dupe check without relying on the transient skip_dupe_check flag.
    if override_dir and album_track_title:
        _route_artist = album_artist or (artist or "").strip()
        upsert_album_track_lock(album_release_mbid, album_name or "", _route_artist, album_track_title, job_id)

    # Queue the download based on source
    if body.download_type == "playlist":
        spawn_daemon_thread(process_playlist_download, job_id, body.video_id, title, body.convert_audio, True,
                            user_id=user_id)
    else:
        spawn_daemon_thread(
            process_acquisition_cycle,
            job_id,
            selected_candidate,
            artist or "",
            title,
            body.convert_audio,
            target_id=acquisition_target_id,
            cycle=acquisition_cycle,
            automatic=False,
            allowed_sources={source},
            priority_source=source,
            playlist_name=body.playlist_name,
            use_playlists_dir=body.use_playlists_dir,
            user_id=user_id,
            override_dir=override_dir,
            skip_dupe_check=bool(override_dir),
        )

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


def _expose_conversion_intent(row: dict) -> dict:
    """Normalise the conversion flag and keep the pre-v4 field name alongside it.

    The column is honest now, so this exists only for clients written against the
    old name. Works for jobs, watched playlists and watched artists alike; they all
    carry the same flag under the same column.
    """
    convert_audio = bool(row.get("convert_audio", 0))
    row["convert_audio"] = convert_audio
    row["convert_to_flac"] = convert_audio
    return row


def _attach_acquisition_summaries(conn, jobs: list[dict]) -> None:
    """Attach durable retry history without turning the Queue query into N+1 SQL.

    A retry never resurrects a failed Queue row, it opens a new one for the next
    cycle and leaves the failed row standing as a record of what happened (see
    `_create_rescue_job`). Left unscoped, this function used to hand every job
    sharing a target the *target's* whole saga, so an old failed card could show
    a later cycle's "completed" line under its own permanently-red status, an
    outright contradiction. Each job now only sees its own cycle and earlier;
    if a later cycle for the same target went on to succeed, it gets a pointer
    to that job instead of a share of its history.
    """
    target_ids = sorted({
        job.get("acquisition_target_id") for job in jobs
        if job.get("acquisition_target_id")
    })
    if not target_ids:
        return
    placeholders = ",".join("?" * len(target_ids))
    target_rows = conn.execute(
        f"""SELECT id, mode, cycle_count, attempt_count, status,
                   last_attempt_at, last_error
            FROM acquisition_targets WHERE id IN ({placeholders})""",
        target_ids,
    ).fetchall()
    targets = {row["id"]: dict(row) for row in target_rows}
    attempt_rows = conn.execute(
        f"""SELECT target_id, cycle_number, sequence, source, candidate_id,
                   candidate_title, candidate_artist, candidate_quality,
                   status, error, started_at, completed_at
            FROM (
                SELECT target_id, cycle_number, sequence, source, candidate_id,
                       candidate_title, candidate_artist, candidate_quality,
                       status, error, started_at, completed_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY target_id
                           ORDER BY cycle_number DESC, sequence DESC
                       ) AS recent_rank
                FROM acquisition_attempts
                WHERE target_id IN ({placeholders})
            )
            WHERE recent_rank <= 20
            ORDER BY target_id, cycle_number, sequence""",
        target_ids,
    ).fetchall()
    histories: dict[str, list[dict]] = {}
    for row in attempt_rows:
        history = histories.setdefault(row["target_id"], [])
        history.append(dict(row))

    # Later cycle, same target, actually landed: work out which job gets to
    # claim the win, so an earlier failed job can point to it by ID.
    completed_target_ids = [tid for tid, t in targets.items() if t["status"] == "completed"]
    winning_job_of_target: dict[str, str] = {}
    if completed_target_ids:
        placeholders2 = ",".join("?" * len(completed_target_ids))
        winner_rows = conn.execute(
            f"""SELECT acquisition_target_id, id FROM jobs
                WHERE acquisition_target_id IN ({placeholders2})
                  AND status IN ('completed', 'completed_with_errors')
                ORDER BY acquisition_cycle DESC""",
            completed_target_ids,
        ).fetchall()
        for row in winner_rows:
            winning_job_of_target.setdefault(row["acquisition_target_id"], row["id"])

    for job in jobs:
        target = targets.get(job.get("acquisition_target_id"))
        if not target:
            continue
        own_cycle = job.get("acquisition_cycle")
        full_history = histories.get(target["id"], [])
        history = (
            [a for a in full_history if int(a.get("cycle_number") or 0) <= int(own_cycle)]
            if own_cycle is not None else full_history
        )
        job["acquisition_mode"] = target["mode"]
        job["acquisition_cycle_count"] = int(own_cycle) if own_cycle is not None else int(target["cycle_count"] or 0)
        job["acquisition_attempt_count"] = len(history)
        job["acquisition_status"] = target["status"]
        scoped_last_attempt = max(
            (a.get("completed_at") or a.get("started_at") or "" for a in history), default=""
        )
        job["acquisition_last_attempt_at"] = _ensure_utc_suffix(scoped_last_attempt or target["last_attempt_at"])
        job["acquisition_last_error"] = target["last_error"]
        job["acquisition_attempts"] = [
            {
                **attempt,
                "started_at": _ensure_utc_suffix(attempt.get("started_at")),
                "completed_at": _ensure_utc_suffix(attempt.get("completed_at")),
            }
            for attempt in history
        ]
        winning_job_id = winning_job_of_target.get(target["id"])
        if (
            winning_job_id and winning_job_id != job["id"]
            and job.get("status") == "failed"
            and own_cycle is not None and int(own_cycle) < int(target["cycle_count"] or 0)
        ):
            job["acquisition_superseded_by"] = winning_job_id


@app.get("/api/jobs")
def get_jobs(limit: int = 20, http_request: Request = None):
    """Get recent jobs"""
    from pathlib import Path as _Path
    user_id = http_request.state.user_id if http_request else None
    is_admin = http_request.state.is_admin if http_request else True
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE {_scope_frag} ORDER BY created_at DESC LIMIT ?",
            (*_scope_params, limit)
        ).fetchall()

        # Pre-fetch resolved_paths for all jobs in one query.
        # check_duplicate only walks Singles; playlist-folder tracks would be
        # falsely marked deleted without this.
        if rows:
            job_ids = [r["id"] for r in rows]
            placeholders = ",".join("?" * len(job_ids))
            rp_rows = conn.execute(
                f"SELECT job_id, resolved_path FROM watched_playlist_tracks WHERE job_id IN ({placeholders}) AND resolved_path IS NOT NULL",
                job_ids,
            ).fetchall()
            resolved_paths: dict = {r["job_id"]: r["resolved_path"] for r in rp_rows}
        else:
            resolved_paths = {}

        jobs = []
        stale_ids = []  # Jobs that claim file exists but it doesn't
        for row in rows:
            job = _expose_conversion_intent(dict(row))
            job['created_at'] = _ensure_utc_suffix(job.get('created_at'))
            job['completed_at'] = _ensure_utc_suffix(job.get('completed_at'))
            job['progress_at'] = _ensure_utc_suffix(job.get('progress_at'))
            try:
                job['source_history'] = json.loads(job.get('source_history') or '[]')
            except (TypeError, ValueError):
                job['source_history'] = []
            if not job['source_history'] and job.get('source'):
                job['source_history'] = [job['source']]

            # Keep the queue API cheap. Full library reconciliation can walk a
            # large/remote music tree and is handled by the background monitor;
            # here we only verify exact paths already stored on watched rows.
            if (job.get('status') in ('completed', 'completed_with_errors')
                    and not job.get('file_deleted')
                    and job.get('artist') and job.get('title')):
                known_path = job.get('final_path') or resolved_paths.get(job['id'])
                if known_path and _Path(known_path).is_absolute() and not _Path(known_path).exists():
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

        _attach_acquisition_summaries(conn, jobs)

    return {"jobs": jobs}


@app.get("/api/jobs/downloadable")
def get_downloadable_jobs(page: int = 1, per_page: int = 50, http_request: Request = None):
    """Return completed jobs with files available to save to device, newest first."""
    offset = (page - 1) * per_page
    user_id = http_request.state.user_id if http_request else None
    is_admin = http_request.state.is_admin if http_request else True
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        total = conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE status = 'completed' AND (file_deleted IS NULL OR file_deleted = 0) AND {_scope_frag}",
            _scope_params
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT id, title, artist, status, created_at, completed_at
               FROM jobs
               WHERE status = 'completed' AND (file_deleted IS NULL OR file_deleted = 0)
                 AND {_scope_frag}
               ORDER BY completed_at DESC
               LIMIT ? OFFSET ?""",
            (*_scope_params, per_page, offset)
        ).fetchall()
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "jobs": [dict(r) for r in rows],
    }


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, http_request: Request):
    """Get a specific job"""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        cursor = conn.execute(
            f"SELECT * FROM jobs WHERE id = ? AND {_scope_frag}",
            (job_id, *_scope_params)
        )
        row = cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    job = _expose_conversion_intent(dict(row))
    job['created_at'] = _ensure_utc_suffix(job.get('created_at'))
    job['completed_at'] = _ensure_utc_suffix(job.get('completed_at'))
    job['progress_at'] = _ensure_utc_suffix(job.get('progress_at'))
    try:
        job['source_history'] = json.loads(job.get('source_history') or '[]')
    except (TypeError, ValueError):
        job['source_history'] = []
    if not job['source_history'] and job.get('source'):
        job['source_history'] = [job['source']]
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        _attach_acquisition_summaries(conn, [job])
    return job


@app.get("/api/jobs/{job_id}/download")
def download_job_file(job_id: str, http_request: Request):
    """Serve the downloaded audio file for a job directly to the browser."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        cursor = conn.execute(
            f"SELECT * FROM jobs WHERE id = ? AND {_scope_frag}",
            (job_id, *_scope_params)
        )
        row = cursor.fetchone()
        # Also fish out the resolved_path from watched_playlist_tracks if it exists.
        # This is more reliable than reconstructing the path, especially for playlist folders.
        rp_cursor = conn.execute(
            "SELECT resolved_path FROM watched_playlist_tracks WHERE job_id = ? AND resolved_path IS NOT NULL LIMIT 1",
            (job_id,)
        )
        rp_row = rp_cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    job = dict(row)

    if job.get("file_deleted"):
        raise HTTPException(status_code=404, detail="File has been deleted")

    if job.get("status") not in ("completed", "completed_with_errors"):
        raise HTTPException(status_code=404, detail="Job not yet complete")

    artist = job.get("artist", "")
    title = job.get("title", "")
    if not title:
        raise HTTPException(status_code=404, detail="Job has no title")

    from utils import check_duplicate, sanitize_filename

    # Prefer the job's authoritative final path, then the older watched-row path.
    file_path = authoritative_audio_file(job.get("final_path"), user_id)
    if file_path is None and rp_row:
        file_path = authoritative_audio_file(rp_row["resolved_path"], user_id)

    # Fall back to walking the Singles layout (covers non-watched single downloads).
    if file_path is None:
        file_path = check_duplicate(artist, title, user_id=user_id)

    if not file_path or not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    # Use "Artist - Title.ext" as the download filename regardless of where it lives.
    download_name = f"{sanitize_filename(artist)} - {sanitize_filename(title)}{file_path.suffix}" if artist else f"{sanitize_filename(title)}{file_path.suffix}"

    return FileResponse(
        path=str(file_path),
        media_type="application/octet-stream",
        filename=download_name,
    )


_AUDIO_MIME_TYPES = {
    ".flac": "audio/flac", ".opus": "audio/ogg", ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".webm": "audio/webm",
}


@app.get("/api/jobs/{job_id}/stream")
def stream_job_file(job_id: str, http_request: Request):
    """Stream the audio file for in-browser playback."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        row = conn.execute(
            f"SELECT * FROM jobs WHERE id = ? AND {_scope_frag}",
            (job_id, *_scope_params)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    job = dict(row)
    artist = job.get("artist", "")
    title = job.get("title", "")
    if not title:
        raise HTTPException(status_code=404, detail="Job has no title")

    from utils import check_duplicate

    file_path = authoritative_audio_file(job.get("final_path"), user_id)
    if file_path is None:
        file_path = check_duplicate(artist, title, user_id=user_id)
    if not file_path or not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    mime = _AUDIO_MIME_TYPES.get(file_path.suffix.lower(), "audio/mpeg")
    return FileResponse(path=str(file_path), media_type=mime)


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, http_request: Request):
    """Retry a failed job"""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        cursor = conn.execute(
            f"SELECT * FROM jobs WHERE id = ? AND {_scope_frag}",
            (job_id, *_scope_params)
        )
        row = cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Job not found")

        job = dict(row)

        # Track Upgrades jobs are a swap-in-place, not a standalone acquisition: their
        # staging directory is long gone by the time this row is visible, so retrying
        # would silently download into a folder nobody will ever look in again.
        if job["download_type"] == "upgrade":
            raise HTTPException(
                status_code=400,
                detail="Track upgrade jobs can't be re-downloaded from here; search again from Watched Upgrades",
            )

        # Allow retrying failed jobs or re-downloading completed jobs
        if job["status"] not in ("failed", "completed", "completed_with_errors"):
            raise HTTPException(status_code=400, detail="Only failed or completed jobs can be retried")

        # Reset job status
        conn.execute(
            "UPDATE jobs SET status = ?, error = NULL, completed_at = NULL, file_deleted = 0, final_path = NULL, "
            "progress_at = datetime('now') WHERE id = ?",
            ("queued", job_id)
        )
        conn.commit()

    # Re-queue the job based on source type
    convert_audio = bool(job.get("convert_audio", 1))
    source_allowlist = get_job_source_allowlist(job_id)

    # Restore watched-playlist routing so retried jobs land in Playlists, not Singles.
    # The routing flags are not stored on the job row itself, so we look them up via
    # watched_playlist_tracks (queue-track-candidate path) or bulk_import_tracks (refresh path).
    _pl_name, _use_pl_dir = None, False
    if job["download_type"] != "playlist":
        with db_conn() as conn:
            conn.row_factory = sqlite3.Row
            pl_row = conn.execute("""
                SELECT wp.name, wp.use_playlists_dir
                FROM watched_playlist_tracks wpt
                JOIN watched_playlists wp ON wp.id = wpt.playlist_id
                WHERE wpt.job_id = ?
            """, (job_id,)).fetchone()
            if not pl_row:
                pl_row = conn.execute("""
                    SELECT wp.name, wp.use_playlists_dir
                    FROM bulk_import_tracks bit
                    JOIN bulk_imports bi ON bi.id = bit.import_id
                    JOIN watched_playlists wp ON wp.id = bi.watch_playlist_id
                    WHERE bit.job_id = ?
                """, (job_id,)).fetchone()
            if pl_row and pl_row["use_playlists_dir"]:
                _pl_name = pl_row["name"]
                _use_pl_dir = True

    if job["download_type"] == "playlist":
        spawn_daemon_thread(process_playlist_download, job_id, job["video_id"], job["playlist_name"], convert_audio, True,
                            user_id=user_id)
    elif job.get("source") == "soulseek" and job.get("slskd_username") and job.get("slskd_filename"):
        spawn_daemon_thread(
            process_slskd_download,
            job_id,
            job["slskd_username"],
            job["slskd_filename"],
            job.get("artist", ""),
            job.get("title", ""),
            convert_audio,
            user_id=user_id,
            override_dir=job.get("override_dir"),
            playlist_name=_pl_name,
            use_playlists_dir=_use_pl_dir,
            slskd_size=job.get("slskd_size"),
        )
    else:
        # For both YouTube and URL-based sources (SoundCloud, zvu4no, etc):
        # search the job's allowed sources and pick the best untried candidate.
        # Re-trying the same source_url or video_id that already failed is pointless.
        prior_id = job.get("video_id") or ""
        attempted = {prior_id} if prior_id else set()
        new_id = prior_id
        new_source_url = None
        artist_hint = job.get("artist") or ""
        title_hint  = job.get("title") or ""
        if artist_hint or title_hint:
            query = f"{artist_hint} - {title_hint}".strip(" -")
            try:
                retry_sources = sorted(source_allowlist) if source_allowlist is not None else None
                for cand in search_all(query, limit=12, sources=retry_sources,
                                       slot_wait=SEARCH_SLOT_WAIT_AUTOMATED)[0]:
                    cand_id = (cand.get("video_id") or "").strip()
                    if cand_id and cand_id not in attempted:
                        new_id = cand_id
                        new_source_url = cand.get("source_url")
                        break
            except Exception as e:
                print(f"Re-download alternate search failed for job {job_id}: {e}")
        spawn_daemon_thread(
            process_download,
            job_id,
            new_id,
            convert_audio,
            source_url=new_source_url,
            playlist_name=_pl_name,
            use_playlists_dir=_use_pl_dir,
            user_id=user_id,
            override_dir=job.get("override_dir"),
            skip_dupe_check=bool(job.get("override_dir")),
            attempted_ids=attempted,
            fallback_allowed_sources=source_allowlist,
        )

    return {"job_id": job_id, "status": "queued"}


def _rescue_context(job_id: str, user_id: str | None, is_admin: bool) -> tuple[dict, dict]:
    """Load an owned failed job and its durable automatic-acquisition target."""
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        _scope_frag = _scope_frag.replace("user_id", "j.user_id")
        row = conn.execute(
            f"""SELECT j.*, at.user_id AS target_user_id, at.owner_type,
                       at.owner_key, at.mode AS target_mode, at.artist AS target_artist,
                       at.title AS target_title, at.isrc AS target_isrc,
                       at.allowed_sources, at.priority_source,
                       at.convert_audio AS target_convert_audio,
                       at.destination_json, at.status AS target_status
                FROM jobs j
                JOIN acquisition_targets at ON at.id = j.acquisition_target_id
                WHERE j.id = ? AND {_scope_frag}""",
            (job_id, *_scope_params),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Automatic acquisition job not found")
    data = dict(row)
    if data.get("target_mode") != "automatic":
        raise HTTPException(
            status_code=400,
            detail="This was a manually pinned source; use Re-download to honour that source",
        )
    if data.get("status") != "failed":
        raise HTTPException(status_code=400, detail="Only failed automatic jobs can be rescued")
    try:
        destination = json.loads(data.get("destination_json") or "{}")
    except (TypeError, ValueError):
        destination = {}
    return data, destination


def _candidate_can_download(candidate: dict) -> bool:
    source = (candidate.get("source") or "youtube").strip().lower()
    if source == "youtube":
        return bool(candidate.get("video_id") and is_valid_youtube_id(candidate["video_id"]))
    if source == "soulseek":
        return bool(candidate.get("slskd_username") and candidate.get("slskd_filename"))
    if source in URL_BASED_SOURCES:
        # Rescue candidates are replayed automatically later, well away from any
        # human, so a bogus URL that slipped into storage must not get a second
        # chance at being fetched.
        return is_valid_source_url(source, candidate.get("source_url"))
    return False


def _rescue_candidates_for_target(target_id: str, allowed_sources: set[str] | None) -> list[dict]:
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        candidates = stored_rescue_candidates(conn, target_id)
        attempts = conn.execute(
            """SELECT source, candidate_id
               FROM acquisition_attempts WHERE target_id = ?""",
            (target_id,),
        ).fetchall()
    attempted = {
        ((row["source"] or "youtube").lower(), str(row["candidate_id"] or ""))
        for row in attempts
    }
    results = []
    for candidate in candidates:
        source = (candidate.get("source") or "youtube").strip().lower()
        if allowed_sources is not None and source not in allowed_sources:
            continue
        candidate["attempted"] = (source, str(candidate.get("video_id") or "")) in attempted
        candidate["selectable"] = _candidate_can_download(candidate)
        results.append(candidate)
    return results


def _rescue_routing(job: dict, destination: dict) -> tuple[str | None, bool, str | None]:
    use_playlists_dir = bool(destination.get("use_playlists_dir"))
    custom_subdir = (destination.get("custom_subdir") or "").strip() or None
    playlist_name = (destination.get("playlist_name") or "").strip() or None
    watch_playlist_id = destination.get("watch_playlist_id")
    if watch_playlist_id:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT name, use_playlists_dir, custom_subdir FROM watched_playlists WHERE id = ?",
                (watch_playlist_id,),
            ).fetchone()
        if row:
            playlist_name = row[0]
            use_playlists_dir = bool(row[1])
            custom_subdir = (row[2] or "").strip() or custom_subdir
    if not (use_playlists_dir or custom_subdir):
        playlist_name = None
    return playlist_name, use_playlists_dir, custom_subdir


def _search_rescue_candidates(job: dict, slot_wait: float) -> list[dict]:
    """Repeat automatic search without dropping an album/playlist's ISRC identity."""
    artist = job.get("target_artist") or job.get("artist") or ""
    title = job.get("target_title") or job.get("title") or ""
    allowed = parse_allowed_sources(job.get("allowed_sources"))
    raw: list[dict] = []
    isrc = (job.get("target_isrc") or "").strip()
    if isrc and (allowed is None or "monochrome" in allowed):
        try:
            from monochrome import resolve_by_isrc
            exact = resolve_by_isrc(isrc, artist, title)
            if exact:
                raw.append(exact)
        except Exception as exc:
            print(f"Rescue ISRC lookup failed for {artist} - {title}: {exc}")
    searched, _ = search_all(
        f"{artist} - {title}".strip(" -"),
        limit=12,
        sources=sorted(allowed) if allowed is not None else None,
        include_soulseek=True,
        slot_wait=slot_wait,
        return_all_source_results=True,
    )
    seen = {
        ((item.get("source") or "youtube").lower(), str(item.get("video_id") or ""))
        for item in raw
    }
    raw.extend(
        item for item in searched
        if ((item.get("source") or "youtube").lower(), str(item.get("video_id") or ""))
        not in seen
    )
    ranked, _rejected = rank_automatic_candidates(
        raw, artist, title, priority_source=job.get("priority_source")
    )
    return ranked


def _create_rescue_job(job: dict, destination: dict, candidate: dict | None = None) -> tuple[str, int]:
    """Append a new Queue row without erasing the failed attempt it rescues."""
    target_id = job["acquisition_target_id"]
    new_job_id = str(uuid.uuid4())[:8]
    candidate = candidate or {}
    source = (candidate.get("source") or job.get("source") or "youtube").strip().lower()
    video_id = candidate.get("video_id") or ""
    source_url = candidate.get("source_url")
    if source == "youtube" and video_id and not source_url:
        source_url = f"https://www.youtube.com/watch?v={video_id}"
    if source == "soulseek" and not source_url and candidate.get("slskd_username"):
        source_url = f"soulseek://{candidate['slskd_username']}/{candidate.get('slskd_filename') or ''}"

    with db_conn() as conn:
        # BEGIN IMMEDIATE takes the write lock before we look, so the "is one
        # already running?" check and the row that answers it cannot be separated.
        # Two clicks on Retry used to both find nothing running and both queue a
        # rescue, doubling up on a target that is only allowed one at a time.
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute(
            """SELECT 1 FROM jobs
               WHERE acquisition_target_id = ? AND status IN ('queued', 'downloading')
               LIMIT 1""",
            (target_id,),
        ).fetchone()
        if active:
            conn.execute("ROLLBACK")
            raise HTTPException(status_code=409, detail="A rescue attempt is already running")

        # Claiming the cycle inside the same transaction means a rescue that
        # loses the race does not quietly burn a cycle on its way out.
        cycle = begin_acquisition_cycle(target_id, conn=conn)
        conn.execute(
            """INSERT INTO jobs
               (id, video_id, title, artist, status, download_type, playlist_name,
                source, slskd_username, slskd_filename, slskd_size, convert_audio,
                source_url, user_id, override_dir, album_release_mbid, album_name,
                album_track_title, album_track_number, album_track_total,
                skip_mismatch_check, acquisition_target_id, acquisition_cycle,
                progress_stage)
               VALUES (?, ?, ?, ?, 'queued', 'single', ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, 'Finding next option')""",
            (
                new_job_id, video_id, job.get("target_title") or job.get("title") or "",
                job.get("target_artist") or job.get("artist") or "",
                job.get("playlist_name"), source,
                candidate.get("slskd_username"), candidate.get("slskd_filename"),
                candidate.get("slskd_size") or candidate.get("size"),
                int(bool(job.get("target_convert_audio"))), source_url,
                job.get("target_user_id") or job.get("user_id"),
                destination.get("override_dir") or job.get("override_dir"),
                destination.get("album_release_mbid") or job.get("album_release_mbid"),
                job.get("album_name"), job.get("album_track_title"),
                job.get("album_track_number"), job.get("album_track_total"),
                int(bool(job.get("skip_mismatch_check"))), target_id, cycle,
            ),
        )
        conn.execute(
            """UPDATE bulk_import_tracks
               SET job_id = ?, status = 'queued', error = NULL, acquisition_cycle = ?
               WHERE acquisition_target_id = ?""",
            (new_job_id, cycle, target_id),
        )
        watch_playlist_id = destination.get("watch_playlist_id")
        if watch_playlist_id:
            conn.execute(
                """UPDATE watched_playlist_tracks
                   SET job_id = ?, downloaded_at = NULL
                   WHERE playlist_id = ? AND track_hash = ?""",
                (
                    new_job_id, watch_playlist_id,
                    hash_track(job.get("target_artist") or "", job.get("target_title") or ""),
                ),
            )
        watch_artist_id = destination.get("watch_artist_id")
        if watch_artist_id:
            conn.execute(
                """UPDATE watched_artist_tracks
                   SET job_id = ?, downloaded_at = NULL
                   WHERE artist_id = ? AND track_hash = ?""",
                (
                    new_job_id, watch_artist_id,
                    hash_track(job.get("target_artist") or "", job.get("target_title") or ""),
                ),
            )
        conn.commit()
    return new_job_id, cycle


def _run_rescue_cycle(
    job: dict,
    destination: dict,
    new_job_id: str,
    cycle: int,
    candidates: list[dict],
    automatic: bool,
) -> None:
    target_id = job["acquisition_target_id"]
    artist = job.get("target_artist") or job.get("artist") or ""
    title = job.get("target_title") or job.get("title") or ""
    allowed_sources = parse_allowed_sources(job.get("allowed_sources"))
    priority_source = job.get("priority_source")

    if automatic:
        untried = [candidate for candidate in candidates if not candidate.get("attempted")]
        candidate_pool = [candidate for candidate in untried if _candidate_can_download(candidate)]
        if not candidate_pool:
            try:
                candidate_pool = _search_rescue_candidates(
                    job, SEARCH_SLOT_WAIT_AUTOMATED
                )
            except Exception as exc:
                candidate_pool = []
                failure = f"Rescue search failed: {exc}"
        else:
            candidate_pool, _rejected = rank_automatic_candidates(
                candidate_pool, artist, title, priority_source=priority_source
            )
        if not candidate_pool:
            failure = locals().get("failure", "No suitable rescue candidates found")
            with db_conn() as conn:
                conn.execute(
                    """UPDATE jobs SET status = 'failed', error = ?, progress_stage = NULL,
                              completed_at = datetime('now') WHERE id = ?""",
                    (failure, new_job_id),
                )
                conn.execute(
                    """UPDATE bulk_import_tracks SET status = 'failed', error = ?
                       WHERE acquisition_target_id = ?""",
                    (failure, target_id),
                )
                conn.commit()
            finish_acquisition_cycle(target_id, "failed", failure)
            return
        save_search_decision(
            new_job_id,
            f"{artist} - {title}".strip(" -"),
            acquisition_candidate_summary(candidate_pool[0]),
            [acquisition_candidate_summary(item) for item in candidate_pool[1:4]],
        )
    else:
        candidate_pool = [candidate for candidate in candidates if _candidate_can_download(candidate)]
        if not candidate_pool:
            failure = "The selected candidate no longer has enough download information"
            with db_conn() as conn:
                conn.execute(
                    "UPDATE jobs SET status = 'failed', error = ?, progress_stage = NULL WHERE id = ?",
                    (failure, new_job_id),
                )
                conn.execute(
                    """UPDATE bulk_import_tracks SET status = 'failed', error = ?
                       WHERE acquisition_target_id = ?""",
                    (failure, target_id),
                )
                conn.commit()
            finish_acquisition_cycle(target_id, "failed", failure)
            return

    playlist_name, use_playlists_dir, custom_subdir = _rescue_routing(job, destination)
    process_acquisition_cycle(
        new_job_id,
        candidate_pool[0],
        artist,
        title,
        bool(job.get("target_convert_audio")),
        target_id=target_id,
        cycle=cycle,
        automatic=automatic,
        candidate_pool=candidate_pool,
        allowed_sources=allowed_sources if automatic else {
            (candidate_pool[0].get("source") or "youtube").lower()
        },
        priority_source=priority_source if automatic else candidate_pool[0].get("source"),
        playlist_name=playlist_name,
        use_playlists_dir=use_playlists_dir,
        user_id=job.get("target_user_id") or job.get("user_id"),
        override_dir=destination.get("override_dir") or job.get("override_dir"),
        skip_dupe_check=bool(destination.get("override_dir") or job.get("override_dir")),
        custom_subdir=custom_subdir,
    )
    with db_conn() as conn:
        state_row = conn.execute(
            "SELECT status, error FROM jobs WHERE id = ?", (new_job_id,)
        ).fetchone()
        if state_row:
            track_status = (
                "completed"
                if state_row[0] in ("completed", "completed_with_errors")
                else "cancelled" if state_row[0] == "cancelled" else "failed"
            )
            conn.execute(
                """UPDATE bulk_import_tracks SET status = ?, error = ?
                   WHERE acquisition_target_id = ?""",
                (
                    track_status,
                    None if track_status == "completed" else state_row[1],
                    target_id,
                ),
            )
            conn.commit()


@app.get("/api/jobs/{job_id}/rescue-candidates")
def get_job_rescue_candidates(job_id: str, http_request: Request):
    job, _destination = _rescue_context(
        job_id, http_request.state.user_id, http_request.state.is_admin
    )
    candidates = _rescue_candidates_for_target(
        job["acquisition_target_id"], parse_allowed_sources(job.get("allowed_sources"))
    )
    return {
        "artist": job.get("target_artist") or job.get("artist") or "",
        "title": job.get("target_title") or job.get("title") or "",
        "candidates": candidates,
    }


@app.post("/api/jobs/{job_id}/rescue-candidates/refresh")
def refresh_job_rescue_candidates(job_id: str, http_request: Request):
    job, _destination = _rescue_context(
        job_id, http_request.state.user_id, http_request.state.is_admin
    )
    artist = job.get("target_artist") or job.get("artist") or ""
    title = job.get("target_title") or job.get("title") or ""
    try:
        ranked = _search_rescue_candidates(job, SEARCH_SLOT_WAIT_INTERACTIVE)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Candidate search failed: {exc}")
    if ranked:
        save_search_decision(
            job_id,
            f"{artist} - {title}".strip(" -"),
            acquisition_candidate_summary(ranked[0]),
            [acquisition_candidate_summary(item) for item in ranked[1:4]],
        )
    return get_job_rescue_candidates(job_id, http_request)


@app.post("/api/jobs/{job_id}/retry-next")
def retry_job_next_option(job_id: str, http_request: Request):
    job, destination = _rescue_context(
        job_id, http_request.state.user_id, http_request.state.is_admin
    )
    candidates = _rescue_candidates_for_target(
        job["acquisition_target_id"], parse_allowed_sources(job.get("allowed_sources"))
    )
    new_job_id, cycle = _create_rescue_job(job, destination)
    spawn_daemon_thread(
        _run_rescue_cycle, job, destination, new_job_id, cycle, candidates, True
    )
    return {"job_id": new_job_id, "status": "queued"}


@app.post("/api/jobs/{job_id}/rescue-candidate")
def queue_job_rescue_candidate(
    job_id: str, body: QueueRescueCandidateRequest, http_request: Request
):
    job, destination = _rescue_context(
        job_id, http_request.state.user_id, http_request.state.is_admin
    )
    allowed = parse_allowed_sources(job.get("allowed_sources"))
    candidates = _rescue_candidates_for_target(job["acquisition_target_id"], allowed)
    candidate = next(
        (item for item in candidates if item.get("candidate_key") == body.candidate_key),
        None,
    )
    if not candidate:
        raise HTTPException(status_code=404, detail="Stored candidate not found")
    if not candidate.get("selectable"):
        raise HTTPException(status_code=400, detail="Candidate download details have expired; search again")
    new_job_id, cycle = _create_rescue_job(job, destination, candidate)
    spawn_daemon_thread(
        _run_rescue_cycle, job, destination, new_job_id, cycle, [candidate], False
    )
    return {"job_id": new_job_id, "status": "queued"}


@app.post("/api/jobs/{job_id}/force-accept")
def force_accept_job(job_id: str, http_request: Request):
    """Set skip_mismatch_check on a job and retry it. Handy shortcut from the queue card.

    Ownership is checked before anything is written. Knowing someone else's
    eight-character job ID should not be enough to wipe their mismatch evidence,
    even if the retry itself would have been refused a moment later.
    """
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    scope_frag, scope_params = _user_scope(user_id, is_admin)
    with db_conn() as conn:
        owned = conn.execute(
            f"SELECT 1 FROM jobs WHERE id = ? AND {scope_frag}", (job_id, *scope_params)
        ).fetchone()
        if not owned:
            raise HTTPException(status_code=404, detail="Job not found")
        conn.execute(
            "UPDATE jobs SET skip_mismatch_check = 1 WHERE id = ?", (job_id,)
        )
        # Tidy up any mismatch log entries for this job while we're at it
        conn.execute(
            "DELETE FROM watched_match_mismatches WHERE job_id = ?", (job_id,)
        )
        conn.commit()
    # Delegate to the normal retry flow (which will now honour the skip flag)
    return retry_job(job_id, http_request)


@app.delete("/api/jobs/{job_id}/file")
def delete_job_file(job_id: str, http_request: Request):
    """Delete the downloaded audio file (and lyrics) for a completed job."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        cursor = conn.execute(
            f"SELECT * FROM jobs WHERE id = ? AND {_scope_frag}",
            (job_id, *_scope_params)
        )
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

    from utils import check_duplicate, move_to_trash
    existing = authoritative_audio_file(job.get("final_path"), user_id)
    if existing is None:
        existing = check_duplicate(artist, title, user_id=user_id)
    if not existing:
        # File already gone, just mark it and move on
        with db_conn() as conn:
            conn.execute("UPDATE jobs SET file_deleted = 1 WHERE id = ?", (job_id,))
            conn.commit()
        return {"deleted": [], "job_id": job_id, "trashed": False}

    # Move the file to the trash bin. Covers both playlist-owned and library files;
    # the whole point of the trash is that nothing gets permanently nuked, so the
    # user can restore without re-downloading.
    trashed_files = []
    trash_result = move_to_trash(existing, user_id=user_id)
    if trash_result:
        trashed_files.append(existing.name)
        lrc_trash = trash_result.with_suffix(".lrc")
        if lrc_trash.exists():
            trashed_files.append(lrc_trash.name)
    else:
        # Trash failed; fall back to permanent delete so we don't leave the user stuck
        try:
            existing.unlink()
            trashed_files.append(existing.name)
            lrc_file = existing.with_suffix(".lrc")
            if lrc_file.exists():
                lrc_file.unlink()
                trashed_files.append(lrc_file.name)
            # Clean up empty parent directory
            track_dir = existing.parent
            if track_dir.exists() and not any(track_dir.iterdir()):
                track_dir.rmdir()
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Failed to delete: {e}")

    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("UPDATE jobs SET file_deleted = 1 WHERE id = ?", (job_id,))

        # Clear downloaded_at so the track reappears as missing and can be retried/replaced.
        conn.execute(
            "UPDATE watched_playlist_tracks SET downloaded_at = NULL WHERE job_id = ?",
            (job_id,)
        )

        # Fetch playlist info for M3U rebuild (if this job belongs to a watched playlist)
        playlist_row = conn.execute(
            """SELECT wp.id, wp.name, wp.make_m3u, wp.use_playlists_dir, wp.sync_mode,
                      wp.custom_subdir, wp.user_id
               FROM watched_playlists wp
               JOIN bulk_imports bi ON bi.watch_playlist_id = wp.id
               JOIN bulk_import_tracks bt ON bt.import_id = bi.id AND bt.job_id = ?
               WHERE wp.make_m3u = 1
               LIMIT 1""",
            (job_id,)
        ).fetchone()

        if not playlist_row:
            # The bulk import trail can go cold; the watched track itself never does.
            playlist_row = conn.execute(
                """SELECT wp.id, wp.name, wp.make_m3u, wp.use_playlists_dir, wp.sync_mode,
                          wp.custom_subdir, wp.user_id
                   FROM watched_playlist_tracks wpt
                   JOIN watched_playlists wp ON wp.id = wpt.playlist_id
                   WHERE wpt.job_id = ? AND wp.make_m3u = 1
                   LIMIT 1""",
                (job_id,)
            ).fetchone()

        conn.commit()

    # Rebuild M3U to remove this track from the playlist file
    if playlist_row:
        # custom_subdir and user_id matter: without them a playlist routed to its
        # own folder gets rebuilt against the default Playlists dir instead, which
        # writes the M3U somewhere nobody is looking. Every other caller passes both.
        rebuild_watched_playlist_m3u(
            playlist_row["id"], playlist_row["name"],
            use_playlists_dir=bool(playlist_row["use_playlists_dir"]),
            sync_mode=playlist_row["sync_mode"] or "append",
            user_id=playlist_row["user_id"],
            custom_subdir=playlist_row["custom_subdir"] or None,
        )

    return {"deleted": trashed_files, "job_id": job_id, "trashed": bool(trashed_files)}


@app.patch("/api/jobs/{job_id}/tags")
def patch_job_tags(job_id: str, body: PatchTagsRequest, http_request: Request):
    """Correct the artist, title, and/or album tags on a completed download."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin

    new_artist = body.artist.strip()
    new_title  = body.title.strip()
    new_album  = body.album.strip()
    new_album_artist = body.album_artist.strip()
    new_year = body.year.strip()
    new_track_number = body.track_number if body.track_number and body.track_number > 0 else None
    new_track_total = body.track_total if body.track_total and body.track_total > 0 else None
    if not new_artist or not new_title:
        raise HTTPException(status_code=422, detail="Artist and title are required")
    if new_track_number and new_track_total and new_track_number > new_track_total:
        raise HTTPException(status_code=422, detail="Track number cannot be greater than total tracks")

    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        row = conn.execute(
            f"SELECT * FROM jobs WHERE id = ? AND {_scope_frag}",
            (job_id, *_scope_params)
        ).fetchone()
        rp_playlist = conn.execute(
            "SELECT resolved_path FROM watched_playlist_tracks WHERE job_id = ? AND resolved_path IS NOT NULL LIMIT 1",
            (job_id,)
        ).fetchone()
        rp_artist = conn.execute(
            "SELECT resolved_path FROM watched_artist_tracks WHERE job_id = ? AND resolved_path IS NOT NULL LIMIT 1",
            (job_id,)
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    job = dict(row)
    if job["status"] not in ("completed", "completed_with_errors"):
        raise HTTPException(status_code=400, detail="Only completed jobs can be edited")
    if job.get("file_deleted"):
        raise HTTPException(status_code=400, detail="File has been deleted")

    old_artist = job.get("artist") or ""
    old_title  = job.get("title") or ""
    if not old_artist or not old_title:
        raise HTTPException(status_code=400, detail="Job has no artist/title metadata")

    # Resolve the file on disk, same priority order as delete_job_file
    from utils import check_duplicate
    file_path = authoritative_audio_file(job.get("final_path"), user_id)
    for rp_row in (rp_playlist, rp_artist):
        if file_path is not None:
            break
        if rp_row:
            file_path = authoritative_audio_file(rp_row["resolved_path"], user_id)
    if file_path is None:
        file_path = check_duplicate(old_artist, old_title, user_id=user_id)
    if not file_path or not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    # Work out whether we need to rename (artist or title changed)
    needs_rename = (new_artist != old_artist) or (new_title != old_title)
    new_file_path = file_path
    if needs_rename:
        new_stem = f"{sanitize_filename(new_artist)} - {sanitize_filename(new_title)}"
        new_file_path = file_path.parent / (new_stem + file_path.suffix)
        if new_file_path != file_path and new_file_path.exists():
            raise HTTPException(status_code=409, detail="A file with that artist and title already exists")

    # Write tags first (in-place, file stays at old path)
    try:
        apply_metadata_to_file(
            file_path,
            artist=new_artist,
            title=new_title,
            album=new_album,
            year=new_year or None,
            track_number=new_track_number,
            track_total=new_track_total,
            album_artist=new_album_artist or None,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write tags: {e}")

    # Rename the file and sidecar if needed
    if needs_rename and new_file_path != file_path:
        try:
            file_path.rename(new_file_path)
            set_file_permissions(new_file_path)
            old_lrc = file_path.with_suffix(".lrc")
            if old_lrc.exists():
                try:
                    old_lrc.rename(new_file_path.with_suffix(".lrc"))
                    set_file_permissions(new_file_path.with_suffix(".lrc"))
                except OSError:
                    pass  # Stranded .lrc is a minor nuisance, not worth failing the whole request
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Tags written but rename failed: {e}")

    # Update the database now that disk is consistent
    resolved_str = str(new_file_path)
    with db_conn() as conn:
        conn.execute(
            "UPDATE jobs SET artist = ?, title = ?, album_name = ?, final_path = ? WHERE id = ?",
            (new_artist, new_title, new_album, resolved_str, job_id)
        )
        if rp_playlist:
            conn.execute(
                "UPDATE watched_playlist_tracks SET resolved_path = ? WHERE job_id = ?",
                (resolved_str, job_id)
            )
        if rp_artist:
            conn.execute(
                "UPDATE watched_artist_tracks SET resolved_path = ? WHERE job_id = ?",
                (resolved_str, job_id)
            )
        conn.commit()

    # The index holds the artist and title this file used to carry, and duplicate
    # detection trusts them: leave the old row standing and a later download of
    # some unrelated track gets refused as a duplicate of a name this file no
    # longer has. So the row is removed and read again rather than relocated,
    # because a retag changed the bytes and not merely the name. Unconditionally,
    # even when nothing was renamed, since an album correction moves no file and
    # the arrival hook skips re-reading a file whose size and timestamp have not
    # budged; without this it would happily serve the old album forever.
    library_index.remove_file(file_path)
    library_index.index_file(new_file_path)

    # Fire-and-forget rescan so library managers pick up the rename/retag
    spawn_daemon_thread(trigger_navidrome_scan, user_id=user_id)
    spawn_daemon_thread(trigger_jellyfin_scan, user_id=user_id)
    spawn_daemon_thread(trigger_plex_scan, user_id=user_id)

    return {
        "success": True,
        "artist": new_artist,
        "title": new_title,
        "album": new_album,
        "album_artist": new_album_artist,
        "year": new_year,
        "track_number": new_track_number,
        "track_total": new_track_total,
    }


@app.get("/api/jobs/{job_id}/musicbrainz-guess")
def get_job_musicbrainz_guess(job_id: str, artist: str, title: str, http_request: Request):
    """Return a best-effort MusicBrainz tag guess for a queue item."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    artist = (artist or "").strip()
    title = (title or "").strip()
    offset_raw = (http_request.query_params.get("offset") or "0").strip()
    if not artist or not title:
        raise HTTPException(status_code=422, detail="Artist and title are required")
    try:
        offset = int(offset_raw)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid guess offset")
    if offset < 0:
        raise HTTPException(status_code=422, detail="Invalid guess offset")

    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        row = conn.execute(
            f"SELECT id, status, file_deleted, final_path FROM jobs WHERE id = ? AND {_scope_frag}",
            (job_id, *_scope_params)
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    if row["status"] not in ("completed", "completed_with_errors"):
        raise HTTPException(status_code=400, detail="Only completed jobs can be edited")
    if row["file_deleted"]:
        raise HTTPException(status_code=400, detail="File has been deleted")

    from utils import check_duplicate

    file_path = authoritative_audio_file(row["final_path"], user_id)
    if file_path is None:
        candidate = check_duplicate(artist, title, user_id=user_id)
        if candidate and candidate.exists():
            file_path = candidate

    guess = guess_musicbrainz_tags(artist, title, offset=offset, file_path=file_path)
    if not guess:
        raise HTTPException(status_code=404, detail="No more suitable MusicBrainz matches found")

    return guess


@app.delete("/api/jobs/cleanup")
def cleanup_jobs(
    http_request: Request,
    status: Optional[str] = None,
    confirm: bool = False,
):
    """Delete completed, failed, or stale jobs.

    Admins clear the lot; standard users only clear their own rows.
    Peons get bounced; the UI hides the button but a curious one with dev
    tools open shouldn't get to nuke anything. The confirmation query parameter
    is deliberately enforced by the API as well as the browser prompt: scripts
    and test suites should not be able to erase Queue history by accident.
    """
    user = getattr(http_request.state, "user", None) or {}
    if user.get("role") == "peon":
        raise HTTPException(status_code=403, detail="Peons cannot clear the queue")
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="Confirmation required (use ?confirm=true)",
        )
    if status not in (None, "both", "completed", "failed", "stale"):
        raise HTTPException(
            status_code=400,
            detail="Invalid status (use completed, failed, stale, or both)",
        )

    # First, mark any stale jobs as failed so they get cleaned up
    cleanup_stale_jobs()

    is_admin = http_request.state.is_admin
    user_id = http_request.state.user_id
    scope_clause = "" if is_admin else " AND user_id = ?"
    scope_params: tuple = () if is_admin else (user_id,)

    with db_conn() as conn:
        if status == "completed":
            cursor = conn.execute(
                f"DELETE FROM jobs WHERE status IN ('completed', 'completed_with_errors'){scope_clause}",
                scope_params,
            )
        elif status == "failed":
            cursor = conn.execute(
                f"DELETE FROM jobs WHERE status = 'failed'{scope_clause}",
                scope_params,
            )
        elif status == "stale":
            cursor = conn.execute(
                f"DELETE FROM jobs WHERE status IN ('downloading', 'queued') "
                f"AND created_at < datetime('now', ? || ' seconds'){scope_clause}",
                (str(-STALE_JOB_TIMEOUT), *scope_params),
            )
        else:  # Omitted or explicitly "both".
            cursor = conn.execute(
                f"DELETE FROM jobs WHERE status IN ('completed', 'completed_with_errors', 'failed'){scope_clause}",
                scope_params,
            )

        deleted_count = cursor.rowcount
        conn.commit()

    return {"deleted": deleted_count}


# =============================================================================
# Trash Bin API
# =============================================================================

@app.get("/api/trash")
def list_trash(http_request: Request):
    """List all files sitting in the trash bin."""
    from settings import get_trash_dir
    user_id = http_request.state.user_id
    trash_dir = get_trash_dir(user_id=user_id)
    if not trash_dir.exists():
        return {"files": [], "total_size": 0}

    files = []
    total_size = 0
    walk_errors = []

    def _on_walk_error(err):
        walk_errors.append(str(err))
        print(f"[trash] Failed to read {getattr(err, 'filename', trash_dir)}: {err}")

    for dirpath, _dirnames, filenames in os.walk(trash_dir, onerror=_on_walk_error):
        for fname in filenames:
            fp = Path(dirpath) / fname
            if fp.suffix.lower() == ".lrc":
                continue
            try:
                rel = fp.relative_to(trash_dir)
                stat = fp.stat()
                size = stat.st_size
                total_size += size
                files.append({
                    "path": str(rel),
                    "name": fp.stem,
                    "ext": fp.suffix,
                    "size": size,
                    "modified": stat.st_mtime,
                })
            except OSError as e:
                print(f"[trash] Failed to inspect {fp}: {e}")
                continue

    if walk_errors and not files:
        raise HTTPException(status_code=500, detail="Trash exists but could not be read")

    # Sort newest first so the most recently trashed files appear at the top
    files.sort(key=lambda f: f["modified"], reverse=True)
    return {"files": files, "total_size": total_size}


@app.get("/api/trash/stream")
def stream_trash_file(http_request: Request, path: str = ""):
    """Stream a trashed audio file for in-browser playback. Have a listen before you decide."""
    from settings import get_trash_dir
    user_id = http_request.state.user_id
    if not path:
        raise HTTPException(status_code=400, detail="No path specified")

    trash_dir = get_trash_dir(user_id=user_id)
    trash_path = trash_dir / path

    try:
        trash_path.resolve().relative_to(trash_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")

    if not trash_path.exists():
        raise HTTPException(status_code=404, detail="File not found in trash")

    mime = _AUDIO_MIME_TYPES.get(trash_path.suffix.lower(), "audio/mpeg")
    return FileResponse(path=str(trash_path), media_type=mime)


def _complete_restored_failed_job(
    restored: Path, user_id: str | None, is_admin: bool, artist: str, title: str
) -> str | None:
    """Mark the one failed Queue job represented by a restored audio file complete.

    Newer rejected downloads keep their intended final path, which is an exact
    match after Restore. Older rows did not record that path, so they get a
    conservative artist/title fallback only when it identifies one job.
    """
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        scope_frag, scope_params = _user_scope(user_id, is_admin)
        rows = conn.execute(
            f"""SELECT id FROM jobs
                WHERE status = 'failed' AND final_path = ? AND {scope_frag}""",
            (str(restored), *scope_params),
        ).fetchall()
        if not rows and artist and title:
            rows = conn.execute(
                f"""SELECT id FROM jobs
                    WHERE status = 'failed'
                      AND (final_path IS NULL OR final_path = '')
                      AND artist = ? COLLATE NOCASE
                      AND title = ? COLLATE NOCASE
                      AND {scope_frag}
                    LIMIT 2""",
                (artist, title, *scope_params),
            ).fetchall()
        if len(rows) != 1:
            return None
        job_id = rows[0]["id"]
        conn.execute(
            """UPDATE jobs
               SET status = 'completed', error = NULL, final_path = ?,
                   file_deleted = 0, progress_stage = NULL, completed_at = datetime('now')
               WHERE id = ?""",
            (str(restored), job_id),
        )
        conn.commit()
    return job_id


@app.post("/api/trash/restore")
def restore_trash_file(http_request: Request, path: str = ""):
    """Restore a specific file from the trash to its original library location."""
    from utils import restore_from_trash
    from settings import get_trash_dir
    user_id = http_request.state.user_id
    if not path:
        raise HTTPException(status_code=400, detail="No path specified")

    trash_dir = get_trash_dir(user_id=user_id)
    trash_path = trash_dir / path

    # Safety: make sure the resolved path is actually inside the trash dir
    try:
        trash_path.resolve().relative_to(trash_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")

    if not trash_path.exists():
        raise HTTPException(status_code=404, detail="File not found in trash")

    restored = restore_from_trash(trash_path, user_id=user_id)
    if not restored:
        raise HTTPException(status_code=500, detail="Failed to restore file")

    # A restored rejected download is a deliberate acceptance of that version.
    # Keep the Queue and watched-track state honest instead of leaving a red failed
    # card behind for a file the user has explicitly put back in their library.
    restored_job_id = None
    restored_artist = restored_title = ""
    try:
        from metadata import read_artist_title
        restored_artist, restored_title = read_artist_title(restored)
        restored_job_id = _complete_restored_failed_job(
            restored, user_id, False, restored_artist, restored_title
        )
        if restored_job_id:
            from downloads import _mark_watched_track_downloaded, _mark_watched_artist_track_downloaded
            _mark_watched_track_downloaded(restored_job_id, resolved_path=restored, skip_mismatch=True)
            _mark_watched_artist_track_downloaded(restored_job_id, resolved_path=restored)
    except Exception as e:
        # The restore itself already succeeded. A queue-reconciliation hiccup must
        # not pretend otherwise or trap the file in the bin.
        print(f"Restore Queue reconciliation failed for {restored}: {e}")

    # Re-stamp the playlist COMMENT tag: a restore just moves the file back, so a
    # track that was wrongly binned would otherwise come back untagged until the
    # next manual playlist refresh. Read the artist/title off the file and let the
    # shared helper (a no-op when the setting's off) put the playlist names back.
    try:
        from downloads import _tag_track_comment
        if restored_artist and restored_title:
            _tag_track_comment(restored, restored_artist, restored_title)
    except Exception as e:
        print(f"Restore COMMENT re-tag failed for {restored}: {e}")

    return {"restored": str(restored.name), "path": str(restored), "job_id": restored_job_id}


@app.delete("/api/trash")
def empty_trash(http_request: Request):
    """Permanently delete everything in your own trash bin. No coming back from this one.

    Bins are per-account now, so this empties the caller's, not the whole house's.
    No admin gate: that made sense when the bin was one shared heap, but a bin
    you cannot empty is a bin with a lock on the lid. Same trust model as
    /api/trash/file, which has always let you bin your own one at a time.
    """
    from settings import get_trash_dir
    import shutil
    user_id = http_request.state.user_id
    trash_dir = get_trash_dir(user_id=user_id)
    if not trash_dir.exists():
        return {"deleted": 0}

    count = sum(1 for _ in trash_dir.rglob("*") if _.is_file())
    shutil.rmtree(trash_dir, ignore_errors=True)
    return {"deleted": count}


@app.delete("/api/trash/file")
def delete_trash_file(http_request: Request, path: str = ""):
    """Permanently delete a single file from the trash. Truly gone this time."""
    from settings import get_trash_dir
    user_id = http_request.state.user_id
    if not path:
        raise HTTPException(status_code=400, detail="No path specified")

    trash_dir = get_trash_dir(user_id=user_id)
    trash_path = trash_dir / path

    try:
        trash_path.resolve().relative_to(trash_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")

    if not trash_path.exists():
        raise HTTPException(status_code=404, detail="File not found in trash")

    try:
        trash_path.unlink()
        # Also remove the lyrics sidecar if present
        lrc = trash_path.with_suffix(".lrc")
        if lrc.exists():
            lrc.unlink()
        # Clean up empty directories
        parent = trash_path.parent
        while parent != trash_dir and str(parent).startswith(str(trash_dir)):
            try:
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete: {e}")

    return {"deleted": trash_path.name}


# =============================================================================
# Blacklist / Report API
# =============================================================================

@app.post("/api/blacklist")
def add_blacklist_entry(request: BlacklistRequest, http_request: Request):
    """Report a bad track and/or block an uploader."""
    if not http_request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    if not request.video_id and not request.uploader:
        raise HTTPException(status_code=400, detail="Need at least a video_id or uploader to blacklist")

    entries_created = []

    with db_conn() as conn:
        # Blacklist the specific video
        if request.video_id:
            # Upsert  -  if the same video_id is already blacklisted, update the reason
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

    clear_automated_search_cache()
    return {"entries": entries_created}


@app.get("/api/blacklist")
def list_blacklist(http_request: Request, limit: int = 100, offset: int = 0):
    """List all blacklist entries for the management UI."""
    if not http_request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
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
def remove_blacklist_entry(entry_id: int, http_request: Request):
    """Remove a blacklist entry by ID."""
    if not http_request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    with db_conn() as conn:
        cursor = conn.execute("DELETE FROM blacklist WHERE id = ?", (entry_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Blacklist entry not found")
        conn.commit()

    clear_automated_search_cache()
    return {"deleted": entry_id}


# =============================================================================
# Bulk Import API
# =============================================================================

@app.post("/api/bulk-import-async")
def bulk_import_async(body: AsyncBulkImportRequest, http_request: Request):
    """Start an async bulk import job"""
    _enforce_peon_format(http_request, body)
    lines = body.songs.strip().split('\n')
    import_id = str(uuid.uuid4())[:8]
    user_id = http_request.state.user_id

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

    # Normalise priority_source: empty / "any" / "all" all mean "no preference".
    _priority_source = (body.priority_source or "").strip().lower() or None
    if _priority_source in ("any", "all", "none"):
        _priority_source = None
    _preferred_sources = (body.preferred_sources or "all").strip().lower() or "all"
    if _preferred_sources in ("any", "none"):
        _preferred_sources = "all"
    _playlist_name = body.playlist_name
    if body.create_playlist:
        _playlist_name = sanitize_playlist_name(body.playlist_name, body.playlist_source_url or import_id)

    # Create bulk import record
    with db_conn() as conn:
        conn.execute(
            """INSERT INTO bulk_imports
               (id, status, total_tracks, create_playlist, playlist_name, convert_audio, use_playlists_dir, user_id, preferred_sources, priority_source)
               VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (import_id, len(tracks_to_import), int(body.create_playlist),
             _playlist_name, int(body.convert_audio), int(body.use_playlists_dir), user_id, _preferred_sources, _priority_source)
        )

        # Insert all tracks
        for track_index, track in enumerate(tracks_to_import):
            target_id = ensure_acquisition_target(
                owner_type="bulk_import",
                owner_key=f"{import_id}:{track['line_num']}",
                mode="automatic",
                artist=track["artist"],
                title=track["song"],
                user_id=user_id,
                allowed_sources=_preferred_sources,
                priority_source=_priority_source,
                convert_audio=body.convert_audio,
                destination={
                    "create_playlist": bool(body.create_playlist),
                    "playlist_name": _playlist_name,
                    "use_playlists_dir": bool(body.use_playlists_dir),
                },
                conn=conn,
            )
            conn.execute(
                """INSERT INTO bulk_import_tracks
                   (import_id, line_num, artist, song, status,
                    original_youtube_video_id, acquisition_target_id)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    import_id, track["line_num"], track["artist"],
                    track["song"],
                    (
                        body.original_youtube_video_ids[track_index]
                        if body.original_youtube_video_ids
                        and track_index < len(body.original_youtube_video_ids)
                        else None
                    ),
                    target_id,
                )
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
def get_bulk_import_status(import_id: str, http_request: Request):
    """Get status of a bulk import job"""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        cursor = conn.execute(
            f"SELECT * FROM bulk_imports WHERE id = ? AND {_scope_frag}",
            (import_id, *_scope_params)
        )
        import_row = cursor.fetchone()

        if not import_row:
            raise HTTPException(status_code=404, detail="Import not found")

        import_row = _expose_conversion_intent(dict(import_row))

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

        # Count download statuses by joining bulk_import_tracks with jobs.
        # dupe_skipped: completed jobs whose error field starts with "Already exists"
        # (the duplicate-skip path in process_download marks the job completed and
        # stuffs a human-readable reason into error). It's the only API-visible
        # signal that the library dupe check actually short-circuited the work.
        cursor = conn.execute(
            """SELECT
                   SUM(CASE WHEN j.status IN ('completed', 'completed_with_errors') THEN 1 ELSE 0 END) as completed,
                   SUM(CASE WHEN j.status = 'failed' THEN 1 ELSE 0 END) as download_failed,
                   SUM(CASE WHEN j.status IN ('queued', 'downloading') THEN 1 ELSE 0 END) as still_queued,
                   SUM(CASE WHEN j.status IN ('completed', 'completed_with_errors')
                             AND (j.error LIKE 'Already exists%' OR j.error LIKE 'Already exists in %')
                            THEN 1 ELSE 0 END) as dupe_skipped
               FROM bulk_import_tracks t
               JOIN jobs j ON t.job_id = j.id
               WHERE t.import_id = ?""",
            (import_id,)
        )
        row = cursor.fetchone()
        completed_count = row[0] or 0
        download_failed_count = row[1] or 0
        still_queued_count = row[2] or 0
        dupe_skipped_count = row[3] or 0

    total_failed = import_row["failed"] + download_failed_count
    search_done = import_row["status"] in ("completed", "cancelled", "error")
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
        "dupe_skipped": dupe_skipped_count,
        "rate_limited": import_row["rate_limited_until"] is not None,
        "error": import_row["error"],
        "recent_tracks": recent_tracks,
        "complete": all_done
    }


@app.post("/api/bulk-import/{import_id}/cancel")
def cancel_bulk_import(import_id: str, http_request: Request):
    """Cancel untouched import work and retain completed files/Queue rows."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        scope_frag, scope_params = _user_scope(user_id, is_admin)
        import_row = conn.execute(
            f"SELECT id, status FROM bulk_imports WHERE id = ? AND {scope_frag}",
            (import_id, *scope_params),
        ).fetchone()
        if not import_row:
            raise HTTPException(status_code=404, detail="Import not found")
        if import_row["status"] in ("completed", "cancelled", "error"):
            return {"import_id": import_id, "status": import_row["status"], "already_finished": True}
        result = request_bulk_import_cancellation(import_id, conn=conn)
        status = conn.execute(
            "SELECT status FROM bulk_imports WHERE id = ?", (import_id,)
        ).fetchone()[0]
        conn.commit()
    return {"import_id": import_id, "status": status, **result}


@app.get("/api/bulk-imports")
def list_bulk_imports(limit: int = 10, http_request: Request = None):
    """List recent bulk imports"""
    user_id = http_request.state.user_id if http_request else None
    is_admin = http_request.state.is_admin if http_request else True
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        cursor = conn.execute(
            f"SELECT * FROM bulk_imports WHERE {_scope_frag} ORDER BY created_at DESC LIMIT ?",
            (*_scope_params, limit)
        )
        imports = [_expose_conversion_intent(dict(row)) for row in cursor.fetchall()]

    return {"imports": imports}


# =============================================================================
# Playlist Fetch API (Spotify, Amazon Music)
# =============================================================================

@app.post("/api/fetch-playlist")
@app.post("/api/spotify-playlist")  # Backwards compat
def fetch_playlist(request: Request, body: PlaylistFetchRequest):
    """Fetch track list from a supported public playlist URL."""
    url = body.url.strip()
    platform, _ = detect_playlist_platform(url)

    # fetch_playlist_tracks returns (artist, title) tuples - reformat to the
    # "Artist - Title" strings the bulk import UI expects, plus a playlist name.
    user_id = request.state.user_id
    fetched = fetch_playlist_tracks(url, platform, user_id=user_id)
    tracks_tuples, playlist_name, warning = fetched[:3]
    original_video_ids = fetched[3] if len(fetched) > 3 else [None] * len(tracks_tuples)
    playlist_name = sanitize_playlist_name(playlist_name, url)
    tracks = [f"{artist} - {title}" for artist, title in tracks_tuples]
    resp = {"tracks": tracks, "playlist_name": playlist_name, "count": len(tracks), "platform": platform}
    if any(original_video_ids):
        resp["original_youtube_video_ids"] = original_video_ids
    if warning:
        resp["warning"] = warning
    return resp


# =============================================================================
# Long-form YouTube Splitting API
# =============================================================================

@app.post("/api/longform/detect")
def longform_detect(body: LongformDetectRequest, request: Request):
    """Probe a single YouTube video for long-form split eligibility. No download."""
    user_id = request.state.user_id
    return detect_longform_candidate(body.url.strip(), user_id=user_id)


@app.post("/api/longform/split")
def longform_split(body: LongformSplitRequest, request: Request):
    """Confirm a split: download the video once, then cut/tag each segment."""
    user_id = request.state.user_id
    segments = [s.model_dump() for s in body.segments]
    error = validate_segments(segments)
    if error:
        raise HTTPException(status_code=400, detail=error)
    split_id = create_longform_split(
        body.url.strip(), body.video_title.strip(), body.album_artist.strip(), segments, user_id=user_id,
    )
    return {"split_id": split_id, "total_segments": len(segments), "status": "pending"}


@app.get("/api/longform/{split_id}/status")
def longform_status(split_id: str, http_request: Request):
    """Poll a long-form split's progress."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        scope_frag, scope_params = _user_scope(user_id, is_admin)
        split_row = conn.execute(
            f"SELECT * FROM longform_splits WHERE id = ? AND {scope_frag}",
            (split_id, *scope_params),
        ).fetchone()
        if not split_row:
            raise HTTPException(status_code=404, detail="Split not found")
        segments = conn.execute(
            "SELECT segment_index, title, artist, status, error, final_path FROM longform_split_segments "
            "WHERE split_id = ? ORDER BY segment_index",
            (split_id,),
        ).fetchall()

    split_row = dict(split_row)
    complete = split_row["status"] in ("completed", "completed_with_errors", "failed", "cancelled")
    return {
        "split_id": split_id,
        "status": split_row["status"],
        "video_title": split_row["video_title"],
        "total_segments": split_row["total_segments"],
        "completed_segments": split_row["completed_segments"],
        "failed_segments": split_row["failed_segments"],
        "error": split_row["error"],
        "segments": [dict(s) for s in segments],
        "complete": complete,
    }


@app.post("/api/longform/{split_id}/cancel")
def longform_cancel(split_id: str, http_request: Request):
    """Ask an in-progress split to stop before its next segment."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        scope_frag, scope_params = _user_scope(user_id, is_admin)
        split_row = conn.execute(
            f"SELECT id FROM longform_splits WHERE id = ? AND {scope_frag}",
            (split_id, *scope_params),
        ).fetchone()
    if not split_row:
        raise HTTPException(status_code=404, detail="Split not found")
    cancelled = cancel_longform_split(split_id)
    return {"split_id": split_id, "cancelled": cancelled}


# =============================================================================
# Watched Playlists API
# =============================================================================

@app.post("/api/watched-playlists")
def add_watched_playlist(body: WatchedPlaylistRequest, http_request: Request):
    """Add a new playlist to watch for new tracks"""
    _enforce_peon_format(http_request, body)
    platform, platform_id = detect_playlist_platform(body.url)
    user_id = http_request.state.user_id

    # ListenBrainz username: fan out into one watched playlist per "Created for You" playlist
    if platform == "listenbrainz_user":
        lb_playlists = fetch_listenbrainz_createdfor(platform_id)

        # LB playlists are weekly  -  mirror mode is the sensible default
        sync_mode = "mirror"
        # Weekly refresh makes sense since LB regenerates them weekly
        refresh_hours = max(body.refresh_interval_hours, 168)

        created = []
        skipped = 0

        with db_conn() as conn:
            conn.row_factory = sqlite3.Row
            for lb in lb_playlists:
                existing = conn.execute(
                    "SELECT id FROM watched_playlists WHERE url = ? AND (user_id = ? OR (user_id IS NULL AND ? IS NULL))",
                    (lb["playlist_url"], user_id, user_id)
                ).fetchone()
                if not existing:
                    # LB rotates playlist UUIDs weekly, so also match by username + name prefix
                    # (the part before ", week of YYYY-MM-DD") to avoid duplicate watched playlists.
                    name_prefix = lb["name"].split(", week of")[0]
                    existing = conn.execute(
                        """SELECT id FROM watched_playlists
                           WHERE lb_username = ? AND name LIKE ?
                           AND (user_id = ? OR (user_id IS NULL AND ? IS NULL))""",
                        (platform_id, name_prefix + "%", user_id, user_id)
                    ).fetchone()
                if existing:
                    skipped += 1
                    continue

                playlist_id = str(uuid.uuid4())[:8]
                _lb_priority = (body.priority_source or "").strip().lower() or None
                if _lb_priority in ("any", "all", "none"):
                    _lb_priority = None
                conn.execute("""
                    INSERT INTO watched_playlists
                    (id, url, name, platform, refresh_interval_hours, last_checked, convert_audio, make_m3u, use_playlists_dir, sync_mode, last_track_count, user_id, preferred_sources, priority_source, lb_username, custom_subdir, redownload_missing_files)
                    VALUES (?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (playlist_id, lb["playlist_url"], lb["name"], "listenbrainz",
                      refresh_hours, int(body.convert_audio),
                      int(body.make_m3u), int(body.use_playlists_dir), sync_mode, len(lb["tracks"]), user_id,
                      body.preferred_sources or "all", _lb_priority, platform_id,
                      (body.custom_subdir or "").strip() or None,
                      None if body.redownload_missing_files is None else int(body.redownload_missing_files)))

                for artist, title in lb["tracks"]:
                    track_hash = hash_track(artist, title)
                    conn.execute("""
                        INSERT OR IGNORE INTO watched_playlist_tracks
                        (playlist_id, track_hash, artist, title)
                        VALUES (?, ?, ?, ?)
                    """, (playlist_id, track_hash, artist, title))

                created.append({"id": playlist_id, "name": lb["name"], "track_count": len(lb["tracks"])})

            conn.commit()

        if not created:
            raise HTTPException(status_code=409, detail="All ListenBrainz 'Created for You' playlists are already being watched")

        # Kick off bulk imports in background for each new playlist
        for i, lb in enumerate([p for p in lb_playlists if any(c["name"] == p["name"] for c in created)]):
            playlist_id = created[i]["id"]
            if lb["tracks"]:
                start_bulk_import_for_tracks(
                    lb["tracks"],
                    body.convert_audio,
                    watch_playlist_id=playlist_id,
                    use_playlists_dir=body.use_playlists_dir,
                    user_id=user_id,
                    preferred_sources=body.preferred_sources or "all",
                    priority_source=body.priority_source,
                    custom_subdir=(body.custom_subdir or "").strip() or None,
                )

        wake_scheduler()

        return {
            "created": len(created),
            "skipped": skipped,
            "playlists": created,
            "message": f"Added {len(created)} ListenBrainz playlist(s)"
            + (f" ({skipped} already watched)" if skipped else ""),
        }

    with db_conn() as conn:
        # Check for duplicate
        conn.row_factory = sqlite3.Row

        existing = conn.execute(
            "SELECT id FROM watched_playlists WHERE url = ? AND (user_id = ? OR (user_id IS NULL AND ? IS NULL))",
            (body.url, user_id, user_id)
        ).fetchone()

        if existing:
            raise HTTPException(status_code=409, detail="This playlist is already being watched")

        # Fetch playlist to get name and initial tracks
        try:
            fetched = fetch_playlist_tracks(body.url, platform, user_id=user_id)
            tracks, playlist_name, fetch_warning = fetched[:3]
            original_video_ids = fetched[3] if len(fetched) > 3 else [None] * len(tracks)
            playlist_name = sanitize_playlist_name(playlist_name, body.url)
        except HTTPException:
            raise

        # Create playlist record
        playlist_id = str(uuid.uuid4())[:8]

        sync_mode = body.sync_mode if body.sync_mode in ("append", "mirror") else "append"
        _new_priority = (body.priority_source or "").strip().lower() or None
        if _new_priority in ("any", "all", "none"):
            _new_priority = None
        _new_preferred_sources = (
            "soundcloud"
            if platform == "soundcloud"
            and (not body.preferred_sources or body.preferred_sources == "all")
            else (body.preferred_sources or "all")
        )
        conn.execute("""
            INSERT INTO watched_playlists
            (id, url, name, platform, refresh_interval_hours, last_checked, convert_audio, make_m3u, use_playlists_dir, sync_mode, last_track_count, user_id, preferred_sources, priority_source, custom_subdir, redownload_missing_files)
            VALUES (?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (playlist_id, body.url, playlist_name, platform,
              body.refresh_interval_hours, int(body.convert_audio),
              int(body.make_m3u), int(body.use_playlists_dir), sync_mode, len(tracks), user_id,
              _new_preferred_sources,
              _new_priority,
              (body.custom_subdir or "").strip() or None,
              None if body.redownload_missing_files is None else int(body.redownload_missing_files)))

        # Insert all current tracks as "seen"
        for index, (artist, title) in enumerate(tracks):
            track_hash = hash_track(artist, title)
            conn.execute("""
                INSERT OR IGNORE INTO watched_playlist_tracks
                (playlist_id, track_hash, artist, title, original_youtube_video_id)
                VALUES (?, ?, ?, ?, ?)
            """, (playlist_id, track_hash, artist, title, original_video_ids[index]))

        conn.commit()

    import_id = None
    if tracks:
        import_id = start_bulk_import_for_tracks(
            tracks,
            body.convert_audio,
            watch_playlist_id=playlist_id,
            use_playlists_dir=body.use_playlists_dir,
            user_id=user_id,
            preferred_sources=_new_preferred_sources,
            priority_source=body.priority_source,
            custom_subdir=(body.custom_subdir or "").strip() or None,
            original_youtube_video_ids=original_video_ids,
        )

    resp = {
        "id": playlist_id,
        "name": playlist_name,
        "platform": platform,
        "track_count": len(tracks),
        "refresh_interval_hours": body.refresh_interval_hours,
        "import_id": import_id,
        "message": f"Now watching '{playlist_name}' with {len(tracks)} tracks queued for download"
    }
    if fetch_warning:
        resp["warning"] = fetch_warning
    wake_scheduler()
    return resp


@app.get("/api/watched-playlists")
def list_watched_playlists(http_request: Request):
    """List all watched playlists"""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        # Counts describe what the playlist contains NOW, not everything it has
        # ever contained. A 50-track chart that churns weekly used to report
        # hundreds of "tracks", which told the user nothing useful about either
        # the playlist or their library. Departed tracks are counted separately;
        # append mode keeps them, mirror mode has merely stopped listing them.
        playlists = conn.execute(f"""
            SELECT
                wp.*,
                (SELECT COUNT(*) FROM watched_playlist_tracks wpt
                  WHERE wpt.playlist_id = wp.id AND wpt.removed_at IS NULL) as tracked_count,
                (SELECT COUNT(*) FROM watched_playlist_tracks wpt
                  WHERE wpt.playlist_id = wp.id AND wpt.removed_at IS NULL
                    AND wpt.downloaded_at IS NOT NULL) as downloaded_count,
                (SELECT COUNT(*) FROM watched_playlist_tracks wpt
                  WHERE wpt.playlist_id = wp.id AND wpt.removed_at IS NOT NULL) as departed_count
            FROM watched_playlists wp
            WHERE {_scope_frag}
            ORDER BY wp.created_at DESC
        """, _scope_params).fetchall()

    return {
        "playlists": [_expose_conversion_intent(dict(p)) for p in playlists]
    }


@app.get("/api/watched-playlists/schedule")
def get_watched_schedule():
    """Get the current watched playlist check schedule"""
    return {
        "check_interval_hours": WATCHED_PLAYLIST_CHECK_HOURS,
        "enabled": WATCHED_PLAYLIST_CHECK_HOURS > 0
    }


def _scoped_playlist_dicts(http_request: Request) -> list[dict]:
    """The caller's watched playlists as plain dicts (admins get the lot)."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        frag, params = _user_scope(user_id, is_admin)
        rows = conn.execute(
            f"""SELECT id, name, user_id, use_playlists_dir, custom_subdir, sync_mode
                FROM watched_playlists WHERE {frag}""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/watched-playlists/orphans")
@prioritise_over_audio_audit
def list_playlist_orphans(http_request: Request):
    """Audio files sitting in playlist folders that no watched playlist claims.

    Mirror-mode removals leave files behind by design; this rounds them up so the
    user can decide whether to rehome them in Singles. Read-only; nothing moves.
    """
    from watched_playlists import find_orphaned_playlist_files
    return {"orphans": find_orphaned_playlist_files(_scoped_playlist_dicts(http_request))}


@app.post("/api/watched-playlists/orphans/move")
def move_playlist_orphans(request: OrphanMoveRequest, http_request: Request):
    """Move selected orphaned playlist files into the Singles layout.

    Only files directly inside the caller's watched playlist folders are eligible;
    anything else is skipped, not errored, so a stale UI list can't cause drama.
    """
    if not request.files:
        raise HTTPException(status_code=400, detail="No files given")
    from watched_playlists import move_orphans_to_singles
    return move_orphans_to_singles(request.files, _scoped_playlist_dicts(http_request))


@app.get("/api/watched-playlists/{playlist_id}")
def get_watched_playlist(playlist_id: str, http_request: Request):
    """Get details of a watched playlist including track history"""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        playlist = conn.execute(
            f"SELECT * FROM watched_playlists WHERE id = ? AND {_scope_frag}",
            (playlist_id, *_scope_params)
        ).fetchone()

        if not playlist:
            raise HTTPException(status_code=404, detail="Watched playlist not found")

        tracks = conn.execute("""
            SELECT * FROM watched_playlist_tracks
            WHERE playlist_id = ?
            ORDER BY first_seen DESC
        """, (playlist_id,)).fetchall()

    return {
        "playlist": _expose_conversion_intent(dict(playlist)),
        "tracks": [dict(t) for t in tracks]
    }


@app.put("/api/watched-playlists/{playlist_id}")
def update_watched_playlist(playlist_id: str, request: WatchedPlaylistUpdate, http_request: Request):
    """Update watched playlist settings"""
    if _is_peon(http_request):
        request.convert_audio = None  # Peons cannot change conversion setting
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        playlist = conn.execute(
            f"SELECT * FROM watched_playlists WHERE id = ? AND {_scope_frag}",
            (playlist_id, *_scope_params)
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
            if request.enabled:
                # Resuming gives the playlist a clean slate: drop any auto-pause
                # note and reset the gone-strike counter so it isn't re-paused on
                # the next single hiccup.
                updates.append("auto_paused = 0")
                updates.append("pause_reason = NULL")
                updates.append("gone_strikes = 0")

        if request.convert_audio is not None:
            updates.append("convert_audio = ?")
            params.append(int(request.convert_audio))

        if request.make_m3u is not None:
            updates.append("make_m3u = ?")
            params.append(int(request.make_m3u))

        if request.use_playlists_dir is not None:
            updates.append("use_playlists_dir = ?")
            params.append(int(request.use_playlists_dir))

        if request.sync_mode is not None and request.sync_mode in ("append", "mirror"):
            updates.append("sync_mode = ?")
            params.append(request.sync_mode)

        if request.preferred_sources is not None:
            updates.append("preferred_sources = ?")
            params.append(request.preferred_sources or "all")

        if request.priority_source is not None:
            # Empty / "any" / "all" / "none" all clear the override.
            _p = (request.priority_source or "").strip().lower() or None
            if _p in ("any", "all", "none"):
                _p = None
            updates.append("priority_source = ?")
            params.append(_p)

        if request.custom_subdir is not None:
            updates.append("custom_subdir = ?")
            params.append((request.custom_subdir or "").strip() or None)

        if "redownload_missing_files" in request.model_fields_set:
            updates.append("redownload_missing_files = ?")
            params.append(
                None if request.redownload_missing_files is None
                else int(request.redownload_missing_files)
            )

        if updates:
            params.append(playlist_id)
            conn.execute(
                f"UPDATE watched_playlists SET {', '.join(updates)} WHERE id = ?",
                params
            )
            conn.commit()

        # Fetch updated record
        updated = conn.execute(
            f"SELECT * FROM watched_playlists WHERE id = ? AND {_scope_frag}",
            (playlist_id, *_scope_params)
        ).fetchone()

    wake_scheduler()
    return {"playlist": dict(updated)}


@app.delete("/api/watched-playlists/{playlist_id}")
def delete_watched_playlist(playlist_id: str, http_request: Request):
    """Remove a watched playlist and its track history"""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        playlist = conn.execute(
            f"SELECT * FROM watched_playlists WHERE id = ? AND {_scope_frag}",
            (playlist_id, *_scope_params)
        ).fetchone()

        if not playlist:
            raise HTTPException(status_code=404, detail="Watched playlist not found")

        # Cancellation and deletion share this transaction. The worker can see
        # the cancellation flag before the playlist row disappears, so it may
        # finish its active files but cannot schedule the untouched tail.
        cancelled = request_bulk_import_cancellation(
            watch_playlist_id=playlist_id,
            conn=conn,
        )
        # Delete tracks first (FK constraint)
        conn.execute("DELETE FROM watched_playlist_tracks WHERE playlist_id = ?", (playlist_id,))
        conn.execute("DELETE FROM watched_playlists WHERE id = ?", (playlist_id,))
        conn.commit()

    wake_scheduler()
    return {
        "message": f"Deleted watched playlist '{playlist['name']}'",
        "cancelled_imports": cancelled["imports"],
        "cancelled_jobs": cancelled["jobs"],
    }


@app.get("/api/watched-playlists/{playlist_id}/missing")
def get_missing_watched_tracks(playlist_id: str, http_request: Request):
    """Return tracks that were never successfully downloaded for a watched playlist.

    A track is 'missing' if it has no downloaded_at timestamp and either has no job,
    or its job ended in failure. Tracks still actively queued or downloading are excluded.

    In mirror mode, a track that has left the upstream playlist is not missing, it
    is simply gone, so it is excluded; chasing it would be downloading something
    the playlist no longer asks for. Append mode still lists them, because there
    it is history it deliberately keeps and can still fill in.
    """
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        playlist = conn.execute(
            f"SELECT name, sync_mode FROM watched_playlists WHERE id = ? AND {_scope_frag}",
            (playlist_id, *_scope_params)
        ).fetchall()
        playlist = playlist[0] if playlist else None

        if not playlist:
            raise HTTPException(status_code=404, detail="Watched playlist not found")

        _departed_frag = (
            "AND wpt.removed_at IS NULL"
            if (playlist["sync_mode"] or "append") == "mirror" else ""
        )
        rows = conn.execute(
            f"""SELECT wpt.artist, wpt.title, wpt.first_seen, wpt.removed_at, j.status as job_status
               FROM watched_playlist_tracks wpt
               LEFT JOIN jobs j ON wpt.job_id = j.id
               WHERE wpt.playlist_id = ?
                 AND wpt.downloaded_at IS NULL
                 {_departed_frag}
                 AND (j.status IS NULL OR j.status IN ('failed', 'completed_with_errors'))""",
            (playlist_id,)
        ).fetchall()

    return {
        "playlist_name": playlist["name"],
        "missing": [dict(r) for r in rows],
        "count": len(rows),
    }


@app.get("/api/watched-playlists/{playlist_id}/tracks")
def get_watched_playlist_tracks(playlist_id: str, http_request: Request):
    """Return all tracks for a watched playlist with their download status.

    Each track includes its current job status so the UI can show downloaded,
    failed, pending, and departed tracks in one place. Every row is returned,
    but the summary counts describe current membership only, so they agree with
    the figures on the playlist card instead of quietly contradicting them.
    """
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        playlist = conn.execute(
            f"SELECT name, sync_mode FROM watched_playlists WHERE id = ? AND {_scope_frag}",
            (playlist_id, *_scope_params)
        ).fetchall()
        playlist = playlist[0] if playlist else None

        if not playlist:
            raise HTTPException(status_code=404, detail="Watched playlist not found")

        rows = conn.execute(
            """SELECT wpt.track_hash, wpt.artist, wpt.title, wpt.downloaded_at, wpt.job_id,
                      wpt.first_seen, wpt.removed_at,
                      j.status as job_status, j.error as job_error
               FROM watched_playlist_tracks wpt
               LEFT JOIN jobs j ON wpt.job_id = j.id
               WHERE wpt.playlist_id = ?
               ORDER BY wpt.first_seen""",
            (playlist_id,)
        ).fetchall()

    tracks = [dict(r) for r in rows]
    current = [t for t in tracks if not t["removed_at"]]
    downloaded = sum(1 for t in current if t["downloaded_at"])
    failed = sum(1 for t in current if not t["downloaded_at"] and t["job_status"] in ("failed", "completed_with_errors", None))
    pending = sum(1 for t in current if not t["downloaded_at"] and t["job_status"] in ("queued", "downloading"))

    return {
        "playlist_name": playlist["name"],
        "sync_mode": playlist["sync_mode"] or "append",
        "tracks": tracks,
        "total": len(current),
        "downloaded": downloaded,
        "failed": failed,
        "pending": pending,
        "departed": len(tracks) - len(current),
    }


@app.delete("/api/watched-playlists/{playlist_id}/tracks/{track_hash}/file")
def delete_watched_playlist_track_file(
    playlist_id: str,
    track_hash: str,
    http_request: Request,
):
    """Trash a watched track by its stable playlist identity.

    Queue rows are disposable history. A playlist track is not, so Replace must
    not depend on the job which happened to download its current file.
    """
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        track = conn.execute(
            f"""SELECT wpt.artist, wpt.title, wpt.resolved_path, wpt.job_id,
                       wp.id, wp.name, wp.make_m3u, wp.use_playlists_dir,
                       wp.sync_mode, wp.custom_subdir, wp.user_id
                FROM watched_playlist_tracks wpt
                JOIN watched_playlists wp ON wp.id = wpt.playlist_id
                WHERE wpt.playlist_id = ? AND wpt.track_hash = ?
                  AND {_scope_frag}""",
            (playlist_id, track_hash, *_scope_params),
        ).fetchone()

    if not track:
        raise HTTPException(status_code=404, detail="Watched playlist track not found")

    artist = track["artist"] or ""
    title = track["title"] or ""
    if not artist or not title:
        raise HTTPException(status_code=400, detail="Track has no artist/title metadata")

    from utils import check_duplicate, move_to_trash

    existing = authoritative_audio_file(track["resolved_path"], user_id)
    if existing is None:
        existing = check_duplicate(artist, title, user_id=user_id)

    trashed_files = []
    if existing:
        trash_result = move_to_trash(existing, user_id=user_id)
        if trash_result:
            trashed_files.append(existing.name)
            lrc_trash = trash_result.with_suffix(".lrc")
            if lrc_trash.exists():
                trashed_files.append(lrc_trash.name)
        else:
            try:
                existing.unlink()
                trashed_files.append(existing.name)
                lrc_file = existing.with_suffix(".lrc")
                if lrc_file.exists():
                    lrc_file.unlink()
                    trashed_files.append(lrc_file.name)
                track_dir = existing.parent
                if track_dir.exists() and not any(track_dir.iterdir()):
                    track_dir.rmdir()
            except OSError as e:
                raise HTTPException(status_code=500, detail=f"Failed to delete: {e}")

    with db_conn() as conn:
        if track["job_id"]:
            _scope_frag, _scope_params = _user_scope(user_id, is_admin)
            conn.execute(
                f"UPDATE jobs SET file_deleted = 1 WHERE id = ? AND {_scope_frag}",
                (track["job_id"], *_scope_params),
            )
        conn.execute(
            """UPDATE watched_playlist_tracks
               SET downloaded_at = NULL, resolved_path = NULL, job_id = NULL
               WHERE playlist_id = ? AND track_hash = ?""",
            (playlist_id, track_hash),
        )
        conn.commit()

    if track["make_m3u"]:
        rebuild_watched_playlist_m3u(
            track["id"], track["name"],
            use_playlists_dir=bool(track["use_playlists_dir"]),
            sync_mode=track["sync_mode"] or "append",
            user_id=track["user_id"],
            custom_subdir=track["custom_subdir"] or None,
        )

    return {
        "deleted": trashed_files,
        "playlist_id": playlist_id,
        "track_hash": track_hash,
        "trashed": bool(trashed_files),
    }


@app.post("/api/watched-playlists/{playlist_id}/retry-track")
def retry_missing_track(playlist_id: str, request: RetryMissingTrackRequest, http_request: Request):
    """Manually retry a single missing track for a watched playlist.

    Searches all sources for the best match and queues a download, routing the
    result back into the watched playlist's folder and M3U (if enabled).
    """
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        playlist = conn.execute(
            f"SELECT id, name, convert_audio, use_playlists_dir, preferred_sources, priority_source, custom_subdir FROM watched_playlists WHERE id = ? AND {_scope_frag}",
            (playlist_id, *_scope_params)
        ).fetchone()

    if not playlist:
        raise HTTPException(status_code=404, detail="Watched playlist not found")

    custom_subdir = (playlist["custom_subdir"] or "").strip() or None
    # Kick off a single-track bulk import linked to this watched playlist so the
    # M3U rebuild and watched_playlist_tracks update happen automatically on completion.
    import_id = start_bulk_import_for_tracks(
        tracks=[(request.artist, request.title)],
        convert_audio=bool(playlist["convert_audio"]),
        watch_playlist_id=playlist_id,
        use_playlists_dir=bool(playlist["use_playlists_dir"]),
        user_id=user_id,
        preferred_sources=playlist["preferred_sources"] or "all",
        priority_source=playlist["priority_source"],
        custom_subdir=custom_subdir,
    )

    return {"import_id": import_id, "status": "queued", "message": f"Searching for {request.artist} - {request.title}"}


@app.get("/api/watched-playlists/{playlist_id}/track-candidates")
def get_watched_playlist_track_candidates(
    playlist_id: str,
    artist: str,
    title: str,
    http_request: Request,
    limit: int = 4,
):
    """Return top manual-pick candidates for a missing watched-playlist track."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    artist = (artist or "").strip()
    title = (title or "").strip()
    if not artist or not title:
        raise HTTPException(status_code=400, detail="artist and title are required")

    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        playlist = conn.execute(
            f"""SELECT id, preferred_sources
                FROM watched_playlists
                WHERE id = ? AND {_scope_frag}""",
            (playlist_id, *_scope_params)
        ).fetchone()

    if not playlist:
        raise HTTPException(status_code=404, detail="Watched playlist not found")

    preferred_sources = playlist["preferred_sources"] or "all"
    sources = None if preferred_sources == "all" else [s.strip() for s in preferred_sources.split(",") if s.strip()]
    query = f"{artist} - {title}".strip(" -")
    fetch_limit = max(1, min(limit, 10))
    # Someone is waiting on this list in the UI, so queue briefly rather than
    # making them stare at a spinner while a bulk import finishes its turn.
    results, _ = search_all(query, limit=fetch_limit, sources=sources, include_soulseek=True,
                            slot_wait=SEARCH_SLOT_WAIT_INTERACTIVE)

    return {
        "query": query,
        "results": results[:fetch_limit],
    }


@app.post("/api/watched-playlists/{playlist_id}/queue-track-candidate")
def queue_watched_playlist_track_candidate(
    playlist_id: str,
    request: QueueMissingTrackCandidateRequest,
    http_request: Request,
):
    """Queue a specific candidate for a missing watched-playlist track."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    source = (request.source or "youtube").strip().lower()
    artist = (request.artist or "").strip()
    title = (request.title or "").strip()
    if not artist or not title:
        raise HTTPException(status_code=400, detail="artist and title are required")

    if source == "youtube":
        if not request.video_id or not is_valid_youtube_id(request.video_id):
            raise HTTPException(status_code=400, detail="Invalid YouTube video ID")
    elif source in URL_BASED_SOURCES:
        if not request.source_url:
            raise HTTPException(status_code=400, detail=f"{source.capitalize()} candidate requires source_url")
        try:
            validate_source_url(source, request.source_url)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    elif source == "soulseek":
        if not (request.slskd_username and request.slskd_filename):
            raise HTTPException(status_code=400, detail="Soulseek candidate requires username and filename")
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported source: {source}")

    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        playlist = conn.execute(
            f"""SELECT id, name, convert_audio, use_playlists_dir, custom_subdir,
                       preferred_sources, priority_source
                FROM watched_playlists
                WHERE id = ? AND {_scope_frag}""",
            (playlist_id, *_scope_params)
        ).fetchone()
        if not playlist:
            raise HTTPException(status_code=404, detail="Watched playlist not found")

        track_hash = hash_track(artist, title)
        track = conn.execute(
            "SELECT playlist_id FROM watched_playlist_tracks WHERE playlist_id = ? AND track_hash = ?",
            (playlist_id, track_hash),
        ).fetchone()
        if not track:
            raise HTTPException(status_code=404, detail="Watched playlist track not found")

        job_id = str(uuid.uuid4())[:8]
        if source == "soulseek":
            source_url = f"soulseek://{request.slskd_username}/{request.slskd_filename}"
        elif source in URL_BASED_SOURCES:
            source_url = request.source_url
        else:
            source_url = f"https://www.youtube.com/watch?v={request.video_id}"

        target_id = ensure_acquisition_target(
            owner_type="watched_playlist",
            owner_key=f"{playlist_id}:{track_hash}",
            mode="automatic",
            artist=artist,
            title=title,
            user_id=user_id,
            allowed_sources=playlist["preferred_sources"] or "all",
            priority_source=playlist["priority_source"],
            convert_audio=bool(playlist["convert_audio"]),
            destination={
                "watch_playlist_id": playlist_id,
                "use_playlists_dir": bool(playlist["use_playlists_dir"]),
                "custom_subdir": playlist["custom_subdir"],
            },
            conn=conn,
        )

        conn.execute(
            """INSERT INTO jobs
               (id, video_id, title, artist, status, download_type, source, slskd_username, slskd_filename,
                slskd_size, convert_audio, source_url, user_id, acquisition_target_id)
               VALUES (?, ?, ?, ?, 'queued', 'single', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                request.video_id,
                title,
                artist,
                source,
                request.slskd_username,
                request.slskd_filename,
                request.slskd_size,
                int(bool(playlist["convert_audio"])),
                source_url,
                user_id,
                target_id,
            ),
        )
        conn.execute(
            "UPDATE watched_playlist_tracks SET job_id = ?, downloaded_at = NULL WHERE playlist_id = ? AND track_hash = ?",
            (job_id, playlist_id, track_hash),
        )
        conn.commit()

    acquisition_cycle = begin_acquisition_cycle(target_id)
    attach_job_to_acquisition(job_id, target_id, acquisition_cycle)

    convert_audio = bool(playlist["convert_audio"])
    custom_subdir = (playlist["custom_subdir"] or "").strip() or None
    playlist_name = playlist["name"] if (playlist["use_playlists_dir"] or custom_subdir) else None
    use_playlists_dir = bool(playlist["use_playlists_dir"])

    selected_candidate = {
        "video_id": request.video_id,
        "title": title,
        "artist": artist,
        "channel": artist,
        "source": source,
        "source_url": source_url,
        "slskd_username": request.slskd_username,
        "slskd_filename": request.slskd_filename,
        "slskd_size": request.slskd_size,
    }
    spawn_daemon_thread(
        process_acquisition_cycle,
        job_id,
        selected_candidate,
        artist,
        title,
        convert_audio,
        target_id=target_id,
        cycle=acquisition_cycle,
        automatic=False,
        allowed_sources={source},
        priority_source=source,
        playlist_name=playlist_name,
        use_playlists_dir=use_playlists_dir,
        user_id=user_id,
        custom_subdir=custom_subdir,
    )

    return {"job_id": job_id, "status": "queued", "message": f"Queued {artist} - {title}"}


@app.post("/api/watched-playlists/{playlist_id}/refresh")
def refresh_single_playlist(playlist_id: str, http_request: Request):
    """Force an immediate refresh of a specific watched playlist"""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        playlist = conn.execute(
            f"SELECT * FROM watched_playlists WHERE id = ? AND {_scope_frag}",
            (playlist_id, *_scope_params)
        ).fetchone()

        if not playlist:
            raise HTTPException(status_code=404, detail="Watched playlist not found")

    result = refresh_watched_playlist(playlist_id)
    return result


@app.post("/api/watched-playlists/check-all")
def check_all_watched_playlists(http_request: Request):
    """Check all playlists due for refresh (called by cron)"""
    if not http_request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
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


@app.post("/api/watched-playlists/tag-all-playlists-comment")
def tag_all_playlists_comment(http_request: Request):
    """One-shot backfill: stamp playlist name(s) into COMMENT for every downloaded
    watched track, so an existing library catches up with the macOS Music feature.
    New downloads are tagged automatically at completion; this is for what came before.
    """
    if not http_request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    if not get_setting_bool("playlist_comment_tagging", False):
        raise HTTPException(status_code=400, detail="Enable 'Tag playlist names in COMMENT' in Settings first")

    from metadata import set_playlist_comment
    from downloads import _all_playlist_names_for_track

    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT DISTINCT artist, title, resolved_path
               FROM watched_playlist_tracks
               WHERE downloaded_at IS NOT NULL AND resolved_path IS NOT NULL"""
        ).fetchall()

    tagged = skipped = errors = 0
    for row in rows:
        audio_file = Path(row["resolved_path"])
        if not audio_file.exists():
            skipped += 1
            continue
        try:
            with db_conn() as conn2:
                conn2.row_factory = sqlite3.Row
                names = _all_playlist_names_for_track(conn2, row["artist"], row["title"])
            if names and set_playlist_comment(audio_file, names):
                tagged += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"Tag-all COMMENT failed for {audio_file}: {e}")
            errors += 1

    return {"tagged": tagged, "skipped": skipped, "errors": errors}


# ---------------------------------------------------------------------------
# Watched Artists
# ---------------------------------------------------------------------------

@app.get("/api/watched-artists/search")
def search_watched_artist(q: str):
    """Search MusicBrainz for an artist by name. Returns up to 5 candidates."""
    if not q or not q.strip():
        return {"results": []}
    results = search_artist_mbid(q.strip())
    return {"results": results}


@app.post("/api/watched-artists")
def add_watched_artist(body: WatchedArtistRequest, http_request: Request):
    """Add an artist to watch. Seeds all known singles then queues any after from_date."""
    _enforce_peon_format(http_request, body)
    user_id = http_request.state.user_id

    # Singles and albums are independent follows, so arriving here for an artist
    # you already watch is no longer an error: it means "and albums too, please".
    # Switch on whichever mode was asked for and leave the other one exactly as it
    # was; nobody wants to lose their singles history by browsing an album list.
    with db_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM watched_artists WHERE mbid = ? AND (user_id = ? OR (user_id IS NULL AND ? IS NULL))",
            (body.mbid, user_id, user_id)
        ).fetchone()

    if existing:
        artist_id = existing[0]
        with db_conn() as conn:
            if body.watch_singles:
                # Re-following for singles re-applies the from_date the user just
                # picked; that is the whole point of the form they filled in.
                conn.execute(
                    "UPDATE watched_artists SET watch_singles = 1, from_date = ?, enabled = 1 WHERE id = ?",
                    (body.from_date, artist_id)
                )
            conn.commit()
        already_watched = True
    else:
        artist_id = str(uuid.uuid4())[:8]
        already_watched = False
        with db_conn() as conn:
            conn.execute(
                """INSERT INTO watched_artists
                   (id, name, mbid, from_date, refresh_interval_hours, convert_audio,
                    watch_singles, user_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (artist_id, body.name, body.mbid, body.from_date,
                 body.refresh_interval_hours, int(body.convert_audio),
                 int(body.watch_singles), user_id)
            )
            conn.commit()

    # If auto_add_albums was requested, seed the current album list as
    # already-seen BEFORE flipping the flag on, so the first refresh (spawned
    # below) can never mistake the whole back catalogue for new arrivals. If
    # MusicBrainz is unreachable right now, the artist is still created and
    # watched for singles as normal; auto_add_albums simply stays off and the
    # warning tells the user to flip the toggle again once MB is back.
    auto_add_albums_warning = None
    already_auto_adding = False
    if already_watched:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT auto_add_albums FROM watched_artists WHERE id = ?", (artist_id,)
            ).fetchone()
            already_auto_adding = bool(row and row[0])
    if body.auto_add_albums and not already_auto_adding:
        try:
            seed_artist_albums(artist_id, body.mbid, user_id=user_id)
            with db_conn() as conn:
                conn.execute("UPDATE watched_artists SET auto_add_albums = 1 WHERE id = ?", (artist_id,))
                conn.commit()
        except MusicBrainzUnavailable as e:
            auto_add_albums_warning = (
                f"Could not seed the current album list ({e}); auto-add-albums was left off. "
                "Try the toggle again once MusicBrainz is reachable."
            )

    # The first refresh seeds the entire back-catalogue from MusicBrainz, which
    # for a prolific artist (Radiohead, looking at you) can take a couple of
    # minutes. Run it in the background so the request returns immediately; the UI
    # already polls refresh_state/refresh_stage to show progress. Running it inline
    # would leave the HTTP request hanging long enough to trip client/proxy timeouts.
    spawn_daemon_thread(refresh_watched_artist, artist_id)
    response = {
        "id": artist_id,
        "name": body.name,
        "mbid": body.mbid,
        "from_date": body.from_date,
        "auto_add_albums": (bool(body.auto_add_albums) or already_auto_adding) and auto_add_albums_warning is None,
        "watch_singles": bool(body.watch_singles),
        "already_watched": already_watched,
        "refresh_state": "running",
        "seeding": True,
    }
    if auto_add_albums_warning:
        response["warning"] = auto_add_albums_warning
    return response


@app.get("/api/watched-artists")
def list_watched_artists(http_request: Request):
    """List all watched artists with track counts."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        rows = conn.execute(
            f"""SELECT wa.*,
                (SELECT COUNT(*) FROM watched_artist_tracks wat WHERE wat.artist_id = wa.id) as tracked_count,
                (SELECT COUNT(*) FROM watched_artist_tracks wat WHERE wat.artist_id = wa.id AND wat.downloaded_at IS NOT NULL) as downloaded_count
               FROM watched_artists wa
               WHERE {_scope_frag}
               ORDER BY wa.name COLLATE NOCASE""",
            _scope_params
        ).fetchall()
    return {"artists": [_expose_conversion_intent(dict(r)) for r in rows]}


@app.get("/api/upgrades/candidates")
def list_upgrade_candidates(http_request: Request, page: int = 1, per_page: int = 10):
    """Paginated list of library files sitting below target quality (Watched Upgrades).

    Read-only: each row carries any cached search result, but searching is triggered
    per-row by the client (see /search below). Hidden from peons.
    """
    if _is_peon(http_request):
        raise HTTPException(status_code=403, detail="Not available for this account")
    if not get_setting_bool("enable_track_upgrades", False):
        return {"enabled": False, "items": [], "total": 0, "page": 1, "pages": 0}
    data = get_candidates_page(http_request.state.user_id, page=page, per_page=per_page)
    data["enabled"] = True
    return data


@app.post("/api/upgrades/candidates/{candidate_id}/search")
def search_upgrade_candidate(candidate_id: int, http_request: Request, force: bool = False):
    """Search for a better copy of one candidate (network). Client calls this per row, ~1/s."""
    if _is_peon(http_request):
        raise HTTPException(status_code=403, detail="Not available for this account")
    if not get_setting_bool("enable_track_upgrades", False):
        raise HTTPException(status_code=400, detail="Track upgrades are disabled")
    result = search_candidate(http_request.state.user_id, candidate_id, force=force)
    if result is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return result


@app.post("/api/upgrades/candidates/{candidate_id}/dismiss")
def dismiss_upgrade_candidate(candidate_id: int, http_request: Request):
    """Stop suggesting an upgrade for this file (until the file itself changes)."""
    if _is_peon(http_request):
        raise HTTPException(status_code=403, detail="Not available for this account")
    if not dismiss_candidate(http_request.state.user_id, candidate_id):
        raise HTTPException(status_code=404, detail="Candidate not found")
    return {"status": "ok"}


@app.post("/api/upgrades/candidates/{candidate_id}/upgrade")
def upgrade_candidate(candidate_id: int, http_request: Request, force: bool = False):
    """Download the proposed copy, verify it, and swap it in if it passes every check.

    Synchronous: it downloads and fingerprints, so it can take a little while. The old
    file goes to quarantine (manual purge), never deleted. Hidden from peons.
    force=true skips the same-recording gates (still quarantines the old file).
    """
    if _is_peon(http_request):
        raise HTTPException(status_code=403, detail="Not available for this account")
    if not get_setting_bool("enable_track_upgrades", False):
        raise HTTPException(status_code=400, detail="Track upgrades are disabled")
    result = perform_upgrade(http_request.state.user_id, candidate_id, force=force)
    if result.get("status") == "error" and result.get("reason") == "Candidate not found":
        raise HTTPException(status_code=404, detail="Candidate not found")
    return result


@app.post("/api/upgrades/upgrade-all")
def upgrade_all(http_request: Request):
    """Best-effort upgrade of every below-target candidate that has a proposal.

    Runs in the background (downloads take time); the UI refreshes afterwards.
    """
    if _is_peon(http_request):
        raise HTTPException(status_code=403, detail="Not available for this account")
    if not get_setting_bool("enable_track_upgrades", False):
        raise HTTPException(status_code=400, detail="Track upgrades are disabled")
    user_id = http_request.state.user_id
    # Mark it live here, not in the thread, so the UI's first poll can't slip
    # into the gap and conclude the run already finished.
    mark_upgrade_all_starting(user_id)
    spawn_daemon_thread(perform_upgrade_all, user_id)
    return {"status": "started"}


@app.get("/api/upgrades/upgrade-all/status")
def upgrade_all_status(http_request: Request):
    """Progress of this user's background Upgrade All run, so the UI can poll it
    and refresh the list itself instead of asking the user to hit reload."""
    if _is_peon(http_request):
        raise HTTPException(status_code=403, detail="Not available for this account")
    return get_upgrade_all_progress(http_request.state.user_id)


@app.post("/api/upgrades/rescan")
def rescan_upgrades(http_request: Request):
    """Kick off an immediate library scan (admin only). Cheap and network-free."""
    if not http_request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    if not get_setting_bool("enable_track_upgrades", False):
        raise HTTPException(status_code=400, detail="Track upgrades are disabled")
    return {"status": "ok", "totals": run_scan_all()}


@app.get("/api/upgrades/quarantine")
def get_quarantine(http_request: Request):
    """List files a Track Upgrade swap displaced. Admins also see files sitting
    in the quarantine folder with no tracking row (predates this feature, or
    dropped in by hand); those are delete-only since their original library
    location was never recorded.
    """
    if _is_peon(http_request):
        raise HTTPException(status_code=403, detail="Not available for this account")
    items = list_quarantine(http_request.state.user_id, is_admin=http_request.state.is_admin)
    return {"items": items}


@app.delete("/api/upgrades/quarantine/orphaned")
def delete_orphaned_quarantine_entry(filename: str, http_request: Request):
    """Delete a quarantine-folder file with no tracking row (admin only).

    Registered before the /{entry_id} routes below: Starlette matches path
    routes in registration order, and "orphaned" is a syntactically valid
    (if not a numeric) value for {entry_id}, so this has to come first or it's
    unreachable, shadowed by the generic route.
    """
    if not http_request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    result = delete_orphaned_quarantine_file(filename)
    if result.get("status") == "error":
        raise HTTPException(status_code=404, detail=result.get("reason") or "File not found")
    return result


@app.post("/api/upgrades/quarantine/{entry_id}/restore")
def restore_quarantine_entry(entry_id: int, http_request: Request):
    """Undo one upgrade: put the quarantined file back where it lived.

    Whatever the upgrade put there instead is itself quarantined rather than
    deleted, so this can be undone too if it turns out to be the wrong call.
    """
    if _is_peon(http_request):
        raise HTTPException(status_code=403, detail="Not available for this account")
    result = restore_from_quarantine(
        http_request.state.user_id, entry_id, is_admin=http_request.state.is_admin
    )
    if result.get("status") == "error" and result.get("reason") == "Quarantine entry not found":
        raise HTTPException(status_code=404, detail="Quarantine entry not found")
    return result


@app.delete("/api/upgrades/quarantine/{entry_id}")
def delete_quarantine_entry_route(entry_id: int, http_request: Request):
    """Permanently discard a quarantined file. No undo past this point."""
    if _is_peon(http_request):
        raise HTTPException(status_code=403, detail="Not available for this account")
    result = delete_quarantine_entry(
        http_request.state.user_id, entry_id, is_admin=http_request.state.is_admin
    )
    if result.get("status") == "error" and result.get("reason") == "Quarantine entry not found":
        raise HTTPException(status_code=404, detail="Quarantine entry not found")
    return result


def _audio_audit_filters(
    classification: str = "",
    container: str = "",
    codec: str = "",
    source: str = "",
    effective_quality: str = "",
    lossless_only: str = "",
) -> dict:
    if lossless_only not in ("", "stored", "recorded"):
        raise HTTPException(
            status_code=400,
            detail="lossless_only must be 'stored' or 'recorded'",
        )
    return {
        "classification": classification,
        "container": container,
        "codec": codec,
        "source": source,
        "effective_quality": effective_quality,
        "lossless_only": lossless_only,
    }


@app.get("/api/audio-audit")
def audio_audit_status(http_request: Request):
    """Current trusted snapshot plus any in-progress read-only scan."""
    if _is_peon(http_request):
        raise HTTPException(status_code=403, detail="Not available for this account")
    return get_audit_status(http_request.state.user_id)


@app.post("/api/audio-audit/scans")
def start_audio_audit(http_request: Request):
    """Start a full-library provenance scan without changing any audio files."""
    if _is_peon(http_request):
        raise HTTPException(status_code=403, detail="Not available for this account")
    return start_audit_scan(http_request.state.user_id)


@app.get("/api/audio-audit/files")
def audio_audit_files(
    http_request: Request,
    page: int = 1,
    per_page: int = 25,
    classification: str = "",
    container: str = "",
    codec: str = "",
    source: str = "",
    effective_quality: str = "",
    lossless_only: str = "",
):
    """Paginated evidence rows from the last complete audit snapshot."""
    if _is_peon(http_request):
        raise HTTPException(status_code=403, detail="Not available for this account")
    filters = _audio_audit_filters(
        classification,
        container,
        codec,
        source,
        effective_quality,
        lossless_only,
    )
    return get_audit_files(
        http_request.state.user_id,
        filters=filters,
        page=page,
        per_page=per_page,
    )


@app.get("/api/audio-audit/export")
def export_audio_audit(
    http_request: Request,
    format: str = "csv",
    classification: str = "",
    container: str = "",
    codec: str = "",
    source: str = "",
    effective_quality: str = "",
    lossless_only: str = "",
):
    """Download the filtered snapshot as an explicitly dry-run CSV or JSON report."""
    if _is_peon(http_request):
        raise HTTPException(status_code=403, detail="Not available for this account")
    export_format = format.strip().lower()
    if export_format not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="format must be 'csv' or 'json'")
    filters = _audio_audit_filters(
        classification,
        container,
        codec,
        source,
        effective_quality,
        lossless_only,
    )
    report = get_audit_export(http_request.state.user_id, filters)
    scan = report["metadata"].get("scan") or {}
    scan_id = scan.get("id") or "no-snapshot"
    headers = {
        "Content-Disposition": (
            f'attachment; filename="musicgrabber-audio-audit-{scan_id}.{export_format}"'
        )
    }

    if export_format == "json":
        body = json.dumps(report, ensure_ascii=False, indent=2, default=str)
        return StreamingResponse(
            iter([body]),
            media_type="application/json; charset=utf-8",
            headers=headers,
        )

    output = io.StringIO()
    fieldnames = [
        "criteria_version",
        "read_only_notice",
        "scan_id",
        "scan_completed_at",
        "path",
        "artist",
        "title",
        "classification",
        "classification_label",
        "stored_quality",
        "effective_quality",
        "container",
        "codec",
        "bitrate_kbps",
        "sample_rate_hz",
        "bits_per_sample",
        "source",
        "source_quality",
        "source_codec",
        "source_bitrate_kbps",
        "evidence",
        "caveats",
        "read_error",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for item in report["files"]:
        writer.writerow({
            "criteria_version": report["metadata"]["criteria_version"],
            "read_only_notice": report["metadata"]["read_only_notice"],
            "scan_id": scan_id,
            "scan_completed_at": scan.get("completed_at"),
            "path": item.get("path"),
            "artist": item.get("artist"),
            "title": item.get("title"),
            "classification": item.get("classification"),
            "classification_label": item.get("classification_label"),
            "stored_quality": item.get("stored_quality"),
            "effective_quality": item.get("effective_quality"),
            "container": item.get("container"),
            "codec": item.get("codec"),
            "bitrate_kbps": item.get("bitrate_kbps"),
            "sample_rate_hz": item.get("sample_rate_hz"),
            "bits_per_sample": item.get("bits_per_sample"),
            "source": item.get("source"),
            "source_quality": item.get("source_quality"),
            "source_codec": item.get("source_codec"),
            "source_bitrate_kbps": item.get("source_bitrate_kbps"),
            "evidence": json.dumps(item.get("evidence") or [], ensure_ascii=False),
            "caveats": json.dumps(item.get("caveats") or [], ensure_ascii=False),
            "read_error": item.get("read_error"),
        })
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )


@app.put("/api/watched-artists/{artist_id}")
def update_watched_artist(artist_id: str, request: WatchedArtistUpdate, http_request: Request):
    """Update a watched artist's settings."""
    if _is_peon(http_request):
        request.convert_audio = None  # Peons cannot change conversion setting
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    _scope_frag, _scope_params = _user_scope(user_id, is_admin)

    updates: list[str] = []
    params: list = []
    if request.enabled is not None:
        updates.append("enabled = ?"); params.append(int(request.enabled))
    if request.refresh_interval_hours is not None:
        updates.append("refresh_interval_hours = ?"); params.append(request.refresh_interval_hours)
    if request.convert_audio is not None:
        updates.append("convert_audio = ?"); params.append(int(request.convert_audio))
    if request.from_date is not None:
        updates.append("from_date = ?"); params.append(request.from_date)

    # auto_add_albums: switching it ON seeds the artist's current albums as
    # already-seen BEFORE the flag itself is persisted below, so nothing reading
    # this flag (the scheduler included) can ever observe "on" without the seed
    # rows already in place. That is the anti-avalanche guarantee; flipping it
    # off, or leaving an already-on artist on, needs no reseed.
    if request.auto_add_albums is not None:
        with db_conn() as conn:
            row = conn.execute(
                f"SELECT auto_add_albums, mbid FROM watched_artists WHERE id = ? AND {_scope_frag}",
                (artist_id, *_scope_params)
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Artist not found")
        if request.auto_add_albums and not row[0]:
            try:
                seed_artist_albums(artist_id, row[1], user_id=user_id)
            except MusicBrainzUnavailable as e:
                raise HTTPException(
                    status_code=503,
                    detail=f"Could not seed the current album list: {e}. Try again in a moment.",
                )
        updates.append("auto_add_albums = ?"); params.append(int(request.auto_add_albums))

    if request.watch_singles is not None:
        updates.append("watch_singles = ?"); params.append(int(request.watch_singles))

    # Both switches off leaves a follow that follows nothing, which would sit in
    # the list quietly doing sod all forever. Refuse it and point at Delete, which
    # is what the user actually meant.
    if request.watch_singles is not None or request.auto_add_albums is not None:
        with db_conn() as conn:
            current = conn.execute(
                f"SELECT watch_singles, auto_add_albums FROM watched_artists WHERE id = ? AND {_scope_frag}",
                (artist_id, *_scope_params)
            ).fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="Artist not found")
        next_singles = request.watch_singles if request.watch_singles is not None else bool(current[0])
        next_albums = request.auto_add_albums if request.auto_add_albums is not None else bool(current[1])
        if not next_singles and not next_albums:
            raise HTTPException(
                status_code=400,
                detail="An artist has to be followed for singles, albums or both. "
                       "To stop following them entirely, use Delete.",
            )

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    with db_conn() as conn:
        conn.execute(
            f"UPDATE watched_artists SET {', '.join(updates)} WHERE id = ? AND {_scope_frag}",
            (*params, artist_id, *_scope_params)
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        updated = conn.execute(
            f"SELECT * FROM watched_artists WHERE id = ? AND {_scope_frag}",
            (artist_id, *_scope_params)
        ).fetchone()
    if not updated:
        raise HTTPException(status_code=404, detail="Artist not found")
    wake_artist_scheduler()
    return dict(updated)


@app.delete("/api/watched-artists/{artist_id}")
def delete_watched_artist(artist_id: str, http_request: Request):
    """Remove a watched artist and all its tracked tracks."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        artist = conn.execute(
            f"SELECT name FROM watched_artists WHERE id = ? AND {_scope_frag}",
            (artist_id, *_scope_params)
        ).fetchone()
        if not artist:
            raise HTTPException(status_code=404, detail="Artist not found")
        conn.execute("DELETE FROM watched_artist_tracks WHERE artist_id = ?", (artist_id,))
        conn.execute("DELETE FROM watched_artists WHERE id = ?", (artist_id,))
        conn.commit()
    wake_artist_scheduler()
    return {"success": True, "message": f"Stopped watching {artist[0]}"}


@app.post("/api/watched-artists/{artist_id}/refresh")
def refresh_single_artist(artist_id: str, http_request: Request):
    """Manually trigger a refresh for one watched artist."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        row = conn.execute(
            f"SELECT id FROM watched_artists WHERE id = ? AND {_scope_frag}",
            (artist_id, *_scope_params)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Artist not found")
    # Background, same as create: a manual refresh of a prolific artist would
    # otherwise hang the request for minutes. The UI polls refresh_state.
    spawn_daemon_thread(refresh_watched_artist, artist_id)
    return {"artist_id": artist_id, "refresh_state": "running", "started": True}


@app.get("/api/watched-artists/{artist_id}/tracks")
def get_watched_artist_tracks(artist_id: str, http_request: Request, limit: int = 50, offset: int = 0):
    """Return a page of tracked singles for a watched artist with job status.

    Prolific artists run to hundreds of singles, so the list is paginated for
    display ('cycles'). limit<=0 means "everything" for callers that still want
    the lot. Always returns the total so the UI can render a pager.
    """
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    # Clamp to sane bounds; limit<=0 is the "give me all of it" escape hatch.
    paged = limit > 0
    limit = min(limit, 500) if paged else -1
    offset = max(offset, 0)
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        artist = conn.execute(
            f"SELECT name FROM watched_artists WHERE id = ? AND {_scope_frag}",
            (artist_id, *_scope_params)
        ).fetchone()
        if not artist:
            raise HTTPException(status_code=404, detail="Artist not found")
        total = conn.execute(
            "SELECT COUNT(*) FROM watched_artist_tracks WHERE artist_id = ?",
            (artist_id,)
        ).fetchone()[0]
        query = """SELECT wat.artist, wat.title, wat.release_date, wat.downloaded_at,
                      wat.resolved_path, wat.job_id, j.status as job_status, j.error as job_error
               FROM watched_artist_tracks wat
               LEFT JOIN jobs j ON wat.job_id = j.id
               WHERE wat.artist_id = ?
               ORDER BY wat.release_date DESC NULLS LAST, wat.title"""
        params: tuple = (artist_id,)
        if paged:
            query += " LIMIT ? OFFSET ?"
            params = (artist_id, limit, offset)
        tracks = conn.execute(query, params).fetchall()
    return {
        "artist": artist[0],
        "tracks": [dict(t) for t in tracks],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/watched-artists/{artist_id}/missing")
def get_missing_artist_tracks(artist_id: str, http_request: Request):
    """Return singles that haven't been downloaded and have no active job."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        artist = conn.execute(
            f"SELECT name FROM watched_artists WHERE id = ? AND {_scope_frag}",
            (artist_id, *_scope_params)
        ).fetchone()
        if not artist:
            raise HTTPException(status_code=404, detail="Artist not found")
        tracks = conn.execute(
            """SELECT wat.artist, wat.title, wat.release_date, j.status as job_status, j.error as job_error
               FROM watched_artist_tracks wat
               LEFT JOIN jobs j ON wat.job_id = j.id
               WHERE wat.artist_id = ?
                 AND wat.downloaded_at IS NULL
                 AND (j.status IS NULL OR j.status NOT IN ('queued', 'downloading'))
               ORDER BY wat.release_date DESC NULLS LAST, wat.title""",
            (artist_id,)
        ).fetchall()
    return {"artist": artist[0], "tracks": [dict(t) for t in tracks]}


@app.get("/api/watched-artists/{artist_id}/albums")
def get_watched_artist_albums(artist_id: str, http_request: Request):
    """Return known albums with both historical and current display state.

    ``status`` retains the event that introduced the row (seen/queued/failed),
    while ``display_status`` combines the linked import with a cheap on-disk
    count. Populated by seeding (on enabling
    auto_add_albums) and by refreshes once it's on; empty for artists that have
    never had the toggle switched on."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        artist = conn.execute(
            f"SELECT name FROM watched_artists WHERE id = ? AND {_scope_frag}",
            (artist_id, *_scope_params)
        ).fetchone()
        if not artist:
            raise HTTPException(status_code=404, detail="Artist not found")
        rows = conn.execute(
            """SELECT waa.title, waa.year, waa.release_mbid,
                      waa.release_group_mbid, waa.status, waa.queued_at,
                      waa.import_id, waa.attempt_count, waa.last_attempt_at,
                      waa.last_error, bi.status AS import_status,
                      bi.album_total_tracks AS expected_track_count
               FROM watched_artist_albums waa
               LEFT JOIN bulk_imports bi ON bi.id = waa.import_id
               WHERE waa.artist_id = ?
               ORDER BY year DESC NULLS LAST, title""",
            (artist_id,)
        ).fetchall()

    artist_name = artist["name"]
    out = []
    for r in rows:
        d = dict(r)
        # Cheap filesystem count only; no MusicBrainz call per row. The import's
        # album_total_tracks is the expected full release size, even when that
        # particular import only had to fetch a missing subset.
        audio_count = album_audio_file_count(
            artist_name, d["title"], user_id=user_id
        )
        d.update(watched_album_display_state(
            d.get("status"),
            d.get("import_status"),
            audio_count,
            d.get("expected_track_count"),
        ))
        if d.get("status") == "failed" and d.get("last_error"):
            count = int(d.get("attempt_count") or 0)
            d["status_detail"] = (
                f"Queue attempt {count} failed: {d['last_error']}. "
                "It will retry on the next artist check."
            )
        out.append(d)
    return {"artist": artist_name, "albums": out}


@app.post("/api/watched-artists/{artist_id}/retry-track")
def retry_missing_artist_track(artist_id: str, request: RetryMissingTrackRequest, http_request: Request):
    """Retry downloading a specific missing single."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        artist = conn.execute(
            f"SELECT name, convert_audio FROM watched_artists WHERE id = ? AND {_scope_frag}",
            (artist_id, *_scope_params)
        ).fetchone()
        if not artist:
            raise HTTPException(status_code=404, detail="Artist not found")
    import_id = start_bulk_import_for_tracks(
        [(request.artist, request.title)],
        convert_audio=bool(artist[1]),
        watch_artist_id=artist_id,
        user_id=user_id,
    )
    return {"success": True, "import_id": import_id}


@app.post("/api/watched-artists/{artist_id}/retry-all-missing")
def retry_all_missing_artist_tracks(artist_id: str, http_request: Request):
    """Queue all undownloaded singles for this artist in one bulk import."""
    user_id = http_request.state.user_id
    is_admin = http_request.state.is_admin
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        _scope_frag, _scope_params = _user_scope(user_id, is_admin)
        artist = conn.execute(
            f"SELECT name, convert_audio FROM watched_artists WHERE id = ? AND {_scope_frag}",
            (artist_id, *_scope_params)
        ).fetchone()
        if not artist:
            raise HTTPException(status_code=404, detail="Artist not found")
        tracks = conn.execute(
            """SELECT artist, title FROM watched_artist_tracks
               WHERE artist_id = ?
                 AND downloaded_at IS NULL
                 AND (job_id IS NULL OR job_id NOT IN (
                     SELECT id FROM jobs WHERE status IN ('queued', 'downloading')
                 ))
               ORDER BY release_date DESC NULLS LAST, title""",
            (artist_id,)
        ).fetchall()
    if not tracks:
        return {"success": True, "queued": 0, "import_id": None}
    import_id = start_bulk_import_for_tracks(
        [(t["artist"], t["title"]) for t in tracks],
        convert_audio=bool(artist["convert_audio"]),
        watch_artist_id=artist_id,
        user_id=user_id,
    )
    return {"success": True, "queued": len(tracks), "import_id": import_id}


@app.post("/api/watched-artists/check-all")
def check_all_watched_artists(http_request: Request):
    """Trigger a refresh for all watched artists that are due."""
    if not http_request.state.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        artists = conn.execute("""
            SELECT id, name FROM watched_artists
            WHERE enabled = 1
            AND (last_checked IS NULL
                 OR datetime(last_checked, '+' || refresh_interval_hours || ' hours') < datetime('now'))
        """).fetchall()

    if not artists:
        return {"checked": 0, "message": "No artists due for refresh", "results": []}

    results = []
    for a in artists:
        result = refresh_watched_artist(a["id"])
        results.append(result)

    total_new = sum(r.get("new_tracks", 0) for r in results)
    return {
        "checked": len(results),
        "total_new_tracks": total_new,
        "results": results,
    }


# =============================================================================
# Album Download Routes
# =============================================================================

@app.get("/api/albums/search-artist")
def albums_search_artist(q: str):
    """Search MusicBrainz for an artist by name.

    Returns up to 5 candidates with mbid, name, disambiguation, and score.
    Returns 503 when MB is unreachable so the UI can show a Retry button
    instead of pretending the artist does not exist.
    """
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="Query parameter 'q' is required")
    try:
        results = search_artist_mbid(q.strip())
    except MusicBrainzUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"MusicBrainz unreachable: {exc}. Try again in a moment.")
    return {"artists": results}


@app.get("/api/albums/artist/{mbid}/albums")
def albums_list_artist_albums(mbid: str, http_request: Request, artist_name: str = ""):
    """Fetch studio albums for a MusicBrainz artist MBID.

    Returns [{title, year, release_mbid, release_group_mbid}, ...] sorted by year.
    Pass ?artist_name= and each album also carries `on_disk`, so a tickable album
    list can grey out the ones already sitting in the library rather than inviting
    you to download your own record collection back to yourself. That check is
    pure filesystem, no extra MusicBrainz traffic.
    503 on MB unavailable.
    """
    if not mbid or not mbid.strip():
        raise HTTPException(status_code=400, detail="Artist MBID is required")
    try:
        albums = fetch_artist_albums(mbid.strip())
    except MusicBrainzUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"MusicBrainz unreachable: {exc}. Try again in a moment.")

    artist_name = (artist_name or "").strip()
    if artist_name:
        user_id = http_request.state.user_id
        for album in albums:
            album["on_disk"] = album_on_disk(artist_name, album.get("title") or "", user_id=user_id)
    return {"albums": albums}


@app.get("/api/albums/release/{release_mbid}/tracks")
def albums_get_tracklist(release_mbid: str):
    """Fetch the tracklist for a MusicBrainz release MBID.

    Returns [{position, title}, ...] in track order.
    503 on MB unavailable.
    """
    if not release_mbid or not release_mbid.strip():
        raise HTTPException(status_code=400, detail="Release MBID is required")
    try:
        tracks = fetch_album_tracks(release_mbid.strip())
    except MusicBrainzUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"MusicBrainz unreachable: {exc}. Try again in a moment.")
    if not tracks:
        raise HTTPException(status_code=404, detail="No tracks found for this release")
    return {"tracks": tracks}


@app.get("/api/albums/release-group/{release_group_mbid}/tracks")
def albums_get_tracklist_by_group(release_group_mbid: str):
    """Resolve a release-group MBID to a representative release, then fetch its tracklist.

    The Albums browser lists release-groups now (see fetch_artist_albums), so
    it doesn't know which pressing to download until an album is actually
    opened. This does that resolution here, once, for the one album clicked,
    rather than for the whole list up front at one MusicBrainz request per
    second. Returns {release_mbid, tracks: [{position, title}, ...]} so the
    frontend can remember the resolved release for the rest of the session.
    503 on MB unavailable, 404 if nothing could be resolved.
    """
    if not release_group_mbid or not release_group_mbid.strip():
        raise HTTPException(status_code=400, detail="Release group MBID is required")
    try:
        release_mbid = resolve_album_release(release_group_mbid.strip())
    except MusicBrainzUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"MusicBrainz unreachable: {exc}. Try again in a moment.")
    if not release_mbid:
        raise HTTPException(status_code=404, detail="Could not find a release to download for this album")
    try:
        tracks = fetch_album_tracks(release_mbid)
    except MusicBrainzUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"MusicBrainz unreachable: {exc}. Try again in a moment.")
    if not tracks:
        raise HTTPException(status_code=404, detail="No tracks found for this release")
    return {"release_mbid": release_mbid, "tracks": tracks}


# _album_track_stem, album_track_status (formerly _album_track_status) and
# _normalise_album_match_text now live in albums.py, alongside the rest of the
# album pipeline (imported at the top of this file). _match_album_track below
# still leans on _normalise_album_match_text for its own fuzzy matching.


def _match_album_track(tracks: list[dict], candidate_title: str) -> dict | None:
    """Return a single best album track match for a candidate title."""
    cand = _normalise_album_match_text(candidate_title)
    if not cand:
        return None

    exact = [t for t in tracks if _normalise_album_match_text(t.get("title") or "") == cand]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None

    cand_tokens = set(cand.split())
    fuzzy = []
    for t in tracks:
        tn = _normalise_album_match_text(t.get("title") or "")
        if not tn:
            continue
        t_tokens = set(tn.split())
        overlap = len(cand_tokens & t_tokens)
        # Require ≥80% of the shorter token set to overlap; floor at 2 tokens to
        # avoid matching on coincidental single words in short titles like "My Love".
        min_overlap = max(2, round(0.8 * min(len(cand_tokens), len(t_tokens))))
        if overlap >= min_overlap:
            fuzzy.append((overlap, t))
    if len(fuzzy) == 1:
        return fuzzy[0][1]
    return None


@app.post("/api/albums/release/{release_mbid}/match-track")
def albums_match_track(release_mbid: str, body: dict):
    """Match a candidate song to a specific track on the selected album."""
    title = (body.get("title") or "").strip()
    candidate_artist = (body.get("artist") or "").strip()
    album_artist = (body.get("album_artist") or "").strip()
    if not release_mbid or not release_mbid.strip():
        raise HTTPException(status_code=400, detail="Release MBID is required")
    if not title:
        raise HTTPException(status_code=400, detail="Track title is required")

    if candidate_artist and album_artist:
        cand_artist_n = _normalise_album_match_text(candidate_artist)
        album_artist_n = _normalise_album_match_text(album_artist)
        if cand_artist_n and album_artist_n:
            cand_tokens = {t for t in cand_artist_n.split() if len(t) > 1}
            album_tokens = {t for t in album_artist_n.split() if len(t) > 1}
            if album_tokens and not album_tokens.issubset(cand_tokens):
                raise HTTPException(
                    status_code=422,
                    detail=f"Result artist '{candidate_artist}' does not match selected album artist '{album_artist}'",
                )

    tracks = fetch_album_tracks(release_mbid.strip())
    if not tracks:
        raise HTTPException(status_code=404, detail="No tracks found for this release")

    matched = _match_album_track(tracks, title)
    if not matched:
        raise HTTPException(status_code=422, detail=f"No album-track match found for '{title}'")

    try:
        track_number = int((matched.get("position") or "").strip())
    except Exception:
        track_number = None

    return {
        "matched": True,
        "track_title": (matched.get("title") or "").strip(),
        "track_number": track_number,
        "track_total": len(tracks),
    }


@app.get("/api/albums/release/{release_mbid}/missing")
def albums_missing_tracks(release_mbid: str, artist: str, album_title: str, request: Request):
    """Report which tracks are already present on disk for a selected album."""
    artist = (artist or "").strip()
    album_title = (album_title or "").strip()
    if not release_mbid or not release_mbid.strip():
        raise HTTPException(status_code=400, detail="Release MBID is required")
    if not artist or not album_title:
        raise HTTPException(status_code=400, detail="artist and album_title are required")

    tracks = fetch_album_tracks(release_mbid.strip())
    if not tracks:
        raise HTTPException(status_code=404, detail="No tracks found for this release")

    user_id = getattr(request.state, "user_id", None)
    status = album_track_status(artist, album_title, tracks, user_id=user_id)
    return {
        "album_dir": str(status["album_dir"]),
        "total_tracks": len(status["tracks"]),
        "tracks": status["tracks"],
        "existing_count": len(status["existing_tracks"]),
        "missing_count": len(status["missing_tracks"]),
        "existing_tracks": status["existing_tracks"],
        "missing_tracks": status["missing_tracks"],
        "has_existing_m3u": bool(status["m3u_files"]),
        "existing_m3u_files": status["m3u_files"],
    }


@app.get("/api/albums/dirs")
@prioritise_over_audio_audit
def albums_list_artists(request: Request):
    """List artist folders found under the Albums directory on disk."""
    user_id = getattr(request.state, "user_id", None)
    base = get_albums_dir(user_id=user_id)
    if not base.exists():
        return {"artists": []}
    artists = sorted(
        [d.name for d in base.iterdir() if d.is_dir() and not d.name.startswith(".")],
        key=str.casefold,
    )
    return {"artists": artists}


@app.get("/api/albums/dirs/{artist}")
@prioritise_over_audio_audit
def albums_list_albums(artist: str, request: Request):
    """List album folders within an artist directory."""
    if ".." in artist:
        raise HTTPException(status_code=400, detail="Invalid artist name")
    user_id = getattr(request.state, "user_id", None)
    base = get_albums_dir(user_id=user_id)
    artist_dir = base / sanitize_filename(artist)
    if not artist_dir.exists():
        raise HTTPException(status_code=404, detail="Artist directory not found")
    albums = sorted(
        [d.name for d in artist_dir.iterdir() if d.is_dir() and not d.name.startswith(".")],
        key=str.casefold,
    )
    return {"albums": albums}


@app.get("/api/albums/dirs/{artist}/{album}/info")
@prioritise_over_audio_audit
def albums_dir_info(artist: str, album: str, request: Request):
    """Read .albuminfo sidecar from an album directory and return MB tracklist."""
    if ".." in artist or ".." in album:
        raise HTTPException(status_code=400, detail="Invalid path")
    user_id = getattr(request.state, "user_id", None)
    base = get_albums_dir(user_id=user_id)
    album_dir = base / sanitize_filename(artist) / sanitize_filename(album)
    albuminfo_path = album_dir / ".albuminfo"
    if not albuminfo_path.exists():
        return {"found": False}
    try:
        data = json.loads(albuminfo_path.read_text(encoding="utf-8"))
        release_mbid = (data.get("release_mbid") or "").strip()
        tracks = fetch_album_tracks(release_mbid) if release_mbid else []
        return {
            "found": True,
            "artist": data.get("artist", ""),
            "album": data.get("album", ""),
            "release_mbid": release_mbid,
            "tracks": tracks,
        }
    except Exception:
        return {"found": False}


@app.post("/api/albums/resolve-url")
def albums_resolve_url(body: PlaylistFetchRequest, http_request: Request):
    """Turn a MusicBrainz release or release-group URL into album download fields.

    The MBID is right there in the URL, so there is nothing to scrape and nothing
    to guess: we resolve it to artist/title and hand back exactly what
    /api/albums/download wants. Release-group URLs (the ones MusicBrainz shows
    you when you search) are resolved to a representative official release.
    """
    url = (body.url or "").strip()
    parsed = parse_musicbrainz_release_url(url)
    if not parsed:
        raise HTTPException(
            status_code=400,
            detail="Not a MusicBrainz release URL. Expected musicbrainz.org/release/<mbid> or /release-group/<mbid>.",
        )
    kind, mbid = parsed
    try:
        summary = fetch_release_summary(kind, mbid)
    except MusicBrainzUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not summary:
        raise HTTPException(status_code=404, detail="MusicBrainz has no release at that URL")
    return summary


@app.get("/api/albums/search-release")
def albums_search_release(q: str, artist: str | None = None):
    """Search MusicBrainz for an album by name, with or without an artist.

    Deliberately does not require an artist first: soundtracks, compilations
    and Various Artists releases are first-class citizens here, not awkward
    edge cases. Somebody typing "Trainspotting" should not have to nominate
    which of its thirty-odd contributors counts as the owner.

    release_mbid comes back null on purpose. Resolving a representative
    release costs one rate-limited MusicBrainz request per candidate, and
    nobody needs that for the nine albums they are about to ignore; call
    /api/albums/resolve-release-group once the user has actually picked one.
    """
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="Query parameter 'q' is required")
    try:
        releases = search_release_groups(q.strip(), artist=(artist or "").strip() or None,
                                         resolve_releases=False)
    except MusicBrainzUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"MusicBrainz unreachable: {exc}. Try again in a moment.")
    return {"releases": releases}


@app.post("/api/albums/resolve-release-group")
def albums_resolve_release_group(body: ReleaseGroupResolveRequest):
    """Turn a chosen release-group into concrete album download fields.

    The other half of the lazy search above: one request, for the one album
    the user actually wants, handing back exactly what /api/albums/download
    expects.
    """
    rg_mbid = (body.release_group_mbid or "").strip()
    if not rg_mbid:
        raise HTTPException(status_code=400, detail="release_group_mbid is required")
    try:
        summary = fetch_release_summary("release-group", rg_mbid)
    except MusicBrainzUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"MusicBrainz unreachable: {exc}. Try again in a moment.")
    if not summary:
        raise HTTPException(status_code=404, detail="MusicBrainz has no usable release in that release group")
    return summary


@app.post("/api/albums/resolve-album-url")
def albums_resolve_album_url(body: PlaylistFetchRequest):
    """Work out whether a pasted URL is an ALBUM on a supported service, and what it is.

    Spotify, Apple Music, YouTube Music, Beatport, Tidal and Monochrome.
    Returns recognised=False for anything else, including playlist and track
    URLs from those same services, so the caller can fall through to the
    ordinary playlist path without a fuss.

    The confidence gate matters here: `confident` false means the caller must
    ask the user which candidate they meant. Guessing wrong does not cost one
    duff track, it costs a whole duff album.
    """
    url = (body.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    try:
        return resolve_album_url(url)
    except MusicBrainzUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"MusicBrainz unreachable: {exc}. Try again in a moment.")


@app.post("/api/albums/download")
def albums_download(body: AlbumDownloadRequest, http_request: Request):
    """Queue a full album for download.

    Fetches tracklist from MusicBrainz, creates a bulk import job routed to
    Albums/Artist/Album/ instead of the normal Singles layout.
    Returns {import_id} for polling via /api/bulk-import/{id}/status.

    The actual pipeline (tracklist fetch, existing/missing diff, .albuminfo
    sidecar, in-flight guard, queueing) lives in albums.py so watched_artists.py
    can reuse it for auto-added albums without importing this whole file.
    """
    _enforce_peon_format(http_request, body)
    user_id = getattr(http_request.state, "user_id", None)

    artist = body.artist.strip()
    album_title = body.album_title.strip()
    release_mbid = body.release_mbid.strip()

    if not artist or not album_title or not release_mbid:
        raise HTTPException(status_code=400, detail="artist, album_title, and release_mbid are required")

    try:
        result = queue_album_download(
            artist,
            album_title,
            release_mbid,
            make_m3u=body.make_m3u,
            m3u_name=(body.m3u_name or "").strip(),
            convert_audio=body.convert_audio,
            user_id=user_id,
        )
    except AlbumTracklistUnavailable as e:
        raise HTTPException(status_code=404, detail=str(e))

    # Picked by hand from a followed artist's album list? Record it as dealt with,
    # otherwise the auto-add diff spots it as a shiny new arrival on the next
    # refresh and queues the whole thing all over again.
    watch_artist_id = (body.watch_artist_id or "").strip()
    group_mbid = (body.release_group_mbid or "").strip() or release_mbid
    if watch_artist_id:
        try:
            with db_conn() as conn:
                conn.execute(
                    """INSERT INTO watched_artist_albums
                       (artist_id, release_group_mbid, release_mbid, title, year, status,
                        queued_at, import_id)
                       VALUES (?, ?, ?, ?, '', 'queued', datetime('now'), ?)
                       ON CONFLICT(artist_id, release_group_mbid) DO UPDATE SET
                           status = 'queued',
                           queued_at = datetime('now'),
                           import_id = excluded.import_id""",
                    (watch_artist_id, group_mbid, release_mbid, album_title,
                     result.get("import_id"))
                )
                conn.commit()
        except Exception as e:
            # Bookkeeping only. The album is already queued and that is the bit
            # the user cares about; worst case auto-add offers it again later.
            print(f"Could not record hand-picked album for artist {watch_artist_id}: {e}")

    return result


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("LISTEN_ADDR", "0.0.0.0")
    port = int(os.getenv("LISTEN_PORT", "8080"))
    uvicorn.run(app, host=host, port=port)
