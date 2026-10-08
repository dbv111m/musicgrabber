"""
MusicGrabber - Soulseek/slskd Integration

Authentication, search, download, and quality parsing.
"""

import errno
import os
import re
import shutil
import time
import hashlib
from pathlib import Path
from typing import Optional

import httpx

from constants import (
    TIMEOUT_HTTP_REQUEST, TIMEOUT_SLSKD_API, TIMEOUT_SLSKD_SEARCH, TIMEOUT_SLSKD_ALBUM_SEARCH,
    TIMEOUT_SLSKD_DOWNLOAD, SLSKD_MAX_RESULTS, SLSKD_MIN_QUALITY_SCORE,
    SLSKD_REQUIRE_FREE_SLOT, SLSKD_MATCH_CONFIDENCE_FLOOR,
    SLSKD_HASH_CHUNK_BYTES, SLSKD_PRUNE_MAX_DEPTH, SLSKD_ARTIST_MATCH_FLOOR,
)
from settings import get_setting, get_setting_bool
from matching import (
    score_track_against_filename, parse_query,
    normalise_string, is_junk_artist, similarity, split_path_segments,
    core_string, clean_title, query_requests_variant,
)


# slskd auth token cache, keyed by (url, user) so different users
# with different slskd instances get their own tokens
_slskd_token_cache: dict[tuple[str, str], tuple[str, float]] = {}

SLSKD_SOURCE_TRUST_BONUS = 35
SLSKD_LOSSLESS_BONUS = 25
SLSKD_HIRES_BONUS = 15
SLSKD_QUEUE_PENALTY_PER_ITEM = 2
SLSKD_QUEUE_PENALTY_CAP = 40


def slskd_enabled(user_id: str | None = None) -> bool:
    """Check if slskd integration is configured"""
    if not get_setting_bool("source_soulseek_enabled", False, user_id=user_id):
        return False
    url = get_setting("slskd_url", user_id=user_id)
    user = get_setting("slskd_user", user_id=user_id)
    password = get_setting("slskd_pass", user_id=user_id)
    return bool(url and user and password)


def get_slskd_token(user_id: str | None = None) -> Optional[str]:
    """Get a valid slskd auth token, refreshing if needed"""
    url = get_setting("slskd_url", user_id=user_id)
    user = get_setting("slskd_user", user_id=user_id)
    password = get_setting("slskd_pass", user_id=user_id)

    if not (url and user and password):
        return None

    cache_key = (url, user)
    cached = _slskd_token_cache.get(cache_key)
    if cached and time.time() < cached[1] - 60:
        return cached[0]

    try:
        with httpx.Client(timeout=TIMEOUT_HTTP_REQUEST) as client:
            response = client.post(
                f"{url}/api/v0/session",
                json={"username": user, "password": password}
            )
            if response.status_code == 200:
                data = response.json()
                token = data["token"]
                expires = data["expires"]
                _slskd_token_cache[cache_key] = (token, expires)
                return token
    except Exception as e:
        print(f"slskd auth failed: {e}")

    return None


def parse_slskd_quality(file_info: dict) -> tuple[str, int]:
    """
    Parse quality info from slskd file response.
    Returns (quality_label, sort_score) where higher score = better quality.
    """
    filename = file_info.get("filename", "").lower()
    bit_depth = file_info.get("bitDepth", 0)
    sample_rate = file_info.get("sampleRate", 0)
    bit_rate = file_info.get("bitRate", 0)

    # Determine format from filename extension
    if filename.endswith(".flac"):
        if bit_depth >= 24:
            return (f"FLAC {bit_depth}bit/{sample_rate//1000}kHz", 150)
        return ("FLAC", 100)
    elif filename.endswith(".wav"):
        return ("WAV", 95)
    elif filename.endswith(".mp3"):
        if bit_rate >= 320:
            return ("MP3 320", 80)
        elif bit_rate >= 256:
            return ("MP3 256", 70)
        elif bit_rate >= 192:
            return ("MP3 192", 60)
        else:
            return (f"MP3 {bit_rate}", 50)
    elif filename.endswith(".m4a") or filename.endswith(".aac"):
        if bit_rate >= 256:
            return ("AAC 256", 75)
        return (f"AAC {bit_rate}", 65)
    elif filename.endswith(".ogg") or filename.endswith(".opus"):
        return ("OGG/Opus", 70)
    else:
        return ("Unknown", 30)


def _display_bitrate(file_info: dict) -> Optional[int]:
    """Bitrate in kbps for the search card.

    slskd declares one for MP3s and keeps quiet for FLAC, so where it stays quiet
    we work it out from size and duration. That's the real average bitrate rather
    than a nominal one, which for lossless is the more honest number anyway.
    """
    declared = file_info.get("bitRate")
    if isinstance(declared, (int, float)) and declared > 0:
        return int(declared)

    size = file_info.get("size")
    length = file_info.get("length")
    if isinstance(size, (int, float)) and isinstance(length, (int, float)) and size > 0 and length > 0:
        return int(size * 8 / length / 1000)
    return None


def _peer_queue_length(response: dict) -> Optional[int]:
    """Return slskd's peer queue depth when it supplied a usable value."""
    value = response.get("queueLength")
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def normalize_slskd_path(path: str) -> str:
    """Normalise slskd paths for matching"""
    return path.replace("\\", "/").strip()


def get_slskd_local_path(download_info: dict) -> Optional[str]:
    """Try to pull a local file path from slskd download info"""
    for key in ("localPath", "localFilename", "downloadedFilePath", "downloadPath", "path", "fullPath"):
        value = download_info.get(key)
        if value:
            return value
    return None


def _completed_equivalent_paths(path: Path, download_roots: list[Path]) -> list[Path]:
    """Return likely completed-file paths for a transient incomplete path."""
    candidates = []
    parts = path.parts
    if "incomplete" not in parts:
        return candidates

    for index, part in enumerate(parts):
        if part != "incomplete":
            continue
        without_incomplete = Path(*parts[:index], *parts[index + 1:])
        candidates.append(without_incomplete)

        for root in download_roots:
            try:
                rel_after_incomplete = Path(*parts[index + 1:])
                candidates.append(root / rel_after_incomplete)
            except TypeError:
                continue
    return candidates


