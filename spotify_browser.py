"""
MusicGrabber - Spotify Headless Browser Script

Standalone script executed as a subprocess by spotify.py.
Receives spotify_type and spotify_id via environment variables.
Outputs JSON to stdout: {success, tracks, playlist_name, count} or {success, error}.
"""

import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

spotify_type = os.environ["SPOTIFY_TYPE"]
spotify_id = os.environ["SPOTIFY_ID"]
url = f"https://open.spotify.com/{spotify_type}/{spotify_id}"
SELECTOR = '[data-testid="tracklist-row"]'

tracks = []
playlist_name = f"Spotify {spotify_type.title()}"

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = context.new_page()

        page.goto(url, timeout=60000)
        time.sleep(3)

        # Accept cookie consent if present — this can block page rendering
        cookie_selectors = [
            "button:has-text('Accept cookies')",
            "button:has-text('Accept Cookies')",
            "button:has-text('ACCEPT COOKIES')",
            "[data-testid='cookie-policy-manage-dialog-accept-button']",
            "button.onetrust-close-btn-handler"
        ]
        for sel in cookie_selectors:
            try:
                btn = page.query_selector(sel)
                if btn:
                    print(f"DEBUG: Found cookie button with selector: {sel}", file=sys.stderr)
                    btn.click()
                    time.sleep(2)
                    break
            except Exception as e:
                print(f"DEBUG: Cookie selector {sel} failed: {e}", file=sys.stderr)

        # Wait for track list to load
        page.wait_for_selector(SELECTOR, timeout=30000)

        try:
            # Get playlist name from the page
            title_elem = page.query_selector('[data-testid="playlist-page"] h1')
            if not title_elem:
                title_elem = page.query_selector('[data-testid="entityTitle"] h1')
            if not title_elem:
                title_elem = page.query_selector('h1')
            if title_elem:
                name = title_elem.inner_text().strip()
                if name and name != "Your Library":
                    playlist_name = name
        except Exception:
            pass

        # Spotify uses virtualised scrolling — tracks get unloaded as you scroll.
        # Extract tracks incrementally while scrolling.
        seen_tracks = set()
        stale_count = 0
        last_seen_count = 0

        def extract_visible_tracks():
            for row in page.query_selector_all(SELECTOR):
                try:
                    text = row.inner_text().strip()
                    parts = text.split(chr(10))
                    parts = [pt.strip() for pt in parts if pt.strip()]

                    # Only extract tracks that have a track number
                    if not parts or not parts[0].isdigit():
                        continue

                    # Skip the track number
                    parts = parts[1:]

                    # Skip "E" for Explicit marker
                    if parts and parts[0] == "E":
                        parts = parts[1:]

                    if len(parts) >= 2:
                        track_name = parts[0].strip()
                        artist = parts[1].strip()
                        if artist == "E" and len(parts) >= 3:
                            artist = parts[2].strip()
                        if track_name and artist and artist != "E":
                            seen_tracks.add(f"{artist} - {track_name}")
                except Exception:
                    continue

        # First extraction before scrolling
        extract_visible_tracks()

        while stale_count < 20:
            rows = page.query_selector_all(SELECTOR)
            if rows:
                rows[-1].scroll_into_view_if_needed()
            time.sleep(0.3)

            extract_visible_tracks()

            if len(seen_tracks) == last_seen_count:
                stale_count += 1
            else:
                stale_count = 0
                last_seen_count = len(seen_tracks)

        tracks = list(seen_tracks)
        browser.close()

    print(json.dumps({"success": True, "tracks": tracks, "playlist_name": playlist_name, "count": len(tracks)}))

except Exception as e:
    print(json.dumps({"success": False, "error": str(e)}))
