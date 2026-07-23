from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Country:
    code: str
    name: str
    source_url: str


COUNTRIES = {
    "AT": Country(
        "AT",
        "Österreich",
        "https://download.geofabrik.de/europe/austria-latest.osm.pbf",
    ),
    "DE": Country(
        "DE",
        "Deutschland",
        "https://download.geofabrik.de/europe/germany-latest.osm.pbf",
    ),
    "CH": Country(
        "CH",
        "Schweiz",
        "https://download.geofabrik.de/europe/switzerland-latest.osm.pbf",
    ),
    "IT": Country(
        "IT",
        "Italien",
        "https://download.geofabrik.de/europe/italy-latest.osm.pbf",
    ),
}


def get_country(code: str) -> Country:
    normalized = code.upper()
    try:
        return COUNTRIES[normalized]
    except KeyError as exc:
        allowed = ", ".join(COUNTRIES)
        raise ValueError(f"Unbekanntes Land {code!r}; erlaubt: {allowed}") from exc

