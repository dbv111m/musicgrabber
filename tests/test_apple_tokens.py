"""Apple Music amp-api auth: the user token must reach the catalog path too.

Issue #94. Apple's anonymous developer token now 401s on the public catalog
endpoint, which meant any playlist over ~300 tracks silently fell back to HTML
scraping and lost the rest. Sending the user's token is the fix; saying so when
we are truncated is the apology.
"""

import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import apple


def test_auth_headers_send_both_spellings():
    headers = apple._auth_headers("devtoken", "usertoken")
    assert headers["Authorization"] == "Bearer devtoken"
    assert headers["Origin"] == apple._WEB_ORIGIN
    # Apple's web player says media-user-token, MusicKit's docs say
    # Music-User-Token. Same value, so send both and stop guessing.
    assert headers["Music-User-Token"] == "usertoken"
    assert headers["media-user-token"] == "usertoken"


def test_auth_headers_omit_user_token_when_absent():
    headers = apple._auth_headers("devtoken", None)
    assert "Music-User-Token" not in headers
    assert "media-user-token" not in headers


def test_catalog_path_carries_the_user_token(monkeypatch):
    """A public playlist URL must still hand Apple the user's token."""
    seen = []

    def fake_fetch_json(url, headers=None):
        seen.append(headers or {})
        return {"data": [{"attributes": {"artistName": "Blur", "name": "Song 2"}}]}

    monkeypatch.setattr(apple, "_fetch_json", fake_fetch_json)
    monkeypatch.setattr(apple, "_extract_web_bundle_url", lambda *_a: "https://x/bundle.js")
    monkeypatch.setattr(apple, "_extract_web_token", lambda _u: "devtoken")

    html = '<link rel="canonical" href="https://music.apple.com/gb/playlist/x/pl.abc">'
    result = apple._fetch_via_api(
        "https://music.apple.com/gb/playlist/x/pl.abc", html, "X", "usertoken"
    )

    assert result["tracks"] == ["Blur - Song 2"]
    assert seen[0]["media-user-token"] == "usertoken"


def test_html_fallback_warns_when_it_hits_apple_s_cap(monkeypatch):
    monkeypatch.setattr(
        apple, "_extract_tracks_from_html",
        lambda _html: [f"Artist {i} - Track {i}" for i in range(299)],
    )
    result = apple._fetch_via_html("<html></html>", "Opluisteren", False)
    assert "truncated" in result["warning"]
    assert "Settings" in result["warning"]

    with_token = apple._fetch_via_html("<html></html>", "Opluisteren", True)
    assert "expired" in with_token["warning"]


def test_html_fallback_stays_quiet_for_a_short_playlist(monkeypatch):
    monkeypatch.setattr(
        apple, "_extract_tracks_from_html",
        lambda _html: [f"Artist {i} - Track {i}" for i in range(40)],
    )
    result = apple._fetch_via_html("<html></html>", "Short one", False)
    assert "warning" not in result


def test_empty_html_scrape_is_still_a_422(monkeypatch):
    monkeypatch.setattr(apple, "_extract_tracks_from_html", lambda _html: [])
    with pytest.raises(HTTPException) as excinfo:
        apple._fetch_via_html("<html></html>", "Nothing", False)
    assert excinfo.value.status_code == 422
