"""
Download flow tests.

Fast tests: queue jobs, verify shape and fields, clean up immediately (no waiting).
Slow tests: actually wait for completion and check outcomes including file placement.

Run fast only:  pytest -m "not slow"
Run all:        pytest -m slow (or --slow via run_tests.sh)

---------------------------------------------------------------------
Clarification on download_type values:
  "single"   (default) -- one video_id, land in Singles/ (or Playlists/ if
             use_playlists_dir=True + playlist_name set).
  "playlist" -- treats video_id as a YouTube playlist ID and fans out via
             process_playlist_download. Not used in these tests.
---------------------------------------------------------------------
"""

import pathlib
import time
from contextlib import contextmanager

import pytest


from live_media import MATCHABLE_SONG, next_short_clip, tidy_up

# Short clips come from live_media's pool, a different one per download.
# AcoustID fingerprinting is skipped for <30s files, so MB won't match.

# A track MusicBrainz reliably identifies via AcoustID fingerprint.
_MATCH_YT_ID, _MATCH_ARTIST, _MATCH_TITLE = MATCHABLE_SONG

_TERMINAL = ("completed", "failed", "completed_with_errors")

# Playlist-routed tests write "Combo Test.m3u", and nothing over the API can
# delete an M3U, so they used to leave one in the real Playlists folder every
# run. A dot-folder is skipped by Navidrome, Jellyfin and Plex alike, so the
# leftover sits where no media server will ever offer it as a playlist.
_TEST_PLAYLISTS_SUBDIR = ".musicgrabber-tests"


# ---------------------------------------------------------------------------
# Fast: numbered filename padding (issue #86)
# ---------------------------------------------------------------------------

def test_numbered_output_stem_pads_to_two_digits_by_default():
    # playlist_routed=True adds the "Artist - " prefix (same as _playlist_stem),
    # which sidesteps the organise_by_artist DB lookup for this unit test.
    import downloads

    stem = downloads._numbered_output_stem(
        "Artist", "Track", "fallback", track_number=1, playlist_routed=True,
    )
    assert stem == "Artist - 01 - Track"


def test_numbered_output_stem_pads_to_ten_digit_track_when_ten_or_above():
    import downloads

    stem = downloads._numbered_output_stem(
        "Artist", "Track", "fallback", track_number=10, playlist_routed=True,
    )
    assert stem == "Artist - 10 - Track"


def test_numbered_output_stem_pads_wider_for_large_track_totals():
    # A 120-track box set should get "007", not "07" lost among "112".
    import downloads

    stem = downloads._numbered_output_stem(
        "Artist", "Track", "fallback", track_number=7, track_total=120, playlist_routed=True,
    )
    assert stem == "Artist - 007 - Track"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wait_for_job(api, base_url, job_id, timeout=120):
    """Poll until the job reaches a terminal state. Returns the final job dict."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        r = api.get(f"{base_url}/api/jobs/{job_id}", timeout=10)
        assert r.status_code == 200
        job = r.json()
        if job.get("status") in _TERMINAL:
            return job
    raise TimeoutError(f"Job {job_id} did not complete within {timeout}s")


def _queue_download(api, base_url, **kwargs):
    """POST /api/download with the given payload. Returns (job_id, response_json)."""
    video_id, artist, title = next_short_clip()
    payload = {
        "video_id": video_id,
        "title": title,
        "artist": artist,
        "source": "youtube",
        "convert_to_flac": True,
        **kwargs,
    }
    r = api.post(f"{base_url}/api/download", json=payload, timeout=15)
    assert r.status_code == 200, f"POST /api/download failed: {r.text}"
    data = r.json()
    job_id = data.get("job_id") or data.get("id")
    assert job_id, f"No job_id in response: {data}"
    return job_id, data


def _cleanup_downloaded_file(api, base_url, job_id=None):
    if job_id:
        tidy_up(api, base_url, job_id)


@contextmanager
def _temp_settings(api, base_url, **overrides):
    """Temporarily patch settings, restoring the originals on exit."""
    original = api.get(f"{base_url}/api/settings", timeout=10).json()
    api.put(f"{base_url}/api/settings", json=overrides, timeout=10)
    try:
        yield original
    finally:
        restore = {k: original.get(k) for k in overrides}
        api.put(f"{base_url}/api/settings", json=restore, timeout=10)


def _assert_completed(job):
    assert job["status"] in ("completed", "completed_with_errors"), \
        f"Download failed or timed out: {job.get('error')}"
    assert not (job.get("error") or "").startswith("Already exists"), \
        f"Nothing downloaded, the dupe check matched a file left behind: {job['error']}"


def _mb_skip_if_no_override(job, artist, title):
    """Skip (not fail) when MB returned no album so routing never triggered.

    A dupe hit is a failure, not a skip: the test song is in the library, so
    nothing downloaded and nothing was routed, and a skip hides that forever.
    """
    assert not (job.get("error") or "").startswith("Already exists"), (
        f"'{artist} - {title}' is already in the library, so routing was never "
        f"exercised ({job['error']}). Pick another MATCHABLE_SONG in live_media.py."
    )
    if not job.get("override_dir"):
        pytest.skip(
            f"MusicBrainz didn't return album metadata for '{artist} - {title}'; "
            "routing can't be verified (download itself completed OK)"
        )


def _import_downloads_or_skip():
    try:
        import downloads
    except ModuleNotFoundError as exc:
        pytest.skip(f"downloads module dependencies unavailable: {exc.name}")
    return downloads


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_navidrome_duplicate_retries_with_primary_credited_artist(monkeypatch):
    downloads = _import_downloads_or_skip()
    queries = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None, **kwargs):
            query = params["query"]
            queries.append(query)
            songs = []
            if query == "The Chemical Brothers Go":
                songs = [{
                    "title": "Go",
                    "artist": "The Chemical Brothers",
                    "albumArtist": "The Chemical Brothers",
                    "album": "Born in the Echoes",
                    "path": "/music/The Chemical Brothers/Go.flac",
                }]
            return _FakeResponse({
                "subsonic-response": {
                    "status": "ok",
                    "searchResult2": {"song": songs},
                }
            })

    monkeypatch.setattr(downloads, "get_setting_bool", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        downloads,
        "get_setting",
        lambda key, user_id=None: {
            "navidrome_url": "http://navidrome",
            "navidrome_user": "user",
            "navidrome_pass": "pass",
        }.get(key, ""),
    )
    monkeypatch.setattr(downloads.httpx, "Client", FakeClient)

    found = downloads.check_navidrome_duplicate(
        "The Chemical Brothers, Q-Tip", "Go"
    )

    assert found == pathlib.Path("/music/The Chemical Brothers/Go.flac")
    assert queries == [
        "The Chemical Brothers, Q-Tip Go",
        "The Chemical Brothers Go",
    ]


def test_lidarr_duplicate_matches_primary_credited_artist(monkeypatch):
    downloads = _import_downloads_or_skip()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            if url.endswith("/api/v1/artist"):
                return _FakeResponse([
                    {"id": 42, "artistName": "The Chemical Brothers"},
                ])
            if url.endswith("/api/v1/track"):
                return _FakeResponse([{
                    "title": "Go",
                    "hasFile": True,
                    "trackFileId": 7,
                }])
            if url.endswith("/api/v1/trackfile"):
                return _FakeResponse([{
                    "id": 7,
                    "path": "/music/The Chemical Brothers/Go.flac",
                }])
            raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(
        downloads,
        "get_setting",
        lambda key, user_id=None: {
            "lidarr_url": "http://lidarr",
            "lidarr_api_key": "secret",
        }.get(key, ""),
    )
    monkeypatch.setattr(downloads.httpx, "Client", FakeClient)

    found = downloads.check_lidarr_duplicate(
        "The Chemical Brothers, Q-Tip", "Go"
    )

    assert found == pathlib.Path("/music/The Chemical Brothers/Go.flac")


def test_trigger_plex_scan_refreshes_only_music_sections(monkeypatch):
    downloads = _import_downloads_or_skip()
    refreshed = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None, **kwargs):
            assert headers.get("X-Plex-Token") == "token"
            if url.endswith("/library/sections"):
                return _FakeResponse({
                    "MediaContainer": {
                        "Directory": [
                            {"key": "1", "type": "movie", "title": "Movies"},
                            {"key": "2", "type": "artist", "title": "Music"},
                            {"key": "3", "type": "artist", "title": "Audiobooks"},
                        ]
                    }
                })
            refreshed.append(url)
            return _FakeResponse({})

    monkeypatch.setattr(
        downloads,
        "get_setting",
        lambda key, user_id=None: {
            "plex_url": "http://plex",
            "plex_token": "token",
        }.get(key, ""),
    )
    monkeypatch.setattr(downloads.httpx, "Client", FakeClient)

    downloads.trigger_plex_scan()

    assert refreshed == [
        "http://plex/library/sections/2/refresh",
        "http://plex/library/sections/3/refresh",
    ]


def test_trigger_jellyfin_scan_uses_modern_authorization(monkeypatch):
    downloads = _import_downloads_or_skip()
    requests = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, **kwargs):
            requests.append((url, headers))
            return _FakeResponse({})

    monkeypatch.setattr(
        downloads,
        "get_setting",
        lambda key, user_id=None: {
            "jellyfin_url": "http://jellyfin",
            "jellyfin_api_key": "token",
        }.get(key, ""),
    )
    monkeypatch.setattr(downloads.httpx, "Client", FakeClient)

    downloads.trigger_jellyfin_scan()

    assert requests == [(
        "http://jellyfin/Library/Refresh",
        {"Authorization": 'MediaBrowser Token="token"'},
    )]


def test_trigger_plex_scan_noop_when_unconfigured(monkeypatch):
    downloads = _import_downloads_or_skip()
    called = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            called.append(True)

    monkeypatch.setattr(downloads, "get_setting", lambda key, user_id=None: "")
    monkeypatch.setattr(downloads.httpx, "Client", FakeClient)

    downloads.trigger_plex_scan()

    assert called == []


@pytest.mark.parametrize("stderr, expected", [
    # The exact shape that flaked a release build: YouTube's thumbnail CDN
    # didn't serve the .webp, so the convert/embed step died even though the
    # audio downloaded fine. Must be recoverable, not fatal.
    ("ERROR: [Errno 2] No such file or directory: '/music/Singles/jawed/Me at the zoo.webp'", True),
    ("ERROR: Unable to embed thumbnail in the output file", True),
    ("ERROR: [Errno 2] No such file or directory: cover.jpg", True),
    # Genuine audio failures must NOT be swallowed as recoverable thumbnail blips.
    ("ERROR: unable to download video data: HTTP Error 403: Forbidden", False),
    ("ERROR: Postprocessing: Conversion failed!", False),
    ("", False),
])
def test_thumbnail_postprocess_failure_detection(stderr, expected):
    downloads = _import_downloads_or_skip()
    assert downloads._is_thumbnail_postprocess_failure(stderr) is expected


# yt-dlp runs --convert-thumbnails as a 'before_dl' postprocessor, so a missing
# thumbnail kills the job before any audio is fetched and there is nothing left
# to salvage. These cover the retry that gives the track a second chance.

def test_strip_thumbnail_args_removes_art_handling_and_nothing_else():
    downloads = _import_downloads_or_skip()
    cmd = downloads._build_ytdlp_download_cmd(
        "abc123", "/music/Singles/Artist/Title.%(ext)s", False, use_cookies=False
    )
    stripped = downloads._strip_thumbnail_args(cmd)

    assert "--embed-thumbnail" not in stripped
    assert "--convert-thumbnails" not in stripped
    assert "jpg" not in stripped          # the value went with its flag
    assert "--ppa" not in stripped
    assert not any(arg.startswith("ffmpeg:") for arg in stripped)

    # Everything that actually fetches audio must survive intact.
    assert stripped[0] == "yt-dlp"
    assert stripped[-1].endswith("abc123")
    for kept in ("-x", "--embed-metadata", "--add-metadata", "-o", "--audio-quality"):
        assert kept in stripped
    assert "/music/Singles/Artist/Title.%(ext)s" in stripped
    assert stripped.count("--parse-metadata") == 2


def test_retry_without_thumbnail_reruns_with_art_disabled(monkeypatch):
    downloads = _import_downloads_or_skip()
    seen = {}

    def fake_run(cmd, timeout, has_cookies):
        seen["cmd"] = cmd
        return "result", False

    monkeypatch.setattr(downloads, "_run_ytdlp_with_retries", fake_run)

    result, timed_out = downloads._retry_ytdlp_without_thumbnail(
        ["yt-dlp", "--embed-thumbnail", "--convert-thumbnails", "jpg", "-x", "url"],
        300,
        False,
        "ERROR: [Errno 2] No such file or directory: '/music/Singles/a/b.webp'",
    )

    assert (result, timed_out) == ("result", False)
    assert seen["cmd"] == ["yt-dlp", "-x", "url"]


def test_retry_without_thumbnail_leaves_real_failures_alone(monkeypatch):
    """A 403 is not a cover-art problem; retrying without art would just waste a call."""
    downloads = _import_downloads_or_skip()

    def explode(*args, **kwargs):
        raise AssertionError("should not have retried")

    monkeypatch.setattr(downloads, "_run_ytdlp_with_retries", explode)

    assert downloads._retry_ytdlp_without_thumbnail(
        ["yt-dlp", "--embed-thumbnail", "url"],
        300,
        False,
        "ERROR: unable to download video data: HTTP Error 403: Forbidden",
    ) == (None, False)


@pytest.mark.parametrize(
    "actual,advertised,expected_ok",
    [
        (30, 240, False),   # classic CDN preview: valid audio, wrong amount of it
        (180, 240, False),  # a decodable but substantially truncated response
        (218, 240, True),   # within 10%; metadata/rounding drift gets grace
        (14, 20, True),     # tiny tracks also need the 15-second absolute grace
        (420, 420, True),   # an intentional seven-minute live/extended version
        (420, 240, True),   # longer-than-advertised is not a preview shortfall
        (30, None, True),   # sources that advertise no duration keep old checks
    ],
)
def test_selected_result_duration_detects_samples_without_rejecting_variants(
    actual, advertised, expected_ok,
):
    downloads = _import_downloads_or_skip()
    ok, reason = downloads._check_duration_against_selected_result(actual, advertised)
    assert ok is expected_ok
    assert bool(reason) is (not expected_ok)


def test_manual_completeness_gate_requires_search_token_and_single_job(monkeypatch):
    """Only a real manual search pick should activate the selected-duration gate."""
    import sqlite3
    from contextlib import contextmanager

    downloads = _import_downloads_or_skip()
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE jobs (
               id TEXT PRIMARY KEY,
               search_token TEXT,
               selected_duration_secs REAL,
               download_type TEXT
           )"""
    )
    conn.executemany(
        "INSERT INTO jobs VALUES (?, ?, ?, ?)",
        [
            ("manual", "server-token", 240, "single"),
            ("automatic", None, 240, "single"),
            ("playlist", "server-token", 240, "playlist"),
        ],
    )

    @contextmanager
    def fake_db_conn():
        yield conn

    monkeypatch.setattr(downloads, "db_conn", fake_db_conn)
    assert downloads._check_manual_download_completeness(30, "manual")[0] is False
    assert downloads._check_manual_download_completeness(30, "automatic") == (True, "")
    assert downloads._check_manual_download_completeness(30, "playlist") == (True, "")
    # The selected seven-minute version matches its own advertised duration,
    # while the old manual-pick rule still permits it to differ from a
    # four-minute canonical MusicBrainz recording.
    conn.execute(
        "UPDATE jobs SET selected_duration_secs = 420 WHERE id = 'manual'"
    )
    assert downloads._check_manual_download_completeness(420, "manual") == (True, "")
    assert downloads._check_duration_against_mb(
        420, {"expected_duration_secs": 240}, "Artist", "Song (live)", job_id="manual",
    ) == (True, "")


