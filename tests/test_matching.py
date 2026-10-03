"""
Unit tests for the song matching engine.

These are pure unit tests, no running MusicGrabber needed; they live in tests/
so the existing run_tests.sh picks them up.
"""

import os
import sys

# Add project root so the matching module is importable when pytest is run
# from inside tests/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from matching import (
    clean_artist,
    clean_title,
    compute_match_confidence,
    core_string,
    duration_similarity,
    is_junk_artist,
    normalise_string,
    parse_query,
    query_requests_variant,
    score_track_against_filename,
    similarity,
    split_path_segments,
)


# ---- normalisation ---------------------------------------------------------

def test_normalise_folds_diacritics():
    assert normalise_string("Beyoncé") == "beyonce"
    assert normalise_string("Sigur Rós") == "sigur ros"


def test_normalise_collapses_separators():
    assert normalise_string("Pig&Dan") == "pig dan"
    assert normalise_string("AC/DC") == "ac dc"


def test_normalise_keeps_dollar():
    assert normalise_string("A$AP Rocky") == "a$ap rocky"


def test_normalise_drops_punctuation():
    assert normalise_string("Hello, World!") == "hello world"


def test_normalise_handles_latin_extended():
    # Ø, Æ, Þ, Ð and friends don't decompose via NFKD; they need an explicit
    # mapping so metal track names like ØUTSIDER stay searchable rather than
    # collapsing to UTSIDER and matching arbitrary nonsense.
    assert normalise_string("ØUTSIDER") == "outsider"
    assert normalise_string("Mötley Crüe") == "motley crue"
    assert normalise_string("Æquinox") == "aequinox"


def test_normalise_preserves_cjk():
    text = "命の灯火"
    assert normalise_string(text) == text


def test_core_string_strips_everything():
    assert core_string("Don't Stop Me Now") == "dontstopmenow"
    assert core_string("Don't Stop Me Now!") == "dontstopmenow"


# ---- clean_title / clean_artist -------------------------------------------

def test_clean_title_strips_feat():
    assert clean_title("Stay (feat. Justin Bieber)") == "stay"
    assert clean_title("Stay feat. Justin Bieber") == "stay"
    assert clean_title("Track (Explicit)") == "track"


def test_clean_title_keeps_remix_marker():
    # Critical: version info must survive cleaning so similarity() can detect it.
    assert "remix" in clean_title("Stay (Remix)")
    assert "live" in clean_title("Stay (Live at Wembley)")


def test_clean_artist_keeps_collaborator_separators():
    # "Daryl Hall & John Oates" must not become "Daryl Hall"
    assert "john oates" in clean_artist("Daryl Hall & John Oates")
    assert "blood" in clean_artist("Blood & Water")


def test_clean_artist_strips_feat():
    assert clean_artist("Drake feat. 21 Savage") == "drake"
    assert clean_artist("Drake ft 21 Savage") == "drake"


# ---- similarity -----------------------------------------------------------

def test_similarity_identical():
    assert similarity("hello world", "hello world") == 1.0


def test_similarity_empty():
    assert similarity("", "anything") == 0.0
    assert similarity("anything", "") == 0.0


def test_similarity_remix_suffix_heavy_penalty():
    # "Stay" vs "Stay Remix" must score below the 0.55 confidence floor
    # so the original mix wins over remix candidates.
    score = similarity("stay", "stay remix")
    assert score <= 0.30


def test_similarity_live_suffix_heavy_penalty():
    score = similarity("stay", "stay live at wembley")
    assert score <= 0.30


def test_similarity_remaster_light_penalty():
    # Remasters are still fundamentally the same recording.
    score = similarity("stay", "stay remastered")
    assert 0.5 <= score <= 0.75


def test_similarity_handles_typos():
    # SequenceMatcher should give credit for near-misses where token overlap
    # would give 0 (different token sets).
    assert similarity("kraftwerk", "kraftwork") >= 0.85


# ---- duration -------------------------------------------------------------

def test_duration_within_5s():
    assert duration_similarity(180, 182) == 1.0


def test_duration_unknown_neutral():
    assert duration_similarity(None, 200) == 0.5
    assert duration_similarity(0, 200) == 0.5


def test_duration_far_off():
    assert duration_similarity(180, 360) < 0.5


# ---- junk artist gate -----------------------------------------------------

@pytest.mark.parametrize("text", [
    "Various Artists", "various artists", "VA", "Unknown Artist",
])
def test_junk_artist_detected(text):
    assert is_junk_artist(text)


def test_junk_artist_negative():
    assert not is_junk_artist("The Beatles")
    assert not is_junk_artist("Daft Punk")


