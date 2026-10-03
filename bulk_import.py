"""
MusicGrabber - Bulk Import Logic

Line cleaning, import job creation, and background worker.
"""

import re
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from constants import (
    BULK_IMPORT_SEARCH_ATTEMPTS,
    BULK_IMPORT_SEARCH_DELAY,
    BULK_IMPORT_SEARCH_RETRY_DELAY,
    BULK_IMPORT_SEARCH_RETRY_MAX_DELAY,
)
from db import db_conn, upsert_album_track_lock
from downloads import process_acquisition_cycle, create_bulk_playlist
from acquisition import (
    acquisition_candidate_summary,
    begin_acquisition_cycle,
    ensure_acquisition_target,
    finish_acquisition_cycle,
    rank_automatic_candidates,
)
from notifications import send_notification
from search import search_all_cached, log_ranked_results
from settings import get_setting_int
from utils import hash_track, spawn_daemon_thread

# Limits concurrent downloads spawned by bulk imports to avoid overwhelming
# YouTube with simultaneous requests and starving the DB connection pool.
_download_pool = None
_download_pool_size = 0
TRACKS_PER_IMPORT = 2


def _get_download_pool() -> ThreadPoolExecutor:
    """Return the download pool, recreating it if the configured size changed."""
    global _download_pool, _download_pool_size
    wanted = max(1, min(get_setting_int("max_concurrent_downloads", 3), 10))
    if _download_pool is None or wanted != _download_pool_size:
        if _download_pool is not None:
            _download_pool.shutdown(wait=False)
        _download_pool = ThreadPoolExecutor(max_workers=wanted)
        _download_pool_size = wanted
    return _download_pool


def _cancel_imports_with_conn(
    conn: sqlite3.Connection,
    *,
    import_id: str | None = None,
    watch_playlist_id: str | None = None,
) -> dict:
    """Request cancellation inside the caller's transaction."""
    if not import_id and not watch_playlist_id:
        raise ValueError("import_id or watch_playlist_id is required")
    selector = "id = ?" if import_id else "watch_playlist_id = ?"
    value = import_id or watch_playlist_id
    rows = conn.execute(
        f"""SELECT id FROM bulk_imports
            WHERE {selector}
              AND status IN ('pending', 'processing', 'cancelling')""",
        (value,),
    ).fetchall()
    import_ids = [row[0] for row in rows]
    if not import_ids:
        return {"imports": 0, "tracks": 0, "jobs": 0, "active": 0}

    placeholders = ",".join("?" * len(import_ids))
    conn.execute(
        f"""UPDATE bulk_imports
            SET cancel_requested = 1,
                status = 'cancelling',
                error = 'Cancellation requested; active downloads may finish',
                progress_at = datetime('now')
            WHERE id IN ({placeholders})""",
        import_ids,
    )

    # Work that has not started is cancelled immediately. Queue rows remain as
    # history; only their lifecycle state changes.
    job_cursor = conn.execute(
        f"""UPDATE jobs
            SET status = 'cancelled',
                error = 'Cancelled before download started',
                progress_stage = NULL,
                completed_at = datetime('now')
            WHERE status = 'queued'
              AND id IN (
                  SELECT job_id FROM bulk_import_tracks
                  WHERE import_id IN ({placeholders}) AND job_id IS NOT NULL
              )""",
        import_ids,
    )
    track_cursor = conn.execute(
        f"""UPDATE bulk_import_tracks
            SET status = 'cancelled', error = 'Cancelled before download started'
            WHERE import_id IN ({placeholders})
              AND (
                  status IN ('pending', 'searching')
                  OR (status = 'queued' AND job_id IN (
                      SELECT id FROM jobs WHERE status = 'cancelled'
                  ))
              )""",
        import_ids,
    )
    conn.execute(
        f"""UPDATE acquisition_attempts
            SET status = 'cancelled', error = 'Cancelled before download started',
                completed_at = datetime('now')
            WHERE status = 'attempting'
              AND job_id IN (
                  SELECT bit.job_id FROM bulk_import_tracks bit
                  JOIN jobs j ON j.id = bit.job_id
                  WHERE bit.import_id IN ({placeholders})
                    AND j.status = 'cancelled'
              )""",
        import_ids,
    )
    conn.execute(
        f"""UPDATE acquisition_targets
            SET status = 'cancelled', last_error = 'Cancelled before download started',
                updated_at = datetime('now')
            WHERE id IN (
                SELECT acquisition_target_id FROM bulk_import_tracks
                WHERE import_id IN ({placeholders})
                  AND status = 'cancelled'
                  AND acquisition_target_id IS NOT NULL
            )""",
        import_ids,
    )
    active = conn.execute(
        f"""SELECT COUNT(*)
            FROM bulk_import_tracks bit
            JOIN jobs j ON j.id = bit.job_id
            WHERE bit.import_id IN ({placeholders}) AND j.status = 'downloading'""",
        import_ids,
    ).fetchone()[0]
    if not active:
        conn.execute(
            f"""UPDATE bulk_imports
                SET status = 'cancelled', completed_at = datetime('now'),
                    error = 'Cancelled; completed files and Queue history retained'
                WHERE id IN ({placeholders})""",
            import_ids,
        )
    return {
        "imports": len(import_ids),
        "tracks": track_cursor.rowcount,
        "jobs": job_cursor.rowcount,
        "active": active,
    }


