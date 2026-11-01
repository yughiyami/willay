"""Loads the offline fixture of SIMULATED public signals (backend/fixtures/signals/public_signals_simulated.json).

The file must say `"synthetic": true` and every link must point to example.org, so a simulated report can never be mistaken
for a real page. Times are stored as hours before "now" so the set always sits inside the 72-hour window.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from willay.domain.signals import PublicSignal, SignalEventType
from willay.ports.signals import registered_host

FIXTURE_FILE = Path(__file__).resolve().parents[2] / "fixtures" / "signals" / "public_signals_simulated.json"
FAKE_HOST = "example.org"


class SimulatedSignalsError(ValueError):
    """The fixture is not a clearly synthetic set."""


def load_simulated_signals(path: Path | str = FIXTURE_FILE, now: datetime | None = None) -> list[PublicSignal]:
    moment = now or datetime.now(timezone.utc)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SimulatedSignalsError(f"cannot read the simulated signals file: {type(exc).__name__}") from exc
    if not isinstance(data, dict) or data.get("synthetic") is not True:
        raise SimulatedSignalsError(f"{path} is not flagged synthetic: simulated signals must say so explicitly")
    signals: list[PublicSignal] = []
    for item in data.get("signals", []):
        try:
            url = str(item["source_url"])
            if registered_host(url) != FAKE_HOST:
                raise SimulatedSignalsError(f"simulated signal links must point to {FAKE_HOST}, got '{url}'")
            signals.append(
                PublicSignal(
                    event_type=SignalEventType(str(item["event_type"])),
                    place=str(item["place"]),
                    ubigeo=str(item["ubigeo"]),
                    district=str(item["district"]),
                    lon=float(item["lon"]),
                    lat=float(item["lat"]),
                    event_time=moment - timedelta(hours=float(item["hours_ago"])),
                    confidence=float(item["confidence"]),
                    source_url=url,
                    source_domain=FAKE_HOST,
                    level=str(item.get("level", "settlement")),
                    simulated=True,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, SimulatedSignalsError):
                raise
            raise SimulatedSignalsError(f"invalid simulated signal: {type(exc).__name__}: {exc}") from exc
    return signals
