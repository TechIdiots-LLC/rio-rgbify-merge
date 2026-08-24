# rio-rgbify changelog

## main

### ✨ Features and improvements

- **PMTiles output, and PMTiles as a source.** Name the output `.pmtiles` and
  both commands write one; `source_type: "pmtiles"` reads one. A PMTiles
  archive is a single file a range request can read a tile out of, which is
  what a tileset has to be to be served from object storage or seeded to a
  swarm — where an MBTiles has to be unpacked or proxied first.

  ```
  rio rgbify -e mapbox -b -10000 -i 0.1 --min-z 0 --max-z 8 --format png dem.vrt terrain.pmtiles
  ```

  ```json
  {
      "output_type": "pmtiles",
      "output_path": "/path/to/merged.pmtiles",
      "sources": [
          { "path": "/path/to/bathymetry.pmtiles", "source_type": "pmtiles" },
          { "path": "/path/to/terrain.mbtiles", "source_type": "mbtiles" }
      ]
  }
  ```

  Sources of either kind mix freely in one merge, and take the same options as
  each other — `PMTilesSource` inherits every field of `MBTilesSource` rather
  than repeating them, so an option added to one is an option both have.

  Nothing is written to an MBTiles on the way. Tiles go into the archive as
  they are encoded, which is what keeps a planet-scale run from needing a
  scratch copy of its own output; only one directory entry per tile is held in
  memory. The container is taken from the output file extension, or named with
  `--archive-format` for `rgbify` and `output_type` for `merge`, for an output
  called something else.

  The archives are **clustered** — tiles are written in ascending tile id, so a
  reader asking for a range of the file gets tiles that are near each other on
  the map. That is the whole point of the format for a range-requesting client,
  and it is not automatic: it is why `rgbify` sorts its tile list and consumes
  worker results in order rather than as they finish, and why the merge hands
  encoded tiles back to one writer instead of having every worker write its own.

  Header and metadata match what our [mbutil](https://github.com/TechIdiots-LLC/mbutil)
  fork writes, so `mb-util` reads these archives and converts them back and
  forth. `encoding` — the one thing about a terrain tileset that cannot be read
  off the pixels — travels in the metadata, and metadata values are strings
  either way, which is what an MBTiles `metadata` table hands back.

  Conformance to [spec/v3](https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md)
  is tested by parsing the raw bytes rather than by reading an archive back
  with the library that wrote it — a writer bug and a reader bug that agree
  would pass that. Every shape this package can produce goes through it, since
  the ones that break a MUST are the unusual ones.

- **`name`, `description` and `attribution` can be set on the output.**
  `--name`, `--description` and `--attribution` for `rgbify`; the same three
  keys at the top level of a `merge` config. They were not settable at all
  before: every tileset came out called "Terrain" or "Merged Terrain", with a
  description that was the timestamp of the run and no credit line anywhere.

  ```
  rio rgbify --name "Ocean Floor" --attribution "© GEBCO 2026" dem.vrt out.pmtiles
  ```

  ```json
  {
      "name": "Ocean Floor",
      "description": "GEBCO bathymetry under JAXA land",
      "attribution": "© GEBCO 2026, © JAXA"
  }
  ```

  Worth setting at the point the tileset is built rather than afterwards. A
  PMTiles archive keeps its metadata between the root directory and the leaf
  directories, so saying something different later changes its length, moves
  every offset that follows it, and means writing the whole file again — which
  for a planet-scale terrain archive is not a correction anyone makes twice.

  `attribution` is left out of the metadata when it is not given, rather than
  written empty: a consumer that renders a credit line renders an empty one.
  `description` still falls back to the time of the run. Both containers and
  all three commands — `rgbify`, `merge`, and raster output — record them the
  same way, and all three survive a round trip through `mb-util`.

### 🐞 Bug fixes

- **The zoom range in the PMTiles header now describes the tiles that are
  actually there.** `pmtiles` 3.7.0 takes it from the first and last directory
  entries' tile ids, and the last entry's id is where that entry's *run*
  starts. Identical tiles are run-length encoded, so a run crossing a zoom
  boundary left `max_zoom` naming a zoom shallower than the deepest tile in
  the archive — and a client reads that header to decide what to request, so
  the tiles past it were never asked for.

  Terrain is the case that hits it: an ocean tile is byte-identical over huge
  areas, so the runs are long. An all-ocean z0–z3 archive came out declaring
  `max_zoom: 0` with every z3 tile present and unreachable. The header is
  rewritten after finalising with the range that was written.

- **`version` in tileset metadata is a valid SemVer string.** It was `"1"`.
  The PMTiles v3 spec requires this key to be valid
  [SemVer 2.0.0](https://semver.org/spec/v2.0.0.html) where it appears, and
  these archives are read by clients that hold the spec to it. Changed for
  MBTiles output too, so that converting one with `mb-util` does not produce a
  PMTiles that breaks the spec.

## 0.7.0

### ✨ Features and improvements

- **`mask_range`: mask a band of heights, not a list of exact values.** Nodata
  is rarely one number by the time it reaches a merge. A source resampled on
  its way to being built does not hold what it was authored with, so a sea
  authored as `0` arrives spread over -0.9 m to 0 — and `mask_values` on the
  two ends of that leaves everything between standing proud of whatever is
  underneath, which a hillshade picks out as a scatter of bright pixels.

  ```json
  { "path": "planet.mbtiles", "mask_range": [-1, 0] }
  ```

  A list of bands works too, and both ends belong to the band. Compared to the
  thousandth, because a height decoded through float arithmetic is not the
  value it was authored with — 0 comes back as 1.8e-12, and -0.2 as
  -0.20000000298 — and a band that excluded its own endpoint would leave the
  row of pixels at its edge behind, which is the artefact it exists to remove.
  Raster sources take it as well as tiled ones.

- **`feather_metres`: a fade measured in ground rather than in pixels.** A
  hillshade reads slope rather than height, so what decides whether a seam
  disappears is the drop divided by the ground underneath it — and a pixel is a
  different amount of ground at every zoom. Over a 7 m disagreement at 55°N,
  `feather: 8` is a gradient of 0.08 at z12 and 1.28 at z16: invisible at one
  end, and at the other a saturated band wider than the cliff it replaced.

  ```json
  { "path": "swissalti.mbtiles", "bounds": [5, 45, 11, 48], "feather_metres": 50 }
  ```

  Fifty metres holds 0.14 at every zoom, which is ordinary hillside. The pixels
  are worked out for each tile from
  `40075016.686 × cos(latitude) / 2^zoom / tile_size`. Below the zoom where the
  fade is under a pixel wide it rounds to nothing, and it is capped at a quarter
  of the tile — 128 pixels on a 512px grid, which is where the old maximum of 64
  came from. `feather_meters` is read as well, since every config key is read
  with `.get()` and one spelled the other way would silently do nothing.

  The fade still applies at a `cutline` or `bounds` edge only. The holes
  `mask_values`, `mask_range` and `mask_colors` leave are not faded here: that
  needs the tiles either side of the one being built, so a tile can tell whether
  a hole continues past its own border, and it is not built.

### 🐞 Bug fixes

- **A raster source never built.** `base_val` and `interval` are the output
  encoding's and belong to the merger, which has them; the config parser passed
  them to `RasterSource`, which has neither. That raised a `TypeError` the merge
  command logged and swallowed, so `source_type: "raster"` produced no sources
  and merged nothing while reporting success. They are no longer passed, and the
  README no longer lists them as raster source fields — they were never read
  there.

## 0.6.0

### ✨ Features and improvements

- **`cutline`, `bounds` and `feather`: clip a source to a shape, and fade it in
  at the edge of one.** The merge took the upper source outright wherever it
  had data, so where a high-resolution local DEM stopped, the next pixel was a
  different survey on a different vertical datum. Under a hillshade that is a
  wall.

  ```json
  {
    "path": "swissalti.mbtiles",
    "cutline": "switzerland.geojson",
    "feather": 16
  }
  ```

  The step left is the height difference divided by the feather, which makes
  the number predictable from what it has to hide: 40 m faded over 16 pixels
  steps 2.5 m a pixel rather than 40 m at once. It covers a vertical datum
  disagreement too, which is the same wall by another cause and otherwise wants
  a hand-tuned `height_adjustment`.

  `bounds` is `[west, south, east, north]` and is built as a four-cornered
  cutline rather than handled separately, so one cannot disagree with the other
  about what an edge is. `feather` is capped at 64 pixels — past that the ramp
  never reaches full weight inside a 256px tile, and the source is being turned
  down rather than blended in.

  Two rules that are not obvious and are what the tests hold. The ramp runs
  inward only, because a cutline says where a source's data is good and
  spreading it outward would answer for ground the config just excluded. And a
  feathered source over ground nothing else covers stands at full weight, or it
  would erode itself by the width of its own feather exactly where it is the
  only thing there.

  The distance is exact Euclidean, via `scipy.ndimage.distance_transform_edt`,
  so a diagonal boundary ramps at the same rate as a straight one. The geometry
  is loaded once per process and cached by path — the source config is pickled
  to a worker for every tile, so the path travels and the coordinates do not.

  A tile outside the cutline's extent is rejected on the bounding box without
  rasterising anything. Inside it, a 20,000-vertex boundary costs about 12 ms
  per 256px tile and 29 ms per 512px one; the obvious next step is indexing the
  segments so a tile no edge crosses can be settled with one point-in-polygon
  test, as it is nowhere near the boundary.

- **`mask_colors`, which masks by the pixel a source stored rather than by the
  height it decodes to.** `mask_values` can only say "every pixel at this
  height", and a source marking its nodata with #000000 usually decodes that to
  a height real ground elsewhere is also at — so masking it takes out both. A
  colour is what the source actually said.

  ```json
  { "path": "swissalti.mbtiles", "mask_colors": ["#000000"] }
  ```

  Accepts `"#rrggbb"`, `"rrggbb"` and `[r, g, b]`. Compared exactly, on the
  channels as stored, before any height adjustment — for the same reason
  `mask_values` is. A colour that cannot be read is refused rather than
  skipped: a mask that silently matches nothing is the failure this is most
  prone to.

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
