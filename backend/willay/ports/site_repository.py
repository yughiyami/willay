from __future__ import annotations

from typing import Protocol, Sequence

from willay.domain.entities import Settlement, TailingsSite, WaterIntake


class SiteRepositoryPort(Protocol):
    """Read access to tailings sites and downstream settlements."""

    def list_sites(self) -> Sequence[TailingsSite]: ...

    def get_site(self, site_id: str) -> TailingsSite | None: ...

    def list_settlements(self) -> Sequence[Settlement]: ...

    def list_water_intakes(self) -> Sequence[WaterIntake]:
        """Drinking-water intakes (coordinates may be approximate; see WaterIntake.approximate)."""
        ...
