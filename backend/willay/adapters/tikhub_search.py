"""TikHub public-post search adapter (https://docs.tikhub.io, OpenAPI at https://api.tikhub.io/openapi.json).

WARNING: this reads public posts of social platforms (TikTok, X/Twitter) through a third-party scraping API. The platforms' terms
may prohibit that, and Peru's Ley 29733 applies. It is OFF by default (WILLAY_TIKHUB_ENABLED=0); the operator must verify the
legality before enabling it. See docs/SIGNALS.md.

Requests (GET, `Authorization: Bearer <TIKHUB_API_KEY>`), verified against the OpenAPI document's paths and parameter names:

  TikTok  /api/v1/tiktok/app/v3/fetch_video_search_result  keyword, offset, count, sort_type, publish_time (7 = last week), region
  X       /api/v1/twitter/web/fetch_search_timeline        keyword, search_type (Latest), cursor

The OpenAPI document describes the response only as a generic envelope (`code`, `message`, `data`), so the parsers below look for
post objects tolerantly. The response shapes have NOT been exercised against the live service. Never called live in this repository.

Data minimization (identical to the Tavily adapter): from each post only the canonical URL, the platform, the publish time and a
snippet of at most 400 characters are kept, and the snippet is used solely for extraction and then discarded. Usernames, handles,
display names, profile URLs, avatars, follower counts and every other field are never read into a result. The URL is rebuilt from
the post id so it carries no handle. Images and videos are never requested, downloaded or processed.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterator, Mapping

import httpx

from willay.ports.signals import HAZARD_TERMS, SearchResult, parse_query

DEFAULT_BASE_URL = "https://api.tikhub.io"
ENV_KEY = "TIKHUB_API_KEY"
ENV_BASE_URL = "TIKHUB_API_BASE_URL"
ENV_PLATFORMS = "WILLAY_TIKHUB_PLATFORMS"
ENV_MAX_REQUESTS = "WILLAY_TIKHUB_MAX_REQUESTS"
PLATFORMS = ("tiktok", "twitter")
DEFAULT_MAX_REQUESTS = 5
SNIPPET_LIMIT = 400
TIKTOK_PATH = "/api/v1/tiktok/app/v3/fetch_video_search_result"
TWITTER_PATH = "/api/v1/twitter/web/fetch_search_timeline"
_RETRY_STATUSES = (429, 500, 502, 503, 504)
_RETRIES = 1
_RETRY_BACKOFF_S = 1.0
_FATAL_STATUSES = (401, 402, 403)  # bad or unfunded key: stop at once, do not spend more requests

_MENTION = re.compile(r"(?<![\w])@[\w.]+")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\w)\+?\d(?:[\s().-]?\d){6,}")


class MissingTikHubKeyError(RuntimeError):
    pass


class TikHubApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class TikHubConfig:
    api_key: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    platforms: tuple[str, ...] = PLATFORMS
    max_requests: int = DEFAULT_MAX_REQUESTS  # most TikTok requests per search() call (X always uses one boolean query)

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "TikHubConfig":
        key = (environ.get(ENV_KEY) or "").strip()
        if not key:
            raise MissingTikHubKeyError(f"{ENV_KEY} is not set")
        base = (environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL).strip().rstrip("/")
        wanted = [p.strip().lower() for p in (environ.get(ENV_PLATFORMS) or "").split(",")]
        platforms = tuple(dict.fromkeys(p for p in wanted if p in PLATFORMS)) or PLATFORMS
        try:
            max_requests = int(environ.get(ENV_MAX_REQUESTS) or DEFAULT_MAX_REQUESTS)
        except ValueError:
            max_requests = DEFAULT_MAX_REQUESTS
        return cls(api_key=key, base_url=base, platforms=platforms, max_requests=max_requests if max_requests >= 1 else DEFAULT_MAX_REQUESTS)


def sanitize_snippet(text: object) -> str:
    """Post text for extraction only: no @handles, links, e-mail addresses or phone numbers (hashtags stay); at most 400 characters."""
    value = str(text or "")
    for pattern in (_URL, _EMAIL, _MENTION, _PHONE):
        value = pattern.sub(" ", value)
    return " ".join(value.split())[:SNIPPET_LIMIT]


def _quote(place: str) -> str:
    return f'"{place}"' if " " in place else place


def _tiktok_keywords(query: str, limit: int) -> list[str]:
    """TikTok search has no boolean OR: pair hazard terms with places in turn so each appears in the first requests."""
    hazards, places = parse_query(query)
    if not hazards:
        return [query.strip()] if query.strip() else []
    keywords: list[str] = []
    for index in range(max(len(hazards), len(places))):
        keyword = f"{hazards[index % len(hazards)]} {places[index % len(places)]}"
        if keyword not in keywords:
            keywords.append(keyword)
    return keywords[:limit]


def _twitter_keyword(query: str) -> str:
    hazards, places = parse_query(query)
    if not hazards:
        return query.strip()
    return f"({' OR '.join(hazards or list(HAZARD_TERMS))}) ({' OR '.join(_quote(p) for p in places)})"


def _posts(node: Any, is_post: Callable[[Mapping], bool]) -> Iterator[Mapping]:
    """Post objects of a response; once a post is found its inner objects (quoted posts, authors) are not visited."""
    if isinstance(node, Mapping):
        if is_post(node):
            yield node
            return
        for value in node.values():
            yield from _posts(value, is_post)
    elif isinstance(node, list):
        for value in node:
            yield from _posts(value, is_post)


def _unix_time(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if seconds > 1e11:  # milliseconds
        seconds /= 1000.0
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc) if seconds > 0 else None
    except (OverflowError, OSError, ValueError):
        return None


def _text_time(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _unix_time(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.isdigit():
        return _unix_time(text)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y")  # X: "Tue Feb 10 13:30:00 +0000 2026"
        except ValueError:
            try:
                parsed = parsedate_to_datetime(text)  # "Tue, 10 Feb 2026 13:30:00 GMT"
            except (TypeError, ValueError):
                return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _post_id(value: object) -> str:
    text = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    return text if text.isdigit() else ""


class TikHubPublicSearchAdapter:
    """Implements PublicSearchPort over TikHub's REST API for TikTok and X posts. Off unless the operator enables it."""

    def __init__(
        self,
        config: TikHubConfig,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._config = config
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self._sleep = sleep
        self._clock = clock

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "TikHubPublicSearchAdapter":
        return cls(TikHubConfig.from_env(environ))

    def _redact(self, text: str) -> str:
        return text.replace(self._config.api_key, "[redacted]")

    def _get(self, path: str, params: dict) -> dict:
        url = f"{self._config.base_url}{path}"
        headers = {"Authorization": f"Bearer {self._config.api_key}", "Accept": "application/json"}
        for attempt in range(_RETRIES + 1):
            try:
                response = self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                raise TikHubApiError(self._redact(f"TikHub request failed: {type(exc).__name__}")) from exc
            if response.status_code in _RETRY_STATUSES and attempt < _RETRIES:
                self._sleep(_RETRY_BACKOFF_S * (attempt + 1))
                continue
            if response.status_code >= 400:
                raise TikHubApiError(f"TikHub answered {response.status_code}: {self._redact(response.text[:200])}", response.status_code)
            try:
                payload = response.json()
            except ValueError as exc:
                raise TikHubApiError("TikHub answered with something that is not JSON", response.status_code) from exc
            if not isinstance(payload, dict):
                raise TikHubApiError("TikHub answered with an unexpected body", response.status_code)
            code = payload.get("code")
            if isinstance(code, int) and not isinstance(code, bool) and code != 200:
                raise TikHubApiError(f"TikHub answered code {code}: {self._redact(str(payload.get('message') or '')[:200])}", code)
            return payload
        raise TikHubApiError("TikHub request failed")  # pragma: no cover - the loop always returns or raises

    # --- platforms ---------------------------------------------------------------------------------------------

    def _tiktok(self, query: str, max_results: int) -> list[SearchResult]:
        found: list[SearchResult] = []
        failure: TikHubApiError | None = None
        succeeded = False
        for keyword in _tiktok_keywords(query, self._config.max_requests):
            params = {"keyword": keyword, "offset": 0, "count": max(1, min(int(max_results), 20)), "sort_type": 0, "publish_time": 7, "region": "PE"}
            try:
                payload = self._get(TIKTOK_PATH, params)
            except TikHubApiError as exc:
                if exc.status_code in _FATAL_STATUSES:
                    raise
                failure = exc
                continue
            succeeded = True
            found.extend(self._tiktok_results(payload.get("data")))
        if failure is not None and not succeeded:
            raise failure
        return found

    @staticmethod
    def _tiktok_results(data: Any) -> list[SearchResult]:
        results: list[SearchResult] = []
        for post in _posts(data, lambda d: "aweme_id" in d):
            post_id = _post_id(post.get("aweme_id"))
            snippet = sanitize_snippet(post.get("desc"))
            if not post_id or not snippet:
                continue
            results.append(
                SearchResult(title="", url=f"https://www.tiktok.com/@/video/{post_id}", published=_unix_time(post.get("create_time")), snippet=snippet, platform="tiktok")
            )
        return results

    def _twitter(self, query: str) -> list[SearchResult]:
        payload = self._get(TWITTER_PATH, {"keyword": _twitter_keyword(query), "search_type": "Latest"})
        results: list[SearchResult] = []
        for post in _posts(payload.get("data"), _is_tweet):
            legacy = post.get("legacy") if isinstance(post.get("legacy"), Mapping) else post
            post_id = _post_id(post.get("rest_id") or legacy.get("id_str") or post.get("tweet_id") or post.get("id_str"))
            snippet = sanitize_snippet(legacy.get("full_text") or legacy.get("text"))
            if not post_id or not snippet:
                continue
            results.append(
                SearchResult(title="", url=f"https://x.com/i/status/{post_id}", published=_text_time(legacy.get("created_at")), snippet=snippet, platform="twitter")
            )
        return results

    # --- port --------------------------------------------------------------------------------------------------

    def search(self, query: str, *, since: datetime, max_results: int = 5) -> list[SearchResult]:
        found: list[SearchResult] = []
        errors: list[TikHubApiError] = []
        for platform in self._config.platforms:
            try:
                found.extend(self._tiktok(query, max_results) if platform == "tiktok" else self._twitter(query))
            except TikHubApiError as exc:
                if exc.status_code in _FATAL_STATUSES or len(self._config.platforms) == 1:
                    raise
                errors.append(exc)
        if errors and not found and len(errors) == len(self._config.platforms):
            raise errors[0]
        seen: set[str] = set()
        results: list[SearchResult] = []
        for item in found:
            if item.url in seen or (item.published is not None and item.published < since):
                continue
            seen.add(item.url)
            results.append(item)
        return results


def _is_tweet(node: Mapping) -> bool:
    legacy = node.get("legacy")
    if isinstance(legacy, Mapping) and ("full_text" in legacy or "text" in legacy):
        return "rest_id" in node or "id_str" in legacy
    return "tweet_id" in node and ("text" in node or "full_text" in node)
