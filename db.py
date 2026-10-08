"""
MusicGrabber - Database Layer

SQLite connection management, schema creation, and job monitoring.
"""

import json
import sqlite3
from contextlib import contextmanager
import queue
import threading
import time
from pathlib import Path
from constants import (
    DB_PATH,
    DB_BUSY_TIMEOUT_MS,
    DB_LOCK_RETRY_ATTEMPTS,
    DB_LOCK_RETRY_DELAY,
    DB_LOCK_RETRY_MAX_DELAY,
    STALE_JOB_TIMEOUT,
    STALE_JOB_CHECK_INTERVAL,
    STALE_BULK_IMPORT_TIMEOUT,
    STALE_BULK_IMPORT_MAX_RESUMES,
    STALE_BULK_IMPORT_ABANDON_AFTER,
    STALE_LONGFORM_SPLIT_TIMEOUT,
    LIBRARY_RECONCILE_INTERVAL,
    SEARCH_LOG_RETENTION_DAYS,
    WATCHED_REFRESH_STALE_SECONDS,
)

# The hifi-api setting was retired in v4.3.0 (migration v20). Its old default
# was blank; the historical migrations below still reference it by name.
MONOCHROME_HIFI_API_URL = ""


def _is_lock_error(exc: sqlite3.Error) -> bool:
    """True for the two flavours of "someone else is writing, go away"."""
    msg = str(exc).lower()
    return "database is locked" in msg or "database is busy" in msg


