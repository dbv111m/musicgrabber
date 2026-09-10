"""
MusicGrabber - Application Constants

All shared constants in one place for easy tuning.
"""

import os
from pathlib import Path

VERSION = "4.1.2"


def _normalise_root_path(value: str) -> str:
    value = (value or "").strip()
    if not value or value == "/":
        return ""
    return "/" + value.strip("/")

# Timeout values (in seconds)
TIMEOUT_YTDLP_INFO = 30          # Getting video/playlist info
TIMEOUT_YTDLP_SEARCH = 30        # Search queries
# Wall-clock ceiling for a whole multi-source search. Fast sources finish in a
# second or two; this stops one limping source (looking at you, freemp3cloud)
# from holding the entire response hostage. Stragglers are abandoned, not awaited.
# Set generously enough to let Monochrome's lossless ladder finish on a bad day
# (direct lookup down, on the slow fallback) rather than silently dropping the one
# source that actually serves FLAC. The proper fix for the wait is a progress UI.
SEARCH_ALL_DEADLINE = int(os.getenv("SEARCH_ALL_DEADLINE", "30"))  # Multi-source search collection deadline
# Each provider has one admission slot, so two searches wanting the same source
# at the same moment means one waits. How long it is prepared to wait depends
# entirely on who is asking: a user watching a spinner would rather have results
# a moment late than an error, while a bulk import has nobody watching at all and
# would much rather wait than lose a track for good. Both sit comfortably inside
# SEARCH_ALL_DEADLINE, since a source that spends the whole deadline queueing
# would be reported as timed out and contribute nothing anyway.
# Measured rather than guessed: a YouTube search takes ~2.5s on an idle box but
# 5 to 7s under any real load, so the original 5s was almost exactly one search
# long and amounted to a coin toss. A wait has to comfortably outlast the search
# in front of it or it may as well not be a wait at all. Results a few seconds
# late beat an error every time; the user only ever waits when something else is
# genuinely running.
SEARCH_SLOT_WAIT_INTERACTIVE = float(os.getenv("SEARCH_SLOT_WAIT_INTERACTIVE", "15"))
SEARCH_SLOT_WAIT_AUTOMATED = float(os.getenv("SEARCH_SLOT_WAIT_AUTOMATED", "15"))
TIMEOUT_YTDLP_DOWNLOAD = int(os.getenv("TIMEOUT_YTDLP_DOWNLOAD", "300"))  # Downloading a track (5 minutes)
# Getting a preview URL. yt-dlp now solves YouTube's player challenge through
# Deno, which is a few seconds when the answer is cached and rather more when
# YouTube has just shipped a new player. 15s sat right on that boundary and
# turned a slow resolve into a 504.
TIMEOUT_YTDLP_PREVIEW = int(os.getenv("TIMEOUT_YTDLP_PREVIEW", "30"))
TIMEOUT_YTDLP_PLAYLIST = 60      # Getting playlist contents
TIMEOUT_FFMPEG_CONVERT = int(os.getenv("TIMEOUT_FFMPEG_CONVERT", "120"))  # Converting audio formats

# Long-form YouTube splitting (DJ mixes, "CD1/CD2" compilation uploads, etc.)
LONGFORM_SPLIT_THRESHOLD_SECONDS = 720  # 12 minutes; videos shorter than this aren't split candidates
TIMEOUT_YTDLP_COMMENTS = 45      # Bounded comment fetch when hunting for a cue sheet
TIMEOUT_LONGFORM_DOWNLOAD = 900  # Audio-only download of a long mix wants more headroom than a normal track

# Loudness normalisation (EBU R128, two-pass ffmpeg loudnorm; lossy web sources only).
# -14 LUFS is what Spotify and YouTube level to, so normalised grabs sit comfortably
# next to streamed stuff instead of alternating between whisper and jet engine.
LOUDNORM_TARGET_I = float(os.getenv("LOUDNORM_TARGET_I", "-14.0"))     # Integrated loudness target (LUFS)
LOUDNORM_TARGET_TP = float(os.getenv("LOUDNORM_TARGET_TP", "-1.0"))    # True-peak ceiling (dBTP)
LOUDNORM_TARGET_LRA = float(os.getenv("LOUDNORM_TARGET_LRA", "11.0"))  # Loudness range target (LU)
LOUDNORM_SKIP_DELTA_LU = float(os.getenv("LOUDNORM_SKIP_DELTA_LU", "1.0"))  # Already this close to target? Skip the re-encode
TIMEOUT_LOUDNORM = int(os.getenv("TIMEOUT_LOUDNORM", "180"))  # Per loudnorm ffmpeg pass

