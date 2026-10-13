"""
MusicGrabber - Beatport playlist/chart fetching

Beatport is a Next.js app, so every page ships its data inside a
<script id="__NEXT_DATA__"> JSON blob. We grab that, walk the tree for
objects that look like tracks (name + artists array), and call it a day.

Supports:
  https://www.beatport.com/top-100
  https://www.beatport.com/genre/techno/6/top-100
  https://www.beatport.com/chart/some-chart-name/12345
"""

import json
import re
import urllib.error
import urllib.request

from fastapi import HTTPException

from constants import TIMEOUT_HTTP_SPOTIFY

_TIMEOUT = TIMEOUT_HTTP_SPOTIFY
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}


def fetch_beatport_playlist(url: str) -> dict:
    """Fetch tracks from a Beatport chart or Top 100 URL.

    Returns dict with: tracks (list of "Artist - Title"), playlist_name, count.
    """
    print(f"[Beatport] Fetching: {url}")
    html = _fetch_html(url)
    playlist_name = _extract_page_title(html, url)
    tracks = _extract_tracks(html)

    if not tracks:
        raise HTTPException(
            status_code=422,
            detail=(
                "No tracks found on that Beatport page. "
                "It may be empty, region-locked, or the page structure has changed."
            ),
        )

    print(f"[Beatport] Found {len(tracks)} tracks from '{playlist_name}'")
    return {"tracks": tracks, "playlist_name": playlist_name, "count": len(tracks)}


def _fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Beatport returned HTTP {exc.code}. The page may not be publicly accessible.",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch Beatport page: {exc}")


def fetch_beatport_release_info(url: str) -> dict:
    """Fetch just the artist and release title for a Beatport /release/ URL.

    Deliberately targeted rather than reusing _extract_tracks: a release page
    also carries a "recommendations" block, and the generic tree-walk happily
    hoovers that up too, so a one-track single comes back claiming 37 tracks by
    half the label's roster. The release's own object is right there under its
    own query key, so we take it from the horse's mouth.

    Returns {"artist": str|None, "album": str|None}.
    """
    print(f"[Beatport] Fetching release: {url}")
    html = _fetch_html(url)

    match = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL
    )
    if not match:
        return {"artist": None, "album": None}

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {"artist": None, "album": None}

    try:
        queries = data["props"]["pageProps"]["dehydratedState"]["queries"]
    except (KeyError, TypeError):
        return {"artist": None, "album": None}

    for query in queries:
        key = query.get("queryKey")
        # The release's own entry is keyed ["release-<id>"]; its recommendations
        # sit under ["release-<id>-recommendations"], which we want no part of.
        if not (isinstance(key, list) and len(key) == 1
                and isinstance(key[0], str)
                and key[0].startswith("release-")
                and not key[0].endswith("-recommendations")):
            continue

        release = (query.get("state") or {}).get("data")
        if not isinstance(release, dict):
            continue

        album = (release.get("name") or "").strip() or None
        artists = release.get("artists") or []
        artist = None
        if isinstance(artists, list) and artists:
            first = artists[0]
            if isinstance(first, dict):
                artist = (first.get("name") or "").strip() or None
        return {"artist": artist, "album": album}

    return {"artist": None, "album": None}


def _extract_page_title(html: str, url: str = "") -> str:
    # For top-100 style URLs, derive a clean name from the URL itself rather than
    # trusting og:title, which Beatport fills with SEO marketing guff.
    if url:
        genre_top100 = re.search(
            r'beatport\.com/genre/([^/]+)/\d+/top-100', url, re.IGNORECASE
        )
        if genre_top100:
            genre = genre_top100.group(1).replace("-", " ").title()
            return f"{genre} Top 100"

        if re.search(r'beatport\.com/top-100', url, re.IGNORECASE):
            return "Beatport Top 100"

    # For named charts, og:title has the actual chart name
    og = re.search(r'property="og:title"\s+content="([^"]+)"', html)
    if og:
        title = og.group(1).strip()
        title = re.sub(r"\s*\|\s*Beatport$", "", title, flags=re.IGNORECASE).strip()
        if title:
            return title

    title_tag = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    if title_tag:
        title = title_tag.group(1).strip()
        title = re.sub(r"\s*\|\s*Beatport$", "", title, flags=re.IGNORECASE).strip()
        if title:
            return title

    return "Beatport Chart"


def _extract_tracks(html: str) -> list[str]:
    # Pull the __NEXT_DATA__ JSON blob that Next.js embeds on every page
    match = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL
    )
    if not match:
        return []

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []

    seen: list[str] = []
    _walk(data, seen, depth=0)
    return seen


def _walk(obj, seen: list[str], depth: int) -> None:
    """Recursively hunt for Beatport track objects: {name: str, artists: [{name: str}, ...]}."""
    if depth > 25:
        return

    if isinstance(obj, dict):
        name = obj.get("name")
        artists = obj.get("artists")

        if (
            isinstance(name, str)
            and name.strip()
            and isinstance(artists, list)
            and artists
            and isinstance(artists[0], dict)
            and isinstance(artists[0].get("name"), str)
        ):
            artist_str = ", ".join(
                a["name"].strip() for a in artists if isinstance(a.get("name"), str) and a["name"].strip()
            )
            title = name.strip()
            # Beatport often has a mix_name like "Original Mix" - skip it, MusicBrainz
            # won't care and search results are cleaner without it
            if artist_str and title:
                entry = f"{artist_str} - {title}"
                if entry not in seen:
                    seen.append(entry)

        for v in obj.values():
            _walk(v, seen, depth + 1)

    elif isinstance(obj, list):
        for item in obj:
            _walk(item, seen, depth + 1)
