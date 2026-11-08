"""Strict artist matching for automatic downloads.

Album, bulk and watched downloads know who the track is by. These tests pin the
normaliser, the evidence ordering of the verdict (fingerprint, then duration,
then provider metadata, then free text), the candidate-ranking keyword filter,
and the rule that tags come from the request rather than the uploader.

The concrete cases come from a real library: 160 of 1,150 album grabs were
filed under the wrong artist, each in one of the shapes exercised below.
"""

import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import artist_match as am  # noqa: E402
from acquisition import rank_automatic_candidates  # noqa: E402

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Lynyrd Skynyrd - Topic", "Lynyrd Skynyrd"),
    ("ShinedownVEVO", "Shinedown"),
    ("Genesis Official", "Genesis"),
    ("Nirvana (Official)", "Nirvana"),
    ("Charlie Daniels", "Charlie Daniels"),
    ("Live", "Live"),  # never strip a name down to nothing
])
def test_strip_channel_branding(raw, expected):
    assert am.strip_channel_branding(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("The Righteous Brothers", "righteous brothers"),
    ("Simon & Garfunkel", "simon garfunkel"),
    ("Simon and Garfunkel", "simon garfunkel"),
    ("Mötley Crüe", "motley crue"),
    ("Rihanna feat. Jay-Z", "rihanna"),
    ("Ben E. King", "ben e king"),
])
def test_normalise_artist(raw, expected):
    assert am.normalise_artist(raw) == expected


def test_split_artist_credits_handles_every_separator():
    assert am.split_artist_credits("A & B, C feat. D x E") == ["a", "b", "c", "d", "e"]
    assert am.split_artist_credits("Various Artists") == []


# ---------------------------------------------------------------------------
# Name scoring
# ---------------------------------------------------------------------------

T = am.STRICT_ARTIST_MATCH_THRESHOLD


@pytest.mark.parametrize("expected,candidate", [
    ("Lynyrd Skynyrd", "Lynyrd Skynyrd - Topic"),
    ("Lynyrd Skynyrd", "lynyrd skynyrd"),
    ("Britney Spears", "BritneySpearsVEVO"),
    ("The Charlie Daniels Band", "Charlie Daniels"),
    ("Daryl Hall & John Oates", "Hall and Oates"),
    ("Rihanna", "Rihanna feat. Jay-Z"),          # legitimate collaboration
    ("Rihanna feat. Jay-Z", "Rihanna"),
    ("Kraftwerk", "Kraftwork"),                   # a typo, not another band
    ("Nirvana", "Nirvana (Official)"),
])
def test_same_act_scores_as_a_match(expected, candidate):
    assert am.artist_match_score(expected, candidate) >= T


@pytest.mark.parametrize("expected,candidate", [
    ("Lynyrd Skynyrd", "Shinedown"),
    ("Lynyrd Skynyrd", "Charlie Daniels"),
    ("Softy", "Genesis"),
    ("The Righteous Brothers", "The Unrighteous Brothers"),  # parody, not a typo
    ("The Righteous Brothers", "lightningrodrecords"),
    ("Ben E. King", "TJ DW"),
    ("Ben E. King", "Stand By Me"),                 # the title is not an artist
    ("DaBaby", "Baby Sound Lab"),
    ("Sleepermane", "2. Nightingale"),
    ("Genesis", "Genesis Owusu"),                   # one word inside a longer name
    ("Nirvana", "Nirvana (UK)"),                    # namesake qualifier
    ("Lynyrd Skynyrd", "Various Artists"),
    ("Lynyrd Skynyrd", ""),
])
def test_different_act_does_not_score_as_a_match(expected, candidate):
    assert am.artist_match_score(expected, candidate) < T


@pytest.mark.parametrize("expected,title,agrees", [
    ("Lynyrd Skynyrd", "Lynyrd Skynyrd - Simple Man (Official Audio)", True),
    ("Lynyrd Skynyrd", "Simple Man - Lynyrd Skynyrd (Lyrics)", True),
    ("Lynyrd Skynyrd", "[Classic Rock] Lynyrd Skynyrd - Simple Man", True),
    ("Lynyrd Skynyrd", "Simple Man by Lynyrd Skynyrd", True),
    ("Lynyrd Skynyrd", "Shinedown - Simple Man (Official Video)", False),
    ("Ben E. King", '"Stand By Me"', False),
    ("Sleepermane", "2. Nightingale", False),
    ("Bonnie Raitt", "\"I Can't Make You Love Me\"", False),
    ("The Righteous Brothers", "The Unrighteous Brothers - Unchained Melody", False),
])
def test_title_artist_mention(expected, title, agrees):
    assert (am.title_artist_mention_score(expected, title) >= T) is agrees


