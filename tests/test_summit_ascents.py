from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bergchronik_tourdata.pbf import Node, Way
from bergchronik_tourdata.summit_ascents import build_all_summit_ascents, build_summit_ascent


class _Reader:
    def __init__(self, _path: Path):
        pass

    def tagged_nodes(self):
        return iter([
            Node(1, 47.0000, 12.0000, {"natural": "peak", "name": "Testgipfel"}),
            Node(2, 46.9970, 12.0000, {"amenity": "parking"}),
            Node(3, 46.9985, 12.0000, {}),
            Node(4, 46.9997, 12.0000, {}),
        ])

    def tagged_ways(self):
        return iter([
            Way(10, (2, 3, 4, 1), {"highway": "path", "sac_scale": "alpine_hiking", "via_ferrata_scale": "3-", "climbing:grade:uiaa": "II"}),
        ])

    def all_nodes(self):
        return iter([(1, 47.0000, 12.0000), (2, 46.9970, 12.0000), (3, 46.9985, 12.0000), (4, 46.9997, 12.0000)])


class SummitAscentTest(unittest.TestCase):
    def test_creates_an_ascent_that_reaches_the_peak_and_keeps_uiaa(self):
        with tempfile.TemporaryDirectory() as directory, patch("bergchronik_tourdata.summit_ascents.PbfReader", _Reader):
            output = Path(directory) / "summit.geojsonseq"
            result = build_summit_ascent(Path(directory) / "fixture.osm.pbf", output, "AT", "Testgipfel")
            feature = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(result["peak"], "Testgipfel")
        self.assertEqual(feature["properties"]["uiaa_grade"], "II")
        self.assertEqual(feature["properties"]["sac_scale"], "T4")
        self.assertEqual(feature["properties"]["via_ferrata_scale"], "B/C")
        self.assertIn("requires_climbing", feature["properties"]["safety_flags"])
        self.assertFalse(feature["properties"]["roundtrip"])
        self.assertEqual(feature["geometry"]["coordinates"][-1], [12.0, 47.0])

    def test_creates_one_non_roundtrip_ascent_per_reachable_summit(self):
        with tempfile.TemporaryDirectory() as directory, patch("bergchronik_tourdata.summit_ascents.PbfReader", _Reader):
            output = Path(directory) / "summits.geojsonseq"
            result = build_all_summit_ascents(Path(directory) / "fixture.osm.pbf", output, "AT")
            features = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(result, {"peaks_seen": 1, "routes_written": 1})
        self.assertEqual(features[0]["properties"]["peak_name"], "Testgipfel")
        self.assertFalse(features[0]["properties"]["roundtrip"])
        self.assertIn("requires_climbing", features[0]["properties"]["safety_flags"])
