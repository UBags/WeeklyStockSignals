"""Webhook transport — POST a JSON payload to the subscriber's URL.

The HTTP poster is injectable for tests. Production uses requests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from collections.abc import Callable

from ..domain import NotificationBatch
from .batcher import render_json

logger = logging.getLogger(__name__)


# A poster takes (url, json_body) and returns True iff the HTTP status was
# 2xx. Tests inject a fake; production uses _requests_post.
WebhookPoster = Callable[[str, list[dict]], bool]


def _requests_post(url: str, body: list[dict]) -> bool:
    """Default poster. Imported lazily so the package doesn't hard-require
    `requests` for environments that only use other transports."""
    try:
        import requests
    except ImportError:
        logger.error(
            "WebhookTransport requires the 'requests' package; install it"
        )
        return False
    try:
        resp = requests.post(
            url, json=body, timeout=10.0,
            headers={"Content-Type": "application/json"},
        )
        return 200 <= resp.status_code < 300
    except Exception:
        logger.exception("Webhook POST failed for %s", url)
        return False


@dataclass
class WebhookTransport:
    poster: WebhookPoster | None = None

    def send(self, address: str, batch: NotificationBatch) -> bool:
        if not batch.events:
            return True
        body = render_json(batch)
        post = self.poster or _requests_post
        return post(address, body)
