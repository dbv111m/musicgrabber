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


def _deezer(monkeypatch, *, search=None, related=None, top=None, status=200, owned=None):
    """Stand in for Deezer and the library, recording what was asked for.

    `owned` is the set of artist identities the library holds; None means the
    index could not answer, which is a different thing entirely and is tested.
    """
    calls = []

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append(url)
        if "/search/artist" in url:
            return _Resp({"data": search or []}, status)
        if "/related" in url:
            return _Resp({"data": related or []}, status)
        if "/top" in url:
            return _Resp({"data": top or []}, status)
        return _Resp({}, 404)

    monkeypatch.setattr(discovery.httpx, "get", fake_get)
    monkeypatch.setattr(discovery, "_listenbrainz_related", lambda name: [])
    monkeypatch.setattr(discovery, "get_setting",
                        lambda key, default=None, user_id=None: "/music")
    monkeypatch.setattr(discovery.library_index, "known_artists", lambda root: owned)
    monkeypatch.setattr(discovery.library_index, "find_by_tags",
                        lambda root, artist, title: None)
    discovery.reset_cache()
    return calls


def _artist(name, fans, id=1):
    return {"id": id, "name": name, "nb_fan": fans, "picture_medium": f"http://img/{id}"}


def _names(entries):
    return [e["artist"] for e in entries]


def _track(title, id=1, preview="http://preview/x.mp3"):
    return {"id": id, "title": title, "preview": preview, "duration": 200,
            "album": {"cover_medium": "http://cover/x"}}


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

    assert _names(discovery.similar_artists("Pendulum")) == [
        "The Qemists", "Camo & Krooked", "Sub Focus"]


def test_the_artist_you_asked_about_is_not_a_discovery(monkeypatch):
    _deezer(monkeypatch,
            search=[_artist("Orbital", 5000, id=9)],
            related=[_artist("Orbital", 1), _artist("The Orb", 2)])

    assert _names(discovery.similar_artists("Orbital")) == ["The Orb"]


def test_listenbrainz_fills_in_when_deezer_has_nothing(monkeypatch):
    """The old source earns its keep on artists Deezer has never heard of."""
    _deezer(monkeypatch, search=[])
    monkeypatch.setattr(discovery, "_listenbrainz_related",
                        lambda name: [{"artist": "Some Obscure Act", "deezer_id": None, "picture": None},
                                      {"artist": "Another One", "deezer_id": None, "picture": None}])

    assert _names(discovery.similar_artists("Whoever")) == ["Some Obscure Act", "Another One"]


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
                        lambda name: [{"artist": "netsky", "deezer_id": None, "picture": None},
                                      {"artist": "Sub Focus", "deezer_id": None, "picture": None}])

    assert _names(discovery.similar_artists("Pendulum", limit=10)) == ["Netsky", "Sub Focus"]


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


# ---------------------------------------------------------------------------
# A suggestion you already own is not a discovery
# ---------------------------------------------------------------------------

def test_artists_already_in_the_library_are_not_offered_as_new(monkeypatch):
    """The complaint that prompted this: "I have tons of that already".

    Asking about Therapy? suggested Nirvana, Faith No More and The Smashing
    Pumpkins, all of which were sitting in the library. Measured across eight
    real seeds, 61% of raw suggestions were already owned, rising to 90% for
    Taylor Swift.
    """
    _deezer(monkeypatch,
            search=[_artist("Therapy?", 15418, id=1)],
            related=[_artist("Nirvana", 1, id=2), _artist("Life of Agony", 2, id=3),
                     _artist("Faith No More", 3, id=4), _artist("L7", 4, id=5)],
            owned={"nirvana", "faith no more"})

    assert _names(discovery.similar_artists("Therapy?")) == ["Life of Agony", "L7"]


def test_an_index_that_cannot_answer_does_not_empty_the_page(monkeypatch):
    """None is "I could not look", not "they own nothing".

    Treating the two the same would either suggest their whole collection back
    at them or, here, filter everything away and show a blank page. Showing a few
    familiar names is the better failure.
    """
    _deezer(monkeypatch,
            search=[_artist("Therapy?", 15418, id=1)],
            related=[_artist("Nirvana", 1, id=2), _artist("L7", 2, id=3)],
            owned=None)

    assert _names(discovery.similar_artists("Therapy?")) == ["Nirvana", "L7"]


def test_ownership_filtering_can_be_turned_off(monkeypatch):
    _deezer(monkeypatch,
            search=[_artist("Therapy?", 15418, id=1)],
            related=[_artist("Nirvana", 1, id=2), _artist("L7", 2, id=3)],
            owned={"nirvana"})

    assert _names(discovery.similar_artists("Therapy?", exclude_owned=False)) == ["Nirvana", "L7"]


