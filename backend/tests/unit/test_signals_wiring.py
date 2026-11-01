"""Environment wiring of the public signals layer: off by default, and a key alone never turns it on."""

from willay.adapters.tavily_search import TavilySearchAdapter
from willay.adapters.tikhub_search import TikHubPublicSearchAdapter
from willay.api.dependencies import attach_signals_from_env, synthetic_dependencies
from willay.application.public_signals import PublicSignalsHub

KEY_VALUE = "tvly-not-a-real-key"


class FakeExtractor:
    def extract_json(self, **kwargs):
        return {"events": []}


def deps(with_extractor: bool):
    d = synthetic_dependencies()
    d.extractor = FakeExtractor() if with_extractor else None
    return d


def test_signals_are_off_by_default_and_no_search_adapter_exists():
    d = attach_signals_from_env(deps(True), {})
    assert isinstance(d.signals, PublicSignalsHub)
    assert d.signals.mode == "off" and d.signal_search is None and d.signals.enabled is False


def test_a_key_alone_does_not_enable_anything():
    d = attach_signals_from_env(deps(True), {"TAVILY_API_KEY": KEY_VALUE})
    assert d.signals.mode == "off" and d.signal_search is None
    d = attach_signals_from_env(deps(True), {"TAVILY_API_KEY": KEY_VALUE, "WILLAY_SIGNALS_ENABLED": "0"})
    assert d.signals.mode == "off"


def test_enabled_without_the_key_stays_off_and_names_the_variable_but_never_a_value():
    d = attach_signals_from_env(deps(True), {"WILLAY_SIGNALS_ENABLED": "1"})
    assert d.signals.mode == "off" and d.signal_search is None
    assert "TAVILY_API_KEY" in d.signals.note


def test_enabled_without_the_extraction_model_stays_off_and_says_what_is_missing():
    d = attach_signals_from_env(deps(False), {"WILLAY_SIGNALS_ENABLED": "1", "TAVILY_API_KEY": KEY_VALUE})
    assert d.signals.mode == "off" and d.signal_search is None
    assert "WILLAY_META_ENABLED" in d.signals.note and KEY_VALUE not in d.signals.note


def test_enabled_with_key_and_extractor_goes_live():
    d = attach_signals_from_env(deps(True), {"WILLAY_SIGNALS_ENABLED": "1", "TAVILY_API_KEY": KEY_VALUE})
    assert d.signals.mode == "live" and d.signals.enabled is True
    assert isinstance(d.signal_search, TavilySearchAdapter)
    assert KEY_VALUE not in repr(d.signals.__dict__) and KEY_VALUE not in d.signals.note


def test_tuning_variables_are_read_and_bad_values_fall_back():
    d = attach_signals_from_env(
        deps(True), {"WILLAY_SIGNALS_ENABLED": "1", "TAVILY_API_KEY": KEY_VALUE, "WILLAY_SIGNALS_MAX_QUERIES": "3", "WILLAY_SIGNALS_COOLDOWN_S": "30"}
    )
    assert d.signals_max_queries == 3 and d.signals_cooldown_s == 30.0
    d = attach_signals_from_env(
        deps(True), {"WILLAY_SIGNALS_ENABLED": "1", "TAVILY_API_KEY": KEY_VALUE, "WILLAY_SIGNALS_MAX_QUERIES": "many", "WILLAY_SIGNALS_COOLDOWN_S": "-5"}
    )
    assert d.signals_max_queries == 8 and d.signals_cooldown_s == 300.0


def test_the_default_hub_can_show_the_simulated_set():
    hub = synthetic_dependencies().signals
    assert hub.simulation_available
    hub.set_simulated(True)
    assert hub.mode == "simulated" and hub.snapshot()["signals"]


TIKHUB_VALUE = "tikhub-not-a-real-key"
LIVE = {"WILLAY_SIGNALS_ENABLED": "1"}


def test_tikhub_is_off_by_default_even_with_its_key_and_signals_enabled():
    d = attach_signals_from_env(deps(True), {**LIVE, "TAVILY_API_KEY": KEY_VALUE, "TIKHUB_API_KEY": TIKHUB_VALUE})
    assert isinstance(d.signal_search, TavilySearchAdapter)
    d = attach_signals_from_env(deps(True), {**LIVE, "TAVILY_API_KEY": KEY_VALUE, "TIKHUB_API_KEY": TIKHUB_VALUE, "WILLAY_TIKHUB_ENABLED": "0"})
    assert isinstance(d.signal_search, TavilySearchAdapter)


def test_the_tikhub_flag_alone_does_nothing_without_the_signals_flag():
    d = attach_signals_from_env(deps(True), {"WILLAY_TIKHUB_ENABLED": "1", "TIKHUB_API_KEY": TIKHUB_VALUE})
    assert d.signals.mode == "off" and d.signal_search is None


def test_tikhub_enabled_without_its_key_names_the_variable_and_keeps_tavily():
    d = attach_signals_from_env(deps(True), {**LIVE, "TAVILY_API_KEY": KEY_VALUE, "WILLAY_TIKHUB_ENABLED": "1"})
    assert isinstance(d.signal_search, TavilySearchAdapter)
    assert "TIKHUB_API_KEY" in d.signals.note and TIKHUB_VALUE not in d.signals.note


def test_tikhub_alone_goes_live_without_a_tavily_key():
    d = attach_signals_from_env(deps(True), {**LIVE, "WILLAY_TIKHUB_ENABLED": "1", "TIKHUB_API_KEY": TIKHUB_VALUE})
    assert d.signals.mode == "live" and isinstance(d.signal_search, TikHubPublicSearchAdapter)
    assert TIKHUB_VALUE not in repr(d.signals.__dict__) and TIKHUB_VALUE not in d.signals.note
    assert "TikHub" in d.signals.note


def test_both_sources_are_used_together():
    d = attach_signals_from_env(deps(True), {**LIVE, "TAVILY_API_KEY": KEY_VALUE, "WILLAY_TIKHUB_ENABLED": "1", "TIKHUB_API_KEY": TIKHUB_VALUE})
    kinds = {type(source) for source in d.signal_search}
    assert kinds == {TavilySearchAdapter, TikHubPublicSearchAdapter}


def test_tikhub_enabled_without_the_extraction_model_stays_off():
    d = attach_signals_from_env(deps(False), {**LIVE, "WILLAY_TIKHUB_ENABLED": "1", "TIKHUB_API_KEY": TIKHUB_VALUE})
    assert d.signals.mode == "off" and d.signal_search is None and "WILLAY_META_ENABLED" in d.signals.note
