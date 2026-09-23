"""Unit tests for the qbdlx direct-Qobuz fallback. No network: the token fetch
and the signed Qobuz call are both monkeypatched."""

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import qbdlx


_TOKEN = {"token": "T", "app_id": "798273057", "app_secret": "SEC", "country": "GB"}


def setup_function():
    qbdlx._token_cache = []
    qbdlx._token_cache_at = 0.0
    qbdlx._token_fetch_failed_at = 0.0
    qbdlx._good_token = None  # otherwise one test's favourite skews the next
    qbdlx._known_bad_tokens = set()
    # Stub the persistence hooks out entirely and pretend the restore already
    # ran, so the unit suite never touches a real database (db.py's connection
    # pool blocks for a good few seconds on an empty pool before it even gets
    # to failing -- exactly the sort of thing "no network" tests must dodge).
    # The persistence-specific tests below restore real hooks, against fakes,
    # never a real DB, to exercise that path deliberately.
    qbdlx._good_token_restore_attempted = True
    qbdlx._persist_good_token = lambda token: None
    qbdlx._load_persisted_good_token = lambda: None


def test_resolve_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(qbdlx, "qbdlx_enabled", lambda: False)
    assert qbdlx.resolve_qobuz_stream_url("GBAYE9200070", 6) is None


def test_resolve_returns_none_on_empty_isrc(monkeypatch):
    monkeypatch.setattr(qbdlx, "qbdlx_enabled", lambda: True)
    assert qbdlx.resolve_qobuz_stream_url("", 6) is None


def test_resolve_returns_none_when_pool_empty(monkeypatch):
    monkeypatch.setattr(qbdlx, "qbdlx_enabled", lambda: True)
    monkeypatch.setattr(qbdlx, "_fetch_shared_tokens", lambda force=False: [])
    assert qbdlx.resolve_qobuz_stream_url("GBAYE9200070", 6) is None


def test_resolve_signs_and_returns_url(monkeypatch):
    monkeypatch.setattr(qbdlx, "qbdlx_enabled", lambda: True)
    monkeypatch.setattr(qbdlx, "_fetch_shared_tokens", lambda force=False: [_TOKEN])

    calls = []

    def fake_signed_call(token, path, params, signed_concat=None):
        calls.append((path, params, signed_concat))
        if path == "catalog/search":
            return {"tracks": {"items": [{"id": 33933680, "isrc": "GBAYE9200070"}]}}
        if path == "track/getFileUrl":
            # The signed concat must match the verified scheme exactly.
            assert signed_concat == "trackgetFileUrlformat_id6intentstreamtrack_id33933680"
            return {"url": "https://streaming-qobuz-std.akamaized.net/file?x=1", "format_id": 6}
        return None

    monkeypatch.setattr(qbdlx, "_signed_call", fake_signed_call)

    url = qbdlx.resolve_qobuz_stream_url("GBAYE9200070", 6)
    assert url == "https://streaming-qobuz-std.akamaized.net/file?x=1"
    assert [c[0] for c in calls] == ["catalog/search", "track/getFileUrl"]


def test_resolve_falls_through_to_next_token(monkeypatch):
    monkeypatch.setattr(qbdlx, "qbdlx_enabled", lambda: True)
    bad = {"token": "B", "app_id": "1", "app_secret": "x", "country": "FR"}
    monkeypatch.setattr(qbdlx, "_fetch_shared_tokens", lambda force=False: [bad, _TOKEN])

    def fake_signed_call(token, path, params, signed_concat=None):
        if token["country"] == "FR":
            return None  # first token is a dud all round
        if path == "catalog/search":
            return {"tracks": {"items": [{"id": 1, "isrc": "GBAYE9200070"}]}}
        if path == "track/getFileUrl":
            return {"url": "https://cdn/ok.flac", "format_id": 6}
        return None

    monkeypatch.setattr(qbdlx, "_signed_call", fake_signed_call)
    assert qbdlx.resolve_qobuz_stream_url("GBAYE9200070", 6) == "https://cdn/ok.flac"


