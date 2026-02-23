# Music Grabber 🎵

**v2.0.4**

A self-hosted music acquisition service. Search YouTube, SoundCloud, and Monochrome (Tidal lossless) -- tap a result and it downloads the best quality audio as FLAC straight into your music library.

If you find it useful, consider buying me a coffee: https://ko-fi.com/geekphreek

## Why?

Lidarr's great for albums, but grabbing a single track you heard on the radio shouldn't require navigating menus or pulling an artist's entire discography. This is for the "I want one song, not a commitment" use case.

## Features
New in **v2.0.0**:
- **Monochrome/Tidal lossless search** -- full free-text search via the Monochrome API. Returns genuine lossless FLAC results with proper artist, album, cover art, and quality metadata. Results show "Lossless" or "Hi-Res" badges and rank above YouTube when available
- **Direct FLAC downloads** -- Monochrome downloads bypass yt-dlp entirely. FLAC streams directly from the Tidal CDN with embedded cover art and accurate metadata from the Tidal catalogue. Faster and more reliable than YouTube extraction
- **"All" is now the default search** -- searches YouTube, SoundCloud, and Monochrome in parallel. Lossless Monochrome results float to the top; YouTube and SoundCloud fill in the gaps for tracks not on Tidal
- **Monochrome preview** -- hover-to-preview works for Monochrome tracks using AAC streams (browser-native, no yt-dlp subprocess)
- **Configurable Monochrome instance** -- `MONOCHROME_API_URL` env var lets you point at community mirror instances

and the rest of them:
- **Mobile-friendly UI** -- designed for quick searches from your phone
- **Dark/light theme** -- toggle between themes with the moon/sun button; preference saved per browser
- **Settings tab** -- configure all integrations via UI (no docker-compose editing required)
- **Optional API authentication** -- protect your instance with an API key
- **Hover to preview** -- on desktop, hover over a result for 2 seconds to hear a preview (works for YouTube, SoundCloud, and Monochrome)
- **Multi-source search** -- YouTube, SoundCloud, and Monochrome (Tidal lossless) with parallel searching and quality-based ranking
- **Soulseek integration** -- optional slskd support for higher quality sources (FLAC from P2P) *(in progress -- needs testing)*
- **Playlist support** -- download entire playlists with automatic M3U generation
- **Watched playlists** -- monitor Spotify/YouTube playlists and auto-download new tracks; searches all sources and grabs the best quality available
- **Bulk import** -- paste or upload a text file of songs to auto-search and queue; searches YouTube, SoundCloud, and Monochrome in parallel, picks the best result
- **Best quality FLAC** -- extracts highest available audio quality
- **Minimum bitrate enforcement** -- optionally reject downloads below a configurable bitrate threshold
- **Audio quality display** -- completed downloads show codec and bitrate in the queue details, with honest reporting for lossy-to-FLAC conversions
- **Enhanced metadata** -- AcoustID audio fingerprinting with MusicBrainz lookups, falling back to source-embedded/guessed tags
- **Synced lyrics** -- automatic lyrics fetching from LRClib, saved as `.lrc` files
- **Auto-organise** -- creates `Singles/Artist/Title.flac` structure (or flat `Singles/Title.flac` when "Organise by Artist" is off)
- **Duplicate detection** -- skips already-downloaded tracks
- **Job queue** -- track download progress, retry failed jobs, re-download or delete files from the queue, and see metadata provenance (`Metadata:` shows AcoustID fingerprint, MusicBrainz text match, or source guessed)
- **Statistics dashboard** -- download counts, success rate, daily chart, top artists, search analytics
- **Webhook notifications** -- get notified via Telegram, email, or generic webhook on download events
- **YouTube cookie support** -- upload browser cookies in Settings to bypass YouTube bot detection
- **Optional Navidrome/Jellyfin integration** -- auto-triggers library rescan after downloads

## Why FLAC?

For YouTube and SoundCloud, FLAC conversion is primarily for standardisation and consistent tagging -- it does not improve audio quality beyond the source, it only preserves what is already there. **Monochrome downloads are genuine lossless** -- the FLAC comes directly from the Tidal CDN, so you get the real deal. If you prefer to keep the original format from YouTube/SoundCloud, disable FLAC conversion and files will be saved as-is.