# ReplayGain 2.0. Tags only: we measure the file and write the numbers, then the
# player decides what to do about it. The audio itself is never re-encoded, which
# is the entire point of preferring this to baked-in normalisation.
# The spec's reference is -18 LUFS; -14 would make everything quieter than the
# rest of the world expects, so resist the temptation to match LOUDNORM_TARGET_I.
REPLAYGAIN_REFERENCE_LUFS = float(os.getenv("REPLAYGAIN_REFERENCE_LUFS", "-18.0"))
REPLAYGAIN_MAX_ALBUM_TRACKS = int(os.getenv("REPLAYGAIN_MAX_ALBUM_TRACKS", "60"))  # Sanity cap on the album pass

# Shared quality tiers, higher == better. Used by the upgrades scanner (which
# tiers files on disk) and by the search results filter (which tiers what a
# source claims it will give us). One scale, so "320 or better" means the same
# thing in both places. Lossless is one flat tier on purpose: a FLAC is a FLAC,
# and we never "upgrade" one lossless wrapper into another.
TIER_UNKNOWN = 0     # Source declined to say; not the same as "bad"
TIER_LOSSY_128 = 1
TIER_LOSSY_192 = 2
TIER_LOSSY_256 = 3
TIER_LOSSY_320 = 4
TIER_LOSSLESS = 5


def kbps_to_tier(kbps: int) -> int:
    """Bucket an effective average bitrate into a lossy tier."""
    if kbps >= 300:
        return TIER_LOSSY_320
    if kbps >= 240:
        return TIER_LOSSY_256
    if kbps >= 170:
        return TIER_LOSSY_192
    return TIER_LOSSY_128
TIMEOUT_HTTP_REQUEST = 10        # MusicBrainz, LRClib, Navidrome API calls
TIMEOUT_HTTP_SPOTIFY = 30        # Spotify embed fetch
SPOTIFY_EMBED_MAX_ATTEMPTS = 3   # Spotify's embed edge throws transient 502/503/504s; retry before giving up
SPOTIFY_EMBED_RETRY_BACKOFF = 1.5  # Seconds, multiplied by attempt number for a simple linear backoff

