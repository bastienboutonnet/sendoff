"""SMTP email delivery. Adapted from billwatch's remind.py::send_email, but the
recipient is a parameter (sendoff mails individual users, not one fixed inbox).
No-op (returns False) unless SMTP_HOST is configured.
"""
from __future__ import annotations

import logging
from email.utils import formataddr
from typing import Optional

from . import config

log = logging.getLogger("sendoff.mail")


def send_email(to: str, subject: str, body: str, html_body: Optional[str] = None) -> bool:
    """Send one email. When html_body is given the message is
    multipart/alternative: `body` is the plaintext fallback, HTML the preferred
    view. Returns True on success, False on any failure or missing config."""
    if not config.EMAIL_ENABLED or not config.SMTP_HOST or not to:
        return False
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = formataddr((config.SENDER_NAME, config.EMAIL_FROM or config.SMTP_USER))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    try:
        if config.SMTP_SECURITY == "ssl":
            server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=20)
        else:
            server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20)
        with server:
            if config.SMTP_SECURITY == "starttls":
                server.starttls()
            if config.SMTP_USER:
                server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        log.warning("email to %s failed: %s", to, e)
        return False