# ---------------------------------------------------------------------------
# _maybe_mark_monochrome_unhealthy: a single track missing from every leg
# (tracks API, Deezer rescue, Tidal stream) raises the exact same exception as
# the whole leg being down. These confirm we re-check the leg itself before
# parking Monochrome for everyone over one unlucky search result.
# ---------------------------------------------------------------------------

def test_maybe_mark_monochrome_unhealthy_skips_healthy_leg(monkeypatch):
    downloads = _import_downloads_or_skip()
    import monochrome
    import servicecheck

    monkeypatch.setattr(monochrome, "download_leg_healthy",
                         lambda: (True, "Monochrome tracks API answering at https://tracks.test"))
    calls = []
    monkeypatch.setattr(servicecheck, "mark_unhealthy", lambda *a, **k: calls.append((a, k)))

    downloads._maybe_mark_monochrome_unhealthy(
        RuntimeError("Monochrome: no stream available for ISRC GBZZZ9900001 at an allowed quality on any leg")
    )

    assert calls == []  # one obscure track is not evidence the whole source is down


def test_maybe_mark_monochrome_unhealthy_parks_source_when_leg_is_down(monkeypatch):
    downloads = _import_downloads_or_skip()
    import monochrome
    import servicecheck

    reason = "Monochrome tracks API unreachable: HTTPStatusError: 503 Service Unavailable"
    monkeypatch.setattr(monochrome, "download_leg_healthy", lambda: (False, reason))
    calls = []
    monkeypatch.setattr(servicecheck, "mark_unhealthy", lambda *a, **k: calls.append((a, k)))

    downloads._maybe_mark_monochrome_unhealthy(RuntimeError("boom"))

    assert calls == [(("monochrome", reason), {})]


def test_maybe_mark_monochrome_unhealthy_falls_back_to_exception_text_when_leg_gives_no_reason(monkeypatch):
    downloads = _import_downloads_or_skip()
    import monochrome
    import servicecheck

    monkeypatch.setattr(monochrome, "download_leg_healthy", lambda: (False, ""))
    calls = []
    monkeypatch.setattr(servicecheck, "mark_unhealthy", lambda *a, **k: calls.append((a, k)))

    downloads._maybe_mark_monochrome_unhealthy(RuntimeError("boom"))

    assert calls == [(("monochrome", "boom"), {})]


def test_maybe_mark_monochrome_unhealthy_fails_open_when_probe_errors(monkeypatch):
    """If we can't even tell whether the leg is healthy, don't punish it for that."""
    downloads = _import_downloads_or_skip()
    import monochrome
    import servicecheck

    def _boom():
        raise RuntimeError("probe itself blew up")
    monkeypatch.setattr(monochrome, "download_leg_healthy", _boom)
    calls = []
    monkeypatch.setattr(servicecheck, "mark_unhealthy", lambda *a, **k: calls.append((a, k)))

    downloads._maybe_mark_monochrome_unhealthy(RuntimeError("no stream available"))

    assert calls == []


