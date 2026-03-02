# Changelog

## v2.0.5 (2026-03-01)

### Fixed
- **Code review fixes**: Removed duplicate except block in utils.py, replaced print() with logging throughout codebase, added SQL injection protection with column whitelist in downloads.py, fixed type annotations, added ffprobe existence check
- **Proxy support for Docker**: Added HTTP_PROXY/HTTPS_PROXY support for Telegram file uploads. Containers behind proxy can now send audio files to Telegram users
- **NO_PROXY for localhost**: Added NO_PROXY environment variable to exclude localhost requests from proxy, fixing 503 errors on local API calls

### Changed
- **Telegram direct search**: Any text message now triggers search directly without needing to click "Search music" button first
- **Chat actions in Telegram**: Added "typing" indicator during search and "upload_document" indicator during download for better UX
- **Cleaner quality labels**: Removed "LOSSLESS" from search result titles (all Monochrome tracks are lossless), show "Hi-Res" only for actual hi-res tracks
- **Configurable API URL**: Added MUSICGRABBER_API_URL environment variable for custom deployments

## v2.0.4 (2026-02-19)

### Fixed
- **Monochrome quality fallback**: Some tracks return 403 at the LOSSLESS tier (Tidal restricts certain catalogue items). Now falls back to HIGH quality automatically rather than failing the download outright
## v2.0.3 (2026-02-19)

### Changed
- **Multi-source bulk import and watched playlists**: Bulk import and watched playlist auto-downloads now search all sources (YouTube, SoundCloud, Monochrome) in parallel instead of YouTube only. The highest-scoring result wins -- so if a track is available lossless on Monochrome, that's what gets downloaded

## v2.0.2 (2026-02-16)

### Changed
- **Simplified directory picker**: Replaced the browsable tree (with breadcrumb navigation and "Browse" button) with a single flat dropdown. Lists all existing subdirectories up to 2 levels deep, plus `/music (root)` for flat downloads and a `Custom path...` option for freeform input. Fewer clicks, less faff
- **Recursive directory listing**: `/api/music-dirs` now supports `recursive=true` and `max_depth` parameters, so the dropdown fetches the full folder tree in one request instead of level-by-level AJAX calls
- **Case-insensitive directory sorting**: Folder lists are now sorted case-insensitively so `Albums` and `albums` sit together

### Added
- **Server-side `singles_subdir` validation**: The settings API now validates the subfolder path on save -- rejects traversal attempts (`..`), normalises slashes, and confirms the resolved path stays within `MUSIC_DIR`
- **Music root as download target**: Setting the subfolder to `.` (via the `/music (root)` dropdown option) saves files directly into the music directory with no subfolder

### Fixed
- **Custom path env-lock inheritance**: The custom path text input now correctly inherits the disabled state when the setting is locked via environment variable

## v2.0.1 (2026-02-15)

### Changed
- **Singles subfolder is now a dropdown**: Replaced the free-text input with a directory picker that lists existing subdirectories from the music library. Prevents typos, trailing spaces, and other user-input gremlins. Includes a "New folder..." option for creating new directories with validated input
- **Filtered system directories**: The directory picker hides dotfiles and `@`-prefixed system folders (Synology `@Recycle`, `@Recently-Snapshot`, etc.)

### Added
- **`GET /api/music-dirs` endpoint**: Lists subdirectories of `MUSIC_DIR` for the subfolder picker

## v2.0.0 (2026-02-15)

### Added
- **Full Monochrome/Tidal search**: Free-text search via the Monochrome API returns lossless FLAC results with proper artist, album, cover art, and quality metadata. Monochrome results appear alongside YouTube and SoundCloud when searching, ranked higher thanks to genuine lossless quality
- **Direct FLAC downloads from Monochrome**: Downloads bypass yt-dlp entirely -- FLAC files stream directly from the Tidal CDN. Faster, simpler, no bot detection headaches. Cover art is embedded automatically from Tidal's image CDN
- **Hi-Res and Lossless quality badges**: Search results show "Lossless" or "Hi-Res" badges with colour-coded styling. Monochrome results display album name alongside artist
- **Monochrome API preview**: Preview playback uses AAC streams from the API (browser-native, no yt-dlp subprocess)
- **Configurable API instance**: `MONOCHROME_API_URL` env var lets you point at community mirror instances
- **Default search source changed to "All"**: Searches all sources in parallel by default so Monochrome lossless results compete with YouTube/SoundCloud on quality score

