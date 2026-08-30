"""Regression tests for the Selenium-assisted MP3Phoenix resurrection."""

from types import SimpleNamespace

import pytest

import mp3phoenix
import mp3phoenix_browser
import search


_FRAGMENT = """ignored<!|!>ignored<!|!>
<div>
  <div class="musicTheme-results-info__card_artist"><b>Massive Attack</b></div>
  <a class="musicTheme-results-info__card_tracklink">Teardrop</a>
  <span class="dur">5:31</span>
  <a class="musicTheme-results-info__card_download link"
     href="//mp3phoenix.net/getmp3/token/file.mp3">Download</a>
</div>
"""


@pytest.fixture(autouse=True)
def _reset_circuit_breaker():
    """_CLIENT is a module-level singleton; production code mutates its
    _failures/_disabled_until directly, which monkeypatch cannot undo. Reset
    it around every test so a failure recorded by one test can't trip the
    breaker for an unrelated test later in the run."""
    mp3phoenix._CLIENT._failures = 0
    mp3phoenix._CLIENT._disabled_until = 0.0
    yield
    mp3phoenix._CLIENT._failures = 0
    mp3phoenix._CLIENT._disabled_until = 0.0


def test_source_previews_but_stays_disabled_by_default():
    source = search.SOURCE_REGISTRY["mp3phoenix"]
    # Off by default like Soulseek: it briefly launches Chrome for Cloudflare
    # clearance, which no install should get sprung on it by an upgrade.
    assert source["default_enabled"] is False
    # Preview goes out through our own address rather than the provider's, because
    # Cloudflare will not serve the browser directly.
    assert source["has_preview"] is True
    assert source["label"] == "MP3Phoenix"


def test_browser_subprocess_does_not_receive_application_secrets(monkeypatch):
    monkeypatch.setenv("NAVIDROME_PASS", "nope")
    monkeypatch.setenv("SLSKD_PASS", "also-nope")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = mp3phoenix._browser_subprocess_env()
    assert env["PATH"] == "/usr/bin"
    assert env["PYTHONUNBUFFERED"] == "1"
    assert "NAVIDROME_PASS" not in env
    assert "SLSKD_PASS" not in env


def test_parse_results_restores_scored_320k_mp3():
    results = mp3phoenix._parse_results(_FRAGMENT, "Massive Attack - Teardrop")
    assert len(results) == 1
    result = results[0]
    assert result["source"] == "mp3phoenix"
    assert result["channel"] == "Massive Attack"
    assert result["title"] == "Teardrop"
    assert result["quality"] == "320kbps"
    assert result["source_url"].startswith("https://mp3phoenix.net/getmp3/")


def test_browser_exports_clearance_session():
    class FakeCdp:
        def get_all_cookies(self):
            return [SimpleNamespace(name="cf_clearance", value="token", domain=".mp3phoenix.net", path="/")]

        def evaluate(self, _script):
            return "Chrome UA"

    details = mp3phoenix_browser._session_details(SimpleNamespace(cdp=FakeCdp()))
    assert details["user_agent"] == "Chrome UA"
    assert details["cookies"][0]["name"] == "cf_clearance"


class _FakeResponse:
    def __init__(self, body="", status=200, content_type="text/html", chunks=()):
        self.text = body
        self.status_code = status
        self.headers = {"content-type": content_type}
        self._chunks = chunks

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, _size):
        return iter(self._chunks)

    def close(self):
        pass


class _FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.cookies = self
        self.headers = {}

    def set(self, *_args, **_kwargs):
        pass

    def get(self, *_args, **_kwargs):
        return next(self.responses)

    def close(self):
        pass


def _session_details():
    return {
        "user_agent": "Chrome UA",
        "cookies": [{
            "name": "cf_clearance", "value": "token",
            "domain": ".mp3phoenix.net", "path": "/",
        }],
    }


def test_search_bootstraps_once_then_reuses_http_session(monkeypatch):
    browser_calls = []

    class FakeBrowser:
        def request(self, payload, timeout):
            browser_calls.append((payload, timeout))
            return _session_details()

    monkeypatch.setattr(mp3phoenix, "_BROWSER", FakeBrowser())
    session = _FakeSession([_FakeResponse(_FRAGMENT), _FakeResponse(_FRAGMENT)])
    monkeypatch.setattr(mp3phoenix.requests, "Session", lambda **_kwargs: session)
    monkeypatch.setattr(mp3phoenix, "_CLIENT", mp3phoenix._PhoenixClient())
    results = mp3phoenix.search_mp3phoenix("Massive Attack - Teardrop", 4)
    again = mp3phoenix.search_mp3phoenix("Massive Attack - Teardrop", 4)
    assert len(results) == 1
    assert len(again) == 1
    assert browser_calls[0][0] == {"action": "session"}
    assert len(browser_calls) == 1


