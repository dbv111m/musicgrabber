"""
MusicGrabber - Search Source Registry

Extensible source architecture. YouTube, SoundCloud and friends are supported.
Adding a new source is one function and one registry entry.
"""

import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from copy import deepcopy
from concurrent.futures import (
    ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError,
)

from constants import (
    TIMEOUT_YTDLP_SEARCH,
    SEARCH_ALL_DEADLINE,
    SEARCH_SLOT_WAIT_INTERACTIVE,
    SEARCH_SLOT_WAIT_AUTOMATED,
    TIMEOUT_SLSKD_SEARCH,
    SLSKD_EMPTY_RETRY_DELAY,
    SOUNDCLOUD_SEARCH_MULTIPLIER, SOUNDCLOUD_SEARCH_MIN_FETCH,
    SEARCH_MAX_PER_SOURCE,
    SEARCH_MAX_PER_SOURCE_YOUTUBE,
    SEARCH_MAX_PER_SOURCE_SOUNDCLOUD, SEARCH_MAX_PER_SOURCE_ZVU4NO,
    SEARCH_MAX_PER_SOURCE_FREEMP3CLOUD,
    SEARCH_MAX_PER_SOURCE_SOULSEEK, SEARCH_MAX_PER_SOURCE_MONOCHROME,
    AUTOMATED_SEARCH_CACHE_TTL_SECONDS, AUTOMATED_SEARCH_CACHE_MAX_ENTRIES,
    TIER_UNKNOWN, TIER_LOSSY_320, TIER_LOSSLESS, kbps_to_tier,
)
from db import get_blacklisted_video_ids, get_blacklisted_uploaders
from metadata import fetch_mb_expected_duration, search_artist_mbid, lookup_musicbrainz
from settings import get_setting_bool, get_setting_int
from monochrome import search_monochrome, monochrome_enabled
from slskd import slskd_enabled, search_slskd
from zvu4no import search_zvu4no
from freemp3cloud import search_freemp3cloud
import servicecheck
from youtube import (
    search_youtube, score_search_result_with_breakdown, format_score_breakdown, parse_duration,
    _parse_query_artist_title, _query_has_variation, run_ytdlp_with_retries,
)

# Penalty large enough to push blacklisted uploaders to the bottom of results
# without hiding them entirely  -  the user might still want to see them
_BLACKLIST_UPLOADER_PENALTY = 500


# ---------------------------------------------------------------------------
# SoundCloud search
# ---------------------------------------------------------------------------

def search_monochrome_source(query: str, limit: int = 10) -> list[dict]:
    """Wrap search_monochrome with the enabled-check so the registry stays consistent."""
    if not monochrome_enabled():
        return []
    return search_monochrome(query, limit)


def search_soulseek(query: str, limit: int = 10, retry_empty: bool = True) -> list[dict]:
    """Search Soulseek via slskd and return normal search-result dictionaries."""
    if not slskd_enabled():
        return []

    raw = search_slskd(query, timeout_secs=TIMEOUT_SLSKD_SEARCH)
    if not raw and retry_empty:
        # Soulseek's distributed search is moody: a term can come back empty even
        # when the files plainly exist, and a fresh search often reaches different
        # peers. Give it one more go before we declare the network soulless.
        time.sleep(SLSKD_EMPTY_RETRY_DELAY)
        raw = search_slskd(query, timeout_secs=TIMEOUT_SLSKD_SEARCH)

    results = []
    for item in raw[:limit]:
        duration_raw = item.get("duration", "0")
        try:
            duration = parse_duration(int(duration_raw))
        except (TypeError, ValueError):
            duration = str(duration_raw or "")
        result = dict(item)
        # Fallback id mirrors slskd.py's stable recipe (same file, same id
        # across searches); a positional slskd_0 would mean a different
        # "identity" every search, and blacklists would never stick.
        result["video_id"] = item.get("id") or (
            "slskd_" + hashlib.md5(
                f"{item.get('slskd_username', '')}|{item.get('slskd_filename', '')}".encode()
            ).hexdigest()[:12]
        )
        result["duration"] = duration
        result["thumbnail"] = ""
        result["source_url"] = f"soulseek://{item.get('slskd_username', '')}/{item.get('slskd_filename', '')}"
        result["slskd_size"] = item.get("slskd_size") or item.get("size")
        results.append(result)
    return results