# Tidal's embed player now server-renders only the first 50 tracks, so we read
# playlists from the public API instead. The token below is lifted from the
# embed's own JS bundle; if Tidal ever rotates it we re-scrape it on a 401.
TIDAL_API_BASE = "https://api.tidal.com/v1"
TIDAL_API_TOKEN = "TPIsV0A9lyiqKl9u"   # Public embed-player token, not a credential
TIDAL_API_PAGE_SIZE = 100              # Tidal 400s on anything larger; it is not negotiable
TIDAL_API_COUNTRY = "GB"               # Required param; totals are the same whichever you pick
TIMEOUT_SLSKD_SEARCH = 12        # Soulseek search polling
SLSKD_EMPTY_RETRY_DELAY = 1      # Pause before a single retry when Soulseek comes back empty (its distributed search is moody)
TIMEOUT_SLSKD_DOWNLOAD = 600     # Soulseek download (10 minutes)
TIMEOUT_SLSKD_API = 30           # slskd API calls
TIMEOUT_SPOTIFY_BROWSER = 180    # Headless browser base/floor timeout (3 minutes)
SPOTIFY_BROWSER_STALL_SECONDS = 30  # No-progress cutoff while scrolling long Spotify playlists
# Spotify's public page is now a JS shell with no server-rendered track count, so we
# can no longer size the browser timeout up front from a cheap HTTP fetch. Instead the
# headless browser reads the real count from the rendered DOM and uses it as the scroll
# completion target. The outer subprocess timeout is a generous ceiling; the browser
# self-terminates (target reached, scroll stalled, or its own deadline) and returns
# whatever it has, so a huge playlist yields partial results instead of being hard-killed
# and silently falling back to the truncated 100-track embed.
SPOTIFY_BROWSER_MAX_SECONDS = 1500       # Generous ceiling for very large playlists (~25 min)
SPOTIFY_BROWSER_SECONDS_PER_TRACK = 0.12 # Per-track time estimate when the count is known
SPOTIFY_BROWSER_BASE_OVERHEAD = 120      # Fixed startup/page-load overhead in the estimate
SPOTIFY_BROWSER_DEADLINE_BUFFER = 15     # Browser bows out this many seconds before the hard kill
TIMEOUT_AMAZON_BROWSER = 180     # Amazon Music playlist scraping (3 minutes)
TIMEOUT_AMAZON_ALBUM_BROWSER = 90  # Amazon Music album naming; only needs a page title
AMAZON_ALBUM_TITLE_WAIT = 45     # How long the album script waits for Amazon to name the page
TIMEOUT_FPCALC = 30              # Audio fingerprinting via fpcalc
TIMEOUT_MP3PHOENIX_SEARCH = int(os.getenv("TIMEOUT_MP3PHOENIX_SEARCH", "30"))
TIMEOUT_MP3PHOENIX_DOWNLOAD = int(os.getenv("TIMEOUT_MP3PHOENIX_DOWNLOAD", "180"))
# A preview takes the opening chunk and hangs up rather than pulling the lot, so
# it waits nothing like as long as a download. A megabyte is roughly 25 seconds
# at 320 kbps, which is ample for deciding whether it is the right song, and MP3
# is quite happy being cut off mid-flow.
TIMEOUT_MP3PHOENIX_PREVIEW = int(os.getenv("TIMEOUT_MP3PHOENIX_PREVIEW", "45"))
MP3PHOENIX_PREVIEW_BYTES = int(os.getenv("MP3PHOENIX_PREVIEW_BYTES", str(1024 * 1024)))
# How long the Chrome bootstrap gets to clear Cloudflare and hand back cookies.
# Forwarded to mp3phoenix_browser.py; the parent's own kill timer allows this
# plus a buffer, so the child isn't killed mid-clearance (see MONOCHROME_BROWSER_AUTH_TIMEOUT).
MP3PHOENIX_BROWSER_TIMEOUT = int(os.getenv("MP3PHOENIX_BROWSER_TIMEOUT", "75"))
MP3PHOENIX_SESSION_TTL = int(os.getenv("MP3PHOENIX_SESSION_TTL", "3600"))
MP3PHOENIX_BROWSER_FAILURE_LIMIT = 3
MP3PHOENIX_BROWSER_FAILURE_COOLDOWN = int(os.getenv("MP3PHOENIX_BROWSER_FAILURE_COOLDOWN", "600"))
TIMEOUT_ZVU4NO_SEARCH = 15       # zvu4no HTML search
TIMEOUT_ZVU4NO_DOWNLOAD = int(os.getenv("TIMEOUT_ZVU4NO_DOWNLOAD", "120"))  # zvu4no direct MP3 stream
TIMEOUT_FREEMP3CLOUD_SEARCH = 20    # FreeMp3Cloud landing + form POST (two round-trips)
TIMEOUT_FREEMP3CLOUD_DOWNLOAD = int(os.getenv("TIMEOUT_FREEMP3CLOUD_DOWNLOAD", "120"))  # FreeMp3Cloud direct MP3 stream
TIMEOUT_MONOCHROME_SEARCH = 15   # Monochrome and direct-Qobuz catalogue lookups
# Whole Tidal metadata leg, across every endpoint and query variant
MONOCHROME_HIFI_SEARCH_BUDGET = float(os.getenv("MONOCHROME_HIFI_SEARCH_BUDGET", "15"))
TIMEOUT_MONOCHROME_DOWNLOAD = int(os.getenv("TIMEOUT_MONOCHROME_DOWNLOAD", "300"))  # Qobuz FLAC CDN download (FLACs are big)

# Preview snippets: Monochrome's browser leg only ever yields encrypted
# FLAC-in-MP4, so a hover preview is decrypted to a short MP3 and served
# locally. Measured at roughly 1.2s to prepare, hence the modest timeout.
PREVIEW_SNIPPET_SECONDS = int(os.getenv("PREVIEW_SNIPPET_SECONDS", "30"))
PREVIEW_SNIPPET_TTL = int(os.getenv("PREVIEW_SNIPPET_TTL", "600"))
PREVIEW_SNIPPET_MAX_CACHED = int(os.getenv("PREVIEW_SNIPPET_MAX_CACHED", "20"))
TIMEOUT_PREVIEW_SNIPPET_BUILD = int(os.getenv("TIMEOUT_PREVIEW_SNIPPET_BUILD", "60"))
STALE_JOB_TIMEOUT = 900          # Mark downloading/queued jobs as failed after 15 minutes
STALE_JOB_CHECK_INTERVAL = 120   # Check for stale jobs every 2 minutes