def test_http_download_streams_chunks_to_staging(monkeypatch, tmp_path):
    payload = b"ID3" + (b"phoenix" * 50)
    response = _FakeResponse(content_type="audio/mpeg", chunks=(payload[:20], payload[20:]))
    response.headers["content-length"] = str(len(payload))
    monkeypatch.setattr(mp3phoenix._CLIENT, "_session", _FakeSession([response]))
    monkeypatch.setattr(mp3phoenix._CLIENT, "_refreshed_at", mp3phoenix.time.monotonic())
    destination = tmp_path / "track.mp3"
    mp3phoenix.download_mp3phoenix_track(
        "https://mp3phoenix.net/getmp3/token/file.mp3", destination
    )
    assert destination.read_bytes() == payload
    assert not (tmp_path / "track.mp3.part").exists()


def test_http_download_rejects_non_audio_response(monkeypatch, tmp_path):
    response = _FakeResponse("ordinary error page", content_type="text/html")
    monkeypatch.setattr(mp3phoenix._CLIENT, "_session", _FakeSession([response]))
    monkeypatch.setattr(mp3phoenix._CLIENT, "_refreshed_at", mp3phoenix.time.monotonic())
    destination = tmp_path / "challenge.mp3"
    try:
        mp3phoenix.download_mp3phoenix_track(
            "https://mp3phoenix.net/getmp3/token/file.mp3", destination
        )
    except RuntimeError as exc:
        assert "instead of audio" in str(exc)
    else:
        raise AssertionError("HTML challenge response was accepted as an MP3")


def test_preview_takes_the_opening_chunk_and_hangs_up(monkeypatch):
    """A preview auditions a track; it has no business dragging the whole file
    through the one guarded session that searches and downloads are queueing for."""
    served = []

    class _CountingResponse(_FakeResponse):
        def iter_content(self, size):
            for index in range(50):
                served.append(index)
                yield b"x" * size

    response = _CountingResponse(content_type="audio/mpeg")
    monkeypatch.setattr(mp3phoenix._CLIENT, "_session", _FakeSession([response]))
    monkeypatch.setattr(mp3phoenix._CLIENT, "_refreshed_at", mp3phoenix.time.monotonic())
    monkeypatch.setattr(mp3phoenix, "MP3PHOENIX_PREVIEW_BYTES", 128 * 1024)

    snippet = mp3phoenix.fetch_mp3phoenix_preview(
        "https://mp3phoenix.net/getmp3/token/file.mp3"
    )

    assert len(snippet) == 128 * 1024
    assert len(served) == 2  # 64 KB a go, so two of them, and then we stop asking


def test_preview_rejects_non_audio_response(monkeypatch):
    """A Cloudflare challenge page is not a song, however confidently it is served."""
    response = _FakeResponse("ordinary error page", content_type="text/html")
    monkeypatch.setattr(mp3phoenix._CLIENT, "_session", _FakeSession([response]))
    monkeypatch.setattr(mp3phoenix._CLIENT, "_refreshed_at", mp3phoenix.time.monotonic())

    with pytest.raises(RuntimeError, match="instead of audio"):
        mp3phoenix.fetch_mp3phoenix_preview(
            "https://mp3phoenix.net/getmp3/token/file.mp3"
        )


def test_preview_refuses_a_url_that_is_not_mp3phoenix():
    """The preview endpoint is a fetcher; it does not take requests from strangers."""
    with pytest.raises(ValueError):
        mp3phoenix.fetch_mp3phoenix_preview("https://example.com/definitely-a-song.mp3")


@pytest.mark.parametrize("url", [
    "http://mp3phoenix.net/getmp3/token/file.mp3",
    "https://www.mp3phoenix.net/getmp3/token/file.mp3",
    "https://mp3phoenix.net/",
    "https://mp3phoenix.net/ajax/music/not-a-track",
    "https://mp3phoenix.net/getmp3/",
    "https://mp3phoenix.net:443/getmp3/token/file.mp3",
])
def test_preview_refuses_provider_urls_that_are_not_search_result_media(url):
    with pytest.raises(ValueError, match="Invalid MP3Phoenix download URL"):
        mp3phoenix.fetch_mp3phoenix_preview(url)


def test_bad_preview_does_not_open_the_shared_provider_circuit(monkeypatch):
    """A caller can hold a stale or invented media token. Its preview should
    fail, but search and downloads for every other user must remain available."""
    responses = [
        _FakeResponse("ordinary error page", content_type="text/html")
        for _ in range(mp3phoenix.MP3PHOENIX_BROWSER_FAILURE_LIMIT)
    ]
    monkeypatch.setattr(mp3phoenix._CLIENT, "_session", _FakeSession(responses))
    monkeypatch.setattr(mp3phoenix._CLIENT, "_refreshed_at", mp3phoenix.time.monotonic())

    for _ in responses:
        with pytest.raises(RuntimeError, match="instead of audio"):
            mp3phoenix.fetch_mp3phoenix_preview(
                "https://mp3phoenix.net/getmp3/stale-token/file.mp3"
            )

    assert mp3phoenix._CLIENT._failures == 0
    assert mp3phoenix.browser_healthy()[0] is True