def _sha256_of(path: Path) -> str:
    """SHA-256 of a file, read in chunks so a 24-bit FLAC doesn't live in RAM."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(SLSKD_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_copy(source: Path, dest: Path) -> tuple[bool, str]:
    """Confirm a copy arrived intact. Size first (cheap), then SHA-256 (certain)."""
    try:
        source_size = source.stat().st_size
        dest_size = dest.stat().st_size
    except OSError as e:
        return False, f"could not stat both files ({e})"

    if source_size != dest_size:
        return False, f"size mismatch, {source_size} vs {dest_size} bytes"

    try:
        if _sha256_of(source) != _sha256_of(dest):
            return False, "SHA-256 mismatch"
    except OSError as e:
        return False, f"could not hash both files ({e})"

    return True, ""


def _inside_roots(path: Path, roots: list[Path]) -> bool:
    """True only if path sits strictly *inside* one of the download roots.

    The roots themselves come back False, which is the whole point: we are about
    to delete empty directories, and deleting somebody's downloads folder because
    it happened to be empty would be a memorable bug.
    """
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            if resolved.relative_to(root.resolve()).parts:
                return True
        except (OSError, ValueError):
            continue
    return False


def prune_empty_dirs(start: Path, roots: list[Path]) -> None:
    """Sweep up folders left empty by a move, walking upwards until something is in the way."""
    current = start
    for _ in range(SLSKD_PRUNE_MAX_DEPTH):
        if not _inside_roots(current, roots):
            return
        try:
            if any(current.iterdir()):
                return
            current.rmdir()
        except OSError:
            return
        print(f"slskd: Tidied away empty folder {current}")
        current = current.parent


class AmbiguousSlskdMatch(Exception):
    """Several files answer to the same name and size, so none of them get picked."""


def _pick_sized_match(root: Path, filename: str, expected_size: int) -> Path | None:
    """Find the one file under root called `filename` that is also the right size.

    The basename alone is a terrible identifier on Soulseek; half the network has
    an `01 - Intro.flac` knocking about. Matching on name alone used to hand back
    whichever one rglob tripped over first, and with slskd_move_completed on that
    unlucky stranger then got deleted from disk. So: the byte count has to agree
    with what the peer promised, and if two files still both fit we refuse rather
    than guess. A failed download is annoying; eating an unrelated one is worse.
    """
    matches = [
        path for path in root.rglob(filename)
        if path.is_file() and _size_or_none(path) == expected_size
    ]
    if not matches:
        return None
    if len(matches) > 1:
        listed = ", ".join(str(path) for path in sorted(matches)[:5])
        raise AmbiguousSlskdMatch(
            f"{len(matches)} files under {root} match '{filename}' at {expected_size} bytes "
            f"({listed}). Refusing to guess which one is yours."
        )
    return matches[0]


def _size_or_none(path: Path) -> int | None:
    """st_size, or None if the file evaporated mid-scan. Races happen."""
    try:
        return path.stat().st_size
    except OSError:
        return None


def deliver_slskd_file(source: Path, dest: Path, download_roots: list[Path] | None = None) -> Path:
    """Bring a completed slskd download into MusicGrabber's staging directory.

    Copies by default, leaving slskd's own copy alone to keep sharing back to
    the network. With ``slskd_move_completed`` on it moves instead:

    * same mount: a plain rename, which is atomic and instant. Nothing is copied,
      so there is nothing to verify and no source left to delete.
    * across mounts: copy, verify by size and SHA-256, then delete. Note that
      separate Docker bind mounts count as separate here even when they sit on
      one disk, because rename(2) says EXDEV and rename(2) gets the last word. If
      verification fails the source stays exactly where it was and the dubious
      copy is binned, so the retry has something to retry with.
    * source deleted but not deletable (read-only mount, permissions): the file
      is already safely delivered, so we grumble into the log and carry on
      rather than failing a download over housekeeping.
    """
    roots = download_roots or []

    if not get_setting_bool("slskd_move_completed", False):
        shutil.copy2(source, dest)
        print(f"slskd: Copied {source} to {dest}")
        return dest

    try:
        os.replace(source, dest)
        print(f"slskd: Moved {source} to {dest}")
        prune_empty_dirs(source.parent, roots)
        return dest
    except OSError as e:
        # EXDEV is the expected one (separate mounts); anything else still gets
        # the long way round, because copy2 will raise properly if it is fatal.
        if e.errno != errno.EXDEV:
            print(f"slskd: Rename refused ({e}), falling back to copy and verify")

    shutil.copy2(source, dest)
    verified, reason = verify_copy(source, dest)
    if not verified:
        dest.unlink(missing_ok=True)
        raise Exception(f"slskd move failed verification ({reason}); source left where it was")

    try:
        source.unlink()
        print(f"slskd: Moved {source} to {dest} (copied, verified by size and SHA-256, source removed)")
        prune_empty_dirs(source.parent, roots)
    except OSError as e:
        print(f"slskd: Copied {source} to {dest} but could not remove the source: {e}")

    return dest


def should_retry_slskd_error(error_message: str) -> bool:
    """Decide whether to retry based on slskd error text"""
    msg = error_message.lower()
    retry_markers = [
        "aborted",
        "rejected",
        "cancelled",
        "failed",
        "timed out",
        "timeout",
        "queued",
        "missing slskd file size",
    ]
    return any(marker in msg for marker in retry_markers)


def _normalise_transfer_state(state: object) -> str:
    """Return a stable text label for slskd/Soulseek transfer states."""
    if isinstance(state, str):
        return state
    if isinstance(state, int):
        completed = bool(state & 16)
        if completed and state & 32:
            return "Completed, Succeeded"
        if completed and state & 64:
            return "Completed, Cancelled"
        if completed and state & 128:
            return "Completed, TimedOut"
        if completed and state & 256:
            return "Completed, Errored"
        if completed and state & 512:
            return "Completed, Rejected"
        if completed and state & 1024:
            return "Completed, Aborted"
        if completed:
            return "Completed"
        if state & 2:
            return "Queued"
        if state & 4:
            return "Initializing"
        if state & 8:
            return "InProgress"
    return str(state or "")


# Folder names that describe how somebody organises their disk rather than who
# made the music. Soulseek is full of them: "music/FLAC/P/Paramore/..." is three
# segments of filing cabinet before you reach an actual artist.
_CONTAINER_SEGMENTS = frozenset({
    "music", "musique", "musik", "musica", "muzyka", "muziek", "my music", "media", "audio",
    "album", "albums", "single", "singles", "ep", "eps", "compilation", "compilations",
    "anthologies", "anthology", "discography", "discographie", "discographies",
    "collection", "collections", "library", "itunes", "itunes music", "archive", "archives",
    "shared", "share", "sharing", "shared music", "downloads", "download",
    "soulseek", "soulseek downloads", "complete", "incomplete", "sorted", "unsorted",
    "new", "misc", "other", "various", "stuff", "rips", "rip", "incoming", "assorted",
    "flac", "mp3", "wav", "ape", "aac", "ogg", "opus", "lossless", "lossy", "hi res", "hires",
    "cd", "cds", "vinyl", "web", "main", "instrumental", "root", "home", "public",
    "temp", "tmp", "upload", "uploads", "folder", "files", "torrents", "tracker", "music pack",
})

# Words that give a folder away as somebody's stash rather than a name, wherever
# they appear in it: "Foobar Rips", "FLAC Archive", "PMEDiA Music Pack 032 of 2023".
_CONTAINER_WORDS = frozenset({
    "rips", "pack", "packs", "discography", "discographie", "discographies",
    "downloads", "uploads", "collection", "collections", "tracker", "torrents",
    "bootlegs", "archive", "archives", "pirated", "pirate", "warez", "seedbox",
})

_DISC_FOLDER_RE = re.compile(r'^(cd|disc|disk|vol|volume)\s*\d*$')
_BARE_YEAR_RE = re.compile(r'^(19|20)\d{2}$')
_YEAR_RE = re.compile(r'(?<!\d)((?:19|20)\d{2})(?!\d)')
_FULL_DATE_RE = re.compile(
    r'(?<!\d)((?:19|20)\d{2})[-._](?:0[1-9]|1[0-2])[-._](?:0[1-9]|[12]\d|3[01])(?!\d)'
)
_AUDIO_EXT_RE = re.compile(r'\.(flac|mp3|m4a|aac|ogg|opus|wav|wma|ape|alac)$', re.IGNORECASE)
# Optional leading disc number, so "1-02 Ignorance" and "02.14 Ignorance" give up
# the track rather than handing back a title that still starts with a number.
_TRACK_PREFIX_RE = re.compile(r'^(?:\d{1,2}[\s.\-_]+)?(\d{1,3})[\s.\-_]+')
_ARTIST_PREFIX_RE = re.compile(r'^(.{2,}?)\s*[-–—]\s+(.+)$')
_CLOSERS = {")": "(", "]": "[", "}": "{"}


def _tidy_edges(text: str) -> str:
    """Trim leading and trailing rubbish, sparing brackets that still have a partner.

    A blunt strip turns "Brand New Eyes (deluxe version)" into "...(deluxe version"
    the moment anything else is removed from the front, which looks like a bug
    because it is one.
    """
    text = text.strip(" -–—_.,")
    openers = {value: key for key, value in _CLOSERS.items()}
    while text and text[0] in openers and text.count(text[0]) > text.count(openers[text[0]]):
        text = text[1:].strip(" -–—_.,")
    while text and text[-1] in _CLOSERS and text.count(text[-1]) > text.count(_CLOSERS[text[-1]]):
        text = text[:-1].strip(" -–—_.,")
    return text


def _plausible_name(text: str) -> bool:
    """Reject candidates with no letters in them: "09-22" is a date, not a band."""
    return sum(character.isalpha() for character in text) >= 2


def _underscores_to_spaces(text: str) -> str:
    """Some rippers use underscores where the rest of us use spaces.

    Only when there isn't a real space in sight, so "Blue_Monday (12 inch)" keeps
    whatever its owner meant by that.
    """
    return text.replace("_", " ") if "_" in text and " " not in text else text


# Trailing decoration people staple onto album folders. An album genuinely called
# "FLAC" would be a bold artistic choice and we are prepared to take that risk.
_FORMAT_TOKENS = frozenset({
    "flac", "mp3", "wav", "alac", "ape", "aac", "m4a", "ogg", "opus",
    "lossless", "16bit", "24bit",
})


def _strip_format_suffix(text: str) -> str:
    """Drop a trailing "- FLAC" or "[MP3]" from an album name."""
    for _ in range(3):
        match = re.search(r'[\s\-–—]+[\[\(]?([A-Za-z0-9]+)[\]\)]?\s*$', text)
        if not match or normalise_string(match.group(1)) not in _FORMAT_TOKENS:
            return text
        remainder = text[:match.start()].strip()
        if not remainder:
            return text
        text = remainder
    return text


_TITLE_FORMAT_SUFFIX_RE = re.compile(
    r'''\s*\[(?:
        flac|mp3|wav|alac|ape|aac|m4a|ogg|opus|lossless|
        \d{2,4}\s*kbps|
        (?:16|24|32)\s*[-/]\s*(?:44(?:\.1)?|48|88(?:\.2)?|96|176(?:\.4)?|192)
    )\]\s*$''',
    re.IGNORECASE | re.VERBOSE,
)


def _strip_title_format_suffix(text: str) -> str:
    """Remove unmistakable format labels while preserving actual title text."""
    for _ in range(3):
        cleaned = _TITLE_FORMAT_SUFFIX_RE.sub("", text).rstrip()
        if cleaned == text or not cleaned:
            break
        text = cleaned
    return text


def _name_variants(name: str) -> list[str]:
    """Offer clean names hidden inside common Soulseek shelf labels."""
    variants = []

    # "Knife Party (2011-2019)" should match Knife Party, while a collaborative
    # shelf called "Pendulum (Knife Party)" should also be able to match the name
    # in brackets.  Keep the raw folder as the final fallback, not the first guess.
    bracketed = re.match(r'^(.+?)\s*[\(\[\{]([^\)\]\}]+)[\)\]\}]\s*$', name)
    if bracketed:
        for part in bracketed.groups():
            part = part.strip()
            if part and _plausible_name(part) and part not in variants:
                variants.append(part)

    for part in re.split(r'\s+[-–—]\s+', name):
        part = part.strip()
        if part and part not in variants:
            variants.append(part)
    if name not in variants:
        variants.append(name)
    return variants


def _looks_like_container(segment: str, *, allow_bare_year: bool = False) -> bool:
    """True when a folder name is filing, not a name worth showing anyone.

    ``allow_bare_year`` is for album folders, where "1989" is a hopeless artist
    but a perfectly good Taylor Swift record.
    """
    if not segment or segment.startswith("@@"):
        return True
    if _AUDIO_EXT_RE.search(segment):
        return True
    norm = normalise_string(segment)
    # Two characters or fewer covers the alphabetical buckets: "P", "A-C", "#".
    if len(norm) < 3 and not norm.isdigit():
        return True
    return bool(
        norm in _CONTAINER_SEGMENTS
        or _CONTAINER_WORDS & set(norm.split())
        or is_junk_artist(segment)
        or _DISC_FOLDER_RE.match(norm)
        or (not allow_bare_year and _BARE_YEAR_RE.match(norm))
    )


def _split_year(text: str) -> tuple[str, Optional[int]]:
    """Pull a release year out of a folder name and hand back what is left.

    Handles "(2009)", "[2009]", "2009 - Album" and the trailing "Album 2009".
    If removing it would leave nothing behind the year *is* the name, so Taylor
    Swift's "1989" and Dr Dre's "2001" keep their titles and lose their years.
    """
    # Consume an entire ISO-shaped date before considering a bare year. Removing
    # only "2013" from "2013-05-06" used to leave "05-06" masquerading as part
    # of the album title.
    match = _FULL_DATE_RE.search(text)
    pattern = _FULL_DATE_RE if match else _YEAR_RE
    match = match or _YEAR_RE.search(text)
    if not match:
        return text.strip(), None

    cleaned = pattern.sub("", text, count=1)
    cleaned = re.sub(r'[\[\(\{]\s*[\]\)\}]', ' ', cleaned)  # empty brackets left behind
    cleaned = _tidy_edges(re.sub(r'\s{2,}', ' ', cleaned))
    if not cleaned:
        return text.strip(), None
    return cleaned, int(match.group(1))


def _split_artist_prefix(text: str) -> tuple[Optional[str], str]:
    """Split an "Artist - Title" name, forgiving the stray dashes people leave in."""
    match = _ARTIST_PREFIX_RE.match(text)
    if not match:
        return None, text.strip()
    left = match.group(1).strip(" -–—_.")
    right = match.group(2).strip(" -–—_.")
    if not left or not right:
        return None, text.strip()
    return left, right


def parse_slskd_path(filepath: str, query_artist: str | None = None) -> dict:
    """Pull artist, album, year, track number and title out of a Soulseek path.

    Soulseek folder layouts are a free-for-all: some people file by artist, some
    by format, some by first letter, some by whatever their ripper called the
    folder that Tuesday. Position alone gets the artist right about one time in
    five, so when the search has told us who we are looking for, we go and look
    for them; position is only the fallback.
    """
    empty = {"artist": "", "album": "", "year": None, "track_number": None, "title": ""}
    segments = split_path_segments(filepath)
    if not segments:
        return empty

    folders, basename = segments[:-1], segments[-1]

    title = _underscores_to_spaces(_AUDIO_EXT_RE.sub("", basename) or basename)
    track_number = None
    track_match = _TRACK_PREFIX_RE.match(title)
    if track_match:
        track_number = int(track_match.group(1))
        title = title[track_match.end():]
    basename_artist, title = _split_artist_prefix(title.strip())
    title = _strip_title_format_suffix(title)

    # The album folder is the nearest parent that isn't a "CD2"-style subfolder.
    album_folder, album_index = "", None
    for index in range(len(folders) - 1, -1, -1):
        if _DISC_FOLDER_RE.match(normalise_string(folders[index])):
            continue
        album_folder, album_index = folders[index], index
        break

    album, year, album_artist = "", None, None
    if album_folder and not _looks_like_container(album_folder, allow_bare_year=True):
        album, year = _split_year(_underscores_to_spaces(album_folder))
        album_artist, album = _split_artist_prefix(album)
        album = _tidy_edges(_strip_format_suffix(album))
    else:
        album_index = None  # a container folder has no artist sitting above it

    # A file dropped straight into the artist's own folder has no album, whatever
    # the folder is called. Only when nothing sensible sits above it, mind: a
    # self-titled record under "Paramore/Paramore" is a real album.
    if album and query_artist and similarity(normalise_string(album), normalise_string(query_artist)) >= SLSKD_ARTIST_MATCH_FLOOR:
        parent_is_a_name = (
            album_index is not None and album_index > 0
            and not _looks_like_container(folders[album_index - 1])
        )
        if not parent_is_a_name:
            album, year, album_artist = "", None, None

    # Some rippers stamp the entire path into the filename. If the title still opens
    # with the album we just worked out, that is filing, not part of the song's name.
    if album:
        without_album = re.sub(
            r'^' + re.escape(album) + r'\s*[-–—_.]*\s*', "", title, count=1, flags=re.IGNORECASE
        )
        if without_album and without_album != title:
            title = without_album
            track_match = _TRACK_PREFIX_RE.match(title)
            if track_match:
                track_number = track_number or int(track_match.group(1))
                title = title[track_match.end():].strip()

    # Everything that could plausibly be a name, best structural guess first.
    candidates: list[str] = []
    if album_index is not None and album_index > 0:
        parent = folders[album_index - 1]
        if not _looks_like_container(parent):
            candidates.append(parent)
    for name in (album_artist, basename_artist):
        if name and not is_junk_artist(name) and _plausible_name(name) and name not in candidates:
            candidates.append(name)
    for index in range(len(folders) - 1, -1, -1):
        # The album folder is the album, not the artist, even when it is the only
        # thing in the path that looks like a name.
        if index == album_index or _looks_like_container(folders[index]):
            continue
        if folders[index] not in candidates:
            candidates.append(folders[index])

    artist = ""
    if query_artist:
        # Believe the segment that agrees with the search over the one that merely
        # sits in the right place. This is what rescues "music/P/Paramore/Albums"
        # from being filed under "P", and it costs nothing when the path is sane.
        target = normalise_string(query_artist)
        best, best_score = "", 0.0
        for candidate in candidates:
            for variant in _name_variants(candidate):
                score = similarity(normalise_string(variant), target)
                if score > best_score:
                    best, best_score = variant, score
        if best and best_score >= SLSKD_ARTIST_MATCH_FLOOR:
            artist = best

    if not artist and candidates:
        artist = candidates[0]
    if not artist and query_artist:
        # Nothing in the path owns up to an artist, so fall back to what was asked
        # for. The match percentage on the card is the honesty check here.
        artist = query_artist

    # "02-paramore-ignorance.flac": no spaces around the dash, so the usual split
    # leaves the artist glued to the title. Only unglue it when the leading chunk
    # is the artist we just worked out, otherwise Jay-Z loses half his name.
    if artist and not basename_artist:
        lead, separator, rest = title.partition("-")
        if separator and rest.strip(" -–—_.") and similarity(normalise_string(lead), normalise_string(artist)) >= SLSKD_ARTIST_MATCH_FLOOR:
            title = rest.strip(" -–—_.")

    return {
        # Tidied because a folder called "paramore_" should not put a stray
        # underscore on the end of somebody's name in the results list.
        "artist": _tidy_edges(artist),
        "album": album.strip(),
        "year": year,
        "track_number": track_number,
        "title": title.strip() or basename,
    }


def extract_track_info_from_path(filepath: str) -> tuple[str, str]:
    """
    Extract artist and title from a Soulseek file path.
    Tries common patterns like 'Artist/Album/## - Title.ext'.

    Kept as the two-value shape the download path expects, "Unknown" and all.
    """
    parsed = parse_slskd_path(filepath)
    return parsed["artist"] or "Unknown", parsed["title"]


def _run_slskd_search(query: str, timeout_secs: int) -> list[dict]:
    """Run one slskd search and return its raw per-peer responses.

    Shared by the track search and the album folder search, which read the
    same responses very differently. Any failure is an empty list.
    """
    token = get_slskd_token()
    if not token:
        return []

    slskd_url = get_setting("slskd_url")

    try:
        headers = {"Authorization": f"Bearer {token}"}

        with httpx.Client(timeout=TIMEOUT_SLSKD_API) as client:
            # Start search
            search_response = client.post(
                f"{slskd_url}/api/v0/searches",
                headers=headers,
                json={"searchText": query}
            )

            if search_response.status_code != 200:
                print(f"slskd search failed: {search_response.status_code}")
                return []

            search_data = search_response.json()
            search_id = search_data["id"]
            print(f"slskd: Search started, ID: {search_id}")

            # Poll for results - wait for completion or timeout
            start_time = time.time()
            last_file_count = 0
            final_status = None
            while time.time() - start_time < timeout_secs:
                time.sleep(1)

                status_response = client.get(
                    f"{slskd_url}/api/v0/searches/{search_id}",
                    headers=headers
                )

                if status_response.status_code == 200:
                    final_status = status_response.json()
                    file_count = final_status.get("fileCount", 0)
                    if file_count != last_file_count:
                        print(f"slskd: Polling... {file_count} files, {final_status.get('responseCount', 0)} responses")
                        last_file_count = file_count
                    if final_status.get("isComplete"):
                        print(f"slskd: Search complete. {file_count} files total")
                        break

            if final_status:
                print(f"slskd: Final status - {final_status.get('fileCount', 0)} files, {final_status.get('responseCount', 0)} responses")

            # Small delay to allow responses to be fully indexed
            time.sleep(1)

            # Get responses with a short retry window in case indexing lags
            responses = []
            responses_deadline = time.time() + max(10, min(20, timeout_secs))
            while time.time() < responses_deadline:
                responses_response = client.get(
                    f"{slskd_url}/api/v0/searches/{search_id}/responses",
                    headers=headers
                )
                if responses_response.status_code == 200:
                    responses = responses_response.json()
                    if responses:
                        break
                time.sleep(0.5)

            print(f"slskd: Got {len(responses)} user responses")

            # Clean up search
            try:
                client.delete(f"{slskd_url}/api/v0/searches/{search_id}", headers=headers)
            except Exception:
                pass

            return responses

    except Exception as e:
        print(f"slskd search error: {e}")
        return []


def search_slskd(query: str, timeout_secs: int = TIMEOUT_SLSKD_SEARCH) -> list[dict]:
    """
    Search slskd and return normalized results.
    Returns list of dicts with: id, title, artist, quality, score, source, slskd_* fields
    """
    responses = _run_slskd_search(query, timeout_secs)
    results = []

    try:
        # Process results - pick best file from each user
        seen_tracks = set()
        skipped_locked = 0
        skipped_quality = 0
        skipped_no_slot = 0
        skipped_low_match = 0

        # Pull artist/title hints out of the query so the path-segment
        # scorer has something to work against. Watched playlist queries
        # come in as "Artist - Title" already; bare-text searches fall
        # back to title-only and lean on the basename.
        query_artist, query_title = parse_query(query)
        if not query_title:
            query_title = query

        for response in responses:
            username = response.get("username", "")
            has_free_slot = response.get("hasFreeUploadSlot", False)
            upload_speed = response.get("uploadSpeed", 0)
            queue_length = _peer_queue_length(response)

            if SLSKD_REQUIRE_FREE_SLOT and not has_free_slot:
                skipped_no_slot += 1
                continue

            files = response.get("files") or response.get("fileInfos") or response.get("fileInfo") or []
            for file_info in files:
                if file_info.get("isLocked", False):
                    skipped_locked += 1
                    continue

                filepath = file_info.get("filename", "")
                quality_label, quality_score = parse_slskd_quality(file_info)

                # Skip low quality
                if quality_score < SLSKD_MIN_QUALITY_SCORE:
                    skipped_quality += 1
                    continue

                parsed = parse_slskd_path(filepath, query_artist=query_artist)
                artist = parsed["artist"] or "Unknown"
                title = parsed["title"]

                # Dedupe by artist+title+quality
                # Keep one copy per peer, not one copy across the entire
                # network. Otherwise whichever peer happened to answer first
                # erased the less-busy alternative before ranking saw either
                # queue depth.
                track_key = f"{username.lower()}|{artist.lower()}|{title.lower()}|{quality_label}"
                if track_key in seen_tracks:
                    continue
                seen_tracks.add(track_key)

                duration_secs = file_info.get("length")
                duration_secs = duration_secs if isinstance(duration_secs, (int, float)) and duration_secs > 0 else None

                # Path-aware match confidence (0.0-1.0). Splits the slskd
                # filename on path separators and scores artist, album,
                # and title against each segment independently. Stops
                # "muse" matching "museum" and stops Various-Artists
                # folders from winning the auction.
                confidence, match_breakdown = score_track_against_filename(
                    expected_artist=query_artist or artist,
                    expected_title=query_title,
                    filename=filepath,
                    candidate_duration_s=duration_secs,
                    query=query,
                )

                if confidence < SLSKD_MATCH_CONFIDENCE_FLOOR:
                    skipped_low_match += 1
                    continue

                score_breakdown = list(match_breakdown)

                # Confidence is 0.0-1.0; scale to a 0-200 base so right-
                # title slskd matches stay competitive with the YouTube
                # scorer's typical 100-220 range plus its own +120 hi-res
                # quality bonus on Monochrome. A perfect match earns 200
                # of relevance before the quality and source bonuses
                # below stack on top.
                relevance_score = int(confidence * 200) + quality_score
                if quality_score:
                    score_breakdown.append(f"source_quality=+{quality_score}")
                # Soulseek users often share properly ripped files. Give
                # these results a source-trust lift after title/artist
                # relevance, so good matches beat lossy web sources without
                # letting unrelated files win just because they are FLAC.
                relevance_score += SLSKD_SOURCE_TRUST_BONUS
                score_breakdown.append(f"soulseek_trust=+{SLSKD_SOURCE_TRUST_BONUS}")
                quality_upper = quality_label.upper()
                if "FLAC" in quality_upper or "WAV" in quality_upper:
                    relevance_score += SLSKD_LOSSLESS_BONUS
                    score_breakdown.append(f"lossless=+{SLSKD_LOSSLESS_BONUS}")
                if bit_depth := file_info.get("bitDepth", 0):
                    if isinstance(bit_depth, int) and bit_depth >= 24:
                        relevance_score += SLSKD_HIRES_BONUS
                        score_breakdown.append(f"hires=+{SLSKD_HIRES_BONUS}")
                if has_free_slot:
                    relevance_score += 10
                    score_breakdown.append("free_slot=+10")
                if upload_speed > 1000000:  # > 1MB/s
                    relevance_score += 5
                    score_breakdown.append("fast_uploader=+5")
                if queue_length:
                    queue_penalty = min(
                        queue_length * SLSKD_QUEUE_PENALTY_PER_ITEM,
                        SLSKD_QUEUE_PENALTY_CAP,
                    )
                    relevance_score -= queue_penalty
                    score_breakdown.append(f"peer_queue=-{queue_penalty}")

                results.append({
                    # Stable id derived from who's sharing what: the same
                    # file gets the same id across searches, so blacklists
                    # and already-tried lists actually stick (a random uuid
                    # here made every retry a case of amnesia).
                    "id": "slskd_" + hashlib.md5(f"{username}|{filepath}".encode()).hexdigest()[:12],
                    "title": title,
                    "artist": artist,
                    "album": parsed["album"],
                    "year": parsed["year"],
                    "channel": username,  # Show username as "channel"
                    "quality": quality_label,
                    "relevance_score": relevance_score,
                    "score_breakdown": score_breakdown,
                    # 0.0-1.0 from the path scorer. The card shows it as a
                    # percentage so a hopeful match can't pass itself off as
                    # a certainty just because it landed at the top.
                    "match_confidence": round(confidence, 3),
                    "bitrate": _display_bitrate(file_info),
                    # Peer-level wait signal from slskd. It nudges an equally
                    # good result down the list but never removes it, because a
                    # busy lossless peer can still be the best remaining source.
                    "queue_length": queue_length,
                    "source": "soulseek",
                    "duration": str(file_info.get("length", 0)),
                    "size": file_info.get("size", 0),
                    "slskd_username": username,
                    "slskd_filename": filepath,
                    "slskd_size": file_info.get("size", 0),
                })

        print(
            "slskd: Skipped "
            f"{skipped_locked} locked, {skipped_quality} low quality, "
            f"{skipped_no_slot} no free slot, "
            f"{skipped_low_match} low match, kept {len(results)}"
        )

    except Exception as e:
        print(f"slskd search error: {e}")

    # Sort by relevance score (descending). Audio quality is one component.
    results.sort(key=lambda x: x["relevance_score"], reverse=True)

    return results[:SLSKD_MAX_RESULTS]


# How alike two track titles must be, once punctuation has failed to settle it.
# High on purpose: inside one album folder the right file is nearly always an
# exact match, and a near miss is far more often a different song.
_ALBUM_FOLDER_TITLE_FLOOR = 0.9


def _album_folder_tier(quality_label: str) -> Optional[str]:
    """'lossless', 'mp3' for MP3 320, or None for anything an album folder may not be."""
    upper = quality_label.upper()
    if upper.startswith(("FLAC", "WAV")):
        return "lossless"
    if upper == "MP3 320":
        return "mp3"
    return None


def _album_folder_key(username: str, filepath: str) -> tuple[str, ...]:
    """One peer's album folder, with any "CD1"/"Disc 2" subfolder folded into it."""
    folders = split_path_segments(filepath)[:-1]
    while folders and _DISC_FOLDER_RE.match(normalise_string(folders[-1])):
        folders = folders[:-1]
    return (username, *folders)


