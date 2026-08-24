from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bergchronik_tourdata.pbf import Node, Way
from bergchronik_tourdata.summit_ascents import build_summit_ascent


class _Reader:
    def __init__(self, _path: Path):
        pass

    def tagged_nodes(self):
        return iter([
            Node(1, 47.0000, 12.0000, {"natural": "peak", "name": "Testgipfel"}),
            Node(2, 46.9980, 12.0000, {"amenity": "parking"}),
            Node(3, 46.9990, 12.0000, {}),
            Node(4, 46.9997, 12.0000, {}),
        ])

    def tagged_ways(self):
        return iter([
            Way(10, (2, 3, 4, 1), {"highway": "path", "sac_scale": "T4", "climbing:grade:uiaa": "II"}),
        ])

    def all_nodes(self):
        return iter([(1, 47.0000, 12.0000), (2, 46.9980, 12.0000), (3, 46.9990, 12.0000), (4, 46.9997, 12.0000)])


class SummitAscentTest(unittest.TestCase):
    def test_creates_an_ascent_that_reaches_the_peak_and_keeps_uiaa(self):
        with tempfile.TemporaryDirectory() as directory, patch("bergchronik_tourdata.summit_ascents.PbfReader", _Reader):
            output = Path(directory) / "summit.geojsonseq"
            result = build_summit_ascent(Path(directory) / "fixture.osm.pbf", output, "AT", "Testgipfel")
            feature = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(result["peak"], "Testgipfel")
        self.assertEqual(feature["properties"]["uiaa_grade"], "II")
        self.assertIn("requires_climbing", feature["properties"]["safety_flags"])
        self.assertFalse(feature["properties"]["roundtrip"])
        self.assertEqual(feature["geometry"]["coordinates"][-1], [12.0, 47.0])
