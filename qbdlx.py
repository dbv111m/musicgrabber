"""
MusicGrabber - direct Qobuz fallback

When Monochrome's own tracks API cannot resolve a track, this
module talks to the official Qobuz API directly, signing requests with a shared
free-account token (the same pool the qbdlx web UI at
qbdlx.launchpd.cloud hands out under its "Free account" tab).

The flow mirrors classic qobuz-dl:
  1. GET a pool of {token, app_id, app_secret, country} from the shared webhook.
  2. catalog/search?query=ISRC  ->  track_id
  3. track/getFileUrl (signed)  ->  a real streaming-qobuz-std.akamaized.net URL

Signature scheme (verified live):
  request_sig = MD5("trackgetFileUrl" + "format_id"+fmt + "intent"+"stream"
                    + "track_id"+id + ts + app_secret)
  headers: X-App-Id, X-User-Auth-Token

Important: the free shared tokens resolve to 16-bit/44.1kHz lossless FLAC
(format_id 6), NOT 24-bit hi-res, no matter which format you ask for. That's
fine for a fallback. A genuine FLAC beats a download that face-plants.

Some tokens in the pool get quietly downgraded by Qobuz to 30-second preview
MP3s (the response carries "sample": true and format_id 5, whatever format_id
was actually requested). We walk straight past those and keep trying tokens
until one hands back the real thing, because nobody asked for the chorus on
a loop.

"""

import hashlib
import threading
import time
import httpx

from constants import (
    QBDLX_FALLBACK_ENABLED,
    QBDLX_SHARED_TOKENS_URL,
    QBDLX_QOBUZ_API_BASE,
    QBDLX_TOKEN_CACHE_TTL,
    QBDLX_TOKEN_FAILURE_COOLDOWN,
    TIMEOUT_MONOCHROME_SEARCH,
)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
}

# Cached token pool: list of {token, app_id, app_secret, country}. The pool
# rotates upstream, so we re-fetch it every QBDLX_TOKEN_CACHE_TTL seconds.
_token_cache: list[dict] = []
_token_cache_at: float = 0.0
_token_lock = threading.Lock()
# When the pool host last failed to answer. Inside the cooldown we hand back
# whatever we have (often nothing) at once, so callers fall through to other
# sources instead of queueing behind the lock for a timeout apiece.
_token_fetch_failed_at: float = 0.0

# The token that last handed back a genuine stream, so the next download starts
# with the one we know works instead of trudging through the pool from the top.
# Measured 2026-08-03: 3 of 28 tokens could actually deliver, and they were the
# last three in the pool, so a cold walk cost ~100 seconds to reach a 0.24s
# answer. Tokens rot without warning, so this is a hint, never a guarantee: if
# the remembered one has since been downgraded we simply carry on down the list.
# Persisted to the settings table so a container restart doesn't pay the cold
# walk again either; restored once per process, best-effort (see
# _ensure_good_token_restored). Tests short-circuit the restore so the unit
# suite never touches a real database, see tests/test_qbdlx.py::setup_function.
_good_token: str | None = None
_good_token_lock = threading.Lock()
_good_token_restore_attempted = False

# Tokens proven dead (or downgraded to sample-only) this cache cycle, so
# resolving track two of a playlist doesn't re-pay the timeout for a token
# track one already ruled out. Cleared whenever the pool is actually refetched
# with fresh data (see _fetch_shared_tokens); in-memory only, no need to
# survive a restart since it describes a cycle that will have moved on by then.
_known_bad_tokens: set[str] = set()
_known_bad_lock = threading.Lock()


def qbdlx_enabled() -> bool:
    """Whether the qbdlx fallback is allowed to run.

    Env var locks it; otherwise the DB setting wins, defaulting to the env-driven
    constant (True out of the box).
    """
    from settings import get_setting_bool
    return get_setting_bool("monochrome_qbdlx_fallback_enabled", QBDLX_FALLBACK_ENABLED)


