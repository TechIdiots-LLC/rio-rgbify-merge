import os
import json
import click
from click.testing import CliRunner

import numpy as np
import pytest

import rasterio as rio
from rio_rgbify.scripts.cli import main_group as cli, rgbify, merge
from rio_rgbify.database import MBTilesDatabase

from raster_tester.compare import affaux, upsample_array


in_elev_src = os.path.join(os.path.dirname(__file__), "fixtures", "elev.tif")
expected_src = os.path.join(os.path.dirname(__file__), "expected", "elev-rgb.tif")


def flex_compare(r1, r2, thresh=10):
    upsample = 4
    r1 = r1[::upsample]
    r2 = r2[::upsample]
    toAff, frAff = affaux(upsample)
    r1 = upsample_array(r1, upsample, frAff, toAff)
    r2 = upsample_array(r2, upsample, frAff, toAff)
    tdiff = np.abs(r1.astype(np.float64) - r2.astype(np.float64))

    click.echo(
        "{0} values exceed the threshold difference with a max variance of {1}".format(
            np.sum(tdiff > thresh), tdiff.max()
        ),
        err=True,
    )

    return not np.any(tdiff > thresh)


@pytest.mark.skip(reason="Single-file GeoTIFF output mode removed in rewrite; rgbify now always writes MBTiles and requires --min-z/--max-z")
def test_cli_good_elev():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            rgbify,
            [in_elev_src, "rgb.tif", "--interval", 0.001, "--base-val", -100, "-j", 1],
        )

        assert result.exit_code == 0

        with rio.open("rgb.tif") as created:
            with rio.open(expected_src) as expected:
                carr = created.read()
                earr = expected.read()
                for a, b in zip(carr, earr):
                    assert flex_compare(a, b)


def test_cli_fail_elev():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            rgbify,
            [
                in_elev_src,
                "rgb.tif",
                "--interval",
                0.00000001,
                "--base-val",
                -100,
                "-j",
                1,
            ],
        )
        assert result.exit_code == 1
        assert result.exception


def test_mbtiler_webp():
    runner = CliRunner()
    with runner.isolated_filesystem():
        out_mbtiles_finer = "output-0-dot-1.mbtiles"
        result_finer = runner.invoke(
            rgbify,
            [
                in_elev_src,
                out_mbtiles_finer,
                "--interval",
                0.1,
                "--min-z",
                10,
                "--max-z",
                11,
                "--format",
                "webp",
                "-j",
                1,
            ],
        )
        assert result_finer.exit_code == 0

        out_mbtiles_coarser = "output-1.mbtiles"
        result_coarser = runner.invoke(
            rgbify,
            [
                in_elev_src,
                out_mbtiles_coarser,
                "--min-z",
                10,
                "--max-z",
                11,
                "--format",
                "webp",
                "-j",
                1,
            ],
        )
        assert result_coarser.exit_code == 0

        assert os.path.getsize(out_mbtiles_finer) > os.path.getsize(out_mbtiles_coarser)


def test_mbtiler_png():
    runner = CliRunner()
    with runner.isolated_filesystem():
        out_mbtiles_finer = "output-0-dot-1.mbtiles"
        result_finer = runner.invoke(
            rgbify,
            [
                in_elev_src,
                out_mbtiles_finer,
                "--interval",
                0.1,
                "--min-z",
                10,
                "--max-z",
                11,
                "--format",
                "png",
            ],
        )
        assert result_finer.exit_code == 0

        out_mbtiles_coarser = "output-1.mbtiles"
        result_coarser = runner.invoke(
            rgbify,
            [
                in_elev_src,
                out_mbtiles_coarser,
                "--min-z",
                10,
                "--max-z",
                11,
                "--format",
                "png",
                "-j",
                1,
            ],
        )
        assert result_coarser.exit_code == 0

        assert os.path.getsize(out_mbtiles_finer) > os.path.getsize(out_mbtiles_coarser)


def test_mbtiler_png_bounding_tile():
    runner = CliRunner()
    with runner.isolated_filesystem():
        out_mbtiles_not_limited = "output-not-limited.mbtiles"
        result_not_limited = runner.invoke(
            rgbify,
            [
                in_elev_src,
                out_mbtiles_not_limited,
                "--min-z",
                12,
                "--max-z",
                12,
                "--format",
                "png",
            ],
        )
        assert result_not_limited.exit_code == 0

        out_mbtiles_limited = "output-limited.mbtiles"
        result_limited = runner.invoke(
            rgbify,
            [
                in_elev_src,
                out_mbtiles_limited,
                "--min-z",
                12,
                "--max-z",
                12,
                "--format",
                "png",
                "--bounding-tile",
                "[654, 1582, 12]",
            ],
        )
        assert result_limited.exit_code == 0

        assert os.path.getsize(out_mbtiles_not_limited) > os.path.getsize(
            out_mbtiles_limited
        )

        result_badtile = runner.invoke(
            rgbify,
            [
                in_elev_src,
                out_mbtiles_limited,
                "--min-z",
                12,
                "--max-z",
                12,
                "--format",
                "png",
                "--bounding-tile",
                "654-1582-12",
            ],
        )
        assert result_badtile.exit_code == 1
        assert "is not valid" in str(result_badtile.exception)


