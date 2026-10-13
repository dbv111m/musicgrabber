"""
Unit tests for metadata.verify_recording and its live-detection helpers.

These are pure unit tests; every MusicBrainz/network call is monkeypatched so
the suite is fast and offline. They are NOT marked slow.

The fixtures here are the acceptance bar for Task F of the ISRC-anchored album
download plan (see docs/isrc-anchored-album-download.md, the cross-cutting
live-detection rules). The headline guarantee: the bare word "live" is never a
trigger on its own. "Live and Let Die", "Live Forever", "Livin' on a Prayer"
and the band "Live" are all studio music and MUST be kept.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _import_metadata_or_skip():
    try:
        import metadata
    except ModuleNotFoundError as exc:  # pragma: no cover - env guard
        pytest.skip(f"metadata module dependencies unavailable: {exc.name}")
    return metadata


@pytest.fixture
def metadata(monkeypatch):
    """Import metadata with MusicBrainz disambiguation lookups stubbed out so no
    test ever reaches the network. Individual tests can re-patch
    `_lookup_recording_disambiguation` to feed a specific MB disambiguation."""
    md = _import_metadata_or_skip()
    # Default: MB knows nothing extra (no disambiguation). Tests that want a
    # live disambiguation override this.
    monkeypatch.setattr(md, "_lookup_recording_disambiguation", lambda rec_id: None)
    return md


# Convenience builders -------------------------------------------------------

def _result(title, recording_id="rec-matched", artist="Some Artist"):
    """Mimic the dict shape `_lookup_acoustid` returns."""
    return {
        "title": title,
        "artist": artist,
        "album": None,
        "year": None,
        "recording_id": recording_id,
    }


# ---- positional live-annotation detector -----------------------------------
# This is the heart of correctness: the word "live" must only count as a
# version annotation when positional, never as a bare substring.

def test_title_word_live_and_let_die_not_an_annotation(metadata):
    assert metadata._title_has_live_annotation("Live and Let Die") is False


def test_title_word_live_forever_not_an_annotation(metadata):
    assert metadata._title_has_live_annotation("Live Forever") is False


def test_substring_livin_on_a_prayer_not_an_annotation(metadata):
    assert metadata._title_has_live_annotation("Livin' on a Prayer") is False


def test_alive_is_not_live(metadata):
    assert metadata._title_has_live_annotation("Alive") is False
    assert metadata._title_has_live_annotation("Stayin' Alive") is False


def test_bracketed_live_is_an_annotation(metadata):
    assert metadata._title_has_live_annotation("Hysteria (Live)") is True
    assert metadata._title_has_live_annotation("Hysteria [Live]") is True


def test_bracketed_live_from_is_an_annotation(metadata):
    assert metadata._title_has_live_annotation(
        "Where the Streets Have No Name (Live from Slane Castle)"
    ) is True


def test_trailing_dash_live_is_an_annotation(metadata):
    assert metadata._title_has_live_annotation("Hysteria - Live") is True
    assert metadata._title_has_live_annotation("Hysteria - Live at the Apollo") is True


def test_live_at_phrase_anywhere_is_an_annotation(metadata):
    assert metadata._title_has_live_annotation("Song Title Live at Wembley") is True


def test_unplugged_bracket_is_an_annotation(metadata):
    assert metadata._title_has_live_annotation("Come as You Are (Unplugged)") is True


# ---- disambiguation helper -------------------------------------------------

def test_disambiguation_live_word_boundary(metadata):
    assert metadata._disambiguation_is_live("live, 2001-06-23: Slane Castle") is True
    assert metadata._disambiguation_is_live("alive remaster") is False
    assert metadata._disambiguation_is_live("") is False
    assert metadata._disambiguation_is_live(None) is False


# ---- verify_recording: the mandatory fixture table -------------------------

def test_live_and_let_die_kept_single_flow(metadata):
    # Wings, studio track with "live" as a title word. No expected MBID.
    verdict = metadata.verify_recording(_result("Live and Let Die"))
    assert verdict != "reject_live"
    assert verdict == "ok"


def test_live_forever_kept_single_flow(metadata):
    verdict = metadata.verify_recording(_result("Live Forever"))
    assert verdict == "ok"


def test_livin_on_a_prayer_kept_single_flow(metadata):
    verdict = metadata.verify_recording(_result("Livin' on a Prayer"))
    assert verdict == "ok"


def test_artist_named_live_kept(metadata):
    # "Lightning Crashes" by the band Live. The artist name carries "live" but
    # the title does not, and we never inspect the artist for live-ness.
    verdict = metadata.verify_recording(
        _result("Lightning Crashes", artist="Live")
    )
    assert verdict == "ok"


def test_everything_zen_expected_mbid_match_is_ok(metadata):
    # Album flow: AcoustID matched exactly the recording we asked for.
    verdict = metadata.verify_recording(
        _result("Everything Zen", recording_id="bush-everything-zen"),
        expected_recording_mbid="bush-everything-zen",
    )
    assert verdict == "ok"


def test_streets_live_from_slane_rejected(metadata):
    # Album flow, different recording, bracketed live annotation -> reject_live.
    verdict = metadata.verify_recording(
        _result(
            "Where the Streets Have No Name (Live from Slane Castle)",
            recording_id="u2-streets-live",
        ),
        expected_recording_mbid="u2-streets-studio",
    )
    assert verdict == "reject_live"


def test_hysteria_live_rejected_single_flow(metadata):
    # Single flow, no expected MBID, bracketed live annotation -> reject_live.
    verdict = metadata.verify_recording(_result("Hysteria (Live)"))
    assert verdict == "reject_live"


def test_nirvana_unplugged_query_keeps_live(metadata):
    # The user explicitly asked for the unplugged take, so a live/unplugged
    # recording is exactly what they wanted. Never reject.
    verdict = metadata.verify_recording(
        _result("Something in the Way (Unplugged)"),
        query="nirvana unplugged",
    )
    assert verdict != "reject_live"
    assert verdict == "ok"


def test_query_requests_live_keeps_live_album_flow(metadata):
    # Same exemption applies on the album flow: a query asking for live wins.
    verdict = metadata.verify_recording(
        _result("Hysteria (Live)", recording_id="def-leppard-live"),
        expected_recording_mbid="def-leppard-studio",
        query="def leppard hysteria live",
    )
    assert verdict != "reject_live"


def test_different_non_live_recording_high_confidence_is_reject_wrong(metadata):
    # Album flow, different MBID, not live, confident -> reject_wrong.
    verdict = metadata.verify_recording(
        _result("Everything Zen", recording_id="some-cover-version"),
        expected_recording_mbid="bush-everything-zen",
        high_confidence=True,
    )
    assert verdict == "reject_wrong"


def test_different_non_live_recording_low_confidence_is_uncertain(metadata):
    # Same as above but the identification is not confident -> uncertain (keep).
    # A remaster/reissue is a different MBID but still fine, so we only reject on
    # a confident signal.
    verdict = metadata.verify_recording(
        _result("Everything Zen", recording_id="remaster-version"),
        expected_recording_mbid="bush-everything-zen",
        high_confidence=False,
    )
    assert verdict == "uncertain"


# ---- the critical false-positive guards ------------------------------------

def test_no_acoustid_match_is_uncertain(metadata):
    assert metadata.verify_recording(None) == "uncertain"
    assert metadata.verify_recording(
        None, expected_recording_mbid="anything"
    ) == "uncertain"


def test_mb_disambiguation_live_triggers_reject_single_flow(metadata, monkeypatch):
    # No title annotation, but MB says the recording is live. MB is authoritative.
    monkeypatch.setattr(
        metadata, "_lookup_recording_disambiguation",
        lambda rec_id: "live, 2001-06-23: Slane Castle",
    )
    verdict = metadata.verify_recording(_result("Where the Streets Have No Name"))
    assert verdict == "reject_live"


def test_mb_disambiguation_non_live_does_not_trigger(metadata, monkeypatch):
    monkeypatch.setattr(
        metadata, "_lookup_recording_disambiguation",
        lambda rec_id: "2014 remaster",
    )
    verdict = metadata.verify_recording(_result("Some Studio Song"))
    assert verdict == "ok"


def test_exact_mbid_match_never_inspects_live_annotation(metadata, monkeypatch):
    # Identity beats strings: even if the title looks live and MB says live, an
    # exact MBID match is the recording we wanted. (Defensive: this should not
    # even reach the live check.)
    monkeypatch.setattr(
        metadata, "_lookup_recording_disambiguation",
        lambda rec_id: "live at wherever",
    )
    verdict = metadata.verify_recording(
        _result("Hysteria (Live)", recording_id="wanted-this-exact-one"),
        expected_recording_mbid="wanted-this-exact-one",
    )
    assert verdict == "ok"
