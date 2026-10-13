"""
Tests for reading artist, album, year and track out of a Soulseek file path.

Every path below is a real one, taken from a live "paramore - ignorance" search
against the Soulseek network. The old heuristic (first folder that isn't on a
short blocklist) got the artist right on one of the first five results and
offered up "Musique", "FLAC Archive", "P" and the filename itself for the rest.
Since that artist is what the card shows and what the download gets tagged with,
one in five was not a good enough hit rate.

The trick that fixes it is embarrassingly simple: the search already knows who
we are looking for, so look for them in the path rather than guessing from
position. Position is still the fallback for when nobody told us.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slskd import parse_slskd_path, extract_track_info_from_path


# The five that came back top of a real search, with what each one is really about.
REAL_WORLD_PATHS = [
    (r"@@udfqx\Music\Paramore\Brand New Eyes (2009)\02 - Ignorance.flac", "Brand New Eyes", 2009),
    (r"@@iptlc\Musique\FLAC\Paramore\Brand New Eyes\2-Ignorance.flac", "Brand New Eyes", None),
    (r"@@xhptn\FLAC Archive\Paramore\2009 - Brand New Eyes\02 - Ignorance.flac", "Brand New Eyes", 2009),
    (r"Foobar Rips\[2009] Paramore - Brand New Eyes\02 - Ignorance.flac", "Brand New Eyes", 2009),
    (r"music\P\Paramore\Albums\Brand New Eyes [2009]\02. Ignorance.flac", "Brand New Eyes", 2009),
]


@pytest.mark.parametrize("path,album,year", REAL_WORLD_PATHS)
def test_the_searched_artist_is_found_wherever_it_is_hiding(path, album, year):
    """All five used to disagree with each other. All five now say Paramore."""
    parsed = parse_slskd_path(path, query_artist="Paramore")

    assert parsed["artist"] == "Paramore", f"guessed '{parsed['artist']}' from {path}"
    assert parsed["album"] == album
    assert parsed["year"] == year
    assert parsed["title"] == "Ignorance"
    assert parsed["track_number"] == 2


@pytest.mark.parametrize("path,album,year", REAL_WORLD_PATHS[:3] + REAL_WORLD_PATHS[4:])
def test_position_alone_still_works_when_nobody_said_who_we_want(path, album, year):
    """The download path calls this with no query, so structure has to carry it."""
    parsed = parse_slskd_path(path)

    assert parsed["artist"] == "Paramore", f"guessed '{parsed['artist']}' from {path}"
    assert parsed["album"] == album


def test_a_compilation_gives_up_the_real_artist_and_a_clean_title():
    """A 54-track radio compilation, where the artist only appears in the filename."""
    parsed = parse_slskd_path(
        "@@ifacc\\TNTracker\\PMEDiA Music Pack 032 of 2023\\"
        "Various Artists - HITS ACÚSTICOS (2023)\\"
        "54. Paramore - Ignorance (Acoustic Version).mp3",
        query_artist="Paramore",
    )

    assert parsed["artist"] == "Paramore"
    assert parsed["album"] == "HITS ACÚSTICOS"
    assert parsed["year"] == 2023
    assert parsed["track_number"] == 54
    assert parsed["title"] == "Ignorance (Acoustic Version)"


def test_various_artists_is_never_the_artist():
    parsed = parse_slskd_path(r"Music\Various Artists\Now 42\03 - Some Song.mp3")

    assert parsed["artist"] != "Various Artists"


def test_an_album_that_is_just_a_year_keeps_its_name():
    """Taylor Swift's 1989 and Dr Dre's 2001 are titles, not release dates."""
    parsed = parse_slskd_path(r"Music\Taylor Swift\1989\03 Style.flac", query_artist="Taylor Swift")

    assert parsed["album"] == "1989"
    assert parsed["year"] is None
    assert parsed["artist"] == "Taylor Swift"


def test_a_disc_subfolder_does_not_become_the_album():
    parsed = parse_slskd_path(r"Pink Floyd\The Wall (1979)\CD1\05 - Another Brick in the Wall.flac")

    assert parsed["artist"] == "Pink Floyd"
    assert parsed["album"] == "The Wall"
    assert parsed["year"] == 1979


def test_alphabetical_buckets_and_format_folders_are_not_people():
    """"music/P/Paramore" has three folders and only one of them is a band."""
    for path in (r"music\P\Paramore\Riot!\01 - For a Pessimist.flac",
                 r"shared\FLAC\Paramore\Riot!\01 - For a Pessimist.flac",
                 r"downloads\lossless\Paramore\Riot!\01 - For a Pessimist.flac"):
        assert parse_slskd_path(path)["artist"] == "Paramore", path


def test_a_loose_file_still_yields_artist_and_title():
    parsed = parse_slskd_path(r"@@exvys\music\Soulseek Downloads\Paramore- - Ignorance.mp3")

    assert parsed["artist"] == "Paramore"
    assert parsed["title"] == "Ignorance"


def test_the_query_artist_is_the_last_resort_not_the_first():
    """A path that plainly names somebody else must not be relabelled to suit the search."""
    parsed = parse_slskd_path(
        r"Music\Hayley Williams\Petals for Armor\04 - Simmer.flac",
        query_artist="Paramore",
    )

    assert parsed["artist"] == "Hayley Williams"


def test_the_query_artist_fills_a_genuine_blank():
    parsed = parse_slskd_path(r"@@abc\downloads\complete\track01.flac", query_artist="Paramore")

    assert parsed["artist"] == "Paramore"


def test_the_two_value_helper_still_says_unknown_when_it_has_nothing():
    """The download path checks for exactly this string, so it has to survive."""
    artist, title = extract_track_info_from_path(r"@@abc\downloads\complete\track01.flac")

    assert artist == "Unknown"
    assert title == "track01"


def test_a_title_containing_a_dash_is_not_mistaken_for_an_artist_prefix():
    parsed = parse_slskd_path(r"Music\Radiohead\OK Computer\05 - Let Down.flac")

    assert parsed["title"] == "Let Down"
    assert parsed["artist"] == "Radiohead"


def test_a_disc_prefixed_track_number_does_not_end_up_in_the_title():
    """"1-02 Ignorance" is disc 1 track 2, not track 1 of something called "02 Ignorance"."""
    parsed = parse_slskd_path(r"@@zydiv\Music\Paramore\Brand New Eyes\1-02 Ignorance.flac")

    assert parsed["title"] == "Ignorance"
    assert parsed["track_number"] == 2


def test_a_bracketed_edition_keeps_its_closing_bracket():
    """Stripping the year used to take the last bracket with it on the way out."""
    parsed = parse_slskd_path(r"music\Paramore\(2009) Brand New Eyes (deluxe version)\02.14 Ignorance.flac")

    assert parsed["album"] == "Brand New Eyes (deluxe version)"
    assert parsed["year"] == 2009
    assert parsed["title"] == "Ignorance"


def test_a_loose_file_in_the_artist_folder_has_no_album():
    """"music/Paramore/02-paramore-ignorance.flac" is a stray file, not the album "Paramore"."""
    parsed = parse_slskd_path(r"music\Paramore\02-paramore-ignorance.flac", query_artist="Paramore")

    assert parsed["album"] == ""
    assert parsed["title"] == "ignorance"
    assert parsed["artist"] == "Paramore"


def test_a_self_titled_album_is_still_an_album():
    """The rule above must not eat Paramore's actual self-titled record."""
    parsed = parse_slskd_path(r"music\Paramore\Paramore\03 - Now.flac", query_artist="Paramore")

    assert parsed["album"] == "Paramore"
    assert parsed["artist"] == "Paramore"