def parse_soundcloud_search_results(stdout: str, query: str | None = None) -> list[dict]:
    """Parse yt-dlp JSON output from an scsearch query."""
    results = []
    for line in stdout.strip().split("\n"):
        if not line:
            continue
        try:
            data = json.loads(line)
            # SoundCloud sets are 'playlist' type  -  skip them for single-track search
            is_playlist = data.get("_type") == "playlist"
            if is_playlist:
                continue

            title = data.get("title", "Unknown")
            # SoundCloud uses 'uploader' rather than 'channel'
            channel = data.get("uploader", data.get("channel", "Unknown"))
            duration_secs = data.get("duration") or 0
            views = data.get("view_count")
            relevance_score, score_breakdown = score_search_result_with_breakdown(
                title, channel, query,
                duration_seconds=duration_secs or None,
                view_count=views,
            )

            results.append({
                "video_id": data.get("id", ""),
                "title": title,
                "channel": channel,
                "duration": parse_duration(duration_secs) if duration_secs else "",
                "thumbnail": data.get("thumbnail", ""),
                "is_playlist": False,
                "video_count": None,
                "source": "soundcloud",
                "source_url": data.get("webpage_url", data.get("url", "")),
                "quality": None,
                "relevance_score": relevance_score,
                "score_breakdown": score_breakdown,
                "slskd_username": None,
                "slskd_filename": None,
                "slskd_size": None,
            })
        except json.JSONDecodeError:
            continue
    return results


def search_soundcloud(query: str, limit: int) -> list[dict]:
    """Search SoundCloud via yt-dlp and return normalised results."""
    try:
        fetch_limit = max(limit * SOUNDCLOUD_SEARCH_MULTIPLIER, SOUNDCLOUD_SEARCH_MIN_FETCH)

        cmd = [
            "yt-dlp",
            "--dump-json",
            "--flat-playlist",
            "--no-warnings",
            f"scsearch{fetch_limit}:{query}",
        ]

        result, timed_out = run_ytdlp_with_retries(
            cmd,
            TIMEOUT_YTDLP_SEARCH,
            allow_cookie_fallback=False,
            operation=f"SoundCloud search for '{query}'",
        )

        if timed_out or result is None or result.returncode != 0:
            return []

        results = parse_soundcloud_search_results(result.stdout, query=query)
        results.sort(key=lambda x: x["relevance_score"], reverse=True)
        return results[:limit]

    except Exception as e:
        print(f"SoundCloud search error: {e}")
        return []


# ---------------------------------------------------------------------------
# Source registry  -  add new sources here
# ---------------------------------------------------------------------------

SOURCE_REGISTRY = {
    "youtube": {
        "label": "YouTube",
        "badge": "YT",
        "colour": "#ff0000",
        "search_fn": search_youtube,
        "has_preview": True,
    },
    "soundcloud": {
        "label": "SoundCloud",
        "badge": "SC",
        "colour": "#ff5500",
        "search_fn": search_soundcloud,
        "has_preview": True,
    },
    "zvu4no": {
        "label": "zvu4no",
        "badge": "ZV",
        "colour": "#7a6aee",
        "search_fn": search_zvu4no,
        "has_preview": True,
    },
    "freemp3cloud": {
        "label": "FreeMp3Cloud",
        "badge": "FMC",
        "colour": "#3a2fd6",
        "search_fn": search_freemp3cloud,
        "has_preview": True,
    },
    "soulseek": {
        "label": "Soulseek",
        "badge": "SLK",
        "colour": "#7c3aed",
        "search_fn": search_soulseek,
        "has_preview": False,
        "default_enabled": False,
    },
    "monochrome": {
        "label": "Monochrome",
        "badge": "MONO",
        "colour": "#0f766e",
        "search_fn": search_monochrome_source,
        "has_preview": True,
        "default_enabled": True,
    },
}

SEARCH_MAX_PER_SOURCE_BY_SOURCE = {
    "youtube": SEARCH_MAX_PER_SOURCE_YOUTUBE,
    "soundcloud": SEARCH_MAX_PER_SOURCE_SOUNDCLOUD,
    "zvu4no": SEARCH_MAX_PER_SOURCE_ZVU4NO,
    "freemp3cloud": SEARCH_MAX_PER_SOURCE_FREEMP3CLOUD,
    "soulseek": SEARCH_MAX_PER_SOURCE_SOULSEEK,
    "monochrome": SEARCH_MAX_PER_SOURCE_MONOCHROME,
}

