"""
MusicGrabber - Monochrome Source

The resolution ladder, in order of preference:

  Search:   Deezer (clean ISRCs, typo-tolerant, machine-readable version labels)
            -> Tidal hifi-api top-up (catalogue gaps, Deezer outage)
            -> qbdlx direct Qobuz (everything else face-down)
  Download: Monochrome tracks API -> qbdlx direct Qobuz
            -> Deezer ISRC rescue -> Tidal stream via hifi-api
            -> fail honestly

Search endpoint:  GET {DEEZER}/search?q=query (ISRC arrives inline, free of charge)
Tidal search:     GET {HIFI_API}/search?s=query
Tidal stream:     GET {HIFI_API}/track/?id=ID&quality=LOSSLESS (last-ditch leg)
Tracks API:       see monochrome_tracks.py (search by artist/title, match the ISRC)
CDN audio:        https://streaming-qobuz-std.akamaized.net/... (direct FLAC, no auth)

source_url format: monochrome://track_id?isrc=ISRC&quality=HI_RES_LOSSLESS&src=tidal
The `src` param records which leg found the track (tidal/deezer/qbdlx); only
tidal-sourced results may use the Tidal stream fallback, because for the other
legs the netloc is not a Tidal ID and resolving it as one could fetch a
completely different song. Nobody wants surprise polka.
"""

import base64
import hashlib
import json
import re
import shutil
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import httpx

from constants import (
    TIMEOUT_MONOCHROME_SEARCH,
    MONOCHROME_HIFI_SEARCH_BUDGET,
    TIMEOUT_MONOCHROME_DOWNLOAD,
    MONOCHROME_HIFI_API_URL,
    DEEZER_API_URL,
    TIMEOUT_DEEZER,
)
from matching import compute_match_confidence
from settings import get_setting_bool
from youtube import score_search_result_with_breakdown, parse_duration, _parse_query_artist_title

# ISRC: two-letter country, three alphanumeric registrant, two-digit year,
# five-digit designation. Twelve characters, no punctuation, no exceptions,
# whatever Tidal's metadata department may believe.
_ISRC_RE = re.compile(r"^[A-Za-z]{2}[A-Za-z0-9]{3}\d{7}$")


def _isrc_valid(isrc: str) -> bool:
    return bool(_ISRC_RE.fullmatch((isrc or "").strip()))

# Quality map: Tidal tag → (quality string stored in source_url, Qobuz format ID)
_QUALITY_MAP = {
    "HIRES_LOSSLESS": ("HI_RES_LOSSLESS", 27),
    "LOSSLESS":       ("LOSSLESS",         7),
    "HIGH":           ("HIGH",             6),
}
_SOURCE_QUALITY_TO_QOBUZ_FORMAT = {source_quality: qobuz_fmt for source_quality, qobuz_fmt in _QUALITY_MAP.values()}

# Score bonuses per quality tier. Kept near Soulseek's stack so Monochrome can
# compete on overall ranking, but the HIRES-over-LOSSLESS delta is deliberately
# small (15). Anything more lets piano covers and tribute bands ride the HIRES
# bonus straight over the legitimate studio master.
_QUALITY_BONUS = {
    "HI_RES_LOSSLESS": 175,
    "LOSSLESS":        160,
    "HIGH":             30,
}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
}

_KNOWN_PUBLIC_HIFI_API_URLS = {
    "https://us-west.monochrome.tf",
    "https://monochrome-api.samidy.com",
    "https://api.monochrome.tf",
    "https://eu-central.monochrome.tf",
}
_hifi_api_url_cache = None

def monochrome_enabled() -> bool:
    return get_setting_bool("source_monochrome_enabled", True)


def _hifi_api_url() -> str:
    from settings import get_setting
    return get_setting("monochrome_hifi_api_url", MONOCHROME_HIFI_API_URL).rstrip("/")


def _split_endpoint_urls(value: str) -> list[str]:
    urls = []
    seen = set()
    for part in re.split(r"[\s,]+", value or ""):
        url = part.strip().rstrip("/")
        if not url or not re.match(r"https?://", url, re.I) or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _hifi_api_urls() -> list[str]:
    configured = _split_endpoint_urls(_hifi_api_url())
    defaults = _split_endpoint_urls(MONOCHROME_HIFI_API_URL)

    if not configured:
        candidates = defaults
    elif len(configured) == 1 and configured[0] in _KNOWN_PUBLIC_HIFI_API_URLS:
        candidates = configured + defaults
    else:
        candidates = configured

    if _hifi_api_url_cache and _hifi_api_url_cache in candidates:
        candidates = [_hifi_api_url_cache] + [url for url in candidates if url != _hifi_api_url_cache]

    deduped = []
    seen = set()
    for url in candidates:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped or defaults


def _remember_hifi_api_url(base: str) -> None:
    global _hifi_api_url_cache
    _hifi_api_url_cache = base


def download_leg_healthy() -> tuple[bool, str]:
    """Health check for the source layer: can Monochrome actually stream a FLAC?

    The tracks API gets one small live search; if that is down, qbdlx gets a
    live stream probe instead.
    """
    reasons = []
    try:
        from monochrome_tracks import health as tracks_health
        tracks_ok, tracks_reason = tracks_health()
        if tracks_ok:
            return True, tracks_reason
        if tracks_reason:
            reasons.append(tracks_reason)
    except Exception as exc:
        reasons.append(str(exc))
        print(f"Monochrome: tracks API health check errored: {exc}")

    try:
        from qbdlx import qbdlx_enabled, download_leg_healthy as qbdlx_healthy
        if qbdlx_enabled():
            ok, qbdlx_reason = qbdlx_healthy()
            if ok:
                return True, qbdlx_reason
            if qbdlx_reason:
                reasons.append(qbdlx_reason)
        else:
            reasons.append("qbdlx fallback disabled")
    except Exception as exc:
        reasons.append(str(exc))
        print(f"Monochrome: qbdlx health probe errored: {exc}")

    return False, "; ".join(reasons) or "tracks API and qbdlx unavailable"


def _cover_url(cover_uuid: str) -> str:
    """Convert Tidal cover UUID to a resources.tidal.com thumbnail URL."""
    if not cover_uuid:
        return ""
    return f"https://resources.tidal.com/images/{cover_uuid.replace('-', '/')}/320x320.jpg"


# When several copies of a track sit at the same quality tier, prefer the
# canonical studio version. Tidal exposes two signals that make this far easier
# than guessing from album titles:
#
#   item.version     : non-empty for live/remix/demo/rehearsal/karaoke/etc.;
#                      empty (or just a remaster note) on the canonical track.
#   item.popularity  : Tidal's own popularity ranking. Canonical masters tend
#                      to dominate. We use it as a small tiebreaker.
#
# Album titles are still a useful fallback (soundtracks, karaoke comps, tribute
# albums often have clean version fields but obvious album names).

