"""Conversion request naming and backwards-compatibility tests."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import (
    AlbumDownloadRequest,
    AsyncBulkImportRequest,
    DownloadRequest,
    SettingsUpdate,
    WatchedArtistRequest,
    WatchedArtistUpdate,
    WatchedPlaylistRequest,
    WatchedPlaylistUpdate,
)


def test_download_request_uses_honest_conversion_name():
    request = DownloadRequest(video_id="track", title="Track", convert_audio=True)

    assert request.convert_audio is True
    assert "convert_audio" in request.model_dump()
    assert "convert_to_flac" not in request.model_dump()


def test_download_request_accepts_legacy_conversion_name():
    request = DownloadRequest(video_id="track", title="Track", convert_to_flac=True)

    assert request.convert_audio is True


def test_all_conversion_requests_accept_canonical_and_legacy_names():
    request_factories = [
        lambda key: AsyncBulkImportRequest(songs="Artist - Track", **key),
        lambda key: WatchedPlaylistRequest(url="https://example.test/playlist", **key),
        lambda key: WatchedPlaylistUpdate(**key),
        lambda key: WatchedArtistRequest(mbid="00000000-0000-0000-0000-000000000000", name="Artist", from_date="2026-08-08", **key),
        lambda key: WatchedArtistUpdate(**key),
        lambda key: AlbumDownloadRequest(artist="Artist", album_title="Album", release_mbid="00000000-0000-0000-0000-000000000000", **key),
    ]

    for make_request in request_factories:
        assert make_request({"convert_audio": True}).convert_audio is True
        assert make_request({"convert_to_flac": True}).convert_audio is True


def test_settings_update_accepts_canonical_and_legacy_names():
    """The stored setting was renamed too, so its API field needs the same courtesy."""
    assert SettingsUpdate(default_convert_audio=True).default_convert_audio is True
    assert SettingsUpdate(default_convert_to_flac=True).default_convert_audio is True


def test_settings_update_writes_under_the_current_key_only():
    """An old client's spelling must land on the new key, not resurrect the old one."""
    written = SettingsUpdate(default_convert_to_flac=True).model_dump(exclude_unset=True)

    assert written == {"default_convert_audio": True}


def test_unset_conversion_field_stays_out_of_a_settings_patch():
    """Otherwise every settings save would quietly rewrite the conversion mode."""
    assert "default_convert_audio" not in SettingsUpdate(audio_format="flac").model_dump(exclude_unset=True)
