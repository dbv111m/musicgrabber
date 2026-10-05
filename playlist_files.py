"""
MusicGrabber - M3U playlist files on disk

The files themselves, as opposed to the watched playlists that write some of
them: list, read, reorder, prune, add to, create and bin.

Two rules keep this from eating anybody's playlist:

1. **Untouched entries go back exactly as they came out.** The client never sends
   an entry's text, only its position in the file it loaded, so a path in some
   charset we cannot even display survives an edit byte for byte. New entries are
   written the way the downloader writes them (`_m3u_entry`), relative to the M3U.
2. **A watched playlist's M3U is read-only here.** `rebuild_watched_playlist_m3u`
   rewrites it from the database on every refresh, so an edit would last until
   the next one. Save as copy forks it into a file nothing else will touch.

Files are identified by their path relative to the music folder, and only the
places MusicGrabber files playlists are in scope: anywhere under the Playlists
folder, or the top level of Singles when there is no Playlists folder.
"""

import os
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

import library_index
from constants import MUSIC_DIR
from db import db_conn
from settings import get_playlists_dir, get_setting, get_singles_dir, resolve_custom_subdir
from utils import authoritative_audio_file, move_to_trash, sanitize_playlist_name, set_file_permissions


class PlaylistFileError(Exception):
    """Something the caller asked for that we will not or cannot do."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# =============================================================================
# Where playlists live
# =============================================================================

def _music_dir(user_id: str | None) -> Path:
    return Path(get_setting("music_dir", str(MUSIC_DIR), user_id=user_id))


def _norm(path: Path | str) -> str:
    return os.path.normpath(str(path))


def _inside(path: Path | str, directory: Path | str) -> bool:
    try:
        Path(_norm(path)).relative_to(_norm(directory))
        return True
    except ValueError:
        return False


def default_dir(user_id: str | None) -> Path:
    """Where a brand new playlist goes: the same place the downloader would put one."""
    return get_playlists_dir(user_id=user_id) or get_singles_dir(user_id=user_id)


def _candidate_files(user_id: str | None) -> list[Path]:
    playlists_dir = get_playlists_dir(user_id=user_id)
    singles_dir = get_singles_dir(user_id=user_id)
    found: dict[str, Path] = {}
    if playlists_dir and playlists_dir.is_dir():
        for dirpath, _dirs, files in os.walk(playlists_dir):
            for name in files:
                if name.lower().endswith(".m3u"):
                    path = Path(dirpath) / name
                    found.setdefault(_norm(path), path)
    if singles_dir.is_dir():
        for path in singles_dir.glob("*.m3u"):
            found.setdefault(_norm(path), path)
    return [p for p in found.values() if p.is_file() and not p.is_symlink()]


def resolve_m3u(user_id: str | None, rel: str, *, must_exist: bool = True) -> Path:
    """The M3U a client-supplied relative path names, or a PlaylistFileError.

    Normalised before it is checked, so `Playlists/../../etc/passwd.m3u` is
    judged by where it actually points rather than by how polite it looks.
    """
    rel = (rel or "").strip().lstrip("/")
    if not rel or not rel.lower().endswith(".m3u"):
        raise PlaylistFileError(400, "Not an M3U file")
    music_dir = _music_dir(user_id)
    path = Path(_norm(music_dir / rel))
    playlists_dir = get_playlists_dir(user_id=user_id)
    singles_dir = get_singles_dir(user_id=user_id)
    allowed = (
        (playlists_dir is not None and _inside(path, playlists_dir))
        or _norm(path.parent) == _norm(singles_dir)
    )
    if not allowed or not _inside(path, music_dir):
        raise PlaylistFileError(400, "That file is outside the playlist folders")
    if path.is_symlink():
        raise PlaylistFileError(400, "Symlinked playlists are not editable")
    if must_exist and not path.is_file():
        raise PlaylistFileError(404, "Playlist file not found")
    return path


def _rel(user_id: str | None, path: Path) -> str:
    return Path(_norm(path)).relative_to(_norm(_music_dir(user_id))).as_posix()


def watched_m3u_paths(user_id: str | None) -> set[str]:
    """Every M3U a watched playlist will rebuild, as normalised path strings.

    Legacy rows with no owner count too. This is a guard, so it errs towards
    calling a file watched; the worst case is Save as copy instead of Save.
    """
    paths: set[str] = set()
    try:
        with db_conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, name, use_playlists_dir, custom_subdir, user_id
                   FROM watched_playlists
                   WHERE make_m3u = 1 AND (user_id IS ? OR user_id IS NULL)""",
                (user_id,),
            ).fetchall()
    except Exception as e:
        print(f"[m3u] Could not read watched playlists: {e}")
        return paths
    for row in rows:
        owner = row["user_id"]
        try:
            if row["custom_subdir"]:
                m3u_dir = resolve_custom_subdir(row["custom_subdir"], user_id=owner)
            elif row["use_playlists_dir"]:
                m3u_dir = get_playlists_dir(user_id=owner)
            else:
                m3u_dir = None
            m3u_dir = m3u_dir or get_singles_dir(user_id=owner)
        except Exception:
            continue
        paths.add(_norm(m3u_dir / f"{sanitize_playlist_name(row['name'], row['id'])}.m3u"))
    return paths