_AUTOMATED_SEARCH_CACHE: OrderedDict[tuple, tuple[float, list[dict], dict | None]] = OrderedDict()
_AUTOMATED_SEARCH_CACHE_LOCK = threading.Lock()
_SOURCE_SEARCH_SLOTS: dict[str, tuple[int, threading.BoundedSemaphore]] = {}
_SOURCE_SEARCH_SLOTS_LOCK = threading.Lock()


class SourceSearchBusy(RuntimeError):
    """Raised when a provider was still busy when we ran out of patience.

    Emphatically NOT the same as "this provider has nothing for you". Treating
    the two alike is how a bulk import once wrote "No results found" against
    thirteen tracks it had never actually got round to searching for.
    """


def _source_search_slot(source_name: str) -> threading.BoundedSemaphore:
    """Return the bounded admission gate shared by searches for a provider."""
    try:
        configured = get_setting_int("search_concurrency", 1)
    except Exception:
        configured = 1
    limit = max(1, min(configured, 5))
    with _SOURCE_SEARCH_SLOTS_LOCK:
        existing = _SOURCE_SEARCH_SLOTS.get(source_name)
        if existing and existing[0] == limit:
            return existing[1]
        # A changed limit gets a fresh gate. In-flight holders finish against
        # the old object; new searches use the new bound without over-releasing
        # or mutating a live semaphore's private state.
        slot = threading.BoundedSemaphore(limit)
        _SOURCE_SEARCH_SLOTS[source_name] = (limit, slot)
        return slot


def _run_source_search(source_name: str, cfg: dict, query: str, limit: int,
                       retry_empty_soulseek: bool = True,
                       slot_wait: float = 0.0) -> list[dict]:
    """Run one provider, queueing politely for its slot rather than barging in.

    The slot stops several searches hammering one provider at once. It used to be
    a non-blocking grab, which suited an impatient user retyping their query and
    nobody else: every other caller got an instant refusal that was
    indistinguishable from a genuine miss. *slot_wait* lets each caller say how
    long it is prepared to queue, which is all the difference between a search
    that works and one that fails for no reason the user can see.

    slot_wait <= 0 keeps the original barge-in-or-give-up behaviour.
    """
    slot = _source_search_slot(source_name)
    acquired = slot.acquire(timeout=slot_wait) if slot_wait > 0 else slot.acquire(blocking=False)
    if not acquired:
        raise SourceSearchBusy(
            f"{source_name} was still busy after waiting {slot_wait:.0f}s"
            if slot_wait > 0 else
            f"{source_name} still has a search in progress"
        )
    try:
        search_fn = cfg["search_fn"]
        if source_name == "soulseek" and search_fn is search_soulseek:
            return search_fn(query, limit, retry_empty=retry_empty_soulseek)
        return search_fn(query, limit)
    finally:
        slot.release()


def _per_source_result_cap(source_name: str, result_limit: int, active_source_count: int) -> int:
    """Return a balanced merge cap for the current number of sources.

    The source-specific caps keep a prolific provider from flooding a normal
    multi-source search. When only a few sources are enabled (or healthy), raise
    that cap to an even share of the requested result count so they can still
    fill the page between them.
    """
    configured_cap = SEARCH_MAX_PER_SOURCE_BY_SOURCE.get(source_name, SEARCH_MAX_PER_SOURCE)
    if active_source_count <= 0:
        return 0
    fair_share = (result_limit + active_source_count - 1) // active_source_count
    return min(result_limit, max(configured_cap, fair_share))


def _mb_duration_lookup(query: str) -> float | None:
    """Return the MusicBrainz canonical duration for an artist/title query, or None.

    Only fires when the query contains a ' - ' separator AND doesn't request a
    specific variation (remix, live, etc.). Silent on any failure.
    """
    if _query_has_variation(query):
        return None
    artist, title = _parse_query_artist_title(query)
    if not artist or not title:
        return None
    return fetch_mb_expected_duration(artist, title)