### Changed
- **Monochrome metadata source label**: Downloads from Monochrome now report metadata source as "Monochrome/Tidal API" rather than "guessed" -- because Tidal actually knows what it's serving
- **Version bump to 2.0.0**: Major feature release -- Monochrome integration turns MusicGrabber from a YouTube downloader into a proper multi-source music acquisition tool


## v1.9.2 (2026-02-13)

### Added
- **MusicBrainz artist normalisation**: When MusicBrainz returns a canonical artist name, it's now used everywhere — file tags, directory name, and the jobs database. Files are automatically relocated to the correct artist folder if the name differs from the original source. Prevents duplicate artist folders from inconsistent casing or spelling across YouTube/SoundCloud uploaders

### Fixed
- **Top Artists case grouping**: Stats queries now group artists case-insensitively, displaying the most popular casing variant and summing counts across all variants. "BAD BUNNY" (2) and "Bad Bunny" (1) now merge into a single "BAD BUNNY" (3) entry
- **SoundCloud preview playback**: SoundCloud migrated some tracks to a new CDN with different format IDs (`http_mp3_standard` instead of `http_mp3_1_0`). The old format selector would fall through to HLS (`application/vnd.apple.mpegurl`) which browsers can't play. Preview now tries both format IDs before falling back


## v1.9.1 (2026-02-13)

### Added
- **AcoustID fingerprint metadata lookup**: New metadata pipeline fingerprints downloaded audio with `fpcalc`/Chromaprint, looks up AcoustID matches, and enriches year/album via MusicBrainz recording ID. Falls back to text-based MusicBrainz lookup when fingerprinting is unavailable or low-confidence
- **Flat directory mode**: New `organise_by_artist` setting and UI toggle ("Organise by Artist"). When disabled, tracks are saved directly under `Singles` with no artist subfolders
- **Stats reset action**: New `DELETE /api/stats` endpoint and "Reset Stats" button in the Stats tab to explicitly clear historical stats data
- **Metadata provenance tracking**: Jobs now store a `metadata_source` value so queue details can show where final tags came from (`AcoustID fingerprint`, `MusicBrainz text match`, or source guessed metadata for YouTube/SoundCloud/Soulseek)

### Changed
- **Queue clear semantics**: "Clear Queue" remains queue/job cleanup only; stats/history reset is now a separate explicit action
- **Duplicate/path handling across layouts**: Duplicate detection and bulk playlist file resolution now work across both folder layouts (artist subfolders and flat)
- **AcoustID configuration**: `ACOUSTID_API_KEY` now supports environment override via `ACOUSTID_API_KEY`

### Fixed
- **Settings API model mismatch**: `organise_by_artist` is now included in `SettingsUpdate`, so the Settings toggle persists correctly via `PUT /api/settings`
- **Stats reset safety**: `DELETE /api/stats` now requires explicit confirmation (`?confirm=true`) to prevent accidental history wipes
- **YouTube title edge case -> hidden output file**: Titles with trailing separators/suffixes (e.g. patterns like `Artist -- Title - Official Video`) could be cleaned to an empty title, causing yt-dlp to output hidden files like `.webm.flac` and fail with "audio file not found". Parsing now rejects empty cleaned titles, falls back safely, and download naming enforces a non-empty basename

## v1.9.0 (2026-02-10)

