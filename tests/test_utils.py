import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_sanitize_playlist_name_uses_url_fallback_when_name_sanitizes_empty():
    from utils import sanitize_playlist_name

    assert (
        sanitize_playlist_name("///", "https://monochrome.tf/playlist/0dfc3b10-fbdb-4419-bf54-11b90051fa6c")
        == "0dfc3b10-fbdb-4419-bf54-11b90051fa6c"
    )


def test_sanitize_playlist_name_keeps_extended_characters_and_removes_path_separators():
    from utils import sanitize_playlist_name

    assert sanitize_playlist_name("Beyoncé / Café ☕") == "Beyoncé Café ☕"


def test_sanitize_playlist_name_never_returns_empty():
    from utils import sanitize_playlist_name

    assert sanitize_playlist_name("///") == "Playlist"


def test_cap_filename_stem_keeps_short_names_untouched():
    from utils import cap_filename_stem

    stem = "Daft Punk - Get Lucky"
    assert cap_filename_stem(stem) == stem


def test_cap_filename_stem_fits_within_name_max_with_extension():
    # The real-world crasher: a 30-artist remix that combined to 257 bytes and
    # blew up with [Errno 36] mid-download, stalling the rest of the playlist.
    from utils import cap_filename_stem, sanitize_filename
    from constants import MAX_FILENAME_BYTES

    artist = ("Krept & Konan, Abra Cadabra, BackRoad Gee, French Montana, Clavish, "
              "Beenie Man, Pa Salieu, Bandokay, Double Lz, DoRoad")
    title = ("Dat Way (Remix) [feat. Abra Cadabra, Backroad Gee, French Montana, "
             "Clavish, Beenie Man, Pa Salieu, Bandokay, Double Lz, K-Trap & DoRoad]")
    raw = f"{sanitize_filename(artist)} - {sanitize_filename(title)}"
    assert len(raw.encode("utf-8")) > MAX_FILENAME_BYTES  # would have crashed

    capped = cap_filename_stem(raw)
    # Stem + any extension we use, plus yt-dlp's worst-case temp suffix, must fit.
    assert len((capped + ".f399.webm.part").encode("utf-8")) <= MAX_FILENAME_BYTES


def test_truncate_to_bytes_never_splits_a_multibyte_glyph():
    from utils import truncate_to_bytes

    # Each emoji is 4 UTF-8 bytes; truncating to 10 must drop to 8 (2 glyphs),
    # never leave a mangled half-character.
    out = truncate_to_bytes("🎵🎵🎵", 10)
    assert out == "🎵🎵"
    out.encode("utf-8")  # must be valid UTF-8, would raise otherwise


def test_sanitize_filename_caps_multibyte_to_byte_budget():
    from utils import sanitize_filename
    from constants import MAX_FILENAME_BYTES

    # 200 CJK chars is under the char cap but 600 bytes, well over NAME_MAX.
    out = sanitize_filename("あ" * 200)
    assert len(out.encode("utf-8")) <= MAX_FILENAME_BYTES


def test_sanitize_filename_folds_unicode_dashes_to_ascii_hyphen():
    # Real-world bug (issue #87): MusicBrainz/YouTube titles carry "smart"
    # hyphens that look identical in a terminal but come out as "?" on disk
    # through some SFTP clients and non-UTF-8-locale tools.
    from utils import sanitize_filename

    assert sanitize_filename("alt‐J") == "alt-J"       # hyphen
    assert sanitize_filename("alt‑J") == "alt-J"       # non-breaking hyphen
    assert sanitize_filename("Song – Remix") == "Song - Remix"  # en dash
    assert sanitize_filename("Song — Remix") == "Song - Remix"  # em dash


def test_artist_credit_lookup_variants_include_primary_artist():
    from utils import artist_credit_lookup_variants, artist_credits_match

    assert artist_credit_lookup_variants("The Chemical Brothers, Q-Tip") == [
        "The Chemical Brothers, Q-Tip",
        "The Chemical Brothers",
    ]
    assert artist_credit_lookup_variants("Disclosure feat. Sam Smith") == [
        "Disclosure feat. Sam Smith",
        "Disclosure",
    ]
    assert artist_credits_match("山下達郎", "山下達郎") is True


