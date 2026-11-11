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

    p_init = sub.add_parser("ensure-data", help="build all regions if their catalog is missing")
    p_init.add_argument("--force", action="store_true")

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
        elif args.cmd == "ensure-data":
            from marsgis.pipeline import build_region, is_built

            for rid in list_regions():
                if args.force or not is_built(rid):
                    build_region(rid)
                else:
                    log.info("region data present", extra={"region": rid})
    except Exception:
        log.exception("command failed", extra={"cmd": args.cmd})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
