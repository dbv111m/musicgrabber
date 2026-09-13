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

4. **A suggestion you already own is not a discovery.** Measured before this
   was added: 61% of raw suggestions were already in the library, rising to 90%
   for Taylor Swift and 80% for Nirvana. Filtering against the library index is
   not a refinement, it is the difference between the feature working and not.

5. **Nothing in here may raise into a caller.** Discovery is a nicety. A dead
   upstream returns an empty list and the page says so; it never 500s. If the
   library index cannot say what is owned, nothing is filtered, because showing
   a few familiar names beats showing an empty page.

6. **Taste seeds: Navidrome, then the library, then downloads, in that order,
   never blended.** Measured (see docs/music-discovery-research.md sections 6
   and 9): Navidrome's play counts are the cleanest weighted signal on offer
   when it is configured, the library index is the dependable, unweighted
   floor underneath it, and download history is a distant third, on one real
   box actively led by `jawed`, a YouTube test video, not a taste. Averaging
   three signals on three different scales would be inventing a ranking none
   of them gave us, the same objection as merging Deezer with ListenBrainz
   above, so the first source that answers wins outright. "Various Artists" is
   dropped from every one of them; it is not a taste, it is 128 compilation
   albums on one real library alone.
"""

import threading
import time
from pathlib import Path

import httpx

import library_index
from constants import (
    DEEZER_API_URL,
    MUSIC_DIR,
    TIMEOUT_DEEZER,
    TIMEOUT_HTTP_REQUEST,
    TIMEOUT_LISTENBRAINZ,
    VERSION,
)
from settings import get_setting
from utils import artist_credit_match_score, subsonic_auth_params

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
    cached = _cached("resolve", name)
    if cached is not None:
        return cached[0]
    try:
        resp = httpx.get(
            f"{DEEZER_API_URL}/search/artist",
            params={"q": name, "limit": _SEARCH_CANDIDATES},
            timeout=TIMEOUT_DEEZER,
            headers=_USER_AGENT,
        )
        if resp.status_code != 200:
            return None          # a transient upstream is not worth remembering
        candidates = resp.json().get("data", []) or []
    except Exception:
        return None

    scored = [
        (artist_credit_match_score(name, c.get("name") or ""), c.get("nb_fan", 0), c)
        for c in candidates
    ]
    plausible = [s for s in scored if s[0] >= NAME_MATCH_FLOOR]
    # A name nothing matches is cached too. "No such artist" is a real answer and
    # re-asking Deezer about it on every hop through the rabbit hole is rude.
    best = max(plausible, key=lambda s: (s[0], s[1]))[2] if plausible else None
    _remember("resolve", name, [best])
    return best


def _deezer_related(name: str) -> list[dict]:
    """Related artists, keeping the Deezer id we have already paid to look up.

    Throwing the id away and resolving the same artist again by name later is a
    whole extra round trip per suggestion, and this feature's entire shape is
    clicking from one artist to the next.
    """
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
        related = [
            {
                "artist": a["name"],
                "deezer_id": a.get("id"),
                "picture": a.get("picture_medium"),
                "fans": a.get("nb_fan", 0),
            }
            for a in resp.json().get("data", []) or []
            if a.get("name")
        ]
        # Ordered by how many people have heard of them, because Deezer's own
        # order is neither similarity nor popularity: Shaboozey came back as
        # Gareth (971 fans), Warren Zeiders (32k), Jelly Roll (228k), Corey Kent
        # (1.7k). A row is a shelf you scroll along, so the first card sets
        # whether anyone trusts the rest of it, and that first card was a covers
        # act with 971 fans whose big number is somebody else's song. The obscure
        # ones are still all there, just further along, which is the right way
        # round for something you are meant to browse.
        related.sort(key=lambda a: a["fans"], reverse=True)
        return related
    except Exception:
        return []


# ── ListenBrainz ────────────────────────────────────────────────────────

def _listenbrainz_related(name: str) -> list[dict]:
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
            return [
                {"artist": e["name"], "deezer_id": None, "picture": None}
                for e in lb.json() if e.get("name")
            ]
    except Exception:
        return []


# ── Public API ──────────────────────────────────────────────────────────

def _owned_artists(user_id: str | None) -> set[str] | None:
    """The identities already in this user's library, or None if we cannot tell.

    None and the empty set mean very different things here, which is why the
    index distinguishes them: None is "I could not look", and must never be
    treated as "they own nothing", or discovery would confidently re-suggest
    their entire record collection back at them.
    """
    root = str(Path(get_setting("music_dir", str(MUSIC_DIR), user_id=user_id)))
    cached = _cached("owned", root)
    if cached is not None:
        return cached[0]
    try:
        owned = library_index.known_artists(root)
    except Exception:
        owned = None
    _remember("owned", root, [owned])
    return owned


def _identity(name: str) -> str | None:
    from library_index import _tag_keys
    return _tag_keys(name, "x")[0]


def similar_artists(
    name: str,
    limit: int = 25,
    *,
    user_id: str | None = None,
    exclude_owned: bool = True,
) -> list[dict]:
    """Artists resembling `name` that the user does not already have.

    Deezer's answers lead because they were measurably both more available and
    more likely to be something new. ListenBrainz is appended rather than merged
    by rank: the two score on different scales and pretending otherwise would be
    inventing a ranking neither of them gave us.

    `exclude_owned` is the difference between discovery and a list of records
    already on the shelf. It is skipped, deliberately and silently, when the
    index cannot say what is owned: a page with a few familiar names on it is a
    great deal more use than an empty one.

    An empty list means nobody could help, which is a normal answer here.
    """
    name = (name or "").strip()
    if not name:
        return []
    cache_key = f"{name}\x00{user_id or ''}\x00{int(exclude_owned)}"
    cached = _cached("similar", cache_key)
    if cached is not None:
        return cached[:limit]

    owned = _owned_artists(user_id) if exclude_owned else None

    seen: set[str] = set()
    ordered: list[dict] = []
    for source in (_deezer_related, _listenbrainz_related):
        for entry in source(name):
            artist = entry["artist"]
            key = artist.casefold()
            if key in seen or artist_credit_match_score(name, artist) >= NAME_MATCH_FLOOR:
                continue          # not itself, and not the same name twice
            if owned is not None:
                identity = _identity(artist)
                if identity and identity in owned:
                    continue      # they have this one already
            seen.add(key)
            ordered.append(entry)
        if len(ordered) >= limit:
            break                 # Deezer answered; no need to trouble MusicBrainz

    _remember("similar", cache_key, ordered)
    return ordered[:limit]


def artist_taster(
    name: str,
    *,
    deezer_id: int | None = None,
    user_id: str | None = None,
) -> dict | None:
    """One track to introduce an artist by, with a 30-second preview.

    Not simply their biggest hit. The whole premise is that the listener does not
    know this artist, so a track they already own is a wasted introduction, and
    the most famous song is the one they are most likely to have. We take the top
    ten and offer the highest-ranked one that is not already in their library.

    If they own all ten, the hit is returned anyway and flagged as owned; at that
    point the honest thing is to say "you have this" rather than dig for an
    obscurity nobody chose.

    Deezer's preview is a 30-second MP3 on its own CDN, needs no key and no
    account, and is what makes hovering to listen possible at all.
    """
    if deezer_id is None:
        artist = resolve_artist(name)
        if not artist:
            return None
        deezer_id = artist.get("id")
        name = artist.get("name") or name
    if deezer_id is None:
        return None

    cached = _cached("taster", f"{deezer_id}\x00{user_id or ''}")
    if cached is not None:
        return cached[0]

    try:
        resp = httpx.get(
            f"{DEEZER_API_URL}/artist/{deezer_id}/top",
            params={"limit": 10},
            timeout=TIMEOUT_DEEZER,
            headers=_USER_AGENT,
        )
        if resp.status_code != 200:
            return None
        tracks = [t for t in resp.json().get("data", []) or [] if t.get("preview")]
    except Exception:
        return None
    if not tracks:
        return None

    root = str(Path(get_setting("music_dir", str(MUSIC_DIR), user_id=user_id)))
    chosen = tracks[0]
    for track in tracks:
        try:
            hits = library_index.find_by_tags(root, name, track.get("title") or "")
        except Exception:
            hits = None
        if hits is not None and not hits:
            chosen = track        # a completed index says they have not got this one
            break

    taster = {
        "artist": name,
        "title": chosen.get("title"),
        "preview_url": chosen.get("preview"),
        "deezer_id": deezer_id,
        "track_id": chosen.get("id"),
        "duration": chosen.get("duration"),
        "cover": (chosen.get("album") or {}).get("cover_medium"),
        "owned": chosen is tracks[0] and _owns(root, name, tracks[0].get("title") or ""),
    }
    _remember("taster", f"{deezer_id}\x00{user_id or ''}", [taster])
    return taster


def _owns(root: str, artist: str, title: str) -> bool:
    try:
        hits = library_index.find_by_tags(root, artist, title)
    except Exception:
        return False
    return bool(hits)


# ── Taste seeds ─────────────────────────────────────────────────────────

def _is_various_artists(name: str | None) -> bool:
    return (name or "").strip().casefold() == "various artists"


def _navidrome_seeds(user_id: str | None, limit: int) -> list[dict]:
    """Seed artists from Navidrome's most-played albums.

    The cleanest weighted signal available, when Navidrome is configured at
    all. `getAlbumList2?type=frequent` is Navidrome's own most-played sort, so
    the summing here is only needed to fold multiple albums by one artist
    together, not to do the ranking ourselves.
    """
    url = get_setting("navidrome_url", user_id=user_id)
    user = get_setting("navidrome_user", user_id=user_id)
    passwd = get_setting("navidrome_pass", user_id=user_id)
    if not (url and user and passwd):
        return []

    try:
        params = subsonic_auth_params(user, passwd)
        params.update({"type": "frequent", "size": 120})
        resp = httpx.get(
            f"{url.rstrip('/')}/rest/getAlbumList2",
            params=params,
            timeout=TIMEOUT_HTTP_REQUEST,
            headers=_USER_AGENT,
        )
        if resp.status_code != 200:
            return []
        data = resp.json().get("subsonic-response", {})
        if data.get("status") != "ok":
            return []
        albums = data.get("albumList2", {}).get("album", []) or []
    except Exception:
        return []

    counts: dict[str, int] = {}
    for album in albums:
        artist = (album.get("artist") or "").strip()
        if not artist or _is_various_artists(artist):
            continue          # 128 compilation albums, not a taste
        counts[artist] = counts.get(artist, 0) + int(album.get("playCount") or 0)

    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [
        {"artist": artist, "reason": f"Because you listen to {artist}", "weight": count}
        for artist, count in ranked[:limit]
    ]


def _library_seeds(user_id: str | None, limit: int) -> list[dict]:
    """Seed artists by track count from the library index.

    The dependable floor: no external service to configure, and it knows what
    you own even if it has no idea what you actually play.
    """
    root = str(Path(get_setting("music_dir", str(MUSIC_DIR), user_id=user_id)))
    try:
        with library_index.index_conn(read_only=True) as conn:
            rows = conn.execute(
                """
                SELECT artist, COUNT(*) n FROM library_files
                WHERE root_path = ? AND artist IS NOT NULL AND artist != ''
                GROUP BY artist_key
                ORDER BY n DESC
                LIMIT ?
                """,
                (root, limit + 1),          # +1 headroom for a filtered "Various Artists"
            ).fetchall()
    except Exception:
        return []

    seeds = []
    for artist, n in rows:
        if not artist or _is_various_artists(artist):
            continue
        seeds.append({
            "artist": artist,
            "reason": f"Because your library is full of {artist}",
            "weight": int(n),
        })
        if len(seeds) >= limit:
            break
    return seeds


def _download_seeds(limit: int) -> list[dict]:
    """Seed artists from completed download jobs.

    The weakest signal by a distance. On a real box the top "artist" by
    download count was `jawed`, a YouTube test video, followed by `Test
    Artist`; still worth a try when nothing else is configured.
    """
    from db import db_conn

    try:
        with db_conn() as conn:
            rows = conn.execute(
                """
                SELECT artist, COUNT(*) n FROM jobs
                WHERE status LIKE 'completed%' AND artist IS NOT NULL AND artist != ''
                AND (source IS NULL OR source != 'library_rebuild')
                GROUP BY LOWER(artist)
                ORDER BY n DESC
                LIMIT ?
                """,
                (limit + 1,),          # +1 headroom for a filtered "Various Artists"
            ).fetchall()
    except Exception:
        return []

    seeds = []
    for artist, n in rows:
        if not artist or _is_various_artists(artist):
            continue
        seeds.append({
            "artist": artist,
            "reason": f"Because you downloaded {artist}",
            "weight": int(n),
        })
        if len(seeds) >= limit:
            break
    return seeds


def taste_seeds(user_id: str | None = None, limit: int = 12) -> tuple[list[dict], str]:
    """Artists to build "Find New Artists" rows from, and which source they came from.

    Navidrome, then the library index, then download history, in that fixed
    order; the first one that yields anything wins outright, never a blend.
    The three signals live on different scales, so averaging them would be
    inventing a ranking none of them actually gave us. See the module
    docstring, point 6, for the measurements behind the ordering.

    Never raises. A source that cannot answer is skipped, not fatal; if
    nothing can answer at all, that is `([], "none")`, which is a normal
    result here, not an error.
    """
    for signal, source in (
        ("navidrome", lambda: _navidrome_seeds(user_id, limit)),
        ("library", lambda: _library_seeds(user_id, limit)),
        ("downloads", lambda: _download_seeds(limit)),
    ):
        try:
            seeds = source()
        except Exception:
            seeds = []
        if seeds:
            return seeds, signal
    return [], "none"


def discovery_rows(
    user_id: str | None = None, limit: int = 5, per_row: int = 10
) -> tuple[list[dict], str]:
    """Rows of "artists like X" built from `taste_seeds`, and where the seeds came from.

    Returns the signal alongside the rows so a caller wanting both does not have
    to pick the seeds twice; the empty-page message depends on knowing whether we
    had nothing to go on or simply found nothing new, and asking again would mean
    a second trip to Navidrome or the database for an answer we already had.

    Ownership filtering already happens inside `similar_artists`; this does
    not repeat it. A seed whose suggestions all turn out to be things the user
    already owns comes back empty and is skipped rather than shown as a row
    with nothing in it, which is why more seeds are asked for than rows are
    needed: `taste_seeds`'s own default of up to twelve gives this room to
    skip a few duds and still fill the page.
    """
    seeds, signal = taste_seeds(user_id=user_id)
    rows: list[dict] = []
    for seed in seeds:
        try:
            artists = similar_artists(seed["artist"], limit=per_row, user_id=user_id)
        except Exception:
            artists = []
        if not artists:
            continue          # every suggestion was already owned, or nobody answered
        rows.append({"seed": seed["artist"], "reason": seed["reason"], "artists": artists})
        if len(rows) >= limit:
            break
    return rows, signal
