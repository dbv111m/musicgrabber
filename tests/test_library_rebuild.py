"""The admin library rebuild reads the index instead of walking the library.

`clean_rebuild` exists to mint history rows for audio that has no matching
`jobs.final_path`, usually after somebody imported a pile of music by hand. It
used to walk all three configured roots and `resolve()` every single file to do
it, which on a real library is twenty thousand round trips over SMB to discover
what a reconciliation wrote down yesterday.

The walk stays as the fallback, because "I saw nothing" and "I could not look"
must never be the same answer when the result becomes rows in a database.
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


@pytest.fixture
def fresh_db(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_DIR", str(tmp_path / "music"))
    monkeypatch.setattr(constants, "DB_PATH", tmp_path / "rebuild.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "rebuild.db")
    monkeypatch.setattr(db, "_DB_POOL_SIZE", 4)
    monkeypatch.setattr(db, "_db_pool", queue.LifoQueue(maxsize=4))
    monkeypatch.setattr(db, "_db_pool_created", 0)
    monkeypatch.setattr(db, "_db_pool_lock", threading.Lock())
    db.init_db()
    return db


@pytest.fixture
def library(monkeypatch, tmp_path):
    """A music root with a singles folder, an albums folder, and no playlists."""
    import app

    root = tmp_path / "music"
    singles = root / "Singles" / "Fatboy Slim"
    albums = root / "Albums" / "Fatboy Slim" / "Better Living"
    singles.mkdir(parents=True)
    albums.mkdir(parents=True)
    (singles / "Praise You.flac").write_bytes(b"audio")
    (albums / "01 Right Here Right Now.flac").write_bytes(b"audio")

    monkeypatch.setattr(library_index, "LIBRARY_INDEX_DB_PATH", tmp_path / "index.db")
    library_index.reset_for_tests()
    monkeypatch.setattr(app, "get_singles_dir", lambda user_id=None: root / "Singles")
    monkeypatch.setattr(app, "get_albums_dir", lambda user_id=None: root / "Albums")
    monkeypatch.setattr(app, "get_playlists_dir", lambda user_id=None: None)
    yield root
    library_index.reset_for_tests()


def _count_walks(monkeypatch):
    """Wrap the shared walker so a test can prove it was, or was not, used."""
    import app

    real = app.iter_library_audio_files
    walked = []

    def counted(directory, extensions):
        walked.append(str(directory))
        return real(directory, extensions)

    monkeypatch.setattr(app, "iter_library_audio_files", counted)
    return walked


def _index_everything(root):
    library_index.reconcile(root, probe=lambda path: {
        "artist": "Fatboy Slim", "title": path.stem, "album": "Better Living",
        "codec": "flac", "bitrate_kbps": 900, "duration": 300.0,
        "source": None, "source_quality": None, "source_codec": None,
        "source_bitrate_kbps": 0, "file_id": None,
    })


def _final_paths(database):
    with database.db_conn() as conn:
        return sorted(
            row[0] for row in conn.execute(
                "SELECT final_path FROM jobs WHERE source = 'library_rebuild'"
            ).fetchall()
        )


def test_rebuild_reads_the_index_and_never_touches_the_disk(fresh_db, library):
    import app

    _index_everything(library)
    with pytest.MonkeyPatch.context() as patch:
        walked = _count_walks(patch)
        assert app._rebuild_library_index() == 2

    assert walked == []
    assert _final_paths(fresh_db) == sorted([
        str(library / "Singles" / "Fatboy Slim" / "Praise You.flac"),
        str(library / "Albums" / "Fatboy Slim" / "Better Living" / "01 Right Here Right Now.flac"),
    ])


def test_rebuild_falls_back_to_the_walk_without_an_index(fresh_db, library):
    import app

    with pytest.MonkeyPatch.context() as patch:
        walked = _count_walks(patch)
        assert app._rebuild_library_index() == 2

    assert walked == [str(library / "Singles"), str(library / "Albums")]
    assert len(_final_paths(fresh_db)) == 2


def test_rebuild_skips_audio_the_queue_already_knows_about(fresh_db, library):
    import app

    already = str(library / "Singles" / "Fatboy Slim" / "Praise You.flac")
    with fresh_db.db_conn() as conn:
        conn.execute(
            """INSERT INTO jobs (id, artist, title, status, final_path)
               VALUES ('known-01', 'Fatboy Slim', 'Praise You', 'completed', ?)""",
            (already,),
        )
        conn.commit()
    _index_everything(library)

    assert app._rebuild_library_index() == 1
    assert already not in _final_paths(fresh_db)


def test_rebuild_agrees_with_itself_whichever_route_it_took(fresh_db, library):
    """Index and walk must mint the same paths, or the same library gives two
    different histories depending on whether a reconciliation has run yet."""
    import app

    assert app._rebuild_library_index() == 2
    from_walk = _final_paths(fresh_db)

    with fresh_db.db_conn() as conn:
        conn.execute("DELETE FROM jobs WHERE source = 'library_rebuild'")
        conn.commit()

    _index_everything(library)
    assert app._rebuild_library_index() == 2

    assert _final_paths(fresh_db) == from_walk


def test_rebuild_is_idempotent(fresh_db, library):
    import app

    _index_everything(library)
    assert app._rebuild_library_index() == 2
    assert app._rebuild_library_index() == 0


def test_a_symlinked_library_does_not_get_a_second_history_row(fresh_db, monkeypatch, tmp_path):
    """`final_path` is written resolved, so the comparison has to be too.

    Reading the index instead of walking lost the per-file `resolve()` along
    with the walk, which on a library reached through a symlink would have
    minted a duplicate history row for every file already in the queue. The
    root is resolved once instead: no scan descends a symlinked directory, so
    nothing beneath it can resolve anywhere else.
    """
    import app

    real = tmp_path / "nas" / "music"
    singles = real / "Singles" / "Orbital"
    singles.mkdir(parents=True)
    (singles / "Halcyon.flac").write_bytes(b"audio")
    link = tmp_path / "music-link"
    link.symlink_to(real)

    monkeypatch.setattr(library_index, "LIBRARY_INDEX_DB_PATH", tmp_path / "index.db")
    library_index.reset_for_tests()
    monkeypatch.setattr(app, "get_singles_dir", lambda user_id=None: link / "Singles")
    monkeypatch.setattr(app, "get_albums_dir", lambda user_id=None: None)
    monkeypatch.setattr(app, "get_playlists_dir", lambda user_id=None: None)

    # The download path stores what `_local_final_path` gave it, which is resolved.
    with fresh_db.db_conn() as conn:
        conn.execute(
            """INSERT INTO jobs (id, artist, title, status, final_path)
               VALUES ('known-02', 'Orbital', 'Halcyon', 'completed', ?)""",
            (str((singles / "Halcyon.flac").resolve()),),
        )
        conn.commit()

    assert app._rebuild_library_index() == 0
    _index_everything(link / "Singles")
    assert app._rebuild_library_index() == 0
    library_index.reset_for_tests()