# Track-version patterns. First match wins. A bare "remastered" tag is NOT
# penalised: Tidal almost never carries the un-remastered original master, so
# the remaster IS the canonical version.
#
# The third tuple element is a "waiver" pattern: if the user's own query
# contains it, the penalty is dropped. That way someone searching for
# "spawn soundtrack" or "live at wembley" or "instrumental version" isn't
# punished for getting exactly what they asked for.
_VERSION_PENALTIES = [
    (re.compile(r"\b(karaoke|tribute|piano cover|originally performed)\b", re.I), -120, "version_karaoke_or_tribute",
     re.compile(r"\b(karaoke|tribute)\b", re.I)),
    (re.compile(r"\b(live|unplugged|rehearsal|boombox|in concert|live aid)\b", re.I), -60, "version_live",
     re.compile(r"\b(live|unplugged|concert)\b", re.I)),
    (re.compile(r"\b(demo|outtake|alternate|early|rough mix|work tape|monitor mix)\b", re.I), -45, "version_demo_or_alt",
     re.compile(r"\b(demo|outtake|alternate)\b", re.I)),
    (re.compile(r"\b(instrumental|a cappella|backing track)\b", re.I),  -45, "version_instrumental",
     re.compile(r"\b(instrumental|a cappella|backing)\b", re.I)),
    (re.compile(r"\b(remix|extended|edit|mix|dub|radio|single version)\b", re.I), -30, "version_remix_or_edit",
     re.compile(r"\b(remix|edit|mix|dub|extended)\b", re.I)),
    (re.compile(r"\b(muppet|orchestral|piano version|acoustic version)\b", re.I), -50, "version_arrangement",
     re.compile(r"\b(muppet|orchestral|acoustic|piano)\b", re.I)),
]

_ALBUM_PENALTIES = [
    (re.compile(r"\b(karaoke|tribute|piano covers?)\b", re.I), -80, "album_karaoke_or_tribute",
     re.compile(r"\b(karaoke|tribute)\b", re.I)),
    (re.compile(r"\b(soundtrack|original score|o\.?s\.?t\.?)\b", re.I), -50, "album_soundtrack",
     re.compile(r"\b(soundtrack|score|ost|o\.s\.t)\b", re.I)),
    (re.compile(r"\b(live|unplugged|in concert|at wembley|at reading|rehearsals?)\b", re.I), -40, "album_live",
     re.compile(r"\b(live|unplugged|concert)\b", re.I)),
    (re.compile(r"\b(greatest hits|best of|anthology|essentials?|the hits|compilation|disco night|pop classics|hits collection)\b", re.I), -35, "album_compilation",
     re.compile(r"\b(greatest hits|best of|anthology|compilation|hits)\b", re.I)),
]


def _apply_penalty_set(text: str, query_lower: str, rules: list) -> tuple[int, str | None]:
    if not text:
        return 0, None
    for pattern, penalty, reason, waiver in rules:
        if pattern.search(text):
            if waiver.search(query_lower):
                return 0, None
            return penalty, f"{reason}={penalty}"
    return 0, None


def _version_penalty(version: str, query: str) -> tuple[int, str | None]:
    """Return (penalty, reason) from the Tidal track `version` field, query-aware."""
    if not version:
        return 0, None
    if re.fullmatch(r"\s*(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?\s*", version, re.I):
        return 0, None
    return _apply_penalty_set(version, query.lower(), _VERSION_PENALTIES)


def _album_edition_penalty(album_title: str, query: str) -> tuple[int, str | None]:
    """Return (penalty, reason) for the Tidal album title, query-aware."""
    return _apply_penalty_set(album_title, query.lower(), _ALBUM_PENALTIES)


