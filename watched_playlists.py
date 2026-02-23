"""
MusicGrabber - Watched Playlists

Platform detection, track fetching, playlist refresh, and background scheduler.
"""

import json
import random
import re
import sqlite3
import subprocess
import time

from fastapi import HTTPException

from constants import (
    TIMEOUT_YTDLP_PLAYLIST, TIMEOUT_HTTP_SPOTIFY,
    WATCHED_PLAYLIST_CHECK_HOURS,
)
from db import db_conn
from bulk_import import start_bulk_import_for_tracks
from models import SpotifyPlaylistRequest
from amazon import fetch_amazon_playlist
from spotify import fetch_spotify_playlist_via_browser
from utils import extract_artist_title, hash_track, spawn_daemon_thread
from youtube import _ytdlp_base_args

import httpx


def detect_playlist_platform(url: str) -> tuple[str, str]:
    """Detect platform and extract ID from playlist URL

    Returns (platform, id) or raises HTTPException if invalid
    """
    # Spotify playlist
    spotify_playlist = re.match(r'https?://open\.spotify\.com/playlist/([a-zA-Z0-9]+)', url)
    if spotify_playlist:
        return "spotify", spotify_playlist.group(1)

    # Spotify album
    spotify_album = re.match(r'https?://open\.spotify\.com/album/([a-zA-Z0-9]+)', url)
    if spotify_album:
        return "spotify", spotify_album.group(1)

    # YouTube playlist
    youtube_playlist = re.match(r'https?://(www\.)?(youtube\.com|youtu\.be)/playlist\?list=([a-zA-Z0-9_-]+)', url)
    if youtube_playlist:
        return "youtube", youtube_playlist.group(3)

    # Amazon Music playlist (user or curated, any regional TLD)
    amazon_playlist = re.match(r'https?://music\.amazon\.[a-z.]+/(user-playlists|playlists)/\S+', url)
    if amazon_playlist:
        return "amazon", url  # Full URL needed — no extractable ID

    raise HTTPException(
        status_code=400,
        detail="Invalid playlist URL. Supported: Spotify playlists/albums, YouTube playlists, Amazon Music playlists."
    )


def _fetch_spotify_playlist_embed(url: str) -> dict:
    """Fetch Spotify playlist tracks via embed endpoint.
    This is the fast path that works for playlists with <100 tracks.
    """
    # Extract ID and type from URL
    playlist_match = re.match(r'https?://open\.spotify\.com/playlist/([a-zA-Z0-9]+)', url)
    album_match = re.match(r'https?://open\.spotify\.com/album/([a-zA-Z0-9]+)', url)

    if playlist_match:
        spotify_id = playlist_match.group(1)
        spotify_type = "playlist"
    elif album_match:
        spotify_id = album_match.group(1)
        spotify_type = "album"
    else:
        raise HTTPException(status_code=400, detail="Invalid Spotify URL. Expected playlist or album URL.")

    # Fetch the embed page. Spotify's public playlist API is gone, so we scrape the
    # embed HTML which includes a predictable JSON-in-HTML "title"/"subtitle" pattern.
    # If this breaks, inspect the embed HTML for renamed fields or a new data blob.
    try:
        with httpx.Client(timeout=TIMEOUT_HTTP_SPOTIFY, follow_redirects=True) as client:
            response = client.get(
                f"https://open.spotify.com/embed/{spotify_type}/{spotify_id}",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                }
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail=f"{spotify_type.title()} not found or is private")
        raise HTTPException(status_code=502, detail=f"Failed to fetch {spotify_type}: {e}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Failed to connect to Spotify: {e}")

    html_content = response.text

    # Extract name
    playlist_name = f"Spotify {spotify_type.title()}"
    title_matches = re.findall(r'"title":"([^"]+)"', html_content)
    if title_matches:
        playlist_name = title_matches[0]

    # Extract tracks using title/subtitle pattern
    tracks = []
    titles = re.findall(r'"title":"([^"]+)"', html_content)
    subtitles = re.findall(r'"subtitle":"([^"]+)"', html_content)

    if len(titles) > 1 and len(subtitles) > 1:
        track_titles = titles[1:]  # Skip playlist name
        track_artists = subtitles[1:]  # Skip "Spotify"

        for title, artist in zip(track_titles, track_artists):
            try:
                title = json.loads(f'"{title}"')
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            try:
                artist = json.loads(f'"{artist}"')
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            tracks.append(f"{artist} - {title}")

    if not tracks:
        raise HTTPException(
            status_code=422,
            detail=f"Could not extract tracks from {spotify_type}. It may be empty or Spotify's page structure may have changed."
        )

    # If near the embed limit, try headless browser for full list
    if len(tracks) >= 95:
        print(f"Spotify embed returned {len(tracks)} tracks (near limit), trying headless browser...")
        try:
            browser_result = fetch_spotify_playlist_via_browser(spotify_id, spotify_type)
            if browser_result["count"] > len(tracks):
                print(f"Headless browser returned {browser_result['count']} tracks (embed had {len(tracks)})")
                return browser_result
        except HTTPException as e:
            print(f"Headless browser failed ({e.detail}), using embed results")
        except Exception as e:
            print(f"Headless browser error: {e}, using embed results")

        return {
            "tracks": tracks,
            "playlist_name": playlist_name,
            "count": len(tracks),
            "warning": f"Playlist may be truncated at {len(tracks)} tracks. Full extraction failed."
        }

    return {
        "tracks": tracks,
        "playlist_name": playlist_name,
        "count": len(tracks)
    }


