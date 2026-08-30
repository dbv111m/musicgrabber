"""
MusicGrabber - Metadata Enrichment

AcoustID fingerprinting, MusicBrainz lookups, LRClib lyrics, and audio file tagging.
"""

import json
import base64
import math
import re
import subprocess
import uuid
from pathlib import Path
from typing import Optional

import httpx
from mutagen.flac import FLAC

from constants import (
    VERSION, TIMEOUT_HTTP_REQUEST, TIMEOUT_FPCALC,
    ACOUSTID_MIN_SCORE, MIN_SONG_DURATION_SECS,
    MB_ARTIST_SEARCH_LIMIT, TIMEOUT_MUSICBRAINZ_ARTIST,
    MB_RECORDING_SEARCH_LIMIT, MB_TEXT_SCORE_FLOOR, MB_RECORDING_SPREAD_WEIGHT,
    MB_ALT_TAKE_PENALTY, MB_TITLE_MISMATCH_PENALTY, MB_STUDIO_ALBUM_FILTER,
    MB_RELEASE_GROUP_FALLBACK_MAX_RELEASES,
    DEEZER_SEARCH_URL, DEEZER_API_URL, TIMEOUT_DEEZER,
    DEEZER_METADATA_MATCH_FLOOR, DEEZER_METADATA_SEARCH_LIMIT,
)
from matching import (
    compute_match_confidence, query_requests_variant, _HEAVY_VERSION_KEYWORDS,
    similarity, clean_title, clean_artist, is_junk_artist,
)
from settings import get_setting, get_setting_bool
from utils import set_file_permissions


class MusicBrainzUnavailable(Exception):
    """Raised when MusicBrainz is unreachable after retries (timeout, connect
    error, or persistent 5xx/429). Distinct from "MB returned a valid empty
    result" so the API layer can show a sensible 'try again' message instead
    of pretending the artist or album simply doesn't exist."""


def _mb_get_with_retry(url: str, *, params: dict, headers: dict, timeout: float, attempts: int = 3) -> httpx.Response:
    """GET against MusicBrainz with retry on timeouts, connection errors and
    transient HTTP statuses (429/5xx). Returns the final httpx.Response on
    success, or raises MusicBrainzUnavailable if every attempt fails.

    Backoff is 1s, then 3s -- gentle enough that we do not hammer MB's
    one-request-per-second rate limit on the way back up.
    """
    import time as _time

    retriable_statuses = {429, 500, 502, 503, 504}
    last_error: Optional[str] = None

    for attempt in range(1, attempts + 1):
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.get(url, params=params, headers=headers)
            if resp.status_code in retriable_statuses:
                last_error = f"HTTP {resp.status_code}"
            else:
                return resp
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        except Exception:
            # Anything weirder, let the caller decide -- do not swallow.
            raise

        if attempt < attempts:
            _time.sleep(1 if attempt == 1 else 3)

    raise MusicBrainzUnavailable(
        f"MusicBrainz unreachable after {attempts} attempts ({last_error or 'unknown error'})"
    )


def _extract_track_position(release: dict) -> tuple[int | None, int | None]:
    """Pull (track number, track total) out of a release's media block.

    MusicBrainz cannot quite decide what to call things. The search API returns
    the one matched track under `track` (singular); the lookup API returns the
    same thing under `tracks` (plural). We read both, because arguing with a web
    service about its own schema is a fight nobody wins.

    Falls back to `track-offset + 1` when the printed number is not a plain
    integer, which is how vinyl ends up as "A1" instead of "3".
    """
    for medium in release.get("media") or []:
        entries = medium.get("track") or medium.get("tracks") or []
        if not entries:
            continue
        track = entries[0] or {}
        number = track.get("number") or track.get("position")
        if not str(number or "").strip().isdigit():
            offset = medium.get("track-offset")
            number = offset + 1 if isinstance(offset, int) else None
        if not number:
            continue
        try:
            number = int(str(number).strip())
        except (TypeError, ValueError):
            continue
        total = medium.get("track-count")
        try:
            total = int(total) if total else None
        except (TypeError, ValueError):
            total = None
        return number, total
    return None, None


# Markers that say "this is not the canonical studio take". A recording called
# "Karma Police (Live at Glastonbury)" is a fine recording; it is just not the one
# anybody means when they ask for Karma Police.
_MB_ALT_TAKE_RE = re.compile(
    r"\b(live|remix|mix|demo|instrumental|acoustic|karaoke|edit|reprise|"
    r"radio version|alternate|rehearsal|session)\b", re.I)


def _mb_search_recordings(query: str, headers: dict) -> list[dict]:
    """One recording search. Returns [] on any unhappiness, because a failed
    second opinion should not sink the whole lookup."""
    try:
        with httpx.Client(timeout=TIMEOUT_HTTP_REQUEST) as client:
            response = client.get(
                "https://musicbrainz.org/ws/2/recording/",
                params={
                    "query": query,
                    "fmt": "json",
                    "limit": MB_RECORDING_SEARCH_LIMIT,
                    "inc": "releases release-groups artist-credits",
                },
                headers=headers,
            )
        if response.status_code != 200:
            return []
        return response.json().get("recordings") or []
    except Exception:
        return []


def _mb_merge_recordings(*batches: list[dict]) -> list[dict]:
    """Merge recording batches by id, unioning their release lists.

    The same recording arrives from different queries carrying different releases,
    because each query only returns the releases that matched it. Keeping the
    first copy and binning the rest throws away the studio album, which is a
    remarkably effective way to never find the studio album.
    """
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for batch in batches:
        for rec in batch:
            if int(rec.get("score", 0)) < MB_TEXT_SCORE_FLOOR:
                continue
            rid = rec.get("id")
            existing = by_id.get(rid)
            if existing is None:
                merged = dict(rec)
                merged["releases"] = list(rec.get("releases") or [])
                by_id[rid] = merged
                order.append(rid)
                continue
            known = {r.get("id") for r in existing["releases"]}
            for rel in rec.get("releases") or []:
                if rel.get("id") not in known:
                    existing["releases"].append(rel)
                    known.add(rel.get("id"))
    return [by_id[rid] for rid in order]


def _normalise_track_title(value: str) -> str:
    """Lowercase, strip punctuation and squash spaces, for comparing titles."""
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _score_recording_canonicity(rec: dict, expected_artist: str,
                                expected_title: str = "") -> float:
    """How likely is this the canonical studio recording of the song?

    The strongest signal turns out to be sheer release count. The studio take ends
    up on the album, the single, the greatest hits and 200 compilations, while a
    given live version appears on exactly one bootleg. Counting where a recording
    turned up is a decent proxy for "this is the one people actually mean".

    The title is the other half of it. MusicBrainz is scrupulous about naming the
    odd ones out, so "Smells Like Teen Spirit (Boombox Rehearsals)" tells us
    exactly what it is, provided we bother to read it.
    """
    releases = rec.get("releases") or []
    if not releases:
        return -9999.0

    best_release = max(_release_score_for(rel, expected_artist) for rel in releases)
    # Diminishing returns, so a recording on 200 compilations doesn't automatically
    # trounce one on 30.
    spread = math.log1p(len(releases)) * MB_RECORDING_SPREAD_WEIGHT

    penalty = 0.0
    rec_title = rec.get("title") or ""
    if _MB_ALT_TAKE_RE.search(rec_title):
        penalty += MB_ALT_TAKE_PENALTY
    # Qualifiers bolted onto what we asked for ("(Boombox Rehearsals)",
    # "(unplugged)") mark a variant rather than the thing itself. A title that is
    # different all the way through is a different matter entirely: that is how a
    # romanised query legitimately lands on 夜に駆ける, and punishing it would undo
    # the alias fallback we just went to the trouble of adding.
    if expected_title:
        want = _normalise_track_title(expected_title)
        got = _normalise_track_title(rec_title)
        if want and got != want and want in got:
            penalty += MB_TITLE_MISMATCH_PENALTY

    return best_release + spread - penalty


def _release_score_for(rel: dict, expected_artist: str) -> int:
    """Score a release using the existing release-group scorer.

    Release-group data is sometimes thin in search results, so we top it up from
    the release itself before handing it over.
    """
    rg = dict(rel.get("release-group") or {})
    if not rg.get("artist-credit"):
        rg["artist-credit"] = rel.get("artist-credit") or []
    if rel.get("date") and not rg.get("first-release-date"):
        rg["_date"] = rel["date"]
    return _score_release_group(rg, expected_artist)


def _mb_resolve_recording_via_release_group(
    artist: str,
    title: str,
    headers: dict,
) -> Optional[dict]:
    """Resolve a weak text-search result through an exact-title release group.

    MusicBrainz does not support browsing release groups by recording. What it
    *does* provide is a useful three-link chain for songs released as singles:
    exact release-group search -> releases in that group -> releases containing
    the recurring recording. The recording used on most editions of the single
    is a much better studio-take signal than a page full of one-off bootlegs.

    This is deliberately a fallback. It costs three more rate-limited requests,
    and songs without an eponymous single/EP simply keep the ordinary result.
    """
    import time as _time

    try:
        group_response = _mb_get_with_retry(
            "https://musicbrainz.org/ws/2/release-group/",
            params={
                "query": f'artist:"{artist}" AND releasegroup:"{title}"',
                "fmt": "json",
                "limit": 10,
            },
            headers=headers,
            timeout=TIMEOUT_HTTP_REQUEST,
        )
        if group_response.status_code != 200:
            return None

        wanted_title = _normalise_track_title(title)
        groups = [
            group for group in (group_response.json().get("release-groups") or [])
            if int(group.get("score", 0)) >= MB_TEXT_SCORE_FLOOR
            and _normalise_track_title(group.get("title") or "") == wanted_title
        ]
        if not groups:
            return None

        # A same-name single is the strongest evidence, followed by an EP. An
        # album named after its title track remains a useful last resort.
        type_order = {"Single": 3, "EP": 2, "Album": 1}
        group = max(groups, key=lambda item: (
            type_order.get(item.get("primary-type") or "", 0),
            int(item.get("score", 0)),
        ))

        _time.sleep(1)
        group_releases_response = _mb_get_with_retry(
            "https://musicbrainz.org/ws/2/release/",
            params={
                "release-group": group.get("id"),
                "status": "official",
                "fmt": "json",
                "limit": 100,
                "inc": "recordings release-groups artist-credits",
            },
            headers=headers,
            timeout=TIMEOUT_HTTP_REQUEST,
        )
        if group_releases_response.status_code != 200:
            return None

        # Count distinct releases, rather than raw track appearances: a boxed
        # set with the same song on two discs should not get two votes.
        candidates: dict[str, dict] = {}
        for release in group_releases_response.json().get("releases") or []:
            release_id = release.get("id")
            seen_on_release: set[str] = set()
            for medium in release.get("media") or []:
                for track in medium.get("tracks") or []:
                    recording = track.get("recording") or {}
                    recording_id = recording.get("id")
                    if (
                        not recording_id
                        or recording_id in seen_on_release
                        or _normalise_track_title(recording.get("title") or track.get("title") or "")
                           != wanted_title
                    ):
                        continue
                    seen_on_release.add(recording_id)
                    entry = candidates.setdefault(recording_id, {
                        "recording": dict(recording),
                        "release_ids": set(),
                    })
                    if release_id:
                        entry["release_ids"].add(release_id)

        if not candidates:
            return None

        chosen = max(
            candidates.values(),
            key=lambda item: (
                len(item["release_ids"]),
                bool(item["recording"].get("length")),
                item["recording"].get("id") or "",
            ),
        )["recording"]

        # The release-group leg identifies the take, but its releases are all
        # singles/EPs. One stable browse by recording restores album context.
        _time.sleep(1)
        recording_releases_response = _mb_get_with_retry(
            "https://musicbrainz.org/ws/2/release/",
            params={
                "recording": chosen.get("id"),
                "type": "album",
                "status": "official",
                "fmt": "json",
                "limit": 100,
                "inc": "release-groups artist-credits recordings",
            },
            headers=headers,
            timeout=TIMEOUT_HTTP_REQUEST,
        )
        if recording_releases_response.status_code == 200:
            releases = recording_releases_response.json().get("releases") or []
            if releases:
                # A release browse returns complete tracklists, whereas recording
                # search returns only the matched track. Trim it to the same shape
                # so _extract_track_position does not mistake album track 1 for
                # the song we actually resolved.
                matching_releases = []
                for release in releases:
                    matching_media = []
                    for medium in release.get("media") or []:
                        tracks = [
                            track for track in (medium.get("tracks") or [])
                            if (track.get("recording") or {}).get("id") == chosen.get("id")
                        ]
                        if not tracks:
                            continue
                        matched_medium = dict(medium)
                        matched_medium["tracks"] = tracks
                        matching_media.append(matched_medium)
                    if matching_media:
                        matched_release = dict(release)
                        matched_release["media"] = matching_media
                        matching_releases.append(matched_release)
                if matching_releases:
                    chosen["releases"] = matching_releases

        return chosen if chosen.get("releases") else None
    except Exception:
        # A costly second opinion must never turn otherwise usable metadata into
        # a failed lookup when MusicBrainz has a transient wobble.
        return None


