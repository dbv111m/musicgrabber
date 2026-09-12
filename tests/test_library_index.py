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
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import library_index
import library_scans


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
    assert index.find_by_tags(library, "Fatboy Slim", "Praise You") == moved


def test_a_deleted_file_is_pruned(index, library):
    keeper = _track(library, "Praise You.flac")
    doomed = _track(library, "Rockafeller Skank.flac")
    index.reconcile(library, probe=Probe())

    doomed.unlink()
    summary = index.reconcile(library, probe=Probe())

    assert summary["deleted"] == 1
    assert index.file_count(library) == 1
    assert index.find_by_tags(library, "Fatboy Slim", "Praise You") == keeper


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
    assert index.find_by_tags(a, "Fatboy Slim", "B Side") is None
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
    assert index.find_by_tags(library, "Fatboy Slim", "Right Here Right Now") == arrival


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
    assert index.find_by_tags(library, "Fatboy Slim", "Praise You") == moved


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
