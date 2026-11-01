"""Dispatch proposals: the agents propose, only an explicit operator click (with an id) approves."""

import pytest

from willay.application.orchestration.runtime import (
    ClipStore,
    Proposal,
    ProposalError,
    ProposalStore,
    approve_proposal,
)
from willay.domain.entities import AlertTier, EnfenMagnitude, HazardContext, SenamhiLevel


def pending_evacuation_context():
    return HazardContext(
        enfen_magnitude=EnfenMagnitude.EXTRAORDINARY,
        senamhi_level=SenamhiLevel.ORANGE,
        observed_rain_24h_mm=35.0,
        water_rise_cm=45.0,
    )


def make(store, tier=AlertTier.EVACUATION, evaluated="PENDING_CONFIRMATION", context=None):
    return store.add(
        Proposal(
            run_id="run-1", site_id="s1", site_name="Alpha", tier=tier, evaluated_tier=evaluated,
            reasons=("r",), context=context or pending_evacuation_context(), households_targeted=4,
        )
    )


def test_store_assigns_ids_and_lists_in_order():
    store = ProposalStore()
    a, b = make(store), make(store)
    assert a.id != b.id and [p.id for p in store.list()] == [a.id, b.id]
    assert store.get(a.id) is a and store.get("zzz") is None


def test_approval_needs_an_operator_confirmation_id():
    store = ProposalStore()
    p = make(store)
    with pytest.raises(ProposalError):
        approve_proposal(store, p.id, "   ", static_risk=0.4)
    assert p.status == "awaiting_operator"


def test_approving_a_pending_evacuation_re_evaluates_with_the_operator_confirmation():
    store = ProposalStore()
    p = make(store)
    approved = approve_proposal(store, p.id, "OP-7", static_risk=0.4)
    assert approved.status == "approved" and approved.operator_confirmation_id == "OP-7"
    assert approved.final_tier is AlertTier.EVACUATION


def test_an_attention_proposal_is_also_approved_by_an_operator_click():
    store = ProposalStore()
    ctx = HazardContext(senamhi_level=SenamhiLevel.ORANGE)
    p = make(store, tier=AlertTier.ATTENTION, evaluated="ATTENTION", context=ctx)
    assert approve_proposal(store, p.id, "OP-1", static_risk=0.4).final_tier is AlertTier.ATTENTION


def test_a_stale_proposal_whose_conditions_no_longer_hold_is_refused():
    store = ProposalStore()
    p = make(store, context=HazardContext(senamhi_level=SenamhiLevel.ORANGE))  # no rain, no rise: not even pending
    with pytest.raises(ProposalError):
        approve_proposal(store, p.id, "OP-1", static_risk=0.4)


def test_a_proposal_can_be_handled_only_once():
    store = ProposalStore()
    p = make(store)
    approve_proposal(store, p.id, "OP-1", static_risk=0.4)
    with pytest.raises(ProposalError):
        approve_proposal(store, p.id, "OP-2", static_risk=0.4)
    with pytest.raises(ProposalError):
        store.reject(p.id)


def test_rejecting_closes_the_proposal_without_dispatching():
    store = ProposalStore()
    p = make(store)
    assert store.reject(p.id).status == "rejected"


def test_unknown_proposal_is_an_error():
    with pytest.raises(ProposalError):
        approve_proposal(ProposalStore(), "nope", "OP-1", static_risk=0.4)


def test_clip_store_keeps_clips_by_id():
    clips = ClipStore()
    clips.add("c1", b"data", "video/mp4")
    assert clips.get("c1").data == b"data" and clips.get("c2") is None and clips.ids() == ["c1"]
