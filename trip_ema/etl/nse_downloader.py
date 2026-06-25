"""NSE listings downloader.

Handles NSE's anti-bot protection: the archives endpoint requires a
real browser User-Agent and a session that has visited the main site
first (which sets the cookies the archive endpoint validates).

This is brittle by nature. NSE periodically tightens its protection;
if downloads start failing, the symptom shows up here. The CLI's
`refresh-listings` subcommand surfaces failures clearly so you know
to investigate.

Kept separate from the parser so:
  - The parser can be tested against committed fixture CSVs.
  - The downloader can be tested with a mock requests.Session.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


EQUITIES_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
ETFS_URL = "https://nsearchives.nseindia.com/content/equities/eq_etfseclist.csv"
WARMUP_URL = "https://www.nseindia.com/"

# Browser-like UA. NSE rejects requests with the default `python-requests`
# UA. Update this if NSE starts rejecting it too (Chrome's UA string
# evolves). The exact version doesn't matter as long as it's plausible.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class DownloadResult:
    url: str
    content: bytes
    status_code: int


class NseDownloader:
    """Wraps a requests.Session with the cookie-warmup + UA dance.

    Pass an injected session for testing. The default is a fresh one
    per instance, which is what you want at the CLI level (every
    `refresh-listings` invocation is a fresh state).
    """

    def __init__(
        self,
        session=None,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 30.0,
        warmup_url: str = WARMUP_URL,
    ):
        import requests
        self._session = session or requests.Session()
        self._timeout = timeout
        self._warmup_url = warmup_url
        self._session.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/csv,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        })
        self._warmed_up = False

    def _warmup(self) -> None:
        """Hit the main NSE site once to acquire cookies. Idempotent —
        subsequent calls are no-ops within the session lifetime."""
        if self._warmed_up:
            return
        logger.info("NSE: warming up session", extra={"url": self._warmup_url})
        resp = self._session.get(self._warmup_url, timeout=self._timeout)
        # Don't error on warmup status — NSE sometimes returns 200 with
        # a meaningless body. The cookies we want get set regardless.
        # Sleep briefly so we don't look like a bot.
        time.sleep(1.0)
        self._warmed_up = True

    def fetch(self, url: str, max_retries: int = 3) -> DownloadResult:
        """Download a CSV (or any other resource) from NSE. Retries
        with exponential backoff on transient failures."""
        self._warmup()
        last_error: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(
                    "NSE: fetching",
                    extra={"url": url, "attempt": attempt},
                )
                resp = self._session.get(url, timeout=self._timeout)
                # NSE often returns 200 with a CSV body. 4xx/5xx are
                # actual failures.
                if resp.status_code >= 400:
                    raise RuntimeError(
                        f"NSE returned HTTP {resp.status_code} for {url}"
                    )
                content = resp.content
                if not content:
                    raise RuntimeError(f"NSE returned empty body for {url}")
                return DownloadResult(
                    url=url, content=content, status_code=resp.status_code,
                )
            except Exception as e:
                last_error = e
                logger.warning(
                    "NSE: fetch failed; will retry",
                    extra={
                        "url": url, "attempt": attempt,
                        "max_retries": max_retries, "error": str(e),
                    },
                )
                if attempt < max_retries:
                    time.sleep(2.0 ** attempt)
        raise RuntimeError(
            f"NSE fetch failed after {max_retries} attempts: {last_error}"
        )

    def fetch_equities(self) -> bytes:
        return self.fetch(EQUITIES_URL).content

    def fetch_etfs(self) -> bytes:
        return self.fetch(ETFS_URL).content
