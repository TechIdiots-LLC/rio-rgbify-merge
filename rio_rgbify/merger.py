import sqlite3
import rasterio
import mercantile
from rasterio.warp import reproject, Resampling
import numpy as np
import math
import io
from PIL import Image
from multiprocessing import Pool, Process, Queue
from pathlib import Path
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from typing import Optional, Tuple, List, Dict
from contextlib import contextmanager
from rio_rgbify.database import MBTilesDatabase
from rio_rgbify.image import ImageFormat, ImageEncoder
from queue import Queue
import functools
from rio_rgbify.cutline import feather_pixels, load_cutline
from rio_rgbify.smoothing import blur_margin, crop_margin, grow_bounds, smooth
import time
import multiprocessing #Import the multiprocessing library

def retry(attempts, base_delay=1, max_delay=10):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(attempts):
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    last_exception = e
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    logging.warning(f"Database locked, retry attempt {attempt+1} after {delay} seconds...")
                    time.sleep(delay)

            if last_exception:
                logging.error(f"Failed after {attempts} attempts, raising last exception")
                raise last_exception
            return None
        return wrapper
    return decorator

class EncodingType(Enum):
    MAPBOX = "mapbox"
    TERRARIUM = "terrarium"
    # The formula comes from the config rather than from the name. Same four
    # numbers MapLibre's style-spec uses:
    #   height = r*redFactor + g*greenFactor + b*blueFactor - baseShift
    CUSTOM = "custom"

@dataclass
class MBTilesSource:
    """Configuration for an MBTiles source file"""
    path: Path
    encoding: EncodingType
    height_adjustment: float = 0.0 # Added height adjustment
    base_val: float = -10000 # Add base val, with default of -10000 for mapbox
    interval: float = 0.1 # Add interval with default of 0.1 for mapbox
    mask_values: list = field(default_factory=lambda: [0.0])
    # A band of heights meaning no data, as [low, high] or a list of those.
    # Nodata is rarely one number once an archive has been resampled: a sea
    # authored as 0 arrives spread over -0.9 m to 0, and masking the two ends
    # of that leaves everything between standing proud of what is underneath.
    mask_range: list = field(default_factory=list)
    # Colours meaning no data, as "#rrggbb" or [r, g, b]. Exact where a height
    # mask has to guess: a source marking nodata with a particular pixel says
    # so in the bytes, and the height that pixel decodes to is one real ground
    # elsewhere may also be at.
    mask_colors: list = field(default_factory=list)
    # A GeoJSON file, or four numbers, limiting where this source contributes.
    # The path travels rather than the geometry: a national boundary is
    # megabytes of coordinates and the source config is pickled to a worker for
    # every tile, so it is loaded once per process and cached by path.
    cutline: str = None
    bounds: list = None
    # Pixels to fade in over at that edge. 0 makes the edge a switch.
    feather: int = 0
    # The same fade as metres of ground, converted per tile. A hillshade reads
    # slope, so a fade in pixels is a different slope at every zoom and one
    # number cannot suit them all. Wins over `feather` where both are set.
    feather_metres: float = 0
    # Only read when encoding is CUSTOM, and then all four are required.
    encoding_factors: dict = None

    def __post_init__(self):
        if not self.path.exists():
            raise ValueError(f"Source file does not exist: {self.path}")
        if self.encoding == EncodingType.CUSTOM:
            # Checked here rather than at the first tile: a run that is going
            # to fail on every tile should fail before it reads any of them.
            ImageEncoder._custom_factors_or_raise(self.encoding_factors)


@dataclass
class TileData:
    """Container for decoded tile data"""
    data: np.ndarray
    meta: dict
    source_zoom: int


