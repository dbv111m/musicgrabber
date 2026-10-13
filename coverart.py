"""
MusicGrabber - Cover Art Sourcing

Tries really hard to find proper album artwork for every downloaded track.
Fallback chain: Cover Art Archive, iTunes, Deezer.
If all three strike out, the yt-dlp video thumbnail is preserved as a
silent last resort (apply_metadata_to_file leaves existing pictures
alone when album_art_bytes is None).
"""

import threading
from pathlib import Path

import httpx

from constants import (
    COVER_ART_TIMEOUT,
    ITUNES_SEARCH_URL,
    DEEZER_SEARCH_URL,
    TIMEOUT_HTTP_REQUEST,
)
from utils import set_file_permissions


# ── Thread-safe caches ──────────────────────────────────────────────────
# MBID cache: keyed by MusicBrainz release MBID (exact, authoritative)
_MBID_CACHE: dict[str, tuple[bytes, str] | None] = {}
_MBID_CACHE_LOCK = threading.Lock()

# Search cache: keyed by (artist_lower, title_lower) for iTunes/Deezer
_SEARCH_CACHE: dict[tuple[str, str], tuple[bytes, str] | None] = {}
_SEARCH_CACHE_LOCK = threading.Lock()

# URL-only cache for search result thumbnails (just the remote URL, no download).
# The lookup kind is part of the key: an album and a song can quite reasonably
# share a title while needing different Deezer endpoints and different artwork.
_URL_CACHE: dict[tuple[str, str, str], str | None] = {}
_URL_CACHE_LOCK = threading.Lock()


# ── Helpers ─────────────────────────────────────────────────────────────