class MGConnection(sqlite3.Connection):
    """A pooled connection that tidies up after itself and retries lock errors.

    Two separate things produce "database is locked" on a WAL database with a
    connection pool, and only one of them is honest contention:

    1. A SELECT whose rows are never fully read (the classic `fetchone()` on a
       multi-row query) leaves a read transaction, and therefore a WAL snapshot,
       open on that connection. As soon as anybody else commits, the snapshot is
       stale and the next write on that connection fails *instantly* with
       SQLITE_BUSY_SNAPSHOT, reported to us as "database is locked".
       busy_timeout does not wait for this, because there is nothing to wait for,
       and the connection stays poisoned until that statement is closed or reset,
       which is what made the symptom so wonderfully intermittent. With a LIFO
       pool, the poisoned connection is also the first one handed out next.
       Hence: track the cursors a lease opens and close them when the lease ends.
    2. Genuine writer-versus-writer contention, where sqlite's busy_timeout can
       be outlasted by a bulk import and a fistful of download threads all
       fancying the single WAL write lock at once. Hence: retry with backoff.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lease_cursors: list[sqlite3.Cursor] = []

    def _retrying(self, op, may_reset: bool):
        """Run `op`, giving it another go if sqlite says it is locked.

        `may_reset` is only ever True when nothing is pending on this
        connection, because resetting mid-transaction would quietly bin writes
        the caller thinks it has made. Losing data beats no data on nobody's
        scorecard.
        """
        delay = DB_LOCK_RETRY_DELAY
        for attempt in range(DB_LOCK_RETRY_ATTEMPTS):
            try:
                return op()
            except sqlite3.OperationalError as e:
                last_go = attempt == DB_LOCK_RETRY_ATTEMPTS - 1
                if last_go or not _is_lock_error(e):
                    raise
                if may_reset:
                    # Nothing of the caller's was pending when this statement
                    # started, and a statement that lost the lock did not write
                    # anything, so there is nothing to lose by clearing the
                    # connection's read snapshot before trying again. This is
                    # the fix for (1). Note that the failed statement will have
                    # opened a transaction of its own, hence deciding this from
                    # the state captured beforehand rather than from now.
                    self.reset_lease_state()
                time.sleep(delay)
                delay = min(delay * 2, DB_LOCK_RETRY_MAX_DELAY)

    def execute(self, *args, **kwargs) -> sqlite3.Cursor:
        clean = not self.in_transaction
        cursor = self._retrying(lambda: super(MGConnection, self).execute(*args, **kwargs), clean)
        self._lease_cursors.append(cursor)
        return cursor

    def executemany(self, *args, **kwargs) -> sqlite3.Cursor:
        clean = not self.in_transaction
        cursor = self._retrying(
            lambda: super(MGConnection, self).executemany(*args, **kwargs), clean
        )
        self._lease_cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        # No resetting here: a rollback would throw away the very transaction we
        # are trying to land. Just wait it out and hope the writer ahead of us
        # gets on with it.
        self._retrying(lambda: super(MGConnection, self).commit(), False)

    def reset_lease_state(self) -> None:
        """Close this lease's cursors and end any transaction still open.

        Called when a connection goes back to the pool, so the next borrower
        gets a connection with no lingering read snapshot and no half-finished
        transaction from somebody else's afternoon.
        """
        cursors, self._lease_cursors = self._lease_cursors, []
        for cursor in cursors:
            try:
                cursor.close()
            except sqlite3.Error:
                pass
        try:
            self.rollback()
        except sqlite3.Error:
            pass


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(
        DB_PATH,
        timeout=DB_BUSY_TIMEOUT_MS / 1000,
        check_same_thread=False,
        factory=MGConnection,
    )
    conn.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.reset_lease_state()
    return conn


_DB_POOL_SIZE = 8
_db_pool: "queue.LifoQueue[sqlite3.Connection]" = queue.LifoQueue(maxsize=_DB_POOL_SIZE)
_db_pool_created = 0
_db_pool_lock = threading.Lock()


def _get_pooled_conn() -> sqlite3.Connection:
    global _db_pool_created
    try:
        # Try to grab an idle connection first.
        return _db_pool.get_nowait()
    except queue.Empty:
        pass
    # Nothing idle, but a cold pool is not an exhausted one. The pool only ever
    # fills up as connections are handed back, so in a fresh process there is
    # nothing to wait for and the timeout below simply had to elapse. That is
    # what used to stall the very first query of every boot by a full 15
    # seconds, with the port unopened and the user staring at nothing. Grow the
    # pool on demand up to its ceiling instead.
    with _db_pool_lock:
        if _db_pool_created < _DB_POOL_SIZE:
            conn = get_db()
            # Failed opens are not borrowers; do not reserve seats for ghosts.
            _db_pool_created += 1
            return conn
    # Pool is at full stretch and every connection is out on loan; either wait
    # for one to come back (up to 15s) or open a fresh connection as a safety
    # valve. Under heavy concurrent load (bulk import + multiple download
    # threads) spawning unlimited connections causes them to queue up and fight
    # over the single WAL write lock, which is what produces "database is
    # locked" crashes. Blocking here serialises checkout instead of flooding
    # sqlite with competing writers.
    try:
        return _db_pool.get(timeout=15)
    except queue.Empty:
        # Genuinely exhausted after 15s; open a new one rather than hang forever.
        return get_db()


def _return_pooled_conn(conn: sqlite3.Connection) -> None:
    try:
        _db_pool.put_nowait(conn)
    except queue.Full:
        conn.close()


@contextmanager
def db_conn() -> sqlite3.Connection:
    conn = _get_pooled_conn()
    try:
        yield conn
    finally:
        conn.row_factory = None
        # Closes this lease's cursors and rolls back anything left open, so no
        # forgotten SELECT can leave the next borrower unable to write.
        conn.reset_lease_state()
        _return_pooled_conn(conn)


def init_db():
    with db_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            video_id TEXT,
            title TEXT,
            artist TEXT,
            status TEXT DEFAULT 'queued',
            error TEXT,
            download_type TEXT DEFAULT 'single',
            playlist_name TEXT,
            total_tracks INTEGER,
            completed_tracks INTEGER DEFAULT 0,
            failed_tracks INTEGER DEFAULT 0,
            skipped_tracks INTEGER DEFAULT 0,
            m3u_path TEXT,
            source TEXT DEFAULT 'youtube',
            source_history TEXT,
            slskd_username TEXT,
            slskd_filename TEXT,
            slskd_size INTEGER,
            convert_audio INTEGER DEFAULT 0,
            source_url TEXT,
            selected_duration_secs REAL,
            file_deleted INTEGER DEFAULT 0,
            final_path TEXT,
            metadata_source TEXT,
            override_dir TEXT,
            album_release_mbid TEXT,
            album_name TEXT,
            album_track_title TEXT,
            album_track_number INTEGER,
            album_track_total INTEGER,
            album_context TEXT,
            skip_mismatch_check INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        )
    """)

        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN source TEXT DEFAULT 'youtube'")
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN source_history TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN slskd_username TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN slskd_filename TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN slskd_size INTEGER")
        except sqlite3.OperationalError:
            pass
        # Renamed from convert_to_flac in v4.0.0. A DB still carrying the old name
        # is left well alone here; _rename_conversion_columns() below renames it so
        # the values survive. Adding convert_audio now would leave the old column
        # sulking in the corner with everyone's actual settings inside it.
        _job_cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        if not {"convert_audio", "convert_to_flac"} & _job_cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN convert_audio INTEGER DEFAULT 0")
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN source_url TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN failed_tracks INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN skipped_tracks INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN search_query TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN search_token TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN selected_duration_secs REAL")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN audio_quality TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN file_deleted INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN final_path TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN metadata_source TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN override_dir TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN album_release_mbid TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN album_name TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN album_track_title TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN album_track_number INTEGER")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN album_track_total INTEGER")
        except sqlite3.OperationalError:
            pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_search_token ON jobs(search_token)")
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN album_context TEXT")
        except sqlite3.OperationalError:
            pass

        # Bulk imports table - tracks the overall import job
        conn.execute("""
        CREATE TABLE IF NOT EXISTS bulk_imports (
            id TEXT PRIMARY KEY,
            status TEXT DEFAULT 'pending',
            total_tracks INTEGER DEFAULT 0,
            searched INTEGER DEFAULT 0,
            queued INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0,
            skipped INTEGER DEFAULT 0,
            create_playlist INTEGER DEFAULT 0,
            playlist_name TEXT,
            convert_audio INTEGER DEFAULT 0,
            watch_playlist_id TEXT,
            use_playlists_dir INTEGER DEFAULT 0,
            watch_artist_id TEXT,
            user_id TEXT,
            preferred_sources TEXT DEFAULT 'all',
            priority_source TEXT,
            override_dir TEXT,
            album_release_mbid TEXT,
            album_total_tracks INTEGER,
            album_year TEXT,
            rate_limited_until TIMESTAMP,
            cancel_requested INTEGER DEFAULT 0,
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            -- Bumped every time a track is dealt with. A long import is perfectly
            -- entitled to take hours, so silence is the symptom worth watching,
            -- not elapsed time.
            progress_at TIMESTAMP,
            -- How many times this import has been brought back from the dead.
            resume_count INTEGER DEFAULT 0,
            completed_at TIMESTAMP
        )
    """)

        # Individual tracks within a bulk import
        conn.execute("""
        CREATE TABLE IF NOT EXISTS bulk_import_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id TEXT NOT NULL,
            line_num INTEGER,
            artist TEXT,
            song TEXT,
            status TEXT DEFAULT 'pending',
            job_id TEXT,
            video_id TEXT,
            isrc TEXT,
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (import_id) REFERENCES bulk_imports(id)
        )
    """)

        # Index for faster lookups
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bulk_import_tracks_import_id ON bulk_import_tracks(import_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bulk_import_tracks_status ON bulk_import_tracks(status)")

        try:
            conn.execute("ALTER TABLE bulk_imports ADD COLUMN watch_playlist_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE bulk_imports ADD COLUMN watch_artist_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE bulk_imports ADD COLUMN album_total_tracks INTEGER")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE bulk_imports ADD COLUMN cancel_requested INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # Album flow carries a per-track ISRC so we can chase the exact studio recording;
        # every other importer leaves this NULL and behaves exactly as before.
        try:
            conn.execute("ALTER TABLE bulk_import_tracks ADD COLUMN isrc TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE bulk_import_tracks ADD COLUMN original_youtube_video_id TEXT")
        except sqlite3.OperationalError:
            pass
        # Where an album track sits on the release. line_num is only its place in
        # the import, which for a missing-only top-up of a double album is fiction.
        for column in ("album_track_number", "album_track_total", "album_disc_number", "album_disc_total"):
            try:
                conn.execute(f"ALTER TABLE bulk_import_tracks ADD COLUMN {column} INTEGER")
            except sqlite3.OperationalError:
                pass

        # Long-form YouTube splitting: one master video cut into many tracks via
        # ffmpeg, rather than N independent per-line searches, so it gets its own
        # pair of tables instead of overloading bulk_imports/bulk_import_tracks.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS longform_splits (
            id TEXT PRIMARY KEY,
            source_url TEXT NOT NULL,
            video_id TEXT,
            video_title TEXT,
            album_artist TEXT,
            override_dir TEXT,
            status TEXT DEFAULT 'pending',
            detected_via TEXT,
            total_segments INTEGER DEFAULT 0,
            completed_segments INTEGER DEFAULT 0,
            failed_segments INTEGER DEFAULT 0,
            master_staging_path TEXT,
            cancel_requested INTEGER DEFAULT 0,
            user_id TEXT,
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            progress_at TIMESTAMP,
            completed_at TIMESTAMP
        )
    """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS longform_split_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            split_id TEXT NOT NULL,
            segment_index INTEGER NOT NULL,
            start_seconds REAL NOT NULL,
            end_seconds REAL NOT NULL,
            title TEXT NOT NULL,
            artist TEXT,
            status TEXT DEFAULT 'pending',
            final_path TEXT,
            error TEXT,
            FOREIGN KEY (split_id) REFERENCES longform_splits(id)
        )
    """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_longform_split_segments_split_id ON longform_split_segments(split_id)")

        # Watched playlists - playlists to monitor for new tracks
        conn.execute("""
        CREATE TABLE IF NOT EXISTS watched_playlists (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            name TEXT,
            platform TEXT NOT NULL,
            refresh_interval_hours INTEGER DEFAULT 24,
            last_checked TIMESTAMP,
            last_track_count INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            convert_audio INTEGER DEFAULT 0,
            make_m3u INTEGER DEFAULT 0,
            use_playlists_dir INTEGER DEFAULT 0,
            sync_mode TEXT DEFAULT 'append',
            stale_navidrome_paths INTEGER DEFAULT 0,
            unresolved_tracks INTEGER DEFAULT 0,
            redownload_missing_files INTEGER DEFAULT NULL,
            preferred_sources TEXT DEFAULT 'all',
            priority_source TEXT,
            lb_username TEXT,
            refresh_state TEXT DEFAULT 'idle',
            refresh_stage TEXT,
            refresh_started_at TIMESTAMP,
            refresh_completed_at TIMESTAMP,
            refresh_error TEXT,
            refresh_import_id TEXT,
            gone_strikes INTEGER DEFAULT 0,
            auto_paused INTEGER DEFAULT 0,
            pause_reason TEXT,
            user_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, url)
        )
    """)

        # Tracks seen in watched playlists (for detecting new additions)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS watched_playlist_tracks (
            playlist_id TEXT NOT NULL,
            track_hash TEXT NOT NULL,
            artist TEXT,
            title TEXT,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            downloaded_at TIMESTAMP,
            library_checked_at TIMESTAMP,
            job_id TEXT,
            PRIMARY KEY (playlist_id, track_hash),
            FOREIGN KEY (playlist_id) REFERENCES watched_playlists(id) ON DELETE CASCADE
        )
    """)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_watched_tracks_playlist ON watched_playlist_tracks(playlist_id)")
        conn.execute("""
        CREATE TRIGGER IF NOT EXISTS clear_watched_playlist_track_job_after_delete
        AFTER DELETE ON jobs
        BEGIN
            UPDATE watched_playlist_tracks
            SET job_id = NULL
            WHERE job_id = OLD.id;
        END
        """)
        try:
            conn.execute("ALTER TABLE watched_playlist_tracks ADD COLUMN original_youtube_video_id TEXT")
        except sqlite3.OperationalError:
            pass

        # Search history logs for stats
        conn.execute("""
        CREATE TABLE IF NOT EXISTS search_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            artist TEXT,
            result_count INTEGER DEFAULT 0,
            source TEXT DEFAULT 'youtube',
            search_token TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
        try:
            conn.execute("ALTER TABLE search_logs ADD COLUMN search_token TEXT")
        except sqlite3.OperationalError:
            pass
        conn.execute(
            "UPDATE search_logs SET search_token = lower(hex(randomblob(16))) "
            "WHERE search_token IS NULL OR search_token = ''"
        )

        conn.execute("CREATE INDEX IF NOT EXISTS idx_search_logs_created_at ON search_logs(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_search_logs_artist ON search_logs(artist)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_search_logs_search_token "
            "ON search_logs(search_token) WHERE search_token IS NOT NULL"
        )

        # Settings table - stores configuration that can be edited via UI
        conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

        # Blacklist  -  reported bad tracks and blocked uploaders
        conn.execute("""
        CREATE TABLE IF NOT EXISTS blacklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT,
            uploader TEXT,
            source TEXT,
            reason TEXT,
            note TEXT,
            job_id TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_blacklist_video ON blacklist(video_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_blacklist_uploader ON blacklist(uploader, source)")

        # Last-known source health survives restarts, so a dead provider does not
        # get a free pardon merely because the container had a cup of tea.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS source_health (
            source_id TEXT PRIMARY KEY,
            healthy INTEGER NOT NULL,
            checked_at REAL NOT NULL,
            reason TEXT,
            disabled_until REAL NOT NULL DEFAULT 0
        )
        """)

        # Track upgrades  -  the Phase 1 scan caches one row per eligible (our-tagged)
        # file. The filesystem stays the source of truth; this is just a cache keyed by
        # (user_id, path) and refreshed on every scan. mtime lets re-scans skip
        # unchanged files. dismissed_mtime records the file mtime at the moment the user
        # dismissed it, so a later edit to the file lapses the dismissal automatically.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS upgrade_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT '',
            path TEXT NOT NULL,
            mtime REAL,
            file_size INTEGER,
            codec TEXT,
            bitrate_kbps INTEGER,
            duration REAL,
            file_tier INTEGER,
            target_tier INTEGER,
            below_target INTEGER DEFAULT 0,
            source TEXT,
            artist TEXT,
            title TEXT,
            dismissed INTEGER DEFAULT 0,
            dismissed_mtime REAL,
            upgrade_state TEXT DEFAULT 'none',
            last_scanned TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, path)
        )
    """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_upgrade_below ON upgrade_candidates(user_id, below_target, dismissed)")

        # Phase 2: per-candidate search cache. Populated on demand when the user views the
        # Watched Upgrades page; found_at gives the TTL so revisits don't re-hammer sources.
        # found_searched distinguishes "searched, nothing better" from "not searched yet".
        for _col, _decl in [
            ("file_id", "TEXT"),
            ("content_sha256", "TEXT"),
            ("source_quality", "TEXT"),
            ("source_codec", "TEXT"),
            ("source_bitrate_kbps", "INTEGER"),
            ("found_at", "TIMESTAMP"),
            ("found_searched", "INTEGER DEFAULT 0"),
            ("found_source", "TEXT"),
            ("found_quality", "TEXT"),
            ("found_tier", "INTEGER"),
            ("found_confidence", "REAL"),
            ("found_verified", "INTEGER DEFAULT 0"),
            ("found_video_id", "TEXT"),
            ("found_source_url", "TEXT"),
            ("found_slskd_username", "TEXT"),
            ("found_slskd_filename", "TEXT"),
            ("found_slskd_size", "INTEGER"),
        ]:
            try:
                conn.execute(f"ALTER TABLE upgrade_candidates ADD COLUMN {_col} {_decl}")
            except sqlite3.OperationalError:
                pass
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_upgrade_file_id "
            "ON upgrade_candidates(user_id, file_id)"
        )

        # One row per file a Track Upgrade swap displaced. quarantine_path is where it
        # physically sits now; original_path is where a restore puts it back;
        # replaced_by_path is where the upgrade that displaced it currently lives (usually
        # the same as original_path, but a format-changing upgrade, e.g. mp3 -> flac, gives
        # it a different filename, so both need tracking to undo the swap cleanly).
        conn.execute("""
        CREATE TABLE IF NOT EXISTS upgrade_quarantine (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT '',
            quarantine_path TEXT NOT NULL,
            original_path TEXT NOT NULL,
            replaced_by_path TEXT NOT NULL,
            artist TEXT,
            title TEXT,
            file_size INTEGER,
            quarantined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_upgrade_quarantine_user "
            "ON upgrade_quarantine(user_id, quarantined_at)"
        )

        # Audio Provenance Audit. Runs are immutable snapshots while they are being
        # built; is_current is flipped only after the last file has been inspected.
        # This keeps a half-scanned library from masquerading as a finished report.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS audio_audit_runs (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT '',
            music_root TEXT NOT NULL,
            criteria_version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            is_current INTEGER NOT NULL DEFAULT 0,
            total_files INTEGER NOT NULL DEFAULT 0,
            scanned_files INTEGER NOT NULL DEFAULT 0,
            classified_files INTEGER NOT NULL DEFAULT 0,
            unreadable_files INTEGER NOT NULL DEFAULT 0,
            excluded_paths INTEGER NOT NULL DEFAULT 0,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            error TEXT
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS audio_audit_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT '',
            path TEXT NOT NULL,
            filename TEXT NOT NULL,
            file_size INTEGER,
            mtime REAL,
            container TEXT,
            codec TEXT,
            bitrate_kbps INTEGER,
            duration REAL,
            sample_rate_hz INTEGER,
            bits_per_sample INTEGER,
            channels INTEGER,
            source TEXT,
            source_quality TEXT,
            source_codec TEXT,
            source_bitrate_kbps INTEGER,
            file_id TEXT,
            artist TEXT,
            title TEXT,
            stored_quality TEXT NOT NULL,
            effective_quality TEXT NOT NULL,
            effective_tier INTEGER,
            classification TEXT NOT NULL,
            classification_label TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            caveats_json TEXT NOT NULL DEFAULT '[]',
            read_error TEXT,
            FOREIGN KEY(run_id) REFERENCES audio_audit_runs(id) ON DELETE CASCADE,
            UNIQUE(run_id, path)
        )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audio_audit_current "
            "ON audio_audit_runs(user_id, is_current, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audio_audit_class "
            "ON audio_audit_files(run_id, classification)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audio_audit_codec "
            "ON audio_audit_files(run_id, container, codec)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audio_audit_source "
            "ON audio_audit_files(run_id, source)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audio_audit_effective "
            "ON audio_audit_files(run_id, effective_quality)"
        )

        # Migration: add uploader column to jobs (raw channel/uploader name)
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN uploader TEXT")
        except sqlite3.OperationalError:
            pass

        # Migration: add make_m3u to watched_playlists
        try:
            conn.execute("ALTER TABLE watched_playlists ADD COLUMN make_m3u INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        # Migration: add use_playlists_dir to watched_playlists and bulk_imports
        try:
            conn.execute("ALTER TABLE watched_playlists ADD COLUMN use_playlists_dir INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE bulk_imports ADD COLUMN use_playlists_dir INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        # Migration: sync_mode for watched playlists (append = grow forever, mirror = track removals)
        try:
            conn.execute("ALTER TABLE watched_playlists ADD COLUMN sync_mode TEXT DEFAULT 'append'")
        except sqlite3.OperationalError:
            pass

        # Migration: removed_at for tracked playlist tracks (set when a track vanishes from upstream)
        try:
            conn.execute("ALTER TABLE watched_playlist_tracks ADD COLUMN removed_at TIMESTAMP")
        except sqlite3.OperationalError:
            pass

        # Migration: stale_navidrome_paths - count of dead Navidrome entries found during last M3U rebuild
        try:
            conn.execute("ALTER TABLE watched_playlists ADD COLUMN stale_navidrome_paths INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE watched_playlists ADD COLUMN refresh_state TEXT DEFAULT 'idle'")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE watched_playlists ADD COLUMN refresh_stage TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE watched_playlists ADD COLUMN refresh_started_at TIMESTAMP")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE watched_playlists ADD COLUMN refresh_completed_at TIMESTAMP")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE watched_playlists ADD COLUMN refresh_error TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE watched_playlists ADD COLUMN refresh_import_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            # ListenBrainz "Created for You" playlists rotate weekly; store the username so we can
            # re-resolve the current week's UUID when the pinned one goes stale.
            conn.execute("ALTER TABLE watched_playlists ADD COLUMN lb_username TEXT")
        except sqlite3.OperationalError:
            pass

        # Watched artists - artists to monitor for new singles via MusicBrainz
        conn.execute("""
        CREATE TABLE IF NOT EXISTS watched_artists (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            mbid TEXT NOT NULL UNIQUE,
            from_date TEXT NOT NULL,
            refresh_interval_hours INTEGER DEFAULT 24,
            last_checked TIMESTAMP,
            last_track_count INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            convert_audio INTEGER DEFAULT 0,
            refresh_state TEXT DEFAULT 'idle',
            refresh_stage TEXT,
            refresh_started_at TIMESTAMP,
            refresh_completed_at TIMESTAMP,
            refresh_error TEXT,
            refresh_import_id TEXT,
            auto_add_albums INTEGER DEFAULT 0,
            unresolved_tracks INTEGER DEFAULT 0,
            -- Singles and albums are independent switches. Defaults to 1 because
            -- watching an artist has always meant singles; an albums-only follow
            -- turns this off so the refresh doesn't go hunting singles nobody asked for.
            watch_singles INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS watched_artist_tracks (
            artist_id TEXT NOT NULL,
            track_hash TEXT NOT NULL,
            artist TEXT,
            title TEXT,
            release_date TEXT,
            release_mbid TEXT,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            downloaded_at TIMESTAMP,
            job_id TEXT,
            resolved_path TEXT,
            PRIMARY KEY (artist_id, track_hash),
            FOREIGN KEY (artist_id) REFERENCES watched_artists(id) ON DELETE CASCADE
        )
    """)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_watched_artist_tracks_artist ON watched_artist_tracks(artist_id)")

        # Watched artist albums - known albums per followed artist, used by the
        # "automatically add new albums" toggle. status: 'seen' (known, not
        # queued), 'queued' (sent to the album pipeline), 'failed' (queueing blew
        # up, worth another look). Unconditional CREATE TABLE like its sibling
        # above; no version gate needed since IF NOT EXISTS already covers fresh
        # and existing DBs alike.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS watched_artist_albums (
            artist_id TEXT NOT NULL,
            release_group_mbid TEXT,
            release_mbid TEXT,
            title TEXT,
            year TEXT,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            queued_at TIMESTAMP,
            import_id TEXT,
            status TEXT DEFAULT 'seen',
            attempt_count INTEGER DEFAULT 0,
            last_attempt_at TIMESTAMP,
            last_error TEXT,
            UNIQUE (artist_id, release_group_mbid),
            FOREIGN KEY (artist_id) REFERENCES watched_artists(id) ON DELETE CASCADE
        )
    """)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_watched_artist_albums_artist ON watched_artist_albums(artist_id)")
        for _column, _declaration in (
            ("attempt_count", "INTEGER DEFAULT 0"),
            ("last_attempt_at", "TIMESTAMP"),
            ("last_error", "TEXT"),
        ):
            try:
                conn.execute(
                    f"ALTER TABLE watched_artist_albums ADD COLUMN {_column} {_declaration}"
                )
            except sqlite3.OperationalError:
                pass

        # Migration: resolved_path - actual on-disk path saved at download time.
        # Sidesteps artist/title lookup mismatches caused by romanisation or
        # metadata normalisation (e.g. Spotify sends '山下達郎', file lands as 'Tatsuro Yamashita').
        try:
            conn.execute("ALTER TABLE watched_playlist_tracks ADD COLUMN resolved_path TEXT")
        except sqlite3.OperationalError:
            pass

        # Migration: track position within the source playlist for correct M3U ordering
        try:
            conn.execute("ALTER TABLE watched_playlist_tracks ADD COLUMN position INTEGER")
        except sqlite3.OperationalError:
            pass

        # Last external-library reconciliation for append-only historical rows.
        # Failed lookups are throttled so an unchanged chart history does not
        # make every refresh repeat the same slow Navidrome/Lidarr requests.
        try:
            conn.execute(
                "ALTER TABLE watched_playlist_tracks ADD COLUMN library_checked_at TIMESTAMP"
            )
        except sqlite3.OperationalError:
            pass

        # Multi-user support tables
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            is_active INTEGER DEFAULT 1,
            force_password_change INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS download_tokens (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            used_at TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
        )
        """)

        # Reusable sibling of download_tokens, for <audio> playback. Not bound to
        # a job: the stream endpoints already scope what they'll serve to the
        # caller, so this only needs to say who the caller is.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS stream_tokens (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, key),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_download_tokens_expires ON download_tokens(expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_download_tokens_user ON download_tokens(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_download_tokens_job ON download_tokens(job_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stream_tokens_expires ON stream_tokens(expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stream_tokens_user ON stream_tokens(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_settings_user ON user_settings(user_id)")

        # Multi-user: add user_id to all domain tables
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN user_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE bulk_imports ADD COLUMN user_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE watched_playlists ADD COLUMN user_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE watched_artists ADD COLUMN user_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE blacklist ADD COLUMN user_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE search_logs ADD COLUMN user_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE watched_playlists ADD COLUMN preferred_sources TEXT DEFAULT 'all'")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE bulk_imports ADD COLUMN preferred_sources TEXT DEFAULT 'all'")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE watched_playlists ADD COLUMN priority_source TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE bulk_imports ADD COLUMN priority_source TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE bulk_imports ADD COLUMN override_dir TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE bulk_imports ADD COLUMN album_release_mbid TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            # The release year, so every track on an album agrees on one, rather
            # than each going off to MusicBrainz and coming back with its own.
            conn.execute("ALTER TABLE bulk_imports ADD COLUMN album_year TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE watched_playlists ADD COLUMN custom_subdir TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE bulk_imports ADD COLUMN custom_subdir TEXT")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN skip_mismatch_check INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN progress_stage TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            # Heartbeat for the stale-job monitor: bumped on every job update so
            # a slow-but-alive download isn't declared dead on age alone.
            conn.execute("ALTER TABLE jobs ADD COLUMN progress_at TIMESTAMP")
        except sqlite3.OperationalError:
            pass

        # Watched track match mismatches  -  persistent audit log so we can spot
        # normalisation gaps without relying on Docker log retention.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS watched_match_mismatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            playlist_id TEXT,
            expected_artist TEXT,
            expected_title TEXT,
            actual_artist TEXT,
            actual_title TEXT,
            exp_normalised TEXT,
            got_normalised TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mismatches_created ON watched_match_mismatches(created_at)"
        )

        # Search decisions  -  records why the scorer picked a particular candidate
        # over its rivals for automated downloads (bulk import, watched playlists).
        # Only populated for automated searches; manual picks don't need justification.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS search_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            query TEXT,
            decision_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_search_decisions_job ON search_decisions(job_id)"
        )

        # Acquisition ledger - one durable target survives the individual Queue
        # jobs created by later watched-playlist/artist refresh cycles. Historical
        # imports are deliberately not backfilled: creating rows is lazy, otherwise
        # an upgrade could wake thousands of old failures and start downloading.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS acquisition_targets (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            owner_type TEXT NOT NULL,
            owner_key TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'automatic',
            artist TEXT,
            title TEXT,
            isrc TEXT,
            allowed_sources TEXT DEFAULT 'all',
            priority_source TEXT,
            convert_audio INTEGER DEFAULT 0,
            destination_json TEXT,
            cycle_count INTEGER DEFAULT 0,
            attempt_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            last_attempt_at TIMESTAMP,
            last_error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            UNIQUE(owner_type, owner_key)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS acquisition_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id TEXT NOT NULL,
            cycle_number INTEGER NOT NULL,
            job_id TEXT,
            sequence INTEGER NOT NULL,
            source TEXT NOT NULL,
            candidate_id TEXT,
            candidate_title TEXT,
            candidate_artist TEXT,
            candidate_quality TEXT,
            status TEXT NOT NULL DEFAULT 'attempting',
            error TEXT,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY(target_id) REFERENCES acquisition_targets(id) ON DELETE CASCADE,
            UNIQUE(target_id, cycle_number, sequence)
        )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_acquisition_attempts_target "
            "ON acquisition_attempts(target_id, cycle_number, sequence)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_acquisition_attempts_job "
            "ON acquisition_attempts(job_id)"
        )
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN acquisition_target_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN acquisition_cycle INTEGER")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN requested_video_fallback INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE bulk_import_tracks ADD COLUMN acquisition_target_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE bulk_import_tracks ADD COLUMN acquisition_cycle INTEGER")
        except sqlite3.OperationalError:
            pass

        # Album track locks  -  records which artist/title combinations are actively
        # being processed for album downloads.  Persists across failures so retries
        # still bypass dupe check.  Cleared only on success or by the stale monitor.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS album_track_locks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            release_mbid TEXT,
            album_name   TEXT NOT NULL,
            album_artist TEXT NOT NULL,
            track_title  TEXT NOT NULL COLLATE NOCASE,
            status       TEXT NOT NULL DEFAULT 'pending',
            job_id       TEXT,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_atl_track "
            "ON album_track_locks (album_artist, track_title)"
        )
        # Without this unique index the ON CONFLICT(release_mbid, track_title)
        # clause in upsert_album_track_lock() has nothing to conflict against,
        # and SQLite refuses the statement entirely. NULL release_mbids are still
        # allowed to repeat (SQLite treats NULLs as distinct in unique indexes),
        # which is exactly what we want for the folder-only routing branch.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_atl_release_track "
            "ON album_track_locks (release_mbid, track_title)"
        )

        # One live import per album folder, enforced by SQLite rather than by
        # hopeful timing. queue_album_download() checks for an in-flight import
        # and then creates one, and two impatient clicks can both pass the check
        # before either insert lands, queueing the album twice into the same
        # directory. A partial unique index makes the second insert lose, which
        # the caller catches and reports as "already in progress".
        #
        # Wrapped because an install that already queued a duplicate cannot build
        # the index; a warning beats refusing to boot, and the old check-then-act
        # still catches the everyday case.
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_bulk_imports_active_album "
                "ON bulk_imports (override_dir) "
                "WHERE override_dir IS NOT NULL "
                "AND completed_at IS NULL "
                "AND status IN ('pending', 'processing')"
            )
        except sqlite3.DatabaseError as e:
            print(f"DB: could not create idx_bulk_imports_active_album ({e}); "
                  "duplicate active album imports may already exist")

        # --- convert_to_flac → convert_audio (v4.0.0) ---
        # The flag never meant "make me a FLAC"; it means "convert to whatever
        # audio_format says", which for most of v3 quietly meant Opus. The column
        # now says what it does.
        #
        # This deliberately runs BEFORE the numbered migrations rather than as a
        # v13 block after them. The v1 migration rebuilds watched_playlists and
        # watched_artists by listing their columns by name, and it still runs on a
        # brand-new DB (db_version starts at 0), so it has to find the new name
        # already in place. No version stamp is needed: the PRAGMA check below is
        # its own idempotence, and it is a no-op on every DB created since.
        for _conv_table in ("jobs", "bulk_imports", "watched_playlists", "watched_artists"):
            _conv_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({_conv_table})")}
            if "convert_audio" not in _conv_cols and "convert_to_flac" in _conv_cols:
                conn.execute(f"ALTER TABLE {_conv_table} RENAME COLUMN convert_to_flac TO convert_audio")
                print(f"DB: renamed {_conv_table}.convert_to_flac → {_conv_table}.convert_audio")

        # The stored setting gets the same treatment. If a row somehow already
        # exists under the new key it stays the winner and the old one is binned,
        # rather than two rows arguing about it forever.
        conn.execute("""
            UPDATE settings SET key = 'default_convert_audio'
            WHERE key = 'default_convert_to_flac'
              AND NOT EXISTS (SELECT 1 FROM settings WHERE key = 'default_convert_audio')
        """)
        conn.execute("DELETE FROM settings WHERE key = 'default_convert_to_flac'")

        # --- DB version tracking ---
        # Version is stored in settings as 'db_version' (integer string).
        # Increment when table recreations or other irreversible migrations run.
        db_version_row = conn.execute(
            "SELECT value FROM settings WHERE key = 'db_version'"
        ).fetchone()
        db_version = int(db_version_row[0]) if db_version_row else 0

        # v1: Relax unique constraints on watched_playlists and watched_artists so
        # multiple users can independently watch the same URL / artist.
        # Also scope the search_logs unique index to (user_id, search_token).
        # SQLite can't drop constraints, so we recreate the affected tables.
        if db_version < 1:
            # watched_playlists: url UNIQUE → (user_id, url) UNIQUE
            conn.execute("""
            CREATE TABLE IF NOT EXISTS watched_playlists_new (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                name TEXT,
                platform TEXT NOT NULL,
                refresh_interval_hours INTEGER DEFAULT 24,
                last_checked TIMESTAMP,
                last_track_count INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                convert_audio INTEGER DEFAULT 0,
                make_m3u INTEGER DEFAULT 0,
                use_playlists_dir INTEGER DEFAULT 0,
                sync_mode TEXT DEFAULT 'append',
                stale_navidrome_paths INTEGER DEFAULT 0,
                preferred_sources TEXT DEFAULT 'all',
                lb_username TEXT,
                custom_subdir TEXT,
                refresh_state TEXT DEFAULT 'idle',
                refresh_stage TEXT,
                refresh_started_at TIMESTAMP,
                refresh_completed_at TIMESTAMP,
                refresh_error TEXT,
                refresh_import_id TEXT,
                user_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, url)
            )
            """)
            # Older DBs may not have custom_subdir yet (the ALTER above adds it),
            # but we still SELECT it explicitly so the column survives the copy.
            # If somehow it's missing on the source table, fall back to NULL.
            try:
                conn.execute("""
                INSERT OR IGNORE INTO watched_playlists_new
                SELECT id, url, name, platform, refresh_interval_hours, last_checked,
                       last_track_count, enabled, convert_audio,
                       COALESCE(make_m3u, 0),
                       COALESCE(use_playlists_dir, 0),
                       COALESCE(sync_mode, 'append'),
                       COALESCE(stale_navidrome_paths, 0),
                       COALESCE(preferred_sources, 'all'),
                       lb_username,
                       custom_subdir,
                       COALESCE(refresh_state, 'idle'),
                       refresh_stage, refresh_started_at, refresh_completed_at,
                       refresh_error, refresh_import_id,
                       user_id, created_at
                FROM watched_playlists
                """)
            except sqlite3.OperationalError:
                # Source table lacks custom_subdir for some reason; copy without it.
                conn.execute("""
                INSERT OR IGNORE INTO watched_playlists_new
                SELECT id, url, name, platform, refresh_interval_hours, last_checked,
                       last_track_count, enabled, convert_audio,
                       COALESCE(make_m3u, 0),
                       COALESCE(use_playlists_dir, 0),
                       COALESCE(sync_mode, 'append'),
                       COALESCE(stale_navidrome_paths, 0),
                       COALESCE(preferred_sources, 'all'),
                       lb_username,
                       NULL,
                       COALESCE(refresh_state, 'idle'),
                       refresh_stage, refresh_started_at, refresh_completed_at,
                       refresh_error, refresh_import_id,
                       user_id, created_at
                FROM watched_playlists
                """)
            conn.execute("DROP TABLE watched_playlists")
            conn.execute("ALTER TABLE watched_playlists_new RENAME TO watched_playlists")

            # watched_artists: mbid UNIQUE → (user_id, mbid) UNIQUE
            conn.execute("""
            CREATE TABLE IF NOT EXISTS watched_artists_new (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                mbid TEXT NOT NULL,
                from_date TEXT NOT NULL,
                refresh_interval_hours INTEGER DEFAULT 24,
                last_checked TIMESTAMP,
                last_track_count INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                convert_audio INTEGER DEFAULT 0,
                refresh_state TEXT DEFAULT 'idle',
                refresh_stage TEXT,
                refresh_started_at TIMESTAMP,
                refresh_completed_at TIMESTAMP,
                refresh_error TEXT,
                refresh_import_id TEXT,
                user_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, mbid)
            )
            """)
            conn.execute("""
            INSERT OR IGNORE INTO watched_artists_new
            SELECT id, name, mbid, from_date, refresh_interval_hours, last_checked,
                   last_track_count, enabled, convert_audio,
                   COALESCE(refresh_state, 'idle'),
                   refresh_stage, refresh_started_at, refresh_completed_at,
                   refresh_error, refresh_import_id,
                   user_id, created_at
            FROM watched_artists
            """)
            conn.execute("DROP TABLE watched_artists")
            conn.execute("ALTER TABLE watched_artists_new RENAME TO watched_artists")

            # search_logs: drop the global unique index on search_token;
            # uniqueness is now enforced per (user_id, search_token).
            conn.execute("DROP INDEX IF EXISTS idx_search_logs_search_token")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_search_logs_user_token "
                "ON search_logs(user_id, search_token) WHERE search_token IS NOT NULL"
            )

            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('db_version', '1')"
            )
            print("DB migrated to version 1: multi-user unique constraints applied")

        # v2: Seed the Monochrome metadata URL for version-skipping upgrades.
        if db_version < 2:
            conn.execute("""
                INSERT INTO settings (key, value, updated_at)
                VALUES ('monochrome_hifi_api_url', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = CURRENT_TIMESTAMP
                WHERE value = '' OR value IS NULL
            """, (MONOCHROME_HIFI_API_URL, MONOCHROME_HIFI_API_URL))
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('db_version', '2')"
            )
            print("DB migrated to version 2: Monochrome metadata URL default seeded")

        # v4: The eu-central Render node was suspended by its owner, so hifi-api lookups
        # via that hostname now return 503. The apex api.monochrome.tf is CDN-routed and
        # picks a healthy node automatically; switch anyone still pinned to the dead one.
        if db_version < 4:
            conn.execute("""
                UPDATE settings
                SET value = 'https://api.monochrome.tf',
                    updated_at = CURRENT_TIMESTAMP
                WHERE key = 'monochrome_hifi_api_url'
                  AND value = 'https://eu-central.monochrome.tf'
            """)
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('db_version', '4')"
            )
            print("DB migrated to version 4: stale eu-central hifi-api URL retired")

        # v5: The apex hifi-api endpoint also moved to a Render suspension page
        # in May 2026. Monochrome's live frontend still advertises the Samidy
        # instance, and it returns Tidal search metadata for the same API shape.
        if db_version < 5:
            conn.execute("""
                UPDATE settings
                SET value = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE key = 'monochrome_hifi_api_url'
                  AND value IN ('https://api.monochrome.tf', 'https://eu-central.monochrome.tf')
            """, (MONOCHROME_HIFI_API_URL,))
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('db_version', '5')"
            )
            print("DB migrated to version 5: suspended Monochrome hifi-api URL retired")

        # v6: Rebuild every user's hifi-api URL list from scratch. Most of the
        # known public instances are now dead (api/eu-central suspended, entire
        # qqdl.site cluster down). Rather than matching exact strings we just
        # strip confirmed-dead entries from whatever the user had, then prepend
        # the current live defaults. Custom self-hosted URLs survive if they're
        # not on the dead list.
        if db_version < 6:
            import re as _re
            _DEAD_HIFI_URLS = {
                "https://api.monochrome.tf",
                "https://eu-central.monochrome.tf",
                "https://hifi.geeked.wtf",
                "https://maus.qqdl.site",
                "https://vogel.qqdl.site",
                "https://katze.qqdl.site",
                "https://hund.qqdl.site",
                "https://wolf.qqdl.site",
                "https://tidal.kinoplus.online",
                "https://mono.scavengerfurs.net",
            }
            _NEW_LIVE = ["https://us-west.monochrome.tf", "https://monochrome-api.samidy.com"]

            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'monochrome_hifi_api_url'"
            ).fetchone()
            current = (row[0] if row else "") or ""

            existing = []
            seen_e = set()
            for part in _re.split(r"[\s,]+", current):
                url = part.strip().rstrip("/")
                if not url or not _re.match(r"https?://", url, _re.I) or url in seen_e:
                    continue
                seen_e.add(url)
                existing.append(url)

            # Surviving custom URLs: not dead, not already in the new live defaults
            custom = [u for u in existing if u not in _DEAD_HIFI_URLS and u not in _NEW_LIVE]
            new_value = ",".join(_NEW_LIVE + custom)

            conn.execute("""
                INSERT INTO settings (key, value, updated_at)
                VALUES ('monochrome_hifi_api_url', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                               updated_at = excluded.updated_at
            """, (new_value,))
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('db_version', '6')"
            )
            print(f"DB migrated to version 6: Monochrome hifi-api URLs rebuilt → {new_value}")

        # v8: Track when a watched playlist keeps coming back "not found" so we can
        # auto-pause vanished playlists with a note instead of retrying forever.
        if db_version < 8:
            for _col8, _ddl8 in (
                ("gone_strikes", "ALTER TABLE watched_playlists ADD COLUMN gone_strikes INTEGER DEFAULT 0"),
                ("auto_paused", "ALTER TABLE watched_playlists ADD COLUMN auto_paused INTEGER DEFAULT 0"),
                ("pause_reason", "ALTER TABLE watched_playlists ADD COLUMN pause_reason TEXT"),
            ):
                try:
                    conn.execute(_ddl8)
                except sqlite3.OperationalError:
                    pass  # Column already present (fresh DB built from the new schema)
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('db_version', '8')"
            )
            print("DB migrated to version 8: watched playlists gain gone-strike auto-pause tracking")

        # v10: artists can now opt in to "automatically add new albums". The column
        # only needs adding for DBs that predate it; the watched_artist_albums table
        # itself is created unconditionally above (CREATE TABLE IF NOT EXISTS), same
        # as its watched_artist_tracks sibling, so there's nothing to migrate there.
        if db_version < 10:
            try:
                conn.execute("ALTER TABLE watched_artists ADD COLUMN auto_add_albums INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # Column already present (fresh DB built from the new schema)
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('db_version', '10')"
            )
            print("DB migrated to version 10: watched artists gain auto_add_albums, watched_artist_albums table added")

        # v11: following an artist splits into two independent switches, singles and
        # albums. Every artist watched before now was watched for singles, so the
        # column defaults to 1 and existing follows carry on exactly as they were;
        # nobody wakes up to find their favourite band quietly unfollowed.
        if db_version < 11:
            try:
                conn.execute("ALTER TABLE watched_artists ADD COLUMN watch_singles INTEGER DEFAULT 1")
            except sqlite3.OperationalError:
                pass  # Column already present (fresh DB built from the new schema)
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('db_version', '11')"
            )
            print("DB migrated to version 11: watched artists gain watch_singles")

        # A bulk import had no way of saying "I'm still going", so a worker whose
        # thread died left a row claiming to be processing until the heat death of
        # the universe. progress_at is the same heartbeat trick the jobs table
        # already uses; resume_count stops a doomed import being revived forever.
        if db_version < 12:
            for column, spec in (
                ("progress_at", "TIMESTAMP"),
                ("resume_count", "INTEGER DEFAULT 0"),
            ):
                try:
                    conn.execute(f"ALTER TABLE bulk_imports ADD COLUMN {column} {spec}")
                except sqlite3.OperationalError:
                    pass  # Column already present (fresh DB built from the new schema)
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('db_version', '12')"
            )
            print("DB migrated to version 12: bulk imports gain a progress heartbeat")

        # v13: Monochrome retired the proxy API and every public resolver died.
        # Remove the obsolete setting so old installations do not keep exposing
        # a control that no runtime path reads.
        if db_version < 13:
            conn.execute("DELETE FROM settings WHERE key = 'monochrome_qobuz_proxy_url'")
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('db_version', '13')"
            )
            print("DB migrated to version 13: obsolete Qobuz proxy setting removed")

        # v14: the known public hifi-api instances (the shipped default) all stopped
        # working once Tidal revoked their OAuth client, same fate as the Qobuz
        # proxies in v13. Clear a stored value only if it still matches one of those
        # now-dead defaults untouched; a value genuinely pointed at someone's own
        # self-hosted hifi-api is left exactly as they set it.
        if db_version < 14:
            _dead_hifi_api_urls = {
                "https://us-west.monochrome.tf",
                "https://monochrome-api.samidy.com",
                "https://api.monochrome.tf",
                "https://eu-central.monochrome.tf",
                "https://us-west.monochrome.tf,https://monochrome-api.samidy.com",
            }
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'monochrome_hifi_api_url'"
            ).fetchone()
            if row and row[0] and row[0].strip() in _dead_hifi_api_urls:
                conn.execute("DELETE FROM settings WHERE key = 'monochrome_hifi_api_url'")
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('db_version', '14')"
            )
            print("DB migrated to version 14: cleared dead default hifi-api URL, if it was still set to one")

        # v15: with redownload_missing_files switched off, a track whose file has
        # wandered off (hello, MusicBee) stays marked downloaded instead of being
        # fetched again on a loop. Count them per playlist so the card can own up
        # to why the M3U is looking thinner than the track list.
        if db_version < 15:
            try:
                conn.execute("ALTER TABLE watched_playlists ADD COLUMN unresolved_tracks INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # Column already present (fresh DB built from the new schema)
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('db_version', '15')"
            )
            print("DB migrated to version 15: watched playlists can now count tracks they have stopped chasing")

        # v16: a playlist can override the global missing-file policy, while
        # watched artists expose the same unresolved count playlists already had.
        if db_version < 16:
            try:
                conn.execute("ALTER TABLE watched_playlists ADD COLUMN redownload_missing_files INTEGER DEFAULT NULL")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE watched_artists ADD COLUMN unresolved_tracks INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('db_version', '16')"
            )
            print("DB migrated to version 16: playlists gain missing-file overrides and artists gain unresolved counts")

        # v17: job_id is a live relationship, not permanent track identity. The
        # playlist track has its own stable (playlist_id, track_hash) key, so clear
        # links whenever Queue history is deleted and tidy links left by older
        # releases. The trigger covers every deletion path, including future ones.
        if db_version < 17:
            conn.execute(
                """UPDATE watched_playlist_tracks
                   SET job_id = NULL
                   WHERE job_id IS NOT NULL
                     AND NOT EXISTS (
                         SELECT 1 FROM jobs WHERE jobs.id = watched_playlist_tracks.job_id
                     )"""
            )
            conn.execute(
                """CREATE TRIGGER IF NOT EXISTS clear_watched_playlist_track_job_after_delete
                   AFTER DELETE ON jobs
                   BEGIN
                       UPDATE watched_playlist_tracks
                       SET job_id = NULL
                       WHERE job_id = OLD.id;
                   END"""
            )
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('db_version', '17')"
            )
            print("DB migrated to version 17: cleared stale watched-playlist job links")

        # v18: Monochrome switched its Turnstile-guarded playback off and the
        # browser route went with it. Drop the orphaned toggle so Settings stops
        # offering a switch for a route that no longer exists.
        if db_version < 18:
            conn.execute("DELETE FROM settings WHERE key = 'monochrome_browser_fallback_enabled'")
            conn.execute("DELETE FROM user_settings WHERE key = 'monochrome_browser_fallback_enabled'")
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('db_version', '18')"
            )
            print("DB migrated to version 18: retired Monochrome browser playback setting removed")

        # v19: qbdlx's shared Qobuz token host went dark for good, and the route
        # went with it. Same tidy-up as v18, for the same reason.
        if db_version < 19:
            conn.execute("DELETE FROM settings WHERE key = 'monochrome_qbdlx_fallback_enabled'")
            conn.execute("DELETE FROM user_settings WHERE key = 'monochrome_qbdlx_fallback_enabled'")
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('db_version', '19')"
            )
            print("DB migrated to version 19: retired qbdlx fallback setting removed")

        # v20: nobody has a public hifi-api to point at since Tidal revoked their
        # OAuth client, and the tracks API does everything it did, so the setting
        # goes, along with MP3Phoenix's source toggle (retired in the same release).
        if db_version < 20:
            for key in ("monochrome_hifi_api_url", "source_mp3phoenix_enabled"):
                conn.execute("DELETE FROM settings WHERE key = ?", (key,))
                conn.execute("DELETE FROM user_settings WHERE key = ?", (key,))
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('db_version', '20')"
            )
            print("DB migrated to version 20: retired hifi-api and MP3Phoenix settings removed")

        # v21: album routing always files into Albums now, singles and playlists
        # alike, so the switch choosing between Singles and Albums has gone.
        if db_version < 21:
            conn.execute("DELETE FROM settings WHERE key = 'auto_album_singles_use_albums_dir'")
            conn.execute("DELETE FROM user_settings WHERE key = 'auto_album_singles_use_albums_dir'")
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('db_version', '21')"
            )
            print("DB migrated to version 21: album routing's Singles-or-Albums switch removed")

        # Defensive backstop: early dev builds of v1 silently dropped custom_subdir
        # when recreating watched_playlists. Re-add it for any DB that already
        # passed through that mangled migration. Harmless if the column is present.
        try:
            conn.execute("ALTER TABLE watched_playlists ADD COLUMN custom_subdir TEXT")
        except sqlite3.OperationalError:
            pass

        # Same shape as the custom_subdir backstop: the v1 table recreate predates
        # priority_source, so a fresh DB initialised in one go loses the column.
        # Idempotent; harmless on DBs that already have it.
        try:
            conn.execute("ALTER TABLE watched_playlists ADD COLUMN priority_source TEXT")
        except sqlite3.OperationalError:
            pass

        conn.commit()


