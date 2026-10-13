"""The shared yt-dlp retry policy must be bounded and honest about absence."""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import youtube


def _result(returncode: int, stderr: str = "", stdout: str = ""):
    return subprocess.CompletedProcess(["yt-dlp"], returncode, stdout, stderr)


def _quiet_policy(monkeypatch):
    monkeypatch.setattr(youtube, "_sleep_if_botted", lambda: None)
    monkeypatch.setattr(youtube, "_note_bot_block", lambda: None)
    monkeypatch.setattr(youtube.time, "sleep", lambda _seconds: None)


def test_api_page_failure_is_transient_not_missing():
    assert youtube.classify_ytdlp_failure(
        "ERROR: Unable to download API page: HTTP Error 503"
    ) == "transient"


def test_transient_403_retries_then_succeeds(monkeypatch):
    _quiet_policy(monkeypatch)
    results = iter([
        _result(1, "HTTP Error 403: Forbidden"),
        _result(0, stdout="done"),
    ])
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return next(results)

    monkeypatch.setattr(youtube.subprocess, "run", fake_run)
    result, timed_out = youtube.run_ytdlp_with_retries(
        ["yt-dlp", "https://example.test/video"],
        10,
        max_retries=2,
        retry_delay=0,
        allow_cookie_fallback=False,
    )

    assert result.returncode == 0
    assert timed_out is False
    assert len(calls) == 2


def test_timeout_retries_then_succeeds(monkeypatch):
    _quiet_policy(monkeypatch)
    outcomes = iter([
        subprocess.TimeoutExpired(["yt-dlp"], 10),
        _result(0, stdout="done"),
    ])
    calls = 0

    def fake_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(youtube.subprocess, "run", fake_run)
    result, timed_out = youtube.run_ytdlp_with_retries(
        ["yt-dlp", "https://example.test/video"],
        10,
        max_retries=2,
        retry_delay=0,
        allow_cookie_fallback=False,
    )

    assert result.returncode == 0
    assert timed_out is False
    assert calls == 2


def test_private_or_deleted_media_is_not_retried(monkeypatch):
    _quiet_policy(monkeypatch)
    calls = 0

    def fake_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _result(1, "This video is private")

    monkeypatch.setattr(youtube.subprocess, "run", fake_run)
    result, timed_out = youtube.run_ytdlp_with_retries(
        ["yt-dlp", "https://example.test/private"],
        10,
        max_retries=4,
        retry_delay=0,
        allow_cookie_fallback=False,
    )

    assert result.returncode == 1
    assert timed_out is False
    assert calls == 1


def test_cookie_failure_gets_one_bounded_cookieless_cycle(monkeypatch):
    _quiet_policy(monkeypatch)
    calls = []
    outcomes = iter([
        _result(1, "Requested format is not available"),
        _result(0, stdout="done"),
    ])
    monkeypatch.setattr(youtube, "_note_cookie_failure", lambda: None)

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return next(outcomes)

    monkeypatch.setattr(youtube.subprocess, "run", fake_run)
    result, timed_out = youtube.run_ytdlp_with_retries(
        ["yt-dlp", "--cookies", "/tmp/cookies", "https://example.test/video"],
        10,
        max_retries=0,
        retry_delay=0,
    )

    assert result.returncode == 0
    assert timed_out is False
    assert "--cookies" in calls[0]
    assert "--cookies" not in calls[1]
    assert len(calls) == 2
