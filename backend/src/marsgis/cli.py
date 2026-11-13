"""Command line entry point: `marsgis <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from marsgis.config import get_settings, list_regions, load_sources
from marsgis.logs import configure_logging

log = logging.getLogger("marsgis.cli")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="marsgis", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="download raw sources (verbatim, with provenance)")
    p_fetch.add_argument("source", nargs="*", help="source ids (default: all)")

    p_inspect = sub.add_parser("inspect", help="print raster metadata as JSON")
    p_inspect.add_argument("path", help="local path or URL (GDAL /vsicurl/ for URLs)")

    p_build = sub.add_parser("build", help="build processed layers for a region")
    p_build.add_argument("--region", default=None, help="region id (default: all regions)")
    p_build.add_argument("--layer", action="append", help="only (re)publish these layers")

    p_init = sub.add_parser(
        "ensure-data",
        help="bring every region up to date with its config (idempotent; fast no-op when "
             "nothing changed)")
    p_init.add_argument("--force", action="store_true", help=argparse.SUPPRESS)  # compat

    p_fs = sub.add_parser("feature-stats", help="exact quantiles of a published layer")
    p_fs.add_argument("--region", default="jezero")
    p_fs.add_argument("--layer", required=True)
    p_fs.add_argument("--above", type=float, action="append", default=[],
                      help="also report the fraction of cells above this value")

    p_cmp = sub.add_parser("compare-dtm", help="vertical comparison of two elevation layers")
    p_cmp.add_argument("--region", default="jezero")
    p_cmp.add_argument("--fine", default="hirise_elevation")
    p_cmp.add_argument("--coarse", default="elevation")

    p_route = sub.add_parser("route", help="plan a route (prints JSON summary)")
    p_route.add_argument("--region", default="jezero")
    p_route.add_argument("--vehicle", default="perseverance")
    p_route.add_argument("--engine", default="dijkstra", choices=["dijkstra", "mcp"])
    p_route.add_argument("--margin", type=float, default=None, help="planning margin (m)")
    p_route.add_argument("lonlat", nargs="+", help="waypoints as lon,lat (deg E, planetocentric)")
    p_route.add_argument("--full", action="store_true", help="print the full result")

    p_sens = sub.add_parser("sensitivity",
                            help="re-plan a route under varied vehicle parameters")
    p_sens.add_argument("--region", default="jezero")
    p_sens.add_argument("--vehicle", default="perseverance")
    p_sens.add_argument("lonlat", nargs="+", help="waypoints as lon,lat")

    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    try:
        if args.cmd == "fetch":
            from marsgis.ingest import fetch_source, validate_source

            sources = load_sources()
            for sid in args.source or list(sources):
                validate_source(fetch_source(sources[sid]))
        elif args.cmd == "inspect":
            from marsgis.ingest import describe_raster

            print(json.dumps(describe_raster(args.path), indent=2, default=str))
        elif args.cmd == "build":
            from marsgis.pipeline import build_region

            for rid in [args.region] if args.region else list_regions():
                versions = build_region(rid, only=args.layer)
                print(json.dumps({"region": rid, "versions": versions}, indent=2))
        elif args.cmd == "feature-stats":
            from marsgis.catalog import layer_path
            from marsgis.quality import feature_stats

            print(json.dumps(feature_stats(layer_path(args.region, args.layer),
                                           tuple(args.above)), indent=2))
        elif args.cmd == "compare-dtm":
            from marsgis.catalog import layer_path
            from marsgis.quality import compare_dtms

            print(json.dumps(compare_dtms(layer_path(args.region, args.fine),
                                          layer_path(args.region, args.coarse)), indent=2))
        elif args.cmd == "route":
            from marsgis.routing import plan_for_region

            pts = [tuple(map(float, p.split(","))) for p in args.lonlat]
            res = plan_for_region(args.region, args.vehicle,
                                  [{"lon": lon, "lat": lat} for lon, lat in pts],
                                  engine=args.engine, margin_m=args.margin)
            if not args.full:
                res = {k: res[k] for k in ("summary", "legs", "warnings", "class_runs",
                                           "compute_ms", "inputs")}
            print(json.dumps(res, indent=2))
        elif args.cmd == "sensitivity":
            from marsgis.sensitivity import run_sensitivity

            pts = [tuple(map(float, p.split(","))) for p in args.lonlat]
            print(json.dumps(run_sensitivity(args.region, args.vehicle, pts), indent=2))
        elif args.cmd == "ensure-data":
            from marsgis.pipeline import build_region

            # Always run the content-addressed build: unchanged layers are skipped by
            # version, and a changed recipe/vehicle/source declaration is rebuilt instead
            # of silently serving stale layers.
            for rid in list_regions():
                build_region(rid)
                log.info("region data up to date", extra={"region": rid})
    except Exception:
        log.exception("command failed", extra={"cmd": args.cmd})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