def test_channel_trust_prefers_the_artists_own_channels():
    assert am.channel_trust("Lynyrd Skynyrd", "Lynyrd Skynyrd - Topic") == 2
    assert am.channel_trust("Lynyrd Skynyrd", "LynyrdSkynyrdVEVO") == 1
    assert am.channel_trust("Lynyrd Skynyrd", "Shinedown - Topic") == 0
    assert am.channel_trust("Lynyrd Skynyrd", "Some Fan Channel") == 0


# ---------------------------------------------------------------------------
# The verdict, in trust order
# ---------------------------------------------------------------------------

def test_fingerprint_naming_another_artist_rejects_even_with_matching_uploader():
    """The Genesis case: AcoustID said Genesis, the album was Softy's."""
    verdict = am.evaluate_artist_match(
        "Softy", recording_artist="Genesis", uploader="Softy - Topic",
        actual_duration_secs=287, expected_duration_secs=287,
    )
    assert verdict.decision == "reject"
    assert "Genesis" in verdict.reason


def test_fingerprint_naming_the_artist_accepts_without_anything_else():
    verdict = am.evaluate_artist_match(
        "Lynyrd Skynyrd", recording_artist="Lynyrd Skynyrd", uploader="random reupload",
    )
    assert verdict.accepted


def test_fingerprint_verdict_applies_even_when_strict_is_off():
    verdict = am.evaluate_artist_match("Softy", recording_artist="Genesis", strict=False)
    assert verdict.decision == "reject"


def test_duration_far_off_rejects_without_fingerprint():
    """Shinedown's Simple Man is 256 s; MusicBrainz expects Skynyrd's at 357 s."""
    verdict = am.evaluate_artist_match(
        "Lynyrd Skynyrd", uploader="Shinedown", video_title="Shinedown - Simple Man",
        actual_duration_secs=256, expected_duration_secs=357,
    )
    assert verdict.decision == "reject"
    assert "duration" in verdict.reason


def test_duration_off_by_more_than_eight_seconds_rejects():
    verdict = am.evaluate_artist_match(
        "Lynyrd Skynyrd", uploader="Lynyrd Skynyrd - Topic",
        actual_duration_secs=370, expected_duration_secs=357,
    )
    assert verdict.decision == "reject"


def test_duration_match_plus_uploader_agreement_accepts():
    """"Lynyrd Skynyrd - Topic" uploading Simple Man at the right length passes."""
    verdict = am.evaluate_artist_match(
        "Lynyrd Skynyrd", uploader="Lynyrd Skynyrd - Topic",
        video_title="Simple Man", actual_duration_secs=360, expected_duration_secs=357,
    )
    assert verdict.accepted


def test_duration_match_plus_title_prefix_agreement_accepts():
    verdict = am.evaluate_artist_match(
        "Lynyrd Skynyrd", uploader="Classic Rock Uploads",
        video_title="Lynyrd Skynyrd - Simple Man (Official Audio)",
        actual_duration_secs=355, expected_duration_secs=357,
    )
    assert verdict.accepted


def test_duration_match_without_any_name_agreement_holds():
    """"TJ DW" posting "Stand By Me" at the right length is not Ben E. King yet."""
    verdict = am.evaluate_artist_match(
        "Ben E. King", uploader="TJ DW", video_title="Stand By Me",
        actual_duration_secs=178, expected_duration_secs=178,
    )
    assert verdict.decision == "hold"


def test_provider_metadata_is_strong_evidence_when_present():
    topic = am.evaluate_artist_match(
        "Lynyrd Skynyrd", provider_artist="Lynyrd Skynyrd", uploader="Lynyrd Skynyrd - Topic",
    )
    assert topic.accepted
    other = am.evaluate_artist_match("Lynyrd Skynyrd", provider_artist="Shinedown")
    assert other.decision == "reject"


def test_uploader_agreement_alone_holds_in_strict_mode_and_accepts_otherwise():
    strict = am.evaluate_artist_match("Lynyrd Skynyrd", uploader="Lynyrd Skynyrd - Topic")
    assert strict.decision == "hold"
    relaxed = am.evaluate_artist_match("Lynyrd Skynyrd", uploader="Lynyrd Skynyrd - Topic", strict=False)
    assert relaxed.accepted