def test_resolve_skips_tokens_downgraded_to_a_sample(monkeypatch):
    monkeypatch.setattr(qbdlx, "qbdlx_enabled", lambda: True)
    sample_token = {"token": "S", "app_id": "1", "app_secret": "x", "country": "US"}
    monkeypatch.setattr(qbdlx, "_fetch_shared_tokens", lambda force=False: [sample_token, _TOKEN])

    def fake_signed_call(token, path, params, signed_concat=None):
        if path == "catalog/search":
            return {"tracks": {"items": [{"id": 33933680, "isrc": "GBAYE9200070"}]}}
        if path == "track/getFileUrl":
            if token["country"] == "US":
                # Downgraded entitlement: Qobuz hands back a 30-second preview
                # regardless of the requested format_id.
                return {"url": "https://cdn/preview.mp3", "format_id": 5, "sample": True}
            return {"url": "https://cdn/real.flac", "format_id": 6}
        return None

    monkeypatch.setattr(qbdlx, "_signed_call", fake_signed_call)
    assert qbdlx.resolve_qobuz_stream_url("GBAYE9200070", 6) == "https://cdn/real.flac"


def test_resolve_returns_none_when_every_token_only_offers_a_sample(monkeypatch):
    monkeypatch.setattr(qbdlx, "qbdlx_enabled", lambda: True)
    monkeypatch.setattr(qbdlx, "_fetch_shared_tokens", lambda force=False: [_TOKEN])

    def fake_signed_call(token, path, params, signed_concat=None):
        if path == "catalog/search":
            return {"tracks": {"items": [{"id": 33933680, "isrc": "GBAYE9200070"}]}}
        if path == "track/getFileUrl":
            return {"url": "https://cdn/preview.mp3", "format_id": 5, "sample": True}
        return None

    monkeypatch.setattr(qbdlx, "_signed_call", fake_signed_call)
    assert qbdlx.resolve_qobuz_stream_url("GBAYE9200070", 6) is None


def test_resolve_remembers_the_token_that_worked(monkeypatch):
    """The pool is mostly duds, so the winner goes to the front of the queue."""
    monkeypatch.setattr(qbdlx, "qbdlx_enabled", lambda: True)
    dud = {"token": "DUD", "app_id": "1", "app_secret": "x", "country": "US"}
    good = {"token": "GOOD", "app_id": "2", "app_secret": "y", "country": "FR"}
    monkeypatch.setattr(qbdlx, "_fetch_shared_tokens", lambda force=False: [dud, good])

    tried = []

    def fake_signed_call(token, path, params, signed_concat=None):
        if path == "catalog/search":
            tried.append(token["token"])
            return {"tracks": {"items": [{"id": 33933680, "isrc": "GBAYE9200070"}]}}
        if path == "track/getFileUrl":
            if token["token"] == "DUD":
                return {"url": "https://cdn/preview.mp3", "format_id": 5, "sample": True}
            return {"url": "https://cdn/real.flac", "format_id": 6}
        return None

    monkeypatch.setattr(qbdlx, "_signed_call", fake_signed_call)

    assert qbdlx.resolve_qobuz_stream_url("GBAYE9200070", 6) == "https://cdn/real.flac"
    assert tried == ["DUD", "GOOD"]  # cold start walks the pool

    tried.clear()
    assert qbdlx.resolve_qobuz_stream_url("GBAYE9200070", 6) == "https://cdn/real.flac"
    assert tried == ["GOOD"]  # second time we go straight to the winner


