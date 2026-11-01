"""TikHub public-search adapter with a mocked HTTP transport: no network, no key needed.

The request shapes follow TikHub's OpenAPI (https://api.tikhub.io/openapi.json). The RESPONSE bodies below are
representative samples (TikHub's OpenAPI documents only a generic envelope), so the parser is deliberately tolerant.
"""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from willay.adapters.tikhub_search import (
    DEFAULT_BASE_URL,
    SNIPPET_LIMIT,
    MissingTikHubKeyError,
    TikHubApiError,
    TikHubConfig,
    TikHubPublicSearchAdapter,
    sanitize_snippet,
)
from willay.ports.signals import HAZARD_TERMS, SearchResult, parse_query

KEY = "tikhub-test-key-not-real"
NOW = datetime(2026, 2, 10, 15, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(hours=72)
TIKTOK_PATH = "/api/v1/tiktok/app/v3/fetch_video_search_result"
TWITTER_PATH = "/api/v1/twitter/web/fetch_search_timeline"
QUERY = "huaico OR deslizamiento OR desborde OR relave OR inundación Chosica OR Ñaña OR Santa Rosa Perú"


def ts(hours_ago: float) -> int:
    return int((NOW - timedelta(hours=hours_ago)).timestamp())


def make_adapter(handler, **config):
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(recording))
    cfg = TikHubConfig(api_key=KEY, **config)
    return TikHubPublicSearchAdapter(cfg, client, sleep=lambda s: None, clock=lambda: NOW), seen


def tiktok_item(aweme_id="7300000000000000001", desc="Huaico en Chosica, bajó la quebrada #huaico", hours_ago=5, **extra_author):
    return {
        "aweme_info": {
            "aweme_id": aweme_id,
            "desc": desc,
            "create_time": ts(hours_ago),
            "share_url": f"https://www.tiktok.com/@juan.perez/video/{aweme_id}?utm_source=x",
            "author": {"unique_id": "juan.perez", "nickname": "Juan Pérez", "uid": "999", "avatar_thumb": {"url_list": ["https://p16.example/a.jpg"]}, "follower_count": 12345, **extra_author},
            "statistics": {"digg_count": 3},
            "video": {"cover": {"url_list": ["https://p16.example/c.jpg"]}},
        }
    }


def tiktok_body(*items):
    return {"code": 200, "message": "ok", "data": {"search_item_list": list(items), "has_more": 1, "cursor": 20}}


def tweet(rest_id="1890000000000000001", text="Se desbordó el río en Ñaña, muy peligroso", hours_ago=4):
    created = (NOW - timedelta(hours=hours_ago)).strftime("%a %b %d %H:%M:%S +0000 %Y")
    return {
        "entryId": f"tweet-{rest_id}",
        "content": {
            "itemContent": {
                "tweet_results": {
                    "result": {
                        "rest_id": rest_id,
                        "core": {"user_results": {"result": {"legacy": {"screen_name": "maria_lima", "name": "María", "followers_count": 77, "profile_image_url_https": "https://pbs.example/p.jpg"}}}},
                        "legacy": {"full_text": text, "created_at": created, "id_str": rest_id, "user_id_str": "55"},
                    }
                }
            }
        },
    }


def twitter_body(*tweets):
    return {"code": 200, "data": {"search_by_raw_query": {"search_timeline": {"timeline": {"instructions": [{"type": "TimelineAddEntries", "entries": list(tweets)}]}}}}}


