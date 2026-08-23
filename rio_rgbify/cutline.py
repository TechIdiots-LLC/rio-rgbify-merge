"""Clipping a source to a shape, and fading it in at the edge of one.

Where a high-resolution local DEM meets a coarser global one, the merge takes
the upper source outright wherever it has data -- so the pixel on one side of
the boundary is one survey and the pixel on the other is another, on a
different vertical datum. Under a hillshade that reads as a wall.

`feather` turns the edge into a ramp. The weight climbs from 0 at the boundary
to 1 that many pixels inside it, and the merge mixes the two sources across
that band instead of switching between them. The step left is the height
difference divided by the feather, which is what makes the number predictable
from what it has to hide: two sources 40 m apart, faded over 16 pixels, step
2.5 m a pixel rather than 40 m at once.

The ramp runs inward only. A cutline says where a source's data is good, so
spreading it outward would answer for ground the config just said this source
does not cover.
"""

import json
import threading
from dataclasses import dataclass
from pathlib import Path

import mercantile
import numpy as np
import rasterio
from rasterio import features
from scipy.ndimage import distance_transform_edt

from rio_rgbify.smoothing import crop_margin, grow_bounds

# As wide a fade as a source may ask for, in pixels: a quarter of a 256px tile.
# The ramp is measured from the boundary, so a feather wider than the tile is
# one that never reaches full weight anywhere inside it -- the source is then
# not being blended in, it is being turned down.
MAX_FEATHER = 64

# Cutlines are loaded once per process and keyed by path. A national boundary
# is megabytes of coordinates, and the source config is pickled to a worker for
# every tile -- so the geometry must not travel with it. The path does.
_LOADED = {}
_LOADED_LOCK = threading.Lock()


@dataclass
class Cutline:
    """A shape a source is clipped to, ready to rasterise per tile."""

    geometries: list
    bounds: tuple

    def covers(self, tile):
        """
        Whether the shape's extent reaches this tile at all.

        The cheap question, asked so the expensive one usually does not have to
        be. Only the extent: a tile inside the bounding box may still be
        outside the shape, which is what rasterising settles.

        Parameters
        ----------
        tile: mercantile.Tile
            The tile being built.

        Returns
        -------
        bool
            False when nothing of this source belongs here.
        """
        box = mercantile.bounds(tile)
        west, south, east, north = self.bounds
        return not (
            box.east <= west
            or box.west >= east
            or box.north <= south
            or box.south >= north
        )

    def weights(self, tile, size, feather=0):
        """
        Per-pixel weight for this tile: 0 outside the shape, 1 well inside.

        Rasterised with a border where the edge is feathered, because the ramp
        is measured from the boundary and a boundary just outside the tile
        still decides what the pixels inside it weigh. The shape is known in
        full, so that costs the extra rows and nothing else.

        Parameters
        ----------
        tile: mercantile.Tile
            The tile being built.
        size: int
            Pixels per side.
        feather: int
            How far the ramp runs inward, in pixels. 0 makes the edge a switch.

        Returns
        -------
        np.ndarray
            Weights in 0..1, shaped (size, size).
        """
        if not self.covers(tile):
            return np.zeros((size, size), dtype=np.float32)

        margin = int(feather)
        side = size + margin * 2
        box = mercantile.bounds(tile)
        grown = grow_bounds(box.west, box.south, box.east, box.north, margin, size)
        transform = rasterio.transform.from_bounds(*grown, side, side)

        mask = features.rasterize(
            self.geometries,
            out_shape=(side, side),
            transform=transform,
            fill=0,
            default_value=1,
            dtype="uint8",
        )

        if feather > 0:
            # Distance to the nearest pixel the shape does not cover, which is
            # how far inside the boundary each pixel sits. Exact Euclidean, so
            # a diagonal boundary ramps at the same rate as a straight one.
            distance = distance_transform_edt(mask)
            weight = np.minimum(1.0, distance / feather)
        else:
            weight = mask

        return crop_margin(weight.astype(np.float32), size, margin)


def _rectangle(bounds):
    """A four-cornered cutline, as GeoJSON."""
    west, south, east, north = (float(v) for v in bounds)
    if east <= west or north <= south:
        raise ValueError(f"bounds needs west < east and south < north: {bounds}")
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [west, south],
                [east, south],
                [east, north],
                [west, north],
                [west, south],
            ]
        ],
    }


def _geometries_of(document):
    """Every geometry in a GeoJSON document, whatever it is wrapped in."""
    kind = document.get("type")
    if kind == "FeatureCollection":
        return [
            feature["geometry"]
            for feature in document.get("features", [])
            if feature.get("geometry")
        ]
    if kind == "Feature":
        return [document["geometry"]] if document.get("geometry") else []
    if kind == "GeometryCollection":
        return list(document.get("geometries", []))
    if kind:
        return [document]
    raise ValueError("not a GeoJSON document")


def _extent_of(geometries):
    """The bounding box of every coordinate in them."""
    west = south = float("inf")
    east = north = float("-inf")

    def walk(coordinates):
        nonlocal west, south, east, north
        if coordinates and isinstance(coordinates[0], (int, float)):
            x, y = coordinates[0], coordinates[1]
            west, east = min(west, x), max(east, x)
            south, north = min(south, y), max(north, y)
            return
        for part in coordinates:
            walk(part)

    for geometry in geometries:
        walk(geometry.get("coordinates", []))
    if west > east or south > north:
        raise ValueError("the cutline has no coordinates")
    return (west, south, east, north)


def load_cutline(path=None, bounds=None):
    """
    The shape a source is clipped to.

    Parameters
    ----------
    path: str or Path
        A GeoJSON file. Coordinates are longitude and latitude, which is what
        the tile bounds are in.
    bounds: list
        west, south, east, north -- a rectangle, built as a four-cornered
        cutline rather than handled separately, so one implementation cannot
        disagree with the other about what an edge is.

    Returns
    -------
    Cutline or None
        None when neither was given.
    """
    if bounds:
        geometries = [_rectangle(bounds)]
        return Cutline(geometries=geometries, bounds=_extent_of(geometries))
    if not path:
        return None

    key = str(Path(path).resolve())
    with _LOADED_LOCK:
        held = _LOADED.get(key)
    if held is not None:
        return held

    with open(key, encoding="utf-8") as handle:
        document = json.load(handle)
    geometries = _geometries_of(document)
    if not geometries:
        raise ValueError(f"no geometry in {path}")
    cutline = Cutline(geometries=geometries, bounds=_extent_of(geometries))

    with _LOADED_LOCK:
        _LOADED[key] = cutline
    return cutline


def feather_for(value):
    """
    How far a source fades in, bounded.

    Parameters
    ----------
    value: int or float or None
        What the config asked for, in pixels.

    Returns
    -------
    int
        Pixels, 0 when it does not fade.
    """
    if not value:
        return 0
    asked = int(round(float(value)))
    if asked <= 0:
        return 0
    return min(asked, MAX_FEATHER)