def _fetch_shared_tokens(force: bool = False) -> list[dict]:
    """Return the shared token pool, cached for QBDLX_TOKEN_CACHE_TTL seconds.

    Returns whatever we last had on a fetch failure rather than blowing up; a
    stale token still beats no token, and the caller treats an empty list as
    "fallback unavailable" anyway.
    """
    global _token_cache, _token_cache_at, _token_fetch_failed_at
    with _token_lock:
        fresh = _token_cache and (time.time() - _token_cache_at) < QBDLX_TOKEN_CACHE_TTL
        if fresh and not force:
            return _token_cache
        cooling = (time.time() - _token_fetch_failed_at) < QBDLX_TOKEN_FAILURE_COOLDOWN
        if cooling and not force:
            return _token_cache

        try:
            resp = httpx.get(
                QBDLX_SHARED_TOKENS_URL,
                headers=_HEADERS,
                timeout=TIMEOUT_MONOCHROME_SEARCH,
                follow_redirects=True,
            )
            resp.raise_for_status()
            data = resp.json()
            tokens = [
                t for t in (data if isinstance(data, list) else [])
                if t.get("token") and t.get("app_id") and t.get("app_secret")
            ]
            if tokens:
                _token_cache = tokens
                _token_cache_at = time.time()
                with _known_bad_lock:
                    _known_bad_tokens.clear()  # fresh pool, fresh chances
                _token_fetch_failed_at = 0.0
                return tokens
            print("qbdlx: shared token pool came back empty")
        except Exception as exc:
            print(
                f"qbdlx: failed to fetch shared tokens: {exc}; "
                f"not asking again for {QBDLX_TOKEN_FAILURE_COOLDOWN}s"
            )

        _token_fetch_failed_at = time.time()
        return _token_cache  # last known good, possibly empty


def _signed_call(token: dict, path: str, params: dict, signed_concat: str | None = None) -> dict | None:
    """Make a (optionally signed) Qobuz API call with this token. None on failure."""
    p = dict(params)
    p["app_id"] = token["app_id"]
    if signed_concat is not None:
        ts = int(time.time())
        sig = hashlib.md5((signed_concat + str(ts) + token["app_secret"]).encode()).hexdigest()
        p["request_ts"] = ts
        p["request_sig"] = sig
    try:
        resp = httpx.get(
            f"{QBDLX_QOBUZ_API_BASE.rstrip('/')}/{path}",
            params=p,
            headers={
                **_HEADERS,
                "X-App-Id": str(token["app_id"]),
                "X-User-Auth-Token": token["token"],
            },
            timeout=TIMEOUT_MONOCHROME_SEARCH,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        print(f"qbdlx: Qobuz {path} failed ({token.get('country', '?')}): {exc}")
        return None


def _resolve_track_id(token: dict, isrc: str) -> int | None:
    """Resolve an ISRC to a Qobuz track id via catalog/search."""
    body = _signed_call(token, "catalog/search", {"query": isrc, "limit": 5})
    if not body:
        # The call itself failed (network, auth, timeout): that's the token's
        # fault, not this ISRC's. An empty-but-successful result further down
        # is a different story, so that path leaves the token's name clean.
        _mark_token_bad(token.get("token"))
        return None
    items = ((body.get("tracks") or {}).get("items")) or []
    # Prefer an exact ISRC match; fall back to the top result if Qobuz doesn't
    # echo the ISRC back on the item.
    for item in items:
        if (item.get("isrc") or "").upper() == isrc.upper():
            return item.get("id")
    return items[0].get("id") if items else None


def resolve_qobuz_track_id(isrc: str) -> int | None:
    """Resolve an ISRC to a Qobuz track id using the shared qbdlx token pool."""
    if not qbdlx_enabled():
        return None
    if not isrc:
        return None

    tokens = _fetch_shared_tokens()
    for token in _usable_tokens(tokens):
        track_id = _resolve_track_id(token, isrc)
        if track_id:
            return track_id
    return None


def search_qobuz_catalog(query: str, limit: int = 10) -> list[dict]:
    """Search the Qobuz catalogue by free text via the shared token pool.

    This is the search-leg counterpart to the download fallback: when
    Monochrome's hifi-api is face-down, we can still find tracks by asking
    Qobuz directly (the same catalog/search the ISRC resolver uses, just with
    a real "artist title" query). Returns the raw Qobuz track item dicts so the
    caller does the scoring/shaping and result formatting stays in one place.

    Returns [] when the fallback is disabled, the query is empty, or no token
    in the pool can answer.
    """
    if not qbdlx_enabled():
        return []
    if not (query or "").strip():
        return []

    tokens = _fetch_shared_tokens()
    for token in _usable_tokens(tokens):
        body = _signed_call(token, "catalog/search", {"query": query, "limit": limit})
        if not body:
            _mark_token_bad(token.get("token"))
            continue
        items = ((body.get("tracks") or {}).get("items")) or []
        if items:
            return items
    return []


def lookup_qobuz_isrc(isrc: str) -> tuple[list[dict], bool]:
    """Return exact Qobuz catalogue matches and whether every API call failed.

    The boolean distinguishes a clean catalogue miss from an unavailable token
    pool. Search callers may discard a confirmed miss, but keep an unverified
    result for the tracks API to settle at download time.
    """
    if not qbdlx_enabled() or not isrc:
        return [], True

    tokens = _fetch_shared_tokens()
    if not tokens:
        return [], True

    for token in _usable_tokens(tokens):
        body = _signed_call(token, "catalog/search", {"query": isrc, "limit": 10})
        if body is None:
            _mark_token_bad(token.get("token"))
            continue
        items = ((body.get("tracks") or {}).get("items")) or []
        matches = [item for item in items if (item.get("isrc") or "").upper() == isrc.upper()]
        return matches, False
    return [], True


def _mark_token_bad(token: str | None) -> None:
    """Note a token as dead for the rest of this cache cycle.

    Only call this for a token-level fault (the call itself failed, or Qobuz
    downgraded the entitlement), never for a clean "this ISRC isn't in the
    catalogue" answer: that would wrongly write off a perfectly healthy token
    just because one track was missing.
    """
    if not token:
        return
    with _known_bad_lock:
        _known_bad_tokens.add(token)


def _load_persisted_good_token() -> str | None:
    """Best-effort read of the last known-good token from the settings table."""
    try:
        from db import db_conn
        with db_conn() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'qbdlx_good_token'"
            ).fetchone()
        return row[0] if row and row[0] else None
    except Exception as exc:
        print(f"qbdlx: could not restore last-good token: {exc}")
        return None


