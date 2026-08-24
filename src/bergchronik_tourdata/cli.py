from __future__ import annotations

import argparse
import json
from pathlib import Path

from .countries import get_country
from .pipeline import build_catalog, search_catalog
from .summit_ascents import build_all_summit_ascents, build_summit_ascent


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bergchronik-tourdata",
        description="OpenStreetMap-Wanderrouten für BergChronik aufbereiten.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="Länderkatalog erzeugen")
    build.add_argument("--input", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--country", required=True, choices=["AT", "DE", "CH", "IT"])
    build.add_argument("--source-timestamp")
    build.add_argument("--include-gpx", action="store_true")

    search = commands.add_parser("search", help="Katalog durchsuchen")
    search.add_argument("--database", type=Path, required=True)
    search.add_argument("--query", required=True)
    search.add_argument("--country")
    search.add_argument("--limit", type=int, default=20)

    summit = commands.add_parser("summit-ascent", help="OSM-Gipfelaufstieg erzeugen")
    summit.add_argument("--input", type=Path, required=True)
    summit.add_argument("--output", type=Path, required=True)
    summit.add_argument("--country", required=True, choices=["AT", "DE", "CH", "IT"])
    summit.add_argument("--peak", required=True)
    summit.add_argument("--radius-km", type=float, default=30.0)

    all_summits = commands.add_parser(
        "summit-ascents", help="Alle erreichbaren OSM-Gipfelaufstiege eines Landes erzeugen"
    )
    all_summits.add_argument("--input", type=Path, required=True)
    all_summits.add_argument("--output", type=Path, required=True)
    all_summits.add_argument("--country", required=True, choices=["AT", "DE", "CH", "IT"])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "build":
        stats = build_catalog(
            args.input,
            args.output,
            get_country(args.country),
            args.source_timestamp,
            args.include_gpx,
        )
        print(json.dumps(stats.__dict__, ensure_ascii=False, indent=2))
        return 0

    if args.command == "summit-ascent":
        result = build_summit_ascent(args.input, args.output, args.country, args.peak, args.radius_km)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "summit-ascents":
        result = build_all_summit_ascents(args.input, args.output, args.country)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    results = search_catalog(
        args.database,
        args.query,
        args.country,
        args.limit,
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0
