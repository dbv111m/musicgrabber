"""User-defined version rejection (GitLab #96).

"live" was the only thing you could refuse. Now you can refuse demos, acoustic
takes, radio edits and whatever remix has been foisted on you, with the same
paranoid matching discipline: only inside a version annotation, only on a
confident AcoustID identification, and never for something you asked for.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import downloads
import metadata


# ---------------------------------------------------------------------------
# Keyword parsing
# ---------------------------------------------------------------------------

def test_parse_reject_keywords_tidies_user_input():
    assert metadata.parse_reject_keywords(
        " Demo , acoustic,, RADIO   EDIT ,remix, demo "
    ) == ["demo", "acoustic", "radio edit", "remix"]


def test_parse_reject_keywords_handles_nothing_gracefully():
    assert metadata.parse_reject_keywords("") == []
    assert metadata.parse_reject_keywords(" , , ") == []


# ---------------------------------------------------------------------------
# Annotation matching. The whole point is what it does NOT match.
# ---------------------------------------------------------------------------

KEYWORDS = ["demo", "acoustic", "radio edit", "remix"]


@pytest.mark.parametrize("title,expected", [
    ("Song 2 (Acoustic)", "acoustic"),
    ("Song 2 [Demo]", "demo"),
    ("Song 2 - Radio Edit", "radio edit"),
    ("Titanium (Tiesto Remix)", "remix"),           # remixer name not needed
    ("Song 2 (2011 Remaster) [Demo]", "demo"),      # later segment still counts
])
def test_annotation_matches(title, expected):
    assert metadata.title_has_version_annotation(title, KEYWORDS) == expected


@pytest.mark.parametrize("title", [
    "Demolition Man",          # "demo" as a substring of a real word
    "Acoustic Alchemy Theme",  # keyword as part of the actual song name
    "Radio Ga Ga",
    "Remixology",
    "Song 2 (Live at Wembley)",  # live is the other setting's business
    "Song 2",
])
def test_annotation_leaves_honest_titles_alone(title):
    assert metadata.title_has_version_annotation(title, KEYWORDS) is None


def test_no_keywords_means_no_opinion():
    assert metadata.title_has_version_annotation("Song 2 (Demo)", []) is None


# ---------------------------------------------------------------------------
# The verdict helper
# ---------------------------------------------------------------------------

def _settings(monkeypatch, keywords):
    monkeypatch.setattr(
        downloads, "get_setting",
        lambda key, default="", **_kw: keywords if key == "reject_version_keywords" else default,
    )


def test_verdict_flags_an_unwanted_version(monkeypatch):
    _settings(monkeypatch, "demo, acoustic")
    verdict = downloads._unwanted_keyword_verdict(
        {"title": "Song 2 (Acoustic)"}, "Song 2", user_id=None
    )
    assert verdict == "acoustic"


def test_verdict_respects_what_the_user_actually_asked_for(monkeypatch):
    """Ask for the acoustic version and you get the acoustic version."""
    _settings(monkeypatch, "demo, acoustic")
    verdict = downloads._unwanted_keyword_verdict(
        {"title": "Song 2 (Acoustic)"}, "Song 2 (Acoustic)", user_id=None
    )
    assert verdict is None


def test_verdict_is_silent_when_the_setting_is_empty(monkeypatch):
    _settings(monkeypatch, "")
    assert downloads._unwanted_keyword_verdict(
        {"title": "Song 2 (Demo)"}, "Song 2", user_id=None
    ) is None


# ---------------------------------------------------------------------------
# The backstop itself
# ---------------------------------------------------------------------------

def _stub_rejection_plumbing(monkeypatch, keywords, *, reject_live=False):
    binned = []
    monkeypatch.setattr(
        downloads, "get_setting",
        lambda key, default="", **_kw: keywords if key == "reject_version_keywords" else default,
    )
    monkeypatch.setattr(
        downloads, "get_setting_bool",
        lambda key, default=False, **_kw: reject_live if key == "reject_live_versions" else default,
    )
    monkeypatch.setattr(downloads, "move_to_trash", lambda path, **_kw: binned.append(path))
    monkeypatch.setattr(downloads, "_note_blacklist_entry", lambda **_kw: None)
    return binned


def _reject(**overrides):
    kwargs = dict(
        artist="Blur", title="Song 2", video_id="vid1",
        source="youtube", user_id=None,
    )
    kwargs.update(overrides)
    return downloads._reject_if_unwanted_version(
        "job1", "/tmp/song.flac",
        {"title": "Song 2 (Demo)", "metadata_source": "acoustid_fingerprint"},
        **kwargs,
    )


def test_keyword_match_bins_the_file_and_names_the_keyword(monkeypatch):
    binned = _stub_rejection_plumbing(monkeypatch, "demo")
    reason = _reject()
    assert reason and "demo" in reason
    assert binned == ["/tmp/song.flac"]


def test_keyword_rejection_needs_a_fingerprint(monkeypatch):
    """Text-search metadata is a guess, and we do not bin files on a guess."""
    binned = _stub_rejection_plumbing(monkeypatch, "demo")
    reason = downloads._reject_if_unwanted_version(
        "job1", "/tmp/song.flac",
        {"title": "Song 2 (Demo)", "metadata_source": "musicbrainz_text"},
        artist="Blur", title="Song 2", video_id="vid1",
        source="youtube", user_id=None,
    )
    assert reason is None
    assert binned == []


def test_nothing_configured_means_nothing_happens(monkeypatch):
    binned = _stub_rejection_plumbing(monkeypatch, "")
    assert _reject() is None
    assert binned == []
