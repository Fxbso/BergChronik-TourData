from __future__ import annotations

import json
import sqlite3
import struct
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

from bergchronik_tourdata.countries import get_country
from bergchronik_tourdata.pbf import PbfReader
from bergchronik_tourdata.pipeline import build_catalog, search_catalog


def _varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    result = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        result.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(result)


def _zigzag(value: int) -> int:
    return (value << 1) ^ (value >> 63)


def _field_varint(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _field_bytes(number: int, value: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _packed(values: list[int], *, zigzag: bool = False) -> bytes:
    return b"".join(_varint(_zigzag(value) if zigzag else value) for value in values)


def _deltas(values: list[int]) -> list[int]:
    previous = 0
    result = []
    for value in values:
        result.append(value - previous)
        previous = value
    return result


def _test_pbf(path: Path) -> None:
    strings = [
        "",
        "type",
        "route",
        "hiking",
        "name",
        "Testweg",
        "network",
        "lwn",
        "roundtrip",
        "no",
    ]
    string_table = b"".join(
        _field_bytes(1, value.encode("utf-8")) for value in strings
    )

    node_ids = [1, 2, 3]
    lat_values = [470_000_000, 470_001_000, 470_002_000]
    lon_values = [110_000_000, 110_001_000, 110_002_000]
    dense = b"".join(
        [
            _field_bytes(1, _packed(_deltas(node_ids), zigzag=True)),
            _field_bytes(8, _packed(_deltas(lat_values), zigzag=True)),
            _field_bytes(9, _packed(_deltas(lon_values), zigzag=True)),
        ]
    )
    way = b"".join(
        [
            _field_varint(1, 10),
            _field_bytes(8, _packed(_deltas(node_ids), zigzag=True)),
        ]
    )
    relation = b"".join(
        [
            _field_varint(1, 100),
            _field_bytes(2, _packed([1, 2, 4, 6, 8])),
            _field_bytes(3, _packed([2, 3, 5, 7, 9])),
            _field_bytes(8, _packed([0])),
            _field_bytes(9, _packed([10], zigzag=True)),
            _field_bytes(10, _packed([1])),
        ]
    )
    group = (
        _field_bytes(2, dense)
        + _field_bytes(3, way)
        + _field_bytes(4, relation)
    )
    primitive = _field_bytes(1, string_table) + _field_bytes(2, group)
    blob = _field_varint(2, len(primitive)) + _field_bytes(
        3, zlib.compress(primitive)
    )
    header = _field_bytes(1, b"OSMData") + _field_varint(3, len(blob))
    path.write_bytes(struct.pack(">I", len(header)) + header + blob)


class PbfReaderTest(unittest.TestCase):
    def test_reads_dense_nodes_ways_and_relations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.osm.pbf"
            _test_pbf(path)
            reader = PbfReader(path)

            relations = list(reader.relations())
            wanted_ways = {10}
            wanted_nodes = {1, 2, 3}
            ways = list(reader.ways(wanted_ways))
            nodes = list(reader.nodes(wanted_nodes))

            self.assertEqual(relations[0].tags["name"], "Testweg")
            self.assertEqual(relations[0].members[0].ref, 10)
            self.assertEqual(ways[0].node_refs, (1, 2, 3))
            self.assertAlmostEqual(nodes[0][1], 47.0)
            self.assertAlmostEqual(nodes[0][2], 11.0)
            self.assertEqual(wanted_ways, set())
            self.assertEqual(wanted_nodes, set())


class PipelineTest(unittest.TestCase):
    def test_builds_searchable_catalog_geojson_and_gpx(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pbf = root / "sample.osm.pbf"
            output = root / "output"
            _test_pbf(pbf)

            stats = build_catalog(
                pbf,
                output,
                get_country("AT"),
                source_timestamp="2026-07-23T00:00:00Z",
                include_gpx=True,
            )

            self.assertEqual(stats.routes_written, 1)
            database = output / "bergchronik-routes-AT.sqlite"
            connection = sqlite3.connect(database)
            row = connection.execute(
                "SELECT name, distance_m, network FROM routes"
            ).fetchone()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            connection.close()
            self.assertEqual(row[0], "Testweg")
            self.assertGreater(row[1], 0)
            self.assertEqual(row[2], "lwn")
            self.assertEqual(integrity[0], "ok")

            results = search_catalog(database, "Testweg")
            self.assertEqual(results[0]["osm_relation_id"], 100)

            geojson = json.loads(
                (output / "bergchronik-routes-AT.geojson").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                geojson["features"][0]["geometry"]["type"],
                "MultiLineString",
            )
            with zipfile.ZipFile(
                output / "bergchronik-routes-AT-gpx.zip"
            ) as archive:
                self.assertIn("gpx/AT/100.gpx", archive.namelist())


if __name__ == "__main__":
    unittest.main()
