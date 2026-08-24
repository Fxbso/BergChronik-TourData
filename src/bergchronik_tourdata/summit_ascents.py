"""Targeted, evidence-backed summit ascent extraction from an OSM PBF file."""
from __future__ import annotations

import heapq
import json
import math
import re
from collections import defaultdict
from gc import collect
from pathlib import Path

from .pbf import PbfReader

WALKABLE = {"path", "footway", "track", "steps", "pedestrian"}
STARTS = {("amenity", "parking"), ("highway", "parking_entrance"), ("tourism", "information"), ("public_transport", "platform")}
SAC_SCALE = {
    "hiking": "T1",
    "mountain_hiking": "T2",
    "demanding_mountain_hiking": "T3",
    "alpine_hiking": "T4",
    "demanding_alpine_hiking": "T5",
    "difficult_alpine_hiking": "T6",
}


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


def _sac(tags: list[dict[str, str]]) -> str:
    values = [SAC_SCALE.get(item.get("sac_scale", "").strip().lower(), "") for item in tags]
    return max(values, key=lambda value: int(value[1:]) if value else 0, default="")


def _via_ferrata(tags: list[dict[str, str]]) -> str:
    """Normalize common OSM ferrata scales to the A-F scale used by the app."""
    values = [item.get("via_ferrata_scale", "").strip().upper() for item in tags]
    normalized: list[str] = []
    for raw in values:
        if not raw:
            continue
        if re.fullmatch(r"[A-F](?:/[A-F])?", raw):
            normalized.append(raw)
            continue
        half = re.fullmatch(r"(?:KS|K)?\s*([0-6])[.,]5", raw)
        numeric = re.fullmatch(r"(?:KS|K)?\s*([0-6])(?:\s*(?:/|-|TO|BIS)\s*([0-6]))?([+-])?", raw)
        if half:
            first = max(0, min(5, int(half.group(1)) - 1))
            normalized.append("ABCDEF"[first] + "/" + "ABCDEF"[min(5, first + 1)])
        elif numeric:
            first = int(numeric.group(1))
            second = int(numeric.group(2)) if numeric.group(2) else None
            modifier = numeric.group(3) or ""
            grade = lambda number: "ABCDEF"[max(0, min(5, number - 1))]
            if second is not None and second != first:
                low, high = min(first, second), max(first, second)
                normalized.append(grade(low) + "/" + grade(high) if high - low == 1 else grade(high))
            elif modifier == "+" and first < 6:
                normalized.append(grade(first) + "/" + grade(first + 1))
            elif modifier == "-" and first > 1:
                normalized.append(grade(first - 1) + "/" + grade(first))
            else:
                normalized.append(grade(first))
    return max(normalized, key=lambda value: "ABCDEF".index(value[-1]), default="")


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
    sac = _sac(tags)
    ferrata = _via_ferrata(tags)
    flags = (["requires_climbing"] if uiaa else []) + (["via_ferrata", "requires_via_ferrata_set"] if ferrata else [])
    feature = {"type": "Feature", "properties": {"route_id": f"osm-footgraph-{country.lower()}-{peak.osm_id}", "country": country, "peak_osm_id": str(peak.osm_id), "peak_name": peak.tags.get("name", peak_name), "peak_lat": peak.lat, "peak_lon": peak.lon, "name": f"OSM-Aufstieg auf {peak.tags.get('name', peak_name)}", "start_name": "Kartierter OSM-Startpunkt", "route_kind": "summit_ascent", "roundtrip": False, "source": "OpenStreetMap-Fußgraph", "source_url": f"https://www.openstreetmap.org/node/{peak.osm_id}", "distance_m": round(distances[source]), "confidence": 0.78, "uiaa_grade": uiaa, "sac_scale": sac, "via_ferrata_scale": ferrata, "safety_flags": flags}, "geometry": {"type": "LineString", "coordinates": [[coordinates[node][1], coordinates[node][0]] for node in nodes]}}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(feature, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return {"peak": feature["properties"]["peak_name"], "distance_m": feature["properties"]["distance_m"], "uiaa_grade": uiaa, "output": str(output_path)}


def _tile_key(lat: float, lon: float, tile_degrees: float) -> tuple[int, int]:
    return math.floor(lat / tile_degrees), math.floor(lon / tile_degrees)


def _tile_bounds(
    key: tuple[int, int], tile_degrees: float, buffer_km: float
) -> tuple[float, float, float, float]:
    min_lat, min_lon = key[0] * tile_degrees, key[1] * tile_degrees
    mid_lat = min_lat + tile_degrees / 2
    lat_buffer = buffer_km / 111.0
    lon_buffer = buffer_km / max(20.0, 111.0 * math.cos(math.radians(mid_lat)))
    return (
        min_lat - lat_buffer,
        min_lat + tile_degrees + lat_buffer,
        min_lon - lon_buffer,
        min_lon + tile_degrees + lon_buffer,
    )


def _in_bounds(lat: float, lon: float, bounds: tuple[float, float, float, float]) -> bool:
    return bounds[0] <= lat <= bounds[1] and bounds[2] <= lon <= bounds[3]


def _summit_feature(
    country: str,
    peak: Node,
    nodes: list[int],
    tags: list[dict[str, str]],
    coordinates: dict[int, tuple[float, float]],
    distance_m: float,
) -> dict[str, object]:
    uiaa = max((_uiaa(item) for item in tags), key=lambda value: (len(value), value), default="")
    sac = _sac(tags)
    ferrata = _via_ferrata(tags)
    flags = (["requires_climbing"] if uiaa else [])
    if ferrata:
        flags.extend(["via_ferrata", "requires_via_ferrata_set"])
    return {
        "type": "Feature",
        "properties": {
            "route_id": f"osm-footgraph-{country.lower()}-{peak.osm_id}",
            "country": country,
            "peak_osm_id": str(peak.osm_id),
            "peak_name": peak.tags["name"],
            "peak_lat": peak.lat,
            "peak_lon": peak.lon,
            "name": f"OSM-Aufstieg auf {peak.tags['name']}",
            "start_name": "Kartierter OSM-Startpunkt",
            "route_kind": "summit_ascent",
            "roundtrip": False,
            "source": "OpenStreetMap-Fußgraph",
            "source_url": f"https://www.openstreetmap.org/node/{peak.osm_id}",
            "distance_m": round(distance_m),
            "confidence": 0.78,
            "uiaa_grade": uiaa,
            "sac_scale": sac,
            "via_ferrata_scale": ferrata,
            "safety_flags": flags,
        },
        "geometry": {
            "type": "LineString",
            "coordinates": [[coordinates[node][1], coordinates[node][0]] for node in nodes],
        },
    }


def build_all_summit_ascents(
    input_path: Path,
    output_path: Path,
    country: str,
    tile_degrees: float = 2.0,
    buffer_km: float = 20.0,
) -> dict[str, int]:
    """Build local graph tiles so a country's full foot graph never enters memory."""
    if tile_degrees < 0.5 or buffer_km < 5.0:
        raise ValueError("Kachelgröße muss mindestens 0,5 Grad und Puffer mindestens 5 km sein.")

    tiles: dict[tuple[int, int], list[Node]] = defaultdict(list)
    for node in PbfReader(input_path).tagged_nodes():
        if node.tags.get("natural") == "peak" and node.tags.get("name"):
            tiles[_tile_key(node.lat, node.lon, tile_degrees)].append(node)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    peaks_seen = sum(len(peaks) for peaks in tiles.values())
    with output_path.open("w", encoding="utf-8") as handle:
        for number, (tile, peaks) in enumerate(sorted(tiles.items()), start=1):
            print(f"[{number}/{len(tiles)}] Verarbeite {len(peaks)} Gipfel in Kachel {tile}", flush=True)
            bounds = _tile_bounds(tile, tile_degrees, buffer_km)
            starts = {
                node.osm_id
                for node in PbfReader(input_path).tagged_nodes()
                if _in_bounds(node.lat, node.lon, bounds)
                and any(node.tags.get(key) == value for key, value in STARTS)
            }
            coordinates = {
                node_id: (lat, lon)
                for node_id, lat, lon in PbfReader(input_path).all_nodes()
                if _in_bounds(lat, lon, bounds)
            }
            graph: dict[int, list[tuple[int, float, dict[str, str]]]] = {}
            for way in PbfReader(input_path).tagged_ways():
                if (
                    way.tags.get("highway") not in WALKABLE
                    or way.tags.get("foot") == "no"
                    or way.tags.get("access") in {"private", "no"}
                ):
                    continue
                for left, right in zip(way.node_refs, way.node_refs[1:]):
                    if left not in coordinates or right not in coordinates:
                        continue
                    edge = _distance_m(*coordinates[left], *coordinates[right])
                    if 0 < edge < 2_000:
                        graph.setdefault(left, []).append((right, edge, way.tags))
                        graph.setdefault(right, []).append((left, edge, way.tags))

            starts.intersection_update(graph)
            distances = {node: 0.0 for node in starts}
            queue = [(0.0, node) for node in starts]
            previous: dict[int, tuple[int, dict[str, str]]] = {}
            while queue:
                cost, current = heapq.heappop(queue)
                if cost != distances.get(current) or cost > 75_000:
                    continue
                for neighbor, edge, tags in graph.get(current, []):
                    candidate = cost + edge
                    if candidate < distances.get(neighbor, math.inf):
                        distances[neighbor] = candidate
                        previous[neighbor] = (current, tags)
                        heapq.heappush(queue, (candidate, neighbor))

            nearby: dict[tuple[int, int], list[int]] = defaultdict(list)
            for node_id in distances:
                lat, lon = coordinates[node_id]
                nearby[math.floor(lat * 100), math.floor(lon * 100)].append(node_id)
            for peak in peaks:
                row, column = math.floor(peak.lat * 100), math.floor(peak.lon * 100)
                candidates = [
                    node_id
                    for x in range(row - 1, row + 2)
                    for y in range(column - 1, column + 2)
                    for node_id in nearby.get((x, y), [])
                    if _within(coordinates[node_id], (peak.lat, peak.lon), 100.0)
                ]
                if not candidates:
                    continue
                end = min(candidates, key=lambda node_id: _distance_m(*coordinates[node_id], peak.lat, peak.lon))
                nodes, tags = [end], []
                while nodes[-1] not in starts:
                    previous_node, edge_tags = previous[nodes[-1]]
                    nodes.append(previous_node)
                    tags.append(edge_tags)
                nodes.reverse()
                tags.reverse()
                feature = _summit_feature(country, peak, nodes, tags, coordinates, distances[end])
                handle.write(json.dumps(feature, ensure_ascii=False, separators=(",", ":")) + "\n")
                written += 1
            del starts, coordinates, graph, distances, queue, previous, nearby
            collect()
    return {"peaks_seen": peaks_seen, "routes_written": written}