def test_owning_a_collaboration_counts_as_owning_the_artist(monkeypatch):
    """The library tags a credit, not an identity.

    A library holding "Pendulum feat. Jasmine Yee" does own Pendulum, and
    `known_artists` expands credits to identities so this lines up. Real cases:
    Duke Dumont and The Qemists were both hiding inside collaborations.
    """
    _deezer(monkeypatch,
            search=[_artist("Sub Focus", 5000, id=1)],
            related=[_artist("Pendulum", 1, id=2), _artist("Netsky", 2, id=3)],
            owned={"pendulum"})     # as expanded from "Pendulum feat. Jasmine Yee"

    assert _names(discovery.similar_artists("Sub Focus")) == ["Netsky"]


# ---------------------------------------------------------------------------
# A track to introduce them by
# ---------------------------------------------------------------------------

def test_the_taster_skips_a_track_you_already_have(monkeypatch):
    """Not simply the biggest hit.

    The famous one is the one they are most likely to already own, and a track
    they own is a wasted introduction. Real case: Radiohead moves off "Creep",
    which was in the library, onto "Let Down", which was not.
    """
    _deezer(monkeypatch,
            search=[_artist("Radiohead", 900000, id=1)],
            top=[_track("Creep", id=10), _track("Let Down", id=11)])
    have = {"creep"}
    monkeypatch.setattr(discovery.library_index, "find_by_tags",
                        lambda root, artist, title: ["/x"] if title.casefold() in have else [])

    taster = discovery.artist_taster("Radiohead")

    assert taster["title"] == "Let Down"
    assert taster["preview_url"] == "http://preview/x.mp3"


def test_owning_the_whole_top_ten_still_gets_you_a_track(monkeypatch):
    """Karl owns all ten of Nirvana's top tracks. Verified, not assumed.

    At that point digging for an obscurity nobody chose is worse than saying
    plainly that they have it.
    """
    _deezer(monkeypatch,
            search=[_artist("Nirvana", 10067908, id=1)],
            top=[_track("Smells Like Teen Spirit", id=10), _track("Lithium", id=11)])
    monkeypatch.setattr(discovery.library_index, "find_by_tags",
                        lambda root, artist, title: ["/x"])

    taster = discovery.artist_taster("Nirvana")

    assert taster["title"] == "Smells Like Teen Spirit"
    assert taster["owned"] is True


def test_a_track_without_a_preview_is_no_use_for_hovering(monkeypatch):
    _deezer(monkeypatch,
            search=[_artist("X", 100, id=1)],
            top=[_track("Silent One", id=10, preview=""), _track("Audible One", id=11)])

    assert discovery.artist_taster("X")["title"] == "Audible One"


def test_a_known_deezer_id_skips_the_lookup(monkeypatch):
    """The rabbit hole already has the id; do not pay to find it again."""
    calls = _deezer(monkeypatch, top=[_track("Run You")])

    taster = discovery.artist_taster("The Qemists", deezer_id=4242)

    assert taster["title"] == "Run You"
    assert not any("/search/artist" in c for c in calls)


def test_an_artist_with_no_tracks_is_no_taster(monkeypatch):
    _deezer(monkeypatch, search=[_artist("X", 100, id=1)], top=[])
    assert discovery.artist_taster("X") is None


def test_a_taster_is_not_fetched_twice(monkeypatch):
    calls = _deezer(monkeypatch, top=[_track("Run You")])
    discovery.artist_taster("Q", deezer_id=7)
    first = len(calls)
    discovery.artist_taster("Q", deezer_id=7)
    assert len(calls) == first


def test_resolving_the_same_name_twice_does_not_ask_twice(monkeypatch):
    """The rabbit hole resolves the same artists over and over."""
    calls = _deezer(monkeypatch, search=[_artist("Pendulum", 371612, id=281)])
    discovery.resolve_artist("Pendulum")
    first = len(calls)
    discovery.resolve_artist("Pendulum")
    assert len(calls) == first


def test_a_name_that_resolves_to_nobody_is_remembered_as_such(monkeypatch):
    """"No such artist" is an answer worth caching too."""
    calls = _deezer(monkeypatch, search=[_artist("Marcos Brunet", 146114)])
    assert discovery.resolve_artist("Jay Burnett") is None
    first = len(calls)
    assert discovery.resolve_artist("Jay Burnett") is None
    assert len(calls) == first


def test_a_failing_upstream_is_not_cached_as_an_answer(monkeypatch):
    """A 503 is a bad moment, not a fact about the artist."""
    calls = _deezer(monkeypatch, search=[_artist("Pendulum", 371612, id=281)], status=503)
    discovery.resolve_artist("Pendulum")
    first = len(calls)
    discovery.resolve_artist("Pendulum")
    assert len(calls) > first, "a transient failure must not be remembered"