def _persist_good_token(token: str | None) -> None:
    """Best-effort write of the last known-good token.

    So a container restart doesn't pay the cold walk again: see the 2026-08-03
    measurement above, ~100 seconds to find a live token from a standing start.
    """
    if not token:
        return
    try:
        from db import db_conn
        with db_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('qbdlx_good_token', ?)",
                (token,),
            )
            conn.commit()
    except Exception as exc:
        print(f"qbdlx: could not persist good token: {exc}")


def _ensure_good_token_restored() -> None:
    """Seed _good_token from the DB once per process, if nothing has beaten it there.

    Best-effort and one-shot: if the read fails, or something already set a
    favourite this session (a real download beat us to it), just carry on.
    """
    global _good_token, _good_token_restore_attempted
    with _good_token_lock:
        if _good_token_restore_attempted:
            return
        _good_token_restore_attempted = True
        already_have_one = _good_token is not None
    if already_have_one:
        return
    restored = _load_persisted_good_token()
    if restored:
        with _good_token_lock:
            if _good_token is None:
                _good_token = restored


def _remember_good_token(token: dict) -> None:
    """Note the token that just delivered, so the next call tries it first."""
    global _good_token
    tok = token.get("token")
    with _good_token_lock:
        changed = tok != _good_token
        _good_token = tok
    if changed:
        # Same favourite as last time: skip the write. A big playlist can call
        # this hundreds of times in a row once the favourite settles, and the
        # settings table doesn't need the same value rewritten every time.
        _persist_good_token(tok)


def _tokens_best_first(tokens: list[dict]) -> list[dict]:
    """Return the pool with the last known-good token promoted to the front.

    Everything else keeps its original order, so a stale favourite costs us one
    wasted attempt rather than a reshuffled pool we can no longer reason about.
    """
    _ensure_good_token_restored()
    with _good_token_lock:
        favourite = _good_token
    if not favourite:
        return tokens
    promoted = [t for t in tokens if t.get("token") == favourite]
    if not promoted:
        return tokens  # pool has rotated since; no harm done
    return promoted + [t for t in tokens if t.get("token") != favourite]


