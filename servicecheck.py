"""
MusicGrabber - Source Health Checks

Living the pirate lifestyle means free services come and go, and when a source
is down its results used to still show, rank high, and then fail silently at
preview/download time. Nobody enjoys a result that won't play.

This module checks whether each search source can actually deliver, hides the
ones that can't, and parks a failing source for a cooldown before re-checking.

Design notes:
- Each source gets the *cheapest* check that proves it works. Monochrome runs
  one small search against its tracks API, the route that actually streams,
  then pulls the first megabyte of audio, since search kept answering while
  downloads died; the Deezer search leg being up is worthless if that cannot deliver. Direct-MP3 sites just need a live root. The
  big platforms need host reachability. Soulseek needs to be configured and
  slskd reachable.
- Last-known state is persisted in SQLite. Startup honours a still-live cooldown
  and only re-checks entries that are stale or due, so restarts do not pardon a
  provider in the middle of an outage.
- No source here imports the search registry at module load (search.py imports
  us), so any cross-reference is a lazy import inside a function.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from constants import (
    SOURCE_HEALTH_CHECK_INTERVAL,
    SOURCE_HEALTH_COOLDOWN,
    SERVICECHECK_TIMEOUT,
)
from settings import get_setting_bool

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
}

# source_id -> {"healthy": bool, "checked_at": float, "reason": str, "disabled_until": float}
_HEALTH: dict[str, dict] = {}
_LOCK = threading.RLock()

# source_id -> unix timestamp of when a probe started. Stops a stampede of
# concurrent searches all launching their own identical probe of the same
# dead service; one probe at a time is plenty.
_IN_FLIGHT: dict[str, float] = {}
# A probe older than this is presumed wedged (hung socket, misbehaving upstream)
# and a fresh one is allowed past the guard.
_PROBE_STUCK_SECONDS = 120

# Search-side strike tracking: a source that blows through the multi-source
# search deadline repeatedly is clearly having a bad day, so after a few
# consecutive timeouts we park it without waiting for the next scheduled
# probe to notice. One success wipes the slate clean.
_SEARCH_TIMEOUT_STRIKE_LIMIT = 3
_search_timeout_strikes: dict[str, int] = {}


# ---------------------------------------------------------------------------
# Low-level reachability helpers
# ---------------------------------------------------------------------------

def _http_ok(url: str, method: str = "GET") -> tuple[bool, str]:
    """A 2xx/3xx (or any non-5xx) from the host means it's alive enough to query."""
    try:
        resp = httpx.request(
            method, url,
            headers=_HEADERS,
            timeout=SERVICECHECK_TIMEOUT,
            follow_redirects=True,
        )
        # 4xx still proves the host is up and serving; only 5xx / network death counts as down.
        if resp.status_code < 500:
            return True, ""
        return False, f"HTTP {resp.status_code}"
    except Exception as exc:
        return False, f"unreachable ({exc})"


# ---------------------------------------------------------------------------
# Per-source checks: each returns (healthy: bool, reason: str)
# ---------------------------------------------------------------------------

def _check_monochrome() -> tuple[bool, str]:
    """Gate on the tracks API, the download route that actually serves audio."""
    from monochrome import download_leg_healthy
    return download_leg_healthy()


def _check_youtube() -> tuple[bool, str]:
    # If YouTube responds, yt-dlp can work; no need to run a real search.
    return _http_ok("https://www.youtube.com", method="HEAD")


def _check_soundcloud() -> tuple[bool, str]:
    return _http_ok("https://soundcloud.com", method="HEAD")


def _check_zvu4no() -> tuple[bool, str]:
    from zvu4no import _BASE_URL
    return _http_ok(_BASE_URL)


def _check_freemp3cloud() -> tuple[bool, str]:
    from freemp3cloud import _BASE_URL
    return _http_ok(_BASE_URL)


def _check_jiosaavn() -> tuple[bool, str]:
    from jiosaavn import check_health
    return check_health()


