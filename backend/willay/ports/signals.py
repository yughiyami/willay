"""Ports for public signals: a web search that returns minimal results.

A result carries the page title, URL, published date and a short snippet, nothing else (no author, no profile, no full text).
The search never touches social platforms: their domains are excluded in the request and filtered again on the way in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import urlsplit

# Social platforms are never searched or kept: Willay reads public news and web pages, it does not scrape social networks.
SOCIAL_DOMAINS: tuple[str, ...] = (
    "facebook.com",
    "fb.com",
    "fb.watch",
    "instagram.com",
    "x.com",
    "twitter.com",
    "t.co",
    "tiktok.com",
    "youtube.com",
    "youtu.be",
    "t.me",
    "telegram.me",
    "whatsapp.com",
    "linkedin.com",
    "reddit.com",
    "threads.net",
    "snapchat.com",
    "pinterest.com",
    "vk.com",
    "discord.com",
)


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    published: datetime | None
    snippet: str


def registered_host(url: str) -> str:
    """Lower-case host of an http(s) URL without 'www.', port or credentials; '' when the URL is not a web URL."""
    try:
        parts = urlsplit((url or "").strip())
        host = parts.hostname
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not host:
        return ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def is_social_host(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in SOCIAL_DOMAINS)


class PublicSearchPort(Protocol):
    """Searches news and public web pages published since a moment. Implementations never return social-platform pages."""

    def search(self, query: str, *, since: datetime, max_results: int = 5) -> list[SearchResult]: ...
