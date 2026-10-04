"""Unit tests for notification delivery plumbing."""

import os
import smtplib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notifications


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls = []
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def ehlo(self):
        self.calls.append(("ehlo",))

    def starttls(self, *, context):
        self.calls.append(("starttls", context))

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def sendmail(self, sender, recipients, message):
        self.calls.append(("sendmail", sender, recipients, message))


def test_email_delivery_uses_starttls_auth_and_trimmed_recipients(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(notifications.smtplib, "SMTP", FakeSMTP)

    notifications._deliver_email(
        "Subject",
        "Body",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="sender@example.com",
        smtp_pass="secret",
        smtp_to="one@example.com, two@example.com ",
        smtp_tls=True,
    )

    smtp = FakeSMTP.instances[-1]
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.example.com", 587, 15)
    assert [call[0] for call in smtp.calls] == ["ehlo", "starttls", "ehlo", "login", "sendmail"]
    assert smtp.calls[-1][1:3] == (
        "sender@example.com",
        ["one@example.com", "two@example.com"],
    )


def test_email_delivery_can_use_plain_smtp_without_auth(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(notifications.smtplib, "SMTP", FakeSMTP)

    notifications._deliver_email(
        "Subject",
        "Body",
        smtp_host="mail.internal",
        smtp_port=25,
        smtp_from="musicgrabber@example.com",
        smtp_to="listener@example.com",
        smtp_tls=False,
    )

    assert [call[0] for call in FakeSMTP.instances[-1].calls] == ["ehlo", "sendmail"]


def test_email_test_reports_authentication_failure(monkeypatch):
    def reject_login(*_args, **_kwargs):
        raise smtplib.SMTPAuthenticationError(535, b"Bad credentials")

    monkeypatch.setattr(notifications, "_deliver_email", reject_login)
    success, message = notifications.send_test_email(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="sender@example.com",
        smtp_pass="wrong",
        smtp_to="listener@example.com",
        smtp_tls=True,
    )

    assert success is False
    assert "authentication failed" in message.lower()


def test_email_delivery_rejects_incomplete_settings_before_connecting(monkeypatch):
    def should_not_connect(*_args, **_kwargs):
        raise AssertionError("SMTP connection should not be attempted")

    monkeypatch.setattr(notifications.smtplib, "SMTP", should_not_connect)

    success, message = notifications.send_test_email(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="",
        smtp_pass="",
        smtp_from="",
        smtp_to="listener@example.com",
        smtp_tls=True,
    )

    assert success is False
    assert "from address" in message.lower()


def test_failure_notices_are_held_but_successes_still_go(monkeypatch):
    sent = []
    monkeypatch.setattr(
        notifications, "_should_notify",
        lambda notification_type, status, error=None, user_id=None: sent.append(notification_type) or False,
    )
    with notifications.failure_notices_held():
        notifications.send_notification("error", "Track", status="failed", error="nope")
        notifications.send_notification("single", "Track", status="completed")
    notifications.send_notification("error", "Track", status="failed", error="nope")
    assert sent == ["single", "error"]