def test_check_duplicate_falls_back_to_primary_credited_artist(tmp_path, monkeypatch):
    import utils

    singles = tmp_path / "Singles"
    existing = singles / "The Chemical Brothers" / "Go.flac"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"already here")

    monkeypatch.setattr(
        utils,
        "get_download_dir",
        lambda artist, user_id=None: singles / utils.sanitize_filename(artist),
    )
    monkeypatch.setattr(utils, "get_singles_dir", lambda user_id=None: singles)
    monkeypatch.setattr(utils, "get_albums_dir", lambda user_id=None: tmp_path / "Albums")
    monkeypatch.setattr(utils, "get_playlists_dir", lambda user_id=None: None)

    assert (
        utils.check_duplicate("The Chemical Brothers, Q-Tip", "Go")
        == existing
    )


def test_check_duplicate_finds_externally_renamed_file_by_tags(tmp_path, monkeypatch):
    import metadata
    import time
    import utils

    music = tmp_path / "Music"
    renamed = music / "Filed Elsewhere" / "001 completely renamed.flac"
    renamed.parent.mkdir(parents=True)
    renamed.write_bytes(b"tagged audio")

    monkeypatch.setattr(utils, "get_setting", lambda *a, **kw: str(music))
    monkeypatch.setattr(utils, "get_download_dir", lambda *a, **kw: music / "Singles" / "Missing")
    monkeypatch.setattr(utils, "get_singles_dir", lambda *a, **kw: music / "Singles")
    monkeypatch.setattr(utils, "get_albums_dir", lambda *a, **kw: music / "Albums")
    monkeypatch.setattr(utils, "get_playlists_dir", lambda *a, **kw: music / "Playlists")
    monkeypatch.setattr(
        metadata, "read_artist_title",
        lambda path: ("Blur", "Song 2") if path == renamed else (None, None),
    )
    utils._tag_index_cache.clear()
    utils._tag_index_errors.clear()

    assert utils.check_duplicate("Blur", "Song 2", user_id="alice") is None
    deadline = time.monotonic() + 2
    while utils.library_tag_index_in_progress("alice"):
        assert time.monotonic() < deadline
        time.sleep(.005)
    assert utils.check_duplicate("Blur", "Song 2", user_id="alice") == renamed


# ---------------------------------------------------------------------------
# Library scans must stay inside the library.
#
# NAS shares keep their deleted files in a hidden bin *inside* the share, so a
# naive rglob counts last month's deletions as part of the collection.
# ---------------------------------------------------------------------------


def test_is_excluded_scan_dir_catches_the_usual_nas_suspects():
    import utils
    for name in (".Trash", ".Trash-1000", ".Trash-1001", ".Trashes",
                 "@Recycle", "@Recycle.bin", "#recycle", "$RECYCLE.BIN",
                 "RECYCLER", "@eaDir", "System Volume Information",
                 "lost+found", ".upgrade_quarantine"):
        assert utils.is_excluded_scan_dir(name), f"{name} should be excluded"


def test_is_excluded_scan_dir_leaves_real_band_names_alone():
    """Suede and the New York Dolls both have an album called Trash."""
    import utils
    for name in ("Trash", "Trashcan Sinatras", "Recycle Culture",
                 "The Recycler", "trashy", "", "Recycled J"):
        assert not utils.is_excluded_scan_dir(name), f"{name} should be kept"


def test_iter_library_audio_files_skips_nas_bins(tmp_path):
    import utils
    root = tmp_path / "Singles"
    (root / "Fatboy Slim").mkdir(parents=True)
    keeper = root / "Fatboy Slim" / "Praise You.flac"
    keeper.write_bytes(b"audio")

    # Deleted last month, still physically present, thanks QNAP.
    (root / "@Recycle" / "Fatboy Slim").mkdir(parents=True)
    (root / "@Recycle" / "Fatboy Slim" / "Praise You.flac").write_bytes(b"binned")
    (root / ".Trash-1000").mkdir()
    (root / ".Trash-1000" / "Rockafeller Skank.mp3").write_bytes(b"binned")
    # Synology drops these next to everything; they are not music.
    (root / "Fatboy Slim" / "@eaDir").mkdir()
    (root / "Fatboy Slim" / "@eaDir" / "thumb.flac").write_bytes(b"nope")

    found = sorted(entry.path for entry in utils.iter_library_audio_files(root))
    assert found == [keeper]


