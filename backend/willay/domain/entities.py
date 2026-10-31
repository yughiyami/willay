"""Domain entities and value objects for Willay Relaves."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class OrderedEnum(Enum):
    """Enum whose members compare by declaration order."""

    def _index(self) -> int:
        return list(type(self)).index(self)

    def __lt__(self, other: object) -> bool:
        if other.__class__ is self.__class__:
            return self._index() < other._index()  # type: ignore[attr-defined]
        return NotImplemented

    def __le__(self, other: object) -> bool:
        if other.__class__ is self.__class__:
            return self._index() <= other._index()  # type: ignore[attr-defined]
        return NotImplemented

    def __gt__(self, other: object) -> bool:
        if other.__class__ is self.__class__:
            return self._index() > other._index()  # type: ignore[attr-defined]
        return NotImplemented

    def __ge__(self, other: object) -> bool:
        if other.__class__ is self.__class__:
            return self._index() >= other._index()  # type: ignore[attr-defined]
        return NotImplemented


class EnfenMagnitude(OrderedEnum):
    """ENFEN coastal El Nino magnitude scale."""

    NEUTRAL = "neutral"
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"
    EXTRAORDINARY = "extraordinary"


class RainfallAnomaly(Enum):
    """Seasonal rainfall anomaly forecast category for a region."""

    BELOW_NORMAL = "below_normal"
    NORMAL = "normal"
    ABOVE_NORMAL = "above_normal"


class SenamhiLevel(OrderedEnum):
    """SENAMHI warning level for a province."""

    GREEN = "green"
    YELLOW = "yellow"
    ORANGE = "orange"
    RED = "red"


class SiteStatus(Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    ABANDONED = "abandoned"


class RiskLevel(OrderedEnum):
    """Declared risk level of a tailings site."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


class AlertTier(Enum):
    NONE = "NONE"
    PREPARATION = "PREPARATION"
    ATTENTION = "ATTENTION"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    EVACUATION = "EVACUATION"


def _check_coordinates(lon: float, lat: float) -> None:
    if not -180.0 <= lon <= 180.0 or not -90.0 <= lat <= 90.0:
        raise ValueError(f"invalid coordinates lon={lon} lat={lat}")


@dataclass(frozen=True)
class PathPoint:
    lon: float
    lat: float
    cumulative_distance_m: float

    def __post_init__(self) -> None:
        _check_coordinates(self.lon, self.lat)
        if self.cumulative_distance_m < 0:
            raise ValueError("cumulative_distance_m must be >= 0")


@dataclass(frozen=True)
class FlowPath:
    """Downstream flow path as an ordered polyline with cumulative distance."""

    points: tuple[PathPoint, ...]

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ValueError("a flow path needs at least two points")
        previous = self.points[0].cumulative_distance_m
        for point in self.points[1:]:
            if point.cumulative_distance_m < previous:
                raise ValueError("cumulative distance must be non-decreasing")
            previous = point.cumulative_distance_m

    @property
    def total_length_m(self) -> float:
        return self.points[-1].cumulative_distance_m


@dataclass(frozen=True)
class Settlement:
    id: str
    name: str
    lon: float
    lat: float
    population: int
    households: int
    has_water_intake: bool = False
    safe_point: str | None = None

    def __post_init__(self) -> None:
        _check_coordinates(self.lon, self.lat)
        if self.population < 0 or self.households < 0:
            raise ValueError("population and households must be >= 0")


@dataclass(frozen=True)
class TailingsSite:
    id: str
    name: str
    lon: float
    lat: float
    volume_m3: float
    status: SiteStatus
    declared_risk: RiskLevel
    distance_to_river_m: float
    susceptibility: float
    district: str = ""
    province: str = ""
    synthetic: bool = False
    flow_path: FlowPath | None = None

    def __post_init__(self) -> None:
        _check_coordinates(self.lon, self.lat)
        if self.volume_m3 < 0 or self.distance_to_river_m < 0:
            raise ValueError("volume_m3 and distance_to_river_m must be >= 0")
        if not 0.0 <= self.susceptibility <= 1.0:
            raise ValueError("susceptibility must be within [0, 1]")


@dataclass(frozen=True)
class HazardContext:
    """Hazard inputs known at evaluation time for one site/household."""

    enfen_magnitude: EnfenMagnitude = EnfenMagnitude.NEUTRAL
    seasonal_rainfall_anomaly: RainfallAnomaly = RainfallAnomaly.NORMAL
    senamhi_level: SenamhiLevel = SenamhiLevel.GREEN
    forecast_rain_72h_mm: float = 0.0
    observed_rain_24h_mm: float = 0.0
    water_rise_cm: float | None = None
    watcher_report: bool = False
    operator_confirmed: bool = False
