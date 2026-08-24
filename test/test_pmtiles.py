"""
Tests for PMTiles output and PMTiles sources.

Three things can go wrong here independently of the merge maths: the y
convention across the MBTiles/PMTiles boundary, the header and metadata that
consumers read, and whether the archive is clustered. The last one is the
quiet failure -- an unclustered archive is valid and reads correctly, and only
shows up as a range request fetching tiles nobody asked for.
"""

import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from click.testing import CliRunner

from rio_rgbify.database import MBTilesDatabase
from rio_rgbify.image import ImageEncoder, ImageFormat
from rio_rgbify.merger import (
    EncodingType,
    MBTilesSource,
    PMTilesSource,
    TerrainRGBMerger,
    _open_source,
    _read_tile,
)
from rio_rgbify.pmtiles_writer import (
    PMTilesReader,
    PMTilesWriter,
    flip_y,
    is_pmtiles_path,
    tile_id_from_tms,
    tile_id_from_xyz,
)
from rio_rgbify.mbtiler import RGBTiler
from rio_rgbify.scripts.cli import main_group as cli, rgbify

from pmtiles.tile import Compression, TileType


TILE_SIZE = 256
WORLD = (-180.0, -85.05, 180.0, 85.05)

ELEV_SRC = os.path.join(os.path.dirname(__file__), "fixtures", "elev.tif")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tile_bytes(elev=100.0, encoding="mapbox", fmt=ImageFormat.PNG):
    """Encode a flat elevation to Terrain RGB image bytes."""
    data = np.full((TILE_SIZE, TILE_SIZE), elev, dtype=np.float32)
    rgb = ImageEncoder.data_to_rgb(data, encoding, 0.1, base_val=-10000)
    return ImageEncoder.save_rgb_to_bytes(rgb, fmt, TILE_SIZE)


def _make_mbtiles(path, tiles, encoding="mapbox", bounds=WORLD):
    """Write an MBTiles at *path*. `tiles` is {(z, x, tms_y): elevation}."""
    zooms = [z for z, _, _ in tiles]
    with MBTilesDatabase(str(path)) as db:
        db.add_bounds_center_metadata(
            list(bounds), min(zooms), max(zooms), encoding, "png", "test"
        )
        for (z, x, tms_y), elev in tiles.items():
            db.insert_tile_with_retry([x, tms_y, z], _tile_bytes(elev, encoding))


def _make_pmtiles(path, tiles, encoding="mapbox", bounds=WORLD, sort=True):
    """Write a PMTiles at *path*. `tiles` is {(z, x, tms_y): elevation}."""
    zooms = [z for z, _, _ in tiles]
    keys = list(tiles)
    if sort:
        keys.sort(key=lambda k: tile_id_from_tms(*k))
    with PMTilesWriter(path) as w:
        w.add_bounds_center_metadata(
            list(bounds), min(zooms), max(zooms), encoding, "png", "test"
        )
        for key in keys:
            z, x, tms_y = key
            w.insert_tile_with_retry([x, tms_y, z], _tile_bytes(tiles[key], encoding))


def _elevation_of(tile_bytes, encoding="mapbox"):
    """Mean elevation a tile decodes to, in the encoding it was written in."""
    rgb = np.array(Image.open(io.BytesIO(tile_bytes)).convert("RGB")).astype(np.float64)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    if encoding == "terrarium":
        return float((r * 256 + g + b / 256 - 32768).mean())
    return float((-10000 + ((r * 65536 + g * 256 + b) * 0.1)).mean())


# ---------------------------------------------------------------------------
# Coordinates and path sniffing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("out.pmtiles", True),
    ("out.PMTiles", True),
    (Path("dir/out.pmtiles"), True),
    ("out.mbtiles", False),
    ("out.pmtiles.mbtiles", False),
    ("pmtiles", False),
])
def test_is_pmtiles_path(path, expected):
    assert is_pmtiles_path(path) is expected


def test_flip_y_is_its_own_inverse():
    for z in range(0, 6):
        for y in range(1 << z):
            assert flip_y(z, flip_y(z, y)) == y


