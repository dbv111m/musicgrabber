"""
MusicGrabber - Persistent library index

A disposable, rebuildable cache of facts about every audio file beneath a
library root: size, mtime, inode, codec, bitrate, duration, artist, title, and
whether the file carries our SOURCE tag.

Four things in MusicGrabber used to walk the library independently and open the
same files to learn overlapping subsets of one mutagen parse. This is that parse,
done once, written down.

Design rules, in descending order of how cross you should be if one is broken:

1. **It is a cache.** The filesystem is the source of truth. A missing,
   locked, corrupt or never-completed index is an unavailable optional hint, never an
   error. Nothing in here may raise into a caller, delay a download, or fail an
   API request. Every public function returns a "cannot help you" value instead.
2. **It lives in its own database file.** Its own connections, no ATTACH, no
   shared lock domain with the primary database. A reconciliation write cannot
   block a download, and the recovery procedure for a corrupt index is to delete
   the file and let it rebuild.
3. **It is keyed on the library root, not on the user.** A file's size, codec and
   tags are facts about the file; they do not change depending on who is asking.
   Users sharing a root share the rows. Isolation is enforced where it always
   was, by `authoritative_audio_file` checking containment on the way out.
4. **Files MusicGrabber did not download get a row too**, with `source` NULL.
   That negative cache is most of the point. On a real library 98% of the files
   are not ours, and without a row saying so we open every one of them on every
   scan, forever, to rediscover it.
"""

import os
import sqlite3
import threading
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from audio_probe import probe_file
from constants import (
    AUDIO_EXTENSIONS,
    LIBRARY_INDEX_BUSY_TIMEOUT_MS,
    LIBRARY_INDEX_DB_PATH,
    LIBRARY_INDEX_DELETE_FLOOR,
    LIBRARY_INDEX_PROGRESS_SECONDS,
    LIBRARY_INDEX_RECONCILE_HOURS,
    LIBRARY_INDEX_START_DELAY,
    LIBRARY_INDEX_WRITE_BATCH,
    MUSIC_DIR,
)
from settings import get_setting
from utils import iter_library_audio_files, path_has_excluded_scan_dir

# 3: duplicate keys now keep version brackets ("(Instrumental)", "(Live at
# Wembley)") and drop the rest. Same table shape, new key formula, so version 2
# is re-keyed in place rather than rebuilt (see _rekey_tag_identities).
SCHEMA_VERSION = 3

