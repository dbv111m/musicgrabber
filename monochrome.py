"""
MusicGrabber - Monochrome Source

The resolution ladder, in order of preference:

  Search:   Deezer (clean ISRCs, typo-tolerant, machine-readable version labels),
            each result checked against the tracks API
            -> tracks API search (Deezer light, unconvincing or face-down)
  Download: Monochrome tracks API -> Deezer ISRC rescue -> fail honestly

Search endpoint:  GET {DEEZER}/search?q=query (ISRC arrives inline, free of charge)
Tracks API:       see monochrome_tracks.py (search by artist/title, match the ISRC)

source_url format: monochrome://track_id?isrc=ISRC&quality=LOSSLESS&src=deezer
The download keys off the ISRC alone; the netloc and `src` (deezer/tracks, or
tidal/qbdlx on jobs from older releases) only record where a result came from.
"""

import hashlib
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
    TIMEOUT_MONOCHROME_DOWNLOAD,
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

def monochrome_enabled() -> bool:
    return get_setting_bool("source_monochrome_enabled", True)


def download_leg_healthy() -> tuple[bool, str]:
    """Health check for the source layer: can Monochrome actually stream a FLAC?

    One small live search against the tracks API, the route that serves
    Monochrome downloads.
    """
    from monochrome_tracks import health as tracks_health
    return tracks_health()


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


def _tracks_isrc_lookup(isrc: str, artist: str, title: str) -> tuple[dict | None, bool]:
    """Ask Monochrome's tracks API whether it stocks this exact recording.

    Returns (track, transport_failure). A clean "not stocked" is (None, False);
    (None, True) means the API could not be asked, so absence proves nothing.
    """
    try:
        from monochrome_tracks import find_track
        return find_track(isrc, artist, title), False
    except Exception as exc:
        print(f"Monochrome: tracks API ISRC lookup failed for {isrc}: {exc}")
        return None, True


def _tracks_result_fields(track: dict) -> dict:
    """Title, primary artist, cover and duration (seconds) from a tracks API entry.

    The first listed artist is the primary one; features already sit in the
    title, the same way Deezer's primary artist does.
    """
    names = track.get("artistNames") or []
    return {
        "title": (track.get("title") or "").strip(),
        "artist": (names[0] if names else "").strip(),
        "cover": track.get("artwork") or "",
        "duration": round((track.get("duration") or 0) / 1000),
    }


def _deezer_rank_bonus(rank: int | None) -> tuple[int, str | None]:
    """Deezer rank (0 to ~1M) squeezed into a small 0 to +10 tiebreaker."""
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
    dict on a hit, or None when the tracks API cleanly has nothing for this ISRC,
    which is the caller's signal to fall back to a free-text search. The API can
    only be searched by name, so a missing title is also a None.

    Deliberately conservative when the tracks API is unavailable: we return
    None rather than gambling on a maybe-dead ISRC. The fallback search
    is safer than blindly queueing a recording we could not confirm exists. This
    differs on purpose from _deezer_search_leg's optimistic "label it and hope"
    behaviour; here we have a clean fallback waiting, so we use it.
    """
    isrc = (isrc or "").strip().upper()
    if not _isrc_valid(isrc):
        return None

    track, _transport_failure = _tracks_isrc_lookup(isrc, artist, title)
    if not track:
        # Clean miss or tracks API unavailable; let the caller fall back.
        return None

    # The tracks API serves 16/44.1 FLAC, full stop.
    quality = "LOSSLESS"
    # Prefer the catalogue's own metadata; fall back to the caller's hints when a
    # field is missing (the download keys off the ISRC, so this is cosmetic).
    fields = _tracks_result_fields(track)

    bonus = _QUALITY_BONUS[quality]
    # Flat high score: this is a direct, identity-pinned pick, not a contender in
    # a ranked free-text list, so it sits above anything the search legs produce.
    relevance_score = 1000 + bonus
    score_breakdown = ["via=isrc-direct", f"source_quality=+{bonus}"]

    from monochrome_tracks import track_id
    return _build_monochrome_result(
        track_id=track_id(track),
        isrc=isrc,
        quality=quality,
        src="tracks",
        title=fields["title"] or title,
        artist=fields["artist"] or artist,
        duration=fields["duration"],
        cover=fields["cover"],
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

    def _stocked(row) -> tuple[dict | None, bool]:
        (idx, dz), detail_body, code = row
        if not _isrc_valid(code):
            return None, False
        track_artist = ((detail_body or {}).get("artist") or {}).get("name") or artist
        return _tracks_isrc_lookup(code, track_artist, dz.get("title") or titles[idx])

    with ThreadPoolExecutor(max_workers=min(4, len(pairs))) as pool:
        lookups = list(pool.map(_stocked, zip(pairs, details, isrcs)))

    resolved: dict[int, dict] = {}
    for (idx, dz), detail_body, isrc, (track, transport_failure) in zip(pairs, details, isrcs, lookups):
        if not _isrc_valid(isrc):
            continue
        if not track and not transport_failure:
            continue  # The tracks API answered and does not stock it; let the per-track search try.
        breakdown = [f"via=deezer-album:{album.get('id')}"]
        quality = "LOSSLESS"
        if not track:
            # Same bargain the search leg strikes: unverifiable is not the same
            # as absent, and the tracks API may be back by download time.
            breakdown.append("unverified (tracks API unavailable)")

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
    """Primary Monochrome search leg: Deezer finds the track, the tracks API confirms it.

    Deezer's catalogue search is far more forgiving of typos than the hifi-api,
    every result carries a clean ISRC, and `title_version` flags live/karaoke
    versions explicitly instead of making us guess from punctuation. Each
    candidate ISRC is then checked against Monochrome's tracks API so we only
    show tracks we can actually download.
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

    # Only verify the contenders; no point asking the tracks API about the
    # page-two also-rans.
    candidates.sort(key=lambda c: c["relevance_score"], reverse=True)
    candidates = candidates[:limit]

    with ThreadPoolExecutor(max_workers=min(4, len(candidates))) as pool:
        lookups = list(pool.map(
            lambda c: _tracks_isrc_lookup(c["isrc"], c["artist"], c["title"]), candidates
        ))

    results = []
    for cand, (track, transport_failure) in zip(candidates, lookups):
        if not track and not transport_failure:
            # The tracks API answered and does not stock this ISRC: not downloadable.
            continue
        # The tracks API serves 16/44.1 FLAC whatever the master, so that is the label.
        quality_str = "LOSSLESS"
        if not track:
            # Could not ask, which is not the same as absent. Label it honestly
            # rather than dropping it; the download will find out.
            cand["score_breakdown"].append("unverified (tracks API unavailable)")

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
    malformed or unknown to the tracks API. Returns "" rather than guessing: a confident
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


