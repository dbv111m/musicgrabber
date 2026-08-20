"""
MusicGrabber - Tidal playlist fetching

Tidal's main web app is a client-side SPA with no useful SSR data.
The embed player (embed.tidal.com/playlists/UUID) is a different story:
it server-renders the full track list as custom <list-item> web components.
We fetch that, scrape the slots, and call it done.
"""

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
_EMBED_URL = "https://embed.tidal.com/playlists/{uuid}"
_ALBUM_EMBED_URL = "https://embed.tidal.com/albums/{album_id}"


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
    """Fetch tracks from a Tidal playlist via the embed player.

    Returns dict with: tracks (list of "Artist - Title"), playlist_name, count.
    """
    url = _EMBED_URL.format(uuid=uuid)
    print(f"[Tidal] Fetching embed: {url}")
    html = _fetch_html(url)
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
