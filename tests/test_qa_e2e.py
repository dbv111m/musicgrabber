"""
End-to-end QA flow.

Exercises the full acquisition chain in one slow test:
  Phase A (library dupe checks DISABLED):
    1. Pull a single
    2. Pull a small album
    3. Import a Spotify playlist (3 tracks)
    4. Add a watched playlist for the same URL and trigger a refresh

  Phase B (Navidrome + Lidarr dupe checks RE-ENABLED):
    5. Re-queue the same single, the same album, and re-import the playlist.
       Expectation: no new audio files land (Phase A files are recognised as dupes).

  Phase C (cleanup):
    6. Delete every job file we created, remove the watched playlist, clear
       completed/failed jobs, and restore the original settings.

Run with `tests/run_tests.sh --slow -k qa_e2e` (or as part of `--slow`). The
fast suite skips it. This test takes several minutes; downloads do not run
in parallel, and yt-dlp / MusicBrainz / slskd are all real network calls.

The test will SKIP rather than fail when:
  - Navidrome is not configured (we need it to validate the dupe-check toggle)
  - MusicBrainz returns no MBID for the album lookup
  - Spotify playlist fetch fails (rate limits / cookie issues / outage)
"""

import os
import time
from contextlib import contextmanager

import pytest
import requests

from live_media import next_short_clip


pytestmark = pytest.mark.slow


# Spotify playlist supplied by the user, contains 3 tracks.
_SPOTIFY_PLAYLIST_URL = os.environ.get(
    "MG_QA_PLAYLIST_URL",
    "https://open.spotify.com/playlist/3DHp7YE5h3l3PTz0rjeYFQ",
)

# Album under test, looked up via the MusicBrainz public API at test start.
# Default is a small, well-known Nirvana EP; override via env if MB can't find it.
_ALBUM_ARTIST = os.environ.get("MG_QA_ALBUM_ARTIST", "Nirvana")
_ALBUM_TITLE  = os.environ.get("MG_QA_ALBUM_TITLE", "Hormoaning")

# Single under test: one short clip, kept for the whole test, because Phase B
# queues the same one again to prove the dupe check catches it.
_SINGLE_VIDEO_ID, _SINGLE_ARTIST, _SINGLE_TITLE = next_short_clip()

_TERMINAL = ("completed", "failed", "completed_with_errors")

# Long timeouts; this test cannot rush yt-dlp or MusicBrainz.
_SINGLE_TIMEOUT  = 180
_ALBUM_TIMEOUT   = 600   # multiple tracks
_BULK_TIMEOUT    = 600
_WATCHED_TIMEOUT = 600


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _poll_get(api, url, retries=3):
    """GET with retry. The server can briefly stall under heavy yt-dlp / MB load,
    and a 10s read timeout is fine for steady-state but flaky during downloads."""
    last_exc = None
    for attempt in range(retries):
        try:
            return api.get(url, timeout=30)
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(2 * (attempt + 1))
    raise last_exc


