"""SYNTHETIC demo households (invented people and phone numbers; never real data).

The bundled fixture is used by default. To try a real phone call, copy it to a LOCAL file (outside git), put your
own number in it, add that number to WILLAY_CALL_ALLOWLIST and point WILLAY_DEMO_HOUSEHOLDS_FILE at the copy.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from willay.domain.dispatch import Household, ResponseKind

ENV_HOUSEHOLDS_FILE = "WILLAY_DEMO_HOUSEHOLDS_FILE"
DEFAULT_FILE = Path(__file__).resolve().parents[2] / "fixtures" / "households_synthetic.json"


def load_households(path: Path | str | None = None, environ: Mapping[str, str] | None = None) -> tuple[Household, ...]:
    env = os.environ if environ is None else environ
    chosen = path or env.get(ENV_HOUSEHOLDS_FILE) or DEFAULT_FILE
    data = json.loads(Path(chosen).read_text(encoding="utf-8"))
    return tuple(
        Household(
            id=h["id"],
            settlement_id=h["settlement_id"],
            label=h["label"],
            phone=h["phone"],
            language=h.get("language", "es"),
            demo_call_outcome=ResponseKind(h.get("demo_call_outcome", "NO_ANSWER")),
        )
        for h in data["households"]
    )
