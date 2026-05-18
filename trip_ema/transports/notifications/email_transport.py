"""Email transport.

Production: send via SMTP. Test/dev: inject a fake sender callable.

The transport's only public surface is `send(address, batch) -> bool`.
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable

from ..domain import NotificationBatch
from .batcher import render_text

logger = logging.getLogger(__name__)


# A "sender" is a callable that takes (EmailMessage) and returns nothing on
# success, raises on failure. The default uses smtplib; tests inject a fake.
EmailSender = Callable[[EmailMessage], None]


@dataclass
class EmailTransport:
    """SMTP-based email transport."""

    from_address: str
    smtp_host: str
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    use_tls: bool = True
    subject: str = "Trip EMA signal updates"
    # Injection point for tests.
    sender: EmailSender | None = None

    def send(self, address: str, batch: NotificationBatch) -> bool:
        body = render_text(batch)
        if not body:
            return True   # nothing to send is trivially successful

        msg = EmailMessage()
        msg["From"] = self.from_address
        msg["To"] = address
        msg["Subject"] = self.subject
        msg.set_content(body)

        try:
            if self.sender is not None:
                self.sender(msg)
            else:
                self._send_smtp(msg)
            return True
        except Exception:
            logger.exception("Email delivery failed for %s", address)
            return False

    def _send_smtp(self, msg: EmailMessage) -> None:
        with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
            if self.use_tls:
                server.starttls()
            if self.smtp_user and self.smtp_password:
                server.login(self.smtp_user, self.smtp_password)
            server.send_message(msg)