# Bulk imports run for as long as they need, so they get judged on a progress
# heartbeat rather than a wall clock. An hour of complete silence means the
# worker thread has died; the import itself is fine and can be picked back up.
STALE_BULK_IMPORT_TIMEOUT = int(os.getenv("STALE_BULK_IMPORT_TIMEOUT", "3600"))
# How many times an import may be brought back to life before we accept it is
# never going to finish. Counts restarts too: whatever killed it, three goes is
# plenty, and an unbounded retry is just a slow-motion infinite loop.
STALE_BULK_IMPORT_MAX_RESUMES = int(os.getenv("STALE_BULK_IMPORT_MAX_RESUMES", "3"))
# Past this age we stop trying to be helpful. Resuming a months-old import means
# downloading a pile of music you had almost certainly stopped waiting for.
STALE_BULK_IMPORT_ABANDON_AFTER = int(os.getenv("STALE_BULK_IMPORT_ABANDON_AFTER", str(7 * 24 * 3600)))

# Long-form splits don't resume mid-cut (unlike bulk imports); a quiet one just
# gets marked failed once its heartbeat goes stale for this long.
STALE_LONGFORM_SPLIT_TIMEOUT = int(os.getenv("STALE_LONGFORM_SPLIT_TIMEOUT", "3600"))

# SQLite locking. busy_timeout is how long sqlite itself will queue behind
# another writer before giving up; the retry settings are our own second chance
# on top of that, for the cases sqlite refuses to wait for at all (a stale WAL
# snapshot fails instantly, no queueing involved).
DB_BUSY_TIMEOUT_MS = int(os.getenv("DB_BUSY_TIMEOUT_MS", "10000"))
DB_LOCK_RETRY_ATTEMPTS = 4       # Total tries per statement, including the first
DB_LOCK_RETRY_DELAY = 0.2        # First backoff pause in seconds, doubling thereafter
DB_LOCK_RETRY_MAX_DELAY = 1.5    # Ceiling for that doubling, so we never nap for ages
LIBRARY_RECONCILE_INTERVAL = int(os.getenv("LIBRARY_RECONCILE_INTERVAL", "1800"))  # Reconcile deleted/renamed files every 30 minutes

# Bulk import settings
BULK_IMPORT_SEARCH_DELAY = 1.0           # Seconds between searches (be courteous to all sources)
# An empty search is very often a wobble rather than a verdict: a second import
# holding the per-source admission slot, a proxy having a moment, a provider
# rate-limiting us. Give the track a few goes before writing it off, because a
# track marked 'failed' is never looked at again.
BULK_IMPORT_SEARCH_ATTEMPTS = 3          # Total tries per track, including the first
BULK_IMPORT_SEARCH_RETRY_DELAY = 4.0     # First backoff pause in seconds, doubling thereafter
BULK_IMPORT_SEARCH_RETRY_MAX_DELAY = 30.0  # Ceiling for that doubling; an album shouldn't take all afternoon

# Playlist creation
PLAYLIST_WAIT_MAX = 3600         # Max seconds to wait for downloads to complete (1 hour)
PLAYLIST_WAIT_INTERVAL = 10      # Seconds between completion checks

# Search and results
YOUTUBE_SEARCH_MULTIPLIER = 3    # Fetch N times more results than requested for scoring
YOUTUBE_SEARCH_MIN_FETCH = 30    # Minimum results to fetch for scoring
SOUNDCLOUD_SEARCH_MULTIPLIER = 2 # Less noise on SoundCloud, so fewer extras needed
SOUNDCLOUD_SEARCH_MIN_FETCH = 15 # Minimum results to fetch for scoring
SLSKD_MAX_RESULTS = 20           # Max Soulseek results to return
SEARCH_MAX_PER_SOURCE = 4        # Baseline contribution cap for an "All" search; sparse source sets can expand to fill the requested page
SEARCH_MAX_PER_SOURCE_YOUTUBE = 6
SEARCH_MAX_PER_SOURCE_MP3PHOENIX = 4
SEARCH_MAX_PER_SOURCE_SOUNDCLOUD = 4
SEARCH_MAX_PER_SOURCE_ZVU4NO = 4
SEARCH_MAX_PER_SOURCE_FREEMP3CLOUD = 4
SEARCH_MAX_PER_SOURCE_SOULSEEK = 6
SEARCH_MAX_PER_SOURCE_MONOCHROME = 6
AUTOMATED_SEARCH_CACHE_TTL_SECONDS = 900  # Reuse identical bulk/watch searches briefly without keeping direct links stale for long
AUTOMATED_SEARCH_CACHE_MAX_ENTRIES = 500 # Bound the in-memory cache on large libraries
SLSKD_MIN_QUALITY_SCORE = 50     # Minimum quality score to include result
SLSKD_MATCH_CONFIDENCE_FLOOR = float(os.getenv("SLSKD_MATCH_CONFIDENCE_FLOOR", "0.55"))  # 0.0-1.0; reject worse than this
SLSKD_ARTIST_MATCH_FLOOR = 0.85  # How closely a path segment must resemble the searched artist to be believed
SLSKD_HASH_CHUNK_BYTES = 1024 * 1024  # Read size when verifying a cross-filesystem move
SLSKD_PRUNE_MAX_DEPTH = 6        # How far up to sweep empty folders after a move, before we stop being helpful
# Cross-source fallback (source_offline_fallback): a swapped-in candidate must clear
# this confidence bar so we don't "rescue" a dead source by grabbing the wrong song.
# Stricter than the slskd floor on purpose; a silent fail beats a confident wrong track.
FALLBACK_MATCH_CONFIDENCE_FLOOR = float(os.getenv("FALLBACK_MATCH_CONFIDENCE_FLOOR", "0.70"))  # 0.0-1.0
MAX_SEARCH_QUERY_LENGTH = 512    # Max characters allowed in search input
SEARCH_LOG_RETENTION_DAYS = 90   # Keep search analytics for N days

