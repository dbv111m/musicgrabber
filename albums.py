"""
MusicGrabber - Album Download Pipeline

Everything involved in turning a MusicBrainz release into a queued bulk import:
existing/missing track comparison, the .albuminfo sidecar, the in-flight
duplicate guard, and handing the missing tracks to the bulk importer.

This used to live inline in app.py's /api/albums/download route. It moved here
so watched_artists.py (artist album-following) can call the same pipeline
without app.py importing watched_artists.py importing app.py in a circle that
would make Python very cross indeed.
"""

import contextlib
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from constants import AUDIO_EXTENSIONS
from db import db_conn
from downloads import rebuild_album_m3u
from metadata import fetch_album_release
from settings import get_albums_dir, get_setting_bool
from utils import clean_title, sanitize_filename, sanitize_playlist_name, set_file_permissions


class AlbumTracklistUnavailable(ValueError):
    """Raised when MusicBrainz has no tracklist for the requested release.

    A plain exception rather than an HTTPException, since this module has no
    business knowing about FastAPI. The route translates it back into the same
    404 it always returned.
    """


def _album_track_stem(artist: str, title: str, user_id: str | None = None) -> str:
    """Build the expected filename stem for an album track in override_dir mode."""
    safe_title = sanitize_filename(title or "") or "Unknown Title"
    if not get_setting_bool("organise_by_artist", True, user_id=user_id):
        safe_artist = sanitize_filename(artist or "Unknown Artist")
        return f"{safe_artist} - {safe_title}"
    return safe_title