def test_resolve_falls_back_when_remembered_token_goes_stale(monkeypatch):
    """Tokens rot; a stale favourite must cost one attempt, not the whole result."""
    monkeypatch.setattr(qbdlx, "qbdlx_enabled", lambda: True)
    first = {"token": "FIRST", "app_id": "1", "app_secret": "x", "country": "FR"}
    second = {"token": "SECOND", "app_id": "2", "app_secret": "y", "country": "NL"}
    monkeypatch.setattr(qbdlx, "_fetch_shared_tokens", lambda force=False: [first, second])

    downgraded = {"yes": False}
    tried = []

    def fake_signed_call(token, path, params, signed_concat=None):
        if path == "catalog/search":
            tried.append(token["token"])
            return {"tracks": {"items": [{"id": 33933680, "isrc": "GBAYE9200070"}]}}
        if path == "track/getFileUrl":
            if token["token"] == "FIRST" and downgraded["yes"]:
                return {"url": "https://cdn/preview.mp3", "format_id": 5, "sample": True}
            if token["token"] == "FIRST":
                return {"url": "https://cdn/first.flac", "format_id": 6}
            return {"url": "https://cdn/second.flac", "format_id": 6}
        return None

    monkeypatch.setattr(qbdlx, "_signed_call", fake_signed_call)

    assert qbdlx.resolve_qobuz_stream_url("GBAYE9200070", 6) == "https://cdn/first.flac"
    assert qbdlx._good_token == "FIRST"

    downgraded["yes"] = True  # Qobuz quietly pulls FIRST's entitlement
    tried.clear()
    assert qbdlx.resolve_qobuz_stream_url("GBAYE9200070", 6) == "https://cdn/second.flac"
    assert tried == ["FIRST", "SECOND"]
    assert qbdlx._good_token == "SECOND"  # favourite moves on


def test_tokens_best_first_survives_a_rotated_pool(monkeypatch):
    """The upstream pool rotates; a favourite that's gone must not lose the rest."""
    qbdlx._good_token = "VANISHED"
    pool = [{"token": "A"}, {"token": "B"}]
    assert qbdlx._tokens_best_first(pool) == pool


def test_resolve_prefers_exact_isrc_match(monkeypatch):
    monkeypatch.setattr(qbdlx, "qbdlx_enabled", lambda: True)
    monkeypatch.setattr(qbdlx, "_fetch_shared_tokens", lambda force=False: [_TOKEN])

    captured = {}

    def fake_signed_call(token, path, params, signed_concat=None):
        if path == "catalog/search":
            return {"tracks": {"items": [
                {"id": 111, "isrc": "WRONGISRC0001"},
                {"id": 222, "isrc": "GBAYE9200070"},
            ]}}
        if path == "track/getFileUrl":
            captured["track_id"] = params["track_id"]
            return {"url": "https://cdn/match.flac", "format_id": 6}
        return None

    monkeypatch.setattr(qbdlx, "_signed_call", fake_signed_call)
    qbdlx.resolve_qobuz_stream_url("GBAYE9200070", 6)
    assert captured["track_id"] == 222


def test_resolve_qobuz_track_id_returns_first_working_token_match(monkeypatch):
    monkeypatch.setattr(qbdlx, "qbdlx_enabled", lambda: True)
    monkeypatch.setattr(qbdlx, "_fetch_shared_tokens", lambda force=False: [_TOKEN])

    def fake_signed_call(token, path, params, signed_concat=None):
        assert path == "catalog/search"
        return {"tracks": {"items": [
            {"id": 111, "isrc": "WRONGISRC0001"},
            {"id": 222, "isrc": "GBAYE9200070"},
        ]}}

    monkeypatch.setattr(qbdlx, "_signed_call", fake_signed_call)

    assert qbdlx.resolve_qobuz_track_id("GBAYE9200070") == 222