# File handling
MAX_FILENAME_LENGTH = 200        # Maximum characters in sanitised filenames
# NAME_MAX is 255 bytes on ext4 and most Linux/NAS filesystems, and it counts
# bytes, not characters. sanitize_filename caps each part (artist, title) at
# MAX_FILENAME_LENGTH chars, but "Artist - Title" can still combine to well over
# 255 bytes and blow up mid-download with [Errno 36] File name too long. The
# reserve leaves headroom for the extension, yt-dlp's intermediate suffixes
# (.fNNN, .part, .temp) and dedup numbering like " (1)".
MAX_FILENAME_BYTES = 255         # Per-component byte limit (NAME_MAX)
FILENAME_STEM_RESERVE_BYTES = 40 # Headroom kept free below NAME_MAX for the stem
COOKIES_FILE = Path("/data/cookies.txt")  # yt-dlp cookies file path
AUDIO_EXTENSIONS = ['.flac', '.opus', '.m4a', '.webm', '.mp3', '.ogg']

# Directory names that library scans must never walk into. NAS and desktop
# operating systems love to drop a hidden bin inside the very share you asked
# them to look after, so a deleted album can sit in @Recycle for weeks looking
# for all the world like it is still in the library. Matched case-insensitively
# on the directory name alone, at any depth.
#
# Also covers macOS/Windows metadata dumps, and the fseventsd/Spotlight caches
# that make a Time Machine volume take a fortnight to scan.
EXCLUDED_SCAN_DIR_NAMES = frozenset({
    '.trash',                # MusicGrabber's own bin, plus generic *nix
    '.trash-1000',           # Linux/Synology per-UID trash (see also prefix match below)
    '.trashes',              # macOS on removable/network volumes
    '@recycle',              # QNAP Network Recycle Bin
    '@recycle.bin',          # QNAP, older firmware
    '#recycle',              # Synology Recycle Bin
    '$recycle.bin',          # Windows/SMB shares
    'recycler',              # Windows, pre-Vista
    '.upgrade_quarantine',   # MusicGrabber's upgrade holding pen
    '.mg_staging',           # per-job download staging, see STAGING_DIR_NAME
    '@eadir',                # Synology thumbnail/index sidecar folders
    '.ds_store',             # macOS, a directory in some sync-tool wreckage
    '.spotlight-v100',       # macOS Spotlight index
    '.fseventsd',            # macOS filesystem event log
    '.documentrevisions-v100',
    '.temporaryitems',
    'system volume information',  # Windows/SMB
    'lost+found',            # fsck salvage, never music
})

# Per-UID Linux trash folders are `.Trash-1000`, `.Trash-1001`, and so on, so an
# exact-name set can never catch the lot. Prefix-matched, case-insensitively.
EXCLUDED_SCAN_DIR_PREFIXES = ('.trash-',)

# Each download gets its own directory under {music_dir}/.mg_staging/ to work in.
# yt-dlp writes its .part, .webp and final files there, and only the finished
# audio is moved into the library. Two jobs for the same track therefore cannot
# scribble on each other's working files, which they cheerfully used to do.
STAGING_DIR_NAME = '.mg_staging'

# YouTube 403 retry
YTDLP_403_MAX_RETRIES = 2       # Retry attempts on 403/Forbidden errors
YTDLP_403_RETRY_DELAY = 3       # Seconds between retries

