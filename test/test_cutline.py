"""Clipping a source to a shape, and fading it in at the edge of one."""

import json

import mercantile
import numpy as np
import pytest

from rio_rgbify.cutline import MAX_FEATHER, feather_for, load_cutline
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