def test_tile_id_conventions_differ_where_they_should():
    """The two sort keys must disagree, or one of them is silently wrong."""
    # z=2, x=1: TMS y=0 is XYZ y=3.
    assert tile_id_from_tms(2, 1, 0) == tile_id_from_xyz(2, 1, 3)
    assert tile_id_from_tms(2, 1, 0) != tile_id_from_xyz(2, 1, 0)
    # z=0 has one tile, so the two agree there and prove nothing.
    assert tile_id_from_tms(0, 0, 0) == tile_id_from_xyz(0, 0, 0)


def test_tile_ids_rise_with_zoom():
    """process_zoom_level sorts within a level and relies on this across them."""
    per_zoom = [
        [tile_id_from_tms(z, x, y) for x in range(1 << z) for y in range(1 << z)]
        for z in range(5)
    ]
    for lower, higher in zip(per_zoom, per_zoom[1:]):
        assert max(lower) < min(higher)


# ---------------------------------------------------------------------------
# PMTilesWriter
# ---------------------------------------------------------------------------

class TestPMTilesWriter:

    def test_writes_a_readable_archive(self, tmp_path):
        out = tmp_path / "out.pmtiles"
        _make_pmtiles(out, {(0, 0, 0): 100.0, (1, 0, 0): 200.0, (1, 1, 1): 300.0})

        assert out.exists() and out.stat().st_size > 0
        reader = PMTilesReader(out)
        try:
            assert reader.header()["addressed_tiles_count"] == 3
        finally:
            reader.close()

    def test_no_mbtiles_is_written_on_the_way(self, tmp_path):
        """The archive is the only file the run leaves behind."""
        out = tmp_path / "out.pmtiles"
        _make_pmtiles(out, {(0, 0, 0): 100.0, (1, 0, 0): 200.0})
        assert [p.name for p in tmp_path.iterdir()] == ["out.pmtiles"]

    def test_tile_y_survives_the_round_trip(self, tmp_path):
        """A tile written at a TMS y comes back at the same TMS y.

        MBTiles counts y from the south and PMTiles from the north. A single
        missing flip puts every tile in the wrong hemisphere without failing
        anything, so this checks two tiles that are each other's flip.
        """
        out = tmp_path / "out.pmtiles"
        elevations = {(2, 1, 0): 100.0, (2, 1, 3): 900.0}
        _make_pmtiles(out, elevations)

        reader = PMTilesReader(out)
        try:
            for (z, x, tms_y), elev in elevations.items():
                got = reader.get_tile(z, x, tms_y)
                assert got is not None, f"no tile at {z}/{x}/{tms_y}"
                assert _elevation_of(got) == pytest.approx(elev, abs=0.5)
        finally:
            reader.close()

    def test_use_inverse_y_means_the_caller_has_an_xyz_y(self, tmp_path):
        out = tmp_path / "out.pmtiles"
        with PMTilesWriter(out) as w:
            w.add_bounds_center_metadata(list(WORLD), 2, 2, "mapbox", "png")
            # XYZ y=3 at z=2 is TMS y=0
            w.insert_tile_with_retry([1, 3, 2], _tile_bytes(500.0), use_inverse_y=True)

        reader = PMTilesReader(out)
        try:
            assert reader.get_tile(2, 1, 0) is not None
            assert reader.get_tile(2, 1, 3) is None
        finally:
            reader.close()

    def test_sorted_input_is_clustered(self, tmp_path):
        out = tmp_path / "out.pmtiles"
        _make_pmtiles(out, {(2, x, y): 10.0 * (x + y) for x in range(4) for y in range(4)})
        reader = PMTilesReader(out)
        try:
            assert reader.header()["clustered"] is True
        finally:
            reader.close()

    def test_unsorted_input_is_reported_unclustered(self, tmp_path):
        """The guard the callers' sorting exists to satisfy.

        Feeding tiles in a deliberately wrong order has to be *detected*,
        otherwise a caller that forgot to sort would look identical to one
        that did and the clustered assertions elsewhere would prove nothing.
        """
        out = tmp_path / "out.pmtiles"
        tiles = {(2, x, y): 10.0 * (x + y + 1) for x in range(4) for y in range(4)}
        with PMTilesWriter(out) as w:
            w.add_bounds_center_metadata(list(WORLD), 2, 2, "mapbox", "png")
            for key in sorted(tiles, key=lambda k: -tile_id_from_tms(*k)):
                z, x, tms_y = key
                w.insert_tile_with_retry([x, tms_y, z], _tile_bytes(tiles[key]))
            assert w.clustered is False

        reader = PMTilesReader(out)
        try:
            assert reader.header()["clustered"] is False
            # Still a correct archive, just not one worth range-requesting.
            for (z, x, tms_y), elev in tiles.items():
                assert _elevation_of(reader.get_tile(z, x, tms_y)) == pytest.approx(elev, abs=0.5)
        finally:
            reader.close()

    def test_header_and_metadata(self, tmp_path):
        out = tmp_path / "out.pmtiles"
        _make_pmtiles(out, {(0, 0, 0): 10.0, (1, 0, 0): 20.0},
                      encoding="terrarium", bounds=(-10.0, -5.0, 10.0, 5.0))

        reader = PMTilesReader(out)
        try:
            header, meta = reader.header(), reader.metadata()
        finally:
            reader.close()

        assert header["tile_type"] == TileType.PNG
        # Terrain RGB is lossless already; gzipping a PNG buys nothing and
        # costs every reader an inflate per tile.
        assert header["tile_compression"] == Compression.NONE
        assert (header["min_zoom"], header["max_zoom"]) == (0, 1)
        assert header["min_lon_e7"] == -100000000
        assert header["max_lat_e7"] == 50000000
        assert header["center_zoom"] == 0

        # The encoding is the one thing a terrain consumer cannot guess from
        # the pixels, so it has to survive into the PMTiles metadata.
        assert meta["encoding"] == "terrarium"
        assert meta["format"] == "png"
        assert meta["bounds"] == "-10.0,-5.0,10.0,5.0"

    def test_metadata_values_are_strings(self, tmp_path):
        """What an MBTiles metadata table hands back, its column being `text`.

        A tileset described one way in an MBTiles and another in a PMTiles is
        a difference consumers have to special-case.
        """
        out = tmp_path / "out.pmtiles"
        _make_pmtiles(out, {(0, 0, 0): 10.0, (1, 0, 0): 20.0})
        reader = PMTilesReader(out)
        try:
            meta = reader.metadata()
        finally:
            reader.close()
        assert all(isinstance(v, str) for v in meta.values()), meta
        assert meta["minzoom"] == "0" and meta["maxzoom"] == "1"

    def test_webp_sets_the_webp_tile_type(self, tmp_path):
        out = tmp_path / "out.pmtiles"
        with PMTilesWriter(out) as w:
            w.add_bounds_center_metadata(list(WORLD), 0, 0, "mapbox", "webp")
            w.insert_tile_with_retry([0, 0, 0], _tile_bytes(fmt=ImageFormat.WEBP))

        reader = PMTilesReader(out)
        try:
            assert reader.header()["tile_type"] == TileType.WEBP
            assert reader.metadata()["format"] == "webp"
        finally:
            reader.close()

    def test_identical_tiles_are_stored_once(self, tmp_path):
        out = tmp_path / "out.pmtiles"
        _make_pmtiles(out, {(2, x, y): 100.0 for x in range(4) for y in range(4)})
        reader = PMTilesReader(out)
        try:
            header = reader.header()
            assert header["addressed_tiles_count"] == 16
            assert header["tile_contents_count"] == 1
        finally:
            reader.close()

    def test_no_tiles_raises_rather_than_writing_a_broken_archive(self, tmp_path):
        out = tmp_path / "out.pmtiles"
        with pytest.raises(ValueError, match="No tiles were written"):
            with PMTilesWriter(out) as w:
                w.add_bounds_center_metadata(list(WORLD), 0, 1, "mapbox", "png")
        assert not out.exists()

    def test_a_failed_run_leaves_no_part_written_archive(self, tmp_path):
        out = tmp_path / "out.pmtiles"
        with pytest.raises(RuntimeError):
            with PMTilesWriter(out) as w:
                w.add_bounds_center_metadata(list(WORLD), 0, 0, "mapbox", "png")
                w.insert_tile_with_retry([0, 0, 0], _tile_bytes())
                raise RuntimeError("boom")
        assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# PMTiles as a merge source
