"""Targeted, evidence-backed summit ascent extraction from an OSM PBF file."""
from __future__ import annotations

import heapq
import json
import math
import re
import sqlite3
import tempfile
from collections import defaultdict
from gc import collect
from pathlib import Path

from .pbf import PbfReader

WALKABLE = {"path", "footway", "track", "steps", "pedestrian"}
SAC_SCALE = {
    "hiking": "T1",
    "mountain_hiking": "T2",
    "demanding_mountain_hiking": "T3",
    "alpine_hiking": "T4",
    "demanding_alpine_hiking": "T5",
    "difficult_alpine_hiking": "T6",
}


def _start_kind(tags: dict[str, str]) -> tuple[str, float] | None:
    """Return a plausible human trail start and its preference penalty.

    Generic information points and public-transport platforms are deliberately
    excluded: in the Alps these are commonly guideposts close to a summit.
    """
    if tags.get("highway") == "trailhead" or tags.get("information") == "trailhead":
        return "trailhead", 0.0
    if tags.get("amenity") == "parking" or tags.get("highway") == "parking_entrance":
        return "parking", 0.0
    if tags.get("tourism") == "alpine_hut":
        # Huts are a fallback only. Any reachable valley trailhead/parking
        # within the accepted 60 km route limit must win over a hut close to
        # the summit; otherwise routes such as Großglockner started halfway up.
        return "alpine_hut", 70_000.0
    return None


def _start_label(tags: dict[str, str], kind: str) -> str:
    name = (tags.get("name") or tags.get("ref") or "").strip()
    if name:
        return name
    return {"trailhead": "Wanderparkplatz / Ausgangspunkt", "parking": "Wanderparkplatz", "alpine_hut": "Berghütte"}.get(kind, "Ausgangspunkt")


def _elevation(tags: dict[str, str]) -> int | None:
    raw = (tags.get("ele") or "").strip().lower().replace("m", "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", raw)
    if not match:
        return None
    value = round(float(match.group(0)))
    return value if -500 <= value <= 9000 else None


def _minimum_route_distance(peak_elevation_m: int | None) -> float:
    if peak_elevation_m is None:
        return 500.0
    if peak_elevation_m >= 3500:
        return 2_000.0
    if peak_elevation_m >= 3000:
        return 1_500.0
    if peak_elevation_m >= 2500:
        return 1_000.0
    return 500.0


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


def _snap_starts_to_graph(starts: list[object], graph: dict[int, list], coordinates: dict[int, tuple[float, float]]) -> dict[int, dict[str, object]]:
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for node_id in graph:
        lat, lon = coordinates[node_id]
        grid[(math.floor(lat * 200), math.floor(lon * 200))].append(node_id)
    snapped: dict[int, dict[str, object]] = {}
    for start in starts:
        details = _start_kind(start.tags)
        if details is None:
            continue
        kind, penalty = details
        row, column = math.floor(start.lat * 200), math.floor(start.lon * 200)
        candidates = [
            node_id
            for x in range(row - 1, row + 2)
            for y in range(column - 1, column + 2)
            for node_id in grid.get((x, y), [])
        ]
        if not candidates:
            continue
        node_id = min(candidates, key=lambda value: _distance_m(start.lat, start.lon, *coordinates[value]))
        snap_distance = _distance_m(start.lat, start.lon, *coordinates[node_id])
        if snap_distance > 450.0:
            continue
        item = {
            "node_id": node_id,
            "osm_id": start.osm_id,
            "name": _start_label(start.tags, kind),
            "kind": kind,
            "lat": start.lat,
            "lon": start.lon,
            "elevation_m": _elevation(start.tags),
            "penalty": penalty + snap_distance,
        }
        current = snapped.get(node_id)
        if current is None or float(item["penalty"]) < float(current["penalty"]):
            snapped[node_id] = item
    return snapped


def _point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    lat, lon = point
    inside = False
    previous = polygon[-1]
    for current in polygon:
        lat_a, lon_a = previous
        lat_b, lon_b = current
        if (lat_a > lat) != (lat_b > lat):
            crossing = (lon_b - lon_a) * (lat - lat_a) / (lat_b - lat_a) + lon_a
            if lon < crossing:
                inside = not inside
        previous = current
    return inside


