"""
MusicGrabber - Music discovery

Finding artists a user does not already have. Kept deliberately out of `app.py`,
which is already 6,600 lines and did not need the company.

Design notes, because the measurements behind them are not obvious from the code:

1. **Deezer leads, ListenBrainz fills in.** Measured over eight of a real
   library's most-played artists, ListenBrainz Labs answered three of them and
   returned nothing at all for the other five, silently. Deezer answered seven,
   needs no API key, no auth and no account, and produced the highest proportion
   of artists the user did not already own. It is the better horse; ListenBrainz
   stays because it occasionally knows about artists Deezer has never heard of.

2. **Resolving the artist is where the real bugs live.** Deezer's search returns
   the *first* match, not the best one. Searching for "Pendulum" gives an artist
   with 18 fans and no related artists at all, while the real Pendulum, 371,612
   fans, sits second. "Nirvana" gives a 222-fan band from the sixties ahead of
   the ten-million-fan one. So we take the ten best hits and pick by name match
   first, fan count only as a tie-break.

3. **Name matching is `artist_credit_match_score`, not a fresh normaliser.** It
   already understands "feat." and collaborator presentation, and on the real
   failures it scores "Jay Burnett" against Deezer's suggested "Marcos Brunet" at
   0.00 while still scoring "The Chemical Brothers" against "Chemical Brothers"
   at 1.00. Hence the floor at 1.0. Without this, discovery cheerfully
   recommended Spanish-language worship music off the back of a compilation
   credit, which is a memorable way to lose someone's trust.

4. **Nothing in here may raise into a caller.** Discovery is a nicety. A dead
   upstream returns an empty list and the page says so; it never 500s.
"""

import threading
import time

import httpx

from constants import (
    DEEZER_API_URL,
    TIMEOUT_DEEZER,
    TIMEOUT_LISTENBRAINZ,
    VERSION,
)
from utils import artist_credit_match_score

# A name has to look like the artist we asked for. 1.0 admits "The Chemical
# Brothers" matching "Chemical Brothers"; the impostors in the docstring all
# score 0.50 or below.
NAME_MATCH_FLOOR = 1.0

# Deezer hands back ten candidates cheaply, and the right one is not always first.
_SEARCH_CANDIDATES = 10

# Related artists change on a timescale of months. An hour is plenty, and it
# matters because the point of this feature is clicking through artist after
# artist, which would otherwise be one API round trip per click.
_CACHE_TTL_SECONDS = 3600

_USER_AGENT = {"User-Agent": f"MusicGrabber/{VERSION} (self-hosted music tool)"}

_LB_ALGORITHM = (
    "session_based_days_7500_session_300_contribution_5"
    "_threshold_10_limit_100_filter_True_skip_30"
)

_cache: dict[tuple[str, str], tuple[float, list]] = {}
_cache_lock = threading.Lock()


def _cached(kind: str, key: str):
    with _cache_lock:
        hit = _cache.get((kind, key.casefold()))
    if hit and time.monotonic() - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]
    return None


def _remember(kind: str, key: str, value: list) -> list:
    with _cache_lock:
        _cache[(kind, key.casefold())] = (time.monotonic(), value)
    return value


def reset_cache() -> None:
    """Forget everything. Tests, and anyone debugging a stale suggestion."""
    with _cache_lock:
        _cache.clear()


# ── Deezer ──────────────────────────────────────────────────────────────

def resolve_artist(name: str) -> dict | None:
    """Find the Deezer artist a name actually refers to, or None.

    Name match first, popularity only to break ties between genuine matches.
    Picking by popularity alone is how "Jay Burnett" becomes "Marcos Brunet";
    picking the first hit is how "Pendulum" becomes an artist with 18 fans.
    """
    name = (name or "").strip()
    if not name:
        return None
    try:
        resp = httpx.get(
            f"{DEEZER_API_URL}/search/artist",
            params={"q": name, "limit": _SEARCH_CANDIDATES},
            timeout=TIMEOUT_DEEZER,
            headers=_USER_AGENT,
        )
        if resp.status_code != 200:
            return None
        candidates = resp.json().get("data", []) or []
    except Exception:
        return None

    scored = [
        (artist_credit_match_score(name, c.get("name") or ""), c.get("nb_fan", 0), c)
        for c in candidates
    ]
    plausible = [s for s in scored if s[0] >= NAME_MATCH_FLOOR]
    if not plausible:
        return None
    best = max(plausible, key=lambda s: (s[0], s[1]))
    return best[2]


def _deezer_related(name: str) -> list[str]:
    artist = resolve_artist(name)
    if not artist or not artist.get("id"):
        return []
    try:
        resp = httpx.get(
            f"{DEEZER_API_URL}/artist/{artist['id']}/related",
            timeout=TIMEOUT_DEEZER,
            headers=_USER_AGENT,
        )
        if resp.status_code != 200:
            return []
        return [a["name"] for a in resp.json().get("data", []) or [] if a.get("name")]
    except Exception:
        return []


# ── ListenBrainz ────────────────────────────────────────────────────────

def _listenbrainz_related(name: str) -> list[str]:
    """The original source. Kept as a supplement, no longer relied upon."""
    try:
        with httpx.Client(timeout=TIMEOUT_LISTENBRAINZ, headers=_USER_AGENT) as client:
            mb = client.get(
                "https://musicbrainz.org/ws/2/artist",
                params={"query": name, "limit": 1, "fmt": "json"},
            )
            if mb.status_code != 200:
                return []
            hits = mb.json().get("artists", []) or []
            if not hits:
                return []
            lb = client.get(
                "https://labs.api.listenbrainz.org/similar-artists/json",
                params={"artist_mbids": hits[0]["id"], "algorithm": _LB_ALGORITHM},
            )
            if lb.status_code != 200:
                return []
            return [e["name"] for e in lb.json() if e.get("name")]
    except Exception:
        return []


# ── Public API ──────────────────────────────────────────────────────────

def similar_artists(name: str, limit: int = 25) -> list[str]:
    """Artists resembling `name`, best source first, de-duplicated.

    Deezer's answers lead because they were measurably both more available and
    more likely to be something the user does not already own. ListenBrainz is
    appended rather than merged by rank: the two score on different scales and
    pretending otherwise would be inventing a ranking neither of them gave us.

    An empty list means nobody could help, which is a normal answer here.
    """
    name = (name or "").strip()
    if not name:
        return []
    cached = _cached("similar", name)
    if cached is not None:
        return cached[:limit]

    seen: set[str] = set()
    ordered: list[str] = []
    for source in (_deezer_related, _listenbrainz_related):
        for artist in source(name):
            key = artist.casefold()
            if key in seen or artist_credit_match_score(name, artist) >= NAME_MATCH_FLOOR:
                continue          # not itself, and not the same name twice
            seen.add(key)
            ordered.append(artist)
        if len(ordered) >= limit:
            break                 # Deezer answered; no need to trouble MusicBrainz

    _remember("similar", name, ordered)
    return ordered[:limit]