def _mb_album_lookup(query: str) -> dict | None:
    """Find the MusicBrainz album a track belongs to, if we can figure it out.

    Parses "Artist - Title" from the query, then does a proper recording search
    via MusicBrainz (artist + title) and picks the best-scored release using the
    same release-group heuristics as post-download tagging. Much more reliable
    than fuzzy-matching a track title against an artist's album discography.

    Returns {artist_name, artist_mbid, album_title, release_mbid} or None.
    """
    if _query_has_variation(query):
        return None
    artist, title = _parse_query_artist_title(query)
    if not artist or not title:
        return None

    mb = lookup_musicbrainz(artist, title)
    if not mb or not mb.get("album") or not mb.get("release_mbid"):
        return None

    # Get the artist MBID for the frontend album browser
    artists = search_artist_mbid(artist)
    artist_mbid = artists[0]["mbid"] if artists else None
    artist_name = artists[0]["name"] if artists else (mb.get("artist") or artist)

    return {
        "artist_name": artist_name,
        "artist_mbid": artist_mbid,
        "album_title": mb["album"],
        "release_mbid": mb["release_mbid"],
    }


# What each source is prepared to tell us about quality *before* we download it,
# which is a shorter list than you might hope:
#
#   Soulseek     "FLAC", "FLAC 24bit/96kHz", "MP3 320", "AAC 256", "WAV", ...
#   FreeMp3Cloud "320kbps" or "128kbps"
#   Monochrome   "LOSSLESS"                             (a tier, not kbps)
#   zvu4no       "MP3"                                  (format only, no bitrate)
#   YouTube      nothing at all
#   SoundCloud   nothing at all
#
# So a "minimum bitrate" filter cannot be honest about every source. We bucket
# into the same 1-5 tier scale the upgrades scanner already uses, and anything
# that declines to say gets tier 0, "unknown", which the UI treats separately
# rather than silently guessing on the user's behalf.
_LOSSLESS_QUALITY_WORDS = ("flac", "wav", "alac", "lossless", "aiff", "ape", "wavpack")


def quality_tier_of_result(result: dict) -> int:
    """Bucket a search result's declared quality into the shared 1-5 tier scale.

    Returns TIER_UNKNOWN (0) when the source did not say, which is not the same
    as "bad": a YouTube result is simply an unknown quantity until it lands.
    """
    label = (result.get("quality") or "").strip()
    if not label:
        return TIER_UNKNOWN

    lowered = label.lower()
    if any(word in lowered for word in _LOSSLESS_QUALITY_WORDS):
        return TIER_LOSSLESS

    # Monochrome's "HIGH" is Tidal's lossy tier: 320 kbps AAC in practice.
    if lowered == "high":
        return TIER_LOSSY_320

    kbps_match = re.search(r"(\d{2,4})\s*(?:kbps)?", lowered)
    if kbps_match:
        try:
            return kbps_to_tier(int(kbps_match.group(1)))
        except ValueError:
            return TIER_UNKNOWN

    # A bare format name ("MP3") tells us the container and nothing else.
    return TIER_UNKNOWN


def project_search_result(item: dict) -> dict:
    """Project a raw source result into the browser/API result contract.

    Keeping this beside the source normalisation makes it usable by both the
    blocking and streaming endpoints without importing ``app`` (which starts
    schedulers as a module side effect).  In particular, do not discard an
    explicit artist: for Soulseek, ``channel`` is the peer sharing the file,
    not the performer.
    """
    return {
        "video_id": item["video_id"],
        "title": item["title"],
        "artist": item.get("artist"),
        "channel": item["channel"],
        "duration": item["duration"],
        "thumbnail": item["thumbnail"],
        "is_playlist": item.get("is_playlist", False),
        "video_count": item.get("video_count"),
        "source": item["source"],
        "source_url": item.get("source_url"),
        "quality": item["quality"],
        "relevance_score": item["relevance_score"],
        "quality_tier": item.get("quality_tier", quality_tier_of_result(item)),
        "slskd_username": item["slskd_username"],
        "slskd_filename": item["slskd_filename"],
        "slskd_size": item.get("slskd_size") or item.get("size"),
        # Detail the card shows when the source bothered to tell us. Soulseek
        # supplies size and bitrate per file and a match confidence per path;
        # every other source leaves these None and the card simply omits them.
        "album": item.get("album") or None,
        "year": item.get("year"),
        "size_bytes": item.get("size") or item.get("slskd_size"),
        "bitrate": item.get("bitrate"),
        "match_confidence": item.get("match_confidence"),
        "queue_length": item.get("queue_length"),
    }