def _mb_find_best_recording(artist: str, title: str, headers: dict) -> Optional[dict]:
    """Search MusicBrainz for the recording that best represents artist/title.

    Two searches: the plain one, plus a studio-album-filtered second opinion. Their
    recordings are merged, the most canonical-looking recording is chosen (by
    _score_recording_canonicity, not raw text relevance), and only then do we hand
    it back for a release to be picked from it. If both come back empty we try once
    more against aliases, which is the only way romanised non-English titles ever
    match.

    Shared by lookup_musicbrainz (ordinary downloads) and the "Guess Again" tag
    editor, so a rehearsal bootleg getting mistaken for the studio single only
    ever needs fixing in one place.
    """
    import time as _time

    base = f'artist:"{artist}" AND recording:"{title}"'

    plain = _mb_search_recordings(base, headers)
    _time.sleep(1)  # MusicBrainz rate limit: 1 req/sec
    studio = _mb_search_recordings(base + MB_STUDIO_ALBUM_FILTER, headers)
    recordings = _mb_merge_recordings(plain, studio)

    if not recordings:
        # Nothing matched a recording title. Aliases carry transliterations, so
        # "Yoru ni Kakeru" can still find 夜に駆ける. Only worth a request when
        # we have nothing at all, since alias matching is looser.
        _time.sleep(1)
        alias_query = f'artist:"{artist}" AND (recording:"{title}" OR alias:"{title}")'
        alias_hits = _mb_search_recordings(alias_query, headers)
        recordings = _mb_merge_recordings(alias_hits)
        plain = plain or alias_hits

    if not recordings:
        # Distinguish "MusicBrainz has never heard of this" from "it has, but
        # only at a confidence we refuse to act on". The second one is worth
        # saying out loud, since it means we kept the source metadata on purpose.
        if plain:
            best = max(int(r.get("score", 0)) for r in plain)
            print(f"MusicBrainz text search score too low ({best}) for "
                  f"{artist} - {title}, skipping")
        return None

    recording = max(recordings,
                    key=lambda rec: _score_recording_canonicity(rec, artist, title))

    # A popular studio take normally appears on many releases. When the best
    # search hit has only a few, recording search has probably handed us one
    # live bootleg from a catalogue full of them. Resolve the exact-title
    # release group only in that weak-evidence case; the ordinary two-request
    # path stays quick for well-behaved catalogues.
    if len(recording.get("releases") or []) <= MB_RELEASE_GROUP_FALLBACK_MAX_RELEASES:
        _time.sleep(1)
        group_recording = _mb_resolve_recording_via_release_group(
            artist, title, headers,
        )
        if group_recording:
            recording = group_recording

    return recording


def lookup_musicbrainz(artist: str, title: str) -> Optional[dict]:
    """Look up track metadata from MusicBrainz.

    Delegates recording selection to _mb_find_best_recording, then picks the
    best-scoring release from whichever recording it settles on.
    """
    if not get_setting_bool("enable_musicbrainz", True):
        return None

    try:
        headers = {"User-Agent": f"MusicGrabber/{VERSION} (https://gitlab.com/g33kphr33k/musicgrabber)"}
        recording = _mb_find_best_recording(artist, title, headers)
        if not recording:
            return None

        # Extract metadata
        metadata = {
            "title": recording.get("title"),
            "artist": recording["artist-credit"][0]["name"] if recording.get("artist-credit") else None,
            "metadata_source": "musicbrainz_text",
        }

        # length is in milliseconds; convert to seconds for the duration check
        length_ms = recording.get("length")
        if length_ms:
            metadata["expected_duration_secs"] = length_ms / 1000.0

        # Get release information for album, date, and track position.
        # Score releases to avoid landing on 'Promo Only Radio Vol. 47' type junk.
        if recording.get("releases"):
            release = max(recording["releases"],
                          key=lambda rel: _release_score_for(rel, artist))
            metadata["release_mbid"] = release.get("id")
            metadata["album"] = release.get("title")
            metadata["date"] = release.get("date")

            # Extract year from date
            if metadata.get("date"):
                year_match = re.match(r'(\d{4})', metadata["date"])
                if year_match:
                    metadata["year"] = year_match.group(1)

            # Track position within the release. The search API nests the matched
            # track under `track` (singular), not `tracks`, which is why singles
            # spent a long while arriving with no track number at all.
            track_num, track_total = _extract_track_position(release)
            if track_num:
                metadata["track_number"] = track_num
                metadata["track_total"] = track_total

        return metadata

    except Exception:
        # If MusicBrainz lookup fails, just continue without it
        return None


def _build_musicbrainz_guess_for_release(
    recording: dict,
    release: dict | None,
    artist: str,
    title: str,
    headers: dict,
) -> dict:
    # Join with "" not " ": each credit's joinphrase already brings its own
    # separator (" & ", " feat. "), so adding ours gives "Underworld &  Iggy Pop".
    credited_artist = "".join(
        (ac.get("name") or ac.get("artist", {}).get("name", "")) + (ac.get("joinphrase") or "")
        for ac in (recording.get("artist-credit") or [])
        if isinstance(ac, dict)
    ).strip() or artist

    metadata = {
        "artist": credited_artist,
        "title": recording.get("title") or title,
        "album": "",
        "album_artist": credited_artist,
        "year": "",
        "track_number": None,
        "track_total": None,
        "release_mbid": None,
        "recording_mbid": recording.get("id"),
        "metadata_source": "musicbrainz_text",
    }

    if not release:
        return metadata

    release_id = release.get("id")
    metadata["release_mbid"] = release_id
    metadata["album"] = release.get("title") or ""
    release_artist = "".join(
        (ac.get("name") or ac.get("artist", {}).get("name", "")) + (ac.get("joinphrase") or "")
        for ac in (release.get("artist-credit") or [])
        if isinstance(ac, dict)
    ).strip()
    if release_artist:
        metadata["album_artist"] = release_artist

    release_date = release.get("date") or ""
    year_match = re.match(r"(\d{4})", release_date)
    if year_match:
        metadata["year"] = year_match.group(1)

    if not release_id:
        return metadata

    with httpx.Client(timeout=TIMEOUT_MUSICBRAINZ_ARTIST) as client:
        release_resp = client.get(
            f"https://musicbrainz.org/ws/2/release/{release_id}",
            params={"inc": "recordings artists", "fmt": "json"},
            headers=headers,
        )
    if release_resp.status_code != 200:
        return metadata

    release_data = release_resp.json()
    release_artist_credit = release_data.get("artist-credit") or []
    release_artist_name = "".join(
        (ac.get("name") or ac.get("artist", {}).get("name", "")) + (ac.get("joinphrase") or "")
        for ac in release_artist_credit
        if isinstance(ac, dict)
    ).strip()
    if release_artist_name:
        metadata["album_artist"] = release_artist_name
    if not metadata["album"]:
        metadata["album"] = release_data.get("title") or ""
    if not metadata["year"]:
        release_year_match = re.match(r"(\d{4})", release_data.get("date") or "")
        if release_year_match:
            metadata["year"] = release_year_match.group(1)

    recording_id = recording.get("id")
    fallback_track = None
    for medium in release_data.get("media") or []:
        track_total = medium.get("track-count")
        for track in medium.get("tracks") or []:
            track_recording = track.get("recording") or {}
            track_title = track_recording.get("title") or track.get("title") or ""
            matches_recording = recording_id and track_recording.get("id") == recording_id
            matches_title = (
                not fallback_track
                and track_title
                and track_title.strip().lower() == (metadata["title"] or "").strip().lower()
            )
            if matches_recording or matches_title:
                fallback_track = {
                    "track_number": track.get("position") or track.get("number"),
                    "track_total": track_total,
                }
                if matches_recording:
                    break
        if fallback_track and fallback_track.get("track_number"):
            break

    if fallback_track:
        try:
            metadata["track_number"] = int(fallback_track.get("track_number")) if fallback_track.get("track_number") else None
        except (TypeError, ValueError):
            metadata["track_number"] = None
        try:
            metadata["track_total"] = int(fallback_track.get("track_total")) if fallback_track.get("track_total") else None
        except (TypeError, ValueError):
            metadata["track_total"] = None

    return metadata


def guess_musicbrainz_tag_candidates(artist: str, title: str, file_path: Path = None) -> list[dict]:
    """Return ordered MusicBrainz tag candidates for a track.

    Tier 1, when the actual file is available: fingerprint it and ask AcoustID
    what it really is, then list that recording's releases as candidates. This
    is grounded in what the audio actually sounds like, so it cannot be fooled
    by a text search matching a rehearsal tape whose title happens to contain
    the words we searched for.

    Tier 2: the same canonicity-scored recording search lookup_musicbrainz uses
    for ordinary downloads, widening the net (or providing the only net, if
    fingerprinting found nothing to work with).
    """
    if not get_setting_bool("enable_musicbrainz", True):
        return []

    artist = (artist or "").strip()
    title = (title or "").strip()
    if not artist or not title:
        return []

    headers = {"User-Agent": f"MusicGrabber/{VERSION} (https://gitlab.com/g33kphr33k/musicgrabber)"}
    candidates: list[dict] = []
    seen_release_ids: set[str] = set()
    seen_album_keys: set[tuple[str, str]] = set()

    def _add_release_candidates(recording: dict, releases: list[dict], source: str) -> None:
        sorted_releases = sorted(releases, key=lambda rel: _release_score_for(rel, artist), reverse=True)
        for release in sorted_releases:
            release_id = release.get("id")
            album_key = ((release.get("title") or "").strip().lower(), (release.get("date") or "")[:4])
            if release_id and release_id in seen_release_ids:
                continue
            if album_key in seen_album_keys:
                continue
            if release_id:
                seen_release_ids.add(release_id)
            seen_album_keys.add(album_key)
            candidate = _build_musicbrainz_guess_for_release(recording, release, artist, title, headers)
            candidate["metadata_source"] = source
            candidates.append(candidate)

    try:
        if file_path and file_path.exists():
            fp_result = _run_fpcalc(file_path)
            if fp_result:
                duration, fingerprint = fp_result
                if duration >= MIN_SONG_DURATION_SECS:
                    acoustid_meta = _lookup_acoustid(duration, fingerprint, artist, title)
                    recording_id = acoustid_meta.get("recording_id") if acoustid_meta else None
                    if recording_id:
                        data = _mb_fetch_recording_with_releases(recording_id, headers)
                        if data and data.get("releases"):
                            _add_release_candidates(data, data["releases"], "acoustid_fingerprint")
    except Exception as e:
        print(f"AcoustID candidate tier failed for '{artist} - {title}': {e}")

    try:
        recording = _mb_find_best_recording(artist, title, headers)
        if recording:
            releases = recording.get("releases") or []
            if releases:
                _add_release_candidates(recording, releases, "musicbrainz_text")
            elif not candidates:
                candidates.append(_build_musicbrainz_guess_for_release(recording, None, artist, title, headers))
    except Exception as e:
        print(f"MusicBrainz tag guess failed for '{artist} - {title}': {e}")

    return candidates


def guess_musicbrainz_tags(artist: str, title: str, offset: int = 0, file_path: Path = None) -> Optional[dict]:
    """Return one MusicBrainz tag guess for a track, by ordered candidate index."""
    candidates = guess_musicbrainz_tag_candidates(artist, title, file_path=file_path)
    if not candidates:
        return None
    if offset < 0 or offset >= len(candidates):
        return None
    guess = dict(candidates[offset])
    guess["candidate_index"] = offset
    guess["candidate_count"] = len(candidates)
    return guess


def _run_fpcalc(file_path: Path) -> Optional[tuple[int, str]]:
    """Run fpcalc on an audio file and return (duration, fingerprint).

    Returns None if fpcalc isn't installed, the file is unreadable,
    or the audio is too short to fingerprint (happens with previews
    and other sad little clips).
    """
    try:
        result = subprocess.run(
            ["fpcalc", "-json", str(file_path)],
            capture_output=True, text=True,
            timeout=TIMEOUT_FPCALC
        )
        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)
        duration = int(data.get("duration", 0))
        fingerprint = data.get("fingerprint", "")

        if not fingerprint or duration < 1:
            return None

        return duration, fingerprint

    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
        return None


def _score_recording(recording: dict, expected_artist: str, expected_title: str) -> int:
    """Score how well an AcoustID recording matches what we think we downloaded.

    AcoustID returns a pile of recordings for a fingerprint  -  covers, remasters,
    compilations, and occasionally Kylie Minogue. This picks the one that
    actually matches what we asked for.
    """
    score = 0
    artist_names = [a.get("name", "").lower() for a in recording.get("artists", [])]
    rec_title = (recording.get("title") or "").lower()
    exp_artist = expected_artist.lower()
    exp_title = expected_title.lower()

    # Artist match is the strongest signal
    if any(exp_artist in name or name in exp_artist for name in artist_names):
        score += 10

    # Title match  -  bonus for exact match, smaller bonus for substring
    if exp_title == rec_title:
        score += 8
    elif exp_title in rec_title or rec_title in exp_title:
        score += 5

    # Penalise covers, remixes, and karaoke  -  we want the real deal
    if "cover" in rec_title or "karaoke" in rec_title or "tribute" in rec_title:
        score -= 8

    # Penalise remastered/live/session versions  -  prefer the original
    if "remaster" in rec_title or "live" in rec_title or "session" in rec_title:
        score -= 2

    # Slight bonus for having release groups (means it's well-catalogued)
    if recording.get("releasegroups"):
        score += 1

    return score


# Secondary types that disqualify a release from deciding what album a track
# belongs to. Scoring already penalises these, but a penalty only helps when
# there is something better to lose to: max() over a single dreadful candidate
# still returns the dreadful candidate. "Promo Only: Mainstream Radio, January
# 2010" scored -20 and was picked anyway, purely because it was the only release
# MusicBrainz knew about for that recording, and a Paramore single duly acquired
# a track number from a radio promo compilation.
_POOR_ALBUM_CONTEXT_SECONDARY = {
    "compilation", "live", "remix", "soundtrack",
    "dj-mix", "mixtape/street", "demo", "interview",
}


def _release_group_is_poor_album_context(rg: dict) -> bool:
    """True when a release group should not be trusted for album name or track position.

    Deliberately categorical rather than a score threshold: "is this a compilation"
    is a fact MusicBrainz already tells us, whereas "is -8 bad enough" is a number
    somebody has to keep re-tuning every time the scorer changes.
    """
    secondary = [t.lower() for t in (rg.get("secondary-types") or rg.get("secondarytypes") or [])]
    if any(t in _POOR_ALBUM_CONTEXT_SECONDARY for t in secondary):
        return True
    for ac in rg.get("artist-credit") or []:
        if isinstance(ac, dict):
            name = (ac.get("name") or (ac.get("artist") or {}).get("name") or "").lower()
            if "various" in name:
                return True
    return False