def route(tiktok=None, twitter=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TIKTOK_PATH:
            return httpx.Response(200, json=tiktok if tiktok is not None else tiktok_body())
        if request.url.path == TWITTER_PATH:
            return httpx.Response(200, json=twitter if twitter is not None else twitter_body())
        return httpx.Response(404, json={"detail": "unexpected path"})

    return handler


# --- configuration ------------------------------------------------------------------------------------------------


def test_config_reads_only_the_named_variable_and_requires_it():
    assert TikHubConfig.from_env({"TIKHUB_API_KEY": KEY}).api_key == KEY
    assert TikHubConfig.from_env({"TIKHUB_API_KEY": f"  {KEY} "}).api_key == KEY
    with pytest.raises(MissingTikHubKeyError) as raised:
        TikHubConfig.from_env({})
    assert "TIKHUB_API_KEY" in str(raised.value) and KEY not in str(raised.value)
    with pytest.raises(MissingTikHubKeyError):
        TikHubConfig.from_env({"TIKHUB_API_KEY": "  "})


def test_config_defaults_and_overrides_and_the_key_never_shows_in_repr():
    config = TikHubConfig.from_env({"TIKHUB_API_KEY": KEY})
    assert config.base_url == DEFAULT_BASE_URL == "https://api.tikhub.io"
    assert config.platforms == ("tiktok", "twitter") and config.max_requests == 5
    custom = TikHubConfig.from_env(
        {"TIKHUB_API_KEY": KEY, "TIKHUB_API_BASE_URL": "https://example.test/", "WILLAY_TIKHUB_PLATFORMS": "twitter, bogus ,TikTok", "WILLAY_TIKHUB_MAX_REQUESTS": "2"}
    )
    assert custom.base_url == "https://example.test" and custom.platforms == ("twitter", "tiktok") and custom.max_requests == 2
    assert KEY not in repr(custom)
    bad = TikHubConfig.from_env({"TIKHUB_API_KEY": KEY, "WILLAY_TIKHUB_PLATFORMS": "bogus", "WILLAY_TIKHUB_MAX_REQUESTS": "many"})
    assert bad.platforms == ("tiktok", "twitter") and bad.max_requests == 5
    assert TikHubConfig.from_env({"TIKHUB_API_KEY": KEY, "WILLAY_TIKHUB_MAX_REQUESTS": "0"}).max_requests == 5


# --- query parsing (shared helper in the port) --------------------------------------------------------------------


def test_parse_query_splits_the_hazard_terms_from_the_places():
    hazards, places = parse_query(QUERY)
    assert hazards == list(HAZARD_TERMS)
    assert places == ["Chosica", "Ñaña", "Santa Rosa"]


def test_parse_query_of_free_text_has_no_structure():
    assert parse_query("algo distinto") == ([], [])
    assert parse_query("") == ([], [])


# --- request shape --------------------------------------------------------------------------------------------------


def test_tiktok_request_follows_the_openapi_shape_and_sends_the_key_only_in_the_header():
    adapter, seen = make_adapter(route(), platforms=("tiktok",), max_requests=1)
    adapter.search(QUERY, since=SINCE, max_results=7)
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "GET" and request.url.path == TIKTOK_PATH
    assert request.headers["authorization"] == f"Bearer {KEY}"
    params = dict(request.url.params)
    assert params["keyword"] == "huaico Chosica" and params["count"] == "7" and params["offset"] == "0"
    assert params["publish_time"] == "7" and params["region"] == "PE"
    assert KEY not in str(request.url)


def test_twitter_request_uses_latest_and_one_boolean_query():
    adapter, seen = make_adapter(route(), platforms=("twitter",))
    adapter.search(QUERY, since=SINCE)
    assert len(seen) == 1 and seen[0].url.path == TWITTER_PATH
    params = dict(seen[0].url.params)
    assert params["search_type"] == "Latest"
    assert params["keyword"] == '(huaico OR deslizamiento OR desborde OR relave OR inundación) (Chosica OR Ñaña OR "Santa Rosa")'
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"


def test_tiktok_requests_are_capped_and_cover_every_hazard_and_place_in_turn():
    adapter, seen = make_adapter(route(), platforms=("tiktok",), max_requests=5)
    adapter.search(QUERY, since=SINCE)
    keywords = [dict(r.url.params)["keyword"] for r in seen]
    assert keywords == ["huaico Chosica", "deslizamiento Ñaña", "desborde Santa Rosa", "relave Chosica", "inundación Ñaña"]
    capped, seen2 = make_adapter(route(), platforms=("tiktok",), max_requests=2)
    capped.search(QUERY, since=SINCE)
    assert len(seen2) == 2


def test_only_the_two_search_endpoints_are_ever_called_and_no_profile_or_media_endpoint():
    adapter, seen = make_adapter(route(tiktok_body(tiktok_item()), twitter_body(tweet())))
    adapter.search(QUERY, since=SINCE)
    assert {r.url.path for r in seen} <= {TIKTOK_PATH, TWITTER_PATH}


def test_a_query_without_structure_is_searched_as_free_text():
    adapter, seen = make_adapter(route(), platforms=("tiktok", "twitter"))
    adapter.search("huaico en Chosica", since=SINCE)
    assert [dict(r.url.params)["keyword"] for r in seen] == ["huaico en Chosica", "huaico en Chosica"]


def test_a_disabled_platform_is_never_requested():
    adapter, seen = make_adapter(route(), platforms=("twitter",))
    adapter.search(QUERY, since=SINCE)
    assert {r.url.path for r in seen} == {TWITTER_PATH}


# --- data minimization ----------------------------------------------------------------------------------------------


def test_tiktok_post_keeps_only_url_platform_time_and_a_short_snippet():
    adapter, _ = make_adapter(route(tiktok_body(tiktok_item())), platforms=("tiktok",), max_requests=1)
    [item] = adapter.search(QUERY, since=SINCE)
    assert isinstance(item, SearchResult)
    assert item.platform == "tiktok" and item.title == ""
    assert item.url == "https://www.tiktok.com/@/video/7300000000000000001"
    assert item.published == NOW - timedelta(hours=5)
    assert "Huaico en Chosica" in item.snippet and len(item.snippet) <= SNIPPET_LIMIT
    flat = repr(item)
    for personal in ("juan", "Juan", "Pérez", "999", "12345", "avatar", "p16.example", "follower", "unique_id"):
        assert personal not in flat


def test_twitter_post_keeps_only_url_platform_time_and_a_short_snippet():
    adapter, _ = make_adapter(route(twitter=twitter_body(tweet())), platforms=("twitter",))
    [item] = adapter.search(QUERY, since=SINCE)
    assert item.platform == "twitter" and item.title == ""
    assert item.url == "https://x.com/i/status/1890000000000000001"
    assert item.published == NOW - timedelta(hours=4)
    assert "desbordó" in item.snippet
    flat = repr(item)
    for personal in ("maria", "María", "screen_name", "followers", "pbs.example", "55"):
        assert personal not in flat


def test_a_flat_tweet_shape_and_iso_times_are_understood_too():
    flat = {"tweet_id": "42", "text": "Huaico en Chosica", "created_at": (NOW - timedelta(hours=1)).isoformat()}
    adapter, _ = make_adapter(route(twitter={"code": 200, "data": {"tweets": [flat]}}), platforms=("twitter",))
    [item] = adapter.search(QUERY, since=SINCE)
    assert item.url == "https://x.com/i/status/42" and item.published == NOW - timedelta(hours=1)


def test_sanitize_snippet_removes_handles_links_emails_and_phone_numbers_but_keeps_hashtags():
    raw = "RT @maria_lima: huaico en #Chosica!! llamar 987 654 321 o a juan@correo.pe  mira https://t.co/abc y www.fb.com/x   +51 987654321"
    cleaned = sanitize_snippet(raw)
    for gone in ("@maria_lima", "maria_lima", "987", "654", "juan@correo.pe", "https://", "t.co", "www.fb.com", "+51"):
        assert gone not in cleaned
    assert "#Chosica" in cleaned and "huaico" in cleaned
    assert "  " not in cleaned


def test_snippet_is_capped_at_400_characters():
    long_text = "huaico " * 200
    assert len(sanitize_snippet(long_text)) <= SNIPPET_LIMIT == 400


def test_a_post_without_text_or_id_is_dropped():
    body = tiktok_body({"aweme_info": {"aweme_id": "1", "desc": "", "create_time": ts(1)}}, {"aweme_info": {"desc": "sin id", "create_time": ts(1)}})
    adapter, _ = make_adapter(route(body), platforms=("tiktok",), max_requests=1)
    assert adapter.search(QUERY, since=SINCE) == []


def test_posts_older_than_the_window_are_dropped_and_an_unknown_time_is_kept():
    items = [tiktok_item("1", hours_ago=100), tiktok_item("2", hours_ago=2)]
    undated = tiktok_item("3")
    del undated["aweme_info"]["create_time"]
    adapter, _ = make_adapter(route(tiktok_body(*items, undated)), platforms=("tiktok",), max_requests=1)
    results = adapter.search(QUERY, since=SINCE)
    assert [r.url.rsplit("/", 1)[1] for r in results] == ["2", "3"]
    assert results[1].published is None


def test_the_same_post_found_twice_is_returned_once():
    adapter, _ = make_adapter(route(tiktok_body(tiktok_item("9"), tiktok_item("9"))), platforms=("tiktok",), max_requests=3)
    assert len(adapter.search(QUERY, since=SINCE)) == 1


def test_unexpected_response_bodies_give_no_results_instead_of_crashing():
    for body in ({"code": 200, "data": None}, {"code": 200, "data": "x"}, {"code": 200}, {"code": 200, "data": {"search_item_list": ["a", 1, None]}}):
        adapter, _ = make_adapter(route(tiktok=body, twitter=body))
        assert adapter.search(QUERY, since=SINCE) == []


# --- errors -----------------------------------------------------------------------------------------------------------


def test_an_authentication_error_stops_at_once_and_never_echoes_the_key():
    adapter, seen = make_adapter(lambda request: httpx.Response(401, text=f"bad token {KEY}"))
    with pytest.raises(TikHubApiError) as raised:
        adapter.search(QUERY, since=SINCE)
    assert raised.value.status_code == 401 and KEY not in str(raised.value) and "[redacted]" in str(raised.value)
    assert len(seen) == 1


def test_a_429_is_retried_once_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json=tiktok_body(tiktok_item()))

    adapter, _ = make_adapter(handler, platforms=("tiktok",), max_requests=1)
    assert len(adapter.search(QUERY, since=SINCE)) == 1 and calls["n"] == 2


