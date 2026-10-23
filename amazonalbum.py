"""
MusicGrabber - Amazon Music Headless Album Script

Standalone script for naming a public Amazon Music album page.  Receives the
URL via environment and prints JSON to stdout:
{success, artist, album} or {success, error}.

Why this exists separately from amazonpl.py: the playlist scraper reads rows
out of the DOM with Playwright, which works perfectly on playlist pages and
not at all on album ones.  An album page renders twenty <music-image-row>
skeletons that never populate, fires no JSON that carries the tracklist, and
keeps nothing useful in window.amznMusic, so there is no row to read however
long you wait.  Driving Playwright via browserless CDP, the page sets a
document title of the form:

    Play <ALBUM> by <ARTIST> on Amazon Music Unlimited

and that is the entire job.  All the album pipeline needs is an artist and an
album name; MusicBrainz supplies the real tracklist once the album is matched,
so a scraper that can name the record has done everything asked of it.

An album Amazon will not serve (delisted, region-locked, plain wrong ASIN)
leaves the generic "Amazon Music | Listen to songs, podcasts, & more" title,
which does not match the pattern, so a dud comes back as a clean failure
rather than a confident wrong answer.
"""

import json
import os
import re
import time

try:
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:
    sync_playwright = None

# "Play Dai Dai by Shakira & Burna Boy on Amazon Music Unlimited".  The album
# is GREEDY and the artist lazy, so the split lands on the LAST " by " rather
# than the first: an album called "Poems by Heart" keeps its preposition
# instead of donating it to the artist.  Both the "Unlimited" and the plain
# "Amazon Music" suffixes are accepted, since Amazon uses both depending on
# the storefront.
_TITLE_RE = re.compile(
    r"^Play\s+(?P<album>.+)\s+by\s+(?P<artist>.+?)\s+on\s+Amazon Music(?:\s+Unlimited)?\s*$",
    re.IGNORECASE,
)

# Amazon will not render a thing until the consent dialog is dealt with, and it
# builds its buttons out of <music-button> web components rather than anything
# a CSS selector would recognise, hence going in by text.
_ACCEPT_COOKIES_JS = r"""(() => {
  const targets = ['Accept Cookies', 'Accept cookies', 'Accept all', 'Allow all', 'Agree'];
  for (const el of document.querySelectorAll('music-button, button, [role="button"], input[type=submit]')) {
    const text = (el.innerText || el.textContent || el.value || '').trim();
    for (const t of targets) {
      if (text.toLowerCase() === t.toLowerCase()) { el.click(); return t; }
    }
  }
  return null;
})()"""

_TITLE_JS = "(() => document.title)()"


def parse_album_title(title: str) -> tuple[str | None, str | None]:
    """Pull (artist, album) out of Amazon's document title.

    Returns (None, None) for the generic Amazon Music title, which is what a
    dead or unavailable ASIN leaves behind.
    """
    match = _TITLE_RE.match((title or "").strip())
    if not match:
        return None, None
    album = match.group("album").strip()
    artist = match.group("artist").strip()
    return (artist or None), (album or None)


def main() -> None:
    url = os.environ.get("AMAZON_ALBUM_URL", "").strip()
    if not url:
        print(json.dumps({"success": False, "error": "Missing AMAZON_ALBUM_URL"}))
        return

    try:
        timeout = max(10, int(os.environ.get("AMAZON_ALBUM_TIMEOUT", "45")))
    except ValueError:
        timeout = 45

    if sync_playwright is None:
        print(json.dumps({
            "success": False,
            "error": "Playwright is not installed in this environment",
        }))
        return

    browser_endpoint = os.environ.get("BROWSER_ENDPOINT", "http://host.docker.internal:3000")

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(browser_endpoint)
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "success": False,
                            "error": f"browser_unavailable: {exc}",
                        }
                    )
                )
                return
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            time.sleep(3)

            try:
                page.evaluate(_ACCEPT_COOKIES_JS)
                time.sleep(1.5)
            except Exception:
                # No banner, or it moved. The title check below is the real test.
                pass

            # Poll rather than sleep a fixed lump: the title arrives when the
            # app has decided what page it is on, which is usually quick and
            # occasionally not.
            deadline = time.time() + timeout
            artist = album = None
            while time.time() < deadline:
                try:
                    title = page.evaluate(_TITLE_JS)
                    artist, album = parse_album_title(title)
                except Exception:
                    artist = album = None
                if artist and album:
                    break
                time.sleep(1.5)

            browser.close()

            if not (artist and album):
                print(json.dumps({
                    "success": False,
                    "error": "Amazon did not name this album; it may be delisted, "
                             "region-locked or not an album at all",
                }))
                return

            print(json.dumps({"success": True, "artist": artist, "album": album}))
    except Exception as exc:
        print(json.dumps({"success": False, "error": f"Amazon album browser failed: {exc}"}))


if __name__ == "__main__":
    main()