def _stamp_quality_tiers(results: list[dict]) -> None:
    """Add quality_tier to each result in place, for the UI's quality filter."""
    for result in results:
        result["quality_tier"] = quality_tier_of_result(result)


def _apply_mb_duration_scores(results: list[dict], expected_duration_secs: float) -> None:
    """Mutate relevance_score on each result based on delta from MB expected duration.

    Operates in-place  -  call after blacklist filtering, before final sort.
    """
    for r in results:
        dur_str = r.get("duration", "")
        if not dur_str:
            continue
        # duration field is stored as "M:SS" or "H:MM:SS" string
        parts = dur_str.split(":")
        try:
            if len(parts) == 2:
                secs = int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:
                secs = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            else:
                continue
        except (ValueError, IndexError):
            continue
        if secs <= 0:
            continue
        delta_ratio = abs(secs - expected_duration_secs) / expected_duration_secs
        if delta_ratio <= 0.02:
            r["relevance_score"] += 40
            r.setdefault("score_breakdown", []).append("mb_search_duration=+40")
        elif delta_ratio <= 0.05:
            r["relevance_score"] += 20
            r.setdefault("score_breakdown", []).append("mb_search_duration=+20")
        elif delta_ratio <= 0.10:
            pass
        elif delta_ratio <= 0.25:
            r["relevance_score"] -= 30
            r.setdefault("score_breakdown", []).append("mb_search_duration=-30")
        else:
            r["relevance_score"] -= 60
            r.setdefault("score_breakdown", []).append("mb_search_duration=-60")


def log_ranked_results(context: str, query: str, results: list[dict], top_n: int = 3) -> None:
    """Print the top scored candidates with their main score reasons."""
    if not results:
        print(f"{context}: no candidates for '{query}'")
        return
    print(f"{context}: top {min(top_n, len(results))} candidates for '{query}'")
    for idx, r in enumerate(results[:top_n], start=1):
        title = (r.get("title") or "").strip() or "Unknown"
        channel = (r.get("channel") or "").strip() or "Unknown"
        source = r.get("source") or "unknown"
        score = r.get("relevance_score")
        breakdown = format_score_breakdown(r.get("score_breakdown") or [])
        print(f"  {idx}. [{source}] {channel} - {title} (score {score}) :: {breakdown}")


def _apply_blacklist_filter(results: list[dict], source: str | None = None) -> list[dict]:
    """Remove blacklisted videos and penalise blacklisted uploaders.

    Loads the blacklist once per call (not per result)  -  the lists are small
    so this is cheap and avoids hammering the DB.
    """
    blocked_ids = get_blacklisted_video_ids()
    # Collect blocked uploaders for all relevant sources in one pass
    sources_to_check = {source} if source else {r.get("source", "youtube") for r in results}
    blocked_uploaders: dict[str, set[str]] = {}
    for s in sources_to_check:
        blocked_uploaders[s] = get_blacklisted_uploaders(s)

    filtered = []
    for r in results:
        if r.get("video_id") in blocked_ids:
            continue
        r_source = r.get("source", "youtube")
        channel = (r.get("channel") or "").lower()
        if channel and channel in blocked_uploaders.get(r_source, set()):
            r["relevance_score"] = r.get("relevance_score", 0) - _BLACKLIST_UPLOADER_PENALTY
        filtered.append(r)
    return filtered


def _enabled_sources(include_soulseek: bool = False) -> dict:
    """Return the subset of SOURCE_REGISTRY that is currently enabled in settings."""
    return {
        name: cfg for name, cfg in SOURCE_REGISTRY.items()
        if (include_soulseek or name != "soulseek")
        and get_setting_bool(f"source_{name}_enabled", cfg.get("default_enabled", True))
    }