def _score_release_group(rg: dict, expected_artist: str) -> int:
    """Score a MusicBrainz release group for use as the canonical album.

    Higher = better. Prefers studio albums by the actual artist; penalises
    compilations, promos, radio edits, deluxe/remaster editions, and Various
    Artists releases so we don't end up tagging everything as 'Promo Only
    Modern Rock Radio, Vol 47' or 'Astroworld X (Expanded Anniversary Remix)'.
    """
    score = 0
    primary_type  = (rg.get("type") or rg.get("primary-type") or "").lower()
    secondary_types = [t.lower() for t in (rg.get("secondary-types") or rg.get("secondarytypes") or [])]
    title = (rg.get("title") or "").lower()

    # Strongly prefer studio albums
    if primary_type == "album":
        score += 10
    elif primary_type == "single":
        score += 4
    elif primary_type == "ep":
        score += 3

    # Penalise compilations, soundtracks, remixes, live albums, promos
    _bad_secondary = {"compilation", "live", "remix", "soundtrack", "dj-mix", "mixtape/street", "demo"}
    if any(t in _bad_secondary for t in secondary_types):
        score -= 8

    # Penalise "Promo Only", "Radio", "Now That's What I Call Music", etc.
    _bad_title_fragments = ["promo only", "promo-only", "various artist", "radio edit",
                            "now that's what i call", "now that's what", "hits ", "greatest hits",
                            "best of", "collection", "the very best", "extracts from",
                            "extracts", "sampler", "advance", "promo sampler", "album sampler"]
    if any(frag in title for frag in _bad_title_fragments):
        score -= 10

    # Penalise reissues, deluxe editions, anniversary pressings, etc.
    # These are almost never the canonical release the user actually wants.
    _edition_fragments = ["deluxe", "remaster", "anniversary", "expanded",
                          "bonus track", "special edition", "collector",
                          "complete edition", "super deluxe"]
    if any(frag in title for frag in _edition_fragments):
        score -= 4

    # Slight preference for shorter titles; the original album is usually
    # "Astroworld" not "Astroworld X (Expanded Anniversary Edition)".
    # Cap the penalty so absurdly long names don't dominate the score.
    title_len = len(rg.get("title") or "")
    if title_len > 30:
        score -= min((title_len - 30) // 10, 3)  # -1 per 10 chars over 30, max -3

    # Prefer earlier releases; the original pressing is more likely canonical.
    # Works with both release-group `first-release-date` and individual
    # release `date` (callers may stash it under `_date` before scoring).
    date_str = rg.get("first-release-date") or rg.get("_date") or ""
    year_match = re.match(r'(\d{4})', date_str)
    if year_match:
        year = int(year_match.group(1))
        # Small bonus scaled so earlier years win ties but don't override
        # type-based scoring. 2000 -> +2, 2010 -> +1, 2020+ -> 0
        score += max(0, (2025 - year) // 10)

    # Check release group artist credits
    rg_artist_credit = rg.get("artist-credit") or []
    for ac in rg_artist_credit:
        if isinstance(ac, dict):
            artist_name = (ac.get("name") or ac.get("artist", {}).get("name") or "").lower()
            if "various" in artist_name:
                score -= 12
            elif expected_artist and expected_artist.lower() in artist_name:
                score += 6  # Artist's own release
            elif expected_artist and artist_name in expected_artist.lower():
                score += 4  # Close enough

    return score


def _extract_recording_metadata(recording: dict, expected_artist: str = "") -> dict:
    """Pull artist, title, album, and recording_id from an AcoustID recording."""
    metadata = {
        "title": recording.get("title"),
        "artist": None,
        "album": None,
        "year": None,
        "recording_id": recording.get("id"),
    }

    artists = recording.get("artists", [])
    if artists:
        metadata["artist"] = " & ".join(
            a.get("name", "") for a in artists if a.get("name")
        )

    artist_for_scoring = metadata["artist"] or expected_artist

    # Extract album from release groups  -  prefer studio albums by the actual artist
    # Same rule as the by-id lookup: a compilation is not an album this track
    # belongs to, it is somewhere the track happens to also appear.
    releasegroups = [
        rg for rg in (recording.get("releasegroups", []) or [])
        if not _release_group_is_poor_album_context(rg)
    ]
    if releasegroups:
        album_rg = max(releasegroups, key=lambda rg: _score_release_group(rg, artist_for_scoring))
        metadata["album"] = album_rg.get("title")

    return metadata


def _lookup_acoustid(duration: int, fingerprint: str,
                     expected_artist: str = "", expected_title: str = "") -> Optional[dict]:
    """Ask AcoustID what this audio actually is.

    Returns a dict with title, artist, album, and recording_id
    if we get a confident match, or None if AcoustID shrugs.
    Uses the expected artist/title to pick the best recording from
    the (often chaotic) list AcoustID returns.
    """
    try:
        headers = {"User-Agent": f"MusicGrabber/{VERSION} (https://gitlab.com/g33kphr33k/musicgrabber)"}

        params = {
            "client": get_setting("acoustid_api_key", "0NILMQojj4"),
            "duration": duration,
            "fingerprint": fingerprint,
            "meta": "recordings releasegroups",
        }

        with httpx.Client(timeout=TIMEOUT_HTTP_REQUEST) as client:
            response = client.get(
                "https://api.acoustid.org/v2/lookup",
                params=params, headers=headers
            )

        if response.status_code != 200:
            return None

        data = response.json()
        results = data.get("results", [])
        if not results:
            return None

        # Collect all recordings from results with a good fingerprint score
        all_recordings = []
        for result in results:
            fp_score = result.get("score", 0)
            if fp_score < ACOUSTID_MIN_SCORE:
                continue
            for rec in result.get("recordings", []):
                if rec.get("title"):
                    all_recordings.append((fp_score, rec))

        if not all_recordings:
            best_score = results[0].get("score", 0) if results else 0
            print(f"AcoustID: no usable recordings (best fingerprint score {best_score:.2f})")
            return None

        # Pick the recording that best matches what we think we downloaded
        best_rec = max(
            all_recordings,
            key=lambda x: _score_recording(x[1], expected_artist, expected_title)
        )
        fp_score, recording = best_rec
        match_score = _score_recording(recording, expected_artist, expected_title)

        # Require a meaningful positive signal. Score breakdown: artist match=+10,
        # exact title=+8, partial title=+5, release groups=+1. A score of 0-9 means
        # the title matched but the artist didn't; that's not enough to trust, since
        # "Killing in the Name" will match any cover version. Require at least artist
        # OR (title + release group), i.e. a minimum of 10 to accept.
        if match_score < 10:
            print(f"AcoustID: best recording match score {match_score} is too low, skipping")
            return None

        metadata = _extract_recording_metadata(recording, expected_artist=expected_artist)

        print(f"AcoustID match (fp {fp_score:.2f}, match {match_score}): {metadata['artist']} - {metadata['title']}")
        return metadata

    except Exception as e:
        print(f"AcoustID lookup failed: {e}")
        return None


def _mb_fetch_recording_with_releases(recording_id: str, headers: dict) -> Optional[dict]:
    """Fetch a recording by MBID with its releases/release-groups/media attached.

    `media` is what makes a release carry its track listing; without it
    MusicBrainz cheerfully returns releases with no media block at all, and any
    track-number hunt over them finds precisely nothing.
    """
    try:
        url = f"https://musicbrainz.org/ws/2/recording/{recording_id}"
        params = {"inc": "releases release-groups artist-credits media", "fmt": "json"}
        with httpx.Client(timeout=TIMEOUT_HTTP_REQUEST) as client:
            response = client.get(url, params=params, headers=headers)
        if response.status_code != 200:
            return None
        return response.json()
    except Exception:
        return None


def _lookup_musicbrainz_by_id(recording_id: str, expected_artist: str = "") -> Optional[dict]:
    """Fetch release date from MusicBrainz using a recording MBID.

    AcoustID gives us the recording ID but not the release date,
    so we pop over to MusicBrainz to fill in that gap.
    """
    try:
        headers = {"User-Agent": f"MusicGrabber/{VERSION} (https://gitlab.com/g33kphr33k/musicgrabber)"}

        data = _mb_fetch_recording_with_releases(recording_id, headers)
        if not data:
            return None

        releases = data.get("releases", [])
        if not releases:
            return None

        # Pick the best release rather than blindly taking the first.
        # Wraps the release in a fake release-group dict so _score_release_group can do its job.
        def _release_score(rel: dict) -> int:
            rg = rel.get("release-group") or {}
            # Fold release-level artist credit into the rg dict for scoring
            rg_for_score = dict(rg)
            if not rg_for_score.get("artist-credit"):
                rg_for_score["artist-credit"] = rel.get("artist-credit") or []
            # Stash release date so the scorer can prefer earlier pressings
            if rel.get("date") and not rg_for_score.get("first-release-date"):
                rg_for_score["_date"] = rel["date"]
            return _score_release_group(rg_for_score, expected_artist)

        release = max(releases, key=_release_score)
        result = {}

        # A recording that MusicBrainz only knows from a compilation gives us a
        # correct artist and title and a thoroughly misleading album. Take the
        # former and leave the latter: no album name, no track position, no
        # release id for cover art. Blank beats confidently wrong, because a blank
        # album tag is obviously missing whereas a wrong one gets believed.
        rg_for_context = dict(release.get("release-group") or {})
        if not rg_for_context.get("artist-credit"):
            rg_for_context["artist-credit"] = release.get("artist-credit") or []
        if _release_group_is_poor_album_context(rg_for_context):
            print(
                f"MB: ignoring album context from {release.get('title')!r} "
                f"(secondary types {rg_for_context.get('secondary-types')}); "
                "keeping artist/title only"
            )
        else:
            result["release_mbid"] = release.get("id")

            date_str = release.get("date", "")
            if date_str:
                year_match = re.match(r'(\d{4})', date_str)
                if year_match:
                    result["year"] = year_match.group(1)

            if release.get("title"):
                result["album"] = release["title"]

            # Track position within the release
            track_num, track_total = _extract_track_position(release)
            if track_num:
                result["track_number"] = track_num
                result["track_total"] = track_total

        # Recording-level length (ms) is on the top-level recording object
        length_ms = data.get("length")
        if length_ms:
            result["expected_duration_secs"] = length_ms / 1000.0

        return result if result else None

    except Exception:
        return None


# The "live" family of version keywords, pulled out of matching.py's shared
# list. We deliberately only treat these as a version *annotation*, never as a
# bare substring of the title. "Live and Let Die" is studio music; the band
# "Live" is studio music; "Livin' on a Prayer" does not even contain the word.
# See the cross-cutting live-detection rules in the ISRC-anchored album plan.
_LIVE_VERSION_KEYWORDS = tuple(
    kw for kw in _HEAVY_VERSION_KEYWORDS
    if kw in ("live", "live at", "live from", "in concert", "unplugged")
)


def _title_has_live_annotation(title: str) -> bool:
    """True only when "live"/"unplugged"/"in concert" appears as a positional
    version annotation, never as part of the core song title.

    This mirrors matching.py `similarity()`: that function only treats a heavy
    version keyword as significant when it is in the *trailing* segment after a
    prefix match, so a bare substring like "Live and Let Die" or the band
    "Live" never trips it. We apply the same discipline here:

    - bracketed:    "(live...)", "[live...]"
    - trailing dash: " - live", " - live at/from/in ..."
    - explicit live phrases anywhere: "live at", "live from", "live in",
      "in concert", "recorded live", "unplugged"

    A naive `"live" in title.lower()` is explicitly NOT used; it is a bug.
    """
    if not title:
        return False
    t = title.lower().strip()

    # Helper: does a trailing segment START with a live annotation? Prefix
    # discipline, never a bare substring, so "Deliver" / "Alive" never register.
    def _segment_is_live(seg: str) -> bool:
        seg = seg.strip().lstrip(' -')  # tolerate "(- live)" style separators
        if not seg:
            return False
        triggers = set(_LIVE_VERSION_KEYWORDS) | {"unplugged", "recorded live", "in concert"}
        return any(seg == kw or seg.startswith(kw + " ") for kw in triggers)

    # 1. Bracketed annotation: "(Live)", "(Live at Wembley)", "[Live from Slane Castle]".
    for match in re.finditer(r'[\(\[]([^\)\]]*)[\)\]]', t):
        if _segment_is_live(match.group(1)):
            return True

    # 2. Trailing dash annotation: " - Live", " - Live at the Apollo".
    #    Reuse matching.py's discipline of looking only at the trailing segment.
    dash_split = re.split(r'\s[-–]\s', t)
    if len(dash_split) > 1 and _segment_is_live(dash_split[-1]):
        return True

    # 3. Explicit live phrases anywhere in the title. These cannot be part of a
    #    core song name; "live at", "live from", "live in <place>" etc. always
    #    denote a recording context.
    for phrase in ("live at", "live from", "live in", "in concert", "recorded live"):
        if phrase in t:
            return True

    return False


def _disambiguation_is_live(disambiguation: str) -> bool:
    """True if a MusicBrainz recording disambiguation marks it as live.

    MB writes things like "live, 2001-06-23: Slane Castle". This is
    authoritative, so a hit here is enough on its own.
    """
    if not disambiguation:
        return False
    d = disambiguation.lower()
    # Word-boundary so "alive" / "deliverance" do not register.
    return bool(re.search(r'\blive\b', d)) or "unplugged" in d or "in concert" in d


def _lookup_recording_disambiguation(recording_id: str) -> Optional[str]:
    """Fetch a recording's MusicBrainz `disambiguation` string, or None.

    Kept as its own tiny function (rather than folded into _lookup_musicbrainz_by_id)
    so the verification path can ask MB the one question it cares about and so the
    tests can monkeypatch it without faking the whole release-scoring round trip.
    Returns None on any failure; the caller treats None as "no live signal".
    """
    if not recording_id:
        return None
    if not get_setting_bool("enable_musicbrainz", True):
        return None
    try:
        headers = {"User-Agent": f"MusicGrabber/{VERSION} (https://gitlab.com/g33kphr33k/musicgrabber)"}
        url = f"https://musicbrainz.org/ws/2/recording/{recording_id}"
        params = {"fmt": "json"}
        with httpx.Client(timeout=TIMEOUT_HTTP_REQUEST) as client:
            response = client.get(url, params=params, headers=headers)
        if response.status_code != 200:
            return None
        data = response.json()
        return data.get("disambiguation") or None
    except Exception:
        return None


def verify_recording(acoustid_result: Optional[dict], *,
                     expected_recording_mbid: Optional[str] = None,
                     query: Optional[str] = None,
                     high_confidence: bool = True) -> str:
    """Return a verdict on whether the downloaded file is the recording we wanted.

    Returns one of 'ok' | 'reject_live' | 'reject_wrong' | 'uncertain'. The
    caller (Task G) treats 'uncertain' exactly like 'ok' (keep the file); only
    a CONFIDENT contrary identification ever produces a reject_* verdict. The
    asymmetry is the whole safety of the feature: a false reject trashes a
    legitimate download of something obscure, so when in doubt we do nothing.

    Args:
        acoustid_result: the metadata dict from `_lookup_acoustid` (carries
            `recording_id` and `title`), or None if AcoustID could not place
            the file. None / a result below `ACOUSTID_MIN_SCORE` (which
            `_lookup_acoustid` already filters out, returning None) is
            'uncertain'.
        expected_recording_mbid: the studio recording MBID we asked for, known
            on the album flow via `fetch_album_tracks`. Absent on the single flow.
        query: the original free-text search query. If the user explicitly asked
            for a live/unplugged take, we never reject for live-ness.
        high_confidence: whether the AcoustID identification is confident enough
            to act on a "wrong recording" verdict. Defaults True (the presence of
            a non-None result already means it cleared `ACOUSTID_MIN_SCORE` and the
            recording-match floor inside `_lookup_acoustid`). A caller with extra
            doubt can pass False to soften a reject_wrong down to 'uncertain'.

    Live detection follows the cross-cutting rules: identity beats strings (an
    exact MBID match is 'ok' and never re-examined), a bare "live" substring is
    never a trigger, MusicBrainz `disambiguation` is authoritative, and the
    exemptions (query asked for it, title-word "live", any uncertainty) always win.
    """
    # No confident AcoustID match at all -> we cannot say anything. Keep.
    if not acoustid_result:
        return "uncertain"

    matched_mbid = acoustid_result.get("recording_id")
    # The user explicitly asked for a variant (live/unplugged/acoustic/...).
    # In that case live-ness is wanted, not a defect: never reject for it.
    user_wants_variant = query_requests_variant(query) if query else False

    # --- Album flow: we have the exact recording MBID we wanted. ------------
    if expected_recording_mbid:
        # Identity beats strings. An exact match is the recording we wanted,
        # full stop; the word "live" in the title is irrelevant here.
        if matched_mbid and matched_mbid == expected_recording_mbid:
            return "ok"

        # Different recording. Is it a live take?
        if _is_live_recording(acoustid_result) and not user_wants_variant:
            return "reject_live"

        # Different, not live. Could be a remaster/reissue (different MBID, same
        # performance, perfectly fine) -> only reject on a confident signal.
        if matched_mbid and high_confidence:
            return "reject_wrong"
        return "uncertain"

    # --- Single flow: no expected MBID, judge live-ness alone. --------------
    if _is_live_recording(acoustid_result) and not user_wants_variant:
        return "reject_live"

    return "ok"


def _is_live_recording(acoustid_result: dict) -> bool:
    """True if the identified recording looks like a live take.

    Prefers MusicBrainz `disambiguation` (authoritative) when available, and
    otherwise falls back to a positional annotation check on the title. Never a
    bare substring; see `_title_has_live_annotation`.
    """
    title = acoustid_result.get("title") or ""

    # Title annotation is cheap and offline; check it first.
    if _title_has_live_annotation(title):
        return True

    # MusicBrainz disambiguation is authoritative. The AcoustID metadata does
    # not carry it (the lookup asks for "recordings releasegroups"), so we ask
    # MB the one question, reusing the by-recording-id endpoint. Best-effort:
    # None / a network wobble just means "no extra live signal", which keeps
    # the file (the safe direction).
    rec_id = acoustid_result.get("recording_id")
    if rec_id:
        disambiguation = _lookup_recording_disambiguation(rec_id)
        if _disambiguation_is_live(disambiguation or ""):
            return True

    return False


def lookup_musicbrainz_by_isrc(isrc: str, expected_artist: str = "", expected_title: str = "") -> Optional[dict]:
    """Look up a recording by ISRC.

    Tidal hands us a real ISRC at search time, so we can ask MusicBrainz the
    exact question instead of guessing by title and crossing our fingers.
    The ISRC endpoint returns the recording; we then reuse the by-ID release
    scoring to land on a sensible album/year/track number.

    An ISRC is not always a one-recording answer: MusicBrainz has plenty of
    cases where the same ISRC is attached to both the studio take and one of
    its umpteen bootleg live versions (data-entry error upstream, not ours to
    fix). Taking whichever the API lists first previously meant "Creep" could
    come back tagged as a 1998 Tibetan Freedom Concert bootleg. Score every
    candidate the same way the title-search path already does, and take the
    best one.
    """
    if not get_setting_bool("enable_musicbrainz", True):
        return None
    if not isrc:
        return None
    try:
        headers = {"User-Agent": f"MusicGrabber/{VERSION} (https://gitlab.com/g33kphr33k/musicgrabber)"}
        url = f"https://musicbrainz.org/ws/2/isrc/{isrc}"
        params = {"inc": "artist-credits releases release-groups", "fmt": "json"}
        with httpx.Client(timeout=TIMEOUT_HTTP_REQUEST) as client:
            response = client.get(url, params=params, headers=headers)
        if response.status_code != 200:
            return None
        data = response.json()
        recordings = data.get("recordings") or []
        if not recordings:
            return None

        recording = max(
            recordings,
            key=lambda rec: _score_recording_canonicity(rec, expected_artist, expected_title),
        )
        recording_id = recording.get("id")
        artist_credit = recording.get("artist-credit") or []
        artist_name = "".join(
            (ac.get("name") or ac.get("artist", {}).get("name", "")) + (ac.get("joinphrase") or "")
            for ac in artist_credit
            if isinstance(ac, dict)
        ).strip() or expected_artist or None

        metadata = {
            "title": recording.get("title"),
            "artist": artist_name,
            "recording_id": recording_id,
            "metadata_source": "musicbrainz_isrc",
        }
        length_ms = recording.get("length")
        if length_ms:
            metadata["expected_duration_secs"] = length_ms / 1000.0

        # Re-use the by-ID lookup so we get the same release-scoring as everyone else
        if recording_id:
            extra = _lookup_musicbrainz_by_id(recording_id, expected_artist=artist_name or expected_artist)
            if extra:
                for k, v in extra.items():
                    if v and not metadata.get(k):
                        metadata[k] = v
        return metadata

    except Exception as e:
        print(f"MusicBrainz ISRC lookup failed for {isrc}: {e}")
        return None


# Deezer occasionally files a track under a greatest-hits or live package even
# when record_type says "album", and the title usually gives it away. Belt and
# braces alongside the record_type check so we don't route a studio single into
# "The Very Best Of...".
_DEEZER_NONCANONICAL_ALBUM_RE = re.compile(
    r"\b(greatest hits|best of|the hits|hits collection|anthology|essentials?|"
    r"compilation|live|unplugged|in concert|karaoke|tribute|soundtrack|"
    r"original score|o\.?s\.?t\.?)\b",
    re.IGNORECASE,
)


def lookup_deezer_album(artist: str, title: str) -> Optional[dict]:
    """Ask Deezer which album a track belongs to, to fill MusicBrainz's gaps.

    MusicBrainz is canonical but slow to ingest new releases, so plenty of fresh
    singles come back album-less and never get routed out of Singles/. Deezer's
    catalogue is bang up to date and hands us the album inline, so we use it
    purely to plug that hole: only when MB gave us no album, and only if Deezer
    is confident it's the same track AND the album looks like a real studio
    release (record_type album/ep, not a 'Now 87' compilation).

    Returns a dict with 'album' (+ optional year/track_number/track_total), or
    None. Deliberately does NOT return a duration, so it never trips the
    MusicBrainz duration sanity check on a path that previously had no album.
    """
    if not get_setting_bool("enable_deezer_metadata", True):
        return None
    if not (artist and title):
        return None
    try:
        with httpx.Client(timeout=TIMEOUT_DEEZER) as client:
            # Advanced query keeps Deezer honest about which field is which; if
            # that's too strict to match, fall back to a loose free-text search.
            params = {"q": f'artist:"{artist}" track:"{title}"', "limit": DEEZER_METADATA_SEARCH_LIMIT}
            resp = client.get(DEEZER_SEARCH_URL, params=params)
            items = (resp.json() or {}).get("data") or [] if resp.status_code == 200 else []
            if not items:
                resp = client.get(DEEZER_SEARCH_URL, params={"q": f"{artist} {title}", "limit": DEEZER_METADATA_SEARCH_LIMIT})
                items = (resp.json() or {}).get("data") or [] if resp.status_code == 200 else []
            if not items:
                return None

            # Shared confidence scorer picks the best track and the version-aware
            # penalty keeps a remix/live cut from sneaking past a plain query.
            best, best_conf = None, 0.0
            for item in items:
                cand_title = item.get("title") or ""
                cand_artist = (item.get("artist") or {}).get("name") or ""
                conf, _ = compute_match_confidence(artist, title, cand_title, cand_artist)
                if conf > best_conf:
                    best, best_conf = item, conf
            if not best or best_conf < DEEZER_METADATA_MATCH_FLOOR:
                return None

            album_obj = best.get("album") or {}
            album_id = album_obj.get("id")
            album_title = (album_obj.get("title") or "").strip()
            if not album_id or not album_title:
                return None

            # One more call to learn the album's type, year and track count.
            # Only ever runs on the MB-miss path, so it's cheap in aggregate.
            album_resp = client.get(f"{DEEZER_API_URL}/album/{album_id}")
            if album_resp.status_code != 200:
                return None
            album = album_resp.json() or {}

        record_type = (album.get("record_type") or "").lower()
        if record_type not in ("album", "ep"):
            # Singles and compilations don't earn a folder of their own.
            return None
        if _DEEZER_NONCANONICAL_ALBUM_RE.search(album_title):
            return None

        meta = {"album": album_title, "album_artist": artist, "metadata_source": "deezer_album"}
        release_date = album.get("release_date") or ""
        if len(release_date) >= 4 and release_date[:4].isdigit():
            meta["year"] = release_date[:4]
        nb_tracks = album.get("nb_tracks")
        if isinstance(nb_tracks, int) and nb_tracks > 0:
            meta["track_total"] = nb_tracks
        # Deezer's album tracklist comes back in running order but doesn't carry
        # an explicit track_position, so the track's index in the list is its
        # number. Correct for single-disc albums (the overwhelming majority of
        # the new-single case this path serves); only set track_total alongside
        # it so we never write a bare "/11" with no number.
        track_id = best.get("id")
        tracklist = (album.get("tracks") or {}).get("data") or []
        for idx, entry in enumerate(tracklist):
            if entry.get("id") == track_id:
                meta["track_number"] = idx + 1
                break
        else:
            meta.pop("track_total", None)
        return meta

    except Exception as e:
        print(f"Deezer album lookup failed for {artist} - {title}: {e}")
        return None


def lookup_metadata(artist: str, title: str, file_path: Path = None) -> Optional[dict]:
    """Look up track metadata, trying audio fingerprinting first.

    The hierarchy of increasingly desperate measures:
    1. Fingerprint the file with fpcalc -> query AcoustID
    2. If AcoustID matches, fetch the release date from MusicBrainz by recording ID
    3. If fingerprinting fails or scores too low, fall back to text-based MusicBrainz search
    4. If MusicBrainz still gave us no album, ask Deezer to fill that one field

    Returns a dict with 'title', 'artist', 'album', 'year' or None.
    """
    result = _lookup_metadata_musicbrainz(artist, title, file_path)

    # Step 4: MusicBrainz couldn't pin down an album (common for brand-new
    # releases it hasn't ingested yet). Deezer usually can, so let it fill the
    # one field auto-album routing actually needs. We never overwrite anything
    # MusicBrainz was sure about; Deezer only adds what's missing.
    if get_setting_bool("enable_deezer_metadata", True) and (not result or not result.get("album")):
        deezer = lookup_deezer_album(artist, title)
        if deezer and deezer.get("album"):
            if result is None:
                result = {"title": title, "artist": artist}
            for key in ("album", "year", "track_number", "track_total", "album_artist"):
                if deezer.get(key) and not result.get(key):
                    result[key] = deezer[key]
            result.setdefault("metadata_source", deezer.get("metadata_source", "deezer_album"))

    return result


def _lookup_metadata_musicbrainz(artist: str, title: str, file_path: Path = None) -> Optional[dict]:
    """AcoustID + MusicBrainz half of lookup_metadata (steps 1-3)."""
    if not get_setting_bool("enable_musicbrainz", True):
        return None

    # Step 1: Try AcoustID fingerprinting (if we have a file to work with)
    if file_path and file_path.exists():
        fp_result = _run_fpcalc(file_path)
        if fp_result:
            duration, fingerprint = fp_result
            if duration < MIN_SONG_DURATION_SECS:
                # Clips this short fingerprint unreliably  -  AcoustID might return
                # a confident match for the correct song, but we'd be tagging the wrong
                # (too short) file with metadata that doesn't describe it. Skip it.
                print(
                    f"AcoustID skipped: file is only {duration}s "
                    f"(< {MIN_SONG_DURATION_SECS}s), too short to fingerprint reliably"
                )
                fp_result = None
        if fp_result:
            duration, fingerprint = fp_result
            acoustid_meta = _lookup_acoustid(duration, fingerprint, artist, title)

            if acoustid_meta:
                acoustid_meta["metadata_source"] = "acoustid_fingerprint"
                # Step 2: Fill in release info (album, year, duration) from MusicBrainz
                recording_id = acoustid_meta.get("recording_id")
                if recording_id:
                    mb_extra = _lookup_musicbrainz_by_id(recording_id, expected_artist=artist)
                    if mb_extra:
                        if mb_extra.get("year") and not acoustid_meta.get("year"):
                            acoustid_meta["year"] = mb_extra["year"]
                        if mb_extra.get("album") and not acoustid_meta.get("album"):
                            acoustid_meta["album"] = mb_extra["album"]
                        if mb_extra.get("track_number") and not acoustid_meta.get("track_number"):
                            acoustid_meta["track_number"] = mb_extra["track_number"]
                        if mb_extra.get("track_total") and not acoustid_meta.get("track_total"):
                            acoustid_meta["track_total"] = mb_extra["track_total"]
                        if mb_extra.get("expected_duration_secs") and not acoustid_meta.get("expected_duration_secs"):
                            acoustid_meta["expected_duration_secs"] = mb_extra["expected_duration_secs"]
                        if mb_extra.get("release_mbid") and not acoustid_meta.get("release_mbid"):
                            acoustid_meta["release_mbid"] = mb_extra["release_mbid"]

                return acoustid_meta

    # Step 3: Fall back to text-based MusicBrainz search
    return lookup_musicbrainz(artist, title)


def fetch_mb_expected_duration(artist: str, title: str) -> Optional[float]:
    """Quick MusicBrainz lookup to get the canonical duration for a track.

    Used at search time to score results by how close their duration is to
    what MusicBrainz considers the real thing. Returns seconds as a float,
    or None if MB is disabled, the track is unknown, or the lookup fails.
    No file required  -  text search only.
    """
    result = lookup_musicbrainz(artist, title)
    return result.get("expected_duration_secs") if result else None


def fetch_lyrics(artist: str, title: str) -> Optional[str]:
    """Fetch synced lyrics from LRClib API"""
    if not get_setting_bool("enable_lyrics", True):
        return None

    try:
        headers = {"User-Agent": f"MusicGrabber/{VERSION} (https://gitlab.com/g33kphr33k/musicgrabber)"}

        with httpx.Client(timeout=TIMEOUT_HTTP_REQUEST) as client:
            # Try the get endpoint first (exact match)
            params = {
                "artist_name": artist,
                "track_name": title
            }

            response = client.get(
                "https://lrclib.net/api/get",
                params=params,
                headers=headers
            )

            if response.status_code == 200:
                data = response.json()
                # Prefer synced lyrics, fall back to plain
                if data.get("syncedLyrics"):
                    return data["syncedLyrics"]
                elif data.get("plainLyrics"):
                    return data["plainLyrics"]

            # If exact match fails, try search
            search_params = {"q": f"{artist} {title}"}
            search_response = client.get(
                "https://lrclib.net/api/search",
                params=search_params,
                headers=headers
            )

            if search_response.status_code == 200:
                results = search_response.json()
                if results:
                    # Return first match with synced lyrics, or first with plain
                    for result in results:
                        if result.get("syncedLyrics"):
                            return result["syncedLyrics"]
                    for result in results:
                        if result.get("plainLyrics"):
                            return result["plainLyrics"]

        return None

    except Exception as e:
        # If lyrics lookup fails, log and continue without
        print(f"Lyrics lookup failed for {artist} - {title}: {e}")
        return None


def save_lyrics_file(flac_path: Path, lyrics: str):
    """Save lyrics as .lrc file alongside the audio file"""
    lrc_path = flac_path.with_suffix(".lrc")
    lrc_path.write_text(lyrics, encoding="utf-8")
    set_file_permissions(lrc_path)


def _is_source_branding(text: str) -> bool:
    """Return True if the string looks like YouTube/distributor auto-generated boilerplate.

    Matches the block that yt-dlp stuffs into COMMENT tags, e.g.:
      "Provided to YouTube by DistroKid\\n\\nTrack Name · Artist\\n\\n℗ 2024 Label\\n\\n..."
    Also catches the shorter auto-generated variant and standalone rights lines.
    """
    if not text:
        return False
    t = text.strip()
    return bool(
        re.match(r'Provided to YouTube by ', t)
        or re.match(r'Auto-generated by YouTube', t, re.IGNORECASE)
        or re.match(r'℗\s*\d{4}', t)
        or re.match(r'Released on:\s', t)
    )


def read_artist_title(file_path: Path) -> tuple[str | None, str | None]:
    """Read artist and title from an audio file's tags, format-agnostic.

    Uses mutagen's easy mode so FLAC/MP3/M4A/Ogg all answer with the same keys.
    Returns (artist, title), either may be None. Handy when we have a file on disk
    but need to know what it is (e.g. re-tagging a restored track).
    """
    try:
        import mutagen
        audio = mutagen.File(str(file_path), easy=True)
        if not audio:
            return None, None
        artist = (audio.get("artist", [None]) or [None])[0]
        title = (audio.get("title", [None]) or [None])[0]
        return (artist or None), (title or None)
    except Exception:
        return None, None


def _split_track_field(raw) -> tuple[object, object]:
    """Split a "2/14"-style track field into its number and total.

    Returns (number, total), total being None when the field is a plain number.
    """
    if raw is None:
        return None, None
    text = str(raw).strip()
    if not text:
        return None, None
    if "/" in text:
        number, _, total = text.partition("/")
        return number.strip(), total.strip()
    return text, None


def _positive_int_or_none(value) -> int | None:
    """Track positions are counting numbers; anything else is not worth keeping."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def read_existing_track_number(file_path: Path) -> tuple[int | None, int | None]:
    """Read existing track number and total from an audio file's tags.

    Returns (track_number, track_total), either or both may be None.
    Useful for checking whether a source already baked in track info before we
    overwrite it with MusicBrainz guesses.
    """
    try:
        suffix = file_path.suffix.lower()
        if suffix == ".flac":
            audio = FLAC(str(file_path))
            tn = audio.get("TRACKNUMBER", [None])[0]
            tt = audio.get("TRACKTOTAL", audio.get("TOTALTRACKS", [None]))[0]
        elif suffix == ".mp3":
            from mutagen.easyid3 import EasyID3
            audio = EasyID3(str(file_path))
            tn = (audio.get("tracknumber", [None]) or [None])[0]
            tt = None
        elif suffix in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            audio = MP4(str(file_path))
            trkn = audio.get("trkn", [(None, None)])[0]
            tn, tt = (trkn[0], trkn[1]) if trkn else (None, None)
            if tt == 0:
                tt = None
        elif suffix in (".ogg", ".opus"):
            from mutagen.oggopus import OggOpus
            from mutagen.oggvorbis import OggVorbis
            audio = OggOpus(str(file_path)) if suffix == ".opus" else OggVorbis(str(file_path))
            tn = audio.get("TRACKNUMBER", [None])[0]
            tt = audio.get("TRACKTOTAL", audio.get("TOTALTRACKS", [None]))[0]
        else:
            return None, None

        # "2/14" is legal in a Vorbis comment just as it is in ID3, and plenty of
        # rippers write it that way. This used to be unpacked for MP3 only, so a
        # FLAC saying "02/11" hit int("02/11"), raised, and was reported as having
        # no track number at all, at which point a MusicBrainz guess walked in and
        # took its place. Losing the perfectly readable TRACKTOTAL on the way out
        # was the insult after the injury.
        tn, tn_total = _split_track_field(tn)
        if tt in (None, ""):
            tt = tn_total

        # Converted separately: one unparseable field should not discard the other.
        return _positive_int_or_none(tn), _positive_int_or_none(tt)
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# ReplayGain 2.0
#
# Four numbers per file, and every container has its own opinion about where to
# put them. Vorbis comments (FLAC/Ogg/Opus) take plain uppercase keys, ID3 wants
# lowercase TXXX frames, and MP4 wants freeform iTunes atoms. Nobody sat down and
# agreed this; it simply accreted.
# ---------------------------------------------------------------------------

_REPLAYGAIN_KEYS = (
    "replaygain_track_gain",
    "replaygain_track_peak",
    "replaygain_album_gain",
    "replaygain_album_peak",
    "replaygain_reference_loudness",
)


def _format_gain(gain_db: float) -> str:
    """ReplayGain gains are written as an explicitly signed number, ' dB' suffix.

    The plus sign on positive gains is not decoration; plenty of parsers in the
    wild expect it, and it costs one character to keep them happy.
    """
    return f"{gain_db:+.2f} dB"


def _format_peak(peak: float) -> str:
    """Peaks are linear sample values, conventionally to six decimal places."""
    return f"{max(peak, 0.0):.6f}"


def read_replaygain_tags(file_path: Path) -> dict:
    """Return whatever ReplayGain tags a file already carries, keys lowercased.

    Empty dict means either "no tags" or "we could not read it"; the caller
    treats both the same way, so we do not bother distinguishing.
    """
    try:
        suffix = file_path.suffix.lower()
        found: dict[str, str] = {}

        if suffix in (".flac", ".ogg", ".oga", ".opus"):
            audio = _open_vorbis_comment_file(file_path)
            if audio is None:
                return {}
            for key, values in audio.items():
                lowered = key.lower()
                if lowered in _REPLAYGAIN_KEYS and values:
                    found[lowered] = str(values[0])

        elif suffix == ".mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError
            try:
                audio = ID3(str(file_path))
            except ID3NoHeaderError:
                return {}
            for frame in audio.getall("TXXX"):
                lowered = (frame.desc or "").lower()
                if lowered in _REPLAYGAIN_KEYS and frame.text:
                    found[lowered] = str(frame.text[0])

        elif suffix in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            audio = MP4(str(file_path))
            for key, values in audio.items():
                lowered = key.split(":")[-1].lower()
                if lowered in _REPLAYGAIN_KEYS and values:
                    value = values[0]
                    found[lowered] = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)

        return found
    except Exception:
        return {}


def _open_vorbis_comment_file(file_path: Path):
    """Open a FLAC/Ogg/Opus file for Vorbis-comment editing, or None."""
    suffix = file_path.suffix.lower()
    if suffix == ".flac":
        return FLAC(str(file_path))
    if suffix == ".opus":
        from mutagen.oggopus import OggOpus
        return OggOpus(str(file_path))
    if suffix in (".ogg", ".oga"):
        from mutagen.oggvorbis import OggVorbis
        return OggVorbis(str(file_path))
    return None


def apply_replaygain_tags(
    file_path: Path,
    track_gain_db: float | None = None,
    track_peak: float | None = None,
    album_gain_db: float | None = None,
    album_peak: float | None = None,
    reference_lufs: float | None = None,
    replace_existing: bool = False,
) -> bool:
    """Write ReplayGain 2.0 tags. Never touches a single audio sample.

    Returns True if anything was written. When `replace_existing` is False and
    the file already has the tags we were about to write, we leave them alone;
    somebody went to the trouble of calculating those, and it was probably not
    an accident.

    Album values are optional, so a single track can be tagged now and given
    album values later once the rest of its album has turned up.
    """
    try:
        values: dict[str, str] = {}
        if track_gain_db is not None:
            values["replaygain_track_gain"] = _format_gain(track_gain_db)
        if track_peak is not None:
            values["replaygain_track_peak"] = _format_peak(track_peak)
        if album_gain_db is not None:
            values["replaygain_album_gain"] = _format_gain(album_gain_db)
        if album_peak is not None:
            values["replaygain_album_peak"] = _format_peak(album_peak)
        if reference_lufs is not None:
            values["replaygain_reference_loudness"] = f"{reference_lufs:.2f} LUFS"
        if not values:
            return False

        if not replace_existing:
            existing = read_replaygain_tags(file_path)
            values = {k: v for k, v in values.items() if k not in existing}
            if not values:
                return False

        suffix = file_path.suffix.lower()

        if suffix in (".flac", ".ogg", ".oga", ".opus"):
            audio = _open_vorbis_comment_file(file_path)
            if audio is None:
                return False
            # Vorbis comment keys are case-insensitive but duplicable, so drop any
            # existing spelling before writing ours or players see two answers.
            for key in list(audio.keys()):
                if key.lower() in values:
                    del audio[key]
            for key, value in values.items():
                audio[key.upper()] = value
            audio.save()

        elif suffix == ".mp3":
            from mutagen.id3 import ID3, TXXX, ID3NoHeaderError
            try:
                audio = ID3(str(file_path))
            except ID3NoHeaderError:
                audio = ID3()
            for key, value in values.items():
                audio.delall(f"TXXX:{key}")
                audio.add(TXXX(encoding=3, desc=key, text=[value]))
            audio.save(str(file_path))

        elif suffix in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4, MP4FreeForm
            audio = MP4(str(file_path))
            for key, value in values.items():
                atom = f"----:com.apple.iTunes:{key}"
                audio[atom] = [MP4FreeForm(value.encode("utf-8"))]
            audio.save()

        else:
            # .webm and friends have nowhere sensible to put these.
            return False

        set_file_permissions(file_path)
        return True

    except Exception as e:
        print(f"ReplayGain tagging failed for {file_path.name}: {e}")
        return False


def apply_metadata_to_file(
    file_path: Path,
    artist: str,
    title: str,
    album: str = "",
    year: str = None,
    track_number: int | None = None,
    track_total: int | None = None,
    album_art_bytes: bytes | None = None,
    album_art_mime: str | None = None,
    album_artist: str | None = None,
    source: str | None = None,
    source_quality: str | None = None,
    file_id: str | None = None,
    source_codec: str | None = None,
    source_bitrate_kbps: int | None = None,
    compilation: bool = False,
):
    """Apply metadata to audio file using mutagen (supports multiple formats).

    source / source_quality stamp where MusicGrabber fetched the audio and at
    what quality. The SOURCE tag doubles as the "this file is ours" eligibility
    marker for the track-upgrades feature; without it a file is invisible to
    upgrades. Only written when provided, so existing tags are never clobbered.

    compilation, when True, sets the iTunes-style "part of a compilation" flag so
    Plex/Navidrome group the file under one Various-Artists album rather than
    spawning a separate album per track. Only written when True, never cleared.
    """
    try:
        if source and not file_id:
            file_id = str(uuid.uuid4())
        if source_quality and not source_codec:
            match = re.search(r"\bFROM\s+([A-Z0-9]+)", source_quality.upper())
            if not match:
                match = re.search(r"^([A-Z0-9]+)(?:\s+|$)", source_quality.upper())
            source_codec = match.group(1) if match else None
        if source_quality and source_bitrate_kbps is None:
            origin_text = source_quality.split("from", 1)[-1] if "from" in source_quality.lower() else source_quality
            match = re.search(r"(\d+)\s*kbps", origin_text, re.I)
            source_bitrate_kbps = int(match.group(1)) if match else None
        suffix = file_path.suffix.lower()
        track_number = int(track_number) if track_number else None
        track_total = int(track_total) if track_total else None
        art_mime = (album_art_mime or "image/jpeg").lower()
        has_art = bool(album_art_bytes)

        if suffix == '.flac':
            from mutagen.flac import Picture
            audio = FLAC(str(file_path))
            audio["ARTIST"] = artist
            audio["TITLE"] = title
            if album:
                audio["ALBUM"] = album
            if album_artist:
                audio["ALBUMARTIST"] = album_artist
                audio["ALBUM ARTIST"] = album_artist
            if compilation:
                audio["COMPILATION"] = "1"
            if year:
                audio["DATE"] = year
            if track_number:
                audio["TRACKNUMBER"] = str(track_number)
                if track_total:
                    audio["TRACKTOTAL"] = str(track_total)
                    audio["TOTALTRACKS"] = str(track_total)
            # Wipe yt-dlp source branding from COMMENT tag
            if any(_is_source_branding(c) for c in audio.get("COMMENT", [])):
                audio["COMMENT"] = []
            if source:
                audio["SOURCE"] = source
            if source_quality:
                audio["SOURCE_QUALITY"] = source_quality
            if file_id and not audio.get("MUSICGRABBER_FILE_ID"):
                audio["MUSICGRABBER_FILE_ID"] = file_id
            if source_codec:
                audio["SOURCE_CODEC"] = source_codec
            if source_bitrate_kbps is not None:
                audio["SOURCE_BITRATE"] = str(source_bitrate_kbps)
            if has_art:
                pic = Picture()
                pic.type = 3  # front cover
                pic.mime = art_mime
                pic.data = album_art_bytes
                audio.clear_pictures()
                audio.add_picture(pic)
            audio.save()

        elif suffix == '.mp3':
            from mutagen.easyid3 import EasyID3
            from mutagen.mp3 import MP3
            from mutagen.id3 import ID3, APIC
            try:
                audio = EasyID3(str(file_path))
            except Exception:
                # If no ID3 tag exists, create one
                mp3 = MP3(str(file_path))
                mp3.add_tags()
                mp3.save()
                audio = EasyID3(str(file_path))
            audio["artist"] = artist
            audio["title"] = title
            if album:
                audio["album"] = album
            if album_artist:
                audio["albumartist"] = [album_artist]
            if compilation:
                # TCMP is the iTunes compilation flag; EasyID3 doesn't know it by
                # default, so register it as a plain text key before writing.
                try:
                    EasyID3.RegisterTextKey("compilation", "TCMP")
                    audio["compilation"] = ["1"]
                except Exception:
                    pass
            if year:
                audio["date"] = year
            if track_number:
                tn = f"{track_number}/{track_total}" if track_total else str(track_number)
                audio["tracknumber"] = [tn]
            if any(_is_source_branding(c) for c in audio.get("comment", [])):
                audio["comment"] = []
            if source or source_quality:
                # EasyID3 won't take arbitrary keys; register them as TXXX frames.
                EasyID3.RegisterTXXXKey("source", "SOURCE")
                EasyID3.RegisterTXXXKey("source_quality", "SOURCE_QUALITY")
                EasyID3.RegisterTXXXKey("musicgrabber_file_id", "MUSICGRABBER_FILE_ID")
                EasyID3.RegisterTXXXKey("source_codec", "SOURCE_CODEC")
                EasyID3.RegisterTXXXKey("source_bitrate", "SOURCE_BITRATE")
                if source:
                    audio["source"] = source
                if source_quality:
                    audio["source_quality"] = source_quality
                if file_id and not audio.get("musicgrabber_file_id"):
                    audio["musicgrabber_file_id"] = file_id
                if source_codec:
                    audio["source_codec"] = source_codec
                if source_bitrate_kbps is not None:
                    audio["source_bitrate"] = str(source_bitrate_kbps)
            audio.save()
            if has_art:
                mp3 = MP3(str(file_path), ID3=ID3)
                if mp3.tags is None:
                    mp3.add_tags()
                mp3.tags.delall("APIC")
                mp3.tags.add(APIC(encoding=3, mime=art_mime, type=3, desc="Cover", data=album_art_bytes))
                mp3.save(v2_version=3)

        elif suffix in ['.m4a', '.mp4']:
            from mutagen.mp4 import MP4, MP4Cover
            audio = MP4(str(file_path))
            audio["\xa9ART"] = [artist]
            audio["\xa9nam"] = [title]
            if album:
                audio["\xa9alb"] = [album]
            if album_artist:
                audio["aART"] = [album_artist]
            if compilation:
                audio["cpil"] = True
            if year:
                audio["\xa9day"] = [year]
            if track_number:
                audio["trkn"] = [(track_number, track_total or 0)]
            # \xa9cmt is the comment atom
            if any(_is_source_branding(c) for c in audio.get("\xa9cmt", [])):
                audio["\xa9cmt"] = []
            # Freeform atoms for our source/quality markers (values are bytes)
            if source:
                audio["----:com.musicgrabber:SOURCE"] = [source.encode("utf-8")]
            if source_quality:
                audio["----:com.musicgrabber:SOURCE_QUALITY"] = [source_quality.encode("utf-8")]
            if file_id and not audio.get("----:com.musicgrabber:FILE_ID"):
                audio["----:com.musicgrabber:FILE_ID"] = [file_id.encode("utf-8")]
            if source_codec:
                audio["----:com.musicgrabber:SOURCE_CODEC"] = [source_codec.encode("utf-8")]
            if source_bitrate_kbps is not None:
                audio["----:com.musicgrabber:SOURCE_BITRATE"] = [str(source_bitrate_kbps).encode("utf-8")]
            if has_art:
                fmt = MP4Cover.FORMAT_PNG if art_mime == "image/png" else MP4Cover.FORMAT_JPEG
                audio["covr"] = [MP4Cover(album_art_bytes, imageformat=fmt)]
            audio.save()

        elif suffix in ['.ogg', '.opus']:
            from mutagen.oggopus import OggOpus
            from mutagen.oggvorbis import OggVorbis
            from mutagen.flac import Picture
            try:
                if suffix == '.opus':
                    audio = OggOpus(str(file_path))
                else:
                    audio = OggVorbis(str(file_path))
                audio["ARTIST"] = artist
                audio["TITLE"] = title
                if album:
                    audio["ALBUM"] = album
                if album_artist:
                    audio["ALBUMARTIST"] = album_artist
                    audio["ALBUM ARTIST"] = album_artist
                if compilation:
                    audio["COMPILATION"] = "1"
                if year:
                    audio["DATE"] = year
                if track_number:
                    audio["TRACKNUMBER"] = str(track_number)
                    if track_total:
                        audio["TRACKTOTAL"] = str(track_total)
                        audio["TOTALTRACKS"] = str(track_total)
                if any(_is_source_branding(c) for c in audio.get("COMMENT", [])):
                    audio["COMMENT"] = []
                if source:
                    audio["SOURCE"] = source
                if source_quality:
                    audio["SOURCE_QUALITY"] = source_quality
                if file_id and not audio.get("MUSICGRABBER_FILE_ID"):
                    audio["MUSICGRABBER_FILE_ID"] = file_id
                if source_codec:
                    audio["SOURCE_CODEC"] = source_codec
                if source_bitrate_kbps is not None:
                    audio["SOURCE_BITRATE"] = str(source_bitrate_kbps)
                if has_art:
                    pic = Picture()
                    pic.type = 3  # front cover
                    pic.mime = art_mime
                    pic.data = album_art_bytes
                    audio["METADATA_BLOCK_PICTURE"] = [base64.b64encode(pic.write()).decode("ascii")]
                audio.save()
            except Exception:
                pass  # Some ogg variants may not be supported

        # For .webm and other unsupported formats, skip metadata (yt-dlp handles it)

    except Exception:
        # If metadata application fails, continue anyway
        pass


def set_playlist_comment(file_path: Path, playlist_names: list[str]) -> bool:
    """Write playlist name(s) into the audio COMMENT tag, and nothing else.

    macOS Music has no notion of our M3U files, but it can build smart playlists
    that match on the Comments field, so stuffing the playlist name(s) there lets
    Mac folk recreate the playlist natively. A track on several playlists gets the
    lot, joined with ' | '.

    Deliberately comment-only: it never touches artist/title/album, so it is safe to
    call retroactively over an already-tagged library without clobbering anything
    AcoustID/MusicBrainz worked out.

    Idempotent: if the COMMENT already matches, it returns without rewriting the file,
    so it is cheap to call on every M3U rebuild. Returns True only when it wrote.
    """
    names = sorted({(n or "").strip() for n in (playlist_names or []) if (n or "").strip()})
    if not names:
        return False
    comment = " | ".join(names)
    try:
        suffix = file_path.suffix.lower()
        if suffix == '.flac':
            audio = FLAC(str(file_path))
            if audio.get("COMMENT") == [comment]:
                return False
            audio["COMMENT"] = [comment]
            audio.save()
        elif suffix == '.mp3':
            # A proper ID3v2.3 COMM frame is what modern macOS Music reads as "Comments";
            # we also refresh the legacy ID3v1 trailer (v1=2) as belt-and-braces for older
            # setups. Drop any existing COMM (incl. yt-dlp's source-URL one) first.
            from mutagen.id3 import ID3, COMM, ID3NoHeaderError
            try:
                tags = ID3(str(file_path))
            except ID3NoHeaderError:
                tags = ID3()
            existing = tags.getall("COMM")
            if len(existing) == 1 and existing[0].desc == "" and existing[0].text == [comment]:
                return False
            tags.delall("COMM")
            tags.add(COMM(encoding=3, lang="eng", desc="", text=[comment]))
            # v1=2 writes/refreshes the ID3v1 tag (replacing any stale one) alongside v2.3.
            tags.save(str(file_path), v2_version=3, v1=2)
        elif suffix in ('.m4a', '.mp4'):
            from mutagen.mp4 import MP4
            audio = MP4(str(file_path))
            if audio.get("\xa9cmt") == [comment]:
                return False
            audio["\xa9cmt"] = [comment]
            audio.save()
        elif suffix in ('.ogg', '.opus'):
            from mutagen.oggopus import OggOpus
            from mutagen.oggvorbis import OggVorbis
            audio = OggOpus(str(file_path)) if suffix == '.opus' else OggVorbis(str(file_path))
            if audio.get("COMMENT") == [comment]:
                return False
            audio["COMMENT"] = [comment]
            audio.save()
        else:
            return False  # webm and friends: yt-dlp owns those tags, leave them be
        return True
    except Exception as e:
        print(f"set_playlist_comment failed for {file_path}: {e}")
        return False


def search_artist_mbid(name: str) -> list[dict]:
    """Search MusicBrainz for an artist by name.

    Returns up to MB_ARTIST_SEARCH_LIMIT candidates ordered by match score,
    each as {mbid, name, disambiguation, score}. Empty list when MB returned
    a valid empty result; raises MusicBrainzUnavailable when MB is unreachable
    so the API layer can tell the user to retry instead of pretending the
    artist does not exist.
    """
    headers = {"User-Agent": f"MusicGrabber/{VERSION} (https://gitlab.com/g33kphr33k/musicgrabber)"}
    params = {
        "query": name,
        "limit": MB_ARTIST_SEARCH_LIMIT,
        "fmt": "json",
    }
    response = _mb_get_with_retry(
        "https://musicbrainz.org/ws/2/artist",
        params=params, headers=headers, timeout=TIMEOUT_MUSICBRAINZ_ARTIST,
    )
    if response.status_code != 200:
        # 4xx (bad query etc) -- definitive, not a network problem.
        return []
    artists = response.json().get("artists", [])
    results = []
    for a in artists:
        results.append({
            "mbid": a.get("id", ""),
            "name": a.get("name", ""),
            "disambiguation": a.get("disambiguation", ""),
            "score": int(a.get("score", 0)),
        })
    # Artists whose name actually matches come first, then everything else.
    # Within each group MusicBrainz's own relevance score leads, with exact
    # capitalisation as the tiebreak; that still keeps "SiR" above "Sir" when
    # the two score alike, without letting case pedantry decide the whole
    # ordering. It used to: searching "Raye" put three obscure 85-scoring
    # artists literally called "Raye" above RAYE the English singer, who
    # scores 100 but spells herself in capitals. Being upstaged by an
    # unnamed feature credit on a Dead Prez record is no way to be found.
    name_lower = name.lower()
    results.sort(key=lambda r: (
        0 if r["name"].lower() == name_lower else 1,
        -r["score"],
        0 if r["name"] == name else 1,
    ))
    return results


def fetch_artist_singles(mbid: str) -> list[dict]:
    """Fetch all singles for an artist from MusicBrainz.

    Returns a flat list of track dicts: {title, artist, release_date, release_mbid}.
    Singles with multiple tracks (A-side + B-side) are each returned as separate rows.
    Release date may be an empty string if MusicBrainz doesn't know it yet.
    Paginates automatically; sleeps 1 second between pages to respect rate limits.
    """
    import time as _time
    headers = {"User-Agent": f"MusicGrabber/{VERSION} (https://gitlab.com/g33kphr33k/musicgrabber)"}
    tracks: list[dict] = []
    offset = 0
    limit = 100
    total = None

    # Secondary types that disqualify a release from being a plain single.
    # MusicBrainz uses these on the release-group to tag remixes, live cuts,
    # compilations, soundtracks and the like.
    _EXCLUDED_SECONDARY_TYPES = {
        "Remix", "Live", "Compilation", "Soundtrack", "Interview",
        "Spokenword", "Audiobook", "Audio drama", "DJ-mix", "Mixtape/Street",
    }

    try:
        while True:
            params = {
                "artist": mbid,
                "type": "single",
                "limit": limit,
                "offset": offset,
                "inc": "recordings artist-credits release-groups",
                "fmt": "json",
            }
            with httpx.Client(timeout=TIMEOUT_MUSICBRAINZ_ARTIST) as client:
                response = client.get("https://musicbrainz.org/ws/2/release", params=params, headers=headers)
            if response.status_code != 200:
                print(f"MusicBrainz singles fetch failed for {mbid}: HTTP {response.status_code}")
                break
            data = response.json()
            if total is None:
                total = data.get("release-count", 0)
            releases = data.get("releases", [])
            if not releases:
                break

            for release in releases:
                # Skip anything with a disqualifying secondary type
                rg = release.get("release-group") or {}
                secondary_types = rg.get("secondary-types") or []
                if any(t in _EXCLUDED_SECONDARY_TYPES for t in secondary_types):
                    continue

                release_mbid = release.get("id", "")
                release_date = release.get("date") or release.get("first-release-date") or ""
                # Flatten all recordings on the release to individual track rows
                for medium in release.get("media", []):
                    for track in medium.get("tracks", []):
                        recording = track.get("recording", {})
                        rec_title = recording.get("title") or track.get("title") or release.get("title", "")
                        # Prefer the credited artist on the recording; fall back to release artist
                        artist_credits = (
                            recording.get("artist-credit")
                            or release.get("artist-credit")
                            or []
                        )
                        artist_name = "".join(
                            (ac.get("name") or ac.get("artist", {}).get("name", ""))
                            + (ac.get("joinphrase") or "")
                            for ac in artist_credits
                            if isinstance(ac, dict)
                        ).strip() or ""
                        if rec_title:
                            tracks.append({
                                "title": rec_title,
                                "artist": artist_name,
                                "release_date": release_date,
                                "release_mbid": release_mbid,
                            })
            offset += len(releases)
            if offset >= total:
                break
            _time.sleep(1)  # MusicBrainz rate limit: 1 req/sec
    except Exception as e:
        print(f"MusicBrainz singles fetch error for {mbid}: {e}")

    return tracks


def fetch_artist_albums(mbid: str) -> list[dict]:
    """Fetch studio albums and EPs for an artist from MusicBrainz.

    Returns [{title, year, release_mbid, release_group_mbid, primary_type}, ...]
    sorted by year ascending.
    Filters out compilations, live albums, soundtracks and other non-studio releases.

    EPs count. Ask MusicBrainz for `type=album` alone and Knife Party's entire
    discography comes back as one record, because Haunted House, Rage Valley and
    the rest are all typed EP; whole genres would look like they had never
    released anything. Singles stay out, or a prolific artist's list becomes a
    hundred one-track entries you have to scroll past.

    Paginates automatically; sleeps 1 second between pages to respect rate limits.
    Raises MusicBrainzUnavailable when MB is unreachable on the very first page
    (so the UI can show a retry prompt). If MB dies partway through pagination
    we keep whatever we already collected -- a partial list beats nothing.
    """
    import time as _time
    headers = {"User-Agent": f"MusicGrabber/{VERSION} (https://gitlab.com/g33kphr33k/musicgrabber)"}
    albums: list[dict] = []
    offset = 0
    limit = 100
    total = None
    releases_by_group: dict[str, list[dict]] = {}
    group_metadata: dict[str, dict] = {}

    _EXCLUDED_SECONDARY_TYPES = {
        "Compilation", "Live", "Remix", "Soundtrack", "Interview",
        "Spokenword", "Audiobook", "Audio drama", "DJ-mix", "Mixtape/Street",
        "Demo",
    }

    while True:
        params = {
            "artist": mbid,
            "type": "album|ep",
            "limit": limit,
            "offset": offset,
            # Media counts let the representative-release policy distinguish a
            # complete two-disc issue from a one-disc fragment of the same
            # release group. Status is part of the base release payload.
            "inc": "release-groups media",
            "fmt": "json",
        }
        try:
            response = _mb_get_with_retry(
                "https://musicbrainz.org/ws/2/release",
                params=params, headers=headers, timeout=TIMEOUT_MUSICBRAINZ_ARTIST,
            )
        except MusicBrainzUnavailable:
            if offset == 0:
                # Nothing collected yet -- bubble up so the UI prompts a retry.
                raise
            # Partial data is better than none; stop here and return what we have.
            print(f"MusicBrainz albums fetch for {mbid} stopped after partial pagination (offset={offset})")
            break
        if response.status_code != 200:
            print(f"MusicBrainz albums fetch failed for {mbid}: HTTP {response.status_code}")
            break
        try:
            data = response.json()
        except Exception as e:
            print(f"MusicBrainz albums fetch parse error for {mbid}: {e}")
            break
        if total is None:
            total = data.get("release-count", 0)
        releases = data.get("releases", [])
        if not releases:
            break

        for release in releases:
            rg = release.get("release-group") or {}
            rg_id = rg.get("id", "")
            secondary_types = rg.get("secondary-types") or []
            if any(t in _EXCLUDED_SECONDARY_TYPES for t in secondary_types):
                continue
            # Collect the entire group across every page before choosing. Taking
            # the first row here silently selected partial releases whenever an
            # early one-disc issue happened to precede the complete edition.
            identity = rg_id or f"release:{release.get('id', '')}"
            releases_by_group.setdefault(identity, []).append(release)
            group_metadata[identity] = rg

        offset += len(releases)
        if offset >= total:
            break
        _time.sleep(1)  # MusicBrainz rate limit: 1 req/sec

    for identity, releases in releases_by_group.items():
        release = _pick_release_from_group(releases)
        if not release:
            continue
        rg = group_metadata.get(identity) or {}
        rg_id = rg.get("id", "")
        # Release-group title is canonical and avoids pressing/disc decoration.
        title = (rg.get("title") or release.get("title") or "").strip()
        date = release.get("date") or release.get("first-release-date") or ""
        if title:
            albums.append({
                "title": title,
                "year": date[:4] if date else "",
                "release_mbid": release.get("id", ""),
                # The release-group is the album's stable identity. Anything
                # remembering "have I seen this album before?" wants this one,
                # not whichever representative pressing currently wins.
                "release_group_mbid": rg_id,
                "primary_type": rg.get("primary-type") or "",
            })

    albums.sort(key=lambda a: a["year"] or "9999")
    return albums


def fetch_album_release(release_mbid: str) -> dict:
    """Fetch a release from MusicBrainz as {"year": "1973", "tracks": [...]}.

    Same single HTTP call as `fetch_album_tracks`, which is the point: the
    release date is sitting right there in the response we already ask for, so
    there is no excuse for a second round trip just to learn what year it is.
    `year` is the date of *this* pressing, so a 2015 remaster says 2015, and is
    "" when MusicBrainz is coy about it.

    Raises MusicBrainzUnavailable when MB is unreachable after retries.
    """
    headers = {"User-Agent": f"MusicGrabber/{VERSION} (https://gitlab.com/g33kphr33k/musicgrabber)"}
    params = {"inc": "recordings+isrcs+artist-credits", "fmt": "json"}
    response = _mb_get_with_retry(
        f"https://musicbrainz.org/ws/2/release/{release_mbid}",
        params=params, headers=headers, timeout=TIMEOUT_MUSICBRAINZ_ARTIST,
    )
    if response.status_code != 200:
        print(f"MusicBrainz tracklist fetch failed for {release_mbid}: HTTP {response.status_code}")
        return {"year": "", "tracks": []}
    try:
        data = response.json()
    except Exception as e:
        print(f"MusicBrainz tracklist parse error for {release_mbid}: {e}")
        return {"year": "", "tracks": []}
    # MB dates come as "1973", "1973-05" or "1973-05-25"; we only ever want the front.
    year_match = re.match(r"(\d{4})", data.get("date") or "")
    year = year_match.group(1) if year_match else ""
    release_credit = data.get("artist-credit") or []
    tracks: list[dict] = []
    for medium in data.get("media", []):
        for track in medium.get("tracks", []):
            recording = track.get("recording") or {}
            title = recording.get("title") or track.get("title", "")
            position = str(track.get("position") or track.get("number") or "")
            # Track-level credit first: it is the one specific to this pressing,
            # and it is what differs on a compilation. Recording, then release,
            # pick up the slack.
            credits = (
                track.get("artist-credit")
                or recording.get("artist-credit")
                or release_credit
                or []
            )
            artist = "".join(
                (ac.get("name") or ac.get("artist", {}).get("name", ""))
                + (ac.get("joinphrase") or "")
                for ac in credits
                if isinstance(ac, dict)
            ).strip()
            # MusicBrainz hands back a list of ISRCs per recording; take the first
            # populated one. Most studio recordings have exactly one.
            isrc = next((code.strip() for code in (recording.get("isrcs") or []) if (code or "").strip()), None)
            if title:
                tracks.append({
                    "position": position,
                    "title": title,
                    "artist": artist,
                    "isrc": isrc,
                    "recording_mbid": recording.get("id") or None,
                })
    return {"year": year, "tracks": tracks}


def fetch_album_tracks(release_mbid: str) -> list[dict]:
    """Fetch the tracklist for a specific release from MusicBrainz.

    Returns [{position, title, artist, isrc, recording_mbid}, ...] in track order.
    Position is a string (e.g. "1", "A1") as MusicBrainz provides it.
    The ISRC pins the exact studio recording so the album download path can grab
    that specific cut rather than a live take; recording_mbid is for the
    post-download fingerprint check. Both are None when MusicBrainz has nothing.

    `artist` is the performer credited on that track, which on a normal album is
    just the album artist repeated, and on a compilation is the entire point.
    Without it every track on a soundtrack goes looking for "Various Artists -
    Whatever", which is not a recording anyone has ever released, and album
    mode's strict artist gate then rejects the real one when it turns up.
    Empty when MusicBrainz declines to say; callers fall back to the release
    credit.

    Raises MusicBrainzUnavailable when MB is unreachable after retries.
    """
    return fetch_album_release(release_mbid)["tracks"]


# ---------------------------------------------------------------------------
# MusicBrainz release URLs
#
# The cheapest possible album input: the URL already contains the MBID, so there
# is no scraping, no fuzzy matching and no guessing which "Greatest Hits" was
# meant. Paste a link, get that exact release.
# ---------------------------------------------------------------------------

_MB_URL_RE = re.compile(
    r"""^https?://
        (?:beta\.)?
        (?:musicbrainz\.org|mbrainz\.org)
        /(release-group|release)
        /([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})
    """,
    re.IGNORECASE | re.VERBOSE,
)


def parse_musicbrainz_release_url(url: str) -> tuple[str, str] | None:
    """Return ("release"|"release-group", mbid) for a MusicBrainz album URL.

    None for anything else, including recording and artist URLs, which are not
    albums however much they might wish to be.
    """
    match = _MB_URL_RE.match((url or "").strip())
    if not match:
        return None
    return match.group(1).lower(), match.group(2).lower()


def _pick_release_from_group(releases: list[dict]) -> dict | None:
    """Choose one release to represent a release group.

    A popular album can have two dozen pressings. Prefer an official release with
    the largest complete tracklist/media set, then the earliest pressing. This
    keeps a two-disc compilation from collapsing into an early one-disc fragment.
    Country is deliberately ignored; picking a favourite nation is a good way to
    start an argument and a bad way to choose a tracklist.
    """
    def _track_count(release: dict) -> int:
        explicit = release.get("track-count") or release.get("track_count")
        try:
            explicit_count = int(explicit or 0)
        except (TypeError, ValueError):
            explicit_count = 0
        media_count = 0
        for medium in release.get("media") or []:
            try:
                media_count += int(
                    medium.get("track-count")
                    or medium.get("track_count")
                    or len(medium.get("tracks") or [])
                    or 0
                )
            except (TypeError, ValueError):
                continue
        return max(explicit_count, media_count)

    def _score(release: dict) -> tuple:
        official = (release.get("status") or "").lower() == "official"
        # A bare "1998" sorts before "1998-10-12" as a string, which would hand
        # the prize to the vaguest entry in the group. Pad imprecise dates to the
        # end of their year so a properly dated early pressing wins instead.
        date = (release.get("date") or "").strip()
        if len(date) == 4:
            date += "-12-31"
        elif len(date) == 7:
            date += "-31"
        elif not date:
            date = "9999-12-31"
        media_count = len(release.get("media") or [])
        return (0 if official else 1, -_track_count(release), -media_count, date)

    candidates = [r for r in releases if r.get("id")]
    return min(candidates, key=_score) if candidates else None


def _release_track_count(release: dict) -> int:
    """Total tracks across every medium in a full MusicBrainz release."""
    total = 0
    for medium in release.get("media") or []:
        try:
            total += int(
                medium.get("track-count")
                or medium.get("track_count")
                or len(medium.get("tracks") or [])
                or 0
            )
        except (TypeError, ValueError):
            continue
    if total:
        return total
    try:
        return int(release.get("track-count") or release.get("track_count") or 0)
    except (TypeError, ValueError):
        return 0


def _browse_group_releases(group_mbid: str, headers: dict) -> list[dict]:
    """Fetch release candidates with media counts in one MusicBrainz request."""
    try:
        response = _mb_get_with_retry(
            "https://musicbrainz.org/ws/2/release/",
            params={
                "release-group": group_mbid,
                "inc": "media artist-credits release-groups",
                "limit": 100,
                "fmt": "json",
            },
            headers=headers,
            timeout=TIMEOUT_MUSICBRAINZ_ARTIST,
        )
        if response.status_code == 200:
            return response.json().get("releases") or []
    except Exception as exc:
        # The group lookup already supplied basic releases, so a browse wobble
        # is allowed to fall back to those rather than losing the whole album.
        print(f"MusicBrainz release completeness lookup failed for {group_mbid}: {exc}")
    return []


def fetch_release_summary(kind: str, mbid: str) -> dict | None:
    """Resolve a MusicBrainz release or release-group MBID to album basics.

    Returns {artist, album_title, release_mbid, year, track_count} ready to hand
    to the album download endpoint, or None if MusicBrainz has never heard of it.
    Raises MusicBrainzUnavailable when MusicBrainz is unreachable after retries.
    """
    if not get_setting_bool("enable_musicbrainz", True):
        return None
    headers = {"User-Agent": f"MusicGrabber/{VERSION} (https://gitlab.com/g33kphr33k/musicgrabber)"}

    group = None
    requested_group_mbid = mbid if kind == "release-group" else None
    if kind == "release-group":
        response = _mb_get_with_retry(
            f"https://musicbrainz.org/ws/2/release-group/{mbid}",
            params={"inc": "releases artist-credits", "fmt": "json"},
            headers=headers, timeout=TIMEOUT_MUSICBRAINZ_ARTIST,
        )
        if response.status_code != 200:
            return None
        group = response.json()
        detailed_releases = _browse_group_releases(mbid, headers)
        release = _pick_release_from_group(
            detailed_releases or group.get("releases") or []
        )
        if not release:
            return None
        mbid = release["id"]
        # Fall through and look the chosen release up properly, so the title and
        # credit come from the pressing we are actually going to download.

    # `media` is what carries the per-disc track counts; without it the caller
    # gets a confident "0 tracks", which is not the sort of confidence anyone needs.
    response = _mb_get_with_retry(
        f"https://musicbrainz.org/ws/2/release/{mbid}",
        params={"inc": "artist-credits release-groups media", "fmt": "json"},
        headers=headers, timeout=TIMEOUT_MUSICBRAINZ_ARTIST,
    )
    if response.status_code != 200:
        return None
    data = response.json()

    # A direct release URL can point at a one-disc fragment whose release-group
    # represents the complete record. Resolve the group and switch only when a
    # demonstrably more complete official release exists. If completeness data
    # is unavailable, preserve the exact release the user pasted.
    embedded_group = data.get("release-group") or {}
    group_mbid = requested_group_mbid or embedded_group.get("id")
    if group is None and group_mbid:
        try:
            group_response = _mb_get_with_retry(
                f"https://musicbrainz.org/ws/2/release-group/{group_mbid}",
                params={"inc": "releases artist-credits", "fmt": "json"},
                headers=headers,
                timeout=TIMEOUT_MUSICBRAINZ_ARTIST,
            )
            if group_response.status_code == 200:
                group = group_response.json()
        except Exception as exc:
            print(f"MusicBrainz release-group context lookup failed for {group_mbid}: {exc}")

        candidates = _browse_group_releases(group_mbid, headers)
        best = _pick_release_from_group(candidates)
        if (
            best
            and best.get("id") != data.get("id")
            and _release_track_count(best) > _release_track_count(data)
        ):
            better_response = _mb_get_with_retry(
                f"https://musicbrainz.org/ws/2/release/{best['id']}",
                params={"inc": "artist-credits release-groups media", "fmt": "json"},
                headers=headers,
                timeout=TIMEOUT_MUSICBRAINZ_ARTIST,
            )
            if better_response.status_code == 200:
                data = better_response.json()

    # Join with "" not " ": MusicBrainz puts the separator in each credit's
    # joinphrase (" & ", " feat. "), so adding our own gives "Underworld &  Iggy Pop".
    release_artist = "".join(
        (ac.get("name") or ac.get("artist", {}).get("name", "")) + (ac.get("joinphrase") or "")
        for ac in (data.get("artist-credit") or [])
        if isinstance(ac, dict)
    ).strip()
    group_artist = _join_artist_credit((group or {}).get("artist-credit") or [])
    artist = group_artist or release_artist
    # The release-group title is canonical. It removes pressing decoration such
    # as "(disc 1)" only when MusicBrainz itself says the undecorated group title
    # is the record's identity; legitimate titles containing "disc" survive.
    album_title = ((group or {}).get("title") or data.get("title") or "").strip()
    if not artist or not album_title:
        return None

    year = ""
    year_match = re.match(r"(\d{4})", data.get("date") or "")
    if year_match:
        year = year_match.group(1)

    # Multi-disc releases report a count per medium; the album has the lot.
    track_count = _release_track_count(data)

    return {
        "artist": artist,
        "album_title": album_title,
        "release_mbid": data.get("id") or mbid,
        "year": year,
        "track_count": track_count,
    }


# ---------------------------------------------------------------------------
# Album-name search and fuzzy album matching
#
# Bulk Import's job is to accept "whatever the user pasted or typed", which
# includes an album name with no artist attached. Trainspotting, Now That's
# What I Call Music 42, the Guardians of the Galaxy soundtrack: all perfectly
# reasonable things to type, none of them owned by a single artist. This
# search deliberately does NOT apply the studio-album filtering that
# fetch_artist_albums uses; that filter exists to keep an artist's own
# discography free of noise, which is the opposite of what a browse surface
# for compilations and soundtracks needs.
# ---------------------------------------------------------------------------

def _join_artist_credit(credits: list) -> str:
    """Join a MusicBrainz artist-credit array into a display string.

    MusicBrainz stores the separator inside each credit's own joinphrase
    (" & ", " feat. "), so we join with "" rather than a space; a plain space
    join gives "Underworld &  Iggy Pop", extra space and all.
    """
    return "".join(
        (ac.get("name") or ac.get("artist", {}).get("name", "")) + (ac.get("joinphrase") or "")
        for ac in (credits or [])
        if isinstance(ac, dict)
    ).strip()


def _resolve_representative_release(release_group_mbid: str, headers: dict) -> str | None:
    """Pick a representative official release MBID for a release-group.

    Same picking logic fetch_release_summary uses for a release-group MBID
    (_pick_release_from_group), reused rather than reinvented. Deliberately
    skips the second full-release lookup fetch_release_summary makes after
    that, since search_release_groups already has title/artist/year from the
    search hit itself and doesn't need to re-confirm them.
    """
    response = _mb_get_with_retry(
        f"https://musicbrainz.org/ws/2/release-group/{release_group_mbid}",
        params={"inc": "releases", "fmt": "json"},
        headers=headers, timeout=TIMEOUT_MUSICBRAINZ_ARTIST,
    )
    if response.status_code != 200:
        return None
    try:
        group = response.json()
    except Exception:
        return None
    detailed_releases = _browse_group_releases(release_group_mbid, headers)
    release = _pick_release_from_group(
        detailed_releases or group.get("releases") or []
    )
    return release.get("id") if release else None


def search_release_groups(
    query: str, artist: str | None = None, limit: int = 10, resolve_releases: bool = True
) -> list[dict]:
    """Search MusicBrainz release-groups by title, optionally narrowed by artist.

    This is a browse surface, not the artist-albums poller: compilations,
    soundtracks and Various Artists releases are NOT filtered out here on
    purpose, since a user typing "Trainspotting" or "Now That's What I Call
    Music 42" with no artist in mind is exactly who this is for. Secondary
    types come back verbatim so the caller can label what it's showing rather
    than pretending everything is a plain studio album.

    Each result: {title, artist, year, release_group_mbid, release_mbid,
    primary_type, secondary_types, score}. release_mbid is a representative
    official release for the group, resolved the same way fetch_release_summary
    resolves a release-group MBID (see _resolve_representative_release); it's
    None when MusicBrainz has no releases catalogued yet for that group, or
    when resolving it hit a wobble (a candidate missing its release_mbid still
    beats losing the whole search over one bad apple).

    Raises MusicBrainzUnavailable only if the initial search itself cannot
    reach MusicBrainz, so the caller can offer a Retry.

    resolve_releases controls how much work a search costs. Resolving a
    representative release means one extra MusicBrainz request per candidate,
    each courteously spaced a second apart, so a default search goes from one
    request to eleven and from instant to over ten seconds. A browse list does
    not need it: nobody needs the release MBID of the nine albums they are not
    going to pick. Pass False for interactive search and resolve the single
    chosen candidate afterwards with fetch_release_summary("release-group", id);
    candidates then come back with release_mbid set to None. It defaults True
    so existing callers keep the behaviour they were written against.
    """
    if not get_setting_bool("enable_musicbrainz", True):
        return []
    query = (query or "").strip()
    if not query:
        return []

    import time as _time

    headers = {"User-Agent": f"MusicGrabber/{VERSION} (https://gitlab.com/g33kphr33k/musicgrabber)"}
    lucene_query = f'releasegroup:"{query}"'
    if artist and artist.strip():
        lucene_query += f' AND artist:"{artist.strip()}"'

    response = _mb_get_with_retry(
        "https://musicbrainz.org/ws/2/release-group/",
        params={"query": lucene_query, "fmt": "json", "limit": limit},
        headers=headers, timeout=TIMEOUT_MUSICBRAINZ_ARTIST,
    )
    if response.status_code != 200:
        return []
    try:
        groups = response.json().get("release-groups") or []
    except Exception:
        return []

    results = []
    for rg in groups[:limit]:
        rg_id = rg.get("id")
        title = rg.get("title") or ""
        if not title or not rg_id:
            continue

        date = rg.get("first-release-date") or ""
        year = date[:4] if date[:4].isdigit() else ""

        release_mbid = None
        if resolve_releases:
            _time.sleep(1)  # MusicBrainz rate limit: 1 req/sec, courtesy between requests
            try:
                release_mbid = _resolve_representative_release(rg_id, headers)
            except MusicBrainzUnavailable:
                # One candidate's release resolution having a wobble shouldn't
                # sink the whole browse list.
                release_mbid = None

        results.append({
            "title": title,
            "artist": _join_artist_credit(rg.get("artist-credit") or []),
            "year": year,
            "release_group_mbid": rg_id,
            "release_mbid": release_mbid,
            "primary_type": rg.get("primary-type") or "",
            "secondary_types": rg.get("secondary-types") or [],
            "score": int(rg.get("score", 0)),
        })

    return results


# Confidence gate for treating a scraped artist/album pair as a solid
# MusicBrainz match. Below this, the caller MUST ask the user to confirm
# rather than guessing; a wrong album match downloads an entire wrong album's
# worth of tracks, a much bigger mess than a wrong single track, so the bar
# sits well above the sort of confidence a single-track match would settle
# for. Picked by feel rather than measurement; move to constants.py if it
# ever needs tuning against real-world data.
ALBUM_MATCH_CONFIDENCE_THRESHOLD = 0.75


def _album_artist_score(expected_artist: str, candidate_artist: str) -> float:
    """Artist half of an album match score.

    Reuses matching.py's normalisation and similarity(), but deliberately does
    NOT apply its junk-artist gate: "Various Artists" is a legitimate, correct
    MusicBrainz credit for compilations and soundtracks, not noise to zero out.
    Track matching zeroes it because nobody wants a single track mistagged
    "Various Artists"; album matching has the opposite problem, since that IS
    the right credit for a chunk of what this function exists to find. A VA
    candidate is scored as a decent-but-not-perfect match instead, so the
    album title carries the real weight of the decision.
    """
    if is_junk_artist(candidate_artist):
        return 0.6
    if not expected_artist:
        return 0.5  # Nothing supplied to compare against.
    return similarity(clean_artist(expected_artist), clean_artist(candidate_artist or ""))


def match_album_to_musicbrainz(artist: str, album: str) -> dict:
    """Fuzzy-match a scraped artist/album pair to a MusicBrainz release.

    Runs search_release_groups(album, artist=artist) and scores each result
    against the input using matching.py's existing similarity()/clean_title()/
    clean_artist() primitives (the same fuzzy stack the rest of MusicGrabber
    already trusts for tracks), rather than a new one invented for albums.

    Returns:
        {
            "match": the best candidate (search_release_groups shape) or None,
            "confidence": float 0.0-1.0,
            "confident": bool,  # True only once confidence clears ALBUM_MATCH_CONFIDENCE_THRESHOLD
            "candidates": up to 5 candidates, best first, for the UI to offer
                          when not confident,
        }

    Raises MusicBrainzUnavailable when MusicBrainz cannot be reached at all
    (propagated straight from search_release_groups).
    """
    # Scoring only looks at title and artist, both of which the search hit
    # already carries, so there is no sense resolving a release for ten
    # candidates when nine of them are about to lose. The winner gets its
    # release resolved below.
    candidates = search_release_groups(album, artist=artist, limit=10, resolve_releases=False)
    if not candidates:
        return {"match": None, "confidence": 0.0, "confident": False, "candidates": []}

    scored = []
    for cand in candidates:
        title_score = similarity(clean_title(album or ""), clean_title(cand.get("title") or ""))
        artist_score = _album_artist_score(artist or "", cand.get("artist") or "")
        # Title carries most of the weight, same balance compute_match_confidence
        # strikes for tracks: the title is what the user actually typed/scraped.
        confidence = title_score * 0.65 + artist_score * 0.35
        scored.append((confidence, cand))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_confidence, best_candidate = scored[0]

    # Resolve a real release for the winner only; the album pipeline needs a
    # release MBID, and one request beats ten. A wobble here leaves it None,
    # which the caller must treat as "cannot queue this yet" rather than a
    # match failure.
    if best_candidate.get("release_group_mbid") and not best_candidate.get("release_mbid"):
        headers = {"User-Agent": f"MusicGrabber/{VERSION} (https://gitlab.com/g33kphr33k/musicgrabber)"}
        try:
            best_candidate["release_mbid"] = _resolve_representative_release(
                best_candidate["release_group_mbid"], headers
            )
        except MusicBrainzUnavailable:
            best_candidate["release_mbid"] = None

    return {
        "match": best_candidate,
        "confidence": round(best_confidence, 3),
        "confident": best_confidence >= ALBUM_MATCH_CONFIDENCE_THRESHOLD,
        "candidates": [cand for _, cand in scored[:5]],
    }
