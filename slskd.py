"""
MusicGrabber - Soulseek/slskd Integration

Authentication, search, download, and quality parsing.
"""

import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx

from constants import (
    TIMEOUT_HTTP_REQUEST, TIMEOUT_SLSKD_API, TIMEOUT_SLSKD_SEARCH,
    TIMEOUT_SLSKD_DOWNLOAD, SLSKD_MAX_RESULTS, SLSKD_MIN_QUALITY_SCORE,
    SLSKD_REQUIRE_FREE_SLOT,
)
from settings import get_setting


# slskd auth token cache
_slskd_token = None
_slskd_token_expires = 0


def slskd_enabled() -> bool:
    """Check if slskd integration is configured"""
    url = get_setting("slskd_url")
    user = get_setting("slskd_user")
    password = get_setting("slskd_pass")
    return bool(url and user and password)


def get_slskd_token() -> Optional[str]:
    """Get a valid slskd auth token, refreshing if needed"""
    global _slskd_token, _slskd_token_expires

    if not slskd_enabled():
        return None

    # Return cached token if still valid (with 60s buffer)
    if _slskd_token and time.time() < _slskd_token_expires - 60:
        return _slskd_token

    url = get_setting("slskd_url")
    user = get_setting("slskd_user")
    password = get_setting("slskd_pass")

    try:
        with httpx.Client(timeout=TIMEOUT_HTTP_REQUEST) as client:
            response = client.post(
                f"{url}/api/v0/session",
                json={"username": user, "password": password}
            )
            if response.status_code == 200:
                data = response.json()
                _slskd_token = data["token"]
                _slskd_token_expires = data["expires"]
                return _slskd_token
    except Exception as e:
        print(f"slskd auth failed: {e}")

    return None


def parse_slskd_quality(file_info: dict) -> tuple[str, int]:
    """
    Parse quality info from slskd file response.
    Returns (quality_label, sort_score) where higher score = better quality.
    """
    filename = file_info.get("filename", "").lower()
    bit_depth = file_info.get("bitDepth", 0)
    sample_rate = file_info.get("sampleRate", 0)
    bit_rate = file_info.get("bitRate", 0)

    # Determine format from filename extension
    if filename.endswith(".flac"):
        if bit_depth >= 24:
            return (f"FLAC {bit_depth}bit/{sample_rate//1000}kHz", 150)
        return ("FLAC", 100)
    elif filename.endswith(".wav"):
        return ("WAV", 95)
    elif filename.endswith(".mp3"):
        if bit_rate >= 320:
            return ("MP3 320", 80)
        elif bit_rate >= 256:
            return ("MP3 256", 70)
        elif bit_rate >= 192:
            return ("MP3 192", 60)
        else:
            return (f"MP3 {bit_rate}", 50)
    elif filename.endswith(".m4a") or filename.endswith(".aac"):
        if bit_rate >= 256:
            return ("AAC 256", 75)
        return (f"AAC {bit_rate}", 65)
    elif filename.endswith(".ogg") or filename.endswith(".opus"):
        return ("OGG/Opus", 70)
    else:
        return ("Unknown", 30)


def normalize_slskd_path(path: str) -> str:
    """Normalise slskd paths for matching"""
    return path.replace("\\", "/").strip()


def get_slskd_local_path(download_info: dict) -> Optional[str]:
    """Try to pull a local file path from slskd download info"""
    for key in ("localPath", "localFilename", "downloadedFilePath", "downloadPath", "path", "fullPath"):
        value = download_info.get(key)
        if value:
            return value
    return None


def should_retry_slskd_error(error_message: str) -> bool:
    """Decide whether to retry based on slskd error text"""
    msg = error_message.lower()
    retry_markers = [
        "aborted",
        "rejected",
        "cancelled",
        "failed",
        "timed out",
        "timeout",
        "queued",
    ]
    return any(marker in msg for marker in retry_markers)


def extract_track_info_from_path(filepath: str) -> tuple[str, str]:
    """
    Extract artist and title from a Soulseek file path.
    Tries common patterns like 'Artist/Album/## - Title.ext'
    """
    # Get just the filename
    filename = filepath.split("\\")[-1]
    # Remove extension
    name = re.sub(r'\.[^.]+$', '', filename)
    # Remove track number prefix like "01 - " or "01. "
    name = re.sub(r'^\d+[\s.\-]+', '', name)

    # Try to extract artist from path
    parts = filepath.replace("\\", "/").split("/")
    artist = "Unknown"

    # Look for artist in path (usually 2-3 levels up from file)
    for i, part in enumerate(parts):
        # Skip common folder names
        if part.lower() in ["music", "main", "albums", "singles", "anthologies", "instrumental", "@@*"]:
            continue
        if part.startswith("@@"):
            continue
        if re.match(r'^\[\d{4}\]', part):  # Album folder like [1976] Arrival
            continue
        if re.match(r'^cd\d*$', part.lower()):  # CD1, CD2, etc.
            continue
        # First real folder name is likely the artist
        if i > 0 and not part.startswith("["):
            artist = part
            break

    return artist, name