def test_a_feat_collaboration_passes_on_fingerprint_and_on_text():
    by_fingerprint = am.evaluate_artist_match("Rihanna", recording_artist="Rihanna & JAY-Z")
    assert by_fingerprint.accepted
    by_text = am.evaluate_artist_match(
        "Rihanna feat. Jay-Z", uploader="RihannaVEVO",
        video_title="Rihanna - Umbrella ft. JAY-Z", actual_duration_secs=276, expected_duration_secs=275,
    )
    assert by_text.accepted


@pytest.mark.parametrize("expected,evidence", [
    # requested artist, then what the junk upload offered
    ("Lynyrd Skynyrd", dict(uploader="Shinedown", video_title="Shinedown - Simple Man",
                            actual_duration_secs=256, expected_duration_secs=357)),
    ("Lynyrd Skynyrd", dict(uploader="Charlie Daniels", video_title="Charlie Daniels - Simple Man",
                            actual_duration_secs=250, expected_duration_secs=357)),
    ("Softy", dict(recording_artist="Genesis", uploader="Genesis")),
    ("The Righteous Brothers", dict(uploader="lightningrodrecords",
                                    video_title="The Unrighteous Brothers - Unchained Melody")),
    ("Ben E. King", dict(uploader="TJ DW", video_title='"Stand By Me"')),
    ("DaBaby", dict(uploader="Baby Sound Lab", video_title="POP DAT THANG")),
    ("Bonnie Raitt", dict(uploader="someone", video_title="\"I Can't Make You Love Me\"")),
    ("Sleepermane", dict(uploader="lofi uploads", video_title="2. Nightingale")),
])
def test_every_observed_wrong_artist_grab_is_kept_out_of_the_album(expected, evidence):
    assert not am.evaluate_artist_match(expected, **evidence).accepted


def test_no_expected_artist_means_nothing_to_verify():
    assert am.evaluate_artist_match("", uploader="anyone").accepted


# ---------------------------------------------------------------------------
# Version keywords: the strict-mode default and the ranking filter
# ---------------------------------------------------------------------------

def test_effective_keywords_default_only_in_strict_mode_and_only_when_unset():
    assert am.effective_reject_keywords("", strict=True) == list(am.DEFAULT_REJECT_VERSION_KEYWORDS)
    assert am.effective_reject_keywords("", strict=False) == []
    assert am.effective_reject_keywords("demo, acoustic", strict=True) == ["demo", "acoustic"]


@pytest.mark.parametrize("candidate,requested,keyword", [
    ("Simple Man (Shinedown Cover)", "Simple Man", "cover"),
    ("Simple Man - Live at Knebworth", "Simple Man", "live"),
    ("Simple Man [Nightcore]", "Simple Man", "nightcore"),
    ("Simple Man (Acoustic)", "Simple Man (Acoustic)", None),   # asked for it
    ("Live and Let Die", "Live and Let Die", None),             # bare word, not an annotation
    ("Simple Man (Official Audio)", "Simple Man", None),
])
def test_unwanted_version_keyword(candidate, requested, keyword):
    keywords = list(am.DEFAULT_REJECT_VERSION_KEYWORDS)
    assert am.unwanted_version_keyword(candidate, requested, keywords) == keyword


def _candidate(video_id, title, channel, **extra):
    return {"video_id": video_id, "title": title, "channel": channel, "source": "youtube",
            "duration": "5:57", "relevance_score": 50, **extra}


def test_ranking_drops_annotated_versions_and_prefers_the_topic_channel():
    candidates = [
        _candidate("cover", "Simple Man (Cover)", "Some Guitarist"),
        _candidate("fan", "Lynyrd Skynyrd - Simple Man", "Classic Rock Uploads", relevance_score=90),
        _candidate("topic", "Simple Man", "Lynyrd Skynyrd - Topic"),
    ]
    ranked, rejected = rank_automatic_candidates(
        candidates, "Lynyrd Skynyrd", "Simple Man",
        reject_keywords=list(am.DEFAULT_REJECT_VERSION_KEYWORDS),
    )
    assert [c["video_id"] for c in rejected] == ["cover"]
    assert "reject_keyword='cover'" in rejected[0]["_acquisition_match_breakdown"]
    assert [c["video_id"] for c in ranked] == ["topic", "fan"]


def test_ranking_without_keywords_keeps_the_old_behaviour():
    candidates = [_candidate("cover", "Simple Man (Cover)", "Lynyrd Skynyrd")]
    ranked, rejected = rank_automatic_candidates(candidates, "Lynyrd Skynyrd", "Simple Man")
    # compute_match_confidence already demotes it as a wrong version; it is not
    # the keyword filter's doing, so the breakdown carries no keyword note.
    for cand in ranked + rejected:
        assert not any(b.startswith("reject_keyword") for b in cand["_acquisition_match_breakdown"])