def _album_routing_on(monkeypatch, downloads, tmp_path, m3u_disabled=False):
    monkeypatch.setattr(downloads, "_update_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(downloads, "set_file_permissions", lambda *args, **kwargs: None)
    monkeypatch.setattr(downloads, "get_singles_dir", lambda user_id=None: tmp_path / "Singles")
    monkeypatch.setattr(downloads, "get_albums_dir", lambda user_id=None: tmp_path / "Albums")
    monkeypatch.setattr(downloads, "_watched_playlist_m3u_disabled",
                        lambda name, user_id: m3u_disabled)
    monkeypatch.setattr(
        downloads,
        "get_setting_bool",
        lambda key, default=False, user_id=None: {
            "auto_album_singles": True,
            "organise_by_artist": False,
        }.get(key, default),
    )


def test_auto_route_single_goes_to_albums_under_the_album_artist(tmp_path, monkeypatch):
    downloads = _import_downloads_or_skip()
    _album_routing_on(monkeypatch, downloads, tmp_path)

    source_dir = tmp_path / "Singles" / "Luis Fonsi, Daddy Yankee, Justin Bieber"
    source_dir.mkdir(parents=True)
    audio_file = source_dir / "Despacito (Remix).flac"
    audio_file.write_bytes(b"not real audio")

    routed, did_route = downloads._auto_route_to_album(
        audio_file,
        "Luis Fonsi, Daddy Yankee, Justin Bieber",
        "Despacito (Remix)",
        {"album": "Vida", "album_artist": "Luis Fonsi"},
        "job-id",
        None,
    )

    assert did_route is True
    assert routed == tmp_path / "Albums" / "Luis Fonsi" / "Vida" / "Luis Fonsi - Despacito (Remix).flac"
    assert routed.exists()
    assert not source_dir.exists()  # the emptied artist folder is tidied away


def test_auto_route_playlist_track_goes_to_albums_not_the_playlist_folder(tmp_path, monkeypatch):
    downloads = _import_downloads_or_skip()
    _album_routing_on(monkeypatch, downloads, tmp_path)

    playlist_dir = tmp_path / "Playlists" / "Test"
    playlist_dir.mkdir(parents=True)
    audio_file = playlist_dir / "Luis Fonsi, Daddy Yankee, Justin Bieber - Despacito (Remix).flac"
    audio_file.write_bytes(b"not real audio")

    routed, did_route = downloads._auto_route_to_album(
        audio_file,
        "Luis Fonsi, Daddy Yankee, Justin Bieber",
        "Despacito (Remix)",
        {"album": "Vida", "album_artist": "Luis Fonsi"},
        "job-id",
        None,
        playlist_name="Test",
    )

    assert did_route is True
    assert routed == tmp_path / "Albums" / "Luis Fonsi" / "Vida" / "Luis Fonsi - Despacito (Remix).flac"
    assert routed.exists()


def test_auto_route_leaves_a_playlist_without_an_m3u_alone(tmp_path, monkeypatch):
    """No M3U means the folder is the playlist; moving tracks out would lose it."""
    downloads = _import_downloads_or_skip()
    _album_routing_on(monkeypatch, downloads, tmp_path, m3u_disabled=True)

    playlist_dir = tmp_path / "Playlists" / "Test"
    playlist_dir.mkdir(parents=True)
    audio_file = playlist_dir / "Ramones - Blitzkrieg Bop.flac"
    audio_file.write_bytes(b"not real audio")

    routed, did_route = downloads._auto_route_to_album(
        audio_file, "Ramones", "Blitzkrieg Bop",
        {"album": "Ramones", "album_artist": "Ramones"},
        "job-id", None, playlist_name="Test",
    )

    assert did_route is False
    assert routed == audio_file and audio_file.exists()
    assert not (tmp_path / "Albums").exists()


def test_m3u_entries_are_relative_to_the_m3u_unless_from_outside_the_library(tmp_path, monkeypatch):
    downloads = _import_downloads_or_skip()
    music = tmp_path / "music"
    monkeypatch.setattr(downloads, "get_setting", lambda key, default=None, user_id=None:
                        str(music) if key == "music_dir" else default)
    playlists = music / "Playlists"

    assert downloads._m3u_entry(playlists / "Rock Mix" / "a.flac", playlists) == "Rock Mix/a.flac"
    assert downloads._m3u_entry(music / "Albums" / "Ramones" / "Ramones" / "b.flac", playlists) \
        == "../Albums/Ramones/Ramones/b.flac"
    # Navidrome's own filesystem path: not ours to rewrite
    assert downloads._m3u_entry(pathlib.Path("/navidrome/music/c.flac"), playlists) == "/navidrome/music/c.flac"


def _setup_job_album_context_db(monkeypatch, downloads):
    """In-memory sqlite with just enough schema for _get_job_album_context."""
    import sqlite3
    from contextlib import contextmanager

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            override_dir TEXT,
            album_release_mbid TEXT,
            album_name TEXT,
            album_track_title TEXT,
            album_track_number INTEGER,
            album_track_total INTEGER,
            download_type TEXT DEFAULT 'single'
        );
        CREATE TABLE bulk_imports (id TEXT PRIMARY KEY, override_dir TEXT, album_release_mbid TEXT,
                                    total_tracks INTEGER, album_total_tracks INTEGER,
                                    album_year TEXT);
        CREATE TABLE bulk_import_tracks (id INTEGER PRIMARY KEY AUTOINCREMENT, import_id TEXT,
                                          job_id TEXT, song TEXT, line_num INTEGER);
    """)

    @contextmanager
    def fake_db_conn():
        yield conn

    monkeypatch.setattr(downloads, "db_conn", fake_db_conn)
    return conn


def test_get_job_album_context_ignores_staging_dir_for_upgrade_jobs(monkeypatch):
    """Regression guard: Track Upgrades stages downloads under a scratch temp
    dir (e.g. /data/upg_42_xyz), not a real Artist/Album path. Treating that
    temp dir as album routing wrote garbage ARTIST/ALBUM tags ("data" /
    "upg_42_xyz") over the user's real metadata on swap-in."""
    downloads = _import_downloads_or_skip()
    conn = _setup_job_album_context_db(monkeypatch, downloads)

    conn.execute(
        "INSERT INTO jobs (id, override_dir, download_type) VALUES (?, ?, ?)",
        ("job-1", "/data/upg_42_abc123", "upgrade"),
    )
    conn.commit()

    assert downloads._get_job_album_context("job-1") == {}


def test_get_job_album_context_still_derives_names_for_album_jobs(monkeypatch):
    """Sanity check the upgrade-job carve-out didn't break real album routing,
    where override_dir genuinely is an Albums/Artist/Album path."""
    downloads = _import_downloads_or_skip()
    conn = _setup_job_album_context_db(monkeypatch, downloads)

    conn.execute(
        "INSERT INTO jobs (id, override_dir, download_type) VALUES (?, ?, ?)",
        ("job-2", "/music/Albums/Luis Fonsi/Vida", "single"),
    )
    conn.commit()

    ctx = downloads._get_job_album_context("job-2")
    assert ctx["album_artist"] == "Luis Fonsi"
    assert ctx["album_name"] == "Vida"


def test_get_job_album_context_carries_the_album_year(monkeypatch):
    """The year we captured when the album was queued must reach the tagger.
    Without it each track asks MusicBrainz on its own and an album can come
    back wearing four different years, which pleases absolutely nobody."""
    downloads = _import_downloads_or_skip()
    conn = _setup_job_album_context_db(monkeypatch, downloads)

    conn.execute(
        "INSERT INTO bulk_imports (id, override_dir, album_release_mbid, total_tracks,"
        " album_total_tracks, album_year) VALUES (?, ?, ?, ?, ?, ?)",
        ("imp-1", "/music/Albums/Pink Floyd/The Dark Side of the Moon", "rel-1", 10, 10, "1973"),
    )
    conn.execute(
        "INSERT INTO bulk_import_tracks (import_id, job_id, song, line_num) VALUES (?, ?, ?, ?)",
        ("imp-1", "job-3", "Time", 4),
    )
    conn.commit()

    ctx = downloads._get_job_album_context("job-3")
    assert ctx["year"] == "1973"
    assert ctx["album_name"] == "The Dark Side of the Moon"


def test_get_job_album_context_year_is_none_when_musicbrainz_had_no_date(monkeypatch):
    """A missing year must stay None so the per-track MusicBrainz guess can
    still have a go, rather than an empty string stamping a blank DATE tag."""
    downloads = _import_downloads_or_skip()
    conn = _setup_job_album_context_db(monkeypatch, downloads)

    conn.execute(
        "INSERT INTO bulk_imports (id, override_dir, album_release_mbid, total_tracks,"
        " album_total_tracks, album_year) VALUES (?, ?, ?, ?, ?, ?)",
        ("imp-2", "/music/Albums/Some Artist/Some Album", "rel-2", 3, 3, ""),
    )
    conn.execute(
        "INSERT INTO bulk_import_tracks (import_id, job_id, song, line_num) VALUES (?, ?, ?, ?)",
        ("imp-2", "job-4", "Track One", 1),
    )
    conn.commit()

    assert downloads._get_job_album_context("job-4")["year"] is None


# ---------------------------------------------------------------------------
# Regression guards for "Add to playlist" / watched-playlist M3U writes.
# These cover two paper-cut bugs we shipped 2.8.15 to fix; if either of these
# tests fails, the playlist .m3u file on disk is silently not being updated.
# ---------------------------------------------------------------------------

def test_append_to_physical_m3u_writes_to_playlists_dir_when_configured(tmp_path, monkeypatch):
    """Happy path: playlists_subdir is set, M3U lands in Playlists/."""
    downloads = _import_downloads_or_skip()

    playlists_dir = tmp_path / "Playlists"
    playlists_dir.mkdir()
    monkeypatch.setattr(downloads, "get_playlists_dir", lambda user_id=None: playlists_dir)
    monkeypatch.setattr(downloads, "get_singles_dir", lambda user_id=None: tmp_path / "Singles")

    audio_file = playlists_dir / "Rock Mix" / "Some Track.flac"
    audio_file.parent.mkdir(parents=True)
    audio_file.write_bytes(b"not real audio")

    downloads._append_to_physical_m3u(audio_file, "Rock Mix", use_playlists_dir=True)

    m3u = playlists_dir / "Rock Mix.m3u"
    assert m3u.exists(), "Expected Playlists/Rock Mix.m3u to be created"
    contents = m3u.read_text(encoding="utf-8")
    assert "#EXTM3U" in contents
    # Relative path inside the playlist folder for portability.
    assert "Rock Mix/Some Track.flac" in contents


def test_append_to_physical_m3u_falls_back_to_singles_when_playlists_dir_unset(tmp_path, monkeypatch):
    """Bug 2 regression guard: without a Playlists folder configured, the
    M3U should still be written (to Singles), not silently dropped."""
    downloads = _import_downloads_or_skip()

    singles_dir = tmp_path / "Singles"
    singles_dir.mkdir()
    # Simulate playlists_subdir unset: get_playlists_dir returns None.
    monkeypatch.setattr(downloads, "get_playlists_dir", lambda user_id=None: None)
    monkeypatch.setattr(downloads, "get_singles_dir", lambda user_id=None: singles_dir)

    audio_file = singles_dir / "Jawed" / "Me at the zoo.flac"
    audio_file.parent.mkdir(parents=True)
    audio_file.write_bytes(b"not real audio")

    downloads._append_to_physical_m3u(audio_file, "My Faves", use_playlists_dir=True)

    m3u = singles_dir / "My Faves.m3u"
    assert m3u.exists(), "Expected Singles/My Faves.m3u as fallback when Playlists dir is unset"
    contents = m3u.read_text(encoding="utf-8")
    assert "#EXTM3U" in contents
    assert str(audio_file) in contents


