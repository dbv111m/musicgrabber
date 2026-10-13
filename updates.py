"""
MusicGrabber - Update check

Asks GitLab's public tags API what the newest release is, so the UI can mention
it before somebody finds out the hard way that a source has changed its address
again. The answer is cached, so GitLab hears from us twice a day at most, and
never at all when update_check_enabled is off.
"""

import logging
import re
import threading
import time

import httpx

from constants import (
    VERSION, UPDATE_CHECK_TAGS_URL, UPDATE_CHECK_CHANGELOG_URL,
    UPDATE_CHECK_INTERVAL, UPDATE_CHECK_RETRY, TIMEOUT_UPDATE_CHECK,
)
from settings import get_setting_bool

logger = logging.getLogger(__name__)

# Release tags only. The repo also carries archive/* tags for buried code, which
# are not releases however much they might like to be.
_RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

_lock = threading.Lock()
_cache = {"latest": None, "expires_at": 0.0}


def _version_tuple(version: str) -> tuple[int, int, int] | None:
    """'4.2.4', 'v4.2.4' or '4.3.0-dev' -> (4, 2, 4) etc. None if it is not a version."""
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", version or "")
    return tuple(int(part) for part in match.groups()) if match else None


def _fetch_latest_release() -> str | None:
    """Newest vX.Y.Z tag on GitLab, without the v. None if GitLab will not say."""
    try:
        resp = httpx.get(
            UPDATE_CHECK_TAGS_URL,
            params={"order_by": "version", "sort": "desc", "per_page": 20},
            timeout=TIMEOUT_UPDATE_CHECK,
        )
        resp.raise_for_status()
        names = [tag.get("name", "") for tag in resp.json()]
    except Exception as e:
        logger.info(f"Update check could not reach GitLab: {e}")
        return None

    releases = [name for name in names if _RELEASE_TAG.match(name)]
    if not releases:
        return None
    return max(releases, key=_version_tuple)[1:]


def _latest_release() -> str | None:
    """Cached latest release. A failure is cached too, for a shorter spell."""
    with _lock:
        now = time.monotonic()
        if now >= _cache["expires_at"]:
            latest = _fetch_latest_release()
            _cache["latest"] = latest
            _cache["expires_at"] = now + (UPDATE_CHECK_INTERVAL if latest else UPDATE_CHECK_RETRY)
        return _cache["latest"]


def get_update_status() -> dict:
    """What the UI needs to decide whether to show the update banner."""
    status = {
        "enabled": get_setting_bool("update_check_enabled", True),
        "current": VERSION,
        "latest": None,
        "update_available": False,
        "changelog_url": UPDATE_CHECK_CHANGELOG_URL,
    }
    if not status["enabled"]:
        return status

    latest = _latest_release()
    current = _version_tuple(VERSION)
    status["latest"] = latest
    # A :dev build always carries the next version, so it is never behind a release.
    status["update_available"] = bool(latest and current and _version_tuple(latest) > current)
    return status
