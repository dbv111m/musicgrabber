"""Unit tests for the long-form YouTube split cue-sheet parser and helpers.

Pure-function coverage only (no yt-dlp/network calls, no DB) - the detect/
download/cut pipeline itself is exercised manually against the local
container per docs/requests and bugs.md's guardrails.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestParseCueSheetText:
    def test_plain_mm_ss_lines(self):
        from longform_split import parse_cue_sheet_text

        text = "00:00 Track One\n03:45 Track Two\n07:10 Track Three"
        points = parse_cue_sheet_text(text)
        assert [p["start_seconds"] for p in points] == [0.0, 225.0, 430.0]
        assert [p["title"] for p in points] == ["Track One", "Track Two", "Track Three"]

    def test_hh_mm_ss_lines(self):
        from longform_split import parse_cue_sheet_text

        points = parse_cue_sheet_text("1:02:15 Long Mix Continues")
        assert points == [{"start_seconds": 3735.0, "title": "Long Mix Continues"}]

    def test_numbered_and_bracketed_lines(self):
        from longform_split import parse_cue_sheet_text

        text = "1. 00:00 Track One\n2) [03:45] Track Two"
        points = parse_cue_sheet_text(text)
        assert [p["title"] for p in points] == ["Track One", "Track Two"]

    def test_parenthesised_timestamps(self):
        # YouTube auto-links a bare "H:MM:SS" in a description regardless of
        # what wraps it, and parentheses are the more common style in the wild
        # (e.g. "(0:00:00) Artist - Title"), not square brackets.
        from longform_split import parse_cue_sheet_text

        text = "(0:00:00) Track One\n(0:07:07) Track Two"
        points = parse_cue_sheet_text(text)
        assert [p["start_seconds"] for p in points] == [0.0, 427.0]
        assert [p["title"] for p in points] == ["Track One", "Track Two"]

    def test_real_world_description_regression(self):
        # The actual description text of Karl's test video (Red Jerry: Deep &
        # Chilled Euphoria CD1), pulled via yt-dlp. This video has no YouTube
        # chapters and no cue sheet in its comments, only here - the case that
        # sent the split panel down the manual-entry path before the
        # description tier was added. Real hand-typed text: inconsistent
        # apostrophes, nested parens in titles, no track after the cue list.
        from longform_split import parse_cue_sheet_text, _fill_end_times, _split_artist_title

        description = (
            "Label: Telstar TV (2001)\n"
            "https://www.discogs.com/release/102273\n"
            "Mixed by Red Jerry.\n\n"
            "(0:00:00) Jean F. Cochois - Maximum Reflexion (On A Mountain High)\n"
            "(0:07:07) Accadia - Blind Visions\n"
            "(0:13:46) Rados - My Soul Is At The End of The Universe\n"
            "(0:18:08) Leftfield - Melt\n"
            "(0:21:58) Moonman - Galaxia (Eliot J Remix)\n"
            "(0:27:07) Rui Da Silva - Water\n"
            "(0:31:50) Bliss - Song For Olabi\n"
            "(0:36:20) Solarstone - Jabberwock (Chillout Mix)\n"
            "(0:41:59) Datar - B (Am'b'ient Mix)\n"
            "(0:46:52) Groove Armada - Your Song (Tim 'Love' Lee's Semi-Bearded Remix)\n"
            "(0:52:15) New Vision - Fields Of Wisdom\n"
            "(0:58:35) Space Manoeuvres - Stage One (Blain Sandhag Mix)\n"
            "(1:04:49) Autechre - Basscadet (Beaumont Hannant Two Mix)\n"
            "(1:12:27) Miro - By Your Side (Miro's Rolled Mix)\n\n"
            '"Copyright Disclaimer Under Section 107 of the Copyright Act 1976..."'
        )
        points = parse_cue_sheet_text(description)
        assert len(points) == 14
        assert points[0] == {"start_seconds": 0.0, "title": "Jean F. Cochois - Maximum Reflexion (On A Mountain High)"}
        assert points[-1]["start_seconds"] == 4347.0  # 1:12:27
        assert points[-1]["title"] == "Miro - By Your Side (Miro's Rolled Mix)"

        segments = _fill_end_times(points, duration=4655.0)
        assert segments[0]["end_seconds"] == 427.0  # next track's start (0:07:07)
        assert segments[-1]["end_seconds"] == 4655.0  # last track runs to the video's end

        artist, title = _split_artist_title(segments[0]["title"])
        assert artist == "Jean F. Cochois"
        assert title == "Maximum Reflexion (On A Mountain High)"

    def test_dash_separator_is_stripped_from_title(self):
        from longform_split import parse_cue_sheet_text

        points = parse_cue_sheet_text("00:00 - Above & Beyond - Sun & Moon")
        assert points[0]["title"] == "Above & Beyond - Sun & Moon"

    def test_prose_mentioning_a_timestamp_is_not_a_cue_line(self):
        # Real cue sheet lines start with the timestamp; a comment saying
        # "great fade at 16:55" should never be mistaken for one.
        from longform_split import parse_cue_sheet_text

        assert parse_cue_sheet_text("That slow fade at 16:55 was great") == []

    def test_empty_and_blank_input(self):
        from longform_split import parse_cue_sheet_text

        assert parse_cue_sheet_text("") == []
        assert parse_cue_sheet_text("   \n\n  ") == []

    def test_line_with_timestamp_but_no_title_is_skipped(self):
        from longform_split import parse_cue_sheet_text

        assert parse_cue_sheet_text("00:00") == []


class TestBestCueSheetComment:
    def test_picks_comment_with_most_matches(self):
        from longform_split import _best_cue_sheet_comment

        comments = [
            {"text": "That slow fade at 16:55 was great"},  # one incidental mention
            {"text": "00:00 Intro\n03:20 Track Two\n07:00 Track Three\n11:15 Track Four"},
            {"text": "05:00 Another partial list\n10:00 Second entry"},
        ]
        best = _best_cue_sheet_comment(comments)
        assert len(best) == 4
        assert best[0]["title"] == "Intro"

    def test_single_incidental_mention_is_rejected(self):
        from longform_split import _best_cue_sheet_comment

        comments = [{"text": "check out 2:30, it's great"}]
        assert _best_cue_sheet_comment(comments) == []

    def test_no_comments(self):
        from longform_split import _best_cue_sheet_comment

        assert _best_cue_sheet_comment([]) == []


class TestFillEndTimes:
    def test_end_is_next_start_and_last_is_duration(self):
        from longform_split import _fill_end_times

        cue_points = [
            {"start_seconds": 0.0, "title": "A"},
            {"start_seconds": 100.0, "title": "B"},
            {"start_seconds": 250.0, "title": "C"},
        ]
        segments = _fill_end_times(cue_points, duration=400.0)
        assert [s["end_seconds"] for s in segments] == [100.0, 250.0, 400.0]


class TestSplitArtistTitle:
    def test_splits_artist_and_title(self):
        from longform_split import _split_artist_title

        assert _split_artist_title("Above & Beyond - Sun & Moon") == ("Above & Beyond", "Sun & Moon")

    def test_no_separator_leaves_artist_none(self):
        from longform_split import _split_artist_title

        assert _split_artist_title("Just A Title") == (None, "Just A Title")


class TestValidateSegments:
    def test_empty_list_is_rejected(self):
        from longform_split import validate_segments

        assert validate_segments([]) is not None

    def test_blank_title_is_rejected(self):
        from longform_split import validate_segments

        segs = [{"title": "  ", "start_seconds": 0.0, "end_seconds": 10.0}]
        assert validate_segments(segs) is not None

    def test_end_before_start_is_rejected(self):
        from longform_split import validate_segments

        segs = [{"title": "Track", "start_seconds": 10.0, "end_seconds": 5.0}]
        assert validate_segments(segs) is not None

    def test_segment_past_duration_is_rejected(self):
        from longform_split import validate_segments

        segs = [{"title": "Track", "start_seconds": 0.0, "end_seconds": 500.0}]
        assert validate_segments(segs, duration=400.0) is not None

    def test_valid_segments_pass(self):
        from longform_split import validate_segments

        segs = [
            {"title": "Track One", "start_seconds": 0.0, "end_seconds": 100.0},
            {"title": "Track Two", "start_seconds": 100.0, "end_seconds": 200.0},
        ]
        assert validate_segments(segs, duration=200.0) is None