def upsert_album_track_lock(
    release_mbid: str | None,
    album_name: str,
    album_artist: str,
    track_title: str,
    job_id: str,
) -> None:
    """Insert or update an album track lock to mark a track as in-flight.

    Safe to call multiple times for the same track; subsequent calls just update
    the job_id and bump status to 'downloading'.
    """
    with db_conn() as conn:
        if release_mbid:
            conn.execute(
                """INSERT INTO album_track_locks
                   (release_mbid, album_name, album_artist, track_title, status, job_id)
                   VALUES (?, ?, ?, ?, 'downloading', ?)
                   ON CONFLICT(release_mbid, track_title)
                   DO UPDATE SET status = 'downloading', job_id = excluded.job_id""",
                (release_mbid, album_name, album_artist, track_title, job_id),
            )
        else:
            # No MBID, folder-only routing.  Check for an existing pending row first
            # to avoid stacking up duplicates from rapid retries.
            existing = conn.execute(
                "SELECT id FROM album_track_locks "
                "WHERE release_mbid IS NULL AND album_name = ? AND track_title = ?",
                (album_name, track_title),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE album_track_locks SET status = 'downloading', job_id = ? "
                    "WHERE id = ?",
                    (job_id, existing[0]),
                )
            else:
                conn.execute(
                    """INSERT INTO album_track_locks
                       (release_mbid, album_name, album_artist, track_title, status, job_id)
                       VALUES (NULL, ?, ?, ?, 'downloading', ?)""",
                    (album_name, album_artist, track_title, job_id),
                )
        conn.commit()


