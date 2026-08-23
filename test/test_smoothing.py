"""Smoothing an upscaled tile without eating its nodata or its edges."""

import mercantile
import numpy as np
import pytest
import rasterio
from rasterio.enums import Resampling

from rio_rgbify.merger import TerrainRGBMerger, TileData
from rio_rgbify.smoothing import blur_margin, crop_margin, grow_bounds, smooth


def test_smooth_leaves_nodata_where_it_found_it():
    # scipy's gaussian_filter makes NaN out of NaN in the kernel, so one masked
    # pixel took a disc the width of the kernel with it. mask_values defaults
    # to [0.0] and sea level is exactly 0 in most DEMs, so this ate the
    # coastline of every upscaled tile.
    data = np.full((9, 9), 100.0)
    data[4, 4] = np.nan

    out = smooth(data, 1.5)

    assert np.isnan(out).sum() == 1, "the blur spread the nodata"
    assert out[4, 3] == pytest.approx(100.0), "it pulled the neighbours down"


def test_smooth_still_blurs():
    data = np.zeros((9, 9))
    data[:, 4:] = 100.0
    out = smooth(data, 1.5)
    steps = np.abs(np.diff(out[4]))
    assert steps.max() < 100.0, "the step should be softened"


def test_smooth_is_the_input_when_nothing_is_asked_for():
    data = np.arange(16.0).reshape(4, 4)
    assert smooth(data, 0) is data


def test_blur_margin_covers_the_kernel_and_is_capped():
    # scipy truncates at four sigma, so the border has to be that wide to hold
    # everything the kernel reads.
    assert blur_margin(0, 512) == 0
    assert blur_margin(2, 512) == 8
    assert blur_margin(1000, 512) == 128, "it should not exceed a quarter"


def test_grow_and_crop_are_inverses():
    west, south, east, north = grow_bounds(0.0, 0.0, 8.0, 8.0, 2, 8)
    assert (west, south, east, north) == (-2.0, -2.0, 10.0, 10.0)

    padded = np.arange(144.0).reshape(1, 12, 12)
    assert crop_margin(padded, 8, 2).shape == (1, 8, 8)
    assert crop_margin(padded, 8, 2)[0, 0, 0] == padded[0, 2, 2]


class _Resampler:
    """Just enough of a merger to call the one method under test."""

    gaussian_blur_sigma = 1.5
    resampling = Resampling.bilinear
    _resample_if_needed = TerrainRGBMerger._resample_if_needed


def _parent_tile(z, x, y, size=64):
    """A parent tile with real relief on it, as TileData."""
    heights = np.zeros((1, size, size), dtype=np.float32)
    for row in range(size):
        for column in range(size):
            heights[0, row, column] = (
                column * 40.0 + 300 * np.sin(column / 5.0) + 200 * np.cos(row / 4.0)
            )
    bounds = mercantile.bounds(mercantile.Tile(x=x, y=y, z=z))
    meta = {
        "driver": "GTiff",
        "count": 1,
        "dtype": rasterio.float32,
        "width": size,
        "height": size,
        "crs": "EPSG:3857",
        "transform": rasterio.transform.from_bounds(
            bounds.west, bounds.south, bounds.east, bounds.north, size, size
        ),
    }
    return TileData(data=heights, meta=meta, source_zoom=z)


def test_two_neighbours_agree_about_the_edge_they_share():
    # A tile is filtered on its own, so without a border the two tiles either
    # side of a boundary compute it from different data and step apart. That
    # draws a faint grid at tile boundaries, worst on steep ground.
    size = 64
    parent_z, child_z = 8, 10
    px, py = 40, 60
    span = 2 ** (child_z - parent_z)

    def child(x):
        tile = mercantile.Tile(x=x, y=py * span, z=child_z)
        bounds = mercantile.bounds(tile)
        transform = rasterio.transform.from_bounds(
            bounds.west, bounds.south, bounds.east, bounds.north, size, size
        )
        return _Resampler()._resample_if_needed(
            _parent_tile(parent_z, px, py, size), tile, transform, size
        )

    left = child(px * span + 1)
    right = child(px * span + 2)
    step = float(np.abs(right[:, 0] - left[:, -1]).max())

    # The two columns are neighbours on the ground, so a smooth field puts them
    # within a pixel of slope of each other. The parent runs 40 m per pixel and
    # is upscaled four times, so a pixel here is a quarter of that.
    assert step < 40.0, f"the shared edge steps by {step:.1f} m"


def test_an_upscaled_tile_does_not_grow_its_nodata():
    # The whole reason the blur had to stop spreading NaN: this is a coastline,
    # and mask_values turns it into exactly this.
    size = 64
    parent = _parent_tile(8, 40, 60, size)
    parent.data[0, :, :8] = np.nan
    before = int(np.isnan(parent.data).sum()) / parent.data.size

    tile = mercantile.Tile(x=40 * 4 + 1, y=60 * 4, z=10)
    bounds = mercantile.bounds(tile)
    transform = rasterio.transform.from_bounds(
        bounds.west, bounds.south, bounds.east, bounds.north, size, size
    )
    out = _Resampler()._resample_if_needed(parent, tile, transform, size)

    # This child sits away from the masked strip, so nothing of it should
    # arrive here at all.
    assert not np.isnan(out).any(), (
        f"{np.isnan(out).mean():.0%} of the tile came back masked, "
        f"from a parent that was {before:.0%} masked and not underneath it"
    )