## Screenshots

| Search & Results | Bulk Import | Queue |
|:---:|:---:|:---:|
| ![Search and Results](assets/SearchAndResults.png) | ![Bulk Import](assets/BulkImport.png) | ![Queue](assets/Queue.png) |

| Watched Playlists | Settings | Dark & Light Theme |
|:---:|:---:|:---:|
| ![Watched Playlists](assets/WatchedPlaylists.png) | ![Settings](assets/SettingsTab.png) | ![Dark and Light Theme](assets/NightAndDay.png) |

## Quick Start

### Option A: Using Docker Hub (Recommended)

1. **Create a docker-compose.yml**
   ```yaml
   services:
     music-grabber:
       image: g33kphr33k/musicgrabber:latest
       container_name: music-grabber
       restart: unless-stopped
       # Required for Spotify playlists over 100 tracks (headless browser)
       shm_size: '2gb'
       ports:
         - "38274:8080"
       volumes:
         - /path/to/your/music:/music
         - ./data:/data
       environment:
         - MUSIC_DIR=/music
         - DB_PATH=/data/music_grabber.db
         - ENABLE_MUSICBRAINZ=true
         - DEFAULT_CONVERT_TO_FLAC=true
         # Optional: Run as specific user (like *arr stack) for correct file permissions
         # - PUID=1000
         # - PGID=1000
         # Optional: Navidrome auto-rescan
         # - NAVIDROME_URL=http://navidrome:4533
         # - NAVIDROME_USER=admin
         # - NAVIDROME_PASS=yourpassword
         # Optional: Jellyfin auto-rescan
         # - JELLYFIN_URL=http://jellyfin:8096
         # - JELLYFIN_API_KEY=your-jellyfin-api-key
         # Optional: Notifications
         # - NOTIFY_ON=playlists,bulk,errors
         # - TELEGRAM_WEBHOOK_URL=https://api.telegram.org/bot{token}/sendMessage?chat_id={chat_id}
         # - WEBHOOK_URL=https://your-webhook-endpoint.com/hook
         # - SMTP_HOST=smtp.example.com
         # - SMTP_PORT=587
         # - SMTP_USER=user@example.com
         # - SMTP_PASS=password
         # - SMTP_TO=you@example.com
         # Optional: Use a Monochrome mirror instead of the default instance
         # - MONOCHROME_API_URL=https://api.monochrome.tf
   ```

2. **Run**
   ```bash
   docker compose up -d
   ```

3. **Access the UI** at `http://your-server:38274`

### Option B: Build from Source

1. **Clone and configure**
   ```bash
   git clone https://gitlab.com/g33kphr33k/musicgrabber.git
   cd musicgrabber
   ```

2. **Edit docker-compose.yml**

   Update the music volume path and optionally add Navidrome credentials:
   ```yaml
   volumes:
     - /path/to/your/music:/music  # <-- your music directory
     - ./data:/data                # <-- keep the job database
   ```
   ```yaml
   environment:
     - NAVIDROME_URL=http://navidrome:4533
     - NAVIDROME_USER=admin
     - NAVIDROME_PASS=yourpassword
   ```

3. **Build and run**
   ```bash
   docker compose up -d --build
   ```

4. **Access the UI**

   Open `http://your-server:38274` on your phone or browser.

## Configuration

### Settings Tab (Recommended)

The easiest way to configure MusicGrabber is via the **Settings tab** in the UI. You can configure:

- **General**: MusicBrainz metadata, lyrics fetching, default FLAC conversion, minimum audio bitrate, artist subfolder organisation
- **Soulseek (slskd)**: URL, credentials, downloads path
- **Navidrome**: URL and credentials for library refresh
- **Jellyfin**: URL and API key for library refresh
- **Notifications**: Telegram webhook, generic webhook URL, and SMTP settings
- **YouTube**: Upload browser cookies for authenticated downloads
- **Blacklist**: View and manage reported tracks and blocked uploaders
- **Security**: API key for authentication