def test_append_to_physical_m3u_does_not_duplicate_entries(tmp_path, monkeypatch):
    downloads = _import_downloads_or_skip()

    playlists_dir = tmp_path / "Playlists"
    playlists_dir.mkdir()
    monkeypatch.setattr(downloads, "get_playlists_dir", lambda user_id=None: playlists_dir)
    monkeypatch.setattr(downloads, "get_singles_dir", lambda user_id=None: tmp_path / "Singles")

    audio_file = playlists_dir / "Rock Mix" / "Track.flac"
    audio_file.parent.mkdir(parents=True)
    audio_file.write_bytes(b"not real audio")

    downloads._append_to_physical_m3u(audio_file, "Rock Mix", use_playlists_dir=True)
    downloads._append_to_physical_m3u(audio_file, "Rock Mix", use_playlists_dir=True)

    m3u = playlists_dir / "Rock Mix.m3u"
    body = m3u.read_text(encoding="utf-8")
    assert body.count("Rock Mix/Track.flac") == 1, f"Expected one entry, got: {body!r}"


def test_append_to_physical_m3u_noop_without_playlist_name(tmp_path, monkeypatch):
    downloads = _import_downloads_or_skip()

    singles_dir = tmp_path / "Singles"
    singles_dir.mkdir()
    monkeypatch.setattr(downloads, "get_playlists_dir", lambda user_id=None: None)
    monkeypatch.setattr(downloads, "get_singles_dir", lambda user_id=None: singles_dir)

    audio_file = singles_dir / "Jawed" / "Me at the zoo.flac"
    audio_file.parent.mkdir(parents=True)
    audio_file.write_bytes(b"not real audio")

    downloads._append_to_physical_m3u(audio_file, "", use_playlists_dir=True)
    downloads._append_to_physical_m3u(audio_file, None, use_playlists_dir=True)

    # Nothing should land on disk when no playlist is named.
    assert not any(p.suffix == ".m3u" for p in singles_dir.glob("**/*"))


def test_append_to_physical_m3u_falls_back_when_name_sanitizes_empty(tmp_path, monkeypatch):
    downloads = _import_downloads_or_skip()

    singles_dir = tmp_path / "Singles"
    singles_dir.mkdir()
    monkeypatch.setattr(downloads, "get_playlists_dir", lambda user_id=None: None)
    monkeypatch.setattr(downloads, "get_singles_dir", lambda user_id=None: singles_dir)

    audio_file = singles_dir / "Artist" / "Track.flac"
    audio_file.parent.mkdir(parents=True)
    audio_file.write_bytes(b"not real audio")

    downloads._append_to_physical_m3u(audio_file, "///", use_playlists_dir=False)

    m3u = singles_dir / "Playlist.m3u"
    assert m3u.exists(), "Expected empty-after-sanitizing playlist names to fall back"


# ---------------------------------------------------------------------------
# Regression guards for issue #82: "Create M3U" off must mean off. The per-track
# append used to ignore the setting entirely, creating the file the user had
# explicitly declined and appending in download order, which quietly shuffled
# the playlist out of its upstream running order.
# ---------------------------------------------------------------------------

def _wire_watched_playlists_table(monkeypatch, downloads, rows):
    """Give downloads.db_conn a tiny watched_playlists table and nothing else.

    rows is a list of (name, make_m3u, user_id) tuples.
    """
    import sqlite3
    from contextlib import contextmanager

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE watched_playlists (id TEXT PRIMARY KEY, name TEXT, "
        "make_m3u INTEGER DEFAULT 0, user_id TEXT)"
    )
    for i, (name, make_m3u, user_id) in enumerate(rows):
        conn.execute(
            "INSERT INTO watched_playlists (id, name, make_m3u, user_id) VALUES (?, ?, ?, ?)",
            (f"pl-{i}", name, make_m3u, user_id),
        )
    conn.commit()

    @contextmanager
    def fake_db_conn():
        yield conn

    monkeypatch.setattr(downloads, "db_conn", fake_db_conn)
    return conn


def _wire_playlists_dir(monkeypatch, downloads, tmp_path):
    playlists_dir = tmp_path / "Playlists"
    playlists_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(downloads, "get_playlists_dir", lambda user_id=None: playlists_dir)
    monkeypatch.setattr(downloads, "get_singles_dir", lambda user_id=None: tmp_path / "Singles")
    return playlists_dir


def test_append_to_physical_m3u_skipped_when_watched_playlist_has_m3u_disabled(tmp_path, monkeypatch):
    """make_m3u = 0 means no M3U gets conjured into existence."""
    downloads = _import_downloads_or_skip()

    playlists_dir = _wire_playlists_dir(monkeypatch, downloads, tmp_path)
    _wire_watched_playlists_table(monkeypatch, downloads, [("Rock Mix", 0, None)])

    audio_file = playlists_dir / "Rock Mix" / "Some Track.flac"
    audio_file.parent.mkdir(parents=True)
    audio_file.write_bytes(b"not real audio")

    downloads._append_to_physical_m3u(audio_file, "Rock Mix", use_playlists_dir=True)

    assert not (playlists_dir / "Rock Mix.m3u").exists(), \
        "Create M3U is off; MusicGrabber should not have created the file"


def test_append_to_physical_m3u_leaves_existing_file_alone_when_disabled(tmp_path, monkeypatch):
    """An M3U that already exists is left exactly as the user left it."""
    downloads = _import_downloads_or_skip()

    playlists_dir = _wire_playlists_dir(monkeypatch, downloads, tmp_path)
    _wire_watched_playlists_table(monkeypatch, downloads, [("Rock Mix", 0, None)])

    m3u = playlists_dir / "Rock Mix.m3u"
    original = "#EXTM3U\nRock Mix/Old Track.flac\n"
    m3u.write_text(original, encoding="utf-8")

    audio_file = playlists_dir / "Rock Mix" / "New Track.flac"
    audio_file.parent.mkdir(parents=True)
    audio_file.write_bytes(b"not real audio")

    downloads._append_to_physical_m3u(audio_file, "Rock Mix", use_playlists_dir=True)

    assert m3u.read_text(encoding="utf-8") == original, \
        "Create M3U is off; the existing playlist file must not be touched"


def test_append_to_physical_m3u_still_writes_when_watched_playlist_wants_m3u(tmp_path, monkeypatch):
    """The guard must not overreach: make_m3u = 1 still appends as before."""
    downloads = _import_downloads_or_skip()

    playlists_dir = _wire_playlists_dir(monkeypatch, downloads, tmp_path)
    _wire_watched_playlists_table(monkeypatch, downloads, [("Rock Mix", 1, None)])

    audio_file = playlists_dir / "Rock Mix" / "Some Track.flac"
    audio_file.parent.mkdir(parents=True)
    audio_file.write_bytes(b"not real audio")

    downloads._append_to_physical_m3u(audio_file, "Rock Mix", use_playlists_dir=True)

    contents = (playlists_dir / "Rock Mix.m3u").read_text(encoding="utf-8")
    assert "Rock Mix/Some Track.flac" in contents


def test_append_to_physical_m3u_unaffected_for_non_watched_playlists(tmp_path, monkeypatch):
    """Bulk imports own no watched_playlists row, so nothing changes for them."""
    downloads = _import_downloads_or_skip()

    playlists_dir = _wire_playlists_dir(monkeypatch, downloads, tmp_path)
    # A watched playlist exists with M3U off, but under a completely different name.
    _wire_watched_playlists_table(monkeypatch, downloads, [("Someone Else's Mix", 0, None)])

    audio_file = playlists_dir / "Road Trip" / "Some Track.flac"
    audio_file.parent.mkdir(parents=True)
    audio_file.write_bytes(b"not real audio")

    downloads._append_to_physical_m3u(audio_file, "Road Trip", use_playlists_dir=True)

    contents = (playlists_dir / "Road Trip.m3u").read_text(encoding="utf-8")
    assert "Road Trip/Some Track.flac" in contents


def test_append_to_physical_m3u_scopes_make_m3u_check_to_the_owning_user(tmp_path, monkeypatch):
    """Another user's identically named playlist must not veto this write."""
    downloads = _import_downloads_or_skip()

    playlists_dir = _wire_playlists_dir(monkeypatch, downloads, tmp_path)
    _wire_watched_playlists_table(monkeypatch, downloads, [("Rock Mix", 0, "someone-else")])

    audio_file = playlists_dir / "Rock Mix" / "Some Track.flac"
    audio_file.parent.mkdir(parents=True)
    audio_file.write_bytes(b"not real audio")

    downloads._append_to_physical_m3u(audio_file, "Rock Mix", use_playlists_dir=True, user_id="me")

    contents = (playlists_dir / "Rock Mix.m3u").read_text(encoding="utf-8")
    assert "Rock Mix/Some Track.flac" in contents