def fetch_playlist_tracks(url: str, platform: str) -> tuple[list[tuple[str, str]], str]:
    """Fetch tracks from a playlist URL

    Returns (list of (artist, title) tuples, playlist_name)
    """
    if platform == "spotify":
        result = _fetch_spotify_playlist_embed(url)

        # Parse "Artist - Title" format back to tuples
        tracks = []
        for track_str in result["tracks"]:
            if " - " in track_str:
                artist, title = track_str.split(" - ", 1)
                tracks.append((artist.strip(), title.strip()))
            else:
                tracks.append(("Unknown", track_str.strip()))

        return tracks, result["playlist_name"]

    elif platform == "youtube":
        # Use yt-dlp to get playlist info
        m = re.search(r'list=([a-zA-Z0-9_-]+)', url)
        if not m:
            raise HTTPException(status_code=400, detail="Invalid YouTube playlist URL: no list= parameter found")
        playlist_id = m.group(1)

        info_cmd = [
            "yt-dlp",
            *_ytdlp_base_args(),
            "--dump-json",
            "--flat-playlist",
            "--no-warnings",
            f"https://www.youtube.com/playlist?list={playlist_id}"
        ]

        try:
            result = subprocess.run(info_cmd, capture_output=True, text=True, timeout=TIMEOUT_YTDLP_PLAYLIST)
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="Timeout fetching YouTube playlist")

        if result.returncode != 0:
            raise HTTPException(status_code=502, detail="Failed to fetch YouTube playlist")

        tracks = []
        playlist_name = "YouTube Playlist"

        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            try:
                data = json.loads(line)
                # First entry often has playlist title
                if data.get("playlist_title") and playlist_name == "YouTube Playlist":
                    playlist_name = data["playlist_title"]

                if data.get("id"):
                    title = data.get("title", "Unknown")
                    channel = data.get("channel", data.get("uploader", "Unknown"))
                    artist, clean_title_val = extract_artist_title(title, channel)
                    tracks.append((artist, clean_title_val))
            except json.JSONDecodeError:
                continue

        if not tracks:
            raise HTTPException(status_code=422, detail="No tracks found in YouTube playlist")

        return tracks, playlist_name

    elif platform == "amazon":
        result = fetch_amazon_playlist(url)

        tracks = []
        for track_str in result["tracks"]:
            if " - " in track_str:
                artist, title = track_str.split(" - ", 1)
                tracks.append((artist.strip(), title.strip()))
            else:
                tracks.append(("Unknown", track_str.strip()))

        return tracks, result["playlist_name"]

    raise HTTPException(status_code=400, detail=f"Unsupported platform: {platform}")


