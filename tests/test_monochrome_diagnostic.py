"""Tests for the Settings "Test Downloads" Monochrome diagnostic.

The diagnostic is the thing users will lean on when Monochrome sulks, so it had
better be honest about which leg broke, and it had better not leave temp FLACs
scattered around the container when it does.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


TEST_RESULT = {
    "video_id": "mono_test",
    "title": "Creep",
    "channel": "Radiohead",
    "source": "monochrome",
    "source_url": "monochrome://12345?isrc=GBAYE9200070&quality=LOSSLESS&src=tidal",
    "quality": "LOSSLESS",
}


def _statuses(report):
    return {step["name"]: step["status"] for step in report["steps"]}


@pytest.fixture
def happy_monochrome(monkeypatch, tmp_path):
    """Stub every external leg so the diagnostic can run offline and pass."""
    import monochrome

    monkeypatch.setattr(monochrome, "monochrome_enabled", lambda: True)
    monkeypatch.setattr(
        "monochrome_tracks.health", lambda: (True, "Monochrome tracks API answering at https://tracks.example")
    )

    class FakeResponse:
        def raise_for_status(self):
            return None

    monkeypatch.setattr(monochrome.httpx, "get", lambda *a, **k: FakeResponse())
    monkeypatch.setattr(monochrome, "search_monochrome", lambda q, limit: [dict(TEST_RESULT)])
    monkeypatch.setattr(
        monochrome,
        "_resolve_monochrome_stream_url",
        lambda url, artist_hint="", title_hint="", trace=None, **kw: (
            trace.append({"leg": "Monochrome tracks API", "ok": True, "detail": "FLAC"})
            if trace is not None else None
        ) or "https://cdn.example/track.flac",
    )

    def fake_download(source_url, output_path, artist_hint="", title_hint=""):
        output_path.write_bytes(b"x" * 4096)

    monkeypatch.setattr(monochrome, "download_monochrome_track", fake_download)
    monkeypatch.setattr("downloads._validate_audio_integrity", lambda p: (True, "", 238.0))
    monkeypatch.setattr("downloads.probe_audio_quality", lambda p, source_info=None: ("FLAC 44.1kHz 16bit", 0))
    return monochrome


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_diagnostic_passes_when_every_leg_behaves(happy_monochrome):
    report = happy_monochrome.run_download_diagnostic()
    assert report["success"] is True
    assert report["hint"] == ""
    statuses = _statuses(report)
    assert statuses["Test track found"] == "ok"
    assert statuses["Audio downloaded"] == "ok"
    assert statuses["Audio verified"] == "ok"


def test_diagnostic_cleans_up_its_temp_file(happy_monochrome, monkeypatch):
    downloaded = []

    def capture(source_url, output_path, artist_hint="", title_hint=""):
        downloaded.append(output_path)
        output_path.write_bytes(b"x" * 4096)

    monkeypatch.setattr(happy_monochrome, "download_monochrome_track", capture)
    happy_monochrome.run_download_diagnostic()
    assert downloaded, "diagnostic never attempted a download"
    assert not downloaded[0].exists(), "diagnostic left its test download behind"


# ---------------------------------------------------------------------------
# Failures, which are the entire point of the exercise
# ---------------------------------------------------------------------------

def test_diagnostic_reports_the_leg_that_failed(happy_monochrome, monkeypatch):
    def resolve_with_dead_legs(url, artist_hint="", title_hint="", trace=None, **kw):
        if trace is not None:
            trace.append({
                "leg": "Monochrome tracks API",
                "ok": False,
                "detail": "no track with ISRC GBAYE9200070 in the catalogue",
            })
        raise RuntimeError("Monochrome: no stream available for ISRC GBAYE9200070")

    monkeypatch.setattr(happy_monochrome, "_resolve_monochrome_stream_url", resolve_with_dead_legs)
    report = happy_monochrome.run_download_diagnostic()

    assert report["success"] is False
    statuses = _statuses(report)
    assert statuses["Monochrome tracks API"] == "fail"
    assert statuses["Stream URL"] == "fail"
    # No point downloading anything once no leg produced a URL.
    assert "Audio downloaded" not in statuses


def test_diagnostic_points_at_the_address_override_when_tracks_api_is_down(happy_monochrome, monkeypatch):
    monkeypatch.setattr(
        "monochrome_tracks.health",
        lambda: (False, "Monochrome tracks API unreachable: HTTPStatusError: 503 Service Unavailable"),
    )

    def resolve_nothing(url, artist_hint="", title_hint="", trace=None, **kw):
        if trace is not None:
            trace.append({"leg": "Monochrome tracks API", "ok": False, "detail": "HTTPStatusError: 503"})
        return ""

    monkeypatch.setattr(happy_monochrome, "_resolve_monochrome_stream_url", resolve_nothing)
    report = happy_monochrome.run_download_diagnostic()
    assert report["success"] is False
    assert _statuses(report)["Tracks API reachable"] == "fail"
    assert "MONOCHROME_TRACKS_API_URL" in report["hint"]


def test_a_network_timeout_is_blamed_on_the_network(happy_monochrome, monkeypatch):
    """DNS failure and a slow upstream both mention timeouts; order matters."""
    def resolve_no_network(url, artist_hint="", title_hint="", trace=None, **kw):
        if trace is not None:
            trace.append({
                "leg": "Monochrome tracks API",
                "ok": False,
                "detail": "ConnectError: [Errno -2] Name or service not known (timed out)",
            })
        return ""

    monkeypatch.setattr(happy_monochrome, "_resolve_monochrome_stream_url", resolve_no_network)
    report = happy_monochrome.run_download_diagnostic()
    assert "DNS" in report["hint"]


def test_diagnostic_fails_loudly_when_the_bytes_are_not_audio(happy_monochrome, monkeypatch):
    monkeypatch.setattr("downloads._validate_audio_integrity", lambda p: (False, "No audio stream found", 0.0))
    report = happy_monochrome.run_download_diagnostic()
    assert report["success"] is False
    assert _statuses(report)["Audio verified"] == "fail"


def test_diagnostic_survives_a_download_that_explodes(happy_monochrome, monkeypatch):
    def boom(source_url, output_path, artist_hint="", title_hint=""):
        raise RuntimeError("Monochrome: download truncated for tidal/12345: got 10 of 4096 bytes")

    monkeypatch.setattr(happy_monochrome, "download_monochrome_track", boom)
    report = happy_monochrome.run_download_diagnostic()
    assert report["success"] is False
    assert _statuses(report)["Audio downloaded"] == "fail"


def test_diagnostic_warns_when_monochrome_is_switched_off(happy_monochrome, monkeypatch):
    monkeypatch.setattr(happy_monochrome, "monochrome_enabled", lambda: False)
    report = happy_monochrome.run_download_diagnostic()
    assert report["success"] is True  # the download path still works, it's just hidden from search
    assert _statuses(report)["Monochrome source"] == "warn"


def test_diagnostic_fails_cleanly_when_search_finds_nothing(happy_monochrome, monkeypatch):
    monkeypatch.setattr(happy_monochrome, "search_monochrome", lambda q, limit: [])
    report = happy_monochrome.run_download_diagnostic()
    assert report["success"] is False
    assert _statuses(report)["Test track found"] == "fail"


def test_diagnostic_refuses_to_run_twice_at_once(happy_monochrome, monkeypatch):
    """Second caller gets told to wait rather than pulling a second FLAC."""
    happy_monochrome._diagnostic_lock.acquire()
    try:
        report = happy_monochrome.run_download_diagnostic()
    finally:
        happy_monochrome._diagnostic_lock.release()
    assert report["success"] is False
    assert report.get("busy") is True
    assert report["steps"] == []


# ---------------------------------------------------------------------------
# The trace plumbing the diagnostic depends on
# ---------------------------------------------------------------------------

def test_resolver_records_each_leg_it_tries(monkeypatch):
    import monochrome

    monkeypatch.setattr(
        "monochrome_tracks.resolve_stream_url",
        lambda *a: (_ for _ in ()).throw(RuntimeError("tracks API not reachable")),
    )
    monkeypatch.setattr(monochrome, "_deezer_isrc_rescue", lambda *a: "GBNEW2500001")

    trace = []
    with pytest.raises(RuntimeError, match="no stream available"):
        monochrome._resolve_monochrome_stream_url(
            TEST_RESULT["source_url"], artist_hint="Radiohead", title_hint="Creep", trace=trace,
        )
    legs = [(entry["leg"], entry["ok"]) for entry in trace]
    assert legs == [
        ("Monochrome tracks API", False),
        ("Deezer ISRC rescue", True),
        ("Monochrome tracks API", False),
    ]


def test_resolver_trace_is_optional():
    """Everyday callers pass no trace and must not notice it exists."""
    import inspect
    import monochrome

    sig = inspect.signature(monochrome._resolve_monochrome_stream_url)
    assert sig.parameters["trace"].default is None
