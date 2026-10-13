"""
MusicGrabber - YouTube / yt-dlp Operations

Cookie handling, bot backoff, search, and scoring.
"""

import json
import os
import re
import random
import subprocess
import threading
import time
from pathlib import Path

from constants import (
    BOT_BACKOFF_MIN_SECONDS, BOT_BACKOFF_MAX_SECONDS,
    COOKIES_FILE, TIMEOUT_YTDLP_SEARCH,
    YTDLP_403_MAX_RETRIES, YTDLP_403_RETRY_DELAY,
    YOUTUBE_SEARCH_MULTIPLIER, YOUTUBE_SEARCH_MIN_FETCH,
    YTDLP_PLAYER_CLIENT, MIN_SONG_DURATION_SECS,
)
from settings import get_setting, get_setting_int
from matching import compute_match_confidence


# YouTube bot/backoff state
_bot_backoff_until = 0.0
_bot_backoff_lock = threading.Lock()
_cookies_disabled_until = 0.0
_cookies_lock = threading.Lock()


def _has_valid_cookie_entries(cookies_text: str) -> bool:
    """Check for at least one Netscape-format cookie entry (tabs-separated)."""
    for raw_line in cookies_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Netscape format can prefix HttpOnly entries with "#HttpOnly_"
        if line.startswith("#HttpOnly_"):
            if line.count("\t") >= 6:
                return True
            continue
        # Skip comments
        if line.startswith("#"):
            continue
        if line.count("\t") >= 6:
            return True
    return False


def _cookie_lines_for_domain_check(cookies_text: str) -> list[str]:
    """Return cookie lines (including HttpOnly-prefixed entries) for domain checks."""
    lines = []
    for raw_line in cookies_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            lines.append(line)
            continue
        if line.startswith("#"):
            continue
        lines.append(line)
    return lines


# Auth cookies are the ones that actually matter for YouTube login. Session/state
# cookies (ST-*, CONSISTENCY, YSC, etc.) are ephemeral noise and don't gate access.
_AUTH_COOKIE_NAMES = {
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID",
    "__Secure-1PAPISID", "__Secure-3PAPISID",
    "__Secure-1PSIDTS", "__Secure-3PSIDTS",
    "__Secure-1PSIDCC", "__Secure-3PSIDCC",
    "LOGIN_INFO",
}


def get_user_cookies_file(user_id: str | None = None) -> "Path":
    """Return the cookie file path for a user, or the global default.

    Per-user cookie files live alongside the global one as cookies-{user_id}.txt.
    Falls back to the global COOKIES_FILE when no user_id is provided.
    """
    if user_id:
        return COOKIES_FILE.parent / f"cookies-{user_id}.txt"
    return COOKIES_FILE


def get_cookies_expiry(cookies_text: str) -> int | None:
    """Return the soonest expiry Unix timestamp among auth cookies, or None if
    no expiry info is found (session cookies with expiry=0 are ignored)."""
    soonest = None
    for raw_line in cookies_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip #HttpOnly_ prefix so the tab-split works normally
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        name = parts[5]
        if name not in _AUTH_COOKIE_NAMES:
            continue
        try:
            expiry = int(parts[4])
        except ValueError:
            continue
        if expiry <= 0:
            continue  # Session cookie  -  no fixed expiry
        if soonest is None or expiry < soonest:
            soonest = expiry
    return soonest


def clear_expired_cookies(user_id: str | None = None) -> bool:
    """Delete cookies from settings if all auth cookies have expired.

    When user_id is provided, checks and clears that user's cookies.
    Returns True if cookies were cleared, False otherwise.
    """
    from settings import get_setting, set_setting, set_user_setting
    cookies_text = get_setting("youtube_cookies", "", user_id=user_id)
    if not cookies_text.strip():
        return False
    expiry = get_cookies_expiry(cookies_text)
    if expiry is None:
        return False
    if time.time() < expiry:
        return False
    # Every auth cookie has expired  -  bin them. Clear the same scope we read,
    # or we wipe the global setting and leave the expired ones exactly where
    # they were, which is the worst of both worlds.
    print("YouTube cookies have expired  -  clearing from settings")
    if user_id:
        set_user_setting(user_id, "youtube_cookies", "")
    else:
        set_setting("youtube_cookies", "")
    _sync_cookies_file(user_id=user_id)
    return True