class TerrainRGBMerger:
    """
    A class to merge multiple Terrain RGB MBTiles files.
    """
    def __init__(self, sources, output_path, output_encoding=EncodingType.MAPBOX, output_nodata=None,
                 resampling=Resampling.lanczos, sparse_tiles=False, processes=None, default_tile_size=512,
                 output_image_format=ImageFormat.PNG,
                 min_zoom=0, max_zoom=None, bounds=None, gaussian_blur_sigma=0.2,
                 bounds_source=None, output_encoding_factors=None):
        self.sources = sources
        self.output_path = Path(output_path)
        self.output_encoding = output_encoding
        self.output_nodata = output_nodata
        self.resampling = resampling
        self.sparse_tiles = sparse_tiles
        self.processes = processes or multiprocessing.cpu_count()
        self.logger = logging.getLogger(__name__)
        self.default_tile_size = default_tile_size
        self.output_image_format = output_image_format
        self.min_zoom = min_zoom
        self.max_zoom = max_zoom
        self.bounds = bounds
        self.write_queue = Queue()
        self.gaussian_blur_sigma = gaussian_blur_sigma
        self.bounds_source = bounds_source
        # Only meaningful when output_encoding is CUSTOM.
        self.output_encoding_factors = output_encoding_factors

        """
        Initializes the TerrainRGBMerger.

        Parameters
        ----------
        sources : List[MBTilesSource]
            A list of MBTiles source configurations.
        output_path : Path
            The path to the output MBTiles file.
        output_encoding : EncodingType, optional
            The encoding for the output tiles. Defaults to EncodingType.MAPBOX.
        resampling : int, optional
            The resampling method to use during tile merging. Defaults to Resampling.lanczos.
        processes : Optional[int], optional
            The number of processes to use for parallel processing. Defaults to multiprocessing.cpu_count().
        default_tile_size : int, optional
            The default tile size in pixels. Defaults to 512.
        output_image_format : ImageFormat, optional
            The output image format of the tiles. Defaults to ImageFormat.PNG
        min_zoom : int, optional
            The minimum zoom level to process tiles, defaults to 0.
        max_zoom : Optional[int], optional
            The maximum zoom level to process tiles, if None, we use the maximum available, defaults to None.
        bounds : Optional[List[float]], optional
            The bounding box to limit the tiles being generated, defaults to None. If None, the bounds of the last source will be used.
        gaussian_blur_sigma: float
            The sigma value to use for the gaussian blur filter, defaults to 0.2
        bounds_source: Optional[int]
            The index of the source to use for the bounds and tiles, defaults to None
        """
    
    def _decode_tile(self, tile_data: bytes, tile: mercantile.Tile, encoding: EncodingType, source: MBTilesSource, source_index: int) -> Tuple[Optional[np.ndarray], dict]:
        """
        Decode tile data using specified encoding format

        Parameters
        ----------
        tile_data : bytes
            The raw tile data.
        tile : mercantile.Tile
            The mercantile tile object.
        encoding : EncodingType
            The encoding used for the tile.
        source : MBTilesSource
            The MBTiles source.
        source_index : int
            The index of the source

        Returns
        -------
        Tuple[Optional[np.ndarray], dict]
            A tuple containing the decoded elevation data and metadata, or None, None if decoding fails.
        """
        if not isinstance(tile_data, bytes) or len(tile_data) == 0:
            raise ValueError("Invalid tile data")
            
        try:
            # Convert the image to a PNG using Pillow
            image = Image.open(io.BytesIO(tile_data))
            image = image.convert('RGB')  # Force to RGB
            image_png = io.BytesIO()
            image.save(image_png, format='PNG', bits=8)
            image_png.seek(0)
            
            with rasterio.open(image_png) as dataset:
                # Check if we can read data properly
                rgb = dataset.read(masked=False).astype(np.int32)
                
                if rgb.ndim != 3 or rgb.shape[0] != 3:
                    self.logger.error(f"Unexpected RGB shape in tile {tile.z}/{tile.x}/{tile.y}: {rgb.shape}")
                    return None, {}

                elevation = ImageEncoder._decode(rgb, source.base_val, source.interval, encoding.value, source.encoding_factors) # Use the static decode method from the encoder
                elevation = ImageEncoder._mask_elevation(elevation, source.mask_values)
                # Before the height adjustment, for the same reason mask_values
                # is: both are compared against what the source stored, and
                # shifting first would stop them matching.
                elevation = ImageEncoder._mask_colors(elevation, rgb, source.mask_colors)
                # A band as well as, not instead of: a source may have a
                # sentinel it names exactly and a stretch of ground it does not
                # want either.
                elevation = ImageEncoder._mask_range(elevation, source.mask_range)

                #Apply height adjustment
                elevation += source.height_adjustment
                
                bounds = mercantile.bounds(tile)
                meta = dataset.meta.copy()
                meta.update({
                    'count': 1,
                    'dtype': rasterio.float32,
                    'driver': 'GTiff',
                    'crs': 'EPSG:3857',
                    'transform': rasterio.transform.from_bounds(
                        bounds.west, bounds.south, bounds.east, bounds.north,
                        meta['width'], meta['height']
                    )
                })
                
                return elevation, meta
        except Exception as e:
            self.logger.error(f"Failed to decode tile data, returning None, None: {e}")
            return None, {}

    def _extract_tile(self, source: MBTilesSource, zoom: int, x: int, y: int, source_conns: Dict[Path, sqlite3.Connection], source_index: int) -> Optional[TileData]:
        """Extract and decode a tile, with fallback to parent tiles
        
        Parameters
        ----------
        source : MBTilesSource
            The MBTiles source to use
        zoom : int
            The zoom level of the tile
        x : int
            The x index of the tile
        y : int
            The y index of the tile
        source_index : int
            The index of the source in the sources list.

        Returns
        -------
        Optional[TileData]
            TileData object or None if it cannot be extracted.
        """
        current_zoom = zoom
        current_x, current_y = x, y
        
        while current_zoom >= 0:
            conn = source_conns[source.path] # get the database connection from the dictionary
            cursor = conn.cursor()
            cursor.execute(
                "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                (current_zoom, current_x, current_y)
            )
            result = cursor.fetchone()
            
            if result is not None:
                try:
                    data_meta = self._decode_tile(result[0], mercantile.Tile(current_x, current_y, current_zoom), source.encoding, source, source_index) #pass in source
                    if data_meta[0] is None:
                        return None
                    if data_meta[0].size == 0:
                        return None
                    return TileData(data_meta[0], data_meta[1], current_zoom)
                except Exception as e:
                    self.logger.error(f"Failed to decode tile //: {e}")
                    return None
            
            if current_zoom > 0:
                current_x //= 2
                current_y //= 2
            current_zoom -= 1
        
        return None

    def _merge_tiles(self, tile_datas: List[Optional[TileData]], target_tile: mercantile.Tile) -> Optional[np.ndarray]:
        """Merge tiles from multiple sources, handling upscaling and priorities"""
        if not any(tile_datas):
            return None

        # Sparse tiles: skip this tile if no source has a native tile at the target zoom
        # with actual (non-all-NaN) data.  A native tile where every pixel has been masked
        # out is treated the same as having no data — the result would be identical to what
        # the client produces by overzooming from the highest available lower-zoom tile, so
        # there is no point storing it.  Only tiles where at least one source contributes
        # real pixels (land, coast, or bathymetry at native resolution) are written.
        if self.sparse_tiles:
            has_native_with_data = any(
                td is not None
                and td.source_zoom == target_tile.z
                and not np.all(np.isnan(td.data))
                for td in tile_datas
            )
            if not has_native_with_data:
                return None  # Skip — client can overzoom from a lower-zoom tile

        bounds = mercantile.bounds(target_tile)

        # Use the tile size of the first tile, or the default if no primary tile
        tile_size = self.default_tile_size
        if tile_datas[0] is not None and 'width' in tile_datas[0].meta and 'height' in tile_datas[0].meta:
            tile_size = tile_datas[0].meta['width']

        target_transform = rasterio.transform.from_bounds(
            bounds.west, bounds.south, bounds.east, bounds.north,
            tile_size, tile_size
        )

        result = None

        for i, tile_data in enumerate(tile_datas):
            if tile_data is not None:
                resampled_data = self._resample_if_needed(tile_data, target_tile, target_transform, tile_size)

                weight = self._clip_weights(i, target_tile, tile_size)

                # height_adjustment is already applied in _decode_tile, before
                # masking, which is the only place it can go: mask_values are
                # compared against raw decoded heights, so shifting first would
                # stop them matching. Applying it again here doubled it.
                if result is None:
                    result = resampled_data
                    # Nothing underneath to blend with, so a weight can only
                    # say whether this pixel is there at all.
                    if weight is not None:
                        result = np.where(weight > 0, result, np.nan)
                elif weight is None:
                    mask = ~np.isnan(resampled_data)
                    if np.any(mask):
                        result[mask] = resampled_data[mask]
                else:
                    result = self._blend(result, resampled_data, weight)

        # Nothing survived the merge, so there is no tile worth writing.
        #
        # This ran after the output_nodata substitution below, where it could
        # never fire: by then every pixel holds a real value. It is unreachable
        # from here too, because has_native_with_data above already returned for
        # the only case that produces an all-NaN result -- but that guard asks a
        # stricter question ("is any source native here?") than this one, so the
        # two are not interchangeable and this belongs before the substitution
        # rather than after it.
        if self.sparse_tiles and result is not None and np.all(np.isnan(result)):
            return None

        # Replace NaN values (original nodata) with the output_nodata value.
        if result is not None and self.output_nodata is not None:
            result[np.isnan(result)] = self.output_nodata

        return result

    def _clip_weights(self, index: int, target_tile: mercantile.Tile, tile_size: int):
        """
        How much of a source counts, per pixel, for the tile being built.

        Parameters
        ----------
        index: int
            Which source. tile_datas is built in self.sources order.
        target_tile: mercantile.Tile
            The tile being built.
        tile_size: int
            Pixels per side.

        Returns
        -------
        np.ndarray or None
            Weights in 0..1, or None where the source is not clipped at all --
            which is the common case and worth not allocating for.
        """
        if index >= len(self.sources):
            return None
        source = self.sources[index]
        cutline = load_cutline(
            getattr(source, "cutline", None), getattr(source, "bounds", None)
        )
        if cutline is None:
            return None
        return cutline.weights(
            target_tile, tile_size, feather_pixels(source, target_tile, tile_size)
        )

    @staticmethod
    def _blend(under: np.ndarray, over: np.ndarray, weight: np.ndarray) -> np.ndarray:
        """
        Mix a clipped source into what is already there.

        A weight of 1 takes the pixel outright, as an unclipped source does; 0
        leaves what is underneath; between them the two are mixed. Only where
        there is something underneath to mix with -- a partly-weighted source
        over a hole stands alone, because fading into nothing would erode it by
        the width of its own feather exactly where it is the only thing
        covering that ground.

        Parameters
        ----------
        under: np.ndarray
            What the merge has so far.
        over: np.ndarray
            This source's heights, NaN where it has none.
        weight: np.ndarray
            0 to 1, per pixel.

        Returns
        -------
        np.ndarray
            The merged heights.
        """
        share = np.where(np.isnan(over), 0.0, weight)
        alone = np.isnan(under) | (share >= 1.0)
        with np.errstate(invalid="ignore"):
            mixed = under * (1.0 - share) + over * share
        return np.where(
            share <= 0.0, under, np.where(alone, over, mixed)
        )

    def _resample_if_needed(self, tile_data: TileData, target_tile: mercantile.Tile, target_transform, tile_size) -> np.ndarray:
        """Resample tile data if source zoom differs from target"""
        #print(f"_resample_if_needed called with tile_data: , target_tile: ")
        if tile_data.source_zoom != target_tile.z:
            zoom_diff = abs(target_tile.z - tile_data.source_zoom)
            
            #Scale the blur based on the zoom difference
            dynamic_sigma = self.gaussian_blur_sigma * (zoom_diff)
            source_tile = mercantile.Tile(x=target_tile.x // (2**(target_tile.z - tile_data.source_zoom)),
                                            y=target_tile.y // (2**(target_tile.z - tile_data.source_zoom)),
                                            z=tile_data.source_zoom
                                            )
            source_bounds = mercantile.bounds(source_tile)

            
            
            x_offset = (target_tile.x % (2**(target_tile.z - tile_data.source_zoom)))
            y_offset = (target_tile.y % (2**(target_tile.z - tile_data.source_zoom)))
            
            #Determine the sub region bounds.
            sub_region_width = (source_bounds.east - source_bounds.west) / (2**(target_tile.z - tile_data.source_zoom))
            sub_region_height = (source_bounds.north - source_bounds.south) / (2**(target_tile.z - tile_data.source_zoom))

            sub_region_west = source_bounds.west + (x_offset * sub_region_width)
            sub_region_south = source_bounds.south + (y_offset * sub_region_height)
            sub_region_east = sub_region_west + sub_region_width
            sub_region_north = sub_region_south + sub_region_height

            # The blur reads a neighbourhood and this reprojects one tile, so
            # without a border the two tiles either side of a boundary compute
            # it from different data and step apart -- a faint grid at tile
            # boundaries under a hillshade. The pixels are already in hand: the
            # parent tile covers this sub-region and everything around it, so
            # the destination window is widened by the blur's own reach and the
            # border is cropped off afterwards.
            margin = blur_margin(dynamic_sigma, tile_size)
            side = tile_size + margin * 2
            grown = grow_bounds(
                sub_region_west, sub_region_south, sub_region_east, sub_region_north,
                margin, tile_size,
            )
            sub_region_transform = rasterio.transform.from_bounds(*grown, side, side)
            
            with rasterio.io.MemoryFile() as memfile:
                with memfile.open(**tile_data.meta) as src:
                    
                    # NaN rather than zeros, and declared as the nodata on
                    # both sides. Zeros would be read as sea level everywhere
                    # the border runs off the parent tile, and a resampler not
                    # told what nodata is averages it into the neighbours --
                    # which is how a masked coastline grew by the width of the
                    # kernel at every zoom it was upscaled through.
                    dst_data = np.full((1, side, side), np.nan, dtype=np.float32)
                    reproject(
                        source=tile_data.data,
                        destination=dst_data,
                        src_transform=tile_data.meta['transform'],
                        src_crs=tile_data.meta['crs'],
                        src_nodata=np.nan,
                        dst_transform=sub_region_transform,
                        dst_crs=tile_data.meta['crs'],
                        dst_nodata=np.nan,
                        resampling=self.resampling
                    )
                    blurred_data = crop_margin(
                        smooth(dst_data, dynamic_sigma), tile_size, margin
                    )


                    if blurred_data.ndim == 3:
                        return blurred_data[0]
                    else:
                        return blurred_data
        if tile_data.data.ndim == 3:
            return tile_data.data[0]
        else:
            return tile_data.data

    def process_tile(self, tile: mercantile.Tile, source_conns: Dict[Path, sqlite3.Connection], write_queue: Queue) -> None:
        """Process a single tile, merging data from multiple sources"""
        #print(f"process_tile called with tile: ")
        try:
            # Extract tiles from all sources
            self.logger.debug(f"Start process tile  {tile.z}/{tile.x}/{tile.y}")
            tile_datas = [self._extract_tile(source, tile.z, tile.x, tile.y, source_conns, i) for i, source in enumerate(self.sources)]
            self.logger.debug(f"tile datas: {len(tile_datas)}")

            if not any(tile_datas):
                self.logger.debug(f"No data found for tile {tile.z}/{tile.x}/{tile.y}")
                return

            # Merge the elevation data
            merged_elevation = self._merge_tiles(tile_datas, tile)
            
            if merged_elevation is None:
                self.logger.debug(f"No merged elevation for {tile.z}/{tile.x}/{tile.y}")
                return
            
            # Encode using output format and save
            rgb_data = ImageEncoder.data_to_rgb(
                merged_elevation,
                self.output_encoding,
                0.1,
                base_val=-10000,
                factors=self.output_encoding_factors
            )
            image_bytes = ImageEncoder.save_rgb_to_bytes(rgb_data, self.output_image_format, self.default_tile_size)
            
            logging.debug(f"image_bytes {len(image_bytes)}")
            write_queue.put((tile, image_bytes))
            self.logger.info(f"Successfully processed tile {tile.z}/{tile.x}/{tile.y}")
        except Exception as e:
            self.logger.error(f"Error processing tile {tile.z}/{tile.x}/{tile.y}: {e}")
            raise

    def process_zoom_level(self, zoom: int, verbose):
        """Process all tiles for a given zoom level in parallel"""
        self.logger.info(f"Processing zoom level ")
        source_conns = {}
        for s in self.sources:
            source_conns[s.path] = sqlite3.connect(s.path)
        
        # Get list of tiles to process
        tiles = self._get_tiles_for_zoom(zoom, source_conns)
        self.logger.info(f"Found {len(tiles)} tiles to process")

        # Create task tuples with all necessary data
        tasks = [
            (
                tile,
                # The sources themselves rather than a tuple of their fields.
                # That tuple had to be kept in step by hand at both ends and
                # was not: encoding_factors never reached the workers, so a
                # custom-encoded source failed in every one of them.
                list(self.sources),
                self.output_path,
                self.output_encoding.value,
                self.output_nodata,
                self.resampling,
                self.sparse_tiles,
                self.output_image_format.value,
                verbose
            )
            for tile in tiles
        ]

        # Process tiles in parallel using the standalone function
        with multiprocessing.Pool(self.processes) as pool:
            for _ in pool.imap_unordered(
                process_tile_task,
                tasks,
                chunksize=1
            ):
                pass
        for conn in source_conns.values():
            if conn:
                conn.close()

    def _get_tiles_for_zoom(self, zoom: int, source_conns: Dict[Path, sqlite3.Connection]) -> List[mercantile.Tile]:
        tiles = set()
        
        if self.bounds is not None:
            w,s,e,n = self.bounds
            print(f" West:{w} North: {n} East: {e} South: {s}")
            for x, y in _tile_range(mercantile.tile(w, n, zoom), mercantile.tile(e, s, zoom)):
                y = int(math.pow(2, zoom)) - y - 1
                tiles.add(mercantile.Tile(x=x, y=y, z=zoom))
        else:
            # Get tiles from the specified source, or the last one if it does not exist
            if self.bounds_source is not None and 0 <= self.bounds_source < len(self.sources):
                source = self.sources[self.bounds_source]
            else:
                source = self.sources[-1]
            conn = source_conns[source.path]
            cursor = conn.cursor()
            cursor.execute(
                'SELECT DISTINCT tile_column, tile_row FROM tiles WHERE zoom_level = ?',
                (zoom,)
            )
            rows = cursor.fetchall()
            
            if not rows:
                self.logger.warning(f"No tiles found for zoom level {zoom} in source {source.path}")
            else:
                #self.logger.debug(f"Rows fetched for zoom level : ")
                for row in rows:
                    if isinstance(row, tuple) and len(row) == 2:
                        x, y = row
                        tiles.add(mercantile.Tile(x=x, y=y, z=zoom))
                    else:
                        self.logger.warning(f"Skipping invalid row: {row}")
        
        return list(tiles)


    def get_max_zoom_level(self) -> int:
        """Get the maximum zoom level from the last source"""
        # Get tiles from the specified source, or the last one if it does not exist
        if self.bounds_source is not None and 0 <= self.bounds_source < len(self.sources):
            source = self.sources[self.bounds_source]
        else:
            source = self.sources[-1]

        with sqlite3.connect(source.path) as conn:
             cursor = conn.cursor()
             cursor.execute("SELECT MAX(zoom_level) FROM tiles")
             result = cursor.fetchone()
             max_zoom = result[0] if result and result[0] is not None else 0
             return max_zoom

    def process_all(self, min_zoom: int = 0, verbose = False):
        """Process all zoom levels"""
        max_zoom = self.max_zoom if self.max_zoom is not None else self.get_max_zoom_level()
        self.logger.info(f"Processing zoom levels {min_zoom} to {max_zoom}")

        with MBTilesDatabase(self.output_path) as db:
             db.add_bounds_center_metadata(self.bounds, self.min_zoom, max_zoom, self.output_encoding.value, self.output_image_format.value, "Merged Terrain")


        for zoom in range(min_zoom, max_zoom + 1):
             self.process_zoom_level(zoom, verbose)

        self.logger.info("Completed processing all zoom levels")

