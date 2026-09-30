"""Monochrome's tracks API: search it, match the ISRC, fetch the FLAC.

In September 2026 Monochrome moved from monochrome.tf to monochrome.st and
switched its Turnstile-guarded unified playback off, leaving MusicGrabber's
headless Chrome with nobody to talk to. What replaced it is refreshingly dull:
a plain HTTP catalogue with no auth, no token and no browser.

  Search:  GET {API}/search/tracks?q=...&limit=N  (every track carries its ISRC)
  Audio:   GET {API}/track/{trackId}              (the FLAC itself, 16/44.1)

There is no ISRC lookup, and an ISRC used as the query returns unrelated
nonsense, so we search by artist and title and keep only the exact ISRC match.
The same lookup doubles as Monochrome's "can we actually download this?"
check for search results, now that Qobuz is no longer there to ask.
Quality is fixed: the endpoint ignores any quality parameter, and so does
Monochrome's own player.

The API address is read from Monochrome's web client rather than trusted to
this file, because this is its third move and it is unlikely to be the last.
The retired browser route, for the curious, lives at git tag
archive/monochrome-browser-leg.
"""

import re
import threading
import time
from urllib.parse import urljoin

import httpx

from constants import (
    MONOCHROME_TRACKS_API_FALLBACK,
    MONOCHROME_TRACKS_API_URL,
    MONOCHROME_TRACKS_DISCOVERY_TTL,
    MONOCHROME_WEB_URL,
    TIMEOUT_MONOCHROME_SEARCH,
)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Accept": "application/json",
}

# A retired domain answers with a tiny page whose only job is a meta refresh to
# the new one. httpx follows HTTP redirects on its own; this is the other kind.
_META_REFRESH_RE = re.compile(
    r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]+content=["'][^"']*?url=([^"'\s>]+)""",
    re.IGNORECASE,
)
_BUNDLE_RE = re.compile(r"""<script[^>]+src=["']([^"']*assets/index-[^"']+\.js)""")
# The client class that owns /search/tracks and /track/{id} takes its base URL
# as a constructor default. Minified names change every build, so find the
# name first and then the string it is bound to.
_CLIENT_DEFAULT_RE = re.compile(r"constructor\([\w$]+=([\w$]+)\)\{this\.baseUrl=")

_RETRY_STATUSES = {502, 503, 504}

_discovery_lock = threading.Lock()
_discovered: tuple[str, float] | None = None


def _fetch_web_client() -> httpx.Response:
    """Load Monochrome's front page, following meta-refresh hops to a new domain."""
    url = MONOCHROME_WEB_URL
    for _ in range(3):
        resp = httpx.get(url, headers=_HEADERS, timeout=TIMEOUT_MONOCHROME_SEARCH, follow_redirects=True)
        match = _META_REFRESH_RE.search(resp.text[:4096])
        if not match:
            resp.raise_for_status()
            return resp
        url = urljoin(str(resp.url), match.group(1))
    raise RuntimeError(f"Monochrome web client redirected too many times (last hop {url})")


def _discover_api_base() -> str:
    page = _fetch_web_client()
    bundle_match = _BUNDLE_RE.search(page.text)
    if not bundle_match:
        raise RuntimeError("Monochrome client bundle was not found")
    bundle = httpx.get(
        urljoin(str(page.url), bundle_match.group(1)),
        headers=_HEADERS,
        timeout=TIMEOUT_MONOCHROME_SEARCH,
        follow_redirects=True,
    )
    bundle.raise_for_status()
    name_match = _CLIENT_DEFAULT_RE.search(bundle.text)
    if name_match:
        value_match = re.search(
            rf'(?<![\w$]){re.escape(name_match.group(1))}="(https?://[^"\\]+)"', bundle.text
        )
        if value_match:
            return value_match.group(1).rstrip("/")
    raise RuntimeError("Monochrome tracks API address was not found in the client bundle")