# =============================================================================
# Reading and writing the format
# =============================================================================

def parse_m3u(raw: bytes) -> tuple[list[str], list[dict], list[str]]:
    """Split an M3U into header lines, entries, and trailing comment lines.

    Each entry is {"meta": [...], "line": str}: the directive and comment lines
    sitting directly above it (#EXTINF and friends) travel with it, so moving a
    track moves its duration and display name too. Decoded with surrogateescape
    so encode() gives back the original bytes, whatever charset wrote them.
    """
    text = raw.decode("utf-8", errors="surrogateescape")
    if text.startswith("﻿"):
        text = text[1:]
    header: list[str] = []
    entries: list[dict] = []
    pending: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            # Directives before the first entry or #EXTINF describe the file, not a track.
            if not entries and not pending and not stripped.upper().startswith("#EXTINF"):
                header.append(line)
            else:
                pending.append(line)
            continue
        entries.append({"meta": pending, "line": line})
        pending = []
    return header, entries, pending


def serialise_m3u(header: list[str], entries: list[dict], trailer: list[str]) -> bytes:
    lines = list(header) or ["#EXTM3U"]
    for entry in entries:
        lines.extend(entry["meta"])
        lines.append(entry["line"])
    lines.extend(trailer)
    return ("\n".join(lines) + "\n").encode("utf-8", errors="surrogateescape")


def _display(text: str) -> str:
    return text.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")


def resolve_entry(line: str, m3u_dir: Path, music_dir: Path) -> tuple[Path | None, str]:
    """Where an entry points, and whether we can vouch for it.

    "ok" exists; "missing" should be in the music folder and is not; "external"
    is a URL, or a path from outside the music folder that does not exist here.
    Those are what Navidrome and Lidarr hand back, written as their side sees
    the library, so they are not ours to call missing or to tidy away.
    """
    target = line.strip()
    if "://" in target:
        if not target.lower().startswith("file://"):
            return None, "external"
        target = unquote(urlparse(target).path)
    path = Path(target.replace("\\", "/"))
    if not path.is_absolute():
        path = m3u_dir / path
    path = Path(_norm(path))
    if os.path.isfile(path):
        return path, "ok"
    if _inside(path, music_dir):
        return path, "missing"
    return path, "external"


def _extinf_title(meta: list[str]) -> str:
    for line in meta:
        if line.upper().startswith("#EXTINF") and "," in line:
            return line.split(",", 1)[1].strip()
    return ""


def _load(path: Path) -> tuple[bytes, int]:
    try:
        st = path.stat()
        return path.read_bytes(), st.st_mtime_ns
    except OSError as e:
        raise PlaylistFileError(500, f"Could not read the playlist: {e}")


def _atomic_write(path: Path, data: bytes) -> int:
    """Write via a temp file and a rename, so a crash leaves the old file, not half a new one."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise PlaylistFileError(500, f"Could not write the playlist: {e}")
    set_file_permissions(path)
    return path.stat().st_mtime_ns


def _invalidate_routing_list(user_id: str | None) -> None:
    from library_scans import snapshots
    snapshots.forget(("playlists", user_id, str(get_playlists_dir(user_id=user_id))))


# =============================================================================
# Operations
# =============================================================================

def list_playlists(user_id: str | None) -> list[dict]:
    music_dir = _music_dir(user_id)
    watched = watched_m3u_paths(user_id)
    results = []
    for path in _candidate_files(user_id):
        try:
            raw, mtime_ns = _load(path)
        except PlaylistFileError:
            continue
        _header, entries, _trailer = parse_m3u(raw)
        missing = sum(
            1 for e in entries if resolve_entry(e["line"], path.parent, music_dir)[1] == "missing"
        )
        results.append({
            "path": _rel(user_id, path),
            "name": path.stem,
            "folder": _rel(user_id, path.parent),
            "entries": len(entries),
            "missing": missing,
            "watched": _norm(path) in watched,
            "mtime_ns": str(mtime_ns),
        })
    results.sort(key=lambda p: (p["name"].casefold(), p["path"]))
    return results


def read_playlist(user_id: str | None, rel: str) -> dict:
    path = resolve_m3u(user_id, rel)
    raw, mtime_ns = _load(path)
    _header, entries, _trailer = parse_m3u(raw)
    music_dir = _music_dir(user_id)
    resolved = [resolve_entry(e["line"], path.parent, music_dir) for e in entries]
    tags = library_index.describe_files(
        str(p) for p, state in resolved if p is not None and state == "ok"
    )
    rows = []
    for index, (entry, (target, state)) in enumerate(zip(entries, resolved)):
        info = tags.get(str(target), {}) if target is not None else {}
        rows.append({
            "index": index,
            "line": _display(entry["line"]),
            "path": str(target) if target is not None else None,
            "state": state,
            "artist": info.get("artist") or "",
            "title": info.get("title") or "",
            "album": info.get("album") or "",
            "duration": info.get("duration") or 0,
            "label": _display(_extinf_title(entry["meta"])) or (target.stem if target else _display(entry["line"])),
        })
    return {
        "path": _rel(user_id, path),
        "name": path.stem,
        "watched": _norm(path) in watched_m3u_paths(user_id),
        "mtime_ns": str(mtime_ns),
        "entries": rows,
    }


def _build_entries(user_id: str | None, m3u_dir: Path, old_entries: list[dict], wanted: list[dict]) -> list[dict]:
    """Turn the client's list of {index} and {file} picks into entries to write."""
    from downloads import _m3u_entry

    out = []
    for item in wanted:
        if item.get("index") is not None:
            try:
                index = int(item["index"])
            except (TypeError, ValueError):
                raise PlaylistFileError(400, "Bad entry index")
            if not 0 <= index < len(old_entries):
                raise PlaylistFileError(409, "The playlist changed underneath you; reopen it and try again")
            out.append(old_entries[index])
        elif item.get("file"):
            audio = authoritative_audio_file(item["file"], user_id)
            if audio is None:
                raise PlaylistFileError(400, f"Not a track in your library: {item['file']}")
            out.append({"meta": [], "line": _m3u_entry(audio, m3u_dir, user_id=user_id)})
        else:
            raise PlaylistFileError(400, "Each entry needs an index or a file")
    return out


