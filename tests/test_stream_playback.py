"""Playback stream tokens.

An <audio> element cannot send an Authorization header, so every play button in
the UI was quietly getting a 401 and reporting "Could not play file". The fix is
a reusable, user-bound token in the URL. These tests keep it honest: it must work
for playback, survive the range requests a seeking player makes, and stay firmly
away from every other endpoint.
"""

import pytest
import requests

import middleware


# ---------------------------------------------------------------------------
# Offline: which paths the middleware will even consider a playback token for
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/api/jobs/abc12345/stream",
    "/api/trash/stream",
])
def test_playback_paths_are_recognised(path):
    assert middleware._is_audio_stream_path(path) is True


@pytest.mark.parametrize("path", [
    "/api/search/stream",            # NDJSON search, a very different animal
    "/api/jobs/abc12345/download",   # has its own single-use token
    "/api/jobs/abc12345",
    "/api/jobs/abc12345/stream/nope",
    "/api/trash",
    "/api/trash/stream/extra",
    "/api/settings",
    "/",
])
def test_everything_else_is_not_a_playback_path(path):
    assert middleware._is_audio_stream_path(path) is False


def test_trailing_slashes_do_not_smuggle_a_path_through():
    """Leading and trailing slashes are stripped, so these are the same path."""
    assert middleware._is_audio_stream_path("/api/trash/stream/") is True
    assert middleware._is_audio_stream_path("api/jobs/x/stream") is True


# ---------------------------------------------------------------------------
# Integration: the token against the running container
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def session_auth(is_single_user):
    """Is session auth actually in play on this instance?

    This used to read `users_exist` itself, because the shared `is_single_user`
    fixture was reading `auth_required` ("an API key is set", nothing to do with
    logins) and therefore lying. That's since been fixed at the source, so this
    is just the inverse of it again.
    """
    return not is_single_user


@pytest.fixture(scope="module")
def stream_token(api, base_url, session_auth):
    if not session_auth:
        pytest.skip("single-user mode - playback needs no token")
    r = api.post(f"{base_url}/api/auth/stream-token", timeout=10)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("token"), "no token issued"
    assert d.get("expires_in", 0) > 0
    return d["token"]


@pytest.fixture(scope="module")
def playable_job(api, base_url):
    """A completed job whose file is still on disk, or nothing to test with."""
    r = api.get(f"{base_url}/api/jobs?limit=50", timeout=15)
    assert r.status_code == 200
    payload = r.json()
    jobs = payload if isinstance(payload, list) else payload.get("jobs", [])
    for job in jobs:
        if job.get("status") == "completed" and not job.get("file_deleted"):
            return job["id"]
    pytest.skip("no completed job with a file to play")


def test_bare_audio_request_without_a_token_is_refused(base_url, playable_job, session_auth):
    """No header, no token: exactly what a naked <audio src> looks like."""
    if not session_auth:
        pytest.skip("single-user mode - no auth to refuse with")
    r = requests.get(f"{base_url}/api/jobs/{playable_job}/stream", timeout=15)
    assert r.status_code == 401


def test_token_lets_a_headerless_player_through(base_url, playable_job, stream_token):
    r = requests.get(
        f"{base_url}/api/jobs/{playable_job}/stream",
        params={"stream_token": stream_token},
        timeout=30,
    )
    assert r.status_code == 200, r.text[:200]
    assert r.headers.get("content-type", "").startswith("audio/")


def test_the_token_is_reusable(base_url, playable_job, stream_token):
    """Single-use would die a second into the first track."""
    for _ in range(3):
        r = requests.get(
            f"{base_url}/api/jobs/{playable_job}/stream",
            params={"stream_token": stream_token},
            timeout=30,
        )
        assert r.status_code == 200


def test_range_requests_work_so_seeking_does_too(base_url, playable_job, stream_token):
    r = requests.get(
        f"{base_url}/api/jobs/{playable_job}/stream",
        params={"stream_token": stream_token},
        headers={"Range": "bytes=100-999"},
        timeout=30,
    )
    assert r.status_code == 206
    assert len(r.content) == 900


def test_a_made_up_token_gets_nowhere(base_url, playable_job, session_auth):
    if not session_auth:
        pytest.skip("single-user mode - no auth to refuse with")
    r = requests.get(
        f"{base_url}/api/jobs/{playable_job}/stream",
        params={"stream_token": "definitely-not-a-real-token"},
        timeout=15,
    )
    assert r.status_code == 401


def test_the_token_does_not_open_the_download_endpoint(base_url, playable_job, stream_token, session_auth):
    """Downloads have their own single-use token; this one is for listening only."""
    if not session_auth:
        pytest.skip("single-user mode - no auth to refuse with")
    r = requests.get(
        f"{base_url}/api/jobs/{playable_job}/download",
        params={"stream_token": stream_token},
        timeout=15,
    )
    assert r.status_code == 401


def test_the_token_does_not_open_the_search_stream(base_url, stream_token, session_auth):
    if not session_auth:
        pytest.skip("single-user mode - no auth to refuse with")
    r = requests.post(
        f"{base_url}/api/search/stream",
        params={"stream_token": stream_token},
        json={"query": "nothing in particular"},
        timeout=15,
    )
    assert r.status_code == 401


def test_the_token_does_not_open_the_rest_of_the_api(base_url, stream_token, session_auth):
    if not session_auth:
        pytest.skip("single-user mode - no auth to refuse with")
    for path in ("/api/settings", "/api/jobs", "/api/trash"):
        r = requests.get(f"{base_url}{path}", params={"stream_token": stream_token}, timeout=15)
        assert r.status_code == 401, f"{path} accepted a playback token"


def test_trash_playback_accepts_the_token(api, base_url, stream_token):
    r = api.get(f"{base_url}/api/trash", timeout=15)
    assert r.status_code == 200
    payload = r.json()
    files = payload if isinstance(payload, list) else payload.get("files", [])
    if not files:
        pytest.skip("nothing in the trash to play")
    path = files[0]["path"]

    bare = requests.get(f"{base_url}/api/trash/stream", params={"path": path}, timeout=15)
    assert bare.status_code == 401

    ok = requests.get(
        f"{base_url}/api/trash/stream",
        params={"path": path, "stream_token": stream_token},
        timeout=30,
    )
    assert ok.status_code == 200
    assert ok.headers.get("content-type", "").startswith("audio/")
