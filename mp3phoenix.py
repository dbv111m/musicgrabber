"""MP3Phoenix source using Selenium-assisted HTTP sessions."""

import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from urllib.parse import quote_plus, urlsplit

from curl_cffi import requests

from constants import (
    MP3PHOENIX_BROWSER_FAILURE_LIMIT,
    MP3PHOENIX_BROWSER_FAILURE_COOLDOWN,
    MP3PHOENIX_BROWSER_TIMEOUT,
    MP3PHOENIX_SESSION_TTL,
    MP3PHOENIX_PREVIEW_BYTES,
    TIMEOUT_MP3PHOENIX_DOWNLOAD,
    TIMEOUT_MP3PHOENIX_PREVIEW,
    TIMEOUT_MP3PHOENIX_SEARCH,
)
from youtube import parse_duration, score_search_result_with_breakdown


_BASE_URL = "https://mp3phoenix.net"
_AJAX_URL = f"{_BASE_URL}/ajax/music/"
_DELIMITER = "<!|!>"
_RESULT_PREFIX = "MUSICGRABBER_MP3PHOENIX="

_RE_ARTIST = re.compile(r'musicTheme-results-info__card_artist"[^>]*>\s*<b>(.*?)</b>', re.DOTALL)
_RE_TITLE = re.compile(r'musicTheme-results-info__card_tracklink"[^>]*>(.*?)</a>', re.DOTALL)
_RE_DUR = re.compile(r'<span class="dur">(.*?)</span>')
_RE_HREF = re.compile(
    r'musicTheme-results-info__card_download link"[^>]*href="((?:https?:)?//[^/"]+/getmp3/[^"]+|/getmp3/[^"]+)"'
)
_RE_TAGS = re.compile(r"<[^>]+>")


def _browser_subprocess_env() -> dict[str, str]:
    """Pass browser plumbing, never application credentials, to the broker."""
    allowed = (
        "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR",
        "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
    )
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env["PYTHONUNBUFFERED"] = "1"
    env["MP3PHOENIX_BROWSER_TIMEOUT"] = str(MP3PHOENIX_BROWSER_TIMEOUT)
    return env


def _clean_text(value: str) -> str:
    return html.unescape(_RE_TAGS.sub("", value or "")).strip()


def _duration_to_secs(value: str) -> int:
    try:
        parts = [int(part) for part in value.strip().split(":")]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except (AttributeError, ValueError):
        pass
    return 0


def is_mp3phoenix_url(url: str | None) -> bool:
    """Return whether *url* has the media shape emitted by MP3Phoenix search.

    This is deliberately narrower than a hostname allowlist. Preview URLs come
    from an authenticated caller and a provider-homepage URL is not a track; if
    it reaches the shared client and returns HTML it must not be allowed to
    count as a provider failure.
    """
    if not url:
        return False
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or hostname != "mp3phoenix.net" or port is not None:
        return False
    parts = [part for part in parsed.path.split("/") if part]
    return len(parts) >= 2 and parts[0] == "getmp3" and all(part not in (".", "..") for part in parts)


