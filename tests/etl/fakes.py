"""Test doubles for the ETL ports.

Replaces what used to be `@patch("yfinance.download")` — a global patch of a
third-party module, which meant the test suite could not run without yfinance
installed and could not see what the source actually asked for without
interrogating a mock.
"""

from __future__ import annotations

from typing import Any


class FakeDownloader:
    """A Downloader whose responses are programmed in order.

    Each element of `responses` is returned, or raised if it is an exception.
    A callable is invoked with the download kwargs — use that when the response
    depends on which tickers were asked for. The last element repeats once the
    list is exhausted, so a test that only cares about the steady state does not
    have to count calls.
    """

    def __init__(self, *responses: Any) -> None:
        if not responses:
            raise ValueError("FakeDownloader needs at least one response")
        self._responses = list(responses)
        self.calls: list[dict] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        i = min(len(self.calls) - 1, len(self._responses) - 1)
        r = self._responses[i]
        if isinstance(r, BaseException):
            raise r
        if callable(r):
            return r(**kwargs)
        return r


class RecordingSleeper:
    """A Sleeper that records instead of sleeping.

    Tests assert on `.slept` — the actual backoff schedule — which is stronger
    than asserting that time.sleep was called, and takes no wall-clock time.
    """

    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(float(seconds))

    @property
    def total(self) -> float:
        return sum(self.slept)


class ManualClock:
    """A MonotonicClock the test drives by hand. Does NOT advance when the
    RecordingSleeper is called — pair them deliberately: a test that wants a
    backoff window to expire says so."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def set(self, t: float) -> None:
        self.t = float(t)

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


class SleepingClock:
    """A ManualClock wired to a Sleeper: sleeping advances time, exactly as it
    does in production. This is the pairing most throttle tests want — it lets a
    backoff window actually elapse without any wall-clock cost."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = float(t)
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(float(seconds))
        self.t += float(seconds)

    @property
    def total(self) -> float:
        return sum(self.slept)
