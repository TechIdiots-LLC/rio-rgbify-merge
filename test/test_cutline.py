"""Clipping a source to a shape, and fading it in at the edge of one."""

import json

import mercantile
import numpy as np
import pytest

from rio_rgbify.cutline import (
    MAX_FEATHER,
    feather_for,
    feather_pixels,
    load_cutline,
    metres_per_pixel,
)
from rio_rgbify.merger import TerrainRGBMerger

WORLD = mercantile.Tile(x=0, y=0, z=0)
BOX = mercantile.bounds(WORLD)
WEST_HALF = [BOX.west, BOX.south, (BOX.west + BOX.east) / 2, BOX.north]


def test_bounds_are_a_cutline_with_four_corners():
    # Built as a shape rather than handled separately, so one implementation
    # cannot disagree with the other about what an edge is.
    cutline = load_cutline(bounds=WEST_HALF)
    assert cutline.bounds == pytest.approx(tuple(WEST_HALF))
    assert cutline.geometries[0]["type"] == "Polygon"


def test_bounds_the_wrong_way_round_are_refused():
    with pytest.raises(ValueError):
        load_cutline(bounds=[10, 0, 0, 10])
    with pytest.raises(ValueError):
        load_cutline(bounds=[0, 10, 10, 0])


def test_nothing_asked_for_is_no_cutline():
    assert load_cutline() is None
    assert load_cutline(path=None, bounds=None) is None


@pytest.mark.parametrize(
    "document",
    [
        {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]},
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]],
            },
        },
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]],
                    },
                }
            ],
        },
    ],
)
def test_reads_a_shape_however_it_is_wrapped(tmp_path, document):
    path = tmp_path / f"{document['type']}.geojson"
    path.write_text(json.dumps(document), encoding="utf-8")

    cutline = load_cutline(path=str(path))

    assert cutline.bounds == (0.0, 0.0, 2.0, 2.0)


