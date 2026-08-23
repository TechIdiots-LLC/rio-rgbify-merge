# rio-rgbify changelog

## main

### ✨ Features and improvements

- **The `custom` terrain encoding, where the config supplies the formula.** A
  source, and the output, can now say how their channels pack a height instead
  of choosing between `mapbox` and `terrarium`, using the four numbers
  MapLibre's style-spec defines:

  ```
  height = r*redFactor + g*greenFactor + b*blueFactor - baseShift
  ```

  Taken from maplibre-gl-js's own `dem_data.ts`, including the sign — `baseShift`
  is subtracted, where `base_val` is added, so a mapbox `base_val` of `-10000` is
  a `baseShift` of `10000`. Packing follows MapLibre's too, scaling by the
  smallest factor so the least-significant channel is not rounded away before the
  others have had their share.

  All four numbers are required. Three of four is refused rather than half-read:
  the tile cannot be decoded either way, and a partial guess produces heights
  that look plausible and are wrong. A source is checked when it is built, so a
  run that cannot work fails before it reads a tile rather than at every one.

  Sources may mix encodings freely, with each other and with the output —
  everything is decoded to metres before it is merged.

  `mapbox` and `terrarium` are unchanged, byte for byte: `custom` is a third
  branch rather than a rewrite of the two, which the pixel-level reference tiles
  confirm.

### 🐞 Bug fixes

- **Smoothing was eating masked ground and drawing a grid at tile boundaries.**
  Two separate faults in the same few lines, both invisible in the tile you are
  looking at and both visible in the map.

  `scipy.ndimage.gaussian_filter` makes NaN out of any NaN in the kernel, so one
  masked pixel took a disc the width of the kernel with it — and `reproject`,
  never told what nodata was, averaged NaN into its neighbours before that. One
  masked pixel in a 9×9 array came back as 81. `mask_values` defaults to `[0.0]`
  and sea level is exactly 0 in most DEMs, so this ate the coastline of every
  upscaled tile, by more at every zoom since the sigma grows with the distance.

  Both stages now declare NaN as their nodata. The blur is a normalised
  convolution — values blurred with nodata counted as zero, a mask of what was
  known blurred the same way, one divided by the other — and the mask is
  restored afterwards, so nodata neither grows nor shrinks. A blur is meant to
  change the heights, not the shape of what has them.

  Separately, a tile was filtered on its own, so the two tiles either side of a
  boundary computed it from different data and stepped apart. The pixels were
  already in hand: the parent covers this sub-region and everything around it,
  so the destination window is widened by the blur's own reach and cropped
  afterwards. Two neighbours out of one parent, `gaussian_blur_sigma` 1.5,
  stepping beyond what the same two unblurred tiles step:

  | upscale | sigma | border | before   | after    |
  | ------- | ----- | ------ | -------- | -------- |
  | 2       | 3     | 12 px  | 202.41 m | −0.64 m  |
  | 4       | 6     | 24 px  | 228.43 m | 0.35 m   |
  | 6       | 9     | 36 px  | 58.80 m  | 8.44 m   |
  | 8       | 12    | 48 px  | 57.39 m  | 21.78 m  |

  The border is capped at a quarter of the tile, which is what leaves a residue
  at the deepest upscales — there the kernel is wider than the cap allows.


## 0.5.0

The first version to carry the merge subsystem in its number. Everything under
"Features and improvements" below has been on `master` since March 2026 but
shipped under 0.4.1, which was set in August 2023 — so a `0.4.1` install may or
may not have any of it, depending on when it was taken.

### ✨ Features and improvements

Present since March 2026, released here for the first time under a version of
its own.

- **`rio-rgbify merge`**, driven by a JSON config: combine several MBTiles or
  raster sources into one output, with per-source `encoding`, `mask_values`,
  `height_adjustment`, `base_val` and `interval`.
- **Sparse tiles** (`sparse_tiles`): skip writing a tile when no source has a
  native tile at that zoom, leaving the client to overzoom from a lower one.
- **`raster_merger`**, the same merge over GeoTIFF sources rather than MBTiles.
- **Zoom-scaled gaussian blur** (`gaussian_blur_sigma`), applied in proportion to
  how far a tile was upscaled from its parent.
- **`output_nodata`**, `bounds`, `bounds_source`, `min_zoom` and `max_zoom`.
- **`database.py`** and **`image.py`**, replacing `encoders.py`.

### 🐞 Bug fixes

- **Layer priority is last-wins again.** `_merge_tiles` had been changed so the
  _first_ source wins and later ones only fill its holes. Sources are listed
  bottom first, so the last one paints over the rest — which is what the README
  describes ("the last input source will be the base layer for tiles", with
  `bounds`, `max_zoom` and `bounds_source` all defaulting to the last file), and
  what `merge_example.json` assumes in listing bathymetry before terrain.

  Under the inverted rule a config of `[GEBCO z0-z8, detailed planet z0-z16]`
  let the coarse global source win everywhere it had data; and because
  `_extract_tile` falls back to a parent tile, its upscaled z8 tile kept winning
  above z8 too, so every zoom past 8 lost its detail. Anyone who ran a merge from
  `master` between 2026-03-20 and this release should rebuild.

- **`height_adjustment` is applied once.** It belongs in `_decode_tile`, which is
  the only place it can go: `mask_values` are compared against raw decoded
  heights, so shifting earlier would stop them matching. `_merge_tiles` added it
  a second time to the already-adjusted result, doubling it — a source set to
  `-5.0` shifted by `-10.0`. This affected the `merge_sparse` branch rather than
  `master`; the rule is now written down beside the code.

- **The sparse empty-tile check no longer sits where it cannot fire.** It ran
  after `output_nodata` had replaced every NaN, by which point no pixel was NaN.
  It is still unreachable — `has_native_with_data` returns first for every input
  that would produce an all-NaN result — but that guard asks a stricter question
  ("is any source native here?"), so the two are not interchangeable, and the
  check now reads as what it means.

## 0.4.1

- Version bump only. Never published to PyPI.

## 0.4.0

- Forked from [mapbox/rio-rgbify](https://github.com/mapbox/rio-rgbify).
