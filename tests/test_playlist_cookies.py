"""Playlist metadata must use the owner's cookies, with the shared fallback."""

import json
import os
import subprocess
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watched_playlists
import youtube


@pytest.mark.parametrize("user_id,personal", [("alice", "present"), ("alice", "empty"), ("alice", "missing"), (None, "missing")])
def test_youtube_playlist_cookie_selection(tmp_path, monkeypatch, user_id, personal):
    shared = tmp_path / "cookies.txt"
    shared.write_text("shared cookies")
    own = tmp_path / "cookies-alice.txt"
    if personal != "missing":
        own.write_text("personal cookies" if personal == "present" else "")
    monkeypatch.setattr(youtube, "COOKIES_FILE", shared)
    monkeypatch.setattr(youtube, "_cookies_allowed", lambda: True)
    calls = []

    def fake_run(cmd, timeout, **kwargs):
        calls.append(cmd)
        payload = json.dumps({"id": "video123", "title": "Blur - Song 2", "channel": "Blur", "playlist_title": "Favourites"})
        return subprocess.CompletedProcess(cmd, 0, payload, ""), False

    monkeypatch.setattr(watched_playlists, "run_ytdlp_with_retries", fake_run)
    tracks, name, warning, ids = watched_playlists.fetch_playlist_tracks(
        "https://www.youtube.com/playlist?list=PLexample", "youtube", user_id=user_id,
    )

    cmd = calls[0]
    expected = own if personal == "present" else shared
    assert cmd[cmd.index("--cookies") + 1] == str(expected)
    assert tracks == [("Blur", "Song 2")]
    assert (name, warning, ids) == ("Favourites", None, ["video123"])


# ---------------------------------------------------------------------------
# Personalised YouTube Music playlists (issue #95)
#
# YouTube serves these to anyone who asks, just with somebody else's songs in
# them. A wrong answer delivered confidently is worse than no answer, so these
# must never be enumerated anonymously.
# ---------------------------------------------------------------------------

PERSONALISED_URL = (
    "https://music.youtube.com/playlist?list=RDTMAK5uy_nGQKSMIkpr4o9VI_2i56pkGliD6FQRo50"
)


@pytest.mark.parametrize("playlist_id,expected", [
    ("RDTMAK5uy_nGQKSMIkpr4o9VI_2i56pkGliD6FQRo50", True),  # Discover Mix
    ("RDTMAK5uy_kset8DisdE7LSD4TNjEVvrKRTmG7a56sY", True),  # My Supermix
    ("LM", True),                                            # Liked Music
    ("WL", True),                                            # Watch Later
    ("RDCLAK5uy_kset8DisdE7LSD4TNjEVvrKRTmG7a56sY", False),  # editorial, same for all
    ("RDMMabcdef12345", False),                              # radio from a video
    ("PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI", False),           # ordinary playlist
])
def test_personalised_playlist_detection(playlist_id, expected):
    url = f"https://music.youtube.com/playlist?list={playlist_id}"
    assert youtube.is_personalised_youtube_playlist(url) is expected
    assert youtube.is_personalised_youtube_playlist("", playlist_id) is expected


def _cookie_jar(tmp_path, monkeypatch, *, populated=True):
    shared = tmp_path / "cookies.txt"
    if populated:
        shared.write_text("shared cookies")
    monkeypatch.setattr(youtube, "COOKIES_FILE", shared)
    return shared


def test_personalised_playlist_keeps_cookies_and_refuses_fallback(tmp_path, monkeypatch):
    shared = _cookie_jar(tmp_path, monkeypatch)
    # Cookies are in the sin bin after some unrelated failure elsewhere. A
    # personalised playlist must still use them rather than ask anonymously.
    monkeypatch.setattr(youtube, "_cookies_allowed", lambda: False)
    calls = []

    def fake_run(cmd, timeout, **kwargs):
        calls.append((cmd, kwargs))
        payload = json.dumps({
            "id": "vid1", "title": "Blur - Song 2", "channel": "Blur",
            "playlist_title": "Discover Mix",
        })
        return subprocess.CompletedProcess(cmd, 0, payload, ""), False

    monkeypatch.setattr(watched_playlists, "run_ytdlp_with_retries", fake_run)
    tracks, name, _warning, _ids = watched_playlists.fetch_playlist_tracks(
        PERSONALISED_URL, "youtube", user_id=None,
    )

    cmd, kwargs = calls[0]
    assert cmd[cmd.index("--cookies") + 1] == str(shared)
    assert kwargs["allow_cookie_fallback"] is False
    # The music.youtube.com URL survives intact; rewriting the host is one more
    # way to mislay the account context.
    assert cmd[-1] == PERSONALISED_URL
    assert tracks == [("Blur", "Song 2")] and name == "Discover Mix"


def test_personalised_playlist_without_cookies_refuses_to_guess(tmp_path, monkeypatch):
    _cookie_jar(tmp_path, monkeypatch, populated=False)
    monkeypatch.setattr(youtube, "_cookies_allowed", lambda: True)

    def explode(*_args, **_kwargs):
        raise AssertionError("yt-dlp must not run without cookies here")

    monkeypatch.setattr(watched_playlists, "run_ytdlp_with_retries", explode)

    with pytest.raises(HTTPException) as excinfo:
        watched_playlists.fetch_playlist_tracks(PERSONALISED_URL, "youtube", user_id=None)
    assert excinfo.value.status_code == 401
    assert "cookies" in excinfo.value.detail.lower()


def test_personalised_playlist_failure_is_an_auth_error(tmp_path, monkeypatch):
    _cookie_jar(tmp_path, monkeypatch)
    monkeypatch.setattr(youtube, "_cookies_allowed", lambda: True)

    def fake_run(cmd, timeout, **kwargs):
        stderr = "ERROR: This playlist does not exist or is private"
        return subprocess.CompletedProcess(cmd, 1, "", stderr), False

    monkeypatch.setattr(watched_playlists, "run_ytdlp_with_retries", fake_run)

    with pytest.raises(HTTPException) as excinfo:
        watched_playlists.fetch_playlist_tracks(PERSONALISED_URL, "youtube", user_id=None)
    # Not a 404. The playlist is fine; it is the session YouTube dislikes.
    assert excinfo.value.status_code == 401
    assert "expired" in excinfo.value.detail.lower()


def test_ordinary_playlist_still_allows_the_cookie_fallback(tmp_path, monkeypatch):
    _cookie_jar(tmp_path, monkeypatch)
    monkeypatch.setattr(youtube, "_cookies_allowed", lambda: True)
    calls = []

    def fake_run(cmd, timeout, **kwargs):
        calls.append((cmd, kwargs))
        payload = json.dumps({"id": "vid1", "title": "Blur - Song 2", "channel": "Blur"})
        return subprocess.CompletedProcess(cmd, 0, payload, ""), False

    monkeypatch.setattr(watched_playlists, "run_ytdlp_with_retries", fake_run)
    watched_playlists.fetch_playlist_tracks(
        "https://www.youtube.com/playlist?list=PLexample", "youtube", user_id=None,
    )
    assert calls[0][1]["allow_cookie_fallback"] is True