# ---------------------------------------------------------------------------
# Tags come from the request. End to end through the real YouTube worker, with
# yt-dlp, MusicBrainz and the network stubbed out.
# ---------------------------------------------------------------------------

def _import_worker_or_skip():
    try:
        import db
        import downloads
    except ModuleNotFoundError as exc:  # pragma: no cover - env guard
        pytest.skip(f"worker dependencies unavailable: {exc.name}")
    return db, downloads


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    db, downloads = _import_worker_or_skip()
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "strict.db")
    monkeypatch.setattr(db, "_db_pool", queue.LifoQueue(maxsize=db._DB_POOL_SIZE))
    monkeypatch.setattr(db, "_db_pool_created", 0)
    monkeypatch.setattr(db, "_db_pool_lock", threading.Lock())
    db.init_db()
    music = tmp_path / "music"
    music.mkdir()
    monkeypatch.setenv("MUSIC_DIR", str(music))
    monkeypatch.setenv("ENABLE_DEEZER_METADATA", "false")
    import settings
    monkeypatch.setattr(settings, "TRASH_ROOT", tmp_path / "trash")
    monkeypatch.setattr(downloads, "get_album_art_context", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(downloads, "fetch_cover_art", lambda *args, **kwargs: None)
    monkeypatch.setattr(downloads, "fetch_lyrics", lambda *args, **kwargs: None)
    for name in ("trigger_navidrome_scan", "trigger_jellyfin_scan", "trigger_plex_scan",
                 "send_notification", "_apply_replaygain", "_apply_album_replaygain",
                 "_copy_to_auto_import", "_normalise_loudness"):
        monkeypatch.setattr(downloads, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(downloads, "check_duplicate", lambda *args, **kwargs: None)
    monkeypatch.setattr(downloads, "check_navidrome_duplicate", lambda *args, **kwargs: None)
    monkeypatch.setattr(downloads, "check_lidarr_duplicate", lambda *args, **kwargs: None)
    monkeypatch.setattr(downloads.library_index, "index_file", lambda *args, **kwargs: None)

    source = tmp_path / "source.flac"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=2", "-y", str(source)],
                   check=True, capture_output=True)

    def download_bytes(cmd, *args, **kwargs):
        output = cmd[cmd.index("-o") + 1].replace("%(ext)s", "flac")
        shutil.copyfile(source, output)
        return subprocess.CompletedProcess(cmd, 0, "", ""), False

    monkeypatch.setattr(downloads, "_run_ytdlp_with_retries", download_bytes)
    return db, downloads, music


def _queue_automatic_job(db, artist, title, album_dir=None):
    """A bulk-import style job: the request states artist and title up front."""
    from acquisition import (
        attach_job_to_acquisition,
        begin_acquisition_cycle,
        ensure_acquisition_target,
    )
    job_id = str(uuid.uuid4())[:8]
    with db.db_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id, video_id, title, artist, status, download_type, source, convert_audio, override_dir)"
            " VALUES (?, 'abcdefghijk', ?, ?, 'queued', 'single', 'youtube', 0, ?)",
            (job_id, title, artist, str(album_dir) if album_dir else None),
        )
        conn.commit()
    target = ensure_acquisition_target(
        owner_type="bulk_import", owner_key=f"test:{job_id}", mode="automatic",
        artist=artist, title=title, user_id=None,
    )
    cycle = begin_acquisition_cycle(target)
    attach_job_to_acquisition(job_id, target, cycle)
    return job_id


def _stub_ytdlp_info(monkeypatch, downloads, info):
    monkeypatch.setattr(
        downloads, "run_ytdlp_with_retries",
        lambda cmd, *args, **kwargs: (subprocess.CompletedProcess(cmd, 0, json.dumps(info), ""), False),
    )


def _job(db, job_id):
    with db.db_conn() as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def test_verified_download_is_tagged_from_the_request_not_the_uploader(worker_env, monkeypatch):
    from mutagen import File as MutagenFile
    db, downloads, music = worker_env
    job_id = _queue_automatic_job(db, "Ben E. King", "Stand By Me")
    _stub_ytdlp_info(monkeypatch, downloads, {"title": '"Stand By Me"', "channel": "TJ DW"})
    # MusicBrainz, anchored on the request: a text match with the expected length (the
    # 2-second test file has no real duration, so we stub the integrity probe too).
    monkeypatch.setattr(downloads, "lookup_metadata", lambda *a, **k: {
        "artist": "Ben E. King", "title": "Stand by Me", "album": "Don't Play That Song!",
        "year": "1962", "expected_duration_secs": 178.0, "metadata_source": "musicbrainz_text",
    })
    monkeypatch.setattr(downloads, "_validate_audio_integrity", lambda path: (True, "", 178.0))
    # The only agreement comes from the fingerprint, stubbed to the right artist.
    monkeypatch.setattr(downloads, "_artist_gate", lambda *a, **k: None)

    downloads.process_download(job_id, "abcdefghijk", convert_audio=False)

    job = _job(db, job_id)
    assert job["status"] == "completed", job["error"]
    tags = MutagenFile(job["final_path"], easy=True)
    assert tags["artist"] == ["Ben E. King"]
    assert tags["title"] == ["Stand By Me"]
    assert job["artist"] == "Ben E. King" and job["title"] == "Stand By Me"
    assert job["uploader"] == "TJ DW"