def _guess_cover_mime(data: bytes, content_type: str | None = None) -> str:
    """Best-effort cover MIME detection for tag embedding."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in ("image/jpeg", "image/jpg", "image/png"):
        return "image/jpeg" if ct == "image/jpg" else ct
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return "image/jpeg"


# ── Individual sources ──────────────────────────────────────────────────

def _fetch_caa_cover(release_mbid: str) -> tuple[bytes, str] | None:
    """Fetch album front art from Cover Art Archive for a MusicBrainz release.

    The gold standard: authoritative, high-quality, keyed by exact MBID.
    Tries the 500px version first, falls back to full-size.
    """
    mbid = (release_mbid or "").strip()
    if not mbid:
        return None
    urls = (
        f"https://coverartarchive.org/release/{mbid}/front-500",
        f"https://coverartarchive.org/release/{mbid}/front",
    )
    for url in urls:
        try:
            resp = httpx.get(url, timeout=TIMEOUT_HTTP_REQUEST, follow_redirects=True)
            if resp.status_code == 200 and resp.content:
                mime = _guess_cover_mime(resp.content, resp.headers.get("content-type"))
                return resp.content, mime
        except Exception:
            continue
    return None


def _fetch_itunes_cover(artist: str, title: str) -> tuple[bytes, str] | None:
    """Try the iTunes Search API for cover art. Free, no auth required.

    Apple's catalogue is enormous and the artwork is consistently good quality.
    We scale the default 100x100 thumbnail up to 600x600.
    """
    try:
        resp = httpx.get(
            ITUNES_SEARCH_URL,
            params={"term": f"{artist} {title}", "media": "music", "limit": 5},
            timeout=COVER_ART_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        results = resp.json().get("results", [])
        if not results:
            return None
        # First result is usually bang on for exact artist+title queries
        artwork_url = results[0].get("artworkUrl100", "")
        if not artwork_url:
            return None
        # Scale from the thumbnail to something worth embedding
        artwork_url = artwork_url.replace("100x100", "600x600")
        img_resp = httpx.get(artwork_url, timeout=COVER_ART_TIMEOUT, follow_redirects=True)
        if img_resp.status_code == 200 and img_resp.content:
            mime = _guess_cover_mime(img_resp.content, img_resp.headers.get("content-type"))
            return img_resp.content, mime
    except Exception:
        pass
    return None


def _fetch_deezer_cover(artist: str, title: str) -> tuple[bytes, str] | None:
    """Try the Deezer API for cover art. Free, no auth required.

    The backup plan when iTunes doesn't have it. Returns 500x500 album covers.
    """
    try:
        resp = httpx.get(
            DEEZER_SEARCH_URL,
            params={"q": f'artist:"{artist}" track:"{title}"'},
            timeout=COVER_ART_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        results = resp.json().get("data", [])
        if not results:
            return None
        cover_url = results[0].get("album", {}).get("cover_big", "")
        if not cover_url:
            return None
        img_resp = httpx.get(cover_url, timeout=COVER_ART_TIMEOUT, follow_redirects=True)
        if img_resp.status_code == 200 and img_resp.content:
            mime = _guess_cover_mime(img_resp.content, img_resp.headers.get("content-type"))
            return img_resp.content, mime
    except Exception:
        pass
    return None


# ── Public API ──────────────────────────────────────────────────────────

def fetch_cover_art(
    artist: str,
    title: str,
    release_mbid: str | None = None,
) -> tuple[bytes, str] | None:
    """Try really hard to find cover art for a track.

    Fallback chain: Cover Art Archive, iTunes, Deezer.
    Returns (image_bytes, mime_type) or None if everything strikes out.
    Results are cached so repeated calls for the same track are cheap.
    """
    # ── 1. Cover Art Archive (authoritative, keyed by release MBID) ──
    mbid = (release_mbid or "").strip()
    if mbid:
        with _MBID_CACHE_LOCK:
            if mbid in _MBID_CACHE:
                cached = _MBID_CACHE[mbid]
                if cached:
                    return cached
                # None = already tried and failed; fall through to other sources

        if mbid not in _MBID_CACHE:
            result = _fetch_caa_cover(mbid)
            with _MBID_CACHE_LOCK:
                _MBID_CACHE[mbid] = result
            if result:
                print(f"Cover art: found via Cover Art Archive for release {mbid}")
                return result

    # ── 2 & 3. Search-based fallback (iTunes, then Deezer) ──
    cache_key = (artist.lower().strip(), title.lower().strip())
    with _SEARCH_CACHE_LOCK:
        if cache_key in _SEARCH_CACHE:
            return _SEARCH_CACHE[cache_key]

    for label, fetcher in (("iTunes", _fetch_itunes_cover), ("Deezer", _fetch_deezer_cover)):
        result = fetcher(artist, title)
        if result:
            print(f"Cover art: found via {label} for {artist} - {title}")
            with _SEARCH_CACHE_LOCK:
                _SEARCH_CACHE[cache_key] = result
            return result

    # Cache the miss so we don't pester iTunes and Deezer again for this track
    print(f"Cover art: no art found for {artist} - {title} (all sources exhausted)")
    with _SEARCH_CACHE_LOCK:
        _SEARCH_CACHE[cache_key] = None
    return None


def get_album_art_context(job_id: str) -> tuple[bytes | None, str | None]:
    """Return cached (cover_bytes, mime) for album-mode jobs.

    Looks up the release MBID from the job's album context (bulk_imports table
    or job row), then fetches from Cover Art Archive with caching.
    """
    release_mbid = _get_album_release_mbid(job_id)
    if not release_mbid:
        return None, None

    with _MBID_CACHE_LOCK:
        if release_mbid in _MBID_CACHE:
            cached = _MBID_CACHE[release_mbid]
            return cached if cached else (None, None)

    fetched = _fetch_caa_cover(release_mbid)
    with _MBID_CACHE_LOCK:
        _MBID_CACHE[release_mbid] = fetched

    return fetched if fetched else (None, None)


def ensure_album_cover_files(
    override_dir: str | None,
    album_art_bytes: bytes | None,
    album_art_mime: str | None,
) -> None:
    """Write cover files for album-mode folders so library scanners can pick artwork."""
    if not override_dir or not album_art_bytes:
        return
    try:
        album_dir = Path(override_dir)
        album_dir.mkdir(parents=True, exist_ok=True)
        ext = ".png" if (album_art_mime or "").lower() == "image/png" else ".jpg"
        for stem in ("cover", "folder"):
            p = album_dir / f"{stem}{ext}"
            if p.exists() and p.stat().st_size > 0:
                continue
            p.write_bytes(album_art_bytes)
            set_file_permissions(p)
    except Exception:
        # Non-fatal; embedded artwork tags are still applied when possible.
        pass


def cache_cover_art(release_mbid: str, art: tuple[bytes, str] | None) -> None:
    """Manually cache a cover art result (used by sidecar fallback in downloads)."""
    with _MBID_CACHE_LOCK:
        _MBID_CACHE[release_mbid] = art


def fetch_cover_art_url(artist: str, title: str, *, album: bool = False) -> str | None:
    """Return a cover art image URL for display in search results.

    Unlike fetch_cover_art(), this returns just a remote URL without downloading
    the image bytes; the browser loads it directly. Tries iTunes then Deezer.
    Album cards use Deezer's album search rather than pretending the album name
    is a track title.
    Results are cached so repeated calls for the same track are free.
    """
    cache_key = ("album" if album else "track", artist.lower().strip(), title.lower().strip())
    with _URL_CACHE_LOCK:
        if cache_key in _URL_CACHE:
            return _URL_CACHE[cache_key]

    deezer_lookup = _deezer_album_artwork_url if album else _deezer_artwork_url
    url = _itunes_artwork_url(artist, title) or deezer_lookup(artist, title)

    with _URL_CACHE_LOCK:
        _URL_CACHE[cache_key] = url
    return url


def _itunes_artwork_url(artist: str, title: str) -> str | None:
    try:
        resp = httpx.get(
            ITUNES_SEARCH_URL,
            params={"term": f"{artist} {title}", "media": "music", "limit": 5},
            timeout=COVER_ART_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        results = resp.json().get("results", [])
        if not results:
            return None
        url = results[0].get("artworkUrl100", "")
        return url.replace("100x100", "600x600") if url else None
    except Exception:
        return None


def _deezer_artwork_url(artist: str, title: str) -> str | None:
    try:
        resp = httpx.get(
            DEEZER_SEARCH_URL,
            params={"q": f'artist:"{artist}" track:"{title}"'},
            timeout=COVER_ART_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        results = resp.json().get("data", [])
        if not results:
            return None
        return results[0].get("album", {}).get("cover_big") or None
    except Exception:
        return None


def _deezer_album_artwork_url(artist: str, album: str) -> str | None:
    """Return artwork from Deezer's album-shaped search response."""
    try:
        resp = httpx.get(
            f"{DEEZER_SEARCH_URL.rstrip('/')}/album",
            params={"q": f'artist:"{artist}" album:"{album}"'},
            timeout=COVER_ART_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        results = resp.json().get("data", [])
        if not results:
            return None
        return results[0].get("cover_big") or None
    except Exception:
        return None


# ── Internal helpers ────────────────────────────────────────────────────

def _get_album_release_mbid(job_id: str) -> str | None:
    """Return MusicBrainz release MBID for album-mode jobs, if available."""
    if not job_id:
        return None

    # Import here to avoid circular dependency
    from db import db_conn

    # Check job-level album context first
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT album_release_mbid FROM jobs WHERE id = ? AND album_release_mbid IS NOT NULL",
                (job_id,),
            ).fetchone()
        if row and (row[0] or "").strip():
            return row[0].strip()
    except Exception:
        pass

    # Fall back to bulk_imports table for tracks queued via album download
    try:
        with db_conn() as conn:
            row = conn.execute(
                """
                SELECT bi.album_release_mbid
                FROM bulk_import_tracks bit
                JOIN bulk_imports bi ON bi.id = bit.import_id
                WHERE bit.job_id = ?
                  AND bi.override_dir IS NOT NULL
                  AND bi.override_dir != ''
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        if not row:
            return None
        mbid = (row[0] or "").strip()
        return mbid or None
    except Exception:
        return None
