"""
MusicGrabber - Amazon Music Headless Playlist Script

Standalone script for scraping public Amazon Music playlist pages.
Receives URL via environment and prints JSON to stdout:
{success, tracks, playlist_name, count} or {success, error}.
"""

import json
import os
import re
import sys
import time
from typing import Any

try:
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:
    sync_playwright = None


def _normalise_track(artist: str, title: str) -> str:
    artist = re.sub(r"\s+", " ", artist).strip()
    title = re.sub(r"\s+", " ", title).strip()
    return f"{artist} - {title}"


def _looks_like_duration(text: str) -> bool:
    return bool(re.match(r"^\d{1,2}:\d{2}$", text))


def _extract_from_payload(payload: Any, out: set[str]) -> None:
    """Best-effort recursive extractor for track-like JSON objects."""
    if isinstance(payload, dict):
        typename = str(payload.get("__typename", "")).lower()
        has_track_hint = any(
            k in payload for k in ("trackId", "asin", "duration", "durationMs", "isExplicit")
        ) or "track" in typename

        title = payload.get("title") or payload.get("name")
        artist = (
            payload.get("artistName")
            or payload.get("primaryArtistName")
            or payload.get("displayArtistName")
            or payload.get("subtitle")
        )

        if isinstance(payload.get("artist"), dict):
            artist = payload["artist"].get("name") or artist
        elif isinstance(payload.get("artist"), str):
            artist = payload.get("artist")

        if has_track_hint and isinstance(title, str) and isinstance(artist, str):
            title = title.strip()
            artist = artist.strip()
            if title and artist and title.lower() != artist.lower():
                out.add(_normalise_track(artist, title))

        for value in payload.values():
            _extract_from_payload(value, out)
    elif isinstance(payload, list):
        for item in payload:
            _extract_from_payload(item, out)


def _collect_visible_tracks(page) -> set[str]:
    """Extract visible rows from the rendered DOM.

    Amazon Music uses <music-image-row> web components with structured
    columns: .col1 (title), .col2 (artist), .col3 (album).  We try
    those first, then fall back to generic row selectors for resilience.
    """
    # Primary strategy: Amazon's structured web components
    structured_js = r"""
() => {
  const tracks = [];
  for (const row of document.querySelectorAll('music-image-row')) {
    const col1 = row.querySelector('.col1');
    const col2 = row.querySelector('.col2');
    if (!col1 || !col2) continue;
    // Use the column's full text so compound artists aren't truncated
    // (e.g. "Kay Kyser & His Orchestra" might be split across child links)
    const title = (col1.innerText || col1.textContent || '').trim();
    const artist = (col2.innerText || col2.textContent || '').trim();
    if (title && artist) tracks.push({title, artist});
  }
  return tracks;
}
"""
    tracks: set[str] = set()
    for item in page.evaluate(structured_js):
        title = item["title"].strip()
        artist = item["artist"].strip()
        if title and artist and title.lower() != artist.lower():
            tracks.add(_normalise_track(artist, title))

    if tracks:
        return tracks

    # Fallback: generic row selectors with heuristic parsing
    fallback_js = r"""
() => {
  const rowSelectors = [
    '[data-testid*="track"]', '[data-testid*="Track"]',
    '[role="row"]', 'tr', 'li'
  ];
  const rows = [];
  for (const sel of rowSelectors) {
    for (const el of document.querySelectorAll(sel)) {
      const txt = (el.innerText || '').trim();
      if (txt) rows.push(txt);
    }
  }
  return rows;
}
"""
    for text in page.evaluate(fallback_js):
        parts = [p.strip() for p in text.split("\n") if p.strip()]
        if len(parts) < 2:
            continue

        parts = [p for p in parts if p.lower() not in {"e", "explicit", "shuffle"}]
        parts = [p for p in parts if not _looks_like_duration(p)]

        if len(parts) < 2:
            continue

        if parts[0].isdigit() and len(parts) >= 3:
            title = parts[1]
            artist = parts[2]
        else:
            title = parts[0]
            artist = parts[1]

        lowered = f"{title} {artist}".lower()
        if any(
            marker in lowered
            for marker in ("listeners", "followers", "songs", "playlist", "album", "station")
        ):
            continue

        if title and artist and title.lower() != artist.lower():
            tracks.add(_normalise_track(artist, title))

    return tracks