def _route_crosses_glacier(nodes: list[int], coordinates: dict[int, tuple[float, float]], glaciers: list[list[tuple[float, float]]]) -> bool:
    for node_id in nodes:
        point = coordinates[node_id]
        if any(_point_in_polygon(point, polygon) for polygon in glaciers if len(polygon) >= 4):
            return True
    return False


def _safety_flags(sac: str, uiaa: str, ferrata: str, peak_elevation_m: int | None, crosses_glacier: bool) -> list[str]:
    flags: list[str] = []
    if uiaa:
        flags.extend(["requires_climbing", "uiaa_passage"])
    if ferrata:
        flags.extend(["via_ferrata", "requires_via_ferrata_set"])
    if sac in {"T4", "T5", "T6"}:
        flags.append("high_alpine")
    if peak_elevation_m is not None and peak_elevation_m >= 3000:
        flags.extend(["high_altitude", "glacier_conditions_check_required"])
    if crosses_glacier:
        flags.extend(["mapped_glacier_crossing", "requires_glacier"])
    return list(dict.fromkeys(flags))


def build_summit_ascent(input_path: Path, output_path: Path, country: str, peak_name: str, radius_km: float = 30.0) -> dict[str, object]:
    wanted = _normal(peak_name)
    peaks = [node for node in PbfReader(input_path).tagged_nodes() if node.tags.get("natural") == "peak" and _normal(node.tags.get("name", "")) == wanted]
    if not peaks:
        raise ValueError(f"Gipfel {peak_name!r} wurde im OSM-Extrakt nicht gefunden.")
    peak = peaks[0]
    peak_position = (peak.lat, peak.lon)
    radius = max(5_000.0, min(float(radius_km) * 1000.0, 60_000.0))
    starts: list[object] = []
    for node in PbfReader(input_path).tagged_nodes():
        if _within((node.lat, node.lon), peak_position, radius) and _start_kind(node.tags) is not None:
            starts.append(node)
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
    start_nodes = _snap_starts_to_graph(starts, graph, coordinates)
    nearby_targets = [node_id for node_id, position in coordinates.items() if node_id in graph and _within(position, peak_position, 100.0)]
    targets = [min(nearby_targets, key=lambda node_id: _distance_m(*coordinates[node_id], *peak_position))] if nearby_targets else []
    if not start_nodes or not targets:
        raise ValueError("Kein kartierter Startpunkt oder kein Fußweg bis zum Gipfel. Keine Route erzeugt.")
    distances = {node_id: 0.0 for node_id in targets}
    queue = [(0.0, node_id) for node_id in targets]
    previous: dict[int, tuple[int, dict[str, str]]] = {}
    source = None
    source_score = math.inf
    minimum_distance = _minimum_route_distance(_elevation(peak.tags))
    while queue:
        cost, current = heapq.heappop(queue)
        if cost != distances.get(current):
            continue
        if cost > source_score:
            break
        if current in start_nodes and cost >= minimum_distance:
            score = cost + float(start_nodes[current]["penalty"])
            if score < source_score:
                source = current
                source_score = score
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
    peak_elevation_m = _elevation(peak.tags)
    start = start_nodes[source]
    flags = _safety_flags(sac, uiaa, ferrata, peak_elevation_m, False)
    feature = {"type": "Feature", "properties": {"route_id": f"osm-footgraph-{country.lower()}-{peak.osm_id}", "country": country, "peak_osm_id": str(peak.osm_id), "peak_name": peak.tags.get("name", peak_name), "peak_lat": peak.lat, "peak_lon": peak.lon, "peak_elevation_m": peak_elevation_m, "name": f"OSM-Aufstieg auf {peak.tags.get('name', peak_name)}", "start_name": start["name"], "start_lat": start["lat"], "start_lon": start["lon"], "start_kind": start["kind"], "route_kind": "summit_ascent", "roundtrip": False, "source": "OpenStreetMap-Fußgraph", "source_url": f"https://www.openstreetmap.org/node/{peak.osm_id}", "distance_m": round(distances[source]), "confidence": 0.8, "uiaa_grade": uiaa, "sac_scale": sac, "via_ferrata_scale": ferrata, "glacier_status": "not_assessed", "safety_flags": flags}, "geometry": {"type": "LineString", "coordinates": [[coordinates[node][1], coordinates[node][0]] for node in nodes]}}
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
    start: dict[str, object],
    crosses_glacier: bool,
) -> dict[str, object]:
    uiaa = max((_uiaa(item) for item in tags), key=lambda value: (len(value), value), default="")
    sac = _sac(tags)
    ferrata = _via_ferrata(tags)
    peak_elevation_m = _elevation(peak.tags)
    flags = _safety_flags(sac, uiaa, ferrata, peak_elevation_m, crosses_glacier)
    return {
        "type": "Feature",
        "properties": {
            "route_id": f"osm-footgraph-{country.lower()}-{peak.osm_id}",
            "country": country,
            "peak_osm_id": str(peak.osm_id),
            "peak_name": peak.tags["name"],
            "peak_lat": peak.lat,
            "peak_lon": peak.lon,
            "peak_elevation_m": peak_elevation_m,
            "name": f"OSM-Aufstieg auf {peak.tags['name']}",
            "start_name": start["name"],
            "start_lat": start["lat"],
            "start_lon": start["lon"],
            "start_kind": start["kind"],
            "route_kind": "summit_ascent",
            "roundtrip": False,
            "source": "OpenStreetMap-Fußgraph",
            "source_url": f"https://www.openstreetmap.org/node/{peak.osm_id}",
            "distance_m": round(distance_m),
            "confidence": 0.82 if start["kind"] in {"parking", "trailhead"} else 0.76,
            "uiaa_grade": uiaa,
            "sac_scale": sac,
            "via_ferrata_scale": ferrata,
            "glacier_status": "mapped_intersection" if crosses_glacier else "not_detected_in_simple_osm_polygons",
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
    """Build summit ascents from a single PBF scan per entity type.

    The temporary SQLite index keeps the country-wide footway set on disk.
    Tiles then query that index instead of parsing the PBF file again.
    """
    if tile_degrees < 0.5 or buffer_km < 5.0:
        raise ValueError("Kachelgröße muss mindestens 0,5 Grad und Puffer mindestens 5 km sein.")

    print("[1/4] Lese Gipfel und kartierte Startpunkte", flush=True)
    tiles: dict[tuple[int, int], list[Node]] = defaultdict(list)
    starts: list[Node] = []
    for node in PbfReader(input_path).tagged_nodes():
        if node.tags.get("natural") == "peak" and node.tags.get("name"):
            tiles[_tile_key(node.lat, node.lon, tile_degrees)].append(node)
        if _start_kind(node.tags) is not None:
            starts.append(node)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    peaks_seen = sum(len(peaks) for peaks in tiles.values())
    with tempfile.TemporaryDirectory(prefix="bergchronik-summits-") as temporary:
        index_path = Path(temporary) / "footways.sqlite"
        database = sqlite3.connect(index_path)
        database.executescript("""
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            CREATE TABLE ways (id INTEGER PRIMARY KEY, refs TEXT NOT NULL, tags TEXT NOT NULL);
            CREATE VIRTUAL TABLE way_bounds USING rtree(id, min_lat, max_lat, min_lon, max_lon);
            CREATE TABLE glaciers (id INTEGER PRIMARY KEY, refs TEXT NOT NULL);
            CREATE VIRTUAL TABLE glacier_bounds USING rtree(id, min_lat, max_lat, min_lon, max_lon);
        """)
        needed_nodes: set[int] = set()
        way_rows: list[tuple[int, str, str]] = []
        glacier_rows: list[tuple[int, str]] = []
        print("[2/4] Indexiere begehbare OSM-Wege", flush=True)
        for way in PbfReader(input_path).tagged_ways():
            if way.tags.get("natural") == "glacier" and len(way.node_refs) >= 4 and way.node_refs[0] == way.node_refs[-1]:
                glacier_rows.append((way.osm_id, json.dumps(way.node_refs)))
                needed_nodes.update(way.node_refs)
                if len(glacier_rows) >= 2_000:
                    database.executemany("INSERT OR REPLACE INTO glaciers VALUES (?, ?)", glacier_rows)
                    glacier_rows.clear()
                continue
            if way.tags.get("highway") not in WALKABLE or way.tags.get("foot") == "no" or way.tags.get("access") in {"private", "no"}:
                continue
            if len(way.node_refs) < 2:
                continue
            tags = {key: way.tags[key] for key in ("sac_scale", "via_ferrata_scale", "climbing:grade:uiaa", "climbing:grade") if key in way.tags}
            way_rows.append((way.osm_id, json.dumps(way.node_refs), json.dumps(tags)))
            needed_nodes.update(way.node_refs)
            if len(way_rows) >= 5_000:
                database.executemany("INSERT OR REPLACE INTO ways VALUES (?, ?, ?)", way_rows)
                way_rows.clear()
        if way_rows:
            database.executemany("INSERT OR REPLACE INTO ways VALUES (?, ?, ?)", way_rows)
        if glacier_rows:
            database.executemany("INSERT OR REPLACE INTO glaciers VALUES (?, ?)", glacier_rows)
        database.commit()

        print("[3/4] Lese Koordinaten der Fußwege", flush=True)
        coordinates: dict[int, tuple[float, float]] = {}
        for node_id, lat, lon in PbfReader(input_path).all_nodes():
            if node_id in needed_nodes:
                coordinates[node_id] = (lat, lon)
        needed_nodes.clear()

        bound_rows: list[tuple[int, float, float, float, float]] = []
        for way_id, refs_json in database.execute("SELECT id, refs FROM ways"):
            positions = [coordinates[node_id] for node_id in json.loads(refs_json) if node_id in coordinates]
            if len(positions) < 2:
                continue
            latitudes, longitudes = zip(*positions)
            bound_rows.append((way_id, min(latitudes), max(latitudes), min(longitudes), max(longitudes)))
            if len(bound_rows) >= 5_000:
                database.executemany("INSERT INTO way_bounds VALUES (?, ?, ?, ?, ?)", bound_rows)
                bound_rows.clear()
        if bound_rows:
            database.executemany("INSERT INTO way_bounds VALUES (?, ?, ?, ?, ?)", bound_rows)
        glacier_bound_rows: list[tuple[int, float, float, float, float]] = []
        for glacier_id, refs_json in database.execute("SELECT id, refs FROM glaciers"):
            positions = [coordinates[node_id] for node_id in json.loads(refs_json) if node_id in coordinates]
            if len(positions) < 4:
                continue
            latitudes, longitudes = zip(*positions)
            glacier_bound_rows.append((glacier_id, min(latitudes), max(latitudes), min(longitudes), max(longitudes)))
            if len(glacier_bound_rows) >= 2_000:
                database.executemany("INSERT INTO glacier_bounds VALUES (?, ?, ?, ?, ?)", glacier_bound_rows)
                glacier_bound_rows.clear()
        if glacier_bound_rows:
            database.executemany("INSERT INTO glacier_bounds VALUES (?, ?, ?, ?, ?)", glacier_bound_rows)
        database.commit()

        with output_path.open("w", encoding="utf-8") as handle:
            for number, (tile, peaks) in enumerate(sorted(tiles.items()), start=1):
                print(f"[4/4] Kachel {number}/{len(tiles)}: {len(peaks)} Gipfel", flush=True)
                bounds = _tile_bounds(tile, tile_degrees, buffer_km)
                graph: dict[int, list[tuple[int, float, dict[str, str]]]] = {}
                query = """
                    SELECT w.refs, w.tags FROM way_bounds b JOIN ways w ON w.id = b.id
                    WHERE b.min_lat <= ? AND b.max_lat >= ? AND b.min_lon <= ? AND b.max_lon >= ?
                """
                for refs_json, tags_json in database.execute(query, (bounds[1], bounds[0], bounds[3], bounds[2])):
                    refs = json.loads(refs_json)
                    tags = json.loads(tags_json)
                    for left, right in zip(refs, refs[1:]):
                        if left not in coordinates or right not in coordinates:
                            continue
                        if not _in_bounds(*coordinates[left], bounds) or not _in_bounds(*coordinates[right], bounds):
                            continue
                        edge = _distance_m(*coordinates[left], *coordinates[right])
                        if 0 < edge < 2_000:
                            graph.setdefault(left, []).append((right, edge, tags))
                            graph.setdefault(right, []).append((left, edge, tags))
                glacier_query = """
                    SELECT g.refs FROM glacier_bounds b JOIN glaciers g ON g.id = b.id
                    WHERE b.min_lat <= ? AND b.max_lat >= ? AND b.min_lon <= ? AND b.max_lon >= ?
                """
                glaciers = [
                    [coordinates[node_id] for node_id in json.loads(refs_json) if node_id in coordinates]
                    for (refs_json,) in database.execute(glacier_query, (bounds[1], bounds[0], bounds[3], bounds[2]))
                ]
                tile_starts = [node for node in starts if _in_bounds(node.lat, node.lon, bounds)]
                start_nodes = _snap_starts_to_graph(tile_starts, graph, coordinates)
                costs = {node_id: float(start["penalty"]) for node_id, start in start_nodes.items()}
                route_distances = {node_id: 0.0 for node_id in start_nodes}
                origins = {node_id: node_id for node_id in start_nodes}
                queue = [(cost, node_id) for node_id, cost in costs.items()]
                heapq.heapify(queue)
                previous: dict[int, tuple[int, dict[str, str]]] = {}
                while queue:
                    cost, current = heapq.heappop(queue)
                    if cost != costs.get(current) or route_distances[current] > 75_000:
                        continue
                    for neighbor, edge, tags in graph.get(current, []):
                        candidate = cost + edge
                        if candidate < costs.get(neighbor, math.inf):
                            costs[neighbor] = candidate
                            route_distances[neighbor] = route_distances[current] + edge
                            origins[neighbor] = origins[current]
                            previous[neighbor] = (current, tags)
                            heapq.heappush(queue, (candidate, neighbor))
                nearby: dict[tuple[int, int], list[int]] = defaultdict(list)
                for node_id in costs:
                    lat, lon = coordinates[node_id]
                    nearby[math.floor(lat * 100), math.floor(lon * 100)].append(node_id)
                for peak in peaks:
                    row, column = math.floor(peak.lat * 100), math.floor(peak.lon * 100)
                    candidates = [node_id for x in range(row - 1, row + 2) for y in range(column - 1, column + 2) for node_id in nearby.get((x, y), []) if _within(coordinates[node_id], (peak.lat, peak.lon), 100.0)]
                    if not candidates:
                        continue
                    end = min(candidates, key=lambda node_id: costs[node_id])
                    route_distance = route_distances[end]
                    if not _minimum_route_distance(_elevation(peak.tags)) <= route_distance <= 60_000.0:
                        continue
                    nodes, tags = [end], []
                    origin = origins[end]
                    while nodes[-1] != origin:
                        previous_node, edge_tags = previous[nodes[-1]]
                        nodes.append(previous_node)
                        tags.append(edge_tags)
                    nodes.reverse()
                    tags.reverse()
                    crosses_glacier = _route_crosses_glacier(nodes, coordinates, glaciers)
                    feature = _summit_feature(country, peak, nodes, tags, coordinates, route_distance, start_nodes[origin], crosses_glacier)
                    handle.write(json.dumps(feature, ensure_ascii=False, separators=(",", ":")) + "\n")
                    written += 1
                del graph, costs, route_distances, origins, queue, previous, nearby, glaciers, start_nodes
                collect()
        database.close()
    return {"peaks_seen": peaks_seen, "routes_written": written}