def _check_soulseek() -> tuple[bool, str]:
    """Soulseek is opt-in. If it's not enabled it's simply out of play (healthy);
    if it IS enabled, a token round-trip proves slskd is reachable and auth works."""
    from slskd import slskd_enabled, get_slskd_token
    if not slskd_enabled():
        return True, ""  # disabled by toggle, not unhealthy; handled by the source filter
    try:
        token = get_slskd_token()
    except Exception as exc:
        return False, f"slskd unreachable ({exc})"
    if token:
        return True, ""
    return False, "slskd unreachable or auth failed"


# source_id -> check callable. Sources absent here are assumed healthy.
_CHECKS = {
    "monochrome": _check_monochrome,
    "youtube": _check_youtube,
    "soundcloud": _check_soundcloud,
    "zvu4no": _check_zvu4no,
    "freemp3cloud": _check_freemp3cloud,
    "jiosaavn": _check_jiosaavn,
    "soulseek": _check_soulseek,
}
_PERSISTENT_SOURCE_IDS = frozenset(_CHECKS)


def _read_persisted_health() -> dict[str, dict]:
    """Read persisted built-in source verdicts, failing open during early startup."""
    try:
        from db import db_conn
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT source_id, healthy, checked_at, reason, disabled_until FROM source_health"
            ).fetchall()
        return {
            row[0]: {
                "healthy": bool(row[1]),
                "checked_at": float(row[2] or 0),
                "reason": row[3] or "",
                "disabled_until": float(row[4] or 0),
            }
            for row in rows if row[0] in _PERSISTENT_SOURCE_IDS
        }
    except Exception as exc:
        print(f"servicecheck: could not restore persisted health: {exc}")
        return {}


def _persist_health_entry(source_id: str, entry: dict) -> None:
    """Best-effort persistence; health checks must still work if SQLite is busy."""
    if source_id not in _PERSISTENT_SOURCE_IDS:
        return
    try:
        from db import db_conn
        with db_conn() as conn:
            conn.execute(
                """INSERT INTO source_health
                   (source_id, healthy, checked_at, reason, disabled_until)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(source_id) DO UPDATE SET
                       healthy = excluded.healthy,
                       checked_at = excluded.checked_at,
                       reason = excluded.reason,
                       disabled_until = excluded.disabled_until""",
                (
                    source_id,
                    int(bool(entry.get("healthy"))),
                    float(entry.get("checked_at", 0)),
                    entry.get("reason", ""),
                    float(entry.get("disabled_until", 0)),
                ),
            )
            conn.commit()
    except Exception as exc:
        print(f"servicecheck: could not persist {source_id} health: {exc}")


def load_persisted_health() -> dict[str, dict]:
    """Restore last-known verdicts into memory and return what was loaded."""
    restored = _read_persisted_health()
    with _LOCK:
        _HEALTH.update(restored)
    return restored


# ---------------------------------------------------------------------------
# Settings-driven tuneables
# ---------------------------------------------------------------------------

def _checks_enabled() -> bool:
    return get_setting_bool("source_health_checks_enabled", True)


def _check_interval() -> float:
    from settings import get_setting_int
    minutes = get_setting_int("source_health_check_interval_minutes", 0)
    return minutes * 60 if minutes > 0 else SOURCE_HEALTH_CHECK_INTERVAL


def _cooldown() -> float:
    from settings import get_setting_int
    minutes = get_setting_int("source_health_cooldown_minutes", 0)
    return minutes * 60 if minutes > 0 else SOURCE_HEALTH_COOLDOWN


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_source_available(source_id: str) -> bool:
    """Fast, no-network check: is this source currently allowed to return results?

    Unknown sources are innocent until a check proves otherwise; a source last
    seen unhealthy stays hidden until a fresh probe clears it (the cooldown
    expiring merely schedules that re-probe, it isn't a pardon). Checks being
    globally disabled means everything is always "available".
    """
    if not _checks_enabled():
        return True
    with _LOCK:
        entry = _HEALTH.get(source_id)
        if not entry:
            return True  # unknown == innocent until a check proves otherwise
        return bool(entry["healthy"])


def _needs_check(entry: dict | None, now: float) -> bool:
    """Is this health entry due a re-probe? (Missing, cooldown expired, or stale.)"""
    if not entry:
        return True
    if not entry.get("healthy") and now >= entry.get("disabled_until", 0):
        return True
    return (now - entry.get("checked_at", 0)) >= _check_interval()


