"""Notification transport interface.

A transport takes an address string + a NotificationBatch and tries to
deliver. Returns True on success, False on retryable failure. Raises only
on unrecoverable bugs.

Implementations live in sibling modules (email, webhook, slack, ...).
"""

from __future__ import annotations

from typing import Protocol

from ..domain import NotificationBatch


class NotificationTransport(Protocol):
    """Structural type for any transport implementation."""

    def send(self, address: str, batch: NotificationBatch) -> bool:
        """Deliver a batch to a single address.

        Returns:
            True if delivery succeeded.
            False if delivery failed and is worth retrying later.

        Raises:
            Only on unrecoverable bugs (e.g. malformed batch). Network/
            timeout failures should be caught by the transport and
            returned as False so the SubscriberDistributor can decide
            what to do.
        """
        ...


class NullTransport:
    """A transport that succeeds without doing anything. Useful as the
    default when an exchange has no configured transport, and in tests.
    """

    def send(self, address: str, batch: NotificationBatch) -> bool:
        return True
