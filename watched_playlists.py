"""
MusicGrabber - Watched Playlists

Platform detection, track fetching, playlist refresh, and background scheduler.
"""

import json
import pathlib
import re
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import HTTPException

from constants import (
    TIMEOUT_YTDLP_PLAYLIST, TIMEOUT_HTTP_SPOTIFY,
    SPOTIFY_EMBED_MAX_ATTEMPTS, SPOTIFY_EMBED_RETRY_BACKOFF,
    WATCHED_PLAYLIST_CHECK_HOURS, WATCHED_HISTORY_RECHECK_HOURS,
    WATCHED_REFRESH_STALE_SECONDS,
    WATCHED_GONE_STRIKES_BEFORE_PAUSE,
    LISTENBRAINZ_API_URL, TIMEOUT_LISTENBRAINZ, TIMEOUT_LISTENBRAINZ_PLAYLIST,
    AUDIO_EXTENSIONS,
)
from db import db_conn
from bulk_import import start_bulk_import_for_tracks
from amazon import fetch_amazon_playlist
from apple import fetch_apple_music_playlist
from beatport import fetch_beatport_playlist
from tidal import fetch_tidal_playlist
from downloads import rebuild_watched_playlist_m3u
from settings import (
    get_playlists_dir, get_setting, get_setting_bool, get_download_dir,
    resolve_custom_subdir,
)
from spotify import fetch_spotify_playlist_via_browser
from utils import (
    extract_artist_title, hash_track, spawn_daemon_thread, sanitize_filename,
    check_duplicate, sanitize_playlist_name,
)
from downloads import check_navidrome_duplicate, check_lidarr_duplicate, _tag_track_comment
from youtube import (
    _ytdlp_base_args,
    classify_ytdlp_failure,
    is_personalised_youtube_playlist,
    resolve_cookie_file,
    run_ytdlp_with_retries,
)

import httpx


def _normalise_match_text(text: str) -> str:
    """Normalise text for loose track/file matching."""
    t = (text or "").lower()
    t = t.replace("’", "'").replace("‘", "'").replace("`", "'")
    t = re.sub(r"\s*[\(\[].*?[\)\]]", "", t)
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _find_track_in_dir(track_dir: pathlib.Path, artist: str, title: str) -> pathlib.Path | None:
    """Find a track's file in a folder by exact stem, then fuzzy match. None if absent."""
    if not track_dir.exists():
        return None

    stem = f"{sanitize_filename(artist or 'Unknown Artist')} - {sanitize_filename(title or 'Unknown Title')}"
    for ext in AUDIO_EXTENSIONS:
        candidate = track_dir / f"{stem}{ext}"
        if candidate.exists():
            return candidate

    # Fuzzy fallback so metadata wobble does not hide real files.
    artist_n = _normalise_match_text(artist)
    title_n = _normalise_match_text(title)
    if not artist_n or not title_n:
        return None
    for ext in AUDIO_EXTENSIONS:
        for p in track_dir.glob(f"*{ext}"):
            if " - " not in p.stem:
                continue
            f_artist, f_title = p.stem.split(" - ", 1)
            fa = _normalise_match_text(f_artist)
            ft = _normalise_match_text(f_title)
            artist_ok = fa and (artist_n in fa or fa in artist_n)
            title_ok = ft and (title_n in ft or ft in title_n)
            if artist_ok and title_ok:
                return p
    return None


def _find_playlist_file(playlist_name: str, artist: str, title: str, user_id: str | None = None) -> pathlib.Path | None:
    """Find a track's file inside Playlists/<playlist_name>/, or None if absent."""
    playlists_dir = get_playlists_dir(user_id=user_id)
    if not playlists_dir:
        return None
    return _find_track_in_dir(playlists_dir / sanitize_filename(playlist_name), artist, title)


def _locate_local_track_file(
    playlist_name: str,
    use_playlists_dir: bool,
    artist: str,
    title: str,
    job_artist: str = "",
    job_title: str = "",
    user_id: str | None = None,
    resolved_path: str | None = None,
    lidarr_cache: dict | None = None,
) -> tuple[bool, pathlib.Path | None]:
    """Resolve a local file for this watched track.

    Returns (found, local_path). found means "the track exists somewhere and should
    not be re-downloaded"; local_path is the actual on-disk file when we have one
    (so callers can store it and stamp COMMENT tags), or None when only Navidrome
    vouches for it (we can't tag a file we can't see).

    Checks the stored resolved_path first (covers tracks found in album folders or
    other non-Singles locations), then MusicGrabber's own library, then falls back
    to Navidrome and Lidarr. Synthetic relative paths still prove the track exists
    and block a duplicate download; absolute paths are retained for M3U rebuilding
    even when the media path is not mounted into this container.
    """
    # Fastest check: if we recorded exactly where the file landed, trust it
    if resolved_path:
        rp = pathlib.Path(resolved_path)
        if rp.is_absolute() and rp.exists():
            return True, rp

    pairs = []
    for a, t in ((job_artist, job_title), (artist, title)):
        a = (a or "").strip()
        t = (t or "").strip()
        if a and t and (a, t) not in pairs:
            pairs.append((a, t))

    for a, t in pairs:
        dupe = check_duplicate(a, t, user_id=user_id)
        if dupe:
            return True, dupe
        if use_playlists_dir:
            pf = _find_playlist_file(playlist_name, a, t, user_id=user_id)
            if pf:
                return True, pf

    # Search and download dedupe consult both services, so refresh must do the
    # same or a Lidarr-only track loops forever as "missing". An absolute path is
    # useful to an M3U even when MusicGrabber cannot stat it (different container
    # path namespace); a relative sentinel proves existence but is not writable.
    for a, t in pairs:
        external_path = check_navidrome_duplicate(a, t, user_id=user_id)
        if external_path is not None:
            usable_path = external_path if external_path.is_absolute() else None
            return True, usable_path

    for a, t in pairs:
        if lidarr_cache is None:
            external_path = check_lidarr_duplicate(a, t, user_id=user_id)
        else:
            external_path = check_lidarr_duplicate(
                a, t, user_id=user_id, cache=lidarr_cache
            )
        if external_path is not None:
            usable_path = external_path if external_path.is_absolute() else None
            return True, usable_path

    return False, None


def _heal_unresolved_watched_track(
    conn: sqlite3.Connection,
    playlist: dict,
    track_hash: str,
    row,
    fallback_artist: str = "",
    fallback_title: str = "",
    lidarr_cache: dict | None = None,
) -> bool:
    """Mark an unresolved watched row downloaded when its file already exists.

    This is shared by the current-upstream diff and append-history reconciliation.
    Append playlists retain tracks after they leave the upstream list, so limiting
    this check to the current upstream snapshot leaves those historical rows stuck
    as "missing" forever.
    """
    found, local_file, artist, title = _resolve_unresolved_watched_track(
        playlist,
        row,
        fallback_artist=fallback_artist,
        fallback_title=fallback_title,
        lidarr_cache=lidarr_cache,
    )
    if not found:
        return False

    _persist_healed_watched_track(
        conn, playlist, track_hash, artist, title, local_file
    )
    return True


def _resolve_unresolved_watched_track(
    playlist: dict,
    row,
    fallback_artist: str = "",
    fallback_title: str = "",
    lidarr_cache: dict | None = None,
) -> tuple[bool, pathlib.Path | None, str, str]:
    """Resolve one watched row without touching the caller's SQLite connection."""
    artist = row["artist"] or fallback_artist
    title = row["title"] or fallback_title
    found, local_file = _locate_local_track_file(
        playlist["name"],
        bool(playlist.get("use_playlists_dir", False)),
        artist,
        title,
        row["job_artist"] or "",
        row["job_title"] or "",
        user_id=playlist.get("user_id"),
        resolved_path=row["resolved_path"],
        lidarr_cache=lidarr_cache,
    )
    return found, local_file, artist, title


def _persist_healed_watched_track(
    conn: sqlite3.Connection,
    playlist: dict,
    track_hash: str,
    artist: str,
    title: str,
    local_file: pathlib.Path | None,
) -> None:
    """Persist a successful resolution on the refresh thread."""
    conn.execute(
        """UPDATE watched_playlist_tracks
           SET downloaded_at = datetime('now'),
               resolved_path = COALESCE(?, resolved_path)
           WHERE playlist_id = ? AND track_hash = ?""",
        (
            str(local_file) if local_file else None,
            playlist["id"],
            track_hash,
        ),
    )
    if local_file:
        _tag_track_comment(local_file, artist, title, conn=conn)