def test_mbtiler_webp_badzoom():
    runner = CliRunner()
    with runner.isolated_filesystem():
        out_mbtiles = "output.mbtiles"
        result = runner.invoke(
            rgbify,
            [
                in_elev_src,
                out_mbtiles,
                "--min-z",
                10,
                "--max-z",
                9,
                "--format",
                "webp",
                "-j",
                1,
            ],
        )
        assert result.exit_code == 1
        assert result.exception


def test_mbtiler_webp_badboundingtile():
    runner = CliRunner()
    with runner.isolated_filesystem():
        out_mbtiles = "output.mbtiles"
        result = runner.invoke(
            rgbify,
            [
                in_elev_src,
                out_mbtiles,
                "--min-z",
                10,
                "--max-z",
                9,
                "--format",
                "webp",
                "--bounding-tile",
                "654, 1582, 12",
            ],
        )
        assert result.exit_code == 1
        assert result.exception


def test_mbtiler_webp_badboundingtile_values():
    runner = CliRunner()
    with runner.isolated_filesystem():
        out_mbtiles = "output.mbtiles"
        result = runner.invoke(
            rgbify,
            [
                in_elev_src,
                out_mbtiles,
                "--min-z",
                10,
                "--max-z",
                9,
                "--format",
                "webp",
                "--bounding-tile",
                "[654, 1582]",
            ],
        )
        assert result.exit_code == 1
        assert result.exception


def test_bad_input_format():
    runner = CliRunner()
    with runner.isolated_filesystem():
        out_mbtiles = "output.lol"
        result = runner.invoke(
            rgbify,
            [
                in_elev_src,
                out_mbtiles,
                "--min-z",
                10,
                "--max-z",
                9,
                "--format",
                "webp",
                "-j",
                1,
            ],
        )
        assert result.exit_code == 1
        assert result.exception

def test_merge_command():
    runner = CliRunner()
    with runner.isolated_filesystem():
        # Create a sample config file
        config_data = {
            "sources": [
                {"path": "test1.mbtiles", "encoding": "mapbox", "height_adjustment": 5},
                {"path": "test2.mbtiles", "encoding": "terrarium", "height_adjustment": -10}
                ],
            "output_path": "merged.mbtiles",
            "output_format": "png",
            "output_encoding": "mapbox",
            "resampling": "bilinear"
        }
        
        with open("config.json", "w") as f:
            json.dump(config_data, f)

        # Create valid (empty) MBTiles databases so merger can query them
        with MBTilesDatabase("test1.mbtiles") as _:
            pass
        with MBTilesDatabase("test2.mbtiles") as _:
            pass

        result = runner.invoke(
            cli,
            [
                "merge",
                "--config",
                "config.json",
                "-j",
                 "1"
            ]
        )
        assert result.exit_code == 0
        assert os.path.exists("merged.mbtiles")


