from __future__ import annotations

from typing import Protocol


class SegmentationPort(Protocol):
    """Water segmentation on camera frames (e.g. SAM)."""

    def measure_water_rise_cm(self, reference_frame: bytes, current_frame: bytes) -> float | None:
        """Return the measured rise in centimeters, or None if it cannot be measured."""
        ...