def _check_mtime(expected: str | None, actual: int) -> None:
    if expected is not None and str(expected) != str(actual):
        raise PlaylistFileError(
            409, "Something else changed this playlist since you opened it (a download, probably). Reopen it and redo your edit."
        )


def save_playlist(user_id: str | None, rel: str, mtime_ns: str, entries: list[dict]) -> dict:
    path = resolve_m3u(user_id, rel)
    if _norm(path) in watched_m3u_paths(user_id):
        raise PlaylistFileError(403, "A watched playlist rebuilds this file on every refresh. Save it as a copy instead.")
    raw, current_mtime = _load(path)
    _check_mtime(mtime_ns, current_mtime)
    header, old_entries, trailer = parse_m3u(raw)
    new_entries = _build_entries(user_id, path.parent, old_entries, entries)
    new_mtime = _atomic_write(path, serialise_m3u(header, new_entries, trailer))
    _invalidate_routing_list(user_id)
    return {"path": _rel(user_id, path), "mtime_ns": str(new_mtime), "entries": len(new_entries)}


def create_playlist(
    user_id: str | None, name: str, copy_from: str | None = None,
    mtime_ns: str | None = None, entries: list[dict] | None = None,
) -> dict:
    """A new empty playlist, or a copy of an existing one (with edits applied).

    A copy sits beside its source so every relative entry still resolves.
    """
    safe = sanitize_playlist_name(name or "", "") if (name or "").strip() else ""
    if not safe:
        raise PlaylistFileError(400, "Give the playlist a name")
    header, old_entries, trailer = [], [], []
    if copy_from:
        source = resolve_m3u(user_id, copy_from)
        raw, current_mtime = _load(source)
        _check_mtime(mtime_ns, current_mtime)
        header, old_entries, trailer = parse_m3u(raw)
        target_dir = source.parent
        if entries is None:
            entries = [{"index": i} for i in range(len(old_entries))]
    else:
        target_dir = default_dir(user_id)
    path = target_dir / f"{safe}.m3u"
    resolve_m3u(user_id, _rel(user_id, path), must_exist=False)
    if path.exists():
        raise PlaylistFileError(409, f'There is already a playlist called "{safe}"')
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise PlaylistFileError(500, f"Could not create the playlist folder: {e}")
    new_entries = _build_entries(user_id, target_dir, old_entries, entries or [])
    new_mtime = _atomic_write(path, serialise_m3u(header, new_entries, trailer))
    _invalidate_routing_list(user_id)
    return {"path": _rel(user_id, path), "name": path.stem, "mtime_ns": str(new_mtime), "entries": len(new_entries)}


def delete_playlist(user_id: str | None, rel: str) -> dict:
    """Bin the M3U. Never the audio: that belongs to the library, not the list."""
    path = resolve_m3u(user_id, rel)
    if _norm(path) in watched_m3u_paths(user_id):
        raise PlaylistFileError(403, "A watched playlist would only write this file again. Turn off its Generate M3U instead.")
    if move_to_trash(path, user_id=user_id) is None:
        raise PlaylistFileError(500, "Could not move the playlist to the trash")
    _invalidate_routing_list(user_id)
    return {"deleted": _rel(user_id, path)}


def search_library(user_id: str | None, query: str, limit: int = 50) -> dict:
    root = _music_dir(user_id)
    results = library_index.search_files(root, query, limit=limit)
    if results is None:
        return {"results": [], "index_ready": False}
    return {"results": results, "index_ready": True}