def _album_title_score(expected: str, found: str) -> float:
    """How surely a file's title is this track; 0.0 when it plainly is not.

    Exact once punctuation is gone, or very nearly the same and not a live or
    remixed take standing in for the studio one.
    """
    if not (expected and found):
        return 0.0
    if core_string(expected) == core_string(found):
        return 1.0
    if query_requests_variant(found) and not query_requests_variant(expected):
        return 0.0
    score = similarity(clean_title(expected), clean_title(found))
    return score if score >= _ALBUM_FOLDER_TITLE_FLOOR else 0.0


_BRACKETED_RE = re.compile(r'\s*[\(\[\{][^\)\]\}]*[\)\]\}]')


def _album_name_matches(expected: str, found: str) -> bool:
    """Is this folder named after this album?

    Bracketed tags ("[16-44]", "(Remastered)") are ignored, but a folder that
    calls itself live or remixed is not the studio album, however its files
    are named.
    """
    if query_requests_variant(found) and not query_requests_variant(expected):
        return False
    expected_bare = normalise_string(_BRACKETED_RE.sub("", expected))
    found_bare = normalise_string(_BRACKETED_RE.sub("", found))
    return bool(found_bare) and similarity(found_bare, expected_bare) >= SLSKD_ARTIST_MATCH_FLOOR


