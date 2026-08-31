"""
MusicGrabber - Streaming album URL resolution

Detects whether a pasted URL points at a whole ALBUM, as opposed to a
playlist or a single track, on the services MusicGrabber already knows how to
scrape, then turns that into a MusicBrainz match ready for the album download
pipeline (or a confirmation prompt, if the match isn't confident enough).

Every service here borrows its existing scraper rather than talking to the
service directly:
  - Spotify's playlist/album embed parser lives in watched_playlists.py
    (it already treats a Spotify /album/ URL as a "playlist" of tracks for
    watching purposes; we just ask it what's on the album).
  - Apple Music's playlist/album fetcher lives in apple.py and already
    supports /album/ URLs natively.
  - YouTube Music albums are the auto-generated OLAK5uy_ playlists, read with
    the same yt-dlp flat-playlist call the ordinary playlist path uses.
  - Beatport /release/ pages and Tidal (and Monochrome-hosted Tidal) /album/
    pages have small dedicated readers in beatport.py and tidal.py.

Note that all any of these has to produce is an artist and an album title.
The real tracklist comes from MusicBrainz once the album is matched, so a
scraper that can name the album has done its whole job here.

Amazon Music album pages are the odd one out and need their own browser.
Everything else here borrows a scraper that already worked; Amazon's album
pages hand Playwright twenty <music-image-row> skeletons that never populate,
fire no JSON carrying the record, and keep nothing useful in window.amznMusic.
Under SeleniumBase's undetected mode, however, the page titles itself
"Play <ALBUM> by <ARTIST> on Amazon Music Unlimited", which is the whole job.
That lives in amazonalbum.py; see docs/requests and bugs.md for the list of
approaches that did not work, so nobody re-runs them.
"""

import re

from apple import fetch_apple_music_playlist
from matching import parse_query
from metadata import match_album_to_musicbrainz


_SPOTIFY_ALBUM_RE = re.compile(
    r'^https?://open\.spotify\.com/album/([A-Za-z0-9]+)(?:[/?#].*)?$'
)
# Apple Music album URLs are /<storefront>/album/<slug>/<numeric id>, e.g.
# https://music.apple.com/gb/album/ok-computer/1097864980. A trailing
# "?i=<track id>" deep-links one track within the album page; the URL still
# names the whole album, so it stays recognised.
_APPLE_ALBUM_RE = re.compile(
    r'^https?://music\.apple\.com/[a-z]{2}/album/[^/?#]+/(\d+)(?:[/?#].*)?$',
    re.IGNORECASE,
)
# YouTube's auto-generated album playlists are the ones prefixed OLAK5uy_, on
# either the music. or www. host. That prefix is the whole signal: an ordinary
# user playlist (PL...) or a mix (RD...) is not an album and must not be
# treated as one. Album *browse* URLs (music.youtube.com/browse/MPREb_...) are
# not matched, because yt-dlp cannot read that shape.
_YOUTUBE_ALBUM_RE = re.compile(
    r'^https?://(?:music|www)\.youtube\.com/(?:playlist|watch)\?'
    r'(?:[^#]*&)?list=(OLAK5uy_[A-Za-z0-9_-]+)(?:[&#].*)?$',
    re.IGNORECASE,
)
# Beatport calls an album a "release": /release/<slug>/<numeric id>.
_BEATPORT_RELEASE_RE = re.compile(
    r'^https?://(?:www\.)?beatport\.com/release/[^/?#]+/(\d+)(?:[/?#].*)?$',
    re.IGNORECASE,
)
# Tidal albums, both tidal.com/album/<id> and tidal.com/browse/album/<id>,
# mirroring how the playlist path accepts both shapes.
_TIDAL_ALBUM_RE = re.compile(
    r'^https?://(?:www\.)?tidal\.com/(?:browse/)?album/(\d+)(?:[/?#].*)?$',
    re.IGNORECASE,
)
# Amazon Music albums are /albums/<ASIN> on any regional TLD. A playlist lives
# at /playlists/ or /user-playlists/ and must not be caught here.
_AMAZON_ALBUM_RE = re.compile(
    r'^https?://music\.amazon\.[a-z.]+/albums/([A-Z0-9]{10})(?:[/?#].*)?$',
    re.IGNORECASE,
)
# Monochrome album shares are Tidal album ids behind Monochrome's own host,
# exactly as its playlist shares are Tidal playlist UUIDs.
_MONOCHROME_ALBUM_RE = re.compile(
    r'^https?://(?:www\.)?(?:monochrome\.tf|monochrome\.samidy\.com)/album/(\d+)(?:[/?#].*)?$',
    re.IGNORECASE,
)