def search_slskd(query: str, timeout_secs: int = TIMEOUT_SLSKD_SEARCH) -> list[dict]:
    """
    Search slskd and return normalized results.
    Returns list of dicts with: id, title, artist, quality, score, source, slskd_* fields
    """
    token = get_slskd_token()
    if not token:
        return []

    slskd_url = get_setting("slskd_url")
    results = []

    try:
        headers = {"Authorization": f"Bearer {token}"}

        with httpx.Client(timeout=TIMEOUT_SLSKD_API) as client:
            # Start search
            search_response = client.post(
                f"{slskd_url}/api/v0/searches",
                headers=headers,
                json={"searchText": query}
            )

            if search_response.status_code != 200:
                print(f"slskd search failed: {search_response.status_code}")
                return []

            search_data = search_response.json()
            search_id = search_data["id"]
            print(f"slskd: Search started, ID: {search_id}")

            # Poll for results - wait for completion or timeout
            start_time = time.time()
            last_file_count = 0
            final_status = None
            while time.time() - start_time < timeout_secs:
                time.sleep(1)

                status_response = client.get(
                    f"{slskd_url}/api/v0/searches/{search_id}",
                    headers=headers
                )

                if status_response.status_code == 200:
                    final_status = status_response.json()
                    file_count = final_status.get("fileCount", 0)
                    if file_count != last_file_count:
                        print(f"slskd: Polling... {file_count} files, {final_status.get('responseCount', 0)} responses")
                        last_file_count = file_count
                    if final_status.get("isComplete"):
                        print(f"slskd: Search complete. {file_count} files total")
                        break

            if final_status:
                print(f"slskd: Final status - {final_status.get('fileCount', 0)} files, {final_status.get('responseCount', 0)} responses")

            # Small delay to allow responses to be fully indexed
            time.sleep(1)

            # Get responses with a short retry window in case indexing lags
            responses = []
            responses_deadline = time.time() + min(5, timeout_secs)
            while time.time() < responses_deadline:
                responses_response = client.get(
                    f"{slskd_url}/api/v0/searches/{search_id}/responses",
                    headers=headers
                )
                if responses_response.status_code == 200:
                    responses = responses_response.json()
                    if responses:
                        break
                time.sleep(0.5)

            print(f"slskd: Got {len(responses)} user responses")

            # Process results - pick best file from each user
            seen_tracks = set()
            skipped_locked = 0
            skipped_quality = 0
            skipped_no_slot = 0

            for response in responses:
                username = response.get("username", "")
                has_free_slot = response.get("hasFreeUploadSlot", False)
                upload_speed = response.get("uploadSpeed", 0)

                if SLSKD_REQUIRE_FREE_SLOT and not has_free_slot:
                    skipped_no_slot += 1
                    continue

                files = response.get("files") or response.get("fileInfos") or response.get("fileInfo") or []
                for file_info in files:
                    if file_info.get("isLocked", False):
                        skipped_locked += 1
                        continue

                    filepath = file_info.get("filename", "")
                    quality_label, quality_score = parse_slskd_quality(file_info)

                    # Skip low quality
                    if quality_score < SLSKD_MIN_QUALITY_SCORE:
                        skipped_quality += 1
                        continue

                    artist, title = extract_track_info_from_path(filepath)

                    # Dedupe by artist+title+quality
                    track_key = f"{artist.lower()}|{title.lower()}|{quality_label}"
                    if track_key in seen_tracks:
                        continue
                    seen_tracks.add(track_key)

                    # Boost score for free slots and fast uploaders
                    adjusted_score = quality_score
                    if has_free_slot:
                        adjusted_score += 10
                    if upload_speed > 1000000:  # > 1MB/s
                        adjusted_score += 5

                    results.append({
                        "id": f"slskd_{uuid.uuid4().hex[:8]}",
                        "title": title,
                        "artist": artist,
                        "channel": username,  # Show username as "channel"
                        "quality": quality_label,
                        "quality_score": adjusted_score,
                        "source": "soulseek",
                        "duration": str(file_info.get("length", 0)),
                        "size": file_info.get("size", 0),
                        "slskd_username": username,
                        "slskd_filename": filepath,
                    })

            print(
                "slskd: Skipped "
                f"{skipped_locked} locked, {skipped_quality} low quality, "
                f"{skipped_no_slot} no free slot, kept {len(results)}"
            )

            # Clean up search
            try:
                client.delete(f"{slskd_url}/api/v0/searches/{search_id}", headers=headers)
            except Exception:
                pass

    except Exception as e:
        print(f"slskd search error: {e}")

    # Sort by quality score (descending)
    results.sort(key=lambda x: x["quality_score"], reverse=True)

    return results[:SLSKD_MAX_RESULTS]