def test_one_failing_request_does_not_lose_the_others():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(400, text="bad keyword")
        return httpx.Response(200, json=tiktok_body(tiktok_item(f"7{calls['n']}")))

    adapter, _ = make_adapter(handler, platforms=("tiktok",), max_requests=3)
    assert len(adapter.search(QUERY, since=SINCE)) == 2


def test_when_every_request_fails_the_error_is_raised():
    adapter, _ = make_adapter(lambda request: httpx.Response(500, text="down"), platforms=("tiktok",), max_requests=2)
    with pytest.raises(TikHubApiError):
        adapter.search(QUERY, since=SINCE)


def test_an_envelope_error_code_and_non_json_are_errors():
    adapter, _ = make_adapter(lambda r: httpx.Response(200, json={"code": 402, "message": "insufficient balance"}), platforms=("tiktok",), max_requests=1)
    with pytest.raises(TikHubApiError) as raised:
        adapter.search(QUERY, since=SINCE)
    assert raised.value.status_code == 402
    html, _ = make_adapter(lambda r: httpx.Response(200, text="<html>"), platforms=("tiktok",), max_requests=1)
    with pytest.raises(TikHubApiError):
        html.search(QUERY, since=SINCE)


def test_a_network_error_is_reported_by_type_only():
    def handler(request):
        raise httpx.ConnectError(f"cannot reach {KEY}")

    adapter, _ = make_adapter(handler, platforms=("twitter",))
    with pytest.raises(TikHubApiError) as raised:
        adapter.search(QUERY, since=SINCE)
    assert "ConnectError" in str(raised.value) and KEY not in str(raised.value)


def test_requests_are_valid_json_free_get_calls_without_a_body():
    adapter, seen = make_adapter(route(), max_requests=1)
    adapter.search(QUERY, since=SINCE)
    assert all(r.content == b"" for r in seen)
    json.dumps([str(r.url) for r in seen])