def _probe_in_flight(source_id: str, now: float) -> bool:
    """True if a live (not wedged) probe for this source is already running."""
    started = _IN_FLIGHT.get(source_id)
    return started is not None and (now - started) < _PROBE_STUCK_SECONDS


def check_source(source_id: str, force: bool = False) -> dict:
    """Run the source's health check if stale or forced; return its health entry.

    Sources with no registered check are always healthy. On failure the source is
    parked for the cooldown so we don't hammer a dead service every search.
    """
    check_fn = _CHECKS.get(source_id)
    if check_fn is None:
        return {"healthy": True, "checked_at": time.time(), "reason": "", "disabled_until": 0}

    now = time.time()
    with _LOCK:
        entry = _HEALTH.get(source_id)
        if entry and not force and not _needs_check(entry, now):
            return entry
        # Someone's already probing this one; serve what we have rather than
        # piling a second identical probe on a service that's likely down.
        if not force and _probe_in_flight(source_id, now):
            return entry or {"healthy": True, "checked_at": 0.0, "reason": "first check in flight", "disabled_until": 0}
        _IN_FLIGHT[source_id] = now

    try:
        try:
            healthy, reason = check_fn()
        except Exception as exc:
            healthy, reason = False, f"check error: {exc}"

        entry = {
            "healthy": healthy,
            "checked_at": time.time(),
            "reason": "" if healthy else reason,
            "disabled_until": 0 if healthy else time.time() + _cooldown(),
        }
        with _LOCK:
            _HEALTH[source_id] = entry
    finally:
        with _LOCK:
            _IN_FLIGHT.pop(source_id, None)

    if not healthy:
        print(f"servicecheck: {source_id} unavailable: {reason} (parked ~{int(_cooldown() // 60)}m)")
    _persist_health_entry(source_id, entry)
    return entry


def check_sources(source_ids: list[str] | tuple[str, ...] | set[str], force: bool = False) -> dict[str, dict]:
    """Check a subset of sources in parallel, usually the active search sources.

    Deliberately NOT a `with` block: exiting a ThreadPoolExecutor context calls
    shutdown(wait=True), which would make the per-future timeout below purely
    decorative by then waiting for the slowest probe anyway. We collect what
    lands in time and walk away; a straggler probe finishes in the background
    and writes its own verdict into _HEALTH via check_source.
    """
    ids = [sid for sid in source_ids if sid in _CHECKS]
    if not ids or not _checks_enabled():
        return {}
    pool = ThreadPoolExecutor(max_workers=len(ids))
    try:
        futures = {pool.submit(check_source, sid, force): sid for sid in ids}
        results = {}
        for fut in futures:
            sid = futures[fut]
            try:
                results[sid] = fut.result(timeout=SERVICECHECK_TIMEOUT + 5)
            except Exception:
                # Probe still running (or crashed); don't fabricate a verdict,
                # the in-flight probe will record the real one when it lands.
                with _LOCK:
                    entry = _HEALTH.get(sid)
                results[sid] = entry or {"healthy": True, "checked_at": 0.0, "reason": "check still running", "disabled_until": 0}
        return results
    finally:
        pool.shutdown(wait=False)