def search_source(source: str, query: str, limit: int,
                  slot_wait: float = SEARCH_SLOT_WAIT_INTERACTIVE) -> list[dict]:
    """Search a single registered source.

    Unlike the multi-source fan-out there is nothing to fall back on here, so a
    busy provider is worth waiting for. If it is still busy afterwards the
    SourceSearchBusy propagates, and the route turns it into an honest "try again
    shortly" rather than pretending the server broke.
    """
    if source not in SOURCE_REGISTRY:
        raise ValueError(f"Unknown search source: {source}")
    cfg = SOURCE_REGISTRY[source]
    if not get_setting_bool(f"source_{source}_enabled", cfg.get("default_enabled", True)):
        return []

    # Fire MB duration lookup in parallel with the source search so it doesn't
    # add any latency  -  both finish before we sort and return.
    with ThreadPoolExecutor(max_workers=2) as pool:
        search_future = pool.submit(
            _run_source_search, source, cfg, query, limit, slot_wait=slot_wait
        )
        mb_future = pool.submit(_mb_duration_lookup, query)
        results = search_future.result()
        expected_dur = mb_future.result()

    results = _apply_blacklist_filter(results, source=source)
    if expected_dur:
        _apply_mb_duration_scores(results, expected_dur)
    results.sort(key=lambda x: x["relevance_score"], reverse=True)
    return results[:limit]


def _search_all_events(
    query: str,
    limit: int,
    sources: list[str] | None = None,
    include_soulseek: bool = False,
    enforce_availability: bool = True,
    slot_wait: float = 0.0,
):
    """Run the multi-source fan-out, yielding progress events as sources land.

    This is the single source of truth for multi-source search. The blocking
    search_all() drains it into a merged list; the streaming search endpoint
    forwards the very same events to the browser as NDJSON, so the two never
    drift apart. Events (one dict per yield):

      {"type": "start", "query": ..., "sources": [names]}
      {"type": "source", "source": name, "status": "skipped", "reason": ...}
      {"type": "source", "source": name, "status": "done", "count": n, "results": [...]}
      {"type": "source", "source": name, "status": "timeout"}
      {"type": "source", "source": name, "status": "busy"}
      {"type": "source", "source": name, "status": "error"}
      {"type": "album_suggestion", <album fields>}
      {"type": "done"}

    Per-source results are already blacklist-filtered, capped and scored (with the
    MB duration bonus applied once the MB lookup has resolved, which in practice
    beats the slower source searches), so a consumer only needs to sort the union.

    *enforce_availability* gates the servicecheck parking: True for the normal
    multi-source search, False when the caller explicitly asked for a specific
    source and should get it even if it's currently parked.
    """
    active = _enabled_sources(include_soulseek=include_soulseek)
    if sources is not None:
        # An explicit source list is an allow-list, not a preference. If none of
        # its sources are currently enabled, return no results rather than quietly
        # widening the search to every enabled provider.
        requested = {
            str(source).strip().lower()
            for source in sources
            if source is not None and str(source).strip()
        }
        active = {k: v for k, v in active.items() if k in requested}

    parked: list[str] = []
    if enforce_availability:
        # Park the sources last seen unhealthy so their results don't show up
        # only to fall over at play or download time. Stale verdicts refresh in
        # a background thread; a probe must NEVER block the search itself, or a
        # a slow Monochrome playback probe turns every search into dead air
        # (this happened; nobody enjoyed it).
        servicecheck.refresh_sources_async(set(active))
        available = {n: c for n, c in active.items() if servicecheck.is_source_available(n)}
        parked = [n for n in active if n not in available]
        active = available

    yield {"type": "start", "query": query, "sources": list(active)}
    for name in parked:
        yield {"type": "source", "source": name, "status": "skipped", "reason": "offline"}

    if not active:
        yield {"type": "done"}
        return

    # NOTE: do NOT wrap this pool in a `with` block. Exiting a ThreadPoolExecutor
    # context manager calls shutdown(wait=True), which blocks until every source
    # has finished, slowest included, which would defeat the whole point of the
    # wall-clock deadline below. We collect with a hard deadline and walk away
    # from any source still dawdling past it.
    futures = {}
    pool = ThreadPoolExecutor(max_workers=len(active) + 2)
    expected_dur = None
    album_suggestion = None
    try:
        retry_empty_soulseek = len(active) == 1
        for name, cfg in active.items():
            futures[
                pool.submit(
                    _run_source_search,
                    name,
                    cfg,
                    query,
                    limit,
                    retry_empty_soulseek,
                    slot_wait=slot_wait,
                )
            ] = name
        # MB lookups run alongside the source searches at no extra cost
        mb_future = pool.submit(_mb_duration_lookup, query)
        mb_album_future = pool.submit(_mb_album_lookup, query)

        timed_out = False
        try:
            for future in as_completed(futures, timeout=SEARCH_ALL_DEADLINE):
                source_name = futures[future]
                # Grab the MB duration the instant it's ready so this and every
                # later batch gets the duration bonus.
                if expected_dur is None and mb_future.done():
                    try:
                        expected_dur = mb_future.result()
                    except Exception:
                        expected_dur = None
                try:
                    source_results = future.result()
                except SourceSearchBusy as e:
                    # Distinct from an error on purpose. We never got to ask this
                    # provider anything, so its silence says nothing about whether
                    # it has the track, and the caller must not read it as a miss.
                    print(f"search_all: {source_name} busy: {e}")
                    yield {"type": "source", "source": source_name, "status": "busy"}
                    continue
                except Exception as e:
                    print(f"search_all: {source_name} failed: {e}")
                    yield {"type": "source", "source": source_name, "status": "error"}
                    continue
                # Keep the normal per-source flood protection, but let a small
                # enabled/healthy source set collectively fill the requested page.
                per_source_cap = _per_source_result_cap(source_name, limit, len(active))
                batch = _apply_blacklist_filter(source_results[:per_source_cap], source=source_name)
                _stamp_quality_tiers(batch)
                if expected_dur:
                    _apply_mb_duration_scores(batch, expected_dur)
                batch.sort(key=lambda x: x["relevance_score"], reverse=True)
                servicecheck.record_search_success(source_name)
                yield {
                    "type": "source",
                    "source": source_name,
                    "status": "done",
                    "count": len(batch),
                    "results": batch,
                }
        except FuturesTimeoutError:
            # Deadline hit. The laggards keep running in the background with nobody
            # waiting on them; tell the client which ones didn't make it.
            timed_out = True

        if timed_out:
            for f, name in futures.items():
                if not f.done():
                    # Three consecutive deadline blow-outs and servicecheck parks
                    # the source, so a limping platform can't bleed a 500-track
                    # bulk import 30 seconds at a time.
                    servicecheck.record_search_timeout(name)
                    yield {"type": "source", "source": name, "status": "timeout"}
            print(f"search_all: {SEARCH_ALL_DEADLINE}s deadline hit")

        # MB album suggestion is best-effort; if it hasn't landed by now, skip it.
        try:
            album_suggestion = mb_album_future.result(timeout=0.01) if mb_album_future.done() else None
        except Exception:
            album_suggestion = None
        if album_suggestion:
            yield {"type": "album_suggestion", **album_suggestion}
    finally:
        # wait=False so a stuck source can't re-introduce the very hang we killed;
        # cancel_futures tidies up anything that never got to start.
        pool.shutdown(wait=False, cancel_futures=True)

    yield {"type": "done"}


