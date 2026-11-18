"""Command-line interface.

python -m mrt_route Bedok                      # plan from Bedok (default settings)
python -m mrt_route route "Dhoby Ghaut" --time-limit 10 --exact 300
python -m mrt_route stations                   # list stations and codes
python -m mrt_route network                    # model / graph statistics
python -m mrt_route serve --port 8000          # start the web app
python -m mrt_route precompute                 # rebuild the route book
"""

from __future__ import annotations

import argparse
import json
import sys

from .network import StationNotFoundError, load_network
from .planner import format_report, plan_route
from .solver import SolverSettings, shutdown_pool

SUBCOMMANDS = {"route", "stations", "network", "serve", "precompute"}
UPCOMING_DATE = "2026-12-26"


def _floats(text: str) -> list[float]:
    return [float(x) for x in text.split(",") if x.strip()]


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as-of", help="network date YYYY-MM-DD (default: today)")
    p.add_argument(
        "--include-upcoming",
        action="store_true",
        help=f"include stations opening on {UPCOMING_DATE} (TEL Stage 5, DTL3e)",
    )
    p.add_argument(
        "--reversal-penalty",
        type=float,
        default=None,
        help="minutes to turn back (alight + wait for the opposite train); 0 = pure two-layer graph",
    )
    p.add_argument("--data", help="alternative network JSON file")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mrt_route", description="All-stations Singapore MRT route planner"
    )
    sub = parser.add_subparsers(dest="command")

    r = sub.add_parser("route", help="plan the all-stations route from a station")
    r.add_argument("start", help="starting station name or code, e.g. 'Bedok', 'Orchard', 'NS24'")
    _common(r)
    r.add_argument("--time-limit", type=float, default=6.0, help="seconds per Guided Local Search run (default 6)")
    r.add_argument("--lambdas", type=_floats, default=[0.3, 1.0, 2.5], help="GLS lambda portfolio, e.g. 0.3,1,2.5")
    r.add_argument("--no-warm-start", action="store_true", help="disable the two-layer warm start")
    r.add_argument("--workers", type=int, default=None, help="parallel solver processes (default: CPU count)")
    r.add_argument(
        "--exact",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="also run the CP-SAT exact model to improve/prove optimality",
    )
    r.add_argument("--live", action="store_true", help="ignore the precomputed route book and solve now")
    r.add_argument("--json", metavar="FILE", help="write the full plan as JSON ('-' for stdout)")
    r.add_argument("--quiet", action="store_true", help="only print the summary")

    s = sub.add_parser("stations", help="list stations")
    _common(s)

    n = sub.add_parser("network", help="print network and graph statistics")
    _common(n)

    w = sub.add_parser("serve", help="run the web app")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)
    w.add_argument("--debug", action="store_true")

    p = sub.add_parser("precompute", help="rebuild the precomputed route book")
    p.add_argument("--out", default=None, help="route book path (default: bundled data/route_book.json)")
    p.add_argument("--as-of", action="append", help="network date(s); repeatable")
    p.add_argument("--reversal-penalty", type=float, action="append", help="penalty value(s); repeatable")
    p.add_argument("--data", help="alternative network JSON file")
    p.add_argument("--stations", nargs="*", help="subset of stations")
    p.add_argument("--time-limit", type=float, default=10.0)
    p.add_argument("--lambdas", type=_floats, default=[0.3, 0.6, 1.0, 2.5])
    p.add_argument("--no-warm-start", action="store_true")
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--exact", type=float, default=0.0, metavar="SECONDS")
    p.add_argument("--no-resume", action="store_true", help="recompute stations already in the book")
    return parser


def _as_of(args) -> str | None:
    return UPCOMING_DATE if getattr(args, "include_upcoming", False) else args.as_of


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in SUBCOMMANDS and not argv[0].startswith("-"):
        argv.insert(0, "route")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == "route":
            return _route(args)
        if args.command == "stations":
            net = load_network(args.data, as_of=_as_of(args))
            for st in sorted(net.stations.values(), key=lambda s: s.name):
                print(f"{st.name:<22} {st.code_label}")
            print(f"\n{len(net.stations)} stations ({len(net.interchanges)} interchanges) as of {net.as_of}")
            return 0
        if args.command == "network":
            from .model import get_model

            model = get_model(_as_of(args), args.reversal_penalty, args.data)
            print(json.dumps(model.describe(), indent=2, default=str))
            return 0
        if args.command == "serve":
            from webapp.app import create_app

            create_app().run(host=args.host, port=args.port, debug=args.debug, threaded=True)
            return 0
        if args.command == "precompute":
            from .planner import ROUTE_BOOK_PATH
            from .precompute import main_precompute

            args.out = args.out or str(ROUTE_BOOK_PATH)
            return main_precompute(args)
    except StationNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        shutdown_pool()
    return 1


def _route(args) -> int:
    settings = SolverSettings(
        time_limit_s=args.time_limit,
        lambdas=tuple(args.lambdas),
        warm_start=not args.no_warm_start,
        workers=args.workers,
    )
    progress = (lambda line: print(f"  [exact] {line}", file=sys.stderr)) if args.exact else None
    plan = plan_route(
        args.start,
        as_of=_as_of(args),
        reversal_penalty_min=args.reversal_penalty,
        settings=settings,
        data_path=args.data,
        exact_time_s=args.exact,
        use_route_book=not args.live,
        progress=progress,
    )
    if args.json:
        text = json.dumps(plan.to_dict(), indent=2, ensure_ascii=False, default=str)
        if args.json == "-":
            print(text)
            return 0
        with open(args.json, "w", encoding="utf-8") as fh:
            fh.write(text)
    from .model import get_model

    net = get_model(plan.as_of, plan.reversal_penalty_min, args.data).network
    report = format_report(plan, net)
    if args.quiet:
        report = report.split("\nItinerary")[0]
    print(report)
    return 0
