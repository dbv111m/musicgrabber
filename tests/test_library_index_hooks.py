"""The index follows MusicGrabber's own writes instead of waiting for a scan.

Phase 5. The reconciliation runs daily, which for most of the index is fine:
every reader validates what it gets back, so a stale row costs nothing but the
speed it saved. Two things are not fine, and are why these hooks exist.

The first is duplicate detection, which trusts the index's artist and title
outright: "a completed index's miss must not start another whole-library crawl".
Retag a file through the UI and the old row keeps the old name, so a later,
entirely unrelated download of that name is refused as a duplicate of a file
that no longer carries it.

The second is simply that a file we just wrote is a file we just read. Paying
24ms over SMB tomorrow to rediscover what we knew today is the whole thing this
index was built to stop.
"""

import os
import queue
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import constants
import db
import library_index
import settings
import utils


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def index(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_DIR", str(tmp_path / "music"))
    monkeypatch.setattr(library_index, "LIBRARY_INDEX_DB_PATH", tmp_path / "index.db")
    library_index.reset_for_tests()
    yield library_index
    library_index.reset_for_tests()


@pytest.fixture
def library(monkeypatch, tmp_path, index):
    """A music root the index has already scanned, holding one track."""
    root = tmp_path / "music"
    artist_dir = root / "Singles" / "Orbital"
    artist_dir.mkdir(parents=True)
    (artist_dir / "Halcyon.flac").write_bytes(b"audio")

    monkeypatch.setattr(library_index, "configured_roots", lambda: [str(root)])
    monkeypatch.setattr(library_index, "probe_file", _fake_probe)
    index.reconcile(root, probe=_fake_probe)
    return root


def _fake_probe(path):
    """Enough of a probe for the index, without needing real FLAC bytes."""
    return {
        "artist": "Orbital", "title": path.stem, "album": "In Sides",
        "codec": "flac", "bitrate_kbps": 900, "duration": 300.0,
        "source": "musicgrabber", "source_quality": "lossless",
        "source_codec": None, "source_bitrate_kbps": 0, "file_id": None,
    }


def _rows(root):
    with library_index.index_conn() as conn:
        return sorted(
            row[0] for row in conn.execute(
                "SELECT relative_path FROM library_files WHERE root_path = ?",
                (str(root),),
            ).fetchall()
        )


def _titles(root):
    with library_index.index_conn() as conn:
        return sorted(
            row[0] for row in conn.execute(
                "SELECT title FROM library_files WHERE root_path = ?", (str(root),)
            ).fetchall()
        )


# ---------------------------------------------------------------------------
# What the hooks refuse to record
# ---------------------------------------------------------------------------

def test_a_playlist_file_is_not_a_library_file(index, library):
    """Watched playlists record their .m3u as the job's final path."""
    m3u = library / "Singles" / "Sixties Mix.m3u"
    m3u.write_text("#EXTM3U\n")

    assert index.index_file(m3u) is False
    assert _rows(library) == ["Singles/Orbital/Halcyon.flac"]


def test_a_lyrics_sidecar_is_not_a_library_file(index, library):
    lrc = library / "Singles" / "Orbital" / "Halcyon.lrc"
    lrc.write_text("[00:00.00] a wall of synths\n")

    assert index.index_file(lrc) is False
    assert _rows(library) == ["Singles/Orbital/Halcyon.flac"]


def test_a_file_in_a_bin_inside_the_share_is_declined(index, library):
    """Synology and QNAP park deletions inside the share; scans skip them."""
    binned = library / "#recycle" / "Orbital" / "Halcyon.flac"
    binned.parent.mkdir(parents=True)
    binned.write_bytes(b"audio")

    assert index.index_file(binned) is False
    assert _rows(library) == ["Singles/Orbital/Halcyon.flac"]


def test_a_move_into_a_bin_is_a_departure_not_a_rename(index, library):
    track = library / "Singles" / "Orbital" / "Halcyon.flac"
    binned = library / "#recycle" / "Halcyon.flac"
    binned.parent.mkdir(parents=True)
    track.rename(binned)

    assert index.move_file(track, binned) is True
    assert _rows(library) == []


def test_a_move_out_of_a_bin_is_an_arrival(index, library):
    binned = library / "#recycle" / "Belfast.flac"
    binned.parent.mkdir(parents=True)
    binned.write_bytes(b"audio")
    restored = library / "Singles" / "Orbital" / "Belfast.flac"
    binned.rename(restored)

    assert index.move_file(binned, restored) is True
    assert _rows(library) == [
        "Singles/Orbital/Belfast.flac", "Singles/Orbital/Halcyon.flac",
    ]


def test_a_file_the_index_already_holds_is_not_reopened(index, library, monkeypatch):
    """A duplicate-skipped download still records a completed job.

    Reopening a file whose size and timestamp have not moved is the exact work
    this index exists to stop. A bulk import of mostly-owned tracks would
    otherwise pay for a probe per track to rewrite rows it already had.
    """
    probes = []

    def counted(path):
        probes.append(str(path))
        return _fake_probe(path)

    monkeypatch.setattr(library_index, "probe_file", counted)
    track = library / "Singles" / "Orbital" / "Halcyon.flac"

    assert index.index_file(track) is True
    assert probes == []

    # And a file that has genuinely moved on is read, exactly as before.
    track.write_bytes(b"a longer and quite different recording")
    assert index.index_file(track) is True
    assert probes == [str(track)]


# ---------------------------------------------------------------------------
# Trash and restore
# ---------------------------------------------------------------------------

@pytest.fixture
def trash(monkeypatch, tmp_path, library):
    """MusicGrabber's own bin, which lives under /data and not in the library."""
    bin_dir = tmp_path / "data" / "trash"
    monkeypatch.setattr(settings, "get_trash_dir", lambda user_id=None: bin_dir)
    monkeypatch.setattr(utils, "get_trash_dir", lambda user_id=None: bin_dir)
    monkeypatch.setattr(
        utils, "get_setting",
        lambda key, default=None, user_id=None: str(library) if key == "music_dir" else default,
    )
    return bin_dir


def test_trashing_a_track_forgets_it_immediately(library, trash):
    track = library / "Singles" / "Orbital" / "Halcyon.flac"

    assert utils.move_to_trash(track) is not None
    assert _rows(library) == []


def test_restoring_a_track_indexes_it_again(library, trash):
    track = library / "Singles" / "Orbital" / "Halcyon.flac"
    binned = utils.move_to_trash(track)
    assert _rows(library) == []

    assert utils.restore_from_trash(binned) is not None
    assert _rows(library) == ["Singles/Orbital/Halcyon.flac"]


def test_an_unavailable_index_does_not_break_the_bin(monkeypatch, library, trash):
    """Rule one: the index is a cache, and a broken cache loses nobody's music."""
    monkeypatch.setattr(library_index, "init_index", lambda: False)
    track = library / "Singles" / "Orbital" / "Halcyon.flac"

    binned = utils.move_to_trash(track)
    assert binned is not None and binned.exists()
    assert utils.restore_from_trash(binned) is not None
    assert track.exists()


# ---------------------------------------------------------------------------
# A completed download
# ---------------------------------------------------------------------------

@pytest.fixture
def jobs_db(monkeypatch, tmp_path):
    monkeypatch.setattr(constants, "DB_PATH", tmp_path / "hooks.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "hooks.db")
    monkeypatch.setattr(db, "_DB_POOL_SIZE", 4)
    monkeypatch.setattr(db, "_db_pool", queue.LifoQueue(maxsize=4))
    monkeypatch.setattr(db, "_db_pool_created", 0)
    monkeypatch.setattr(db, "_db_pool_lock", threading.Lock())
    db.init_db()
    with db.db_conn() as conn:
        conn.execute(
            """INSERT INTO jobs (id, artist, title, status)
               VALUES ('job-0001', 'Orbital', 'Belfast', 'downloading')"""
        )
        conn.commit()
    return db


def test_a_completed_download_is_indexed_where_it_landed(jobs_db, library):
    import downloads

    arrival = library / "Singles" / "Orbital" / "Belfast.flac"
    arrival.write_bytes(b"audio")

    downloads._update_job("job-0001", status="completed", final_path=str(arrival))

    assert _rows(library) == [
        "Singles/Orbital/Belfast.flac", "Singles/Orbital/Halcyon.flac",
    ]


def test_a_failed_download_is_not_indexed(jobs_db, library):
    """final_path on a failure is breadcrumbs, and often points at a file the
    integrity check has already binned."""
    import downloads

    wreckage = library / "Singles" / "Orbital" / "Belfast.flac"
    wreckage.write_bytes(b"audio")

    downloads._update_job("job-0001", status="failed", final_path=str(wreckage))

    assert _rows(library) == ["Singles/Orbital/Halcyon.flac"]


def test_a_playlist_job_completing_indexes_nothing(jobs_db, library):
    import downloads

    m3u = library / "Singles" / "Sixties Mix.m3u"
    m3u.write_text("#EXTM3U\n")

    downloads._update_job("job-0001", status="completed", final_path=str(m3u))

    assert _rows(library) == ["Singles/Orbital/Halcyon.flac"]


def test_a_job_update_that_is_not_a_completion_indexes_nothing(jobs_db, library):
    import downloads

    arrival = library / "Singles" / "Orbital" / "Belfast.flac"
    arrival.write_bytes(b"audio")

    downloads._update_job("job-0001", progress_stage="Probing quality")

    assert _rows(library) == ["Singles/Orbital/Halcyon.flac"]


# ---------------------------------------------------------------------------
# A retag through the UI
# ---------------------------------------------------------------------------

class _Request:
    """Just enough of a FastAPI request for a route that only reads state."""

    def __init__(self, user_id=None, is_admin=True):
        self.state = type("S", (), {"user_id": user_id, "is_admin": is_admin})()


@pytest.fixture
def retaggable(monkeypatch, jobs_db, library):
    """A completed job whose file is where the queue says it is."""
    import app
    from models import PatchTagsRequest

    track = library / "Singles" / "Orbital" / "Halcyon.flac"
    with jobs_db.db_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'completed', artist = 'Orbital', "
            "title = 'Halcyon', final_path = ? WHERE id = 'job-0001'",
            (str(track),),
        )
        conn.commit()

    monkeypatch.setattr(
        utils, "get_setting",
        lambda key, default=None, user_id=None: str(library) if key == "music_dir" else default,
    )
    # Real tag writing wants a real FLAC, and this test is about the index.
    monkeypatch.setattr(app, "apply_metadata_to_file", lambda *a, **k: None)
    monkeypatch.setattr(app, "spawn_daemon_thread", lambda *a, **k: None)
    return app, PatchTagsRequest, track


def test_a_retag_does_not_leave_the_old_name_in_the_index(index, library, retaggable):
    """The one hook fixing a bug rather than buying speed.

    `find_by_tags` treats a completed index's miss as authoritative, so a row
    still carrying the pre-retag artist and title will match a download of that
    name and get it refused as a duplicate of a file that is not there.
    """
    app, PatchTagsRequest, track = retaggable
    assert index.find_by_tags(library, "Orbital", "Halcyon") == [track]

    app.patch_job_tags(
        "job-0001",
        PatchTagsRequest(artist="Orbital", title="Halcyon On and On"),
        _Request(),
    )

    assert _rows(library) == ["Singles/Orbital/Orbital - Halcyon On and On.flac"]
    assert index.find_by_tags(library, "Orbital", "Halcyon") == []


def test_a_retag_writes_the_new_tags_rather_than_moving_the_old_row(
    monkeypatch, index, library, retaggable
):
    """A retag changed the bytes, so the row is re-read, not relocated."""
    app, PatchTagsRequest, _track = retaggable
    monkeypatch.setattr(library_index, "probe_file", lambda path: dict(
        _fake_probe(path), title="Halcyon On and On"
    ))

    app.patch_job_tags(
        "job-0001",
        PatchTagsRequest(artist="Orbital", title="Halcyon On and On"),
        _Request(),
    )

    assert _titles(library) == ["Halcyon On and On"]


def test_an_album_only_retag_still_refreshes_the_row(monkeypatch, index, library, retaggable):
    """No rename, so nothing moved, but the album in the row is now a lie."""
    app, PatchTagsRequest, track = retaggable
    monkeypatch.setattr(library_index, "probe_file", lambda path: dict(
        _fake_probe(path), album="Orbital 2"
    ))

    app.patch_job_tags(
        "job-0001",
        PatchTagsRequest(artist="Orbital", title="Halcyon", album="Orbital 2"),
        _Request(),
    )

    assert _rows(library) == ["Singles/Orbital/Halcyon.flac"]
    with library_index.index_conn() as conn:
        assert conn.execute(
            "SELECT album FROM library_files WHERE relative_path = ?",
            ("Singles/Orbital/Halcyon.flac",),
        ).fetchone()[0] == "Orbital 2"


# ---------------------------------------------------------------------------
# Undoing an upgrade
# ---------------------------------------------------------------------------

def test_restoring_from_quarantine_swaps_both_rows(monkeypatch, jobs_db, index, library):
    """The upgrade's file leaves and the original comes back, in one go."""
    import upgrades

    quarantine = library.parent / "data" / ".upgrade_quarantine"
    quarantine.mkdir(parents=True)
    monkeypatch.setattr(upgrades, "_quarantine_dir", lambda: quarantine)
    monkeypatch.setattr(upgrades, "scan_user_library", lambda user_id: None)
    monkeypatch.setattr(library_index, "probe_file", _fake_probe)

    # The upgrade replaced Halcyon.flac with a better Halcyon.flac, and the
    # index has been told about it, exactly as the swap hook would have.
    original = library / "Singles" / "Orbital" / "Halcyon.flac"
    banished = quarantine / "20260912000000__Halcyon.flac"
    original.rename(banished)
    original.write_bytes(b"better audio")
    index.index_file(original)

    with jobs_db.db_conn() as conn:
        conn.execute(
            """INSERT INTO upgrade_quarantine
               (user_id, quarantine_path, original_path, replaced_by_path,
                artist, title, file_size)
               VALUES ('', ?, ?, ?, 'Orbital', 'Halcyon', 5)""",
            (str(banished), str(original), str(original)),
        )
        conn.commit()
        entry_id = conn.execute("SELECT id FROM upgrade_quarantine").fetchone()[0]

    result = upgrades.restore_from_quarantine(None, entry_id, is_admin=True)

    assert result["status"] == "restored"
    assert _rows(library) == ["Singles/Orbital/Halcyon.flac"]
    # And the row describes the file that is actually there now.
    with library_index.index_conn() as conn:
        assert conn.execute(
            "SELECT size_bytes FROM library_files WHERE relative_path = ?",
            ("Singles/Orbital/Halcyon.flac",),
        ).fetchone()[0] == original.stat().st_size
