"""GitHub connector, using the official REST API (https://docs.github.com/rest).

Why GitHub matters at pre-seed: for developer-facing companies (most of AI
infrastructure) the public repo is often the first product. Star velocity in the first
90 days is one of the few traction signals that exists before revenue, and issues opened
by people outside the team are evidence of real usage rather than launch-day curiosity.

Discovery searches for young repositories with fast star growth in configurable topics.
Snapshots record:

* ``github.stars``, ``github.forks``, ``github.open_issues``: current counts
* ``github.contributors``: total contributors, including anonymous
* ``github.commits_30d``: commits on the default branch in the last 30 days
* ``github.external_issues_30d``: issues opened in the last 30 days by users who are not
  owners, members or collaborators of the repo
* A reconstructed daily ``github.stars`` history from stargazer timestamps, so a repo we
  discover at day 60 still gets its first-60-day curve. This is only done when the full
  history fits in ``max_star_pages``; a partial history would understate early velocity.

Limitations: stars can be bought or brigaded, and a company's traction may live in a
repo under a founder's personal account rather than the org. Both are reasons the score
in Phase 2 reports confidence and never relies on one metric.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime, time, timedelta
from typing import Any

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

API = "https://api.github.com"

DEFAULT_TOPICS: tuple[str, ...] = (
    "llm", "llmops", "llm-inference", "inference", "vector-database", "rag",
    "ai-agents", "llm-evaluation", "mlops", "fine-tuning", "gpu",
)

# Hosts that are not a company's own domain.
_NON_COMPANY_HOSTS = {"github.com", "github.io", "pypi.org", "npmjs.com", "huggingface.co"}
_TEAM = {"OWNER", "MEMBER", "COLLABORATOR"}
_LAST_PAGE = re.compile(r'[?&]page=(\d+)[^>]*>;\s*rel="last"')


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GitHubConnector(Connector):
    source_kind = SourceKind.GITHUB
    source_name = "GitHub REST API"
    source_url = "https://docs.github.com/rest"
    terms_note = (
        "Official public API, authenticated with a personal token; reads public "
        "repository metadata only, within GitHub's documented rate limits."
    )

    def __init__(
        self,
        client: httpx.Client | None = None,
        token: str | None = None,
        topics: Sequence[str] = DEFAULT_TOPICS,
        min_stars: int = 50,
        max_repo_age_days: int = 90,
        max_star_pages: int = 10,
        requests_per_minute: float = 60,
        http: RateLimitedClient | None = None,
    ):
        settings = get_settings()
        token = token if token is not None else settings.github_token
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": settings.signal_user_agent,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        client = client or httpx.Client(base_url=API, timeout=30)
        client.headers.update(headers)
        self.http = http or RateLimitedClient(client, requests_per_minute=requests_per_minute)
        self.topics = tuple(topics)
        self.min_stars = min_stars
        self.max_repo_age_days = max_repo_age_days
        self.max_star_pages = max_star_pages

    # ----------------------------------------------------------------- discovery

    def discover(self, since: datetime) -> Iterable[CompanyCandidate]:
        """Repos created since ``since`` (and within ``max_repo_age_days``) with at least
        ``min_stars`` stars, in any configured topic. One candidate per owner."""
        floor = max(since, datetime.now(UTC) - timedelta(days=self.max_repo_age_days))
        seen: set[str] = set()
        for topic in self.topics:
            q = f"topic:{topic} created:>={floor.date().isoformat()} stars:>={self.min_stars}"
            data = self.http.get(
                "/search/repositories",
                params={"q": q, "sort": "stars", "order": "desc", "per_page": 50},
            ).json()
            for repo in data.get("items", []):
                owner = repo["owner"]["login"]
                if owner.lower() in seen:
                    continue
                seen.add(owner.lower())
                yield self._candidate(repo)

    def _candidate(self, repo: dict[str, Any]) -> CompanyCandidate:
        owner = repo["owner"]
        is_org = owner.get("type") == "Organization"
        homepage = normalize_domain(repo.get("homepage"))
        if homepage and any(homepage == h or homepage.endswith("." + h)
                            for h in _NON_COMPANY_HOSTS):
            homepage = None
        now = datetime.now(UTC)
        url = repo["html_url"]
        return CompanyCandidate(
            name=owner["login"] if is_org else repo["name"],
            domain=homepage,
            description=repo.get("description"),
            launch_date=_parse_ts(repo["created_at"]).date(),
            github_org=owner["login"] if is_org else None,
            github_repo=repo["full_name"],
            source_url=url,
            observations=[
                SignalObservation("github.stars", repo["stargazers_count"], now, "stars", url),
                SignalObservation("github.forks", repo["forks_count"], now, "forks", url),
            ],
            raw={"repo": repo["full_name"], "topics": repo.get("topics", [])},
        )

    # ----------------------------------------------------------------- snapshots

    def snapshot(self, company: Company) -> Iterable[SignalObservation]:
        repo_name = company.github_repo or self._main_repo(company.github_org)
        if not repo_name:
            return []
        return list(self._snapshot_repo(repo_name))

    def _main_repo(self, org: str | None) -> str | None:
        """The org's most-starred public repo, used when no main repo is recorded."""
        if not org:
            return None
        try:
            repos = self.http.get(f"/orgs/{org}/repos", params={"per_page": 100}).json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        if not repos:
            return None
        return max(repos, key=lambda r: r["stargazers_count"])["full_name"]

    def _snapshot_repo(self, full_name: str) -> Iterator[SignalObservation]:
        now = datetime.now(UTC)
        since = now - timedelta(days=30)
        repo = self.http.get(f"/repos/{full_name}").json()
        url = repo["html_url"]

        yield SignalObservation("github.stars", repo["stargazers_count"], now, "stars", url)
        yield SignalObservation("github.forks", repo["forks_count"], now, "forks", url)
        yield SignalObservation("github.open_issues", repo["open_issues_count"], now,
                                "issues", url)
        yield SignalObservation(
            "github.contributors",
            self._count(f"/repos/{full_name}/contributors", {"anon": "1"}),
            now, "people", f"{url}/graphs/contributors",
        )
        yield SignalObservation(
            "github.commits_30d",
            self._count(f"/repos/{full_name}/commits", {"since": since.isoformat()}),
            now, "commits", f"{url}/commits",
        )
        yield SignalObservation(
            "github.external_issues_30d",
            self._external_issues(full_name, since),
            now, "issues", f"{url}/issues",
        )
        yield from self._star_history(full_name, repo["stargazers_count"], url)

    def _count(self, path: str, params: dict[str, str]) -> int:
        """Count items in a list endpoint with one request, using the ``Link`` header:
        with ``per_page=1`` the last page number equals the item count."""
        response = self.http.get(path, params={**params, "per_page": 1})
        match = _LAST_PAGE.search(response.headers.get("link", ""))
        if match:
            return int(match.group(1))
        return len(response.json()) if response.content else 0

    def _external_issues(self, full_name: str, since: datetime) -> int:
        # The API's ``since`` filters on update time, so re-check creation time here.
        issues = self.http.get(
            f"/repos/{full_name}/issues",
            params={"state": "all", "since": since.isoformat(), "per_page": 100},
        ).json()
        return sum(
            1 for issue in issues
            if "pull_request" not in issue
            and issue.get("author_association") not in _TEAM
            and _parse_ts(issue["created_at"]) >= since
        )

    def _star_history(
        self, full_name: str, total_stars: int, url: str
    ) -> Iterator[SignalObservation]:
        """Cumulative stars at the end of each day that gained stars."""
        per_page = 100
        if total_stars == 0 or total_stars > self.max_star_pages * per_page:
            return
        per_day: Counter = Counter()
        for page in range(1, self.max_star_pages + 1):
            batch = self.http.get(
                f"/repos/{full_name}/stargazers",
                params={"per_page": per_page, "page": page},
                headers={"Accept": "application/vnd.github.star+json"},
            ).json()
            for star in batch:
                per_day[_parse_ts(star["starred_at"]).date()] += 1
            if len(batch) < per_page:
                break
        running = 0
        today = datetime.now(UTC).date()
        for day in sorted(per_day):
            running += per_day[day]
            if day >= today:
                continue  # today's value is covered by the live snapshot
            end_of_day = datetime.combine(day, time(23, 59, 59), tzinfo=UTC)
            yield SignalObservation("github.stars", running, end_of_day, "stars",
                                    f"{url}/stargazers", raw={"reconstructed": True})