def complete_album_track_lock(
    release_mbid: str | None,
    track_title: str,
    album_name: str,
) -> None:
    """Mark an album track lock as completed once the file is safely on disk."""
    with db_conn() as conn:
        if release_mbid:
            conn.execute(
                "UPDATE album_track_locks SET status = 'completed', completed_at = datetime('now') "
                "WHERE release_mbid = ? AND track_title = ?",
                (release_mbid, track_title),
            )
        else:
            conn.execute(
                "UPDATE album_track_locks SET status = 'completed', completed_at = datetime('now') "
                "WHERE release_mbid IS NULL AND album_name = ? AND track_title = ?",
                (album_name, track_title),
            )
        conn.commit()


def get_album_track_lock(
    release_mbid: str | None,
    track_title: str | None,
    album_name: str | None,
) -> dict | None:
    """Return the lock row for an album track, or None if no active lock exists."""
    if not track_title:
        return None
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        if release_mbid:
            row = conn.execute(
                "SELECT * FROM album_track_locks WHERE release_mbid = ? AND track_title = ?",
                (release_mbid, track_title),
            ).fetchone()
        elif album_name:
            row = conn.execute(
                "SELECT * FROM album_track_locks "
                "WHERE release_mbid IS NULL AND album_name = ? AND track_title = ?",
                (album_name, track_title),
            ).fetchone()
        else:
            return None
    return dict(row) if row else None