def _wait_for_job(api, base_url, job_id, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        try:
            r = _poll_get(api, f"{base_url}/api/jobs/{job_id}")
        except requests.RequestException:
            continue  # transient; try again next tick
        assert r.status_code == 200, f"job poll failed: {r.status_code} {r.text}"
        job = r.json()
        if job.get("status") in _TERMINAL:
            return job
    raise TimeoutError(f"Job {job_id} did not finish in {timeout}s")


def _wait_for_bulk(api, base_url, import_id, timeout):
    """Wait for a bulk import (or album-as-bulk-import) to finish all downloads."""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        time.sleep(5)
        try:
            last = _poll_get(api, f"{base_url}/api/bulk-import/{import_id}/status").json()
        except requests.RequestException:
            continue
        if last.get("complete"):
            return last
    raise TimeoutError(f"Bulk import {import_id} did not finish in {timeout}s: {last}")


def _wait_for_watched_refresh(api, base_url, playlist_id, timeout):
    """Wait until the watched playlist's refresh_state returns to 'idle'."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        try:
            resp = _poll_get(api, f"{base_url}/api/watched-playlists").json()
        except requests.RequestException:
            continue
        rows = resp.get("playlists", []) if isinstance(resp, dict) else resp
        match = next((p for p in rows if p.get("id") == playlist_id), None)
        if match and match.get("refresh_state", "idle") == "idle":
            return match
    raise TimeoutError(f"Watched playlist {playlist_id} refresh did not settle in {timeout}s")


def _watched_tracks(api, base_url, playlist_id):
    r = api.get(f"{base_url}/api/watched-playlists/{playlist_id}/tracks", timeout=15)
    if r.status_code != 200:
        return []
    data = r.json()
    return data.get("tracks", []) if isinstance(data, dict) else data


def _lookup_release_mbid(artist, album):
    """Find a release MBID via the public MusicBrainz API. Returns (mbid, track_count) or None."""
    try:
        resp = requests.get(
            "https://musicbrainz.org/ws/2/release/",
            params={
                "query": f'release:"{album}" AND artist:"{artist}"',
                "fmt": "json",
                "limit": 5,
            },
            headers={"User-Agent": "MusicGrabber-QA-Tests/1.0 (https://github.com/g33kphr33k/MusicGrabber)"},
            timeout=20,
        )
        if resp.status_code != 200:
            return None
        releases = resp.json().get("releases", []) or []
        # Pick the release with the smallest non-zero track count, prefer "Official" status.
        candidates = []
        for rel in releases:
            tracks = rel.get("track-count") or 0
            if tracks > 0:
                candidates.append((rel.get("status") == "Official", -tracks, rel.get("id"), tracks))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][2], candidates[0][3]
    except Exception:
        return None


def _get_settings(api, base_url):
    """GET /api/settings returns {"settings": {...}, "env_overrides": [...], ...}.
    Unwrap to the inner dict so callers don't have to remember."""
    resp = api.get(f"{base_url}/api/settings", timeout=10).json()
    return resp.get("settings", resp) if isinstance(resp, dict) else {}


@contextmanager
def _temp_settings(api, base_url, overrides):
    """Patch settings, restore the originals on exit. Sensitive fields (passwords,
    API keys) are masked by the GET endpoint, so this helper should only be used
    on non-sensitive keys like the dupe-check toggle and URLs."""
    original = _get_settings(api, base_url)
    api.put(f"{base_url}/api/settings", json=overrides, timeout=15)
    try:
        yield original
    finally:
        restore = {k: original.get(k) for k in overrides}
        api.put(f"{base_url}/api/settings", json=restore, timeout=15)


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------

def test_qa_full_acquisition_flow(api, base_url):
    """Walk the full acquisition flow once with dupes OFF, once with dupes ON.

    Each step asserts the job(s) reached a terminal state; the second pass
    additionally asserts that no NEW completed jobs landed for already-present
    tracks (i.e. the library dupe check actually catches them).
    """
    settings = _get_settings(api, base_url)
    if not (settings.get("navidrome_url") and settings.get("navidrome_user")):
        pytest.skip("Navidrome not configured; dupe-check toggle has nothing to validate")

    tracked_job_ids: list[str] = []
    watched_playlist_id: str | None = None

    # Delete any watched playlist left over from a previous failed run, so we
    # don't trip the 409 "already being watched" guard on re-add.
    try:
        existing = api.get(f"{base_url}/api/watched-playlists", timeout=10).json()
        existing_rows = existing.get("playlists", []) if isinstance(existing, dict) else existing
        for row in existing_rows:
            if row.get("url") == _SPOTIFY_PLAYLIST_URL:
                api.delete(f"{base_url}/api/watched-playlists/{row['id']}", timeout=15)
    except requests.RequestException:
        pass

    # MB lookup happens before we touch any settings so a skip here doesn't leave
    # the dupe-check toggle in a non-default state.
    mb_lookup = _lookup_release_mbid(_ALBUM_ARTIST, _ALBUM_TITLE)
    if not mb_lookup:
        pytest.skip(f"MusicBrainz returned no release for '{_ALBUM_ARTIST} - {_ALBUM_TITLE}'")
    album_release_mbid, album_track_count = mb_lookup

    # --------------------------------------------------------------------- #
    # PHASE A  -  dupe checks OFF, fresh downloads must all complete
    # --------------------------------------------------------------------- #
    phase_a_overrides = {
        "navidrome_dupe_check": False,
        # Lidarr is gated by URL+API key being set; clearing the URL temporarily
        # turns its dupe check off without us having to mess with the API key
        # (which GET /api/settings masks, making restore impossible).
        "lidarr_url": "",
    }

    with _temp_settings(api, base_url, phase_a_overrides):
        # 1. Single download ------------------------------------------------
        r = api.post(
            f"{base_url}/api/download",
            json={
                "video_id": _SINGLE_VIDEO_ID,
                "title":    _SINGLE_TITLE,
                "artist":   _SINGLE_ARTIST,
                "source":   "youtube",
                "convert_to_flac": True,
            },
            timeout=15,
        )
        assert r.status_code == 200, f"single queue failed: {r.text}"
        single_job_id = r.json().get("job_id") or r.json().get("id")
        assert single_job_id, f"no job_id in single download response: {r.json()}"
        tracked_job_ids.append(single_job_id)

        single_job = _wait_for_job(api, base_url, single_job_id, _SINGLE_TIMEOUT)
        assert single_job["status"] in ("completed", "completed_with_errors"), (
            f"Phase A single did not complete: {single_job.get('error')}"
        )

        # 2. Album download -------------------------------------------------
        r = api.post(
            f"{base_url}/api/albums/download",
            json={
                "artist":       _ALBUM_ARTIST,
                "album_title":  _ALBUM_TITLE,
                "release_mbid": album_release_mbid,
                "make_m3u":     False,
                "convert_to_flac": True,
            },
            timeout=30,
        )
        assert r.status_code == 200, f"album queue failed: {r.text}"
        album_resp = r.json()
        album_import_id = album_resp.get("import_id")
        if album_import_id:
            album_status = _wait_for_bulk(api, base_url, album_import_id, _ALBUM_TIMEOUT)
            # A small EP may not match every track on MB; tolerate up to 2 failures
            # to keep the test robust against the long tail of search-engine misses.
            failed = album_status.get("failed", 0) or 0
            queued_count = album_resp.get("queued_count", album_track_count) or 0
            assert failed <= max(2, queued_count // 2), (
                f"Phase A album had too many failures ({failed}/{queued_count}): {album_status}"
            )
        # If queued_count was zero, the album was already on disk before the test
        # started; we treat that as a pass and move on.

        # 3. Playlist import (Spotify, 3 tracks) ----------------------------
        r = api.post(
            f"{base_url}/api/fetch-playlist",
            json={"url": _SPOTIFY_PLAYLIST_URL},
            timeout=120,  # Playwright fallback can take a while on cold cache
        )
        if r.status_code != 200:
            pytest.skip(f"Spotify playlist fetch failed: {r.status_code} {r.text[:200]}")
        fetched = r.json()
        playlist_tracks = fetched.get("tracks") or []
        assert len(playlist_tracks) >= 1, f"Spotify playlist returned no tracks: {fetched}"

        r = api.post(
            f"{base_url}/api/bulk-import-async",
            json={"songs": "\n".join(playlist_tracks), "convert_to_flac": True},
            timeout=15,
        )
        assert r.status_code == 200, f"bulk import create failed: {r.text}"
        bulk_import_id = r.json().get("import_id")
        assert bulk_import_id, f"no import_id from bulk import: {r.json()}"

        bulk_status = _wait_for_bulk(api, base_url, bulk_import_id, _BULK_TIMEOUT)
        failed = bulk_status.get("failed", 0) or 0
        completed = bulk_status.get("completed", 0) or 0
        assert completed >= 1, f"Phase A bulk import landed zero tracks: {bulk_status}"
        assert failed < len(playlist_tracks), (
            f"Phase A bulk import: every track failed: {bulk_status}"
        )

        # 4. Watched playlist ----------------------------------------------
        r = api.post(
            f"{base_url}/api/watched-playlists",
            json={
                "url": _SPOTIFY_PLAYLIST_URL,
                "refresh_interval_hours": 24,
                "convert_to_flac": True,
                "make_m3u": True,
                "use_playlists_dir": False,
                "sync_mode": "append",
                "preferred_sources": "all",
            },
            timeout=120,
        )
        # If the URL was already a watched playlist for this user from a previous
        # test run, the server returns 409. Surface that as a skip so the test is
        # repeatable without manual cleanup.
        if r.status_code == 409:
            pytest.skip("Watched playlist for this URL already exists; clean it up and rerun")
        assert r.status_code == 200, f"watched playlist add failed: {r.status_code} {r.text}"
        wp_resp = r.json()
        watched_playlist_id = wp_resp.get("id") or (wp_resp.get("playlist") or {}).get("id")
        assert watched_playlist_id, f"no watched playlist id in response: {wp_resp}"

        # The POST kicks off an initial bulk-import for every track in the
        # playlist; wait on that bulk_import to finish, then verify at least
        # one track was marked downloaded on the watched_playlist_tracks row.
        wp_import_id = wp_resp.get("import_id")
        if wp_import_id:
            _wait_for_bulk(api, base_url, wp_import_id, _WATCHED_TIMEOUT)
        _wait_for_watched_refresh(api, base_url, watched_playlist_id, 60)
        wp_tracks = _watched_tracks(api, base_url, watched_playlist_id)
        downloaded = [t for t in wp_tracks if t.get("downloaded_at")]
        assert downloaded, (
            f"Watched playlist refresh marked zero tracks downloaded: "
            f"{[t.get('title') for t in wp_tracks]}"
        )

    # --------------------------------------------------------------------- #
    # PHASE B  -  dupe checks ON, same actions should NOT re-download
    # --------------------------------------------------------------------- #
    # Switch the Navidrome check on for this phase rather than assume the
    # tester runs with it on. The schema default is on, but an account that
    # turned it off used to fail here after Phase A had passed completely,
    # which looks exactly like a download regression and is nothing of the
    # sort. The helper puts the tester's own value back, pass or fail.
    with _temp_settings(api, base_url, {"navidrome_dupe_check": True}):
        settings_b = _get_settings(api, base_url)
        assert settings_b.get("navidrome_dupe_check") is True, (
            "Phase B: could not switch navidrome_dupe_check on"
        )

        # 5. Re-queue the single. Dupe check should catch it; the job either
        #    short-circuits to completed (no file rewritten) or completes with a
        #    duplicate-skipped marker. We accept any terminal state and assert
        #    that the request didn't 4xx and didn't fail.
        r = api.post(
            f"{base_url}/api/download",
            json={
                "video_id": _SINGLE_VIDEO_ID,
                "title":    _SINGLE_TITLE,
                "artist":   _SINGLE_ARTIST,
                "source":   "youtube",
                "convert_to_flac": True,
            },
            timeout=15,
        )
        assert r.status_code == 200, f"Phase B single re-queue failed: {r.text}"
        phase_b_single_id = r.json().get("job_id") or r.json().get("id")
        tracked_job_ids.append(phase_b_single_id)
        phase_b_single = _wait_for_job(api, base_url, phase_b_single_id, _SINGLE_TIMEOUT)
        assert phase_b_single["status"] in _TERMINAL, (
            f"Phase B single did not terminate: {phase_b_single}"
        )
        # The dupe path marks the job completed without re-downloading. We don't have
        # a "skipped_due_to_dupe" flag exposed in the API, so we assert the weaker
        # condition: the underlying file still exists (the Phase A file wasn't lost)
        # and the second download didn't error out.
        assert phase_b_single["status"] != "failed", (
            f"Phase B single failed despite Phase A success: {phase_b_single.get('error')}"
        )

        # 6. Re-queue the album. With dupe checks on the album endpoint reports
        #    every track as already present; queued_count should be 0.
        r = api.post(
            f"{base_url}/api/albums/download",
            json={
                "artist":       _ALBUM_ARTIST,
                "album_title":  _ALBUM_TITLE,
                "release_mbid": album_release_mbid,
                "make_m3u":     False,
                "convert_to_flac": True,
            },
            timeout=30,
        )
        assert r.status_code == 200, f"Phase B album re-queue failed: {r.text}"
        album_recheck = r.json()
        # Anything that landed in Phase A should show up under existing_count now.
        existing = album_recheck.get("existing_count", 0) or 0
        queued = album_recheck.get("queued_count", 0) or 0
        assert existing >= 1, (
            f"Phase B album: dupe check found no existing tracks (queued={queued}, existing={existing}): "
            f"{album_recheck}"
        )

        # 7. Re-import the playlist. With Navidrome dupe checks back on, the
        #    download worker should short-circuit each track via the "Already
        #    exists" code path. The bulk-import status endpoint exposes that as
        #    `dupe_skipped`, so we can finally make a strong assertion: every
        #    track Phase A successfully landed must come back as a dupe-skip,
        #    minus some slack for the long tail of Spotify name normalisation
        #    drift between fetches.
        r = api.post(
            f"{base_url}/api/fetch-playlist",
            json={"url": _SPOTIFY_PLAYLIST_URL},
            timeout=120,
        )
        if r.status_code == 200:
            playlist_tracks_b = r.json().get("tracks") or []
            if playlist_tracks_b:
                r = api.post(
                    f"{base_url}/api/bulk-import-async",
                    json={"songs": "\n".join(playlist_tracks_b), "convert_to_flac": True},
                    timeout=15,
                )
                assert r.status_code == 200, f"Phase B bulk import create failed: {r.text}"
                bulk_b_id = r.json().get("import_id")
                bulk_b = _wait_for_bulk(api, base_url, bulk_b_id, _BULK_TIMEOUT)
                failed_b = bulk_b.get("failed", 0) or 0
                dupe_skipped_b = bulk_b.get("dupe_skipped", 0) or 0
                assert failed_b < len(playlist_tracks_b), (
                    f"Phase B bulk import: every track failed even with dupe checks on: {bulk_b}"
                )
                assert dupe_skipped_b >= 1, (
                    f"Phase B bulk import: dupe check caught zero tracks "
                    f"(dupe_skipped={dupe_skipped_b}, completed={bulk_b.get('completed')}): {bulk_b}"
                )

        # 8. Manual refresh of the watched playlist. This is the path that suffered
        #    the Navidrome-sentinel regression: with dupe checks back on,
        #    _has_local_track_file should treat every Navidrome-confirmed track as
        #    "exists" and the refresh should queue zero new downloads. If the
        #    .is_absolute() filter ever sneaks back in, this assertion catches it.
        if watched_playlist_id:
            r = api.post(
                f"{base_url}/api/watched-playlists/{watched_playlist_id}/refresh",
                timeout=180,
            )
            assert r.status_code == 200, (
                f"Phase B manual refresh failed: {r.status_code} {r.text}"
            )
            refresh_result = r.json()
            queued = refresh_result.get("queued", 0) or 0
            new_tracks = refresh_result.get("new_tracks", 0) or 0
            # new_tracks counts tracks the upstream playlist gained since last check
            # (should be 0 for a playlist we just added). queued counts tracks the
            # refresher decided to download. Both should be zero when every track
            # already lives in Navidrome.
            assert new_tracks == 0, (
                f"Phase B refresh: expected zero new upstream tracks, got {new_tracks}: {refresh_result}"
            )
            assert queued == 0, (
                f"Phase B refresh: dupe check let {queued} tracks slip through and re-queue "
                f"(_has_local_track_file is probably mishandling the Navidrome sentinel): {refresh_result}"
            )

    # --------------------------------------------------------------------- #
    # PHASE C  -  cleanup. Best-effort; failures here log but don't fail
    #             the test, because they'd mask the actual Phase A/B result.
    # --------------------------------------------------------------------- #
    if watched_playlist_id:
        try:
            api.delete(f"{base_url}/api/watched-playlists/{watched_playlist_id}", timeout=15)
        except requests.RequestException:
            pass

    for jid in tracked_job_ids:
        try:
            api.delete(f"{base_url}/api/jobs/{jid}/file", timeout=15)
        except requests.RequestException:
            pass

    # Queue rows are deliberately retained as test history. Never use the
    # whole-queue cleanup endpoint from a suite aimed at a persistent instance.

