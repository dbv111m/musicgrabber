import preview_snippets as snippets


def setup_function():
    for token in list(snippets._snippets):
        snippets._discard_locked(token)


def test_stored_bytes_round_trip_to_their_owner():
    """MP3Phoenix hands us audio the browser could already play; the only reason it
    comes through us at all is that Cloudflare will not serve the browser directly."""
    user = {"id": "u1", "username": "karl"}

    token = snippets.store_snippet(b"ID3already-playable", user)

    assert snippets.snippet_user(token) == user
    assert snippets.snippet_path(token).read_bytes() == b"ID3already-playable"


def test_unknown_token_resolves_to_nothing():
    assert snippets.snippet_user("not-a-real-token") is None
    assert snippets.snippet_path("not-a-real-token") is None


def test_expired_snippet_is_refused_and_cleaned_up(monkeypatch):
    token = snippets.store_snippet(b"ID3fake", {"id": "u1"})
    path = snippets.snippet_path(token)

    # Wind the clock past the TTL rather than actually waiting ten minutes.
    monkeypatch.setattr(
        snippets.time, "time",
        lambda: snippets._snippets[token]["created_at"] + snippets.PREVIEW_SNIPPET_TTL + 1,
    )

    assert snippets.snippet_user(token) is None
    assert not path.exists()


def test_cache_stays_under_its_cap():
    made = [
        snippets.store_snippet(b"ID3fake", {"id": "u1"})
        for _ in range(snippets.PREVIEW_SNIPPET_MAX_CACHED + 5)
    ]

    assert len(snippets._snippets) <= snippets.PREVIEW_SNIPPET_MAX_CACHED
    assert snippets.snippet_path(made[-1]) is not None  # newest survives
    assert snippets.snippet_path(made[0]) is None       # oldest evicted
