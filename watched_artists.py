"""
MusicGrabber - Watched Artists

Monitors MusicBrainz for new singles from followed artists and auto-downloads them.
Mirrors the watched playlists pattern: lock, fetch, diff, queue, done.
"""

import sqlite3
import threading
import time

import albums
from constants import WATCHED_PLAYLIST_CHECK_HOURS, WATCHED_REFRESH_STALE_SECONDS
from db import db_conn
from bulk_import import start_bulk_import_for_tracks
from metadata import fetch_artist_singles, fetch_artist_albums, MusicBrainzUnavailable
from settings import get_setting_bool
from utils import hash_track, spawn_daemon_thread, check_duplicate


_scheduler_running = False
_scheduler_lock = threading.Lock()
_scheduler_wake_event = threading.Event()


def wake_artist_scheduler():
    """Wake the artist scheduler after an artist deadline changes."""
    _scheduler_wake_event.set()


def _seconds_until_next_artist_check() -> float:
    """Return the bounded delay until the earliest enabled artist is due."""
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
            FROM watched_artists
            WHERE enabled = 1
        """).fetchone()

    next_due = row[0] if row else None
    if next_due is None:
        return maximum
    return min(maximum, max(1.0, float(next_due) + 1.0))


def seed_artist_albums(artist_id: str, mbid: str, user_id: str | None = None) -> int:
    """Seed an artist's current album list into watched_artist_albums as 'seen', without queueing.

    Called exactly once, at the moment auto_add_albums is switched on (fresh
    follow with the toggle already ticked, or an existing artist flipping it on
    later). This is the anti-avalanche rule: whatever albums already exist get
    marked as already-known and are never queued; only albums that appear on a
    later refresh count as new. Mirrors the existing from_date behaviour for
    singles.

    Raises MusicBrainzUnavailable if MB can't be reached, so the caller can
    decide whether to persist the toggle at all (better to fail the request
    than silently enable auto-add with nothing seeded, which would queue the
    entire back catalogue on the very next refresh).
    """
    current_albums = fetch_artist_albums(mbid)
    if not current_albums:
        return 0

    seeded = 0
    with db_conn() as conn:
        for album in current_albums:
            release_mbid = (album.get("release_mbid") or "").strip()
            if not release_mbid:
                continue
            # The release-group MBID is the album's stable identity. Falling
            # back to release_mbid only matters for anything MusicBrainz hands
            # back without a group, which is rare and harmless.
            group_mbid = (album.get("release_group_mbid") or "").strip() or release_mbid
            cur = conn.execute(
                """INSERT OR IGNORE INTO watched_artist_albums
                   (artist_id, release_group_mbid, release_mbid, title, year, status)
                   VALUES (?, ?, ?, ?, ?, 'seen')""",
                (artist_id, group_mbid, release_mbid, album.get("title") or "", album.get("year") or "")
            )
            if cur.rowcount:
                seeded += 1
        conn.commit()
    return seeded


def _refresh_artist_albums(conn, artist: dict, artist_id: str, user_id: str | None) -> dict:
    """Diff MusicBrainz's current album list against what we already know, and
    queue anything genuinely new via the album pipeline.

    Only called when auto_add_albums is enabled. One MusicBrainz call regardless
    of how many albums there are (fetch_artist_albums paginates and rate-limits
    itself internally). One album failing to queue is recorded as 'failed' and
    does not stop the rest, same lesson bulk import already learned the hard way.
    """
    try:
        current_albums = fetch_artist_albums(artist["mbid"])
    except MusicBrainzUnavailable as e:
        print(f"Album refresh skipped for {artist.get('name', artist_id)}: MusicBrainz unreachable ({e})")
        return {"new_albums": 0, "queued": 0, "failed": 0}

    known_rows = conn.execute(
        """SELECT release_group_mbid, status
           FROM watched_artist_albums
           WHERE artist_id = ?""",
        (artist_id,)
    ).fetchall()
    # A queueing failure is not album identity. It is a transient attempt and
    # gets one fresh go on the next owning artist cycle, just like a failed
    # track acquisition target. Seen/queued releases remain terminal here.
    known = {
        row[0] for row in known_rows
        if row[0] and row[1] in {"seen", "queued"}
    }

    new_count = 0
    queued_count = 0
    failed_count = 0
    convert_audio = bool(artist.get("convert_audio", 1))

    for album in current_albums:
        release_mbid = (album.get("release_mbid") or "").strip()
        group_mbid = (album.get("release_group_mbid") or "").strip() or release_mbid
        if not release_mbid or group_mbid in known:
            continue  # already seeded or successfully handed to the album queue

        existing = conn.execute(
            """SELECT status FROM watched_artist_albums
               WHERE artist_id = ? AND release_group_mbid = ?""",
            (artist_id, group_mbid),
        ).fetchone()
        if not existing:
            new_count += 1
        title = album.get("title") or ""
        year = album.get("year") or ""
        conn.execute(
            """INSERT INTO watched_artist_albums
               (artist_id, release_group_mbid, release_mbid, title, year, status,
                attempt_count, last_attempt_at, last_error)
               VALUES (?, ?, ?, ?, ?, 'processing', 1, datetime('now'), NULL)
               ON CONFLICT(artist_id, release_group_mbid) DO UPDATE SET
                   release_mbid = excluded.release_mbid,
                   title = excluded.title,
                   year = excluded.year,
                   status = 'processing',
                   attempt_count = watched_artist_albums.attempt_count + 1,
                   last_attempt_at = datetime('now'),
                   last_error = NULL""",
            (artist_id, group_mbid, release_mbid, title, year),
        )
        conn.commit()
        try:
            result = albums.queue_album_download(
                artist["name"], title, release_mbid,
                convert_audio=convert_audio, user_id=user_id,
            )
            conn.execute(
                """INSERT INTO watched_artist_albums
                   (artist_id, release_group_mbid, release_mbid, title, year, status, queued_at, import_id)
                   VALUES (?, ?, ?, ?, ?, 'queued', datetime('now'), ?)
                   ON CONFLICT(artist_id, release_group_mbid) DO UPDATE SET
                       status = 'queued', queued_at = datetime('now'),
                       import_id = excluded.import_id, last_error = NULL""",
                (artist_id, group_mbid, release_mbid, title, year, result.get("import_id"))
            )
            conn.commit()
            queued_count += 1
        except Exception as e:
            print(f"Album auto-add failed for {artist.get('name', artist_id)} - '{title}': {e}")
            conn.execute(
                """INSERT INTO watched_artist_albums
                   (artist_id, release_group_mbid, release_mbid, title, year, status)
                   VALUES (?, ?, ?, ?, ?, 'failed')
                   ON CONFLICT(artist_id, release_group_mbid) DO UPDATE SET
                       status = 'failed', last_error = ?""",
                (artist_id, group_mbid, release_mbid, title, year, str(e)[:1000])
            )
            conn.commit()
            failed_count += 1

    return {"new_albums": new_count, "queued": queued_count, "failed": failed_count}


def refresh_watched_artist(artist_id: str) -> dict:
    """Fetch latest singles from MusicBrainz and queue any new ones for download.

    Returns a dict with refresh results including new_tracks count.
    Uses the same atomic lock pattern as watched playlists so concurrent
    refreshes can't stomp on each other.
    """
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        artist = conn.execute(
            "SELECT * FROM watched_artists WHERE id = ?", (artist_id,)
        ).fetchone()

        if not artist:
            return {"error": "Artist not found", "artist_id": artist_id}

        artist = dict(artist)
        user_id = artist.get("user_id")

        # Acquire an atomic per-artist refresh lock.
        lock_cursor = conn.execute(
            """UPDATE watched_artists
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
            (artist_id, str(WATCHED_REFRESH_STALE_SECONDS))
        )
        conn.commit()

        if lock_cursor.rowcount == 0:
            running_state = conn.execute(
                "SELECT refresh_stage, refresh_started_at FROM watched_artists WHERE id = ?",
                (artist_id,)
            ).fetchone()
            return {
                "artist_id": artist_id,
                "name": artist["name"],
                "already_running": True,
                "message": "Refresh already in progress",
                "refresh_stage": running_state["refresh_stage"] if running_state else None,
                "refresh_started_at": running_state["refresh_started_at"] if running_state else None,
            }

        def set_refresh_stage(stage: str) -> None:
            conn.execute(
                """UPDATE watched_artists
                   SET refresh_state = 'running',
                       refresh_stage = ?,
                       refresh_error = NULL
                   WHERE id = ?""",
                (stage, artist_id)
            )
            conn.commit()

        def finish_refresh_success(import_id: str | None) -> None:
            conn.execute(
                """UPDATE watched_artists
                   SET refresh_state = 'idle',
                       refresh_stage = 'done',
                       refresh_error = NULL,
                       refresh_import_id = ?,
                       refresh_completed_at = datetime('now')
                   WHERE id = ?""",
                (import_id, artist_id)
            )
            conn.commit()

        def finish_refresh_error(error_msg: str) -> None:
            conn.execute(
                """UPDATE watched_artists
                   SET refresh_state = 'error',
                       refresh_stage = 'failed',
                       refresh_error = ?,
                       refresh_import_id = NULL,
                       refresh_completed_at = datetime('now')
                   WHERE id = ?""",
                ((error_msg or "Refresh failed")[:800], artist_id)
            )
            conn.commit()

        try:
            # Singles and albums are independent follows. An albums-only artist
            # skips the singles hunt entirely: no MusicBrainz call, so nothing to
            # dedupe, nothing to diff and nothing to queue. The stages below still
            # run, but over an empty list, which costs nothing and keeps the
            # progress spinner honest rather than mysteriously silent.
            watch_singles = bool(artist.get("watch_singles", 1))

            # Fetch current singles from MusicBrainz
            set_refresh_stage("fetching")
            mb_tracks = fetch_artist_singles(artist["mbid"]) if watch_singles else []

            # Deduplicate by hash within this release batch  -  MB can list the same
            # recording across multiple single releases (e.g. regional releases).
            seen_hashes: set[str] = set()
            unique_tracks: list[dict] = []
            for t in mb_tracks:
                h = hash_track(t["artist"] or artist["name"], t["title"])
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    unique_tracks.append(t)

            # Load existing track state
            set_refresh_stage("diffing")
            track_rows = conn.execute(
                """SELECT wat.track_hash, wat.downloaded_at, wat.job_id,
                          wat.artist, wat.title, wat.release_date, j.status as job_status
                   FROM watched_artist_tracks wat
                   LEFT JOIN jobs j ON wat.job_id = j.id
                   WHERE wat.artist_id = ?""",
                (artist_id,)
            ).fetchall()
            tracked = {row["track_hash"]: dict(row) for row in track_rows}

            from_date = artist.get("from_date") or ""
            tracks_to_import: list[tuple[str, str]] = []
            new_count = 0
            # Same bargain as watched playlists: off means a track we downloaded
            # once is never chased again, however far its file has wandered.
            redownload_missing = get_setting_bool("redownload_missing_files", True)

            # Compute pass: decide what to write WITHOUT touching the DB. This is
            # the slow bit  -  check_duplicate() walks the library on disk for every
            # track, and a prolific artist (hello, Radiohead) has hundreds of them.
            # If we held an open write transaction across all those scans, SQLite's
            # single writer lock would be pinned for minutes and everything else
            # (even login, which writes a session row) would block until busy_timeout
            # and start throwing "database is locked". So we only read the in-memory
            # `tracked` dict here and stash the writes to flush in one quick batch.
            pending_writes: list[tuple[str, tuple]] = []
            # Process in cycles: heartbeat the refresh stage every batch so the UI
            # shows the scan is alive, and (below) flush writes in batches so the
            # write lock is taken in short bursts rather than one marathon hold.
            SEED_BATCH = 50
            processed = 0

            for t in unique_tracks:
                track_artist = t["artist"] or artist["name"]
                track_title = t["title"]
                release_date = t.get("release_date") or ""
                track_hash = hash_track(track_artist, track_title)
                existing = tracked.get(track_hash)

                processed += 1
                if processed % SEED_BATCH == 0:
                    set_refresh_stage("diffing")  # liveness heartbeat during a long scan

                if not existing:
                    # New track  -  check disk before inserting so pre-existing
                    # library files are recognised immediately rather than queued.
                    # Scoped to the watch's owner: on a multi-user box the wrong
                    # library is worse than no library at all.
                    existing_file = check_duplicate(track_artist, track_title, user_id=user_id)
                    pending_writes.append((
                        """INSERT OR IGNORE INTO watched_artist_tracks
                           (artist_id, track_hash, artist, title, release_date, release_mbid,
                            downloaded_at, resolved_path)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (artist_id, track_hash, track_artist, track_title,
                         release_date, t.get("release_mbid") or "",
                         "now" if existing_file else None,
                         str(existing_file) if existing_file else None)
                    ))
                    if existing_file:
                        continue  # Already on disk, nothing to queue
                    # Only queue if it's on or after the from_date
                    if not from_date or not release_date or release_date >= from_date:
                        tracks_to_import.append((track_artist, track_title))
                        new_count += 1
                    # else: seeded as already-known, will never be re-queued
                    continue

                # Already tracked  -  check if file went missing
                if existing.get("downloaded_at"):
                    if not redownload_missing:
                        continue  # Downloaded once is downloaded for good
                    if not check_duplicate(
                        existing.get("artist") or track_artist,
                        existing.get("title") or track_title,
                        user_id=user_id,
                    ):
                        # File has vanished  -  clear downloaded_at so it re-queues
                        pending_writes.append((
                            """UPDATE watched_artist_tracks
                               SET downloaded_at = NULL, resolved_path = NULL
                               WHERE artist_id = ? AND track_hash = ?""",
                            (artist_id, track_hash)
                        ))
                        tracks_to_import.append((track_artist, track_title))
                    continue

                # Not downloaded  -  check disk in case the file arrived via another route
                existing_file = check_duplicate(track_artist, track_title, user_id=user_id)
                if existing_file:
                    pending_writes.append((
                        """UPDATE watched_artist_tracks
                           SET downloaded_at = datetime('now'), resolved_path = ?
                           WHERE artist_id = ? AND track_hash = ?""",
                        (str(existing_file), artist_id, track_hash)
                    ))
                    continue

                # Not on disk  -  check job status
                job_status = existing.get("job_status")
                if job_status in ("queued", "downloading"):
                    continue  # In flight, don't double-queue
                # Failed, missing job, or no job  -  retry, but respect from_date.
                # Without this check, pre-date tracks inserted as seeds (no job_id,
                # no downloaded_at) get re-queued on every subsequent refresh.
                stored_release_date = existing.get("release_date") or ""
                if from_date and stored_release_date and stored_release_date < from_date:
                    continue
                tracks_to_import.append((track_artist, track_title))

            # Write pass, in cycles: flush in batches so each transaction is short
            # and the single SQLite writer lock is released between batches, never
            # pinned long enough to wedge other requests.
            for i in range(0, len(pending_writes), SEED_BATCH):
                for sql, params in pending_writes[i:i + SEED_BATCH]:
                    conn.execute(sql, params)
                conn.commit()

            # Queue new/missing tracks via bulk import
            import_id = None
            set_refresh_stage("queueing")
            if tracks_to_import:
                convert_audio = bool(artist.get("convert_audio", 1))
                import_id = start_bulk_import_for_tracks(
                    tracks_to_import,
                    convert_audio=convert_audio,
                    watch_artist_id=artist_id,
                    user_id=user_id,
                )
                conn.execute(
                    "UPDATE watched_artists SET refresh_import_id = ? WHERE id = ?",
                    (import_id, artist_id)
                )
                conn.commit()

            # Auto-add-albums: one MB call to check for newly-appeared albums and
            # queue them via the album pipeline. A failure here (MB down, or one
            # album blowing up) is swallowed so it never sinks the singles refresh
            # above, which has already succeeded and committed by this point.
            album_result = {"new_albums": 0, "queued": 0, "failed": 0}
            if artist.get("auto_add_albums"):
                set_refresh_stage("albums")
                try:
                    album_result = _refresh_artist_albums(conn, artist, artist_id, user_id)
                except Exception as e:
                    print(f"Album auto-add refresh error for {artist.get('name', artist_id)}: {e}")

            # Update last_checked and track count
            total_tracked = len(tracked) + new_count
            conn.execute(
                """UPDATE watched_artists
                   SET last_checked = datetime('now'),
                       last_track_count = ?
                   WHERE id = ?""",
                (total_tracked, artist_id)
            )
            conn.commit()

            finish_refresh_success(import_id)
            wake_artist_scheduler()

            return {
                "artist_id": artist_id,
                "name": artist["name"],
                "new_tracks": new_count,
                "queued": len(tracks_to_import),
                "total_tracked": total_tracked,
                "import_id": import_id,
                "new_albums": album_result["new_albums"],
                "albums_queued": album_result["queued"],
                "albums_failed": album_result["failed"],
            }

        except Exception as e:
            error_msg = str(e)
            print(f"Watched artist refresh error for {artist.get('name', artist_id)}: {error_msg}")
            finish_refresh_error(error_msg)
            wake_artist_scheduler()
            return {
                "artist_id": artist_id,
                "name": artist.get("name", ""),
                "error": error_msg,
            }


def watched_artist_scheduler():
    """Background thread that refreshes each artist when its own deadline is due."""
    print(
        "Watched artist scheduler started "
        f"(maximum sweep interval {WATCHED_PLAYLIST_CHECK_HOURS} hours)"
    )

    # Let the main scheduler go first
    time.sleep(15)
    print("Artist scheduler: Running initial check for overdue artists...")

    while _scheduler_running:
        _scheduler_wake_event.clear()
        try:
            print("Artist scheduler: Checking watched artists...")
            with db_conn() as conn:
                conn.row_factory = sqlite3.Row
                artists = conn.execute("""
                    SELECT id, name FROM watched_artists
                    WHERE enabled = 1
                    AND (last_checked IS NULL
                         OR datetime(last_checked, '+' || refresh_interval_hours || ' hours') < datetime('now'))
                """).fetchall()

            if artists:
                print(f"Artist scheduler: Found {len(artists)} artist(s) due for refresh")
                total_new = 0
                for a in artists:
                    result = refresh_watched_artist(a["id"])
                    total_new += result.get("new_tracks", 0)
                print(f"Artist scheduler: Checked {len(artists)} artist(s), {total_new} new track(s) found")
            else:
                print("Artist scheduler: No artists due for refresh")

        except Exception as e:
            print(f"Artist scheduler error: {e}")

        if not _scheduler_running:
            break

        try:
            sleep_seconds = _seconds_until_next_artist_check()
        except Exception as e:
            print(f"Artist scheduler deadline error: {e}")
            sleep_seconds = max(60.0, WATCHED_PLAYLIST_CHECK_HOURS * 3600.0)

        print(f"Artist scheduler: Next deadline check in {sleep_seconds / 60:.1f} minutes")
        _scheduler_wake_event.wait(timeout=sleep_seconds)


def start_artist_scheduler():
    """Start the watched artist background scheduler if not already running."""
    global _scheduler_running

    if WATCHED_PLAYLIST_CHECK_HOURS <= 0:
        print("Watched artist scheduler disabled (WATCHED_PLAYLIST_CHECK_HOURS=0)")
        return

    with _scheduler_lock:
        if _scheduler_running:
            return
        _scheduler_running = True

    spawn_daemon_thread(watched_artist_scheduler)
