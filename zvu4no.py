"""
MusicGrabber - zvu4no Source

Russian MP3 portal with server-rendered search pages and direct MP3 links.
No auth required. Search results include artist, title, duration, optional
thumbnail, and a data.zvu4it.org download URL.
"""

import hashlib
import html
import re
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from constants import TIMEOUT_ZVU4NO_DOWNLOAD, TIMEOUT_ZVU4NO_SEARCH
from youtube import score_search_result_with_breakdown, parse_duration

_BASE_URL = "https://zvu4it.org"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Referer": _BASE_URL + "/",
}

_RE_BLOCK = re.compile(r'<div class="f-table">.*?(?=<div class="f-table">|<div id="queries"|</div>\s*</div>\s*<div id="amp-player")', re.DOTALL)
_RE_ARTIST = re.compile(r'<div class="artist-name">\s*<a [^>]*>(.*?)</a>\s*</div>', re.DOTALL)
_RE_TITLE = re.compile(r'<div class="track-name">(.*?)</div>', re.DOTALL)
_RE_DUR = re.compile(r'<div class="time-text">(.*?)</div>', re.DOTALL)
# Domain-agnostic: the site rebranded from zvu4no.org to zvu4it.org under our
# feet, so match whichever CDN host serves the links rather than duplicating
# the current hostname here.
_RE_HREF = re.compile(r'<a class="mp3" href="(//data\.[a-z0-9.-]+/download-track/[^"]+\.mp3)"', re.DOTALL)
_RE_IMG = re.compile(r'<img src="(//img\.[a-z0-9.-]+/[^"]+)"', re.DOTALL)
_RE_TAGS = re.compile(r"<[^>]+>")


def is_zvu4no_url(url: str | None) -> bool:
    """Return whether a URL belongs to the provider's current hostname."""
    hostname = (urlsplit(url).hostname or "").lower() if url else ""
    return hostname == "zvu4it.org" or hostname.endswith(".zvu4it.org")


def _clean_text(value: str) -> str:
    value = _RE_TAGS.sub("", value or "")
    return html.unescape(value).strip()


def _duration_to_secs(dur: str) -> int:
    try:
        parts = dur.strip().split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except (ValueError, AttributeError):
        pass
    return 0


def search_zvu4no(query: str, limit: int) -> list[dict]:
    """Search the current zvu4no host and return normalised result dicts."""
    try:
        url = f"{_BASE_URL}/tracks/{quote(query)}"
        resp = httpx.get(url, headers=_HEADERS, timeout=TIMEOUT_ZVU4NO_SEARCH, follow_redirects=True)
        resp.raise_for_status()

        results = []
        seen_urls = set()
        for block in _RE_BLOCK.findall(resp.text):
            artist_m = _RE_ARTIST.search(block)
            title_m = _RE_TITLE.search(block)
            dur_m = _RE_DUR.search(block)
            href_m = _RE_HREF.search(block)
            if not (artist_m and title_m and href_m):
                continue

            artist = _clean_text(artist_m.group(1))
            title = _clean_text(title_m.group(1))
            dur = _clean_text(dur_m.group(1)) if dur_m else ""
            href = "https:" + html.unescape(href_m.group(1))
            if href in seen_urls:
                continue
            seen_urls.add(href)

            duration_secs = _duration_to_secs(dur)
            combined_title = f"{artist} - {title}" if artist else title
            relevance_score, score_breakdown = score_search_result_with_breakdown(
                combined_title, artist, query,
                duration_seconds=duration_secs or None,
                view_count=None,
            )
            # Direct MP3s are useful, but observed bitrate varies. Keep this modest
            # and let existing title/duration scoring do the real work.
            relevance_score += 10
            score_breakdown.append("source_quality=+10")

            img_m = _RE_IMG.search(block)
            thumb = "https:" + html.unescape(img_m.group(1)) if img_m else ""
            video_id = "zv_" + hashlib.md5(href.encode()).hexdigest()[:12]

            results.append({
                "video_id": video_id,
                "title": title,
                "channel": artist,
                "duration": parse_duration(duration_secs) if duration_secs else dur,
                "thumbnail": thumb,
                "is_playlist": False,
                "video_count": None,
                "source": "zvu4no",
                "source_url": href,
                "quality": "MP3",
                "relevance_score": relevance_score,
                "score_breakdown": score_breakdown,
                "slskd_username": None,
                "slskd_filename": None,
                "slskd_size": None,
            })

        results.sort(key=lambda x: x["relevance_score"], reverse=True)
        return results

    except Exception as e:
        print(f"zvu4no search error: {e}")
        return []


def download_zvu4no_track(download_url: str, output_path: Path) -> None:
    """Stream a zvu4no track directly to output_path."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with httpx.stream(
        "GET",
        download_url,
        headers=_HEADERS,
        timeout=TIMEOUT_ZVU4NO_DOWNLOAD,
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
        raise RuntimeError("zvu4no download produced an empty file")
    if expected_size > 0 and actual_size < expected_size:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f"zvu4no download truncated: got {actual_size} of {expected_size} bytes")