Settings are stored in the database and persist across container restarts.

**Environment variable overrides:** If you set a value via environment variable, it takes precedence over the database value and appears as "locked" in the UI.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PUID` | `0` | User ID for file ownership (like *arr stack) |
| `PGID` | `0` | Group ID for file ownership (like *arr stack) |
| `MUSIC_DIR` | `/music` | Music library root inside container |
| `DB_PATH` | `/data/music_grabber.db` | SQLite database path |
| `ENABLE_MUSICBRAINZ` | `true` | Enable MusicBrainz metadata lookups |
| `ENABLE_LYRICS` | `true` | Enable automatic lyrics fetching from LRClib |
| `DEFAULT_CONVERT_TO_FLAC` | `true` | Convert downloads to FLAC by default (can be toggled per-download in UI) |
| `MIN_AUDIO_BITRATE` | `0` | Minimum audio bitrate in kbps. Downloads below this are rejected. 0 = disabled. Lossless (FLAC) always passes |
| `ORGANISE_BY_ARTIST` | `true` | Create artist subfolders under Singles. Set to `false` for a flat directory |
| `WEBHOOK_URL` | - | Generic webhook URL -- receives JSON POST on download completion/failure |
| `MONOCHROME_API_URL` | `https://api.monochrome.tf` | Monochrome API URL -- override to use a community mirror instance |
| `YTDLP_PLAYER_CLIENT` | *(empty)* | Override yt-dlp YouTube player client (expert-only, e.g. `android`, `web,android`) |
| `NAVIDROME_URL` | - | Navidrome server URL (e.g., `http://navidrome:4533`) |
| `NAVIDROME_USER` | - | Navidrome username for API |
| `NAVIDROME_PASS` | - | Navidrome password for API |
| `JELLYFIN_URL` | - | Jellyfin server URL (e.g., `http://jellyfin:8096`) |
| `JELLYFIN_API_KEY` | - | Jellyfin API key for library refresh |
| `SLSKD_URL` | - | slskd API URL (e.g., `http://slskd:5030`) |
| `SLSKD_USER` | - | slskd username |
| `SLSKD_PASS` | - | slskd password |
| `SLSKD_DOWNLOADS_PATH` | - | Path where slskd downloads are accessible (required for Soulseek downloads) |
| `SLSKD_REQUIRE_FREE_SLOT` | `true` | Only show Soulseek results from users with free upload slots |
| `SLSKD_MAX_RETRIES` | `5` | Max retry attempts for failed Soulseek downloads |
| `WATCHED_PLAYLIST_CHECK_HOURS` | `24` | How often to check watched playlists (in hours): 24=daily, 168=weekly, 720=monthly, 0=disabled |
| `NOTIFY_ON` | `playlists,bulk,errors` | Notification triggers (applies to all channels): `singles`, `playlists`, `bulk`, `errors` |
| `TELEGRAM_WEBHOOK_URL` | - | Full Telegram webhook URL (see Notifications section below) |
| `SMTP_HOST` | - | SMTP server hostname |
| `SMTP_PORT` | `587` | SMTP server port |
| `SMTP_USER` | - | SMTP username |
| `SMTP_PASS` | - | SMTP password |
| `SMTP_FROM` | - | From address (defaults to SMTP_USER) |
| `SMTP_TO` | - | Recipient address(es), comma-separated |
| `SMTP_TLS` | `true` | Use STARTTLS |
| `API_KEY` | - | API key for authentication (see Security section) |

### Navidrome Auto-Rescan

To automatically trigger a library scan after downloads, add your Navidrome credentials:

```yaml
environment:
  - NAVIDROME_URL=http://navidrome:4533
  - NAVIDROME_USER=admin
  - NAVIDROME_PASS=yourpassword
```

If running on the same Docker network as Navidrome, use the container name as the hostname.

### Jellyfin Auto-Rescan

To automatically trigger a Jellyfin library scan after downloads:

```yaml
environment:
  - JELLYFIN_URL=http://jellyfin:8096
  - JELLYFIN_API_KEY=your-api-key-here
```