# ---- path splitting -------------------------------------------------------

def test_split_path_segments_handles_backslashes():
    segs = split_path_segments(r"Artist\Album\01 - Track.flac")
    assert segs == ["Artist", "Album", "01 - Track.flac"]


def test_split_path_segments_handles_pipes():
    segs = split_path_segments("artist||album||track.mp3")
    assert "artist" in segs and "album" in segs


# ---- score_track_against_filename ----------------------------------------

def test_perfect_match_high_confidence():
    conf, _ = score_track_against_filename(
        expected_artist="The Beatles",
        expected_title="Let It Be",
        filename=r"The Beatles\Let It Be\07 - Let It Be.flac",
        candidate_duration_s=243,
        expected_duration_s=243,
    )
    assert conf >= 0.85, f"expected high confidence, got {conf}"


def test_word_boundary_prevents_substring_match():
    # "muse" must not match "museum" as the artist segment.
    conf_correct, _ = score_track_against_filename(
        expected_artist="Muse",
        expected_title="Hysteria",
        filename=r"Muse\Absolution\Hysteria.flac",
    )
    conf_wrong, _ = score_track_against_filename(
        expected_artist="Muse",
        expected_title="Hysteria",
        filename=r"Museum Of Sound\Compilation\Hysteria.flac",
    )
    assert conf_correct > conf_wrong + 0.2, (
        f"correct={conf_correct} wrong={conf_wrong}"
    )


def test_junk_folder_rejected():
    conf, breakdown = score_track_against_filename(
        expected_artist="The Beatles",
        expected_title="Let It Be",
        filename=r"Various Artists\60s Hits\Let It Be.flac",
    )
    assert conf == 0.0
    assert any("junk_folder" in b for b in breakdown)


def test_remix_filename_demoted_for_plain_query():
    # Original mix should beat a remix when the query is for the original.
    conf_original, _ = score_track_against_filename(
        expected_artist="Calvin Harris",
        expected_title="Summer",
        filename=r"Calvin Harris\Motion\03 - Summer.flac",
    )
    conf_remix, _ = score_track_against_filename(
        expected_artist="Calvin Harris",
        expected_title="Summer",
        filename=r"Calvin Harris\Motion\03 - Summer (R3hab Remix).flac",
    )
    assert conf_original > conf_remix
    # And the remix should be below the default confidence floor.
    assert conf_remix < 0.55


def test_remix_query_keeps_remix_candidate():
    # When query asks for a remix, remix file should match.
    conf, _ = score_track_against_filename(
        expected_artist="Calvin Harris",
        expected_title="Summer (R3hab Remix)",
        filename=r"Calvin Harris\Motion\03 - Summer (R3hab Remix).flac",
        query="Calvin Harris - Summer Remix",
    )
    assert conf >= 0.7


def test_diacritic_match():
    # "Beyoncé" search must match "Beyonce" filename.
    conf, _ = score_track_against_filename(
        expected_artist="Beyoncé",
        expected_title="Halo",
        filename=r"Beyonce\I Am Sasha Fierce\Halo.flac",
    )
    assert conf >= 0.75


def test_empty_filename_zero_confidence():
    conf, _ = score_track_against_filename(
        expected_artist="x", expected_title="y", filename="",
    )
    assert conf == 0.0


def test_core_title_fast_path():
    # Punctuation differences in title should not stop a perfect match
    # when the artist segment is solid.
    conf, breakdown = score_track_against_filename(
        expected_artist="Beyoncé",
        expected_title="Don't Stop",
        filename=r"Beyonce\Album\Dont Stop.flac",
    )
    assert conf >= 0.90, f"expected fast-path, got {conf}: {breakdown}"


# ---- query parsing --------------------------------------------------------

def test_parse_query_dash():
    a, t = parse_query("The Beatles - Let It Be")
    assert a == "The Beatles" and t == "Let It Be"


def test_parse_query_no_separator():
    a, t = parse_query("Let It Be")
    assert a == "" and t == "Let It Be"


def test_query_requests_variant():
    assert query_requests_variant("Calvin Harris Summer Remix")
    assert query_requests_variant("Some song acoustic")
    assert not query_requests_variant("Just a regular song")


# ---- compute_match_confidence (YouTube/Monochrome/etc) -------------

def test_streaming_official_video_high_confidence():
    # Classic YouTube shape: "Artist - Title (Official Video)" against
    # a query that asks for the bare title.
    conf, _ = compute_match_confidence(
        expected_artist="Britney Spears",
        expected_title="Everytime",
        candidate_title="Britney Spears - Everytime (Official HD Video)",
        candidate_artist="BritneySpearsVEVO",
    )
    assert conf >= 0.85, f"expected high confidence, got {conf}"


