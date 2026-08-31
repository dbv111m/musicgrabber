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


# How long to wait for Amazon to render its first row before concluding the
# page is a dud and worth one reload.
ROW_RENDER_WAIT = 20


def _wait_for_rows(page, timeout: float = ROW_RENDER_WAIT) -> int:
    """Poll until at least one row has actually rendered text.

    Returns how many populated rows were seen, 0 if the page never got going.
    A skeleton row exists in the DOM long before it has any content in it, so
    counting <music-image-row> elements proves nothing; only text does.
    """
    deadline = time.time() + timeout
    count_js = """
() => {
  let n = 0;
  for (const row of document.querySelectorAll('music-image-row')) {
    const c = row.querySelector('.col1');
    if (c && (c.innerText || '').trim()) n++;
  }
  return n;
}
"""
    while time.time() < deadline:
        try:
            filled = page.evaluate(count_js)
        except Exception:
            filled = 0
        if filled:
            return filled
        time.sleep(1)
    return 0


def _normalise_track(artist: str, title: str) -> str:
    artist = re.sub(r"\s+", " ", artist).strip()
    title = re.sub(r"\s+", " ", title).strip()
    return f"{artist} - {title}"


def _strip_explicit_badge(text: str) -> str:
    r"""Drop Amazon's explicit marker, which lives on its own line in the cell.

    The rendered title cell reads "petal\nE", and collapsing that whitespace
    gives "petal E", so half the chart ends up being searched for under a
    title nobody has ever released.  Only a trailing line that is exactly the
    badge goes; a song genuinely called "Vitamin E" keeps its vitamins.
    """
    return re.sub(r"\n\s*(?:E|EXPLICIT)\s*$", "", text, flags=re.IGNORECASE)


def _strip_featured_suffix(title: str, artist: str) -> str:
    r"""Drop Amazon's "+ Featured Artist" tail when the credit already says so.

    Amazon names a collaboration "Stateside + Zara Larsson" while crediting
    "PinkPantheress feat. Zara Larsson" in the very next column, so the search
    query carries her twice and matches a song of that name nowhere on earth.
    This is Amazon's own title string, not a stray DOM node, so no amount of
    reading the right element gets rid of it.

    Only strip when the trailing name genuinely appears in the artist credit.
    A track actually called "Peaches + Cream" by somebody unrelated keeps its
    cream, which is the whole reason this is not a bare regex.
    """
    if " + " not in title:
        return title
    head, tail = title.rsplit(" + ", 1)
    head, tail = head.strip(), tail.strip()
    if not head or not tail:
        return title
    if tail.lower() in (artist or "").lower():
        return head
    return title


def _looks_like_duration(text: str) -> bool:
    return bool(re.match(r"^\d{1,2}:\d{2}$", text))


def _extract_from_payload(payload: Any, out: dict[str, None]) -> None:
    """Best-effort recursive extractor for track-like JSON objects.

    `out` is a dict used as an ordered set, so discovery order survives.
    """
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
                out.setdefault(_normalise_track(artist, title), None)

        for value in payload.values():
            _extract_from_payload(value, out)
    elif isinstance(payload, list):
        for item in payload:
            _extract_from_payload(item, out)


