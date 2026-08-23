"""Smoothing an upscaled tile, without eating its nodata or its edges.

Going past the zoom a source has tiles for means upscaling one, and an upscaled
DEM renders as visible terracing under a hillshade -- the steps of the parent's
pixel grid, lit from the side. A gaussian hides that, scaled by the zoom
distance so a tile taken from its immediate parent is barely touched and one
taken from six levels up is smoothed hard.

Two things about doing it with `scipy.ndimage.gaussian_filter` directly, both
of which this module exists to avoid.

**It spreads nodata.** NaN in the kernel makes NaN out, so one masked pixel
takes a disc the width of the kernel with it. `mask_values` defaults to `[0.0]`
and sea level is exactly 0 in most DEMs, so this ate the coastline of every
upscaled tile -- by more at every zoom, since the sigma grows with the
distance.

**It clamps at the edge.** A tile is filtered on its own, so two neighbours
compute the pixels along the boundary they share from different data and step
apart. That draws a faint grid at tile boundaries, worst on steep ground.
Neither is visible in the tile you are looking at; both are visible in the map.
"""

import numpy as np
from scipy.ndimage import gaussian_filter

# scipy's own default, and the reason the border is this wide rather than the
# three sigma a gaussian is usually truncated at.
TRUNCATE = 4.0

# As much border as it is worth reprojecting, as a share of the tile. It costs
# ((size + 2m) / size)^2 and covers nothing the kernel does not reach, and a
# sigma wanting more than this is smoothing the tile into itself -- where the
# tile boundary is no longer what is wrong with the result.
MAX_MARGIN_SHARE = 0.25


def blur_margin(sigma, size, share=MAX_MARGIN_SHARE):
    """
    How much extra to reproject around a tile before blurring it.

    Parameters
    ----------
    sigma: float
        Standard deviation of the blur, in destination pixels.
    size: int
        Pixels per side of the tile itself.
    share: float
        The most of the tile the border may be, per side.

    Returns
    -------
    int
        Pixels to add on every side. 0 when there is no blur to do.
    """
    if not sigma > 0:
        return 0
    return int(min(np.ceil(sigma * TRUNCATE), np.floor(size * share)))


def smooth(data, sigma):
    """
    Blur that leaves nodata where it found it.

    Normalised convolution: the values are blurred with nodata counted as zero,
    a mask of what was known is blurred the same way, and dividing one by the
    other renormalises over the samples that actually had data. That is the
    same thing the resampling kernels do with their own weights, and it is what
    stops a masked pixel pulling its neighbours down toward nothing.

    Parameters
    ----------
    data: np.ndarray
        Heights, NaN where there is nothing.
    sigma: float
        Standard deviation, in pixels. Zero returns the input.

    Returns
    -------
    np.ndarray
        The blurred heights, NaN in the same places.
    """
    if not sigma > 0:
        return data

    known = np.isfinite(data)
    if known.all():
        return gaussian_filter(data, sigma=sigma)

    values = np.where(known, data, 0.0)
    blurred = gaussian_filter(values, sigma=sigma)
    weight = gaussian_filter(known.astype(np.float64), sigma=sigma)

    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(weight > 1e-6, blurred / weight, np.nan)

    # Nodata stays exactly where it was. Renormalising alone would fill a hole
    # in from its edges, which is the same coastline moving by the width of the
    # kernel as before -- inward this time instead of outward. A blur is meant
    # to change the heights, not the shape of what has them.
    return np.where(known, out, np.nan).astype(data.dtype, copy=False)


def grow_bounds(west, south, east, north, margin, size):
    """
    A window widened by a border of its own pixels.

    Parameters
    ----------
    west, south, east, north: float
        The window to grow.
    margin: int
        Pixels on every side.
    size: int
        Pixels per side the window is being sampled at.

    Returns
    -------
    tuple
        west, south, east, north.
    """
    if not margin:
        return west, south, east, north
    per_x = (east - west) / size
    per_y = (north - south) / size
    return (
        west - margin * per_x,
        south - margin * per_y,
        east + margin * per_x,
        north + margin * per_y,
    )


def crop_margin(data, size, margin):
    """
    Take the tile back out of an array that was built with a border.

    Parameters
    ----------
    data: np.ndarray
        Shaped (..., size + 2 * margin, size + 2 * margin).
    size: int
        Pixels per side of the tile wanted.
    margin: int
        The border that was added.

    Returns
    -------
    np.ndarray
        The middle of it.
    """
    if not margin:
        return data
    return data[..., margin : margin + size, margin : margin + size]
