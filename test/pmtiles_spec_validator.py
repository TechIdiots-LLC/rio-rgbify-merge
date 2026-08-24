"""Check a PMTiles archive against the v3 specification.

Deliberately does not use the `pmtiles` library. This exists to check what
that library wrote on our behalf, so it re-implements the parse from the spec
text -- otherwise a writer bug and a reader bug that agree with each other
would pass. Section numbers refer to protomaps/PMTiles `spec/v3/spec.md`.

Calibrated against the two well-formed archives shipped in that spec
directory; it also catches the `"type": "raster"` in the third, which is a
genuine violation of section 5.
"""
import gzip
import json
import re
import struct

MAGIC = b"PMTiles"
COMPRESSIONS = {0: "Unknown", 1: "None", 2: "gzip", 3: "brotli", 4: "zstd"}
TILE_TYPES = {0: "Unknown", 1: "MVT", 2: "PNG", 3: "JPEG", 4: "WebP", 5: "AVIF", 6: "MLT"}

# Semantic Versioning 2.0.0, as the spec's section 5 requires of `version`.
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


class SpecViolation(AssertionError):
    pass


def _varint(buf, pos):
    result, shift = 0, 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def _decompress(data, compression):
    if compression == 1:
        return data
    if compression == 2:
        return gzip.decompress(data)
    raise NotImplementedError(f"internal compression {compression}")


class _Directory:
    """One decoded directory, per spec 4.3."""

    def __init__(self, raw, compression, where, fail):
        buf = _decompress(raw, compression)
        pos = 0
        n, pos = _varint(buf, pos)
        if n <= 0:
            fail(f"{where}: a directory MUST hold more than 0 entries (spec 4.2)")

        ids, last = [], 0
        for _ in range(n):
            delta, pos = _varint(buf, pos)
            last += delta
            ids.append(last)
        runs = []
        for _ in range(n):
            v, pos = _varint(buf, pos)
            runs.append(v)
        lengths = []
        for _ in range(n):
            v, pos = _varint(buf, pos)
            lengths.append(v)
        offsets = []
        for i in range(n):
            v, pos = _varint(buf, pos)
            # Offset 0 means "directly after the previous entry" (spec 4.2).
            offsets.append(offsets[i - 1] + lengths[i - 1] if v == 0 and i > 0 else v - 1)

        if pos != len(buf):
            fail(f"{where}: {len(buf) - pos} bytes left after the five encoded "
                 f"parts of a directory (spec 4.2)")
        if not all(length > 0 for length in lengths):
            fail(f"{where}: every entry Length MUST be greater than 0 (spec 4.1)")
        if ids != sorted(ids):
            fail(f"{where}: TileIDs must ascend, or the delta encoding in spec "
                 f"4.2 cannot represent them")

        self.entries = list(zip(ids, offsets, lengths, runs))


