"""
MusicGrabber - Metadata Enrichment

AcoustID fingerprinting, MusicBrainz lookups, LRClib lyrics, and audio file tagging.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Optional

import httpx
from mutagen.flac import FLAC

from constants import (
    VERSION, TIMEOUT_HTTP_REQUEST, TIMEOUT_FPCALC,
    ACOUSTID_API_KEY, ACOUSTID_MIN_SCORE,
)
from settings import get_setting_bool
from utils import set_file_permissions


def lookup_musicbrainz(artist: str, title: str) -> Optional[dict]:
    """Look up track metadata from MusicBrainz"""
    if not get_setting_bool("enable_musicbrainz", True):
        return None

    try:
        headers = {"User-Agent": f"MusicGrabber/{VERSION} (https://gitlab.com/g33kphr33k/musicgrabber)"}

        search_url = "https://musicbrainz.org/ws/2/recording/"
        params = {
            "query": f'artist:"{artist}" AND recording:"{title}"',
            "fmt": "json",
            "limit": 1
        }

        with httpx.Client(timeout=TIMEOUT_HTTP_REQUEST) as client:
            response = client.get(search_url, params=params, headers=headers)

        if response.status_code != 200:
            return None

        data = response.json()

        if not data.get("recordings"):
            return None

        recording = data["recordings"][0]

        # Extract metadata
        metadata = {
            "title": recording.get("title"),
            "artist": recording["artist-credit"][0]["name"] if recording.get("artist-credit") else None,
            "metadata_source": "musicbrainz_text",
        }

        # Get release information for album and date
        if recording.get("releases"):
            release = recording["releases"][0]
            metadata["album"] = release.get("title")
            metadata["date"] = release.get("date")

            # Extract year from date
            if metadata.get("date"):
                year_match = re.match(r'(\d{4})', metadata["date"])
                if year_match:
                    metadata["year"] = year_match.group(1)

        return metadata

    except Exception:
        # If MusicBrainz lookup fails, just continue without it
        return None


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

    AcoustID returns a pile of recordings for a fingerprint — covers, remasters,
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

    # Title match — bonus for exact match, smaller bonus for substring
    if exp_title == rec_title:
        score += 8
    elif exp_title in rec_title or rec_title in exp_title:
        score += 5

    # Penalise covers, remixes, and karaoke — we want the real deal
    if "cover" in rec_title or "karaoke" in rec_title or "tribute" in rec_title:
        score -= 8

    # Penalise remastered/live/session versions — prefer the original
    if "remaster" in rec_title or "live" in rec_title or "session" in rec_title:
        score -= 2

    # Slight bonus for having release groups (means it's well-catalogued)
    if recording.get("releasegroups"):
        score += 1

    return score


def _extract_recording_metadata(recording: dict) -> dict:
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

    # Extract album from release groups — prefer actual albums over singles/compilations
    releasegroups = recording.get("releasegroups", [])
    if releasegroups:
        album_rg = next(
            (rg for rg in releasegroups if rg.get("type") == "Album"),
            releasegroups[0]
        )
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
            "client": ACOUSTID_API_KEY,
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
        metadata = _extract_recording_metadata(recording)

        print(f"AcoustID match (score {fp_score:.2f}): {metadata['artist']} - {metadata['title']}")
        return metadata

    except Exception as e:
        print(f"AcoustID lookup failed: {e}")
        return None


def _lookup_musicbrainz_by_id(recording_id: str) -> Optional[dict]:
    """Fetch release date from MusicBrainz using a recording MBID.

    AcoustID gives us the recording ID but not the release date,
    so we pop over to MusicBrainz to fill in that gap.
    """
    try:
        headers = {"User-Agent": f"MusicGrabber/{VERSION} (https://gitlab.com/g33kphr33k/musicgrabber)"}

        url = f"https://musicbrainz.org/ws/2/recording/{recording_id}"
        params = {"inc": "releases", "fmt": "json"}

        with httpx.Client(timeout=TIMEOUT_HTTP_REQUEST) as client:
            response = client.get(url, params=params, headers=headers)

        if response.status_code != 200:
            return None

        data = response.json()
        releases = data.get("releases", [])
        if not releases:
            return None

        # Grab the first release's date and title
        release = releases[0]
        result = {}

        date_str = release.get("date", "")
        if date_str:
            year_match = re.match(r'(\d{4})', date_str)
            if year_match:
                result["year"] = year_match.group(1)

        if release.get("title"):
            result["album"] = release["title"]

        return result if result else None

    except Exception:
        return None


def lookup_metadata(artist: str, title: str, file_path: Path = None) -> Optional[dict]:
    """Look up track metadata, trying audio fingerprinting first.

    The hierarchy of increasingly desperate measures:
    1. Fingerprint the file with fpcalc -> query AcoustID
    2. If AcoustID matches, fetch the release date from MusicBrainz by recording ID
    3. If fingerprinting fails or scores too low, fall back to text-based MusicBrainz search

    Returns a dict with 'title', 'artist', 'album', 'year' or None.
    """
    if not get_setting_bool("enable_musicbrainz", True):
        return None

    # Step 1: Try AcoustID fingerprinting (if we have a file to work with)
    if file_path and file_path.exists():
        fp_result = _run_fpcalc(file_path)
        if fp_result:
            duration, fingerprint = fp_result
            acoustid_meta = _lookup_acoustid(duration, fingerprint, artist, title)

            if acoustid_meta:
                acoustid_meta["metadata_source"] = "acoustid_fingerprint"
                # Step 2: Fill in the release date from MusicBrainz
                recording_id = acoustid_meta.get("recording_id")
                if recording_id and not acoustid_meta.get("year"):
                    mb_extra = _lookup_musicbrainz_by_id(recording_id)
                    if mb_extra:
                        if mb_extra.get("year"):
                            acoustid_meta["year"] = mb_extra["year"]
                        if mb_extra.get("album") and not acoustid_meta.get("album"):
                            acoustid_meta["album"] = mb_extra["album"]

                return acoustid_meta

    # Step 3: Fall back to text-based MusicBrainz search
    return lookup_musicbrainz(artist, title)


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


def apply_metadata_to_file(file_path: Path, artist: str, title: str, album: str = "Singles", year: str = None):
    """Apply metadata to audio file using mutagen (supports multiple formats)"""
    try:
        suffix = file_path.suffix.lower()

        if suffix == '.flac':
            audio = FLAC(str(file_path))
            audio["ARTIST"] = artist
            audio["TITLE"] = title
            audio["ALBUM"] = album
            if year:
                audio["DATE"] = year
            audio.save()

        elif suffix == '.mp3':
            from mutagen.easyid3 import EasyID3
            from mutagen.mp3 import MP3
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
            audio["album"] = album
            if year:
                audio["date"] = year
            audio.save()

        elif suffix in ['.m4a', '.mp4']:
            from mutagen.mp4 import MP4
            audio = MP4(str(file_path))
            audio["\xa9ART"] = [artist]
            audio["\xa9nam"] = [title]
            audio["\xa9alb"] = [album]
            if year:
                audio["\xa9day"] = [year]
            audio.save()

        elif suffix in ['.ogg', '.opus']:
            from mutagen.oggopus import OggOpus
            from mutagen.oggvorbis import OggVorbis
            try:
                if suffix == '.opus':
                    audio = OggOpus(str(file_path))
                else:
                    audio = OggVorbis(str(file_path))
                audio["ARTIST"] = artist
                audio["TITLE"] = title
                audio["ALBUM"] = album
                if year:
                    audio["DATE"] = year
                audio.save()
            except Exception:
                pass  # Some ogg variants may not be supported

        # For .webm and other unsupported formats, skip metadata (yt-dlp handles it)

    except Exception:
        # If metadata application fails, continue anyway
        pass