### Added
- **SoundCloud search**: Search SoundCloud via yt-dlp `scsearch` — returns results with correct artist (from `uploader` field), duration, thumbnails, and quality scoring. No auth required
- **Source selector**: Segmented button group (YouTube / SoundCloud / All) on the search bar. Selection persisted to localStorage. "All" searches both sources in parallel and merges results by quality score
- **Extensible source architecture**: New `search.py` module with `SOURCE_REGISTRY` dict — adding a new source is one search function and one registry entry. Includes `search_source()`, `search_all()`, and `get_available_sources()` API
- **`GET /api/sources` endpoint**: Returns available search sources with labels, badges, and colours for the frontend
- **SoundCloud downloads**: Full download pipeline support — SoundCloud URLs route through yt-dlp without YouTube-specific cookie/backoff logic
- **SoundCloud preview**: Hover-to-preview works for SoundCloud tracks (passes source URL to the preview endpoint)
- **Source badges**: Search results and queue items show coloured source badges (YT red, SC orange, SLK teal) with consistent `getSourceBadge()` / `getSourceLabel()` helpers
- **Donation link**: Added a subtle Ko-fi "Buy me a coffee" link with coffee icon in Settings (`https://ko-fi.com/geekphreek`)
- **Amazon Music playlist import**: Paste a public Amazon Music playlist URL and import the tracks into bulk import. Uses headless Playwright to scrape Amazon's JS-rendered pages, handling cookie consent banners and virtualised scrolling. Extracted 132 unique tracks from a 139-track playlist in testing (7 were duplicates). Supports user playlists, curated playlists, and all regional Amazon domains
- **Generalised playlist endpoint**: New `/api/fetch-playlist` endpoint routes to Spotify or Amazon scraper based on URL. Old `/api/spotify-playlist` path kept as backwards-compat alias
- **Custom singles subfolder**: New `singles_subdir` setting in Settings > General lets you change the download subfolder name (default: `Singles`). Overridable via `SINGLES_SUBDIR` env var. Changes take effect immediately without restart
- **Source badges on queue items**: Queue entries now show a coloured source badge (YT/SC/SLK) in the bottom-right corner of each card

- **Report / Blacklist system**: Flag bad tracks (wrong track, poor quality, slowed/pitched, ContentID dodge) directly from the queue with a Report button. Blacklisted videos are hidden from search results and bulk imports; blocked uploaders get a heavy score penalty so they sink to the bottom. Manage all entries in Settings > Blacklist with one-click removal
- **Blacklist API**: New `POST /api/blacklist`, `GET /api/blacklist`, `DELETE /api/blacklist/{id}` endpoints for reporting and managing blacklisted tracks and uploaders
- **Uploader tracking**: Jobs now store the raw uploader/channel name (separate from the cleaned artist name) for accurate blacklist matching

### Changed
- **Honest audio quality reporting**: FLAC files converted from lossy sources now show their true origin (e.g. "FLAC (from MP3 128kbps)" instead of "FLAC 44.1kHz 24bit"). The min-bitrate quality gate also uses the source bitrate, so a 64kbps Opus wrapped in FLAC won't sneak past
- **Search routing**: `/api/search` now dispatches via `search.py` based on `source` param instead of calling `search_youtube()` directly
- **Download routing**: `process_download()` accepts optional `source_url` param; SoundCloud downloads skip YouTube ID validation, cookie handling, and 403 retry logic
- **Preview routing**: `/api/preview/{video_id}` accepts `source` and `url` query params for non-YouTube sources
- **Retry routing**: `/api/jobs/{job_id}/retry` passes stored `source_url` for SoundCloud re-downloads
- **Stats source breakdown**: Now shows YouTube, SoundCloud, and Soulseek counts with correct colours
- **Playlist input UX**: Bulk import playlist URL field now uses generic wording and includes a supported-services hint/tooltip driven from a centralised service list
- **Watched playlists**: URL input and description updated to mention Amazon Music alongside Spotify and YouTube