def test_a_cutline_is_read_once_and_kept(tmp_path):
    # The source config is pickled to a worker for every tile, so the path
    # travels and the geometry does not. Reading a national boundary per tile
    # would cost more than the tile.
    path = tmp_path / "shape.geojson"
    path.write_text(
        json.dumps(
            {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
        ),
        encoding="utf-8",
    )

    assert load_cutline(path=str(path)) is load_cutline(path=str(path))


def test_a_file_that_is_not_a_shape_is_refused(tmp_path):
    path = tmp_path / "nope.geojson"
    path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_cutline(path=str(path))


def test_covers_rejects_a_tile_the_shape_does_not_reach():
    cutline = load_cutline(bounds=[0, 0, 1, 1])
    assert cutline.covers(mercantile.Tile(x=0, y=0, z=0))
    # Somewhere in the Pacific, well away from a 1-degree box off Africa.
    assert not cutline.covers(mercantile.Tile(x=0, y=1, z=2))


def test_weights_are_a_switch_when_nothing_is_feathered():
    cutline = load_cutline(bounds=WEST_HALF)
    weights = cutline.weights(WORLD, 32, 0)
    row = weights[16]
    assert set(np.unique(row)) <= {0.0, 1.0}
    assert row[0] == 1.0 and row[-1] == 0.0


def test_weights_ramp_inward_over_the_width_they_were_given():
    cutline = load_cutline(bounds=WEST_HALF)
    weights = cutline.weights(WORLD, 64, 4)
    row = weights[32]

    edge = int(np.argmax(row == 0.0))
    assert row[edge - 1] == pytest.approx(0.25)
    assert row[edge - 2] == pytest.approx(0.5)
    assert row[edge - 3] == pytest.approx(0.75)
    assert row[edge - 4] == pytest.approx(1.0)


def test_the_ramp_never_reaches_past_the_shape():
    # A cutline says where a source's data is good. Spreading the ramp outward
    # would answer for ground the config just said this source does not cover.
    cutline = load_cutline(bounds=WEST_HALF)
    weights = cutline.weights(WORLD, 64, 8)
    row = weights[32]
    edge = int(np.argmax(row == 0.0))
    assert np.all(row[edge:] == 0.0), "it leaked past the boundary"


def test_a_tile_outside_the_shape_weighs_nothing():
    cutline = load_cutline(bounds=[0, 0, 1, 1])
    weights = cutline.weights(mercantile.Tile(x=0, y=1, z=2), 16, 4)
    assert weights.shape == (16, 16)
    assert not weights.any()


@pytest.mark.parametrize(
    "asked,expected",
    [(None, 0), (0, 0), (-4, 0), (8, 8), (8.4, 8), (1000, MAX_FEATHER)],
)
def test_feather_is_bounded(asked, expected):
    assert feather_for(asked) == expected


class TestBlending:
    """What the weight does once the sources are on the same grid."""

    under = np.array([[0.0, 0.0, 0.0, np.nan]])
    over = np.array([[1000.0, 1000.0, 1000.0, 1000.0]])

    def test_full_weight_takes_the_pixel_outright(self):
        weight = np.array([[1.0, 1.0, 1.0, 1.0]])
        out = TerrainRGBMerger._blend(self.under.copy(), self.over, weight)
        assert out[0, 0] == 1000.0

    def test_no_weight_leaves_what_was_underneath(self):
        weight = np.array([[0.0, 0.0, 0.0, 0.0]])
        out = TerrainRGBMerger._blend(self.under.copy(), self.over, weight)
        assert out[0, 0] == 0.0

    def test_part_weight_mixes_the_two(self):
        weight = np.array([[0.25, 0.5, 0.75, 1.0]])
        out = TerrainRGBMerger._blend(self.under.copy(), self.over, weight)
        assert out[0, 0] == pytest.approx(250.0)
        assert out[0, 1] == pytest.approx(500.0)
        assert out[0, 2] == pytest.approx(750.0)

    def test_it_stands_alone_where_there_is_nothing_underneath(self):
        # Otherwise a source that is the only cover for its ground would erode
        # itself by the width of its own feather.
        weight = np.array([[1.0, 1.0, 1.0, 0.25]])
        out = TerrainRGBMerger._blend(self.under.copy(), self.over, weight)
        assert out[0, 3] == 1000.0, "it faded into a hole"

    def test_a_source_with_no_tile_here_changes_nothing(self):
        weight = np.array([[1.0, 1.0, 1.0, 1.0]])
        missing = np.full((1, 4), np.nan)
        out = TerrainRGBMerger._blend(self.under.copy(), missing, weight)
        assert out[0, 0] == 0.0


def test_the_step_left_is_the_drop_divided_by_the_feather():
    # Which is what makes the setting predictable from what it has to hide.
    size = 64
    cutline = load_cutline(bounds=WEST_HALF)
    below = np.zeros((size, size), dtype=np.float32)
    above = np.full((size, size), 1000.0, dtype=np.float32)

    for feather in (0, 4, 8, 16):
        weights = cutline.weights(WORLD, size, feather)
        merged = TerrainRGBMerger._blend(below.copy(), above, weights)
        step = float(np.abs(np.diff(merged[size // 2])).max())
        expected = 1000.0 if feather == 0 else 1000.0 / feather
        assert step == pytest.approx(expected, rel=0.01), (
            f"feather {feather} should step {expected:.0f} m, got {step:.0f} m"
        )


# A fade written in metres of ground rather than in pixels.
#
# What a fade has to hide is two sources disagreeing about the height of the
# same ground, which is a fixed number of metres, and a hillshade reads the
# slope that disagreement makes rather than the height. A fade in pixels is a
# different distance at every zoom, so one number is a gentle ramp low down and
# a saturated band higher up.

LATITUDE = 55
SIZE = 512


def row_at(z):
    """The tile row that latitude falls in."""
    return mercantile.tile(0, LATITUDE, z).y


class Fading:
    """A source config, as far as the fade is concerned."""

    def __init__(self, feather=0, feather_metres=0):
        self.feather = feather
        self.feather_metres = feather_metres


def test_the_ground_under_a_pixel_is_what_every_mercator_scale_is_built_from():
    assert abs(metres_per_pixel(0, 0, 256) - 156543.034) < 0.01
    assert abs(metres_per_pixel(0, 0, 512) - 78271.517) < 0.01


def test_the_ground_under_a_pixel_shrinks_toward_the_poles():
    assert metres_per_pixel(4, 1, 512) < metres_per_pixel(4, 8, 512) / 2


@pytest.mark.parametrize("z", [12, 13, 14, 15, 16])
def test_a_fade_in_metres_covers_the_ground_it_asked_for(z):
    # The one property the field exists for. Rounding to whole pixels is what
    # the tolerance is: at z12 fifty metres is five pixels, so half a pixel is
    # a tenth of the fade, and at z16 it is a hundredth.
    y = row_at(z)
    per_pixel = metres_per_pixel(z, y, SIZE)
    pixels = feather_pixels(
        Fading(feather_metres=50), mercantile.Tile(x=0, y=y, z=z), SIZE
    )
    assert abs(pixels * per_pixel - 50) <= per_pixel / 2 + 1e-9


def test_the_same_fade_in_pixels_is_not_the_same_distance():
    # Eight pixels is 175 m of ground at z12 and 5 m at z16 -- far too generous
    # at one end and far too steep at the other.
    wide = 8 * metres_per_pixel(12, row_at(12), SIZE)
    narrow = 8 * metres_per_pixel(16, row_at(16), SIZE)
    assert wide / narrow > 15, (wide, narrow)


def test_a_fade_in_metres_leaves_one_slope_everywhere():
    # What a hillshade actually reads: a 7 m disagreement over 50 m of ground
    # is ordinary hillside rather than an edge, at every zoom.
    gradients = []
    for z in range(12, 17):
        y = row_at(z)
        pixels = feather_pixels(
            Fading(feather_metres=50), mercantile.Tile(x=0, y=y, z=z), SIZE
        )
        gradients.append(7 / pixels / metres_per_pixel(z, y, SIZE))
    assert max(gradients) < 0.2, gradients
    assert max(gradients) / min(gradients) < 1.3, gradients


def test_a_fade_in_metres_stops_at_a_quarter_of_the_tile():
    # Past that the ramp reaches full weight nowhere inside the tile, and the
    # source is being turned down rather than blended in.
    far = Fading(feather_metres=20000)
    tile = mercantile.Tile(x=0, y=row_at(16), z=16)
    assert feather_pixels(far, tile, 512) == 128
    assert feather_pixels(far, tile, 256) == MAX_FEATHER


def test_a_fade_smaller_than_a_pixel_is_no_fade():
    # 50 m at z8 is a sixth of a pixel, and the whole coastline is inside one
    # pixel there anyway.
    tile = mercantile.Tile(x=0, y=row_at(8), z=8)
    assert feather_pixels(Fading(feather_metres=50), tile, SIZE) == 0


def test_metres_win_over_pixels_rather_than_adding_to_them():
    tile = mercantile.Tile(x=0, y=row_at(15), z=15)
    both = feather_pixels(Fading(feather=4, feather_metres=50), tile, SIZE)
    assert both == feather_pixels(Fading(feather_metres=50), tile, SIZE)
    assert both != 4


def test_without_a_tile_there_is_no_scale_to_convert_against():
    # Rather than guessing a zoom: a fade a different width from the tiles
    # beside it draws the seam it was added to remove.
    assert feather_pixels(Fading(feather_metres=50)) == 0
    assert feather_pixels(Fading(feather=8)) == 8


@pytest.mark.parametrize("z", [13, 15])
def test_the_ramp_a_cutline_draws_is_that_many_pixels_wide(z):
    # End to end: a boundary down the middle of a tile, and the band the
    # weights climb over is what the conversion asked for.
    tile = mercantile.tile(0, LATITUDE, z)
    box = mercantile.bounds(tile)
    middle = (box.west + box.east) / 2
    # Grown well past the tile on the other three sides, so only this edge
    # crosses it and the middle row has one ramp to measure.
    cutline = load_cutline(
        bounds=[middle, box.south - 5, box.east + 5, box.north + 5]
    )
    pixels = feather_pixels(Fading(feather_metres=50), tile, SIZE)
    row = cutline.weights(tile, SIZE, pixels)[SIZE // 2]
    ramp = int(np.count_nonzero((row > 0) & (row < 1)))
    assert abs(ramp - pixels) <= 2, (ramp, pixels)
