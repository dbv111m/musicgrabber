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

from constants import TIMEOUT_AMAZON_BROWSER

_BROWSER_SCRIPT = Path(__file__).parent / "amazonpl.py"


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
            detail="Timeout fetching Amazon Music playlist — the page took too long to load"
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
        raise HTTPException(
            status_code=502,
            detail=f"Failed to scrape Amazon playlist: {data.get('error', 'Unknown error')}"
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