# YouTube bot/backoff handling
BOT_BACKOFF_MIN_SECONDS = 5
BOT_BACKOFF_MAX_SECONDS = 20

# YouTube player client override (empty = yt-dlp default / web client)
YTDLP_PLAYER_CLIENT = os.getenv("YTDLP_PLAYER_CLIENT", "")

# Rate limiting
RATE_LIMIT_REQUESTS = 200        # Max requests per IP per window  -  single-user tool, be generous
RATE_LIMIT_WINDOW = 60           # Window size in seconds

# Login hardening
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCKOUT_SECONDS = int(os.getenv("LOGIN_LOCKOUT_SECONDS", "900"))
LOGIN_ATTEMPT_WINDOW = int(os.getenv("LOGIN_ATTEMPT_WINDOW", "900"))

# Download token auth (for browser file downloads without exposing session tokens in URLs)
DOWNLOAD_TOKEN_TTL_SECONDS = int(os.getenv("DOWNLOAD_TOKEN_TTL_SECONDS", "60"))

# Stream token auth. Same idea, but an <audio> element makes range requests as it
# plays and seeks, so this one is reusable and has to outlive the longest track
# anyone is likely to sit through. An hour should cover even the prog rock.
STREAM_TOKEN_TTL_SECONDS = int(os.getenv("STREAM_TOKEN_TTL_SECONDS", "3600"))

# Transport security
HTTPS_ONLY = os.getenv("HTTPS_ONLY", "false").lower() == "true"
HSTS_MAX_AGE = int(os.getenv("HSTS_MAX_AGE", "31536000"))
ALLOW_API_KEY_QUERY_PARAM = os.getenv("ALLOW_API_KEY_QUERY_PARAM", "false").lower() == "true"

# Configuration from environment - structural paths
MUSIC_DIR = Path(os.getenv("MUSIC_DIR", "/music"))
DB_PATH = Path(os.getenv("DB_PATH", "/data/music_grabber.db"))
ROOT_PATH = _normalise_root_path(os.getenv("ROOT_PATH", ""))

# Other settings that don't change at runtime (not in UI)
SLSKD_REQUIRE_FREE_SLOT = os.getenv("SLSKD_REQUIRE_FREE_SLOT", "true").lower() == "true"
SLSKD_MAX_RETRIES = int(os.getenv("SLSKD_MAX_RETRIES", "5"))
WATCHED_PLAYLIST_CHECK_HOURS = int(os.getenv("WATCHED_PLAYLIST_CHECK_HOURS", "24"))
WATCHED_HISTORY_RECHECK_HOURS = int(os.getenv("WATCHED_HISTORY_RECHECK_HOURS", "24"))
# How often the track-upgrades scan walks the library (hours). Cheap and network-free,
# so daily is plenty; this is not time-sensitive.
UPGRADE_SCAN_INTERVAL_HOURS = int(os.getenv("UPGRADE_SCAN_INTERVAL_HOURS", "24"))
# How long a per-candidate upgrade search result stays cached before a revisit
# re-searches (seconds). Keeps the Watched Upgrades page snappy without re-hammering
# sources on every visit. Default 4 hours.
UPGRADE_SEARCH_TTL_SECONDS = int(os.getenv("UPGRADE_SEARCH_TTL_SECONDS", str(4 * 3600)))
# Minimum match confidence (0..1) for a found result to count as the same track.
# Deliberately high: we are proposing to replace a file, not just rank a search.
UPGRADE_MATCH_FLOOR = float(os.getenv("UPGRADE_MATCH_FLOOR", "0.6"))
WATCHED_REFRESH_STALE_SECONDS = int(os.getenv("WATCHED_REFRESH_STALE_SECONDS", "1800"))
# How many consecutive "not found" (404) refreshes before we assume a watched
# playlist has genuinely vanished upstream and auto-pause it. We wait for a few
# strikes so a transient blip, a private playlist, or an expired login token
# doesn't get a playlist paused on the strength of one bad fetch.
WATCHED_GONE_STRIKES_BEFORE_PAUSE = int(os.getenv("WATCHED_GONE_STRIKES_BEFORE_PAUSE", "3"))

