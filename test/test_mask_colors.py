"""Masking by the colour a source stored, rather than by the height it decodes to."""

import pickle
from pathlib import Path

import numpy as np
import pytest

from rio_rgbify.image import ImageEncoder
from rio_rgbify.merger import EncodingType, MBTilesSource


def _rgb(*pixels):
    """A (3, 1, n) array from a list of (r, g, b)."""
    channels = np.array(pixels, dtype=np.int32).T
    return channels.reshape(3, 1, len(pixels))


@pytest.mark.parametrize(
    "value,expected",
    [
        ("#000000", (0, 0, 0)),
        ("ffffff", (255, 255, 255)),
        ("#1A2b3C", (26, 43, 60)),
        ([12, 34, 56], (12, 34, 56)),
        ((0, 128, 255), (0, 128, 255)),
    ],
)
def test_parse_color_takes_what_somebody_would_paste(value, expected):
    assert ImageEncoder._parse_color(value) == expected


@pytest.mark.parametrize("value", ["nope", "#12345", "#gggggg", [1, 2], 7, None])
def test_parse_color_refuses_what_is_not_one(value):
    # A mask that silently matches nothing is the failure this is most prone
    # to, so it is refused rather than dropped.
    with pytest.raises(ValueError):
        ImageEncoder._parse_color(value)


def test_masks_the_pixel_the_source_marked():
    elevation = np.array([[5.0, 100.0, 7.0]])
    rgb = _rgb((0, 0, 0), (10, 20, 30), (255, 255, 255))

    out = ImageEncoder._mask_colors(elevation, rgb, ["#000000", [255, 255, 255]])

    assert np.isnan(out[0, 0]), "the black nodata pixel survived"
    assert np.isnan(out[0, 2]), "the white nodata pixel survived"
    assert out[0, 1] == 100.0, "it masked real ground"


def test_leaves_everything_alone_when_nothing_is_named():
    elevation = np.array([[5.0, 100.0]])
    rgb = _rgb((0, 0, 0), (1, 1, 1))
    assert np.array_equal(ImageEncoder._mask_colors(elevation, rgb, []), elevation)
    assert np.array_equal(ImageEncoder._mask_colors(elevation, rgb, None), elevation)


def test_matches_the_colour_and_not_the_height_it_decodes_to():
    # The whole reason this exists. Two pixels decode to the same height; only
    # one of them is the colour the source uses for nodata, and mask_values
    # cannot tell them apart.
    elevation = np.array([[42.0, 42.0]])
    rgb = _rgb((0, 0, 0), (1, 2, 3))

    out = ImageEncoder._mask_colors(elevation, rgb, ["#000000"])

    assert np.isnan(out[0, 0])
    assert out[0, 1] == 42.0, "it masked by height after all"


def test_a_source_survives_the_trip_to_a_worker(tmp_path):
    # The source config used to be flattened into a positional tuple and
    # rebuilt on the other side, and the tuple was not kept in step:
    # encoding_factors never reached the workers, so a custom-encoded source
    # failed in every one of them. The sources are pickled whole now.
    path = tmp_path / "source.mbtiles"
    path.write_bytes(b"")

    source = MBTilesSource(
        path=Path(path),
        encoding=EncodingType.CUSTOM,
        mask_values=[0.0, -1.0],
        mask_colors=["#000000"],
        encoding_factors={
            "redFactor": 256,
            "greenFactor": 1,
            "blueFactor": 1 / 256,
            "baseShift": 32768,
        },
    )

    revived = pickle.loads(pickle.dumps(source))

    assert revived.encoding is EncodingType.CUSTOM
    assert revived.encoding_factors == source.encoding_factors
    assert revived.mask_colors == ["#000000"]
    assert revived.mask_values == [0.0, -1.0]
