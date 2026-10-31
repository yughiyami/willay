"""In-memory alert ledger (demo scope: lost on restart, which is acceptable for synthetic households)."""

from __future__ import annotations

import threading
from typing import Sequence

from willay.domain.dispatch import HouseholdAlert, OperatorCase


class InMemoryAlertLedger:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counters: dict[str, int] = {}
        self._alerts: dict[str, HouseholdAlert] = {}
        self._cases: list[OperatorCase] = []

    def new_id(self, prefix: str) -> str:
        with self._lock:
            self._counters[prefix] = self._counters.get(prefix, 0) + 1
            return f"{prefix}-{self._counters[prefix]:04d}"

    def add_alert(self, alert: HouseholdAlert) -> None:
        with self._lock:
            self._alerts[alert.id] = alert

    def get_alert(self, alert_id: str) -> HouseholdAlert | None:
        return self._alerts.get(alert_id)

    def list_alerts(self) -> Sequence[HouseholdAlert]:
        with self._lock:
            return list(self._alerts.values())

    def add_case(self, case: OperatorCase) -> None:
        with self._lock:
            self._cases.append(case)

    def list_cases(self) -> Sequence[OperatorCase]:
        with self._lock:
            return list(self._cases)

    def open_case_for(self, alert_id: str) -> OperatorCase | None:
        with self._lock:
            return next((c for c in self._cases if c.alert_id == alert_id and c.status == "open"), None)