def _setup_watched_playlist_db(monkeypatch, downloads):
    """Wire downloads.db_conn to an in-memory sqlite with the minimal schema
    needed for _mark_watched_track_downloaded's watched-playlist lookup.

    Returns the live connection so the test can seed and assert on it.
    """
    import sqlite3
    from contextlib import contextmanager

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            artist TEXT,
            title TEXT,
            status TEXT,
            error TEXT,
            skip_mismatch_check INTEGER DEFAULT 0,
            user_id TEXT
        );
        CREATE TABLE watched_playlists (
            id TEXT PRIMARY KEY,
            name TEXT,
            make_m3u INTEGER DEFAULT 1,
            use_playlists_dir INTEGER DEFAULT 0,
            sync_mode TEXT DEFAULT 'append',
            custom_subdir TEXT,
            user_id TEXT
        );
        CREATE TABLE watched_playlist_tracks (
            playlist_id TEXT,
            track_hash TEXT,
            artist TEXT,
            title TEXT,
            downloaded_at TIMESTAMP,
            resolved_path TEXT,
            job_id TEXT,
            PRIMARY KEY (playlist_id, track_hash)
        );
        CREATE TABLE bulk_imports (
            id TEXT PRIMARY KEY,
            watch_playlist_id TEXT
        );
        CREATE TABLE bulk_import_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id TEXT,
            job_id TEXT
        );
    """)

    @contextmanager
    def fake_db_conn():
        yield conn

    monkeypatch.setattr(downloads, "db_conn", fake_db_conn)
    return conn


def test_mark_watched_track_downloaded_rebuilds_m3u_via_missing_tracks_retry(monkeypatch):
    """Bug 1 regression guard: when a job was queued via the missing-tracks
    retry endpoint, there is no bulk_imports row, only a watched_playlist_tracks
    row. The post-download M3U rebuild must still find the playlist and fire."""
    downloads = _import_downloads_or_skip()

    conn = _setup_watched_playlist_db(monkeypatch, downloads)

    conn.execute(
        "INSERT INTO jobs (id, artist, title, status, skip_mismatch_check) VALUES (?, ?, ?, ?, ?)",
        ("job-1", "Jawed", "Me at the zoo", "downloading", 1),
    )
    conn.execute(
        "INSERT INTO watched_playlists (id, name, make_m3u, use_playlists_dir, sync_mode, user_id) "
        "VALUES (?, ?, 1, 1, 'append', NULL)",
        ("pl-1", "Rock Mix"),
    )
    conn.execute(
        "INSERT INTO watched_playlist_tracks (playlist_id, track_hash, artist, title, job_id) "
        "VALUES (?, ?, ?, ?, ?)",
        ("pl-1", "hash-1", "Jawed", "Me at the zoo", "job-1"),
    )
    # Deliberately NO bulk_imports / bulk_import_tracks rows: this mirrors the
    # state created by /api/watched-playlists/{id}/queue-track-candidate.
    conn.commit()

    calls = []
    monkeypatch.setattr(
        downloads, "rebuild_watched_playlist_m3u",
        lambda *args, **kwargs: calls.append((args, kwargs)) or None,
    )

    result = downloads._mark_watched_track_downloaded("job-1", resolved_path=None, skip_mismatch=True)

    assert result is True
    assert len(calls) == 1, (
        "Expected rebuild_watched_playlist_m3u to fire for the missing-tracks "
        "retry path; if this fails, the watched playlist .m3u is going stale "
        "after every missing-track retry (see bug fixed in v2.8.15)."
    )
    args, kwargs = calls[0]
    assert args[0] == "pl-1"
    assert args[1] == "Rock Mix"


def test_manual_download_heals_unlinked_featured_artist_watched_row(
    tmp_path, monkeypatch
):
    """A Search-button download has no watched job link, but ownership is ownership."""
    downloads = _import_downloads_or_skip()
    conn = _setup_watched_playlist_db(monkeypatch, downloads)
    existing = tmp_path / "The Chemical Brothers" / "Go.flac"
    existing.parent.mkdir()
    existing.write_bytes(b"already owned")

    conn.execute(
        """INSERT INTO jobs
           (id, artist, title, status, skip_mismatch_check, user_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("manual-1", "The Chemical Brothers", "Go", "downloading", 0, "user-1"),
    )
    conn.execute(
        """INSERT INTO watched_playlists
           (id, name, make_m3u, use_playlists_dir, sync_mode, user_id)
           VALUES (?, ?, 1, 1, 'append', ?)""",
        ("pl-featured", "Top Songs - United Kingdom", "user-1"),
    )
    conn.execute(
        """INSERT INTO watched_playlist_tracks
           (playlist_id, track_hash, artist, title, job_id)
           VALUES (?, ?, ?, ?, NULL)""",
        (
            "pl-featured",
            "chemical-go",
            "The Chemical Brothers, Q-Tip",
            "Go",
        ),
    )
    conn.commit()

    rebuilds = []
    monkeypatch.setattr(
        downloads,
        "rebuild_watched_playlist_m3u",
        lambda *args, **kwargs: rebuilds.append((args, kwargs)) or None,
    )
    monkeypatch.setattr(downloads, "_tag_track_comment", lambda *args, **kwargs: None)

    result = downloads._mark_watched_track_downloaded(
        "manual-1", resolved_path=existing, skip_mismatch=True
    )

    healed = conn.execute(
        """SELECT downloaded_at, resolved_path
           FROM watched_playlist_tracks
           WHERE playlist_id = ? AND track_hash = ?""",
        ("pl-featured", "chemical-go"),
    ).fetchone()
    assert result is True
    assert healed["downloaded_at"] is not None
    assert healed["resolved_path"] == str(existing)
    assert [call[0][0] for call in rebuilds] == ["pl-featured"]


def test_mark_watched_track_downloaded_rebuilds_m3u_via_bulk_import(monkeypatch):
    """Sister test to the missing-tracks one: the bulk-import refresh path
    (the original code path) must also continue to work."""
    downloads = _import_downloads_or_skip()

    conn = _setup_watched_playlist_db(monkeypatch, downloads)

    conn.execute(
        "INSERT INTO jobs (id, artist, title, status, skip_mismatch_check) VALUES (?, ?, ?, ?, ?)",
        ("job-2", "Jawed", "Me at the zoo", "downloading", 1),
    )
    conn.execute(
        "INSERT INTO watched_playlists (id, name, make_m3u, use_playlists_dir, sync_mode, user_id) "
        "VALUES (?, ?, 1, 1, 'append', NULL)",
        ("pl-2", "Pop Mix"),
    )
    conn.execute(
        "INSERT INTO watched_playlist_tracks (playlist_id, track_hash, artist, title, job_id) "
        "VALUES (?, ?, ?, ?, ?)",
        ("pl-2", "hash-2", "Jawed", "Me at the zoo", "job-2"),
    )
    conn.execute("INSERT INTO bulk_imports (id, watch_playlist_id) VALUES (?, ?)", ("imp-1", "pl-2"))
    conn.execute(
        "INSERT INTO bulk_import_tracks (import_id, job_id) VALUES (?, ?)",
        ("imp-1", "job-2"),
    )
    conn.commit()

    calls = []
    monkeypatch.setattr(
        downloads, "rebuild_watched_playlist_m3u",
        lambda *args, **kwargs: calls.append((args, kwargs)) or None,
    )

    result = downloads._mark_watched_track_downloaded("job-2", resolved_path=None, skip_mismatch=True)

    assert result is True
    assert len(calls) == 1
    args, _ = calls[0]
    assert args[0] == "pl-2"
    assert args[1] == "Pop Mix"


# ---------------------------------------------------------------------------
# Fast: shape / creation tests
# ---------------------------------------------------------------------------

def test_queue_single_download_returns_job_id(api, base_url):
    job_id, _ = _queue_download(api, base_url)
    assert job_id
    _cleanup_downloaded_file(api, base_url, job_id)


def test_queue_single_download_job_is_retrievable(api, base_url):
    job_id, _ = _queue_download(api, base_url)
    r = api.get(f"{base_url}/api/jobs/{job_id}", timeout=10)
    assert r.status_code == 200
    assert r.json()["id"] == job_id
    _cleanup_downloaded_file(api, base_url, job_id)


def test_queue_single_download_initial_status(api, base_url):
    job_id, _ = _queue_download(api, base_url)
    status = api.get(f"{base_url}/api/jobs/{job_id}", timeout=10).json().get("status")
    assert status in (*_TERMINAL, "queued", "downloading"), \
        f"Unexpected initial status: {status}"
    _cleanup_downloaded_file(api, base_url, job_id)


def test_queue_playlist_routed_download_accepted(api, base_url):
    """use_playlists_dir + playlist_name on a single-type download is a valid request.

    playlist_name is not persisted to the DB for single downloads (only passed in-memory
    to the worker), so we just verify the request is accepted and the job is created.
    """
    with _temp_settings(api, base_url, playlists_subdir=_TEST_PLAYLISTS_SUBDIR):
        job_id, _ = _queue_download(api, base_url, playlist_name="Test Playlist", use_playlists_dir=True)
        assert api.get(f"{base_url}/api/jobs/{job_id}", timeout=10).json()["id"] == job_id
        _cleanup_downloaded_file(api, base_url, job_id)


def test_download_missing_video_id_rejected(api, base_url):
    r = api.post(f"{base_url}/api/download", json={
        "title": "Test", "artist": "Test", "source": "youtube", "convert_to_flac": True
    }, timeout=10)
    assert r.status_code in (400, 422), f"Expected 400/422, got {r.status_code}"


def test_download_job_shape(api, base_url):
    """Every expected field should be present on a freshly queued job."""
    expected_keys = [
        "id", "video_id", "title", "artist", "status",
        "error", "download_type", "source", "convert_audio", "convert_to_flac",
    ]
    job_id, _ = _queue_download(api, base_url)
    job = api.get(f"{base_url}/api/jobs/{job_id}", timeout=10).json()
    for key in expected_keys:
        assert key in job, f"Job response missing key: {key}"
    _cleanup_downloaded_file(api, base_url, job_id)


# ---------------------------------------------------------------------------
# Slow: setting combination tests
#
# Each test verifies one combination of settings and checks the outcome via
# the job's `override_dir` field (set by the album-routing code when a file
# moves) or just completion status.
#
# Notation used in test names:
#   single  = download_type default, lands in Singles/
#   pl      = use_playlists_dir=True, lands in Playlists/Name/
#   album   = auto_album_singles=True (singles and playlist tracks go to Albums/)
#   trackno = include_track_number_in_filename=True
#
# The tests run as whoever's credentials are in .test-credentials, and inherit
# that account's settings, so playlist tests pin "group as one compilation"
# rather than finding out the hard way what the user chose.
#
# Tests using a short clip: MB won't match (<30s, fingerprint skipped), so
#   album routing never fires. Used to verify routing is OFF.
# Tests using _MATCH_YT_ID: MB should match and return album info.
#   About a minute each. Skipped gracefully if MB doesn't match.
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_single_baseline_completes(api, base_url):
    """Single download with all routing settings off -- the happy path."""
    with _temp_settings(api, base_url,
                        auto_album_singles=False,
                        include_track_number_in_filename=False):
        job_id, _ = _queue_download(api, base_url, convert_to_flac=False)
        job = _wait_for_job(api, base_url, job_id)
    _assert_completed(job)
    assert job.get("override_dir") is None, \
        f"Expected no routing with all settings off, got override_dir={job['override_dir']}"
    _cleanup_downloaded_file(api, base_url, job_id)


