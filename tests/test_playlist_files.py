"""The M3U editor's file handling: parse, rewrite, refuse, and never mangle."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import downloads
import playlist_files as pf


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A music folder with a Playlists folder, two tracks and no database."""
    music = tmp_path / "music"
    playlists = music / "Playlists"
    singles = music / "Singles"
    for d in (playlists / "Rock", singles / "Artist"):
        d.mkdir(parents=True)
    (playlists / "Rock" / "One.flac").write_bytes(b"x")
    (singles / "Artist" / "Two.mp3").write_bytes(b"x")

    monkeypatch.setattr(pf, "_music_dir", lambda user_id: music)
    monkeypatch.setattr(pf, "get_playlists_dir", lambda user_id=None: playlists)
    monkeypatch.setattr(pf, "get_singles_dir", lambda user_id=None: singles)
    monkeypatch.setattr(pf, "watched_m3u_paths", lambda user_id: set())
    monkeypatch.setattr(pf, "set_file_permissions", lambda path: None)
    monkeypatch.setattr(pf, "_invalidate_routing_list", lambda user_id: None)
    monkeypatch.setattr(pf.library_index, "describe_files", lambda paths: {})
    monkeypatch.setattr(downloads, "get_setting", lambda key, default="", user_id=None: str(music))

    def vet(raw, user_id=None):
        p = Path(raw)
        return p if p.is_file() and pf._inside(p, music) else None
    monkeypatch.setattr(pf, "authoritative_audio_file", vet)
    return music


def write(path: Path, text: str) -> Path:
    path.write_bytes(text.encode("utf-8"))
    return path


def test_parse_keeps_extinf_with_its_entry_and_header_apart():
    raw = b"#EXTM3U\n#PLAYLIST:Mix\n#EXTINF:200,A - One\nRock/One.flac\n\nTwo.mp3\n# trailing note\n"
    header, entries, trailer = pf.parse_m3u(raw)
    assert header == ["#EXTM3U", "#PLAYLIST:Mix"]
    assert entries == [
        {"meta": ["#EXTINF:200,A - One"], "line": "Rock/One.flac"},
        {"meta": [], "line": "Two.mp3"},
    ]
    assert trailer == ["# trailing note"]


def test_round_trip_preserves_bytes_it_cannot_decode():
    raw = b"#EXTM3U\nCaf\xe9/Track.mp3\n"
    header, entries, trailer = pf.parse_m3u(raw)
    assert pf.serialise_m3u(header, entries, trailer) == raw


def test_resolve_entry_states(library):
    m3u_dir = library / "Playlists"
    assert pf.resolve_entry("Rock/One.flac", m3u_dir, library)[1] == "ok"
    assert pf.resolve_entry("Rock/Gone.flac", m3u_dir, library)[1] == "missing"
    # Navidrome's idea of the library, not ours to call missing.
    assert pf.resolve_entry("/navidrome/music/X.flac", m3u_dir, library)[1] == "external"
    assert pf.resolve_entry("https://example.com/stream", m3u_dir, library)[1] == "external"


def test_save_reorders_removes_and_adds_without_touching_kept_lines(library):
    m3u = write(library / "Playlists" / "Mix.m3u",
                "#EXTM3U\n#EXTINF:200,A - One\nRock/One.flac\nRock/Gone.flac\n")
    data = pf.read_playlist(None, "Playlists/Mix.m3u")
    assert [e["state"] for e in data["entries"]] == ["ok", "missing"]

    pf.save_playlist(None, "Playlists/Mix.m3u", data["mtime_ns"], [
        {"file": str(library / "Singles" / "Artist" / "Two.mp3")},
        {"index": 0},
    ])
    assert m3u.read_text() == "#EXTM3U\n../Singles/Artist/Two.mp3\n#EXTINF:200,A - One\nRock/One.flac\n"
    assert not list(m3u.parent.glob("*.tmp"))


def test_save_refuses_when_the_file_changed_since_it_was_read(library):
    write(library / "Playlists" / "Mix.m3u", "#EXTM3U\nRock/One.flac\n")
    data = pf.read_playlist(None, "Playlists/Mix.m3u")
    m3u = library / "Playlists" / "Mix.m3u"
    m3u.write_text("#EXTM3U\nRock/One.flac\nRock/New.flac\n")
    os.utime(m3u, ns=(1, 1))
    with pytest.raises(pf.PlaylistFileError) as err:
        pf.save_playlist(None, "Playlists/Mix.m3u", data["mtime_ns"], [{"index": 0}])
    assert err.value.status == 409


def test_watched_m3u_is_read_only_but_can_be_copied(library, monkeypatch):
    m3u = write(library / "Playlists" / "Daily.m3u", "#EXTM3U\nRock/One.flac\nRock/Gone.flac\n")
    monkeypatch.setattr(pf, "watched_m3u_paths", lambda user_id: {pf._norm(m3u)})
    data = pf.read_playlist(None, "Playlists/Daily.m3u")
    assert data["watched"] is True
    with pytest.raises(pf.PlaylistFileError) as err:
        pf.save_playlist(None, "Playlists/Daily.m3u", data["mtime_ns"], [{"index": 0}])
    assert err.value.status == 403
    with pytest.raises(pf.PlaylistFileError):
        pf.delete_playlist(None, "Playlists/Daily.m3u")

    copy = pf.create_playlist(None, "Daily mine", copy_from="Playlists/Daily.m3u",
                              mtime_ns=data["mtime_ns"], entries=[{"index": 0}])
    assert copy["path"] == "Playlists/Daily mine.m3u"
    assert (library / "Playlists" / "Daily mine.m3u").read_text() == "#EXTM3U\nRock/One.flac\n"


def test_create_empty_and_refuse_duplicate_names(library):
    made = pf.create_playlist(None, "Road Trip")
    assert (library / made["path"]).read_text() == "#EXTM3U\n"
    with pytest.raises(pf.PlaylistFileError) as err:
        pf.create_playlist(None, "Road Trip")
    assert err.value.status == 409


@pytest.mark.parametrize("rel", [
    "Playlists/../../outside.m3u",
    "Singles/Artist/Deep.m3u",
    "Playlists/Rock/One.flac",
    "../music2/Playlists/Mix.m3u",
])
def test_resolve_m3u_rejects_paths_outside_playlist_folders(library, rel):
    with pytest.raises(pf.PlaylistFileError):
        pf.resolve_m3u(None, rel, must_exist=False)


def test_new_entries_must_be_library_audio(library, tmp_path):
    write(library / "Playlists" / "Mix.m3u", "#EXTM3U\n")
    data = pf.read_playlist(None, "Playlists/Mix.m3u")
    stray = tmp_path / "elsewhere.mp3"
    stray.write_bytes(b"x")
    with pytest.raises(pf.PlaylistFileError) as err:
        pf.save_playlist(None, "Playlists/Mix.m3u", data["mtime_ns"], [{"file": str(stray)}])
    assert err.value.status == 400


def test_list_counts_missing_but_not_external(library):
    write(library / "Playlists" / "Mix.m3u",
          "#EXTM3U\nRock/One.flac\nRock/Gone.flac\n/navidrome/X.flac\n")
    write(library / "Singles" / "Loose.m3u", "#EXTM3U\n")
    listed = {p["path"]: p for p in pf.list_playlists(None)}
    assert listed["Playlists/Mix.m3u"]["entries"] == 3
    assert listed["Playlists/Mix.m3u"]["missing"] == 1
    assert "Singles/Loose.m3u" in listed