def _collect_visible_tracks(page) -> list[tuple[float, str]]:
    """Extract visible rows from the rendered DOM, with their list positions.

    Amazon Music uses <music-image-row> web components with structured
    columns: .col1 (title), .col2 (artist), .col3 (album).  We try
    those first, then fall back to generic row selectors for resilience.

    Every row comes back paired with an ordering key, because the list is
    virtualised and we crawl it twice; discovery order on its own would dump
    any row we missed on the first pass at the very bottom of the playlist.
    """
    # Primary strategy: Amazon's structured web components
    structured_js = r"""
() => {
  const rows = [];
  for (const row of document.querySelectorAll('music-image-row')) {
    const col1 = row.querySelector('.col1');
    const col2 = row.querySelector('.col2');
    if (!col1 || !col2) continue;
    // The title cell is the anchor; everything else in there is a badge.
    // Amazon hangs an explicit "E" and featured-artist chips off a sibling
    // <div class="tags">, so the cell's full text reads "petal E" or
    // "Stateside + Zara Larsson" and we go looking for songs of that name.
    // The anchor carries the title and nothing else, which fixes both at once
    // and lets a song genuinely called "E85" keep its name without a regex
    // having to be clever about it. Fall back to the cell text for any row
    // that renders without a link (unavailable tracks, layout changes).
    const titleLink = col1.querySelector('music-link a') || col1.querySelector('a');
    const title = titleLink
      ? (titleLink.innerText || titleLink.textContent || '').trim()
      : (col1.innerText || col1.textContent || '').trim();
    // Artists keep the column's full text so compound credits aren't truncated
    // (e.g. "Kay Kyser & His Orchestra" split across child links).
    const artist = (col2.innerText || col2.textContent || '').trim();
    if (!title || !artist) continue;
    const rect = row.getBoundingClientRect();
    rows.push({title, artist, off: row.offsetTop,
               abs: Math.round(rect.top + window.scrollY)});
  }
  // offsetTop is the virtual list's own row offset, so it stays put however
  // far we have scrolled.  If this layout doesn't position its rows we use
  // document coordinates for all of them, rather than mixing the two scales.
  const positioned = rows.some(r => r.off > 0);
  return rows.map(r => ({title: r.title, artist: r.artist,
                         pos: positioned ? r.off : r.abs}));
}
"""
    tracks: list[tuple[float, str]] = []
    for item in page.evaluate(structured_js):
        artist = item["artist"].strip()
        title = _strip_explicit_badge(item["title"]).strip()
        title = _strip_featured_suffix(title, artist)
        if title and artist and title.lower() != artist.lower():
            tracks.append((item["pos"], _normalise_track(artist, title)))

    if tracks:
        return tracks

    # Fallback: generic row selectors with heuristic parsing.  These come from
    # several selectors at once, so their offsetParents disagree; document
    # coordinates are the only scale they all share.
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
      if (!txt) continue;
      const rect = el.getBoundingClientRect();
      rows.push({txt, pos: Math.round(rect.top + window.scrollY)});
    }
  }
  return rows;
}
"""
    for row in page.evaluate(fallback_js):
        parts = [p.strip() for p in row["txt"].split("\n") if p.strip()]
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
            tracks.append((row["pos"], _normalise_track(artist, title)))

    return tracks


def _merge_positioned(collected: list[tuple[float, str]], seen: dict[str, float]) -> None:
    """Fold a scrape into the running set, keeping each track's earliest slot.

    A track we meet twice is the same track; the position it had the first
    time up the page is the one the playlist actually means.
    """
    for position, track in collected:
        existing = seen.get(track)
        if existing is None or position < existing:
            seen[track] = position


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

    # Amazon Music uses <music-button> web components - find by text content
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
    # track -> position in the list; dicts keep insertion order, which is the
    # tie-break for anything the page couldn't give us a position for.
    dom_tracks: dict[str, float] = {}
    network_tracks: dict[str, None] = {}  # ordered set, discovery order

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

            # A cold container occasionally draws an Amazon page that never
            # renders a single row, then behaves perfectly a minute later.
            # Left alone, a watched playlist refreshing seconds after a restart
            # logs a spurious empty fetch and, in mirror mode, concludes the
            # whole playlist has been emptied upstream. One reload is far
            # cheaper than that conversation.
            if not _wait_for_rows(page):
                print("No rows rendered on first load; reloading once", file=sys.stderr)
                try:
                    page.reload(timeout=90000, wait_until="domcontentloaded")
                    time.sleep(3)
                    _accept_cookie_banner(page)
                    _wait_for_rows(page)
                except Exception as exc:
                    print(f"Reload failed: {exc}", file=sys.stderr)

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
                _merge_positioned(_collect_visible_tracks(page), dom_tracks)

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
                _merge_positioned(_collect_visible_tracks(page), dom_tracks)
                page.mouse.wheel(0, 600)
                time.sleep(0.3)
                new_total = len(dom_tracks) + len(network_tracks)
                if new_total == previous:
                    stale += 1
                else:
                    stale = 0
                    previous = new_total

            browser.close()

        # Playlist order, not alphabetical order.  Sorting the "Artist - Title"
        # strings was tidy and completely wrong: a playlist is a running order,
        # and Amazon's is the one the user actually chose to follow.
        tracks = [track for track, _ in sorted(dom_tracks.items(), key=lambda item: item[1])]
        network_only = [track for track in network_tracks if track not in dom_tracks]
        tracks += network_only
        # The network-JSON extractor is on probation. It contributed nothing at
        # all on the playlists measured so far, everything having come from the
        # DOM, but it costs nothing to run and might be the only thing that
        # works on a shape nobody has tried yet. Log what it actually earns so
        # the decision to keep or bin it is eventually made on evidence rather
        # than on a hunch. If this line reads "network-only: 0" for every
        # playlist shape you can find, delete _extract_from_payload guilt-free.
        print(
            f"Amazon extract: dom={len(dom_tracks)} network={len(network_tracks)} "
            f"network-only={len(network_only)}",
            file=sys.stderr,
        )
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
