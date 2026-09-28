from datetime import UTC, datetime, timedelta

import httpx
import pytest

from signal_app.connectors.hackernews import HackerNewsConnector, parse_title
from signal_app.db.models import Company

NOW = datetime.now(UTC)


@pytest.mark.parametrize("title, name, batch", [
    ("Launch HN: Acme (YC F25) – Inference for everyone", "Acme", "F25"),
    ("Launch HN: Acme Labs (YC W26) - Evals in CI", "Acme Labs", "W26"),
    ("Show HN: Tensorfold – serverless LoRA serving", "Tensorfold", None),
    ("Show HN: Quillgate: regression tests for prompts", "Quillgate", None),
    ("Show HN: Acme", "Acme", None),
])
def test_parse_title(title, name, batch):
    parsed = parse_title(title)
    assert parsed["name"] == name and parsed["batch"] == batch
    assert parsed["accelerator"] == ("Y Combinator" if batch else None)


def test_parse_title_rejects_other_posts():
    assert parse_title("Ask HN: Who is hiring?") is None


def hit(object_id, title, url, points=50, comments=10, days_ago=3):
    return {"objectID": str(object_id), "title": title, "url": url, "points": points,
            "num_comments": comments,
            "created_at_i": int((NOW - timedelta(days=days_ago)).timestamp())}


def make_connector(handler, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HackerNewsConnector(client=client, requests_per_minute=1e6, **kwargs)


def test_discover_launch_and_show_hn():
    launches = [hit(1, "Launch HN: Acme (YC F25) – Inference", "https://www.acme.ai")]
    shows = [
        hit(2, "Show HN: Engine – fast kernels", "https://github.com/enginehq/engine"),
        hit(3, "Show HN: Tiny – a toy", "https://tiny.dev", points=3),  # below threshold
        hit(4, "Show HN: I built a thing to track my cat", "https://cat.dev", points=90),
        hit(5, "Show HN: Acme – duplicate of the launch", "https://acme.ai", points=90),
    ]

    def handler(request):
        tag = request.url.params["tags"]
        hits = launches if tag == "launch_hn" else shows
        return httpx.Response(200, json={"hits": hits, "nbPages": 1})

    candidates = list(make_connector(handler).discover(NOW - timedelta(days=30)))
    assert [c.name for c in candidates] == ["Acme", "Engine"]
    acme, engine = candidates
    assert acme.domain == "acme.ai" and acme.accelerator_batch == "F25"
    assert acme.hn_launch_item_id == 1
    assert acme.launch_date == (NOW - timedelta(days=3)).date()
    assert engine.domain is None and engine.github_repo == "enginehq/engine"
    assert engine.github_org == "enginehq"


def test_discover_pages_until_done():
    pages = []

    def handler(request):
        pages.append(int(request.url.params["page"]))
        return httpx.Response(200, json={"hits": [], "nbPages": 3})

    list(make_connector(handler, max_pages=5).discover(NOW))
    assert pages == [0, 1, 2, 0, 1, 2]


def test_snapshot_launch_item_and_domain_mentions():
    def handler(request):
        if "firebaseio" in request.url.host:
            return httpx.Response(200, json={"id": 1, "score": 212, "descendants": 88})
        return httpx.Response(200, json={"hits": [
            {"url": "https://acme.ai/blog/launch", "points": 212},
            {"url": "https://acme.ai/v2", "points": 40},
            {"url": "https://notacme.ai", "points": 999},  # substring match, excluded
        ]})

    company = Company(name="Acme", domain="acme.ai", hn_launch_item_id=1)
    obs = {o.metric: o.value for o in make_connector(handler).snapshot(company)}
    assert obs == {"hn.launch_points": 212, "hn.launch_comments": 88,
                   "hn.stories_total": 2, "hn.points_total": 252}


def test_snapshot_without_hn_presence_returns_nothing():
    def handler(request):
        pytest.fail("no request expected")

    assert make_connector(handler).snapshot(Company(name="Quiet Co")) == []