def cleanup_old_search_logs(retention_days: int = SEARCH_LOG_RETENTION_DAYS) -> int:
    """Delete search log rows older than retention window. Returns deleted row count."""
    with db_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM search_logs WHERE created_at < datetime('now', '-' || ? || ' days')",
            (int(retention_days),)
        )
        deleted = cursor.rowcount
        conn.commit()
        return deleted


def cleanup_stale_jobs(orphaned: bool = False):
    """Mark any downloading/queued jobs with no progress for STALE_JOB_TIMEOUT as failed.
    Handles cases where the background task crashed or the container restarted.
    Judged on the progress heartbeat (falling back to created_at for jobs that
    never got one), so a slow-but-alive download isn't shot for taking its time.

    `orphaned=True` is the boot pass. Every job runs in a daemon thread, so
    after a restart nothing is behind any of them; waiting out the timeout just
    meant a dead job sat on "Downloading" for a quarter of an hour looking busy,
    with no Retry button, which is how Monochrome got blamed for a restart."""
    cutoff = 0 if orphaned else STALE_JOB_TIMEOUT
    reason = "Interrupted by a restart" if orphaned else "Timed out (no progress)"
    with db_conn() as conn:
        cursor = conn.execute(
            """UPDATE jobs SET status = 'failed', error = ?,
               progress_stage = NULL, completed_at = datetime('now')
               WHERE status IN ('downloading', 'queued')
               AND COALESCE(progress_at, created_at) <= datetime('now', ? || ' seconds')""",
            (reason, str(-cutoff))
        )
        if cursor.rowcount > 0:
            print(f"Cleaned up {cursor.rowcount} stale job(s)")
            conn.execute(
                """UPDATE acquisition_attempts
                   SET status = 'failed', error = ?,
                       completed_at = datetime('now')
                   WHERE status = 'attempting'
                     AND job_id IN (
                         SELECT id FROM jobs
                         WHERE status = 'failed' AND error = ?
                     )""",
                (reason, reason),
            )
            conn.execute(
                """UPDATE acquisition_targets
                   SET status = 'failed', last_error = ?,
                       updated_at = datetime('now')
                   WHERE id IN (
                       SELECT DISTINCT target_id FROM acquisition_attempts
                       WHERE status = 'failed' AND error = ?
                   )""",
                (reason, reason),
            )

        # Evict album track locks that never reached completed status  -  these are
        # orphans from crashed workers or abandoned imports.  24-hour threshold gives
        # plenty of headroom for slow downloads without leaving ghosts forever.
        lock_cursor = conn.execute(
            "DELETE FROM album_track_locks "
            "WHERE status != 'completed' AND created_at < datetime('now', '-24 hours')"
        )
        if lock_cursor.rowcount > 0:
            print(f"Evicted {lock_cursor.rowcount} stale album track lock(s)")

        conn.commit()