def _reconcile_append_history(
    conn: sqlite3.Connection,
    playlist: dict,
    tracked: dict,
    current_hashes: set[str],
    lidarr_cache: dict | None = None,
) -> int:
    """Heal unresolved append-only rows that are absent from today's snapshot.

    Historical tracks are deliberately not queued again after leaving the source
    playlist. They are still retained by append mode and shown in its track list,
    though, so refresh must reconcile them against the local/Navidrome/Lidarr
    libraries just like current tracks.
    """
    candidates = []
    history_cutoff = datetime.fromtimestamp(
        time.time() - (WATCHED_HISTORY_RECHECK_HOURS * 3600),
        tz=timezone.utc,
    ).strftime("%Y-%m-%d %H:%M:%S")
    for track_hash, row in tracked.items():
        row_keys = row.keys()
        library_checked_at = (
            row["library_checked_at"] if "library_checked_at" in row_keys else None
        )
        if (
            track_hash in current_hashes
            or row["downloaded_at"]
            # removed_at is deliberately NOT a reason to skip: every historical
            # row here carries one now that departures are recorded in both
            # modes, and skipping them would quietly retire this whole function.
            or row["job_status"] in ("queued", "downloading")
            or (
                library_checked_at
                and str(library_checked_at) >= history_cutoff
            )
        ):
            continue
        # sqlite3.Row is immutable but belongs to a connection used on this
        # thread. Copy it before handing lookup-only work to a worker.
        candidates.append((track_hash, dict(row)))

    def resolve_candidate(candidate):
        track_hash, row = candidate
        found, local_file, artist, title = _resolve_unresolved_watched_track(
            playlist, row, lidarr_cache=lidarr_cache
        )
        return track_hash, found, local_file, artist, title

    if len(candidates) <= 1:
        results = [resolve_candidate(item) for item in candidates]
    else:
        # External duplicate checks are independent HTTP calls. A small pool
        # keeps a long append history from making manual refresh feel hung while
        # avoiding a request burst against Navidrome or Lidarr.
        workers = min(6, len(candidates))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(resolve_candidate, candidates))

    healed = 0
    for track_hash, found, local_file, artist, title in results:
        conn.execute(
            """UPDATE watched_playlist_tracks
               SET library_checked_at = datetime('now')
               WHERE playlist_id = ? AND track_hash = ?""",
            (playlist["id"], track_hash),
        )
        if not found:
            continue
        _persist_healed_watched_track(
            conn, playlist, track_hash, artist, title, local_file
        )
        healed += 1
    return healed


def _count_unresolved_downloaded_history(
    conn: sqlite3.Connection,
    playlist: dict,
    tracked: dict,
    current_hashes: set[str],
    lidarr_cache: dict | None = None,
) -> int:
    """Count retained append-history downloads whose files have wandered off.

    These tracks are no longer upstream, so they must never be queued again.
    When the missing-file policy says to leave downloads alone, they still belong
    in the card's unresolved count because append mode deliberately retains them.
    """
    unresolved = 0
    for track_hash, row in tracked.items():
        if track_hash in current_hashes or not row["downloaded_at"]:
            continue
        found, local_file, artist, title = _resolve_unresolved_watched_track(
            playlist, row, lidarr_cache=lidarr_cache
        )
        if not found:
            unresolved += 1
            continue
        if local_file and str(local_file) != (row["resolved_path"] or ""):
            _persist_healed_watched_track(
                conn, playlist, track_hash, artist, title, local_file
            )
    return unresolved


def _playlist_track_dir(playlist: dict) -> pathlib.Path | None:
    """The on-disk folder a watched playlist's tracks land in, or None if it hasn't one."""
    custom = (playlist.get("custom_subdir") or "").strip()
    user_id = playlist.get("user_id")
    if custom:
        base = resolve_custom_subdir(custom, user_id=user_id)
    elif playlist.get("use_playlists_dir"):
        base = get_playlists_dir(user_id=user_id)
        if not base:
            return None
    else:
        return None
    return base / sanitize_playlist_name(playlist.get("name"), playlist.get("id"))


def find_orphaned_playlist_files(playlists: list[dict]) -> list[dict]:
    """Round up audio files in playlist folders that no playlist claims any more.

    Mirror-mode removals deliberately leave files on disk (we don't bin music just
    because a playlist changed its mind); this finds the leftovers so they can be
    rehomed. Only the top level of each current watched playlist's folder is
    scanned, and where several playlists share a folder (custom subdirs), a file
    claimed by any of them is safe. Folders of playlists no longer being watched
    at all are out of scope; we can't tell leftovers from keepsakes there.

    Append mode keeps its history on purpose, so a departed track there is still
    claimed and must never be offered up as an orphan. Only mirror mode disowns
    what has left the upstream playlist.
    """
    # Group playlists by folder so shared custom folders pool their claims
    folder_map: dict[pathlib.Path, list[dict]] = {}
    for pl in playlists:
        d = _playlist_track_dir(pl)
        if d and d.is_dir():
            folder_map.setdefault(d, []).append(pl)

    orphans: list[dict] = []
    for folder, pls in sorted(folder_map.items()):
        claimed: set[pathlib.Path] = set()
        for pl in pls:
            # Only mirror mode disowns a departed track; append keeps its history,
            # so those files are still claimed and must not be offered up.
            disowns_departures = (pl.get("sync_mode") or "append") == "mirror"
            with db_conn() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    f"""SELECT wpt.artist, wpt.title, wpt.resolved_path,
                              j.artist AS job_artist, j.title AS job_title
                       FROM watched_playlist_tracks wpt
                       LEFT JOIN jobs j ON j.id = wpt.job_id
                       WHERE wpt.playlist_id = ?
                       {"AND wpt.removed_at IS NULL" if disowns_departures else ""}""",
                    (pl["id"],),
                ).fetchall()
            for row in rows:
                if row["resolved_path"]:
                    rp = pathlib.Path(row["resolved_path"])
                    if rp.exists():
                        claimed.add(rp)
                for a, t in ((row["job_artist"], row["job_title"]), (row["artist"], row["title"])):
                    if a and t:
                        hit = _find_track_in_dir(folder, a, t)
                        if hit:
                            claimed.add(hit)

        playlist_names = ", ".join(sorted({p["name"] for p in pls}))
        for f in sorted(folder.iterdir()):
            if not f.is_file() or f.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            if f in claimed:
                continue
            try:
                size_mb = round(f.stat().st_size / (1024 * 1024), 1)
            except OSError:
                size_mb = None
            orphans.append({
                "file": str(f),
                "name": f.name,
                "playlist": playlist_names,
                "size_mb": size_mb,
            })
    return orphans


def move_orphans_to_singles(files: list[str], playlists: list[dict]) -> dict:
    """Move orphaned playlist files into the Singles layout, .lrc riding along.

    Only files sitting directly in a currently-watched playlist folder are
    eligible, so a creatively crafted path can't winkle files out of anywhere
    else. Artist comes from the file's tags, falling back to the filename's
    "Artist - Title" stem; nothing is ever overwritten at the destination.
    """
    from metadata import read_artist_title

    folder_owner: dict[pathlib.Path, str | None] = {}
    for pl in playlists:
        d = _playlist_track_dir(pl)
        if d:
            folder_owner[d.resolve()] = pl.get("user_id")

    moved: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    touched_users: set[str | None] = set()

    for raw in files:
        p = pathlib.Path(raw).resolve()
        parent = p.parent
        if parent not in folder_owner:
            skipped.append({"file": raw, "reason": "not in a watched playlist folder"})
            continue
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXTENSIONS:
            skipped.append({"file": raw, "reason": "not an audio file"})
            continue
        user_id = folder_owner[parent]

        artist = None
        try:
            artist, _ = read_artist_title(p)
        except Exception:
            pass
        if not artist and " - " in p.stem:
            artist = p.stem.split(" - ", 1)[0].strip()

        dest_dir = get_download_dir(artist or "Unknown Artist", user_id=user_id)
        dest = dest_dir / p.name
        if dest.exists():
            skipped.append({"file": raw, "reason": "already exists in Singles"})
            continue
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest))
            lrc = p.with_suffix(".lrc")
            if lrc.exists() and not dest.with_suffix(".lrc").exists():
                shutil.move(str(lrc), str(dest.with_suffix(".lrc")))
            moved.append({"file": raw, "to": str(dest)})
            touched_users.add(user_id)
        except Exception as e:
            errors.append({"file": raw, "reason": str(e)})

    if moved:
        from downloads import trigger_navidrome_scan, trigger_jellyfin_scan, trigger_plex_scan
        for uid in touched_users:
            trigger_navidrome_scan(user_id=uid)
            trigger_jellyfin_scan(user_id=uid)
            trigger_plex_scan(user_id=uid)

    return {"moved": moved, "skipped": skipped, "errors": errors}