def _first_track_artist(tracks: list[str]) -> str | None:
    """Best-guess album artist: the artist half of the first scraped track.

    Good enough for the overwhelming majority of albums, which have a single
    artist. A genuine Various Artists compilation just comes back with
    whichever act happens to sit first in the tracklist; that's fine because
    match_album_to_musicbrainz already treats a "Various Artists" credit as
    legitimate rather than something to gate on, and the album title carries
    most of the matching weight anyway.
    """
    if not tracks:
        return None
    artist, _ = parse_query(tracks[0])
    return artist or None


def parse_album_url(url: str) -> dict | None:
    """Identify an ALBUM url on a supported service and scrape its artist/album.

    Returns {"service": ..., "id": ..., "artist": ..., "album": ...} when the
    url matches a known album shape, where service is one of spotify, apple,
    youtube, beatport, tidal or monochrome. artist/album are filled in on a
    best-effort basis: a scrape failure (dead link, private album, service
    hiccup) leaves them as None but the url is still "recognised", since that
    judgement is a pure pattern match and does not depend on the network call
    succeeding.

    Returns None when the url is not a recognised album url at all, including
    a *playlist* or *track* url from those same services; those are a different
    job (Bulk Import's ordinary playlist path already handles them).
    """
    url = (url or "").strip()
    if not url:
        return None

    spotify_match = _SPOTIFY_ALBUM_RE.match(url)
    if spotify_match:
        return _parse_spotify_album(url, spotify_match.group(1))

    apple_match = _APPLE_ALBUM_RE.match(url)
    if apple_match:
        return _parse_apple_album(url, apple_match.group(1))

    youtube_match = _YOUTUBE_ALBUM_RE.match(url)
    if youtube_match:
        return _parse_youtube_album(youtube_match.group(1))

    beatport_match = _BEATPORT_RELEASE_RE.match(url)
    if beatport_match:
        return _parse_beatport_release(url, beatport_match.group(1))

    tidal_match = _TIDAL_ALBUM_RE.match(url)
    if tidal_match:
        return _parse_tidal_album("tidal", tidal_match.group(1))

    monochrome_match = _MONOCHROME_ALBUM_RE.match(url)
    if monochrome_match:
        return _parse_tidal_album("monochrome", monochrome_match.group(1))

    amazon_match = _AMAZON_ALBUM_RE.match(url)
    if amazon_match:
        return _parse_amazon_album(url, amazon_match.group(1).upper())

    return None


def _parse_spotify_album(url: str, spotify_id: str) -> dict:
    # Lazy import: watched_playlists.py pulls in the whole download/db stack,
    # and this module has no other reason to load any of that at import time.
    from watched_playlists import _fetch_spotify_playlist_embed

    artist, album = None, None
    try:
        result = _fetch_spotify_playlist_embed(url)
        album = result.get("playlist_name") or None
        artist = _first_track_artist(result.get("tracks") or [])
    except Exception as exc:
        print(f"Spotify album scrape failed for {url}: {exc}")

    return {"service": "spotify", "id": spotify_id, "artist": artist, "album": album}


def _parse_apple_album(url: str, apple_id: str) -> dict:
    artist, album = None, None
    try:
        result = fetch_apple_music_playlist(url)
        album = result.get("playlist_name") or None
        artist = _first_track_artist(result.get("tracks") or [])
    except Exception as exc:
        print(f"Apple Music album scrape failed for {url}: {exc}")

    return {"service": "apple", "id": apple_id, "artist": artist, "album": album}