def test_cloudflare_rejection_refreshes_session_once(monkeypatch):
    browser_calls = []

    class FakeBrowser:
        def request(self, payload, timeout):
            browser_calls.append((payload, timeout))
            return _session_details()

    sessions = iter([
        _FakeSession([_FakeResponse("Just a moment", status=403)]),
        _FakeSession([_FakeResponse(_FRAGMENT)]),
    ])
    monkeypatch.setattr(mp3phoenix, "_BROWSER", FakeBrowser())
    monkeypatch.setattr(mp3phoenix.requests, "Session", lambda **_kwargs: next(sessions))
    client = mp3phoenix._PhoenixClient()
    assert client.search("https://mp3phoenix.net/ajax/music/test") == _FRAGMENT
    assert len(browser_calls) == 2


def test_refresh_gives_the_browser_bootstrap_more_time_than_the_search_timeout(monkeypatch):
    """The bootstrap clears Cloudflare (slow); the AJAX search call is quick. Giving
    the bootstrap only the search's own timeout budget risks killing it mid-clearance."""
    browser_calls = []

    class FakeBrowser:
        def request(self, payload, timeout):
            browser_calls.append(timeout)
            return _session_details()

    monkeypatch.setattr(mp3phoenix, "_BROWSER", FakeBrowser())
    monkeypatch.setattr(
        mp3phoenix.requests, "Session", lambda **_kwargs: _FakeSession([_FakeResponse(_FRAGMENT)])
    )
    client = mp3phoenix._PhoenixClient()
    client.search("https://mp3phoenix.net/ajax/music/test")
    assert browser_calls == [mp3phoenix.MP3PHOENIX_BROWSER_TIMEOUT + 20]
    assert browser_calls[0] > mp3phoenix.TIMEOUT_MP3PHOENIX_SEARCH


def test_browser_subprocess_env_forwards_timeout_to_the_child():
    env = mp3phoenix._browser_subprocess_env()
    assert env["MP3PHOENIX_BROWSER_TIMEOUT"] == str(mp3phoenix.MP3PHOENIX_BROWSER_TIMEOUT)


def test_cooldown_does_not_renew_itself_on_every_call(monkeypatch):
    """A RuntimeError raised while the breaker is open must not be recorded as a
    fresh failure, otherwise anything polling MP3Phoenix during the cooldown
    keeps pushing _disabled_until further out and it never expires."""
    class FailIfCalledBrowser:
        def request(self, payload, timeout):
            raise AssertionError("should not attempt to refresh a cooling-down session")

    monkeypatch.setattr(mp3phoenix, "_BROWSER", FailIfCalledBrowser())
    client = mp3phoenix._PhoenixClient()
    client._failures = mp3phoenix.MP3PHOENIX_BROWSER_FAILURE_LIMIT
    client._disabled_until = mp3phoenix.time.monotonic() + 600
    disabled_until_before = client._disabled_until

    for _ in range(3):
        with pytest.raises(RuntimeError, match="cooling down"):
            client.search("https://mp3phoenix.net/ajax/music/test")

    assert client._disabled_until == disabled_until_before
    assert client._failures == mp3phoenix.MP3PHOENIX_BROWSER_FAILURE_LIMIT


def test_parse_results_discards_a_page_with_mismatched_field_counts():
    """Two artist cards but only one title/duration/href: pairing these positionally
    would tag the second card's download link onto the first card's identity."""
    broken_fragment = """ignored<!|!>ignored<!|!>
<div>
  <div class="musicTheme-results-info__card_artist"><b>Massive Attack</b></div>
  <div class="musicTheme-results-info__card_artist"><b>Portishead</b></div>
  <a class="musicTheme-results-info__card_tracklink">Teardrop</a>
  <span class="dur">5:31</span>
  <a class="musicTheme-results-info__card_download link"
     href="//mp3phoenix.net/getmp3/token/file.mp3">Download</a>
</div>
"""
    assert mp3phoenix._parse_results(broken_fragment, "Massive Attack - Teardrop") == []


def test_browser_healthy_reports_remembered_state_without_launching_chrome(monkeypatch):
    class FailIfCalledBrowser:
        def request(self, payload, timeout):
            raise AssertionError("a health check should never launch Chrome")

    monkeypatch.setattr(mp3phoenix, "_BROWSER", FailIfCalledBrowser())

    healthy, _reason = mp3phoenix.browser_healthy()
    assert healthy is True

    mp3phoenix._CLIENT._disabled_until = mp3phoenix.time.monotonic() + 600
    healthy, reason = mp3phoenix.browser_healthy()
    assert healthy is False
    assert "cooling down" in reason
