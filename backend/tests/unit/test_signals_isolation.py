"""Structural guard: the code that decides tiers or sends alerts cannot even see public signals."""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "willay"
SIGNAL_MODULES = ("willay.domain.signals", "willay.application.public_signals", "willay.ports.signals", "willay.adapters.tavily_search", "willay.adapters.signals_fixture")

# Tier rules, dispatch, proposals/approval and the telephony/messaging adapters: none may import the signals layer.
MUST_NOT_IMPORT_SIGNALS = [
    "domain/tiers.py",
    "domain/dispatch.py",
    "application/evaluate_alert_tier.py",
    "application/dispatch_alerts.py",
    "application/orchestration/runtime.py",
    "api/alert_routes.py",
    "adapters/asterisk_ari.py",
    "adapters/twilio_calls.py",
    "adapters/simulated_call.py",
    "adapters/web_whatsapp.py",
    "adapters/alert_ledger.py",
]


def imports_of(relative: str) -> set[str]:
    tree = ast.parse((PACKAGE / relative).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_tier_rules_dispatch_proposals_and_senders_do_not_import_the_signals_layer():
    offenders = [f"{path} imports {module}" for path in MUST_NOT_IMPORT_SIGNALS for module in imports_of(path) if module in SIGNAL_MODULES]
    assert offenders == []


def test_the_only_domain_use_of_signals_is_the_corroborate_function_that_cannot_change_a_tier():
    source = (PACKAGE / "domain" / "signals.py").read_text(encoding="utf-8")
    assert "AlertTier.EVACUATION" not in source and "AlertTier.PENDING_CONFIRMATION" not in source
    tree = ast.parse(source)
    corroborate = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "corroborate")
    # the result is built from the input evaluation with only `reasons` replaced
    replaced = [kw.arg for node in ast.walk(corroborate) if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "replace" for kw in node.keywords]
    assert replaced == ["reasons"]