def refresh_watched_playlist(playlist_id: str) -> dict:
    """Fetch playlist and queue any new tracks for download

    Returns dict with refresh results
    """
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        playlist = conn.execute(
            "SELECT * FROM watched_playlists WHERE id = ?", (playlist_id,)
        ).fetchone()

        if not playlist:
            return {"error": "Playlist not found", "playlist_id": playlist_id}

        playlist = dict(playlist)

        try:
            # Fetch current tracks
            tracks, _ = fetch_playlist_tracks(playlist["url"], playlist["platform"])

            # Load existing track state (including job status)
            track_rows = conn.execute(
                """SELECT wpt.track_hash, wpt.downloaded_at, wpt.job_id, j.status as job_status
                   FROM watched_playlist_tracks wpt
                   LEFT JOIN jobs j ON wpt.job_id = j.id
                   WHERE wpt.playlist_id = ?""",
                (playlist_id,)
            ).fetchall()
            tracked = {row["track_hash"]: row for row in track_rows}

            new_tracks = []
            missing_tracks = []
            for artist, title in tracks:
                track_hash = hash_track(artist, title)
                existing = tracked.get(track_hash)
                if not existing:
                    new_tracks.append((artist, title, track_hash))
                    continue

                if existing["downloaded_at"]:
                    continue

                job_status = existing["job_status"]
                if job_status == "completed":
                    conn.execute(
                        "UPDATE watched_playlist_tracks SET downloaded_at = datetime('now') WHERE playlist_id = ? AND track_hash = ?",
                        (playlist_id, track_hash)
                    )
                    continue

                if job_status in ("queued", "downloading"):
                    continue

                missing_tracks.append((artist, title, track_hash))

            # Insert any new tracks so they are tracked before download
            for artist, title, track_hash in new_tracks:
                conn.execute("""
                    INSERT INTO watched_playlist_tracks
                    (playlist_id, track_hash, artist, title)
                    VALUES (?, ?, ?, ?)
                """, (playlist_id, track_hash, artist, title))

            tracks_to_import = [(artist, title) for artist, title, _ in new_tracks + missing_tracks]
            import_id = None
            if tracks_to_import:
                import_id = start_bulk_import_for_tracks(
                    tracks_to_import,
                    bool(playlist["convert_to_flac"]),
                    watch_playlist_id=playlist_id
                )

            # Update playlist metadata
            conn.execute("""
                UPDATE watched_playlists
                SET last_checked = datetime('now'), last_track_count = ?
                WHERE id = ?
            """, (len(tracks), playlist_id))

            conn.commit()

            queued_count = len(tracks_to_import)
            if queued_count:
                print(
                    f"Watched playlist '{playlist['name']}': {len(new_tracks)} new tracks, "
                    f"{len(missing_tracks)} missing tracks, {queued_count} queued"
                )

            return {
                "playlist_id": playlist_id,
                "name": playlist["name"],
                "total_tracks": len(tracks),
                "new_tracks": len(new_tracks),
                "missing_tracks": len(missing_tracks),
                "queued": queued_count,
                "import_id": import_id,
                "jobs": []
            }

        except HTTPException as e:
            conn.execute(
                "UPDATE watched_playlists SET last_checked = datetime('now') WHERE id = ?",
                (playlist_id,)
            )
            conn.commit()
            return {
                "playlist_id": playlist_id,
                "name": playlist["name"],
                "error": e.detail
            }
        except Exception as e:
            conn.execute(
                "UPDATE watched_playlists SET last_checked = datetime('now') WHERE id = ?",
                (playlist_id,)
            )
            conn.commit()
            return {
                "playlist_id": playlist_id,
                "name": playlist["name"],
                "error": str(e)
            }


# =============================================================================
# Background Scheduler for Watched Playlists
# =============================================================================

_scheduler_running = False


def watched_playlist_scheduler():
    """Background thread that periodically checks watched playlists"""
    global _scheduler_running
    _scheduler_running = True

    print(f"Watched playlist scheduler started (checking every {WATCHED_PLAYLIST_CHECK_HOURS} hours)")

    # Brief delay to let the app fully initialise, then check immediately
    time.sleep(10)
    print("Scheduler: Running initial check for overdue playlists...")

    while _scheduler_running:
        try:
            # Run the check
            print("Scheduler: Checking watched playlists...")
            with db_conn() as conn:
                conn.row_factory = sqlite3.Row

                playlists = conn.execute("""
                    SELECT id, name FROM watched_playlists
                    WHERE enabled = 1
                    AND (last_checked IS NULL
                         OR datetime(last_checked, '+' || refresh_interval_hours || ' hours') < datetime('now'))
                """).fetchall()

            if playlists:
                print(f"Scheduler: Found {len(playlists)} playlists due for refresh")
                total_new = 0
                for playlist in playlists:
                    result = refresh_watched_playlist(playlist["id"])
                    total_new += result.get("new_tracks", 0)
                print(f"Scheduler: Checked {len(playlists)} playlists, {total_new} new tracks found")
            else:
                print("Scheduler: No playlists due for refresh")

        except Exception as e:
            print(f"Scheduler error: {e}")

        # Sleep until next check interval
        base_sleep_seconds = WATCHED_PLAYLIST_CHECK_HOURS * 3600
        jitter = random.uniform(0.95, 1.05)
        sleep_seconds = max(60, int(base_sleep_seconds * jitter))
        elapsed = 0
        while elapsed < sleep_seconds and _scheduler_running:
            time.sleep(60)  # Check every minute if we should stop
            elapsed += 60


def start_scheduler():
    """Start the background scheduler if not already running"""
    global _scheduler_running

    if WATCHED_PLAYLIST_CHECK_HOURS <= 0:
        print("Watched playlist scheduler disabled (WATCHED_PLAYLIST_CHECK_HOURS=0)")
        return

    if _scheduler_running:
        return

    spawn_daemon_thread(watched_playlist_scheduler)
