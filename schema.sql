PRAGMA journal_mode = DELETE;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE routes (
    id INTEGER PRIMARY KEY,
    osm_relation_id INTEGER NOT NULL UNIQUE,
    country TEXT NOT NULL,
    route_type TEXT NOT NULL CHECK (route_type IN ('hiking', 'foot')),
    name TEXT NOT NULL,
    name_de TEXT,
    ref TEXT,
    network TEXT,
    operator TEXT,
    symbol TEXT,
    osmc_symbol TEXT,
    route_from TEXT,
    route_to TEXT,
    roundtrip INTEGER,
    distance_m INTEGER,
    ascent_m INTEGER,
    descent_m INTEGER,
    duration TEXT,
    website TEXT,
    wikipedia TEXT,
    state TEXT,
    min_lat REAL,
    min_lon REAL,
    max_lat REAL,
    max_lon REAL,
    center_lat REAL,
    center_lon REAL,
    geometry_json_zlib BLOB NOT NULL,
    tags_json TEXT NOT NULL,
    quality_flags TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL,
    source_timestamp TEXT NOT NULL
);

CREATE VIRTUAL TABLE route_search USING fts5(
    name,
    name_de,
    ref,
    operator,
    route_from,
    route_to,
    content='routes',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE VIRTUAL TABLE route_bounds USING rtree(
    route_id,
    min_lat,
    max_lat,
    min_lon,
    max_lon
);

CREATE TRIGGER routes_ai AFTER INSERT ON routes BEGIN
    INSERT INTO route_search(rowid, name, name_de, ref, operator, route_from, route_to)
    VALUES (
        new.id, new.name, new.name_de, new.ref, new.operator,
        new.route_from, new.route_to
    );
    INSERT INTO route_bounds(route_id, min_lat, max_lat, min_lon, max_lon)
    VALUES (new.id, new.min_lat, new.max_lat, new.min_lon, new.max_lon);
END;

CREATE INDEX routes_country_type_idx ON routes(country, route_type);
CREATE INDEX routes_network_idx ON routes(network);
CREATE INDEX routes_osm_relation_idx ON routes(osm_relation_id);