Get your API key from Jellyfin: Dashboard → API Keys → Add.

### Notifications (Optional)

Get notified when downloads complete or fail via Telegram, email, or a generic webhook. Configure one or more channels -- the same triggers apply to all.

**Notification triggers** (`NOTIFY_ON`):

| Value | Description |
|-------|-------------|
| `singles` | Notify for each individual track download |
| `playlists` | Notify when playlist downloads complete |
| `bulk` | Notify when bulk imports complete |
| `errors` | Notify when any download fails |

Default is `playlists,bulk,errors` -- notifications for playlist/bulk completions and any failures, but not for every single track.

**Telegram setup:**

1. Create a bot via [@BotFather](https://t.me/BotFather) and copy the token
2. Get your chat ID by messaging [@userinfobot](https://t.me/userinfobot)
3. Build the webhook URL:

```yaml
environment:
  - NOTIFY_ON=playlists,bulk,errors
  - TELEGRAM_WEBHOOK_URL=https://api.telegram.org/bot{token}/sendMessage?chat_id={chat_id}
```

**Email setup (SMTP):**

```yaml
environment:
  - NOTIFY_ON=playlists,bulk,errors
  - SMTP_HOST=smtp.example.com
  - SMTP_PORT=587
  - SMTP_USER=user@example.com
  - SMTP_PASS=password
  - SMTP_FROM=musicgrabber@example.com
  - SMTP_TO=you@example.com
  - SMTP_TLS=true
```

`SMTP_TO` can be a comma-separated list for multiple recipients.

**Generic webhook:**

Set `WEBHOOK_URL` to any URL. MusicGrabber sends a JSON POST with event type, title, artist, status, source, and track counts. Useful for custom integrations (Discord bots, Home Assistant, etc.).

```yaml
environment:
  - WEBHOOK_URL=https://your-endpoint.com/hook
```

### Soulseek Integration (Optional)

MusicGrabber can search [slskd](https://github.com/slskd/slskd) (a Soulseek daemon) for higher quality sources. When configured, search results from both YouTube and Soulseek are displayed, sorted by quality -- FLAC files from Soulseek appear at the top.

**Searching only** (no downloads): If you only want to see what's available on Soulseek without downloading, configure just the API credentials:

```yaml
environment:
  - SLSKD_URL=http://slskd:5030
  - SLSKD_USER=your-slskd-username
  - SLSKD_PASS=your-slskd-password
```

**Full integration** (search + download): To download files from Soulseek, MusicGrabber needs access to slskd's download directory. This requires a shared volume:

```yaml
volumes:
  - /path/to/slskd/downloads:/slskd-downloads  # Mount slskd's downloads folder
environment:
  - SLSKD_URL=http://slskd:5030
  - SLSKD_USER=your-slskd-username
  - SLSKD_PASS=your-slskd-password
  - SLSKD_DOWNLOADS_PATH=/slskd-downloads      # Path inside container
```

**Setup options:**

1. **Same host**: If slskd runs on the same machine, mount its downloads directory directly
2. **Different host**: Use NFS, CIFS/SMB, or similar to make slskd's downloads accessible
3. **Same Docker network**: Ensure both containers can access a shared volume

slskd organises downloads as `{downloads}/{username}/{filename}`, which MusicGrabber will look for automatically.

**Note:** Soulseek is a P2P network. Most users run slskd behind a VPN. This integration only talks to your slskd instance -- it doesn't connect directly to the Soulseek network.

**Status:** Soulseek integration is in progress and needs testing. New Soulseek users may experience rejected downloads until they build reputation by sharing files.

### Spotify Playlist Import

MusicGrabber can import tracks from Spotify playlists and albums. Paste a Spotify URL in the Bulk Import tab to fetch the track list, then import them via YouTube.

**How it works:**

1. **Small playlists (under ~100 tracks)**: Uses Spotify's embed endpoint to quickly fetch track data
2. **Large playlists (100+ tracks)**: Automatically falls back to headless browser scraping

**Headless browser method:**

Spotify's embed API only returns approximately 100 tracks. For larger playlists, MusicGrabber launches a headless Chromium browser (via Playwright) that:

- Loads the full Spotify playlist page
- Automatically dismisses the cookie consent banner
- Scrolls through the entire tracklist to load all tracks (Spotify uses virtualised scrolling that lazy-loads content)
- Extracts track information incrementally during scrolling
- Filters out "Recommended" tracks at the bottom (only numbered playlist tracks are imported)

This process takes a few seconds for playlists with hundreds of tracks. Very large playlists (1000+) may take 10-20 seconds.

**Docker requirements:**

The headless browser requires additional shared memory. The docker-compose.yml includes:

```yaml
shm_size: '2gb'  # Required for Chromium
```

### Watched Playlists

Automatically monitor Spotify or YouTube playlists for new tracks. When new songs are added to a watched playlist, MusicGrabber will detect them and queue them for download.

**How it works:**

1. Add a playlist URL in the "Watched" tab
2. MusicGrabber fetches the current tracklist and stores hashes of each track
3. A built-in scheduler checks playlists periodically (default: daily)
4. New tracks are queued for download via YouTube search

**Configuration:**

The scheduler runs automatically inside the container. Control it with:

```yaml
environment:
  - WATCHED_PLAYLIST_CHECK_HOURS=24  # Check daily (default)
  # Or: 168 for weekly, 720 for monthly, 0 to disable
```

Each playlist also has its own interval (daily, weekly, or monthly) that you set when adding it. The scheduler runs at the global interval and checks which playlists are due based on their individual settings.

**Manual refresh:**

Click "Check All Now" in the UI, or "Refresh" on individual playlists to check immediately regardless of the interval.

**API endpoint:**

For external automation, you can also trigger checks via API:

```bash
curl -X POST http://localhost:38274/api/watched-playlists/check-all
```

### Reverse Proxy (Caddy example)

```
music.yourdomain.com {
    reverse_proxy music-grabber:8080
}
```

## Usage

### Search and Download

1. **Single tracks** -- Search for a song, tap/click the result to download. By default, searches YouTube, SoundCloud, and Monochrome in parallel -- lossless results rank highest
2. **Preview** -- On desktop, hover over a result for 2 seconds to hear a preview (works for all sources)
3. **Playlists** -- Search for a playlist URL or name, tap the playlist result to download all tracks (YouTube playlists only)
4. **Processing feedback** -- Shows "Processing..." immediately when tapped, then "Added to queue ✓"

### Bulk Import

Upload a text file or paste a list of songs in the format:
```
ABBA – Dancing Queen
ABBA – Super Trouper
Backstreet Boys – I Want It That Way
```

The app will:
- Search YouTube for each song automatically
- Queue downloads for best matches
- Show success/failure summary
- All processing happens in-memory (files are not stored on server)

Supports various dash formats: `-`, `–`, `--`

### Queue Management

- **View progress** -- See queued, in-progress, completed, and failed jobs
- **Job details** -- Click completed/failed jobs to see source, timestamps, download duration, and audio quality
- **Re-download** -- Re-queue any completed or failed download (overwrites existing file)
- **Report bad tracks** -- Flag wrong tracks, ContentID dodges, or poor quality from the queue. Blacklisted videos are excluded from future searches
- **Delete from library** -- Remove the audio file and lyrics directly from the queue. If the file is already missing, the job is marked as deleted. Artist folders are removed only when empty
- **Retry failed** -- Click retry on individual failed downloads
- **Clear queue** -- Remove all remembered jobs with the "Clear Queue" button

## File Structure

Downloads are organised as:
```
/music/
└── Singles/
    ├── Artist Name/          # When "Organise by Artist" is on (default)
    │   └── Track Title.flac
    ├── Track Title.flac      # When "Organise by Artist" is off
    └── Playlist Name.m3u
```

- By default, tracks go into `Singles/Artist/` directories
- Disable "Organise by Artist" in Settings to put all tracks directly in `Singles/`
- Playlist downloads generate `.m3u` files with relative paths
- Artist and title are extracted from source metadata (Monochrome provides accurate Tidal metadata; YouTube/SoundCloud are parsed from titles)
- Common patterns like "Artist - Title" are parsed automatically
- YouTube annotations (Official Audio, Lyrics, etc.) are cleaned from titles

### Metadata

**Monochrome/Tidal downloads** come with accurate metadata directly from the Tidal catalogue -- artist, title, album, and cover art are embedded without needing any lookups.

**YouTube/SoundCloud downloads** with `ENABLE_MUSICBRAINZ=true`:
1. Fingerprints the downloaded audio with AcoustID/Chromaprint to identify the actual recording
2. If AcoustID matches confidently, uses the correct artist, title, album, and year from MusicBrainz
3. Falls back to a text-based MusicBrainz search if fingerprinting fails or scores too low
4. Falls back to cleaned source metadata if neither lookup finds anything
5. Sets album to "Singles" by default when no album is found
6. Embeds cover art from source thumbnails

### Duplicate Detection

Before downloading, checks if the track already exists:
- Exact filename match
- Case-insensitive matching
- Skips download and reports as duplicate

## Security

MusicGrabber includes **optional API key authentication** for protecting your instance.

### API Key Authentication

Enable API authentication by setting an API key in the Settings tab or via environment variable:

```yaml
environment:
  - API_KEY=your-secret-key-here
```

When enabled:
- All API requests require the `X-API-Key` header
- The frontend prompts for the key on first visit and stores it in browser localStorage
- Rate limiting applies: 60 requests per minute per IP address

**Setting up:**

1. Go to Settings → Security
2. Enter an API key (any string you choose)
3. Save settings
4. The browser will prompt you for the key

**Environment variable override:** If `API_KEY` is set in the environment, it overrides the database value and cannot be changed via the UI.

### Additional Security Considerations

- For external access, consider a reverse proxy with additional authentication (Caddy, nginx, Authelia)
- The API allows triggering downloads and file operations, so treat access as administrative
- Rate limiting helps prevent abuse but isn't a substitute for proper access control

**Example: Adding basic auth with Caddy (in addition to API key):**

```
music.yourdomain.com {
    basicauth * {
        username $2a$14$hashed_password_here
    }
    reverse_proxy music-grabber:8080
}
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Web UI |
| `GET` | `/api/config` | Get server config (version, defaults, auth_required) |
| `GET` | `/api/settings` | Get all settings (requires auth if API key set) |
| `PUT` | `/api/settings` | Update settings |
| `POST` | `/api/settings/test/{service}` | Test connection (slskd, navidrome, jellyfin) |
| `POST` | `/api/search` | Search sources (`{"query": "...", "limit": 15, "source": "youtube/soundcloud/monochrome/all"}`) |
| `POST` | `/api/search/slskd` | Search Soulseek via slskd (if configured) |
| `GET` | `/api/sources` | List available search sources (for source selector UI) |
| `GET` | `/api/preview/{video_id}` | Get streamable audio URL for preview (`source` + `url` supported for URL-based sources like SoundCloud/Monochrome) |
| `POST` | `/api/download` | Queue download (`{"video_id": "...", "title": "...", "source": "youtube/soundcloud/monochrome", "download_type": "single/playlist"}`) |
| `POST` | `/api/bulk-import-async` | Bulk import songs (async, returns immediately) |
| `GET` | `/api/bulk-import/{id}/status` | Get async bulk import progress |
| `GET` | `/api/bulk-imports` | List recent bulk imports |
| `POST` | `/api/fetch-playlist` | Fetch tracks from supported playlist URL (Spotify or Amazon Music) |
| `POST` | `/api/spotify-playlist` | Backwards-compat alias for Spotify playlist/album fetch |
| `GET` | `/api/stats` | Get statistics (download counts, daily chart, top artists, search analytics) |
| `DELETE` | `/api/stats?confirm=true` | Reset stats history (deletes completed/failed job history and search logs; confirmation required) |
| `GET` | `/api/jobs` | List recent jobs (includes `metadata_source` for provenance) |
| `GET` | `/api/jobs/{id}` | Get job status (includes `metadata_source`) |
| `POST` | `/api/jobs/{id}/retry` | Retry a failed download |
| `DELETE` | `/api/jobs/{id}/file` | Delete downloaded file and lyrics from library |
| `DELETE` | `/api/jobs/cleanup` | Delete jobs (`?status=completed/failed/both`) |
| `POST` | `/api/blacklist` | Report a bad track / block an uploader |
| `GET` | `/api/blacklist` | List all blacklist entries |
| `DELETE` | `/api/blacklist/{id}` | Remove a blacklist entry |
| `GET` | `/api/watched-playlists` | List all watched playlists |
| `POST` | `/api/watched-playlists` | Add a playlist to watch |
| `GET` | `/api/watched-playlists/{id}` | Get watched playlist details |
| `PUT` | `/api/watched-playlists/{id}` | Update watched playlist settings |
| `DELETE` | `/api/watched-playlists/{id}` | Remove a watched playlist |
| `POST` | `/api/watched-playlists/{id}/refresh` | Check playlist for new tracks |
| `POST` | `/api/watched-playlists/check-all` | Check all watched playlists |
| `GET` | `/api/watched-playlists/schedule` | Get next scheduled check time |
| `POST` | `/api/settings/test/youtube-cookies` | Test YouTube cookie validity |
| `GET` | `/api/settings/youtube-cookies/status` | Get cookie upload status |

## Updating yt-dlp

YouTube changes frequently. To update yt-dlp inside the container:

```bash
docker compose exec music-grabber yt-dlp -U
```

Or rebuild the image to get the latest version:

```bash
docker compose build --no-cache
docker compose up -d
```

## Troubleshooting

**Downloads staying inside container / not appearing in mounted volume?**
- Ensure your volume mount matches the `MUSIC_DIR` environment variable
- The default is `MUSIC_DIR=/music`, so mount your music folder to `/music`:
  ```yaml
  volumes:
    - /path/to/your/music:/music  # This MUST match MUSIC_DIR
  environment:
    - MUSIC_DIR=/music
  ```
- Check inside the container: `docker exec music-grabber ls -la /music/Singles/`

**Files created as root / permission denied?**
- By default, the container runs as root (UID 0)
- Set `PUID` and `PGID` to match your host user (like the *arr stack):
  ```yaml
  environment:
    - PUID=1000
    - PGID=1000
  ```
- Find your UID/GID with: `id $USER`

**Downloads failing with 403 errors?**
- YouTube's bot detection may be blocking requests
- Go to Settings → YouTube and upload browser cookies (export from a browser where you're signed into YouTube)
- Use a cookie export extension like "Get cookies.txt LOCALLY" (Chrome/Firefox)
- Cookies expire periodically -- re-export if downloads start failing again

**Downloads failing for other reasons?**
- Check `docker compose logs music-grabber`
- YouTube may have changed something -- try updating yt-dlp
- Some videos are region-locked or age-restricted

**Navidrome not seeing new files?**
- Verify the volume mount paths match
- Check Navidrome's scan interval if auto-rescan isn't configured
- Manually trigger a scan in Navidrome's UI

**Can't access from phone?**
- Ensure port 38274 is open on your firewall
- If using a reverse proxy, check the configuration

**Bulk import not finding songs?**
- Check the format is "Artist - Song" (with a dash separator)
- Try more specific search terms
- Some obscure tracks may not be on YouTube
- Check the results summary for failed searches

**Metadata quality issues?**
- Ensure `ENABLE_MUSICBRAINZ=true` in environment variables
- AcoustID fingerprinting identifies most well-known tracks automatically
- Very short clips (under ~5 seconds) may not fingerprint reliably
- Obscure or newly released tracks may not be in AcoustID or MusicBrainz yet
- Falls back to text-based MusicBrainz search, then to cleaned YouTube metadata

## Contributors

Built with a mix of human creativity and AI assistance.

- **Karl** -- Creator and maintainer
- **Claude (Anthropic)** -- AI pair programmer

## License

Do whatever you want with it. 🤷
