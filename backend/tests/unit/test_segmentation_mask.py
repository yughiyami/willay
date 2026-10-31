"""The stdlib-only bit-row mask used by the water-line geometry."""

import pytest

from willay.domain.segmentation import Mask


def test_rectangle_counts_and_bbox_are_half_open():
    m = Mask.rectangle(10, 8, 2, 1, 5, 4)
    assert m.count() == 9
    assert m.bbox() == (2, 1, 5, 4)
    assert (m.width, m.height) == (10, 8)


def test_empty_mask_has_no_bbox():
    m = Mask.empty(5, 5)
    assert m.count() == 0 and m.bbox() is None


def test_from_ascii_round_trips():
    m = Mask.from_ascii(
        """
        ..#.
        .##.
        ....
        """
    )
    assert (m.width, m.height) == (4, 3)
    assert m.count() == 3
    assert m.bbox() == (1, 0, 3, 2)
    assert m.row_count(1, 0, 4) == 2


def test_row_count_restricted_to_columns():
    m = Mask.rectangle(20, 5, 3, 0, 15, 5)
    assert m.row_count(2, 0, 20) == 12
    assert m.row_count(2, 10, 20) == 5
    assert m.row_count(2, 0, 3) == 0
    assert m.row_count(99, 0, 20) == 0  # out of range rows are empty


def test_union_and_union_requires_same_size():
    a = Mask.rectangle(6, 6, 0, 0, 2, 2)
    b = Mask.rectangle(6, 6, 1, 1, 4, 4)
    u = a.union(b)
    assert u.count() == 4 + 9 - 1
    with pytest.raises(ValueError):
        a.union(Mask.empty(5, 5))


def test_from_raster_places_a_box_local_mask_in_the_frame():
    raster = bytes([1, 0, 1, 1, 1, 0])  # 2 rows x 3 cols
    m = Mask.from_raster(raster, 3, 2, left=4, top=5, frame_width=10, frame_height=8)
    assert m.count() == 4
    assert m.bbox() == (4, 5, 7, 7)
    assert m.row_count(5, 0, 10) == 2 and m.row_count(6, 0, 10) == 2
    assert m.row_count(5, 4, 5) == 1  # x=4 set on the first row


def test_from_raster_clips_to_the_frame():
    raster = bytes([1] * 6)
    m = Mask.from_raster(raster, 3, 2, left=8, top=7, frame_width=10, frame_height=8)
    assert m.count() == 2  # only (8,7) and (9,7) are inside


def test_from_raster_validates_length():
    with pytest.raises(ValueError):
        Mask.from_raster(bytes([1, 0]), 3, 2, left=0, top=0, frame_width=5, frame_height=5)