### Fixed
- **SoundCloud preview**: SoundCloud returns HLS `.m3u8` playlist URLs for `bestaudio` which browsers can't play natively in `<audio>`. Preview now requests the direct HTTP MP3 stream (`http_mp3_1_0`) instead
- **SoundCloud queue false-fail**: Fixed a thread spawn bug where SoundCloud downloads were inserted as `queued` but the API returned an error (`Failed to queue`) because keyword args (`source_url`) were not forwarded to the background thread helper
- **Queue delete button state**: "Delete File" now persists per job after successful deletion (`file_deleted=1`), renders as disabled/greyed "File Deleted", and is reset when "Re-download" is clicked
- **Delete button for missing files**: If a file was deleted externally, the delete button now greys out automatically instead of throwing an error. The jobs list checks file existence on load and updates the flag in the database
- **Empty singles subfolder fallback**: Clearing the singles subfolder setting no longer dumps files into the music root -- it falls back to "Singles"
- **Queue action buttons with special characters**: "Delete File" and "Report" now work reliably for tracks with apostrophes/quotes in artist or title. Switched from fragile inline argument interpolation to data-attribute event binding
- **Queue card expansion state**: Expanded queue items now stay expanded across refreshes after actions like delete/report/reload, instead of collapsing unexpectedly

## v1.8.5 (2026-02-08)

### Added
- **Dark/light theme toggle**: Moon/sun button in the header switches between dark and light themes. Preference saved to localStorage
- **Webhook notifications**: New generic webhook URL setting — sends a JSON POST on download completion/failure with event type, title, artist, status, source, and track counts. Configure via Settings > Notifications or the `WEBHOOK_URL` env var
- **Statistics dashboard**: New "Stats" tab with download overview — completed/failed counts, success rate, library storage usage, daily download chart (last 14 days), source breakdown (YouTube vs Soulseek), top 10 artists, and recent downloads
- **Search analytics in Stats**: Search queries are now logged and shown in the Stats tab with total searches, successful search rate, search-to-download conversion, and most searched artists
- **Delete from library**: Completed jobs in the queue now have a "Delete File" button that removes the audio file and lyrics from disk, plus cleans up empty artist directories
- **Re-download**: Completed and failed jobs now have a "Re-download" button in the queue details to re-queue the download (overwrites existing file)
- **Audio quality display**: Completed downloads now show the audio quality (e.g. "FLAC 44.1kHz 16bit", "OPUS 160kbps") in the queue job details
- **Minimum bitrate setting**: New "Minimum Audio Bitrate" setting in Settings > General. Downloads below this bitrate are automatically rejected with a clear error message. Set to 0 (default) to disable. Lossless formats (FLAC) always pass

### Changed
- **Tab bar**: Now horizontally scrolls on narrow screens to accommodate the sixth tab without wrapping
- **Date display format**: UI dates now render consistently as `YYYY-MM-DD` instead of locale-specific formats

### Fixed
- **Audio quality: 64kbps downloads**: Removed the forced Android YouTube player client (`player_client=android`) which was causing yt-dlp to pull very low bitrate audio (64kbps). Downloads now use YouTube's default web client which serves full-quality audio (~160kbps Opus). An env-var escape hatch (`YTDLP_PLAYER_CLIENT`) is available if needed
- **Search conversion overcounting**: Search-to-download conversion now uses a per-search server token instead of matching raw query text, preventing repeated identical searches from inflating conversion rate
- **Search attribution trust boundary**: Download attribution now validates server-issued search tokens and ignores invalid/untrusted values
- **Search analytics retention**: Added automatic pruning of old `search_logs` rows (90-day retention) to keep stats queries fast and DB growth bounded

## v1.8.4 (2026-02-06)

### Fixed
- **Playlist download permissions**: Audio files downloaded as part of a playlist now get `set_file_permissions` applied, matching single track and Soulseek downloads. Previously, playlist tracks had different permissions on NAS/SMB shares
- **Silent download success with no file**: `process_download` now raises an error if no audio file is found after yt-dlp completes, instead of silently marking the job as completed with no file on disk
- **Stale job timestamp mismatch**: Stale job cleanup now uses SQLite's native `datetime()` functions instead of Python `isoformat()`, fixing a string comparison mismatch between `T` and space separators
- **Scheduler crash on bad playlist URL**: `fetch_playlist_tracks` now guards the `list=` regex match, preventing an `AttributeError` crash if a stored YouTube URL has no `list=` parameter

