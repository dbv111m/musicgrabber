"""Unit tests for the persistent library index (Phase 3a) and its first consumer.

Pure unit tests: a real temporary directory for the files, a real temporary
SQLite file for the index, and a fake probe so nothing has to be genuine audio.
The index is a cache, so most of these tests are really asking one question in
different ways: does it decline to help, rather than break, when something is
wrong?
"""

import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import library_index
import library_scans
import utils


@pytest.fixture
def index(tmp_path, monkeypatch):
    """A library index pointed at a throwaway database and a throwaway library."""
    monkeypatch.setattr(library_index, "LIBRARY_INDEX_DB_PATH", tmp_path / "index.db")
    library_index.reset_for_tests()
    yield library_index
    library_index.reset_for_tests()


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "music"
    (root / "Fatboy Slim").mkdir(parents=True)
    return root


@pytest.fixture
def duplicate_library(index, library, monkeypatch):
    monkeypatch.setattr(utils, "get_setting", lambda *a, **kw: str(library))
    monkeypatch.setattr(utils, "get_download_dir", lambda *a, **kw: library / "Singles")
    monkeypatch.setattr(utils, "get_singles_dir", lambda *a, **kw: library / "Singles")
    monkeypatch.setattr(utils, "get_albums_dir", lambda *a, **kw: library / "Albums")
    monkeypatch.setattr(utils, "get_playlists_dir", lambda *a, **kw: None)
    monkeypatch.setattr(utils, "_tag_index_cache", {})
    monkeypatch.setattr(utils, "_tag_index_running", {})
    monkeypatch.setattr(utils, "_tag_index_errors", {})
    return library


def test_duplicate_reads_persisted_tags_after_restart_without_scanning(index, duplicate_library, monkeypatch):
    track = _track(duplicate_library, "001 renamed.flac")
    index.reconcile(duplicate_library, probe=Probe(returns={"artist": "Fatboy Slim", "title": "Praise You"}))
    index.reset_for_tests()
    scans = []
    monkeypatch.setattr(utils, "build_library_tag_index", lambda **kw: scans.append(1) or {})
    assert utils.check_duplicate("Fatboy Slim", "Praise You", "alice") == track
    assert utils.check_duplicate("Fatboy Slim", "Missing", "alice") is None
    assert scans == []
    assert utils._tag_index_cache == {}
    assert utils._tag_index_running == {}


def test_artist_variants_use_one_query_and_keep_credit_preference(index, duplicate_library, monkeypatch):
    primary = _track(duplicate_library, "a.flac")
    full = _track(duplicate_library, "z.flac")
    def probe(path):
        return {"artist": "Disclosure" if path == primary else "Disclosure feat. Sam Smith", "title": "Latch"}
    index.reconcile(duplicate_library, probe=probe)
    statements = []
    real_conn = index.index_conn
    @contextmanager
    def traced_conn(**kwargs):
        with real_conn(**kwargs) as conn:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 0
            conn.set_trace_callback(statements.append)
            yield conn
    monkeypatch.setattr(index, "index_conn", traced_conn)
    assert utils._tagged_duplicate("Disclosure feat. Sam Smith", "Latch", "alice") == full
    queries = [sql for sql in statements if "FROM library_files" in sql]
    assert len(queries) == 1
    assert "artist_key IN (" in queries[0]
    with real_conn(read_only=True) as conn:
        plan = conn.execute("EXPLAIN QUERY PLAN " + queries[0]).fetchall()
    assert any("USING INDEX library_files_identity" in row[3] for row in plan)
    full.unlink()
    assert utils._tagged_duplicate("Disclosure feat. Sam Smith", "Latch", "alice") == primary


def test_stale_persistent_index_answers_during_refresh(index, duplicate_library, monkeypatch):
    track = _track(duplicate_library, "Praise You.flac")
    index.reconcile(duplicate_library, probe=Probe())
    with index.index_conn() as conn:
        conn.execute("UPDATE library_roots SET last_complete_scan_at = '2000-01-01', last_error = 'failed refresh'")
        conn.commit()
    index._scans_running.add(str(duplicate_library))
    assert utils._tagged_duplicate("Fatboy Slim", "Praise You", "alice") == track
    assert utils._tagged_duplicate("Missing", "Recording", "alice") is None
    assert utils._tag_index_running == {}


