"""
MusicGrabber - JioSaavn Source

India's largest streaming service, asked directly through the same api.php its
own web player uses. No account, no token, no Cloudflare. Most tracks come as
320 kbps AAC in an MP4 container, straight off JioSaavn's CDN.

Two things worth knowing before you go looking for bugs that are not bugs:

- JioSaavn filters search by country, and outside India the Western catalogue
  is mostly missing: from the UK, "Nirvana Lithium" finds two karaoke covers
  and no Nirvana. It takes the country from X-Forwarded-For, so search calls
  claim an Indian address and get the whole catalogue. Downloads come off the
  CDN, which never asked. If JioSaavn stops trusting the header, the health
  probe searches for a Western track precisely so it goes red rather than
  quietly serving karaoke.
- Stream links arrive DES-encrypted with a key that has been public for years.
  Decrypting gives a 96 kbps CDN link; swapping the suffix gives the 320 or 160
  kbps rendition of the same file. The links are unsigned and do not expire.

Hayasaka (hayasaka.8man.in), which is how this source came to our attention, is
a front end for the same API through someone else's proxy. We skip the proxy.
"""

import base64
import html
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from Crypto.Cipher import DES
from Crypto.Util.Padding import unpad

from constants import TIMEOUT_JIOSAAVN_DOWNLOAD, TIMEOUT_JIOSAAVN_SEARCH
from youtube import score_search_result_with_breakdown, parse_duration

_API_URL = "https://www.jiosaavn.com/api.php"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
}
# A Reliance Jio address, which is about as Indian as an IP gets. Only the API
# sees it; the CDN serves anyone.
_INDIAN_IP = "49.36.0.1"
_DES_KEY = b"38346591"
_SEARCH_FETCH = 20

# JioSaavn repeats version annotations: "Blitzkrieg Bop (1999 Remaster) (1999 Remaster)".
_RE_REPEATED_ANNOTATION = re.compile(r"(\s*[(\[][^()\[\]]+[)\]])\1+")


def is_jiosaavn_url(url: str | None) -> bool:
    """Does this URL point at JioSaavn's audio CDN?"""
    hostname = (urlsplit(url).hostname or "").lower() if url else ""
    return hostname == "saavncdn.com" or hostname.endswith(".saavncdn.com")


