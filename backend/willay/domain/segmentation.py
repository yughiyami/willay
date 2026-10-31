"""Segmentation value objects: a stdlib-only bit-row mask and the per-frame tracked objects.

A `Mask` stores one Python int per image row (bit x set means pixel x belongs to the mask), which
keeps the domain free of numpy while staying fast for row/column queries. Boxes and bounding boxes
are half-open: (x1, y1, x2, y2) with x2 and y2 exclusive.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Mask:
    width: int
    height: int
    rows: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("mask dimensions must be positive")
        if len(self.rows) != self.height:
            raise ValueError("a mask needs exactly one row integer per image row")

    # --- constructors ---------------------------------------------------------------------

    @classmethod
    def empty(cls, width: int, height: int) -> "Mask":
        return cls(width, height, (0,) * height)

    @classmethod
    def rectangle(cls, width: int, height: int, x1: int, y1: int, x2: int, y2: int) -> "Mask":
        x1, x2 = max(0, x1), min(width, x2)
        y1, y2 = max(0, y1), min(height, y2)
        row = ((1 << (x2 - x1)) - 1) << x1 if x2 > x1 else 0
        return cls(width, height, tuple(row if y1 <= y < y2 else 0 for y in range(height)))

    @classmethod
    def from_ascii(cls, art: str) -> "Mask":
        lines = [line.strip() for line in art.strip().splitlines() if line.strip()]
        width = len(lines[0])
        if any(len(line) != width for line in lines):
            raise ValueError("ragged ascii mask")
        rows = tuple(sum(1 << x for x, ch in enumerate(line) if ch == "#") for line in lines)
        return cls(width, len(lines), rows)

    @classmethod
    def from_raster(
        cls, raster: bytes, raster_width: int, raster_height: int, *, left: int, top: int,
        frame_width: int, frame_height: int,
    ) -> "Mask":
        """Place a box-local row-major 0/1 raster at (left, top) of a frame, clipping to the frame."""
        if len(raster) != raster_width * raster_height:
            raise ValueError("raster length does not match its dimensions")
        rows = [0] * frame_height
        for ry in range(raster_height):
            y = top + ry
            if not 0 <= y < frame_height:
                continue
            chunk = raster[ry * raster_width : (ry + 1) * raster_width]
            bits = int("".join("1" if b else "0" for b in reversed(chunk)), 2) if chunk else 0
            shifted = bits << left if left >= 0 else bits >> -left
            rows[y] = shifted & ((1 << frame_width) - 1)
        return cls(frame_width, frame_height, tuple(rows))

    # --- queries -------------------------------------------------------------------------

    def count(self) -> int:
        return sum(row.bit_count() for row in self.rows)

    def bbox(self) -> tuple[int, int, int, int] | None:
        ys = [y for y, row in enumerate(self.rows) if row]
        if not ys:
            return None
        union = 0
        for y in ys:
            union |= self.rows[y]
        x1 = (union & -union).bit_length() - 1
        x2 = union.bit_length()
        return (x1, ys[0], x2, ys[-1] + 1)

    def row_count(self, y: int, x1: int, x2: int) -> int:
        """Number of set pixels of row y within columns [x1, x2)."""
        if not 0 <= y < self.height:
            return 0
        x1, x2 = max(0, x1), min(self.width, x2)
        if x2 <= x1:
            return 0
        return ((self.rows[y] >> x1) & ((1 << (x2 - x1)) - 1)).bit_count()

    def union(self, other: "Mask") -> "Mask":
        if (self.width, self.height) != (other.width, other.height):
            raise ValueError("masks must have the same size")
        return Mask(self.width, self.height, tuple(a | b for a, b in zip(self.rows, other.rows)))


@dataclass(frozen=True)
class TrackedObject:
    object_id: str
    box: tuple[int, int, int, int]
    mask: Mask
    confidence: float | None = None


@dataclass(frozen=True)
class FrameSegmentation:
    frame_index: int
    objects: tuple[TrackedObject, ...]


@dataclass(frozen=True)
class VideoSegmentation:
    """Result of one concept-prompt request over a clip (frames with no match are absent)."""

    prompt: str
    width: int
    height: int
    frames: tuple[FrameSegmentation, ...]
    billed_frames: int | None = None