def test_lookup_qobuz_isrc_returns_exact_matches_and_clean_miss(monkeypatch):
    monkeypatch.setattr(qbdlx, "qbdlx_enabled", lambda: True)
    monkeypatch.setattr(qbdlx, "_fetch_shared_tokens", lambda force=False: [_TOKEN])
    monkeypatch.setattr(qbdlx, "_signed_call", lambda *a, **k: {
        "tracks": {"items": [
            {"id": 111, "isrc": "WRONGISRC0001"},
            {"id": 222, "isrc": "GBAYE9200070", "hires": True},
        ]}
    })

    matches, transport_failure = qbdlx.lookup_qobuz_isrc("GBAYE9200070")

    assert [item["id"] for item in matches] == [222]
    assert transport_failure is False


def test_lookup_qobuz_isrc_reports_transport_failure_when_no_token_answers(monkeypatch):
    monkeypatch.setattr(qbdlx, "qbdlx_enabled", lambda: True)
    monkeypatch.setattr(qbdlx, "_fetch_shared_tokens", lambda force=False: [_TOKEN])
    monkeypatch.setattr(qbdlx, "_signed_call", lambda *a, **k: None)

    assert qbdlx.lookup_qobuz_isrc("GBAYE9200070") == ([], True)


# ---------------------------------------------------------------------------
# Known-bad token tracking: a token that fails once (transport/auth failure,
# or a sample-only downgrade) shouldn't cost another timeout for the rest of
# a playlist. A token that simply doesn't have this ISRC is not the token's
# fault and must not be blamed for it.
# ---------------------------------------------------------------------------

def test_usable_tokens_excludes_known_bad_tokens():
    good = {"token": "GOOD"}
    bad = {"token": "BAD"}
    qbdlx._known_bad_tokens = {"BAD"}
    assert qbdlx._usable_tokens([bad, good]) == [good]


def test_usable_tokens_falls_back_to_full_pool_when_all_known_bad():
    a = {"token": "A"}
    b = {"token": "B"}
    qbdlx._known_bad_tokens = {"A", "B"}
    # Both are "known" dead, but trying them again beats returning nothing.
    assert qbdlx._usable_tokens([a, b]) == [a, b]


def test_resolve_marks_token_bad_on_call_failure_and_skips_it_next_time(monkeypatch):
    monkeypatch.setattr(qbdlx, "qbdlx_enabled", lambda: True)
    dead = {"token": "DEAD", "app_id": "1", "app_secret": "x", "country": "US"}
    monkeypatch.setattr(qbdlx, "_fetch_shared_tokens", lambda force=False: [dead, _TOKEN])

    attempted = []

    def fake_signed_call(token, path, params, signed_concat=None):
        if path == "catalog/search":
            attempted.append(token["token"])  # one entry per token actually tried
        if token["token"] == "DEAD":
            return None  # transport/auth failure -- the token's fault, not the ISRC's
        if path == "catalog/search":
            return {"tracks": {"items": [{"id": 33933680, "isrc": "GBAYE9200070"}]}}
        if path == "track/getFileUrl":
            return {"url": "https://cdn/real.flac", "format_id": 6}
        return None

    monkeypatch.setattr(qbdlx, "_signed_call", fake_signed_call)

    assert qbdlx.resolve_qobuz_stream_url("GBAYE9200070", 6) == "https://cdn/real.flac"
    assert attempted == ["DEAD", "T"]
    assert "DEAD" in qbdlx._known_bad_tokens

    attempted.clear()
    assert qbdlx.resolve_qobuz_stream_url("GBAYE9200070", 6) == "https://cdn/real.flac"
    assert attempted == ["T"]  # DEAD was already ruled out; no need to pay its timeout again


def test_resolve_does_not_mark_token_bad_for_a_clean_miss(monkeypatch):
    """A token with no results for this ISRC is a normal catalogue gap, not a fault."""
    monkeypatch.setattr(qbdlx, "qbdlx_enabled", lambda: True)
    monkeypatch.setattr(qbdlx, "_fetch_shared_tokens", lambda force=False: [_TOKEN])

    def fake_signed_call(token, path, params, signed_concat=None):
        assert path == "catalog/search"
        return {"tracks": {"items": []}}  # a real, successful "not found" answer

    monkeypatch.setattr(qbdlx, "_signed_call", fake_signed_call)

    assert qbdlx.resolve_qobuz_stream_url("GBAYE9200070", 6) is None
    assert qbdlx._known_bad_tokens == set()


