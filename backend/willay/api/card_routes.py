"""POST /cards/evacuation: one illustrated evacuation card for a SYNTHETIC household."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from willay.adapters.meta_model_api import MetaApiError
from willay.adapters.meta_usage import BudgetExceededError
from willay.api.dependencies import AppDependencies
from willay.application.evacuation_card import CardRequest, build_card
from willay.domain.entities import AlertTier


class EvacuationCardIn(BaseModel):
    settlement_name: str = Field(min_length=1, max_length=80)
    distance_km: float = Field(ge=0, le=500, description="Distance along the tailings flow path")
    safe_point: str | None = Field(default=None, max_length=80)
    tier: Literal["PREPARATION", "ATTENTION", "EVACUATION"] = Field(description="Tier from /alerts/evaluate (domain rules)")
    synthetic_household: Literal[True] = Field(
        description="Must be true: requests go to a third-party model, so only synthetic households are allowed"
    )


def register_card_routes(app: FastAPI, deps: AppDependencies) -> None:
    @app.post("/cards/evacuation")
    def evacuation_card(request: EvacuationCardIn) -> dict:
        if deps.image_generator is None:
            raise HTTPException(status_code=503, detail="the image model is not configured (set WILLAY_META_ENABLED=1 and META_API_KEY)")
        try:
            card = build_card(
                CardRequest(request.settlement_name, request.distance_km, request.safe_point, AlertTier(request.tier)),
                deps.image_generator,
            )
        except BudgetExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except MetaApiError as exc:
            raise HTTPException(status_code=502, detail=f"image generation failed: {exc}") from exc
        saved_as = None
        if deps.derived_dir is not None:
            saved_as = f"evacuation_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}.png"
            target = deps.derived_dir / "cards"
            target.mkdir(parents=True, exist_ok=True)
            (target / saved_as).write_bytes(card.image)
        return {
            "tier": request.tier,
            "image_png_base64": base64.b64encode(card.image).decode(),
            "intended_text": card.intended_text,
            "review_required": card.review_required,
            "saved_as": saved_as,
            "note": "Synthetic household. Image models can misrender text: a human must review the card against intended_text.",
        }