# AcoustID audio fingerprinting  -  because guessing metadata from titles
# is about as reliable as asking YouTube commenters for facts.
# API key is now configurable via Settings; this is just the confidence threshold.
ACOUSTID_MIN_SCORE = 0.8         # Below this, the match is too dodgy to trust
MIN_SONG_DURATION_SECS = 30      # Files shorter than this are too brief to fingerprint reliably
MAX_AUDIO_START_OFFSET_SECS = 1.0  # Start offsets above this indicate a preview segment, not a full track
MB_DURATION_TOLERANCE = 0.10     # 10% either side of MusicBrainz expected duration; outside = wrong track
# Manual picks may intentionally differ from MusicBrainz, but the downloaded
# bytes should still resemble the duration advertised by the exact result the
# user clicked. Only reject a substantial shortfall so rounding and metadata
# drift do not turn into false preview alarms.
SELECTED_RESULT_DURATION_TOLERANCE = 0.10
SELECTED_RESULT_MIN_SHORTFALL_SECS = 15.0
SILENCE_DETECT_DURATION = 8.0    # Seconds of continuous silence that flags a sabotaged track
SILENCE_DETECT_NOISE = -50.0     # dB threshold below which audio counts as silence
SILENCE_DETECT_MIN_START = 15.0  # Ignore silence that starts before this point (legitimate intros)
SILENCE_DETECT_MAX_END_FRAC = 0.60  # Only scan the first 60% of the track, leaves hidden/secret tracks alone

# Watched artists  -  MusicBrainz artist search and singles polling
MB_ARTIST_SEARCH_LIMIT = 5       # Candidate results returned when searching by name
TIMEOUT_MUSICBRAINZ_ARTIST = 10  # Artist search + singles listing HTTP timeout

# Recording lookup  -  choosing which album a downloaded track belongs to.
#
# MusicBrainz scores text matches purely on string similarity, so every recording
# of a popular song ties on 100 and the ordering between them is arbitrary. It is
# also not stable: the same query run twice returns different slices, which for
# Radiohead managed 0% overlap across three attempts. Asking for one result and
# hoping it is the studio album is therefore a raffle, and we kept losing.
#
# So we ask for a proper shortlist, ask a second time with a studio-album filter,
# and pick the recording before picking the release. Tuned against the corpus in
# tests/fixtures/mb_corpus.json; see tests/tools/eval_mb_strategies.py.
MB_RECORDING_SEARCH_LIMIT = 25   # Shortlist size per query; 100 measured no better
MB_TEXT_SCORE_FLOOR = 85         # Below this the text match is too shaky to trust
MB_RECORDING_SPREAD_WEIGHT = 3.0  # Weight on log1p(release count) when ranking recordings
MB_ALT_TAKE_PENALTY = 12.0       # Penalty for live/remix/demo markers in a recording title
# A recording seen on only a handful of releases is weak evidence for popular
# songs: live bootlegs often occupy the whole search page one recording at a
# time. In that narrow case, resolve the exact-title release group instead.
MB_RELEASE_GROUP_FALLBACK_MAX_RELEASES = 4
# Penalty when the recording title is what we asked for plus extra qualifiers,
# e.g. "(Boombox Rehearsals)" or "(Masters at Work RAW dub)". Deliberately heavy:
# when every candidate is a variant the penalty cancels out, so it can only help.
MB_TITLE_MISMATCH_PENALTY = 25.0

# Lucene fragment restricting a recording search to official studio albums. Used
# as a second opinion, never on its own: it is lethal to tracks whose only home is
# a single or an EP, which is most of dance music.
MB_STUDIO_ALBUM_FILTER = (
    " AND primarytype:album AND status:official"
    " AND NOT secondarytype:live AND NOT secondarytype:compilation"
)

# ListenBrainz API  -  used for similar artist exploration and "Created for You" playlists
# (public API, no auth required for either)
LISTENBRAINZ_API_URL = "https://api.listenbrainz.org"
TIMEOUT_LISTENBRAINZ = 10
TIMEOUT_LISTENBRAINZ_PLAYLIST = 15   # Per-playlist JSPF fetch

# Cover art fallback chain  -  we try really hard to get proper album art
COVER_ART_TIMEOUT = 10           # Per-source HTTP timeout for cover art fetches
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
DEEZER_SEARCH_URL = "https://api.deezer.com/search"

# Default settings for fields that need startup values
# Enables conversion to AUDIO_FORMAT; it has never meant "always FLAC", which is
# precisely why the old DEFAULT_CONVERT_TO_FLAC spelling had to go. That spelling
# is still honoured so existing deployments carry on unchanged.
DEFAULT_CONVERT_AUDIO = os.getenv(
    "DEFAULT_CONVERT_AUDIO",
    os.getenv("DEFAULT_CONVERT_TO_FLAC", "false"),
).lower() == "true"

