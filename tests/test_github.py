from datetime import UTC, datetime, timedelta

import httpx
import pytest

from signal_app.connectors.github import API, GitHubConnector
from signal_app.db.models import Company

NOW = datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def repo(full_name, owner_type="Organization", stars=120, homepage="https://acme.ai",
         created=NOW - timedelta(days=40)):
    owner = full_name.split("/")[0]
    return {
        "full_name": full_name, "name": full_name.split("/")[1],
        "html_url": f"https://github.com/{full_name}",
        "owner": {"login": owner, "type": owner_type},
        "homepage": homepage, "description": "Fast inference",
        "created_at": iso(created), "stargazers_count": stars, "forks_count": 9,
        "open_issues_count": 4, "topics": ["llm"],
    }


def make_connector(handler, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=API)
    return GitHubConnector(client=client, token="", requests_per_minute=1e6, **kwargs)


def test_discover_one_candidate_per_owner_and_skips_github_homepages():
    items = [
        repo("acme/engine"),
        repo("acme/sdk", stars=60),  # same owner, skipped
        repo("solo-dev/tool", owner_type="User", homepage="https://solo-dev.github.io"),
    ]
    seen_queries = []

    def handler(request):
        seen_queries.append(request.url.params["q"])
        return httpx.Response(200, json={"items": items})

    candidates = list(make_connector(handler, topics=["llm"]).discover(NOW - timedelta(days=30)))
    assert [c.name for c in candidates] == ["acme", "tool"]
    acme, tool = candidates
    assert acme.github_org == "acme" and acme.domain == "acme.ai"
    assert acme.github_repo == "acme/engine"
    assert tool.github_org is None and tool.domain is None
    assert {o.metric for o in acme.observations} == {"github.stars", "github.forks"}
    assert "stars:>=50" in seen_queries[0] and "topic:llm" in seen_queries[0]


def test_discover_caps_lookback_at_max_repo_age():
    queries = []

    def handler(request):
        queries.append(request.url.params["q"])
        return httpx.Response(200, json={"items": []})

    list(make_connector(handler, topics=["llm"], max_repo_age_days=90)
         .discover(NOW - timedelta(days=400)))
    floor = (NOW - timedelta(days=90)).date().isoformat()
    assert f"created:>={floor}" in queries[0]


def snapshot_handler(stars=3):
    starred = [NOW - timedelta(days=d) for d in (10, 10, 5)][:stars]

    def handler(request):
        path = request.url.path
        if path == "/repos/acme/engine":
            return httpx.Response(200, json=repo("acme/engine", stars=stars))
        if path.endswith("/contributors"):
            return httpx.Response(200, json=[{}], headers={
                "link": f'<{API}{path}?per_page=1&page=2>; rel="next", '
                        f'<{API}{path}?anon=1&per_page=1&page=7>; rel="last"'})
        if path.endswith("/commits"):
            return httpx.Response(200, json=[{}])  # one page, one commit
        if path.endswith("/issues"):
            return httpx.Response(200, json=[
                {"author_association": "NONE", "created_at": iso(NOW - timedelta(days=2))},
                {"author_association": "CONTRIBUTOR", "created_at": iso(NOW - timedelta(days=3))},
                {"author_association": "MEMBER", "created_at": iso(NOW - timedelta(days=2))},
                {"author_association": "NONE", "created_at": iso(NOW - timedelta(days=2)),
                 "pull_request": {}},
                {"author_association": "NONE", "created_at": iso(NOW - timedelta(days=60))},
            ])
        if path.endswith("/stargazers"):
            assert request.headers["accept"] == "application/vnd.github.star+json"
            return httpx.Response(200, json=[{"starred_at": iso(t)} for t in starred])
        return httpx.Response(404)

    return handler


def test_snapshot_metrics():
    company = Company(name="Acme", github_repo="acme/engine")
    obs = list(make_connector(snapshot_handler()).snapshot(company))
    live = {o.metric: o.value for o in obs if not (o.raw or {}).get("reconstructed")}
    assert live == {
        "github.stars": 3, "github.forks": 9, "github.open_issues": 4,
        "github.contributors": 7, "github.commits_30d": 1,
        "github.external_issues_30d": 2,  # NONE + CONTRIBUTOR, recent, not a PR
    }


def test_star_history_is_cumulative_by_day():
    company = Company(name="Acme", github_repo="acme/engine")
    obs = list(make_connector(snapshot_handler()).snapshot(company))
    history = [(o.observed_at.date(), o.value) for o in obs
               if (o.raw or {}).get("reconstructed")]
    assert history == [((NOW - timedelta(days=10)).date(), 2),
                       ((NOW - timedelta(days=5)).date(), 3)]


def test_star_history_skipped_when_too_large_to_fetch_fully():
    def handler(request):
        if request.url.path.endswith("/stargazers"):
            pytest.fail("should not page through stargazers")
        if request.url.path == "/repos/acme/engine":
            return httpx.Response(200, json=repo("acme/engine", stars=5000))
        return httpx.Response(200, json=[])

    company = Company(name="Acme", github_repo="acme/engine")
    obs = list(make_connector(handler, max_star_pages=10).snapshot(company))
    assert not any((o.raw or {}).get("reconstructed") for o in obs)


def test_snapshot_without_github_presence_returns_nothing():
    def handler(request):
        pytest.fail("no request expected")

    assert list(make_connector(handler).snapshot(Company(name="Offline Co"))) == []


def test_snapshot_falls_back_to_most_starred_org_repo():
    requested = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path == "/orgs/acme/repos":
            return httpx.Response(200, json=[repo("acme/docs", stars=2),
                                             repo("acme/engine", stars=3)])
        return snapshot_handler()(request)

    list(make_connector(handler).snapshot(Company(name="Acme", github_org="acme")))
    assert "/repos/acme/engine" in requested
