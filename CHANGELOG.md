# rio-rgbify changelog

## main

### ✨ Features and improvements

- _...Add new stuff here..._

### 🐞 Bug fixes

- _...Add new stuff here..._

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