def _tracks_search_leg(query: str, limit: int) -> list[dict]:
    """Search Monochrome's tracks API directly.

    Deezer is the usual front door; this tops it up when Deezer comes back light
    or unconvincing, and keeps Monochrome searchable when Deezer has a bad day.
    Anything it returns is by definition something the tracks API can serve, so
    there is nothing to verify, and the quality is honestly LOSSLESS (16/44.1),
    which is all it ever serves.
    """
    try:
        from monochrome_tracks import search_tracks, track_id
        raw_items = search_tracks(query, limit * 2)
    except Exception as exc:
        print(f"Monochrome: tracks API search leg errored: {exc}")
        return []

    results = []
    seen_isrcs = set()
    for item in raw_items:
        if item.get("playable") is False:
            continue
        isrc = (item.get("isrc") or "").strip().upper()
        fields = _tracks_result_fields(item)
        tid = track_id(item)
        if not (_isrc_valid(isrc) and fields["title"] and fields["artist"] and tid):
            continue
        if isrc in seen_isrcs:
            continue
        seen_isrcs.add(isrc)

        relevance_score, score_breakdown = score_search_result_with_breakdown(
            f"{fields['artist']} - {fields['title']}", fields["artist"], query,
            duration_seconds=fields["duration"] or None,
            view_count=None,
        )
        bonus = _QUALITY_BONUS["LOSSLESS"]
        relevance_score += bonus
        score_breakdown.append(f"source_quality=+{bonus}")
        score_breakdown.append("via=tracks-api")

        results.append(_build_monochrome_result(
            track_id=tid,
            isrc=isrc,
            quality="LOSSLESS",
            src="tracks",
            title=fields["title"],
            artist=fields["artist"],
            duration=fields["duration"],
            cover=fields["cover"],
            relevance_score=relevance_score,
            score_breakdown=score_breakdown,
        ))

    results.sort(key=lambda x: x["relevance_score"], reverse=True)
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

    Used to decide whether a full Deezer page is good enough to skip the tracks
    API search. Needs a clean 'Artist - Title' split to judge against; without
    one we can't fairly score, so we keep the old "a full page is fine" behaviour.
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

    Deezer is the primary leg (clean ISRCs, typo-tolerant, each result checked
    against the tracks API). A direct tracks API search tops it up when Deezer
    comes back light or unconvincing, and is the whole answer when Deezer has
    nothing at all.
    """
    deezer_results = _deezer_search_leg(query, limit)
    # A full page of Deezer hits is only worth trusting if one of them actually
    # matches what was asked for. Deezer loves to return a tidy ten fuzzy
    # near-misses, and those would otherwise short-circuit the one catalogue
    # that might hold the track Deezer's ranking buried. So: full page AND a
    # confident match before we call it a day.
    if len(deezer_results) >= limit and _has_confident_match(deezer_results, query):
        return deezer_results[:limit]

    seen = {_result_isrc(r) for r in deezer_results}
    merged = deezer_results + [
        r for r in _tracks_search_leg(query, limit) if _result_isrc(r) not in seen
    ]
    merged.sort(key=lambda x: x["relevance_score"], reverse=True)
    return merged[:limit]


def _resolve_monochrome_stream_url(source_url: str, artist_hint: str = "",
                                   title_hint: str = "", *,
                                   trace: list | None = None) -> str:
    """Resolve a Monochrome result to a direct FLAC URL.

    Monochrome's own tracks API does the work; if it has nothing under the ISRC
    we were handed, a Deezer ISRC rescue gives it a second go with the right one.

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
    isrc = (parse_qs(parsed.query).get("isrc") or [""])[0]

    if not isrc:
        raise RuntimeError(f"Monochrome: no ISRC in source_url {source_url!r}")

    # Tidal occasionally ships ISRCs that fail the most basic format check
    # (ampersands, really?). Monochrome will not resolve those, so
    # ask Deezer for the real one before trying the playback ladder.
    rescued = False
    if not _isrc_valid(isrc):
        print(f"Monochrome: ISRC {isrc!r} is malformed; asking Deezer for the real one")
        rescue_isrc = _deezer_isrc_rescue(artist_hint, title_hint, isrc)
        if rescue_isrc:
            print(f"Monochrome: Deezer rescue swapped ISRC {isrc!r} -> {rescue_isrc}")
            isrc = rescue_isrc
            rescued = True

    def _resolve_via_tracks_api() -> str:
        """Resolve through Monochrome's tracks API: one search, exact ISRC match."""
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

    # The tracks API genuinely has nothing under this ISRC. Before giving up,
    # ask Deezer whether the ISRC we were handed is simply wrong for the
    # recording (Tidal metadata strikes again) and retry with the real one.
    if not cdn_url and not rescued:
        rescue_isrc = _deezer_isrc_rescue(artist_hint, title_hint, isrc)
        if rescue_isrc and rescue_isrc != isrc.upper():
            print(f"Monochrome: tracks API had nothing for ISRC {isrc}; retrying with Deezer's {rescue_isrc}")
            _note("Deezer ISRC rescue", True, f"swapped ISRC {isrc} -> {rescue_isrc}, retrying")
            isrc = rescue_isrc
            cdn_url = _resolve_via_tracks_api()

    if not cdn_url:
        raise RuntimeError(
            f"Monochrome: no stream available for ISRC {isrc} "
            f"(tracks API, then Deezer ISRC rescue)"
        )
    return cdn_url


