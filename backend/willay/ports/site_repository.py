from __future__ import annotations

from typing import Protocol, Sequence

from willay.domain.entities import Settlement, TailingsSite


class SiteRepositoryPort(Protocol):
    """Read access to tailings sites and downstream settlements."""

    def list_sites(self) -> Sequence[TailingsSite]: ...

    def get_site(self, site_id: str) -> TailingsSite | None: ...

    def list_settlements(self) -> Sequence[Settlement]: ...