def _match_album_folder(folder: dict, artist: str, album_title: str, titles: list[str]) -> Optional[dict]:
    """Map every title onto one file in this folder, or None if it cannot.

    The folder has to be this artist's copy of this album, hold every title,
    and be one encoding throughout. Extra files (bonus tracks, artwork, a
    second edition's leftovers) are simply ignored.
    """
    parsed_files = []
    for file_info in folder["files"]:
        label, quality_score = parse_slskd_quality(file_info)
        parsed = parse_slskd_path(file_info.get("filename", ""), query_artist=artist)
        parsed_files.append((file_info, parsed, label, quality_score))

    # Judged once for the folder: every file in it shares the same path above.
    first = parsed_files[0][1]
    if similarity(normalise_string(first["artist"]), normalise_string(artist)) < SLSKD_ARTIST_MATCH_FLOOR:
        return None
    if not _album_name_matches(album_title, first["album"]):
        return None

    picks: dict[int, tuple[int, float]] = {}
    used: set[int] = set()
    for index, title in enumerate(titles):
        options = [
            (_album_title_score(title, parsed["title"]), quality_score, n)
            for n, (_info, parsed, _label, quality_score) in enumerate(parsed_files)
            if n not in used
        ]
        best = max(options, default=(0.0, 0, -1))
        if best[0] <= 0.0:
            return None
        used.add(best[2])
        picks[index] = (best[2], best[0])

    tiers = {_album_folder_tier(parsed_files[n][2]) for n, _score in picks.values()}
    if len(tiers) != 1 or None in tiers:
        return None

    response = folder["response"]
    username = folder["username"]
    results = {}
    for index, (n, confidence) in picks.items():
        file_info, parsed, label, quality_score = parsed_files[n]
        filepath = file_info.get("filename", "")
        length = file_info.get("length")
        length = int(length) if isinstance(length, (int, float)) and length > 0 else 0
        file_id = "slskd_" + hashlib.md5(f"{username}|{filepath}".encode()).hexdigest()[:12]
        results[index] = {
            "id": file_id,
            "video_id": file_id,
            "title": parsed["title"],
            "artist": parsed["artist"] or artist,
            "album": parsed["album"],
            "year": parsed["year"],
            "channel": username,
            "quality": label,
            # Identity-pinned, like Monochrome's album picks: chosen as part of
            # a whole record, not a contender in a ranked free-text list.
            "relevance_score": 1000 + quality_score,
            "score_breakdown": ["via=album-folder", f"source_quality=+{quality_score}"],
            "match_confidence": round(confidence, 3),
            "bitrate": _display_bitrate(file_info),
            "queue_length": _peer_queue_length(response),
            "source": "soulseek",
            "duration": f"{length // 60}:{length % 60:02d}" if length else "",
            "thumbnail": "",
            "size": file_info.get("size", 0),
            "source_url": f"soulseek://{username}/{filepath}",
            "slskd_username": username,
            "slskd_filename": filepath,
            "slskd_size": file_info.get("size", 0),
        }

    lossless = tiers == {"lossless"}
    return {
        "results": results,
        "lossless": lossless,
        "rank": (
            lossless,
            min(parsed_files[n][3] for n, _score in picks.values()),
            bool(response.get("hasFreeUploadSlot", False)),
            -(_peer_queue_length(response) or 0),
            response.get("uploadSpeed", 0) or 0,
        ),
    }