# ---------------------------------------------------------------------------

class TestPMTilesSource:

    def test_inherits_every_mbtiles_source_option(self):
        """The two configs must not drift; PMTilesSource adds no fields."""
        import dataclasses
        mb = {f.name for f in dataclasses.fields(MBTilesSource)}
        pm = {f.name for f in dataclasses.fields(PMTilesSource)}
        assert mb == pm

    def test_missing_file_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="does not exist"):
            PMTilesSource(path=tmp_path / "nope.pmtiles", encoding=EncodingType.MAPBOX)

    def test_reads_the_same_bytes_as_the_mbtiles_reader(self, tmp_path):
        """The same tileset in either container reads back identically."""
        tiles = {(1, 0, 0): 100.0, (1, 1, 1): 250.0}
        mb, pm = tmp_path / "s.mbtiles", tmp_path / "s.pmtiles"
        _make_mbtiles(mb, tiles)
        _make_pmtiles(pm, tiles)

        mb_conn = _open_source(MBTilesSource(path=mb, encoding=EncodingType.MAPBOX))
        pm_conn = _open_source(PMTilesSource(path=pm, encoding=EncodingType.MAPBOX))
        try:
            for (z, x, tms_y) in tiles:
                assert _read_tile(mb_conn, z, x, tms_y) == _read_tile(pm_conn, z, x, tms_y)
            # A tile neither of them holds
            assert _read_tile(pm_conn, 1, 1, 0) is None
            assert _read_tile(mb_conn, 1, 1, 0) is None
        finally:
            mb_conn.close()
            pm_conn.close()

    def test_tiles_at_zoom_is_in_tms_y(self, tmp_path):
        pm = tmp_path / "s.pmtiles"
        _make_pmtiles(pm, {(1, 0, 0): 1.0, (1, 1, 1): 2.0, (2, 0, 0): 3.0})
        reader = PMTilesReader(pm)
        try:
            assert sorted(reader.tiles_at_zoom(1)) == [(0, 0), (1, 1)]
            assert sorted(reader.tiles_at_zoom(2)) == [(0, 0)]
            assert list(reader.tiles_at_zoom(5)) == []
        finally:
            reader.close()

    def test_tiles_at_zoom_does_not_read_the_tile_data(self, tmp_path):
        """Enumerating a level must not cost a read of the whole archive.

        `pmtiles.reader.all_tiles` yields each tile's bytes as it walks, so
        using it here would read a planet archive once per zoom level just to
        learn a set of coordinates. This walks the directories instead, and
        that is only observable as how much it reads.
        """
        out = tmp_path / "big.pmtiles"
        # Distinct payloads, so nothing is deduplicated into a run and the
        # tile data is genuinely the bulk of the file.
        tiles = {}
        for z in (0, 1, 6):
            for x in range(1 << z):
                for y in range(1 << z):
                    tiles[(z, x, y)] = f"tile-{z}-{x}-{y}".encode() * 8
        with PMTilesWriter(out) as w:
            w.add_bounds_center_metadata(list(WORLD), 0, 6, "mapbox", "png")
            for key in sorted(tiles, key=lambda k: tile_id_from_tms(*k)):
                z, x, tms_y = key
                w.insert_tile_with_retry([x, tms_y, z], tiles[key])

        size = out.stat().st_size
        reader = PMTilesReader(out)
        try:
            read = []
            inner = reader._get_bytes
            reader._get_bytes = lambda off, length: (read.append(length), inner(off, length))[1]

            found = sorted(reader.tiles_at_zoom(6))
            assert found == sorted((x, y) for (z, x, y) in tiles if z == 6)
            assert sum(read) < size / 4, (
                f"read {sum(read)} of a {size} byte archive to enumerate one zoom"
            )
        finally:
            reader.close()

    def test_tiles_at_zoom_prunes_leaf_directories(self, monkeypatch, tmp_path):
        """The leaf branch of the walk, which a normal archive rarely reaches.

        A root directory is gzipped delta-encoded varints, so even a few
        hundred thousand clustered tiles fit inside the 16 KB budget and no
        leaves are written at all. Shrinking the budget is the only way to
        cover the descent -- and getting its pruning wrong would show up in
        the field, on the one kind of archive big enough to need leaves.
        """
        import pmtiles.writer as pmtiles_writer_mod
        real = pmtiles_writer_mod.optimize_directories
        monkeypatch.setattr(pmtiles_writer_mod, "optimize_directories",
                            lambda entries, budget: real(entries, 100))

        out = tmp_path / "leafy.pmtiles"
        tiles = {}
        for z in (0, 1, 5, 6):
            for x in range(1 << z):
                for y in range(1 << z):
                    tiles[(z, x, y)] = f"tile-{z}-{x}-{y}".encode() * 4
        with PMTilesWriter(out) as w:
            w.add_bounds_center_metadata(list(WORLD), 0, 6, "mapbox", "png")
            for key in sorted(tiles, key=lambda k: tile_id_from_tms(*k)):
                z, x, tms_y = key
                w.insert_tile_with_retry([x, tms_y, z], tiles[key])

        reader = PMTilesReader(out)
        try:
            assert reader.header()["leaf_directory_length"] > 0, "no leaves to prune"
            for zoom in (0, 1, 5, 6):
                assert sorted(reader.tiles_at_zoom(zoom)) ==                     sorted((x, y) for (z, x, y) in tiles if z == zoom)
            assert list(reader.tiles_at_zoom(4)) == []
        finally:
            reader.close()

    def test_merger_reads_max_zoom_from_the_header(self, tmp_path):
        pm = tmp_path / "s.pmtiles"
        _make_pmtiles(pm, {(0, 0, 0): 1.0, (1, 0, 0): 2.0, (2, 0, 0): 3.0})
        merger = TerrainRGBMerger(
            [PMTilesSource(path=pm, encoding=EncodingType.MAPBOX)],
            output_path=tmp_path / "out.mbtiles",
        )
        assert merger.get_max_zoom_level() == 2


