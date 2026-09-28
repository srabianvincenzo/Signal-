"""The common interface every data source implements, plus a polite HTTP client.

A connector has two jobs:

* ``discover(since)`` finds companies we may not know about yet and returns them as
  ``CompanyCandidate`` objects.
* ``snapshot(company)`` measures a company we already track and returns
  ``SignalObservation`` objects.

Connectors return plain dataclasses and never touch the database. The ingest layer
(``signal_app.ingest``) owns deduplication and writes, so every source is stored the
same way and a connector can be tested with nothing but recorded HTTP responses.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

import httpx

from signal_app.db.models import SourceKind

if TYPE_CHECKING:
    from signal_app.db.models import Company

log = logging.getLogger(__name__)


@dataclass
class SignalObservation:
    """One metric value at one moment, e.g. ``github.stars = 412`` on 2026-09-01."""

    metric: str
    value: float
    observed_at: datetime
    unit: str | None = None
    source_url: str | None = None
    raw: dict[str, Any] | None = None


@dataclass
class CompanyCandidate:
    """A company a connector found. Fields it cannot know are left as ``None``."""

    name: str
    domain: str | None = None
    description: str | None = None
    launch_date: date | None = None
    github_org: str | None = None
    github_repo: str | None = None
    hn_launch_item_id: int | None = None
    accelerator: str | None = None
    accelerator_batch: str | None = None
    source_url: str | None = None
    observations: list[SignalObservation] = field(default_factory=list)
    raw: dict[str, Any] | None = None


class Connector(ABC):
    """Base class for every data source.

    Subclasses set ``source_kind`` and ``source_name`` (used to find or create the
    ``Source`` row) and a ``terms_note`` explaining why the use is permitted.
    """

    source_kind: SourceKind
    source_name: str
    source_url: str | None = None
    terms_note: str | None = None

    @abstractmethod
    def discover(self, since: datetime) -> Iterable[CompanyCandidate]:
        """Yield companies first seen by this source at or after ``since``."""

    @abstractmethod
    def snapshot(self, company: Company) -> Iterable[SignalObservation]:
        """Yield current (and, where the API allows, historical) metrics for a company.

        Yield nothing when the company has no presence on this source. That is a
        normal outcome at pre-seed, not an error.
        """

    def healthcheck(self) -> bool:
        return True


# ---------------------------------------------------------------------------- HTTP


class RateLimitedClient:
    """An ``httpx.Client`` wrapper that spaces out requests and retries politely.

    * Keeps at least ``60 / requests_per_minute`` seconds between requests.
    * Retries 429, 5xx and network errors with exponential backoff
      (``backoff_base * 2**attempt`` seconds, capped at ``max_backoff``).
    * Honours ``Retry-After`` and GitHub's ``X-RateLimit-Reset`` when the server says
      how long to wait, instead of guessing.

    ``sleep`` and ``clock`` are injectable so tests run instantly.
    """

    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        client: httpx.Client,
        requests_per_minute: float = 60,
        max_retries: int = 4,
        backoff_base: float = 2.0,
        max_backoff: float = 900.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.client = client
        self.min_interval = 60.0 / requests_per_minute
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.max_backoff = max_backoff
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        attempt = 0
        while True:
            self._throttle()
            try:
                response = self.client.get(url, **kwargs)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise
                wait = self._backoff(attempt)
                log.warning("network error on %s (%s); retrying in %.1fs", url, exc, wait)
            else:
                if not self._should_retry(response):
                    response.raise_for_status()
                    return response
                if attempt >= self.max_retries:
                    response.raise_for_status()
                    return response
                wait = self._server_wait(response) or self._backoff(attempt)
                log.warning("HTTP %s on %s; retrying in %.1fs", response.status_code, url, wait)
            self._sleep(min(wait, self.max_backoff))
            attempt += 1

    def _throttle(self) -> None:
        now = self._clock()
        if self._last_request is not None:
            gap = now - self._last_request
            if gap < self.min_interval:
                self._sleep(self.min_interval - gap)
        self._last_request = self._clock()

    def _backoff(self, attempt: int) -> float:
        return min(self.backoff_base * 2**attempt, self.max_backoff)

    def _should_retry(self, response: httpx.Response) -> bool:
        if response.status_code in self.RETRY_STATUSES:
            return True
        # GitHub signals an exhausted rate limit with 403 and remaining = 0.
        return (
            response.status_code == 403
            and response.headers.get("x-ratelimit-remaining") == "0"
        )

    @staticmethod
    def _server_wait(response: httpx.Response) -> float | None:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return max(float(retry_after), 0.0)
            except ValueError:
                try:
                    when = parsedate_to_datetime(retry_after)
                    return max((when - datetime.now(UTC)).total_seconds(), 0.0)
                except (TypeError, ValueError):
                    pass
        reset = response.headers.get("x-ratelimit-reset")
        if reset and reset.isdigit():
            return max(int(reset) - time.time(), 0.0) + 1.0
        return None
