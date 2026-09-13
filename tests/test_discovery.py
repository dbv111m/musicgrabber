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
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discovery
import db


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
            related=[_artist("The Qemists", 300, id=1), _artist("Camo & Krooked", 200, id=2),
                     _artist("Sub Focus", 100, id=3)])

    assert _names(discovery.similar_artists("Pendulum")) == [
        "The Qemists", "Camo & Krooked", "Sub Focus"]


def test_the_artist_you_asked_about_is_not_a_discovery(monkeypatch):
    _deezer(monkeypatch,
            search=[_artist("Orbital", 5000, id=9)],
            related=[_artist("Orbital", 200, id=9), _artist("The Orb", 100, id=10)])

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
            related=[_artist("Netsky", 100)])
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
            related=[_artist("Nirvana", 400, id=2), _artist("Life of Agony", 300, id=3),
                     _artist("Faith No More", 200, id=4), _artist("L7", 100, id=5)],
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
            related=[_artist("Nirvana", 200, id=2), _artist("L7", 100, id=3)],
            owned=None)

    assert _names(discovery.similar_artists("Therapy?")) == ["Nirvana", "L7"]


def test_ownership_filtering_can_be_turned_off(monkeypatch):
    _deezer(monkeypatch,
            search=[_artist("Therapy?", 15418, id=1)],
            related=[_artist("Nirvana", 200, id=2), _artist("L7", 100, id=3)],
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
            related=[_artist("Pendulum", 200, id=2), _artist("Netsky", 100, id=3)],
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


# ---------------------------------------------------------------------------
# Taste seeds: Navidrome, then the library, then downloads, never blended
# ---------------------------------------------------------------------------

def _settings(values):
    """A fake get_setting that answers from a dict, falling back to the schema default.

    Real get_setting also consults env vars and the DB; taste_seeds only cares
    that it gets a string back for a given key, so this is all the tests need.
    """
    def fake(key, default="", user_id=None):
        return values.get(key, default)
    return fake


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    """Stands in for a sqlite3 connection; every query gets the same canned rows.

    Fine here because each test only ever runs one query through a given fake
    connection: the library-artist tally or the jobs-artist tally, never both.
    """
    def __init__(self, rows):
        self._rows = rows

    def execute(self, query, params=None):
        return _FakeCursor(self._rows)


def _fake_index_conn(rows):
    @contextmanager
    def _ctx(*, read_only=False):
        yield _FakeConn(rows)
    return _ctx


def _fake_db_conn(rows):
    @contextmanager
    def _ctx():
        yield _FakeConn(rows)
    return _ctx


def _broken_index_conn(*, read_only=False):
    raise RuntimeError("no library index in this test")


def _broken_db_conn():
    raise RuntimeError("no database in this test")


def test_navidrome_seeds_win_when_available(monkeypatch):
    """Navidrome, when configured at all, beats the library and downloads outright.

    Play counts are the cleanest weighted signal on offer (see
    docs/music-discovery-research.md section 9), so it goes first and, if it
    answers, nothing else gets a look-in.
    """
    monkeypatch.setattr(discovery, "get_setting", _settings({
        "navidrome_url": "http://nav.example",
        "navidrome_user": "karl",
        "navidrome_pass": "hunter2",
    }))
    payload = {
        "subsonic-response": {
            "status": "ok",
            "albumList2": {"album": [
                {"artist": "Pendulum", "playCount": 12},
                {"artist": "Pendulum", "playCount": 3},
                {"artist": "Various Artists", "playCount": 999},
                {"artist": "Chase & Status", "playCount": 5},
            ]},
        }
    }
    monkeypatch.setattr(discovery.httpx, "get", lambda *a, **k: _Resp(payload))

    seeds, signal = discovery.taste_seeds()

    assert signal == "navidrome"
    assert seeds[0] == {
        "artist": "Pendulum",
        "reason": "Because you listen to Pendulum",
        "weight": 15,
    }
    assert all(s["artist"] != "Various Artists" for s in seeds)


def test_falls_back_to_the_library_when_navidrome_is_not_configured(monkeypatch):
    monkeypatch.setattr(discovery, "get_setting", _settings({}))
    rows = [("Metallica", 200), ("Various Artists", 999), ("Green Day", 150)]
    monkeypatch.setattr(discovery.library_index, "index_conn", _fake_index_conn(rows))

    seeds, signal = discovery.taste_seeds()

    assert signal == "library"
    assert seeds[0] == {
        "artist": "Metallica",
        "reason": "Because your library is full of Metallica",
        "weight": 200,
    }
    assert all(s["artist"] != "Various Artists" for s in seeds)


def test_falls_back_to_downloads_when_neither_navidrome_nor_the_library_answer(monkeypatch):
    """The weakest signal, but a real one. On a real box `jawed`, a YouTube test
    video, topped this list, which is exactly why it is tried last."""
    monkeypatch.setattr(discovery, "get_setting", _settings({}))
    monkeypatch.setattr(discovery.library_index, "index_conn", _broken_index_conn)
    rows = [("jawed", 1078), ("Various Artists", 40), ("Test Artist", 12)]
    monkeypatch.setattr(db, "db_conn", _fake_db_conn(rows))

    seeds, signal = discovery.taste_seeds()

    assert signal == "downloads"
    assert seeds[0] == {
        "artist": "jawed",
        "reason": "Because you downloaded jawed",
        "weight": 1078,
    }
    assert all(s["artist"] != "Various Artists" for s in seeds)


def test_a_navidrome_error_falls_through_to_the_library_rather_than_giving_up(monkeypatch):
    """Rule 5 again: a dead Navidrome must not sink the library seed too."""
    monkeypatch.setattr(discovery, "get_setting", _settings({
        "navidrome_url": "http://nav.example",
        "navidrome_user": "karl",
        "navidrome_pass": "hunter2",
    }))

    def exploding_get(*a, **k):
        raise RuntimeError("Navidrome is having a day")

    monkeypatch.setattr(discovery.httpx, "get", exploding_get)
    monkeypatch.setattr(discovery.library_index, "index_conn",
                        _fake_index_conn([("Metallica", 200)]))

    seeds, signal = discovery.taste_seeds()

    assert signal == "library"
    assert seeds[0]["artist"] == "Metallica"


def test_a_totally_dead_set_of_sources_returns_the_none_signal(monkeypatch):
    monkeypatch.setattr(discovery, "get_setting", _settings({}))
    monkeypatch.setattr(discovery.library_index, "index_conn", _broken_index_conn)
    monkeypatch.setattr(db, "db_conn", _broken_db_conn)

    assert discovery.taste_seeds() == ([], "none")


# ---------------------------------------------------------------------------
# Discovery rows: one row of suggestions per seed
# ---------------------------------------------------------------------------

def test_discovery_rows_skips_a_seed_whose_suggestions_are_all_filtered_out(monkeypatch):
    """A seed `similar_artists` had nothing new to offer for should not become
    an empty row; it should simply not appear."""
    monkeypatch.setattr(discovery, "taste_seeds", lambda user_id=None, limit=12: (
        [
            {"artist": "Fully Owned Artist", "reason": "Because you listen to Fully Owned Artist", "weight": 10},
            {"artist": "Pendulum", "reason": "Because you listen to Pendulum", "weight": 5},
        ],
        "navidrome",
    ))

    def fake_similar(name, limit=25, *, user_id=None, exclude_owned=True):
        if name == "Fully Owned Artist":
            return []          # everything on offer was already in the library
        return [{"artist": "The Qemists", "deezer_id": 1, "picture": None}]

    monkeypatch.setattr(discovery, "similar_artists", fake_similar)

    rows, signal = discovery.discovery_rows(limit=5)

    assert signal == "navidrome"
    assert [r["seed"] for r in rows] == ["Pendulum"]
    assert rows[0]["reason"] == "Because you listen to Pendulum"
    assert rows[0]["artists"] == [{"artist": "The Qemists", "deezer_id": 1, "picture": None}]


def test_discovery_rows_honours_the_limit(monkeypatch):
    """Eight seeds all answer; only three rows should come back."""
    seeds = [
        {"artist": f"Artist {i}", "reason": f"Because you listen to Artist {i}", "weight": 10 - i}
        for i in range(8)
    ]
    monkeypatch.setattr(discovery, "taste_seeds", lambda user_id=None, limit=12: (seeds, "navidrome"))
    monkeypatch.setattr(
        discovery, "similar_artists",
        lambda name, limit=25, *, user_id=None, exclude_owned=True:
            [{"artist": f"{name} fan", "deezer_id": 1, "picture": None}],
    )

    rows, signal = discovery.discovery_rows(limit=3)

    assert signal == "navidrome"
    assert len(rows) == 3


def test_the_rows_report_where_their_seeds_came_from(monkeypatch):
    """The empty-page message depends on knowing this.

    "You have not listened to anything yet" and "we found nothing new for you"
    are different problems with different answers, and the caller cannot tell
    them apart from an empty list alone. Returning the signal alongside the rows
    also stops the route picking seeds twice, which meant two trips to Navidrome
    or the database for an answer it already had.
    """
    monkeypatch.setattr(discovery, "taste_seeds",
                        lambda user_id=None, limit=12: ([], "none"))

    rows, signal = discovery.discovery_rows()

    assert rows == []
    assert signal == "none"


def test_a_row_leads_with_the_artists_people_have_heard_of(monkeypatch):
    """Deezer's own order is neither similarity nor popularity.

    Asking it about Shaboozey really does return Gareth (971 fans) ahead of
    Jelly Roll (227,785), and Gareth's biggest track is a cover of somebody
    else's song off an album called "Covers (From Across The Pond)". A row is a
    shelf you scroll along, so whoever is on the far left decides whether anyone
    trusts the rest of it. The obscure ones are kept, just further along.
    """
    _deezer(monkeypatch,
            search=[_artist("Shaboozey", 50000, id=1)],
            related=[_artist("Gareth", 971, id=2),
                     _artist("Warren Zeiders", 32372, id=3),
                     _artist("Jelly Roll", 227785, id=4),
                     _artist("Josh Ross", 406, id=5)])

    assert _names(discovery.similar_artists("Shaboozey")) == [
        "Jelly Roll", "Warren Zeiders", "Gareth", "Josh Ross"]