def _usable_tokens(tokens: list[dict]) -> list[dict]:
    """Favourite first, then this cycle's known-bad tokens filtered out.

    Known-bad tokens are excluded rather than merely deprioritised: they rot
    without warning and don't un-rot inside one cache cycle, so there's no
    point paying their timeout again before the pool itself is refetched. If
    filtering would leave nothing to try, fall back to the full list; a last
    honest attempt beats refusing to try at all.
    """
    with _known_bad_lock:
        bad = set(_known_bad_tokens)
    filtered = [t for t in tokens if t.get("token") not in bad]
    return _tokens_best_first(filtered or tokens)


def _pool_health_note(tokens: list[dict]) -> str:
    """Human-readable "N/M usable this cycle" note for health-check reasons.

    Built from what we've *learned* while actually resolving real tracks, not
    a fresh sweep of the whole pool: probing all 28 tokens just to produce a
    number would be hammering a shared free resource for a stat nobody asked
    the tokens themselves to pay for.
    """
    if not tokens:
        return ""
    with _known_bad_lock:
        bad = len(_known_bad_tokens.intersection(t.get("token") for t in tokens))
    return f"{len(tokens) - bad}/{len(tokens)} shared tokens usable this cycle"


def resolve_qobuz_stream_url(isrc: str, quality_fmt: int) -> str | None:
    """Resolve an ISRC to a direct Qobuz CDN FLAC URL via the shared tokens.

    Tries each token in the pool until one yields a stream URL, starting with
    whichever token worked last time. Returns None when the fallback is
    disabled, the pool is empty/unreachable, or no token can resolve the track
    (in which case the caller keeps whatever error it had).
    """
    if not qbdlx_enabled():
        return None
    if not isrc:
        return None

    tokens = _fetch_shared_tokens()
    if not tokens:
        print("qbdlx: no shared tokens available, cannot fall back")
        return None

    for token in _usable_tokens(tokens):
        track_id = _resolve_track_id(token, isrc)
        if not track_id:
            continue
        concat = f"trackgetFileUrlformat_id{quality_fmt}intentstreamtrack_id{track_id}"
        body = _signed_call(
            token,
            "track/getFileUrl",
            {"track_id": track_id, "format_id": quality_fmt, "intent": "stream"},
            signed_concat=concat,
        )
        if not body:
            _mark_token_bad(token.get("token"))
            continue
        if body.get("sample"):
            # This token's entitlement has been downgraded server-side: Qobuz
            # hands back a 30-second preview instead of the track, no matter
            # which format_id we asked for. Not a stream, move on, and don't
            # bother asking this token again until the pool rotates.
            _mark_token_bad(token.get("token"))
            print(
                f"qbdlx: token ({token.get('country', '?')}) only offered a "
                f"sample for ISRC {isrc}, trying next token"
            )
            continue
        url = body.get("url") or ""
        if url:
            served_fmt = body.get("format_id")
            _remember_good_token(token)
            print(
                f"qbdlx: resolved ISRC {isrc} via direct Qobuz "
                f"({token.get('country', '?')} token, format_id {served_fmt})"
            )
            return url

    print(f"qbdlx: no token could resolve a stream for ISRC {isrc} ({_pool_health_note(tokens)})")
    return None


def download_leg_healthy() -> tuple[bool, str]:
    """Can the qbdlx fallback actually serve a FLAC right now?

    Used by servicecheck when Monochrome's tracks API is unavailable.
    Probes Radiohead's Creep, a stable known ISRC. The reason string always
    carries the usable-token count, healthy or not, so degradation shows up before
    the pool is fully dead rather than only once it is.
    """
    if not qbdlx_enabled():
        return False, "qbdlx fallback disabled"
    tokens = _fetch_shared_tokens()
    url = resolve_qobuz_stream_url("GBAYE9200070", 6)
    note = _pool_health_note(tokens)  # after the probe, so it reflects what that probe just learned
    if url:
        return True, note
    return False, f"qbdlx could not resolve a stream ({note})" if note else "qbdlx could not resolve a stream"