def request_bulk_import_cancellation(
    import_id: str | None = None,
    *,
    watch_playlist_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Cancel untouched import work while allowing active files to finish."""
    if conn is not None:
        return _cancel_imports_with_conn(
            conn, import_id=import_id, watch_playlist_id=watch_playlist_id
        )
    with db_conn() as owned_conn:
        result = _cancel_imports_with_conn(
            owned_conn, import_id=import_id, watch_playlist_id=watch_playlist_id
        )
        owned_conn.commit()
        return result


def _normalise_candidate_match_text(text: str) -> str:
    """Normalise text for loose artist matching in search candidates."""
    t = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def _candidate_mentions_expected_artist(candidate: dict, expected_artist: str) -> bool:
    """Return True when candidate title/channel appears to include expected artist.

    For multi-artist credits like 'OGUZ, Nyctonian', passes if ANY of the
    comma-separated artists is mentioned. Requiring the full combined string
    rejects otherwise good results from sources that only show a primary artist.
    """
    if not expected_artist:
        return False

    title_norm = _normalise_candidate_match_text(candidate.get("title", ""))
    channel_norm = _normalise_candidate_match_text(candidate.get("channel", ""))
    combined = f"{title_norm} {channel_norm}".strip()

    # Split comma-separated multi-artist credits and check each one separately.
    # 'OGUZ, Nyctonian' becomes ['OGUZ', 'Nyctonian']; single-artist strings
    # become a one-element list, so the behaviour is identical for the common case.
    artists = [a.strip() for a in expected_artist.split(",") if a.strip()]
    for artist in artists:
        artist_norm = _normalise_candidate_match_text(artist)
        if not artist_norm:
            continue
        if artist_norm in combined:
            return True
        # Fallback: all significant tokens must appear for multi-word artist names.
        tokens = [t for t in artist_norm.split() if len(t) > 1]
        if len(tokens) > 1 and all(t in combined for t in tokens):
            return True

    return False


def _candidate_looks_like_cover(candidate: dict) -> bool:
    """Return True for obvious cover/tribute/karaoke style uploads."""
    combined = _normalise_candidate_match_text(
        f"{candidate.get('title', '')} {candidate.get('channel', '')}"
    )
    markers = (
        "cover",
        "tribute",
        "karaoke",
        "instrumental",
        "for piano",
        "piano version",
    )
    return any(marker in combined for marker in markers)


def _candidate_channel_matches_expected_artist(candidate: dict, expected_artist: str) -> bool:
    """Strict artist-channel match used for album-mode imports."""
    expected_norm = _normalise_candidate_match_text(expected_artist)
    if not expected_norm:
        return False

    channel_norm = _normalise_candidate_match_text(candidate.get("channel", ""))
    if not channel_norm:
        return False

    if channel_norm == expected_norm:
        return True

    allowed_suffixes = {"topic", "official", "music", "records", "channel"}
    if channel_norm.startswith(f"{expected_norm} "):
        suffix_tokens = channel_norm[len(expected_norm):].strip().split()
        if suffix_tokens and all(tok in allowed_suffixes for tok in suffix_tokens):
            return True

    compact_suffixes = ("vevo", "official")
    return any(channel_norm == f"{expected_norm}{suffix}" for suffix in compact_suffixes)


def clean_bulk_import_line(line: str) -> str:
    """Clean a line from bulk import text

    Removes common prefixes like:
    - Numbers: "1.", "1)", "01."
    - Bullets: "•", "-", "*"
    - Comments: "#"
    - Extra whitespace and tabs
    """
    # Strip whitespace
    line = line.strip()

    # Skip comments
    if line.startswith('#'):
        return ""

    # Remove common list prefixes: "1. ", "1) ", "01. ", etc.
    line = re.sub(r'^\d+[\.\)]\s*', '', line)

    # Remove bullet points at start
    line = re.sub(r'^[•\-\*]\s*', '', line)

    # Remove common music symbols
    line = re.sub(r'[♫♪🎵🎶]', '', line)

    # Normalise multiple spaces/tabs to single space
    line = re.sub(r'\s+', ' ', line)

    return line.strip()


def start_bulk_import_for_tracks(
    tracks: list[tuple[str, str]],
    convert_audio: bool,
    watch_playlist_id: Optional[str] = None,
    use_playlists_dir: bool = False,
    watch_artist_id: Optional[str] = None,
    user_id: Optional[str] = None,
    preferred_sources: Optional[str] = None,
    override_dir: Optional[str] = None,
    album_release_mbid: Optional[str] = None,
    album_total_tracks: Optional[int] = None,
    album_year: Optional[str] = None,
    custom_subdir: Optional[str] = None,
    priority_source: Optional[str] = None,
    *,
    track_isrcs: Optional[list[str | None]] = None,
    original_youtube_video_ids: Optional[list[str | None]] = None,
) -> str:
    """Create a bulk import job from a list of (artist, title) tuples.

    priority_source, when set, breaks ties inside the same quality tier. It does
    not promote a lossy source ahead of a confident allowed lossless result.

    track_isrcs, when provided, is a per-track list aligned by index with
    tracks; the matching ISRC is stored against each row so the worker can try
    a precise ISRC-first lookup before falling back to free-text search. Only
    the Albums tab passes this; every other caller leaves it None and each row
    gets a NULL isrc, behaving exactly as before.
    """
    import_id = str(uuid.uuid4())[:8]

    # Normalise priority_source: empty string and "any" both mean "no preference".
    _priority = (priority_source or "").strip().lower() or None
    if _priority in ("any", "all", "none"):
        _priority = None

    with db_conn() as conn:
        conn.execute(
            """INSERT INTO bulk_imports
               (id, status, total_tracks, create_playlist, playlist_name, convert_audio,
                watch_playlist_id, use_playlists_dir, watch_artist_id, user_id, preferred_sources,
                override_dir, album_release_mbid, album_total_tracks, album_year, custom_subdir,
                priority_source)
               VALUES (?, 'pending', ?, 0, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (import_id, len(tracks), int(convert_audio), watch_playlist_id,
             int(use_playlists_dir), watch_artist_id, user_id, preferred_sources or "all",
             override_dir, album_release_mbid, album_total_tracks, album_year or None,
             custom_subdir or None, _priority)
        )

        for line_num, (artist, song) in enumerate(tracks, 1):
            # Guard the index in case track_isrcs is shorter than tracks; a
            # missing entry just means "no ISRC", same as not passing the list.
            _isrc = (
                track_isrcs[line_num - 1]
                if track_isrcs and line_num - 1 < len(track_isrcs)
                else None
            )
            _original_video_id = (
                original_youtube_video_ids[line_num - 1]
                if original_youtube_video_ids and line_num - 1 < len(original_youtube_video_ids)
                else None
            )
            track_hash = hash_track(artist, song)
            if watch_playlist_id:
                owner_type = "watched_playlist"
                owner_key = f"{watch_playlist_id}:{track_hash}"
            elif watch_artist_id:
                owner_type = "watched_artist"
                owner_key = f"{watch_artist_id}:{track_hash}"
            elif override_dir:
                owner_type = "album"
                owner_key = f"{import_id}:{line_num}"
            else:
                owner_type = "bulk_import"
                owner_key = f"{import_id}:{line_num}"
            target_id = ensure_acquisition_target(
                owner_type=owner_type,
                owner_key=owner_key,
                mode="automatic",
                artist=artist,
                title=song,
                user_id=user_id,
                allowed_sources=preferred_sources or "all",
                priority_source=_priority,
                convert_audio=convert_audio,
                isrc=_isrc,
                destination={
                    "watch_playlist_id": watch_playlist_id,
                    "watch_artist_id": watch_artist_id,
                    "use_playlists_dir": bool(use_playlists_dir),
                    "custom_subdir": custom_subdir,
                    "override_dir": override_dir,
                    "album_release_mbid": album_release_mbid,
                },
                conn=conn,
            )
            conn.execute(
                """INSERT INTO bulk_import_tracks
                   (import_id, line_num, artist, song, status, isrc,
                    original_youtube_video_id, acquisition_target_id)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (import_id, line_num, artist, song, _isrc, _original_video_id, target_id)
            )

        conn.commit()

    spawn_daemon_thread(process_bulk_import_worker, import_id)

    return import_id


def _resolve_album_up_front(
    import_id: str,
    override_dir: str,
    allowed_sources: list[str] | None = None,
) -> dict[int, dict]:
    """Pin an album import's tracks to a single release before the loop starts.

    Searching for each track on its own is how "Fin." off the 2023 album ends up
    being "Fin." off the 2025 one: same artist, same title, and nothing in a
    free-text query to separate them. Matching the album as a whole settles that
    once, and spares us N chances of catching a provider mid-wobble.

    Returns {bulk_import_tracks.id: result} for whatever matched. Anything absent
    takes the ordinary per-track route, so a complete miss costs only the lookup.
    """
    # Album resolution is a Monochrome capability. A strict source selection
    # must apply before this shortcut as well as during the ordinary search.
    if allowed_sources is not None and "monochrome" not in allowed_sources:
        return {}

    try:
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT id, artist, song FROM bulk_import_tracks WHERE import_id = ? ORDER BY line_num",
                (import_id,),
            ).fetchall()
    except Exception as exc:
        print(f"Bulk import {import_id}: could not read tracks for album resolution: {exc}")
        return {}
    if not rows:
        return {}

    artist = next((r[1] for r in rows if (r[1] or "").strip()), "")
    # The folder was named after the MusicBrainz release title, and the matcher
    # strips punctuation anyway, so sanitisation does no harm on the way back out.
    album_title = Path(override_dir).name
    if not (artist and album_title):
        return {}

    try:
        from monochrome import resolve_album_tracks
        resolved = resolve_album_tracks(artist, album_title, [r[2] or "" for r in rows])
    except Exception as exc:
        print(f"Bulk import {import_id}: album resolution failed: {exc}")
        return {}

    return {rows[idx][0]: result for idx, result in resolved.items()}


def process_bulk_import_worker(import_id: str):
    """Background worker processing up to two import tracks concurrently.

    Searches all available sources in parallel
    via search_all() and picks the best result by relevance score.
    """
    # Load import details
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM bulk_imports WHERE id = ?", (import_id,))
        import_row = cursor.fetchone()
        if not import_row:
            return
        if bool(import_row["cancel_requested"]):
            conn.execute(
                """UPDATE bulk_imports
                   SET status = 'cancelled', completed_at = datetime('now'),
                       error = 'Cancelled; completed files and Queue history retained'
                   WHERE id = ?""",
                (import_id,),
            )
            conn.commit()
            return

        convert_audio = bool(import_row["convert_audio"])
        create_playlist = bool(import_row["create_playlist"])
        playlist_name = import_row["playlist_name"]
        watch_playlist_id = import_row["watch_playlist_id"]
        watch_artist_id = import_row["watch_artist_id"]
        use_playlists_dir = bool(import_row["use_playlists_dir"])
        user_id = import_row["user_id"]
        override_dir = import_row["override_dir"]  # absolute path string or None
        custom_subdir = (import_row["custom_subdir"] or "").strip() or None
        album_release_mbid = (import_row["album_release_mbid"] or "").strip() or None
        _preferred_sources_raw = import_row["preferred_sources"] or "all"
        # Parse "youtube,soundcloud" into ["youtube", "soundcloud"], or None for "all"
        preferred_sources_list = (
            None if _preferred_sources_raw == "all"
            else [s.strip() for s in _preferred_sources_raw.split(",") if s.strip()]
        )
        # priority_source is a same-quality tie-breaker. It never promotes a
        # lower-quality candidate ahead of an allowed confident lossless one.
        try:
            _priority_source = import_row["priority_source"]
        except (IndexError, KeyError):
            _priority_source = None  # Pre-migration row, column missing
        priority_source = (_priority_source or "").strip().lower() or None

        # For watched playlist imports, playlist_name is stored as NULL in bulk_imports.
        # Fetch the actual name from watched_playlists so folder routing works correctly.
        if (use_playlists_dir or custom_subdir) and not playlist_name and watch_playlist_id:
            row = conn.execute(
                "SELECT name FROM watched_playlists WHERE id = ?", (watch_playlist_id,)
            ).fetchone()
            if row:
                playlist_name = row["name"]

        # Start the heartbeat now, so a worker that dies before its first track
        # is judged from when it actually began rather than when it was queued.
        conn.execute(
            "UPDATE bulk_imports SET status = 'processing', progress_at = datetime('now') WHERE id = ?",
            (import_id,)
        )
        conn.commit()

    base_delay = BULK_IMPORT_SEARCH_DELAY

    # Album imports get one go at matching the whole release before we start
    # picking tracks off individually. Empty dict is a perfectly normal answer.
    album_picks: dict[int, dict] = {}
    if override_dir and album_release_mbid:
        album_picks = _resolve_album_up_front(
            import_id, override_dir, preferred_sources_list
        )

    active_downloads = {}

    def reap_downloads(wait_for_one: bool) -> None:
        """Persist completed futures, optionally waiting for one to finish."""
        completed = []
        while active_downloads:
            completed = [future for future in active_downloads if future.done()]
            if completed:
                break
            if not wait_for_one:
                return
            with db_conn() as heartbeat_conn:
                heartbeat_conn.execute(
                    "UPDATE bulk_imports SET progress_at = datetime('now') WHERE id = ?",
                    (import_id,),
                )
                heartbeat_conn.commit()
            time.sleep(0.5)

        for future in completed:
            context = active_downloads.pop(future)
            track_id = context["track_id"]
            job_id = context["job_id"]
            target_id = context["target_id"]
            try:
                future.result()
            except Exception as exc:
                # Keep one broken worker leg from abandoning the other active
                # track or the untouched remainder of the import.
                with db_conn() as conn:
                    conn.execute(
                        "UPDATE jobs SET status = 'failed', error = ?, completed_at = datetime('now') WHERE id = ?",
                        (str(exc)[:500], job_id),
                    )
                    conn.commit()
                try:
                    finish_acquisition_cycle(target_id, "failed", str(exc))
                except Exception as ledger_error:
                    print(
                        f"Bulk import {import_id}: failed to close acquisition target "
                        f"{target_id}: {ledger_error}"
                    )

            with db_conn() as conn:
                job_state = conn.execute(
                    "SELECT status, error FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                state = job_state[0] if job_state else "failed"
                error = job_state[1] if job_state else "Download job disappeared"
                track_state = (
                    "completed"
                    if state in ("completed", "completed_with_errors")
                    else "cancelled" if state == "cancelled" else "failed"
                )
                conn.execute(
                    "UPDATE bulk_import_tracks SET status = ?, error = ? WHERE id = ?",
                    (track_state, error, track_id),
                )
                conn.execute(
                    "UPDATE bulk_imports SET progress_at = datetime('now') WHERE id = ?",
                    (import_id,),
                )
                conn.commit()

    try:
        while True:
            reap_downloads(wait_for_one=False)
            if len(active_downloads) >= TRACKS_PER_IMPORT:
                reap_downloads(wait_for_one=True)
                continue

            # Get next pending track
            with db_conn() as conn:
                conn.row_factory = sqlite3.Row
                import_state = conn.execute(
                    "SELECT cancel_requested FROM bulk_imports WHERE id = ?",
                    (import_id,),
                ).fetchone()
                if not import_state or bool(import_state["cancel_requested"]):
                    track = None
                    cancelled = True
                else:
                    cancelled = False
                cursor = conn.execute(
                    "SELECT * FROM bulk_import_tracks WHERE import_id = ? AND status = 'pending' ORDER BY line_num LIMIT 1",
                    (import_id,)
                )
                track = None if cancelled else cursor.fetchone()
                # Materialise before releasing connection
                track = dict(track) if track else None

            if not track:
                if active_downloads:
                    reap_downloads(wait_for_one=True)
                    continue
                break

            track_id = track["id"]
            artist = track["artist"]
            song = track["song"]
            target_id = track.get("acquisition_target_id")
            if not target_id:
                track_hash = hash_track(artist, song)
                if watch_playlist_id:
                    owner_type = "watched_playlist"
                    owner_key = f"{watch_playlist_id}:{track_hash}"
                elif watch_artist_id:
                    owner_type = "watched_artist"
                    owner_key = f"{watch_artist_id}:{track_hash}"
                elif override_dir:
                    owner_type = "album"
                    owner_key = f"{import_id}:{track.get('line_num') or track_id}"
                else:
                    owner_type = "bulk_import"
                    owner_key = f"{import_id}:{track.get('line_num') or track_id}"
                target_id = ensure_acquisition_target(
                    owner_type=owner_type,
                    owner_key=owner_key,
                    mode="automatic",
                    artist=artist,
                    title=song,
                    user_id=user_id,
                    allowed_sources=_preferred_sources_raw,
                    priority_source=priority_source,
                    convert_audio=convert_audio,
                    isrc=track.get("isrc"),
                    destination={
                        "watch_playlist_id": watch_playlist_id,
                        "watch_artist_id": watch_artist_id,
                        "use_playlists_dir": use_playlists_dir,
                        "custom_subdir": custom_subdir,
                        "override_dir": override_dir,
                        "album_release_mbid": album_release_mbid,
                    },
                )
            acquisition_cycle = begin_acquisition_cycle(target_id)

            with db_conn() as conn:
                conn.execute(
                    """UPDATE bulk_import_tracks
                       SET status = 'searching', acquisition_target_id = ?, acquisition_cycle = ?
                       WHERE id = ?""",
                    (target_id, acquisition_cycle, track_id),
                )
                conn.commit()

            # Search preferred (or all) sources in parallel, ranked by relevance score
            try:
                search_query = f"{artist} - {song}"

                # Layer 0: the album pick, settled before the loop began. Already
                # tied to one release, which is the only reliable way to tell two
                # identically-titled tracks on different albums apart.
                best_match = album_picks.get(track_id)

                # Layer 1: ISRC-first. If the Albums tab handed us an ISRC for this
                # track, ask Monochrome for that exact studio recording. A hit pins
                # one recording, so a live take cannot sneak through; we then bypass
                # the free-text search and its filters entirely. A miss (or no ISRC)
                # leaves best_match None and the normal free-text leg runs below.
                isrc = (track.get("isrc") or "").strip()
                if (
                    not best_match
                    and isrc
                    and (
                        preferred_sources_list is None
                        or "monochrome" in preferred_sources_list
                    )
                ):
                    from monochrome import resolve_by_isrc
                    best_match = resolve_by_isrc(isrc, artist, song)

                if best_match:
                    # Precise pick: the decision log only needs the query for context.
                    search_results = [best_match]
                    log_ranked_results(f"Bulk import {import_id}", search_query, search_results)
                else:
                    # An empty result is far more often a wobble than a verdict:
                    # a second import holding a provider's admission slot, a proxy
                    # having a moment, someone rate-limiting us. Since a track
                    # marked 'failed' is never looked at again, give it a few goes
                    # with a widening pause before writing it off.
                    search_results = []
                    search_status: dict = {}
                    for attempt in range(1, BULK_IMPORT_SEARCH_ATTEMPTS + 1):
                        search_results, _ = search_all_cached(
                            search_query,
                            limit=10,
                            sources=preferred_sources_list,
                            include_soulseek=True,
                            status_out=search_status,
                            return_all_source_results=True,
                        )
                        if search_results or attempt >= BULK_IMPORT_SEARCH_ATTEMPTS:
                            break
                        pause = min(
                            BULK_IMPORT_SEARCH_RETRY_DELAY * (2 ** (attempt - 1)),
                            BULK_IMPORT_SEARCH_RETRY_MAX_DELAY,
                        )
                        print(
                            f"Bulk import {import_id}: nothing for '{search_query}' "
                            f"(attempt {attempt}/{BULK_IMPORT_SEARCH_ATTEMPTS}), "
                            f"retrying in {pause:.0f}s"
                        )
                        time.sleep(pause)

                    # Automatic acquisition is quality-led. Identity is gated
                    # first; only then do lossless candidates lead progressively
                    # worse lossy tiers. A preferred source breaks ties inside a
                    # tier instead of vaulting a lossy result over lossless.
                    search_results, rejected_results = rank_automatic_candidates(
                        search_results,
                        artist or "",
                        song or "",
                        priority_source=priority_source,
                    )

                    log_ranked_results(f"Bulk import {import_id}", search_query, search_results)

                    if not search_results:
                        # Say which of the two happened. "No results found" for a
                        # source we never actually got to ask is how thirteen
                        # tracks were written off as missing while sitting on
                        # Monochrome the whole time.
                        stuck = search_status.get("busy") or []
                        stalled = search_status.get("timeout") or []
                        if stuck or stalled:
                            unreachable = ", ".join(sorted(set(stuck) | set(stalled)))
                            failure_reason = f"Sources never answered after {BULK_IMPORT_SEARCH_ATTEMPTS} attempts: {unreachable}"
                        else:
                            failure_reason = (
                                "No candidate passed the recording match gate"
                                if rejected_results else "No results found"
                            )
                        with db_conn() as conn:
                            conn.execute(
                                "UPDATE bulk_import_tracks SET status = 'failed', error = ? WHERE id = ?",
                                (failure_reason, track_id)
                            )
                            conn.execute(
                                "UPDATE bulk_imports SET searched = searched + 1, failed = failed + 1, progress_at = datetime('now') WHERE id = ?",
                                (import_id,)
                            )
                            conn.commit()
                        finish_acquisition_cycle(target_id, "failed", failure_reason)
                        time.sleep(base_delay)
                        continue

                    # Results are already sorted by relevance_score descending.
                    best_match = search_results[0]
                    if override_dir and artist:
                        # Album mode: be strict on artist to avoid tribute/cover uploads.
                        strict_matches = [
                            c for c in search_results
                            if not _candidate_looks_like_cover(c)
                            and _candidate_channel_matches_expected_artist(c, artist)
                        ]
                        if strict_matches:
                            best_match = strict_matches[0]
                        else:
                            with db_conn() as conn:
                                conn.execute(
                                    "UPDATE bulk_import_tracks SET status = 'failed', error = ? WHERE id = ?",
                                    ("No strict artist match found", track_id)
                                )
                                conn.execute(
                                    "UPDATE bulk_imports SET searched = searched + 1, failed = failed + 1, progress_at = datetime('now') WHERE id = ?",
                                    (import_id,)
                                )
                                conn.commit()
                            print(
                                f"Album import {import_id}: no strict artist match for "
                                f"'{artist} - {song}', skipping track"
                            )
                            finish_acquisition_cycle(
                                target_id, "failed", "No strict artist match found"
                            )
                            time.sleep(base_delay)
                            continue
                    elif (watch_playlist_id or watch_artist_id) and artist:
                        # Watched imports: prefer a result that mentions the expected artist.
                        # If nothing matches, fail rather than downloading a random top result
                        # that could be a completely different song.
                        for candidate in search_results:
                            if _candidate_mentions_expected_artist(candidate, artist):
                                best_match = candidate
                                break
                        else:
                            wid = watch_playlist_id or watch_artist_id
                            top_title = search_results[0].get("title", "?")
                            top_channel = search_results[0].get("channel", "?")
                            print(
                                f"Watched import {wid}: no candidate matched "
                                f"expected artist '{artist}' for '{song}', "
                                f"refusing top result '{top_title}' by {top_channel}"
                            )
                            with db_conn() as conn:
                                no_match_error = (
                                    f"No artist match (top result was '{top_title}' by {top_channel})"
                                )
                                conn.execute(
                                    "UPDATE bulk_import_tracks SET status = 'failed', error = ? WHERE id = ?",
                                    (no_match_error, track_id)
                                )
                                conn.execute(
                                    "UPDATE bulk_imports SET searched = searched + 1, failed = failed + 1, progress_at = datetime('now') WHERE id = ?",
                                    (import_id,)
                                )
                                conn.commit()
                            finish_acquisition_cycle(target_id, "failed", no_match_error)
                            time.sleep(base_delay)
                            continue

                # Cancellation may arrive while a provider search is running.
                # Re-check before creating a Queue row or scheduling any bytes.
                with db_conn() as conn:
                    cancel_row = conn.execute(
                        "SELECT cancel_requested FROM bulk_imports WHERE id = ?",
                        (import_id,),
                    ).fetchone()
                    if cancel_row and bool(cancel_row[0]):
                        conn.execute(
                            "UPDATE bulk_import_tracks SET status = 'cancelled', error = ? WHERE id = ?",
                            ("Cancelled before download started", track_id),
                        )
                        conn.commit()
                        finish_acquisition_cycle(
                            target_id, "cancelled", "Cancelled before download started"
                        )
                        while active_downloads:
                            reap_downloads(wait_for_one=True)
                        break

                video_id = best_match["video_id"]
                source = best_match.get("source", "youtube")
                source_url = best_match.get("source_url")
                slskd_username = best_match.get("slskd_username")
                slskd_filename = best_match.get("slskd_filename")
                slskd_size = best_match.get("slskd_size") or best_match.get("size")
                if watch_playlist_id or watch_artist_id:
                    wid = watch_playlist_id or watch_artist_id
                    print(
                        f"Watched import {wid}: selected {source} for "
                        f"'{artist} - {song}' ({video_id})"
                    )

                # Create download job and update tracking
                job_id = str(uuid.uuid4())[:8]

                with db_conn() as conn:
                    if source == "soulseek":
                        conn.execute(
                            """INSERT INTO jobs
                               (id, video_id, title, artist, status, download_type, playlist_name, source,
                                slskd_username, slskd_filename, slskd_size, source_url, convert_audio, user_id,
                                acquisition_target_id, acquisition_cycle)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                job_id, video_id, song, artist, "queued", "single",
                                import_id if create_playlist else None,
                                source, slskd_username, slskd_filename, slskd_size,
                                source_url, int(convert_audio), user_id, target_id,
                                acquisition_cycle,
                            )
                        )
                    elif create_playlist:
                        conn.execute(
                            "INSERT INTO jobs (id, video_id, title, artist, status, download_type, playlist_name, source, source_url, convert_audio, user_id, acquisition_target_id, acquisition_cycle) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (job_id, video_id, song, artist, "queued", "single", import_id, source, source_url, int(convert_audio), user_id, target_id, acquisition_cycle)
                        )
                    else:
                        conn.execute(
                            "INSERT INTO jobs (id, video_id, title, artist, status, download_type, source, source_url, convert_audio, user_id, acquisition_target_id, acquisition_cycle) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (job_id, video_id, song, artist, "queued", "single", source, source_url, int(convert_audio), user_id, target_id, acquisition_cycle)
                        )

                    conn.execute(
                        "UPDATE bulk_import_tracks SET status = 'queued', job_id = ?, video_id = ? WHERE id = ?",
                        (job_id, video_id, track_id)
                    )
                    if watch_playlist_id:
                        track_hash = hash_track(artist, song)
                        conn.execute(
                            "UPDATE watched_playlist_tracks SET job_id = ? WHERE playlist_id = ? AND track_hash = ?",
                            (job_id, watch_playlist_id, track_hash)
                        )
                    if watch_artist_id:
                        track_hash = hash_track(artist, song)
                        conn.execute(
                            "UPDATE watched_artist_tracks SET job_id = ? WHERE artist_id = ? AND track_hash = ?",
                            (job_id, watch_artist_id, track_hash)
                        )
                    conn.execute(
                        "UPDATE bulk_imports SET searched = searched + 1, queued = queued + 1, progress_at = datetime('now') WHERE id = ?",
                        (import_id,)
                    )

                    # Record why the scorer picked this candidate over its rivals.
                    # Done inside the same transaction to avoid "database is locked"
                    # from a second connection fighting for the write lock.
                    import json as _json
                    try:
                        _blob = _json.dumps({
                            "selected": acquisition_candidate_summary(best_match),
                            "runners_up": [
                                acquisition_candidate_summary(r)
                                for r in search_results[:4]
                                if r.get("video_id") != best_match.get("video_id")
                            ][:3],
                        })
                        conn.execute(
                            "INSERT INTO search_decisions (job_id, query, decision_json) VALUES (?, ?, ?)",
                            (job_id, search_query, _blob),
                        )
                    except Exception as e:
                        print(f"Failed to save search decision: {e}")

                    conn.commit()

                _pname = playlist_name if (use_playlists_dir or custom_subdir) else None
                # Album downloads (override_dir set) bypass dupe checks; you picked the album
                # intentionally, and the track lives in Albums/ not Singles/ anyway.
                _skip_dupes = bool(override_dir)

                # Register the album track lock so retries stay dupe-check-free
                # even if the thread that spawned them lost the skip_dupe_check flag.
                if override_dir:
                    _od = Path(override_dir)
                    _album_name_lock = _od.name or ""
                    _album_artist_lock = _od.parent.name or ""
                    upsert_album_track_lock(
                        album_release_mbid, _album_name_lock, _album_artist_lock, song, job_id
                    )
                download_future = _get_download_pool().submit(
                    process_acquisition_cycle,
                    job_id,
                    best_match,
                    artist,
                    song,
                    convert_audio,
                    target_id=target_id,
                    cycle=acquisition_cycle,
                    automatic=True,
                    candidate_pool=search_results,
                    allowed_sources=(
                        set(preferred_sources_list)
                        if preferred_sources_list is not None else None
                    ),
                    priority_source=priority_source,
                    playlist_name=_pname,
                    use_playlists_dir=use_playlists_dir,
                    user_id=user_id,
                    override_dir=override_dir,
                    skip_dupe_check=_skip_dupes,
                    custom_subdir=custom_subdir,
                    original_youtube_video_id=track.get("original_youtube_video_id"),
                )
                active_downloads[download_future] = {
                    "track_id": track_id,
                    "job_id": job_id,
                    "target_id": target_id,
                }

            except Exception as e:
                # Recording the failure must never become the failure. If this
                # write itself blows up (a locked database used to do it), the
                # exception escaped the loop and abandoned every remaining
                # track, which is a rotten way to repay one bad row.
                try:
                    with db_conn() as conn:
                        conn.execute(
                            "UPDATE bulk_import_tracks SET status = 'failed', error = ? WHERE id = ?",
                            (str(e)[:200], track_id)
                        )
                        conn.execute(
                            "UPDATE bulk_imports SET searched = searched + 1, failed = failed + 1, progress_at = datetime('now') WHERE id = ?",
                            (import_id,)
                        )
                        conn.commit()
                except Exception as bookkeeping_error:
                    print(
                        f"Bulk import {import_id}: failed to record failure for track "
                        f"{track_id} ({e}): {bookkeeping_error}"
                    )
                try:
                    finish_acquisition_cycle(target_id, "failed", str(e))
                except Exception as ledger_error:
                    print(
                        f"Bulk import {import_id}: failed to close acquisition target "
                        f"{target_id}: {ledger_error}"
                    )

            # Standard delay between searches
            time.sleep(base_delay)

        # All tracks processed, or cancellation stopped the untouched tail.
        with db_conn() as conn:
            conn.row_factory = sqlite3.Row
            cancellation = conn.execute(
                "SELECT cancel_requested FROM bulk_imports WHERE id = ?", (import_id,)
            ).fetchone()
            was_cancelled = bool(cancellation and cancellation["cancel_requested"])
            conn.execute(
                """UPDATE bulk_imports
                   SET status = ?, completed_at = CURRENT_TIMESTAMP,
                       error = CASE WHEN ? THEN 'Cancelled; completed files and Queue history retained' ELSE error END
                   WHERE id = ?""",
                ("cancelled" if was_cancelled else "completed", int(was_cancelled), import_id)
            )
            conn.commit()

            # Get final counts for notification
            cursor = conn.execute(
                """
                SELECT total_tracks, queued, failed, skipped, create_playlist, playlist_name
                FROM bulk_imports
                WHERE id = ?
                """,
                (import_id,)
            )
            final_row = cursor.fetchone()
            final_queued = final_row["queued"] if final_row else 0
            final_failed = final_row["failed"] if final_row else 0
            final_skipped = final_row["skipped"] if final_row else 0
            final_total = final_row["total_tracks"] if final_row else 0
            # Re-read these flags at completion to avoid a race where API updates
            # create_playlist/playlist_name immediately after worker start.
            if final_row:
                create_playlist = bool(final_row["create_playlist"])
                playlist_name = final_row["playlist_name"]

        # Send notification for a completed import. Cancellation is an expected
        # user action, not a failure alert.
        if not was_cancelled:
            bulk_status = "completed_with_errors" if final_failed > 0 else "completed"
            send_notification(
                notification_type="bulk",
                title=playlist_name or f"Bulk import {import_id}",
                status=bulk_status,
                track_count=final_total,
                failed_count=final_failed,
                skipped_count=final_skipped,
                user_id=user_id,
            )

        # Create playlist if requested
        if not was_cancelled and create_playlist and final_queued > 0:
            spawn_daemon_thread(
                create_bulk_playlist,
                import_id,
                playlist_name or f"Playlist {import_id}",
                use_playlists_dir,
                user_id,
            )

    except Exception as e:
        with db_conn() as conn:
            conn.execute(
                "UPDATE bulk_imports SET status = 'error', error = ? WHERE id = ?",
                (str(e)[:500], import_id)
            )
            conn.commit()

        # Send notification for bulk import failure
        send_notification(
            notification_type="error",
            title=playlist_name or f"Bulk import {import_id}",
            status="failed",
            error=str(e),
            user_id=user_id,
        )