def _popularity_bonus(popularity: int | None) -> tuple[int, str | None]:
    """Small tiebreaker. Maps Tidal popularity (0 to 100) to a 0 to +10 bump."""
    if not popularity:
        return 0, None
    bonus = min(10, max(0, popularity // 10))
    if not bonus:
        return 0, None
    return bonus, f"tidal_popularity=+{bonus}"


def _best_quality(tags: list[str]) -> tuple[str, int]:
    """Return (quality_string, qobuz_fmt) for the best available quality tier."""
    for tag in ("HIRES_LOSSLESS", "LOSSLESS", "HIGH"):
        if tag in tags:
            return _QUALITY_MAP[tag]
    return ("HIGH", 6)


def _normalise_query_for_hifi_api(query: str) -> str:
    """Return a Monochrome/Tidal search query with punctuation softened.

    The hifi-api search endpoint is much less forgiving of separator punctuation
    than the other providers. Bulk imports naturally produce queries such as
    "Artist1, Artist2 - Track", which can return zero results even though
    "Artist1 Artist2 Track" succeeds.
    """
    normalised = re.sub(r"[\W_]+", " ", query or "", flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalised).strip()


def _monochrome_search_queries(query: str) -> list[str]:
    """Return hifi-api query variants, preserving the user's exact query first."""
    queries = []
    original = (query or "").strip()
    if original:
        queries.append(original)

    normalised = _normalise_query_for_hifi_api(original)
    if normalised and normalised.lower() != original.lower():
        queries.append(normalised)

    return queries


def _deezer_search_tracks(query: str, limit: int) -> list[dict]:
    """Raw Deezer track search. Returns the data list, or raises on transport woes."""
    resp = httpx.get(
        f"{DEEZER_API_URL}/search",
        params={"q": query, "limit": max(1, min(limit, 50))},
        headers=_HEADERS,
        timeout=TIMEOUT_DEEZER,
        follow_redirects=True,
    )
    resp.raise_for_status()
    body = resp.json()
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError(f"Deezer API error: {body['error']}")
    return (body.get("data") or []) if isinstance(body, dict) else []


def _deezer_phrase(text: str) -> str:
    """Sanitise a field for a Deezer structured query phrase.

    Deezer wraps phrases in double quotes (artist:"x" track:"y"), so an embedded
    quote would slam the phrase shut early. We just swap them for spaces; nobody's
    artist name hinges on a literal double quote.
    """
    return (text or "").replace('"', " ").strip()


def _deezer_search_candidates(query: str, limit: int) -> list[dict]:
    """Gather Deezer track candidates: freetext, plus structured field queries.

    Deezer's freetext relevance can bury an exact track under fuzzier noise (a
    real casualty: 'BUNT. - LIEBE' vanishing under a heap of 'Immer Liebe'),
    whereas an artist:"x" track:"y" query pins it at the top. So when the query
    splits cleanly into "Artist - Title" we fire the structured query in the
    proper order first. The transposed ordering (Title - Artist) is only worth
    trying when the proper order draws a blank, since reversing the fields
    otherwise just drags in funny matches; it's there to rescue a query the user
    typed back-to-front, not to second-guess a good one. Results are de-duplicated
    by Deezer id; the caller's ISRC dedup mops up the rest.
    """
    raw_items: list[dict] = []
    seen_ids: set = set()

    def _absorb(items: list[dict]) -> None:
        for item in items:
            did = item.get("id")
            if did is not None and did in seen_ids:
                continue
            if did is not None:
                seen_ids.add(did)
            raw_items.append(item)

    def _search(q: str) -> list[dict]:
        try:
            return _deezer_search_tracks(q, limit)
        except Exception as exc:
            print(f"Monochrome: Deezer search variant {q!r} failed: {exc}")
            return []

    _absorb(_search(query))  # freetext primary

    artist, title = _parse_query_artist_title(query)
    if artist and title:
        forward = _search(f'artist:"{_deezer_phrase(artist)}" track:"{_deezer_phrase(title)}"')
        _absorb(forward)
        # Only reverse the fields if the proper order found nothing of its own.
        if not forward:
            _absorb(_search(f'artist:"{_deezer_phrase(title)}" track:"{_deezer_phrase(artist)}"'))

    return raw_items


def _qobuz_isrc_lookup(isrc: str) -> tuple[list[dict], bool]:
    """Ask Qobuz directly through qbdlx whether an ISRC exists.

    Returns (matching_items, transport_failure). A clean "Qobuz has never heard
    of it" is ([], False); ([], True) means the shared token route could not
    answer, so absence proves nothing.
    """
    try:
        from qbdlx import lookup_qobuz_isrc
        return lookup_qobuz_isrc(isrc)
    except Exception as exc:
        print(f"Monochrome: direct Qobuz ISRC lookup failed for {isrc}: {exc}")
        return [], True


def _deezer_rank_bonus(rank: int | None) -> tuple[int, str | None]:
    """Deezer rank (0 to ~1M) squeezed into the same 0 to +10 tiebreaker as Tidal."""
    if not rank:
        return 0, None
    bonus = min(10, max(0, int(rank) // 100_000))
    if not bonus:
        return 0, None
    return bonus, f"deezer_rank=+{bonus}"


def _build_monochrome_result(track_id, isrc: str, quality: str, src: str,
                             title: str, artist: str, duration,
                             cover: str, relevance_score: int,
                             score_breakdown: list[str]) -> dict:
    """Stamp out the normalised result dict every Monochrome leg emits.

    The download path keys off the ISRC baked into the monochrome:// URL, so the
    netloc (track_id) is purely informational. Keeps the source_url / video_id
    recipe in one place so the legs cannot drift apart.
    """
    params = urlencode({"isrc": isrc, "quality": quality, "src": src})
    source_url = f"monochrome://{track_id}?{params}"
    video_id = f"mono_{hashlib.md5(source_url.encode()).hexdigest()[:12]}"
    return {
        "video_id": video_id,
        "title": title,
        "channel": artist,
        "duration": parse_duration(duration) if duration else "",
        "thumbnail": cover,
        "is_playlist": False,
        "video_count": None,
        "source": "monochrome",
        "source_url": source_url,
        "quality": quality,
        "relevance_score": relevance_score,
        "score_breakdown": score_breakdown,
        "slskd_username": None,
        "slskd_filename": None,
        "slskd_size": None,
    }


def resolve_by_isrc(isrc: str, artist: str = "", title: str = "") -> dict | None:
    """Resolve a known ISRC straight to a downloadable Monochrome result.

    The precise first attempt for a track whose studio recording we have already
    identified (e.g. from a MusicBrainz album fetch). Returns a normalised result
    dict on a hit, or None when Qobuz cleanly has nothing for this ISRC, which is
    the caller's signal to fall back to a free-text search.

    Deliberately conservative when the direct catalogue route is unavailable:
    we return None rather than gambling on a maybe-dead ISRC. The fallback search
    is safer than blindly queueing a recording we could not confirm exists. This
    differs on purpose from _deezer_search_leg's optimistic "label it and hope"
    behaviour; here we have a clean fallback waiting, so we use it.
    """
    isrc = (isrc or "").strip().upper()
    if not _isrc_valid(isrc):
        return None

    matches, transport_failure = _qobuz_isrc_lookup(isrc)
    if not matches:
        # Clean miss or catalogue route unavailable; let the caller fall back.
        return None

    quality = "HI_RES_LOSSLESS" if any(t.get("hires") for t in matches) else "LOSSLESS"

    # Prefer the catalogue's own metadata; fall back to the caller's hints when a
    # field is missing (the download keys off the ISRC, so this is cosmetic).
    item = matches[0]
    item_artist = ((item.get("performer") or {}).get("name") or "").strip()
    item_title = (item.get("title") or "").strip()
    cover = (((item.get("album") or {}).get("image") or {}).get("large", "") or
             ((item.get("album") or {}).get("image") or {}).get("thumbnail", "")) or ""
    duration = item.get("duration") or 0

    bonus = _QUALITY_BONUS[quality]
    # Flat high score: this is a direct, identity-pinned pick, not a contender in
    # a ranked free-text list, so it sits above anything the search legs produce.
    relevance_score = 1000 + bonus
    score_breakdown = ["via=isrc-direct", f"source_quality=+{bonus}"]

    return _build_monochrome_result(
        track_id=item.get("id", ""),
        isrc=isrc,
        quality=quality,
        src="deezer",
        title=item_title or title,
        artist=item_artist or artist,
        duration=duration,
        cover=cover,
        relevance_score=relevance_score,
        score_breakdown=score_breakdown,
    )


# ---------------------------------------------------------------------------
# Album-level resolution
#
# Resolving an album one track at a time is how you end up with a "Fin." off the
# wrong record: an artist can easily use the same song title twice, and a
# free-text search has nothing to tell the two apart. Asking Deezer for the
# album instead pins every track to a single release, in the right order, and
# trades N chances of catching a provider mid-wobble for two.
# ---------------------------------------------------------------------------

def _normalise_album_text(text: str) -> str:
    """Squash a title down to bare letters and digits for comparison.

    RAYE ends half her song titles with a full stop and Deezer Title Cases The
    Lot, so anything fussier than this only invents disagreements. Bracketed
    asides and feature credits go too, since one side almost always has them and
    the other almost always doesn't.
    """
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.replace("&", " and ")
    t = re.sub(r"\s*[\(\[].*?[\)\]]", " ", t)
    t = re.sub(r"\b(?:feat\.?|ft\.?|featuring)\b.*$", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"[^a-z0-9]+", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def _deezer_get(path: str, params: dict | None = None) -> dict:
    """One Deezer API GET, raising on transport trouble or an error payload."""
    resp = httpx.get(
        f"{DEEZER_API_URL}{path}",
        params=params or {},
        headers=_HEADERS,
        timeout=TIMEOUT_DEEZER,
        follow_redirects=True,
    )
    resp.raise_for_status()
    body = resp.json()
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError(f"Deezer API error: {body['error']}")
    return body if isinstance(body, dict) else {}


def _pick_deezer_album(artist: str, album_title: str, expected_count: int) -> dict | None:
    """Find the Deezer album that matches this MusicBrainz release, or None.

    Deliberately fussy. A wrong album here would poison every track on it, which
    is far worse than falling back to the per-track search, so the artist must
    match and the title must match once both are normalised. Track count only
    breaks ties: deluxe editions and bonus discs mean the counts often differ
    legitimately, and refusing on that alone would reject a lot of good albums.
    """
    want_artist = _normalise_album_text(artist)
    want_title = _normalise_album_text(album_title)
    if not (want_artist and want_title):
        return None

    query = f'artist:"{_deezer_phrase(artist)}" album:"{_deezer_phrase(album_title)}"'
    try:
        body = _deezer_get("/search/album", {"q": query, "limit": 10})
    except Exception as exc:
        print(f"Monochrome: Deezer album search failed for {artist} - {album_title}: {exc}")
        return None

    scored = []
    for item in body.get("data") or []:
        if _normalise_album_text((item.get("artist") or {}).get("name") or "") != want_artist:
            continue
        if _normalise_album_text(item.get("title") or "") != want_title:
            continue
        # Closest track count first, then the biggest edition, so a deluxe only
        # wins when nothing matches the count we were expecting.
        nb = int(item.get("nb_tracks") or 0)
        scored.append((abs(nb - expected_count) if expected_count else 0, -nb, item))

    if not scored:
        return None
    scored.sort(key=lambda row: (row[0], row[1]))
    return scored[0][2]


def resolve_album_tracks(artist: str, album_title: str, titles: list[str]) -> dict[int, dict]:
    """Resolve a whole album's tracklist to downloadable Monochrome results.

    Takes the track titles in MusicBrainz order and returns {index: result} for
    however many could be pinned to a single Deezer album. Anything not returned
    is the caller's cue to fall back to the ordinary per-track search; a partial
    answer is perfectly useful, and the fallback is right there.

    Returns {} whenever the album can't be identified, which is a normal outcome
    rather than an error: plenty of releases simply aren't on Deezer.
    """
    if not (monochrome_enabled() and artist and album_title and titles):
        return {}

    album = _pick_deezer_album(artist, album_title, len(titles))
    if not album:
        return {}

    try:
        detail = _deezer_get(f"/album/{album.get('id')}")
    except Exception as exc:
        print(f"Monochrome: Deezer album {album.get('id')} fetch failed: {exc}")
        return {}

    album_tracks = (detail.get("tracks") or {}).get("data") or []
    if not album_tracks:
        return {}
    cover = detail.get("cover_big") or detail.get("cover") or ""

    # Match on normalised title, claiming each Deezer track at most once so a
    # hidden reprise can't be handed out twice. Position is the tiebreak for
    # albums that genuinely repeat a title (interludes love doing this).
    claimed: set[int] = set()
    pairs: list[tuple[int, dict]] = []
    for idx, title in enumerate(titles):
        want = _normalise_album_text(title)
        if not want:
            continue
        best = None
        for pos, dz in enumerate(album_tracks):
            if pos in claimed or _normalise_album_text(dz.get("title") or "") != want:
                continue
            if best is None or abs(pos - idx) < abs(best[0] - idx):
                best = (pos, dz)
        if best:
            claimed.add(best[0])
            pairs.append((idx, best[1]))

    if not pairs:
        return {}

    # Deezer's album payload omits ISRCs, and the download leg keys off the ISRC,
    # so each matched track needs its own lookup. Taking Deezer's ISRC rather
    # than MusicBrainz's is the whole point: it is guaranteed to be the cut that
    # sits on this album, which is exactly the guarantee we were missing.
    def _detail(dz: dict) -> dict | None:
        try:
            return _deezer_get(f"/track/{dz.get('id')}")
        except Exception as exc:
            print(f"Monochrome: Deezer track {dz.get('id')} fetch failed: {exc}")
            return None

    with ThreadPoolExecutor(max_workers=min(4, len(pairs))) as pool:
        details = list(pool.map(lambda pair: _detail(pair[1]), pairs))

    isrcs = [((d or {}).get("isrc") or "").strip().upper() for d in details]
    with ThreadPoolExecutor(max_workers=min(4, len(pairs))) as pool:
        lookups = list(pool.map(lambda code: _qobuz_isrc_lookup(code) if _isrc_valid(code) else ([], False), isrcs))

    resolved: dict[int, dict] = {}
    for (idx, dz), detail_body, isrc, (qobuz_items, transport_failure) in zip(pairs, details, isrcs, lookups):
        if not _isrc_valid(isrc):
            continue
        breakdown = [f"via=deezer-album:{album.get('id')}"]
        if qobuz_items:
            quality = "HI_RES_LOSSLESS" if any(t.get("hires") for t in qobuz_items) else "LOSSLESS"
        elif transport_failure:
            # Same bargain the search leg strikes: unverifiable is not the same
            # as absent, and qbdlx may well deliver it at download time.
            quality = "LOSSLESS"
            breakdown.append("qobuz_unverified (direct lookup unavailable)")
        else:
            continue  # Qobuz answered and has nothing; let the per-track search try.

        bonus = _QUALITY_BONUS[quality]
        breakdown.append(f"source_quality=+{bonus}")
        resolved[idx] = _build_monochrome_result(
            track_id=dz.get("id", ""),
            isrc=isrc,
            quality=quality,
            src="deezer",
            # Prefer the album's own spelling of the title and artist; it is the
            # pressing we are actually downloading from.
            title=(dz.get("title") or titles[idx]).strip(),
            artist=((detail_body or {}).get("artist") or {}).get("name") or artist,
            duration=dz.get("duration") or 0,
            cover=cover,
            # Identity-pinned, exactly like resolve_by_isrc, so it sits above
            # anything a ranked free-text search could produce.
            relevance_score=1000 + bonus,
            score_breakdown=breakdown,
        )

    print(
        f"Monochrome: album '{artist} - {album_title}' matched Deezer album "
        f"{album.get('id')} ({album.get('title')}), resolved {len(resolved)}/{len(titles)} tracks"
    )
    return resolved


def _deezer_search_leg(query: str, limit: int) -> list[dict]:
    """Primary Monochrome search leg: Deezer finds the track, Qobuz confirms it.

    Deezer's catalogue search is far more forgiving of typos than the hifi-api,
    every result carries a clean ISRC, and `title_version` flags live/karaoke
    versions explicitly instead of making us guess from punctuation. Each
    candidate ISRC is then verified against Qobuz so we only show tracks we can
    actually download, labelled with the quality Qobuz really has.
    """
    try:
        raw_items = _deezer_search_candidates(query, limit * 3)
    except Exception as exc:
        print(f"Monochrome: Deezer search leg errored: {exc}")
        return []
    if not raw_items:
        return []

    candidates = []
    seen_isrcs = set()
    for item in raw_items:
        isrc = (item.get("isrc") or "").strip().upper()
        title = (item.get("title") or "").strip()
        artist = ((item.get("artist") or {}).get("name") or "").strip()
        if not (_isrc_valid(isrc) and title and artist):
            continue
        if isrc in seen_isrcs:
            continue
        seen_isrcs.add(isrc)

        album = ((item.get("album") or {}).get("title") or "")
        cover = ((item.get("album") or {}).get("cover_big") or
                 (item.get("album") or {}).get("cover") or "")
        duration = item.get("duration") or 0
        version = item.get("title_version") or ""

        combined = f"{artist} - {title}"
        relevance_score, score_breakdown = score_search_result_with_breakdown(
            combined, artist, query,
            duration_seconds=duration or None,
            view_count=None,
            album=album,
        )
        for delta, reason in (
            _version_penalty(version, query),
            _album_edition_penalty(album, query),
            _deezer_rank_bonus(item.get("rank")),
        ):
            if delta:
                relevance_score += delta
                score_breakdown.append(reason)

        candidates.append({
            "deezer_id": item.get("id", ""),
            "isrc": isrc,
            "title": title,
            "artist": artist,
            "album": album,
            "cover": cover,
            "duration": duration,
            "relevance_score": relevance_score,
            "score_breakdown": score_breakdown,
        })

    if not candidates:
        return []

    # Only Qobuz-verify the contenders; no point spending shared-token calls on
    # the page-two also-rans.
    candidates.sort(key=lambda c: c["relevance_score"], reverse=True)
    candidates = candidates[:limit]

    with ThreadPoolExecutor(max_workers=min(4, len(candidates))) as pool:
        lookups = list(pool.map(lambda c: _qobuz_isrc_lookup(c["isrc"]), candidates))

    results = []
    for cand, (qobuz_items, transport_failure) in zip(candidates, lookups):
        if qobuz_items:
            quality_str = "HI_RES_LOSSLESS" if any(t.get("hires") for t in qobuz_items) else "LOSSLESS"
        elif transport_failure:
            # The direct lookup route cannot verify this one, but the tracks
            # API may still deliver it. Label conservatively rather than dropping.
            quality_str = "LOSSLESS"
            cand["score_breakdown"].append("qobuz_unverified (direct lookup unavailable)")
        else:
            # Qobuz answered and has nothing for this ISRC: not downloadable.
            continue

        bonus = _QUALITY_BONUS[quality_str]
        relevance_score = cand["relevance_score"] + bonus
        breakdown = cand["score_breakdown"] + [f"source_quality=+{bonus}", "via=deezer-isrc"]

        results.append(_build_monochrome_result(
            track_id=cand["deezer_id"],
            isrc=cand["isrc"],
            quality=quality_str,
            src="deezer",
            title=cand["title"],
            artist=cand["artist"],
            duration=cand["duration"],
            cover=cand["cover"],
            relevance_score=relevance_score,
            score_breakdown=breakdown,
        ))

    results.sort(key=lambda x: x["relevance_score"], reverse=True)
    return results


def _deezer_isrc_rescue(artist: str, title: str, bad_isrc: str) -> str:
    """Find the canonical studio ISRC for artist/title via Deezer.

    Used when the ISRC we were handed (usually by Tidal's metadata) is either
    malformed or unknown to Qobuz. Returns "" rather than guessing: a confident
    miss beats a wrong track.
    """
    artist = (artist or "").strip()
    title = (title or "").strip()
    if not (artist or title):
        return ""
    query = f"{artist} {title}".strip()
    try:
        raw_items = _deezer_search_tracks(query, 10)
    except Exception as exc:
        print(f"Monochrome: Deezer ISRC rescue search failed: {exc}")
        return ""

    best_isrc, best_conf = "", 0.0
    for item in raw_items:
        isrc = (item.get("isrc") or "").strip().upper()
        if not _isrc_valid(isrc) or isrc == (bad_isrc or "").upper():
            continue
        # The whole point is escaping live/karaoke/remix variants, so any
        # penalised version label disqualifies the candidate outright.
        version = item.get("title_version") or ""
        penalty, _reason = _version_penalty(version, query)
        if penalty:
            continue
        confidence, _bd = compute_match_confidence(
            artist or None,
            title or None,
            item.get("title") or "",
            ((item.get("artist") or {}).get("name")) or None,
        )
        if confidence > best_conf:
            best_conf, best_isrc = confidence, isrc
    if best_conf >= 0.6:
        return best_isrc
    return ""


def _qbdlx_search_fallback(query: str, limit: int) -> list[dict]:
    """Search Qobuz directly via qbdlx when the hifi-api search leg is dead.

    The hifi-api (Tidal gateway) is the flaky single point of failure for
    Monochrome search. qbdlx already talks to the real Qobuz API for downloads,
    so we reuse its token pool to search the catalogue too. Results carry an
    ISRC, so the existing monochrome:// download path works unchanged; quality
    is honestly LOSSLESS (16/44.1) since the free shared tokens cap there, even
    when Qobuz reports a hi-res master exists.
    """
    try:
        from qbdlx import search_qobuz_catalog
        raw_items = search_qobuz_catalog(query, limit * 2)
    except Exception as exc:
        print(f"Monochrome: qbdlx search fallback errored: {exc}")
        return []
    if not raw_items:
        return []

    results = []
    seen_isrcs = set()
    for item in raw_items:
        if not item.get("streamable", True):
            continue
        isrc = (item.get("isrc") or "").strip()
        title = (item.get("title") or "").strip()
        artist = ((item.get("performer") or {}).get("name") or "").strip()
        # No ISRC means the download leg can't resolve it on Qobuz; skip it,
        # same rule the hifi-api path applies.
        if not (isrc and title and artist):
            continue
        if isrc in seen_isrcs:
            continue
        seen_isrcs.add(isrc)

        album = ((item.get("album") or {}).get("title") or "")
        cover = ((item.get("album") or {}).get("image") or {}).get("large", "") or \
                ((item.get("album") or {}).get("image") or {}).get("thumbnail", "")
        duration = item.get("duration") or 0
        version = item.get("version") or ""

        combined = f"{artist} - {title}"
        relevance_score, score_breakdown = score_search_result_with_breakdown(
            combined, artist, query,
            duration_seconds=duration or None,
            view_count=None,
            album=album,
        )
        bonus = _QUALITY_BONUS["LOSSLESS"]
        relevance_score += bonus
        score_breakdown.append(f"source_quality=+{bonus}")
        score_breakdown.append("via=qbdlx-direct (hifi-api down)")

        for delta, reason in (
            _version_penalty(version, query),
            _album_edition_penalty(album, query),
        ):
            if delta:
                relevance_score += delta
                score_breakdown.append(reason)

        # The item id slot carries the Qobuz track id here; the download path keys
        # off the ISRC, so the netloc is purely informational. The src marker
        # stops the Tidal stream fallback treating a Qobuz id as a Tidal one.
        results.append(_build_monochrome_result(
            track_id=item.get("id", ""),
            isrc=isrc,
            quality="LOSSLESS",
            src="qbdlx",
            title=title,
            artist=artist,
            duration=duration,
            cover=cover,
            relevance_score=relevance_score,
            score_breakdown=score_breakdown,
        ))

    results.sort(key=lambda x: x["relevance_score"], reverse=True)
    if results:
        print(f"Monochrome: hifi-api search empty, served {len(results)} result(s) via qbdlx direct Qobuz")
    return results[:limit]


def _result_isrc(result: dict) -> str:
    """Pull the ISRC back out of a result's monochrome:// source_url."""
    try:
        params = parse_qs(urlparse(result.get("source_url", "")).query)
        return ((params.get("isrc") or [""])[0]).upper()
    except Exception:
        return ""


_DEEZER_CONFIDENT_MATCH_FLOOR = 0.8


def _has_confident_match(results: list[dict], query: str) -> bool:
    """True if any result is a confident artist/title match for the query.

    Used to decide whether a full Deezer page is good enough to skip the Tidal
    leg. Needs a clean 'Artist - Title' split to judge against; without one we
    can't fairly score, so we keep the old "a full page is fine" behaviour.
    """
    artist, title = _parse_query_artist_title(query)
    if not (artist and title):
        return True
    for r in results:
        confidence, _bd = compute_match_confidence(
            artist or None,
            title or None,
            r.get("title") or "",
            r.get("channel") or None,
            query=query,
        )
        if confidence >= _DEEZER_CONFIDENT_MATCH_FLOOR:
            return True
    return False


def search_monochrome(query: str, limit: int) -> list[dict]:
    """Search the Monochrome ladder and return normalised result dicts.

    Deezer is the primary leg (clean ISRCs, Qobuz-verified, typo-tolerant).
    The Tidal hifi-api tops up when Deezer comes back light (catalogue gaps,
    or Deezer itself having a moment), and qbdlx direct Qobuz is the final
    safety net when both metadata legs are face-down.
    """
    deezer_results = _deezer_search_leg(query, limit)
    # A full page of Deezer hits is only worth trusting if one of them actually
    # matches what was asked for. Deezer loves to return a tidy ten fuzzy
    # near-misses, and that used to short-circuit the Tidal leg, the very source
    # that might hold the track Deezer's ranking buried. So: full page AND a
    # confident match before we call it a day.
    if len(deezer_results) >= limit and _has_confident_match(deezer_results, query):
        return deezer_results[:limit]

    hifi_results = _hifi_search_leg(query, limit)
    if hifi_results:
        seen = {_result_isrc(r) for r in deezer_results}
        merged = deezer_results + [r for r in hifi_results if _result_isrc(r) not in seen]
        merged.sort(key=lambda x: x["relevance_score"], reverse=True)
        return merged[:limit]
    if deezer_results:
        return deezer_results[:limit]
    return _qbdlx_search_fallback(query, limit)


def _hifi_search_leg(query: str, limit: int) -> list[dict]:
    """Search Tidal via hifi-api and return normalised result dicts."""
    try:
        deadline = time.monotonic() + MONOCHROME_HIFI_SEARCH_BUDGET
        items = []
        seen_raw_ids = set()
        search_errors = []

        for base in _hifi_api_urls():
            endpoint_had_success = False
            for hifi_query in _monochrome_search_queries(query):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    resp = httpx.get(
                        f"{base}/search",
                        params={"s": hifi_query, "limit": limit * 3},
                        headers=_HEADERS,
                        timeout=min(TIMEOUT_MONOCHROME_SEARCH, max(0.1, remaining)),
                        follow_redirects=True,
                    )
                    resp.raise_for_status()
                    endpoint_had_success = True
                    _remember_hifi_api_url(base)
                except Exception as exc:
                    search_errors.append(f"{base} {hifi_query!r}: {exc}")
                    continue

                for item in resp.json().get("data", {}).get("items", []):
                    tidal_id = item.get("id")
                    if not tidal_id or tidal_id in seen_raw_ids:
                        continue
                    seen_raw_ids.add(tidal_id)
                    items.append(item)

            if items or endpoint_had_success:
                break
            if time.monotonic() >= deadline:
                break

        if not items and search_errors:
            print(f"Monochrome search error: {'; '.join(search_errors)}")

        results = []
        seen_ids = set()

        for item in items:
            tidal_id = item.get("id")
            if not tidal_id or tidal_id in seen_ids:
                continue
            seen_ids.add(tidal_id)

            title     = item.get("title", "")
            artist    = (item.get("artist") or {}).get("name", "")
            duration  = item.get("duration") or 0
            isrc      = item.get("isrc", "")
            tags      = (item.get("mediaMetadata") or {}).get("tags", [])
            album     = (item.get("album") or {}).get("title", "")
            cover_id  = (item.get("album") or {}).get("cover", "")
            version   = item.get("version") or ""
            popularity = item.get("popularity") or 0

            if not (title and artist):
                continue
            # No ISRC means Qobuz can't find it, which means we can't download or even
            # preview it. Pretending otherwise just leads to broken results and angry users.
            if not isrc:
                continue

            quality_str, _ = _best_quality(tags)
            bonus = _QUALITY_BONUS.get(quality_str, 30)

            combined = f"{artist} - {title}"
            relevance_score, score_breakdown = score_search_result_with_breakdown(
                combined, artist, query,
                duration_seconds=duration or None,
                view_count=None,
                album=album,
            )
            relevance_score += bonus
            score_breakdown.append(f"source_quality=+{bonus}")

            for delta, reason in (
                _version_penalty(version, query),
                _album_edition_penalty(album, query),
                _popularity_bonus(popularity),
            ):
                if delta:
                    relevance_score += delta
                    score_breakdown.append(reason)

            params = urlencode({"isrc": isrc, "quality": quality_str, "src": "tidal"})
            source_url = f"monochrome://{tidal_id}?{params}"
            video_id = f"mono_{hashlib.md5(source_url.encode()).hexdigest()[:12]}"

            results.append({
                "video_id": video_id,
                "title": title,
                "channel": artist,
                "duration": parse_duration(duration) if duration else "",
                "thumbnail": _cover_url(cover_id),
                "is_playlist": False,
                "video_count": None,
                "source": "monochrome",
                "source_url": source_url,
                "quality": quality_str,
                "relevance_score": relevance_score,
                "score_breakdown": score_breakdown,
                "slskd_username": None,
                "slskd_filename": None,
                "slskd_size": None,
            })

        results.sort(key=lambda x: x["relevance_score"], reverse=True)
        return results[:limit]

    except Exception as e:
        # The caller (search_monochrome) decides what to fall back to;
        # this leg just reports honestly that it came up empty.
        print(f"Monochrome search error: {e}")
        return []


def _parse_tidal_track_payload(body) -> str:
    """Extract a direct (non-DRM) stream URL from a hifi-api /track response.

    The instances drift between response shapes as they update, so accept the
    known variants: a bare URL field, a urls list, or a base64 BTS manifest.
    A DASH/Widevine manifest yields "" because we don't do DRM circumvention;
    LOSSLESS and below come through as plain BTS with direct URLs.
    """
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return ""
    for key in ("OriginalTrackUrl", "originalTrackUrl", "url"):
        url = data.get(key)
        if isinstance(url, str) and url.startswith("http"):
            return url
    urls = data.get("urls")
    if isinstance(urls, list) and urls and isinstance(urls[0], str) and urls[0].startswith("http"):
        return urls[0]
    manifest = data.get("manifest")
    mime = (data.get("manifestMimeType") or "").lower()
    if isinstance(manifest, str) and "dash" not in mime:
        try:
            decoded = json.loads(base64.b64decode(manifest))
            decoded_urls = decoded.get("urls") or []
            if decoded_urls and isinstance(decoded_urls[0], str) and decoded_urls[0].startswith("http"):
                return decoded_urls[0]
        except Exception:
            pass
    return ""


def _tidal_stream_url(tidal_id: str, quality: str) -> str:
    """Resolve a direct stream URL from the Tidal hifi-api /track endpoint.

    Last-ditch leg for tracks Qobuz simply doesn't stock (or whose ISRC is
    beyond rescue) but which Tidal happily streams; this is exactly how the
    Monochrome web player serves them. Caps at LOSSLESS (16/44.1 FLAC), since
    Tidal's hi-res is Widevine-wrapped and we are not in that business.
    """
    last_err: Exception | None = None
    for base in _hifi_api_urls():
        try:
            resp = httpx.get(
                f"{base}/track/",
                params={"id": tidal_id, "quality": quality},
                headers=_HEADERS,
                timeout=TIMEOUT_MONOCHROME_SEARCH,
                follow_redirects=True,
            )
            resp.raise_for_status()
            url = _parse_tidal_track_payload(resp.json())
            if url:
                _remember_hifi_api_url(base)
                return url
            last_err = RuntimeError(f"{base}: no direct URL in /track payload")
        except Exception as exc:
            last_err = exc
            continue
    raise RuntimeError(f"Tidal stream unavailable for id {tidal_id} at {quality}: {last_err}")


def _resolve_monochrome_stream_url(source_url: str, artist_hint: str = "",
                                   title_hint: str = "", *,
                                   lossless_only: bool = False,
                                   trace: list | None = None) -> str:
    """Resolve a Monochrome result through the shared stream fallback ladder.

    Monochrome's own tracks API is primary; direct qbdlx and Tidal streams are
    fallbacks. Previews use ``lossless_only=True`` so they never
    quietly step down to a lossy tier.

    Pass ``trace`` (a list) to collect a per-leg record of what was tried and
    what it said. The Settings diagnostic uses it to tell a user which leg
    actually broke, rather than the usual "computer says no".
    """
    def _note(leg: str, ok: bool, detail: str) -> None:
        if trace is not None:
            trace.append({"leg": leg, "ok": ok, "detail": str(detail)[:500]})

    parsed = urlparse(source_url)
    if parsed.scheme != "monochrome":
        raise RuntimeError(f"Invalid Monochrome source URL {source_url!r}")
    tidal_id = parsed.netloc
    params = parse_qs(parsed.query)
    isrc    = (params.get("isrc") or [""])[0]
    quality = (params.get("quality") or ["LOSSLESS"])[0]
    src_leg = (params.get("src") or [""])[0]

    if not isrc:
        raise RuntimeError(f"Monochrome: no ISRC in source_url {source_url!r}")

    # Tidal occasionally ships ISRCs that fail the most basic format check
    # (ampersands, really?). Neither Monochrome nor Qobuz will resolve those, so
    # ask Deezer for the real one before trying the playback ladder.
    rescued = False
    if not _isrc_valid(isrc):
        print(f"Monochrome: ISRC {isrc!r} is malformed; asking Deezer for the real one")
        rescue_isrc = _deezer_isrc_rescue(artist_hint, title_hint, isrc)
        if rescue_isrc:
            print(f"Monochrome: Deezer rescue swapped ISRC {isrc!r} -> {rescue_isrc}")
            isrc = rescue_isrc
            rescued = True

    # Previewing a few seconds should not pull hi-res audio or quietly fall back
    # to the lossy HIGH tier. Format 7 is 16-bit FLAC, our strict preview floor.
    tier_order = ["HI_RES_LOSSLESS", "LOSSLESS", "HIGH"]
    if lossless_only:
        candidates = ["LOSSLESS"]
        quality = "LOSSLESS"
    elif quality in tier_order:
        candidates = tier_order[tier_order.index(quality):]
    else:
        candidates = tier_order

    last_error: Exception | None = None

    def _resolve_via_qbdlx() -> str:
        from qbdlx import resolve_qobuz_stream_url
        errors = []
        for tier in candidates:
            fmt = _SOURCE_QUALITY_TO_QOBUZ_FORMAT.get(tier, 7)
            try:
                fallback_url = resolve_qobuz_stream_url(isrc, fmt)
            except Exception as exc:
                print(f"Monochrome: qbdlx fallback errored for ISRC {isrc}: {exc}")
                errors.append(f"{tier}: {exc}")
                fallback_url = None
            if fallback_url:
                print(f"Monochrome: served ISRC {isrc} via qbdlx direct Qobuz")
                _note("qbdlx direct Qobuz", True, f"stream URL for ISRC {isrc} at {tier}")
                return fallback_url
        _note("qbdlx direct Qobuz", False,
              "; ".join(errors) or f"no Qobuz stream for ISRC {isrc} at any allowed tier")
        return ""

    def _resolve_via_tracks_api() -> str:
        """Resolve through Monochrome's tracks API: one search, exact ISRC match.

        It serves 16/44.1 FLAC whatever was asked for, which is at or above
        every tier the ladder allows, so there is no tier loop here.
        """
        leg = "Monochrome tracks API"
        if not title_hint:
            _note(leg, False, "no title to search by, so this route was not tried")
            return ""
        try:
            from monochrome_tracks import resolve_stream_url
            url = resolve_stream_url(isrc, artist_hint, title_hint) or ""
        except Exception as exc:
            print(f"Monochrome: tracks API errored for ISRC {isrc}: {exc}")
            _note(leg, False, f"{type(exc).__name__}: {exc}")
            return ""
        if url:
            print(f"Monochrome: served ISRC {isrc} via the tracks API")
            _note(leg, True, f"FLAC for ISRC {isrc}")
        else:
            _note(leg, False, f"no track with ISRC {isrc} in the catalogue")
        return url

    cdn_url = _resolve_via_tracks_api()

    if not cdn_url:
        cdn_url = _resolve_via_qbdlx()

    # Qobuz genuinely has nothing under this ISRC. Before giving up on Qobuz,
    # ask Deezer whether the ISRC we were handed is simply wrong for the
    # recording (Tidal metadata strikes again) and retry with the real one.
    if not cdn_url and not rescued:
        rescue_isrc = _deezer_isrc_rescue(artist_hint, title_hint, isrc)
        if rescue_isrc and rescue_isrc != isrc.upper():
            print(f"Monochrome: Qobuz had nothing for ISRC {isrc}; retrying with Deezer's {rescue_isrc}")
            _note("Deezer ISRC rescue", True, f"swapped ISRC {isrc} -> {rescue_isrc}, retrying")
            isrc = rescue_isrc
            cdn_url = _resolve_via_tracks_api()
            if not cdn_url:
                cdn_url = _resolve_via_qbdlx()

    # Final leg: the track may live on Tidal but not Qobuz at all, in which
    # case the hifi-api can stream it directly (this is how the Monochrome web
    # player serves such tracks). Only for tidal-sourced results, where the
    # netloc really is a Tidal ID; hi-res is DRM-locked there, so LOSSLESS is
    # the honest ceiling.
    if not cdn_url and src_leg == "tidal" and tidal_id.isdigit():
        tidal_candidates = ["LOSSLESS"] if lossless_only else [
            tier for tier in candidates if tier in ("LOSSLESS", "HIGH")
        ]
        tidal_errors = []
        for tier in tidal_candidates:
            try:
                cdn_url = _tidal_stream_url(tidal_id, tier)
                print(f"Monochrome: tracks API and Qobuz routes exhausted, streaming tidal/{tidal_id} at {tier} via hifi-api")
                _note("Tidal stream via hifi-api", True, f"tidal/{tidal_id} at {tier}")
                break
            except Exception as exc:
                last_error = exc
                tidal_errors.append(f"{tier}: {exc}")
        if not cdn_url:
            _note("Tidal stream via hifi-api", False, "; ".join(tidal_errors) or "no stream")

    if not cdn_url:
        raise RuntimeError(
            f"Monochrome: no stream available for ISRC {isrc} at an allowed quality "
            f"on any leg (tracks API, qbdlx, "
            f"Deezer rescue, Tidal stream) "
            f"(last error: {last_error})"
        )
    return cdn_url


def download_monochrome_track(source_url: str, output_path: Path,
                              artist_hint: str = "", title_hint: str = "") -> None:
    """Resolve a monochrome:// source URL and stream the audio to output_path."""
    parsed = urlparse(source_url)
    tidal_id = parsed.netloc
    cdn_url = _resolve_monochrome_stream_url(
        source_url,
        artist_hint=artist_hint,
        title_hint=title_hint,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with httpx.stream(
        "GET",
        cdn_url,
        headers=_HEADERS,
        timeout=TIMEOUT_MONOCHROME_DOWNLOAD,
        follow_redirects=True,
    ) as resp:
        resp.raise_for_status()
        expected_size = int(resp.headers.get("content-length", 0))
        with open(output_path, "wb") as f:
            for chunk in resp.iter_bytes(chunk_size=65536):
                f.write(chunk)

    actual_size = output_path.stat().st_size
    if actual_size == 0:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f"Monochrome: download of tidal/{tidal_id} produced an empty file")
    if expected_size > 0 and actual_size < expected_size:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Monochrome: download truncated for tidal/{tidal_id}: "
            f"got {actual_size} of {expected_size} bytes"
        )


def get_monochrome_preview_url(source_url: str, artist_hint: str = "",
                               title_hint: str = "") -> str:
    """Return a lossless CDN URL through the same legs used by downloads."""
    # Keep the old direct-ISRC call shape working for internal callers and
    # upgrades created before previews started passing complete result URLs.
    if "://" not in (source_url or ""):
        source_url = f"monochrome://preview?{urlencode({'isrc': source_url, 'quality': 'LOSSLESS'})}"
    return _resolve_monochrome_stream_url(
        source_url,
        artist_hint=(artist_hint or "").strip()[:300],
        title_hint=(title_hint or "").strip()[:300],
        lossless_only=True,
    )


# =============================================================================
# Settings diagnostic: does Monochrome actually download, right here, right now?
# =============================================================================

# One diagnostic at a time. It pulls a whole FLAC; a settings page with an
# itchy trigger finger should not get to do that four times over.
_diagnostic_lock = threading.Lock()

# Fingerprints of the failures people actually hit, and what to do about them.
# Matched against the whole failure text, lowercased, first match wins.
_DIAGNOSTIC_HINTS = [
    # Checked before the timeout rule below: a container with no route out times
    # out too, and sending those people off to blame Monochrome is unkind.
    (("name or service not known", "temporary failure in name resolution", "connection refused",
      "network is unreachable", "certificate"),
     "The container cannot reach the outside world properly. Check DNS, egress and, if the "
     "container is behind a VPN, that the VPN is actually up."),
    (("tracks api reachable",),
     "Monochrome's tracks API is not answering. Monochrome has moved house before; MusicGrabber "
     "follows a move within a few hours on its own, or you can pin the new address with "
     "MONOCHROME_TRACKS_API_URL. Until then, only qbdlx can serve downloads."),
    (("0 usable", "no usable", "401", "403", "token"),
     "The shared qbdlx Qobuz token pool is dry or refusing us, and the tracks API did not cover "
     "for it. This one is out of your hands."),
    (("timed out", "timeout"),
     "Something ran out of patience. Check the container's network; if that is fine, Monochrome "
     "or Qobuz is having a slow day."),
]


def _diagnostic_hint(failure_text: str) -> str:
    lowered = (failure_text or "").lower()
    for needles, hint in _DIAGNOSTIC_HINTS:
        if any(needle in lowered for needle in needles):
            return hint
    return ""


def run_download_diagnostic(query: str = "") -> dict:
    """Prove (or disprove) that Monochrome can put a real FLAC on disk.

    Walks the whole path an ordinary download takes: tracks API and hifi-api
    reachability, search, the full stream-resolution ladder, a genuine download to a temp
    file, and an ffprobe of the bytes that arrived. The file is deleted
    afterwards, so this costs bandwidth and a minute of patience, nothing else.

    Returns a dict of {success, message, hint, elapsed, steps[]} where each step
    is {name, status: ok|warn|fail|skip, detail}.
    """
    from constants import MONOCHROME_TEST_QUERY

    if not _diagnostic_lock.acquire(blocking=False):
        return {
            "success": False,
            "busy": True,
            "message": "A Monochrome test is already running. Give it a minute.",
            "steps": [],
            "hint": "",
            "elapsed": 0.0,
        }

    query = (query or MONOCHROME_TEST_QUERY).strip()
    steps: list[dict] = []
    started = time.monotonic()
    complaints: list[str] = []

    def add(name: str, status: str, detail: str = "") -> None:
        steps.append({"name": name, "status": status, "detail": str(detail)[:600]})
        if status in ("fail", "warn"):
            complaints.append(f"{name}: {detail}")

    def finish(success: bool, message: str) -> dict:
        hint = "" if success else _diagnostic_hint(" ".join(complaints))
        return {
            "success": success,
            "message": message,
            "hint": hint,
            "elapsed": round(time.monotonic() - started, 1),
            "steps": steps,
        }

    temp_dir = None
    try:
        # ---- 1. What is even switched on ----------------------------------
        try:
            from qbdlx import qbdlx_enabled
            qbdlx_on = qbdlx_enabled()
        except Exception as exc:
            qbdlx_on = False
            print(f"Monochrome diagnostic: qbdlx check errored: {exc}")

        # The tracks API has no switch of its own; it is Monochrome.
        enabled_legs = ["Monochrome tracks API"] + (["qbdlx direct Qobuz"] if qbdlx_on else [])
        if not monochrome_enabled():
            add("Monochrome source", "warn",
                "Disabled in Search Sources, so results never appear in a search. Testing the "
                "download path anyway.")
        else:
            add("Monochrome source", "ok", "Enabled in Search Sources")

        add("Download routes", "ok", "Enabled: " + ", ".join(enabled_legs))

        # ---- 2. Can we reach the APIs at all -------------------------------
        from monochrome_tracks import health as tracks_health
        probe_started = time.monotonic()
        tracks_ok, tracks_detail = tracks_health()
        add("Tracks API reachable", "ok" if tracks_ok else "fail",
            f"{tracks_detail} ({int((time.monotonic() - probe_started) * 1000)}ms)"
            if tracks_ok else tracks_detail)

        endpoints = _hifi_api_urls()
        reachable, endpoint_errors = "", []
        for base in endpoints:
            probe_started = time.monotonic()
            try:
                resp = httpx.get(
                    f"{base}/search",
                    params={"s": query, "limit": 1},
                    headers=_HEADERS,
                    timeout=TIMEOUT_MONOCHROME_SEARCH,
                    follow_redirects=True,
                )
                resp.raise_for_status()
                reachable = f"{base} ({int((time.monotonic() - probe_started) * 1000)}ms)"
                _remember_hifi_api_url(base)
                break
            except Exception as exc:
                endpoint_errors.append(f"{base}: {exc}")
        if reachable:
            add("hifi-api reachable", "ok", reachable)
        elif not endpoints:
            add("hifi-api reachable", "skip", "None configured; only needed for a self-hosted hifi-api")
        else:
            # Not fatal on its own: Deezer is the primary search leg these days.
            add("hifi-api reachable", "warn", "; ".join(endpoint_errors))

        # ---- 3. Find the test track ---------------------------------------
        try:
            results = search_monochrome(query, 1)
        except Exception as exc:
            add("Test track found", "fail", f"{type(exc).__name__}: {exc}")
            return finish(False, f"Monochrome could not search for {query!r}.")
        if not results:
            add("Test track found", "fail", f"No Monochrome result for {query!r}")
            return finish(False, f"Monochrome returned no results for {query!r}.")

        track = results[0]
        source_url = track.get("source_url") or ""
        artist_hint = track.get("channel") or ""
        title_hint = track.get("title") or ""
        isrc = _result_isrc(track) or "unknown"
        add("Test track found", "ok",
            f"{artist_hint} - {title_hint} [ISRC {isrc}, "
            f"{track.get('quality') or 'unknown quality'}]")

        # ---- 4. Walk the stream-resolution ladder -------------------------
        trace: list[dict] = []
        cdn_url = ""
        resolve_error = ""
        try:
            cdn_url = _resolve_monochrome_stream_url(
                source_url, artist_hint=artist_hint, title_hint=title_hint, trace=trace,
            )
        except Exception as exc:
            resolve_error = f"{type(exc).__name__}: {exc}"

        # A leg the user switched off did not "fail", it simply never ran. Saying
        # otherwise sends people hunting for a bug they created on purpose.
        switched_off = {"qbdlx direct Qobuz": not qbdlx_on}
        for entry in trace:
            if not entry["ok"] and switched_off.get(entry["leg"]):
                add(entry["leg"], "skip", "Switched off in settings, so this route was not tried")
            else:
                add(entry["leg"], "ok" if entry["ok"] else "fail", entry["detail"])
        if not cdn_url:
            add("Stream URL", "fail", resolve_error or "No leg produced a playable stream")
            return finish(False, "No Monochrome download route could resolve a stream.")
        add("Stream URL", "ok", urlparse(cdn_url).netloc or "resolved")

        # ---- 5. Actually download it --------------------------------------
        temp_dir = Path(tempfile.mkdtemp(prefix="mg-monochrome-test-"))
        target = temp_dir / "monochrome-test.flac"
        download_started = time.monotonic()
        try:
            download_monochrome_track(
                source_url, target, artist_hint=artist_hint, title_hint=title_hint,
            )
        except Exception as exc:
            add("Audio downloaded", "fail", f"{type(exc).__name__}: {exc}")
            return finish(False, "Monochrome resolved a stream but the download failed.")
        size_mb = target.stat().st_size / (1024 * 1024)
        add("Audio downloaded", "ok",
            f"{size_mb:.1f} MB in {time.monotonic() - download_started:.1f}s")

        # ---- 6. Is it real audio, or a very confident 404 page -------------
        from downloads import _validate_audio_integrity, probe_audio_quality
        ok, reason, duration = _validate_audio_integrity(target)
        if not ok:
            add("Audio verified", "fail", reason)
            return finish(False, "Monochrome downloaded a file, but it is not usable audio.")
        quality_label, _bitrate = probe_audio_quality(target)
        add("Audio verified", "ok",
            f"{quality_label or 'audio'}, {int(duration // 60)}m {int(duration % 60):02d}s")

        return finish(True, "Monochrome downloads are working.")
    except Exception as exc:
        # Belt and braces: a diagnostic that crashes is a poor sort of diagnostic.
        print(f"Monochrome diagnostic: unexpected error: {type(exc).__name__}: {exc}")
        add("Diagnostic", "fail", f"{type(exc).__name__}: {exc}")
        return finish(False, "The Monochrome test itself fell over; see the steps below.")
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        _diagnostic_lock.release()
