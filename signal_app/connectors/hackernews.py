"""Hacker News connector, using the official HN API (Firebase) for item details and the
public Algolia HN Search API for queries.

Why HN matters at pre-seed: "Launch HN" posts are how Y Combinator companies announce
themselves, usually within weeks of their batch, and "Show HN" is where technical
founders ship first versions. Points and comment counts are a rough, public measure of
how a developer audience reacted, and they are comparable across companies because
every launch faces the same front page.

Discovery reads recent Launch HN posts (all of them) and Show HN posts above a points
threshold. Snapshots record:

* ``hn.launch_points`` / ``hn.launch_comments``: the company's recorded launch post
* ``hn.stories_total`` / ``hn.points_total``: every story linking to the company's
  domain, which captures relaunches and organic mentions over time

Limitations: HN skews toward developer tools and against consumer or enterprise
products, and points depend on posting time. Phase 2 normalizes against sector and
cohort peers for that reason.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from signal_app.config import get_settings
from signal_app.connectors.base import (
    CompanyCandidate,
    Connector,
    RateLimitedClient,
    SignalObservation,
)
from signal_app.db.models import Company, SourceKind
from signal_app.ingest import normalize_domain

ALGOLIA = "https://hn.algolia.com/api/v1"
FIREBASE = "https://hacker-news.firebaseio.com/v0"
ITEM_URL = "https://news.ycombinator.com/item?id={}"
MAX_NAME_WORDS = 4

# "Launch HN: Acme (YC F25) – Inference for everyone"
_TITLE = re.compile(
    r"^(?P<kind>Launch|Show) HN:\s*(?P<name>.+?)"
    r"(?:\s*\((?P<accel>YC)\s*(?P<batch>[A-Z]\d{2,4})\))?"
    r"(?:\s+[–—-]\s+|\s*:\s+|$)",
    re.IGNORECASE,
)
_NON_COMPANY_HOSTS = {"github.com", "news.ycombinator.com", "youtube.com", "twitter.com",
                      "x.com", "medium.com", "substack.com", "loom.com"}


def parse_title(title: str) -> dict[str, str | None] | None:
    """Pull the company name and YC batch out of a Launch/Show HN title."""
    match = _TITLE.match(title.strip())
    if not match:
        return None
    return {
        "kind": match.group("kind").lower(),
        "name": match.group("name").strip(),
        "accelerator": "Y Combinator" if match.group("accel") else None,
        "batch": match.group("batch").upper() if match.group("batch") else None,
    }


class HackerNewsConnector(Connector):
    source_kind = SourceKind.HACKERNEWS
    source_name = "Hacker News API"
    source_url = "https://github.com/HackerNews/API"
    terms_note = (
        "Official HN API (Firebase) and the public Algolia HN Search API, both "
        "published for programmatic access; no authentication or scraping."
    )

    def __init__(
        self,
        client: httpx.Client | None = None,
        show_hn_min_points: int = 20,
        max_pages: int = 5,
        requests_per_minute: float = 120,
        http: RateLimitedClient | None = None,
    ):
        client = client or httpx.Client(timeout=30)
        client.headers["User-Agent"] = get_settings().signal_user_agent
        self.http = http or RateLimitedClient(client, requests_per_minute=requests_per_minute)
        self.show_hn_min_points = show_hn_min_points
        self.max_pages = max_pages

    # ----------------------------------------------------------------- discovery

    def discover(self, since: datetime) -> Iterable[CompanyCandidate]:
        seen: set[str] = set()
        for hit in self._search_since(since, "launch_hn"):
            candidate = self._candidate(hit)
            if candidate and candidate.name.lower() not in seen:
                seen.add(candidate.name.lower())
                yield candidate
        for hit in self._search_since(since, "show_hn"):
            if (hit.get("points") or 0) < self.show_hn_min_points:
                continue
            candidate = self._candidate(hit)
            if candidate and candidate.name.lower() not in seen:
                seen.add(candidate.name.lower())
                yield candidate

    def _search_since(self, since: datetime, tag: str) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {
            "tags": tag,
            "numericFilters": f"created_at_i>={int(since.timestamp())}",
            "hitsPerPage": 100,
        }
        for page in range(self.max_pages):
            data = self.http.get(f"{ALGOLIA}/search_by_date",
                                 params={**params, "page": page}).json()
            yield from data.get("hits", [])
            if page + 1 >= data.get("nbPages", 0):
                break

    def _candidate(self, hit: dict[str, Any]) -> CompanyCandidate | None:
        parsed = parse_title(hit.get("title") or "")
        # "Show HN: I built a tool that..." has no product name to extract.
        if not parsed or len(parsed["name"].split()) > MAX_NAME_WORDS:
            return None
        item_id = int(hit["objectID"])
        created = datetime.fromtimestamp(hit["created_at_i"], UTC)
        link = hit.get("url")
        domain, github_repo = _classify_link(link)
        item_url = ITEM_URL.format(item_id)
        now = datetime.now(UTC)
        return CompanyCandidate(
            name=parsed["name"],
            domain=domain,
            description=hit.get("title"),
            launch_date=created.date(),
            github_org=github_repo.split("/")[0] if github_repo else None,
            github_repo=github_repo,
            hn_launch_item_id=item_id,
            accelerator=parsed["accelerator"],
            accelerator_batch=parsed["batch"],
            source_url=item_url,
            observations=[
                SignalObservation("hn.launch_points", hit.get("points") or 0, now,
                                  "points", item_url),
                SignalObservation("hn.launch_comments", hit.get("num_comments") or 0, now,
                                  "comments", item_url),
            ],
            raw={"kind": parsed["kind"], "title": hit.get("title"), "url": link},
        )

    # ----------------------------------------------------------------- snapshots

    def snapshot(self, company: Company) -> Iterable[SignalObservation]:
        now = datetime.now(UTC)
        observations: list[SignalObservation] = []
        if company.hn_launch_item_id:
            item_url = ITEM_URL.format(company.hn_launch_item_id)
            item = self.http.get(f"{FIREBASE}/item/{company.hn_launch_item_id}.json").json()
            if item and not item.get("deleted") and not item.get("dead"):
                observations += [
                    SignalObservation("hn.launch_points", item.get("score") or 0, now,
                                      "points", item_url),
                    SignalObservation("hn.launch_comments", item.get("descendants") or 0,
                                      now, "comments", item_url),
                ]
        if company.domain:
            data = self.http.get(
                f"{ALGOLIA}/search",
                params={"query": company.domain, "restrictSearchableAttributes": "url",
                        "tags": "story", "hitsPerPage": 100},
            ).json()
            hits = [h for h in data.get("hits", [])
                    if normalize_domain(h.get("url")) == company.domain]
            search_url = f"https://hn.algolia.com/?query={company.domain}&type=story"
            observations += [
                SignalObservation("hn.stories_total", len(hits), now, "stories", search_url),
                SignalObservation("hn.points_total", sum(h.get("points") or 0 for h in hits),
                                  now, "points", search_url),
            ]
        return observations


def _classify_link(link: str | None) -> tuple[str | None, str | None]:
    """Split a post's URL into (company domain, GitHub "owner/repo")."""
    domain = normalize_domain(link)
    if not domain:
        return None, None
    if domain == "github.com":
        parts = [p for p in urlparse(link).path.split("/") if p]
        return None, "/".join(parts[:2]) if len(parts) >= 2 else None
    if any(domain == h or domain.endswith("." + h) for h in _NON_COMPANY_HOSTS):
        return None, None
    return domain, None
