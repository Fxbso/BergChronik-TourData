from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


class PbfError(RuntimeError):
    pass


@dataclass(frozen=True)
class RelationMember:
    member_type: str
    ref: int
    role: str


@dataclass(frozen=True)
class Relation:
    osm_id: int
    tags: dict[str, str]
    members: tuple[RelationMember, ...]


@dataclass(frozen=True)
class Way:
    osm_id: int
    node_refs: tuple[int, ...]
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Node:
    osm_id: int
    lat: float
    lon: float
    tags: dict[str, str] = field(default_factory=dict)


def _read_varint(data: bytes | memoryview, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    size = len(data)
    while offset < size and shift < 70:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
    raise PbfError("Ungültiger oder abgeschnittener Protobuf-Varint")


def _signed_64(value: int) -> int:
    return value - (1 << 64) if value & (1 << 63) else value


def _zigzag(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def _fields(data: bytes | memoryview) -> Iterator[tuple[int, int, int | bytes]]:
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        key, offset = _read_varint(view, offset)
        field_number = key >> 3
        wire_type = key & 7
        if wire_type == 0:
            value, offset = _read_varint(view, offset)
            yield field_number, wire_type, value
        elif wire_type == 1:
            end = offset + 8
            if end > len(view):
                raise PbfError("Abgeschnittenes 64-Bit-Protobuf-Feld")
            yield field_number, wire_type, bytes(view[offset:end])
            offset = end
        elif wire_type == 2:
            length, offset = _read_varint(view, offset)
            end = offset + length
            if end > len(view):
                raise PbfError("Abgeschnittenes Protobuf-Feld")
            yield field_number, wire_type, bytes(view[offset:end])
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(view):
                raise PbfError("Abgeschnittenes 32-Bit-Protobuf-Feld")
            yield field_number, wire_type, bytes(view[offset:end])
            offset = end
        else:
            raise PbfError(f"Nicht unterstützter Protobuf-Wire-Type {wire_type}")


def _packed(data: bytes, *, zigzag: bool = False) -> list[int]:
    values: list[int] = []
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        value, offset = _read_varint(view, offset)
        values.append(_zigzag(value) if zigzag else value)
    return values


def _delta(values: list[int]) -> list[int]:
    total = 0
    decoded: list[int] = []
    for value in values:
        total += value
        decoded.append(total)
    return decoded


def _message_values(data: bytes, field_number: int) -> list[bytes]:
    return [
        value
        for number, wire_type, value in _fields(data)
        if number == field_number and wire_type == 2 and isinstance(value, bytes)
    ]


def _string_table(data: bytes) -> tuple[str, ...]:
    strings: list[str] = []
    for number, wire_type, value in _fields(data):
        if number == 1 and wire_type == 2 and isinstance(value, bytes):
            strings.append(value.decode("utf-8", errors="replace"))
    return tuple(strings)


def _tags(
    keys: list[int],
    values: list[int],
    strings: tuple[str, ...],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for key_index, value_index in zip(keys, values):
        if key_index < len(strings) and value_index < len(strings):
            result[strings[key_index]] = strings[value_index]
    return result


def _blob_payload(blob: bytes) -> bytes:
    raw: bytes | None = None
    compressed: bytes | None = None
    raw_size: int | None = None
    for number, wire_type, value in _fields(blob):
        if number == 1 and wire_type == 2 and isinstance(value, bytes):
            raw = value
        elif number == 2 and wire_type == 0 and isinstance(value, int):
            raw_size = value
        elif number == 3 and wire_type == 2 and isinstance(value, bytes):
            compressed = value
        elif number in {4, 5, 6, 7}:
            raise PbfError(
                "Der PBF-Block verwendet eine nicht unterstützte Kompression; "
                "Geofabrik-Dateien verwenden regulär zlib."
            )
    if raw is not None:
        return raw
    if compressed is None:
        raise PbfError("PBF-Blob enthält weder Rohdaten noch zlib-Daten")
    payload = zlib.decompress(compressed)
    if raw_size is not None and len(payload) != raw_size:
        raise PbfError("Dekomprimierte PBF-Blockgröße stimmt nicht")
    return payload


def _blocks(path: Path) -> Iterator[tuple[str, bytes]]:
    with path.open("rb") as handle:
        while True:
            prefix = handle.read(4)
            if not prefix:
                return
            if len(prefix) != 4:
                raise PbfError("Abgeschnittener PBF-Blockheader")
            header_size = struct.unpack(">I", prefix)[0]
            if header_size <= 0 or header_size > 64 * 1024:
                raise PbfError(f"Unplausible PBF-Headergröße {header_size}")
            header = handle.read(header_size)
            if len(header) != header_size:
                raise PbfError("Abgeschnittener PBF-BlobHeader")
            block_type = ""
            data_size = -1
            for number, wire_type, value in _fields(header):
                if number == 1 and wire_type == 2 and isinstance(value, bytes):
                    block_type = value.decode("ascii", errors="strict")
                elif number == 3 and wire_type == 0 and isinstance(value, int):
                    data_size = value
            if not block_type or data_size < 0:
                raise PbfError("PBF-BlobHeader ist unvollständig")
            blob = handle.read(data_size)
            if len(blob) != data_size:
                raise PbfError("Abgeschnittener PBF-Blob")
            yield block_type, _blob_payload(blob)


def _primitive_block(
    data: bytes,
) -> tuple[tuple[str, ...], list[bytes], int, int, int]:
    strings: tuple[str, ...] = ()
    groups: list[bytes] = []
    granularity = 100
    lat_offset = 0
    lon_offset = 0
    for number, wire_type, value in _fields(data):
        if number == 1 and wire_type == 2 and isinstance(value, bytes):
            strings = _string_table(value)
        elif number == 2 and wire_type == 2 and isinstance(value, bytes):
            groups.append(value)
        elif number == 17 and wire_type == 0 and isinstance(value, int):
            granularity = value
        elif number == 19 and wire_type == 0 and isinstance(value, int):
            lat_offset = _signed_64(value)
        elif number == 20 and wire_type == 0 and isinstance(value, int):
            lon_offset = _signed_64(value)
    return strings, groups, granularity, lat_offset, lon_offset


def _parse_relation(data: bytes, strings: tuple[str, ...]) -> Relation:
    osm_id = 0
    keys: list[int] = []
    values: list[int] = []
    roles: list[int] = []
    refs: list[int] = []
    types: list[int] = []
    for number, wire_type, value in _fields(data):
        if number == 1 and wire_type == 0 and isinstance(value, int):
            osm_id = _signed_64(value)
        elif number == 2 and wire_type == 2 and isinstance(value, bytes):
            keys = _packed(value)
        elif number == 3 and wire_type == 2 and isinstance(value, bytes):
            values = _packed(value)
        elif number == 8 and wire_type == 2 and isinstance(value, bytes):
            roles = _packed(value)
        elif number == 9 and wire_type == 2 and isinstance(value, bytes):
            refs = _delta(_packed(value, zigzag=True))
        elif number == 10 and wire_type == 2 and isinstance(value, bytes):
            types = _packed(value)
    type_names = ("node", "way", "relation")
    members: list[RelationMember] = []
    for role_index, ref, member_type in zip(roles, refs, types):
        if member_type >= len(type_names):
            continue
        role = strings[role_index] if role_index < len(strings) else ""
        members.append(RelationMember(type_names[member_type], ref, role))
    return Relation(osm_id, _tags(keys, values, strings), tuple(members))


def _parse_way(data: bytes, strings: tuple[str, ...]) -> Way:
    osm_id = 0
    keys: list[int] = []
    values: list[int] = []
    refs: list[int] = []
    for number, wire_type, value in _fields(data):
        if number == 1 and wire_type == 0 and isinstance(value, int):
            osm_id = _signed_64(value)
        elif number == 2 and wire_type == 2 and isinstance(value, bytes):
            keys = _packed(value)
        elif number == 3 and wire_type == 2 and isinstance(value, bytes):
            values = _packed(value)
        elif number == 8 and wire_type == 2 and isinstance(value, bytes):
            refs = _delta(_packed(value, zigzag=True))
    return Way(osm_id, tuple(refs), _tags(keys, values, strings))


def _dense_nodes(
    data: bytes,
    granularity: int,
    lat_offset: int,
    lon_offset: int,
) -> Iterator[tuple[int, float, float]]:
    ids: list[int] = []
    lats: list[int] = []
    lons: list[int] = []
    for number, wire_type, value in _fields(data):
        if number == 1 and wire_type == 2 and isinstance(value, bytes):
            ids = _delta(_packed(value, zigzag=True))
        elif number == 8 and wire_type == 2 and isinstance(value, bytes):
            lats = _delta(_packed(value, zigzag=True))
        elif number == 9 and wire_type == 2 and isinstance(value, bytes):
            lons = _delta(_packed(value, zigzag=True))
    for osm_id, lat, lon in zip(ids, lats, lons):
        yield (
            osm_id,
            1e-9 * (lat_offset + granularity * lat),
            1e-9 * (lon_offset + granularity * lon),
        )


def _dense_tagged_nodes(
    data: bytes,
    strings: tuple[str, ...],
    granularity: int,
    lat_offset: int,
    lon_offset: int,
) -> Iterator[Node]:
    """Decode tagged DenseNodes; their tags use a zero-terminated key/value stream."""
    positions = list(_dense_nodes(data, granularity, lat_offset, lon_offset))
    key_values: list[int] = []
    for number, wire_type, value in _fields(data):
        if number == 10 and wire_type == 2 and isinstance(value, bytes):
            key_values = _packed(value)

    offset = 0
    for osm_id, lat, lon in positions:
        tags: dict[str, str] = {}
        while offset < len(key_values) and key_values[offset] != 0:
            if offset + 1 >= len(key_values):
                raise PbfError("Abgeschnittene DenseNode-Tags")
            key_index, value_index = key_values[offset], key_values[offset + 1]
            if key_index < len(strings) and value_index < len(strings):
                tags[strings[key_index]] = strings[value_index]
            offset += 2
        if offset < len(key_values):
            offset += 1
        if tags:
            yield Node(osm_id, lat, lon, tags)


def _plain_node(
    data: bytes,
    granularity: int,
    lat_offset: int,
    lon_offset: int,
) -> Node:
    osm_id = 0
    keys: list[int] = []
    values: list[int] = []
    lat = 0
    lon = 0
    for number, wire_type, value in _fields(data):
        if number == 1 and wire_type == 0 and isinstance(value, int):
            osm_id = _zigzag(value)
        elif number == 2 and wire_type == 2 and isinstance(value, bytes):
            keys = _packed(value)
        elif number == 3 and wire_type == 2 and isinstance(value, bytes):
            values = _packed(value)
        elif number == 8 and wire_type == 0 and isinstance(value, int):
            lat = _zigzag(value)
        elif number == 9 and wire_type == 0 and isinstance(value, int):
            lon = _zigzag(value)
    return Node(osm_id, 1e-9 * (lat_offset + granularity * lat), 1e-9 * (lon_offset + granularity * lon), {})


class PbfReader:
    """Streaming reader for the OSM PBF entities needed by this project."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def relations(self) -> Iterator[Relation]:
        for block_type, payload in _blocks(self.path):
            if block_type != "OSMData":
                continue
            strings, groups, _, _, _ = _primitive_block(payload)
            for group in groups:
                for relation_data in _message_values(group, 4):
                    yield _parse_relation(relation_data, strings)

    def ways(self, wanted: set[int]) -> Iterator[Way]:
        remaining = wanted
        for block_type, payload in _blocks(self.path):
            if not remaining:
                return
            if block_type != "OSMData":
                continue
            strings, groups, _, _, _ = _primitive_block(payload)
            for group in groups:
                for way_data in _message_values(group, 3):
                    way = _parse_way(way_data, strings)
                    if way.osm_id in remaining:
                        remaining.remove(way.osm_id)
                        yield way

    def nodes(self, wanted: set[int]) -> Iterator[tuple[int, float, float]]:
        remaining = wanted
        for block_type, payload in _blocks(self.path):
            if not remaining:
                return
            if block_type != "OSMData":
                continue
            _, groups, granularity, lat_offset, lon_offset = _primitive_block(
                payload
            )
            for group in groups:
                for node_data in _message_values(group, 1):
                    node = _plain_node(
                        node_data, granularity, lat_offset, lon_offset
                    )
                    if node.osm_id in remaining:
                        remaining.remove(node.osm_id)
                        yield node.osm_id, node.lat, node.lon
                for dense_data in _message_values(group, 2):
                    for node in _dense_nodes(
                        dense_data, granularity, lat_offset, lon_offset
                    ):
                        if node[0] in remaining:
                            remaining.remove(node[0])
                            yield node

    def tagged_nodes(self) -> Iterator[Node]:
        """Stream tagged ordinary and compact DenseNodes."""
        for block_type, payload in _blocks(self.path):
            if block_type != "OSMData":
                continue
            strings, groups, granularity, lat_offset, lon_offset = _primitive_block(payload)
            for group in groups:
                for node_data in _message_values(group, 1):
                    node = _plain_node(node_data, granularity, lat_offset, lon_offset)
                    # Reparse tags here because plain nodes carry their own string IDs.
                    keys: list[int] = []
                    values: list[int] = []
                    for number, wire_type, value in _fields(node_data):
                        if number == 2 and wire_type == 2 and isinstance(value, bytes): keys = _packed(value)
                        elif number == 3 and wire_type == 2 and isinstance(value, bytes): values = _packed(value)
                    yield Node(node.osm_id, node.lat, node.lon, _tags(keys, values, strings))
                for dense_data in _message_values(group, 2):
                    yield from _dense_tagged_nodes(
                        dense_data, strings, granularity, lat_offset, lon_offset
                    )

    def all_nodes(self) -> Iterator[tuple[int, float, float]]:
        """Stream every coordinate, including compact dense OSM nodes."""
        for block_type, payload in _blocks(self.path):
            if block_type != "OSMData":
                continue
            _, groups, granularity, lat_offset, lon_offset = _primitive_block(payload)
            for group in groups:
                for node_data in _message_values(group, 1):
                    node = _plain_node(node_data, granularity, lat_offset, lon_offset)
                    yield node.osm_id, node.lat, node.lon
                for dense_data in _message_values(group, 2):
                    yield from _dense_nodes(dense_data, granularity, lat_offset, lon_offset)

    def tagged_ways(self) -> Iterator[Way]:
        for block_type, payload in _blocks(self.path):
            if block_type != "OSMData":
                continue
            strings, groups, _, _, _ = _primitive_block(payload)
            for group in groups:
                for way_data in _message_values(group, 3):
                    yield _parse_way(way_data, strings)
