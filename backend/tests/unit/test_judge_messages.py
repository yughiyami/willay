"""Muse Wonder as an advisory judge of composed alert messages; deterministic fact checks stay authoritative."""

import json

import httpx
import pytest

from willay.adapters.meta_model_api import MODEL_JUDGE, MetaModelApiAdapter, MetaModelApiConfig
from willay.application.judge_messages import (
    JUDGE_SCHEMA,
    InvalidJudgement,
    JudgeItem,
    facts_present,
    judge_messages,
)
from willay.domain.entities import AlertTier

SOURCE = "ENFEN Comunicado Oficial N°18-2026 (2026-10-16)"


def item(i=1, tier=AlertTier.ATTENTION, text=None, **kw) -> JudgeItem:
    base = dict(site_name="Relave Prueba", distance_km=3.4, safe_point="Loma segura", official_source=SOURCE)
    base.update(kw)
    text = text or (
        f"[{tier.value}] {base['site_name']}: Siga los boletines. Distancia desde el relave: {base['distance_km']:.1f} km. "
        f"Punto seguro: {base['safe_point']}. Fuente oficial: {base['official_source']}. "
        "Esta alerta complementa las alertas oficiales de SENAMHI, INDECI y ENFEN y no las reemplaza."
    )
    return JudgeItem(id=f"m{i}", tier=tier, text=text, **base)


class FakeExtractor:
    def __init__(self, result):
        self.result, self.calls = result, []

    def extract_json(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def test_approved_standard_wording_is_sent_so_it_is_not_judged_as_an_addition():
    extractor = FakeExtractor({"judgements": [verdict(1)]})
    judge_messages([item(1, approved_text="Siga los boletines.")], extractor)
    (call,) = extractor.calls
    assert "Siga los boletines." in call["content"][0]["text"]
    assert "pre-approved" in call["instructions"].lower()


def test_action_text_is_the_template_wording_of_the_tier():
    from willay.application.compose_alert_message import action_text

    assert "EVACÚE AHORA" in action_text(AlertTier.EVACUATION, "es")
    assert "EVACUATE NOW" in action_text(AlertTier.EVACUATION, "en")


def verdict(i, clarity=5, faithfulness=5, ok=True, issues=()):
    return {"id": f"m{i}", "clarity": clarity, "faithfulness": faithfulness, "low_literacy_ok": ok, "issues": list(issues)}


def test_facts_present_checks_every_required_fact():
    assert facts_present(item()) == []
    missing = facts_present(item(text="Alerta sin datos"))
    assert {"site_name", "distance", "safe_point", "official_source", "official_bodies"} <= set(missing)


def test_one_batched_call_to_the_judge_model_with_the_schema():
    extractor = FakeExtractor({"judgements": [verdict(1), verdict(2)]})
    report = judge_messages([item(1), item(2)], extractor)
    (call,) = extractor.calls
    assert call["model"] == MODEL_JUDGE == "muse-wonder-1.0"
    assert call["schema"] == JUDGE_SCHEMA
    assert call["purpose"] == "judge_messages"
    assert call["max_output_tokens"] > 0
    payload = call["content"][0]["text"]
    assert "m1" in payload and "m2" in payload and SOURCE in payload
    assert [j.id for j in report.judgements] == ["m1", "m2"]


def test_report_aggregates_scores():
    report = judge_messages([item(1), item(2)], FakeExtractor({"judgements": [verdict(1, 4, 5), verdict(2, 2, 3, False, ["too long"])]}))
    assert report.mean_clarity == pytest.approx(3.0)
    assert report.mean_faithfulness == pytest.approx(4.0)
    assert report.judgements[1].issues == ("too long",)
    assert report.low_literacy_ok_count == 1


def test_judge_that_misses_a_fact_the_code_finds_missing_is_flagged_as_disagreement():
    bad = item(1, text="Alerta sin datos")
    report = judge_messages([bad], FakeExtractor({"judgements": [verdict(1, 5, 5)]}))
    assert report.judgements[0].missing_facts  # deterministic result is authoritative
    assert report.judgements[0].disagreement is True
    assert report.disagreements == 1


def test_a_judgement_set_that_does_not_match_the_items_is_rejected():
    with pytest.raises(InvalidJudgement):
        judge_messages([item(1), item(2)], FakeExtractor({"judgements": [verdict(1)]}))
    with pytest.raises(InvalidJudgement):
        judge_messages([item(1)], FakeExtractor({"judgements": [verdict(1, clarity=9)]}))


def test_the_report_states_it_is_advisory():
    report = judge_messages([item(1)], FakeExtractor({"judgements": [verdict(1)]}))
    assert "advisory" in report.to_dict()["note"].lower()
    assert "tier" not in json.dumps(report.to_dict()["summary"]).lower()


def test_adapter_forwards_max_output_tokens_and_model():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"id": "r", "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}], "usage": {"input_tokens": 1, "output_tokens": 1}},
        )

    adapter = MetaModelApiAdapter(MetaModelApiConfig(api_key="k"), httpx.Client(transport=httpx.MockTransport(handler)))
    adapter.extract_json(instructions="x", content=[], schema_name="s", schema={"type": "object"}, model=MODEL_JUDGE, max_output_tokens=3000)
    assert seen[0]["max_output_tokens"] == 3000 and seen[0]["model"] == "muse-wonder-1.0"