# ---------------------------------------------------------------------------
# Merger output
# ---------------------------------------------------------------------------

class TestMergerPMTilesOutput:

    def _sources(self, tmp_path):
        mb = tmp_path / "src.mbtiles"
        _make_mbtiles(mb, {(0, 0, 0): 100.0, (1, 0, 0): 200.0, (1, 1, 1): 300.0})
        return [MBTilesSource(path=mb, encoding=EncodingType.MAPBOX)]

    def _merger(self, sources, out, **kwargs):
        return TerrainRGBMerger(
            sources, output_path=out,
            output_encoding=EncodingType.MAPBOX,
            output_image_format=ImageFormat.PNG,
            min_zoom=0, max_zoom=1, processes=1, bounds=WORLD,
            **kwargs
        )

    def test_extension_selects_the_container(self, tmp_path):
        sources = self._sources(tmp_path)
        assert self._merger(sources, tmp_path / "o.pmtiles").archive_format == "pmtiles"
        assert self._merger(sources, tmp_path / "o.mbtiles").archive_format == "mbtiles"

    def test_explicit_archive_format_wins_over_the_extension(self, tmp_path):
        merger = self._merger(self._sources(tmp_path), tmp_path / "o.mbtiles",
                              archive_format="pmtiles")
        assert merger.archive_format == "pmtiles"

    def test_process_all_writes_a_clustered_pmtiles(self, tmp_path):
        out = tmp_path / "merged.pmtiles"
        self._merger(self._sources(tmp_path), out).process_all(min_zoom=0)

        assert out.exists()
        reader = PMTilesReader(out)
        try:
            header = reader.header()
            assert header["addressed_tiles_count"] > 0
            assert header["clustered"] is True
            assert reader.metadata()["encoding"] == "mapbox"
        finally:
            reader.close()

        # No spool, no intermediate: the archive is the only thing written.
        assert [p.name for p in tmp_path.iterdir() if p.suffix != ".mbtiles"] == \
            ["merged.pmtiles"]

    def test_pmtiles_and_mbtiles_output_agree(self, tmp_path):
        """The container must not change a single pixel."""
        sources = self._sources(tmp_path)

        mb_out = tmp_path / "merged.mbtiles"
        self._merger(sources, mb_out).process_all(min_zoom=0)
        pm_out = tmp_path / "merged.pmtiles"
        self._merger(sources, pm_out).process_all(min_zoom=0)

        conn = sqlite3.connect(str(mb_out))
        try:
            rows = conn.execute(
                "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "the mbtiles run produced nothing to compare against"

        reader = PMTilesReader(pm_out)
        try:
            for z, x, tms_y, data in rows:
                assert reader.get_tile(z, x, tms_y) == data, f"differs at {z}/{x}/{tms_y}"
            assert reader.header()["addressed_tiles_count"] == len(rows)
        finally:
            reader.close()

    def test_pmtiles_source_merges_to_pmtiles_output(self, tmp_path):
        """The shape the swarm workflow uses: pmtiles in, pmtiles out."""
        src = tmp_path / "src.pmtiles"
        _make_pmtiles(src, {(0, 0, 0): 100.0, (1, 0, 0): 200.0, (1, 1, 1): 300.0})

        out = tmp_path / "merged.pmtiles"
        self._merger([PMTilesSource(path=src, encoding=EncodingType.MAPBOX)],
                     out).process_all(min_zoom=0)

        reader = PMTilesReader(out)
        try:
            assert reader.header()["addressed_tiles_count"] > 0
            got = reader.get_tile(1, 0, 0)
            assert got is not None
            assert _elevation_of(got) == pytest.approx(200.0, abs=1.0)
        finally:
            reader.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestMergeCLI:

    def _config(self, tmp_path, source_path, source_type, output_path, output_type):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({
            "output_type": output_type,
            "output_path": str(output_path),
            "output_encoding": "mapbox",
            "output_format": "png",
            "min_zoom": 0,
            "max_zoom": 1,
            "sources": [{
                "path": str(source_path),
                "source_type": source_type,
                "encoding": "mapbox",
            }],
        }))
        return cfg

    def test_pmtiles_output_type(self, tmp_path):
        src = tmp_path / "src.mbtiles"
        _make_mbtiles(src, {(0, 0, 0): 100.0, (1, 0, 0): 200.0})
        out = tmp_path / "out.pmtiles"
        cfg = self._config(tmp_path, src, "mbtiles", out, "pmtiles")

        result = CliRunner().invoke(cli, ["merge", "-c", str(cfg), "-j", "1"])
        assert result.exit_code == 0, result.output
        assert out.exists()

    def test_pmtiles_source_type(self, tmp_path):
        src = tmp_path / "src.pmtiles"
        _make_pmtiles(src, {(0, 0, 0): 100.0, (1, 0, 0): 200.0})
        out = tmp_path / "out.mbtiles"
        cfg = self._config(tmp_path, src, "pmtiles", out, "mbtiles")

        result = CliRunner().invoke(cli, ["merge", "-c", str(cfg), "-j", "1"])
        assert result.exit_code == 0, result.output
        conn = sqlite3.connect(str(out))
        try:
            (count,) = conn.execute("SELECT COUNT(*) FROM tiles").fetchone()
        finally:
            conn.close()
        assert count > 0

    @pytest.mark.parametrize("bad_key", ["output_type", "source_type"])
    def test_unknown_type_is_rejected(self, tmp_path, bad_key):
        src = tmp_path / "src.mbtiles"
        _make_mbtiles(src, {(0, 0, 0): 100.0})
        out = tmp_path / "out.mbtiles"
        cfg = self._config(tmp_path, src, "mbtiles", out, "mbtiles")
        config = json.loads(cfg.read_text())
        if bad_key == "output_type":
            config["output_type"] = "geopackage"
        else:
            config["sources"][0]["source_type"] = "geopackage"
        cfg.write_text(json.dumps(config))

        CliRunner().invoke(cli, ["merge", "-c", str(cfg), "-j", "1"])
        assert not out.exists()