def cleanup_stale_bulk_imports(orphaned: bool = False) -> tuple[int, int]:
    """Revive or bury bulk imports whose worker thread is no longer with us.

    An import runs in a daemon thread, so a crash or a container restart leaves
    the row saying 'processing' with nobody behind it, forever. That is how a
    test instance ended up with 36 of them, the oldest claiming to have been busy
    since January.

    `orphaned=True` is the boot pass: nothing survives a restart, so every
    processing row is dead by definition and there is no point waiting out the
    timeout. Otherwise we only touch imports that have gone quiet for
    STALE_BULK_IMPORT_TIMEOUT, judged on the progress heartbeat, because a
    fourteen-hundred-track import is entitled to take its time.

    Whatever we find is then either resumed (tracks left mid-search go back to
    pending, and a fresh worker picks up where the old one stopped) or failed,
    if it has used up its resumes or is simply too old to be wanted. Returns
    (resumed, failed).
    """
    cutoff = "-0 seconds" if orphaned else f"-{int(STALE_BULK_IMPORT_TIMEOUT)} seconds"
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT bi.id, bi.resume_count, bi.created_at,
                       bi.created_at < datetime('now', '-{int(STALE_BULK_IMPORT_ABANDON_AFTER)} seconds') AS too_old,
                       (bi.watch_playlist_id IS NOT NULL
                        AND NOT EXISTS (SELECT 1 FROM watched_playlists wp WHERE wp.id = bi.watch_playlist_id))
                       OR (bi.watch_artist_id IS NOT NULL
                        AND NOT EXISTS (SELECT 1 FROM watched_artists wa WHERE wa.id = bi.watch_artist_id))
                       AS orphaned_watch
                FROM bulk_imports bi
                WHERE bi.status = 'processing'
                  AND COALESCE(bi.progress_at, bi.created_at) < datetime('now', '{cutoff}')""",
        ).fetchall()
        rows = [dict(r) for r in rows]

    if not rows:
        return 0, 0

    resume_ids, fail_reasons = [], {}
    for row in rows:
        resumes = int(row["resume_count"] or 0)
        if row["orphaned_watch"]:
            # Deleting a watched playlist leaves its in-flight refresh behind.
            # Reviving that would download music for something you have already
            # thrown away, which is nobody's idea of a helpful robot.
            fail_reasons[row["id"]] = "Abandoned: the watched playlist or artist it belonged to was deleted"
        elif row["too_old"]:
            fail_reasons[row["id"]] = (
                "Abandoned: the worker stopped and the import is older than "
                f"{STALE_BULK_IMPORT_ABANDON_AFTER // 86400} days"
            )
        elif resumes >= STALE_BULK_IMPORT_MAX_RESUMES:
            fail_reasons[row["id"]] = (
                f"Gave up after {resumes} resume attempt(s); the worker keeps dying"
            )
        else:
            resume_ids.append(row["id"])

    with db_conn() as conn:
        for import_id, reason in fail_reasons.items():
            conn.execute(
                """UPDATE bulk_imports SET status = 'failed', error = ?,
                   completed_at = datetime('now') WHERE id = ?""",
                (reason, import_id)
            )
            # Tracks that never got an answer are failed alongside it, so the
            # progress numbers stop implying work is still going on somewhere.
            conn.execute(
                """UPDATE bulk_import_tracks SET status = 'failed',
                   error = 'Import abandoned before this track was searched'
                   WHERE import_id = ? AND status IN ('pending', 'searching')""",
                (import_id,)
            )
            conn.execute(
                """UPDATE acquisition_targets
                   SET status = 'failed', last_error = ?, updated_at = datetime('now')
                   WHERE id IN (
                       SELECT acquisition_target_id FROM bulk_import_tracks
                       WHERE import_id = ? AND acquisition_target_id IS NOT NULL
                   )""",
                (reason, import_id),
            )
        for import_id in resume_ids:
            # A track caught mid-search is not failed, just interrupted. Put it
            # back in the queue rather than punishing it for our crash.
            conn.execute(
                "UPDATE bulk_import_tracks SET status = 'pending' WHERE import_id = ? AND status = 'searching'",
                (import_id,)
            )
            conn.execute(
                """UPDATE acquisition_targets
                   SET status = 'failed',
                       last_error = 'Worker interrupted; starting a fresh bounded cycle',
                       updated_at = datetime('now')
                   WHERE id IN (
                       SELECT acquisition_target_id FROM bulk_import_tracks
                       WHERE import_id = ? AND status = 'pending'
                         AND acquisition_target_id IS NOT NULL
                   )""",
                (import_id,),
            )
            conn.execute(
                """UPDATE bulk_imports
                   SET resume_count = COALESCE(resume_count, 0) + 1,
                       progress_at = datetime('now'), error = NULL
                   WHERE id = ?""",
                (import_id,)
            )
        conn.commit()

    # Spawn workers only after the DB work is committed, so a fast worker cannot
    # start reading rows we are still in the middle of rewriting.
    for import_id in resume_ids:
        try:
            # Local imports: utils and bulk_import both lead back to db, and a
            # circular import at module load would ruin everyone's morning.
            from bulk_import import process_bulk_import_worker
            from utils import spawn_daemon_thread
            spawn_daemon_thread(process_bulk_import_worker, import_id)
        except Exception as e:
            print(f"Could not resume bulk import {import_id}: {e}")

    if resume_ids or fail_reasons:
        how = "orphaned at startup" if orphaned else "stalled"
        print(
            f"Bulk import maintenance: {len(resume_ids)} {how} import(s) resumed, "
            f"{len(fail_reasons)} written off"
        )
    return len(resume_ids), len(fail_reasons)


def cleanup_stale_longform_splits(orphaned: bool = False) -> int:
    """Fail any long-form split whose worker thread is no longer with us.

    Unlike bulk imports, a split doesn't resume mid-cut: ffmpeg either finished
    a segment or it didn't, and re-driving that from a stale DB row is more
    machinery than a feature nobody can retry-by-segment yet deserves. So a
    quiet split just gets marked failed. `orphaned=True` is the boot pass:
    nothing survives a restart, so every in-flight row is dead by definition.
    Returns the number of rows failed.
    """
    cutoff = "-0 seconds" if orphaned else f"-{int(STALE_LONGFORM_SPLIT_TIMEOUT)} seconds"
    with db_conn() as conn:
        cursor = conn.execute(
            f"""UPDATE longform_splits SET status = 'failed',
               error = 'Worker interrupted; the split did not finish', completed_at = datetime('now')
               WHERE status IN ('pending', 'downloading', 'splitting')
                 AND COALESCE(progress_at, created_at) < datetime('now', '{cutoff}')"""
        )
        if cursor.rowcount > 0:
            conn.execute(
                """UPDATE longform_split_segments SET status = 'failed',
                   error = 'Split abandoned before this segment was cut'
                   WHERE status IN ('pending', 'cutting', 'tagging')
                     AND split_id IN (SELECT id FROM longform_splits WHERE status = 'failed'
                                      AND error = 'Worker interrupted; the split did not finish')"""
            )
            print(f"Cleaned up {cursor.rowcount} stale long-form split(s)")
        conn.commit()
        return cursor.rowcount


def cleanup_stale_watched_refreshes():
    """Mark stuck watched playlist/artist refresh states as failed."""
    stale_arg = (str(WATCHED_REFRESH_STALE_SECONDS),)
    stale_sql = (
        "SET refresh_state = 'error', refresh_stage = 'failed',"
        " refresh_error = 'Refresh timed out (process interrupted)',"
        " refresh_completed_at = datetime('now')"
        " WHERE refresh_state = 'running'"
        " AND refresh_started_at IS NOT NULL"
        " AND refresh_started_at < datetime('now', '-' || ? || ' seconds')"
    )
    with db_conn() as conn:
        p = conn.execute(f"UPDATE watched_playlists {stale_sql}", stale_arg)
        a = conn.execute(f"UPDATE watched_artists {stale_sql}", stale_arg)
        total = p.rowcount + a.rowcount
        if total > 0:
            print(f"Cleared {total} stale watched refresh state(s)")
        conn.commit()


def reconcile_deleted_library_files(batch_size: int = 500) -> tuple[int, int]:
    """Mark completed jobs as deleted when their files no longer exist.

    This keeps `file_deleted` and watched playlist track state in sync even when
    files are removed or renamed directly on disk (outside MusicGrabber APIs).

    Returns (jobs_marked_deleted, watched_rows_unlinked).
    """
    # Local import avoids circular import: utils -> settings -> db
    from utils import check_duplicate

    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT id, artist, title, final_path, override_dir,
                      album_name, album_track_title, user_id
               FROM jobs
               WHERE status IN ('completed', 'completed_with_errors')
                 AND COALESCE(file_deleted, 0) = 0
                 AND artist IS NOT NULL AND artist != ''
                 AND title IS NOT NULL AND title != ''
               ORDER BY completed_at DESC, created_at DESC
               LIMIT ?""",
            (int(batch_size),)
        ).fetchall()

    if not rows:
        return 0, 0

    # Grab any stored resolved_paths in one shot so we can check those first.
    # check_duplicate only walks Singles; playlist-folder tracks live elsewhere.
    job_ids = [r["id"] for r in rows]
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" * len(job_ids))
        rp_rows = conn.execute(
            f"SELECT job_id, resolved_path FROM watched_playlist_tracks WHERE job_id IN ({placeholders}) AND resolved_path IS NOT NULL",
            job_ids,
        ).fetchall()
    resolved_paths: dict[str, str] = {r["job_id"]: r["resolved_path"] for r in rp_rows}

    stale_ids: list[str] = []
    recovered_paths: list[tuple[str, str]] = []
    for row in rows:
        job_id = row["id"]
        # New jobs record the exact final file. Once that truth exists, do not
        # substitute a similarly named file elsewhere in the library: the file
        # we delivered is either still there or it is not.
        final_path = (row["final_path"] or "").strip()
        if final_path:
            final = Path(final_path)
            if final.is_absolute() and final.exists():
                continue
            stale_ids.append(job_id)
            continue

        # Prefer the stored resolved_path (covers playlist folders).
        rp = resolved_paths.get(job_id)
        if rp:
            resolved = Path(rp)
            if resolved.is_absolute() and resolved.exists():
                recovered_paths.append((str(resolved), job_id))
                continue  # File is right where we left it

        # Pre-final_path album jobs need a safe migration bridge. Search only
        # the configured Albums tree, never the whole music root, and backfill
        # the exact path as soon as one is found.
        if row["override_dir"]:
            try:
                from albums import find_existing_album_track
                existing_album_file = find_existing_album_track(
                    row["artist"],
                    row["album_name"] or Path(row["override_dir"]).name,
                    row["album_track_title"] or row["title"],
                    user_id=row["user_id"],
                )
            except Exception:
                existing_album_file = None
            if existing_album_file:
                recovered_paths.append((str(existing_album_file), job_id))
                continue
        # Fall back to walking Singles layout.
        existing_single = check_duplicate(
            row["artist"], row["title"], user_id=row["user_id"]
        )
        if existing_single:
            recovered_paths.append((str(existing_single), job_id))
        else:
            stale_ids.append(job_id)

    if recovered_paths:
        with db_conn() as conn:
            conn.executemany(
                "UPDATE jobs SET final_path = ? WHERE id = ? AND final_path IS NULL",
                recovered_paths,
            )
            conn.commit()

    if not stale_ids:
        return 0, 0

    with db_conn() as conn:
        conn.executemany(
            "UPDATE jobs SET file_deleted = 1 WHERE id = ?",
            [(jid,) for jid in stale_ids],
        )
        watched_rows = 0
        for jid in stale_ids:
            cursor = conn.execute(
                """UPDATE watched_playlist_tracks
                   SET downloaded_at = NULL,
                       resolved_path = NULL
                   WHERE job_id = ?
                     AND downloaded_at IS NOT NULL""",
                (jid,),
            )
            watched_rows += cursor.rowcount
            cursor = conn.execute(
                """UPDATE watched_artist_tracks
                   SET downloaded_at = NULL,
                       resolved_path = NULL
                   WHERE job_id = ?
                     AND downloaded_at IS NOT NULL""",
                (jid,),
            )
            watched_rows += cursor.rowcount
        conn.commit()

    print(
        f"Library reconcile: marked {len(stale_ids)} job(s) as deleted, "
        f"unlinked {watched_rows} watched track row(s)"
    )
    return len(stale_ids), watched_rows