def refresh_sources_async(source_ids: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    """Kick off background re-probes for any stale sources; never blocks.

    This is what the search fan-out calls: it serves the last-known health
    verdicts immediately and lets a daemon thread bring stale ones up to date
    for the next search. Returns the ids being refreshed, purely for logging.
    """
    if not _checks_enabled():
        return []
    now = time.time()
    with _LOCK:
        stale = [
            sid for sid in source_ids
            if sid in _CHECKS
            and _needs_check(_HEALTH.get(sid), now)
            and not _probe_in_flight(sid, now)
        ]
    if not stale:
        return []
    threading.Thread(
        target=lambda: check_sources(stale),
        daemon=True,
        name="source-health-refresh",
    ).start()
    return stale


def check_all_sources(force: bool = False) -> dict[str, dict]:
    """Check every registered source in parallel. Used at startup and on demand."""
    return check_sources(set(_CHECKS), force=force)


def mark_unhealthy(source_id: str, reason: str) -> None:
    """Let the download path report a real-world failure straight into health state.

    When a download dies because a whole source is offline, we don't want to wait
    for the next scheduled probe: park it now so the very next search hides it.
    """
    if source_id not in _CHECKS:
        return
    entry = {
        "healthy": False,
        "checked_at": time.time(),
        "reason": reason,
        "disabled_until": time.time() + _cooldown(),
    }
    with _LOCK:
        _HEALTH[source_id] = entry
    _persist_health_entry(source_id, entry)
    print(f"servicecheck: {source_id} marked unhealthy from download failure: {reason}")


def record_search_timeout(source_id: str) -> None:
    """Count a multi-source search timeout against a source; three strikes parks it.

    The scheduled probes only notice a source is down when they next run; this
    lets the searches themselves rat out a limping source (up per its probe, yet
    never answering inside the deadline) so bulk imports don't burn the full
    deadline on it for hundreds of tracks in a row.
    """
    if source_id not in _CHECKS or not _checks_enabled():
        return
    with _LOCK:
        strikes = _search_timeout_strikes.get(source_id, 0) + 1
        if strikes < _SEARCH_TIMEOUT_STRIKE_LIMIT:
            _search_timeout_strikes[source_id] = strikes
            return
        _search_timeout_strikes[source_id] = 0
    mark_unhealthy(source_id, f"timed out {_SEARCH_TIMEOUT_STRIKE_LIMIT} consecutive searches")


def record_search_success(source_id: str) -> None:
    """A source answered a search in time; wipe its timeout strikes."""
    with _LOCK:
        _search_timeout_strikes.pop(source_id, None)


def unavailable_sources() -> list[dict]:
    """Sources currently parked as unhealthy, for the API / search toast.

    Returns [{id, label, reason, retry_at}] where retry_at is a unix timestamp.
    A source stays listed until a probe clears it, even if its cooldown has
    technically expired (matching is_source_available's guilty-until-re-proven
    stance), so retry_at is clamped to now for anything overdue a re-check.
    """
    if not _checks_enabled():
        return []
    try:
        from search import SOURCE_REGISTRY
        labels = {sid: cfg.get("label", sid) for sid, cfg in SOURCE_REGISTRY.items()}
    except Exception:
        labels = {}
    now = time.time()
    out = []
    with _LOCK:
        for sid, entry in _HEALTH.items():
            if not entry["healthy"]:
                out.append({
                    "id": sid,
                    "label": labels.get(sid, sid),
                    "reason": entry.get("reason", ""),
                    "retry_at": max(entry.get("disabled_until", 0), now),
                })
    return out


def health_snapshot() -> list[dict]:
    """Full current health state for the /api/sources/health endpoint."""
    try:
        from search import SOURCE_REGISTRY
        labels = {sid: cfg.get("label", sid) for sid, cfg in SOURCE_REGISTRY.items()}
        enabled = {
            sid: get_setting_bool(f"source_{sid}_enabled", cfg.get("default_enabled", True))
            for sid, cfg in SOURCE_REGISTRY.items()
        }
    except Exception:
        labels = {}
        enabled = {}
    with _LOCK:
        snap = []
        for sid in _CHECKS:
            entry = _HEALTH.get(sid)
            snap.append({
                "id": sid,
                "label": labels.get(sid, sid),
                "healthy": entry["healthy"] if entry else None,
                "reason": (entry or {}).get("reason", ""),
                "checked_at": (entry or {}).get("checked_at", 0),
                "retry_at": (entry or {}).get("disabled_until", 0),
                "available": is_source_available(sid),
                "enabled": enabled.get(sid, True),
            })
    return snap


def start_health_checks() -> None:
    """Restore saved verdicts, then refresh only sources that are actually due."""
    if not _checks_enabled():
        return
    load_persisted_health()
    threading.Thread(
        target=lambda: check_all_sources(force=False),
        daemon=True,
        name="source-health-startup",
    ).start()