def test_first_build_in_progress_declines_the_legacy_fallback_walk(index, duplicate_library, monkeypatch):
    """Regression for #93: on a not-yet-indexed root, the legacy tag crawl used
    to start right alongside the persistent index's own first walk, and the two
    starved each other over disk I/O badly enough that the first build never
    completed. While a reconciliation owns the root, the fallback must decline
    rather than pile on."""
    scans = []
    monkeypatch.setattr(utils, "build_library_tag_index", lambda **kw: scans.append(1) or {})
    assert index.reconcile_in_progress(duplicate_library) is False
    index._scans_running.add(str(duplicate_library))
    assert index.reconcile_in_progress(duplicate_library) is True
    assert utils._tagged_duplicate("Fatboy Slim", "Praise You", "alice") is None
    assert scans == []
    assert utils._tag_index_running == {}


@pytest.mark.parametrize("invalid", ["missing", "symlink", "outside", "excluded"])
def test_duplicate_skips_unsafe_indexed_candidates(index, duplicate_library, tmp_path, invalid):
    first = _track(duplicate_library, "a.flac")
    keeper = _track(duplicate_library, "z.flac")
    index.reconcile(duplicate_library, probe=Probe(returns={"artist": "Blur", "title": "Song 2"}))
    first.unlink()
    outside = tmp_path / "outside.flac"
    outside.write_bytes(b"audio")
    if invalid == "symlink":
        first.symlink_to(outside)
    elif invalid in ("outside", "excluded"):
        rel = "../outside.flac" if invalid == "outside" else ".mg_staging/a.flac"
        if invalid == "excluded":
            path = duplicate_library / rel
            path.parent.mkdir()
            path.write_bytes(b"audio")
        with index.index_conn() as conn:
            conn.execute("UPDATE library_files SET relative_path = ? WHERE relative_path = ?", (rel, first.relative_to(duplicate_library).as_posix()))
            conn.commit()
    assert utils._tagged_duplicate("Blur", "Song 2", "alice") == keeper


def test_duplicate_scopes_candidates_to_user_root(index, duplicate_library, tmp_path, monkeypatch):
    track = _track(duplicate_library, "Praise You.flac")
    index.reconcile(duplicate_library, probe=Probe())
    other = tmp_path / "other"
    other.mkdir()
    index.reconcile(other, probe=Probe())
    monkeypatch.setattr(utils, "get_setting", lambda *a, user_id=None, **kw: str(other if user_id == "bob" else duplicate_library))
    assert utils._tagged_duplicate("Fatboy Slim", "Praise You", "alice") == track
    assert utils._tagged_duplicate("Fatboy Slim", "Praise You", "shared") == track
    assert utils._tagged_duplicate("Fatboy Slim", "Praise You", "bob") is None


@pytest.mark.parametrize("state", ["cold", "partial", "corrupt", "locked"])
def test_unavailable_tag_index_uses_existing_nonblocking_fallback(index, duplicate_library, monkeypatch, state):
    index.init_index()
    held = None
    if state == "partial":
        with index.index_conn() as conn:
            conn.execute("INSERT INTO library_roots (root_path) VALUES (?)", (str(duplicate_library),))
            conn.commit()
        path = _track(duplicate_library, "Praise You.flac")
        monkeypatch.setattr(index, "probe_file", Probe())
        assert index.index_file(path)
    elif state == "corrupt":
        Path(index.LIBRARY_INDEX_DB_PATH).write_bytes(b"not a database")
    elif state == "locked":
        held = sqlite3.connect(index.LIBRARY_INDEX_DB_PATH)
        held.execute("PRAGMA journal_mode=DELETE")
        held.execute("BEGIN EXCLUSIVE")
    # A previous complete fallback snapshot may still answer immediately.
    fallback = _track(duplicate_library, "fallback.flac")
    import time
    utils._tag_index_cache[("alice", str(duplicate_library))] = (
        time.monotonic(), {utils._tag_identity("Blur", "Song 2"): fallback},
    )
    try:
        assert index.find_by_tags(duplicate_library, ["Blur"], "Song 2") is None
        assert utils._tagged_duplicate("Blur", "Song 2", "alice") == fallback
    finally:
        if held:
            held.close()


