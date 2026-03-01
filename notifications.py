"""
MusicGrabber - Notification System

Telegram webhook and SMTP email dispatch.
"""

import asyncio
import os
import smtplib
import sqlite3
import subprocess
import tempfile
from email.mime.text import MIMEText
from pathlib import Path

import httpx

from constants import DB_PATH, TIMEOUT_HTTP_REQUEST, MUSIC_DIR
from settings import get_setting, get_setting_bool, get_setting_int


def _build_notification_message(
    notification_type: str,
    title: str,
    artist: str = None,
    source: str = None,
    status: str = "completed",
    error: str = None,
    track_count: int = None,
    failed_count: int = None,
    skipped_count: int = None,
    playlist_name: str = None
) -> tuple[str, str]:
    """Build notification message text and subject line.

    Returns:
        Tuple of (message_body, subject_line)
    """
    if status == "failed":
        status_text = "[FAILED]"
    elif status == "completed_with_errors":
        status_text = "[PARTIAL]"
    else:
        status_text = "[OK]"

    lines = [f"MusicGrabber {status_text}"]
    subject = f"MusicGrabber {status_text}"

    if notification_type == "single":
        track_info = f"{artist} - {title}" if artist else title
        lines.append(track_info)
        subject = f"{subject} - {track_info}"
        if source:
            lines.append(f"Source: {source.capitalize()}")
    elif notification_type == "playlist":
        playlist_info = playlist_name or title
        lines.append(f"Playlist: {playlist_info}")
        subject = f"{subject} - Playlist: {playlist_info}"
        if track_count:
            summary_parts = [f"{track_count} tracks"]
            if failed_count:
                summary_parts.append(f"{failed_count} failed")
            if skipped_count:
                summary_parts.append(f"{skipped_count} skipped")
            lines.append(", ".join(summary_parts))
    elif notification_type == "bulk":
        lines.append(f"Bulk import: {title}")
        subject = f"{subject} - Bulk import"
        if track_count:
            summary_parts = [f"{track_count} tracks"]
            if failed_count:
                summary_parts.append(f"{failed_count} failed")
            if skipped_count:
                summary_parts.append(f"{skipped_count} skipped")
            lines.append(", ".join(summary_parts))

    if error:
        lines.append(f"Error: {error}")

    return "\n".join(lines), subject


def _should_notify(notification_type: str, status: str, error: str = None) -> bool:
    """Check if notifications should be sent for this type."""
    notify_on = get_setting("notify_on", "playlists,bulk,errors")
    enabled_types = [t.strip().lower() for t in notify_on.split(",")]

    type_map = {
        "single": "singles",
        "playlist": "playlists",
        "bulk": "bulk",
        "error": "errors"
    }

    config_type = type_map.get(notification_type, notification_type)
    is_error = status == "failed" or error

    return config_type in enabled_types or (is_error and "errors" in enabled_types)


def _send_telegram(message: str):
    """Send notification via Telegram webhook."""
    telegram_url = get_setting("telegram_webhook_url")
    if not telegram_url:
        return

    try:
        with httpx.Client(timeout=TIMEOUT_HTTP_REQUEST) as client:
            client.post(telegram_url, json={"text": message})
    except Exception:
        pass


def _send_email(subject: str, message: str):
    """Send notification via SMTP email."""
    smtp_host = get_setting("smtp_host")
    smtp_to = get_setting("smtp_to")

    if not smtp_host or not smtp_to:
        return

    smtp_port = get_setting_int("smtp_port", 587)
    smtp_user = get_setting("smtp_user")
    smtp_pass = get_setting("smtp_pass")
    smtp_from = get_setting("smtp_from")
    smtp_tls = get_setting_bool("smtp_tls", True)

    try:
        msg = MIMEText(message)
        msg["Subject"] = subject
        msg["From"] = smtp_from or smtp_user
        msg["To"] = smtp_to

        if smtp_tls:
            server = smtplib.SMTP(smtp_host, smtp_port)
            server.starttls()
        else:
            server = smtplib.SMTP(smtp_host, smtp_port)

        if smtp_user and smtp_pass:
            server.login(smtp_user, smtp_pass)

        server.sendmail(msg["From"], smtp_to.split(","), msg.as_string())
        server.quit()
    except Exception:
        pass


def _send_webhook(
    notification_type: str,
    title: str,
    artist: str = None,
    source: str = None,
    status: str = "completed",
    error: str = None,
    track_count: int = None,
    failed_count: int = None,
    skipped_count: int = None,
    playlist_name: str = None
):
    """Send notification via generic webhook POST."""
    webhook_url = get_setting("webhook_url")
    if not webhook_url:
        return

    payload = {
        "event": f"download.{status}",
        "type": notification_type,
        "title": title,
        "status": status,
    }
    if artist:
        payload["artist"] = artist
    if source:
        payload["source"] = source
    if error:
        payload["error"] = error
    if track_count is not None:
        payload["track_count"] = track_count
    if failed_count is not None:
        payload["failed_count"] = failed_count
    if skipped_count is not None:
        payload["skipped_count"] = skipped_count
    if playlist_name:
        payload["playlist_name"] = playlist_name

    try:
        with httpx.Client(timeout=TIMEOUT_HTTP_REQUEST) as client:
            client.post(webhook_url, json=payload)
    except Exception:
        pass


