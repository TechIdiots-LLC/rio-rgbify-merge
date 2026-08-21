"""
The custom terrain encoding, where the config supplies the formula.

MapLibre's style-spec lets a raster-dem source say how its channels pack a
height instead of choosing between two names:

    height = r*redFactor + g*greenFactor + b*blueFactor - baseShift

These check ours against that formula directly, and check that the two named
encodings are the special cases of it they are supposed to be.
"""

import numpy as np
import pytest

from rio_rgbify.image import ImageEncoder


TERRARIUM_AS_CUSTOM = {
    "redFactor": 256.0,
    "greenFactor": 1.0,
    "blueFactor": 1.0 / 256.0,
    "baseShift": 32768.0,
}

MAPBOX_AS_CUSTOM = {
    "redFactor": 6553.6,
    "greenFactor": 25.6,
    "blueFactor": 0.1,
    "baseShift": 10000.0,
}


def _maplibre_unpack(r, g, b, factors):
    """MapLibre's own formula, to compare against."""
    return (
        r * factors["redFactor"]
        + g * factors["greenFactor"]
        + b * factors["blueFactor"]
        - factors["baseShift"]
    )


class TestDecoding:
    def test_agrees_with_maplibre_on_what_a_pixel_means(self):
        rgb = np.array([[[130]], [[45]], [[200]]], dtype=np.int32)
        got = ImageEncoder._decode(rgb, 0, 0, "custom", TERRARIUM_AS_CUSTOM)
        assert got[0][0] == pytest.approx(
            _maplibre_unpack(130, 45, 200, TERRARIUM_AS_CUSTOM)
        )

    def test_terrarium_is_a_special_case_of_custom(self):
        rgb = np.array([[[130]], [[45]], [[200]]], dtype=np.int32)
        named = ImageEncoder._decode(rgb, 0, 0, "terrarium")
        as_custom = ImageEncoder._decode(rgb, 0, 0, "custom", TERRARIUM_AS_CUSTOM)
        assert named[0][0] == pytest.approx(as_custom[0][0])

    def test_mapbox_is_a_special_case_of_custom(self):
        rgb = np.array([[[12]], [[34]], [[56]]], dtype=np.int32)
        named = ImageEncoder._decode(rgb, -10000, 0.1, "mapbox")
        as_custom = ImageEncoder._decode(rgb, 0, 0, "custom", MAPBOX_AS_CUSTOM)
        assert named[0][0] == pytest.approx(as_custom[0][0], abs=0.001)


class TestRoundTrip:
    @pytest.mark.parametrize("metres", [0.0, 500.5, -1200.0, 8848.0])
    def test_survives_a_formula_of_its_own(self, metres):
        # Half-metre steps over a wider range than mapbox's 0.1 m, which is the
        # reason to reach for custom in the first place.
        factors = {
            "redFactor": 32768.0,
            "greenFactor": 128.0,
            "blueFactor": 0.5,
            "baseShift": 40000.0,
        }
        data = np.full((2, 2), metres, dtype=np.float64)
        rgb = ImageEncoder.data_to_rgb(data, "custom", 0.1, factors=factors)
        back = ImageEncoder._decode(rgb.astype(np.int32), 0, 0, "custom", factors)
        assert back[0][0] == pytest.approx(metres, abs=0.5)

    def test_a_custom_terrarium_matches_the_named_one(self):
        # Encoding through the custom path with terrarium's numbers should
        # produce the same heights the named path does.
        data = np.full((2, 2), 1234.0, dtype=np.float64)
        named = ImageEncoder.data_to_rgb(data, "terrarium", 0.1)
        custom = ImageEncoder.data_to_rgb(
            data, "custom", 0.1, factors=TERRARIUM_AS_CUSTOM
        )
        a = ImageEncoder._decode(named.astype(np.int32), 0, 0, "terrarium")
        b = ImageEncoder._decode(
            custom.astype(np.int32), 0, 0, "custom", TERRARIUM_AS_CUSTOM
        )
        assert a[0][0] == pytest.approx(b[0][0], abs=0.01)


class TestRefusingAnUnreadableEncoding:
    def test_all_four_numbers_or_none(self):
        # Three of four is no better than none: the tile cannot be decoded
        # either way, and a partial guess produces heights that look plausible
        # and are wrong.
        rgb = np.array([[[1]], [[2]], [[3]]], dtype=np.int32)
        with pytest.raises(ValueError, match="redFactor"):
            ImageEncoder._decode(rgb, 0, 0, "custom", None)
        with pytest.raises(ValueError, match="redFactor"):
            ImageEncoder._decode(
                rgb, 0, 0, "custom", {"redFactor": 1, "greenFactor": 1}
            )

    def test_a_zero_channel_factor_is_refused(self):
        # It would divide by zero while packing, and means the channel carries
        # nothing -- which is a mistake rather than an encoding.
        rgb = np.array([[[1]], [[2]], [[3]]], dtype=np.int32)
        with pytest.raises(ValueError, match="must not be zero"):
            ImageEncoder._decode(
                rgb,
                0,
                0,
                "custom",
                {
                    "redFactor": 0,
                    "greenFactor": 1,
                    "blueFactor": 1,
                    "baseShift": 0,
                },
            )
