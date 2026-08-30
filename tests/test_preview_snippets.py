import subprocess

import pytest

import preview_snippets as snippets


def setup_function():
    for token in list(snippets._snippets):
        snippets._discard_locked(token)


def _fake_ffmpeg(monkeypatch, *, returncode=0, write=b"ID3fake-audio-payload"):
    """Stand in for ffmpeg so the suite never goes near a CDN or a real decode."""
    def fake_run(cmd, **kwargs):
        if returncode == 0:
            with open(cmd[-1], "wb") as handle:  # ffmpeg's output path is the last arg
                handle.write(write)
        return subprocess.CompletedProcess(cmd, returncode, "", "ffmpeg said no")
    monkeypatch.setattr(snippets.subprocess, "run", fake_run)


def test_snippet_round_trips_to_its_owner(monkeypatch):
    _fake_ffmpeg(monkeypatch)
    user = {"id": "u1", "username": "karl"}

    token = snippets.build_snippet("https://cdn.test/a.mp4", "a1" * 16, user)

    assert snippets.snippet_user(token) == user
    assert snippets.snippet_path(token).exists()


def test_stored_bytes_round_trip_without_ffmpeg(monkeypatch):
    """MP3Phoenix hands us audio the browser could already play; the only reason it
    comes through us at all is that Cloudflare will not serve the browser directly."""
    def explode(*_args, **_kwargs):
        raise AssertionError("store_snippet has no business calling ffmpeg")
    monkeypatch.setattr(snippets.subprocess, "run", explode)
    user = {"id": "u1", "username": "karl"}

    token = snippets.store_snippet(b"ID3already-playable", user)

    assert snippets.snippet_user(token) == user
    assert snippets.snippet_path(token).read_bytes() == b"ID3already-playable"


def test_unknown_token_resolves_to_nothing():
    assert snippets.snippet_user("not-a-real-token") is None
    assert snippets.snippet_path("not-a-real-token") is None


def test_expired_snippet_is_refused_and_cleaned_up(monkeypatch):
    _fake_ffmpeg(monkeypatch)
    token = snippets.build_snippet("https://cdn.test/a.mp4", "a1" * 16, {"id": "u1"})
    path = snippets.snippet_path(token)

    # Wind the clock past the TTL rather than actually waiting ten minutes.
    monkeypatch.setattr(
        snippets.time, "time",
        lambda: snippets._snippets[token]["created_at"] + snippets.PREVIEW_SNIPPET_TTL + 1,
    )

    assert snippets.snippet_user(token) is None
    assert not path.exists()


def test_cache_stays_under_its_cap(monkeypatch):
    _fake_ffmpeg(monkeypatch)
    made = [
        snippets.build_snippet(f"https://cdn.test/{i}.mp4", "a1" * 16, {"id": "u1"})
        for i in range(snippets.PREVIEW_SNIPPET_MAX_CACHED + 5)
    ]

    assert len(snippets._snippets) <= snippets.PREVIEW_SNIPPET_MAX_CACHED
    assert snippets.snippet_path(made[-1]) is not None  # newest survives
    assert snippets.snippet_path(made[0]) is None       # oldest evicted


def test_failed_ffmpeg_leaves_nothing_behind(monkeypatch):
    _fake_ffmpeg(monkeypatch, returncode=1)

    with pytest.raises(RuntimeError, match="could not be prepared"):
        snippets.build_snippet("https://cdn.test/a.mp4", "a1" * 16, {"id": "u1"})

    assert snippets._snippets == {}


def test_key_is_passed_to_ffmpeg_but_never_lands_in_the_filename(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        with open(cmd[-1], "wb") as handle:
            handle.write(b"ID3fake")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(snippets.subprocess, "run", fake_run)
    token = snippets.build_snippet("https://cdn.test/a.mp4", "b2" * 16, {"id": "u1"})

    assert "-decryption_key" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("-decryption_key") + 1] == "b2" * 16
    assert "b2" * 16 not in str(snippets.snippet_path(token))