def detect_playlist_platform(url: str) -> tuple[str, str]:
    """Detect platform and extract ID from playlist URL

    Returns (platform, id) or raises HTTPException if invalid
    """
    # Spotify liked songs (requires sp_dc cookie)
    if re.match(r'https?://open\.spotify\.com/collection/tracks', url):
        return "spotify_likes", "collection/tracks"

    # Spotify playlist
    spotify_playlist = re.match(r'https?://open\.spotify\.com/playlist/([a-zA-Z0-9]+)', url)
    if spotify_playlist:
        return "spotify", spotify_playlist.group(1)

    # Spotify album
    spotify_album = re.match(r'https?://open\.spotify\.com/album/([a-zA-Z0-9]+)', url)
    if spotify_album:
        return "spotify", spotify_album.group(1)

    # YouTube / YouTube Music  -  /playlist?list=... or /watch?v=...&list=... (Mixes, Radio, etc.)
    youtube_list = re.search(r'https?://(www\.|music\.)?youtube\.com/(?:playlist|watch)\?[^"]*list=([a-zA-Z0-9_-]+)', url)
    if youtube_list:
        return "youtube", youtube_list.group(2)

    # Apple Music playlist or album (public catalog, any storefront)
    apple_playlist = re.match(r'https?://music\.apple\.com/[a-z]{2}/(playlist|album)/', url, re.IGNORECASE)
    if apple_playlist:
        return "apple", url  # Full URL needed  -  storefront is part of the path

    # Apple Music private library playlist
    apple_library = re.match(r'https?://music\.apple\.com/library/(playlist|album)/', url, re.IGNORECASE)
    if apple_library:
        return "apple", url

    # Amazon Music playlist (user or curated, any regional TLD)
    amazon_playlist = re.match(r'https?://music\.amazon\.[a-z.]+/(user-playlists|playlists)/\S+', url)
    if amazon_playlist:
        return "amazon", url  # Full URL needed  -  no extractable ID

    # ListenBrainz individual playlist URL
    lb_playlist = re.match(r'https?://listenbrainz\.org/playlist/([0-9a-f-]{36})', url, re.IGNORECASE)
    if lb_playlist:
        return "listenbrainz", lb_playlist.group(1)

    # ListenBrainz user profile URL  -  triggers "Created for You" fan-out
    lb_user_url = re.match(r'https?://listenbrainz\.org/user/([a-zA-Z0-9_-]+)', url, re.IGNORECASE)
    if lb_user_url:
        return "listenbrainz_user", lb_user_url.group(1)

    # Bare ListenBrainz username (no protocol, no dots  -  just alphanumeric/underscore/hyphen)
    if re.match(r'^[a-zA-Z0-9_-]+$', url) and '.' not in url:
        return "listenbrainz_user", url

    # SoundCloud sets (user playlists) and likes
    soundcloud_sets = re.match(r'https?://soundcloud\.com/[^/]+/sets/[^/?]+', url, re.IGNORECASE)
    if soundcloud_sets:
        return "soundcloud", url

    soundcloud_likes = re.match(r'https?://soundcloud\.com/[^/]+/likes', url, re.IGNORECASE)
    if soundcloud_likes:
        return "soundcloud", url

    # Monochrome playlist shares are Tidal playlist UUIDs behind hifi-api, but
    # the public URL uses Monochrome's own host.
    monochrome_playlist = re.match(
        r'https?://(?:www\.)?(?:monochrome\.tf|monochrome\.samidy\.com)/playlist/([0-9a-f-]{36})',
        url,
        re.IGNORECASE,
    )
    if monochrome_playlist:
        return "monochrome", monochrome_playlist.group(1)

    # Tidal playlist (both tidal.com/browse/playlist/UUID and tidal.com/playlist/UUID)
    tidal_playlist = re.match(r'https?://tidal\.com/(?:browse/)?playlist/([0-9a-f-]{36})', url, re.IGNORECASE)
    if tidal_playlist:
        return "tidal", tidal_playlist.group(1)

    # Beatport Top 100, genre charts, and editorial/user charts
    beatport = re.match(
        r'https?://(?:www\.)?beatport\.com/(top-100|genre/[^/]+/\d+/top-100|chart/[^/]+/\d+)',
        url, re.IGNORECASE
    )
    if beatport:
        return "beatport", url

    raise HTTPException(
        status_code=400,
        detail="Invalid playlist URL. Supported: Spotify playlists/albums/liked songs, YouTube/YouTube Music playlists, Apple Music playlists/albums, Amazon Music playlists, ListenBrainz playlists or usernames, SoundCloud sets/likes, Tidal/Monochrome playlists, Beatport charts/Top 100."
    )