def test_mbtiler_resampling_cli():
  runner = CliRunner()
  with runner.isolated_filesystem():
    out_mbtiles = "output.mbtiles"
    result = runner.invoke(
        rgbify,
        [
            in_elev_src,
            out_mbtiles,
            "--min-z",
            10,
            "--max-z",
            11,
            "--format",
            "png",
            "--resampling",
            "nearest",
            "-j",
            1,
        ],
    )
    assert result.exit_code == 0

    result = runner.invoke(
        rgbify,
        [
            in_elev_src,
            out_mbtiles,
            "--min-z",
            10,
            "--max-z",
            11,
            "--format",
            "png",
            "--resampling",
            "bilinear",
            "-j",
            1,
        ],
    )
    assert result.exit_code == 0

    result = runner.invoke(
        rgbify,
        [
            in_elev_src,
            out_mbtiles,
            "--min-z",
            10,
            "--max-z",
            11,
            "--format",
            "png",
            "--resampling",
            "cubic",
            "-j",
            1,
        ],
    )
    assert result.exit_code == 0
    result = runner.invoke(
        rgbify,
        [
            in_elev_src,
            out_mbtiles,
            "--min-z",
            10,
            "--max-z",
            11,
            "--format",
            "png",
            "--resampling",
            "cubic_spline",
             "-j",
            1,
        ],
    )
    assert result.exit_code == 0
    result = runner.invoke(
        rgbify,
        [
            in_elev_src,
            out_mbtiles,
            "--min-z",
            10,
            "--max-z",
            11,
            "--format",
            "png",
            "--resampling",
            "lanczos",
            "-j",
             1,
        ],
    )
    assert result.exit_code == 0
    result = runner.invoke(
        rgbify,
        [
            in_elev_src,
            out_mbtiles,
            "--min-z",
            10,
            "--max-z",
            11,
            "--format",
            "png",
            "--resampling",
            "average",
            "-j",
            1,
        ],
    )
    assert result.exit_code == 0
    result = runner.invoke(
        rgbify,
        [
            in_elev_src,
            out_mbtiles,
            "--min-z",
            10,
            "--max-z",
            11,
            "--format",
            "png",
             "--resampling",
            "mode",
            "-j",
            1,
        ],
    )
    assert result.exit_code == 0
    result = runner.invoke(
        rgbify,
        [
            in_elev_src,
            out_mbtiles,
            "--min-z",
            10,
            "--max-z",
            11,
            "--format",
            "png",
             "--resampling",
            "gauss",
            "-j",
            1,
        ],
    )
    assert result.exit_code == 0


def test_mbtiler_baseval_cli():
  runner = CliRunner()
  with runner.isolated_filesystem():
    out_mbtiles = "output.mbtiles"
    result = runner.invoke(
        rgbify,
        [
            in_elev_src,
            out_mbtiles,
            "--min-z",
            10,
            "--max-z",
            11,
            "--format",
            "png",
            "--encoding",
            "mapbox",
            "--base-val",
            "-500",
            "-j",
            1,
        ],
    )
    assert result.exit_code == 0


class _Capture:
    """Stands in for a merger, so the sources the config built can be read."""

    built = []

    def __init__(self, sources, **kwargs):
        _Capture.built = sources

    def process_all(self, **kwargs):
        pass


def test_merge_config_carries_the_masking_and_fading_fields(monkeypatch):
    # Every config key is read with .get(), so one spelled differently here
    # than in the file is a setting that silently does nothing -- and the
    # command logs what it would otherwise raise, so nothing fails loudly
    # either.
    monkeypatch.setattr("rio_rgbify.scripts.cli.TerrainRGBMerger", _Capture)
    runner = CliRunner()
    with runner.isolated_filesystem():
        config_data = {
            "sources": [
                {
                    "path": "test1.mbtiles",
                    "mask_range": [-1, 0],
                    "feather_metres": 50,
                    "bounds": [-10, 50, 10, 60],
                },
                {
                    "path": "test2.mbtiles",
                    "mask_range": [[-1, 0], [100, 200]],
                    "feather_meters": 25,
                    "bounds": [-10, 50, 10, 60],
                },
            ],
            "output_path": "merged.mbtiles",
        }
        with open("config.json", "w") as f:
            json.dump(config_data, f)
        for name in ("test1.mbtiles", "test2.mbtiles"):
            with MBTilesDatabase(name) as _:
                pass

        result = runner.invoke(
            cli, ["merge", "--config", "config.json", "-j", "1"]
        )
        assert result.exit_code == 0

    first, second = _Capture.built
    assert first.mask_range == [-1, 0]
    assert first.feather_metres == 50
    assert second.mask_range == [[-1, 0], [100, 200]]
    assert second.feather_metres == 25, "the other spelling is read as well"


def test_a_raster_source_is_built_at_all(monkeypatch):
    # base_val and interval are the output encoding's and belong to the
    # merger. Passed to the source they raised a TypeError the command logged
    # and swallowed, so a raster source never built and the run merged
    # nothing at all.
    monkeypatch.setattr("rio_rgbify.scripts.cli.RasterRGBMerger", _Capture)
    _Capture.built = []
    runner = CliRunner()
    with runner.isolated_filesystem():
        config_data = {
            "output_type": "raster",
            "sources": [
                {
                    "path": in_elev_src,
                    "source_type": "raster",
                    "mask_range": [-1, 0],
                }
            ],
            "output_path": "merged.tif",
        }
        with open("config.json", "w") as f:
            json.dump(config_data, f)

        result = runner.invoke(
            cli, ["merge", "--config", "config.json", "-j", "1"]
        )
        assert result.exit_code == 0

    assert len(_Capture.built) == 1, "the source never built"
    assert _Capture.built[0].mask_range == [-1, 0]