@pytest.mark.slow
def test_single_album_routing_off_no_override_dir(api, base_url):
    """override_dir stays None when auto_album_singles is explicitly off."""
    with _temp_settings(api, base_url, auto_album_singles=False):
        job_id, _ = _queue_download(api, base_url)
        job = _wait_for_job(api, base_url, job_id)
    _assert_completed(job)
    assert job.get("override_dir") is None, \
        f"override_dir should be None with routing off, got: {job['override_dir']}"
    _cleanup_downloaded_file(api, base_url, job_id)


def _albums_subdir(api, base_url):
    settings = api.get(f"{base_url}/api/settings", timeout=10).json()
    return settings.get("albums_subdir") or "Albums"


def _queue_matchable(api, base_url, **kwargs):
    """Download MATCHABLE_SONG and wait. The file is deleted before returning,
    so no assert or skip afterwards can leave it behind."""
    job_id, _ = _queue_download(api, base_url, video_id=_MATCH_YT_ID,
                                title=_MATCH_TITLE, artist=_MATCH_ARTIST, **kwargs)
    job = _wait_for_job(api, base_url, job_id, timeout=300)
    _cleanup_downloaded_file(api, base_url, job_id)
    return job


@pytest.mark.slow
def test_single_album_routing_on_lands_in_albums(api, base_url):
    """With album routing on, a track MB recognises lands in Albums/Artist/Album/."""
    with _temp_settings(api, base_url, auto_album_singles=True):
        job = _queue_matchable(api, base_url)

    _assert_completed(job)
    _mb_skip_if_no_override(job, _MATCH_ARTIST, _MATCH_TITLE)

    od = pathlib.Path(job["override_dir"])
    assert _albums_subdir(api, base_url) in od.parts, \
        f"Expected override_dir inside the Albums dir, got: {od}"
    assert od.parts[-2:] == ("Ramones", "Ramones"), f"expected Albums/Ramones/Ramones, got: {od}"


@pytest.mark.slow
def test_single_track_number_completes(api, base_url):
    """Track number in filename enabled -- single download should still complete."""
    with _temp_settings(api, base_url, include_track_number_in_filename=True):
        job_id, _ = _queue_download(api, base_url, convert_to_flac=False)
        job = _wait_for_job(api, base_url, job_id)
    _assert_completed(job)
    _cleanup_downloaded_file(api, base_url, job_id)


@pytest.mark.slow
def test_playlist_routed_single_album_routing_off(api, base_url):
    """Single track routed to Playlists/ with album routing off -- override_dir stays None.

    Requires playlists_subdir to be non-empty; test sets it temporarily if not.
    """
    with _temp_settings(api, base_url,
                        playlists_subdir=_TEST_PLAYLISTS_SUBDIR,
                        auto_album_singles=False):
        job_id, _ = _queue_download(api, base_url,
                                    playlist_name="Combo Test",
                                    use_playlists_dir=True,
                                    convert_to_flac=False)
        job = _wait_for_job(api, base_url, job_id)

    _assert_completed(job)
    assert job.get("override_dir") is None, \
        f"override_dir should be None with album routing off, got: {job['override_dir']}"
    _cleanup_downloaded_file(api, base_url, job_id)


@pytest.mark.slow
def test_playlist_routed_single_album_routing_on(api, base_url):
    """A playlist track with album routing on goes to Albums/Artist/Album/, not
    the playlist folder: the M3U is what keeps it in the playlist (unit tests
    cover the entry it writes)."""
    with _temp_settings(api, base_url,
                        playlists_subdir=_TEST_PLAYLISTS_SUBDIR,
                        auto_album_singles=True,
                        playlist_album_as_name=False):
        job = _queue_matchable(api, base_url, playlist_name="Combo Test", use_playlists_dir=True)

    _assert_completed(job)
    _mb_skip_if_no_override(job, _MATCH_ARTIST, _MATCH_TITLE)

    od = pathlib.Path(job["override_dir"])
    assert _albums_subdir(api, base_url) in od.parts, \
        f"Album-routed playlist track should be in Albums/, got: {od}"
    assert "Combo Test" not in od.parts, f"should have left the playlist folder: {od}"


@pytest.mark.slow
def test_playlist_routed_compilation_stays_in_the_playlist_folder(api, base_url):
    """Compilation mode wins over album routing: the playlist stays together."""
    with _temp_settings(api, base_url,
                        playlists_subdir=_TEST_PLAYLISTS_SUBDIR,
                        auto_album_singles=True,
                        playlist_album_as_name=True):
        job = _queue_matchable(api, base_url, playlist_name="Combo Test", use_playlists_dir=True)

    _assert_completed(job)
    assert job.get("override_dir") is None, \
        f"compilation playlist tracks must not be album-routed, got: {job['override_dir']}"
    assert "/Combo Test/" in (job.get("final_path") or ""), \
        f"expected the track in the playlist folder, got: {job.get('final_path')}"


@pytest.mark.slow
def test_playlist_routed_single_track_number_completes(api, base_url):
    """Track number in filename + playlist routing -- should complete without error."""
    with _temp_settings(api, base_url,
                        playlists_subdir=_TEST_PLAYLISTS_SUBDIR,
                        include_track_number_in_filename=True,
                        auto_album_singles=False):
        job_id, _ = _queue_download(api, base_url,
                                    playlist_name="Combo Test",
                                    use_playlists_dir=True,
                                    convert_to_flac=False)
        job = _wait_for_job(api, base_url, job_id)
    _assert_completed(job)
    _cleanup_downloaded_file(api, base_url, job_id)


@pytest.mark.slow
def test_playlist_routed_single_all_settings_on(api, base_url):
    """Album routing + track numbers + playlist folder all on at once: still Albums,
    and the track-number naming must not trip up the pipeline."""
    with _temp_settings(api, base_url,
                        playlists_subdir=_TEST_PLAYLISTS_SUBDIR,
                        auto_album_singles=True,
                        playlist_album_as_name=False,
                        include_track_number_in_filename=True):
        job = _queue_matchable(api, base_url, playlist_name="Combo Test", use_playlists_dir=True)

    _assert_completed(job)
    _mb_skip_if_no_override(job, _MATCH_ARTIST, _MATCH_TITLE)

    od = pathlib.Path(job["override_dir"])
    assert _albums_subdir(api, base_url) in od.parts, \
        f"override_dir should be in Albums/ with all settings on, got: {od}"


# Two jobs downloading the same track used to share one output directory and
# delete each other's .part/.webp files mid-flight. Each job now gets its own.

def test_staging_dirs_are_unique_per_job(tmp_path, monkeypatch):
    downloads = _import_downloads_or_skip()
    monkeypatch.setattr(downloads, "get_setting", lambda k, d=None, **kw: str(tmp_path))

    a = downloads._make_staging_dir("job-a")
    b = downloads._make_staging_dir("job-b")

    assert a != b
    assert a.is_dir() and b.is_dir()
    assert a.parent == b.parent == tmp_path / ".mg_staging"

    # The collision that started all this: same filename, different sandboxes.
    (a / "Me at the zoo.webp").write_text("a")
    (b / "Me at the zoo.webp").write_text("b")
    assert (a / "Me at the zoo.webp").read_text() == "a"
    assert (b / "Me at the zoo.webp").read_text() == "b"


def test_clear_staging_dir_removes_parent_only_when_last_one_goes(tmp_path, monkeypatch):
    downloads = _import_downloads_or_skip()
    monkeypatch.setattr(downloads, "get_setting", lambda k, d=None, **kw: str(tmp_path))

    a = downloads._make_staging_dir("job-a")
    b = downloads._make_staging_dir("job-b")
    root = a.parent

    downloads._clear_staging_dir(a)
    assert not a.exists()
    assert root.is_dir()      # job-b is still working in there

    downloads._clear_staging_dir(b)
    assert not root.exists()  # last one out turns the lights off


def test_clear_staging_dir_tolerates_none_and_missing(tmp_path, monkeypatch):
    downloads = _import_downloads_or_skip()
    monkeypatch.setattr(downloads, "get_setting", lambda k, d=None, **kw: str(tmp_path))
    downloads._clear_staging_dir(None)
    downloads._clear_staging_dir(tmp_path / ".mg_staging" / "never-existed")


def test_promote_from_staging_moves_the_finished_file(tmp_path, monkeypatch):
    downloads = _import_downloads_or_skip()
    monkeypatch.setattr(downloads, "get_setting", lambda k, d=None, **kw: str(tmp_path))

    staging = downloads._make_staging_dir("job-a")
    src = staging / "Track.flac"
    src.write_bytes(b"audio")
    dest = tmp_path / "Singles" / "Artist"

    final = downloads._promote_from_staging(src, dest)

    assert final == dest / "Track.flac"
    assert final.read_bytes() == b"audio"
    assert not src.exists()


def test_sweep_staging_dirs_clears_abandoned_sandboxes(tmp_path, monkeypatch):
    downloads = _import_downloads_or_skip()
    monkeypatch.setattr(downloads, "get_setting", lambda k, d=None, **kw: str(tmp_path))

    downloads._make_staging_dir("crashed-1")
    downloads._make_staging_dir("crashed-2")

    assert downloads.sweep_staging_dirs() == 2
    assert not (tmp_path / ".mg_staging").exists()
    assert downloads.sweep_staging_dirs() == 0  # nothing left, and no tantrum


def test_staging_dir_name_is_excluded_from_library_scans():
    """A hidden working folder inside the music root must not show up as library."""
    from constants import EXCLUDED_SCAN_DIR_NAMES, STAGING_DIR_NAME
    assert STAGING_DIR_NAME in EXCLUDED_SCAN_DIR_NAMES


def _tagged_flac(path, **tags):
    """A real, if brief, FLAC. Mocked tag readers cannot catch a clobbering bug."""
    import subprocess
    from mutagen.flac import FLAC
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=0.1", str(path)],
        check=True,
    )
    audio = FLAC(str(path))
    for key, value in tags.items():
        audio[key] = value
    audio.save()
    return path