def test_a_date_stamped_folder_does_not_pass_itself_off_as_an_artist():
    """"2009-09-22 - Brand New Eyes" leaves "09-22" looking like an "Artist - Album" split."""
    parsed = parse_slskd_path(r"@@jkdzp\Music\Paramore\2009-09-22 - Brand New Eyes\02. Ignorance.flac")

    assert parsed["artist"] == "Paramore"
    assert parsed["album"] == "Brand New Eyes"
    assert parsed["year"] == 2009


@pytest.mark.parametrize(
    "folder",
    ["(2013-05-06) Haunted House", "2013-05-06 Haunted House"],
)
def test_a_full_release_date_is_consumed_in_one_piece(folder):
    parsed = parse_slskd_path(
        rf"music\Knife Party\{folder}\01 Power Glove.flac",
        query_artist="Knife Party",
    )

    assert parsed["album"] == "Haunted House"
    assert parsed["year"] == 2013


@pytest.mark.parametrize("marker", ["FLAC", "1733 kbps", "16-44"])
def test_an_unmistakable_format_marker_is_removed_from_the_title(marker):
    parsed = parse_slskd_path(
        rf"music\Knife Party\Haunted House\01 Power Glove [{marker}].flac",
        query_artist="Knife Party",
    )

    assert parsed["title"] == "Power Glove"


