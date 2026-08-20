"""
Regression tests for slskd search result scoring.

Reported by Syrius-consulting: slskd runs the search, slskd's own logs show it
completing with hundreds of files, and not one result ever reaches the UI. The
log line that gives it away:

    slskd search error: cannot access local variable 'adjusted_score'
    where it is not associated with a value

v3.1.0 renamed the running score from `adjusted_score` to `relevance_score` but
only at the point where it is first assigned and where it is read back out; the
five `+=` lines that stack the source-trust, lossless, hi-res, free-slot and
fast-uploader bonuses kept the old name. Python is perfectly happy with that
until the line actually runs, at which point it is an UnboundLocalError.

The blanket `except Exception` around the whole search then swallows it, so the
symptom is not a crash but an eerily empty result list: the very first candidate
that survives the quality and confidence filters takes the entire loop down with
it, before a single result has been appended.

That last detail is why this can hide in plain sight. A search where nothing
clears SLSKD_MATCH_CONFIDENCE_FLOOR never reaches the broken lines and looks
exactly like an honest "no matches".
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slskd


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeClient:
    """Just enough slskd API to walk search_slskd from end to end."""

    def __init__(self, responses):
        self._responses = responses

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def post(self, url, **_kwargs):
        return _FakeResponse({"id": "search-1"})

    def get(self, url, **_kwargs):
        if url.endswith("/responses"):
            return _FakeResponse(self._responses)
        return _FakeResponse({"fileCount": 1, "responseCount": 1, "isComplete": True})

    def delete(self, url, **_kwargs):
        return _FakeResponse({})


def _flac_hit(filename="Fleetwood Mac/Rumours/02 Dreams.flac"):
    """One good, unlocked, lossless file from a user with a slot free.

    Deliberately a strong match: it has to clear the quality and confidence
    filters, because those are exactly what used to shield the bug from view.
    """
    return {
        "username": "generous_stranger",
        "hasFreeUploadSlot": True,
        "uploadSpeed": 2_000_000,
        "files": [{
            "filename": filename,
            "size": 40_000_000,
            "length": 257,
            "isLocked": False,
            "bitRate": 1000,
            "bitDepth": 24,
            "sampleRate": 96000,
            "extension": "flac",
        }],
    }


@pytest.fixture
def slskd_ready(monkeypatch):
    """Point slskd at a fake server and stop the poll loop napping."""
    monkeypatch.setattr(slskd, "get_slskd_token", lambda: "token")
    monkeypatch.setattr(slskd, "get_setting", lambda *a, **k: "http://slskd.test")
    monkeypatch.setattr(slskd.time, "sleep", lambda _s: None)
    monkeypatch.setattr(slskd, "SLSKD_REQUIRE_FREE_SLOT", False)

    def _install(responses):
        monkeypatch.setattr(slskd.httpx, "Client", lambda **_kw: _FakeClient(responses))

    return _install


def test_a_good_match_actually_reaches_the_caller(slskd_ready, capsys):
    """The reported bug: hundreds of files in, nothing out."""
    slskd_ready([_flac_hit()])

    results = slskd.search_slskd("Fleetwood Mac - Dreams")

    assert results, (
        "slskd found a strong lossless match and returned nothing. "
        f"stderr: {capsys.readouterr().out}"
    )
    assert results[0]["source"] == "soulseek"
    assert results[0]["slskd_username"] == "generous_stranger"


def test_scoring_does_not_blow_up_on_an_unbound_name(slskd_ready, capsys):
    """Fail loudly on the specific fault rather than just on the empty list."""
    slskd_ready([_flac_hit()])

    slskd.search_slskd("Fleetwood Mac - Dreams")

    printed = capsys.readouterr().out
    assert "adjusted_score" not in printed, f"the old name is still referenced: {printed}"
    assert "slskd search error" not in printed, f"search raised: {printed}"


def test_the_quality_bonuses_actually_land_on_the_score(slskd_ready):
    """They were being added to a name nobody ever read, so they counted for nothing."""
    slskd_ready([_flac_hit()])

    results = slskd.search_slskd("Fleetwood Mac - Dreams")

    breakdown = results[0]["score_breakdown"]
    assert any("soulseek_trust" in note for note in breakdown)
    assert any("lossless" in note for note in breakdown)
    assert any("hires" in note for note in breakdown)
    # A bonus that appears in the breakdown but not in the number is just a lie
    # with extra steps, so check the total actually reflects them.
    claimed = slskd.SLSKD_SOURCE_TRUST_BONUS + slskd.SLSKD_LOSSLESS_BONUS + slskd.SLSKD_HIRES_BONUS
    assert results[0]["relevance_score"] > claimed


def test_a_lossy_hit_scores_below_an_otherwise_equal_lossless_one(slskd_ready):
    """The bonuses have to order results, not just decorate them."""
    lossy = _flac_hit("Fleetwood Mac/Rumours/02 Dreams.mp3")
    lossy["username"] = "mp3_merchant"
    lossy["files"][0].update(extension="mp3", bitRate=320, bitDepth=0, size=8_000_000)

    slskd_ready([_flac_hit(), lossy])
    results = slskd.search_slskd("Fleetwood Mac - Dreams")

    by_user = {r["slskd_username"]: r["relevance_score"] for r in results}
    assert by_user["generous_stranger"] > by_user["mp3_merchant"]


def test_peer_queue_depth_is_preserved_and_nudges_ranking_without_filtering(slskd_ready):
    free_peer = _flac_hit()
    free_peer["username"] = "free_peer"
    free_peer["queueLength"] = 0

    busy_peer = _flac_hit()
    busy_peer["username"] = "busy_peer"
    busy_peer["queueLength"] = 12

    slskd_ready([busy_peer, free_peer])
    results = slskd.search_slskd("Fleetwood Mac - Dreams")

    assert [result["slskd_username"] for result in results] == ["free_peer", "busy_peer"]
    assert {result["queue_length"] for result in results} == {0, 12}
    assert any("peer_queue=-" in note for note in results[1]["score_breakdown"])


def test_a_very_busy_peer_remains_available(slskd_ready):
    hit = _flac_hit()
    hit["queueLength"] = 10_000
    slskd_ready([hit])

    results = slskd.search_slskd("Fleetwood Mac - Dreams")

    assert results
    assert results[0]["queue_length"] == 10_000
    assert f"peer_queue=-{slskd.SLSKD_QUEUE_PENALTY_CAP}" in results[0]["score_breakdown"]


def test_an_empty_response_list_is_still_just_empty(slskd_ready, capsys):
    """The quiet case must stay quiet; no results is not an error."""
    slskd_ready([])

    assert slskd.search_slskd("Nobody - Nothing") == []
    assert "slskd search error" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Artist attribution
#
# Second report from Syrius-consulting: a Soulseek download is tagged with the
# *username of the person sharing it* rather than the artist, and then filed
# under that username in the library. The screenshot said it all:
#
#   2jqll9htuy62asp1wu - Ignorance
#   soulseek://2jqll9htuy62asp1wu/@@kvkwm\Music\Paramore\Brand New Eyes\2. Ignorance.flac
#
# Paramore is right there in the path. slskd works it out correctly; the loss
# happens later, where the API projects results for the browser and hardcodes
# artist to None, leaving the frontend to fall back to `channel`, which for
# Soulseek is the username. Longstanding, not a v4 regression: it was simply
# unreachable while slskd search itself was broken.
# ---------------------------------------------------------------------------

def test_the_artist_is_read_from_the_path_not_the_uploader():
    """The reported path, verbatim."""
    artist, title = slskd.extract_track_info_from_path(
        r"@@kvkwm\Music\Paramore\Brand New Eyes\2. Ignorance.flac"
    )

    assert artist == "Paramore"
    assert title == "Ignorance"


def test_search_results_carry_the_real_artist_separately_from_the_username(slskd_ready):
    """slskd keeps them apart; whoever consumes this must not conflate them."""
    hit = _flac_hit(r"@@kvkwm\Music\Paramore\Brand New Eyes\2. Ignorance.flac")
    hit["username"] = "2jqll9htuy62asp1wu"
    slskd_ready([hit])

    results = slskd.search_slskd("Paramore - Ignorance")

    assert results, "no results to check"
    assert results[0]["artist"] == "Paramore"
    assert results[0]["channel"] == "2jqll9htuy62asp1wu"
    assert results[0]["artist"] != results[0]["channel"]


def test_the_uploader_name_is_never_accepted_as_the_artist(monkeypatch):
    """Guard for retried jobs and any other caller that gets this wrong.

    Passing the peer's username in as the artist must not survive; the path
    knows better and should win.
    """
    import downloads

    seen = {}
    monkeypatch.setattr(downloads, "_job_was_cancelled", lambda _j: False)
    monkeypatch.setattr(downloads, "_get_job_album_context", lambda _j: {})
    monkeypatch.setattr(downloads, "_get_album_track_tag_context", lambda _j: (None, None))
    monkeypatch.setattr(downloads, "ensure_album_cover_files", lambda *a, **k: None)
    monkeypatch.setattr(downloads, "get_album_art_context", lambda *a, **k: (None, None))

    def _capture(job_id, **fields):
        seen.update(fields)
        # Stop the download dead once the artist has been settled; everything
        # after this point is network and filesystem, and not what we are here for.
        if "artist" in fields:
            raise RuntimeError("stop here")

    monkeypatch.setattr(downloads, "_update_job", _capture)

    downloads.process_slskd_download(
        job_id="job1",
        username="2jqll9htuy62asp1wu",
        filename=r"@@kvkwm\Music\Paramore\Brand New Eyes\2. Ignorance.flac",
        artist="2jqll9htuy62asp1wu",
        title="Ignorance",
    )

    assert seen.get("artist") == "Paramore", f"artist ended up as {seen.get('artist')!r}"
    assert seen.get("uploader") == "2jqll9htuy62asp1wu", "the peer is still recorded, just not as the artist"


@pytest.mark.parametrize("path_guess", ["Music (FLAC)", "complete", "Musique", "P"])
def test_embedded_artist_beats_common_soulseek_container_folders(monkeypatch, path_guess):
    """The four wrong live-search guesses that exposed this bug."""
    import downloads

    monkeypatch.setattr(downloads, "read_artist_title", lambda _path: ("Paramore", "Ignorance"))

    assert downloads._prefer_slskd_embedded_artist(
        downloads.Path("staged.flac"), path_guess
    ) == "Paramore"


@pytest.mark.parametrize("embedded", [None, "", "  ", "Unknown", "Unknown Artist"])
def test_unusable_embedded_artist_keeps_the_path_fallback(monkeypatch, embedded):
    import downloads

    monkeypatch.setattr(downloads, "read_artist_title", lambda _path: (embedded, "Ignorance"))

    assert downloads._prefer_slskd_embedded_artist(
        downloads.Path("staged.flac"), "Paramore"
    ) == "Paramore"


def test_embedded_artist_is_cleaned_before_it_reaches_tags_or_paths(monkeypatch):
    import downloads

    monkeypatch.setattr(
        downloads,
        "read_artist_title",
        lambda _path: ("  Paramore\x00\n  ", "Ignorance"),
    )

    assert downloads._prefer_slskd_embedded_artist(
        downloads.Path("staged.flac"), "Music (FLAC)"
    ) == "Paramore"


def test_real_flac_artist_tag_is_read_before_path_fallback(tmp_path):
    """Do not let a mocked tag reader make the central promise circular."""
    import downloads
    from mutagen.flac import FLAC

    audio_path = tmp_path / "Ignorance.flac"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.1",
            str(audio_path),
        ],
        check=True,
    )
    audio = FLAC(str(audio_path))
    audio["ARTIST"] = "Paramore"
    audio.save()

    assert downloads._prefer_slskd_embedded_artist(
        audio_path, "Music (FLAC)"
    ) == "Paramore"


def test_slskd_stages_file_then_routes_with_embedded_artist(monkeypatch, tmp_path):
    """A weak preflight miss is retried after embedded tags identify the artist."""
    import downloads

    staging = tmp_path / "staging"
    staging.mkdir()
    raw_file = staging / "2. Ignorance.flac"
    raw_file.write_bytes(b"audio-shaped test fixture")
    seen = {"job_updates": [], "duplicate_artists": []}

    monkeypatch.setattr(downloads, "_job_was_cancelled", lambda _j: False)
    monkeypatch.setattr(downloads, "_get_job_album_context", lambda _j: {})
    monkeypatch.setattr(downloads, "_get_album_track_tag_context", lambda _j: (None, None))
    monkeypatch.setattr(downloads, "ensure_album_cover_files", lambda *a, **k: None)
    monkeypatch.setattr(downloads, "get_album_art_context", lambda *a, **k: (None, None))

    def _check_duplicate(artist, _title, **_kwargs):
        seen["duplicate_artists"].append(artist)
        return None

    monkeypatch.setattr(downloads, "check_duplicate", _check_duplicate)
    monkeypatch.setattr(downloads, "check_navidrome_duplicate", lambda *a, **k: None)
    monkeypatch.setattr(downloads, "check_lidarr_duplicate", lambda *a, **k: None)
    monkeypatch.setattr(downloads, "get_setting_bool", lambda *a, **k: True)
    monkeypatch.setattr(downloads, "_playlist_album_tags", lambda *a, **k: (None, None, False))
    monkeypatch.setattr(downloads, "_make_staging_dir", lambda *a, **k: staging)
    monkeypatch.setattr(downloads, "_clear_staging_dir", lambda path: seen.update(cleaned=path))
    monkeypatch.setattr(downloads, "_validate_audio_integrity", lambda _p: (True, "", 219))
    monkeypatch.setattr(downloads, "read_artist_title", lambda _p: ("Paramore", "Ignorance"))
    monkeypatch.setattr(downloads, "send_notification", lambda **_k: None)

    def _download(_username, _filename, dest_dir, **_kwargs):
        seen["download_dest"] = dest_dir
        return raw_file

    def _route(artist, **_kwargs):
        seen["routed_artist"] = artist
        raise RuntimeError("stop after routing decision")

    monkeypatch.setattr(downloads, "download_from_slskd", _download)
    monkeypatch.setattr(downloads, "get_download_dir", _route)
    monkeypatch.setattr(
        downloads,
        "_update_job",
        lambda _job_id, **fields: seen["job_updates"].append(fields),
    )

    downloads.process_slskd_download(
        job_id="job1",
        username="helpful_peer",
        filename=r"Music (FLAC)\Paramore\Brand New Eyes\2. Ignorance.flac",
        artist="Music (FLAC)",
        title="Ignorance",
        slskd_size=40_000_000,
    )

    assert seen["download_dest"] == staging
    assert seen["duplicate_artists"] == ["Music (FLAC)", "Paramore"]
    assert seen["routed_artist"] == "Paramore"
    assert seen["cleaned"] == staging
    assert any(update.get("artist") == "Paramore" for update in seen["job_updates"])


def test_slskd_known_duplicate_is_skipped_before_transfer(monkeypatch, tmp_path):
    """Known playlist metadata must stop Soulseek before any peer transfer."""
    import downloads

    existing = tmp_path / "PinkPantheress - Stateside.opus"
    existing.write_bytes(b"already here")
    updates = []

    monkeypatch.setattr(downloads, "_job_was_cancelled", lambda _j: False)
    monkeypatch.setattr(downloads, "_get_job_album_context", lambda _j: {})
    monkeypatch.setattr(downloads, "_get_album_track_tag_context", lambda _j: (None, None))
    monkeypatch.setattr(downloads, "get_album_art_context", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(downloads, "ensure_album_cover_files", lambda *_a, **_k: None)
    monkeypatch.setattr(downloads, "_complete_if_existing_album_track", lambda *_a, **_k: False)
    monkeypatch.setattr(downloads, "get_setting_bool", lambda *a, **k: True)
    monkeypatch.setattr(downloads, "check_duplicate", lambda *a, **k: existing)
    monkeypatch.setattr(
        downloads,
        "check_navidrome_duplicate",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("Navidrome should not run after a local hit")
        ),
    )
    monkeypatch.setattr(
        downloads,
        "check_lidarr_duplicate",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("Lidarr should not run after a local hit")
        ),
    )
    monkeypatch.setattr(downloads, "_mark_watched_track_downloaded", lambda *a, **k: True)
    monkeypatch.setattr(downloads, "_update_job", lambda _job, **fields: updates.append(fields))
    monkeypatch.setattr(
        downloads,
        "download_from_slskd",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("duplicate track reached Soulseek transfer")
        ),
    )

    downloads.process_slskd_download(
        "job-1",
        "peer",
        "PinkPantheress/Stateside.flac",
        "PinkPantheress",
        "Stateside",
        slskd_size=123,
    )

    assert updates[-1]["status"] == "completed"
    assert updates[-1]["error"].startswith("Already exists:")


def test_album_mode_bypasses_soulseek_duplicate_checks(monkeypatch, tmp_path):
    """Album imports must not turn a fetched file into an ordinary-library dupe."""
    import downloads

    staging = tmp_path / "staging"
    staging.mkdir()
    raw_file = staging / "Track.flac"
    raw_file.write_bytes(b"audio-shaped test fixture")
    reached = []

    monkeypatch.setattr(downloads, "_job_was_cancelled", lambda _j: False)
    monkeypatch.setattr(
        downloads,
        "_get_job_album_context",
        lambda _j: {
            "override_dir": str(tmp_path / "Albums" / "Artist" / "Album"),
            "album_artist": "Artist",
            "album_name": "Album",
            "track_title": "Track",
        },
    )
    monkeypatch.setattr(downloads, "_get_album_track_tag_context", lambda _j: (1, 1))
    monkeypatch.setattr(downloads, "get_album_art_context", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(downloads, "ensure_album_cover_files", lambda *_a, **_k: None)
    monkeypatch.setattr(
        downloads,
        "_complete_if_existing_album_track",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("album-specific duplicate short-circuit ran")
        ),
    )
    for name in ("check_duplicate", "check_navidrome_duplicate", "check_lidarr_duplicate"):
        monkeypatch.setattr(
            downloads,
            name,
            lambda *_a, _name=name, **_k: (_ for _ in ()).throw(
                AssertionError(f"{_name} ran in album mode")
            ),
        )
    monkeypatch.setattr(downloads, "_make_staging_dir", lambda *_a, **_k: staging)
    monkeypatch.setattr(downloads, "_clear_staging_dir", lambda *_a, **_k: None)
    monkeypatch.setattr(downloads, "download_from_slskd", lambda *_a, **_k: raw_file)
    monkeypatch.setattr(downloads, "_validate_audio_integrity", lambda _p: (True, "", 180))
    monkeypatch.setattr(downloads, "_prefer_slskd_embedded_artist", lambda _p, artist: artist)
    monkeypatch.setattr(downloads, "_update_job", lambda *_a, **_k: None)
    monkeypatch.setattr(downloads, "send_notification", lambda **_k: None)

    def stop_after_duplicate_gate(*_args, **_kwargs):
        reached.append(True)
        raise RuntimeError("stop after duplicate gate")

    monkeypatch.setattr(downloads, "_playlist_album_tags", stop_after_duplicate_gate)

    downloads.process_slskd_download(
        "job-1",
        "peer",
        "Artist/Album/Track.flac",
        "Artist",
        "Track",
        override_dir=str(tmp_path / "Albums" / "Artist" / "Album"),
        slskd_size=123,
        skip_dupe_check=True,
    )

    assert reached == [True]


def test_album_mode_bypasses_preflight_album_duplicate_check(monkeypatch):
    """The common non-Soulseek path must honour the same album bypass."""
    import downloads

    monkeypatch.setattr(downloads, "_job_was_cancelled", lambda _j: False)
    monkeypatch.setattr(
        downloads,
        "_get_job_album_context",
        lambda _j: {"override_dir": "/music/Albums/Artist/Album"},
    )
    monkeypatch.setattr(downloads, "_get_album_track_tag_context", lambda _j: (1, 1))
    monkeypatch.setattr(downloads, "get_album_art_context", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(downloads, "ensure_album_cover_files", lambda *_a, **_k: None)
    monkeypatch.setattr(
        downloads,
        "_complete_if_existing_album_track",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("album-specific duplicate short-circuit ran")
        ),
    )
    monkeypatch.setattr(
        downloads,
        "_update_job",
        lambda *_a, **_k: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        downloads.process_download(
            "job-1",
            "abcdefghijk",
            override_dir="/music/Albums/Artist/Album",
            skip_dupe_check=True,
        )