def test_sample_downgrade_marks_token_bad(monkeypatch):
    monkeypatch.setattr(qbdlx, "qbdlx_enabled", lambda: True)
    sample_token = {"token": "S", "app_id": "1", "app_secret": "x", "country": "US"}
    monkeypatch.setattr(qbdlx, "_fetch_shared_tokens", lambda force=False: [sample_token, _TOKEN])

    def fake_signed_call(token, path, params, signed_concat=None):
        if path == "catalog/search":
            return {"tracks": {"items": [{"id": 33933680, "isrc": "GBAYE9200070"}]}}
        if path == "track/getFileUrl":
            if token["country"] == "US":
                return {"url": "https://cdn/preview.mp3", "format_id": 5, "sample": True}
            return {"url": "https://cdn/real.flac", "format_id": 6}
        return None

    monkeypatch.setattr(qbdlx, "_signed_call", fake_signed_call)

    assert qbdlx.resolve_qobuz_stream_url("GBAYE9200070", 6) == "https://cdn/real.flac"
    assert "S" in qbdlx._known_bad_tokens


def test_fetch_shared_tokens_resets_known_bad_on_fresh_pool(monkeypatch):
    qbdlx._known_bad_tokens = {"STALE"}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [dict(_TOKEN)]

    monkeypatch.setattr(qbdlx.httpx, "get", lambda *a, **k: _FakeResponse())

    tokens = qbdlx._fetch_shared_tokens(force=True)

    assert tokens == [_TOKEN]
    assert qbdlx._known_bad_tokens == set()


def _dead_host(calls):
    def fake_get(*_a, **_k):
        calls.append(1)
        raise qbdlx.httpx.ConnectTimeout("timed out")
    return fake_get


def test_dead_token_host_is_asked_once_not_once_per_caller(monkeypatch):
    """An album's fifteen ISRC lookups must not pay fifteen timeouts."""
    calls = []
    monkeypatch.setattr(qbdlx.httpx, "get", _dead_host(calls))

    for _ in range(15):
        assert qbdlx._fetch_shared_tokens() == []
    assert len(calls) == 1
    # And the lookup reports "could not verify", so the caller falls through.
    assert qbdlx.lookup_qobuz_isrc("GBAYE9200070") == ([], True)
    assert len(calls) == 1


def test_stale_pool_is_not_refetched_on_every_call_while_host_is_down(monkeypatch):
    qbdlx._token_cache = [dict(_TOKEN)]
    qbdlx._token_cache_at = 0.0  # long expired
    calls = []
    monkeypatch.setattr(qbdlx.httpx, "get", _dead_host(calls))

    for _ in range(5):
        assert qbdlx._fetch_shared_tokens() == [_TOKEN]
    assert len(calls) == 1


def test_force_ignores_the_failure_cooldown(monkeypatch):
    calls = []
    monkeypatch.setattr(qbdlx.httpx, "get", _dead_host(calls))
    qbdlx._fetch_shared_tokens()
    qbdlx._fetch_shared_tokens(force=True)
    assert len(calls) == 2


def test_cooldown_expires_and_a_recovered_host_is_used(monkeypatch):
    monkeypatch.setattr(qbdlx.httpx, "get", _dead_host([]))
    qbdlx._fetch_shared_tokens()

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [dict(_TOKEN)]

    monkeypatch.setattr(qbdlx.httpx, "get", lambda *a, **k: _FakeResponse())
    qbdlx._token_fetch_failed_at -= qbdlx.QBDLX_TOKEN_FAILURE_COOLDOWN + 1
    assert qbdlx._fetch_shared_tokens() == [_TOKEN]
    assert qbdlx._token_fetch_failed_at == 0.0


