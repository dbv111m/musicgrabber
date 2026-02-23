"""
MusicGrabber - Notification System

Telegram webhook and SMTP email dispatch.
"""

import smtplib
from email.mime.text import MIMEText

import httpx

from constants import TIMEOUT_HTTP_REQUEST
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