### Changed
- **Search scoring: duration awareness**: Results are now scored by duration — typical song length (1:30–7:00) gets a bonus, while clips (<30s), snippets (<90s), extended mixes (12–20min), and full albums (20min+) are penalised
- **Search scoring: view count tiebreaker**: View count is now a modest scoring signal — suspiciously low views (<1K) get a small penalty, high views (100K+) get a small bonus. Deliberately conservative to avoid penalising niche artists
- **Search scoring: Official Audio boost**: "Official Audio" bonus increased from +20 to +35, matching the Topic channel bonus — both signal official studio audio, which is the ideal source for a music grabber
- **Title cleaning: trailing suffixes**: `clean_title()` now strips unbracketed trailing suffixes like "- Official Audio", "- Official Music Video", and "- Official Lyric Video", plus any dangling separators left after cleanup
- **Audio extensions centralised**: The repeated `['.flac', '.opus', '.m4a', '.webm', '.mp3', '.ogg']` list (5 occurrences) is now a single `AUDIO_EXTENSIONS` constant in `constants.py`
- **Navidrome auth deduplicated**: Subsonic API auth logic (salt, MD5 token, params) extracted to `subsonic_auth_params()` in `utils.py`, fixing inconsistent API versions and client names between test and scan endpoints
- **Bulk import thread pool**: Downloads spawned by bulk imports now use a `ThreadPoolExecutor(max_workers=3)` instead of unbounded daemon threads, preventing hundreds of concurrent yt-dlp subprocesses on large imports
- **Bulk import DB connection**: The bulk import worker now acquires and releases DB connections per query instead of holding one for its entire lifetime (which could be hours)
- **Spotify browser script extracted**: The 130-line Playwright f-string with double-brace escaping is now a standalone `spotify_browser.py` script that receives parameters via environment variables — proper syntax highlighting, linting, and no escaping bugs
- **Dockerfile version pins**: Python packages now pinned with compatible release specifiers (`~=`) for reproducible builds
- **Entrypoint banner**: Replaced hardcoded `http://localhost:38274` (Docker host port) with a message showing the actual container port (8080)

### Removed
- **beautifulsoup4**: Removed unused dependency from Dockerfile (~500KB saved)
- **Dead section header**: Removed empty "Legacy Bulk Import (synchronous)" comment block from `app.py`
- **Unused enumerate**: Removed discarded index variable in playlist download loop

## v1.8.3 (2026-02-04)

### Added
- **PUID/PGID support**: Run the container as a specific user/group for correct file ownership (like *arr stack). Set `PUID=1000` and `PGID=1000` in your environment to match your host user
- **Preview button visibility**: The play/preview button on search results is now always visible (dimmed) and highlights on hover, making the feature more discoverable
- **Volume mount warning**: Shows a dismissible warning banner if the music directory doesn't appear to be mounted as a volume (helps catch misconfigured setups where downloads would be lost on container restart)
- **Custom tooltips**: Search results now show "Hover to preview, click to download" tooltip after 0.25s (faster and more reliable than native browser tooltips)

### Fixed
- **Queue timestamps ignore timezone**: Timestamps in the queue now correctly respect the user's timezone. SQLite stores times in UTC, and the frontend now properly interprets them as UTC before converting to local time

## v1.8.2 (2026-02-03)

(Skipped - changes merged into 1.8.3)

## v1.8.1 (2026-01-31)

### Added
- **Queue timestamps ignore timezone**: Timestamps in the queue now correctly respect the user's timezone. SQLite stores times in UTC, and the frontend now properly interprets them as UTC before converting to local time

## v1.8.1 (2026-01-31)

### Added
- **Settings clear buttons**: All text and password settings now have an inline "Clear" button that clears the field and saves in a single click

### Fixed
- **Download permission errors**: When yt-dlp fails with a permission denied error on temp file rename (e.g. `Brunette.temp.flac` → `Brunette.flac`), leftover `.temp.*` files are now cleaned up and the download is retried automatically. Applies to both single track and playlist downloads

## v1.8.0 (2026-01-31)

