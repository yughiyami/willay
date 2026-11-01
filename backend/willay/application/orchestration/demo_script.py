"""The scripted end-to-end demo: ENFEN extraordinary -> PREPARATION -> SENAMHI orange -> ATTENTION -> watcher clip ->
PENDING_CONFIRMATION -> operator confirms -> dispatch -> a household answers 2 -> operator case.

Each planner stage is an ordinary orchestrator run with a scenario (the conditions the domain rules see). The same
script runs live (Meta models) or offline (recorded model turns), so what the audience sees is the real orchestrator,
tools, rules and guardrails either way. Nothing here decides a tier: the scenarios only set inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from willay.application.rank_sites import RankedSite
from willay.domain.dispatch import Household
from willay.domain.entities import EnfenMagnitude, HazardContext, RainfallAnomaly, SenamhiLevel
from willay.domain.tiers import TierConfig

DEMO_CLIP_ID = "demo-clip"
DEMO_MARKER_CM = 100.0
DEMO_OPERATOR_ID = "OP-DEMO"

# With real data no site reaches the default PREPARATION threshold (0.5): the static risk is a product of three factors in
# [0, 1] and the highest real value is about 0.34. The demo therefore lowers the threshold to 0.30 and says so in the trace.
DEMO_TIER_CONFIG = TierConfig(preparation_site_risk_threshold=0.30)
DEMO_CONFIG_NOTE = (
    "demo thresholds: PREPARATION needs site static risk >= 0.30 (default 0.50; with real data no site reaches 0.50). "
    "ATTENTION and EVACUATION use the default rules."
)

BASE = HazardContext(enfen_magnitude=EnfenMagnitude.EXTRAORDINARY, seasonal_rainfall_anomaly=RainfallAnomaly.ABOVE_NORMAL)
AFTER_SENAMHI = HazardContext(
    enfen_magnitude=EnfenMagnitude.EXTRAORDINARY,
    seasonal_rainfall_anomaly=RainfallAnomaly.ABOVE_NORMAL,
    senamhi_level=SenamhiLevel.ORANGE,
    forecast_rain_72h_mm=75.0,
)
AFTER_RAIN = HazardContext(
    enfen_magnitude=EnfenMagnitude.EXTRAORDINARY,
    seasonal_rainfall_anomaly=RainfallAnomaly.ABOVE_NORMAL,
    senamhi_level=SenamhiLevel.ORANGE,
    forecast_rain_72h_mm=75.0,
    observed_rain_24h_mm=32.0,
)


@dataclass(frozen=True)
class DemoStage:
    key: str
    title_es: str
    scenario: HazardContext
    prompt: Callable[[str, str], str]  # (site_id, site_name) -> operator request
    clip_id: str | None = None


STAGES: Sequence[DemoStage] = (
    DemoStage(
        "enfen",
        "ENFEN declara Niño costero extraordinario",
        BASE,
        lambda site_id, name: (
            "El ENFEN declaró un Niño costero de magnitud extraordinaria y se pronostican lluvias sobre lo normal. "
            f"Pide al analista de peligro el ranking de relaves y el nivel de alerta del primero (site_id: {site_id}). "
            "Explica en pocas frases qué relaves requieren PREPARACIÓN."
        ),
    ),
    DemoStage(
        "senamhi",
        "SENAMHI emite aviso naranja",
        AFTER_SENAMHI,
        lambda site_id, name: (
            "SENAMHI emitió un aviso naranja y se pronostican 75 mm de lluvia en las próximas 72 horas. "
            f"Pide al analista de peligro el nivel de alerta actual del relave {site_id} y qué cambió respecto a la etapa anterior."
        ),
    ),
    DemoStage(
        "watcher",
        "Vigía: el río sube en el clip",
        AFTER_RAIN,
        lambda site_id, name: (
            f"Cayeron 32 mm en 24 horas. Hay un clip del río con un poste marcador de {DEMO_MARKER_CM:.0f} cm (clip_id: {DEMO_CLIP_ID}). "
            f"Haz tres delegaciones: 1) al Vigía, mide la subida del río; 2) al Despachador, prepara la propuesta de envío para {site_id}; "
            f"3) al Comunicador, redacta el mensaje para los hogares y genera la ficha de evacuación de {site_id}. "
            "Recuerda que solo el operador humano puede confirmar y enviar."
        ),
        clip_id=DEMO_CLIP_ID,
    ),
)


def pick_demo_site(ranked: Sequence[RankedSite], households: Sequence[Household]) -> RankedSite | None:
    """The highest-ranked site with synthetic demo households downstream (so the dispatch has someone to reach)."""
    household_settlements = {h.settlement_id for h in households}
    for candidate in ranked:
        if candidate.exposure and any(c.settlement_id in household_settlements for c in candidate.exposure.contributions):
            return candidate
    return None