def _accept_cookie_banner(page) -> None:
    """Dismiss the Amazon cookie consent banner.

    Amazon uses <music-button> web components rather than plain <button>,
    so we need JS-based detection to find the right clickable element.
    """
    # Try Playwright selectors first (standard buttons / known IDs)
    selectors = [
        "#sp-cc-accept",
        "input[name='accept']",
    ]
    for selector in selectors:
        try:
            btn = page.query_selector(selector)
            if btn:
                btn.click()
                time.sleep(1.5)
                return
        except Exception:
            continue

    # Amazon Music uses <music-button> web components -- find by text content
    accepted = page.evaluate(r"""
() => {
  const targets = ['Accept Cookies', 'Accept cookies', 'Accept all', 'Allow all', 'Agree'];
  for (const el of document.querySelectorAll('music-button, button, [role="button"]')) {
    const text = (el.innerText || el.textContent || '').trim();
    for (const t of targets) {
      if (text === t || text.toLowerCase() === t.toLowerCase()) {
        el.click();
        return true;
      }
    }
  }
  return false;
}
""")
    if accepted:
        time.sleep(1.5)


def main() -> None:
    if sync_playwright is None:
        print(
            json.dumps(
                {
                    "success": False,
                    "error": "Playwright is not installed in this environment",
                }
            )
        )
        return

    amazon_url = os.environ.get("AMAZON_URL", "").strip()
    if not amazon_url:
        print(json.dumps({"success": False, "error": "Missing AMAZON_URL"}))
        return

    playlist_name = "Amazon Music Playlist"
    dom_tracks: set[str] = set()
    network_tracks: set[str] = set()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()

            def on_response(response):
                try:
                    req = response.request
                    if req.resource_type not in ("xhr", "fetch"):
                        return
                    ctype = response.headers.get("content-type", "")
                    if "json" not in ctype:
                        return
                    data = response.json()
                    _extract_from_payload(data, network_tracks)
                except Exception:
                    return

            page.on("response", on_response)
            page.goto(amazon_url, timeout=90000, wait_until="domcontentloaded")
            time.sleep(3)

            _accept_cookie_banner(page)

            # Wait for the app to settle and capture initial request burst.
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # Extract playlist title if available
            for sel in ("h1", '[data-testid*="title"]'):
                try:
                    el = page.query_selector(sel)
                    if el:
                        text = el.inner_text().strip()
                        if text:
                            playlist_name = text
                            break
                except Exception:
                    continue

            # Scroll through the virtualised list, scraping tracks as we go.
            # Smaller scroll increments help catch rows that would otherwise
            # get recycled between captures in a virtualised container.
            stale = 0
            previous = 0
            while stale < 30:
                dom_tracks.update(_collect_visible_tracks(page))

                page.mouse.wheel(0, 600)
                time.sleep(0.3)

                total = len(dom_tracks) + len(network_tracks)
                if total == previous:
                    stale += 1
                else:
                    stale = 0
                    previous = total

            # Second pass: scroll back to the top and crawl down again.
            # Virtualised lists recycle DOM nodes, so the first batch of
            # rows may have been swapped out before we captured them.
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(1)
            stale = 0
            while stale < 15:
                dom_tracks.update(_collect_visible_tracks(page))
                page.mouse.wheel(0, 600)
                time.sleep(0.3)
                new_total = len(dom_tracks) + len(network_tracks)
                if new_total == previous:
                    stale += 1
                else:
                    stale = 0
                    previous = new_total

            browser.close()

        tracks = sorted(dom_tracks | network_tracks)
        print(
            json.dumps(
                {
                    "success": True,
                    "tracks": tracks,
                    "playlist_name": playlist_name,
                    "count": len(tracks),
                }
            )
        )
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}))


if __name__ == "__main__":
    main()
