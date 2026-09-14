"""
Bulk import tests.

Fast tests verify the API contract (create, list, status shape).
Slow tests submit real tracks and verify they get searched and queued,
but do NOT wait for downloads to complete.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Well-known tracks that should always be searchable
_TEST_TRACKS = "\n".join([
    "Radiohead - Creep",
    "Nirvana - Come As You Are",
    "The Beatles - Let It Be",
])


def test_single_import_download_capacity_is_two():
    """One playlist run may keep two download workers occupied."""
    import bulk_import

    assert bulk_import.TRACKS_PER_IMPORT == 2


def test_bulk_imports_list(api, base_url):
    r = api.get(f"{base_url}/api/bulk-imports", timeout=10)
    assert r.status_code == 200
    d = r.json()
    assert "imports" in d
    assert isinstance(d["imports"], list)


def test_bulk_imports_list_shape(api, base_url):
    imports = api.get(f"{base_url}/api/bulk-imports", timeout=10).json()["imports"]
    for imp in imports[:3]:
        for key in ("id", "status", "total_tracks", "searched", "queued", "failed"):
            assert key in imp, f"bulk import missing key: {key}"


def test_bulk_import_create_returns_id(api, base_url):
    """Verify POST returns an import_id and success flag immediately."""
    r = api.post(
        f"{base_url}/api/bulk-import-async",
        json={"songs": "Test Artist - Test Song", "convert_to_flac": True},
        timeout=15,
    )
    assert r.status_code == 200
    d = r.json()
    assert "import_id" in d, f"no import_id in response: {d}"
    import_id = d["import_id"]
    assert import_id  # not None or empty
    assert d.get("status") in ("pending", "processing", "completed")

    # Status endpoint should work immediately
    s = api.get(f"{base_url}/api/bulk-import/{import_id}/status", timeout=10)
    assert s.status_code == 200

def test_bulk_import_status_shape(api, base_url):
    """Status response should contain all expected fields."""
    r = api.post(
        f"{base_url}/api/bulk-import-async",
        json={"songs": "Test Artist - Test Song", "convert_to_flac": True},
        timeout=15,
    )
    import_id = r.json()["import_id"]

    s = api.get(f"{base_url}/api/bulk-import/{import_id}/status", timeout=10).json()
    for key in ("import_id", "status", "total_tracks", "searched", "queued", "completed", "failed"):
        assert key in s, f"status response missing key: {key}"

    assert s["total_tracks"] == 1, f"expected 1 track, got {s['total_tracks']}"


def test_bulk_import_status_carries_every_track_with_job_id(api, base_url):
    """The Bulk Import tab's live job list needs job_id per track to fetch the
    real job card, and needs every track (not just a handful), or a big
    playlist's tail end would simply be missing from the view."""
    songs = "Radiohead - Creep\nNirvana - Come As You Are\nThe Beatles - Let It Be"
    r = api.post(
        f"{base_url}/api/bulk-import-async",
        json={"songs": songs, "convert_to_flac": True},
        timeout=15,
    )
    import_id = r.json()["import_id"]

    s = api.get(f"{base_url}/api/bulk-import/{import_id}/status", timeout=10).json()
    assert "recent_tracks" not in s, "recent_tracks was replaced by the full 'tracks' list"
    tracks = s["tracks"]
    assert len(tracks) == 3
    for track in tracks:
        for key in ("artist", "song", "status", "error", "job_id"):
            assert key in track, f"track missing key: {key}"

def test_bulk_import_multiline_track_count(api, base_url):
    """total_tracks should match the number of submitted lines."""
    songs = "Radiohead - Creep\nNirvana - Come As You Are\nThe Beatles - Let It Be"
    r = api.post(
        f"{base_url}/api/bulk-import-async",
        json={"songs": songs, "convert_to_flac": True},
        timeout=15,
    )
    assert r.status_code == 200
    import_id = r.json()["import_id"]

    s = api.get(f"{base_url}/api/bulk-import/{import_id}/status", timeout=10).json()
    assert s["total_tracks"] == 3, (
        f"expected 3 tracks for 3 input lines, got {s['total_tracks']}"
    )

def test_bulk_import_bad_format_graceful(api, base_url):
    """Blank lines and malformed entries shouldn't crash the import."""
    songs = "\n\nRadiohead - Creep\n\nThis line has no dash\n\n"
    r = api.post(
        f"{base_url}/api/bulk-import-async",
        json={"songs": songs},
        timeout=15,
    )
    assert r.status_code == 200


def test_bulk_import_status_404_unknown_id(api, base_url):
    r = api.get(f"{base_url}/api/bulk-import/doesnotexist/status", timeout=10)
    assert r.status_code == 404


def test_jobs_filtered_by_import_id_scopes_to_that_import(api, base_url):
    """GET /api/jobs?import_id=... is what the Bulk Import tab uses to draw the
    exact same job cards the Queue tab does. Freshly submitted tracks haven't
    reached a job yet, so this should come back empty rather than erroring,
    and it must never bleed in jobs from someone else's import."""
    r = api.post(
        f"{base_url}/api/bulk-import-async",
        json={"songs": "Test Artist - Test Song", "convert_to_flac": True},
        timeout=15,
    )
    import_id = r.json()["import_id"]

    jobs_resp = api.get(f"{base_url}/api/jobs?import_id={import_id}", timeout=10)
    assert jobs_resp.status_code == 200
    body = jobs_resp.json()
    assert "jobs" in body
    assert isinstance(body["jobs"], list)

    other = api.get(f"{base_url}/api/jobs?import_id=doesnotexist", timeout=10)
    assert other.status_code == 200
    assert other.json()["jobs"] == []


@pytest.mark.slow
def test_bulk_import_tracks_get_searched(api, base_url):
    """
    Submit 3 well-known tracks and verify the import lifecycle accounts for
    every one. A run may download two tracks concurrently. Repeat runs may
    correctly skip tracks already left in the persistent test library.
    """
    r = api.post(
        f"{base_url}/api/bulk-import-async",
        json={"songs": _TEST_TRACKS, "convert_to_flac": True},
        timeout=15,
    )
    assert r.status_code == 200
    import_id = r.json()["import_id"]

    # Poll until the import is done. Live providers can still take a while,
    # especially when an acquisition falls through sources.
    deadline = time.time() + 180
    status = {}
    while time.time() < deadline:
        time.sleep(3)
        status = api.get(f"{base_url}/api/bulk-import/{import_id}/status", timeout=10).json()
        if status.get("status") in ("completed", "error"):
            break

    searched = status.get("searched", 0) or 0
    dupe_skipped = status.get("dupe_skipped", 0) or 0
    # Depending on where the duplicate is found, `searched` may already include
    # it and `dupe_skipped` is therefore a subset rather than a separate bucket.
    assert searched + dupe_skipped >= 3, (
        f"expected all 3 tracks to be searched or duplicate-skipped: {status}"
    )
    assert status.get("failed", 0) < 3, (
        f"all 3 tracks failed to search - something is wrong: {status}"
    )
    # Some tracks should have been queued for download
    useful_outcome = (
        (status.get("queued", 0) or 0)
        + (status.get("completed", 0) or 0)
        + dupe_skipped
    )
    assert useful_outcome >= 1, (
        f"no tracks were queued, completed, or already present: {status}"
    )