def _extract_sp_dc(cookies_text: str) -> str | None:
    """Extract the sp_dc session cookie value from a Netscape-format cookie string."""
    for raw_line in (cookies_text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and parts[5] == "sp_dc":
            return parts[6].strip()
    return None


def _flag_spotify_cookies_expired(user_id: str | None) -> None:
    """Mark Spotify cookies as expired in settings so the UI can warn the user."""
    from settings import set_setting, set_user_setting
    if user_id:
        set_user_setting(user_id, "spotify_cookies_expired", "true")
    else:
        set_setting("spotify_cookies_expired", "true")


def _fetch_spotify_playlist_embed(url: str, sp_dc: str | None = None, user_id: str | None = None) -> dict:
    """Fetch Spotify playlist tracks via embed endpoint.
    This is the fast path that works for playlists with <100 tracks.

    sp_dc is the Spotify session cookie. When provided it enables access to
    private playlists. Without it only public playlists are accessible.
    """
    # Extract ID and type from URL
    playlist_match = re.match(r'https?://open\.spotify\.com/playlist/([a-zA-Z0-9]+)', url)
    album_match = re.match(r'https?://open\.spotify\.com/album/([a-zA-Z0-9]+)', url)

    if playlist_match:
        spotify_id = playlist_match.group(1)
        spotify_type = "playlist"
    elif album_match:
        spotify_id = album_match.group(1)
        spotify_type = "album"
    else:
        raise HTTPException(status_code=400, detail="Invalid Spotify URL. Expected playlist or album URL.")

    # Build headers and cookies for the request.
    # sp_dc is the session credential that unlocks private content.
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    cookies = {"sp_dc": sp_dc} if sp_dc else {}

    # Fetch the embed page. Spotify's public playlist API is gone, so we scrape the
    # embed HTML which includes a predictable JSON-in-HTML "title"/"subtitle" pattern.
    # If this breaks, inspect the embed HTML for renamed fields or a new data blob.
    # Spotify's embed edge has a habit of throwing the odd transient 502/503/504
    # (gateway timeouts under load) or dropping the connection. One blip
    # shouldn't sink the whole fetch, so we retry a few times with a short
    # linear backoff. 401/403/404 are NOT transient (private/missing playlist),
    # so a definitive answer breaks out of the loop and is handled below.
    embed_url = f"https://open.spotify.com/embed/{spotify_type}/{spotify_id}"
    response = None
    last_error = None
    for attempt in range(SPOTIFY_EMBED_MAX_ATTEMPTS):
        try:
            with httpx.Client(timeout=TIMEOUT_HTTP_SPOTIFY, follow_redirects=True) as client:
                response = client.get(embed_url, headers=headers, cookies=cookies)
        except httpx.RequestError as e:
            response = None
            last_error = e
        else:
            if response.status_code not in (502, 503, 504):
                break  # definitive answer (200/401/403/404/...), stop retrying
            last_error = httpx.HTTPStatusError(
                f"transient {response.status_code} from Spotify embed",
                request=response.request, response=response,
            )
        if attempt < SPOTIFY_EMBED_MAX_ATTEMPTS - 1:
            time.sleep(SPOTIFY_EMBED_RETRY_BACKOFF * (attempt + 1))

    try:
        if response is None:
            # Every attempt hit a connection-level error; surface the last one.
            raise HTTPException(status_code=502, detail=f"Failed to connect to Spotify: {last_error}")
        if response.status_code in (401, 403):
            # Embed refused. Could be a private playlist or stale cookies.
            # The headless browser handles both cases more gracefully
            # when sp_dc is available, so try that before giving up.
            if sp_dc:
                print(
                    f"Spotify embed returned {response.status_code} for "
                    f"{spotify_type} {spotify_id}, trying headless browser..."
                )
                try:
                    return fetch_spotify_playlist_via_browser(
                        spotify_id, spotify_type, sp_dc=sp_dc, user_id=user_id,
                    )
                except HTTPException as browser_exc:
                    if browser_exc.detail == "spotify_cookies_expired":
                        _flag_spotify_cookies_expired(user_id)
                    raise
            raise HTTPException(status_code=403, detail=f"{spotify_type.title()} not found or is private")
        response.raise_for_status()
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail=f"{spotify_type.title()} not found or is private")
        raise HTTPException(status_code=502, detail=f"Failed to fetch {spotify_type}: {e}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Failed to connect to Spotify: {e}")

    html_content = response.text
    expected_total = None
    playlist_name = f"Spotify {spotify_type.title()}"
    tracks = []

    # Primary: parse the __NEXT_DATA__ JSON blob Spotify embeds in the page.
    # Previously we scraped this with regexes, but Spotify added nested arrays
    # (e.g. "contentRatings":{"labels":[]}) which broke the non-greedy trackList
    # regex. Proper JSON parsing handles any nesting depth.
    next_data_match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
        html_content,
        re.DOTALL,
    )
    if next_data_match:
        try:
            next_data = json.loads(next_data_match.group(1))
            entity = next_data["props"]["pageProps"]["state"]["data"]["entity"]
            playlist_name = entity.get("title") or entity.get("name") or playlist_name
            for track in entity.get("trackList", []):
                raw_title = track.get("title", "").strip()
                raw_artist = track.get("subtitle", "").strip()
                entity_type = track.get("entityType", "track")
                if not raw_title:
                    continue
                # Subtitle separator is sometimes a non-breaking space; normalise it
                raw_artist = raw_artist.replace(" ", " ")
                if raw_artist.lower() == "music video" or entity_type == "music_video":
                    from utils import extract_artist_title
                    artist, title = extract_artist_title(raw_title, channel="")
                    if artist and title and artist.lower() not in ("unknown artist", ""):
                        tracks.append(f"{artist} - {title}")
                    continue
                if raw_artist:
                    tracks.append(f"{raw_artist} - {raw_title}")
        except (KeyError, TypeError, json.JSONDecodeError):
            tracks = []

    # Fallback: old regex approach, kept in case Spotify ever drops __NEXT_DATA__
    if not tracks:
        print("Spotify __NEXT_DATA__ parse failed, falling back to regex scrape")
        tracklist_match = re.search(r'"trackList":\[(.+?)\](?=,"|\})', html_content, re.DOTALL)
        if tracklist_match:
            tracklist_content = tracklist_match.group(1)
            tl_titles = re.findall(r'"title":"([^"]*)"', tracklist_content)
            tl_subtitles = re.findall(r'"subtitle":"([^"]*)"', tracklist_content)
            tl_entity_types = re.findall(r'"entityType":"([^"]*)"', tracklist_content)
            for i, (raw_title, raw_artist) in enumerate(zip(tl_titles, tl_subtitles)):
                entity_type = tl_entity_types[i] if i < len(tl_entity_types) else "track"
                try:
                    raw_title = json.loads(f'"{raw_title}"')
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
                try:
                    raw_artist = json.loads(f'"{raw_artist}"')
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
                if not raw_title:
                    continue
                if raw_artist.lower() == "music video" or entity_type == "music_video":
                    from utils import extract_artist_title
                    artist, title = extract_artist_title(raw_title, channel="")
                    if artist and title and artist.lower() not in ("unknown artist", ""):
                        tracks.append(f"{artist} - {title}")
                    continue
                if raw_artist:
                    tracks.append(f"{raw_artist} - {raw_title}")

    if not tracks:
        # Embed parsed cleanly but yielded zero tracks. The browser path works
        # for public playlists without sp_dc, so always try it before declaring
        # the playlist empty. Private playlists still need sp_dc and will
        # surface the proper "cookies expired" error if they're missing.
        print(
            f"Spotify embed returned no tracks for {spotify_type} {spotify_id}, "
            "trying headless browser..."
        )
        try:
            return fetch_spotify_playlist_via_browser(
                spotify_id, spotify_type, expected_total=expected_total,
                sp_dc=sp_dc, user_id=user_id,
            )
        except HTTPException as browser_exc:
            if browser_exc.detail == "spotify_cookies_expired":
                _flag_spotify_cookies_expired(user_id)
            # If the browser also couldn't find any tracks, give the user the
            # original "page structure may have changed" message instead of
            # the browser-specific error  -  it's the more accurate diagnosis.
            if browser_exc.status_code == 422:
                raise HTTPException(
                    status_code=422,
                    detail=f"Could not extract tracks from {spotify_type}. It may be empty or Spotify's page structure may have changed."
                )
            raise

    # If at the embed limit, attempt the headless browser to get the full playlist.
    # The browser path works for public playlists without sp_dc; only private
    # playlists actually need the cookie.
    if len(tracks) >= 95:
        print(
            f"Spotify embed returned {len(tracks)} tracks (at limit), trying headless browser"
            + (" with sp_dc..." if sp_dc else " without sp_dc...")
        )
        browser_error = None
        try:
            browser_result = fetch_spotify_playlist_via_browser(
                spotify_id, spotify_type, expected_total=expected_total, sp_dc=sp_dc,
                user_id=user_id,
            )
            if browser_result["count"] > len(tracks):
                print(f"Headless browser returned {browser_result['count']} tracks (embed had {len(tracks)})")
                if expected_total and browser_result["count"] < expected_total:
                    browser_result = dict(browser_result)
                    browser_result["warning"] = (
                        f"Spotify reports {expected_total} items, browser extracted {browser_result['count']}. "
                        "Some tracks may still be missing."
                    )
                return browser_result
            # Browser confirmed same count as embed - playlist is probably complete
            print(f"Headless browser confirmed {browser_result['count']} tracks, playlist looks complete")
        except HTTPException as e:
            browser_error = e.detail
            print(f"Headless browser failed ({e.detail}), using embed results")
        except Exception as e:
            browser_error = str(e)
            print(f"Headless browser error: {e}, using embed results")

        if browser_error:
            expected_note = f" (Spotify reports {expected_total})" if expected_total else ""
            return {
                "tracks": tracks,
                "playlist_name": playlist_name,
                "count": len(tracks),
                "warning": (
                    f"Only the first {len(tracks)} tracks were fetched{expected_note}. "
                    f"Headless browser failed: {browser_error}. "
                    "Check that shm_size: '2gb' is set in docker-compose.yml."
                ),
            }

    return {
        "tracks": tracks,
        "playlist_name": playlist_name,
        "count": len(tracks)
    }


def _parse_listenbrainz_jspf_tracks(jspf_playlist: dict) -> list[tuple[str, str]]:
    """Extract (artist, title) pairs from a JSPF playlist dict.

    JSPF uses 'creator' for artist and 'title' for track name. Tracks with
    either field missing are skipped -- a song with no name is no song at all.
    """
    tracks = []
    for track in jspf_playlist.get("track", []):
        artist = (track.get("creator") or "").strip()
        title = (track.get("title") or "").strip()
        if artist and title:
            tracks.append((artist, title))
    return tracks


def fetch_listenbrainz_createdfor(username: str) -> list[dict]:
    """Fetch all 'Created for You' playlists for a ListenBrainz user.

    Returns a list of dicts, each with:
        playlist_url  -- stable JSPF URL for the individual playlist
        playlist_uuid -- UUID extracted from the URL
        name          -- playlist title from LB
        tracks        -- list of (artist, title) tuples

    Raises HTTPException on error.
    """
    url = f"{LISTENBRAINZ_API_URL}/1/user/{username}/playlists/createdfor"
    try:
        with httpx.Client(timeout=TIMEOUT_LISTENBRAINZ) as client:
            resp = client.get(url, headers={"Accept": "application/json"})
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail=f"ListenBrainz user '{username}' not found")
            resp.raise_for_status()
    except HTTPException:
        raise
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Failed to connect to ListenBrainz: {e}")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"ListenBrainz API error: {e.response.status_code}")

    data = resp.json()
    raw_playlists = data.get("playlists", [])

    if not raw_playlists:
        raise HTTPException(
            status_code=422,
            detail=f"No 'Created for You' playlists found for '{username}'. "
                   "ListenBrainz generates these weekly  -  check back after your account has some listening history."
        )

    results = []
    for entry in raw_playlists:
        playlist = entry.get("playlist", {})
        name = playlist.get("title", "ListenBrainz Playlist").strip()
        # The identifier is a URL like https://listenbrainz.org/playlist/UUID/
        identifier = playlist.get("identifier", "")
        uuid_match = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', identifier, re.IGNORECASE)
        if not uuid_match:
            print(f"ListenBrainz: skipping playlist '{name}'  -  no UUID in identifier '{identifier}'")
            continue
        playlist_uuid = uuid_match.group(1)
        # Store the canonical per-playlist URL (without trailing slash for consistency)
        playlist_url = f"https://listenbrainz.org/playlist/{playlist_uuid}"
        # The listing endpoint always returns track:[]; tracks only exist on the
        # per-playlist JSPF endpoint, so we have to fetch each one individually.
        try:
            tracks, _ = _fetch_listenbrainz_playlist(playlist_uuid)
        except HTTPException as e:
            print(f"ListenBrainz: skipping playlist '{name}' ({playlist_uuid}): {e.detail}")
            continue
        results.append({
            "playlist_url": playlist_url,
            "playlist_uuid": playlist_uuid,
            "name": name,
            "tracks": tracks,
        })

    return results


def _fetch_listenbrainz_playlist(playlist_uuid: str) -> tuple[list[tuple[str, str]], str]:
    """Fetch a single ListenBrainz playlist by UUID via the JSPF API.

    Used during the regular refresh cycle for individual LB playlists.
    Returns (list of (artist, title) tuples, playlist_name).
    """
    url = f"{LISTENBRAINZ_API_URL}/1/playlist/{playlist_uuid}"
    try:
        with httpx.Client(timeout=TIMEOUT_LISTENBRAINZ_PLAYLIST) as client:
            resp = client.get(url, headers={"Accept": "application/json"})
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail="ListenBrainz playlist not found (may have been rotated)")
            resp.raise_for_status()
    except HTTPException:
        raise
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Failed to connect to ListenBrainz: {e}")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"ListenBrainz API error: {e.response.status_code}")

    data = resp.json()
    playlist = data.get("playlist", {})
    name = playlist.get("title", "ListenBrainz Playlist").strip()
    tracks = _parse_listenbrainz_jspf_tracks(playlist)

    if not tracks:
        raise HTTPException(status_code=422, detail="No tracks found in ListenBrainz playlist")

    print(f"Fetched {len(tracks)} tracks from ListenBrainz playlist '{name}'")
    return tracks, name