def _boot_cleanup():
    """Clear state left behind by a crash or restart. Runs once, off the boot path."""
    cleanup_stale_jobs(orphaned=True)
    cleanup_stale_watched_refreshes()
    # Nothing survives a restart, so every import still marked 'processing' is
    # orphaned by definition. No sense making it serve out the timeout first.
    cleanup_stale_bulk_imports(orphaned=True)
    cleanup_stale_longform_splits(orphaned=True)
    reconcile_deleted_library_files()
    from auth import cleanup_expired_download_tokens, cleanup_expired_sessions, cleanup_expired_stream_tokens
    cleanup_expired_sessions()
    cleanup_expired_download_tokens()
    cleanup_expired_stream_tokens()
    # Downloads killed mid-flight leave their staging sandbox behind; nothing
    # survives a restart, so anything still there is scrap.
    from downloads import sweep_staging_dirs
    sweep_staging_dirs()


def _stale_job_monitor():
    """Background thread that periodically checks for stale jobs."""
    try:
        _boot_cleanup()
    except Exception as e:
        print(f"Startup cleanup error: {e}")
    # The boot pass has just reconciled, so start the clock rather than doing
    # the whole library walk again on the very next tick.
    last_reconcile = time.time()
    while True:
        time.sleep(STALE_JOB_CHECK_INTERVAL)
        try:
            cleanup_stale_jobs()
            cleanup_stale_watched_refreshes()
            cleanup_stale_bulk_imports()
            cleanup_stale_longform_splits()
            if LIBRARY_RECONCILE_INTERVAL > 0:
                now = time.time()
                if now - last_reconcile >= LIBRARY_RECONCILE_INTERVAL:
                    reconcile_deleted_library_files()
                    last_reconcile = now
            cleanup_old_search_logs(SEARCH_LOG_RETENTION_DAYS)
            # Bin any sessions that have outstayed their welcome
            from auth import cleanup_expired_download_tokens, cleanup_expired_sessions, cleanup_expired_stream_tokens
            cleanup_expired_sessions()
            cleanup_expired_download_tokens()
            cleanup_expired_stream_tokens()
            # While we're here, evict any expired YouTube cookies so they don't
            # silently rot in settings causing mysterious 403s
            from youtube import clear_expired_cookies
            clear_expired_cookies()
        except Exception as e:
            print(f"Stale job monitor error: {e}")