def resolve_album_folder(artist: str, album_title: str, titles: list[str]) -> Optional[tuple[dict[int, dict], bool]]:
    """Find one peer's folder holding every one of these tracks, in one encoding.

    Searching track by track takes each song from whoever answered best, so an
    album arrives as one person's FLAC, another's MP3 and a third's remaster.
    One folder is one rip. Only a complete folder counts, lossless throughout
    or MP3 320 throughout; anything less is left to the per-track search.

    Returns ({index into titles: result}, lossless) for the best folder, or
    None when Soulseek is off or nobody is sharing the whole record.
    """
    if not (slskd_enabled() and artist and album_title and titles):
        return None

    responses = _run_slskd_search(f"{artist} {album_title}", TIMEOUT_SLSKD_ALBUM_SEARCH)

    folders: dict[tuple[str, ...], dict] = {}
    for response in responses:
        if SLSKD_REQUIRE_FREE_SLOT and not response.get("hasFreeUploadSlot", False):
            continue
        username = response.get("username", "")
        files = response.get("files") or response.get("fileInfos") or response.get("fileInfo") or []
        for file_info in files:
            filepath = file_info.get("filename", "")
            if file_info.get("isLocked", False) or not _AUDIO_EXT_RE.search(filepath):
                continue
            folder = folders.setdefault(
                _album_folder_key(username, filepath),
                {"username": username, "response": response, "files": []},
            )
            folder["files"].append(file_info)

    matches = [
        match for folder in folders.values()
        if (match := _match_album_folder(folder, artist, album_title, titles))
    ]
    if not matches:
        print(f"slskd: no complete album folder for '{artist} - {album_title}' among {len(folders)} folders")
        return None

    best = max(matches, key=lambda m: m["rank"])
    sample = next(iter(best["results"].values()))
    print(
        f"slskd: album folder from {sample['slskd_username']} has all {len(titles)} "
        f"tracks of '{artist} - {album_title}' ({sample['quality']})"
    )
    return best["results"], best["lossless"]


