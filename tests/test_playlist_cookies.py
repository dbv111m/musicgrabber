"""Playlist metadata must use the owner's cookies, with the shared fallback."""

import json
import os
import subprocess
import sys

import pytest

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
