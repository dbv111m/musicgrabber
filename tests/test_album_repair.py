"""Album rescues retain their pressing and folder, even across Queue retries."""

import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from mutagen import File as MutagenFile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import albums
import app
import db
import downloads
import metadata
from models import AlbumTrackRepairRequest, DownloadRequest


RELEASE = "11111111-1111-4111-8111-111111111111"
OTHER_RELEASE = "22222222-2222-4222-8222-222222222222"
TRACKS = [
    {"title": "Same Song", "artist": "Guest One", "position": "1", "disc_number": 1, "disc_total": 2, "track_total": 2},
    {"title": "Second Song", "artist": "Guest One", "position": "2", "disc_number": 1, "disc_total": 2, "track_total": 2},
    {"title": "Same Song", "artist": "Guest Two", "position": "1", "disc_number": 2, "disc_total": 2, "track_total": 1},
]
REQUEST = SimpleNamespace(state=SimpleNamespace(user_id=None, is_admin=True, role="admin"))


@pytest.fixture
def repair_library(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "repair.db")
    monkeypatch.setattr(db, "_db_pool", queue.LifoQueue(maxsize=db._DB_POOL_SIZE))
    monkeypatch.setattr(db, "_db_pool_created", 0)
    monkeypatch.setattr(db, "_db_pool_lock", threading.Lock())
    db.init_db()
    root = tmp_path / "music" / "Albums"
    folder = root / "Various Artists" / "Record Deluxe"
    folder.mkdir(parents=True)
    (folder / ".albuminfo").write_text(json.dumps({
        "artist": "Various Artists", "album": "Record", "release_mbid": RELEASE, "year": "2005",
    }))
    monkeypatch.setattr(albums, "get_albums_dir", lambda user_id=None: root)
    monkeypatch.setattr(app, "get_albums_dir", lambda user_id=None: root)
    monkeypatch.setenv("MUSIC_DIR", str(tmp_path / "music"))
    import settings
    monkeypatch.setattr(settings, "TRASH_ROOT", tmp_path / "trash")
    monkeypatch.setattr(albums, "fetch_album_release", lambda mbid: {"year": "2026", "tracks": TRACKS})
    monkeypatch.setattr(app, "fetch_album_tracks", lambda mbid: TRACKS)
    import library_index
    monkeypatch.setattr(library_index, "move_file", lambda *args, **kwargs: None)
    selection = {"artist": "Various Artists", "album_title": "Record", "release_mbid": RELEASE, "track_index": 2}
    return folder, selection


def test_context_uses_actual_folder_pressing_performer_year_and_disc(repair_library):
    folder, selection = repair_library
    context = albums.resolve_album_track_repair(**selection)
    assert context == {
        "override_dir": str(folder), "release_mbid": RELEASE,
        "album_artist": "Various Artists", "album_name": "Record",
        "track_artist": "Guest Two", "track_title": "Same Song",
        "track_number": 1, "track_total": 1, "disc_number": 2, "disc_total": 2,
        "year": "2005", "title_unique": False,
    }


@pytest.mark.parametrize("changed", [
    {"album_dir": "/tmp/outside/album"}, {"track_index": 30},
    {"release_mbid": OTHER_RELEASE}, {"track_title": "Changed song"},
    {"disc_number": 1}, {"track_number": 2},
])
def test_wrong_or_stale_selection_is_refused(repair_library, changed):
    _, selection = repair_library
    with pytest.raises(ValueError):
        albums.resolve_album_track_repair(**(selection | changed))


def test_symlink_destination_is_refused(repair_library, tmp_path):
    folder, selection = repair_library
    outside = tmp_path / "outside"
    outside.mkdir()
    link = folder.parent / "Record Alias"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        albums.resolve_album_track_repair(**(selection | {"album_dir": str(link)}))


