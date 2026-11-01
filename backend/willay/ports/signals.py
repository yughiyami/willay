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


# Hazard terms of every query (Spanish: huaico = Andean flash flood / debris flow, relave = mine tailings).
HAZARD_TERMS = ("huaico", "deslizamiento", "desborde", "relave", "inundación")

# The only social platforms a result may come from, and only when an explicitly enabled social adapter (TikHub) vouches for it
# by setting `SearchResult.platform`. A social URL without a matching platform is dropped like any other social page.
PLATFORM_HOSTS: dict[str, tuple[str, ...]] = {
    "tiktok": ("tiktok.com",),
    "twitter": ("x.com", "twitter.com"),
}


@dataclass(frozen=True)
class SearchResult:
    """What a search may hand over. `platform` is None for an ordinary web page and names the social platform otherwise;
    a social post has no title and its snippet (already stripped of handles, links and phone numbers) is for extraction only."""

    title: str
    url: str
    published: datetime | None
    snippet: str
    platform: str | None = None


def parse_query(query: str) -> tuple[list[str], list[str]]:
    """Split a query built by the use case ('t1 OR t2 ... tN Place1 OR Place2 ... Perú') into hazard terms and place names.

    Returns ([], []) when the text does not have that structure, so the caller can search it as free text.
    """
    text = (query or "").strip()
    if text.endswith("Perú"):
        text = text[: -len("Perú")].rstrip()
    tokens = [t.strip() for t in text.split(" OR ") if t.strip()]
    hazards: list[str] = []
    places: list[str] = []
    for token in tokens:
        lowered = token.lower()
        matched = next((h for h in HAZARD_TERMS if lowered == h or lowered.startswith(h + " ")), None)
        if matched and not places:
            hazards.append(matched)
            rest = token[len(matched):].strip()
            if rest:
                places.append(rest)
        elif hazards:
            places.append(token)
    if len(hazards) != len(HAZARD_TERMS) or not places:
        return [], []
    return hazards, places


def platform_allows(platform: str | None, url: str) -> bool:
    """True when a social URL's host belongs to the platform the adapter declared."""
    hosts = PLATFORM_HOSTS.get(platform or "")
    host = registered_host(url)
    return bool(hosts) and bool(host) and any(host == h or host.endswith("." + h) for h in hosts)


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