@retry(attempts=5, base_delay=0.5, max_delay=5)
def process_tile_task(task_tuple: tuple) -> None:
    """Standalone function for processing tiles that can be pickled"""
    tile, source_configs, output_path, output_encoding, output_nodata, resampling, sparse_tiles, output_format, verbose = task_tuple
    # Configure logging for each process
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print(f"process_tile_task started for tile {tile.z}/{tile.x}/{tile.y}")

    source_conns = {}
    sources = []
    db = None
    try:
        # Reconstruct MBTilesSource objects and create connections
        for source in source_configs:
            sources.append(source)
            source_conns[source.path] = sqlite3.connect(source.path)

        # create instance
        merger_instance = TerrainRGBMerger(sources, output_path, output_encoding=EncodingType(output_encoding), output_nodata = output_nodata, resampling=resampling, sparse_tiles = sparse_tiles, output_image_format=ImageFormat(output_format))

        # Open database connection for the entire task
        with MBTilesDatabase(output_path) as db:
            # Extract tiles from all sources
            tile_datas = []
            for i, source in enumerate(sources):
                tile_data = merger_instance._extract_tile(source, tile.z, tile.x, tile.y, source_conns, i)
                tile_datas.append(tile_data)

            if not any(tile_datas):
                if verbose:
                    print(f"No tile data for {tile.z}/{tile.x}/{tile.y}")
                return

            # Merge the elevation data
            merged_elevation = merger_instance._merge_tiles(tile_datas, tile)

            if merged_elevation is None:
                if verbose:
                    print(f"No merged elevation {tile.z}/{tile.x}/{tile.y}")
                return

            # Encode using output format
            rgb_data = ImageEncoder.data_to_rgb(
                merged_elevation,
                output_encoding,
                0.1,
                base_val=-10000
            )
            image_bytes = ImageEncoder.save_rgb_to_bytes(rgb_data, output_format)
            if verbose:
                print(f"image_bytes {len(image_bytes)}")
            # Write to output database
            db.insert_tile_with_retry([tile.x, tile.y, tile.z], image_bytes)


    except Exception as e:
        print(f"Error processing tile {tile.z}/{tile.x}/{tile.y}: {e}")
        raise
    finally:
        # Clean up connections
        for conn in source_conns.values():
            if conn:
                conn.close()

def _tile_range(start: mercantile.Tile, stop: mercantile.Tile):
    for x in range(start.x, stop.x + 1):
        for y in range(start.y, stop.y + 1):
            yield x, y