def download_monochrome_track(source_url: str, output_path: Path,
                              artist_hint: str = "", title_hint: str = "") -> None:
    """Resolve a monochrome:// source URL and stream the audio to output_path."""
    track_ref = urlparse(source_url).netloc
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
        raise RuntimeError(f"Monochrome: download of track {track_ref} produced an empty file")
    if expected_size > 0 and actual_size < expected_size:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Monochrome: download truncated for track {track_ref}: "
            f"got {actual_size} of {expected_size} bytes"
        )


def get_monochrome_preview_url(source_url: str, artist_hint: str = "",
                               title_hint: str = "") -> str:
    """Return a playable FLAC URL through the same ladder used by downloads."""
    # Keep the old direct-ISRC call shape working for internal callers and
    # upgrades created before previews started passing complete result URLs.
    if "://" not in (source_url or ""):
        source_url = f"monochrome://preview?{urlencode({'isrc': source_url, 'quality': 'LOSSLESS'})}"
    return _resolve_monochrome_stream_url(
        source_url,
        artist_hint=(artist_hint or "").strip()[:300],
        title_hint=(title_hint or "").strip()[:300],
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
     "MONOCHROME_TRACKS_API_URL. Until then, Monochrome downloads will fail."),
    (("timed out", "timeout"),
     "Something ran out of patience. Check the container's network; if that is fine, Monochrome "
     "is having a slow day."),
]


def _diagnostic_hint(failure_text: str) -> str:
    lowered = (failure_text or "").lower()
    for needles, hint in _DIAGNOSTIC_HINTS:
        if any(needle in lowered for needle in needles):
            return hint
    return ""


def run_download_diagnostic(query: str = "") -> dict:
    """Prove (or disprove) that Monochrome can put a real FLAC on disk.

    Walks the whole path an ordinary download takes: tracks API reachability,
    search, the full stream-resolution ladder, a genuine download to a temp
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
        # ---- 1. Is it even switched on ------------------------------------
        if not monochrome_enabled():
            add("Monochrome source", "warn",
                "Disabled in Search Sources, so results never appear in a search. Testing the "
                "download path anyway.")
        else:
            add("Monochrome source", "ok", "Enabled in Search Sources")

        # ---- 2. Can we reach the tracks API at all --------------------------
        from monochrome_tracks import health as tracks_health
        probe_started = time.monotonic()
        tracks_ok, tracks_detail = tracks_health()
        add("Tracks API reachable", "ok" if tracks_ok else "fail",
            f"{tracks_detail} ({int((time.monotonic() - probe_started) * 1000)}ms)"
            if tracks_ok else tracks_detail)

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

        for entry in trace:
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