### Changed
- **Codebase split**: Monolithic `app.py` (~4778 lines) split into 15 focused modules — `app.py` is now a thin route layer, with main logic in `constants.py`, `models.py`, `db.py`, `settings.py`, `utils.py`, `middleware.py`, `youtube.py`, `slskd.py`, `spotify.py`, `metadata.py`, `notifications.py`, `downloads.py`, `bulk_import.py`, and `watched_playlists.py`
- **Notification function renamed**: `send_telegram_notification` → `send_notification`
- **Dockerfile**: Now copies all Python modules (`COPY *.py`) instead of just `app.py`
- **YouTube backoff settings**: Warns when min/max are misconfigured and swapped
- **Title cleaning**: Consolidated title cleanup regexes into a single pass
- **Search ranking**: YouTube scoring now uses query-aware token matching and stricter artist/title alignment
- **DB connections**: Switched call sites to a context-managed connection helper to ensure closes on error
- **YouTube cookies**: Added a Settings upload button and automatic cooldown when cookies appear stale
- **Background work**: Standardized background downloads/retries to use daemon threads
- **Background threads**: Centralized the daemon thread helper in `utils` for shared use
- **Bulk import search**: Reused shared YouTube search parsing/scoring logic to avoid drift
- **SQLite pooling**: Added a small connection pool for reuse
- **SQLite pooling fix**: Enabled cross-thread connections for pooled reuse in FastAPI
- **File permissions**: Audio files now get `0o666` instead of `0o777` (no execute bit)
- **Bulk import progress**: Progress display now tracks downloads through to completion instead of showing "Complete" while tracks are still downloading
- **YouTube Topic channels**: Artist names from YouTube auto-generated "- Topic" channels are now cleaned up properly
- **Rate limiting**: Added periodic cleanup to prevent long-lived IP entries from accumulating
- **Scheduler jitter**: Watched playlist checks add a small random offset to avoid synchronized polling

### Removed
- **Sync bulk import endpoint**: Removed `/api/bulk-import` (the sync, event-loop-blocking version). Use `/api/bulk-import-async` instead
- **Legacy bulk import model**: Removed unused `BulkImportRequest`
- **Notification alias**: Removed unused `send_telegram_notification` alias

### Fixed
- **Search scoring**: Removed duplicate cover/remix penalty in YouTube scoring
- **YouTube ID validation**: Added basic ID validation before building yt-dlp URLs
- **Title splitting**: Hyphens in compound words (e.g. "T-4") no longer incorrectly split artist from title
- **Variable safety**: `process_download` no longer uses fragile `dir()` checks for variable existence
- **Playlist track failures**: Fixed `NameError` (`processed_tracks` -> `completed_tracks`) that caused a single track failure to kill the entire playlist job
- **Download success path**: Fixed indentation bug where successful first-attempt downloads skipped metadata, library scans, and job completion
- **Connection pool safety**: `row_factory` is now reset when connections are returned to the pool, preventing leaked state between callers
- **DB rollback semantics**: Only roll back open transactions on `db_conn()` exit
- **YouTube cookie test cleanup**: Temp cookie files are now cleaned up on all failure paths
- **yt-dlp retry logic**: Consolidated cookie/backoff retry logic to avoid drift across download paths
- **API key compare**: Constant-time comparison for API keys
- **Search input validation**: Added max length constraints to search queries
- **MusicBrainz UA**: Standardized the User-Agent URL used for MusicBrainz lookups

## v1.7.1 (2026-01-30)

### Added
- **Watched playlist FLAC controls**: Per-playlist FLAC toggle plus a "Convert to FLAC" option when adding a watched playlist
- **Queue job details**: Click completed/failed items in the queue to expand and see source URL, queued/completed timestamps, and download duration
- **Source URL tracking**: Jobs now store the YouTube URL or Soulseek path they were downloaded from
- **Stale job detection**: Background monitor marks stuck downloading/queued jobs as failed after 15 minutes of no progress. Also runs at startup to catch jobs orphaned by container restarts
- **YouTube cookie support**: Paste browser cookies in Settings to authenticate yt-dlp requests and avoid YouTube 403 bot-detection blocks. Includes a "Test Cookies" button that validates against YouTube before saving
- **YouTube 403 auto-retry**: Downloads that hit a 403/Forbidden error automatically retry up to 2 times with increasing backoff. Failed jobs show a clear hint about cookies in the queue error message