def api_base() -> str:
    """Where the tracks API lives today.

    An explicit MONOCHROME_TRACKS_API_URL wins outright. Otherwise the address
    is discovered from the web client and remembered for a few hours; if
    discovery fails, the last known address is used, since a broken discovery
    is far more likely than the API having moved at that exact moment.
    """
    global _discovered
    if MONOCHROME_TRACKS_API_URL:
        return MONOCHROME_TRACKS_API_URL.rstrip("/")
    with _discovery_lock:
        if _discovered and time.monotonic() - _discovered[1] < MONOCHROME_TRACKS_DISCOVERY_TTL:
            return _discovered[0]
        try:
            base = _discover_api_base()
        except Exception as exc:
            base = _discovered[0] if _discovered else MONOCHROME_TRACKS_API_FALLBACK
            print(f"Monochrome: tracks API discovery failed ({exc}); using {base}")
        _discovered = (base, time.monotonic())
        return base


def search_tracks(query: str, limit: int = 25) -> list[dict]:
    """Raw track search. Raises on transport trouble so callers can tell it from a miss.

    The API throws the odd lone 502 between perfectly good answers, and one of
    those landing on a health probe would park Monochrome for everybody. So,
    like Monochrome's own player, a gateway error gets two quick retries.
    """
    for attempt in range(3):
        resp = httpx.get(
            f"{api_base()}/search/tracks",
            params={"q": query, "limit": max(1, min(limit, 50))},
            headers=_HEADERS,
            timeout=TIMEOUT_MONOCHROME_SEARCH,
            follow_redirects=True,
        )
        if resp.status_code not in _RETRY_STATUSES or attempt == 2:
            break
        time.sleep(0.2 * (attempt + 1))
    resp.raise_for_status()
    body = resp.json()
    tracks = body.get("tracks") if isinstance(body, dict) else None
    return tracks if isinstance(tracks, list) else []


def _query_variants(artist: str, title: str) -> list[str]:
    """Artist and title first, then the title alone for a catalogue that files
    the artist differently (featured credits, "The", ampersands and so on)."""
    def tidy(text: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[\W_]+", " ", text or "", flags=re.UNICODE)).strip()

    variants = []
    for q in (tidy(f"{artist} {title}"), tidy(title)):
        if q and q.lower() not in (v.lower() for v in variants):
            variants.append(q)
    return variants


def track_id(track: dict) -> str:
    """The id /track/{id} wants: `trackId` in release listings, `id` in searches."""
    value = str(track.get("trackId") or track.get("id") or "").strip()
    return value if value.isdigit() else ""


def find_track(isrc: str, artist: str, title: str) -> dict | None:
    """Return the catalogue entry for this exact recording, or None if not stocked.

    Only an exact ISRC match counts. A close title from a different pressing is
    how you end up with the live version, and the rest of the ladder (the
    Deezer ISRC rescue in particular) exists to cope with a genuine miss.
    Raises on transport trouble, so "not stocked" and "could not ask" stay
    distinguishable.
    """
    isrc = (isrc or "").strip().upper()
    if not (isrc and title):
        return None
    for query in _query_variants(artist, title):
        for track in search_tracks(query):
            if (track.get("isrc") or "").strip().upper() != isrc:
                continue
            if track.get("playable") is False or not track_id(track):
                continue
            return track
    return None


def resolve_stream_url(isrc: str, artist: str, title: str) -> str | None:
    """Return the FLAC URL for this exact recording, or None if it is not stocked."""
    track = find_track(isrc, artist, title)
    return f"{api_base()}/track/{track_id(track)}" if track else None


def health() -> tuple[bool, str]:
    """Cheap live probe: one small search that should always find something."""
    try:
        base = api_base()
        tracks = search_tracks("Radiohead Creep", 1)
    except Exception as exc:
        return False, f"Monochrome tracks API unreachable: {type(exc).__name__}: {exc}"
    if not tracks:
        return False, f"Monochrome tracks API at {base} answered but found nothing"
    return True, f"Monochrome tracks API answering at {base}"