def test_existing_track_requires_confirmation_and_failed_trash_keeps_it(repair_library, monkeypatch):
    folder, selection = repair_library
    bad = folder / "Same Song.mp3"
    bad.write_bytes(b"bad recording")
    with pytest.raises(HTTPException) as error:
        app.albums_repair_track(AlbumTrackRepairRequest(**selection), REQUEST)
    assert error.value.status_code == 409
    assert bad.exists()
    import utils
    monkeypatch.setattr(utils, "move_to_trash", lambda *args, **kwargs: None)
    with pytest.raises(HTTPException) as error:
        app.albums_repair_track(AlbumTrackRepairRequest(**selection, trash_existing=True), REQUEST)
    assert error.value.status_code == 500
    assert bad.exists()


def test_busy_album_is_refused_before_any_file_is_trashed(repair_library):
    folder, selection = repair_library
    bad = folder / "Same Song.mp3"
    bad.write_bytes(b"bad recording")
    with db.db_conn() as conn:
        conn.execute("INSERT INTO jobs (id, status, override_dir) VALUES ('busy', 'downloading', ?)", (str(folder),))
        conn.commit()
    with pytest.raises(HTTPException) as error:
        app.albums_repair_track(AlbumTrackRepairRequest(**selection, trash_existing=True), REQUEST)
    assert error.value.status_code == 409
    assert bad.exists()


def test_missing_track_queues_with_durable_context_and_rejects_double_click(repair_library, monkeypatch):
    folder, selection = repair_library
    prepared = app.albums_repair_track(AlbumTrackRepairRequest(**selection), REQUEST)
    assert prepared["trashed"] is False
    assert prepared["selection"]["album_dir"] == str(folder)
    calls = []
    monkeypatch.setattr(app, "spawn_daemon_thread", lambda *args, **kwargs: calls.append((args, kwargs)))
    body = DownloadRequest(video_id="abcdefghijk", title="Provider title", artist="Wrong provider artist", album_repair=prepared["selection"])
    result = app.download(body, REQUEST)
    with db.db_conn() as conn:
        conn.row_factory = sqlite3.Row
        job = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (result["job_id"],)).fetchone())
    assert job["artist"] == "Guest Two"
    assert job["title"] == "Same Song"
    assert job["override_dir"] == str(folder)
    assert job["album_release_mbid"] == RELEASE
    assert job["album_name"] == "Record"
    assert job["album_track_number"] == job["album_track_total"] == 1
    assert downloads._get_job_album_context(result["job_id"]) == prepared["context"]
    assert calls[0][1]["override_dir"] == str(folder)
    assert calls[0][1]["skip_dupe_check"] is True
    with pytest.raises(HTTPException) as error:
        app.download(body, REQUEST)
    assert error.value.status_code == 409


def test_queue_retry_retains_repair_disc_context(repair_library, monkeypatch):
    _, selection = repair_library
    context = albums.resolve_album_track_repair(**selection)
    monkeypatch.setattr(app, "spawn_daemon_thread", lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "search_all", lambda *args, **kwargs: ([], {}))
    result = app.download(DownloadRequest(video_id="abcdefghijk", title="Candidate", album_repair=selection), REQUEST)
    with db.db_conn() as conn:
        conn.execute("UPDATE jobs SET status = 'failed' WHERE id = ?", (result["job_id"],))
        conn.commit()
    retried = app.retry_job(result["job_id"], REQUEST)
    assert downloads._get_job_album_context(retried["job_id"]) == context


def test_existing_folder_uses_stored_pressing_in_track_list(repair_library, monkeypatch):
    _, _ = repair_library
    calls = []
    monkeypatch.setattr(app, "fetch_album_tracks", lambda mbid: (calls.append(mbid) or TRACKS))
    result = app.albums_missing_tracks(OTHER_RELEASE, "Various Artists", "Record", REQUEST)
    assert result["release_mbid"] == RELEASE
    assert calls == [OTHER_RELEASE, RELEASE]