# ---------------------------------------------------------------------------
# Good-token persistence: survives a restart without the unit suite ever
# touching a real database (setup_function pins _good_token_restore_attempted
# to True; this test flips it back off to exercise the restore path itself).
# ---------------------------------------------------------------------------

def test_good_token_persists_round_trip(monkeypatch):
    store = {}
    monkeypatch.setattr(qbdlx, "_persist_good_token", lambda token: store.__setitem__("tok", token))
    monkeypatch.setattr(qbdlx, "_load_persisted_good_token", lambda: store.get("tok"))

    qbdlx._remember_good_token({"token": "PERSISTED"})
    assert store["tok"] == "PERSISTED"

    # Simulate a fresh process: no in-memory favourite yet, restore not yet attempted.
    qbdlx._good_token = None
    qbdlx._good_token_restore_attempted = False

    assert qbdlx._tokens_best_first([{"token": "X"}, {"token": "PERSISTED"}]) == [
        {"token": "PERSISTED"}, {"token": "X"},
    ]


def test_good_token_restore_only_hits_the_db_once_per_process(monkeypatch):
    qbdlx._good_token = None
    qbdlx._good_token_restore_attempted = False
    calls = []
    monkeypatch.setattr(qbdlx, "_load_persisted_good_token", lambda: calls.append(1) or None)

    qbdlx._tokens_best_first([])
    qbdlx._tokens_best_first([])

    assert len(calls) == 1


def test_good_token_restore_does_not_clobber_a_favourite_set_this_session(monkeypatch):
    qbdlx._good_token = "SET_THIS_SESSION"
    qbdlx._good_token_restore_attempted = False
    monkeypatch.setattr(qbdlx, "_load_persisted_good_token", lambda: "FROM_DB")

    qbdlx._ensure_good_token_restored()

    assert qbdlx._good_token == "SET_THIS_SESSION"


# ---------------------------------------------------------------------------
# Pool health visibility: a plain count of what we've learned this cycle,
# not a fresh sweep of the whole pool (that would just be hammering a shared
# free resource to produce a stat).
# ---------------------------------------------------------------------------

def test_pool_health_note_format():
    qbdlx._known_bad_tokens = {"B"}
    tokens = [{"token": "A"}, {"token": "B"}, {"token": "C"}]
    assert qbdlx._pool_health_note(tokens) == "2/3 shared tokens usable this cycle"


def test_pool_health_note_empty_pool():
    assert qbdlx._pool_health_note([]) == ""


def test_download_leg_healthy_reports_pool_note_when_healthy(monkeypatch):
    monkeypatch.setattr(qbdlx, "qbdlx_enabled", lambda: True)
    monkeypatch.setattr(qbdlx, "_fetch_shared_tokens", lambda force=False: [_TOKEN])
    monkeypatch.setattr(qbdlx, "resolve_qobuz_stream_url", lambda isrc, fmt: "https://cdn/creep.flac")

    ok, reason = qbdlx.download_leg_healthy()

    assert ok is True
    assert reason == "1/1 shared tokens usable this cycle"


def test_download_leg_healthy_reports_pool_note_when_unhealthy(monkeypatch):
    monkeypatch.setattr(qbdlx, "qbdlx_enabled", lambda: True)
    monkeypatch.setattr(qbdlx, "_fetch_shared_tokens", lambda force=False: [_TOKEN])
    monkeypatch.setattr(qbdlx, "resolve_qobuz_stream_url", lambda isrc, fmt: None)
    qbdlx._known_bad_tokens = {"T"}

    ok, reason = qbdlx.download_leg_healthy()

    assert ok is False
    assert reason == "qbdlx could not resolve a stream (0/1 shared tokens usable this cycle)"