def download_from_slskd(username: str, filename: str, dest_dir: Path, timeout_secs: int = TIMEOUT_SLSKD_DOWNLOAD) -> Optional[Path]:
    """
    Download a file from Soulseek via slskd.
    Returns the path to the downloaded file, or None on failure.

    Uses slskd_downloads_path setting if set; otherwise falls back to common download locations.
    slskd typically organises downloads as: {downloads_path}/{username}/{filename}
    """
    token = get_slskd_token()
    if not token:
        raise Exception("slskd authentication failed")

    slskd_url = get_setting("slskd_url")
    slskd_downloads_path = get_setting("slskd_downloads_path")

    headers = {"Authorization": f"Bearer {token}"}

    # Extract just the filename from the full path
    target_norm = normalize_slskd_path(filename)
    source_filename = Path(target_norm).name

    slskd_download_dirs = []
    if slskd_downloads_path:
        slskd_download_dirs.append(Path(slskd_downloads_path))
    slskd_download_dirs.extend([
        Path("/slskd/downloads"),
        Path("/app/downloads"),
        Path("/downloads"),
    ])
    seen_dirs = set()
    slskd_download_dirs = [
        d for d in slskd_download_dirs
        if not (str(d) in seen_dirs or seen_dirs.add(str(d)))
    ]

    try:
        with httpx.Client(timeout=TIMEOUT_SLSKD_API) as client:
            # Enqueue the download
            enqueue_response = client.post(
                f"{slskd_url}/api/v0/transfers/downloads/{username}",
                headers=headers,
                json=[{"filename": filename}]
            )

            if enqueue_response.status_code not in [200, 201]:
                raise Exception(f"Failed to enqueue download: {enqueue_response.status_code}")

            print(f"slskd: Enqueued download of '{source_filename}' from {username}")

            # Poll for download completion
            start_time = time.time()
            download_complete = False
            downloaded_path = None
            abort_count = 0
            max_abort_requeues = 3  # Re-queue up to 3 times on abort before giving up
            last_state = ""

            while time.time() - start_time < timeout_secs:
                time.sleep(5)

                # Get download status for this user
                status_response = client.get(
                    f"{slskd_url}/api/v0/transfers/downloads/{username}",
                    headers=headers
                )

                if status_response.status_code != 200:
                    continue

                downloads_data = status_response.json()

                # slskd returns { "directories": [...], "files": [...] } structure
                # Each directory has "files" array with the actual transfer info
                files_to_check = []

                if isinstance(downloads_data, dict):
                    # New API format: { directories: [...] }
                    for directory in downloads_data.get("directories", []):
                        files_to_check.extend(directory.get("files", []))
                elif isinstance(downloads_data, list):
                    # Old API format: direct list of files
                    files_to_check = downloads_data

                # Find our file in the downloads
                file_found = False
                for dl in files_to_check:
                    dl_filename = dl.get("filename", "")
                    dl_norm = normalize_slskd_path(dl_filename)
                    dl_base = Path(dl_norm).name
                    if dl_norm == target_norm or dl_base == source_filename or dl_norm.endswith(f"/{source_filename}"):
                        file_found = True
                        state = dl.get("state", "")
                        progress = dl.get("percentComplete", 0)

                        # Only log state changes to reduce noise
                        if state != last_state:
                            print(f"slskd: Download state: {state} ({progress}%)")
                            last_state = state

                        state_lower = state.lower()

                        # Terminal failure states - these won't recover
                        if any(s in state_lower for s in ("failed", "cancelled", "rejected", "errored")):
                            raise Exception(f"Download failed: {state}")

                        # Aborted is often transient - try re-queuing
                        if "aborted" in state_lower:
                            abort_count += 1
                            if abort_count > max_abort_requeues:
                                raise Exception(f"Download aborted {abort_count} times, giving up")

                            print(f"slskd: Download aborted, re-queuing (attempt {abort_count}/{max_abort_requeues})...")
                            time.sleep(2)  # Brief pause before re-queue

                            # Re-enqueue the download
                            requeue_response = client.post(
                                f"{slskd_url}/api/v0/transfers/downloads/{username}",
                                headers=headers,
                                json=[{"filename": filename}]
                            )
                            if requeue_response.status_code not in [200, 201]:
                                print(f"slskd: Re-queue failed with status {requeue_response.status_code}")
                            else:
                                print(f"slskd: Re-queued successfully")

                            last_state = ""  # Reset to log new state
                            break  # Continue polling

                        # Success states
                        if state_lower.startswith("completed") or state_lower == "succeeded":
                            # Make sure it's actually completed successfully, not "CompletedWithError"
                            if "error" not in state_lower:
                                download_complete = True
                                downloaded_path = get_slskd_local_path(dl) or dl_filename
                            else:
                                raise Exception(f"Download completed with error: {state}")
                            break

                if download_complete:
                    break

                # If file disappeared from the queue entirely, it might have been
                # removed or the user went offline - try re-queuing once
                if not file_found and last_state and "queue" not in last_state.lower():
                    print(f"slskd: File no longer in transfer queue, attempting re-queue...")
                    requeue_response = client.post(
                        f"{slskd_url}/api/v0/transfers/downloads/{username}",
                        headers=headers,
                        json=[{"filename": filename}]
                    )
                    if requeue_response.status_code in [200, 201]:
                        print(f"slskd: Re-queued successfully")
                    last_state = ""

            if not download_complete:
                raise Exception(f"Download timed out after {timeout_secs}s")

            # File should now be in slskd's downloads folder
            # slskd typically organises as: {downloads_path}/{username}/{filename}
            candidate_paths = []
            if downloaded_path:
                normalized_path = normalize_slskd_path(downloaded_path)
                dl_path = Path(normalized_path)
                # Only allow absolute paths if they're within a known download directory
                if dl_path.is_absolute():
                    # Security: verify the path is within allowed download directories
                    is_safe = False
                    for slskd_dir in slskd_download_dirs:
                        try:
                            dl_path.resolve().relative_to(slskd_dir.resolve())
                            is_safe = True
                            break
                        except ValueError:
                            continue
                    if is_safe:
                        candidate_paths.append(dl_path)
                    else:
                        print(f"slskd: Ignoring absolute path outside download dirs: {dl_path}")
                else:
                    for slskd_dir in slskd_download_dirs:
                        candidate_paths.append(slskd_dir / dl_path)
                        candidate_paths.append(slskd_dir / username / dl_path)

            for slskd_dir in slskd_download_dirs:
                candidate_paths.append(slskd_dir / username / source_filename)

            for potential_path in candidate_paths:
                # Security: resolve and verify the path is within allowed directories
                try:
                    resolved = potential_path.resolve()
                    is_safe = False
                    for slskd_dir in slskd_download_dirs:
                        try:
                            resolved.relative_to(slskd_dir.resolve())
                            is_safe = True
                            break
                        except ValueError:
                            continue
                    if not is_safe:
                        print(f"slskd: Skipping path outside download dirs: {resolved}")
                        continue
                except (OSError, ValueError):
                    continue

                if potential_path.exists():
                    dest_path = dest_dir / source_filename
                    shutil.copy2(potential_path, dest_path)
                    print(f"slskd: Copied {potential_path} to {dest_path}")
                    return dest_path

            # If not found, search recursively in the username folder
            for slskd_dir in slskd_download_dirs:
                user_dir = slskd_dir / username
                if user_dir.exists():
                    for found_file in user_dir.rglob(source_filename):
                        if found_file.is_file():
                            dest_path = dest_dir / source_filename
                            shutil.copy2(found_file, dest_path)
                            print(f"slskd: Found and copied {found_file} to {dest_path}")
                            return dest_path

            # List what's actually there for debugging
            for slskd_dir in slskd_download_dirs:
                if slskd_dir.exists():
                    print(f"slskd: Downloads directory contents ({slskd_dir}):")
                    for item in slskd_dir.iterdir():
                        print(f"  - {item.name}/")
                        if item.is_dir():
                            for subitem in list(item.iterdir())[:5]:
                                print(f"      {subitem.name}")
                else:
                    print(f"slskd: Downloads directory not found: {slskd_dir}")

            raise Exception(
                "Downloaded file not found at expected location. "
                "Check that the slskd downloads path is mounted into MusicGrabber."
            )

    except Exception as e:
        print(f"slskd download error: {e}")
        raise