def test_unverified_download_is_trashed_logged_and_failed(worker_env, monkeypatch):
    db, downloads, music = worker_env
    album = music / "Albums" / "Ben E. King" / "Don't Play That Song!"
    album.mkdir(parents=True)
    job_id = _queue_automatic_job(db, "Ben E. King", "Stand By Me", album_dir=album)
    _stub_ytdlp_info(monkeypatch, downloads, {"title": '"Stand By Me"', "channel": "TJ DW"})
    monkeypatch.setattr(downloads, "lookup_metadata", lambda *a, **k: None)

    downloads.process_download(job_id, "abcdefghijk", convert_audio=False, override_dir=str(album))

    job = _job(db, job_id)
    assert job["status"] == "failed"
    assert job["error"].startswith("Artist unverified")
    assert not list(album.glob("*.flac")), "nothing may land in the album folder"
    with db.db_conn() as conn:
        rows = conn.execute("SELECT expected_artist, actual_artist FROM watched_match_mismatches WHERE job_id = ?",
                            (job_id,)).fetchall()
    assert rows == [("Ben E. King", "TJ DW")]


def test_fingerprint_contradiction_is_rejected_and_blacklisted(worker_env, monkeypatch):
    db, downloads, music = worker_env
    job_id = _queue_automatic_job(db, "Softy", "Undertow")
    _stub_ytdlp_info(monkeypatch, downloads, {"title": "Undertow", "channel": "Genesis"})

    def lookup(artist, title, path=None, identification=None):
        if identification is not None:
            identification.update({"artist": "Genesis", "title": "Undertow", "recording_id": "x",
                                   "fp_score": 0.97, "match_score": 9, "adopted": False})
        return None

    monkeypatch.setattr(downloads, "lookup_metadata", lookup)
    downloads.process_download(job_id, "abcdefghijk", convert_audio=False)

    job = _job(db, job_id)
    assert job["status"] == "failed"
    assert job["error"].startswith("Artist mismatch") and "Genesis" in job["error"]
    with db.db_conn() as conn:
        row = conn.execute("SELECT reason FROM blacklist WHERE video_id = 'abcdefghijk'").fetchone()
    assert row and row[0] == "wrong_artist"


def test_force_accepted_job_bypasses_the_gate(worker_env, monkeypatch):
    db, downloads, music = worker_env
    job_id = _queue_automatic_job(db, "Ben E. King", "Stand By Me")
    with db.db_conn() as conn:
        conn.execute("UPDATE jobs SET skip_mismatch_check = 1 WHERE id = ?", (job_id,))
        conn.commit()
    _stub_ytdlp_info(monkeypatch, downloads, {"title": '"Stand By Me"', "channel": "TJ DW"})
    monkeypatch.setattr(downloads, "lookup_metadata", lambda *a, **k: None)

    downloads.process_download(job_id, "abcdefghijk", convert_audio=False)

    job = _job(db, job_id)
    assert job["status"] == "completed", job["error"]
    assert job["artist"] == "Ben E. King"


def test_manual_pick_is_never_gated(worker_env, monkeypatch):
    """The user chose this result by hand; the gate has no opinion."""
    db, downloads, music = worker_env
    job_id = str(uuid.uuid4())[:8]
    with db.db_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id, video_id, title, artist, status, download_type, source, convert_audio)"
            " VALUES (?, 'abcdefghijk', 'Simple Man', 'Shinedown', 'queued', 'single', 'youtube', 0)",
            (job_id,),
        )
        conn.commit()
    _stub_ytdlp_info(monkeypatch, downloads, {"title": "Shinedown - Simple Man", "channel": "Shinedown"})
    monkeypatch.setattr(downloads, "lookup_metadata", lambda *a, **k: None)

    downloads.process_download(job_id, "abcdefghijk", convert_audio=False)

    assert _job(db, job_id)["status"] == "completed"