def send_notification(
    notification_type: str,
    title: str,
    artist: str = None,
    source: str = None,
    status: str = "completed",
    error: str = None,
    track_count: int = None,
    failed_count: int = None,
    skipped_count: int = None,
    playlist_name: str = None
):
    """Send notifications to all configured channels (Telegram, Email).

    Args:
        notification_type: One of 'single', 'playlist', 'bulk', 'error'
        title: Track title or import/playlist name
        artist: Artist name (for singles)
        source: Download source (youtube/soulseek)
        status: Job status (completed/failed/completed_with_errors)
        error: Error message if failed
        track_count: Total tracks (for playlists/bulk)
        failed_count: Number of failed tracks
        skipped_count: Number of skipped tracks
        playlist_name: Name of playlist (for playlist downloads)
    """
    if not _should_notify(notification_type, status, error):
        return

    message, subject = _build_notification_message(
        notification_type, title, artist, source, status,
        error, track_count, failed_count, skipped_count, playlist_name
    )

    _send_telegram(message)
    _send_email(subject, message)
    _send_webhook(
        notification_type, title, artist, source, status,
        error, track_count, failed_count, skipped_count, playlist_name
    )


def send_audio_to_telegram(job_id: str):
    """Send downloaded audio file to Telegram user who requested it."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            job = conn.execute(
                "SELECT artist, title, telegram_chat_id, file_path FROM jobs WHERE id = ?",
                (job_id,)
            ).fetchone()

            if not job:
                return

            chat_id = job["telegram_chat_id"]
            if not chat_id:
                return

            file_path = job["file_path"]
            if not file_path:
                return

            # Resolve full path
            if not Path(file_path).is_absolute():
                file_path = MUSIC_DIR / file_path

            audio_file = Path(file_path)
            print(f"Notifications: send_audio_to_telegram called: job_id={job_id}, chat_id={chat_id}, file={file_path}")
            if not audio_file.exists():
                print(f"Notifications: File not found: {audio_file}")
                return

            artist = job["artist"] or "Unknown"
            title = job["title"] or "Unknown"
            size_mb = audio_file.stat().st_size / (1024 * 1024)

            if size_mb > 50:
                print(f"Notifications: File too large for Telegram: {size_mb:.1f}MB")
                return

            # Convert to MP3 if needed (controlled by setting, default True)
            audio_path = str(audio_file)
            is_flac = audio_file.suffix.lower() == '.flac'
            is_opus = audio_file.suffix.lower() == '.opus'
            is_m4a = audio_file.suffix.lower() == '.m4a'
            temp_mp3 = None
            filename = audio_file.name

            # Default to True for Telegram - always convert to MP3
            convert_to_mp3 = get_setting_bool("telegram_convert_to_mp3", True)

            if convert_to_mp3 and (is_flac or is_opus or is_m4a):
                temp_mp3 = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
                temp_mp3.close()
                try:
                    subprocess.run([
                        'ffmpeg', '-i', audio_path,
                        '-b:a', '320k',
                        '-y', temp_mp3.name
                    ], check=True, capture_output=True, timeout=60)
                    audio_path = temp_mp3.name
                    filename = audio_file.stem + '.mp3'
                except subprocess.TimeoutExpired:
                    print(f"Notifications: FFmpeg timeout for {filename}")
                    if temp_mp3 and os.path.exists(temp_mp3.name):
                        os.unlink(temp_mp3.name)
                    return
                except subprocess.CalledProcessError as e:
                    print(f"Notifications: FFmpeg conversion failed: {e}")
                    if temp_mp3 and os.path.exists(temp_mp3.name):
                        os.unlink(temp_mp3.name)
                    return

            # Send audio via Telegram Bot API using curl with longer timeout
            bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
            if not bot_token:
                return

            # Use curl for better compatibility with longer timeout
            caption = f"🎵 {title}\n🎤 {artist}\n\n✅ Скачано MusicGrabber"

            # Use 30 minute timeout for large files (slow upload speeds)
            # Specify filename to ensure Telegram recognizes it as audio
            try:
                # Build curl command with optional proxy
                curl_cmd = [
                    'curl', '-s', '-X', 'POST',
                    f'https://api.telegram.org/bot{bot_token}/sendAudio',
                    '-F', f'chat_id={chat_id}',
                    '-F', f'title={title}',
                    '-F', f'performer={artist}',
                    '-F', f'caption={caption}',
                    '-F', f'audio=@{audio_path};type=audio/mpeg'
                ]

                # Add proxy if configured
                https_proxy = os.getenv('HTTPS_PROXY') or os.getenv('https_proxy')
                if https_proxy:
                    curl_cmd.extend(['--proxy', https_proxy])
                    print(f"Notifications: Using proxy: {https_proxy}")

                result = subprocess.run(curl_cmd, capture_output=True, text=True, timeout=1800)

                if '"ok":true' in result.stdout:
                    print(f"Notifications: Sent audio to {chat_id}: {artist} - {title}")
                else:
                    print(f"Notifications: Failed to send audio: stdout={result.stdout[:500] if result.stdout else 'empty'}, stderr={result.stderr[:200] if result.stderr else 'empty'}, returncode={result.returncode}")
            except subprocess.TimeoutExpired:
                print(f"Notifications: Timeout sending audio to {chat_id}")
            except Exception as e:
                print(f"Notifications: Error sending audio: {e}")

            # Cleanup temp file
            if temp_mp3 and os.path.exists(temp_mp3.name):
                os.unlink(temp_mp3.name)

    except Exception as e:
        print(f"Notifications: Error sending audio: {e}")