### Changed
- **Watched playlist creation**: Now honours the FLAC setting selected at creation time
- **Settings env lock badge**: Replaced "ENV" with a clearer "CONFIG LOCKED" pill
- **Clear Queue**: Now also cleans up stale/stuck downloads, not just completed and failed jobs
- **YouTube download client**: Default yt-dlp player client set to Android to reduce bot blocks (reverted in v1.8.5 — caused 64kbps audio)
- **Bot backoff**: Queue now applies a randomized delay after bot/403 signals to ease rate limits

### Fixed
- **Env-locked settings**: Greyed out locked fields and added hover hint explaining they are set via docker-compose.yml
- **Stuck downloads**: Jobs that were permanently stuck in "downloading" status (e.g. from crashed background tasks or container restarts) are now automatically timed out and can be cleared
- **Queue errors**: Completed jobs now clear stale error messages


## v1.7.0 (2026-01-25)

### Added
- **Settings tab**: New UI tab for configuring all integrations without editing docker-compose.yml
  - Configure slskd, Navidrome, Jellyfin connections
  - Set up notification channels (Telegram, SMTP)
  - Toggle MusicBrainz metadata and lyrics fetching
  - Test connection buttons for slskd, Navidrome, Jellyfin
  - Password fields with show/hide toggle
  - Environment variables override database values (shown as locked in UI)
- **API authentication**: Optional API key protection for all endpoints
  - Set API key in Settings or via `API_KEY` environment variable
  - Frontend prompts for key and stores in browser localStorage
  - Clear/change stored key via Settings UI
- **Rate limiting**: 60 requests per minute per IP address
  - Proper 429 responses with `Retry-After` header
  - `X-RateLimit-Limit`, `X-RateLimit-Remaining` headers on all API responses
  - Respects `X-Forwarded-For` for reverse proxy setups

### Changed
- **Configuration approach**: Settings can now be managed via UI instead of environment variables
- **Security section in README**: Updated with API key authentication details

### Fixed
- **Watched playlist scheduler**: Now checks for due playlists immediately on startup instead of waiting for the first interval to elapse
- **Test connection buttons**: Now use current form values instead of requiring save first
- **Test connection result display**: Results now properly appear after testing
- **Settings save**: Only saves fields that have actually changed (prevents saving placeholder text)
- **FLAC toggle sync**: Header FLAC toggle and Settings FLAC checkbox now stay in sync

### Technical Details
- Settings stored in SQLite `settings` table
- `AuthMiddleware` handles API key validation and rate limiting
- `/api/config` endpoint now returns `auth_required` flag
- All fetch calls wrapped in `apiFetch()` for automatic auth header injection

## v1.6.1 (2026-01-22)

### Added
- **Copy playlist URL**: Watched playlists now include a "Copy URL" button in the UI
- **Watched playlist bulk import**: Newly watched playlists now queue downloads via bulk import
- **Notifications**: Get notified when downloads complete or fail
  - Telegram support via webhook URL (`TELEGRAM_WEBHOOK_URL`)
  - Email support via SMTP (`SMTP_HOST`, `SMTP_USER`, etc.)
  - Shared triggers for all channels (`NOTIFY_ON`): singles, playlists, bulk, errors

### Changed
- **Watched playlist refresh**: Refresh now requeues missing tracks and only pulls what is not yet downloaded

### Fixed
- **Watched playlist download tracking**: Completed jobs now update watched track download status
- **Favicon not showing in browser**: Added mount in FastAPI

## v1.6.0 (2026-01-20)

### Added
- **Centralised configuration constants**: All timeout values and magic numbers now defined at top of `app.py` for easy tuning
- **Dynamic version display**: Frontend now fetches version from `/api/config` endpoint instead of hardcoding

### Changed
- **Version management**: Single `VERSION` constant used throughout backend (FastAPI app, User-Agent strings, API responses)
- **Consistent User-Agent**: All HTTP clients now use `MusicGrabber/{VERSION}` format (fixed outdated `1.1.0` in lyrics fetcher)