def test_auto_route_single_does_not_overwrite_the_files_own_track_number(tmp_path, monkeypatch):
    """The 2026-08-09 bug: a correct "2 of 14" came out the other side as "15 of 16".

    The main tagging pass already respects existing tags; routing into the album
    folder then retagged straight from MusicBrainz and undid it.
    """
    downloads = _import_downloads_or_skip()
    from mutagen.flac import FLAC

    monkeypatch.setattr(downloads, "_update_job", lambda *a, **k: None)
    monkeypatch.setattr(downloads, "set_file_permissions", lambda *a, **k: None)
    monkeypatch.setattr(downloads, "get_singles_dir", lambda user_id=None: tmp_path / "Singles")
    monkeypatch.setattr(downloads, "get_albums_dir", lambda user_id=None: tmp_path / "Albums")
    monkeypatch.setattr(
        downloads, "get_setting_bool",
        lambda key, default=False, user_id=None: {
            "auto_album_singles": True,
            "organise_by_artist": False,
        }.get(key, default),
    )

    source_dir = tmp_path / "Singles" / "Paramore"
    source_dir.mkdir(parents=True)
    audio_file = _tagged_flac(
        source_dir / "Ignorance.flac",
        ARTIST="Paramore", TITLE="Ignorance",
        ALBUM="Brand New Eyes (Deluxe Edition)",
        TRACKNUMBER="2", TRACKTOTAL="14",
    )

    routed, _ = downloads._auto_route_to_album(
        audio_file, "Paramore", "Ignorance",
        # MusicBrainz, having found the track on a radio promo compilation
        {"album": "Brand New Eyes", "album_artist": "Paramore",
         "track_number": 15, "track_total": 16},
        "job-id", None,
    )

    assert routed.exists()
    tags = FLAC(str(routed))
    assert tags["TRACKNUMBER"] == ["2"], "the file's own track number must survive routing"
    assert tags.get("TRACKTOTAL") == ["14"]


def test_auto_route_single_still_fills_in_a_missing_track_number(tmp_path, monkeypatch):
    """Respecting existing tags must not mean refusing to tag an untagged file."""
    downloads = _import_downloads_or_skip()
    from mutagen.flac import FLAC

    monkeypatch.setattr(downloads, "_update_job", lambda *a, **k: None)
    monkeypatch.setattr(downloads, "set_file_permissions", lambda *a, **k: None)
    monkeypatch.setattr(downloads, "get_singles_dir", lambda user_id=None: tmp_path / "Singles")
    monkeypatch.setattr(downloads, "get_albums_dir", lambda user_id=None: tmp_path / "Albums")
    monkeypatch.setattr(
        downloads, "get_setting_bool",
        lambda key, default=False, user_id=None: {
            "auto_album_singles": True,
            "organise_by_artist": False,
        }.get(key, default),
    )

    source_dir = tmp_path / "Singles" / "Paramore"
    source_dir.mkdir(parents=True)
    audio_file = _tagged_flac(source_dir / "Ignorance.flac", ARTIST="Paramore", TITLE="Ignorance")

    routed, _ = downloads._auto_route_to_album(
        audio_file, "Paramore", "Ignorance",
        {"album": "brand new eyes", "album_artist": "Paramore",
         "track_number": 2, "track_total": 11},
        "job-id", None,
    )

    assert FLAC(str(routed))["TRACKNUMBER"] == ["2"]


# ---------------------------------------------------------------------------
# GitLab issue #90: YouTube downloads ran for six months with no cookies.
#
# The gate was written as "not is_soundcloud" and meant it. v2.0.0 rewrote it
# as "not is_url_source"; v2.3.0 then began synthesising a source_url for every
# YouTube job, so the gate closed on YouTube itself. These tests hold the
# original intent in place: cookies for YouTube, never for anyone else.
# ---------------------------------------------------------------------------


def _cookie_gate(source_url):
    """The real decision from downloads.py, not a copy of it that could agree
    with a broken implementation."""
    from downloads import youtube_cookies_apply
    return youtube_cookies_apply(source_url)


def test_youtube_downloads_send_cookies_even_with_a_source_url():
    # The exact shape app.py synthesises "for tracking", which caused all this.
    assert _cookie_gate("https://www.youtube.com/watch?v=nfWlot6h_JM") is True
    assert _cookie_gate("https://music.youtube.com/watch?v=nfWlot6h_JM") is True
    # A bare YouTube job with no source_url at all still gets them, as before.
    assert _cookie_gate(None) is True
    assert _cookie_gate("") is True


def test_other_url_backed_sources_still_get_no_youtube_cookies():
    """Handing YouTube cookies to SoundCloud was never the intention."""
    for url in (
        "https://soundcloud.com/artist/track",
        "https://meln.top/track/123",
        "monochrome://12345",
        "soulseek://someone/some/path.flac",
    ):
        assert _cookie_gate(url) is False, url


def test_download_command_carries_cookies_for_a_youtube_source_url(tmp_path, monkeypatch):
    """End to end on the command builder, which is what actually runs."""
    import downloads
    import youtube

    monkeypatch.setattr(downloads, "get_setting", lambda key, default="", **kw: default)

    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t99\tA\tb\n")
    monkeypatch.setattr(youtube, "COOKIES_FILE", cookies)
    monkeypatch.setattr(youtube, "get_user_cookies_file", lambda user_id: cookies)
    monkeypatch.setattr(youtube, "_cookies_allowed", lambda: True)

    url = "https://www.youtube.com/watch?v=nfWlot6h_JM"
    cmd = downloads._build_ytdlp_download_cmd(
        "nfWlot6h_JM", str(tmp_path / "o.%(ext)s"), True,
        source_url=url, use_cookies=_cookie_gate(url),
    )
    assert "--cookies" in cmd
    assert str(cookies) in cmd

    soundcloud = "https://soundcloud.com/artist/track"
    cmd = downloads._build_ytdlp_download_cmd(
        "x", str(tmp_path / "o.%(ext)s"), True,
        source_url=soundcloud, use_cookies=_cookie_gate(soundcloud),
    )
    assert "--cookies" not in cmd


def test_per_user_cookie_file_is_actually_reached(tmp_path, monkeypatch):
    """user_id was in scope at every call site and passed at none of them."""
    import downloads
    import youtube

    monkeypatch.setattr(downloads, "get_setting", lambda key, default="", **kw: default)

    mine = tmp_path / "user-alice.txt"
    mine.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t99\tA\tb\n")
    everyones = tmp_path / "global.txt"
    everyones.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t99\tZ\tz\n")

    monkeypatch.setattr(youtube, "COOKIES_FILE", everyones)
    monkeypatch.setattr(youtube, "_cookies_allowed", lambda: True)
    monkeypatch.setattr(
        youtube, "get_user_cookies_file",
        lambda user_id: mine if user_id == "alice" else tmp_path / "nope.txt",
    )

    cmd = downloads._build_ytdlp_download_cmd(
        "abc", str(tmp_path / "o.%(ext)s"), True,
        source_url="https://www.youtube.com/watch?v=abc",
        use_cookies=True, user_id="alice",
    )
    assert str(mine) in cmd, "alice's own cookies should win"

    cmd = downloads._build_ytdlp_download_cmd(
        "abc", str(tmp_path / "o.%(ext)s"), True,
        source_url="https://www.youtube.com/watch?v=abc",
        use_cookies=True, user_id="bob",
    )
    assert str(everyones) in cmd, "bob has none of his own, so the global file"


# ---------------------------------------------------------------------------
# "Convert lossless sources only" (GitLab #92): a lossy source with a lossy
# target should be left alone rather than hopped into a different lossy codec.
# ---------------------------------------------------------------------------

def _settings_lookup(monkeypatch, downloads, values):
    """Point downloads.get_setting/get_setting_bool at a plain dict, default-aware."""
    monkeypatch.setattr(downloads, "get_setting", lambda key, default="", **kw: values.get(key, default))
    monkeypatch.setattr(downloads, "get_setting_bool", lambda key, default=False, **kw: values.get(key, default))


def test_target_format_is_lossy_classifies_each_format(monkeypatch):
    downloads = _import_downloads_or_skip()
    _settings_lookup(monkeypatch, downloads, {"alac_bitrate": "lossless"})

    assert downloads._target_format_is_lossy("mp3") is True
    assert downloads._target_format_is_lossy("opus") is True
    assert downloads._target_format_is_lossy("flac") is False
    assert downloads._target_format_is_lossy("alac") is False  # true ALAC is lossless


def test_target_format_is_lossy_for_aac_in_m4a_alac(monkeypatch):
    downloads = _import_downloads_or_skip()
    _settings_lookup(monkeypatch, downloads, {"alac_bitrate": "256k"})
    assert downloads._target_format_is_lossy("alac") is True


def test_skip_lossy_to_lossy_only_when_setting_target_and_source_all_agree(monkeypatch):
    downloads = _import_downloads_or_skip()

    # Setting off: never declines, whatever the codecs are.
    _settings_lookup(monkeypatch, downloads, {"convert_lossless_only": False, "alac_bitrate": "lossless"})
    assert downloads._skip_lossy_to_lossy("MP3", "opus") is False

    # Setting on, but the target is lossless: nothing for it to skip.
    _settings_lookup(monkeypatch, downloads, {"convert_lossless_only": True, "alac_bitrate": "lossless"})
    assert downloads._skip_lossy_to_lossy("MP3", "flac") is False

    # Setting on, lossy target, lossless source: still a normal conversion.
    assert downloads._skip_lossy_to_lossy("FLAC", "opus") is False
    assert downloads._skip_lossy_to_lossy("ALAC", "mp3") is False

    # Setting on, lossy target, lossy source: exactly the hop it exists to skip.
    assert downloads._skip_lossy_to_lossy("MP3", "opus") is True
    assert downloads._skip_lossy_to_lossy("AAC", "mp3") is True

    # Unknown source codec: don't guess either way.
    assert downloads._skip_lossy_to_lossy(None, "opus") is False


def test_ytdlp_cmd_skips_audio_format_for_a_lossy_source_when_setting_on(monkeypatch):
    downloads = _import_downloads_or_skip()
    _settings_lookup(monkeypatch, downloads, {
        "audio_format": "opus", "convert_lossless_only": True, "alac_bitrate": "lossless",
    })

    cmd = downloads._build_ytdlp_download_cmd(
        "abc", "/tmp/o.%(ext)s", True, use_cookies=False, source_codec="MP3",
    )
    assert "--audio-format" not in cmd
    assert "-x" in cmd  # still extracts the audio track, just keeps its codec


