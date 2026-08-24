"""PMTiles output for rio-rgbify.

Tiles go straight into a PMTiles archive rather than through an MBTiles on the
way. `pmtiles.writer.Writer` already streams tile bytes to a temporary file and
keeps only one directory entry per tile in memory -- the same shape as the
writer in pmtiles-swarm and as tippecanoe's -- so the archive costs what its
tile data costs and no more.

What it does not do is sort. An archive is *clustered* only when tiles arrive
in ascending tile id, and a clustered archive is the one a range-requesting
reader wants, so both callers order their work by tile id before handing it
over. `PMTilesWriter.clustered` reports whether that held.
"""
from __future__ import annotations

import datetime
import logging
import os
import traceback
from pathlib import Path
from typing import List, Optional

from pmtiles.reader import MmapSource, Reader, deserialize_directory
from pmtiles.tile import (
    Compression, TileType, serialize_header, tileid_to_zxy, zxy_to_tileid,
)
from pmtiles.writer import Writer

logger = logging.getLogger(__name__)

PMTILES_SUFFIX = ".pmtiles"

# The image formats this package writes, and one alias each.
_TILE_TYPES = {
    "png": TileType.PNG,
    "webp": TileType.WEBP,
    "jpg": TileType.JPEG,
    "jpeg": TileType.JPEG,
}


def is_pmtiles_path(path) -> bool:
    """True when this path names a PMTiles archive rather than an MBTiles one."""
    return str(path).lower().endswith(PMTILES_SUFFIX)


def flip_y(zoom: int, y: int) -> int:
    """TMS y <-> XYZ y. The conversion is its own inverse."""
    return (1 << zoom) - 1 - y


# The tile id an archive will store a tile at, for ordering work by. Two
# functions rather than one with a flag: this package holds tiles at a TMS y
# in the merger and at an XYZ y in the tiler, and a sort that guesses wrong
# still returns a number -- just not the one the archive uses, which shows up
# only as a quietly unclustered archive.

def tile_id_from_tms(z: int, x: int, tms_y: int) -> int:
    return zxy_to_tileid(z, x, flip_y(z, tms_y))


def tile_id_from_xyz(z: int, x: int, xyz_y: int) -> int:
    return zxy_to_tileid(z, x, xyz_y)


class PMTilesReader:
    """Read side of a PMTiles source, in the TMS y the merger works in.

    Only the two things the merger asks an MBTiles connection for: one tile,
    and which tiles exist at a zoom. `close` matches sqlite3.Connection so both
    kinds of source can be cleaned up the same way.
    """

    def __init__(self, path):
        self.path = str(path)
        self._file = open(self.path, "rb")
        self._get_bytes = MmapSource(self._file)
        self._reader = Reader(self._get_bytes)

    def header(self) -> dict:
        return self._reader.header()

    def metadata(self) -> dict:
        return self._reader.metadata()

    def get_tile(self, zoom: int, x: int, tms_y: int) -> Optional[bytes]:
        """Raw tile bytes at TMS coordinates, or None."""
        return self._reader.get(zoom, x, flip_y(zoom, tms_y))

    def tiles_at_zoom(self, zoom: int):
        """Yield (x, tms_y) for every tile at `zoom`.

        PMTiles has no index by zoom, so this walks the directories -- but only
        the directories, and only the ones whose tile ids reach into the level
        asked for. `pmtiles.reader.all_tiles` reads each tile's *bytes* as it
        goes, which for the merger would mean reading a whole planet archive
        once per zoom level to learn a set of coordinates.
        """
        # Tile ids are laid out zoom by zoom, so a level is one contiguous run.
        lo = zxy_to_tileid(zoom, 0, 0)
        hi = lo + (1 << (2 * zoom))
        header = self.header()
        for tile_id in self._walk(
            header["root_offset"], header["root_length"],
            header["leaf_directory_offset"], lo, hi,
        ):
            z, x, xyz_y = tileid_to_zxy(tile_id)
            yield x, flip_y(z, xyz_y)

    def _walk(self, dir_offset: int, dir_length: int, leaf_base: int, lo: int, hi: int):
        """Tile ids in [lo, hi) held under one directory."""
        entries = deserialize_directory(self._get_bytes(dir_offset, dir_length))
        for i, entry in enumerate(entries):
            if entry.run_length > 0:
                start, end = entry.tile_id, entry.tile_id + entry.run_length
                yield from range(max(start, lo), min(end, hi))
                continue
            # run_length 0 marks a leaf directory. It holds the ids from its
            # own up to the next entry's, so a leaf entirely outside the range
            # never has to be read.
            next_id = entries[i + 1].tile_id if i + 1 < len(entries) else None
            if entry.tile_id >= hi or (next_id is not None and next_id <= lo):
                continue
            yield from self._walk(leaf_base + entry.offset, entry.length, leaf_base, lo, hi)

    def max_zoom(self) -> int:
        return self.header()["max_zoom"]

    def close(self):
        self._file.close()