### Technical Details
New constants section at top of `app.py`:
- Timeout values: `TIMEOUT_YTDLP_*`, `TIMEOUT_SLSKD_*`, `TIMEOUT_HTTP_*`, `TIMEOUT_FFMPEG_CONVERT`, `TIMEOUT_SPOTIFY_BROWSER`
- Bulk import: `BULK_IMPORT_SEARCH_DELAY`, `BULK_IMPORT_BACKOFF_DELAYS`, `BULK_IMPORT_BACKOFF_RESET_AFTER`
- Playlist: `PLAYLIST_WAIT_MAX`, `PLAYLIST_WAIT_INTERVAL`
- Search: `YOUTUBE_SEARCH_MULTIPLIER`, `YOUTUBE_SEARCH_MIN_FETCH`, `SLSKD_MAX_RESULTS`, `SLSKD_MIN_QUALITY_SCORE`
- Files: `MAX_FILENAME_LENGTH`

### Removed
- Dead code block in Spotify browser scraper (unreachable `for row in []` loop)

## v1.5.2 (2026-01-19)

### Added
- **Full Spotify playlist support**: Large playlists (100+ tracks) now fully supported via headless browser scraping
- **Playwright integration**: Added Chromium-based browser automation for Spotify pages that exceed embed limits
- **Virtualized scroll handling**: Extracts tracks incrementally while scrolling to handle Spotify's lazy-loading
- **Cookie consent automation**: Automatically dismisses Spotify's cookie banner during scraping

### Technical Details
- Spotify's embed endpoint only returns ~100 tracks maximum
- For larger playlists, MusicGrabber launches a headless Chromium browser via Playwright
- The browser loads the full Spotify page, accepts cookies, then scrolls through the tracklist
- Tracks are extracted incrementally during scrolling (Spotify uses virtualized lists that unload off-screen items)
- Only numbered tracks are extracted, filtering out "Recommended" suggestions at the bottom
- Docker image now includes Playwright and Chromium (~400MB additional)
- Added `shm_size: 2gb` to docker-compose for Chromium's shared memory requirements

### Changed
- Dockerfile now installs Playwright and Chromium browser
- Updated README with detailed Spotify integration documentation

### Removed
- Spotify API authentication code (Spotify has disabled new app creation, so this was unusable)

### Fixed
- **Soulseek retry bug**: Failed Soulseek downloads can now be retried correctly (metadata is persisted in jobs table)
- **Playlist status reporting**: Playlist jobs now track individual track failures and report partial success
- **Path traversal protection**: slskd download paths are now validated to prevent copying files from outside allowed directories

### Security
- Added security documentation to README about lack of built-in authentication
- Recommend reverse proxy with auth for external access

## v1.5.1 (2026-01-18)

### Added
- **Async bulk import**: Large playlist imports (1000+ tracks) now process asynchronously with real-time progress tracking
- **Parallel search and download**: Downloads start immediately as tracks are found, rather than waiting for all searches to complete
- **Rate limiting protection**: Automatic exponential backoff (30s, 60s, 120s, 300s) when YouTube returns 429 errors
- **Spotify album support**: Can now import from Spotify album URLs in addition to playlists
- **Jellyfin integration**: Added support for Jellyfin library refresh after downloads (configure via `JELLYFIN_URL` and `JELLYFIN_API_KEY`)
- **Progress UI**: New 5-column progress display showing Searched, Queued, Done, Failed, and Total counts

### Fixed
- Unicode escape errors when parsing Spotify track names with special characters
- Recent Activity now shows recently processed tracks correctly during bulk imports

### Changed
- Removed 70-track limit on bulk imports
- Bulk import state persisted to database for resilience across restarts

## v1.5.0

### Added
- Jellyfin integration for automatic library refresh

## v1.4.1

### Fixed
- Improved slskd download handling for transient failures

## v1.4.0

### Added
- Soulseek/slskd integration for higher quality sources (requires VPN port forwarding)

## v1.3.0

### Added
- Spotify public playlist import
- Various database async fixes

## Earlier versions

- YouTube search and download via yt-dlp
- MusicBrainz metadata lookups
- LRClib lyrics fetching
- Navidrome library refresh trigger
- Duplicate detection
- M3U playlist generation
- FLAC conversion