def _parse_youtube_album(playlist_id: str) -> dict:
    """Read a YouTube Music OLAK5uy_ album playlist's title and credited artist.

    Lazy import for the same reason as Spotify above: youtube.py drags in the
    download stack, and this module has no other use for it.

    The playlist title is the album name on these auto-generated lists. The
    artist comes from the first entry's uploader, which on an album playlist is
    the artist's (usually "- Topic") channel rather than a random uploader.
    """
    import json
    from constants import TIMEOUT_YTDLP_PLAYLIST
    from youtube import _ytdlp_base_args, run_ytdlp_with_retries

    artist, album = None, None
    try:
        cmd = [
            "yt-dlp",
            *_ytdlp_base_args(),
            "--dump-single-json",
            "--flat-playlist",
            "--no-warnings",
            f"https://music.youtube.com/playlist?list={playlist_id}",
        ]
        result, timed_out = run_ytdlp_with_retries(
            cmd,
            TIMEOUT_YTDLP_PLAYLIST,
            operation=f"YouTube album metadata lookup for {playlist_id}",
        )
        if not timed_out and result is not None and result.returncode == 0:
            data = json.loads(result.stdout)
            album = (data.get("title") or "").strip() or None
            entries = data.get("entries") or []
            if entries:
                first = entries[0] or {}
                artist = (first.get("uploader") or first.get("channel") or "").strip() or None
            # "Album - Topic" channels are YouTube's own plumbing, not a credit.
            if artist:
                artist = re.sub(r'\s*-\s*Topic$', '', artist).strip() or None
    except Exception as exc:
        print(f"YouTube album lookup failed for {playlist_id}: {exc}")

    return {"service": "youtube", "id": playlist_id, "artist": artist, "album": album}


def _parse_beatport_release(url: str, release_id: str) -> dict:
    from beatport import fetch_beatport_release_info

    artist, album = None, None
    try:
        info = fetch_beatport_release_info(url)
        artist, album = info.get("artist"), info.get("album")
    except Exception as exc:
        print(f"Beatport release scrape failed for {url}: {exc}")

    return {"service": "beatport", "id": release_id, "artist": artist, "album": album}


def _parse_tidal_album(service: str, album_id: str) -> dict:
    """Tidal and Monochrome share this: a Monochrome album share is a Tidal
    album id on a different host, so both resolve through the same embed."""
    from tidal import fetch_tidal_album_info

    artist, album = None, None
    try:
        info = fetch_tidal_album_info(album_id)
        artist, album = info.get("artist"), info.get("album")
    except Exception as exc:
        print(f"Tidal album scrape failed for album {album_id}: {exc}")

    return {"service": service, "id": album_id, "artist": artist, "album": album}


def _parse_amazon_album(url: str, asin: str) -> dict:
    """Name an Amazon album by driving SeleniumBase at its page title.

    The slowest reader here by some way, since it starts a real browser and
    waits out a consent dialog, so it deliberately sits last in the dispatch
    order where the cheap pattern matches have already had their go.
    """
    from amazon import fetch_amazon_album_info

    artist, album = None, None
    try:
        info = fetch_amazon_album_info(url)
        artist, album = info.get("artist"), info.get("album")
    except Exception as exc:
        print(f"Amazon album scrape failed for {url}: {exc}")

    return {"service": "amazon", "id": asin, "artist": artist, "album": album}


def resolve_album_url(url: str) -> dict:
    """Resolve a streaming album url all the way to a MusicBrainz match.

    Combines parse_album_url with metadata.match_album_to_musicbrainz so a
    caller can go straight from a pasted url to either the album download
    pipeline (confident match) or a confirmation prompt (not confident).

    Returns:
        {
            "recognised": bool,   # False: not a recognised album url at all
            "service": "spotify"|"apple"|"youtube"|"beatport"|"tidal"|"monochrome"|None,
            "artist": str | None,  # as scraped from the streaming service
            "album": str | None,
            "match": dict | None,  # best MusicBrainz release-group candidate
            "confidence": float,
            "confident": bool,     # True -> caller may proceed to the album pipeline
            "candidates": list,    # offer these when not confident
        }

    Raises MusicBrainzUnavailable (from metadata.py) when MusicBrainz itself
    cannot be reached, so the caller can show a Retry rather than a false
    "no match found".
    """
    parsed = parse_album_url(url)
    if parsed is None:
        return {
            "recognised": False, "service": None, "artist": None, "album": None,
            "match": None, "confidence": 0.0, "confident": False, "candidates": [],
        }

    artist, album = parsed.get("artist"), parsed.get("album")
    if not album:
        # Recognised the url shape but couldn't scrape anything off it; no
        # album title means nothing sensible to match against.
        return {
            "recognised": True, "service": parsed["service"],
            "artist": artist, "album": album,
            "match": None, "confidence": 0.0, "confident": False, "candidates": [],
        }

    match_result = match_album_to_musicbrainz(artist or "", album)
    return {
        "recognised": True,
        "service": parsed["service"],
        "artist": artist,
        "album": album,
        **match_result,
    }