class PMTilesWriter:
    """MBTilesDatabase's interface, writing a PMTiles archive.

    The methods RGBTiler and TerrainRGBMerger call on their output are the
    same either way, so neither needs to know which container it is filling.
    The archive is finalised when the context closes; leaving it by way of an
    exception removes the part-written file instead.
    """

    def __init__(self, outpath):
        self.outpath = Path(outpath)
        self._file = None
        self._writer = None
        self._header = {}
        self._metadata = {}
        self._count = 0
        # The zoom range actually written, as opposed to the one asked for.
        self._min_z = None
        self._max_z = None

    # -- Context manager ----------------------------------------------------

    def __enter__(self):
        self._file = open(self.outpath, "wb")
        self._writer = Writer(self._file)
        return self

    def __exit__(self, exc_t, exc_v, tb):
        try:
            if exc_t:
                traceback.print_exc()
                self._abandon()
                return
            if not self._count:
                # finalize() reads entry[0] for the zoom range and an empty run
                # reaches it as an IndexError about a list index. Say what
                # actually went wrong instead.
                self._abandon()
                raise ValueError(
                    f"No tiles were written, so there is nothing to put in "
                    f"{self.outpath}. Check the zoom range and bounds against "
                    f"the source."
                )
            logger.info(f"Finalising {self._count} tiles into {self.outpath}")
            # The zoom range the archive really covers, which is what a client
            # reads the header to find out.
            self._metadata["minzoom"] = str(self._min_z)
            self._metadata["maxzoom"] = str(self._max_z)
            self._writer.finalize(self._header, self._metadata)
            self._correct_zoom_range()
            self._file.close()
            logger.info(f"Wrote {self.outpath}")
        finally:
            self._writer = None
            self._file = None

    def _correct_zoom_range(self):
        """Rewrite the header if finalize() understated the zoom range.

        pmtiles 3.7.0 takes the range from the first and last directory
        entries' tile ids, and the last entry's id is where its *run* starts.
        A run of identical tiles that crosses a zoom boundary therefore leaves
        max_zoom naming a zoom shallower than the deepest tile in the file, and
        a client reading the header never asks past it. Terrain is the case
        that hits it: an ocean tile is byte-identical over huge areas, so the
        runs are long.

        finalize() fills the header dict in place, so everything else in it is
        already right; only the two zoom bytes are rewritten, over the 127
        bytes at the front of the file.
        """
        if (self._header["min_zoom"], self._header["max_zoom"]) == (self._min_z, self._max_z):
            return
        logger.debug(
            f"correcting header zoom range from "
            f"{self._header['min_zoom']}-{self._header['max_zoom']} to "
            f"{self._min_z}-{self._max_z}"
        )
        self._header["min_zoom"] = self._min_z
        self._header["max_zoom"] = self._max_z
        self._file.seek(0)
        self._file.write(serialize_header(self._header))

    def _abandon(self):
        """Drop the temp buffer and the part-written output."""
        # finalize() is what normally closes the writer's spool file, so on
        # this path it has to be closed by hand or it survives until GC.
        try:
            self._writer.tile_f.close()
        except Exception:
            pass
        self._file.close()
        if self.outpath.exists():
            os.unlink(self.outpath)

    # -- MBTilesDatabase interface ------------------------------------------

    def commit(self):
        """Accepted for interface compatibility; there is nothing to commit."""

    def add_metadata(self, metadata: dict):
        """Merge keys into the archive's JSON metadata.

        Values are stringified, which is what a reader gets back from an
        MBTiles metadata table -- the column is declared `text` -- so the same
        tileset described either way describes itself the same.

        Booleans are the exception and stay booleans. JSON has the type, and a
        consumer that tests one for truth reads the string "false" as true.
        """
        self._metadata.update({
            k: v if isinstance(v, bool) else str(v) for k, v in metadata.items()
        })

    def add_bounds_center_metadata(self, bounds: Optional[List[float]], min_zoom: int,
                                   max_zoom: int, encoding: str, format: str,
                                   name: str = "Terrain", description: Optional[str] = None,
                                   attribution: Optional[str] = None,
                                   sparse: Optional[bool] = None):
        """Build the header, and the metadata that mirrors MBTilesDatabase's.

        Worth getting right at the first attempt for a PMTiles: the metadata
        sits between the root directory and the leaf directories, so changing
        its length moves every offset after it and the archive has to be
        written again to say something different here.
        """
        w, s, e, n = bounds if bounds is not None else (-180.0, -90.0, 180.0, 90.0)
        center_lon, center_lat = (w + e) / 2, (n + s) / 2
        center_zoom = int((min_zoom + max_zoom) / 2)

        self._header = {
            "tile_type": _TILE_TYPES.get(str(format).lower(), TileType.UNKNOWN),
            # Terrain RGB is a lossless image already. Gzipping it buys close
            # to nothing and costs every reader an inflate per tile.
            "tile_compression": Compression.NONE,
            "min_zoom": min_zoom,
            "max_zoom": max_zoom,
            "min_lon_e7": int(w * 10_000_000),
            "min_lat_e7": int(s * 10_000_000),
            "max_lon_e7": int(e * 10_000_000),
            "max_lat_e7": int(n * 10_000_000),
            "center_zoom": center_zoom,
            "center_lon_e7": int(center_lon * 10_000_000),
            "center_lat_e7": int(center_lat * 10_000_000),
        }

        metadata = {
            "format": format,
            "name": name,
            "description": description or f"Created {datetime.datetime.now()}",
            # Spec v3 section 5: if `version` is present it MUST be a valid
            # SemVer 2.0.0 string, and "1" is not one.
            "version": "1.0.0",
            "type": "baselayer",
            "minzoom": min_zoom,
            "maxzoom": max_zoom,
            # The one thing a terrain consumer cannot read off the pixels.
            "encoding": encoding,
            "bounds": f"{w},{s},{e},{n}",
            "center": f"{center_lon},{center_lat},{center_zoom}",
        }
        if attribution:
            metadata["attribution"] = attribution
        if sparse is not None:
            # A real JSON boolean rather than the string the MBTiles column is
            # limited to. This one is read by JavaScript, and "false" is a
            # non-empty string: a consumer writing `metadata.sparse ?? default`
            # would take the string and read it as true, which is the opposite
            # of what the archive says.
            metadata["sparse"] = sparse
        self.add_metadata(metadata)

    def insert_tile_with_retry(self, tile: List[int], contents: bytes,
                               use_inverse_y: bool = False):
        """Write one tile. `tile` is [x, y, z]; y is TMS unless flipped in.

        Named for the MBTilesDatabase method it stands in for, which retries
        around a locked database. Nothing here can be locked -- one process
        holds the archive -- so there is nothing to retry.
        """
        x, y, z = tile
        # PMTiles addresses tiles by XYZ y. `use_inverse_y` is the caller
        # saying it already has one, the way MBTilesDatabase reads the flag.
        xyz_y = y if use_inverse_y else flip_y(z, y)
        self._writer.write_tile(zxy_to_tileid(z, x, xyz_y), contents)
        self._count += 1
        self._min_z = z if self._min_z is None else min(self._min_z, z)
        self._max_z = z if self._max_z is None else max(self._max_z, z)

    # -- Reporting ----------------------------------------------------------

    @property
    def clustered(self) -> bool:
        """Whether the tiles arrived in ascending tile id.

        False still produces a valid archive, but a reader fetching a range of
        it gets tiles it did not want, so both callers sort to keep this True.
        """
        return self._writer.clustered if self._writer else True

    @property
    def tile_count(self) -> int:
        return self._count
