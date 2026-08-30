"""
Test fixtures for MusicGrabber integration tests.

Talks to a running MusicGrabber instance. Configure via env vars:
  MG_BASE_URL    - default: http://localhost:38274 (the local docker container built from this repo)
  MG_USERNAME    - only needed in multi-user mode
  MG_PASSWORD    - only needed in multi-user mode

The shared test client refuses endpoints that erase Queue history. Set
MG_ALLOW_HISTORY_DELETION=1 only when the target is a disposable test instance.
"""

import os
from urllib.parse import urlsplit

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = os.environ.get("MG_BASE_URL", "http://localhost:38274").rstrip("/")
MG_USERNAME = os.environ.get("MG_USERNAME", "")
MG_PASSWORD = os.environ.get("MG_PASSWORD", "")
ALLOW_HISTORY_DELETION = os.environ.get("MG_ALLOW_HISTORY_DELETION") == "1"


class QueueHistorySafeSession(requests.Session):
    """Block whole-history deletion unless a disposable target was explicit."""

    _BLOCKED_DELETE_PATHS = ("/api/jobs/cleanup", "/api/stats")

    def __init__(self, *, allow_history_deletion=False):
        super().__init__()
        self.allow_history_deletion = allow_history_deletion
        self._mount_connection_retries()

    def _mount_connection_retries(self):
        """Survive uvicorn hanging up on an idle keep-alive connection.

        The server closes idle connections after five seconds. urllib3 spots a
        socket that is already dead and quietly opens a new one, so the ordinary
        case is fine; what it cannot dodge is the close crossing in flight with
        our send, which comes back as ConnectionError(RemoteDisconnected) and
        killed roughly one slow run in two, a different test each time. The
        request may or may not have reached a handler in that case: TCP cannot
        tell us whether a server committed work before losing its response.

        Retry only safe read-only methods. Replaying POST/PUT/PATCH/DELETE can
        duplicate real mutations, so those failures remain visible. There is no
        status_forcelist either: a 500 is a finding, and retrying it until it
        goes away is how you ship a bug.
        """
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            allowed_methods=frozenset({"HEAD", "GET", "OPTIONS"}),
            status_forcelist=(),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.mount("http://", adapter)
        self.mount("https://", adapter)

    @classmethod
    def deletes_history(cls, method, url):
        if str(method).upper() != "DELETE":
            return False
        path = urlsplit(str(url)).path.rstrip("/")
        return any(path.endswith(blocked) for blocked in cls._BLOCKED_DELETE_PATHS)

    def request(self, method, url, *args, **kwargs):
        if (
            not self.allow_history_deletion
            and self.deletes_history(method, url)
        ):
            raise RuntimeError(
                "Test client blocked an endpoint that erases Queue history. "
                "Queue rows must be retained; use a disposable instance and set "
                "MG_ALLOW_HISTORY_DELETION=1 only for a deliberate destructive test."
            )
        return super().request(method, url, *args, **kwargs)


@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


@pytest.fixture(scope="session")
def api(base_url):
    """Requests session. Logs in if multi-user mode requires it."""
    s = QueueHistorySafeSession(
        allow_history_deletion=ALLOW_HISTORY_DELETION,
    )
    s.headers["Content-Type"] = "application/json"

    cfg = s.get(f"{base_url}/api/config", timeout=10).json()
    if cfg.get("users_exist") and MG_USERNAME:
        resp = s.post(
            f"{base_url}/api/auth/login",
            json={"username": MG_USERNAME, "password": MG_PASSWORD},
            timeout=10,
        )
        resp.raise_for_status()
        token = resp.json()["token"]
        s.headers["Authorization"] = f"Bearer {token}"

    return s


@pytest.fixture(scope="session")
def config(api, base_url):
    return api.get(f"{base_url}/api/config", timeout=10).json()


@pytest.fixture(scope="session")
def is_single_user(config):
    """True when no login is required, i.e. the instance has fewer than 2 accounts.

    Reads `users_exist` (which app.py sets from `user_count >= 2`), the same
    signal the `api` fixture uses to decide whether to log in. It used to read
    `auth_required`, but that flag means "an API key is set" and has nothing to
    do with logins, so on any instance with accounts but no API key the two
    disagreed and every multi-user path quietly skipped itself.
    """
    return not config.get("users_exist", False)
