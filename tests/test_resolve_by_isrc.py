"""
Unit tests for monochrome.resolve_by_isrc.

Fast, no-network tests: _tracks_isrc_lookup is mocked so nothing hits
Monochrome's tracks API. We verify the happy path, the caller-hint field
fallback, the validation short-circuit on a malformed ISRC, a clean miss, and
the deliberately-conservative transport-failure behaviour.
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _import_monochrome_or_skip():
    try:
        import monochrome
    except ModuleNotFoundError as exc:
        pytest.skip(f"monochrome module dependencies unavailable: {exc.name}")
    return monochrome


# A well-formed ISRC: Radiohead - Creep, reliably in the catalogue.
_VALID_ISRC = "GBAYE9200070"


def _tracks_item(title="Creep", artists=("Radiohead",), track_id="154049681742106624"):
    """A minimal tracks API entry shaped like find_track output."""
    return {
        "id": track_id,
        "trackId": track_id,
        "isrc": _VALID_ISRC,
        "title": title,
        "artistNames": list(artists),
        "duration": 238640,
        "artwork": "https://tracks.test/proxy/mi/cover.jpg",
        "playable": True,
    }


# ---------------------------------------------------------------------------
# 1. Valid ISRC, stocked -> an identity-pinned LOSSLESS result
# ---------------------------------------------------------------------------

def test_stocked_isrc_returns_pinned_lossless_result():
    monochrome = _import_monochrome_or_skip()

    with patch.object(monochrome, "_tracks_isrc_lookup",
                      return_value=(_tracks_item(), False)) as mock_lookup:
        result = monochrome.resolve_by_isrc(_VALID_ISRC, "Radiohead", "Creep")

    assert result is not None
    # The tracks API serves 16/44.1 and nothing else, so that is the only label.
    assert result["quality"] == "LOSSLESS"
    assert result["source"] == "monochrome"
    assert result["source_url"].startswith("monochrome://154049681742106624?")
    assert _VALID_ISRC in result["source_url"]
    assert "src=tracks" in result["source_url"]
    assert "via=isrc-direct" in result["score_breakdown"]
    assert result["duration"] == "3:59"
    assert result["thumbnail"] == "https://tracks.test/proxy/mi/cover.jpg"
    # Direct identity-pinned pick: flat 1000 plus the lossless bonus.
    assert result["relevance_score"] == 1000 + monochrome._QUALITY_BONUS["LOSSLESS"]
    mock_lookup.assert_called_once_with(_VALID_ISRC, "Radiohead", "Creep")


def test_primary_artist_is_the_first_listed():
    """Features already sit in the title; the channel is the lead artist."""
    monochrome = _import_monochrome_or_skip()
    item = _tracks_item(title="Get Lucky (feat. Pharrell Williams)",
                        artists=("Daft Punk", "Pharrell Williams"))

    with patch.object(monochrome, "_tracks_isrc_lookup", return_value=(item, False)):
        result = monochrome.resolve_by_isrc(_VALID_ISRC, "Daft Punk", "Get Lucky")

    assert result["channel"] == "Daft Punk"


# ---------------------------------------------------------------------------
# 2. Field fallback: missing title/artist -> use the caller's hints
# ---------------------------------------------------------------------------

def test_missing_catalogue_fields_fall_back_to_caller_hints():
    monochrome = _import_monochrome_or_skip()
    item = _tracks_item(title="", artists=())

    with patch.object(monochrome, "_tracks_isrc_lookup", return_value=(item, False)):
        result = monochrome.resolve_by_isrc(
            _VALID_ISRC, artist="Caller Artist", title="Caller Title")

    assert result is not None
    # title maps to "title", artist maps to "channel" in the result dict.
    assert result["title"] == "Caller Title"
    assert result["channel"] == "Caller Artist"


# ---------------------------------------------------------------------------
# 3. Malformed ISRC -> None, and the lookup is never attempted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_isrc", ["NOPE", "QT&JC2622262"])
def test_malformed_isrc_short_circuits_before_lookup(bad_isrc):
    monochrome = _import_monochrome_or_skip()

    with patch.object(monochrome, "_tracks_isrc_lookup") as mock_lookup:
        result = monochrome.resolve_by_isrc(bad_isrc, "Some Artist", "Some Title")

    assert result is None
    # Validation should short-circuit; the catalogue must not be touched.
    mock_lookup.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Clean miss: the tracks API answered, has nothing -> None
# ---------------------------------------------------------------------------

def test_clean_miss_returns_none():
    monochrome = _import_monochrome_or_skip()

    with patch.object(monochrome, "_tracks_isrc_lookup", return_value=(None, False)):
        result = monochrome.resolve_by_isrc(_VALID_ISRC, "Radiohead", "Creep")

    assert result is None


# ---------------------------------------------------------------------------
# 5. Tracks API transport failure -> None (conservative)
# ---------------------------------------------------------------------------

def test_transport_failure_returns_none():
    monochrome = _import_monochrome_or_skip()

    with patch.object(monochrome, "_tracks_isrc_lookup", return_value=(None, True)):
        result = monochrome.resolve_by_isrc(_VALID_ISRC, "Radiohead", "Creep")

    assert result is None


# ---------------------------------------------------------------------------
# 6. The real lookup: a missing title means nothing to search by
# ---------------------------------------------------------------------------

def test_no_title_is_a_clean_miss_without_a_search(monkeypatch):
    monochrome = _import_monochrome_or_skip()
    monkeypatch.setattr(
        "monochrome_tracks.search_tracks",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("nothing to search by")),
    )
    assert monochrome._tracks_isrc_lookup(_VALID_ISRC, "Radiohead", "") == (None, False)


def test_lookup_turns_an_outage_into_transport_failure(monkeypatch):
    import httpx
    monochrome = _import_monochrome_or_skip()
    monkeypatch.setattr(
        "monochrome_tracks.search_tracks",
        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")),
    )
    assert monochrome._tracks_isrc_lookup(_VALID_ISRC, "Radiohead", "Creep") == (None, True)
