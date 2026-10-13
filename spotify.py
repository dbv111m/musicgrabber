"""
MusicGrabber - Spotify Playlist Fetching

Scrapes Spotify embed pages and uses headless browser for large playlists.
Spotify killed their public API for playlist access, so we scrape the embed
page which has a predictable JSON-in-HTML structure. For playlists with >100
tracks, the embed is truncated and we fall back to Playwright.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import httpx
from fastapi import HTTPException

from constants import (
    TIMEOUT_SPOTIFY_BROWSER, SPOTIFY_BROWSER_STALL_SECONDS,
    SPOTIFY_BROWSER_MAX_SECONDS, SPOTIFY_BROWSER_SECONDS_PER_TRACK,
    SPOTIFY_BROWSER_BASE_OVERHEAD, SPOTIFY_BROWSER_DEADLINE_BUFFER,
)
from settings import get_setting_int

_BROWSER_SCRIPT = Path(__file__).parent / "spotify_browser.py"


def _fetch_spotify_expected_total(spotify_id: str, spotify_type: str) -> int | None:
    """Best-effort fetch of reported track count from the public Spotify page."""
    url = f"https://open.spotify.com/{spotify_type}/{spotify_id}"
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            response = client.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                },
            )
        if response.status_code != 200:
            return None

        html = response.text
        match = re.search(r'<meta\s+name="music:song_count"\s+content="(\d+)"', html)
        if match:
            return int(match.group(1))
        match = re.search(
            r'<meta\s+property="og:description"\s+content="[^"]*?(\d[\d,]*)\s+items"',
            html,
            re.IGNORECASE,
        )
        if match:
            return int(match.group(1).replace(",", ""))
    except Exception:
        return None
    return None


def fetch_spotify_playlist_via_browser(
    spotify_id: str,
    spotify_type: str,
    expected_total: int | None = None,
    sp_dc: str | None = None,
    user_id: str | None = None,
) -> dict:
    """Fetch playlist/album tracks using a headless browser

    This method works without API credentials by loading the Spotify page
    and scrolling to load all tracks (Spotify lazy-loads them).

    Runs Playwright in a completely separate subprocess to avoid any
    interference from uvicorn's event loop.

    Returns dict with: tracks (list of "Artist - Title"), playlist_name, count
    """
    url = f"https://open.spotify.com/{spotify_type}/{spotify_id}"
    print(f"Fetching Spotify {spotify_type} via headless browser: {url}")

    configured_base_timeout = get_setting_int(
        "spotify_browser_timeout_seconds", TIMEOUT_SPOTIFY_BROWSER
    )
    configured_stall_seconds = get_setting_int(
        "spotify_browser_stall_seconds", SPOTIFY_BROWSER_STALL_SECONDS
    )
    configured_base_timeout = max(60, min(1800, configured_base_timeout))
    configured_stall_seconds = max(5, min(300, configured_stall_seconds))
    ceiling = max(configured_base_timeout, SPOTIFY_BROWSER_MAX_SECONDS)

    if not expected_total:
        # Best-effort, but Spotify's public page is now a JS shell so this almost
        # always comes back None; the browser reads the real count from the DOM.
        expected_total = _fetch_spotify_expected_total(spotify_id, spotify_type)

    # Size the hard subprocess timeout. When we somehow know the count up front, use
    # it as a time guesstimate (a few hundred ms per track plus fixed overhead). When
    # we don't, give the browser a generous ceiling and trust it to stop itself once
    # it has reached the end of the list (it reads the count from the rendered DOM)
    # rather than capping at a tight 300s and timing out on a 5,000-track playlist.
    if expected_total and expected_total > 0:
        estimate = SPOTIFY_BROWSER_BASE_OVERHEAD + int(expected_total * SPOTIFY_BROWSER_SECONDS_PER_TRACK)
        timeout_seconds = max(configured_base_timeout, min(ceiling, estimate))
    else:
        timeout_seconds = ceiling

    env = {**os.environ, "SPOTIFY_TYPE": spotify_type, "SPOTIFY_ID": spotify_id}
    if spotify_type == "collection" and spotify_id == "tracks":
        env["SPOTIFY_IS_LIKED_SONGS"] = "1"
    if expected_total and expected_total > 0:
        env["SPOTIFY_EXPECTED_TOTAL"] = str(expected_total)
    env["SPOTIFY_BROWSER_STALL_SECONDS"] = str(configured_stall_seconds)
    # Tell the browser when we're going to pull the plug so it can bow out cleanly a
    # few seconds early and hand back partial results, instead of being hard-killed
    # (which loses everything and drops us back to the 100-track embed).
    env["SPOTIFY_BROWSER_DEADLINE_SECONDS"] = str(max(30, timeout_seconds - SPOTIFY_BROWSER_DEADLINE_BUFFER))
    # Pass the per-track estimate so the browser can size its own working budget from
    # the count it finds in the DOM (the count we can't see from out here).
    env["SPOTIFY_BROWSER_SECONDS_PER_TRACK"] = str(SPOTIFY_BROWSER_SECONDS_PER_TRACK)
    env["SPOTIFY_BROWSER_BASE_OVERHEAD"] = str(SPOTIFY_BROWSER_BASE_OVERHEAD)
    if sp_dc:
        env["SPOTIFY_SP_DC"] = sp_dc
    if user_id:
        env["SPOTIFY_USER_ID"] = user_id

    print(
        "Spotify browser limits: "
        f"timeout={timeout_seconds}s, stall={configured_stall_seconds}s "
        f"(expected_total={expected_total or 'unknown'})"
    )

    try:
        result = subprocess.run(
            ["python3", str(_BROWSER_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
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
        error = data.get("error", "Unknown error")
        # Browser script signals expired cookies with a specific sentinel
        if error == "spotify_cookies_expired":
            if sp_dc and user_id is not None:
                from settings import set_user_setting
                set_user_setting(user_id, "spotify_cookies_expired", "true")
            elif sp_dc:
                from settings import set_setting
                set_setting("spotify_cookies_expired", "true")
            raise HTTPException(status_code=401, detail="spotify_cookies_expired")
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch Spotify {spotify_type} via browser: {error}"
        )

    tracks = data["tracks"]
    playlist_name = data["playlist_name"]

    print(f"Successfully extracted {len(tracks)} tracks via browser")

    if not tracks:
        raise HTTPException(
            status_code=422,
            detail=f"Could not extract tracks from {spotify_type}. The page structure may have changed."
        )

    result = {
        "tracks": tracks,
        "playlist_name": playlist_name,
        "count": len(tracks),
    }
    # The browser reports the count it read from the page; if we came up short, the
    # playlist is probably huge and ran out of scroll time. Flag it instead of silently
    # handing back a partial list.
    detected_total = data.get("expected_total")
    if detected_total and len(tracks) < detected_total:
        result["warning"] = (
            f"Spotify reports {detected_total} tracks but only {len(tracks)} were extracted. "
            "Very large playlists may need a higher spotify_browser_timeout_seconds."
        )
        print(f"Spotify browser extracted {len(tracks)}/{detected_total} tracks (partial)")
    return result