def _fetch_soundcloud_playlist(url: str) -> tuple[list[tuple[str, str]], str]:
    """Fetch tracks from a SoundCloud set or likes URL via yt-dlp.

    Unlike YouTube, SoundCloud's flat-playlist mode returns stub entries with
    no title or uploader. --dump-single-json fetches the full playlist object
    including complete track metadata in one shot.
    """
    cmd = [
        "yt-dlp",
        "--dump-single-json",
        "--no-warnings",
        url,
    ]
    result, timed_out = run_ytdlp_with_retries(
        cmd,
        TIMEOUT_YTDLP_PLAYLIST,
        allow_cookie_fallback=False,
        operation="SoundCloud playlist metadata lookup",
    )
    if timed_out:
        raise HTTPException(status_code=504, detail="Timeout fetching SoundCloud playlist")

    # yt-dlp may exit non-zero if individual tracks are geo-restricted, but
    # still return valid playlist JSON on stdout.  Parse first, fail later.
    try:
        data = json.loads(result.stdout.strip()) if result is not None else {}
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=502, detail="Failed to fetch SoundCloud playlist")

    playlist_name = data.get("title") or "SoundCloud Playlist"
    entries = data.get("entries") or []

    tracks = []
    seen: set[tuple[str, str]] = set()

    for entry in entries:
        if not entry or not entry.get("id"):
            continue
        title = entry.get("title") or "Unknown"
        channel = entry.get("uploader") or entry.get("channel") or "Unknown"
        artist, clean_title = extract_artist_title(title, channel)
        key = (artist.lower(), clean_title.lower())
        if key not in seen:
            seen.add(key)
            tracks.append((artist, clean_title))

    if not tracks:
        raise HTTPException(status_code=422, detail="No tracks found in SoundCloud playlist")

    return tracks, playlist_name


def fetch_playlist_tracks(url: str, platform: str, user_id: str | None = None) -> tuple[list[tuple[str, str]], str, str | None, list[str | None]]:
    """Fetch tracks from a playlist URL

    Returns tracks, playlist name, warning, and aligned original YouTube IDs.
    """
    if platform == "spotify_likes":
        spotify_cookies_text = get_setting("spotify_cookies", "", user_id=user_id)
        sp_dc = _extract_sp_dc(spotify_cookies_text) if spotify_cookies_text.strip() else None
        if not sp_dc:
            raise HTTPException(
                status_code=401,
                detail="Spotify liked songs requires an sp_dc cookie. Add your Spotify cookies in Settings."
            )
        result = fetch_spotify_playlist_via_browser(
            "tracks", "collection", sp_dc=sp_dc, user_id=user_id
        )
        tracks = []
        for track_str in result["tracks"]:
            if " - " in track_str:
                artist, title = track_str.split(" - ", 1)
                tracks.append((artist.strip(), title.strip()))
            else:
                tracks.append(("Unknown", track_str.strip()))
        return tracks, result["playlist_name"], result.get("warning"), [None] * len(tracks)

    if platform == "spotify":
        spotify_cookies_text = get_setting("spotify_cookies", "", user_id=user_id)
        sp_dc = _extract_sp_dc(spotify_cookies_text) if spotify_cookies_text.strip() else None
        result = _fetch_spotify_playlist_embed(url, sp_dc=sp_dc, user_id=user_id)

        # Parse "Artist - Title" format back to tuples
        tracks = []
        for track_str in result["tracks"]:
            if " - " in track_str:
                artist, title = track_str.split(" - ", 1)
                tracks.append((artist.strip(), title.strip()))
            else:
                tracks.append(("Unknown", track_str.strip()))

        return tracks, result["playlist_name"], result.get("warning"), [None] * len(tracks)

    elif platform == "youtube":
        # Use yt-dlp to get playlist info
        m = re.search(r'list=([a-zA-Z0-9_-]+)', url)
        if not m:
            raise HTTPException(status_code=400, detail="Invalid YouTube playlist URL: no list= parameter found")
        playlist_id = m.group(1)

        # Mix/Radio playlists (RD prefix) only work when seeded with the original watch URL  - 
        # YouTube refuses the bare /playlist?list=RD... form. Pass the URL as-is in that case.
        # Personalised playlists keep their original URL too, music.youtube.com host and all,
        # because rewriting them to www.youtube.com is one more way to lose the account context.
        personalised = is_personalised_youtube_playlist(url, playlist_id)
        if playlist_id.startswith("RD") or personalised:
            ytdlp_url = url
        else:
            ytdlp_url = f"https://www.youtube.com/playlist?list={playlist_id}"

        # An account-personalised playlist has no meaningful anonymous answer: YouTube
        # will happily hand back somebody else's recommendations with a straight face.
        # Refuse to guess rather than quietly importing a stranger's taste.
        if personalised and not resolve_cookie_file(user_id, allow_global_fallback=False):
            raise HTTPException(
                status_code=401,
                detail=(
                    "This is a personalised YouTube Music playlist and needs your YouTube "
                    "cookies to enumerate. Add them in Settings, then refresh."
                ),
            )

        info_cmd = [
            "yt-dlp",
            *_ytdlp_base_args(user_id=user_id, require_cookies=personalised),
            "--dump-json",
            "--flat-playlist",
            "--no-warnings",
            ytdlp_url
        ]

        result, timed_out = run_ytdlp_with_retries(
            info_cmd,
            TIMEOUT_YTDLP_PLAYLIST,
            # Never drop cookies and retry anonymously for a personalised playlist.
            # A cookieless retry "succeeds", which is the whole problem: we would
            # enqueue generic recommendations and mark them as the user's playlist.
            allow_cookie_fallback=not personalised,
            operation=f"YouTube playlist metadata lookup for {playlist_id}",
        )
        if timed_out:
            raise HTTPException(status_code=504, detail="Timeout fetching YouTube playlist")

        if result is None or result.returncode != 0:
            # A bad/missing/private playlist is the caller's mistake (404), not an
            # upstream gateway failure (502). yt-dlp tells us which in stderr: a
            # dud or malformed list id makes YouTube's API answer "400 Bad
            # Request", a deleted/private one says so outright. Anything else
            # (timeouts, transient network) stays a 502.
            stderr = result.stderr if result is not None else ""
            stderr_lc = stderr.lower()
            # Personalised playlists are the one case where "private" and friends
            # mean "your session was not accepted", not "this playlist is gone".
            # Say so plainly instead of sending the user off to check a URL that
            # is perfectly fine.
            if personalised:
                raise HTTPException(
                    status_code=401,
                    detail=(
                        "YouTube would not enumerate this personalised playlist with your "
                        "cookies. They may have expired or belong to a different account; "
                        "re-export them in Settings. Nothing has been queued or changed."
                    ),
                )
            client_error_signs = (
                "does not exist", "unavailable", "private", "has been removed",
                "this playlist does not exist or is private",
                "http error 400", "bad request",
                "not a valid url", "incomplete youtube id",
            )
            if (
                classify_ytdlp_failure(stderr) == "missing"
                or any(s in stderr_lc for s in client_error_signs)
            ):
                raise HTTPException(status_code=404, detail="YouTube playlist not found (it may have been deleted, made private, or the URL is wrong)")
            raise HTTPException(status_code=502, detail="Failed to fetch YouTube playlist")

        tracks = []
        original_video_ids = []
        playlist_name = "YouTube Playlist"

        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            try:
                data = json.loads(line)
                # First entry often has playlist title
                if data.get("playlist_title") and playlist_name == "YouTube Playlist":
                    playlist_name = data["playlist_title"]

                if data.get("id"):
                    title = data.get("title", "Unknown")
                    channel = data.get("channel", data.get("uploader", "Unknown"))
                    artist, clean_title_val = extract_artist_title(title, channel)
                    tracks.append((artist, clean_title_val))
                    original_video_ids.append(data["id"])
            except json.JSONDecodeError:
                continue

        if not tracks:
            raise HTTPException(status_code=422, detail="No tracks found in YouTube playlist")

        return tracks, playlist_name, None, original_video_ids

    elif platform == "apple":
        music_user_token = get_setting("apple_music_user_token", "", user_id=user_id) or None
        result = fetch_apple_music_playlist(url, music_user_token=music_user_token)

        tracks = []
        for track_str in result["tracks"]:
            if " - " in track_str:
                artist, title = track_str.split(" - ", 1)
                tracks.append((artist.strip(), title.strip()))
            else:
                tracks.append(("Unknown", track_str.strip()))

        return tracks, result["playlist_name"], result.get("warning"), [None] * len(tracks)

    elif platform == "amazon":
        result = fetch_amazon_playlist(url)

        tracks = []
        for track_str in result["tracks"]:
            if " - " in track_str:
                artist, title = track_str.split(" - ", 1)
                tracks.append((artist.strip(), title.strip()))
            else:
                tracks.append(("Unknown", track_str.strip()))

        return tracks, result["playlist_name"], None, [None] * len(tracks)

    elif platform == "listenbrainz":
        # Single LB playlist by UUID  -  regular refresh path
        m = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', url, re.IGNORECASE)
        if not m:
            raise HTTPException(status_code=400, detail="Invalid ListenBrainz playlist URL: no UUID found")
        tracks, name = _fetch_listenbrainz_playlist(m.group(1))
        return tracks, name, None, [None] * len(tracks)

    elif platform == "listenbrainz_user":
        # Username URLs are handled at the add-watched level (fan-out to multiple playlists).
        # If we ever reach here at refresh time something has gone wrong.
        raise HTTPException(
            status_code=400,
            detail="ListenBrainz user URLs are only valid when adding a watched playlist. Individual playlist URLs are stored for refresh."
        )

    elif platform == "soundcloud":
        tracks, name = _fetch_soundcloud_playlist(url)
        return tracks, name, None, [None] * len(tracks)

    elif platform in ("tidal", "monochrome"):
        # A Monochrome playlist share link is a Tidal playlist UUID wearing
        # Monochrome's hostname; the same embed-page scrape resolves both,
        # so there's no need for hifi-api (which increasingly means "down").
        m = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', url, re.IGNORECASE)
        if not m:
            raise HTTPException(status_code=400, detail="Invalid Tidal/Monochrome playlist URL: no UUID found")
        result = fetch_tidal_playlist(m.group(1))
        tracks = []
        for track_str in result["tracks"]:
            if " - " in track_str:
                artist, title = track_str.split(" - ", 1)
                tracks.append((artist.strip(), title.strip()))
            else:
                tracks.append(("Unknown", track_str.strip()))
        return tracks, result["playlist_name"], None, [None] * len(tracks)

    elif platform == "beatport":
        result = fetch_beatport_playlist(url)
        tracks = []
        for track_str in result["tracks"]:
            if " - " in track_str:
                artist, title = track_str.split(" - ", 1)
                tracks.append((artist.strip(), title.strip()))
            else:
                tracks.append(("Unknown", track_str.strip()))
        return tracks, result["playlist_name"], None, [None] * len(tracks)

    raise HTTPException(status_code=400, detail=f"Unsupported platform: {platform}")


