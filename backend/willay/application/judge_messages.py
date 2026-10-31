"""Use case: an advisory LLM judge for composed Spanish alert messages (clarity and faithfulness).

The judge (Muse Wonder) scores messages in one batched request. It never changes a message or a tier. Faithfulness
is also checked deterministically (every required fact must appear in the text); when the judge disagrees with the
code, the code wins and the disagreement is reported.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from willay.domain.entities import AlertTier
from willay.ports.agent import StructuredExtractionPort

JUDGE_MODEL = "muse-wonder-1.0"
MAX_OUTPUT_TOKENS = 6000
_BODIES = ("SENAMHI", "INDECI", "ENFEN")

JUDGE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "judgements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "clarity": {"type": "integer", "description": "1 (confusing) to 5 (clear at first read)"},
                    "faithfulness": {
                        "type": "integer",
                        "description": "1 to 5: all given facts are present and nothing is added or changed",
                    },
                    "low_literacy_ok": {"type": "boolean", "description": "Plain enough for readers with low literacy"},
                    "issues": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "clarity", "faithfulness", "low_literacy_ok", "issues"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["judgements"],
    "additionalProperties": False,
}

INSTRUCTIONS = (
    "You review Spanish early-warning messages for communities in Peru. For each message you receive the FACTS it must "
    "convey (tier, site, distance, safe point, official source). Score clarity and faithfulness from 1 to 5, say whether "
    "the wording is plain enough for low-literacy readers, and list concrete issues (in English, short). Faithful means "
    "every fact is present exactly and nothing is added, changed or contradicted, including the statement that the alert "
    "complements the official SENAMHI, INDECI and ENFEN alerts. The tier's standard action sentence (approved_standard_text) "
    "and that disclaimer are pre-approved wording: do not count them as additions, judge them only for clarity. You only "
    "evaluate wording: never suggest a different alert level."
)


class InvalidJudgement(ValueError):
    """The judge's answer does not cover the items or has out-of-range scores."""


@dataclass(frozen=True)
class JudgeItem:
    id: str
    tier: AlertTier
    text: str
    site_name: str
    distance_km: float
    safe_point: str | None
    official_source: str
    approved_text: str | None = None  # the tier's fixed action sentence (pre-approved template wording)


@dataclass(frozen=True)
class Judgement:
    id: str
    clarity: int
    faithfulness: int
    low_literacy_ok: bool
    issues: tuple[str, ...]
    missing_facts: tuple[str, ...]
    disagreement: bool


@dataclass(frozen=True)
class JudgementReport:
    model: str
    judgements: tuple[Judgement, ...]

    @property
    def mean_clarity(self) -> float:
        return sum(j.clarity for j in self.judgements) / len(self.judgements)

    @property
    def mean_faithfulness(self) -> float:
        return sum(j.faithfulness for j in self.judgements) / len(self.judgements)

    @property
    def low_literacy_ok_count(self) -> int:
        return sum(1 for j in self.judgements if j.low_literacy_ok)

    @property
    def disagreements(self) -> int:
        return sum(1 for j in self.judgements if j.disagreement)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "note": "Advisory LLM judgement of wording only. Deterministic fact checks are authoritative; nothing here "
            "changes a message or an alert level.",
            "summary": {
                "n": len(self.judgements),
                "mean_clarity": round(self.mean_clarity, 2),
                "mean_faithfulness": round(self.mean_faithfulness, 2),
                "low_literacy_ok": self.low_literacy_ok_count,
                "deterministic_disagreements": self.disagreements,
            },
            "judgements": [
                {
                    "id": j.id,
                    "clarity": j.clarity,
                    "faithfulness": j.faithfulness,
                    "low_literacy_ok": j.low_literacy_ok,
                    "issues": list(j.issues),
                    "missing_facts": list(j.missing_facts),
                    "disagreement": j.disagreement,
                }
                for j in self.judgements
            ],
        }


def facts_present(item: JudgeItem) -> list[str]:
    """Names of the required facts that do NOT appear in the message text (empty means faithful)."""
    missing = []
    if item.site_name not in item.text:
        missing.append("site_name")
    if f"{item.distance_km:.1f}" not in item.text:
        missing.append("distance")
    if item.safe_point and item.safe_point not in item.text:
        missing.append("safe_point")
    if item.official_source not in item.text:
        missing.append("official_source")
    if not all(body in item.text for body in _BODIES):
        missing.append("official_bodies")
    return missing


def judge_messages(items: list[JudgeItem], extractor: StructuredExtractionPort, model: str = JUDGE_MODEL) -> JudgementReport:
    payload = [
        {
            "id": i.id,
            "message": i.text,
            "facts": {
                "tier": i.tier.value,
                "site_name": i.site_name,
                "distance_km": round(i.distance_km, 1),
                "safe_point": i.safe_point,
                "official_source": i.official_source,
                "approved_standard_text": i.approved_text,
            },
        }
        for i in items
    ]
    raw = extractor.extract_json(
        instructions=INSTRUCTIONS,
        content=[{"type": "input_text", "text": json.dumps(payload, ensure_ascii=False)}],
        schema_name="message_judgements",
        schema=JUDGE_SCHEMA,
        purpose="judge_messages",
        model=model,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )
    try:
        by_id = {str(j["id"]): j for j in raw["judgements"]}
    except (KeyError, TypeError) as exc:
        raise InvalidJudgement("judge answer has no 'judgements' list") from exc
    if set(by_id) != {i.id for i in items}:
        raise InvalidJudgement("judge answer does not cover exactly the submitted messages")
    out: list[Judgement] = []
    for item in items:
        raw_item = by_id[item.id]
        try:
            clarity, faithfulness = int(raw_item["clarity"]), int(raw_item["faithfulness"])
            literate = bool(raw_item["low_literacy_ok"])
            issues = tuple(str(x) for x in raw_item["issues"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidJudgement(f"malformed judgement for {item.id}") from exc
        if not (1 <= clarity <= 5 and 1 <= faithfulness <= 5):
            raise InvalidJudgement(f"scores out of range for {item.id}")
        missing = tuple(facts_present(item))
        out.append(Judgement(item.id, clarity, faithfulness, literate, issues, missing, bool(missing) and faithfulness >= 4))
    return JudgementReport(model=model, judgements=tuple(out))
