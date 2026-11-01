"""Tavily search adapter against the documented request shape, with a mocked HTTP transport: no network, no key needed."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from willay.adapters.tavily_search import (
    DEFAULT_BASE_URL,
    MissingTavilyKeyError,
    TavilyApiError,
    TavilyConfig,
    TavilySearchAdapter,
)
from willay.ports.signals import SOCIAL_DOMAINS, SearchResult, registered_host

KEY = "tvly-test-key-not-real"
NOW = datetime(2026, 2, 10, 15, 0, tzinfo=timezone.utc)
SINCE = datetime(2026, 2, 7, 15, 0, tzinfo=timezone.utc)


def make_adapter(handler, **kwargs):
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(recording))
    adapter = TavilySearchAdapter(TavilyConfig(api_key=KEY), client, sleep=lambda s: None, clock=lambda: NOW, **kwargs)
    return adapter, seen


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content)


def ok(results):
    return lambda request: httpx.Response(200, json={"query": "q", "results": results})


NEWS = {
    "title": "Huaico en Chosica bloquea la Carretera Central",
    "url": "https://diario.example.org/huaico-chosica",
    "content": "Un huaico bajó por la quebrada " + "x" * 900,
    "score": 0.9,
    "published_date": "Tue, 10 Feb 2026 13:30:00 GMT",
}


def test_config_reads_only_the_named_variable_and_requires_it():
    assert TavilyConfig.from_env({"TAVILY_API_KEY": KEY}).api_key == KEY
    assert TavilyConfig.from_env({"TAVILY_API_KEY": "  " + KEY + " "}).api_key == KEY
    with pytest.raises(MissingTavilyKeyError) as raised:
        TavilyConfig.from_env({})
    assert "TAVILY_API_KEY" in str(raised.value)
    with pytest.raises(MissingTavilyKeyError):
        TavilyConfig.from_env({"TAVILY_API_KEY": "   "})


def test_the_key_is_never_shown_in_repr():
    assert KEY not in repr(TavilyConfig(api_key=KEY))


def test_search_makes_a_news_request_and_a_public_pages_request_for_the_last_72_hours():
    adapter, seen = make_adapter(ok([]))
    adapter.search("huaico OR relave Chosica", since=SINCE, max_results=4)
    assert len(seen) == 2
    assert all(str(r.url) == f"{DEFAULT_BASE_URL}/search" and r.method == "POST" for r in seen)
    assert all(r.headers["authorization"] == f"Bearer {KEY}" for r in seen)
    news, general = (body_of(r) for r in seen)
    assert news["topic"] == "news" and news["days"] == 3
    assert general["topic"] == "general" and general["start_date"] == "2026-02-07"
    for body in (news, general):
        assert body["query"] == "huaico OR relave Chosica"
        assert body["max_results"] == 4
        assert body["include_answer"] is False and body["include_raw_content"] is False
        assert body["search_depth"] == "basic"


def test_social_platforms_are_excluded_from_every_request():
    adapter, seen = make_adapter(ok([]))
    adapter.search("huaico", since=SINCE)
    for request in seen:
        excluded = set(body_of(request)["exclude_domains"])
        assert {"facebook.com", "x.com", "twitter.com", "instagram.com", "tiktok.com"} <= excluded
        assert excluded == set(SOCIAL_DOMAINS)


def test_the_key_is_not_in_the_request_body_or_url():
    adapter, seen = make_adapter(ok([]))
    adapter.search("huaico", since=SINCE)
    assert all(KEY not in str(r.url) and KEY.encode() not in r.content for r in seen)


def test_results_keep_only_title_url_published_date_and_a_short_snippet():
    adapter, _ = make_adapter(ok([NEWS | {"raw_content": "texto completo", "author": "Juan Perez", "favicon": "https://x/f.ico", "images": []}]))
    results = adapter.search("huaico", since=SINCE)
    assert len(results) == 1
    result = results[0]
    assert isinstance(result, SearchResult)
    assert result.url == "https://diario.example.org/huaico-chosica"
    assert result.title.startswith("Huaico en Chosica")
    assert result.published == datetime(2026, 2, 10, 13, 30, tzinfo=timezone.utc)
    assert 0 < len(result.snippet) <= 400
    assert set(vars(result)) == {"title", "url", "published", "snippet", "platform"} and result.platform is None  # a web page, not a social post
    assert "Juan" not in repr(result) and "texto completo" not in repr(result)


def test_iso_dates_are_parsed_and_a_missing_date_stays_none():
    adapter, _ = make_adapter(
        ok(
            [
                NEWS | {"url": "https://a.example.org/1", "published_date": "2026-02-10T12:00:00Z"},
                NEWS | {"url": "https://a.example.org/2", "published_date": None},
                NEWS | {"url": "https://a.example.org/3", "published_date": "not a date"},
            ]
        )
    )
    published = {r.url: r.published for r in adapter.search("huaico", since=SINCE)}
    assert published["https://a.example.org/1"] == datetime(2026, 2, 10, 12, 0, tzinfo=timezone.utc)
    assert published["https://a.example.org/2"] is None and published["https://a.example.org/3"] is None


def test_the_same_page_from_both_requests_is_returned_once_and_social_pages_are_dropped_anyway():
    adapter, _ = make_adapter(
        ok(
            [
                NEWS,
                NEWS | {"title": "otra vez"},
                NEWS | {"url": "https://www.facebook.com/groups/vecinos/posts/1"},
                NEWS | {"url": "https://m.youtube.com/watch?v=1"},
                NEWS | {"url": "not a url"},
                NEWS | {"url": ""},
            ]
        )
    )
    urls = [r.url for r in adapter.search("huaico", since=SINCE)]
    assert urls == ["https://diario.example.org/huaico-chosica"]


def test_registered_host_strips_www_ports_and_credentials():
    assert registered_host("https://www.Diario.example.org:8080/a?b=1") == "diario.example.org"
    assert registered_host("https://user:pw@news.example.org/x") == "news.example.org"
    assert registered_host("ftp://x") == ""
    assert registered_host("garbage") == ""


def test_http_errors_are_reported_without_the_key():
    adapter, _ = make_adapter(lambda request: httpx.Response(401, text=f"bad key {KEY}"))
    with pytest.raises(TavilyApiError) as raised:
        adapter.search("huaico", since=SINCE)
    assert raised.value.status_code == 401 and KEY not in str(raised.value)


def test_transient_errors_are_retried_once_then_succeed():
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"detail": "slow down"})
        return httpx.Response(200, json={"results": [NEWS]})

    adapter, _ = make_adapter(flaky)
    assert [r.url for r in adapter.search("huaico", since=SINCE)] == ["https://diario.example.org/huaico-chosica"]


def test_a_non_json_answer_is_an_api_error():
    adapter, _ = make_adapter(lambda request: httpx.Response(200, text="<html>"))
    with pytest.raises(TavilyApiError):
        adapter.search("huaico", since=SINCE)


def test_from_env_builds_an_adapter_without_calling_the_network():
    adapter = TavilySearchAdapter.from_env({"TAVILY_API_KEY": KEY})
    assert isinstance(adapter, TavilySearchAdapter)