def test_ytdlp_cmd_still_converts_a_lossless_source_with_the_setting_on(monkeypatch):
    downloads = _import_downloads_or_skip()
    _settings_lookup(monkeypatch, downloads, {
        "audio_format": "opus", "convert_lossless_only": True, "alac_bitrate": "lossless",
    })

    cmd = downloads._build_ytdlp_download_cmd(
        "abc", "/tmp/o.%(ext)s", True, use_cookies=False, source_codec="FLAC",
    )
    assert "--audio-format" in cmd
    assert "opus" in cmd


def test_ytdlp_cmd_converts_a_lossy_source_when_the_setting_is_off(monkeypatch):
    downloads = _import_downloads_or_skip()
    _settings_lookup(monkeypatch, downloads, {
        "audio_format": "opus", "convert_lossless_only": False, "alac_bitrate": "lossless",
    })

    cmd = downloads._build_ytdlp_download_cmd(
        "abc", "/tmp/o.%(ext)s", True, use_cookies=False, source_codec="MP3",
    )
    assert "--audio-format" in cmd


def test_enforce_target_format_declines_a_lossy_to_lossy_hop(tmp_path, monkeypatch):
    downloads = _import_downloads_or_skip()
    _settings_lookup(monkeypatch, downloads, {
        "audio_format": "opus", "convert_lossless_only": True, "alac_bitrate": "lossless",
    })
    monkeypatch.setattr(downloads, "probe_audio_quality", lambda path, **kw: ("MP3 128kbps", 128))

    def _no_conversion_allowed(*a, **kw):
        raise AssertionError("ffmpeg should never be invoked for a declined conversion")
    monkeypatch.setattr(downloads.subprocess, "run", _no_conversion_allowed)

    audio_file = tmp_path / "track.mp3"
    audio_file.write_bytes(b"not really mp3, just needs to exist")

    result = downloads._enforce_target_format(audio_file, True)
    assert result == audio_file
    assert audio_file.exists()


def test_enforce_target_format_still_converts_a_lossless_source(tmp_path, monkeypatch):
    downloads = _import_downloads_or_skip()
    _settings_lookup(monkeypatch, downloads, {
        "audio_format": "mp3", "convert_lossless_only": True, "mp3_bitrate": "v2",
    })
    monkeypatch.setattr(downloads, "probe_audio_quality", lambda path, **kw: ("FLAC", 0))

    def _fake_run(cmd, **kw):
        pathlib.Path(cmd[-1]).write_bytes(b"converted")
        return type("R", (), {"returncode": 0, "stderr": ""})()
    monkeypatch.setattr(downloads.subprocess, "run", _fake_run)

    audio_file = tmp_path / "track.flac"
    audio_file.write_bytes(b"fake flac bytes")

    result = downloads._enforce_target_format(audio_file, True)
    assert result.suffix == ".mp3"


# ---------------------------------------------------------------------------
# Weekly playlists carrying tracks across editions (issue #97)
#
# A ListenBrainz "Created for You" playlist renames itself every Monday, so its
# folder and M3U rotate with it. A track that survives into the new week still
# lives in the previous week's folder, which is inside the Playlists tree but
# not inside *this* week's folder. It used to fall through to the branch meant
# for Navidrome library paths and get stamped in as an absolute path, which
# plays here and nowhere else.
# ---------------------------------------------------------------------------

def _weekly_playlist_db(monkeypatch, downloads, playlists_dir, sync_mode="mirror"):
    import sqlite3
    from contextlib import contextmanager

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE jobs (id TEXT PRIMARY KEY, artist TEXT, title TEXT);
        CREATE TABLE watched_playlists (
            id TEXT PRIMARY KEY, name TEXT, user_id TEXT,
            stale_navidrome_paths INTEGER DEFAULT 0);
        CREATE TABLE watched_playlist_tracks (
            playlist_id TEXT, track_hash TEXT, artist TEXT, title TEXT,
            downloaded_at TIMESTAMP, resolved_path TEXT, job_id TEXT,
            removed_at TIMESTAMP, position INTEGER,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (playlist_id, track_hash));
    """)

    @contextmanager
    def fake_db_conn():
        yield conn

    monkeypatch.setattr(downloads, "db_conn", fake_db_conn)
    monkeypatch.setattr(downloads, "get_playlists_dir", lambda user_id=None: playlists_dir)
    monkeypatch.setattr(downloads, "_tag_track_comment", lambda *a, **k: None)
    monkeypatch.setattr(downloads, "set_file_permissions", lambda *a, **k: None)
    return conn


def test_weekly_carry_over_track_stays_relative_to_the_playlists_folder(tmp_path, monkeypatch):
    downloads = _import_downloads_or_skip()

    playlists_dir = tmp_path / "Playlists"
    playlists_dir.mkdir()
    conn = _weekly_playlist_db(monkeypatch, downloads, playlists_dir)
    real_get_setting = downloads.get_setting
    monkeypatch.setattr(downloads, "get_setting", lambda key, default=None, user_id=None:
                        str(tmp_path) if key == "music_dir" else real_get_setting(key, default, user_id=user_id))

    last_week = "Weekly Jams for kaisernerox, week of 2026-09-14"
    this_week = "Weekly Jams for kaisernerox, week of 2026-09-21"

    def seed(artist, title, folder, position):
        folder_path = playlists_dir / folder
        folder_path.mkdir(parents=True, exist_ok=True)
        audio = folder_path / f"{artist} - {title}.mp3"
        audio.write_bytes(b"not really audio")
        conn.execute(
            """INSERT INTO watched_playlist_tracks
               (playlist_id, track_hash, artist, title, downloaded_at, resolved_path, position)
               VALUES (?, ?, ?, ?, datetime('now'), ?, ?)""",
            ("pl-1", f"{artist}|{title}", artist, title, str(audio), position),
        )

    conn.execute(
        "INSERT INTO watched_playlists (id, name) VALUES (?, ?)", ("pl-1", this_week))
    seed("Floating Points", "Last Bloom", last_week, 0)   # survived the rollover
    seed("Yussef Dayes", "Rust", this_week, 1)            # new this week
    conn.commit()

    m3u = downloads.rebuild_watched_playlist_m3u(
        "pl-1", this_week, use_playlists_dir=True, sync_mode="mirror", user_id=None,
    )

    entries = [l for l in m3u.read_text().splitlines() if not l.startswith("#")]
    assert entries == [
        f"{last_week}/Floating Points - Last Bloom.mp3",
        f"{this_week}/Yussef Dayes - Rust.mp3",
    ], "last week's track must stay relative to Playlists/, not go absolute"
    assert not any(e.startswith("/") for e in entries)


# ---------------------------------------------------------------------------
# Bulk import M3U: a failed track must not hold the playlist hostage
# ---------------------------------------------------------------------------

@pytest.fixture
def bulk_playlist_db(monkeypatch, tmp_path):
    """A real schema in a temp DB, plus a downloads module whose library
    lookups are stubbed: 'Good' is on disk, everything else is not."""
    import queue
    import threading
    downloads = _import_downloads_or_skip()
    import constants
    import db

    monkeypatch.setenv("MUSIC_DIR", str(tmp_path / "music"))
    monkeypatch.setattr(constants, "DB_PATH", tmp_path / "bulk-m3u.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bulk-m3u.db")
    monkeypatch.setattr(db, "_DB_POOL_SIZE", 4)
    monkeypatch.setattr(db, "_db_pool", queue.LifoQueue(maxsize=4))
    monkeypatch.setattr(db, "_db_pool_created", 0)
    monkeypatch.setattr(db, "_db_pool_lock", threading.Lock())
    db.init_db()

    monkeypatch.setattr(downloads, "get_singles_dir", lambda user_id=None: tmp_path)
    monkeypatch.setattr(
        downloads, "check_duplicate",
        lambda artist, title, user_id=None: tmp_path / f"{artist} - {title}.flac" if title == "Good" else None,
    )
    monkeypatch.setattr(downloads, "_m3u_entry", lambda audio_file, m3u_dir, user_id=None: audio_file.name)
    return downloads, db, tmp_path


def _add_bulk_job(db, job_id, title, status, import_id="imp-1"):
    with db.db_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id, artist, title, status, playlist_name) VALUES (?, ?, ?, ?, ?)",
            (job_id, "Band", title, status, import_id),
        )
        conn.commit()


def test_bulk_playlist_does_not_wait_out_the_hour_for_a_failed_track(bulk_playlist_db, monkeypatch):
    """The Reddit report: one failure kept the M3U away for PLAYLIST_WAIT_MAX,
    because the loop only stopped once every job was 'completed'."""
    downloads, db, tmp_path = bulk_playlist_db
    _add_bulk_job(db, "j1", "Good", "completed")
    _add_bulk_job(db, "j2", "Dud", "failed")

    def no_waiting(_seconds):
        raise AssertionError("waited for a playlist with nothing left in flight")
    monkeypatch.setattr(downloads.time, "sleep", no_waiting)

    downloads.create_bulk_playlist("imp-1", "Mix")

    assert (tmp_path / "Mix.m3u").read_text(encoding="utf-8") == "#EXTM3U\nBand - Good.flac\n"


def test_bulk_playlist_still_waits_for_tracks_in_flight(bulk_playlist_db, monkeypatch):
    """Not waiting at all would write the M3U before the last track lands."""
    downloads, db, tmp_path = bulk_playlist_db
    _add_bulk_job(db, "j1", "Good", "downloading")
    _add_bulk_job(db, "j2", "Dud", "failed")

    naps = []
    def finish_download(seconds):
        naps.append(seconds)
        with db.db_conn() as conn:
            conn.execute("UPDATE jobs SET status = 'completed' WHERE id = 'j1'")
            conn.commit()
    monkeypatch.setattr(downloads.time, "sleep", finish_download)

    downloads.create_bulk_playlist("imp-1", "Mix")

    assert len(naps) == 1
    assert (tmp_path / "Mix.m3u").read_text(encoding="utf-8") == "#EXTM3U\nBand - Good.flac\n"