class _PhoenixBrowser:
    """Start Chrome only long enough to collect fresh clearance cookies."""

    def request(self, payload: dict, timeout: int) -> dict:
        process = subprocess.Popen(
            [sys.executable, "-u", str(Path(__file__).with_name("mp3phoenix_browser.py"))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            bufsize=1,
            env=_browser_subprocess_env(),
        )
        try:
            output, _ = process.communicate(
                json.dumps(payload, separators=(",", ":")) + "\n",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise TimeoutError(f"MP3Phoenix browser request timed out after {timeout}s")
        for line in reversed(output.splitlines()):
            if line.startswith(_RESULT_PREFIX):
                response = json.loads(line[len(_RESULT_PREFIX):])
                if not response.get("success"):
                    raise RuntimeError(response.get("error") or "MP3Phoenix browser request failed")
                return response.get("result") or {}
        raise RuntimeError(f"MP3Phoenix browser exited with code {process.returncode} without a result")


_BROWSER = _PhoenixBrowser()


def _cloudflare_rejected(response) -> bool:
    if response.status_code in (403, 429, 503):
        return True
    content_type = (response.headers.get("content-type") or "").lower()
    if "html" not in content_type:
        return False
    body = response.text.lower()
    return "cf-chl-" in body or "just a moment" in body or "verify you are human" in body


class _CoolingDown(RuntimeError):
    """Raised while the circuit breaker is open. Never counts as a fresh failure,
    otherwise every call made during the cooldown re-arms it and it never expires."""


class _PhoenixClient:
    """Reuse a Chrome-shaped HTTP session, refreshing it only when necessary."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session = None
        self._refreshed_at = 0.0
        self._failures = 0
        self._disabled_until = 0.0

    def _record_failure(self) -> None:
        self._failures += 1
        if self._failures >= MP3PHOENIX_BROWSER_FAILURE_LIMIT:
            self._disabled_until = time.monotonic() + MP3PHOENIX_BROWSER_FAILURE_COOLDOWN

    def _refresh(self) -> None:
        # The child's own Cloudflare-clearance wait defaults to MP3PHOENIX_BROWSER_TIMEOUT
        # (forwarded via env in _browser_subprocess_env); give it that plus a buffer here
        # so a slow-but-succeeding clearance isn't killed by an impatient parent.
        details = _BROWSER.request({"action": "session"}, MP3PHOENIX_BROWSER_TIMEOUT + 20)
        cookies = details.get("cookies") or []
        user_agent = str(details.get("user_agent") or "")
        if not cookies or not user_agent:
            raise RuntimeError("MP3Phoenix browser returned an incomplete session")
        session = requests.Session(impersonate="chrome")
        session.headers.update({"User-Agent": user_agent, "Referer": f"{_BASE_URL}/"})
        for cookie in cookies:
            session.cookies.set(
                cookie["name"], cookie["value"],
                domain=cookie.get("domain"), path=cookie.get("path") or "/",
            )
        old_session, self._session = self._session, session
        if old_session:
            old_session.close()
        self._refreshed_at = time.monotonic()

    def _ensure_session(self, force: bool = False):
        now = time.monotonic()
        if now < self._disabled_until:
            remaining = int(self._disabled_until - now)
            raise _CoolingDown(f"MP3Phoenix session is cooling down for {remaining}s")
        if force or not self._session or now - self._refreshed_at >= MP3PHOENIX_SESSION_TTL:
            self._refresh()
        return self._session

    def _get(self, url: str, *, timeout: int, stream: bool = False, headers: dict | None = None):
        for attempt in range(2):
            session = self._ensure_session(force=attempt == 1)
            response = session.get(url, headers=headers, timeout=timeout, stream=stream)
            if not _cloudflare_rejected(response):
                return response
            response.close()
        raise RuntimeError("MP3Phoenix rejected the refreshed browser session")

    def search(self, url: str) -> str:
        with self._lock:
            response = None
            try:
                response = self._get(
                    url,
                    timeout=TIMEOUT_MP3PHOENIX_SEARCH,
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
                response.raise_for_status()
                body = response.text
                self._failures = 0
                return body
            except _CoolingDown:
                raise
            except Exception:
                self._record_failure()
                raise
            finally:
                if response is not None:
                    response.close()

    def download(self, url: str, output_path: Path) -> None:
        with self._lock:
            partial_path = output_path.with_name(f"{output_path.name}.part")
            response = None
            try:
                response = self._get(url, timeout=TIMEOUT_MP3PHOENIX_DOWNLOAD, stream=True)
                response.raise_for_status()
                content_type = (response.headers.get("content-type") or "").lower()
                if content_type and not (
                    content_type.startswith("audio/") or content_type == "application/octet-stream"
                ):
                    raise RuntimeError(f"MP3Phoenix returned {content_type!r} instead of audio")
                expected_size = int(response.headers.get("content-length") or 0)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                total = 0
                with partial_path.open("wb") as output:
                    for chunk in response.iter_content(256 * 1024):
                        if chunk:
                            output.write(chunk)
                            total += len(chunk)
                if total == 0:
                    raise RuntimeError("MP3Phoenix download produced an empty file")
                if expected_size and total < expected_size:
                    raise RuntimeError(
                        f"MP3Phoenix download truncated: got {total} of {expected_size} bytes"
                    )
                partial_path.replace(output_path)
                self._failures = 0
            except _CoolingDown:
                raise
            except Exception:
                partial_path.unlink(missing_ok=True)
                self._record_failure()
                raise
            finally:
                if response is not None:
                    response.close()

    def preview(self, url: str, max_bytes: int) -> bytes:
        """The opening chunk of a track, for auditioning rather than keeping.

        Cloudflare will not serve the browser directly, so the bytes have to come
        through this session like everything else. Unlike download() we take the
        first chunk and hang up, which spares the provider serving a whole file
        so somebody can decide it is the wrong mix.
        """
        with self._lock:
            response = None
            try:
                response = self._get(url, timeout=TIMEOUT_MP3PHOENIX_PREVIEW, stream=True)
                response.raise_for_status()
                content_type = (response.headers.get("content-type") or "").lower()
                if content_type and not (
                    content_type.startswith("audio/") or content_type == "application/octet-stream"
                ):
                    raise RuntimeError(f"MP3Phoenix returned {content_type!r} instead of audio")
                snippet = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    if chunk:
                        snippet.extend(chunk)
                        if len(snippet) >= max_bytes:
                            break
                if not snippet:
                    raise RuntimeError("MP3Phoenix preview produced no audio")
                self._failures = 0
                return bytes(snippet)
            except _CoolingDown:
                raise
            except Exception:
                # A preview URL is supplied by the caller. Even after its shape
                # has been validated, a stale or invented token can return a
                # perfectly healthy 404/HTML response. That request may fail,
                # but it must not park search and downloads for every user.
                raise
            finally:
                if response is not None:
                    response.close()

    def health(self) -> tuple[bool, str]:
        """Report remembered health without spinning up Chrome for a probe."""
        now = time.monotonic()
        if now < self._disabled_until:
            remaining = int(self._disabled_until - now)
            return False, f"MP3Phoenix is cooling down for {remaining}s after repeated failures"
        return True, "MP3Phoenix session available on demand"


_CLIENT = _PhoenixClient()


def _parse_results(fragment: str, query: str) -> list[dict]:
    parts = fragment.split(_DELIMITER)
    result_html = parts[2] if len(parts) >= 3 else fragment
    artists = _RE_ARTIST.findall(result_html)
    titles = _RE_TITLE.findall(result_html)
    durations = _RE_DUR.findall(result_html)
    hrefs = _RE_HREF.findall(result_html)
    if len({len(artists), len(titles), len(durations), len(hrefs)}) > 1:
        # The four fields below are paired up purely by position. If a card is
        # missing one of them (or something elsewhere in the page matches one
        # of these patterns), a positional zip would silently shift every
        # result after it onto the wrong download link. Discarding the whole
        # page and asking again beats mis-tagging a download with someone
        # else's track.
        print(
            "MP3Phoenix parse error: mismatched result field counts "
            f"(artist={len(artists)}, title={len(titles)}, duration={len(durations)}, href={len(hrefs)}); "
            "discarding this page rather than risk mis-pairing results"
        )
        return []
    results = []
    seen_urls = set()
    for artist_raw, title_raw, duration_raw, href_raw in zip(artists, titles, durations, hrefs):
        artist = _clean_text(artist_raw)
        title = _clean_text(title_raw)
        duration = _clean_text(duration_raw)
        href = html.unescape(href_raw)
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = _BASE_URL + href
        if not is_mp3phoenix_url(href) or href in seen_urls:
            continue
        seen_urls.add(href)
        duration_secs = _duration_to_secs(duration)
        combined = f"{artist} - {title}" if artist else title
        relevance_score, score_breakdown = score_search_result_with_breakdown(
            combined,
            artist,
            query,
            duration_seconds=duration_secs or None,
            view_count=None,
        )
        relevance_score += 30
        score_breakdown.append("source_quality=+30")
        results.append({
            "video_id": "px_" + hashlib.md5(href.encode()).hexdigest()[:12],
            "title": title,
            "channel": artist,
            "duration": parse_duration(duration_secs) if duration_secs else duration,
            "thumbnail": "",
            "is_playlist": False,
            "video_count": None,
            "source": "mp3phoenix",
            "source_url": href,
            "quality": "320kbps",
            "relevance_score": relevance_score,
            "score_breakdown": score_breakdown,
            "slskd_username": None,
            "slskd_filename": None,
            "slskd_size": None,
        })
    results.sort(key=lambda item: item["relevance_score"], reverse=True)
    return results


def search_mp3phoenix(query: str, limit: int) -> list[dict]:
    """Search through the authorised HTTP session, returning no results on failure."""
    try:
        url = _AJAX_URL + quote_plus(query)
        fragment = _CLIENT.search(url)
        return _parse_results(fragment, query)[:limit]
    except Exception as exc:
        print(f"MP3Phoenix search error: {exc}")
        return []


def download_mp3phoenix_track(download_url: str, output_path: Path) -> None:
    """Stream an MP3 through the Chrome-impersonating authorised session."""
    if not is_mp3phoenix_url(download_url):
        raise ValueError("Invalid MP3Phoenix download URL")
    _CLIENT.download(download_url, output_path)
    if not output_path.exists() or output_path.stat().st_size == 0:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("MP3Phoenix download produced no audio file")


def fetch_mp3phoenix_preview(download_url: str) -> bytes:
    """Opening seconds of a track as MP3 bytes, for the preview endpoint."""
    if not is_mp3phoenix_url(download_url):
        raise ValueError("Invalid MP3Phoenix download URL")
    return _CLIENT.preview(download_url, MP3PHOENIX_PREVIEW_BYTES)


def browser_healthy() -> tuple[bool, str]:
    """Report remembered session health without launching Chrome for a probe.

    A real search or download launches Chrome when it actually needs a fresh
    session, and its failures feed the circuit breaker; this just reads that
    state back, matching monochrome_browser.py's browser_fallback_health().
    """
    return _CLIENT.health()