def start_stale_job_monitor():
    """Start the monitor thread, which does the startup cleanup pass itself.

    Nothing here blocks: the boot cleanup walks the library to reconcile deleted
    files, which on a sleepy NAS is not the sort of thing that should stand
    between the user and a web page.
    """
    _stale_monitor_thread = threading.Thread(target=_stale_job_monitor, daemon=True)
    _stale_monitor_thread.start()


# ---------------------------------------------------------------------------
# Blacklist helpers  -  kept close to the DB layer for easy reuse
# ---------------------------------------------------------------------------

def log_match_mismatch(
    job_id: str,
    playlist_id: str,
    expected_artist: str,
    expected_title: str,
    actual_artist: str,
    actual_title: str,
    exp_normalised: str,
    got_normalised: str,
) -> None:
    """Persist a watched-track mismatch so it survives container restarts."""
    try:
        with db_conn() as conn:
            conn.execute(
                """INSERT INTO watched_match_mismatches
                   (job_id, playlist_id, expected_artist, expected_title,
                    actual_artist, actual_title, exp_normalised, got_normalised)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, playlist_id, expected_artist, expected_title,
                 actual_artist, actual_title, exp_normalised, got_normalised),
            )
            conn.commit()
    except Exception as e:
        print(f"Failed to log mismatch: {e}")


def save_search_decision(
    job_id: str,
    query: str,
    selected: dict,
    runners_up: list[dict],
) -> None:
    """Persist the scoring rationale for an automated search decision.

    Stores the winning candidate and top runners-up so users can later
    understand why the scorer picked one result over another, without
    having to tail Docker logs like some sort of animal.
    """
    try:
        blob = json.dumps({
            "selected": selected,
            "runners_up": runners_up[:3],
        })
        with db_conn() as conn:
            conn.execute(
                """INSERT INTO search_decisions (job_id, query, decision_json)
                   VALUES (?, ?, ?)""",
                (job_id, query, blob),
            )
            conn.commit()
    except Exception as e:
        print(f"Failed to save search decision: {e}")


def get_blacklisted_video_ids() -> set[str]:
    """Return all blacklisted video IDs (any source)."""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT video_id FROM blacklist WHERE video_id IS NOT NULL AND video_id != ''"
        ).fetchall()
    return {r[0] for r in rows}


def get_blacklisted_uploaders(source: str) -> set[str]:
    """Return lowercased uploader names blacklisted for a given source."""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT lower(uploader) FROM blacklist "
            "WHERE uploader IS NOT NULL AND uploader != '' AND source = ?",
            (source,)
        ).fetchall()
    return {r[0] for r in rows}


def is_video_blacklisted(video_id: str) -> bool:
    """Quick check for a single video ID."""
    if not video_id:
        return False
    with db_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM blacklist WHERE video_id = ? LIMIT 1",
            (video_id,)
        ).fetchone()
    return row is not None