def refresh_watched_playlist(playlist_id: str) -> dict:
    """Fetch playlist and queue any new tracks for download

    Returns dict with refresh results
    """
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        playlist = conn.execute(
            "SELECT * FROM watched_playlists WHERE id = ?", (playlist_id,)
        ).fetchone()

        if not playlist:
            return {"error": "Playlist not found", "playlist_id": playlist_id}

        playlist = dict(playlist)
        sync_mode = playlist.get("sync_mode", "append")
        user_id = playlist.get("user_id")

        # Acquire an atomic per-playlist refresh lock.
        # If a stale "running" state is older than WATCHED_REFRESH_STALE_SECONDS,
        # this update will take over and start a fresh run.
        lock_cursor = conn.execute(
            """UPDATE watched_playlists
               SET refresh_state = 'running',
                   refresh_stage = 'starting',
                   refresh_started_at = datetime('now'),
                   refresh_completed_at = NULL,
                   refresh_error = NULL,
                   refresh_import_id = NULL
               WHERE id = ?
               AND (
                   refresh_state IS NULL
                   OR refresh_state != 'running'
                   OR refresh_started_at IS NULL
                   OR refresh_started_at < datetime('now', '-' || ? || ' seconds')
               )""",
            (playlist_id, str(WATCHED_REFRESH_STALE_SECONDS))
        )
        conn.commit()

        if lock_cursor.rowcount == 0:
            running_state = conn.execute(
                "SELECT refresh_stage, refresh_started_at FROM watched_playlists WHERE id = ?",
                (playlist_id,)
            ).fetchone()
            return {
                "playlist_id": playlist_id,
                "name": playlist["name"],
                "already_running": True,
                "message": "Refresh already in progress",
                "refresh_stage": running_state["refresh_stage"] if running_state else None,
                "refresh_started_at": running_state["refresh_started_at"] if running_state else None,
            }

        def set_refresh_stage(stage: str) -> None:
            conn.execute(
                """UPDATE watched_playlists
                   SET refresh_state = 'running',
                       refresh_stage = ?,
                       refresh_error = NULL
                   WHERE id = ?""",
                (stage, playlist_id)
            )
            conn.commit()

        def finish_refresh_success(import_id: str | None) -> None:
            # A clean fetch means the playlist is alive and well, so wipe any
            # accumulated "gone" strikes and clear a stale auto-pause note.
            conn.execute(
                """UPDATE watched_playlists
                   SET refresh_state = 'idle',
                       refresh_stage = 'done',
                       refresh_error = NULL,
                       refresh_import_id = ?,
                       refresh_completed_at = datetime('now'),
                       gone_strikes = 0,
                       auto_paused = 0,
                       pause_reason = NULL
                   WHERE id = ?""",
                (import_id, playlist_id)
            )
            conn.commit()

        def finish_refresh_error(error_msg: str) -> None:
            conn.execute(
                """UPDATE watched_playlists
                   SET refresh_state = 'error',
                       refresh_stage = 'failed',
                       refresh_error = ?,
                       refresh_import_id = NULL,
                       refresh_completed_at = datetime('now')
                   WHERE id = ?""",
                ((error_msg or "Refresh failed")[:800], playlist_id)
            )
            conn.commit()

        try:
            # Fetch current tracks
            set_refresh_stage("fetching")
            # ListenBrainz "Created for You" playlists rotate every Monday; check the date stamp in
            # the playlist name ("week of YYYY-MM-DD") and proactively re-resolve if it's ≥6 days old.
            # Also re-resolves reactively on 404 in case the name date wasn't parseable.

            # Self-heal: playlists added before lb_username was introduced have NULL stored.
            # Extract the username from the name ("Weekly Exploration for USERNAME, week of ...") and
            # persist it so the re-resolution logic can do its job.
            if playlist["platform"] == "listenbrainz" and not playlist.get("lb_username"):
                _name_for_heal = playlist.get("name") or ""
                _heal_m = re.search(r'.+ for ([^,]+), week of \d{4}-\d{2}-\d{2}', _name_for_heal)
                if _heal_m:
                    _healed_username = _heal_m.group(1).strip()
                    conn.execute("UPDATE watched_playlists SET lb_username = ? WHERE id = ?",
                                 (_healed_username, playlist_id))
                    conn.commit()
                    playlist["lb_username"] = _healed_username
                    print(f"[lb] Self-healed lb_username='{_healed_username}' for '{_name_for_heal}'")

            if playlist["platform"] == "listenbrainz" and playlist.get("lb_username"):
                playlist_name = playlist["name"] or ""

                def _lb_needs_reresolution() -> bool:
                    m = re.search(r'week of (\d{4}-\d{2}-\d{2})', playlist_name)
                    if not m:
                        return False
                    try:
                        week_date = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        age_days = (datetime.now(timezone.utc) - week_date).days
                        return age_days >= 6
                    except ValueError:
                        return False

                def _lb_playlist_week_date(name: str) -> datetime:
                    m = re.search(r'week of (\d{4}-\d{2}-\d{2})', name or "")
                    if not m:
                        return datetime.min.replace(tzinfo=timezone.utc)
                    try:
                        return datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    except ValueError:
                        return datetime.min.replace(tzinfo=timezone.utc)

                def _lb_reresolution_fetch(prefer_latest: bool = False) -> list:
                    """Re-query createdfor API, update stored URL, return tracks."""
                    print(f"ListenBrainz playlist '{playlist_name}' appears stale, re-resolving via createdfor API")
                    lb_playlists = fetch_listenbrainz_createdfor(playlist["lb_username"])
                    name_prefix = playlist_name.split(", week of")[0]
                    same_family = [p for p in lb_playlists if p["name"].startswith(name_prefix)]
                    if prefer_latest and same_family:
                        matched = max(same_family, key=lambda p: _lb_playlist_week_date(p["name"]))
                    else:
                        # For 404 self-healing, exact match is fine. For stale
                        # weekly playlists, exact match is the old playlist and
                        # must not win over the newer playlist with the same prefix.
                        matched = next((p for p in lb_playlists if p["name"] == playlist_name), None)
                        if not matched and same_family:
                            matched = max(same_family, key=lambda p: _lb_playlist_week_date(p["name"]))
                    if not matched:
                        raise HTTPException(
                            status_code=404,
                            detail=f"ListenBrainz playlist '{playlist_name}' not found in current createdfor list for '{playlist['lb_username']}'"
                        )
                    new_url = matched["playlist_url"]
                    if new_url != playlist["url"]:
                        conn.execute("UPDATE watched_playlists SET url = ?, name = ? WHERE id = ?",
                                     (new_url, matched["name"], playlist_id))
                        conn.commit()
                        playlist["url"] = new_url
                        playlist["name"] = matched["name"]
                        print(f"Updated ListenBrainz URL for '{matched['name']}' to {new_url}")
                    return matched["tracks"]

                if _lb_needs_reresolution():
                    tracks = _lb_reresolution_fetch(prefer_latest=True)
                    original_video_ids = [None] * len(tracks)
                else:
                    try:
                        fetched = fetch_playlist_tracks(playlist["url"], playlist["platform"], user_id=user_id)
                        tracks = fetched[0]
                        original_video_ids = fetched[3] if len(fetched) > 3 else [None] * len(tracks)
                    except HTTPException as e:
                        if e.status_code != 404:
                            raise
                        tracks = _lb_reresolution_fetch(prefer_latest=False)
                        original_video_ids = [None] * len(tracks)
            else:
                fetched = fetch_playlist_tracks(playlist["url"], playlist["platform"], user_id=user_id)
                tracks = fetched[0]
                original_video_ids = fetched[3] if len(fetched) > 3 else [None] * len(tracks)

            # Build a set of hashes for what the upstream playlist currently contains
            current_hashes = {hash_track(artist, title) for artist, title in tracks}

            # Load existing track state (including job status and removal flag)
            set_refresh_stage("diffing")
            track_rows = conn.execute(
                """SELECT wpt.track_hash, wpt.downloaded_at,
                          wpt.library_checked_at, wpt.job_id, wpt.removed_at,
                          wpt.artist, wpt.title, wpt.resolved_path, j.status as job_status,
                          j.artist as job_artist, j.title as job_title
                   FROM watched_playlist_tracks wpt
                   LEFT JOIN jobs j ON wpt.job_id = j.id
                   WHERE wpt.playlist_id = ?""",
                (playlist_id,)
            ).fetchall()
            tracked = {row["track_hash"]: row for row in track_rows}

            new_tracks = []
            missing_tracks = []
            removed_count = 0
            healed_history_count = 0
            unresolved_count = 0
            lidarr_refresh_cache = {}
            # When this is off, a track downloaded once stays downloaded even if
            # we can no longer find the file. Stops the merry-go-round where an
            # external organiser moves files and we re-download them forever.
            global_redownload_missing = get_setting_bool("redownload_missing_files", True)
            playlist_redownload_missing = playlist.get("redownload_missing_files")
            redownload_missing = (
                global_redownload_missing
                if playlist_redownload_missing is None
                else bool(playlist_redownload_missing)
            )

            for position, (artist, title) in enumerate(tracks):
                track_hash = hash_track(artist, title)
                # Always keep positions current so M3U reflects upstream order
                conn.execute(
                    "UPDATE watched_playlist_tracks SET position = ? WHERE playlist_id = ? AND track_hash = ?",
                    (position, playlist_id, track_hash)
                )
                existing = tracked.get(track_hash)
                if not existing:
                    new_tracks.append((artist, title, track_hash))
                    continue

                # Track has reappeared upstream  -  clear the removal flag. Recorded
                # for both sync modes: removed_at is a fact about the upstream
                # playlist, not a mirror-mode opinion about what to do about it.
                if existing["removed_at"]:
                    conn.execute(
                        "UPDATE watched_playlist_tracks SET removed_at = NULL WHERE playlist_id = ? AND track_hash = ?",
                        (playlist_id, track_hash)
                    )

                if existing["downloaded_at"]:
                    # File was deleted manually after being marked downloaded.
                    # If we cannot resolve it locally anymore, treat it as missing and re-queue.
                    found, local_file = _locate_local_track_file(
                        playlist["name"],
                        bool(playlist.get("use_playlists_dir", False)),
                        existing["artist"] or artist,
                        existing["title"] or title,
                        existing["job_artist"] or "",
                        existing["job_title"] or "",
                        user_id=user_id,
                        resolved_path=existing["resolved_path"],
                    )
                    if not found:
                        if not redownload_missing:
                            # Downloaded once, and that's that. The file has gone
                            # walkabout somewhere we cannot see; we note it and
                            # leave well alone rather than fetching a duplicate.
                            unresolved_count += 1
                            continue
                        conn.execute(
                            "UPDATE watched_playlist_tracks SET downloaded_at = NULL WHERE playlist_id = ? AND track_hash = ?",
                            (playlist_id, track_hash)
                        )
                        missing_tracks.append((artist, title, track_hash))
                        continue
                    if local_file:
                        # Heal the record while we're here: remember where the file
                        # actually lives, and make sure its COMMENT tag carries the
                        # playlist names (duplicate-skipped tracks used to miss out).
                        if str(local_file) != (existing["resolved_path"] or ""):
                            conn.execute(
                                "UPDATE watched_playlist_tracks SET resolved_path = ? WHERE playlist_id = ? AND track_hash = ?",
                                (str(local_file), playlist_id, track_hash)
                            )
                        _tag_track_comment(local_file, existing["artist"] or artist, existing["title"] or title, conn=conn)
                    continue

                job_status = existing["job_status"]
                if job_status == "completed":
                    conn.execute(
                        "UPDATE watched_playlist_tracks SET downloaded_at = datetime('now') WHERE playlist_id = ? AND track_hash = ?",
                        (playlist_id, track_hash)
                    )
                    continue

                if job_status in ("queued", "downloading"):
                    continue

                # Job failed (or never ran), but check if the file landed on disk anyway
                # (e.g. a manual download, or a previous sync via a different playlist).
                if _heal_unresolved_watched_track(
                    conn, playlist, track_hash, existing,
                    fallback_artist=artist, fallback_title=title,
                    lidarr_cache=lidarr_refresh_cache,
                ):
                    continue

                missing_tracks.append((artist, title, track_hash))

            # Append mode intentionally keeps tracks after they leave the upstream
            # playlist. Reconcile unresolved historical rows too, otherwise a track
            # discovered later by Navidrome/Lidarr remains permanently "missing" in
            # the UI and never becomes eligible for the append-mode M3U.
            if sync_mode == "append":
                set_refresh_stage("reconciling_history")
                healed_history_count = _reconcile_append_history(
                    conn, playlist, tracked, current_hashes,
                    lidarr_cache=lidarr_refresh_cache,
                )
                if healed_history_count:
                    print(
                        f"Watched playlist '{playlist['name']}': "
                        f"healed {healed_history_count} historical track(s) from the local library"
                    )
                if not redownload_missing:
                    unresolved_count += _count_unresolved_downloaded_history(
                        conn, playlist, tracked, current_hashes,
                        lidarr_cache=lidarr_refresh_cache,
                    )

            # Mark any tracked tracks that are no longer in the upstream playlist.
            # Both modes record it, because "this track left the playlist" is a
            # fact and the counts on the card need it; what each mode *does* about
            # it still differs. Mirror drops them from the M3U, append keeps them
            # (see rebuild_watched_playlist_m3u), and neither ever deletes audio.
            for track_hash, row in tracked.items():
                if track_hash not in current_hashes and not row["removed_at"]:
                    conn.execute(
                        "UPDATE watched_playlist_tracks SET removed_at = datetime('now') WHERE playlist_id = ? AND track_hash = ?",
                        (playlist_id, track_hash)
                    )
                    removed_count += 1

            if removed_count:
                _fate = "dropped from the M3U" if sync_mode == "mirror" else "kept"
                print(
                    f"Watched playlist '{playlist['name']}' ({sync_mode}): "
                    f"{removed_count} track(s) no longer upstream, {_fate}"
                )

            # Insert any new tracks so they are tracked before download
            # Build a position lookup from the current upstream order
            track_positions = {hash_track(a, t): i for i, (a, t) in enumerate(tracks)}
            original_video_by_hash = {
                hash_track(artist, title): original_video_ids[index]
                for index, (artist, title) in enumerate(tracks)
            }
            for artist, title, track_hash in new_tracks:
                conn.execute("""
                    INSERT INTO watched_playlist_tracks
                    (playlist_id, track_hash, artist, title, position, original_youtube_video_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (playlist_id, track_hash, artist, title, track_positions.get(track_hash), original_video_by_hash.get(track_hash)))

            tracks_to_import = [(artist, title) for artist, title, _ in new_tracks + missing_tracks]
            use_playlists_dir = bool(playlist.get("use_playlists_dir", False))
            custom_subdir = (playlist.get("custom_subdir") or "").strip() or None
            import_id = None
            if tracks_to_import:
                set_refresh_stage("queueing")
                import_id = start_bulk_import_for_tracks(
                    tracks_to_import,
                    bool(playlist["convert_audio"]),
                    watch_playlist_id=playlist_id,
                    use_playlists_dir=use_playlists_dir,
                    user_id=user_id,
                    preferred_sources=playlist.get("preferred_sources") or "all",
                    priority_source=playlist.get("priority_source"),
                    custom_subdir=custom_subdir,
                    original_youtube_video_ids=[
                        original_video_by_hash.get(track_hash)
                        for _, _, track_hash in new_tracks + missing_tracks
                    ],
                )

            # Update playlist metadata
            set_refresh_stage("finalizing")
            conn.execute("""
                UPDATE watched_playlists
                SET last_checked = datetime('now'), last_track_count = ?,
                    unresolved_tracks = ?
                WHERE id = ?
            """, (len(tracks), unresolved_count, playlist_id))

            conn.commit()

            # Rebuild M3U from all tracks downloaded so far (new ones are still queued,
            # so they'll appear next refresh once marked downloaded)
            if playlist.get("make_m3u"):
                set_refresh_stage("rebuilding_m3u")
                rebuild_watched_playlist_m3u(
                    playlist_id, playlist["name"],
                    use_playlists_dir=use_playlists_dir,
                    sync_mode=sync_mode,
                    user_id=user_id,
                    custom_subdir=custom_subdir,
                )

            if unresolved_count:
                print(
                    f"Watched playlist '{playlist['name']}': {unresolved_count} downloaded track(s) "
                    f"no longer findable on disk, left as-is (re-download of missing files is off)"
                )

            queued_count = len(tracks_to_import)
            if queued_count:
                print(
                    f"Watched playlist '{playlist['name']}': {len(new_tracks)} new tracks, "
                    f"{len(missing_tracks)} missing tracks, {queued_count} queued"
                )

            finish_refresh_success(import_id)
            return {
                "playlist_id": playlist_id,
                "name": playlist["name"],
                "total_tracks": len(tracks),
                "new_tracks": len(new_tracks),
                "missing_tracks": len(missing_tracks),
                "healed_history_tracks": healed_history_count,
                "unresolved_tracks": unresolved_count,
                "removed_tracks": removed_count,
                "queued": queued_count,
                "import_id": import_id,
                "refresh_state": "idle",
                "refresh_stage": "done",
                "jobs": []
            }

        except HTTPException as e:
            err_msg = str(e.detail)
            # Spotify cookie expiry deserves a human-readable message and a notification
            if err_msg == "spotify_cookies_expired":
                err_msg = "Spotify cookies have expired. Update them in Settings to resume this playlist."
                try:
                    from notifications import send_notification
                    send_notification(
                        "error", playlist["name"], status="failed",
                        error=err_msg, playlist_name=playlist["name"],
                        user_id=user_id,
                    )
                except Exception:
                    pass
            conn.execute(
                "UPDATE watched_playlists SET last_checked = datetime('now') WHERE id = ?",
                (playlist_id,)
            )
            conn.commit()

            # A 404 means the upstream playlist came back "not found": it's been
            # deleted, made private, or the login token for that platform expired.
            # We can't tell which from one fetch, so we count consecutive strikes
            # and only auto-pause once it's clearly not a one-off blip. Pausing
            # (not deleting) leaves the user free to fix the source and Resume.
            auto_paused = False
            if e.status_code == 404:
                new_strikes = (playlist.get("gone_strikes") or 0) + 1
                if new_strikes >= WATCHED_GONE_STRIKES_BEFORE_PAUSE:
                    auto_paused = True
                    pause_note = (
                        f"Auto-paused after {new_strikes} consecutive 'not found' checks, "
                        "so this playlist looks deleted or made private upstream (or the login "
                        "token for that platform has expired). Check the source and your cookies, "
                        "then Resume to retry."
                    )
                    conn.execute(
                        """UPDATE watched_playlists
                           SET enabled = 0, auto_paused = 1, pause_reason = ?, gone_strikes = ?
                           WHERE id = ?""",
                        (pause_note, new_strikes, playlist_id)
                    )
                    conn.commit()
                    err_msg = pause_note
                    try:
                        from notifications import send_notification
                        send_notification(
                            "error", playlist["name"], status="failed",
                            error=pause_note, playlist_name=playlist["name"],
                            user_id=user_id,
                        )
                    except Exception:
                        pass
                    print(f"Watched playlist '{playlist['name']}' auto-paused: {pause_note}")
                else:
                    conn.execute(
                        "UPDATE watched_playlists SET gone_strikes = ? WHERE id = ?",
                        (new_strikes, playlist_id)
                    )
                    conn.commit()
                    print(
                        f"Watched playlist '{playlist['name']}' returned 'not found' "
                        f"(strike {new_strikes}/{WATCHED_GONE_STRIKES_BEFORE_PAUSE})"
                    )

            finish_refresh_error(err_msg)
            return {
                "playlist_id": playlist_id,
                "name": playlist["name"],
                "error": err_msg,
                "auto_paused": auto_paused,
                "refresh_state": "error",
                "refresh_stage": "failed",
            }
        except Exception as e:
            err_msg = str(e)
            conn.execute(
                "UPDATE watched_playlists SET last_checked = datetime('now') WHERE id = ?",
                (playlist_id,)
            )
            conn.commit()
            finish_refresh_error(err_msg)
            return {
                "playlist_id": playlist_id,
                "name": playlist["name"],
                "error": err_msg,
                "refresh_state": "error",
                "refresh_stage": "failed",
            }


# =============================================================================
# Background Scheduler for Watched Playlists
# =============================================================================

_scheduler_running = False
_scheduler_lock = threading.Lock()
_scheduler_wake_event = threading.Event()


def wake_scheduler():
    """Wake the scheduler so a changed playlist deadline is applied immediately."""
    _scheduler_wake_event.set()


def _seconds_until_next_playlist_check() -> float:
    """Return the bounded delay until the earliest enabled playlist is due.

    ``WATCHED_PLAYLIST_CHECK_HOURS`` remains the maximum sweep interval (and its
    zero value still disables scheduling), but a playlist with a shorter interval
    must wake the scheduler sooner.  SQLite owns the timestamp arithmetic here so
    it exactly matches the due-query below, including fractional-hour intervals.
    """
    maximum = max(60.0, WATCHED_PLAYLIST_CHECK_HOURS * 3600.0)
    with db_conn() as conn:
        row = conn.execute("""
            SELECT MIN(
                CASE
                    WHEN last_checked IS NULL THEN 0.0
                    ELSE MAX(
                        0.0,
                        (julianday(last_checked, '+' || refresh_interval_hours || ' hours')
                         - julianday('now')) * 86400.0
                    )
                END
            ) AS seconds_until_due
            FROM watched_playlists
            WHERE enabled = 1
        """).fetchone()

    next_due = row[0] if row else None
    if next_due is None:
        return maximum
    # One second avoids spinning at the exact SQLite timestamp boundary, where
    # the due predicate intentionally remains strictly less-than.
    return min(maximum, max(1.0, float(next_due) + 1.0))


def watched_playlist_scheduler():
    """Background thread that refreshes each playlist when its own deadline is due."""
    print(
        "Watched playlist scheduler started "
        f"(maximum sweep interval {WATCHED_PLAYLIST_CHECK_HOURS} hours)"
    )

    # Brief delay to let the app fully initialise, then check immediately
    time.sleep(10)
    print("Scheduler: Running initial check for overdue playlists...")

    while _scheduler_running:
        # Clear before inspecting the database. If an API mutation arrives during
        # the query/refresh pass it sets the event again and the wait below returns
        # immediately, so no changed deadline is lost.
        _scheduler_wake_event.clear()
        try:
            # Run the check
            print("Scheduler: Checking watched playlists...")
            with db_conn() as conn:
                conn.row_factory = sqlite3.Row

                playlists = conn.execute("""
                    SELECT id, name FROM watched_playlists
                    WHERE enabled = 1
                    AND (last_checked IS NULL
                         OR datetime(last_checked, '+' || refresh_interval_hours || ' hours') < datetime('now'))
                """).fetchall()

            if playlists:
                print(f"Scheduler: Found {len(playlists)} playlists due for refresh")
                total_new = 0
                for playlist in playlists:
                    result = refresh_watched_playlist(playlist["id"])
                    total_new += result.get("new_tracks", 0)
                print(f"Scheduler: Checked {len(playlists)} playlists, {total_new} new tracks found")
            else:
                print("Scheduler: No playlists due for refresh")

        except Exception as e:
            print(f"Scheduler error: {e}")

        if not _scheduler_running:
            break

        try:
            sleep_seconds = _seconds_until_next_playlist_check()
        except Exception as e:
            print(f"Scheduler deadline error: {e}")
            sleep_seconds = max(60.0, WATCHED_PLAYLIST_CHECK_HOURS * 3600.0)

        print(f"Scheduler: Next deadline check in {sleep_seconds / 60:.1f} minutes")
        _scheduler_wake_event.wait(timeout=sleep_seconds)


def start_scheduler():
    """Start the background scheduler if not already running"""
    global _scheduler_running

    if WATCHED_PLAYLIST_CHECK_HOURS <= 0:
        print("Watched playlist scheduler disabled (WATCHED_PLAYLIST_CHECK_HOURS=0)")
        return

    with _scheduler_lock:
        if _scheduler_running:
            return
        _scheduler_running = True

    spawn_daemon_thread(watched_playlist_scheduler)