# Versions whose rows only need their artist/title keys recomputing from the
# tags they already store. Anything else is a shape change and starts again.
_REKEYABLE_VERSIONS = {2}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS library_roots (
    root_path             TEXT PRIMARY KEY,
    last_complete_scan_at TEXT,
    last_attempt_at       TEXT,
    last_error            TEXT,
    file_count            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS library_files (
    root_path           TEXT NOT NULL,
    relative_path       TEXT NOT NULL,
    size_bytes          INTEGER NOT NULL,
    mtime_ns            INTEGER NOT NULL,
    dev                 INTEGER,
    ino                 INTEGER,
    artist              TEXT,
    title               TEXT,
    artist_key          TEXT,
    title_key           TEXT,
    album               TEXT,
    codec               TEXT,
    bitrate_kbps        INTEGER,
    duration            REAL,
    source              TEXT,
    source_quality      TEXT,
    source_codec        TEXT,
    source_bitrate_kbps INTEGER,
    file_id             TEXT,
    indexed_at          TEXT NOT NULL,
    PRIMARY KEY (root_path, relative_path)
);

CREATE INDEX IF NOT EXISTS library_files_identity
    ON library_files (root_path, artist_key, title_key);
CREATE INDEX IF NOT EXISTS library_files_fingerprint
    ON library_files (root_path, size_bytes, mtime_ns);
CREATE INDEX IF NOT EXISTS library_files_file_id
    ON library_files (root_path, file_id);
"""

_PARSED_COLUMNS = (
    "artist", "title", "artist_key", "title_key", "album", "codec",
    "bitrate_kbps", "duration", "source", "source_quality", "source_codec",
    "source_bitrate_kbps", "file_id",
)

_init_lock = threading.Lock()
_initialised = False
_unavailable_logged = False

_scan_state_lock = threading.Lock()
_scans_running: set[str] = set()

_scheduler_running = False
_scheduler_thread: threading.Thread | None = None


# =============================================================================
# Plumbing
# =============================================================================

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def index_conn(*, read_only: bool = False):
    """A short-lived connection to the index database.

    No pool. Index reads are small and infrequent next to the primary database's
    traffic, and opening a SQLite connection costs a fraction of a millisecond;
    a second pool would be a second thing to get subtly wrong for no measurable
    gain. Writers hold one of these for a whole batch, not for a whole scan.
    Download-facing reads use read_only=True: no database creation or lock wait.
    """
    busy_timeout = 0 if read_only else LIBRARY_INDEX_BUSY_TIMEOUT_MS
    database = (
        Path(LIBRARY_INDEX_DB_PATH).absolute().as_uri() + "?mode=ro"
        if read_only else str(LIBRARY_INDEX_DB_PATH)
    )
    conn = sqlite3.connect(database, timeout=busy_timeout / 1000, uri=read_only)
    try:
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout}")
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def init_index() -> bool:
    """Create the index database if needed. False means carry on without one."""
    global _initialised, _unavailable_logged
    if _initialised:
        return True
    with _init_lock:
        if _initialised:
            return True
        try:
            Path(LIBRARY_INDEX_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
            with index_conn() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                found = conn.execute("PRAGMA user_version").fetchone()[0]
                if found in _REKEYABLE_VERSIONS:
                    changed = _rekey_tag_identities(conn)
                    print(f"Library index schema {found} re-keyed to {SCHEMA_VERSION} ({changed} rows changed)")
                elif found and found != SCHEMA_VERSION:
                    # A disposable cache does not get migration scripts. If the
                    # shape has moved on, throw the contents away and let the
                    # next reconciliation rebuild them from the disk, which is
                    # the only source of truth any of this ever had. Re-keying
                    # above is the one exception, because it never reads the
                    # disk; a rebuild leaves duplicate checks half-blind for
                    # as long as a cold build takes, which on a big library is
                    # long enough for a watched playlist to re-download things.
                    print(
                        f"Library index schema {found} is not {SCHEMA_VERSION}; "
                        "discarding and rebuilding from disk"
                    )
                    conn.executescript(
                        "DROP TABLE IF EXISTS library_files;"
                        "DROP TABLE IF EXISTS library_roots;"
                    )
                conn.executescript(_SCHEMA)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.commit()
            _initialised = True
            _unavailable_logged = False
            return True
        except Exception as exc:
            if not _unavailable_logged:
                print(f"Library index unavailable, carrying on without it: {exc}")
                _unavailable_logged = True
            return False


def reset_for_tests() -> None:
    """Forget the "already initialised" flag. Tests only."""
    global _initialised, _unavailable_logged
    with _init_lock:
        _initialised = False
        _unavailable_logged = False
    with _scan_state_lock:
        _scans_running.clear()


def _rekey_tag_identities(conn) -> int:
    """Recompute every row's artist/title keys from the tags it already stores.

    For when only the normalisation rules changed. Seconds rather than a cold
    rebuild's tens of minutes, no file is touched, and rows whose keys come
    out the same are not written. Returns how many rows changed.
    """
    updates = []
    for root, rel, artist, title, artist_key, title_key in conn.execute(
        "SELECT root_path, relative_path, artist, title, artist_key, title_key FROM library_files"
    ):
        new_artist_key, new_title_key = _tag_keys(artist, title)
        if (new_artist_key, new_title_key) != (artist_key, title_key):
            updates.append((new_artist_key, new_title_key, root, rel))
    conn.executemany(
        "UPDATE library_files SET artist_key = ?, title_key = ? WHERE root_path = ? AND relative_path = ?",
        updates,
    )
    return len(updates)


def _tag_keys(artist: str | None, title: str | None) -> tuple[str | None, str | None]:
    """Normalised Artist/Title keys, using the same rules as duplicate checks.

    Imported late: utils will import this module back once duplicate detection
    starts reading the index, and circular imports at module scope are a
    memorable way to break start-up.
    """
    from utils import _tag_identity

    if not artist or not title:
        return None, None
    key = _tag_identity(artist, title)
    if not all(key):
        return None, None
    return key


def _relative_to(root: str, path: Path | str) -> str | None:
    """`path` expressed beneath `root`, or None if it does not live there."""
    try:
        rel = Path(path).relative_to(root)
    except (ValueError, TypeError):
        return None
    return rel.as_posix()


def _signed64(value):
    """Reinterpret an unsigned 64-bit stat field as the signed one SQLite stores.

    SQLite's INTEGER is signed 64-bit. Most filesystems hand out inode numbers
    that fit comfortably, but CIFS synthesises them by hashing the file handle
    when the server will not supply real ones, and those use the full unsigned
    range. About one file in 150 on such a mount then overflows, which used to
    take the entire reconciliation down with it, every day, forever.

    Storing the same 64 bits read as signed keeps every value distinct, which is
    the only thing the hardlink grouping in `storage_totals` ever asks of them.
    Found on Karl's VM, where the mount hashes; invisible on the host beside it,
    where the very same NAS hands over real inode numbers. Filesystems, eh.
    """
    if value is None:
        return None
    value &= 0xFFFFFFFFFFFFFFFF
    return value - 0x10000000000000000 if value >= 0x8000000000000000 else value


def _prefix_range(prefix: str) -> tuple[str, str]:
    """Bounds for "every relative path inside this subdirectory".

    A range scan on the primary key, which beats LIKE both for speed and for not
    having to think about what a '%' in somebody's album title would do.
    """
    start = prefix if prefix.endswith("/") else prefix + "/"
    # '/' is 0x2F, so 0x30 ('0') is the first string that sorts past the subtree.
    return start, start[:-1] + "0"


# =============================================================================
# Reading
# =============================================================================

def root_state(root: Path | str) -> dict | None:
    """What the index knows about a root, or None if it knows nothing."""
    if not init_index():
        return None
    try:
        with index_conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM library_roots WHERE root_path = ?", (str(root),)
            ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def is_ready(root: Path | str) -> bool:
    """True once a complete walk of this root has been published."""
    state = root_state(root)
    return bool(state and state.get("last_complete_scan_at"))


def reconcile_in_progress(root: Path | str) -> bool:
    """True while a reconciliation walk owns this root.

    A cheap in-memory check, no disk I/O, so callers can ask on every request
    without needing a backoff of their own. Exists so the legacy full-library
    fallback (`utils.build_library_tag_index`) can decline to start a second
    walk that would only fight the first one for the same disk: see #93, where
    the two starved each other badly enough that the first build never finished.
    """
    with _scan_state_lock:
        return str(root) in _scans_running


def storage_totals(directories) -> dict | None:
    """`{storage_bytes, file_count}` for these directories, or None if we cannot.

    None means "ask the disk yourself": no index, no complete scan yet, or a
    directory that sits outside every root we have indexed. The caller keeps its
    existing walk for exactly that case.
    """
    if not init_index():
        return None
    try:
        roots = _indexed_roots_ready()
        if not roots:
            return None

        clauses, params = [], []
        for directory in directories:
            if directory is None:
                continue
            target = str(directory)
            owner = _owning_root(roots, target)
            if owner is None:
                return None  # Not covered by any index; the walk is still correct.
            rel = _relative_to(owner, target)
            if rel is None or rel == ".":
                clauses.append("root_path = ?")
                params.append(owner)
            else:
                start, end = _prefix_range(rel)
                clauses.append(
                    "(root_path = ? AND ((relative_path >= ? AND relative_path < ?)))"
                )
                params.extend([owner, start, end])
        if not clauses:
            return {"storage_bytes": 0, "file_count": 0}

        # Hardlinks are counted once, matching the walk this replaces. Rows with
        # no inode recorded fall back to their own path, so they can never
        # collapse into each other by accident.
        sql = f"""
            SELECT COUNT(*) AS files, COALESCE(SUM(size_bytes), 0) AS total FROM (
                SELECT size_bytes FROM library_files
                WHERE {' OR '.join(clauses)}
                GROUP BY CASE
                    WHEN dev IS NULL OR ino IS NULL THEN root_path || '/' || relative_path
                    ELSE dev || ':' || ino
                END
            )
        """
        with index_conn() as conn:
            row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return {"storage_bytes": int(row[1] or 0), "file_count": int(row[0] or 0)}
    except Exception as exc:
        print(f"Library index storage lookup failed, falling back to a walk: {exc}")
        return None


def _indexed_roots_ready() -> list[str]:
    """Roots with a complete published scan, longest first for owner matching."""
    with index_conn() as conn:
        rows = conn.execute(
            "SELECT root_path FROM library_roots WHERE last_complete_scan_at IS NOT NULL"
        ).fetchall()
    return sorted((r[0] for r in rows), key=len, reverse=True)


def _owning_root(roots: list[str], target: str) -> str | None:
    """The indexed root that contains `target`, preferring the most specific."""
    for root in roots:
        if target == root or _relative_to(root, target) is not None:
            return root
    return None


def find_by_tags(root: Path | str, artist: str | list[str], title: str) -> list[Path] | None:
    """Matching candidates in credit preference order, or None if unavailable.

    An empty list is a completed index's honest miss. Age and an active refresh
    do not invalidate its answers. Callers must validate paths before reuse.
    """
    try:
        variants = [artist] if isinstance(artist, str) else artist
        identities = [_tag_keys(variant, title) for variant in variants]
        keys = list(dict.fromkeys(key[0] for key in identities if key[0]))
        title_key = next((key[1] for key in identities if key[1]), None)
        # The download path must neither initialise/migrate the cache nor wait
        # behind an exclusive lock. Startup and the scheduler own that work.
        with index_conn(read_only=True) as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                return None
            state = conn.execute(
                "SELECT last_complete_scan_at FROM library_roots WHERE root_path = ?",
                (str(root),),
            ).fetchone()
            if not state or not state[0]:
                return None
            if not keys or not title_key:
                return []
            placeholders = ','.join('?' for _ in keys)
            rows = conn.execute(
                f"""SELECT artist_key, relative_path FROM library_files
                    WHERE root_path = ? AND artist_key IN ({placeholders}) AND title_key = ?""",
                (str(root), *keys, title_key),
            ).fetchall()
        priority = {key: rank for rank, key in enumerate(keys)}
        # SQL ORDER BY relative_path tempts SQLite to scan the whole root's
        # primary key instead of seeking by identity. Sort only the few hits.
        rows.sort(key=lambda row: (priority[row[0]], row[1]))
        return [Path(root) / row[1] for row in rows]
    except Exception:
        return None


_HINT_SQL = """
SELECT size_bytes, mtime_ns, artist, title, album, codec, bitrate_kbps,
       duration, source, source_quality, source_codec, source_bitrate_kbps,
       file_id
FROM library_files WHERE root_path = ? AND relative_path = ?
"""


def known_artists(root: Path | str) -> set[str] | None:
    """Every artist identity the library holds under `root`, or None if we cannot say.

    Returned as `_tag_keys` identities rather than display names, so callers can
    ask "do they own this?" about a name spelled differently somewhere else. None
    means the index cannot answer and the caller should not pretend the library is
    empty; an empty set would be a claim, and a wrong one.

    This exists for discovery, which is useless without it: suggesting artists
    somebody already owns is the single fastest way to make a recommendation
    feature look stupid. On a real library 61% of raw suggestions were already on
    the shelf.
    """
    # Same late import as _tag_keys, for the same reason: utils imports us back.
    from utils import artist_credit_lookup_variants

    if not init_index():
        return None
    try:
        target = str(root)
        if target not in _indexed_roots_ready():
            return None
        with index_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT artist FROM library_files "
                "WHERE root_path = ? AND artist IS NOT NULL AND artist != ''",
                (target,),
            ).fetchall()
        # Expanded through the credit variants, because the tag on disk is a
        # credit and not an identity. A library holding "Pendulum feat. Jasmine
        # Yee" does own Pendulum, and without this we would cheerfully offer them
        # Pendulum as a discovery. Worth 307 extra keys on a real library, found
        # Duke Dumont and The Qemists hiding inside collaborations, and costs a
        # tenth of a second once an hour.
        artists: set[str] = set()
        for (credit,) in rows:
            for variant in (artist_credit_lookup_variants(credit) or [credit]):
                key = _tag_keys(variant, "x")[0]
                if key:
                    artists.add(key)
        return artists
    except Exception as exc:
        print(f"Library index could not list artists: {exc}")
        return None


def _probe_shaped(row) -> dict:
    """One indexed row wearing `probe_file`'s clothes.

    Deliberately not every key `probe_file` returns: the index has never stored
    sample rate, bit depth or channels because nothing that reads it has ever
    asked for them. Anybody who needs those wants a real probe, and should say
    so by calling one.

    The numeric coercions are fidelity, not fudging. `probe_file` hands back
    integers and floats here rather than None, so a hint that returned NULL
    would be a subtly different shape for callers to trip over later.
    """
    return {
        "artist": row["artist"],
        "title": row["title"],
        "album": row["album"],
        "codec": row["codec"],
        "bitrate_kbps": int(row["bitrate_kbps"] or 0),
        "duration": float(row["duration"] or 0.0),
        "source": row["source"],
        "source_quality": row["source_quality"],
        "source_codec": row["source_codec"],
        "source_bitrate_kbps": int(row["source_bitrate_kbps"] or 0),
        "file_id": row["file_id"],
    }


def _hint_reader(conn, roots: list[str]):
    """Build the per-file lookup used by `probe_hints`."""

    def hint(path, st):
        try:
            owner = _owning_root(roots, str(path))
            if owner is None:
                return None
            rel = _relative_to(owner, path)
            if rel is None:
                return None
            row = conn.execute(_HINT_SQL, (owner, rel)).fetchone()
            if row is None:
                return None
            # The same fingerprint the reconciliation itself trusts, and a
            # stricter one than the callers' own caches use: nanoseconds, not
            # whole seconds. A file that has moved on since the last pass simply
            # gets no answer, and the caller opens it exactly as it always did.
            if row["size_bytes"] != st.st_size or row["mtime_ns"] != st.st_mtime_ns:
                return None
            return _probe_shaped(row)
        except Exception:
            return None

    return hint


@contextmanager
def probe_hints():
    """One held-open reader answering "do I really need to open this file?".

    Yields a callable `hint(path, stat)` returning a probe-shaped dict, or None
    meaning "no idea, open it yourself". None is always a safe answer, so a
    missing, locked or half-migrated index costs the caller nothing but the
    speed it had before.

    This is the negative cache finally being spent. On a real library 98% of the
    files are not ours (19,874 of 20,371 on Karl's) and can never become upgrade
    candidates, yet every scan opened all the ones in its scope to rediscover
    that, at roughly 24 ms a file over SMB, to learn nothing whatsoever. For the
    upgrade scan that was 1,287 of the 1,673 files it walks, and 31.4 of the
    33.6 seconds it took.

    The connection is opened once and reused, because at twenty thousand files a
    connection per lookup would have quietly eaten a good chunk of the winnings.
    The lookups themselves are read-only, so they cannot wait behind anybody's
    lock; unlike the download path this will happily create the database on a
    fresh install first, because background housekeeping can afford to.

    A returned dict with `codec` None is how the index records "mutagen could not
    read this one either". The row exists precisely so that nobody tries again.
    """
    def unavailable(path, st):
        return None

    hint = unavailable
    with ExitStack() as stack:
        try:
            if init_index():
                conn = stack.enter_context(index_conn(read_only=True))
                conn.row_factory = sqlite3.Row
                if conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
                    roots = [
                        r[0] for r in conn.execute(
                            "SELECT root_path FROM library_roots "
                            "WHERE last_complete_scan_at IS NOT NULL"
                        ).fetchall()
                    ]
                    if roots:
                        hint = _hint_reader(conn, sorted(roots, key=len, reverse=True))
        except Exception:
            hint = unavailable
        yield hint


def paths_under(directory: Path | str) -> list[Path] | None:
    """Every indexed audio file beneath a directory, or None if we cannot say.

    None means "walk it yourself": no index, no completed scan, or a directory
    that sits outside every root we hold. An empty list is therefore always an
    honest "that folder has no audio in it", never a shrug, which matters
    because the caller creates database rows from what comes back.
    """
    if not init_index():
        return None
    try:
        roots = _indexed_roots_ready()
        if not roots:
            return None
        target = str(directory)
        owner = _owning_root(roots, target)
        if owner is None:
            return None
        rel = _relative_to(owner, target)
        with index_conn() as conn:
            if rel is None or rel == ".":
                rows = conn.execute(
                    "SELECT relative_path FROM library_files WHERE root_path = ?",
                    (owner,),
                ).fetchall()
            else:
                start, end = _prefix_range(rel)
                rows = conn.execute(
                    "SELECT relative_path FROM library_files WHERE root_path = ? "
                    "AND relative_path >= ? AND relative_path < ?",
                    (owner, start, end),
                ).fetchall()
        base = Path(owner)
        return [base / row[0] for row in rows]
    except Exception as exc:
        print(f"Library index path listing failed, falling back to a walk: {exc}")
        return None


def file_count(root: Path | str) -> int:
    """Rows held for a root. Diagnostics and tests."""
    if not init_index():
        return 0
    try:
        with index_conn() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM library_files WHERE root_path = ?", (str(root),)
                ).fetchone()[0]
            )
    except Exception:
        return 0


# =============================================================================
# Reconciliation
# =============================================================================

def _row_values(info: dict | None) -> tuple:
    """The parsed columns, in `_PARSED_COLUMNS` order, from one probe result.

    `info is None` means mutagen could not read the file. That still earns a
    row: remembering "this one is not worth opening" is the entire point of the
    negative cache, and a file we cannot parse is the clearest case of it.
    """
    if info is None:
        return (None,) * len(_PARSED_COLUMNS)
    artist = info.get("artist")
    title = info.get("title")
    artist_key, title_key = _tag_keys(artist, title)
    return (
        artist, title, artist_key, title_key, info.get("album"), info.get("codec"),
        info.get("bitrate_kbps"), info.get("duration"), info.get("source"),
        info.get("source_quality"), info.get("source_codec"),
        info.get("source_bitrate_kbps"), info.get("file_id"),
    )


_UPSERT = f"""
INSERT INTO library_files
    (root_path, relative_path, size_bytes, mtime_ns, dev, ino,
     {', '.join(_PARSED_COLUMNS)}, indexed_at)
VALUES ({', '.join('?' * (6 + len(_PARSED_COLUMNS) + 1))})
ON CONFLICT(root_path, relative_path) DO UPDATE SET
    size_bytes = excluded.size_bytes,
    mtime_ns = excluded.mtime_ns,
    dev = excluded.dev,
    ino = excluded.ino,
    {', '.join(f'{c} = excluded.{c}' for c in _PARSED_COLUMNS)},
    indexed_at = excluded.indexed_at
"""


def reconcile(root: Path | str, *, walker=None, probe=None) -> dict:
    """Bring one library root's index back in line with what is on disk.

    Returns a summary dict. Never raises; a failure is reported in `error` and
    leaves every previously published row exactly where it was.
    """
    summary = {
        "root": str(root), "walked": 0, "unchanged": 0, "renamed": 0, "probed": 0,
        "deleted": 0, "delete_skipped": False, "ran": False, "error": None,
    }
    if not init_index():
        summary["error"] = "index unavailable"
        return summary

    root_str = str(root)
    try:
        if not Path(root_str).is_dir():
            summary["error"] = f"root is not a directory: {root_str}"
            return summary
    except OSError as exc:
        summary["error"] = f"root unavailable: {exc}"
        return summary

    with _scan_state_lock:
        if root_str in _scans_running:
            summary["error"] = "a reconciliation is already running for this root"
            return summary
        _scans_running.add(root_str)
    try:
        summary["ran"] = True
        return _reconcile_locked(root_str, summary, walker=walker, probe=probe)
    except Exception as exc:
        summary["error"] = str(exc)
        _record_failure(root_str, str(exc))
        print(f"Library index reconciliation failed for {root_str}: {exc}")
        return summary
    finally:
        with _scan_state_lock:
            _scans_running.discard(root_str)


def _reconcile_locked(root: str, summary: dict, *, walker=None, probe=None) -> dict:
    walk = walker or iter_library_audio_files
    probe_fn = probe or probe_file
    started = _now()

    with index_conn() as conn:
        conn.row_factory = sqlite3.Row
        existing = {
            r["relative_path"]: r
            for r in conn.execute(
                "SELECT * FROM library_files WHERE root_path = ?", (root,)
            ).fetchall()
        }
        previous = conn.execute(
            "SELECT file_count FROM library_roots WHERE root_path = ?", (root,)
        ).fetchone()
        previous_count = int(previous["file_count"]) if previous else 0

    # Fingerprint index for rename detection: an organiser that moved a file
    # without touching it leaves size and mtime exactly where they were.
    by_fingerprint: dict[tuple[int, int], list[str]] = {}
    for rel, row in existing.items():
        by_fingerprint.setdefault((row["size_bytes"], row["mtime_ns"]), []).append(rel)

    upserts: list[tuple] = []
    seen: set[str] = set()
    # Orphan rows already claimed by a rename. Kept separate from `seen`, which
    # means "still on disk at this path": a claimed orphan is neither, and it
    # still needs pruning at the end.
    consumed: set[str] = set()

    def flush():
        if not upserts:
            return
        with index_conn() as conn:
            conn.executemany(_UPSERT, upserts)
            conn.commit()
        upserts.clear()

    # Progress reporting, so a twenty minute cold build is distinguishable from
    # a twenty minute hang. Borrowed from Michael Strong's persistent-library-index
    # branch, which had the good manners to say something while it worked.
    last_progress = time.monotonic()

    for entry in walk(Path(root), strict=True):
        path, st = entry.path, entry.stat
        rel = _relative_to(root, path)
        if rel is None:
            continue
        summary["walked"] += 1
        seen.add(rel)
        cached = existing.get(rel)

        if (
            cached is not None
            and cached["size_bytes"] == st.st_size
            and cached["mtime_ns"] == st.st_mtime_ns
        ):
            # Nothing to say and nobody to say it to. A row that still matches
            # the disk needs no write: the prune below works from `seen`, which
            # is already in hand, so recording "still here" 20,000 times would
            # be 20,000 writes nothing ever reads.
            summary["unchanged"] += 1
        else:
            parsed = None
            if cached is None:
                parsed = _rename_of(root, existing, by_fingerprint, seen, consumed, st)
            if parsed is not None:
                summary["renamed"] += 1
            else:
                parsed = _row_values(probe_fn(path))
                summary["probed"] += 1
            upserts.append(
                (root, rel, st.st_size, st.st_mtime_ns,
             _signed64(st.st_dev), _signed64(st.st_ino))
                + tuple(parsed) + (started,)
            )

        if len(upserts) >= LIBRARY_INDEX_WRITE_BATCH:
            flush()

        now = time.monotonic()
        if now - last_progress >= LIBRARY_INDEX_PROGRESS_SECONDS:
            last_progress = now
            print(
                f"Library index: {root}: {summary['walked']} files seen, "
                f"{summary['probed']} read, {summary['renamed']} moved, "
                f"{summary['unchanged']} unchanged so far",
                flush=True,
            )

    flush()

    # The delete guard. Everything above degrades to "slower than it could be";
    # this is the one step that can quietly leave the user without an index.
    stale = [rel for rel in existing if rel not in seen]
    floor = previous_count * LIBRARY_INDEX_DELETE_FLOOR
    if stale and previous_count > 0 and summary["walked"] < floor:
        summary["delete_skipped"] = True
        message = (
            f"refused to prune {len(stale)} rows: the walk saw {summary['walked']} "
            f"files where the last complete scan saw {previous_count}. The index is "
            f"unchanged; check the mount."
        )
        print(f"Library index: {message}")
        _record_failure(root, message, attempted_at=started)
        return summary

    if stale:
        with index_conn() as conn:
            conn.executemany(
                "DELETE FROM library_files WHERE root_path = ? AND relative_path = ?",
                [(root, rel) for rel in stale],
            )
            conn.commit()
        summary["deleted"] = len(stale)

    with index_conn() as conn:
        conn.execute(
            """INSERT INTO library_roots
                   (root_path, last_complete_scan_at, last_attempt_at, last_error, file_count)
               VALUES (?, ?, ?, NULL, ?)
               ON CONFLICT(root_path) DO UPDATE SET
                   last_complete_scan_at = excluded.last_complete_scan_at,
                   last_attempt_at = excluded.last_attempt_at,
                   last_error = NULL,
                   file_count = excluded.file_count""",
            (root, _now(), started, summary["walked"]),
        )
        conn.commit()
    return summary


def _rename_of(root, existing, by_fingerprint, seen, consumed, st) -> tuple | None:
    """Parsed columns inherited from a file that merely moved, if this is one.

    A new path whose size and mtime match a row whose own file has since
    vanished is an organiser's rename, not a new file, so its tags are already
    known and there is nothing to open. Confirming the old path really is gone
    costs one stat, only ever on a genuinely new path, and it is what stops two
    coincidentally identical files swapping identities.
    """
    for candidate in by_fingerprint.get((st.st_size, st.st_mtime_ns), []):
        if candidate in seen or candidate in consumed:
            continue
        try:
            if (Path(root) / candidate).exists():
                continue
        except OSError:
            continue
        consumed.add(candidate)
        row = existing[candidate]
        return tuple(row[column] for column in _PARSED_COLUMNS)
    return None


def _record_failure(root: str, message: str, attempted_at: str | None = None) -> None:
    """Note that a pass did not publish. Published rows are left alone."""
    try:
        with index_conn() as conn:
            conn.execute(
                """INSERT INTO library_roots (root_path, last_attempt_at, last_error)
                   VALUES (?, ?, ?)
                   ON CONFLICT(root_path) DO UPDATE SET
                       last_attempt_at = excluded.last_attempt_at,
                       last_error = excluded.last_error""",
                (root, attempted_at or _now(), message[:500]),
            )
            conn.commit()
    except Exception:
        pass


# =============================================================================
# Hooks. Called the moment we move one of our own files, so the index does not
# spend a day believing yesterday's arrangement.
# =============================================================================

def _indexable(rel: str) -> bool:
    """Whether the walker would ever hand us this file.

    Not everything a completed job points at is audio. A watched playlist records
    its .m3u as the job's final path, and the download plumbing shuffles .lrc
    sidecars about with the same helpers as the music. The index holds rows for
    audio, so anything else is declined here rather than at every call site.

    The directory check is for libraries with a bin inside the share: MusicGrabber
    keeps its own trash and quarantine under /data, but Synology, QNAP and Windows
    all park deletions in a hidden folder within the share itself, and the walker
    skips those. A row for a file no scan will ever see again is a row the next
    reconciliation would only delete.

    Judged on the root-relative path deliberately. A share mounted beneath
    something like /mnt/@recycle is nobody's business but the mount's.
    """
    path = Path(rel)
    if path.suffix.lower() not in AUDIO_EXTENSIONS:
        return False
    return not path_has_excluded_scan_dir(path)


def index_file(path: Path | str) -> bool:
    """Index one file immediately, so our own downloads do not wait for a scan.

    True means the index now describes this file, which includes the case where
    it already did and nothing needed reading. False means it does not, and the
    daily reconciliation will sort it out: no root owns the path, the path is not
    something a scan would ever yield, or the index is simply unavailable.
    """
    if not init_index():
        return False
    try:
        root = _owning_root(_known_roots(), str(path))
        if root is None:
            return False
        rel = _relative_to(root, path)
        if rel is None or not _indexable(rel):
            return False
        st = os.stat(path)
        with index_conn() as conn:
            known = conn.execute(
                "SELECT size_bytes, mtime_ns FROM library_files "
                "WHERE root_path = ? AND relative_path = ?",
                (root, rel),
            ).fetchone()
        # The same fingerprint gate the reconciliation and the upgrade hint both
        # use. A download that turned out to be a duplicate still records a
        # completed job, and reopening a file we already hold current facts about
        # would be exactly the work this whole index exists to stop; a bulk import
        # of mostly-owned tracks would pay 24ms a piece over SMB for nothing.
        # Anything that genuinely changed the file moves one of these two numbers,
        # and callers that changed a file on purpose remove the row first.
        if known and known[0] == st.st_size and known[1] == st.st_mtime_ns:
            return True
        values = (
            (root, rel, st.st_size, st.st_mtime_ns,
             _signed64(st.st_dev), _signed64(st.st_ino))
            + _row_values(probe_file(Path(path))) + (_now(),)
        )
        with index_conn() as conn:
            conn.execute(_UPSERT, values)
            conn.commit()
        return True
    except Exception as exc:
        print(f"Library index could not record {path}: {exc}")
        return False


def remove_file(path: Path | str) -> bool:
    """Forget one file. A miss is a success; the row was already gone."""
    if not init_index():
        return False
    try:
        root = _owning_root(_known_roots(), str(path))
        rel = _relative_to(root, path) if root else None
        if rel is None:
            return False
        with index_conn() as conn:
            conn.execute(
                "DELETE FROM library_files WHERE root_path = ? AND relative_path = ?",
                (root, rel),
            )
            conn.commit()
        return True
    except Exception as exc:
        print(f"Library index could not forget {path}: {exc}")
        return False


def move_file(old_path: Path | str, new_path: Path | str) -> bool:
    """Follow one of our own moves without reopening the file."""
    if not init_index():
        return False
    try:
        roots = _known_roots()
        old_root = _owning_root(roots, str(old_path))
        new_root = _owning_root(roots, str(new_path))
        old_rel = _relative_to(old_root, old_path) if old_root else None
        new_rel = _relative_to(new_root, new_path) if new_root else None
        # Not indexable is not indexed, which for a move means the same thing as
        # being outside the roots entirely, so a file moved into a bin the scans
        # skip is a departure rather than a rename.
        old_rel = old_rel if old_rel and _indexable(old_rel) else None
        new_rel = new_rel if new_rel and _indexable(new_rel) else None
        if old_rel is None or new_rel is None:
            # Moved in or out of the indexed world entirely; index the arrival
            # and forget the departure, rather than pretending it was a rename.
            removed = remove_file(old_path) if old_rel else False
            return index_file(new_path) if new_rel else removed
        with index_conn() as conn:
            conn.execute(
                "DELETE FROM library_files WHERE root_path = ? AND relative_path = ?",
                (new_root, new_rel),
            )
            conn.execute(
                """UPDATE library_files SET root_path = ?, relative_path = ?, indexed_at = ?
                   WHERE root_path = ? AND relative_path = ?""",
                (new_root, new_rel, _now(), old_root, old_rel),
            )
            conn.commit()
        return True
    except Exception as exc:
        print(f"Library index could not follow {old_path} to {new_path}: {exc}")
        return False


def _known_roots() -> list[str]:
    """Every root the index holds rows for, most specific first."""
    try:
        with index_conn() as conn:
            rows = conn.execute("SELECT root_path FROM library_roots").fetchall()
        return sorted((r[0] for r in rows), key=len, reverse=True)
    except Exception:
        return []


# =============================================================================
# Scheduling
# =============================================================================

def configured_roots() -> list[str]:
    """Every distinct music root across the global and per-user settings.

    Distinct, because the index is keyed on the root: three users pointed at the
    same library is one set of rows, not three identical ones.
    """
    roots = {str(Path(get_setting("music_dir", str(MUSIC_DIR))))}
    try:
        from db import db_conn

        with db_conn() as conn:
            user_ids = [r[0] for r in conn.execute("SELECT id FROM users").fetchall()]
        for user_id in user_ids:
            roots.add(str(Path(get_setting("music_dir", str(MUSIC_DIR), user_id=user_id))))
    except Exception as exc:
        print(f"Library index could not read per-user music roots: {exc}")
    return sorted(roots)


def reconcile_all() -> list[dict]:
    """One reconciliation per configured root."""
    return [reconcile(root) for root in configured_roots()]


def library_index_scheduler():
    """Background loop: reconcile each root against the disk, once a day."""
    print(
        f"Library index scheduler started (every {LIBRARY_INDEX_RECONCILE_HOURS}h, "
        f"first pass in {LIBRARY_INDEX_START_DELAY}s)"
    )
    slept = 0
    while slept < LIBRARY_INDEX_START_DELAY and _scheduler_running:
        time.sleep(5)
        slept += 5

    while _scheduler_running:
        try:
            for summary in reconcile_all():
                if summary["error"]:
                    print(f"Library index: {summary['root']}: {summary['error']}")
                elif summary["ran"]:
                    print(
                        f"Library index: {summary['root']}: {summary['walked']} files "
                        f"({summary['unchanged']} unchanged, {summary['renamed']} moved, "
                        f"{summary['probed']} read, {summary['deleted']} gone)"
                    )
        except Exception as exc:
            print(f"Library index scheduler error: {exc}")

        sleep_seconds = max(3600, LIBRARY_INDEX_RECONCILE_HOURS * 3600)
        elapsed = 0
        while elapsed < sleep_seconds and _scheduler_running:
            time.sleep(60)
            elapsed += 60


def start_library_index_scheduler() -> None:
    """Start the reconciliation loop once. Safe to call more than once."""
    global _scheduler_running, _scheduler_thread
    if _scheduler_running:
        return
    if not init_index():
        return
    _scheduler_running = True
    _scheduler_thread = threading.Thread(target=library_index_scheduler, daemon=True)
    _scheduler_thread.start()


def stop_library_index_scheduler() -> None:
    global _scheduler_running
    _scheduler_running = False