def test_tag_lookup_does_not_create_or_migrate_database(index, library):
    assert index.find_by_tags(library, ["Blur"], "Song 2") is None
    assert not Path(index.LIBRARY_INDEX_DB_PATH).exists()
    index.init_index()
    with index.index_conn() as conn:
        conn.execute("PRAGMA user_version = 999")
    assert index.find_by_tags(library, ["Blur"], "Song 2") is None
    with index.index_conn() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 999


def test_filename_match_does_not_query_index(index, duplicate_library, monkeypatch):
    singles = duplicate_library / "Singles"
    singles.mkdir()
    track = singles / "Praise You.flac"
    track.write_bytes(b"audio")
    def unexpected(*a, **kw):
        pytest.fail("filename matches must not need the tag index")
    monkeypatch.setattr(index, "find_by_tags", unexpected)
    assert utils.check_duplicate("Fatboy Slim", "Praise You", "alice") == track


def _track(root, name, payload=b"audio", artist="Fatboy Slim"):
    path = root / "Fatboy Slim" / name
    path.write_bytes(payload)
    return path


class Probe:
    """A stand-in for probe_file that counts and can be told to fail."""

    def __init__(self, source="youtube", returns=None):
        self.calls = []
        self.source = source
        self.returns = returns

    def __call__(self, path):
        self.calls.append(str(path))
        if self.returns is not None:
            return self.returns
        return {
            "artist": "Fatboy Slim", "title": Path(path).stem, "album": "Better Living",
            "codec": "flac", "bitrate_kbps": 900, "duration": 300.0,
            "source": self.source, "source_quality": None, "source_codec": None,
            "source_bitrate_kbps": 0, "file_id": "fid-" + Path(path).stem,
        }


# --- the basics --------------------------------------------------------------

def test_cold_scan_opens_every_file_once(index, library):
    _track(library, "Praise You.flac")
    _track(library, "Rockafeller Skank.flac")
    probe = Probe()

    summary = index.reconcile(library, probe=probe)

    assert summary["walked"] == 2
    assert summary["probed"] == 2
    assert len(probe.calls) == 2
    assert index.file_count(library) == 2
    assert index.is_ready(library)


def test_unchanged_files_are_never_opened_again(index, library):
    _track(library, "Praise You.flac")
    probe = Probe()
    index.reconcile(library, probe=probe)
    probe.calls.clear()

    summary = index.reconcile(library, probe=probe)

    assert probe.calls == []
    assert summary["unchanged"] == 1
    assert summary["probed"] == 0


def test_a_changed_file_is_reopened(index, library):
    track = _track(library, "Praise You.flac")
    probe = Probe()
    index.reconcile(library, probe=probe)
    probe.calls.clear()

    track.write_bytes(b"a different recording entirely")
    summary = index.reconcile(library, probe=probe)

    assert len(probe.calls) == 1
    assert summary["probed"] == 1
    assert summary["unchanged"] == 0