def search_all(query: str, limit: int, sources: list[str] | None = None,
               include_soulseek: bool = False, slot_wait: float = 0.0,
               status_out: dict | None = None,
               return_all_source_results: bool = False) -> tuple[list[dict], dict | None]:
    """Search enabled sources in parallel, merge by relevance score.

    Thin blocking consumer of _search_all_events: it drains the progress events
    into one merged, score-sorted list. If *sources* is provided (list of source
    IDs), only those are used. If none are enabled, the result is empty; an
    explicit allow-list is never widened to other sources.

    Returns (results, album_suggestion) where album_suggestion is a dict with
    artist_name, artist_mbid, album_title, release_mbid, or None if the query
    didn't resolve to a known album.

    Pass *status_out* (an empty dict) to find out which sources never answered:
    it comes back with "busy" and "timeout" lists. An empty result with a
    non-empty "busy" means nobody has told you the track doesn't exist, only
    that we didn't manage to ask, which is a very different thing to act on.
    """
    all_results: list[dict] = []
    album_suggestion = None
    busy: list[str] = []
    timed_out: list[str] = []
    for ev in _search_all_events(query, limit, sources=sources,
                                 include_soulseek=include_soulseek, slot_wait=slot_wait):
        if ev["type"] == "source" and ev["status"] == "done":
            all_results.extend(ev["results"])
        elif ev["type"] == "source" and ev["status"] == "busy":
            busy.append(ev["source"])
        elif ev["type"] == "source" and ev["status"] == "timeout":
            timed_out.append(ev["source"])
        elif ev["type"] == "album_suggestion":
            album_suggestion = {k: v for k, v in ev.items() if k != "type"}
    if status_out is not None:
        status_out["busy"] = busy
        status_out["timeout"] = timed_out
    all_results.sort(key=lambda x: x["relevance_score"], reverse=True)
    return (
        all_results if return_all_source_results else all_results[:limit],
        album_suggestion,
    )