# Monochrome (Qobuz/Tidal). The known public instances (us-west/api/eu-central
# .monochrome.tf, monochrome-api.samidy.com) all stopped working once Tidal
# revoked their OAuth client, so there is no honest public default any more;
# this stays empty unless you point it at a self-hosted hifi-api of your own.
MONOCHROME_HIFI_API_URL = os.getenv("MONOCHROME_HIFI_API_URL", "")
# Current Monochrome playback is protected by a browser Turnstile exchange. A
# short-lived JWT obtained by the real web app can be presented to the unified
# playback API; SeleniumBase supplies the normal browser session only for that
# exchange, while the audio itself still travels through httpx.
MONOCHROME_BROWSER_FALLBACK_ENABLED = os.getenv(
    "MONOCHROME_BROWSER_FALLBACK_ENABLED", "true"
).lower() == "true"
MONOCHROME_WEB_URL = os.getenv("MONOCHROME_WEB_URL", "https://monochrome.tf")
MONOCHROME_BROWSER_AUTH_TIMEOUT = int(os.getenv("MONOCHROME_BROWSER_AUTH_TIMEOUT", "75"))
# The Settings "Test Monochrome" diagnostic downloads this for real, then throws
# it away. Radiohead's Creep is already the qbdlx probe track: old, everywhere,
# and staying everywhere. Keep it to something no catalogue is likely to lose.
MONOCHROME_TEST_QUERY = os.getenv("MONOCHROME_TEST_QUERY", "Radiohead - Creep")

# Deezer public API: no key, no auth, no CAPTCHA, and remarkably typo-tolerant.
# Used as the primary ISRC oracle for Monochrome search, and to rescue tracks
# whose Tidal-supplied ISRC is junk (yes, Tidal ships ISRCs with ampersands in).
DEEZER_API_URL = os.getenv("DEEZER_API_URL", "https://api.deezer.com")
TIMEOUT_DEEZER = int(os.getenv("TIMEOUT_DEEZER", "10"))

# Deezer-as-metadata-fallback: MusicBrainz is canonical but slow to ingest new
# releases, so fresh singles come back album-less and never leave Singles/. When
# MB gives us no album we ask Deezer, but only trust it if it is confident it's
# the same track. The floor keeps us from routing a track into the wrong album.
DEEZER_METADATA_MATCH_FLOOR = float(os.getenv("DEEZER_METADATA_MATCH_FLOOR", "0.65"))
DEEZER_METADATA_SEARCH_LIMIT = int(os.getenv("DEEZER_METADATA_SEARCH_LIMIT", "5"))

# qbdlx fallback: if browser-authenticated Monochrome playback comes up empty,
# sign the official Qobuz API using a shared free-account token (the same pool
# the qbdlx web UI uses).
# Heads up: the free shared tokens deliver 16/44.1 lossless FLAC, not 24-bit
# hi-res, so this is a "a real FLAC beats a failed download" safety net.
QBDLX_FALLBACK_ENABLED = os.getenv("QBDLX_FALLBACK_ENABLED", "true").lower() == "true"
QBDLX_SHARED_TOKENS_URL = os.getenv(
    "QBDLX_SHARED_TOKENS_URL",
    "https://citegptapi.f5.si/webhook/qbdlx/shared",
)
QBDLX_QOBUZ_API_BASE = os.getenv("QBDLX_QOBUZ_API_BASE", "https://www.qobuz.com/api.json/0.2/")
QBDLX_TOKEN_CACHE_TTL = int(os.getenv("QBDLX_TOKEN_CACHE_TTL", "600"))  # re-fetch the pool every N seconds

# Source health checks: living the pirate lifestyle means free services come and
# go, so we check whether each source can actually deliver before showing its
# results. A failed check parks the source for a cooldown, then we re-check.
SOURCE_HEALTH_CHECK_INTERVAL = int(os.getenv("SOURCE_HEALTH_CHECK_INTERVAL", "600"))  # re-check a source's health at most this often (seconds)
SOURCE_HEALTH_COOLDOWN = int(os.getenv("SOURCE_HEALTH_COOLDOWN", "600"))              # how long a failed source stays auto-disabled (seconds)
SERVICECHECK_TIMEOUT = int(os.getenv("SERVICECHECK_TIMEOUT", "8"))                    # per-source health probe timeout (seconds)

# Library scans: serve cached listings and bound duplicate lookup waits.
LIBRARY_SCAN_TTL_SECONDS = 300
LIBRARY_INDEX_WAIT_SECONDS = 5
LIBRARY_SCAN_RETRY_SECONDS = 30
