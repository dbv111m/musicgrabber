"""
MusicGrabber - Spotify Playlist Fetching

Scrapes Spotify embed pages and uses headless browser for large playlists.
Spotify killed their public API for playlist access, so we scrape the embed
page which has a predictable JSON-in-HTML structure. For playlists with >100
tracks, the embed is truncated and we fall back to Playwright.
"""

import json
import os
import subprocess
from pathlib import Path

from fastapi import HTTPException

from constants import TIMEOUT_SPOTIFY_BROWSER

_BROWSER_SCRIPT = Path(__file__).parent / "spotify_browser.py"


def fetch_spotify_playlist_via_browser(spotify_id: str, spotify_type: str) -> dict:
    """Fetch playlist/album tracks using a headless browser

    This method works without API credentials by loading the Spotify page
    and scrolling to load all tracks (Spotify lazy-loads them).

    Runs Playwright in a completely separate subprocess to avoid any
    interference from uvicorn's event loop.

    Returns dict with: tracks (list of "Artist - Title"), playlist_name, count
    """
    url = f"https://open.spotify.com/{spotify_type}/{spotify_id}"
    print(f"Fetching Spotify {spotify_type} via headless browser: {url}")

    env = {**os.environ, "SPOTIFY_TYPE": spotify_type, "SPOTIFY_ID": spotify_id}

    try:
        result = subprocess.run(
            ["python3", str(_BROWSER_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SPOTIFY_BROWSER,
            env=env,
        )
        print(f"Script return code: {result.returncode}")
        print(f"Script stdout: {result.stdout[:500] if result.stdout else 'empty'}")
        print(f"Script stderr: {result.stderr[:500] if result.stderr else 'empty'}")
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail=f"Timeout fetching Spotify {spotify_type} via browser"
        )

    if result.returncode != 0:
        error_msg = result.stderr or "Unknown error"
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch Spotify {spotify_type} via browser: {error_msg}"
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=502,
            detail=f"Invalid response from browser subprocess: {result.stdout[:200]}"
        )

    if not data.get("success"):
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch Spotify {spotify_type} via browser: {data.get('error', 'Unknown error')}"
        )

    tracks = data["tracks"]
    playlist_name = data["playlist_name"]

    print(f"Successfully extracted {len(tracks)} tracks via browser")

    if not tracks:
        raise HTTPException(
            status_code=422,
            detail=f"Could not extract tracks from {spotify_type}. The page structure may have changed."
        )

    return {
        "tracks": tracks,
        "playlist_name": playlist_name,
        "count": len(tracks)
    }