def test_streaming_topic_channel_artist_match():
    # YouTube auto-generated "X - Topic" channels should match.
    conf, _ = compute_match_confidence(
        expected_artist="The Beatles",
        expected_title="Let It Be",
        candidate_title="Let It Be",
        candidate_artist="The Beatles - Topic",
    )
    assert conf >= 0.85


def test_streaming_diacritic_match():
    conf, _ = compute_match_confidence(
        expected_artist="Beyoncé",
        expected_title="Halo",
        candidate_title="Beyonce - Halo (Official Music Video)",
        candidate_artist="BeyonceVEVO",
    )
    assert conf >= 0.80


def test_streaming_remix_demoted_for_plain_query():
    conf_plain, _ = compute_match_confidence(
        expected_artist="Calvin Harris",
        expected_title="Summer",
        candidate_title="Calvin Harris - Summer",
        candidate_artist="CalvinHarrisVEVO",
    )
    conf_remix, _ = compute_match_confidence(
        expected_artist="Calvin Harris",
        expected_title="Summer",
        candidate_title="Calvin Harris - Summer (R3hab Remix)",
        candidate_artist="R3habVEVO",
    )
    assert conf_plain > conf_remix
    # The remix variant should fall well below the plain candidate.
    assert conf_remix < 0.55


def test_streaming_remix_query_accepts_remix():
    conf, _ = compute_match_confidence(
        expected_artist="Calvin Harris",
        expected_title="Summer (R3hab Remix)",
        candidate_title="Calvin Harris - Summer (R3hab Remix)",
        candidate_artist="R3habVEVO",
        query="Calvin Harris - Summer Remix",
    )
    assert conf >= 0.7


def test_streaming_wrong_artist_low_confidence():
    conf, _ = compute_match_confidence(
        expected_artist="The Beatles",
        expected_title="Yesterday",
        candidate_title="Boyz II Men - Yesterday",
        candidate_artist="BoyzIIMenVEVO",
    )
    # Title matches but artist is wrong, should not pass strong-match bar.
    assert conf < 0.70


def test_streaming_junk_channel_rejected():
    conf, breakdown = compute_match_confidence(
        expected_artist="The Beatles",
        expected_title="Let It Be",
        candidate_title="Let It Be",
        candidate_artist="Various Artists",
    )
    assert conf == 0.0
    assert "junk_artist" in breakdown


def test_streaming_typo_in_artist_still_matches():
    conf, _ = compute_match_confidence(
        expected_artist="Kraftwerk",
        expected_title="Autobahn",
        candidate_title="Kraftwork - Autobahn",
        candidate_artist="someuploader",
    )
    # Typo in artist token; SequenceMatcher should give credit.
    assert conf >= 0.65


def test_streaming_wrong_title_right_artist_gated():
    # Real bug we hit: searching "Machine Head - silver" pulled up other
    # Machine Head tracks ("ØUTSIDER", "Circle The Drain") because the
    # artist matched perfectly and the title penalty wasn't enough to
    # overcome the quality bonus on Monochrome HI_RES results.
    conf_right, _ = compute_match_confidence(
        expected_artist="Machine Head",
        expected_title="Silver",
        candidate_title="Silver",
        candidate_artist="Machine Head",
    )
    conf_wrong, breakdown = compute_match_confidence(
        expected_artist="Machine Head",
        expected_title="Silver",
        candidate_title="ØUTSIDER",
        candidate_artist="Machine Head",
    )
    assert conf_right >= 0.85
    # Wrong title with right artist must drop well below the right-title
    # candidate, even though both share the same artist.
    assert conf_wrong < 0.35, f"wrong-title confidence too high: {conf_wrong}"
    assert any("title_gate" in b for b in breakdown)


def test_filename_wrong_title_right_artist_gated():
    # Same bug, slskd path-shape edition.
    conf_right, _ = score_track_against_filename(
        expected_artist="Machine Head",
        expected_title="Silver",
        filename=r"Machine Head\The Burning Red\Silver.flac",
    )
    conf_wrong, _ = score_track_against_filename(
        expected_artist="Machine Head",
        expected_title="Silver",
        filename=r"Machine Head\Catharsis\Outsider.flac",
    )
    assert conf_right >= 0.85
    assert conf_wrong < 0.35, f"wrong-title confidence too high: {conf_wrong}"


