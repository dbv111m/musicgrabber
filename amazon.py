"""
MusicGrabber - Amazon Music Playlist Fetching

Runs amazonpl.py as a subprocess (Playwright headless browser) to scrape
public Amazon Music playlist pages.  Amazon's pages are fully JS-rendered
so there's no fast HTML-only path like Spotify -- it's headless or nothing.
"""

import json
import os
import subprocess
from pathlib import Path

from fastapi import HTTPException

from constants import (
    AMAZON_ALBUM_TITLE_WAIT,
    TIMEOUT_AMAZON_ALBUM_BROWSER,
    TIMEOUT_AMAZON_BROWSER,
)

_BROWSER_SCRIPT = Path(__file__).parent / "amazonpl.py"
_ALBUM_SCRIPT = Path(__file__).parent / "amazonalbum.py"


def fetch_amazon_playlist(url: str) -> dict:
    """Scrape an Amazon Music playlist via headless browser.

    Runs amazonpl.py in a separate subprocess to keep Playwright's
    event loop away from uvicorn's.

    Returns dict with: tracks (list of "Artist - Title"), playlist_name, count
    """
    print(f"Fetching Amazon Music playlist via headless browser: {url}")

    env = {**os.environ, "AMAZON_URL": url}

    try:
        result = subprocess.run(
            ["python3", str(_BROWSER_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_AMAZON_BROWSER,
            env=env,
        )
        print(f"Amazon script return code: {result.returncode}")
        if result.stdout:
            print(f"Amazon script stdout: {result.stdout[:500]}")
        if result.stderr:
            print(f"Amazon script stderr: {result.stderr[:500]}")
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail="Timeout fetching Amazon Music playlist; the page took too long to load"
        )

    if result.returncode != 0:
        error_msg = result.stderr or "Unknown error"
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch Amazon Music playlist: {error_msg}"
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=502,
            detail=f"Invalid response from Amazon browser script: {result.stdout[:200]}"
        )

    if not data.get("success"):
        error = data.get("error", "Unknown error")
        # Browser unavailable — degrade gracefully: return empty tracks so the
        # caller can fall back to non-browser sources instead of 502-ing the
        # whole request.
        if error.startswith("browser_unavailable"):
            print(f"Amazon browser unavailable, degrading: {error}")
            return {"tracks": [], "playlist_name": None}
        raise HTTPException(
            status_code=502,
            detail=f"Failed to scrape Amazon playlist: {error}"
        )

    tracks = data["tracks"]
    playlist_name = data["playlist_name"]

    print(f"Successfully extracted {len(tracks)} tracks from Amazon playlist")

    if not tracks:
        raise HTTPException(
            status_code=422,
            detail="No tracks found in Amazon playlist. The page structure may have changed."
        )

    return {
        "tracks": tracks,
        "playlist_name": playlist_name,
        "count": len(tracks)
    }


def fetch_amazon_album_info(url: str) -> dict:
    """Name an Amazon Music album page: {"artist": ..., "album": ...}.

    Runs amazonalbum.py as a subprocess, same as the playlist path, but that
    script drives Playwright via browserless CDP (BROWSER_ENDPOINT) rather than
    a local Chrome.  Album pages hand Playwright a shell of skeleton rows that
    never fill in; under undetected mode Amazon at least sets a document title
    naming the record, which is all the album pipeline wants.  See
    amazonalbum.py for the full story.

    Raises HTTPException on failure, so a dead ASIN surfaces as an honest
    error rather than a confidently wrong album.
    """
    print(f"Naming Amazon Music album via headless browser: {url}")

    env = {
        **os.environ,
        "AMAZON_ALBUM_URL": url,
        "AMAZON_ALBUM_TIMEOUT": str(AMAZON_ALBUM_TITLE_WAIT),
    }

    try:
        result = subprocess.run(
            ["python3", str(_ALBUM_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_AMAZON_ALBUM_BROWSER,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail="Timeout naming Amazon Music album; the page took too long to load",
        )

    if result.stderr:
        print(f"Amazon album script stderr: {result.stderr[:500]}")

    # SeleniumBase chatters on stdout, so take the last line that parses as the
    # JSON payload rather than assuming the script had the stream to itself.
    data = None
    for line in reversed((result.stdout or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
            break
        except json.JSONDecodeError:
            continue

    if data is None:
        raise HTTPException(
            status_code=502,
            detail=f"Invalid response from Amazon album script: {(result.stdout or '')[:200]}",
        )

    if not data.get("success"):
        error = data.get("error", "Unknown error")
        # Browser unavailable — degrade gracefully: return empty info so the
        # album pipeline can proceed without the Amazon title.
        if error.startswith("browser_unavailable"):
            print(f"Amazon album browser unavailable, degrading: {error}")
            return {"artist": None, "album": None}
        raise HTTPException(
            status_code=502,
            detail=f"Failed to name Amazon album: {error}",
        )

    print(f"Amazon album resolved: {data.get('artist')} - {data.get('album')}")
    return {"artist": data.get("artist"), "album": data.get("album")}
