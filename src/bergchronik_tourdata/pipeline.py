from __future__ import annotations

import json
import math
import sqlite3
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Iterator

from .countries import Country
from .pbf import PbfReader, Relation, RelationMember


ROUTE_TYPES = {"hiking", "foot"}
NETWORKS = {"lwn", "rwn", "nwn", "iwn"}
WAY_ROLES = {
    "",
    "main",
    "forward",
    "backward",
    "alternative",
    "alternate",
    "approach",
    "excursion",
    "connection",
}


@dataclass
class BuildStats:
    relations_seen: int = 0
    routes_selected: int = 0
    routes_written: int = 0
    routes_without_geometry: int = 0
    ways_requested: int = 0
    ways_found: int = 0
    nodes_requested: int = 0
    nodes_found: int = 0


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _parse_int(value: str | None) -> int | None:
    if not value:
        return None
    cleaned = value.lower().replace("meters", "").replace("meter", "")
    cleaned = cleaned.replace("metres", "").replace("metre", "").strip()
    cleaned = cleaned.removesuffix("m").strip().replace(",", ".")
    try:
        return round(float(cleaned))
    except ValueError:
        return None


def _roundtrip(value: str | None) -> int | None:
    if value in {"yes", "true", "1"}:
        return 1
    if value in {"no", "false", "0"}:
        return 0
    return None


def _relation_is_route(relation: Relation) -> bool:
    return (
        relation.tags.get("type") == "route"
        and relation.tags.get("route") in ROUTE_TYPES
    )


def _flatten_way_members(
    relation_id: int,
    relations: dict[int, Relation],
    active: frozenset[int] = frozenset(),
    inherited_role: str = "",
) -> Iterator[tuple[int, str]]:
    if relation_id in active:
        return
    relation = relations.get(relation_id)
    if relation is None:
        return
    next_active = active | {relation_id}
    for member in relation.members:
        role = member.role or inherited_role
        if member.member_type == "way":
            if role in WAY_ROLES:
                yield member.ref, role
        elif member.member_type == "relation":
            yield from _flatten_way_members(
                member.ref,
                relations,
                next_active,
                role,
            )


def _pack_refs(refs: tuple[int, ...]) -> bytes:
    if not refs:
        return b""
    return zlib.compress(struct.pack(f"<{len(refs)}q", *refs), level=6)


def _unpack_refs(payload: bytes) -> tuple[int, ...]:
    if not payload:
        return ()
    raw = zlib.decompress(payload)
    if len(raw) % 8:
        raise ValueError("Ungültige Way-Referenzdaten")
    return struct.unpack(f"<{len(raw) // 8}q", raw)


