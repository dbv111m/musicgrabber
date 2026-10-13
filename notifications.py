"""
MusicGrabber - Notification System

Telegram webhook, SMTP email, generic webhook, and Apprise dispatch.
"""

import smtplib
import socket
import ssl
import threading
from contextlib import contextmanager
from email.mime.text import MIMEText

import httpx

from constants import TIMEOUT_HTTP_REQUEST
from settings import get_setting, get_setting_bool, get_setting_int

# Per-thread: is someone above us still going to try another source?
_failure_hold = threading.local()


@contextmanager
def failure_notices_held():
    """Swallow failure notifications sent from this thread for the duration.

    The download workers announce their own failures, which is right when they
    are the last word and wrong when an acquisition cycle is about to hand the
    track to the next source. Monochrome fell over four times in five minutes on
    2026-10-04, and four "FAILED" pings went out for four tracks that JioSaavn
    and Soulseek delivered seconds later. The cycle holds the workers' notices
    and sends one of its own if every source really does let it down.
    """
    _failure_hold.active = True
    try:
        yield
    finally:
        _failure_hold.active = False


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


def _should_notify(notification_type: str, status: str, error: str = None, user_id: str | None = None) -> bool:
    """Check if notifications should be sent for this type."""
    notify_on = get_setting("notify_on", "playlists,bulk,errors", user_id=user_id)
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


def _send_telegram(message: str, user_id: str | None = None):
    """Send notification via Telegram webhook."""
    telegram_url = get_setting("telegram_webhook_url", user_id=user_id)
    if not telegram_url:
        return

    try:
        with httpx.Client(timeout=TIMEOUT_HTTP_REQUEST) as client:
            client.post(telegram_url, json={"text": message})
    except Exception:
        pass


def _deliver_email(
    subject: str,
    message: str,
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str = "",
    smtp_pass: str = "",
    smtp_from: str = "",
    smtp_to: str,
    smtp_tls: bool = True,
) -> None:
    """Deliver one email, raising a useful exception when SMTP rejects it."""
    host = (smtp_host or "").strip()
    sender = (smtp_from or smtp_user or "").strip()
    recipients = [address.strip() for address in (smtp_to or "").split(",") if address.strip()]
    username = (smtp_user or "").strip()

    if not host:
        raise ValueError("SMTP host is required")
    if not 1 <= smtp_port <= 65535:
        raise ValueError("SMTP port must be between 1 and 65535")
    if not sender:
        raise ValueError("From address is required when no SMTP username is set")
    if not recipients:
        raise ValueError("At least one To address is required")
    if bool(username) != bool(smtp_pass):
        raise ValueError("SMTP username and password must both be set for authentication")

    msg = MIMEText(message)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    with smtplib.SMTP(host, smtp_port, timeout=15) as server:
        server.ehlo()
        if smtp_tls:
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        if username:
            server.login(username, smtp_pass)
        server.sendmail(sender, recipients, msg.as_string())


def _describe_email_error(error: Exception) -> str:
    """Turn SMTP/socket failures into safe, actionable UI copy."""
    if isinstance(error, ValueError):
        return str(error)
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return "SMTP authentication failed; check the username, password, and provider app-password requirements"
    if isinstance(error, smtplib.SMTPNotSupportedError):
        return "The SMTP server does not support STARTTLS; check the port or turn off STARTTLS"
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return "The SMTP server refused the recipient address"
    if isinstance(error, smtplib.SMTPSenderRefused):
        return "The SMTP server refused the From address"
    if isinstance(error, smtplib.SMTPDataError):
        return f"The SMTP server rejected the message (SMTP {error.smtp_code})"
    if isinstance(error, smtplib.SMTPConnectError):
        return f"The SMTP server refused the connection (SMTP {error.smtp_code})"
    if isinstance(error, ssl.SSLError):
        return "STARTTLS failed while verifying the SMTP server certificate"
    if isinstance(error, socket.gaierror):
        return "SMTP host could not be found; check the hostname and DNS"
    if isinstance(error, (socket.timeout, TimeoutError)):
        return "SMTP connection timed out; check the hostname, port, and firewall"
    if isinstance(error, ConnectionRefusedError):
        return "SMTP connection was refused; check the hostname and port"
    if isinstance(error, smtplib.SMTPException):
        return f"SMTP error: {error}"
    return "Email failed; check the SMTP settings and server logs"


def send_test_email(**smtp_settings) -> tuple[bool, str]:
    """Exercise the real SMTP path and return a user-facing result."""
    try:
        _deliver_email(
            "MusicGrabber test email",
            "Test email from MusicGrabber. If you can read this, SMTP notifications are working.",
            **smtp_settings,
        )
        return True, "Test email sent successfully"
    except Exception as error:
        print(f"SMTP test error: {type(error).__name__}: {error}")
        return False, _describe_email_error(error)


def _send_email(subject: str, message: str, user_id: str | None = None):
    """Send notification via SMTP email without interrupting download work."""
    smtp_host = get_setting("smtp_host", user_id=user_id)
    smtp_to = get_setting("smtp_to", user_id=user_id)

    if not smtp_host or not smtp_to:
        return

    try:
        _deliver_email(
            subject,
            message,
            smtp_host=smtp_host,
            smtp_port=get_setting_int("smtp_port", 587, user_id=user_id),
            smtp_user=get_setting("smtp_user", user_id=user_id),
            smtp_pass=get_setting("smtp_pass", user_id=user_id),
            smtp_from=get_setting("smtp_from", user_id=user_id),
            smtp_to=smtp_to,
            smtp_tls=get_setting_bool("smtp_tls", True, user_id=user_id),
        )
    except Exception as error:
        # Notifications must never fail a completed download, but they should no
        # longer vanish without leaving the administrator a clue.
        print(f"SMTP notification error: {type(error).__name__}: {error}")


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
    playlist_name: str = None,
    user_id: str | None = None,
):
    """Send notification via generic webhook POST."""
    webhook_url = get_setting("webhook_url", user_id=user_id)
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


def _send_apprise(title: str, message: str, user_id: str | None = None):
    """Send notification via Apprise (supports Gotify, ntfy, Discord, Pushover, and 50+ more)."""
    url = get_setting("apprise_url", user_id=user_id)
    if not url:
        return

    try:
        import apprise
        a = apprise.Apprise()
        a.add(url)
        a.notify(title=title, body=message)
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
    playlist_name: str = None,
    user_id: str | None = None,
):
    """Send notifications to all configured channels (Telegram, Email, Apprise, webhook).

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
        user_id: User whose notification settings to use (None = global)
    """
    if notification_type == "error" and getattr(_failure_hold, "active", False):
        return
    if not _should_notify(notification_type, status, error, user_id=user_id):
        return

    message, subject = _build_notification_message(
        notification_type, title, artist, source, status,
        error, track_count, failed_count, skipped_count, playlist_name
    )

    _send_telegram(message, user_id=user_id)
    _send_email(subject, message, user_id=user_id)
    _send_apprise(subject, message, user_id=user_id)
    _send_webhook(
        notification_type, title, artist, source, status,
        error, track_count, failed_count, skipped_count, playlist_name,
        user_id=user_id,
    )