def _normalise_album_match_text(text: str) -> str:
    """Normalise track titles for album-track matching."""
    t = clean_title(text or "")
    t = t.replace("’", "'").replace("‘", "'").replace("`", "'")
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = re.sub(r"\b(?:feat\.?|ft\.?|featuring)\b.*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*[\(\[].*?[\)\]]", "", t)
    t = re.sub(r"[^a-z0-9]+", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


_ALBUM_FOLDER_DECORATION = {
    "album", "ep", "lp", "deluxe", "edition", "expanded", "anniversary",
    "remaster", "remastered", "reissue", "bonus", "disc", "disk", "cd",
}


def _strip_album_folder_decoration(value: str) -> str:
    words = _normalise_album_match_text(value).split()
    while words and (
        words[-1] in _ALBUM_FOLDER_DECORATION
        or words[-1].isdigit() and len(words) > 1 and words[-2] in {"disc", "disk", "cd"}
    ):
        words.pop()
    return " ".join(words)


def _folder_names_equivalent(expected: str, actual: str, *, album: bool) -> bool:
    expected_n = _normalise_album_match_text(expected)
    actual_n = _normalise_album_match_text(actual)
    if not expected_n or not actual_n:
        return False
    if expected_n == actual_n:
        return True
    if album and _strip_album_folder_decoration(expected_n) == _strip_album_folder_decoration(actual_n):
        return True
    # Punctuation/transliteration differences should not create another copy,
    # but the threshold stays deliberately high: this is a duplicate check,
    # not a general "albums vaguely like this" browser.
    return SequenceMatcher(None, expected_n, actual_n).ratio() >= (0.88 if album else 0.92)


def _matching_album_dirs(
    artist: str,
    album_title: str,
    user_id: str | None = None,
) -> list[Path]:
    """Equivalent existing album folders, confined to configured Albums."""
    albums_root = get_albums_dir(user_id=user_id)
    if not albums_root.exists():
        return []

    exact_artist = albums_root / sanitize_filename(artist)
    artist_dirs = []
    if exact_artist.is_dir():
        artist_dirs.append(exact_artist)
    for candidate in albums_root.iterdir():
        if (
            candidate.is_dir()
            and candidate not in artist_dirs
            and _folder_names_equivalent(artist, candidate.name, album=False)
        ):
            artist_dirs.append(candidate)

    exact_album_name = sanitize_filename(album_title)
    matches = []
    for artist_dir in artist_dirs:
        exact_album = artist_dir / exact_album_name
        if exact_album.is_dir() and exact_album not in matches:
            matches.append(exact_album)
        for candidate in artist_dir.iterdir():
            if (
                candidate.is_dir()
                and candidate not in matches
                and _folder_names_equivalent(album_title, candidate.name, album=True)
            ):
                matches.append(candidate)
    return matches


def _album_file_matches_track(file_path: Path, artist: str, title: str) -> bool:
    wanted = _normalise_album_match_text(title)
    if not wanted:
        return False
    stem = _normalise_album_match_text(file_path.stem)
    if not stem:
        return False
    # Common album filenames start with a track/disc number or, in flat mode,
    # the artist credit. Remove only those structural prefixes.
    stem = re.sub(r"^(?:cd\s*)?\d+(?:\s+\d+)?\s+", "", stem)
    artist_n = _normalise_album_match_text(artist)
    if artist_n and stem.startswith(f"{artist_n} "):
        stem = stem[len(artist_n):].strip()
    if stem == wanted or stem.endswith(f" {wanted}"):
        return True
    return (
        min(len(stem), len(wanted)) >= 5
        and SequenceMatcher(None, wanted, stem).ratio() >= 0.90
    )


def find_existing_album_track(
    artist: str,
    album_title: str,
    title: str,
    user_id: str | None = None,
) -> Path | None:
    """Find one matching track inside the configured Albums tree only."""
    for album_dir in _matching_album_dirs(artist, album_title, user_id=user_id):
        for file_path in album_dir.iterdir():
            if (
                file_path.is_file()
                and file_path.suffix.lower() in AUDIO_EXTENSIONS
                and _album_file_matches_track(file_path, artist, title)
            ):
                return file_path
    return None


def album_audio_file_count(artist: str, album_title: str, user_id: str | None = None) -> int:
    """Count audio files in equivalent folders inside the configured Albums tree."""
    return sum(
        1
        for album_dir in _matching_album_dirs(artist, album_title, user_id=user_id)
        for path in album_dir.iterdir()
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
    )


def album_on_disk(artist: str, album_title: str, user_id: str | None = None) -> bool:
    """Cheap already-have-it check: does the album folder contain any audio?

    Unlike album_track_status, this never touches MusicBrainz. Used by list views
    (e.g. GET /api/watched-artists/{id}/albums) where fetching every release's
    tracklist just to render a badge would hammer MB for nothing.
    """
    return album_audio_file_count(artist, album_title, user_id=user_id) > 0


def watched_album_display_state(
    raw_status: str | None,
    import_status: str | None,
    audio_file_count: int,
    expected_track_count: int | None,
) -> dict:
    """Derive the current album state shown by the followed-artist panel.

    ``watched_artist_albums.status`` records the event that introduced the row:
    seen, queued, or failed. It was never advanced when the linked bulk import
    finished, so presenting it as live state left completed albums saying
    "Queued" forever. Disk contents are authoritative here; the linked import
    supplies expected size and distinguishes waiting from finished-but-partial.
    """
    raw = (raw_status or "seen").strip().lower()
    current_import = (import_status or "").strip().lower()
    actual = max(0, int(audio_file_count or 0))
    try:
        expected = int(expected_track_count) if expected_track_count is not None else None
    except (TypeError, ValueError):
        expected = None
    if expected is not None and expected <= 0:
        expected = None

    detail = f"{actual}/{expected} tracks on disk" if expected else (
        f"{actual} track{'s' if actual != 1 else ''} on disk" if actual else ""
    )

    if expected and actual >= expected:
        display_status = "complete"
    elif current_import == "processing":
        display_status = "downloading"
    elif current_import == "cancelling":
        display_status = "cancelling"
    elif current_import == "pending":
        display_status = "queued"
    elif actual:
        display_status = "incomplete" if expected else "on_disk"
    elif current_import == "cancelled":
        display_status = "cancelled"
    elif current_import in {"completed", "completed_with_errors", "error"}:
        display_status = "failed"
    else:
        display_status = raw

    return {
        "display_status": display_status,
        "status_detail": detail,
        "on_disk": actual > 0,
        "audio_file_count": actual,
        "expected_track_count": expected,
    }


def album_track_status(artist: str, album_title: str, tracks: list[dict], user_id: str | None = None) -> dict:
    """Return existing/missing status for tracklist against the target album directory."""
    target_dir = get_albums_dir(user_id=user_id) / sanitize_filename(artist) / sanitize_filename(album_title)
    matched_dirs = _matching_album_dirs(artist, album_title, user_id=user_id)
    # Continue an equivalent existing folder rather than creating e.g.
    # "Lost Souls" beside "Lost Souls EP" and splitting the record in two.
    album_dir = target_dir if target_dir in matched_dirs or not matched_dirs else matched_dirs[0]
    audio_files = [
        p
        for existing_dir in matched_dirs
        for p in existing_dir.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    ]

    title_counts = Counter(_normalise_album_match_text(t.get("title") or "") for t in tracks)
    track_status = []
    for t in tracks:
        title = (t.get("title") or "").strip()
        # The performer on this track, which is the release artist on a normal
        # album and somebody else entirely on a compilation.
        track_artist = (t.get("artist") or "").strip() or artist
        stem = _album_track_stem(track_artist, title, user_id=user_id)
        exists_exact = any(
            (existing_dir / f"{stem}{ext}").exists()
            for existing_dir in matched_dirs
            for ext in AUDIO_EXTENSIONS
        )
        exists = bool(
            exists_exact
            or any(_album_file_matches_track(path, track_artist, title) for path in audio_files)
        )
        if (t.get("disc_total") or 1) > 1:
            exists = any(album_repair_files({
                "override_dir": str(folder), "track_artist": track_artist,
                "track_title": title, "track_number": int(t.get("position") or 1),
                "disc_number": t.get("disc_number") or 1,
                "title_unique": title_counts[_normalise_album_match_text(title)] == 1,
            }) for folder in matched_dirs)
        track_status.append({
            "position": t.get("position"),
            "title": title,
            "artist": track_artist,
            "isrc": t.get("isrc"),
            "disc_number": t.get("disc_number", 1),
            "disc_total": t.get("disc_total", 1),
            "track_total": t.get("track_total"),
            "exists": exists,
        })

    existing_tracks = [t for t in track_status if t["exists"]]
    missing_tracks = [t for t in track_status if not t["exists"]]
    m3u_files = sorted(
        {p.name for existing_dir in matched_dirs for p in existing_dir.glob("*.m3u") if p.is_file()},
        key=str.casefold,
    )
    return {
        "album_dir": album_dir,
        "tracks": track_status,
        "existing_tracks": existing_tracks,
        "missing_tracks": missing_tracks,
        "m3u_files": m3u_files,
    }


def resolve_album_track_repair(
    artist: str, album_title: str, release_mbid: str, track_index: int,
    album_dir: str | None = None, user_id: str | None = None,
    track_title: str | None = None, disc_number: int | None = None,
    track_number: int | None = None,
) -> dict:
    """Resolve a rescue to one pressing, one track and a confined album folder."""
    if not artist.strip() or not album_title.strip() or not release_mbid or track_index < 0:
        raise ValueError("An artist, album, release and valid track selection are required")
    root = get_albums_dir(user_id=user_id)
    candidates = _matching_album_dirs(artist, album_title, user_id=user_id)
    target = Path(album_dir) if album_dir else (
        candidates[0] if candidates else root / sanitize_filename(artist) / sanitize_filename(album_title)
    )
    try:
        relative = target.resolve().relative_to(root.resolve())
    except ValueError:
        raise ValueError("Album destination must stay inside your Albums directory")
    if len(relative.parts) != 2 or any(p.is_symlink() for p in (target, target.parent)):
        raise ValueError("Album destination must be an artist/album folder without symlinks")
    expected = root / sanitize_filename(artist) / sanitize_filename(album_title)
    if target != expected and target not in candidates:
        raise ValueError("Album destination does not match the selected artist and album")

    info_path = target / ".albuminfo"
    if info_path.is_symlink():
        raise ValueError("Album info must not be a symlink")
    info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.exists() else {}
    if info.get("release_mbid") and info["release_mbid"] != release_mbid:
        raise ValueError("This folder belongs to a different pressing; reopen its album track list")
    release = fetch_album_release(release_mbid)
    tracks = release["tracks"]
    if track_index >= len(tracks):
        raise ValueError("The selected album track no longer exists; reload the track list")
    track = tracks[track_index]
    number = int(track.get("position") or track_index + 1)
    resolved_disc = int(track.get("disc_number") or 1)
    disc_total = int(track.get("disc_total") or 1)
    if (
        track_title is not None and track_title != track["title"]
        or disc_number is not None and disc_number != resolved_disc
        or track_number is not None and track_number != number
    ):
        raise ValueError("The selected album track has changed; reload the track list")
    return {
        "override_dir": str(target), "release_mbid": release_mbid,
        "album_artist": info.get("artist") or artist,
        "album_name": info.get("album") or album_title,
        "track_artist": track.get("artist") or artist,
        "track_title": track["title"], "track_number": number,
        "track_total": int(track.get("track_total") or len(tracks)),
        "disc_number": resolved_disc, "disc_total": disc_total,
        "year": info.get("year") or release.get("year") or None,
        "title_unique": sum(
            _normalise_album_match_text(t["title"]) == _normalise_album_match_text(track["title"])
            for t in tracks
        ) == 1,
    }


def album_repair_files(context: dict) -> list[Path]:
    """Find this track only in its destination, distinguishing numbered discs."""
    from mutagen import File as MutagenFile
    from mutagen import MutagenError
    target = Path(context["override_dir"])
    matches = []
    if not target.exists():
        return matches
    for path in target.iterdir():
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        if not _album_file_matches_track(path, context["track_artist"], context["track_title"]):
            continue
        # A wrong recording can also have wrong numbering. A unique release
        # title identifies it without trusting those tags; repeated titles need
        # the extra disc/track evidence to distinguish their local copies.
        if context.get("title_unique"):
            matches.append(path)
            continue
        try:
            tags = MutagenFile(str(path), easy=True)
            for key, expected in (("tracknumber", context["track_number"]), ("discnumber", context["disc_number"])):
                values = tags.get(key) if tags else None
                if values and int(str(values[0]).split("/")[0]) != expected:
                    break
            else:
                matches.append(path)
        except (MutagenError, ValueError, OSError):
            # An untagged bad download can still be rescued by its filename.
            matches.append(path)
    return matches


def queue_album_download(
    artist: str,
    album_title: str,
    release_mbid: str,
    make_m3u: bool = False,
    m3u_name: str = "",
    convert_audio: bool | None = None,
    user_id: str | None = None,
) -> dict:
    """Queue a full album for download.

    Fetches the tracklist from MusicBrainz, creates a bulk import job routed to
    Albums/Artist/Album/ instead of the normal Singles layout, and queues only
    the tracks not already present. Returns the same dict shape the
    /api/albums/download route has always returned (import_id, track_count,
    queued_count, existing_count, missing_count, album_dir, plus optional
    warning/m3u/already_queued fields).

    Raises AlbumTracklistUnavailable if MusicBrainz has no tracklist for the
    release.
    """
    release = fetch_album_release(release_mbid)
    tracks = release["tracks"]
    album_year = release["year"]
    if not tracks:
        raise AlbumTracklistUnavailable("Could not fetch tracklist from MusicBrainz")

    status = album_track_status(artist, album_title, tracks, user_id=user_id)
    album_dir = status["album_dir"]
    missing_tracks = status["missing_tracks"]
    album_dir.mkdir(parents=True, exist_ok=True)

    # Write a .albuminfo sidecar so the picker can restore MBID context without a DB.
    # Atomic write (mkstemp + rename) so a crash mid-write leaves nothing corrupt.
    albuminfo_path = album_dir / ".albuminfo"
    if not albuminfo_path.exists():
        tmp_fd, tmp_path = tempfile.mkstemp(dir=album_dir, suffix=".albuminfo.tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "artist": artist,
                    "album": album_title,
                    "release_mbid": release_mbid,
                    "year": album_year,
                }, indent=2))
            Path(tmp_path).rename(albuminfo_path)
            set_file_permissions(albuminfo_path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)

    # Refuse to queue if an in-flight import is already targeting this album directory.
    # Catches double-clicks and impatient re-submissions before any files have landed.
    with db_conn() as conn:
        inflight = conn.execute(
            """SELECT id FROM bulk_imports
               WHERE override_dir = ?
                 AND status IN ('pending', 'processing')
                 AND completed_at IS NULL
               LIMIT 1""",
            (str(album_dir),),
        ).fetchone()
    if inflight:
        return {
            "import_id": inflight[0],
            "track_count": len(tracks),
            "queued_count": len(missing_tracks),
            "existing_count": len(status["existing_tracks"]),
            "missing_count": len(missing_tracks),
            "album_dir": str(album_dir),
            "warning": "Download already in progress for this album.",
            "already_queued": True,
        }

    # Queue only missing tracks; already-present tracks are left as-is.
    # ISRC list runs parallel to track_pairs (same list, same order) so the bulk
    # importer can try the exact studio recording before falling back to free text.
    # Search for the credited performer, not the sleeve. On a Various Artists
    # compilation the two are wildly different, and hunting for
    # "Various Artists - Song" finds precisely nothing.
    track_pairs = [(t.get("artist") or artist, t["title"]) for t in missing_tracks]
    track_isrcs = [t.get("isrc") for t in missing_tracks]
    # Each track's place on the release, so a top-up tags track 7 as 7 rather
    # than as the third thing it happened to queue. Vinyl positions ("A1") have
    # no number in them, so those count along their disc instead.
    disc_counts: Counter = Counter()
    for t in status["tracks"]:
        disc_counts[t["disc_number"]] += 1
        position = str(t.get("position") or "")
        t["track_number"] = int(position) if position.isdigit() else disc_counts[t["disc_number"]]
    track_positions = [
        {
            "track_number": t["track_number"],
            "track_total": t.get("track_total"),
            "disc_number": t["disc_number"],
            "disc_total": t["disc_total"],
        }
        for t in missing_tracks
    ]

    if not track_pairs:
        updated_m3u = None
        if make_m3u:
            updated_m3u = rebuild_album_m3u(album_dir, m3u_name or f"{artist} - {album_title}")
        return {
            "import_id": None,
            "track_count": len(tracks),
            "queued_count": 0,
            "existing_count": len(status["existing_tracks"]),
            "missing_count": 0,
            "album_dir": str(album_dir),
            "m3u_updated": bool(updated_m3u),
            "m3u_path": str(updated_m3u) if updated_m3u else None,
            "warning": "Album already exists on disk. Nothing queued." + (" Existing M3U updated." if updated_m3u else ""),
        }

    from bulk_import import start_bulk_import_for_tracks
    try:
        import_id = start_bulk_import_for_tracks(
            tracks=track_pairs,
            track_isrcs=track_isrcs,
            track_positions=track_positions,
            convert_audio=convert_audio,
            user_id=user_id,
            override_dir=str(album_dir),
            album_release_mbid=release_mbid,
            album_total_tracks=len(tracks),
            album_year=album_year,
        )
    except sqlite3.IntegrityError:
        # Someone else got there between our check above and this insert. The
        # partial unique index caught it, so report their import rather than
        # queueing the same album into the same folder twice.
        with db_conn() as conn:
            winner = conn.execute(
                """SELECT id FROM bulk_imports
                   WHERE override_dir = ?
                     AND status IN ('pending', 'processing')
                     AND completed_at IS NULL
                   LIMIT 1""",
                (str(album_dir),),
            ).fetchone()
        return {
            "import_id": winner[0] if winner else None,
            "track_count": len(tracks),
            "queued_count": len(missing_tracks),
            "existing_count": len(status["existing_tracks"]),
            "missing_count": len(missing_tracks),
            "album_dir": str(album_dir),
            "warning": "Download already in progress for this album.",
            "already_queued": True,
        }

    # If M3U requested, store the album details so create_bulk_playlist can pick it up.
    # We repurpose the existing create_playlist + playlist_name mechanism.
    if make_m3u:
        playlist_label = m3u_name or f"{artist} - {album_title}"
        if playlist_label.lower().endswith(".m3u"):
            playlist_label = playlist_label[:-4]
        playlist_label = sanitize_playlist_name(playlist_label, f"{artist} - {album_title}")
        with db_conn() as conn:
            conn.execute(
                "UPDATE bulk_imports SET create_playlist = 1, playlist_name = ? WHERE id = ?",
                (playlist_label, import_id)
            )
            conn.commit()

    return {
        "import_id": import_id,
        "track_count": len(tracks),
        "queued_count": len(track_pairs),
        "existing_count": len(status["existing_tracks"]),
        "missing_count": len(missing_tracks),
        "album_dir": str(album_dir),
        "warning": f"{len(status['existing_tracks'])} track(s) already existed; queued {len(track_pairs)} missing track(s).",
    }