def validate(path):
    """Raise SpecViolation on the first MUST the archive breaks.

    Returns a dict describing the archive, for a caller that wants to assert
    something further about it.
    """
    violations = []

    def fail(message):
        violations.append(message)

    data = open(path, "rb").read()
    size = len(data)

    # -- 3 Header -----------------------------------------------------------
    if size < 127:
        raise SpecViolation(f"{path}: shorter than the 127-byte header (spec 3)")
    if data[:7] != MAGIC:
        fail(f"magic number MUST be b'PMTiles' (spec 3.2), got {data[:7]!r}")
    if data[7] != 3:
        fail(f"version byte MUST be 3 (spec 3.2), got {data[7]}")

    (root_off, root_len, meta_off, meta_len, leaf_off, leaf_len,
     tile_off, tile_len, n_addressed, n_entries,
     n_contents) = struct.unpack_from("<11Q", data, 8)
    clustered, internal, tile_comp, tile_type, min_z, max_z = struct.unpack_from("<6B", data, 96)
    min_lon, min_lat, max_lon, max_lat = struct.unpack_from("<4i", data, 102)
    center_z = data[118]
    center_lon, center_lat = struct.unpack_from("<2i", data, 119)

    if root_off + root_len > 16384:
        fail(f"header plus root directory MUST fit in 16384 bytes (spec 4), "
             f"got {root_off + root_len}")
    if root_len > 16257:
        fail(f"compressed root directory MUST be at most 16257 bytes (spec 4), got {root_len}")
    if internal not in COMPRESSIONS:
        fail(f"internal compression {internal} is not a value in spec 3.3")
    if tile_comp not in COMPRESSIONS:
        fail(f"tile compression {tile_comp} is not a value in spec 3.3")
    if tile_type not in TILE_TYPES:
        fail(f"tile type {tile_type} is not a value in spec 3.2")
    if clustered not in (0, 1):
        fail(f"clustered MUST be 0x00 or 0x01 (spec 3.2), got {clustered}")
    if max_z < min_z:
        fail(f"max zoom MUST be at least min zoom (spec 3.2), got {min_z}..{max_z}")

    for name, off, length in (("root", root_off, root_len),
                              ("metadata", meta_off, meta_len),
                              ("leaf directories", leaf_off, leaf_len),
                              ("tile data", tile_off, tile_len)):
        if off + length > size:
            fail(f"{name} section [{off}, {off + length}) runs past the {size}-byte file")

    for value, label in ((min_lon, "min"), (max_lon, "max"), (center_lon, "center")):
        if not -1800000000 <= value <= 1800000000:
            fail(f"{label} longitude e7 out of range (spec 3.4): {value}")
    for value, label in ((min_lat, "min"), (max_lat, "max"), (center_lat, "center")):
        if not -900000000 <= value <= 900000000:
            fail(f"{label} latitude e7 out of range (spec 3.4): {value}")
    if min_lon > max_lon or min_lat > max_lat:
        fail("min position must not exceed max position (spec 3.2)")

    # -- 4 Directories ------------------------------------------------------
    root = _Directory(data[root_off:root_off + root_len], internal, "root", fail).entries
    tiles = [e for e in root if e[3] != 0]
    leaves = [e for e in root if e[3] == 0]

    if [tid for tid, *_ in leaves] != sorted(tid for tid, *_ in leaves):
        fail("leaf directories SHOULD be ascending by starting TileID (spec 4)")

    for tid, off, length, _ in leaves:
        if off + length > leaf_len:
            fail(f"leaf entry at {off}+{length} runs past the {leaf_len}-byte leaf section")
            continue
        leaf = _Directory(data[leaf_off + off:leaf_off + off + length],
                          internal, f"leaf@{tid}", fail).entries
        for entry in leaf:
            if entry[3] == 0:
                fail(f"leaf directory nested under another at TileID {entry[0]} (spec 4)")
            tiles.append(entry)

    tiles.sort()
    if len(tiles) != n_entries:
        fail(f"header claims {n_entries} tile entries, directories hold {len(tiles)}")
    if sum(run for *_, run in tiles) != n_addressed:
        fail(f"header claims {n_addressed} addressed tiles, run lengths sum to "
             f"{sum(run for *_, run in tiles)}")
    blobs = {(off, length) for _, off, length, _ in tiles}
    if len(blobs) != n_contents:
        fail(f"header claims {n_contents} tile contents, directories reference {len(blobs)}")

    for tid, off, length, _ in tiles:
        if off + length > tile_len:
            fail(f"tile {tid} at {off}+{length} runs past the {tile_len}-byte tile data section")

    # The zoom range in the header must describe the tiles that are there --
    # both ends, since a client uses them to decide what to request.
    if tiles:
        zooms = set()
        for tid, _, _, run in tiles:
            for i in range(run):
                zooms.add(_zoom_of(tid + i))
        if min(zooms) != min_z:
            fail(f"header min zoom is {min_z} but the shallowest tile is at z{min(zooms)}")
        if max(zooms) != max_z:
            fail(f"header max zoom is {max_z} but the deepest tile is at z{max(zooms)}")

    # -- Clustered (spec 3.2) ----------------------------------------------
    if clustered == 1 and tiles:
        if tiles[0][1] != 0:
            fail("a clustered archive MUST put its first tile entry at offset 0 (spec 3.2)")
        end = 0
        for tid, off, length, _ in tiles:
            # Contiguous with the previous tile, or a back-reference to an
            # earlier one because it was deduplicated.
            if off != end and off >= end:
                fail(f"clustered: tile {tid} starts at {off}, neither contiguous "
                     f"with {end} nor a back-reference (spec 3.2)")
                break
            end = max(end, off + length)

    # -- 5 JSON metadata ----------------------------------------------------
    metadata = {}
    try:
        metadata = json.loads(_decompress(data[meta_off:meta_off + meta_len],
                                          internal).decode("utf-8"))
        if not isinstance(metadata, dict):
            fail("metadata MUST be a JSON object (spec 5)")
            metadata = {}
    except Exception as exc:
        fail(f"metadata is not valid UTF-8 JSON (spec 5): {exc}")

    if tile_type == 1 and "vector_layers" not in metadata:
        fail("an MVT archive MUST carry vector_layers in its metadata (spec 5)")
    if "type" in metadata and metadata["type"] not in ("overlay", "baselayer"):
        fail(f"metadata 'type' MUST be 'overlay' or 'baselayer' (spec 5), "
             f"got {metadata['type']!r}")
    if "version" in metadata and not (
        isinstance(metadata["version"], str) and SEMVER.match(metadata["version"])
    ):
        fail(f"metadata 'version' MUST be valid SemVer 2.0.0 (spec 5), "
             f"got {metadata['version']!r}")
    for key in ("name", "description", "attribution"):
        if key in metadata and not isinstance(metadata[key], str):
            fail(f"metadata {key!r} MUST be a string (spec 5), "
                 f"got {type(metadata[key]).__name__}")

    if violations:
        raise SpecViolation(f"{path}:\n  " + "\n  ".join(violations))

    return dict(
        size=size, addressed=n_addressed, entries=n_entries, contents=n_contents,
        clustered=bool(clustered), internal=COMPRESSIONS[internal],
        tile_compression=COMPRESSIONS[tile_comp], tile_type=TILE_TYPES[tile_type],
        min_zoom=min_z, max_zoom=max_z, center_zoom=center_z,
        leaf_directories=len(leaves), metadata=metadata,
        bounds_e7=(min_lon, min_lat, max_lon, max_lat),
    )


def _zoom_of(tile_id):
    """The zoom a TileID falls in, per the cumulative layout in spec 4.1."""
    zoom, base = 0, 0
    while True:
        count = 1 << (2 * zoom)
        if tile_id < base + count:
            return zoom
        base += count
        zoom += 1
