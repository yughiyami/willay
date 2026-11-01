"""Shared state of the multi-agent orchestrator: services, configuration, clips and dispatch proposals.

Dispatch proposals are the only thing the agents can create. They have no way to send anything: a proposal becomes a
dispatch only when a human operator approves it (with a confirmation id) through the API, and the tier is then
re-evaluated by the domain rules with that confirmation.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence

from willay.application.evaluate_alert_tier import evaluate_alert_tier
from willay.application.rank_sites import RankedSite
from willay.domain.entities import AlertTier, HazardContext, Settlement
from willay.domain.tiers import TierConfig
from willay.ports.image import ImagePort
from willay.ports.llm import LLMPort
from willay.ports.segmentation import VideoSegmentationPort
from willay.ports.templates import ReviewedTemplatesPort

DEFAULT_PLANNER_MODEL = "muse-wonder-1.0"
DEFAULT_FALLBACK_MODEL = "muse-spark-1.3"
DEFAULT_SPECIALIST_MODEL = "muse-spark-1.3"


@dataclass(frozen=True)
class OrchestratorConfig:
    planner_model: str = DEFAULT_PLANNER_MODEL
    planner_fallback_model: str = DEFAULT_FALLBACK_MODEL
    specialist_model: str = DEFAULT_SPECIALIST_MODEL
    min_planner_budget_usd: float = 0.40  # below this remaining budget the planner runs on the cheaper fallback model
    run_budget_usd: float = 0.75  # hard cap on the estimated cost of one run
    max_agent_steps: int = 6
    max_delegations: int = 8
    planner_max_output_tokens: int = 1800
    specialist_max_output_tokens: int = 1400


@dataclass(frozen=True)
class Clip:
    data: bytes
    content_type: str = "video/mp4"


class ClipStore:
    """River-watcher clips uploaded by the operator (kept in memory, no people in frame)."""

    def __init__(self) -> None:
        self._clips: dict[str, Clip] = {}
        self._lock = threading.Lock()

    def add(self, clip_id: str, data: bytes, content_type: str = "video/mp4") -> str:
        with self._lock:
            self._clips[clip_id] = Clip(data, content_type)
        return clip_id

    def get(self, clip_id: str) -> Clip | None:
        with self._lock:
            return self._clips.get(clip_id)

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._clips)


class ProposalError(ValueError):
    """The proposal cannot be handled (unknown, already handled, missing confirmation id, or stale)."""


@dataclass
class Proposal:
    run_id: str
    site_id: str
    site_name: str
    tier: AlertTier  # the tier that would be dispatched
    evaluated_tier: str  # what the domain rules returned without an operator confirmation
    reasons: Sequence[str]
    context: HazardContext  # snapshot the tier was evaluated on
    households_targeted: int = 0
    tier_config: TierConfig | None = None  # thresholds the tier was evaluated with
    id: str = ""
    status: str = "awaiting_operator"  # awaiting_operator | approved | rejected | dispatched
    requires_operator_confirmation_id: bool = True
    operator_confirmation_id: str | None = None
    final_tier: AlertTier | None = None
    dispatch_id: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "site_id": self.site_id,
            "site_name": self.site_name,
            "tier": self.tier.value,
            "evaluated_tier": self.evaluated_tier,
            "reasons": list(self.reasons),
            "households_targeted": self.households_targeted,
            "status": self.status,
            "requires_operator_confirmation_id": self.requires_operator_confirmation_id,
            "operator_confirmation_id": self.operator_confirmation_id,
            "final_tier": None if self.final_tier is None else self.final_tier.value,
            "dispatch_id": self.dispatch_id,
            "created_at": self.created_at,
            "water_rise_cm": self.context.water_rise_cm,
        }


class ProposalStore:
    def __init__(self) -> None:
        self._items: dict[str, Proposal] = {}
        self._lock = threading.Lock()

    def add(self, proposal: Proposal) -> Proposal:
        with self._lock:
            proposal.id = proposal.id or f"prop-{uuid.uuid4().hex[:8]}"
            self._items[proposal.id] = proposal
        return proposal

    def get(self, proposal_id: str) -> Proposal | None:
        with self._lock:
            return self._items.get(proposal_id)

    def list(self) -> list[Proposal]:
        with self._lock:
            return list(self._items.values())

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def reject(self, proposal_id: str) -> Proposal:
        with self._lock:
            proposal = self._items.get(proposal_id)
            if proposal is None:
                raise ProposalError(f"unknown proposal '{proposal_id}'")
            if proposal.status != "awaiting_operator":
                raise ProposalError(f"proposal is already {proposal.status}")
            proposal.status = "rejected"
            return proposal

    def mark_dispatched(self, proposal_id: str, dispatch_id: str) -> None:
        with self._lock:
            proposal = self._items[proposal_id]
            proposal.status, proposal.dispatch_id = "dispatched", dispatch_id

    def reopen(self, proposal_id: str) -> None:
        """Put an approved proposal back when the dispatch itself failed, so the operator can retry."""
        with self._lock:
            proposal = self._items.get(proposal_id)
            if proposal is not None and proposal.status == "approved":
                proposal.status, proposal.operator_confirmation_id, proposal.final_tier = "awaiting_operator", None, None


def approve_proposal(
    store: ProposalStore,
    proposal_id: str,
    operator_confirmation_id: str | None,
    static_risk: float,
    config: TierConfig | None = None,
) -> Proposal:
    """The operator's explicit click. Re-evaluates the tier with the confirmation; refuses a stale proposal."""
    confirmation = (operator_confirmation_id or "").strip()
    with store._lock:  # noqa: SLF001 - the check and the status change must be one step
        proposal = store._items.get(proposal_id)  # noqa: SLF001
        if proposal is None:
            raise ProposalError(f"unknown proposal '{proposal_id}'")
        if proposal.status != "awaiting_operator":
            raise ProposalError(f"proposal is already {proposal.status}")
        if not confirmation:
            raise ProposalError("an operator confirmation id is required to approve a dispatch")
        evaluation = evaluate_alert_tier(replace(proposal.context, operator_confirmed=True), static_risk, config or proposal.tier_config)
        if evaluation.tier is not proposal.tier:
            raise ProposalError(
                f"the conditions changed: the rules now return {evaluation.tier.value}, not {proposal.tier.value}"
            )
        proposal.status = "approved"
        proposal.operator_confirmation_id = confirmation
        proposal.final_tier = evaluation.tier
        return proposal


@dataclass
class OrchestratorServices:
    """Everything the agents' tools may use. Optional capabilities left as None are reported to the agent, not faked."""

    ranked: Callable[[], Sequence[RankedSite]]
    hazard: HazardContext
    official_source: str
    settlements: Callable[[], Sequence[Settlement]]
    clips: ClipStore
    proposals: ProposalStore
    households: Callable[[str], int] = lambda site_id: 0
    templates: ReviewedTemplatesPort | None = None
    llm: LLMPort | None = None
    image: ImagePort | None = None
    segmenter: VideoSegmentationPort | None = None
    districts: Mapping | None = None
    backtest_results: Mapping | None = None
    learned_model: Mapping | None = None
    rain_context: Mapping | None = None
    tier_config: TierConfig | None = None
    water_prompt: str = "water"  # SAM concept prompts; the wording matters (see docs/WATCHER_PROTOCOL.md)
    marker_prompt: str = "red post"
    budget_remaining: Callable[[], float | None] = lambda: None
    clip_probe: Callable[[bytes, str], int | None] | None = None  # (video, suffix) -> total frames
    overlay_renderer: Callable[[bytes, str, object], list[tuple[int, bytes]]] | None = None  # (video, suffix, measurement)
