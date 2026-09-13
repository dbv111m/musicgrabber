"""Discovery: does it find artists you do not already have, and the right ones?

The identity cases here are not invented. Every one is a real failure observed
against the live Deezer API while researching this, including the day discovery
recommended Spanish-language worship music because a compilation credit called
"Jay Burnett" was matched to "Marcos Brunet" on popularity alone.

HTTP is stubbed so the suite stays offline and deterministic, but the matching,
ranking and de-duplication are the real thing.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discovery


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def _deezer(monkeypatch, *, search=None, related=None, status=200):
    """Stand in for Deezer, recording what was asked for."""
    calls = []

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append(url)
        if "/search/artist" in url:
            return _Resp({"data": search or []}, status)
        if "/related" in url:
            return _Resp({"data": related or []}, status)
        return _Resp({}, 404)

    monkeypatch.setattr(discovery.httpx, "get", fake_get)
    monkeypatch.setattr(discovery, "_listenbrainz_related", lambda name: [])
    discovery.reset_cache()
    return calls


def _artist(name, fans, id=1):
    return {"id": id, "name": name, "nb_fan": fans}


# ---------------------------------------------------------------------------
# Identity: the bugs that made discovery embarrassing
# ---------------------------------------------------------------------------

def test_the_real_artist_wins_over_the_first_result(monkeypatch):
    """Deezer returns the first match, not the best one.

    Searching Deezer for "Pendulum" really does put an 18-fan artist with no
    related artists at all ahead of the 371,612-fan one.
    """
    _deezer(monkeypatch, search=[
        _artist("Pendulum", 18, id=76981412),
        _artist("Pendulum", 371612, id=281),
        _artist("Gaias Pendulum", 2271, id=582195),
    ])

    assert discovery.resolve_artist("Pendulum")["id"] == 281


def test_a_different_band_with_the_same_name_is_not_the_one_you_meant(monkeypatch):
    """Deezer offers "Nirvana (UK)", a sixties band with 222 fans, first."""
    _deezer(monkeypatch, search=[
        _artist("Nirvana (UK)", 222, id=1),
        _artist("Nirvana", 10067908, id=2),
    ])

    assert discovery.resolve_artist("Nirvana")["id"] == 2


def test_a_name_nobody_has_is_refused_rather_than_guessed(monkeypatch):
    """The worship-music incident, pinned down.

    "Jay Burnett" is a compilation producer with no Deezer presence. Picking the
    most popular near-miss gave Marcos Brunet, and from there an entire row of
    Spanish-language worship music off the back of one compilation credit.
    """
    _deezer(monkeypatch, search=[
        _artist("Marcos Brunet", 146114),
        _artist("Jay Bennett", 66),
        _artist("Haley Bennett", 239),
    ])

    assert discovery.resolve_artist("Jay Burnett") is None


def test_a_leading_the_does_not_make_it_a_different_band(monkeypatch):
    _deezer(monkeypatch, search=[_artist("Chemical Brothers", 1456602, id=7)])

    assert discovery.resolve_artist("The Chemical Brothers")["id"] == 7


def test_an_obscure_but_correct_match_beats_a_popular_wrong_one(monkeypatch):
    """Popularity only breaks ties between genuine matches; it never overrules."""
    _deezer(monkeypatch, search=[
        _artist("Gotan Project", 117850),
        _artist("PF Project", 144),
    ])

    assert discovery.resolve_artist("PF Project")["name"] == "PF Project"


def test_an_exact_name_beats_a_looser_one_however_popular(monkeypatch):
    """Popularity is the tie-break between equally good names, not a trump card.

    Both of these clear the name floor, so the floor cannot decide it: one is the
    name exactly, the other matches only as a token subset. Sorting on fans alone
    would take the loose match purely for being bigger, which is the same class of
    mistake as picking the first search hit.
    """
    _deezer(monkeypatch, search=[
        _artist("Chemical Brothers", 5_000_000, id=99),        # subset match
        _artist("The Chemical Brothers", 1_456_602, id=7),     # exact
    ])

    assert discovery.resolve_artist("The Chemical Brothers")["id"] == 7


def test_popularity_still_settles_it_between_equal_names(monkeypatch):
    _deezer(monkeypatch, search=[
        _artist("Pendulum", 18, id=76981412),
        _artist("Pendulum", 371612, id=281),
    ])

    assert discovery.resolve_artist("Pendulum")["id"] == 281


def test_nothing_at_all_is_an_answer(monkeypatch):
    _deezer(monkeypatch, search=[])
    assert discovery.resolve_artist("zzqqxx no such artist") is None
    assert discovery.resolve_artist("") is None
    assert discovery.resolve_artist(None) is None


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------

def test_similar_artists_returns_the_related_list(monkeypatch):
    _deezer(monkeypatch,
            search=[_artist("Pendulum", 371612, id=281)],
            related=[_artist("The Qemists", 1), _artist("Camo & Krooked", 2),
                     _artist("Sub Focus", 3)])

    assert discovery.similar_artists("Pendulum") == [
        "The Qemists", "Camo & Krooked", "Sub Focus"]


def test_the_artist_you_asked_about_is_not_a_discovery(monkeypatch):
    _deezer(monkeypatch,
            search=[_artist("Orbital", 5000, id=9)],
            related=[_artist("Orbital", 1), _artist("The Orb", 2)])

    assert discovery.similar_artists("Orbital") == ["The Orb"]


def test_listenbrainz_fills_in_when_deezer_has_nothing(monkeypatch):
    """The old source earns its keep on artists Deezer has never heard of."""
    _deezer(monkeypatch, search=[])
    monkeypatch.setattr(discovery, "_listenbrainz_related",
                        lambda name: ["Some Obscure Act", "Another One"])

    assert discovery.similar_artists("Whoever") == ["Some Obscure Act", "Another One"]


def test_deezer_answering_saves_troubling_musicbrainz(monkeypatch):
    """MusicBrainz asks for one request a second; do not spend that for nothing."""
    _deezer(monkeypatch,
            search=[_artist("Pendulum", 371612, id=281)],
            related=[_artist(f"Act {i}", i) for i in range(30)])
    called = []
    monkeypatch.setattr(discovery, "_listenbrainz_related",
                        lambda name: called.append(name) or [])

    discovery.similar_artists("Pendulum", limit=8)

    assert called == []


def test_duplicates_across_sources_appear_once(monkeypatch):
    _deezer(monkeypatch,
            search=[_artist("Pendulum", 371612, id=281)],
            related=[_artist("Netsky", 1)])
    monkeypatch.setattr(discovery, "_listenbrainz_related",
                        lambda name: ["netsky", "Sub Focus"])

    assert discovery.similar_artists("Pendulum", limit=10) == ["Netsky", "Sub Focus"]


def test_the_limit_is_honoured(monkeypatch):
    _deezer(monkeypatch,
            search=[_artist("X", 100, id=1)],
            related=[_artist(f"Act {i}", i) for i in range(20)])

    assert len(discovery.similar_artists("X", limit=5)) == 5


# ---------------------------------------------------------------------------
# Rule 4: discovery is a nicety and must never break the page
# ---------------------------------------------------------------------------

def test_an_upstream_on_fire_is_an_empty_list_not_an_exception(monkeypatch):
    def exploding_get(*a, **k):
        raise RuntimeError("Deezer is having a day")

    monkeypatch.setattr(discovery.httpx, "get", exploding_get)
    monkeypatch.setattr(discovery, "_listenbrainz_related", lambda name: [])
    discovery.reset_cache()

    assert discovery.resolve_artist("Pendulum") is None
    assert discovery.similar_artists("Pendulum") == []


def test_an_upstream_error_status_is_not_treated_as_data(monkeypatch):
    _deezer(monkeypatch, search=[_artist("X", 1)], status=503)
    assert discovery.resolve_artist("X") is None


def test_a_second_ask_does_not_hit_the_network_again(monkeypatch):
    """The whole point is clicking artist to artist; do not pay each time."""
    calls = _deezer(monkeypatch,
                    search=[_artist("Pendulum", 371612, id=281)],
                    related=[_artist("Netsky", 1)])

    discovery.similar_artists("Pendulum")
    after_first = len(calls)
    discovery.similar_artists("Pendulum")

    assert len(calls) == after_first