def _sync_cookies_file(user_id: str | None = None):
    """Write YouTube cookies from settings to the cookie file on disk.

    When a user_id is provided, syncs to the per-user cookies file.
    Without a user_id, syncs the global cookies file.
    Called when settings are saved and at startup.
    """
    cookie_file = get_user_cookies_file(user_id)
    cookies = get_setting("youtube_cookies", "", user_id=user_id)
    if cookies.strip():
        if not _has_valid_cookie_entries(cookies):
            # Avoid writing invalid cookie data that can break yt-dlp
            if cookie_file.exists():
                cookie_file.unlink()
            return
        cookie_file.parent.mkdir(parents=True, exist_ok=True)
        # It is a jar of live session cookies, so keep it to ourselves, and from
        # the moment it exists rather than a moment later. The chmod covers a
        # file that predates this and so kept whatever umask gave it.
        fd = os.open(cookie_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(cookies)
        try:
            cookie_file.chmod(0o600)
        except OSError:
            pass
    elif cookie_file.exists():
        cookie_file.unlink()


def resolve_cookie_file(
    user_id: str | None = None, *, allow_global_fallback: bool = True
) -> "Path | None":
    """Return the cookie file we would actually hand yt-dlp, or None.

    Per-user file wins if it exists and has content; otherwise the global one.
    Ignores the cooldown window, so callers that must be authenticated can ask
    "do we have cookies at all?" separately from "are we currently trusting
    them?".

    With allow_global_fallback off, a named user gets their own cookies or
    nothing. Borrowing the global jar is a fine rescue on an ordinary download,
    where the worst case is somebody else's Premium bitrate, and quietly wrong
    on a personalised playlist, where the answer returned is a stranger's taste
    presented as yours (issue #95).
    """
    cookie_file = get_user_cookies_file(user_id)
    own = cookie_file.exists() and cookie_file.stat().st_size > 0
    if user_id and not own:
        # Before the sync was scoped, saving cookies wrote the global file and
        # left the user's own one missing. Rebuild it from the setting that was
        # saved correctly all along, rather than making everyone re-export.
        # Best effort: if the settings lookup falls over we simply have no
        # per-user cookies, which is what we already believed.
        try:
            _sync_cookies_file(user_id=user_id)
            own = cookie_file.exists() and cookie_file.stat().st_size > 0
        except Exception:
            pass
    if not own:
        if user_id and not allow_global_fallback:
            return None
        cookie_file = COOKIES_FILE
    if cookie_file.exists() and cookie_file.stat().st_size > 0:
        return cookie_file
    return None


# YouTube Music hands every account the *same* playlist id for its personalised
# mixes and serves different tracks depending on who is asking. That is why an
# anonymous request does not 404, it just quietly returns a stranger's taste,
# which is exactly how issue #95 happened. These ids are only meaningful with
# the owner's cookies attached.
#
# Deliberately NOT listed: RDCLAK5uy_* (editorial YTM playlists, identical for
# everyone) and RDMM* (radio seeded from a video, fine anonymously).
_PERSONALISED_YT_PLAYLIST_IDS = frozenset({
    "LM",  # Liked Music / Your Likes
    "WL",  # Watch Later
})
_PERSONALISED_YT_PLAYLIST_PREFIXES = (
    "RDTMAK5uy_",  # My Supermix, Discover Mix, New Release Mix, daily discovery
)


def is_personalised_youtube_playlist(url: str, playlist_id: str | None = None) -> bool:
    """True when a YouTube/YouTube Music playlist is tied to a signed-in account.

    Such a playlist has no honest anonymous answer, so callers should insist on
    cookies rather than accept whatever YouTube offers a logged-out stranger.
    """
    if not playlist_id:
        match = re.search(r'list=([a-zA-Z0-9_-]+)', url or "")
        playlist_id = match.group(1) if match else ""
    if playlist_id in _PERSONALISED_YT_PLAYLIST_IDS:
        return True
    return playlist_id.startswith(_PERSONALISED_YT_PLAYLIST_PREFIXES)


def _ytdlp_base_args(user_id: str | None = None, *, require_cookies: bool = False):
    """Return common yt-dlp arguments (cookies, optional player-client override).
    These should be prepended after 'yt-dlp' in every command.

    When user_id is provided, the per-user cookie file is used if it exists
    and has content; otherwise falls back to the global cookie file.

    With require_cookies, the caller is telling us the request is meaningless
    without an authenticated session, so the temporary cookie cooldown is
    ignored and the global jar is no longer an acceptable stand-in for a named
    user's own cookies. An anonymous answer to "what is in *my* personalised
    playlist" is not a degraded answer, it is somebody else's answer, and so is
    the answer another user's cookies would give. We would rather fail.
    """
    args = []
    if require_cookies or _cookies_allowed():
        cookie_file = resolve_cookie_file(
            user_id, allow_global_fallback=not require_cookies
        )
        if cookie_file:
            args.extend(["--cookies", str(cookie_file)])
    if YTDLP_PLAYER_CLIENT:
        args.extend(["--extractor-args", f"youtube:player_client={YTDLP_PLAYER_CLIENT}"])
    return args


def _is_ytdlp_403(stderr: str) -> bool:
    """Check if yt-dlp stderr indicates a YouTube 403/bot-block error."""
    lower = stderr.lower()
    return "403" in lower or "forbidden" in lower or "sign in to confirm" in lower


def _strip_cookies_args(cmd: list[str]) -> list[str]:
    """Return a command list with any --cookies args removed."""
    cleaned = []
    skip_next = False
    for arg in cmd:
        if skip_next:
            skip_next = False
            continue
        if arg == "--cookies":
            skip_next = True
            continue
        cleaned.append(arg)
    return cleaned


def _should_retry_without_cookies(stderr: str) -> bool:
    """Decide if a download failure likely stems from cookie/auth issues."""
    lower = stderr.lower()
    return (
        _is_ytdlp_403(stderr)
        or "downloaded file is empty" in lower
        or "http error 403" in lower
        # Cookies from premium/broken sessions can cause YouTube to return a
        # different format manifest where bestaudio/best finds nothing.
        # Retry without cookies  -  the cookieless manifest is usually saner.
        or "requested format is not available" in lower
    )


_YTDLP_DEFINITE_MISSING_SIGNS = (
    "private video",
    "private track",
    "video unavailable",
    "track unavailable",
    "has been removed",
    "no longer available",
    "http error 404",
    "http error 410",
    "not a valid url",
    "incomplete youtube id",
)

_YTDLP_TRANSIENT_SIGNS = (
    "http error 403",
    "forbidden",
    "http error 429",
    "too many requests",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
    "timed out",
    "timeout",
    "unable to download webpage",
    "unable to download api page",
    "requested format is not available",
    "format manifest",
    "connection reset",
    "remote end closed connection",
    "remote disconnected",
    "temporary failure",
    "network is unreachable",
)


def classify_ytdlp_failure(stderr: str = "", *, timed_out: bool = False) -> str:
    """Classify a yt-dlp failure as ``transient``, ``missing`` or ``permanent``.

    The distinction is deliberately conservative. A definite private/deleted
    item is not improved by hammering the provider, while throttling, reduced
    manifests, gateway failures and network timeouts deserve a few bounded
    attempts. In particular, ``unable to download API page`` is transient: it
    describes a failed request, not proof that the requested media is absent.
    """
    if timed_out:
        return "transient"
    lower = (stderr or "").lower()
    if any(sign in lower for sign in _YTDLP_DEFINITE_MISSING_SIGNS):
        return "missing"
    if _is_ytdlp_403(stderr) or any(sign in lower for sign in _YTDLP_TRANSIENT_SIGNS):
        return "transient"
    return "permanent"


def run_ytdlp_with_retries(
    cmd: list[str],
    timeout_secs: int | float,
    *,
    max_retries: int = YTDLP_403_MAX_RETRIES,
    retry_delay: float = YTDLP_403_RETRY_DELAY,
    allow_cookie_fallback: bool = True,
    operation: str = "yt-dlp request",
) -> tuple[subprocess.CompletedProcess | None, bool]:
    """Run any yt-dlp metadata/download command with one bounded policy.

    Transient failures retry with a short linear backoff. Definite missing,
    private or invalid media returns immediately. When cookies were involved
    and the final failure can plausibly be session-related, one cookieless
    retry cycle is allowed; success disables the suspect cookies temporarily.
    Returns ``(result, timed_out)`` so existing download callers retain their
    useful timeout-specific messages.
    """
    retries = max(0, int(max_retries))

    def _run_cycle(run_cmd: list[str]) -> tuple[subprocess.CompletedProcess | None, bool]:
        last_result = None
        timed_out = False
        for attempt in range(retries + 1):
            _sleep_if_botted()
            try:
                last_result = subprocess.run(
                    run_cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout_secs,
                )
                timed_out = False
            except subprocess.TimeoutExpired:
                last_result = None
                timed_out = True

            if last_result is not None and last_result.returncode == 0:
                return last_result, False

            stderr = last_result.stderr if last_result is not None else ""
            failure_kind = classify_ytdlp_failure(stderr, timed_out=timed_out)
            if failure_kind != "transient" or attempt >= retries:
                return last_result, timed_out

            if _is_ytdlp_403(stderr):
                _note_bot_block()
            pause = max(0.0, float(retry_delay)) * (attempt + 1)
            print(
                f"{operation} hit a transient provider failure; retrying "
                f"({attempt + 2}/{retries + 1}) in {pause:.1f}s"
            )
            if pause:
                time.sleep(pause)
        return last_result, timed_out

    result, timed_out = _run_cycle(cmd)
    if result is not None and result.returncode == 0:
        return result, False

    stderr = result.stderr if result is not None else ""
    used_cookies = "--cookies" in cmd
    cookie_candidate = timed_out or _should_retry_without_cookies(stderr)
    if allow_cookie_fallback and used_cookies and cookie_candidate:
        cookieless_cmd = _strip_cookies_args(cmd)
        cookieless_result, cookieless_timed_out = _run_cycle(cookieless_cmd)
        if cookieless_result is not None and cookieless_result.returncode == 0:
            print(f"{operation} succeeded without cookies; disabling suspect cookies temporarily")
            _note_cookie_failure()
        return cookieless_result, cookieless_timed_out

    if result is not None and _is_ytdlp_403(stderr):
        _note_bot_block()
    return result, timed_out


def _get_bot_backoff_window() -> tuple[float, float]:
    """Return (min,max) seconds for bot backoff, enforcing sane bounds."""
    min_seconds = float(get_setting_int("youtube_bot_backoff_min", BOT_BACKOFF_MIN_SECONDS))
    max_seconds = float(get_setting_int("youtube_bot_backoff_max", BOT_BACKOFF_MAX_SECONDS))
    if min_seconds < 0:
        min_seconds = 0.0
    if max_seconds < 0:
        max_seconds = 0.0
    if max_seconds < min_seconds:
        print(
            f"youtube_bot_backoff_min ({min_seconds}) exceeded max ({max_seconds}); "
            "swapping to enforce sane bounds."
        )
        min_seconds, max_seconds = max_seconds, min_seconds
    return min_seconds, max_seconds


def _note_bot_block() -> None:
    """Record bot-block and extend the global backoff window."""
    now = time.time()
    min_seconds, max_seconds = _get_bot_backoff_window()
    sleep_for = random.uniform(min_seconds, max_seconds) if max_seconds > 0 else 0
    with _bot_backoff_lock:
        global _bot_backoff_until
        _bot_backoff_until = max(_bot_backoff_until, now + sleep_for)


def _sleep_if_botted() -> None:
    """Sleep if a recent bot-block was detected to reduce request pressure."""
    with _bot_backoff_lock:
        wait_for = _bot_backoff_until - time.time()
    if wait_for > 0:
        time.sleep(wait_for)


def _cookies_allowed() -> bool:
    with _cookies_lock:
        return time.time() >= _cookies_disabled_until


def _note_cookie_failure(cooldown_seconds: int = 7200) -> None:
    """Disable cookie usage for a cooldown window after likely cookie-related failures."""
    with _cookies_lock:
        global _cookies_disabled_until
        _cookies_disabled_until = max(_cookies_disabled_until, time.time() + cooldown_seconds)


def parse_duration(seconds: float) -> str:
    """Convert seconds to MM:SS or HH:MM:SS format"""
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60}:{seconds % 60:02d}"
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _normalise_search_text(text: str) -> str:
    """Normalise text for loose search matching."""
    text = text.lower()
    text = re.sub(r'[\(\[][^\)\]]*[\)\]]', '', text)
    text = re.sub(r'[^a-z0-9\s]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


_ARTIST_MATCH_NOISE = frozenset({
    "feat", "ft", "featuring", "with", "vs", "x", "and", "the",
})


def _normalise_search_title_text(text: str) -> str:
    """Normalise title text while stripping common collaborator clauses."""
    text = _normalise_search_text(text or "")
    text = re.sub(r'\b(?:feat|ft|featuring)\b\s+.*$', '', text).strip()
    return re.sub(r'\s+', ' ', text).strip()


def _artist_match_tokens(text: str) -> set[str]:
    """Return significant artist tokens for loose collaborator/order matching."""
    norm = _normalise_search_text(text or "")
    if not norm:
        return set()
    tokens = {t for t in norm.split() if t and t not in _ARTIST_MATCH_NOISE}
    return tokens or set(norm.split())


def _token_overlap_ratio(expected: set[str], candidate: set[str]) -> float:
    """How much of the smaller token set overlaps with the other."""
    if not expected or not candidate:
        return 0.0
    smaller = min(len(expected), len(candidate))
    if smaller <= 0:
        return 0.0
    return len(expected & candidate) / smaller


def _artist_match_strength(expected_artist: str, *candidate_artists: str) -> float:
    """Return best overlap strength between expected artist and candidate artist text.

    1.0 means the smaller set is fully contained in the larger one, which covers
    collaborator separator changes and artist-order swaps.
    """
    expected_tokens = _artist_match_tokens(expected_artist)
    if not expected_tokens:
        return 0.0
    best = 0.0
    for candidate in candidate_artists:
        strength = _token_overlap_ratio(expected_tokens, _artist_match_tokens(candidate))
        if strength > best:
            best = strength
    return best


_VARIATION_RE = re.compile(
    r'\b(remix|extended|live|acoustic|instrumental|cover|edit|mix|version|'
    r'unplugged|reprise|demo|radio\s+edit|club\s+mix)\b',
    re.IGNORECASE,
)

_LIVE_REQUEST_RE = re.compile(
    r'\b(live|concert|tour|performance|session|sessions|tiny\s+desk|kexp|'
    r'mahogany|colors?\s+show|radio\s*1|from\s+the\s+basement|'
    r'la\s+blogotheque|paste\s+studio|stripped|in\s+studio)\b',
    re.IGNORECASE,
)

_LIVE_RESULT_RE = re.compile(
    r'(?:[\(\[]\s*live\b|[-–]\s*live\b|\blive\s+at\b|\blive\s+from\b|'
    r'\blive\s+version\b|\bin\s+concert\b|\bin\s+session\b|'
    r'\blive\s+on\b|\brecorded\s+live\b|\bperformed\s+live\b|'
    r'\btiny\s+desk\b|\bkexp\b|\bmahogany\b|\bcolors?\s+show\b|'
    r'\bradio\s*1\b|\bfrom\s+the\s+basement\b|\bla\s+blogotheque\b|'
    r'\bpaste\s+studio\b|\bsession[s]?\b|\bstripped\b)',
    re.IGNORECASE,
)

_PLAIN_TITLE_BENIGN_RE = re.compile(
    r'\b(official|vevo|explicit|clean|stereo|mono|lyrics?|lyric\s+video|'
    r'(?:music\s+)?video|audio|visuali[sz]er|hq|hd|4k)\b',
    re.IGNORECASE,
)

_PLAIN_TITLE_VARIANT_RE = re.compile(
    r'\b(live|concert|tour|performance|session|sessions|tiny\s+desk|kexp|'
    r'mahogany|colors?\s+show|radio\s*1|from\s+the\s+basement|'
    r'la\s+blogotheque|paste\s+studio|stripped|acoustic|demo|instrumental|'
    r'remix|edit|mix|version|unplugged|reprise|bootleg|rework|flip|refix|'
    r'cover|tribute|karaoke|orchestral|piano|a\s?capp?ella|vocals?\s+only)\b',
    re.IGNORECASE,
)

_UNKNOWN_SUFFIX_RE = re.compile(r'[\(\[].*?[\)\]]|\s[-–:|]\s+.+$')

def _query_has_variation(query: str) -> bool:
    """Return True if the query explicitly asks for a non-standard version.

    When the user types "abba - mamma mia remix" they want a remix, so we
    shouldn't penalise results that don't match the studio duration. When
    they just type "abba - mamma mia" we can use MusicBrainz to filter out
    the 1:41 DJ medley nonsense.
    """
    return bool(_VARIATION_RE.search(query or ""))


def _query_requests_live(query: str | None) -> bool:
    """Return True when the query explicitly asks for a live/session-style version."""
    return bool(_LIVE_REQUEST_RE.search(query or ""))


def _result_has_plain_title_shape(title: str, expected_title_norm: str) -> bool:
    """Return True when the raw title looks like a plain studio-title presentation."""
    if not expected_title_norm:
        return False
    raw_norm = _normalise_search_title_text(title)
    if raw_norm != expected_title_norm:
        return False
    lower = (title or "").lower()
    return not _UNKNOWN_SUFFIX_RE.search(lower)


def _variant_penalty_from_title(title: str, expected_title_norm: str) -> int:
    """Return a penalty for extra variant-ish text on a plain-title query."""
    if not expected_title_norm:
        return 0
    lower = (title or "").lower()
    if _PLAIN_TITLE_BENIGN_RE.search(lower):
        # Official/promo fluff is handled elsewhere and shouldn't trigger the
        # "this is probably a different version" penalty by itself.
        lower = _PLAIN_TITLE_BENIGN_RE.sub(" ", lower)
    variant_hits = _PLAIN_TITLE_VARIANT_RE.findall(lower)
    penalty = 0
    if variant_hits:
        unique_hits = {re.sub(r"\s+", " ", hit.strip()) for hit in variant_hits if hit.strip()}
        penalty -= 35
        if len(unique_hits) >= 2:
            penalty -= 15
    elif _UNKNOWN_SUFFIX_RE.search(lower):
        penalty -= 18
    return penalty


def _format_score_breakdown(parts: list[str]) -> str:
    """Compact string for logs/debugging."""
    return ", ".join(parts) if parts else "base=100"


def _parse_query_artist_title(query: str) -> tuple[str | None, str | None]:
    if not query:
        return None, None
    for sep in (" - ", " – ", "  -  ", " | "):
        if sep in query:
            artist, title = query.split(sep, 1)
            return artist.strip(), title.strip()
    return None, None


def _parse_result_artist_title(text: str) -> tuple[str | None, str]:
    """Best-effort split for result titles like 'Artist - Title'."""
    artist, title = _parse_query_artist_title(text)
    if artist is None:
        return None, text
    return artist, title or ""


def _score_search_result_with_breakdown(
    title: str,
    channel: str,
    query: str | None = None,
    duration_seconds: float | None = None,
    view_count: int | None = None,
    album: str | None = None,
    expected_duration_secs: float | None = None,
) -> tuple[int, list[str]]:
    """Score a search result to prioritise official content over live versions

    Higher score = better match
    Lower score = worse match (live, cover, remix, etc.)
    """
    title_lower = title.lower()
    channel_lower = channel.lower()
    album_lower = (album or "").lower()
    score = 100  # Start with base score
    breakdown = ["base=100"]
    query_lower = (query or "").lower()
    query_wants_live = _query_requests_live(query)

    def _bump(delta: int, reason: str) -> None:
        nonlocal score
        if delta == 0:
            return
        score += delta
        breakdown.append(f"{reason}={'+' if delta > 0 else ''}{delta}")

    # Penalties for live performances.
    # Watched playlists should strongly avoid performance variants unless the
    # query explicitly asks for one. The broader regex catches common live branding
    # such as "Tiny Desk", "KEXP", "Mahogany", and "Colors Show", not just "live".
    live_title = bool(_LIVE_RESULT_RE.search(title))
    live_album = bool(_LIVE_RESULT_RE.search(album or ""))
    live_channel = bool(_LIVE_RESULT_RE.search(channel))
    if live_title or live_album or live_channel:
        if query_wants_live:
            _bump(-10, "live_requested")
        elif live_title or live_album:
            _bump(-180, "live_variant")
        else:
            _bump(-120, "live_channel")

    # Absolute disqualifiers: results containing these words are never what anyone wants,
    # regardless of query or context. Scored so low they cannot win even against silence.
    _never_re = r'\b(karaoke|nightcore|sped[- ]up|slowed|8d audio|bass boosted)\b'
    if re.search(_never_re, title_lower) or re.search(_never_re, album_lower):
        _bump(-200, "junk_variant")

    # Bootlegs, flips, and refixes are unofficial fan edits  -  never what we want unless
    # the query explicitly names them (which would be unusual but technically possible).
    _bootleg_re = r'\b(bootleg|flip|refix|rework|mashup)\b'
    if re.search(_bootleg_re, title_lower):
        if not re.search(_bootleg_re, query_lower):
            _bump(-100, "bootleg_edit")

    # Vocals-only uploads are the song with the song taken out, and they match
    # title and duration perfectly, so nothing else catches them. Retail spells
    # it "a cappella"; YouTube manages "acapella", "acappella" and "(Vocals Only)".
    _vocals_only_re = r'\b(a\s?capp?ella|vocals?\s+only|isolated\s+vocals?)\b'
    if re.search(_vocals_only_re, title_lower) or re.search(_vocals_only_re, album_lower):
        if not re.search(_vocals_only_re, query_lower):
            _bump(-100, "vocals_only")

    # Penalties for covers, remixes, instrumentals  -  check title AND album name.
    # Piano cover albums tag the track artist as the original artist, so the
    # album is often the only place the word "cover" appears.
    # Remix/edit penalties are waived when the query itself requests that version.
    _cover_re = r'\b(covers?|remix|instrumental|acoustic version|live session|piano( version| covers?)?|tribute|karaoke)\b'
    if re.search(_cover_re, title_lower) or re.search(_cover_re, album_lower):
        # Don't penalise a remix result when we're explicitly searching for a remix
        if not re.search(r'\b(remix|edit|mix)\b', query_lower):
            _bump(-40, "cover_or_variant")

    # Penalties for lyric videos (usually lower quality)
    if re.search(r'\b(lyric|lyrics)\b', title_lower):
        _bump(-20, "lyric_video")

    # Penalties for fan uploads or unofficial - no cell phone video, thanks
    if re.search(r'\b(fan|unofficial|tribute)\b', title_lower):
        _bump(-30, "unofficial_title")
    if re.search(r'\b(fan|fanpage|tribute|covers?|karaoke)\b', channel_lower):
        _bump(-25, "unofficial_channel")

    # Copyright-filtered uploads: audio muted, pitch-shifted, or otherwise butchered
    # to dodge Content ID. The file is useless. Nuke it from orbit.
    if re.search(r'filter(?:ed)?\s*(?:for\s*)?copyright|copyright\s*filter|pitch\s*shift|freq\s*shift', title_lower):
        _bump(-200, "copyright_dodge")

    # Bonuses for official content
    if re.search(r'\b(official|vevo)\b', title_lower):
        _bump(30, "official_title")

    if re.search(r'\b(official|vevo)\b', channel_lower):
        _bump(40, "official_channel")

    # Bonus for "Topic" channels (often official audio)
    if channel_lower.endswith(" - topic"):
        _bump(35, "topic_channel")

    # Bonus for "official music video" or "official video"
    if re.search(r'official\s*(music)?\s*video', title_lower):
        _bump(25, "official_video")

    # Bonus for official audio (best signal for a music grabber)
    if re.search(r'official\s*audio', title_lower):
        _bump(35, "official_audio")

    # Bonus when channel name appears in title (often "Artist - Title")
    if channel_lower and channel_lower in title_lower:
        _bump(10, "channel_in_title")

    # Query-aware matching (helps prefer exact artist/title matches)
    if query:
        query_norm = _normalise_search_text(query)
        title_norm = _normalise_search_text(title)
        channel_norm = _normalise_search_text(channel)
        combined_norm = f"{title_norm} {channel_norm}".strip()

        stopwords = {
            "official", "music", "video", "lyrics", "lyric", "audio",
            "hd", "hq", "remaster", "remastered", "live", "full", "album",
        }
        query_tokens = [t for t in query_norm.split() if t not in stopwords]
        if query_tokens:
            matches = sum(1 for t in query_tokens if t in combined_norm)
            coverage = matches / len(query_tokens)
            if coverage == 1:
                _bump(20, "query_full_coverage")
            elif coverage >= 0.7:
                _bump(10, "query_good_coverage")
            elif coverage < 0.4:
                _bump(-15, "query_poor_coverage")

        # Confidence-based title/artist matching (replaces the old token-overlap
        # logic). Uses SequenceMatcher fuzzy similarity, version-aware penalty,
        # and a core-title fast path so "Beyoncé"/"Beyonce", "Don't"/"Dont", and
        # "Stay"/"Stay (Remix)" are all handled in one place rather than as a
        # stack of post-hoc regex compensations.
        expected_artist, expected_title = _parse_query_artist_title(query)
        if expected_title or expected_artist:
            match_conf, _match_breakdown = compute_match_confidence(
                expected_artist=expected_artist,
                expected_title=expected_title or query,
                candidate_title=title,
                candidate_artist=channel,
                candidate_duration_s=duration_seconds,
                expected_duration_s=expected_duration_secs,
                query=query,
            )
            # Map [0,1] confidence to a bump. The poor/mismatch tiers are
            # deliberately huge because they need to exceed the maximum
            # plausible quality bonus from any source. Monochrome HI_RES
            # tops at +210, so a wrong-song penalty needs to dominate that
            # plus the +35 official_audio and other regex bumps the same
            # candidate might be racking up. A wrong-song match must NEVER
            # beat a right-song match, regardless of how lossless and hi-res
            # the wrong song is.
            if match_conf >= 0.95:
                _bump(50, f"match_perfect={match_conf:.2f}")
            elif match_conf >= 0.85:
                _bump(40, f"match_strong={match_conf:.2f}")
            elif match_conf >= 0.70:
                _bump(25, f"match_good={match_conf:.2f}")
            elif match_conf >= 0.55:
                _bump(5, f"match_ok={match_conf:.2f}")
            elif match_conf >= 0.40:
                _bump(-60, f"match_weak={match_conf:.2f}")
            elif match_conf >= 0.25:
                _bump(-280, f"match_poor={match_conf:.2f}")
            else:
                _bump(-400, f"match_mismatch={match_conf:.2f}")

        # Verbatim "Artist Title" appearance is a strong concrete signal on top
        # of the fuzzy confidence; keep it as an additive bonus.
        expected_artist_norm = _normalise_search_text(expected_artist or "")
        expected_title_norm = _normalise_search_title_text(expected_title or "")
        if expected_artist_norm and expected_title_norm:
            if f"{expected_artist_norm} {expected_title_norm}" in title_norm:
                _bump(20, "artist_title_phrase")


    # Penalty for reaction videos, compilations
    if re.search(r'\b(reaction|react|compilation|vs)\b', title_lower):
        _bump(-60, "reaction_or_compilation")

    # Penalty for compilation/anthology albums  -  checked on both title and album field.
    # A compilation is still the right song, just not the preferred release context,
    # so the penalty is moderate rather than disqualifying.
    _compilation_album_re = r'\b(anthology|greatest hits|best of|collection|essential|platinum|gold series)\b'
    if re.search(_compilation_album_re, album_lower):
        _bump(-25, "compilation_album")

    # Penalty for extended versions (often DJ mixes)
    if re.search(r'\b(extended|extended mix|extended version)\b', title_lower):
        _bump(-15, "extended_version")

    # Penalties for non-song results or modified audio
    if re.search(r'\b(full album|album|mix|playlist|soundtrack)\b', title_lower):
        _bump(-40, "non_song_result")
    if re.search(r'\b(reverb)\b', title_lower):
        _bump(-45, "reverb")

    # Duration scoring  -  typical songs are 2-6 minutes
    if duration_seconds is not None and duration_seconds > 0:
        if duration_seconds < MIN_SONG_DURATION_SECS:
            _bump(-80, "duration_too_short")   # Previews, intros, clips
        elif duration_seconds < 60:
            _bump(-40, "duration_short")
        elif duration_seconds < 90:
            _bump(-15, "duration_brief")
        elif duration_seconds <= 420:
            _bump(10, "duration_sweet_spot")
        elif duration_seconds <= 720:
            pass          # 7-12 min  -  could be legit long track
        elif duration_seconds <= 1200:
            _bump(-20, "duration_long")
        else:
            _bump(-40, "duration_very_long")

    # View count  -  modest tiebreaker, log-scale to avoid domination
    if view_count is not None and view_count >= 0:
        if view_count < 1_000:
            _bump(-10, "low_views")
        elif view_count >= 100_000:
            _bump(5, "good_views")
            if view_count >= 10_000_000:
                _bump(5, "huge_views")

    # MusicBrainz expected duration scoring  -  the canonical yardstick.
    # If we know how long the studio version should be, results that match
    # get a bonus and results that are wildly off get penalised. This is what
    # stops a 1:41 DJ medley from outranking the actual 3:31 studio track.
    if expected_duration_secs and expected_duration_secs > 0 and duration_seconds and duration_seconds > 0:
        delta_ratio = abs(duration_seconds - expected_duration_secs) / expected_duration_secs
        if delta_ratio <= 0.05:
            _bump(40, "mb_duration_exact")
        elif delta_ratio <= 0.12:
            _bump(20, "mb_duration_close")
        elif delta_ratio <= 0.25:
            pass          # Neutral  -  might be a legit alternate version
        elif delta_ratio <= 0.50:
            _bump(-30, "mb_duration_off")
        else:
            _bump(-60, "mb_duration_way_off")

    return score, breakdown


def score_search_result(
    title: str,
    channel: str,
    query: str | None = None,
    duration_seconds: float | None = None,
    view_count: int | None = None,
    album: str | None = None,
    expected_duration_secs: float | None = None,
) -> int:
    score, _ = _score_search_result_with_breakdown(
        title,
        channel,
        query,
        duration_seconds,
        view_count,
        album,
        expected_duration_secs,
    )
    return score


def score_search_result_with_breakdown(
    title: str,
    channel: str,
    query: str | None = None,
    duration_seconds: float | None = None,
    view_count: int | None = None,
    album: str | None = None,
    expected_duration_secs: float | None = None,
) -> tuple[int, list[str]]:
    return _score_search_result_with_breakdown(
        title,
        channel,
        query,
        duration_seconds,
        view_count,
        album,
        expected_duration_secs,
    )


def format_score_breakdown(parts: list[str]) -> str:
    return _format_score_breakdown(parts)


def parse_youtube_search_results(stdout: str, query: str | None = None) -> list[dict]:
    results = []
    for line in stdout.strip().split('\n'):
        if not line:
            continue
        try:
            data = json.loads(line)
            is_playlist = data.get("_type") == "playlist" or "playlist" in data.get("ie_key", "").lower()
            video_count = data.get("playlist_count") or data.get("n_entries")

            title = data.get("title", "Unknown")
            channel = data.get("channel", data.get("uploader", "Unknown"))
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
                "duration": parse_duration(data.get("duration", 0) or 0) if not is_playlist else "",
                "thumbnail": data.get("thumbnail", f"https://i.ytimg.com/vi/{data.get('id')}/mqdefault.jpg"),
                "is_playlist": is_playlist,
                "video_count": video_count,
                "source": "youtube",
                "source_url": data.get("webpage_url") or (
                    f"https://www.youtube.com/watch?v={data.get('id')}" if data.get("id") else ""
                ),
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


def search_youtube(query: str, limit: int) -> list[dict]:
    """Search YouTube and return normalized results"""
    try:
        fetch_limit = max(limit * YOUTUBE_SEARCH_MULTIPLIER, YOUTUBE_SEARCH_MIN_FETCH)

        cmd = [
            "yt-dlp",
            *_ytdlp_base_args(),
            "--dump-json",
            "--flat-playlist",
            "--no-warnings",
            f"ytsearch{fetch_limit}:{query}",
        ]

        result, timed_out = run_ytdlp_with_retries(
            cmd,
            TIMEOUT_YTDLP_SEARCH,
            operation=f"YouTube search for '{query}'",
        )

        if timed_out or result is None or result.returncode != 0:
            stderr = result.stderr if result is not None else ""
            lower = stderr.lower()

            def _reason(err_lower: str) -> str:
                if not err_lower.strip():
                    return "unknown provider error"
                if "sign in to confirm your age" in err_lower or "age-restricted" in err_lower:
                    return "age-restricted content requires valid cookies"
                if "private video" in err_lower:
                    return "private video"
                if "video unavailable" in err_lower:
                    return "video unavailable or region-restricted"
                if "http error 429" in err_lower or "too many requests" in err_lower:
                    return "rate-limited by YouTube"
                if _is_ytdlp_403(err_lower):
                    return "request blocked (403)"
                if "unable to extract" in err_lower or "failed to extract" in err_lower:
                    return "metadata extraction failed"
                if "unable to download webpage" in err_lower or "timed out" in err_lower:
                    return "provider/network timeout"
                if "requested format is not available" in err_lower:
                    return "format manifest unavailable"
                return "provider rejected the request"

            reason = _reason(lower)
            if timed_out:
                reason = "provider/network timeout"
            print(
                f"YouTube search failed for '{query}': {reason} "
                f"({classify_ytdlp_failure(stderr, timed_out=timed_out)})"
            )
            return []

        results = parse_youtube_search_results(result.stdout, query=query)
        results.sort(key=lambda x: x["relevance_score"], reverse=True)
        return results[:limit]

    except Exception as e:
        print(f"YouTube search error: {e}")
        return []
