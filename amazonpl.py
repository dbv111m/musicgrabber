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


def _extract_from_payload(
    payload: Any,
    rows: dict[int, str],
    playlist_names: list[str] | None = None,
) -> None:
    """Extract Amazon's ordered playlist rows from a ``showHome`` response.

    The response calls these ``VisualRowItemElement`` objects.  Older code
    looked for generic ``title`` and ``artistName`` fields, so it reported
    precisely zero tracks even though the complete playlist was sitting in
    front of it under ``primaryText`` and ``secondaryText1``.  ``rowIndex`` is
    the playlist position and, unlike the rendered page, is available for the
    whole list at once.

    ``rows`` is keyed by position rather than track text so repeated songs do
    not disturb the ordering.  The final conversion deliberately retains the
    scraper's established unique-track behaviour.
    """
    if isinstance(payload, dict):
        interface = str(payload.get("interface", ""))
        if interface.endswith(".VisualRowItemElement"):
            title = payload.get("primaryText")
            artist = payload.get("secondaryText1")
            try:
                position = int(payload.get("rowIndex"))
            except (TypeError, ValueError):
                position = -1

            if (
                position >= 0
                and isinstance(title, str)
                and isinstance(artist, str)
            ):
                artist = artist.strip()
                title = _strip_explicit_badge(title).strip()
                title = _strip_featured_suffix(title, artist)
                if title and artist and title.lower() != artist.lower():
                    rows.setdefault(position, _normalise_track(artist, title))

        # The visible heading observes Amazon's PLAYLIST_NAME storage value.
        # Capture it with the same payload so the JSON path does not depend on
        # the page managing to paint an h1 afterwards.
        observer = payload.get("observer")
        if playlist_names is not None and isinstance(observer, dict):
            if observer.get("storageGroup") == "PLAYLIST_NAME":
                name = payload.get("text")
                if not isinstance(name, str):
                    default = observer.get("defaultValue")
                    name = default.get("text") if isinstance(default, dict) else None
                if isinstance(name, str) and name.strip():
                    playlist_names.append(name.strip())

        for value in payload.values():
            _extract_from_payload(value, rows, playlist_names)
    elif isinstance(payload, list):
        for item in payload:
            _extract_from_payload(item, rows, playlist_names)


def _ordered_network_tracks(rows: dict[int, str]) -> list[str]:
    """Turn positioned JSON rows into the scraper's ordered unique track list."""
    tracks: dict[str, None] = {}
    for _, track in sorted(rows.items()):
        tracks.setdefault(track, None)
    return list(tracks)


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
    # Each intercepted response is kept separate. Amazon can call showHome
    # more than once; the payload with the most rows is the playlist rather
    # than some later navigation fragment.
    network_candidates: list[tuple[dict[int, str], list[str]]] = []

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

            def capture_show_home(route) -> None:
                """Read and forward Amazon's playlist JSON response.

                A normal ``page.on('response')`` listener works for short
                playlists, but Chromium evicts the multi-megabyte body of a
                100+ track list from its inspector cache before Playwright can
                read it. Interception reads the body while forwarding the same
                response to the page, so long playlists do not fall through a
                trapdoor merely for being enthusiastic.
                """
                try:
                    response = route.fetch()
                except Exception as exc:
                    print(f"Amazon JSON request capture failed: {exc}", file=sys.stderr)
                    route.continue_()
                    return

                try:
                    payload = response.json()
                    rows: dict[int, str] = {}
                    names: list[str] = []
                    _extract_from_payload(payload, rows, names)
                    if rows:
                        network_candidates.append((rows, names))
                except Exception as exc:
                    print(f"Amazon JSON response parse failed: {exc}", file=sys.stderr)

                route.fulfill(response=response)

            page.route("**/api/showHome", capture_show_home)

            page.goto(amazon_url, timeout=90000, wait_until="domcontentloaded")
            time.sleep(3)

            _accept_cookie_banner(page)

            # Wait for the app to settle and capture initial request burst.
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            network_rows: dict[int, str] = {}
            network_names: list[str] = []
            if network_candidates:
                network_rows, network_names = max(
                    network_candidates, key=lambda candidate: len(candidate[0])
                )

            # A cold container occasionally draws an Amazon page that never
            # renders a single row, then behaves perfectly a minute later.
            # Left alone, a watched playlist refreshing seconds after a restart
            # logs a spurious empty fetch and, in mirror mode, concludes the
            # whole playlist has been emptied upstream. One reload is far
            # cheaper than that conversation.
            if not network_rows and not _wait_for_rows(page):
                print("No rows rendered on first load; reloading once", file=sys.stderr)
                try:
                    page.reload(timeout=90000, wait_until="domcontentloaded")
                    time.sleep(3)
                    _accept_cookie_banner(page)
                    try:
                        page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass
                    if network_candidates:
                        network_rows, network_names = max(
                            network_candidates, key=lambda candidate: len(candidate[0])
                        )
                    if not network_rows:
                        _wait_for_rows(page)
                except Exception as exc:
                    print(f"Reload failed: {exc}", file=sys.stderr)

            # Amazon rarely goes network-idle, so that wait can give up before
            # showHome arrives. Rows only paint once it has, so by the time
            # _wait_for_rows is happy the JSON is usually sitting there. Look
            # again rather than scroll a recycling list for no reason.
            if not network_rows and network_candidates:
                network_rows, network_names = max(
                    network_candidates, key=lambda candidate: len(candidate[0])
                )

            # Extract playlist title if available
            if network_names:
                playlist_name = network_names[0]
            else:
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

            if not network_rows:
                # Scroll through the virtualised list, scraping tracks as we go.
                # Smaller scroll increments help catch rows that would otherwise
                # get recycled between captures in a virtualised container.
                stale = 0
                previous = 0
                while stale < 30:
                    _merge_positioned(_collect_visible_tracks(page), dom_tracks)

                    page.mouse.wheel(0, 600)
                    time.sleep(0.3)

                    total = len(dom_tracks)
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
                    new_total = len(dom_tracks)
                    if new_total == previous:
                        stale += 1
                    else:
                        stale = 0
                        previous = new_total

            browser.close()

        if network_rows:
            tracks = _ordered_network_tracks(network_rows)
            print(
                f"Amazon extract: json-rows={len(network_rows)} "
                f"unique={len(tracks)}",
                file=sys.stderr,
            )
        else:
            # Playlist order, not alphabetical order. Sorting the strings was
            # tidy and completely wrong: Amazon's running order is the one the
            # user actually chose to follow.
            tracks = [
                track for track, _ in sorted(dom_tracks.items(), key=lambda item: item[1])
            ]
            print(f"Amazon extract: dom-fallback={len(dom_tracks)}", file=sys.stderr)
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
