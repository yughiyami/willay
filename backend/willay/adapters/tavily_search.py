"""Tavily Search adapter (https://docs.tavily.com/documentation/api-reference/endpoint/search).

Searches the public web only: news and general pages, never social platforms (their domains are excluded in the request and
filtered again here). Each call returns title, URL, published date and a short snippet and nothing else: raw page content,
authors and images are neither requested nor kept. The key comes from the environment variable TAVILY_API_KEY, travels in the
Authorization header and is never logged, echoed or put in an error message.

Request shape per call (POST {base}/search): query, topic (news | general), search_depth=basic, max_results,
include_answer=false, include_raw_content=false, exclude_domains, and `days` (news) or `start_date` (general). It follows the
public documentation; it has not been exercised against the live service (see docs/SIGNALS.md).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Mapping

import httpx

from willay.ports.signals import SOCIAL_DOMAINS, SearchResult, is_social_host, registered_host

DEFAULT_BASE_URL = "https://api.tavily.com"
ENV_KEY = "TAVILY_API_KEY"
TITLE_LIMIT = 200
SNIPPET_LIMIT = 400
_RETRY_STATUSES = (429, 500, 502, 503, 504)
_RETRIES = 1
_RETRY_BACKOFF_S = 1.0


class MissingTavilyKeyError(RuntimeError):
    pass


class TavilyApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class TavilyConfig:
    api_key: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "TavilyConfig":
        key = (environ.get(ENV_KEY) or "").strip()
        if not key:
            raise MissingTavilyKeyError(f"{ENV_KEY} is not set")
        base = (environ.get("TAVILY_API_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
        return cls(api_key=key, base_url=base)


def _parse_published(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class TavilySearchAdapter:
    """Implements PublicSearchPort over HTTP."""

    def __init__(
        self,
        config: TavilyConfig,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._clock = clock
        self._config = config
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self._sleep = sleep

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "TavilySearchAdapter":
        return cls(TavilyConfig.from_env(environ))

    def _redact(self, text: str) -> str:
        return text.replace(self._config.api_key, "[redacted]")

    def _post(self, body: dict) -> dict:
        url = f"{self._config.base_url}/search"
        headers = {"Authorization": f"Bearer {self._config.api_key}", "Content-Type": "application/json"}
        for attempt in range(_RETRIES + 1):
            try:
                response = self._client.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                raise TavilyApiError(self._redact(f"Tavily request failed: {type(exc).__name__}")) from exc
            if response.status_code in _RETRY_STATUSES and attempt < _RETRIES:
                self._sleep(_RETRY_BACKOFF_S * (attempt + 1))
                continue
            if response.status_code >= 400:
                detail = self._redact(response.text[:200])
                raise TavilyApiError(f"Tavily answered {response.status_code}: {detail}", response.status_code)
            try:
                payload = response.json()
            except ValueError as exc:
                raise TavilyApiError("Tavily answered with something that is not JSON", response.status_code) from exc
            if not isinstance(payload, dict):
                raise TavilyApiError("Tavily answered with an unexpected body", response.status_code)
            return payload
        raise TavilyApiError("Tavily request failed")  # pragma: no cover - the loop always returns or raises

    def search(self, query: str, *, since: datetime, max_results: int = 5) -> list[SearchResult]:
        common = {
            "query": query,
            "search_depth": "basic",
            "max_results": max(1, min(int(max_results), 10)),
            "include_answer": False,
            "include_raw_content": False,
            "exclude_domains": list(SOCIAL_DOMAINS),
        }
        now = self._clock()
        days = max(1, math.ceil((now - since).total_seconds() / 86400.0)) if since <= now else 1
        news = self._post({**common, "topic": "news", "days": days})
        pages = self._post({**common, "topic": "general", "start_date": since.date().isoformat()})
        seen: set[str] = set()
        results: list[SearchResult] = []
        for payload in (news, pages):
            for item in payload.get("results") or []:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                host = registered_host(url)
                if not host or is_social_host(host) or url in seen:
                    continue
                seen.add(url)
                results.append(
                    SearchResult(
                        title=str(item.get("title") or "").strip()[:TITLE_LIMIT],
                        url=url,
                        published=_parse_published(item.get("published_date")),
                        snippet=" ".join(str(item.get("content") or "").split())[:SNIPPET_LIMIT],
                    )
                )
        return results