def _automated_search_cache_key(query: str, limit: int, sources: list[str] | None,
                                include_soulseek: bool,
                                return_all_source_results: bool = False) -> tuple:
    """Build a key that changes when the usable source selection changes."""
    active = _enabled_sources(include_soulseek=include_soulseek)
    requested = None
    if sources is not None:
        requested = tuple(sorted({
            str(source).strip().lower()
            for source in sources
            if source is not None and str(source).strip()
        }))
        active = {name: cfg for name, cfg in active.items() if name in requested}
    usable = tuple(sorted(
        name for name in active if servicecheck.is_source_available(name)
    ))
    return (
        (query or "").strip().casefold(), int(limit), requested, usable,
        bool(include_soulseek), bool(return_all_source_results),
    )


def clear_automated_search_cache() -> None:
    """Clear cached automated searches, mainly useful to tests and maintenance."""
    with _AUTOMATED_SEARCH_CACHE_LOCK:
        _AUTOMATED_SEARCH_CACHE.clear()


def search_all_cached(query: str, limit: int, sources: list[str] | None = None,
                      include_soulseek: bool = False,
                      slot_wait: float = SEARCH_SLOT_WAIT_AUTOMATED,
                      status_out: dict | None = None,
                      return_all_source_results: bool = False) -> tuple[list[dict], dict | None]:
    """Search for an automated flow, reusing a recent identical result safely.

    Deep copies are returned and stored because bulk priority boosting mutates
    result scores. The short TTL keeps direct-download links fresh enough to use,
    while the bounded LRU prevents a large library becoming a second database.

    Automated callers queue properly for a busy provider by default. Nobody is
    watching a bulk import, and waiting a few seconds is enormously preferable to
    marking a track failed forever over a collision that lasted no time at all.
    """
    key = _automated_search_cache_key(
        query, limit, sources, include_soulseek, return_all_source_results
    )
    now = time.time()
    with _AUTOMATED_SEARCH_CACHE_LOCK:
        expired = [
            cached_key for cached_key, (created_at, _results, _album) in _AUTOMATED_SEARCH_CACHE.items()
            if now - created_at >= AUTOMATED_SEARCH_CACHE_TTL_SECONDS
        ]
        for cached_key in expired:
            _AUTOMATED_SEARCH_CACHE.pop(cached_key, None)
        cached = _AUTOMATED_SEARCH_CACHE.get(key)
        if cached:
            _AUTOMATED_SEARCH_CACHE.move_to_end(key)
            if status_out is not None:
                # A cache hit asked nobody, so nobody was busy.
                status_out["busy"] = []
                status_out["timeout"] = []
            return deepcopy(cached[1]), deepcopy(cached[2])

    results, album_suggestion = search_all(
        query, limit, sources=sources, include_soulseek=include_soulseek,
        slot_wait=slot_wait, status_out=status_out,
        return_all_source_results=return_all_source_results,
    )
    # An empty search is often a provider having a brief wobble. Caching that
    # would turn a momentary miss into fifteen minutes of determined failure.
    if results:
        with _AUTOMATED_SEARCH_CACHE_LOCK:
            _AUTOMATED_SEARCH_CACHE[key] = (
                now, deepcopy(results), deepcopy(album_suggestion)
            )
            _AUTOMATED_SEARCH_CACHE.move_to_end(key)
            while len(_AUTOMATED_SEARCH_CACHE) > AUTOMATED_SEARCH_CACHE_MAX_ENTRIES:
                _AUTOMATED_SEARCH_CACHE.popitem(last=False)
    return results, album_suggestion


def get_available_sources() -> list[dict]:
    """Return source metadata for the frontend source selector."""
    return [
        {
            "id": name,
            "label": cfg["label"],
            "badge": cfg["badge"],
            "colour": cfg["colour"],
            "enabled": get_setting_bool(f"source_{name}_enabled", cfg.get("default_enabled", True)),
            "has_preview": bool(cfg.get("has_preview", True)),
        }
        for name, cfg in SOURCE_REGISTRY.items()
    ]
