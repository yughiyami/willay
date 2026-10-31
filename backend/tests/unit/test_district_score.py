import pytest

from willay.domain.district_score import (
    DistrictFeatures,
    DistrictScoreConfig,
    percentile_ranks,
    score_districts,
)


def features(ubigeo, high=0.0, slope=0.0, exposed=0.0, tailings=0.0, name="D"):
    return DistrictFeatures(
        ubigeo=ubigeo,
        name=name,
        mm_high_share=high,
        mean_slope_deg=slope,
        exposed_population=exposed,
        tailings_upstream=tailings,
    )


# --- percentile ranks ---------------------------------------------------------


def test_percentile_ranks_span_zero_to_one_in_value_order():
    assert percentile_ranks([10.0, 30.0, 20.0]) == [0.0, 1.0, 0.5]


def test_ties_share_the_average_rank():
    assert percentile_ranks([5.0, 5.0, 5.0]) == [0.5, 0.5, 0.5]
    assert percentile_ranks([1.0, 2.0, 2.0, 3.0]) == pytest.approx([0.0, 0.5, 0.5, 1.0])


def test_missing_values_stay_missing_and_do_not_distort_the_others():
    assert percentile_ranks([1.0, None, 3.0]) == [0.0, None, 1.0]


def test_single_value_gets_the_neutral_rank():
    assert percentile_ranks([7.0]) == [0.5]
    assert percentile_ranks([]) == []


# --- composite score ----------------------------------------------------------


def test_score_is_a_weighted_mean_of_component_ranks_in_unit_interval():
    cfg = DistrictScoreConfig(weights={"mm_high_share": 1.0, "mean_slope_deg": 1.0, "exposed_population": 1.0, "tailings_upstream": 1.0})
    districts = [
        features("a", high=0.1, slope=5, exposed=0, tailings=0),
        features("b", high=0.5, slope=15, exposed=1000, tailings=2),
        features("c", high=0.9, slope=25, exposed=9000, tailings=9),
    ]
    scores = {s.ubigeo: s for s in score_districts(districts, cfg)}
    assert scores["a"].value == 0.0
    assert scores["b"].value == pytest.approx(0.5)
    assert scores["c"].value == 1.0
    assert set(scores["c"].components) == {"mm_high_share", "mean_slope_deg", "exposed_population", "tailings_upstream"}


def test_default_weights_are_fixed_a_priori_and_sum_to_one():
    cfg = DistrictScoreConfig()
    assert sum(cfg.weights.values()) == pytest.approx(1.0)
    assert set(cfg.weights) == {"mm_high_share", "mean_slope_deg", "exposed_population", "tailings_upstream"}


def test_weights_change_the_ordering():
    districts = [
        features("a", high=0.9, slope=1, exposed=0, tailings=0),
        features("b", high=0.1, slope=30, exposed=5000, tailings=5),
        features("c", high=0.5, slope=10, exposed=100, tailings=1),
    ]
    susceptibility_only = DistrictScoreConfig(weights={"mm_high_share": 1.0})
    scores = {s.ubigeo: s.value for s in score_districts(districts, susceptibility_only)}
    assert scores["a"] > scores["c"] > scores["b"]


def test_a_missing_feature_is_dropped_and_the_weights_are_renormalised_and_reported():
    cfg = DistrictScoreConfig(weights={"mm_high_share": 1.0, "mean_slope_deg": 1.0})
    districts = [
        features("a", high=0.0, slope=0.0),
        DistrictFeatures("b", "B", mm_high_share=0.5, mean_slope_deg=None, exposed_population=0, tailings_upstream=0),
        features("c", high=1.0, slope=10.0),
    ]
    scores = {s.ubigeo: s for s in score_districts(districts, cfg)}
    assert scores["b"].missing == ("mean_slope_deg",)
    assert scores["b"].value == pytest.approx(0.5)  # only the susceptibility rank remains
    assert scores["a"].missing == ()


def test_a_district_with_no_usable_feature_gets_the_neutral_score_and_is_flagged():
    cfg = DistrictScoreConfig(weights={"mm_high_share": 1.0})
    districts = [features("a", high=0.2), DistrictFeatures("b", "B", None, None, None, None)]
    scores = {s.ubigeo: s for s in score_districts(districts, cfg)}
    assert scores["b"].value == 0.5
    assert scores["b"].missing == ("mm_high_share",)


def test_scoring_never_looks_at_labels_or_scenarios():
    # The inputs are static features only; there is no parameter through which an outcome could leak.
    assert set(DistrictFeatures.__dataclass_fields__) == {
        "ubigeo",
        "name",
        "mm_high_share",
        "mean_slope_deg",
        "exposed_population",
        "tailings_upstream",
    }


def test_invalid_weights_are_rejected():
    with pytest.raises(ValueError):
        DistrictScoreConfig(weights={"mm_high_share": -1.0})
    with pytest.raises(ValueError):
        DistrictScoreConfig(weights={"unknown_feature": 1.0})
    with pytest.raises(ValueError):
        DistrictScoreConfig(weights={})
