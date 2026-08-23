"""
Masking a band of heights, rather than a list of exact values.

Nodata is rarely one number by the time it reaches a merge. A source resampled
on its way to being built does not hold what it was authored with, so a sea
authored as 0 arrives spread over -0.9 m to 0 -- and masking the two ends of
that leaves everything between standing proud of whatever is underneath, which
a hillshade picks out as a scatter of bright pixels.
"""

import numpy as np
import pytest

from rio_rgbify.image import ImageEncoder

ROW = np.array([[-2.0, -1.0, -0.5, 0.0, 0.5, 1.0]])


def test_a_band_masks_everything_inside_it():
    masked = ImageEncoder._mask_range(ROW, [-1, 0])
    assert np.isnan(masked[0, 1:4]).all(), "the band itself"
    assert not np.isnan(masked[0, 0]), "below the band"
    assert not np.isnan(masked[0, 4]), "above the band"


def test_both_ends_belong_to_the_band():
    # A band that excluded the number written on it would leave the row of
    # pixels at its own edge behind, which is the artefact this removes.
    masked = ImageEncoder._mask_range(ROW, [-1, 0])
    assert np.isnan(masked[0, 1]), "the low end"
    assert np.isnan(masked[0, 3]), "the high end"


def test_a_height_that_did_not_survive_being_decoded():
    # A float32 archive holds -0.2 as -0.20000000298, which is outside a band
    # written [-0.2, 0] if the two are compared as they stand.
    heights = np.array([[-0.2]], dtype=np.float32).astype(np.float64)
    assert heights[0, 0] != -0.2, "the premise: it is not the number written"
    assert np.isnan(ImageEncoder._mask_range(heights, [-0.2, 0])).all()


def test_several_bands():
    masked = ImageEncoder._mask_range(ROW, [[-2, -2], [0.5, 1]])
    assert np.isnan(masked[0, 0])
    assert np.isnan(masked[0, 4])
    assert np.isnan(masked[0, 5])
    assert not np.isnan(masked[0, 2]), "between the two bands"


@pytest.mark.parametrize("band", [[-1, 0], [0, -1]])
def test_the_ends_may_be_written_either_way_round(band):
    # A dash is awkward with negative numbers, so the pair is two fields and
    # nobody should have to remember which is which.
    masked = ImageEncoder._mask_range(ROW, band)
    assert np.isnan(masked[0, 1:4]).all()


@pytest.mark.parametrize("asked", [None, [], [[]], [5]])
def test_nothing_asked_for_changes_nothing(asked):
    masked = ImageEncoder._mask_range(ROW, asked)
    assert not np.isnan(masked).any()


def test_it_leaves_the_holes_it_finds():
    heights = np.array([[np.nan, 5.0, -0.5]])
    masked = ImageEncoder._mask_range(heights, [-1, 0])
    assert np.isnan(masked[0, 0]), "already nothing"
    assert masked[0, 1] == 5.0
    assert np.isnan(masked[0, 2])


def test_it_reads_as_a_band_and_not_as_two_values():
    # The distinction the field exists for: mask_values would have masked the
    # two ends and left the middle.
    band = ImageEncoder._mask_range(ROW, [-1, 0])
    values = ImageEncoder._mask_elevation(ROW, [-1, 0])
    assert np.isnan(band[0, 2]), "the band takes what is between"
    assert not np.isnan(values[0, 2]), "the list does not"
