# rio-rgbify

> **Fork:** This is a fork of [mapbox/rio-rgbify](https://github.com/mapbox/rio-rgbify), maintained by TechIdiots LLC under the same MIT License.

Encode arbitrary bit depth rasters in pseudo base-256 as RGB, and merge several
terrain sources into a single tileset.

## Installation

```
pip install rio-rgbify-merge
```

The distribution is called `rio-rgbify-merge` because `rio-rgbify` on PyPI is
the upstream package, which this fork does not replace. The import name and the
commands are unchanged: `import rio_rgbify`, `rio rgbify` and `rio merge`.

To work on it instead:

```
git clone https://github.com/TechIdiots-LLC/rio-rgbify-merge.git

cd rio-rgbify

pip install -e '.[test]'

```
## Required Packages on Ubuntu
To run `rio-rgbify` on Ubuntu, you will need to make sure you have the following installed:

*   `python3-dev`
*   `libspatialindex-dev`
*   `libgeos-dev`
*   `gdal-bin`
*   `python3-gdal`

You can install these using the following command:

```bash
sudo apt update
sudo apt install python3-dev libspatialindex-dev libgeos-dev gdal-bin python3-gdal
```

## CLI usage

`rio-rgbify` now has two subcommands `rgbify` and `merge`.

### `rgbify` Command

The `rgbify` command is used to encode a raster into RGB and output it as a GeoTIFF, an MBTiles file, or a PMTiles archive.

-   Input can be any raster readable by `rasterio`
-   Output can be a GeoTIFF, an MBTiles file, or a PMTiles archive, all created using tile-based processing.
-   The container is taken from the output file extension. `--archive-format` overrides it, for an output named something else.

```
Usage: rio rgbify [OPTIONS] SRC_PATH DST_PATH

  rio-rgbify cli.

Options:
  -b, --base-val FLOAT            The base value of which to base the output
                                  encoding on [DEFAULT=0]
  -i, --interval FLOAT            Describes the precision of the output, by
                                  incrementing interval [DEFAULT=1]
  -r, --round-digits INTEGER      Less significants encoded bits to be set to
                                  0. Round the values, but have better images
                                  compression [DEFAULT=0]
  -e, --encoding [mapbox|terrarium]
                                  RGB encoding to use on the tiles
  --bidx INTEGER                  Band to encode [DEFAULT=1]
  --max-z INTEGER                 Maximum zoom to tile (.mbtiles output only)
  --bounding-tile TEXT            Bounding tile '[, , ]' to limit output tiles
                                  (.mbtiles output only)
  --min-z INTEGER                 Minimum zoom to tile (.mbtiles output only)
  --format [png|webp]             Output tile format (.mbtiles output only)
  --name TEXT                     Tileset name, written to the output's
                                  metadata [DEFAULT: Terrain]
  --description TEXT              Tileset description [DEFAULT: the time of
                                  the run]
  --attribution TEXT              Credit line for the tileset. Omitted from the
                                  metadata when unset
  --archive-format [mbtiles|pmtiles]
                                  Output container, as opposed to --format,
                                  which is the image inside it [DEFAULT: from
                                  the output file extension]
  -j, --workers INTEGER           Workers to run [DEFAULT=4]
  -v, --verbose
  --batch-size INTEGER            Number of tiles to process at a time in each
                                  process.
  --resampling [nearest|bilinear|cubic|cubic_spline|lanczos|average|mode|gaussian]
                                  Resampling method
  --help                          Show this message and exit
```

### Mapbox TerrainRGB example

```
rio rgbify -e mapbox -b -10000 -i 0.1 --min-z 0 --max-z 8 -j 24 --format png SRC_PATH.vrt DST_PATH.mbtiles
```

### Mapzen Terrarium example

```
rio rgbify -e terrarium --min-z 0 --max-z 8 -j 24 --format png SRC_PATH.vrt DST_PATH.mbtiles
```

### PMTiles example

```
rio rgbify -e mapbox -b -10000 -i 0.1 --min-z 0 --max-z 8 -j 24 --format png SRC_PATH.vrt DST_PATH.pmtiles
```

### Naming a tileset

`--name`, `--description` and `--attribution` are written into the output's
metadata. Set them for a PMTiles output in particular: an archive keeps its
metadata between the root directory and the leaf directories, so saying
something different afterwards changes its length, moves every offset that
follows, and means writing the whole file again.

```
rio rgbify -e mapbox --min-z 0 --max-z 12 --format png   --name "Ocean Floor"   --description "GEBCO bathymetry under JAXA land"   --attribution "© GEBCO 2026, © JAXA"   SRC_PATH.vrt DST_PATH.pmtiles
```

### `merge` Command

The `merge` command is used to merge multiple MBTiles, PMTiles, or Raster files into one output MBTiles file or PMTiles archive. This is done by taking a JSON configuration file.

```
Usage: rio merge [OPTIONS]

Options:
 -c, --config PATH       Path to the JSON configuration file [required]
 -j, --workers INTEGER   Workers to run [DEFAULT=4]
 -z, --min-zoom INTEGER       Minimum zoom level to generate.
 -v, --verbose
 --help               Show this message and exit.
```

#### Configuration File

The `merge` command makes use of a json configuration file which should be passed in as the `--config` parameter. The JSON configuration file should follow the following structure:

```json
{
    "source_type": "mbtiles",
    "sources": [
        {
            "path": "/path/to/bathymetry.mbtiles",
            "encoding": "mapbox",
            "height_adjustment": -5.0
        },
        {
            "path": "/path/to/base_terrain.mbtiles",
            "encoding": "mapbox",
            "height_adjustment": 0.0,
            "base_val": -10000,
            "interval": 0.1,
             "mask_values": [-1,0]
        },
        {
            "path": "/path/to/secondary_terrain.mbtiles",
            "encoding": "terrarium",
            "height_adjustment": 10.0
        }
    ],
    "output_path": "/path/to/output.mbtiles",
    "name": "Ocean Floor",
    "description": "GEBCO bathymetry under JAXA land",
    "attribution": "© GEBCO 2026, © JAXA",
    "output_encoding": "mapbox",
    "output_nodata": -9999,
    "output_format": "webp",
    "resampling": "bilinear",
    "sparse_tiles": true,
    "min_zoom": 2,
    "max_zoom": 10,
    "gaussian_blur_sigma": 0.2,
    "bounds": [-10,10,20,50],
    "bounds_source": 1
}
```

```json
{
    "source_type": "raster",
    "sources": [
        {
            "path": "/path/to/raster1.tif",
            "height_adjustment": -5.0,
            "base_val": -10000,
            "interval": 0.1,
            "mask_values": [0]
        },
        {
            "path": "/path/to/raster2.tif",
            "height_adjustment": 10.0,
            "mask_values": [-1,-32767]
        }
    ],
    "output_path": "/path/to/output.mbtiles",
    "output_nodata": -9999,
    "output_encoding": "terrarium",
    "output_format": "webp",
    "resampling": "bilinear",
    "sparse_tiles": true,
    "min_zoom": 2,
    "max_zoom": 10,
    "bounds": [-10,10,20,50],
    "bounds_source": 1
}
```

**Explanation:**

*   **`output_type` (Optional, Default: `mbtiles`):** The output container — `mbtiles`, `pmtiles`, or `raster`. It wins over the `output_path` extension, so a config asking for `pmtiles` gets one whatever the file is called.
*   **`source_type` (Optional, Default: `mbtiles`):** This is a new key which tells the program whether the sources are `mbtiles`, `pmtiles`, or `raster`. `mbtiles` and `pmtiles` sources take exactly the same options as each other.
*   **`sources` (Required):**
    *   A list of objects defining the input MBTiles or Raster files.
    *   **MBTiles and PMTiles Sources**:
        *   `path` (Required): The path to the MBTiles file or PMTiles archive.
        *   `encoding` (Optional, Default: `"mapbox"`): The encoding used for the MBTiles file (`"mapbox"`, `"terrarium"` or `"custom"`).
        *   `redFactor`, `greenFactor`, `blueFactor`, `baseShift` (Required with `"custom"`): The four numbers that describe how this source packs a height, as MapLibre's style-spec defines them — `height = r*redFactor + g*greenFactor + b*blueFactor - baseShift`. All four are required; three of four is refused, because the tile cannot be decoded either way. `snake_case` spellings are accepted too.
        *   `height_adjustment` (Optional, Default: `0.0`): A floating-point value (in meters) to adjust the elevation of that particular input. Positive values raise the elevation, and negative values lower the elevation.
        *   `base_val` (Optional, Default: `-10000`): A floating-point value which will be the base value for mapbox encoded tiles, in meters.
        *    `interval` (Optional, Default: `0.1`): A floating-point value that represents the vertical distance between each level of encoded height.
        *   `mask_values` (Optional, Default `[0.0]`): A list of numbers representing the elevation values to mask.
        *   `mask_range` (Optional, Default `[]`): A `[low, high]` band of heights meaning "no data here", or a list of such bands. Both ends are included. Nodata is rarely one number by the time it reaches a merge: a source resampled on its way to being built does not hold what it was authored with, so a sea authored as `0` arrives spread over `-0.9 m` to `0` — and `mask_values` on the two ends of that leaves everything between standing proud of whatever is underneath, which a hillshade picks out as a scatter of bright pixels. Compared to the thousandth, so a band includes the number written on it.
        *   `mask_colors` (Optional, Default `[]`): A list of pixel colours meaning "no data here", as `"#rrggbb"` or `[r, g, b]`. Exact where `mask_values` has to approximate: a source marking its nodata with a particular pixel says so in the bytes, and the height that pixel decodes to is one real ground elsewhere may also be at. Compared before any height adjustment, on the channels as stored.
        *   `cutline` (Optional): Path to a GeoJSON file limiting where this source contributes. Coordinates are longitude and latitude. Read once per process and cached by path, so a national boundary costs one read rather than one per tile.
        *   `bounds` (Optional): `[west, south, east, north]` — a rectangular cutline, for when a shape file would be overkill. Built as a four-cornered cutline rather than handled separately, so the two cannot disagree about what an edge is.
        *   `feather` (Optional, Default `0`, max `64`): Pixels to fade this source in over at the edge of its `cutline` or `bounds`, instead of stopping dead. Where a high-resolution source meets a coarser one the two disagree — by their survey and by their vertical datum — and the step between them reads as a wall under a hillshade. The step left is the height difference divided by this, so 40 m faded over 16 pixels steps 2.5 m a pixel. The ramp runs inward only, and a feathered source over ground nothing else covers is left at full weight rather than fading into nothing. See `feather_metres`, which is usually the better way to say the same thing.
        *   `feather_metres` (Optional, Default `0`): The same fade written as metres of ground, converted to pixels for each tile that is built. A hillshade reads slope rather than height, so what decides whether a seam disappears is the drop divided by the ground underneath it — and a pixel is a different amount of ground at every zoom. Over a 7 m disagreement at 55°N, `feather: 8` is a gradient of 0.08 at z12 and 1.28 at z16: invisible at one end, and at the other a saturated band wider than the cliff it replaced. `feather_metres: 50` holds 0.14 at both, which is ordinary hillside. Below the zoom where the fade is under a pixel wide it rounds to nothing, and it is capped at a quarter of the tile — 128 pixels on a 512px grid, which is where the `feather` maximum of 64 came from. `feather_meters` is read as well, and it wins over `feather` where both are set.
     *   **Raster Sources**:
        *   `path` (Required): The path to the raster file.
        *   `height_adjustment` (Optional, Default: `0.0`): A floating-point value (in meters) to adjust the elevation of that particular input. Positive values raise the elevation, and negative values lower the elevation.
        *   `mask_values` (Optional, Default `[0.0]`): A list of numbers representing the elevation values to mask.
        *   `mask_range` (Optional, Default `[]`): A `[low, high]` band of heights meaning "no data here", or a list of such bands. Both ends are included. Nodata is rarely one number by the time it reaches a merge: a source resampled on its way to being built does not hold what it was authored with, so a sea authored as `0` arrives spread over `-0.9 m` to `0` — and `mask_values` on the two ends of that leaves everything between standing proud of whatever is underneath, which a hillshade picks out as a scatter of bright pixels. Compared to the thousandth, so a band includes the number written on it.
*   `output_path` (Optional, Default: `"output.mbtiles"`, or `"output.pmtiles"` under `output_type: "pmtiles"`): The output path for the merged archive.
*   `name` (Optional, Default: `"Merged Terrain"`, or `"Merged Raster"` under `output_type: "raster"`): The tileset's name, written to the output's metadata.
*   `description` (Optional, Default: the time of the run): The tileset's description, written to the output's metadata.
*   `attribution` (Optional): A credit line for the tileset. Left out of the metadata entirely when unset, rather than written empty — a consumer renders an empty credit line. Set this for a PMTiles output in particular: an archive keeps its metadata between the root directory and the leaf directories, so saying something different afterwards changes its length, moves every offset that follows, and means writing the whole file again.
*   `output_encoding` (Optional, Default: `"mapbox"`): The output encoding to use (`"mapbox"`, `"terrarium"` or `"custom"`). Recorded in the output's metadata as `encoding`: it is the one thing about a terrain tileset that cannot be read off the pixels, and a server needs it to decode a height.
*   `output_redFactor`, `output_greenFactor`, `output_blueFactor`, `output_baseShift` (Required with `output_encoding: "custom"`): The formula to write the merged tiles with. Sources may use different encodings from each other and from the output — everything is decoded to metres before it is merged, so the encodings only have to be readable, not to match.

    **Note the sign.** `baseShift` is *subtracted*, where `base_val` is added: a mapbox source's `base_val` of `-10000` is a `baseShift` of `10000`. The two named encodings are exactly these numbers — mapbox is `(6553.6, 25.6, 0.1, 10000)` and terrarium is `(256, 1, 0.00390625, 32768)` — so `"custom"` with those values behaves identically to naming them.
*   `output_nodata` (Optional, Default: `None`): The value to use for output nodata replacement.  If set, `NaN` values will be replaced with this value. If `None`, no nodata replacement is performed.
*   `output_format` (Optional, Default: `"png"`): The output image format (`"png"` or `"webp"`).
*   `resampling` (Optional, Default: `"bilinear"`): The method to use for resampling (`"nearest"`, `"bilinear"`, `"cubic"`, `"cubic_spline"`, `"lanczos"`, `"average"`, `"mode"`, or `"gauss"`).
*   `sparse_tiles` (Optional, Default: false): A boolean that determines whether to skip writing tiles that only contain upscaled data. If true, tiles consisting entirely of upscaled data will not be written to the output MBTiles file.

    The value is recorded in the output's metadata as `sparse`, which tells a server whether a missing tile means "overzoom from a lower one" (404) or "there is genuinely nothing here" (204). tileserver-gl merges an archive's metadata straight into its TileJSON, so the archive answers the question itself rather than needing it repeated in the server config. In a PMTiles archive it is a JSON boolean; an MBTiles `metadata` table can only hold text, so there it is the string `"true"` or `"false"`.

    `rgbify` writes no `sparse` key, because it has no such option — every tile it is asked for is one it writes.
*   `min_zoom` (Optional, Default: `0`): The minimum zoom level to process.
*   `max_zoom` (Optional, Default: uses max from last file): The maximum zoom level to process.
*  `bounds` (Optional, Default: bounds of last file): A bounding box to limit the tiles being generated. Should be in the format: `[w,s,e,n]`. **Overrides `bounds_source` if set**.
*  `bounds_source` (Optional, Default: Uses last source): An integer which corresponds to the source in the `sources` array which should be used to generate bounds and tiles from. **The index starts at 0.**
* `gaussian_blur_sigma` (Optional, Default: `0.2`): A floating-point value that controls the base strength of the gaussian blur applied to source tiles during upscaling. The actual blur applied is scaled based on the zoom level difference between the source and the output tile.

  **Understanding Zoom-Level Dependent Blurring:**

   The `gaussian_blur_sigma` parameter no longer directly represents the amount of blur applied. Instead, it serves as a *base* value for the blur. The actual amount of blurring is now *dynamically* adjusted based on the zoom level difference between the source tile and the target tile:

    *   **Base Blur:** The `gaussian_blur_sigma` sets the starting point for how much blurring to apply.
    *   **Dynamic Adjustment:** When a tile is upscaled, the amount of blurring is scaled by the absolute difference between the source zoom level and the target zoom level. For example, if `gaussian_blur_sigma` is `0.2`, a tile that is upscaled by 2 zoom levels will have a sigma of 0.4 applied.
    *   **Adaptive Smoothing:** This means tiles that require significant upscaling receive more smoothing, reducing blockiness, while tiles that are closer to their target zoom receive less smoothing, preserving detail.
    *   **Linear Scaling**: The blurring is scaled linearly by the zoom difference. This value can be changed by multiplying by a different number, and will be considered in later versions.

    **Choosing a `gaussian_blur_sigma` Value:**

    As the `gaussian_blur_sigma` now acts as a base value, a good start is to aim for the ideal smoothing at a single zoom difference. For example if you want 0.4 smoothing when the zoom difference is 2, then use 0.2.
    Start with the default (`0.2`) and experiment, using higher values if the upscaling looks too "bumpy" or if you want more smoothing, and lower values if you think its too blurry.

The merge logic works by merging the input sources in order, applying the height adjustment as it merges. The last input source will be the base layer for tiles, and the bounds of this last file will be used if no bounds are passed in, unless a `bounds_source` parameter has been set.

## Merge Example

```
rio merge --config config.json -j 24
```
