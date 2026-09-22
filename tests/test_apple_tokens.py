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
    monkeypatch.setattr(apple, "_extract_web_tokens", lambda _u: ["devtoken"])

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


# ---------------------------------------------------------------------------
# Which developer token we pick (the actual cause of #94)
#
# Apple ships several JWTs in its web bundle. Only some of them mean anything
# to amp-api, nothing in the file says which, and picking the wrong one earns
# a bare 401 on every request. So: collect them all, best guess first.
# ---------------------------------------------------------------------------

def _jwt(issuer):
    import base64 as b64, json as j

    def seg(obj):
        raw = j.dumps(obj).encode()
        return b64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{seg({'typ': 'JWT'})}.{seg({'iss': issuer})}.notarealsignature"


def test_web_player_token_outranks_the_one_that_401s(monkeypatch):
    """AMPWebPlay is the browser's own token, so try it before the others."""
    dud = _jwt("M62YD85FTQ")      # the one that refuses everything
    player = _jwt("AMPWebPlay")   # the one that works
    other = _jwt("5IKPP2IECQ")
    monkeypatch.setattr(
        apple, "_fetch_text",
        lambda _u: f'const a="{dud}";const b="{other}";const c="{player}";',
    )

    tokens = apple._extract_web_tokens("https://x/bundle.js")

    assert tokens[0] == player, "web player token should be tried first"
    assert set(tokens) == {dud, player, other}, "no candidate may be discarded"


def test_an_explicitly_named_token_still_wins(monkeypatch):
    """If Apple names it developerToken again, believe them."""
    named = _jwt("NAMED")
    player = _jwt("AMPWebPlay")
    monkeypatch.setattr(
        apple, "_fetch_text",
        lambda _u: f'const c="{player}";developerToken:"{named}"',
    )
    assert apple._extract_web_tokens("https://x/bundle.js")[0] == named


def test_candidates_are_deduplicated(monkeypatch):
    player = _jwt("AMPWebPlay")
    monkeypatch.setattr(
        apple, "_fetch_text", lambda _u: f'const a="{player}";const b="{player}";'
    )
    assert apple._extract_web_tokens("https://x/bundle.js") == [player]


def test_a_bundle_with_no_tokens_is_an_error(monkeypatch):
    monkeypatch.setattr(apple, "_fetch_text", lambda _u: "var x = 1;")
    with pytest.raises(RuntimeError):
        apple._extract_web_tokens("https://x/bundle.js")


def test_a_401_moves_on_to_the_next_candidate(monkeypatch):
    """The whole point: one token's 401 must not end the attempt."""
    dud, player = _jwt("M62YD85FTQ"), _jwt("AMPWebPlay")
    monkeypatch.setattr(apple, "_extract_web_tokens", lambda _u: [dud, player])
    monkeypatch.setattr(apple, "_extract_web_bundle_url", lambda *_a: "https://x/b.js")
    tried = []

    def fake_fetch_all(_sf, _kind, _id, token, _lang, _mut=None):
        tried.append(token)
        if token == dud:
            raise RuntimeError("Apple amp-api returned HTTP 401: ")
        return ["Blur - Song 2"]

    monkeypatch.setattr(apple, "_fetch_all_tracks", fake_fetch_all)
    html = '<link rel="canonical" href="https://music.apple.com/gb/playlist/x/pl.abc">'

    result = apple._fetch_via_api("https://music.apple.com/gb/playlist/x/pl.abc", html, "X")

    assert tried == [dud, player], "must try the refused token then move on"
    assert result["tracks"] == ["Blur - Song 2"]


def test_a_non_auth_failure_is_not_retried_on_every_token(monkeypatch):
    """A genuinely broken playlist should fail once, not four times."""
    monkeypatch.setattr(apple, "_extract_web_tokens", lambda _u: [_jwt("a"), _jwt("b")])
    monkeypatch.setattr(apple, "_extract_web_bundle_url", lambda *_a: "https://x/b.js")
    calls = []

    def fake_fetch_all(*_a, **_kw):
        calls.append(1)
        raise RuntimeError("Apple amp-api returned no tracks")

    monkeypatch.setattr(apple, "_fetch_all_tracks", fake_fetch_all)
    html = '<link rel="canonical" href="https://music.apple.com/gb/playlist/x/pl.abc">'

    with pytest.raises(RuntimeError, match="no tracks"):
        apple._fetch_via_api("https://music.apple.com/gb/playlist/x/pl.abc", html, "X")
    assert len(calls) == 1
