"""
Integration tests for MusicGrabber Telegram bot against the running app API.

Requires a running MusicGrabber instance on http://localhost:38274 (see
tests/conftest.py for env vars). Marked @pytest.mark.slow because they hit
real external services (YouTube, etc.).

Run with: ./tests/run_tests.sh --slow
"""

import os
import sys
import time

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_URL = os.environ.get("MG_BASE_URL", "http://localhost:38274").rstrip("/")
API_KEY = os.environ.get("MG_API_KEY", "")


@pytest.fixture(scope="session")
def api():
    """Requests session to the running MusicGrabber instance."""
    s = requests.Session()
    s.headers["Content-Type"] = "application/json"
    if API_KEY:
        s.headers["X-API-Key"] = API_KEY
    yield s


@pytest.fixture
def api_key_headers():
    """Headers with API key if configured."""
    if API_KEY:
        return {"X-API-Key": API_KEY}
    return {}


@pytest.mark.slow
def test_api_config_accessible(api):
    """The /api/config endpoint should be reachable."""
    r = api.get(f"{BASE_URL}/api/config", timeout=10)
    r.raise_for_status()
    data = r.json()
    assert "version" in data


@pytest.mark.slow
def test_api_search_returns_results(api, api_key_headers):
    """Search should return a response (may be empty if no sources available)."""
    r = api.post(
        f"{BASE_URL}/api/search",
        json={"query": "rick astley never gonna give you up", "source": "youtube", "limit": 5},
        headers=api_key_headers,
        timeout=30,
    )
    assert r.status_code == 200, f"search failed: {r.text}"
    data = r.json()
    assert "results" in data


@pytest.mark.slow
def test_download_and_cancel_job(api, api_key_headers):
    """Create a download job via /api/download, then cancel it via /api/jobs/{id}/cancel."""
    # First create a job
    r = api.post(
        f"{BASE_URL}/api/download",
        json={
            "video_id": "dQw4w9WgXcQ",
            "title": "Never Gonna Give You Up",
            "artist": "Rick Astley",
            "source": "youtube",
            "convert_audio": False,
        },
        headers=api_key_headers,
        timeout=30,
    )
    assert r.status_code == 200, f"download failed: {r.text}"
    data = r.json()
    job_id = data.get("job_id")
    assert job_id, f"no job_id in response: {data}"

    # Check status
    r = api.get(
        f"{BASE_URL}/api/jobs/{job_id}",
        headers=api_key_headers,
        timeout=10,
    )
    assert r.status_code == 200, f"job status failed: {r.text}"
    job = r.json()
    assert job.get("status") in ("queued", "downloading", "completed", "failed")

    # Cancel if still queued/downloading and endpoint exists
    if job.get("status") in ("queued", "downloading"):
        r = api.post(
            f"{BASE_URL}/api/jobs/{job_id}/cancel",
            headers=api_key_headers,
            timeout=30,
        )
        if r.status_code == 404:
            # Cancel endpoint not available in this app version - skip cancel test
            pytest.skip(f"Cancel endpoint not available (404), app may be running older code")
        assert r.status_code == 200, f"cancel failed: {r.text}"
        cancel_data = r.json()
        assert cancel_data.get("status") == "cancelled"

    # Clean up: delete the job file if completed
    try:
        api.delete(
            f"{BASE_URL}/api/jobs/{job_id}/file",
            headers=api_key_headers,
            timeout=10,
        )
    except Exception:
        pass


@pytest.mark.slow
def test_cancel_nonexistent_job(api, api_key_headers):
    """Cancelling a nonexistent job should return 404."""
    r = api.post(
        f"{BASE_URL}/api/jobs/nonexistent_job_id_12345/cancel",
        headers=api_key_headers,
        timeout=10,
    )
    assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text}"