def download_from_slskd(username: str, filename: str, dest_dir: Path, timeout_secs: int = TIMEOUT_SLSKD_DOWNLOAD, size: int | None = None) -> Optional[Path]:
    """
    Download a file from Soulseek via slskd.
    Returns the path to the downloaded file, or None on failure.

    Uses slskd_downloads_path setting if set; otherwise falls back to common download locations.
    slskd typically organises downloads as: {downloads_path}/{username}/{filename}
    """
    token = get_slskd_token()
    if not token:
        raise Exception("slskd authentication failed")

    slskd_url = get_setting("slskd_url")
    slskd_downloads_path = get_setting("slskd_downloads_path")

    headers = {"Authorization": f"Bearer {token}"}

    # Extract just the filename from the full path
    target_norm = normalize_slskd_path(filename)
    source_filename = Path(target_norm).name
    if not size or int(size) <= 0:
        raise Exception("Missing slskd file size; retry with fresh search")

    slskd_download_dirs = []
    if slskd_downloads_path:
        slskd_download_dirs.append(Path(slskd_downloads_path))
    slskd_download_dirs.extend([
        Path("/slskd/downloads"),
        Path("/app/downloads"),
        Path("/downloads"),
    ])
    seen_dirs = set()
    slskd_download_dirs = [
        d for d in slskd_download_dirs
        if not (str(d) in seen_dirs or seen_dirs.add(str(d)))
    ]

    try:
        with httpx.Client(timeout=TIMEOUT_SLSKD_API) as client:
            # Enqueue the download
            queue_item = {"filename": filename}
            if size is not None:
                queue_item["size"] = int(size)

            enqueue_response = client.post(
                f"{slskd_url}/api/v0/transfers/downloads/{username}",
                headers=headers,
                json=[queue_item]
            )

            if enqueue_response.status_code not in [200, 201]:
                raise Exception(f"Failed to enqueue download: {enqueue_response.status_code} {enqueue_response.text[:200]}")
            try:
                enqueue_data = enqueue_response.json()
                failed = enqueue_data.get("failed") or enqueue_data.get("Failed") or []
                enqueued = enqueue_data.get("enqueued", enqueue_data.get("Enqueued"))
                if failed or enqueued == 0:
                    raise Exception(f"slskd did not enqueue the file: {enqueue_data}")
            except ValueError:
                pass

            print(f"slskd: Enqueued download of '{source_filename}' from {username} ({int(size)} bytes)")

            # Poll for download completion
            start_time = time.time()
            download_complete = False
            downloaded_path = None
            abort_count = 0
            max_abort_requeues = 3  # Re-queue up to 3 times on abort before giving up
            last_state = ""

            while time.time() - start_time < timeout_secs:
                time.sleep(5)

                # Get download status for this user
                status_response = client.get(
                    f"{slskd_url}/api/v0/transfers/downloads/{username}",
                    headers=headers
                )

                if status_response.status_code != 200:
                    continue

                downloads_data = status_response.json()

                # slskd returns { "directories": [...], "files": [...] } structure
                # Each directory has "files" array with the actual transfer info
                files_to_check = []

                if isinstance(downloads_data, dict):
                    # New API format: { directories: [...] }
                    for directory in downloads_data.get("directories", []):
                        files_to_check.extend(directory.get("files", []))
                elif isinstance(downloads_data, list):
                    # Old API format: direct list of files
                    files_to_check = downloads_data

                # Find our file in the downloads
                file_found = False
                for dl in files_to_check:
                    dl_filename = dl.get("filename", "")
                    dl_norm = normalize_slskd_path(dl_filename)
                    dl_base = Path(dl_norm).name
                    if dl_norm == target_norm or dl_base == source_filename or dl_norm.endswith(f"/{source_filename}"):
                        file_found = True
                        state = _normalise_transfer_state(dl.get("stateDescription", dl.get("state", "")))
                        progress = dl.get("percentComplete", 0)

                        # Only log state changes to reduce noise
                        if state != last_state:
                            print(f"slskd: Download state: {state} ({progress}%)")
                            last_state = state

                        state_lower = state.lower()

                        # Terminal failure states - these won't recover
                        if any(s in state_lower for s in ("failed", "cancelled", "rejected", "errored")):
                            raise Exception(f"Download failed: {state}")

                        # Aborted is often transient - try re-queuing
                        if "aborted" in state_lower:
                            abort_count += 1
                            if abort_count > max_abort_requeues:
                                raise Exception(f"Download aborted {abort_count} times, giving up")

                            print(f"slskd: Download aborted, re-queuing (attempt {abort_count}/{max_abort_requeues})...")
                            time.sleep(2)  # Brief pause before re-queue

                            # Re-enqueue the download
                            requeue_response = client.post(
                                f"{slskd_url}/api/v0/transfers/downloads/{username}",
                                headers=headers,
                                json=[queue_item]
                            )
                            if requeue_response.status_code not in [200, 201]:
                                print(f"slskd: Re-queue failed with status {requeue_response.status_code}")
                            else:
                                print("slskd: Re-queued successfully")

                            last_state = ""  # Reset to log new state
                            break  # Continue polling

                        # Success states
                        if state_lower.startswith("completed") or state_lower == "succeeded":
                            # Make sure it's actually completed successfully, not "CompletedWithError"
                            if "error" not in state_lower:
                                download_complete = True
                                downloaded_path = get_slskd_local_path(dl) or dl_filename
                            else:
                                raise Exception(f"Download completed with error: {state}")
                            break

                if download_complete:
                    break

                # If file disappeared from the queue entirely, it might have been
                # removed or the user went offline - try re-queuing once
                if not file_found and last_state and "queue" not in last_state.lower():
                    print("slskd: File no longer in transfer queue, attempting re-queue...")
                    requeue_response = client.post(
                        f"{slskd_url}/api/v0/transfers/downloads/{username}",
                        headers=headers,
                        json=[queue_item]
                    )
                    if requeue_response.status_code in [200, 201]:
                        print("slskd: Re-queued successfully")
                    last_state = ""

            if not download_complete:
                raise Exception(f"Download timed out after {timeout_secs}s")

            # File should now be in slskd's downloads folder
            # slskd typically organises as: {downloads_path}/{username}/{filename}
            candidate_paths = []
            if downloaded_path:
                normalized_path = normalize_slskd_path(downloaded_path)
                dl_path = Path(normalized_path)
                # Only allow absolute paths if they're within a known download directory
                if dl_path.is_absolute():
                    # Security: verify the path is within allowed download directories
                    is_safe = False
                    for slskd_dir in slskd_download_dirs:
                        try:
                            dl_path.resolve().relative_to(slskd_dir.resolve())
                            is_safe = True
                            break
                        except ValueError:
                            continue
                    if is_safe:
                        candidate_paths.append(dl_path)
                    else:
                        print(f"slskd: Ignoring absolute path outside download dirs: {dl_path}")
                else:
                    for slskd_dir in slskd_download_dirs:
                        candidate_paths.append(slskd_dir / dl_path)
                        candidate_paths.append(slskd_dir / username / dl_path)

            for slskd_dir in slskd_download_dirs:
                candidate_paths.append(slskd_dir / username / source_filename)

            for potential_path in candidate_paths:
                # Security: resolve and verify the path is within allowed directories
                try:
                    resolved = potential_path.resolve()
                    is_safe = False
                    for slskd_dir in slskd_download_dirs:
                        try:
                            resolved.relative_to(slskd_dir.resolve())
                            is_safe = True
                            break
                        except ValueError:
                            continue
                    if not is_safe:
                        print(f"slskd: Skipping path outside download dirs: {resolved}")
                        continue
                except (OSError, ValueError):
                    continue

                path_options = [potential_path, *_completed_equivalent_paths(potential_path, slskd_download_dirs)]
                for path_option in path_options:
                    if path_option.exists():
                        dest_path = dest_dir / source_filename
                        return deliver_slskd_file(path_option, dest_path, slskd_download_dirs)

            # If not found, search recursively in the username folder
            expected_size = int(size)
            for slskd_dir in slskd_download_dirs:
                user_dir = slskd_dir / username
                if user_dir.exists():
                    found_file = _pick_sized_match(user_dir, source_filename, expected_size)
                    if found_file:
                        dest_path = dest_dir / source_filename
                        print(f"slskd: Found {found_file}")
                        return deliver_slskd_file(found_file, dest_path, slskd_download_dirs)

            # Some slskd installs group completed downloads by remote folder or
            # album rather than by Soulseek username. Fall back to the whole
            # configured root after the stricter username lookup fails.
            for slskd_dir in slskd_download_dirs:
                if slskd_dir.exists():
                    found_file = _pick_sized_match(slskd_dir, source_filename, expected_size)
                    if found_file:
                        dest_path = dest_dir / source_filename
                        print(f"slskd: Found {found_file}")
                        return deliver_slskd_file(found_file, dest_path, slskd_download_dirs)

            # List what's actually there for debugging
            for slskd_dir in slskd_download_dirs:
                if slskd_dir.exists():
                    print(f"slskd: Downloads directory contents ({slskd_dir}):")
                    for item in slskd_dir.iterdir():
                        print(f"  - {item.name}/")
                        if item.is_dir():
                            for subitem in list(item.iterdir())[:5]:
                                print(f"      {subitem.name}")
                else:
                    print(f"slskd: Downloads directory not found: {slskd_dir}")

            raise Exception(
                "Downloaded file not found at expected location. "
                "Check that the slskd downloads path is mounted into MusicGrabber."
            )

    except Exception as e:
        print(f"slskd download error: {e}")
        raise