def _api(call: str, timeout: float = TIMEOUT_JIOSAAVN_SEARCH, **params) -> dict:
    resp = httpx.get(
        _API_URL,
        params={
            "__call": call,
            "_format": "json",
            "_marker": "0",
            "api_version": "4",
            "ctx": "web6dot0",
            **params,
        },
        headers={**_HEADERS, "X-Forwarded-For": _INDIAN_IP},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _clean_text(value: str) -> str:
    value = html.unescape(value or "").strip()
    return _RE_REPEATED_ANNOTATION.sub(r"\1", value)


def _decrypt_media_url(encrypted: str) -> str:
    """encrypted_media_url -> the 96 kbps CDN link it hides."""
    raw = DES.new(_DES_KEY, DES.MODE_ECB).decrypt(base64.b64decode(encrypted))
    return unpad(raw, DES.block_size).decode("utf-8").replace("http://", "https://", 1)


def _stream_url(more_info: dict) -> tuple[str, int] | None:
    """Best rendition JioSaavn admits to having, as (url, kbps). None if no stream."""
    encrypted = more_info.get("encrypted_media_url")
    if not encrypted:
        return None  # DRM-only track; nothing we can fetch
    try:
        url = _decrypt_media_url(encrypted)
    except Exception:
        return None
    if "_96." not in url or not is_jiosaavn_url(url):
        return None
    kbps = 320 if str(more_info.get("320kbps", "")).lower() == "true" else 160
    return url.replace("_96.", f"_{kbps}."), kbps


def _artist_of(song: dict) -> str:
    primary = (song.get("more_info") or {}).get("artistMap", {}).get("primary_artists") or []
    names = [_clean_text(a.get("name", "")) for a in primary if a.get("name")]
    if names:
        return ", ".join(names)
    # Older entries without an artist map carry "Artist - Album" in the subtitle.
    return _clean_text((song.get("subtitle") or "").split(" - ")[0])


def search_jiosaavn(query: str, limit: int) -> list[dict]:
    """Search JioSaavn and return normalised result dicts."""
    try:
        data = _api("search.getResults", q=query, p=1, n=_SEARCH_FETCH)
        results = []
        for song in data.get("results") or []:
            more_info = song.get("more_info") or {}
            stream = _stream_url(more_info)
            if not stream:
                continue
            stream_url, kbps = stream

            title = _clean_text(song.get("title", ""))
            artist = _artist_of(song)
            if not title:
                continue
            try:
                duration_secs = int(more_info.get("duration") or 0)
            except (TypeError, ValueError):
                duration_secs = 0

            combined_title = f"{artist} - {title}" if artist else title
            relevance_score, score_breakdown = score_search_result_with_breakdown(
                combined_title, artist, query,
                duration_seconds=duration_secs or None,
                view_count=None,
            )
            # Every JioSaavn track is the label's own release, which YouTube has
            # to earn with "official" (+30) and "Official Audio" (+35) in the
            # title. +70 out-earns that stack, so a 320 beats YouTube's ~128.
            relevance_score += 70
            score_breakdown.append("label_release=+70")
            # Same nudges as FreeMp3Cloud's HQ and non-HQ tiers: a declared 320
            # is worth something, the 160 fallback only just.
            if kbps == 320:
                relevance_score += 30
                score_breakdown.append("source_quality=+30 (320kbps)")
            else:
                relevance_score += 5
                score_breakdown.append("source_quality=+5 (160kbps)")

            thumbnail = (song.get("image") or "").replace("150x150", "500x500")
            results.append({
                "video_id": f"js_{song.get('id', '')}",
                "title": title,
                "channel": artist,
                "duration": parse_duration(duration_secs) if duration_secs else "",
                "thumbnail": thumbnail,
                "is_playlist": False,
                "video_count": None,
                "source": "jiosaavn",
                "source_url": stream_url,
                "quality": f"AAC {kbps}kbps",
                "relevance_score": relevance_score,
                "score_breakdown": score_breakdown,
                "slskd_username": None,
                "slskd_filename": None,
                "slskd_size": None,
            })

        results.sort(key=lambda x: x["relevance_score"], reverse=True)
        return results

    except Exception as e:
        print(f"JioSaavn search error: {e}")
        return []


def check_health() -> tuple[bool, str]:
    """A real search for a Western track, which only turns up from India.

    Karaoke covers alone mean the Indian-address header has stopped working and
    we are back to the local catalogue, which is a failure worth seeing.
    """
    try:
        data = _api("search.getResults", q="nirvana lithium", p=1, n=5)
    except Exception as e:
        return False, f"unreachable ({e})"
    songs = data.get("results") or []
    for song in songs:
        if "nirvana" in _artist_of(song).lower() and _stream_url(song.get("more_info") or {}):
            return True, ""
    if songs:
        return False, "search is geo-filtered again (only covers came back)"
    return False, "search answered without a playable stream"


def download_jiosaavn_track(download_url: str, output_path: Path) -> None:
    """Stream a JioSaavn track (AAC in MP4) directly to output_path."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with httpx.stream(
        "GET",
        download_url,
        headers=_HEADERS,
        timeout=TIMEOUT_JIOSAAVN_DOWNLOAD,
        follow_redirects=True,
    ) as resp:
        resp.raise_for_status()
        expected_size = int(resp.headers.get("content-length", 0))
        with open(output_path, "wb") as f:
            for chunk in resp.iter_bytes(chunk_size=16384):
                f.write(chunk)

    actual_size = output_path.stat().st_size
    if actual_size == 0:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("JioSaavn download produced an empty file")
    if expected_size > 0 and actual_size < expected_size:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f"JioSaavn download truncated: got {actual_size} of {expected_size} bytes")