# ---------------------------------------------------------------------------
# RGBTiler
# ---------------------------------------------------------------------------

class TestRGBTilerPMTilesOutput:

    def test_extension_selects_the_container(self, tmp_path):
        common = dict(inpath=ELEV_SRC, min_z=0, max_z=1)
        assert RGBTiler(outpath=str(tmp_path / "o.pmtiles"), **common).archive_format == "pmtiles"
        assert RGBTiler(outpath=str(tmp_path / "o.mbtiles"), **common).archive_format == "mbtiles"

    def test_explicit_archive_format_wins_over_the_extension(self, tmp_path):
        tiler = RGBTiler(inpath=ELEV_SRC, outpath=str(tmp_path / "o.mbtiles"),
                         min_z=0, max_z=1, archive_format="pmtiles")
        assert tiler.archive_format == "pmtiles"

    def test_the_writer_matches_the_container(self, tmp_path):
        from rio_rgbify.database import MBTilesDatabase as MB
        with RGBTiler(inpath=ELEV_SRC, outpath=str(tmp_path / "o.pmtiles"),
                      min_z=0, max_z=1) as tiler:
            assert isinstance(tiler.db, PMTilesWriter)
        with RGBTiler(inpath=ELEV_SRC, outpath=str(tmp_path / "o.mbtiles"),
                      min_z=0, max_z=1) as tiler:
            assert isinstance(tiler.db, MB)

    def test_cli_writes_a_clustered_pmtiles(self):
        """End to end through `rio rgbify`, which is how this gets used."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(rgbify, [
                ELEV_SRC, "out.pmtiles",
                "--min-z", 10, "--max-z", 11, "--format", "png", "-j", 1,
            ])
            assert result.exit_code == 0, result.output

            reader = PMTilesReader("out.pmtiles")
            try:
                header = reader.header()
                # The tiler feeds tiles in tile-id order for exactly this.
                assert header["clustered"] is True
                assert header["addressed_tiles_count"] > 0
                assert (header["min_zoom"], header["max_zoom"]) == (10, 11)
                assert header["tile_type"] == TileType.PNG
                assert reader.metadata()["encoding"] == "mapbox"
            finally:
                reader.close()

    def test_cli_pmtiles_and_mbtiles_hold_the_same_tiles(self):
        """The container must not change a single pixel."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            args = ["--min-z", 10, "--max-z", 11, "--format", "png", "-j", 1]
            assert runner.invoke(rgbify, [ELEV_SRC, "out.mbtiles"] + args).exit_code == 0
            assert runner.invoke(rgbify, [ELEV_SRC, "out.pmtiles"] + args).exit_code == 0

            conn = sqlite3.connect("out.mbtiles")
            try:
                rows = conn.execute(
                    "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"
                ).fetchall()
            finally:
                conn.close()
            assert rows

            reader = PMTilesReader("out.pmtiles")
            try:
                for z, x, tms_y, data in rows:
                    assert reader.get_tile(z, x, tms_y) == data, f"differs at {z}/{x}/{tms_y}"
                assert reader.header()["addressed_tiles_count"] == len(rows)
            finally:
                reader.close()

    def test_cli_archive_format_flag_overrides_the_extension(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(rgbify, [
                ELEV_SRC, "out.tiles", "--archive-format", "pmtiles",
                "--min-z", 10, "--max-z", 10, "-j", 1,
            ])
            assert result.exit_code == 0, result.output
            reader = PMTilesReader("out.tiles")
            try:
                assert reader.header()["addressed_tiles_count"] > 0
            finally:
                reader.close()


# ---------------------------------------------------------------------------
# mbutil interoperability
#
# The archives written here are read by our mbutil fork, so a header it cannot
# make sense of is a bug on this side. Skipped where mbutil is not checked out
# beside this repo.
# ---------------------------------------------------------------------------

def _mbutil_or_skip():
    root = Path(__file__).resolve().parents[2] / "mbutil"
    if not (root / "mbutil" / "util.py").exists():
        pytest.skip("mbutil checkout not found beside this repo")
    return root


def _run_mbutil(root, *args):
    proc = subprocess.run(
        [sys.executable, str(root / "mb-util"), *[str(a) for a in args]],
        capture_output=True, text=True, cwd=str(root),
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc


class TestMbutilInterop:

    def test_mbutil_reads_what_we_write(self, tmp_path):
        """`mb-util out.pmtiles out.mbtiles` gets back the tiles we put in."""
        root = _mbutil_or_skip()

        pm = tmp_path / "out.pmtiles"
        tiles = {(0, 0, 0): 100.0, (1, 0, 0): 200.0, (1, 1, 1): 300.0}
        _make_pmtiles(pm, tiles, encoding="terrarium")

        back = tmp_path / "back.mbtiles"
        _run_mbutil(root, pm, back)

        conn = sqlite3.connect(str(back))
        try:
            rows = {
                (z, x, y): data for z, x, y, data in conn.execute(
                    "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"
                )
            }
            meta = dict(conn.execute("SELECT name, value FROM metadata"))
        finally:
            conn.close()

        assert set(rows) == set(tiles)
        for key, elev in tiles.items():
            assert _elevation_of(rows[key], "terrarium") == pytest.approx(elev, abs=0.5)
        assert meta["format"] == "png"
        # The one key a terrain tileset cannot be read without.
        assert meta["encoding"] == "terrarium"

    def test_our_header_matches_mbutils_own_conversion(self, tmp_path):
        """Same tiles, one archive from us and one from mb-util: same header."""
        root = _mbutil_or_skip()

        tiles = {(0, 0, 0): 100.0, (1, 0, 0): 200.0, (1, 1, 1): 300.0}
        bounds = (-10.0, -5.0, 10.0, 5.0)

        ours = tmp_path / "ours.pmtiles"
        _make_pmtiles(ours, tiles, bounds=bounds)

        mb = tmp_path / "via.mbtiles"
        _make_mbtiles(mb, tiles, bounds=bounds)
        theirs = tmp_path / "theirs.pmtiles"
        _run_mbutil(root, mb, theirs)

        compared = ("tile_type", "tile_compression", "min_zoom", "max_zoom",
                    "min_lon_e7", "min_lat_e7", "max_lon_e7", "max_lat_e7",
                    "center_zoom", "addressed_tiles_count", "clustered")
        a, b = PMTilesReader(ours), PMTilesReader(theirs)
        try:
            ah, bh = a.header(), b.header()
            assert {k: ah[k] for k in compared} == {k: bh[k] for k in compared}
            for key in tiles:
                assert a.get_tile(*key) == b.get_tile(*key)
        finally:
            a.close()
            b.close()

    def test_round_trip_through_mbutil_preserves_the_tiles(self, tmp_path):
        """pmtiles -> mbtiles -> pmtiles comes back with the same tiles."""
        root = _mbutil_or_skip()

        tiles = {(0, 0, 0): 100.0, (1, 0, 0): 200.0, (1, 1, 1): 300.0}
        ours = tmp_path / "ours.pmtiles"
        _make_pmtiles(ours, tiles)

        mid = tmp_path / "mid.mbtiles"
        _run_mbutil(root, ours, mid)
        back = tmp_path / "back.pmtiles"
        _run_mbutil(root, mid, back)

        a, b = PMTilesReader(ours), PMTilesReader(back)
        try:
            for key in tiles:
                assert a.get_tile(*key) == b.get_tile(*key), f"differs at {key}"
            assert a.header()["addressed_tiles_count"] == b.header()["addressed_tiles_count"]
            assert b.metadata()["encoding"] == "mapbox"
        finally:
            a.close()
            b.close()