@pytest.fixture
def make_audio(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg unavailable")

    def make(path):
        subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=duration=2", "-y", str(path)], check=True, capture_output=True)
        return path

    return make


@pytest.mark.parametrize("extension", ["flac", "mp3", "m4a", "opus"])
def test_disc_and_track_tags_survive_in_supported_formats(make_audio, tmp_path, extension):
    path = make_audio(tmp_path / f"track.{extension}")
    metadata.apply_metadata_to_file(path, "Guest Two", "Same Song", "Record", "2005", track_number=1, track_total=7, disc_number=2, disc_total=3, album_artist="Various Artists")
    tags = MutagenFile(str(path), easy=True)
    assert str(tags["discnumber"][0]).split("/")[0] == "2"
    assert str(tags["tracknumber"][0]).split("/")[0] == "1"
    assert tags["album"][0] == "Record"
    assert tags["albumartist"][0] == "Various Artists"


def test_album_m3u_orders_discs_and_rebuilds_after_trashing(make_audio, repair_library, monkeypatch, tmp_path):
    folder, selection = repair_library
    first = make_audio(folder / "01-02 - Second Song.flac")
    second = make_audio(folder / "02-01 - Same Song.flac")
    metadata.apply_metadata_to_file(first, "Guest One", "Second Song", "Record", track_number=2, disc_number=1, disc_total=2)
    metadata.apply_metadata_to_file(second, "Guest Two", "Same Song", "Record", track_number=1, disc_number=2, disc_total=2)
    playlist = downloads.rebuild_album_m3u(folder, "My Album")
    entries = [line for line in playlist.read_text().splitlines() if not line.startswith("#")]
    assert entries == [first.name, second.name]
    import utils
    trash = tmp_path / "rescued.flac"
    monkeypatch.setattr(utils, "move_to_trash", lambda path, **kwargs: (path.rename(trash) or trash))
    prepared = app.albums_repair_track(AlbumTrackRepairRequest(**selection, trash_existing=True), REQUEST)
    assert prepared["trashed"] is True
    assert trash.exists() and not second.exists()
    assert first.name in playlist.read_text()
    assert second.name not in playlist.read_text()
    assert (folder / ".albuminfo").exists()


def test_multi_disc_filename_keeps_disc_prefix_without_number_setting(repair_library, monkeypatch):
    _, selection = repair_library
    context = albums.resolve_album_track_repair(**selection)
    monkeypatch.setattr(downloads, "get_setting_bool", lambda name, default=False, **kwargs: name == "organise_by_artist")
    assert downloads._album_repair_output_stem(context, "fallback", None) == "02-01 - Same Song"


def test_real_worker_keeps_album_tags_destination_and_m3u_without_a_watched_row(repair_library, monkeypatch, make_audio, tmp_path):
    folder, selection = repair_library
    (folder / "Record.m3u").write_text("#EXTM3U\nold-file.mp3\n")
    source = make_audio(tmp_path / "source.m4a")
    monkeypatch.setattr(app, "spawn_daemon_thread", lambda *args, **kwargs: None)
    result = app.download(DownloadRequest(video_id="abcdefghijk", title="Provider song", album_repair=selection, convert_audio=False), REQUEST)
    monkeypatch.setattr(downloads, "get_album_art_context", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(downloads, "lookup_metadata", lambda *args, **kwargs: {
        "artist": "Wrong artist", "title": "Wrong song", "album": "Wrong album",
        "year": "2026", "track_number": 8, "track_total": 10,
    })
    monkeypatch.setattr(downloads, "fetch_cover_art", lambda *args, **kwargs: None)
    monkeypatch.setattr(downloads, "fetch_lyrics", lambda *args, **kwargs: None)
    for name in ("trigger_navidrome_scan", "trigger_jellyfin_scan", "trigger_plex_scan", "send_notification", "_apply_replaygain", "_apply_album_replaygain", "_copy_to_auto_import", "_normalise_loudness"):
        monkeypatch.setattr(downloads, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(downloads.library_index, "index_file", lambda *args, **kwargs: None)
    downloads._process_direct_mp3_download(
        result["job_id"], "test://audio", "Guest Two", "Same Song",
        convert_audio=False, source_label="jiosaavn", skip_dupe_check=True,
        download_fn=lambda url, path: shutil.copyfile(source, path),
    )
    with db.db_conn() as conn:
        conn.row_factory = sqlite3.Row
        job = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (result["job_id"],)).fetchone())
    assert job["status"] == "completed", job["error"]
    final = folder / "02-01 - Same Song.m4a"
    assert job["final_path"] == str(final)
    tags = MutagenFile(str(final), easy=True)
    assert tags["artist"] == ["Guest Two"]
    assert tags["title"] == ["Same Song"]
    assert tags["album"] == ["Record"]
    assert tags["albumartist"] == ["Various Artists"]
    assert tags["date"] == ["2005"]
    assert tags["discnumber"] == ["2/2"]
    assert tags["tracknumber"] == ["1/1"]
    assert final.name in (folder / "Record.m3u").read_text()
    assert "old-file.mp3" not in (folder / "Record.m3u").read_text()


def test_repair_does_not_overwrite_an_earlier_copy_in_trash(repair_library):
    folder, selection = repair_library
    bad = folder / "Same Song.mp3"
    bad.write_bytes(b"current bad copy")
    from settings import get_trash_dir
    music_root = folder.parents[2]
    earlier = get_trash_dir() / bad.relative_to(music_root)
    earlier.parent.mkdir(parents=True)
    earlier.write_bytes(b"earlier copy")
    with pytest.raises(HTTPException) as error:
        app.albums_repair_track(AlbumTrackRepairRequest(**selection, trash_existing=True), REQUEST)
    assert error.value.status_code == 409
    assert bad.read_bytes() == b"current bad copy"
    assert earlier.read_bytes() == b"earlier copy"


def test_trashing_only_audio_keeps_existing_m3u_without_stale_entry(repair_library):
    folder, selection = repair_library
    bad = folder / "Same Song.mp3"
    bad.write_bytes(b"bad copy")
    playlist = folder / "Record.m3u"
    playlist.write_text("#EXTM3U\nSame Song.mp3\n")
    prepared = app.albums_repair_track(AlbumTrackRepairRequest(**selection, trash_existing=True), REQUEST)
    assert prepared["trashed"] is True
    assert not bad.exists()
    assert playlist.read_text() == "#EXTM3U\n"


def test_track_status_musicbrainz_outage_is_a_retryable_error(repair_library, monkeypatch):
    def unavailable(mbid):
        raise metadata.MusicBrainzUnavailable("offline")
    monkeypatch.setattr(app, "fetch_album_tracks", unavailable)
    with pytest.raises(HTTPException) as error:
        app.albums_missing_tracks(RELEASE, "Various Artists", "Record", REQUEST)
    assert error.value.status_code == 503


@pytest.mark.parametrize("provider_metadata", [None, {"artist": "Wrong artist", "title": "Wrong song", "album": "Singles"}])
def test_youtube_worker_keeps_album_credit_even_without_metadata(repair_library, monkeypatch, make_audio, tmp_path, provider_metadata):
    folder, selection = repair_library
    source = make_audio(tmp_path / "source.flac")
    (folder / "Record.m3u").write_text("#EXTM3U\n")
    monkeypatch.setattr(app, "spawn_daemon_thread", lambda *args, **kwargs: None)
    result = app.download(DownloadRequest(video_id="abcdefghijk", title="Candidate", album_repair=selection, convert_audio=False), REQUEST)
    monkeypatch.setattr(downloads, "get_album_art_context", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(downloads, "lookup_metadata", lambda *args, **kwargs: provider_metadata)
    monkeypatch.setattr(downloads, "fetch_cover_art", lambda *args, **kwargs: None)
    monkeypatch.setattr(downloads, "fetch_lyrics", lambda *args, **kwargs: None)
    for name in ("trigger_navidrome_scan", "trigger_jellyfin_scan", "trigger_plex_scan", "send_notification", "_apply_replaygain", "_apply_album_replaygain", "_copy_to_auto_import", "_normalise_loudness"):
        monkeypatch.setattr(downloads, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(downloads.library_index, "index_file", lambda *args, **kwargs: None)
    info = {"title": "Uploader - Provider song", "channel": "Wrong channel"}
    monkeypatch.setattr(downloads, "run_ytdlp_with_retries", lambda cmd, *args, **kwargs: (subprocess.CompletedProcess(cmd, 0, json.dumps(info), ""), False))

    def download_bytes(cmd, *args, **kwargs):
        output = cmd[cmd.index("-o") + 1].replace("%(ext)s", "flac")
        shutil.copyfile(source, output)
        return subprocess.CompletedProcess(cmd, 0, "", ""), False

    monkeypatch.setattr(downloads, "_run_ytdlp_with_retries", download_bytes)
    downloads.process_download(result["job_id"], "abcdefghijk", convert_audio=False, allow_cross_source_fallback=False)
    with db.db_conn() as conn:
        conn.row_factory = sqlite3.Row
        job = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (result["job_id"],)).fetchone())
    assert job["status"] == "completed", job["error"]
    final = folder / "02-01 - Same Song.flac"
    assert job["artist"] == "Guest Two"
    assert job["final_path"] == str(final)
    tags = MutagenFile(str(final), easy=True)
    assert tags["artist"] == ["Guest Two"]
    assert tags["album"] == ["Record"]
    assert tags["albumartist"] == ["Various Artists"]
    assert tags["discnumber"] == ["2"]
    assert tags["tracknumber"] == ["1"]
    assert final.name in (folder / "Record.m3u").read_text()


def test_restoring_abandoned_repair_refreshes_album_m3u_and_queue(repair_library):
    folder, selection = repair_library
    bad = folder / "Same Song.mp3"
    bad.write_bytes(b"old copy")
    playlist = folder / "Record.m3u"
    playlist.write_text("#EXTM3U\nSame Song.mp3\n")
    with db.db_conn() as conn:
        conn.execute("INSERT INTO jobs (id, status, final_path) VALUES ('original', 'completed', ?)", (str(bad),))
        conn.commit()
    app.albums_repair_track(AlbumTrackRepairRequest(**selection, trash_existing=True), REQUEST)
    assert bad.name not in playlist.read_text()
    result = app.restore_trash_file(REQUEST, str(bad.relative_to(folder.parents[2])))
    assert result["path"] == str(bad)
    assert bad.read_bytes() == b"old copy"
    assert bad.name in playlist.read_text()
    with db.db_conn() as conn:
        assert conn.execute("SELECT file_deleted FROM jobs WHERE id = 'original'").fetchone()[0] == 0


def test_unique_title_repair_can_remove_a_copy_with_wrong_number_tags(repair_library, make_audio):
    folder, selection = repair_library
    bad = make_audio(folder / "Second Song.flac")
    metadata.apply_metadata_to_file(bad, "Wrong artist", "Wrong recording", "Singles", track_number=99, disc_number=9)
    selection = selection | {"track_index": 1}
    status = app.albums_missing_tracks(RELEASE, "Various Artists", "Record", REQUEST)
    assert status["tracks"][1]["exists"] is True
    prepared = app.albums_repair_track(AlbumTrackRepairRequest(**selection, trash_existing=True), REQUEST)
    assert prepared["trashed"] is True
    assert prepared["context"]["track_number"] == 2
    assert prepared["context"]["disc_number"] == 1
    assert not bad.exists()


def test_missing_only_album_import_keeps_release_positions(repair_library, monkeypatch, make_audio):
    """A top-up of a double album must tag and name each track by its place on
    the release, not by the order it happened to be queued in."""
    import bulk_import
    folder, _selection = repair_library
    monkeypatch.setattr(bulk_import, "spawn_daemon_thread", lambda *args, **kwargs: None)
    # Disc 1 track 1 is already on disk, so only two of the three are queued.
    existing = make_audio(folder / "Guest One - Same Song.flac")
    tags = MutagenFile(str(existing), easy=True)
    tags["tracknumber"], tags["discnumber"] = "1", "1"
    tags.save()

    result = albums.queue_album_download("Various Artists", "Record", RELEASE, convert_audio=False)

    with db.db_conn() as conn:
        rows = conn.execute(
            """SELECT line_num, artist, song, album_track_number, album_track_total,
                      album_disc_number, album_disc_total
               FROM bulk_import_tracks WHERE import_id = ? ORDER BY line_num""",
            (result["import_id"],),
        ).fetchall()
        for line_num, job_id in ((1, "job-a"), (2, "job-b")):
            conn.execute(
                "UPDATE bulk_import_tracks SET job_id = ? WHERE import_id = ? AND line_num = ?",
                (job_id, result["import_id"], line_num),
            )
        conn.commit()
    assert [tuple(r) for r in rows] == [
        (1, "Guest One", "Second Song", 2, 2, 1, 2),
        (2, "Guest Two", "Same Song", 1, 1, 2, 2),
    ]

    second = downloads._get_job_album_context("job-a")
    assert (second["track_number"], second["track_total"], second["disc_number"], second["disc_total"]) == (2, 2, 1, 2)
    assert downloads._get_album_track_tag_context("job-a") == (2, 2)
    repeat = downloads._get_job_album_context("job-b")
    assert (repeat["track_artist"], repeat["track_title"], repeat["disc_number"]) == ("Guest Two", "Same Song", 2)
    # Disc 2's "Same Song" no longer lands on top of disc 1's.
    assert downloads._album_repair_output_stem(repeat, "job-b", None) == "02-01 - Same Song"


def test_old_album_import_rows_still_fall_back_to_their_line(repair_library):
    """Rows queued before positions were stored keep the old behaviour."""
    folder, _selection = repair_library
    with db.db_conn() as conn:
        conn.execute(
            """INSERT INTO bulk_imports (id, status, total_tracks, convert_audio, override_dir,
                                         album_release_mbid, album_total_tracks, album_year)
               VALUES ('old-imp', 'completed', 3, 0, ?, ?, 3, '2005')""",
            (str(folder), RELEASE),
        )
        conn.execute(
            """INSERT INTO bulk_import_tracks (import_id, line_num, artist, song, status, job_id)
               VALUES ('old-imp', 3, 'Guest Two', 'Same Song', 'completed', 'old-job')"""
        )
        conn.commit()

    context = downloads._get_job_album_context("old-job")
    assert (context["track_number"], context["track_total"], context["disc_number"]) == (3, 3, None)
    assert downloads._album_repair_output_stem(context, "old-job", None) is None


@pytest.mark.parametrize("sidecar,expected", [(None, "Record"), ({"album": "Record Proper"}, "Record Proper")])
def test_add_to_album_keeps_the_folder_year_out_of_the_album_tag(repair_library, monkeypatch, sidecar, expected):
    """"Add to album..." names the folder it picked; with a "(2004)" folder that
    used to become the ALBUM tag too."""
    folder, _selection = repair_library
    target = folder.parent.parent / "Someone Else" / "Record (2004)"
    target.mkdir(parents=True)
    if sidecar:
        (target / ".albuminfo").write_text(json.dumps(sidecar))
    monkeypatch.setattr(app, "spawn_daemon_thread", lambda *args, **kwargs: None)

    result = app.download(DownloadRequest(
        video_id="abcdefghijk", title="Song", artist="Someone Else",
        album_artist="Someone Else", album_name="Record (2004)",
    ), REQUEST)

    with db.db_conn() as conn:
        override_dir, album_name = conn.execute(
            "SELECT override_dir, album_name FROM jobs WHERE id = ?", (result["job_id"],)
        ).fetchone()
    assert (override_dir, album_name) == (str(target), expected)