def _chunks(values: list[int], size: int = 800) -> Iterator[list[int]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _load_coordinates(
    connection: sqlite3.Connection,
    node_ids: set[int],
) -> dict[int, tuple[float, float]]:
    coordinates: dict[int, tuple[float, float]] = {}
    ordered = list(node_ids)
    for chunk in _chunks(ordered):
        placeholders = ",".join("?" for _ in chunk)
        query = f"SELECT id, lat, lon FROM raw_nodes WHERE id IN ({placeholders})"
        for node_id, lat, lon in connection.execute(query, chunk):
            coordinates[node_id] = (lat, lon)
    return coordinates


def _relation_segments(
    relation: Relation,
    relations: dict[int, Relation],
    connection: sqlite3.Connection,
) -> tuple[list[list[tuple[float, float]]], set[str]]:
    way_members = list(_flatten_way_members(relation.osm_id, relations))
    way_rows: list[tuple[int, str, tuple[int, ...]]] = []
    node_ids: set[int] = set()
    flags: set[str] = set()

    for way_id, role in way_members:
        row = connection.execute(
            "SELECT refs FROM raw_ways WHERE id = ?",
            (way_id,),
        ).fetchone()
        if row is None:
            flags.add("missing_way")
            continue
        refs = _unpack_refs(row[0])
        if len(refs) < 2:
            continue
        way_rows.append((way_id, role, refs))
        node_ids.update(refs)

    coordinates = _load_coordinates(connection, node_ids)
    segments: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    current_end_ref: int | None = None

    for _, role, refs in way_rows:
        points = [coordinates[ref] for ref in refs if ref in coordinates]
        available_refs = [ref for ref in refs if ref in coordinates]
        if len(points) < 2:
            flags.add("missing_node")
            continue
        if len(points) != len(refs):
            flags.add("missing_node")
        if role == "backward":
            points.reverse()
            available_refs.reverse()

        first_ref = available_refs[0]
        last_ref = available_refs[-1]
        if not current:
            current = points
            current_end_ref = last_ref
        elif current_end_ref == first_ref:
            current.extend(points[1:])
            current_end_ref = last_ref
        elif current_end_ref == last_ref:
            points.reverse()
            available_refs.reverse()
            current.extend(points[1:])
            current_end_ref = available_refs[-1]
        else:
            if len(current) >= 2:
                segments.append(current)
            current = points
            current_end_ref = last_ref
            flags.add("disconnected")

    if len(current) >= 2:
        segments.append(current)
    return segments, flags


def _haversine_m(
    first: tuple[float, float],
    second: tuple[float, float],
) -> float:
    lat1, lon1 = map(math.radians, first)
    lat2, lon2 = map(math.radians, second)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    value = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 6_371_008.8 * 2 * math.asin(min(1.0, math.sqrt(value)))


def _distance_m(segments: list[list[tuple[float, float]]]) -> int:
    distance = 0.0
    for segment in segments:
        distance += sum(
            _haversine_m(first, second)
            for first, second in zip(segment, segment[1:])
        )
    return round(distance)


def _geometry(segments: list[list[tuple[float, float]]]) -> dict[str, object]:
    return {
        "type": "MultiLineString",
        "coordinates": [
            [[round(lon, 7), round(lat, 7)] for lat, lon in segment]
            for segment in segments
        ],
    }


def _bounds(
    segments: list[list[tuple[float, float]]],
) -> tuple[float, float, float, float, float, float]:
    points = [point for segment in segments for point in segment]
    min_lat = min(point[0] for point in points)
    max_lat = max(point[0] for point in points)
    min_lon = min(point[1] for point in points)
    max_lon = max(point[1] for point in points)
    return (
        min_lat,
        min_lon,
        max_lat,
        max_lon,
        (min_lat + max_lat) / 2,
        (min_lon + max_lon) / 2,
    )


def _display_name(relation: Relation) -> str:
    tags = relation.tags
    return (
        tags.get("name")
        or tags.get("name:de")
        or tags.get("ref")
        or f"OSM-Wanderroute {relation.osm_id}"
    )


def _gpx_bytes(
    relation: Relation,
    name: str,
    segments: list[list[tuple[float, float]]],
) -> bytes:
    root = ET.Element(
        "gpx",
        {
            "version": "1.1",
            "creator": "BergChronik-TourData",
            "xmlns": "http://www.topografix.com/GPX/1/1",
        },
    )
    metadata = ET.SubElement(root, "metadata")
    ET.SubElement(metadata, "name").text = name
    ET.SubElement(metadata, "link", {
        "href": f"https://www.openstreetmap.org/relation/{relation.osm_id}"
    })
    track = ET.SubElement(root, "trk")
    ET.SubElement(track, "name").text = name
    ET.SubElement(track, "type").text = relation.tags.get("route", "hiking")
    for segment in segments:
        track_segment = ET.SubElement(track, "trkseg")
        for lat, lon in segment:
            ET.SubElement(
                track_segment,
                "trkpt",
                {"lat": f"{lat:.7f}", "lon": f"{lon:.7f}"},
            )
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


class GeoJsonWriter:
    def __init__(self, path: Path):
        self.path = path
        self.handle = path.open("w", encoding="utf-8", newline="\n")
        self.handle.write('{"type":"FeatureCollection","features":[\n')
        self.first = True

    def add(self, feature: dict[str, object]) -> None:
        if not self.first:
            self.handle.write(",\n")
        json.dump(
            feature,
            self.handle,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.first = False

    def close(self) -> None:
        self.handle.write("\n]}\n")
        self.handle.close()

    def __enter__(self) -> "GeoJsonWriter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _prepare_spool(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        PRAGMA temp_store = MEMORY;
        CREATE TABLE raw_ways (id INTEGER PRIMARY KEY, refs BLOB NOT NULL);
        CREATE TABLE raw_nodes (
            id INTEGER PRIMARY KEY,
            lat REAL NOT NULL,
            lon REAL NOT NULL
        );
        """
    )
    return connection


def _prepare_catalog(path: Path) -> sqlite3.Connection:
    schema = Path(__file__).resolve().parents[2] / "schema.sql"
    connection = sqlite3.connect(path)
    connection.executescript(schema.read_text(encoding="utf-8"))
    return connection


def build_catalog(
    input_path: Path,
    output_dir: Path,
    country: Country,
    source_timestamp: str | None = None,
    include_gpx: bool = False,
) -> BuildStats:
    output_dir.mkdir(parents=True, exist_ok=True)
    source_timestamp = source_timestamp or _utc_now()
    stats = BuildStats()
    reader = PbfReader(input_path)

    relations: dict[int, Relation] = {}
    for relation in reader.relations():
        stats.relations_seen += 1
        if _relation_is_route(relation):
            relations[relation.osm_id] = relation
    stats.routes_selected = len(relations)

    needed_ways: set[int] = set()
    for relation_id in relations:
        needed_ways.update(
            way_id
            for way_id, _ in _flatten_way_members(relation_id, relations)
        )
    stats.ways_requested = len(needed_ways)

    spool_path = output_dir / f".{country.code.lower()}-spool.sqlite"
    catalog_path = output_dir / f"bergchronik-routes-{country.code}.sqlite"
    geojson_path = output_dir / f"bergchronik-routes-{country.code}.geojson"
    manifest_path = output_dir / f"manifest-{country.code}.json"
    gpx_path = output_dir / f"bergchronik-routes-{country.code}-gpx.zip"

    for target in (spool_path, catalog_path, geojson_path, manifest_path, gpx_path):
        target.unlink(missing_ok=True)

    spool = _prepare_spool(spool_path)
    needed_nodes: set[int] = set()
    way_batch: list[tuple[int, bytes]] = []
    for way in reader.ways(needed_ways):
        way_batch.append((way.osm_id, _pack_refs(way.node_refs)))
        needed_nodes.update(way.node_refs)
        stats.ways_found += 1
        if len(way_batch) >= 5_000:
            spool.executemany("INSERT INTO raw_ways VALUES (?, ?)", way_batch)
            way_batch.clear()
    if way_batch:
        spool.executemany("INSERT INTO raw_ways VALUES (?, ?)", way_batch)
    spool.commit()

    stats.nodes_requested = len(needed_nodes)
    node_batch: list[tuple[int, float, float]] = []
    for node_id, lat, lon in reader.nodes(needed_nodes):
        node_batch.append((node_id, lat, lon))
        stats.nodes_found += 1
        if len(node_batch) >= 20_000:
            spool.executemany("INSERT INTO raw_nodes VALUES (?, ?, ?)", node_batch)
            node_batch.clear()
    if node_batch:
        spool.executemany("INSERT INTO raw_nodes VALUES (?, ?, ?)", node_batch)
    spool.commit()

    catalog = _prepare_catalog(catalog_path)
    metadata = {
        "format_version": "1",
        "country": country.code,
        "country_name": country.name,
        "source": country.source_url,
        "source_timestamp": source_timestamp,
        "created_at": _utc_now(),
        "data_license": "ODbL-1.0",
        "attribution": "© OpenStreetMap contributors",
    }
    catalog.executemany(
        "INSERT INTO metadata(key, value) VALUES (?, ?)",
        metadata.items(),
    )

    gpx_zip = (
        zipfile.ZipFile(gpx_path, "w", compression=zipfile.ZIP_DEFLATED)
        if include_gpx
        else None
    )
    try:
        with GeoJsonWriter(geojson_path) as geojson:
            for relation in sorted(relations.values(), key=lambda item: item.osm_id):
                segments, flags = _relation_segments(
                    relation, relations, spool
                )
                if not segments:
                    stats.routes_without_geometry += 1
                    continue
                tags = relation.tags
                name = _display_name(relation)
                if "name" not in tags and "name:de" not in tags:
                    flags.add("unnamed")
                if tags.get("state") in {"proposed", "abandoned", "disused"}:
                    flags.add(tags["state"])
                network = tags.get("network")
                if network and network not in NETWORKS:
                    flags.add("nonstandard_network")

                geometry = _geometry(segments)
                geometry_json = json.dumps(
                    geometry,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                bounds = _bounds(segments)
                properties = {
                    "osm_relation_id": relation.osm_id,
                    "country": country.code,
                    "route_type": tags["route"],
                    "name": name,
                    "name_de": tags.get("name:de"),
                    "ref": tags.get("ref"),
                    "network": network,
                    "operator": tags.get("operator"),
                    "distance_m": _distance_m(segments),
                    "ascent_m": _parse_int(tags.get("ascent")),
                    "descent_m": _parse_int(tags.get("descent")),
                    "duration": tags.get("duration"),
                    "roundtrip": _roundtrip(tags.get("roundtrip")),
                    "quality_flags": sorted(flags),
                    "source_url": (
                        "https://www.openstreetmap.org/relation/"
                        f"{relation.osm_id}"
                    ),
                }
                geojson.add(
                    {
                        "type": "Feature",
                        "id": f"osm-relation-{relation.osm_id}",
                        "properties": properties,
                        "geometry": geometry,
                    }
                )

                catalog.execute(
                    """
                    INSERT INTO routes(
                        osm_relation_id, country, route_type, name, name_de,
                        ref, network, operator, symbol, osmc_symbol,
                        route_from, route_to, roundtrip, distance_m,
                        ascent_m, descent_m, duration, website, wikipedia,
                        state, min_lat, min_lon, max_lat, max_lon,
                        center_lat, center_lon, geometry_json_zlib, tags_json,
                        quality_flags, source_url, source_timestamp
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        relation.osm_id,
                        country.code,
                        tags["route"],
                        name,
                        tags.get("name:de"),
                        tags.get("ref"),
                        network,
                        tags.get("operator"),
                        tags.get("symbol"),
                        tags.get("osmc:symbol"),
                        tags.get("from"),
                        tags.get("to"),
                        _roundtrip(tags.get("roundtrip")),
                        properties["distance_m"],
                        properties["ascent_m"],
                        properties["descent_m"],
                        tags.get("duration"),
                        tags.get("website"),
                        tags.get("wikipedia"),
                        tags.get("state"),
                        *bounds,
                        zlib.compress(geometry_json.encode("utf-8"), level=9),
                        json.dumps(
                            tags,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        ",".join(sorted(flags)),
                        properties["source_url"],
                        source_timestamp,
                    ),
                )
                if gpx_zip is not None:
                    gpx_zip.writestr(
                        f"gpx/{country.code}/{relation.osm_id}.gpx",
                        _gpx_bytes(relation, name, segments),
                    )
                stats.routes_written += 1
                if stats.routes_written % 1_000 == 0:
                    catalog.commit()
                    print(
                        f"{country.code}: {stats.routes_written} Routen geschrieben",
                        flush=True,
                    )
    finally:
        if gpx_zip is not None:
            gpx_zip.close()

    catalog.commit()
    catalog.execute("PRAGMA optimize")
    catalog.close()
    spool.close()
    spool_path.unlink(missing_ok=True)

    manifest = {
        **metadata,
        "counts": stats.__dict__,
        "files": {
            "sqlite": catalog_path.name,
            "geojson": geojson_path.name,
            "gpx_zip": gpx_path.name if include_gpx else None,
        },
        "scope": {
            "relation_type": "route",
            "route_values": sorted(ROUTE_TYPES),
            "network_values_preserved": sorted(NETWORKS),
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return stats


def search_catalog(
    database: Path,
    query: str,
    country: str | None = None,
    limit: int = 20,
) -> list[dict[str, object]]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    sql = """
        SELECT r.osm_relation_id, r.country, r.route_type, r.name, r.ref,
               r.network, r.distance_m, r.ascent_m, r.descent_m,
               r.center_lat, r.center_lon, r.quality_flags
        FROM route_search s
        JOIN routes r ON r.id = s.rowid
        WHERE route_search MATCH ?
    """
    parameters: list[object] = [query]
    if country:
        sql += " AND r.country = ?"
        parameters.append(country.upper())
    sql += " ORDER BY bm25(route_search), r.name LIMIT ?"
    parameters.append(max(1, min(limit, 100)))
    rows = connection.execute(sql, parameters).fetchall()
    connection.close()
    return [dict(row) for row in rows]
