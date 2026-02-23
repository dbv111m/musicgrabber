"""
MusicGrabber - Pydantic Request/Response Models
"""

from typing import Optional
from pydantic import BaseModel, Field
from constants import DEFAULT_CONVERT_TO_FLAC, MAX_SEARCH_QUERY_LENGTH


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=MAX_SEARCH_QUERY_LENGTH)
    limit: int = 15
    source: str = "all"  # "youtube", "soundcloud", "monochrome", or "all"

class DownloadRequest(BaseModel):
    video_id: str
    title: str
    artist: Optional[str] = None
    search_token: Optional[str] = None
    download_type: str = "single"  # "single" or "playlist"
    convert_to_flac: bool = DEFAULT_CONVERT_TO_FLAC  # Whether to convert to FLAC or keep original format
    # Source routing
    source: str = "youtube"  # "youtube", "soundcloud", "monochrome", or "soulseek"
    source_url: Optional[str] = None  # Full URL for non-YouTube sources (e.g. SoundCloud/Monochrome)
    # Soulseek-specific fields
    slskd_username: Optional[str] = None
    slskd_filename: Optional[str] = None

class PlaylistFetchRequest(BaseModel):
    url: str  # Spotify, Amazon Music, etc. playlist URL

# Backwards compat alias — older code references this name
SpotifyPlaylistRequest = PlaylistFetchRequest

class AsyncBulkImportRequest(BaseModel):
    songs: str  # Multi-line text with "Artist - Song" format
    create_playlist: bool = False
    playlist_name: Optional[str] = None
    convert_to_flac: bool = DEFAULT_CONVERT_TO_FLAC

class WatchedPlaylistRequest(BaseModel):
    url: str  # Spotify, YouTube, or Amazon Music playlist URL
    refresh_interval_hours: int = 24
    convert_to_flac: bool = DEFAULT_CONVERT_TO_FLAC

class WatchedPlaylistUpdate(BaseModel):
    refresh_interval_hours: Optional[int] = None
    enabled: Optional[bool] = None
    convert_to_flac: Optional[bool] = None

class SettingsUpdate(BaseModel):
    """Settings that can be updated via the UI"""
    # General
    music_dir: Optional[str] = None
    enable_musicbrainz: Optional[bool] = None
    enable_lyrics: Optional[bool] = None
    default_convert_to_flac: Optional[bool] = None
    min_audio_bitrate: Optional[int] = None
    singles_subdir: Optional[str] = None
    organise_by_artist: Optional[bool] = None
    # Soulseek/slskd
    slskd_url: Optional[str] = None
    slskd_user: Optional[str] = None
    slskd_pass: Optional[str] = None
    slskd_downloads_path: Optional[str] = None
    # Navidrome
    navidrome_url: Optional[str] = None
    navidrome_user: Optional[str] = None
    navidrome_pass: Optional[str] = None
    # Jellyfin
    jellyfin_url: Optional[str] = None
    jellyfin_api_key: Optional[str] = None
    # Notifications
    notify_on: Optional[str] = None
    telegram_webhook_url: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_pass: Optional[str] = None
    smtp_from: Optional[str] = None
    smtp_to: Optional[str] = None
    smtp_tls: Optional[bool] = None
    # YouTube
    youtube_cookies: Optional[str] = None
    # Security
    api_key: Optional[str] = None

class SearchResult(BaseModel):
    video_id: str
    title: str
    artist: Optional[str] = None
    channel: str
    duration: str
    thumbnail: str
    is_playlist: bool = False
    video_count: Optional[int] = None
    # Multi-source support
    source: str = "youtube"  # "youtube", "soundcloud", "monochrome", or "soulseek"
    source_url: Optional[str] = None  # Full URL for non-YouTube sources
    quality: Optional[str] = None  # e.g., "LOSSLESS", "HI_RES_LOSSLESS", None for YouTube
    quality_score: int = 40  # For sorting (higher = better)
    slskd_username: Optional[str] = None
    slskd_filename: Optional[str] = None
    # Monochrome-specific extras (available when source == "monochrome")
    album: Optional[str] = None  # Album title from Tidal metadata

class BlacklistRequest(BaseModel):
    """Report a bad track / block an uploader."""
    job_id: Optional[str] = None
    video_id: Optional[str] = None
    uploader: Optional[str] = None
    source: str = "youtube"  # "youtube", "soundcloud", "monochrome", or "soulseek"
    reason: str = "other"  # wrong_track, poor_quality, slowed_pitched, contentid, other
    note: Optional[str] = None  # Optional free-text detail
    block_uploader: bool = False  # Also blacklist the uploader

class TestSlskdRequest(BaseModel):
    url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None

class TestNavidromeRequest(BaseModel):
    url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None

class TestJellyfinRequest(BaseModel):
    url: Optional[str] = None
    api_key: Optional[str] = None

class TestYouTubeCookiesRequest(BaseModel):
    cookies: Optional[str] = None