def test_streaming_no_query_neutral():
    conf, breakdown = compute_match_confidence(
        expected_artist=None,
        expected_title=None,
        candidate_title="anything",
        candidate_artist="anyone",
    )
    assert conf == 0.5
    assert "no_query_info" in breakdown


def test_right_title_wrong_artist_is_gated():
    # A lossless cover is still a cover: Redi Hasa and the Vitamin String
    # Quartet both outranked JioSaavn's real Nirvana on Monochrome's bonus.
    for artist in ("Redi Hasa", "Vitamin String Quartet"):
        conf, breakdown = compute_match_confidence("Nirvana", "Lithium", "Lithium", artist)
        assert conf < 0.40, (artist, conf)
        assert any(b.startswith("artist_gate") for b in breakdown)


def test_namesake_act_is_not_the_artist():
    # Deezer's "Nirvana (UK)" is the 1960s British band.
    conf, breakdown = compute_match_confidence("Nirvana", "Lithium", "Lithium", "Nirvana (UK)")
    assert conf < 0.25
    assert "artist_namesake" in breakdown


def test_bracketed_branding_is_not_a_namesake():
    conf, _ = compute_match_confidence("Raye", "Where Is My Husband!", "WHERE IS MY HUSBAND!", "RAYE (Official)")
    assert conf >= 0.85


def test_named_mix_is_a_wrong_version():
    conf, breakdown = compute_match_confidence("Nirvana", "Lithium", "Lithium (Devonshire Mix)", "Nirvana")
    assert conf < 0.40
    assert any(b.startswith("wrong_version") for b in breakdown)


def test_original_mix_is_the_release():
    conf, _ = compute_match_confidence("Daft Punk", "One More Time", "One More Time (Original Mix)", "Daft Punk")
    assert conf >= 0.85


def test_asking_for_the_mix_gets_the_mix():
    conf, _ = compute_match_confidence("Nirvana", "Lithium (Devonshire Mix)", "Lithium (Devonshire Mix)", "Nirvana")
    assert conf >= 0.85


def test_unknown_artist_is_not_gated():
    conf, breakdown = compute_match_confidence("Nirvana", "Lithium", "Lithium", None)
    assert not any(b.startswith("artist_gate") for b in breakdown)
    assert conf >= 0.55


def test_bracketless_named_mix_is_a_wrong_version():
    conf, _ = compute_match_confidence("Arijit Singh", "Tum Hi Ho", "Tum Hi Ho Bollywood Dance Mix", "Arijit Singh")
    assert conf < 0.40


def test_joined_titles_and_medleys_are_wrong_versions():
    conf, _ = compute_match_confidence("Daft Punk", "One More Time", "One More Time / Aerodynamic", "Daft Punk")
    assert conf < 0.40
    conf, _ = compute_match_confidence("Daft Punk", "One More Time", "Daft Punk Medley: One More Time", "Daft Punk")
    assert conf < 0.40


def test_mentioning_the_artist_is_not_being_the_artist():
    conf, _ = compute_match_confidence(
        "Daft Punk", "One More Time", "Sample a Song (Parody of Daft Punk One More Time)", "DJ Timbo")
    assert conf < 0.40


def test_title_credits_still_count_for_uploaders():
    for title in ("RAYE - Where Is My Husband!", "Where Is My Husband! by RAYE", "Various - RAYE - Where Is My Husband!"):
        conf, _ = compute_match_confidence("Raye", "Where Is My Husband!", title, "SomeUploader")
        assert conf >= 0.85, title


# Vocals-only rips match title and duration perfectly, so only the words give
# them away, and uploaders cannot agree on how to spell those.
@pytest.mark.parametrize("variant", [
    "Acapella", "A Cappella", "Acappella", "A Capella", "Vocals Only", "Isolated Vocals",
])
def test_vocals_only_filename_demoted_for_plain_query(variant):
    conf_original, _ = score_track_against_filename(
        expected_artist="Adele",
        expected_title="Hello",
        filename=r"Adele\25\01 - Hello.flac",
    )
    conf_vocals, breakdown = score_track_against_filename(
        expected_artist="Adele",
        expected_title="Hello",
        filename=rf"Adele\25\01 - Hello ({variant}).flac",
    )
    assert conf_original > conf_vocals
    assert conf_vocals < 0.55


def test_acapella_query_keeps_acapella_candidate():
    assert query_requests_variant("Adele - Hello acapella")
    conf, _ = score_track_against_filename(
        expected_artist="Adele",
        expected_title="Hello (Acapella)",
        filename=r"Adele\Hello (Acapella).flac",
        query="Adele - Hello Acapella",
    )
    assert conf >= 0.7
