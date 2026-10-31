"""Cost estimation, the JSONL usage log and the budget guard for live Meta API calls."""

import json

import pytest

from willay.adapters.meta_usage import (
    BudgetExceededError,
    UsageLog,
    asr_cost,
    image_cost,
    sam_cost,
    text_cost,
)


def test_text_cost_uses_standard_spark_rates():
    # 1M input at 1.25 + 1M output at 4.25
    assert text_cost("muse-spark-1.3", input_tokens=1_000_000, output_tokens=1_000_000) == pytest.approx(5.5)


def test_text_cost_prices_cached_tokens_at_the_cached_rate():
    cost = text_cost("muse-spark-1.3", input_tokens=1_000_000, output_tokens=0, cached_tokens=400_000)
    assert cost == pytest.approx(0.6 * 1.25 + 0.4 * 0.15)


def test_contributor_and_wonder_rates_differ():
    assert text_cost("muse-spark-1.3-contributor", 1_000_000, 1_000_000) == pytest.approx(0.30)
    assert text_cost("muse-wonder-1.0", 1_000_000, 1_000_000) == pytest.approx(24.0)


def test_unknown_text_model_is_priced_at_the_most_expensive_known_rate():
    assert text_cost("some-future-model", 1_000_000, 0) == pytest.approx(4.0)


def test_sam_video_is_billed_per_frame_with_a_50_frame_minimum():
    assert sam_cost(frames=10) == pytest.approx(50 * 0.0002)
    assert sam_cost(frames=300) == pytest.approx(300 * 0.0002)


def test_sam_image_is_billed_per_image():
    assert sam_cost(images=2) == pytest.approx(2 * 0.0025)


def test_image_and_audio_costs():
    assert image_cost(1) == pytest.approx(0.01)
    assert asr_cost(seconds=3600.9) == pytest.approx(0.18)  # whole seconds, rounded down


def test_log_appends_one_json_line_per_call_and_sums_the_cost(tmp_path):
    log = UsageLog(tmp_path / "derived" / "meta_usage.jsonl", budget_usd=3.0)
    log.record(model="muse-spark-1.3", endpoint="/responses", purpose="unit", input_tokens=1000, output_tokens=500,
               est_cost_usd=text_cost("muse-spark-1.3", 1000, 500))
    log.record(model="sam-3.1", endpoint="/responses", purpose="unit", frames=50, est_cost_usd=sam_cost(frames=50))
    lines = (tmp_path / "derived" / "meta_usage.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["model"] == "muse-spark-1.3"
    assert first["input_tokens"] == 1000 and first["output_tokens"] == 500
    assert "ts" in first
    assert log.total_cost() == pytest.approx(sum(json.loads(l)["est_cost_usd"] for l in lines))


def test_log_never_stores_prompts_or_keys(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl")
    log.record(model="m", endpoint="/e", purpose="p", est_cost_usd=0.0, authorization="Bearer secret", prompt="household data")
    text = (tmp_path / "u.jsonl").read_text(encoding="utf-8")
    assert "secret" not in text and "household data" not in text


def test_budget_guard_blocks_a_call_that_would_exceed_the_budget(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl", budget_usd=1.0)
    log.record(model="m", endpoint="/e", purpose="p", est_cost_usd=0.95)
    log.ensure_within_budget(0.05)  # exactly at the budget is allowed
    with pytest.raises(BudgetExceededError):
        log.ensure_within_budget(0.06)


def test_total_cost_of_a_missing_file_is_zero(tmp_path):
    assert UsageLog(tmp_path / "none.jsonl").total_cost() == 0.0