def test_iter_library_audio_files_ignores_non_audio_and_missing_roots(tmp_path):
    import utils
    root = tmp_path / "Singles"
    root.mkdir()
    (root / "cover.jpg").write_bytes(b"art")
    (root / "Praise You.lrc").write_text("lyrics")
    (root / "Praise You.flac").write_bytes(b"audio")

    assert [e.path.name for e in utils.iter_library_audio_files(root)] == ["Praise You.flac"]
    assert list(utils.iter_library_audio_files(tmp_path / "nope")) == []
    assert list(utils.iter_library_audio_files(None)) == []


def test_iter_library_audio_files_refuses_to_follow_symlinks_out(tmp_path):
    """A helpful symlink must not turn a library scan into a whole-NAS scan."""
    import utils
    root = tmp_path / "Singles"
    root.mkdir()
    (root / "Real.flac").write_bytes(b"audio")

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "NotOurs.flac").write_bytes(b"audio")

    try:
        (root / "shortcut").symlink_to(outside, target_is_directory=True)
        (root / "Linked.flac").symlink_to(outside / "NotOurs.flac")
    except (OSError, NotImplementedError):
        import pytest
        pytest.skip("symlinks unavailable on this filesystem")

    assert [e.path.name for e in utils.iter_library_audio_files(root)] == ["Real.flac"]


def test_iter_library_audio_files_hands_back_the_stat_it_took(tmp_path):
    """The whole point of Phase 1: nobody should have to stat the file again."""
    import utils
    root = tmp_path / "Singles"
    root.mkdir()
    track = root / "Praise You.flac"
    track.write_bytes(b"forty seven bytes of extremely convincing audio.")

    (entry,) = list(utils.iter_library_audio_files(root))
    real = track.stat()
    assert entry.path == track
    assert entry.stat.st_size == real.st_size == len(b"forty seven bytes of extremely convincing audio.")
    assert entry.stat.st_mtime_ns == real.st_mtime_ns
    assert (entry.stat.st_dev, entry.stat.st_ino) == (real.st_dev, real.st_ino)


def test_iter_library_audio_files_steps_over_an_unreadable_directory(tmp_path):
    """One sulking directory is not a reason to abandon the whole library."""
    import utils
    import pytest
    root = tmp_path / "Singles"
    (root / "Fatboy Slim").mkdir(parents=True)
    keeper = root / "Fatboy Slim" / "Praise You.flac"
    keeper.write_bytes(b"audio")
    locked = root / "Locked"
    locked.mkdir()
    (locked / "Secret.flac").write_bytes(b"audio")
    os.chmod(locked, 0o000)
    if os.access(locked, os.R_OK):
        pytest.skip("running as root, which can read anything it likes")

    try:
        found = [e.path.name for e in utils.iter_library_audio_files(root)]
        assert found == ["Praise You.flac"]
        with pytest.raises(OSError):
            list(utils.iter_library_audio_files(root, strict=True))
    finally:
        os.chmod(locked, 0o755)


def test_path_has_excluded_scan_dir_respects_root(tmp_path):
    import utils
    from pathlib import Path
    assert utils.path_has_excluded_scan_dir(Path("/music/Singles/@Recycle/x.flac"))
    assert not utils.path_has_excluded_scan_dir(Path("/music/Singles/Suede/Trash.flac"))
    # A library that lives under an excluded-looking parent is still a library.
    assert not utils.path_has_excluded_scan_dir(
        Path("/volume1/#recycle/Music/Suede/Trash.flac"),
        root=Path("/volume1/#recycle/Music"),
    )
