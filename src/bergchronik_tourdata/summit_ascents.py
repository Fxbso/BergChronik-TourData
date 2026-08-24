"""Targeted, evidence-backed summit ascent extraction from an OSM PBF file."""
from __future__ import annotations

import heapq
import json
import math
import re
from pathlib import Path

from .pbf import PbfReader

WALKABLE = {"path", "footway", "track", "steps", "pedestrian"}
STARTS = {("amenity", "parking"), ("highway", "parking_entrance"), ("tourism", "information"), ("public_transport", "platform")}


def _distance_m(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    lat_a, lon_a, lat_b, lon_b = map(math.radians, (lat_a, lon_a, lat_b, lon_b))
    value = math.sin((lat_b - lat_a) / 2) ** 2 + math.cos(lat_a) * math.cos(lat_b) * math.sin((lon_b - lon_a) / 2) ** 2
    return 6_371_000 * 2 * math.asin(math.sqrt(value))


def _normal(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold().replace("ß", "ss"))


def _within(position: tuple[float, float], peak: tuple[float, float], radius: float) -> bool:
    return _distance_m(position[0], position[1], peak[0], peak[1]) <= radius


def _uiaa(tags: dict[str, str]) -> str:
    value = tags.get("climbing:grade:uiaa", tags.get("climbing:grade", "")).upper().replace(" ", "")
    return value if re.fullmatch(r"(?:I|II|III|IV|V|VI|VII|VIII|IX|X|XI|XII)[+-]?", value) else ""


def build_summit_ascent(input_path: Path, output_path: Path, country: str, peak_name: str, radius_km: float = 30.0) -> dict[str, object]:
    wanted = _normal(peak_name)
    peaks = [node for node in PbfReader(input_path).tagged_nodes() if node.tags.get("natural") == "peak" and _normal(node.tags.get("name", "")) == wanted]
    if not peaks:
        raise ValueError(f"Gipfel {peak_name!r} wurde im OSM-Extrakt nicht gefunden.")
    peak = peaks[0]
    peak_position = (peak.lat, peak.lon)
    radius = max(5_000.0, min(float(radius_km) * 1000.0, 60_000.0))
    starts: set[int] = set()
    for node in PbfReader(input_path).tagged_nodes():
        if _within((node.lat, node.lon), peak_position, radius) and any(node.tags.get(key) == value for key, value in STARTS):
            starts.add(node.osm_id)
    coordinates = {node_id: (lat, lon) for node_id, lat, lon in PbfReader(input_path).all_nodes() if _within((lat, lon), peak_position, radius)}
    graph: dict[int, list[tuple[int, float, dict[str, str]]]] = {}
    for way in PbfReader(input_path).tagged_ways():
        if way.tags.get("highway") not in WALKABLE or way.tags.get("foot") == "no" or way.tags.get("access") in {"private", "no"}:
            continue
        for left, right in zip(way.node_refs, way.node_refs[1:]):
            if left not in coordinates or right not in coordinates:
                continue
            edge = _distance_m(*coordinates[left], *coordinates[right])
            if 0 < edge < 2_000:
                graph.setdefault(left, []).append((right, edge, way.tags))
                graph.setdefault(right, []).append((left, edge, way.tags))
    nearby_targets = [node_id for node_id, position in coordinates.items() if node_id in graph and _within(position, peak_position, 100.0)]
    targets = [min(nearby_targets, key=lambda node_id: _distance_m(*coordinates[node_id], *peak_position))] if nearby_targets else []
    if not starts or not targets:
        raise ValueError("Kein kartierter Startpunkt oder kein Fußweg bis zum Gipfel. Keine Route erzeugt.")
    distances = {node_id: 0.0 for node_id in targets}
    queue = [(0.0, node_id) for node_id in targets]
    previous: dict[int, tuple[int, dict[str, str]]] = {}
    source = None
    while queue:
        cost, current = heapq.heappop(queue)
        if cost != distances.get(current):
            continue
        if current in starts:
            source = current
            break
        if cost > 60_000:
            continue
        for neighbor, edge, tags in graph.get(current, []):
            candidate = cost + edge
            if candidate < distances.get(neighbor, math.inf):
                distances[neighbor] = candidate
                previous[neighbor] = (current, tags)
                heapq.heappush(queue, (candidate, neighbor))
    if source is None:
        raise ValueError("Kein zusammenhängender OSM-Aufstieg vom Startpunkt zum Gipfel. Keine Route erzeugt.")
    nodes, tags = [source], []
    while nodes[-1] not in targets:
        following, edge_tags = previous[nodes[-1]]
        nodes.append(following)
        tags.append(edge_tags)
    uiaa = max((_uiaa(item) for item in tags), key=lambda value: (len(value), value), default="")
    sac = max((item.get("sac_scale", "") for item in tags), default="")
    ferrata = max((item.get("via_ferrata_scale", "") for item in tags), default="")
    flags = (["requires_climbing"] if uiaa else []) + (["via_ferrata", "requires_via_ferrata_set"] if ferrata else [])
    feature = {"type": "Feature", "properties": {"route_id": f"osm-footgraph-{country.lower()}-{peak.osm_id}", "country": country, "peak_osm_id": str(peak.osm_id), "peak_name": peak.tags.get("name", peak_name), "peak_lat": peak.lat, "peak_lon": peak.lon, "name": f"OSM-Aufstieg auf {peak.tags.get('name', peak_name)}", "start_name": "Kartierter OSM-Startpunkt", "route_kind": "summit_ascent", "roundtrip": False, "source": "OpenStreetMap-Fußgraph", "source_url": f"https://www.openstreetmap.org/node/{peak.osm_id}", "distance_m": round(distances[source]), "confidence": 0.78, "uiaa_grade": uiaa, "sac_scale": sac, "via_ferrata_scale": ferrata, "safety_flags": flags}, "geometry": {"type": "LineString", "coordinates": [[coordinates[node][1], coordinates[node][0]] for node in nodes]}}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(feature, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return {"peak": feature["properties"]["peak_name"], "distance_m": feature["properties"]["distance_m"], "uiaa_grade": uiaa, "output": str(output_path)}
