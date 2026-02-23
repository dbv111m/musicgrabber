"""
MusicGrabber - Application Constants

All shared constants in one place for easy tuning.
"""

import os
from pathlib import Path

VERSION = "2.0.4"

# Timeout values (in seconds)
TIMEOUT_YTDLP_INFO = 30          # Getting video/playlist info
TIMEOUT_YTDLP_SEARCH = 30        # Search queries
TIMEOUT_YTDLP_DOWNLOAD = 300     # Downloading a track (5 minutes)
TIMEOUT_YTDLP_PREVIEW = 15       # Getting preview URL
TIMEOUT_YTDLP_PLAYLIST = 60      # Getting playlist contents
TIMEOUT_FFMPEG_CONVERT = 120     # Converting audio formats
TIMEOUT_HTTP_REQUEST = 10        # MusicBrainz, LRClib, Navidrome API calls
TIMEOUT_HTTP_SPOTIFY = 30        # Spotify embed fetch
TIMEOUT_SLSKD_SEARCH = 12        # Soulseek search polling
TIMEOUT_SLSKD_DOWNLOAD = 600     # Soulseek download (10 minutes)
TIMEOUT_SLSKD_API = 30           # slskd API calls
TIMEOUT_SPOTIFY_BROWSER = 180    # Headless browser for large playlists (3 minutes)
TIMEOUT_AMAZON_BROWSER = 180     # Amazon Music playlist scraping (3 minutes)
TIMEOUT_FPCALC = 30              # Audio fingerprinting via fpcalc
TIMEOUT_MONOCHROME_API = 15      # Monochrome/Tidal API calls (search + manifest)
STALE_JOB_TIMEOUT = 900          # Mark downloading/queued jobs as failed after 15 minutes
STALE_JOB_CHECK_INTERVAL = 120   # Check for stale jobs every 2 minutes

# Bulk import settings
BULK_IMPORT_SEARCH_DELAY = 1.0           # Seconds between searches (be courteous to all sources)
BULK_IMPORT_BACKOFF_DELAYS = [30, 60, 120, 300]  # Rate limit backoff sequence (unused, kept for reference)
BULK_IMPORT_BACKOFF_RESET_AFTER = 5      # Consecutive successes before reducing backoff (unused, kept for reference)

# Playlist creation
PLAYLIST_WAIT_MAX = 3600         # Max seconds to wait for downloads to complete (1 hour)
PLAYLIST_WAIT_INTERVAL = 10      # Seconds between completion checks

# Search and results
YOUTUBE_SEARCH_MULTIPLIER = 3    # Fetch N times more results than requested for scoring
YOUTUBE_SEARCH_MIN_FETCH = 30    # Minimum results to fetch for scoring
SOUNDCLOUD_SEARCH_MULTIPLIER = 2 # Less noise on SoundCloud, so fewer extras needed
SOUNDCLOUD_SEARCH_MIN_FETCH = 15 # Minimum results to fetch for scoring
SLSKD_MAX_RESULTS = 20           # Max Soulseek results to return
SLSKD_MIN_QUALITY_SCORE = 50     # Minimum quality score to include result
MAX_SEARCH_QUERY_LENGTH = 512    # Max characters allowed in search input
SEARCH_LOG_RETENTION_DAYS = 90   # Keep search analytics for N days

# File handling
MAX_FILENAME_LENGTH = 200        # Maximum characters in sanitised filenames
COOKIES_FILE = Path("/data/cookies.txt")  # yt-dlp cookies file path
AUDIO_EXTENSIONS = ['.flac', '.opus', '.m4a', '.webm', '.mp3', '.ogg']

# YouTube 403 retry
YTDLP_403_MAX_RETRIES = 2       # Retry attempts on 403/Forbidden errors
YTDLP_403_RETRY_DELAY = 3       # Seconds between retries

# YouTube bot/backoff handling
BOT_BACKOFF_MIN_SECONDS = 5
BOT_BACKOFF_MAX_SECONDS = 20

# YouTube player client override (empty = yt-dlp default / web client)
YTDLP_PLAYER_CLIENT = os.getenv("YTDLP_PLAYER_CLIENT", "")

# Rate limiting
RATE_LIMIT_REQUESTS = 60         # Max requests per IP per window
RATE_LIMIT_WINDOW = 60           # Window size in seconds

# Configuration from environment - structural paths
MUSIC_DIR = Path(os.getenv("MUSIC_DIR", "/music"))
DB_PATH = Path(os.getenv("DB_PATH", "/data/music_grabber.db"))

# Other settings that don't change at runtime (not in UI)
SLSKD_REQUIRE_FREE_SLOT = os.getenv("SLSKD_REQUIRE_FREE_SLOT", "true").lower() == "true"
SLSKD_MAX_RETRIES = int(os.getenv("SLSKD_MAX_RETRIES", "5"))
WATCHED_PLAYLIST_CHECK_HOURS = int(os.getenv("WATCHED_PLAYLIST_CHECK_HOURS", "24"))

# AcoustID audio fingerprinting — because guessing metadata from titles
# is about as reliable as asking YouTube commenters for facts
ACOUSTID_API_KEY = os.getenv("ACOUSTID_API_KEY", "0NILMQojj4")
ACOUSTID_MIN_SCORE = 0.8         # Below this, the match is too dodgy to trust

# Monochrome API — Tidal frontend with public lossless FLAC streams.
# Points at the official instance by default; users can override to use
# community mirrors listed at github.com/monochrome-music/monochrome/blob/main/INSTANCES.md
MONOCHROME_API_URL = os.getenv("MONOCHROME_API_URL", "https://api.monochrome.tf")
MONOCHROME_COVER_BASE = "https://resources.tidal.com/images"

# Default settings for fields that need startup values
DEFAULT_CONVERT_TO_FLAC = os.getenv("DEFAULT_CONVERT_TO_FLAC", "true").lower() == "true"
