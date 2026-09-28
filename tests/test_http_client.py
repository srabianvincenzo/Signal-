import httpx
import pytest

from signal_app.connectors.base import RateLimitedClient


def client_for(responses, no_sleep, **kwargs):
    calls = []

    def handler(request):
        calls.append(request)
        result = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(result, Exception):
            raise result
        return result

    http = RateLimitedClient(
        httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.test"),
        requests_per_minute=6000, sleep=no_sleep, clock=lambda: 0.0, **kwargs,
    )
    return http, calls


def test_success_passes_through(no_sleep):
    http, calls = client_for([httpx.Response(200, json={"ok": True})], no_sleep)
    assert http.get("/x").json() == {"ok": True}
    assert len(calls) == 1


def test_retries_429_using_retry_after(no_sleep):
    http, calls = client_for(
        [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)], no_sleep)
    http.get("/x")
    assert len(calls) == 2
    assert 7 in no_sleep.calls


def test_exponential_backoff_on_5xx(no_sleep):
    http, calls = client_for(
        [httpx.Response(502), httpx.Response(503), httpx.Response(200)], no_sleep,
        backoff_base=1.0)
    http.get("/x")
    backoffs = [s for s in no_sleep.calls if s >= 1]
    assert backoffs == [1.0, 2.0]


def test_gives_up_after_max_retries(no_sleep):
    http, calls = client_for([httpx.Response(500)], no_sleep, max_retries=2)
    with pytest.raises(httpx.HTTPStatusError):
        http.get("/x")
    assert len(calls) == 3


def test_github_rate_limit_403_is_retried(no_sleep):
    limited = httpx.Response(403, headers={"x-ratelimit-remaining": "0"})
    http, calls = client_for([limited, httpx.Response(200)], no_sleep)
    http.get("/x")
    assert len(calls) == 2


def test_plain_403_and_404_are_not_retried(no_sleep):
    for status in (403, 404):
        http, calls = client_for([httpx.Response(status)], no_sleep)
        with pytest.raises(httpx.HTTPStatusError):
            http.get("/x")
        assert len(calls) == 1


def test_network_errors_are_retried(no_sleep):
    http, calls = client_for(
        [httpx.ConnectError("boom"), httpx.Response(200)], no_sleep)
    http.get("/x")
    assert len(calls) == 2


def test_throttle_spaces_requests(no_sleep):
    clock = iter([0.0, 0.0, 0.5, 0.5])
    http = RateLimitedClient(
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        requests_per_minute=60, sleep=no_sleep, clock=lambda: next(clock),
    )
    http.get("https://api.test/a")
    http.get("https://api.test/b")
    assert no_sleep.calls == [pytest.approx(0.5)]
