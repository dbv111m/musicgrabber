"""
MusicGrabber - Tidal playlist fetching

Tidal's main web app is a client-side SPA with no useful SSR data. The embed
player (embed.tidal.com/playlists/UUID) server-renders its tracks as custom
<list-item> web components, which was lovely right up until Tidal capped it at
the first 50; a 204-track playlist came back as 50 and looked perfectly healthy.

So playlists now come from the public API at api.tidal.com, paginated properly,
using the token the embed's own JS bundle carries around with it. The embed
scrape stays on as a fallback, because 50 tracks beats nothing if the API sulks.
Album lookups still use the embed; they only want two names, and it still has those.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from fastapi import HTTPException

from constants import (
    TIDAL_API_BASE,
    TIDAL_API_COUNTRY,
    TIDAL_API_PAGE_SIZE,
    TIDAL_API_TOKEN,
    TIMEOUT_HTTP_SPOTIFY,
)

_TIMEOUT = TIMEOUT_HTTP_SPOTIFY
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}
_EMBED_URL = "https://embed.tidal.com/playlists/{uuid}"
_ALBUM_EMBED_URL = "https://embed.tidal.com/albums/{album_id}"
_EMBED_ORIGIN = "https://embed.tidal.com"

# The live API token, seeded from constants and swapped in place if Tidal ever
# rotates theirs and greets us with a 401. A plain rebind is atomic enough for
# our daemon threads; worst case two of them scrape the same token at once.
_api_token = TIDAL_API_TOKEN


class _ApiUnavailable(Exception):
    """The API path did not work out; fall back to scraping the embed."""


def fetch_tidal_album_info(album_id: str) -> dict:
    """Fetch just the artist and album title for a Tidal album, via the embed player.

    Only the two names, deliberately: this feeds the album-URL resolver, which
    hands them to MusicBrainz and takes the real tracklist from there. The album
    embed does render its tracks, but with empty artist slots (every track on an
    album shares the credit shown once at the top), so scraping them would give
    us a list of titles with nobody attached to them. Not worth the bother when
    MusicBrainz is about to supply a better tracklist anyway.

    Returns {"artist": str|None, "album": str|None}.
    """
    url = _ALBUM_EMBED_URL.format(album_id=album_id)
    print(f"[Tidal] Fetching album embed: {url}")
    html = _fetch_html(url)
    return {"artist": _extract_album_artist(html), "album": _extract_album_title(html)}


def _extract_album_title(html: str) -> str | None:
    # <h1 ...><a href="https://tidal.com/album/77640617" ...>Achtung Baby</a></h1>
    match = re.search(r'<h1[^>]*>\s*<a[^>]*>([^<]+)</a>', html)
    if match:
        return match.group(1).strip() or None

    # Older/alternative shape: <h1 class="media-album" title="Album: Achtung Baby">
    match = re.search(r'<h1[^>]+class="media-album"[^>]+title="Album:\s*([^"]+)"', html)
    return match.group(1).strip() if match else None


def _extract_album_artist(html: str) -> str | None:
    # <span class="media-artist" title="Artist: U2"><a ...>U2</a></span>
    match = re.search(r'class="media-artist"[^>]*title="Artist:\s*([^"]+)"', html)
    if match:
        return match.group(1).strip() or None

    match = re.search(r'class="media-artist"[^>]*>\s*(?:<a[^>]*>)?([^<]+)<', html)
    return match.group(1).strip() if match else None


def fetch_tidal_playlist(uuid: str) -> dict:
    """Fetch tracks from a Tidal playlist: API first, embed scrape as backup.

    Returns dict with: tracks (list of "Artist - Title"), playlist_name, count.
    """
    try:
        print(f"[Tidal] Fetching playlist {uuid} from the API")
        tracks, playlist_name = _fetch_playlist_via_api(uuid)
    except _ApiUnavailable as exc:
        print(
            f"[Tidal] API unavailable ({exc}); falling back to the embed scrape, "
            f"which only ever shows the first 50 tracks"
        )
        html = _fetch_html(_EMBED_URL.format(uuid=uuid))
        playlist_name = _extract_playlist_name(html)
        tracks = _extract_tracks(html)

    if not tracks:
        raise HTTPException(
            status_code=422,
            detail=(
                "No tracks found in that Tidal playlist. "
                "It may be empty, private, or the embed structure has changed."
            ),
        )

    print(f"[Tidal] Found {len(tracks)} tracks from '{playlist_name}'")
    return {"tracks": tracks, "playlist_name": playlist_name, "count": len(tracks)}


def _fetch_playlist_via_api(uuid: str) -> tuple[list[str], str]:
    """Read a whole playlist from api.tidal.com, 100 tracks at a time."""
    meta = _api_get(f"/playlists/{uuid}", {"countryCode": TIDAL_API_COUNTRY}, uuid)
    playlist_name = (meta.get("title") or "").strip() or "Tidal Playlist"
    expected = meta.get("numberOfTracks") or 0

    tracks: list[str] = []
    offset = 0
    while True:
        page = _api_get(
            f"/playlists/{uuid}/items",
            {
                "countryCode": TIDAL_API_COUNTRY,
                "offset": offset,
                "limit": TIDAL_API_PAGE_SIZE,
            },
            uuid,
        )
        items = page.get("items") or []
        if not items:
            break

        for wrapper in items:
            # Playlists can hold videos as well; we are only after the audio.
            if wrapper.get("type") != "track":
                continue
            item = wrapper.get("item") or {}
            artist = ((item.get("artist") or {}).get("name") or "").strip()
            title = _track_title(item)
            if artist and title:
                tracks.append(f"{artist} - {title}")

        offset += len(items)
        if offset >= (page.get("totalNumberOfItems") or 0):
            break

    # Silent truncation is the very bug we are fixing, so if the sums stop
    # adding up again we would rather hear about it in the logs.
    if expected and len(tracks) < expected:
        print(
            f"[Tidal] Playlist reports {expected} tracks but we collected {len(tracks)}; "
            f"some may be unavailable or missing artist/title"
        )

    return tracks, playlist_name


def _track_title(item: dict) -> str:
    """Rebuild the title exactly as the embed used to render it.

    The embed baked the version into its title slot ("Enter Sandman (Remastered
    2021)") while the API keeps the two apart. Watched playlists hash these
    strings, so drifting here would look like a pile of tracks leaving and an
    identical pile arriving. Some titles already carry their version inline, so
    take care not to say it twice.
    """
    title = (item.get("title") or "").strip()
    version = (item.get("version") or "").strip()
    if version and version.lower() not in title.lower():
        title = f"{title} ({version})"
    return title


def _api_get(path: str, params: dict, probe_uuid: str) -> dict:
    """GET from Tidal's public API, refreshing the token once on a 401.

    probe_uuid exists purely to give a token re-scrape an embed page to read:
    any real embed page carries the JS bundle, the bare embed root does not.
    """
    global _api_token

    url = f"{TIDAL_API_BASE}{path}?{urllib.parse.urlencode(params)}"
    for attempt in (1, 2):
        token = _api_token
        req = urllib.request.Request(url, headers={**_HEADERS, "X-Tidal-Token": token})
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", errors="ignore"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 1:
                fresh = _scrape_api_token(probe_uuid)
                if fresh and fresh != token:
                    print("[Tidal] API token had rotated; picked up the new one from the embed bundle")
                    _api_token = fresh
                    continue
            raise _ApiUnavailable(f"HTTP {exc.code} from {path}")
        except Exception as exc:
            raise _ApiUnavailable(f"{type(exc).__name__}: {exc}")

    raise _ApiUnavailable(f"401 from {path}, and no fresher token to try")


def _scrape_api_token(uuid: str) -> str | None:
    """Dig the current API token out of the embed player's JS bundle.

    Only ever called when our stored token earns a 401, so the happy path pays
    nothing for this bit of paranoia.
    """
    try:
        html = _fetch_html(_EMBED_URL.format(uuid=uuid))
        bundle = re.search(r'<script[^>]+src="(/embed-resources/js/[^"]+\.js)"', html)
        if not bundle:
            return None
        js = _fetch_html(_EMBED_ORIGIN + bundle.group(1))
        match = re.search(r'X-Tidal-Token"\s*,\s*"([A-Za-z0-9_-]+)"', js)
        return match.group(1) if match else None
    except Exception as exc:
        print(f"[Tidal] Could not re-scrape the API token: {exc}")
        return None


def _fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Tidal embed returned HTTP {exc.code}. The playlist may be private or deleted.",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch Tidal embed page: {exc}")


def _extract_playlist_name(html: str) -> str:
    # <h1 class="media-album" title="Album: Indie Party Hits">
    match = re.search(r'<h1[^>]+class="media-album"[^>]+title="Album:\s*([^"]+)"', html)
    if match:
        return match.group(1).strip()

    # Fallback: og:title or <title>
    og = re.search(r'property="og:title"\s+content="([^"]+)"', html)
    if og:
        return og.group(1).strip()

    return "Tidal Playlist"


def _extract_tracks(html: str) -> list[str]:
    # Each track is wrapped in a <list-item product-type="track"> block
    blocks = re.findall(
        r'<list-item[^>]*product-type="track"[^>]*>(.*?)</list-item>',
        html,
        re.DOTALL,
    )

    tracks = []
    for block in blocks:
        title_m = re.search(r'slot="title">([^<]+)<', block)
        # Artist may be a plain text node or wrapped in an <a> tag
        artist_m = re.search(r'slot="artist">(?:<[^>]+>)?([^<]+)<', block)
        if title_m and artist_m:
            title = title_m.group(1).strip()
            artist = artist_m.group(1).strip()
            if artist and title:
                tracks.append(f"{artist} - {title}")

    return tracks