def test_legitimate_bracketed_title_text_is_preserved():
    parsed = parse_slskd_path(
        r"music\Knife Party\Haunted House\01 Power Glove [VIP Mix].flac",
        query_artist="Knife Party",
    )

    assert parsed["title"] == "Power Glove [VIP Mix]"


@pytest.mark.parametrize(
    "folder,query,expected",
    [
        ("Knife Party (2011-2019)", "Knife Party", "Knife Party"),
        ("Pendulum (Knife Party)", "Knife Party", "Knife Party"),
        ("Pendulum (Knife Party)", "Pendulum", "Pendulum"),
    ],
)
def test_bracketed_artist_shelf_labels_offer_clean_query_matches(folder, query, expected):
    parsed = parse_slskd_path(
        rf"music\{folder}\Haunted House\01 Power Glove.flac",
        query_artist=query,
    )

    assert parsed["artist"] == expected


def test_underscores_are_treated_as_the_spaces_they_plainly_are():
    """A whole folder tree with no spaces in it still has an artist somewhere."""
    parsed = parse_slskd_path(
        r"music\full_albums\rock\paramore_-_brand_new_eyes_(deluxe_edition)\02_ignorance.flac",
        query_artist="Paramore",
    )

    assert parsed["artist"] == "paramore", "genre folder 'rock' won the argument"
    assert parsed["album"] == "brand new eyes (deluxe edition)"
    assert parsed["title"] == "ignorance"


def test_a_two_name_folder_gives_up_the_one_that_was_asked_for():
    """"Paramore - Hayley Williams" is somebody's shelf label, not a band name."""
    parsed = parse_slskd_path(
        r"@@oelin\Music\Paramore - Hayley Williams\2009 - Brand New Eyes (Deluxe Edition)\02. Ignorance.flac",
        query_artist="Paramore",
    )

    assert parsed["artist"] == "Paramore"
    assert parsed["album"] == "Brand New Eyes (Deluxe Edition)"


def test_a_filename_carrying_the_whole_path_is_trimmed_back_to_the_song():
    parsed = parse_slskd_path(
        r"music\Paramore\Brand New Eyes (2009)\Paramore - Brand New Eyes - 02 - Ignorance.flac",
        query_artist="Paramore",
    )

    assert parsed["title"] == "Ignorance"
    assert parsed["track_number"] == 2
    assert parsed["artist"] == "Paramore"


def test_a_stash_folder_does_not_get_credited_as_the_artist():
    """A real result, and "Pirated" is not a band however you look at it."""
    parsed = parse_slskd_path(
        r"@@qqwer\Pirated\Brand New Eyes (Album)\02 Ignorance.flac",
        query_artist="Paramore",
    )

    assert parsed["artist"] == "Paramore"


def test_a_format_tag_stapled_to_the_album_folder_is_removed():
    parsed = parse_slskd_path(
        r"music\Paramore\Paramore - Brand New Eyes (2012) - FLAC\04 - Brick By Boring Brick.flac",
        query_artist="Paramore",
    )

    assert parsed["album"] == "Brand New Eyes"
    assert parsed["year"] == 2012


def test_an_artist_does_not_keep_a_trailing_underscore():
    parsed = parse_slskd_path(r"music\paramore_\brand new eyes (2009)\02 ignorance.flac")

    assert parsed["artist"] == "paramore"


def test_nonsense_in_gives_nothing_out_rather_than_an_exception():
    assert parse_slskd_path("")["artist"] == ""
    assert parse_slskd_path("\\\\\\")["artist"] == ""