def test_a_rename_is_followed_without_reading_a_single_tag(index, library):
    """The case the whole feature exists for: an organiser moved things overnight."""
    track = _track(library, "Praise You.flac")
    probe = Probe()
    index.reconcile(library, probe=probe)
    probe.calls.clear()

    stat = track.stat()
    moved = library / "Fatboy Slim" / "01 - Praise You.flac"
    track.rename(moved)
    os.utime(moved, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    summary = index.reconcile(library, probe=probe)

    assert probe.calls == []
    assert summary["renamed"] == 1
    assert summary["deleted"] == 1
    assert index.file_count(library) == 1
    assert index.find_by_tags(library, "Fatboy Slim", "Praise You") == [moved]


def test_a_deleted_file_is_pruned(index, library):
    keeper = _track(library, "Praise You.flac")
    doomed = _track(library, "Rockafeller Skank.flac")
    index.reconcile(library, probe=Probe())

    doomed.unlink()
    summary = index.reconcile(library, probe=Probe())

    assert summary["deleted"] == 1
    assert index.file_count(library) == 1
    assert index.find_by_tags(library, "Fatboy Slim", "Praise You") == [keeper]


# --- the negative cache, which is most of the point --------------------------

def test_a_file_we_did_not_download_is_remembered_as_not_ours(index, library):
    _track(library, "Someone Elses Record.flac")
    probe = Probe(source=None)

    index.reconcile(library, probe=probe)
    probe.calls.clear()
    index.reconcile(library, probe=probe)

    assert probe.calls == []
    with sqlite3.connect(str(index.LIBRARY_INDEX_DB_PATH)) as conn:
        assert conn.execute("SELECT source FROM library_files").fetchone()[0] is None


def test_an_unreadable_file_is_still_only_opened_once(index, library):
    """A file mutagen cannot parse is the clearest case for remembering it."""
    _track(library, "Corrupt.flac")

    class UnreadableProbe(Probe):
        def __call__(self, path):
            self.calls.append(str(path))
            return None

    probe = UnreadableProbe()
    index.reconcile(library, probe=probe)
    assert len(probe.calls) == 1
    probe.calls.clear()

    index.reconcile(library, probe=probe)
    assert probe.calls == []
    assert index.file_count(library) == 1


# --- the delete guard --------------------------------------------------------

def test_the_delete_guard_refuses_to_publish_a_collapsed_walk(index, library):
    for n in range(10):
        _track(library, f"Track {n}.flac")
    index.reconcile(library, probe=Probe())
    assert index.file_count(library) == 10

    # The mount goes sideways and only one file is visible.
    for path in sorted((library / "Fatboy Slim").glob("*"))[1:]:
        path.unlink()

    summary = index.reconcile(library, probe=Probe())

    assert summary["delete_skipped"] is True
    assert summary["deleted"] == 0
    assert index.file_count(library) == 10, "the index must survive a bad walk intact"
    assert "refused to prune" in index.root_state(library)["last_error"]


def test_a_genuine_shrink_within_the_floor_still_prunes(index, library):
    for n in range(10):
        _track(library, f"Track {n}.flac")
    index.reconcile(library, probe=Probe())

    (library / "Fatboy Slim" / "Track 0.flac").unlink()
    summary = index.reconcile(library, probe=Probe())

    assert summary["delete_skipped"] is False
    assert summary["deleted"] == 1
    assert index.file_count(library) == 9


def test_an_empty_library_can_still_be_indexed_from_scratch(index, library):
    """First run on an empty root must not be mistaken for a collapse."""
    summary = index.reconcile(library, probe=Probe())
    assert summary["error"] is None
    assert summary["walked"] == 0
    assert index.is_ready(library)


# --- failure is never an exception -------------------------------------------

def test_a_missing_root_is_reported_not_raised(index, tmp_path):
    summary = index.reconcile(tmp_path / "not-here", probe=Probe())
    assert summary["error"]
    assert summary["ran"] is False


def test_a_walk_that_explodes_leaves_the_previous_index_intact(index, library):
    _track(library, "Praise You.flac")
    index.reconcile(library, probe=Probe())

    def angry_walker(root, **kwargs):
        yield from ()
        raise OSError("the NAS has gone for a lie-down")

    summary = index.reconcile(library, walker=angry_walker, probe=Probe())

    assert "lie-down" in summary["error"]
    assert index.file_count(library) == 1
    assert "lie-down" in index.root_state(library)["last_error"]


def test_an_unusable_database_degrades_to_no_index(index, library, monkeypatch):
    monkeypatch.setattr(
        library_index, "LIBRARY_INDEX_DB_PATH", Path("/proc/definitely/not/writable.db")
    )
    library_index.reset_for_tests()

    assert index.init_index() is False
    assert index.reconcile(library, probe=Probe())["error"] == "index unavailable"
    assert index.storage_totals([library]) is None
    assert index.find_by_tags(library, "Fatboy Slim", "Praise You") is None
    assert index.is_ready(library) is False


def test_two_reconciliations_of_one_root_do_not_overlap(index, library):
    _track(library, "Praise You.flac")
    seen = {}

    def reentrant_walker(root, **kwargs):
        seen["nested"] = index.reconcile(library, probe=Probe())
        yield from library_index.iter_library_audio_files(root, strict=True)

    index.reconcile(library, walker=reentrant_walker, probe=Probe())

    assert "already running" in seen["nested"]["error"]


# --- keyed on the root, not the user (D2) ------------------------------------

def test_two_roots_are_indexed_independently(index, tmp_path):
    a = tmp_path / "library-a"
    b = tmp_path / "library-b"
    for root, name in ((a, "A Side.flac"), (b, "B Side.flac")):
        (root / "Fatboy Slim").mkdir(parents=True)
        (root / "Fatboy Slim" / name).write_bytes(b"audio")

    index.reconcile(a, probe=Probe())
    index.reconcile(b, probe=Probe())

    assert index.file_count(a) == 1
    assert index.file_count(b) == 1
    assert index.find_by_tags(a, "Fatboy Slim", "B Side") == []
    assert index.find_by_tags(b, "Fatboy Slim", "B Side") is not None


def test_one_root_shared_by_many_users_is_indexed_once(index, library, monkeypatch):
    """Three users, one library, one set of rows. The files do not care who asks."""
    _track(library, "Praise You.flac")
    monkeypatch.setattr(library_index, "get_setting", lambda *a, **kw: str(library))

    class FakeConn:
        def execute(self, *a):
            return [("alice",), ("bob",), ("carol",)]

    import contextlib

    @contextlib.contextmanager
    def fake_db_conn():
        yield FakeConn()

    import db
    monkeypatch.setattr(db, "db_conn", fake_db_conn)

    assert index.configured_roots() == [str(library)]


# --- hooks -------------------------------------------------------------------

def test_index_file_records_one_arrival(index, library, monkeypatch):
    index.reconcile(library, probe=Probe())
    arrival = _track(library, "Right Here Right Now.flac")
    monkeypatch.setattr(library_index, "probe_file", Probe())

    assert index.index_file(arrival) is True
    assert index.find_by_tags(library, "Fatboy Slim", "Right Here Right Now") == [arrival]


def test_remove_file_forgets_one_departure(index, library, monkeypatch):
    track = _track(library, "Praise You.flac")
    index.reconcile(library, probe=Probe())

    assert index.remove_file(track) is True
    assert index.file_count(library) == 0


def test_move_file_follows_without_reopening_anything(index, library, monkeypatch):
    track = _track(library, "Praise You.flac")
    index.reconcile(library, probe=Probe())
    probe = Probe()
    monkeypatch.setattr(library_index, "probe_file", probe)

    moved = library / "Fatboy Slim" / "Praise You (Remaster).flac"
    track.rename(moved)

    assert index.move_file(track, moved) is True
    assert probe.calls == []
    assert index.find_by_tags(library, "Fatboy Slim", "Praise You") == [moved]


def test_hooks_are_idempotent(index, library, monkeypatch):
    track = _track(library, "Praise You.flac")
    monkeypatch.setattr(library_index, "probe_file", Probe())
    index.reconcile(library, probe=Probe())

    index.index_file(track)
    index.index_file(track)
    assert index.file_count(library) == 1

    assert index.remove_file(track) is True
    assert index.remove_file(track) is True
    assert index.file_count(library) == 0


def test_a_hook_for_a_path_outside_every_root_is_a_polite_no(index, library, tmp_path):
    index.reconcile(library, probe=Probe())
    stranger = tmp_path / "elsewhere.flac"
    stranger.write_bytes(b"audio")

    assert index.index_file(stranger) is False
    assert index.remove_file(stranger) is False


# --- storage totals (step 3b) ------------------------------------------------

def test_storage_totals_come_from_the_index(index, library):
    _track(library, "Praise You.flac", payload=b"a" * 100)
    _track(library, "Rockafeller Skank.flac", payload=b"b" * 250)
    index.reconcile(library, probe=Probe())

    assert index.storage_totals([library]) == {"storage_bytes": 350, "file_count": 2}


def test_storage_totals_deduplicate_hardlinks(index, library):
    original = _track(library, "Praise You.flac", payload=b"a" * 100)
    (library / "Fatboy Slim" / "Praise You (copy).flac").hardlink_to(original)
    index.reconcile(library, probe=Probe())

    assert index.file_count(library) == 2
    assert index.storage_totals([library]) == {"storage_bytes": 100, "file_count": 1}


def test_storage_totals_can_answer_for_a_subdirectory(index, library):
    _track(library, "Praise You.flac", payload=b"a" * 100)
    (library / "Albums").mkdir()
    (library / "Albums" / "Elsewhere.flac").write_bytes(b"b" * 700)
    index.reconcile(library, probe=Probe())

    assert index.storage_totals([library / "Albums"]) == {
        "storage_bytes": 700, "file_count": 1,
    }
    assert index.storage_totals([library / "Fatboy Slim"]) == {
        "storage_bytes": 100, "file_count": 1,
    }


def test_storage_totals_decline_before_the_first_complete_scan(index, library):
    _track(library, "Praise You.flac")
    assert index.storage_totals([library]) is None


def test_storage_totals_decline_for_a_directory_outside_the_index(index, library, tmp_path):
    _track(library, "Praise You.flac")
    index.reconcile(library, probe=Probe())

    assert index.storage_totals([tmp_path / "somewhere-else"]) is None


def test_scan_storage_prefers_the_index_but_keeps_the_walk(index, library, monkeypatch):
    """The stats tile must be right whether or not the index can help."""
    _track(library, "Praise You.flac", payload=b"a" * 100)
    index.reconcile(library, probe=Probe())

    from_index = library_scans.scan_storage([library])
    assert from_index == {"storage_bytes": 100, "file_count": 1}

    monkeypatch.setattr(library_index, "storage_totals", lambda directories: None)
    from_walk = library_scans.scan_storage([library])
    assert from_walk == from_index


def test_scan_storage_survives_an_index_that_throws(index, library, monkeypatch):
    _track(library, "Praise You.flac", payload=b"a" * 100)

    def explode(directories):
        raise RuntimeError("the index is having a moment")

    monkeypatch.setattr(library_index, "storage_totals", explode)

    assert library_scans.scan_storage([library]) == {"storage_bytes": 100, "file_count": 1}


# --- schema versioning: a cache is rebuilt, never migrated --------------------

def test_a_stale_schema_is_discarded_rather_than_migrated(index, library):
    """Old shape, no migration script, no drama: throw it away and re-read the disk."""
    _track(library, "Praise You.flac")
    index.reconcile(library, probe=Probe())
    assert index.file_count(library) == 1

    with sqlite3.connect(str(index.LIBRARY_INDEX_DB_PATH)) as conn:
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    library_index.reset_for_tests()

    assert index.init_index() is True
    assert index.file_count(library) == 0, "stale rows must not survive a shape change"
    assert index.is_ready(library) is False

    index.reconcile(library, probe=Probe())
    assert index.file_count(library) == 1


def test_schema_2_is_re_keyed_in_place_not_rebuilt(index, library, monkeypatch):
    """Only the key rules changed in 3, so the rows are re-keyed from the tags
    they already hold. A rebuild would leave duplicate checks half-blind for
    as long as a cold build takes."""
    _track(library, "Praise You (Instrumental).flac")
    _track(library, "Right Here (feat. Someone).flac")
    index.reconcile(library, probe=Probe())

    # Write the keys the way schema 2 did: every bracket thrown away.
    with sqlite3.connect(str(index.LIBRARY_INDEX_DB_PATH)) as conn:
        conn.execute("UPDATE library_files SET title_key = 'praise you' WHERE title LIKE 'Praise You%'")
        conn.execute("UPDATE library_files SET title_key = 'right here' WHERE title LIKE 'Right Here%'")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    library_index.reset_for_tests()

    monkeypatch.setattr(library_index, "probe_file",
                        lambda path: pytest.fail(f"re-keying read a file: {path}"))
    assert index.init_index() is True
    assert index.file_count(library) == 2, "rows must survive a re-key"
    assert index.is_ready(library) is True
    with sqlite3.connect(str(index.LIBRARY_INDEX_DB_PATH)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == library_index.SCHEMA_VERSION

    # The instrumental no longer answers for the song; the feat. still does.
    assert index.find_by_tags(library, ["Fatboy Slim"], "Praise You") == []
    assert len(index.find_by_tags(library, ["Fatboy Slim"], "Praise You (Instrumental)")) == 1
    assert len(index.find_by_tags(library, ["Fatboy Slim"], "Right Here")) == 1


def test_a_current_schema_is_left_well_alone(index, library):
    _track(library, "Praise You.flac")
    index.reconcile(library, probe=Probe())
    library_index.reset_for_tests()

    assert index.init_index() is True
    assert index.file_count(library) == 1


# --- saying something during a long scan -------------------------------------

def test_a_slow_scan_reports_progress(index, library, capsys, monkeypatch):
    """A cold build is twenty minutes long; silence is not an acceptable UI."""
    monkeypatch.setattr(library_index, "LIBRARY_INDEX_PROGRESS_SECONDS", 0)
    _track(library, "Praise You.flac")
    _track(library, "Rockafeller Skank.flac")

    index.reconcile(str(library), probe=Probe())

    lines = [
        line for line in capsys.readouterr().out.splitlines() if "files seen" in line
    ]
    assert lines, "a slow scan must say what it is doing"
    assert "2 files seen" in lines[-1]
    assert "2 read" in lines[-1]


def test_a_quick_scan_says_nothing(index, library, capsys):
    """The ticker is for slow builds. A pass that beats the interval stays quiet."""
    _track(library, "Praise You.flac")
    index.reconcile(str(library), probe=Probe())
    capsys.readouterr()

    index.reconcile(str(library), probe=Probe())

    assert "files seen" not in capsys.readouterr().out


# --- filesystems that hand out unsigned inode numbers -------------------------

def test_unsigned_cifs_inodes_do_not_take_down_the_whole_scan(index, library, monkeypatch):
    """A CIFS mount that hashes file handles produces inodes past 2**63.

    SQLite's INTEGER is signed, so one such file used to abort the batch, unwind
    the reconciliation and leave the index permanently unbuilt. Found on the VM,
    where the mount hashes, having been invisible on the host beside it, where
    the same NAS supplies real inode numbers.
    """
    huge = 16946177935372863836  # a real one, off /music/Singles/The Beatles
    assert huge > 2 ** 63 - 1

    _track(library, "Let It Be.flac")
    _track(library, "Jolene.flac")

    from utils import LibraryFile

    real = os.stat_result

    def walker(root, strict=False):
        for entry in library_index.iter_library_audio_files(root, strict=strict):
            st = entry.stat
            fields = list(st)
            fields[1] = huge  # st_ino
            yield LibraryFile(
                entry.path, real(fields, {"st_mtime_ns": st.st_mtime_ns})
            )

    summary = index.reconcile(str(library), walker=walker, probe=Probe())

    assert summary["error"] is None
    assert summary["walked"] == 2 and summary["probed"] == 2

    with index.index_conn() as conn:
        stored = [r[0] for r in conn.execute("SELECT ino FROM library_files")]
    # The same 64 bits, read as signed, so the two files stay distinguishable.
    assert set(stored) == {huge - 2 ** 64}
    assert index.storage_totals([Path(str(library))])["file_count"] == 1


def test_signed64_is_reversible_and_leaves_ordinary_inodes_alone():
    assert library_index._signed64(45329555) == 45329555
    assert library_index._signed64(None) is None
    assert library_index._signed64(2 ** 64 - 1) == -1
    assert library_index._signed64(2 ** 63) == -(2 ** 63)
    # Distinctness is the whole contract: no two inodes may collide.
    values = [2 ** 63 - 1, 2 ** 63, 2 ** 64 - 1, 0, 1, 45329555]
    assert len({library_index._signed64(v) for v in values}) == len(values)


# --- 3d: spending the negative cache -----------------------------------------

def test_a_hint_answers_for_a_file_the_index_already_opened(index, library):
    track = _track(library, "Praise You.flac")
    index.reconcile(library, probe=Probe())

    with index.probe_hints() as hint:
        info = hint(track, os.stat(track))

    assert info["source"] == "youtube"
    assert info["codec"] == "flac"
    assert info["bitrate_kbps"] == 900
    assert info["duration"] == 300.0
    assert info["artist"] == "Fatboy Slim"
    assert info["title"] == "Praise You"
    assert info["file_id"] == "fid-Praise You"


def test_a_hint_declines_once_the_file_has_changed_size(index, library):
    track = _track(library, "Praise You.flac")
    index.reconcile(library, probe=Probe())
    track.write_bytes(b"a rather longer recording than we had before")

    with index.probe_hints() as hint:
        assert hint(track, os.stat(track)) is None


def test_a_hint_declines_when_only_the_timestamp_moved(index, library):
    """Nanoseconds, not whole seconds. Stricter than the caller's own cache."""
    track = _track(library, "Praise You.flac")
    index.reconcile(library, probe=Probe())
    st = os.stat(track)
    os.utime(track, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))

    with index.probe_hints() as hint:
        assert hint(track, os.stat(track)) is None


def test_a_hint_declines_for_a_file_it_has_never_seen(index, library):
    _track(library, "Praise You.flac")
    index.reconcile(library, probe=Probe())
    newcomer = _track(library, "Rockafeller Skank.flac")

    with index.probe_hints() as hint:
        assert hint(newcomer, os.stat(newcomer)) is None


def test_a_hint_declines_for_a_file_outside_every_root(index, library, tmp_path):
    _track(library, "Praise You.flac")
    index.reconcile(library, probe=Probe())
    stray = tmp_path / "elsewhere.flac"
    stray.write_bytes(b"audio")

    with index.probe_hints() as hint:
        assert hint(stray, os.stat(stray)) is None


def test_hints_decline_before_the_first_complete_scan(index, library):
    track = _track(library, "Praise You.flac")

    with index.probe_hints() as hint:
        assert hint(track, os.stat(track)) is None


def test_a_file_the_index_could_not_read_is_not_offered_for_reopening(index, library):
    """The clearest negative cache there is: one we already know is unparseable."""
    track = _track(library, "corrupt.flac")
    index.reconcile(library, probe=lambda path: None)

    with index.probe_hints() as hint:
        info = hint(track, os.stat(track))

    assert info is not None
    assert info["codec"] is None
    assert info["source"] is None


def test_hints_are_answered_without_a_connection_per_file(index, library, monkeypatch):
    """A connection each would have eaten a good slice of what this saves."""
    tracks = [_track(library, f"track {n}.flac") for n in range(5)]
    index.reconcile(library, probe=Probe())
    opened = []
    real_conn = index.index_conn

    @contextmanager
    def counted_conn(**kwargs):
        opened.append(kwargs)
        with real_conn(**kwargs) as conn:
            yield conn

    monkeypatch.setattr(index, "index_conn", counted_conn)
    with index.probe_hints() as hint:
        assert all(hint(track, os.stat(track)) is not None for track in tracks)

    assert opened == [{"read_only": True}]


def test_hints_survive_an_index_that_throws(index, library, monkeypatch):
    track = _track(library, "Praise You.flac")
    index.reconcile(library, probe=Probe())

    def explode(**kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(index, "index_conn", explode)
    with index.probe_hints() as hint:
        assert hint(track, os.stat(track)) is None


# --- 3e: listing a root without walking it -----------------------------------

def test_paths_under_lists_a_whole_root(index, library):
    first = _track(library, "Praise You.flac")
    second = _track(library, "Rockafeller Skank.flac")
    index.reconcile(library, probe=Probe())

    assert sorted(index.paths_under(library)) == sorted([first, second])


def test_paths_under_lists_one_subdirectory(index, library):
    _track(library, "Praise You.flac")
    (library / "Albums").mkdir()
    album_track = library / "Albums" / "Elsewhere.flac"
    album_track.write_bytes(b"audio")
    index.reconcile(library, probe=Probe())

    assert index.paths_under(library / "Albums") == [album_track]


def test_an_empty_indexed_folder_is_an_answer_not_a_shrug(index, library):
    """"No audio in there" and "I could not look" must never be the same reply;
    the caller turns the answer into rows in the jobs table."""
    _track(library, "Praise You.flac")
    (library / "Albums").mkdir()
    index.reconcile(library, probe=Probe())

    assert index.paths_under(library / "Albums") == []


def test_paths_under_declines_before_a_complete_scan(index, library):
    _track(library, "Praise You.flac")
    assert index.paths_under(library) is None


def test_paths_under_declines_for_a_directory_outside_the_index(index, library, tmp_path):
    _track(library, "Praise You.flac")
    index.reconcile(library, probe=Probe())

    assert index.paths_under(tmp_path / "somewhere-else") is None
