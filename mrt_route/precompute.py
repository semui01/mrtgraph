"""Offline precomputation of the route book (best route from every station, every start line).

The web app serves these instantly for the default configuration and falls back to a live
solve for anything else.  Entries store only the station visiting order per start line; the
route is rebuilt at load time from the all-pairs shortest paths and checked against the
stored travel time, so a stale book is detected and ignored automatically.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from collections.abc import Callable

from .exact import solve_exact
from .graph import to_minutes
from .model import RouteModel, get_model
from .planner import ROUTE_BOOK_PATH, network_fingerprint
from .solver import SolveJob, SolverSettings, run_jobs, solve_each_start_line, station_order, target_stations


def _load(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"books": {}}


def dumps_book(data: dict[str, Any]) -> str:
    """JSON with one compact line per station: small, and diffs stay readable."""
    out = ["{", ' "books": {']
    books = list(data["books"].items())
    for bi, (fp, book) in enumerate(books):
        out.append(f"  {json.dumps(fp)}: {{")
        for key, value in book.items():
            if key != "routes":
                out.append(f"   {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)},")
        out.append('   "routes": {')
        routes = sorted(book["routes"].items())
        for ri, (station, entry) in enumerate(routes):
            line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
            out.append(
                f"    {json.dumps(station, ensure_ascii=False)}: {line}" + ("," if ri < len(routes) - 1 else "")
            )
        out.append("   }")
        out.append("  }" + ("," if bi < len(books) - 1 else ""))
    out.append(" }")
    out.append("}")
    return "\n".join(out) + "\n"


def _save(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(dumps_book(data), encoding="utf-8")
    tmp.replace(path)


def precompute_book(
    model: RouteModel,
    settings: SolverSettings,
    path: Path = ROUTE_BOOK_PATH,
    stations: list[str] | None = None,
    exact_time_s: float = 0.0,
    resume: bool = True,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    data = _load(path)
    fp = network_fingerprint(model)
    book = data["books"].setdefault(
        fp,
        {
            "as_of": model.network.as_of.isoformat(),
            "reversal_penalty_min": model.space.reversal_penalty_min,
            "stations": len(model.network.stations),
            "routes": {},
        },
    )
    book["settings"] = {
        "time_limit_s": settings.time_limit_s,
        "lambdas": list(settings.lambdas),
        "warm_start": settings.warm_start,
        "metaheuristic": settings.metaheuristic,
    }
    names = stations or sorted(model.network.stations)
    for i, station in enumerate(names, 1):
        existing = book["routes"].get(station)
        if resume and existing and not (exact_time_s and not existing.get("exact")):
            continue
        t = time.perf_counter()
        best_per_line, _ = solve_each_start_line(model, station, settings)
        keep = set(target_stations(model, station))
        entry: dict[str, Any] = {"per_line": {}}
        for r in best_per_line:
            prev = (existing or {}).get("per_line", {}).get(r.start_line)
            if prev and prev["minutes"] < r.minutes:  # keep the better of old and new
                entry["per_line"][r.start_line] = prev
                continue
            entry["per_line"][r.start_line] = {
                "minutes": r.minutes,
                "label": r.label,
                "order": station_order(model, station, r.walk, keep),
            }
        best_line = min(entry["per_line"], key=lambda ln: entry["per_line"][ln]["minutes"])
        entry["best_line"] = best_line
        if exact_time_s > 0:
            best = next(r for r in best_per_line if r.start_line == best_line)
            ex = solve_exact(model, station, None, time_limit_s=exact_time_s, hint_walk=best.walk)
            entry["exact"] = {
                "status": ex.status,
                "proven_optimal": ex.proven_optimal,
                "lower_bound_minutes": to_minutes(ex.lower_bound),
                "best_minutes": to_minutes(ex.cost) if ex.cost is not None else None,
                "gap_percent": round(100 * ex.gap, 2) if ex.gap is not None else None,
                "wall_time_s": ex.wall_time_s,
            }
            if ex.walk is not None and ex.cost is not None and ex.cost < best.cost:
                first = model.space.nodes[ex.walk[0]]
                line = model.space.graph.nodes[first]["line"]
                entry["per_line"][line] = {
                    "minutes": to_minutes(ex.cost),
                    "label": "CP-SAT exact",
                    "order": station_order(model, station, ex.walk, keep),
                }
                entry["best_line"] = line
        entry["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        book["routes"][station] = entry
        _save(path, data)
        mins = entry["per_line"][entry["best_line"]]["minutes"]
        log(
            f"[{i}/{len(names)}] {station}: {mins:g} min via {entry['best_line']} ({time.perf_counter() - t:.1f}s)"
        )
    return book


def polish_book(
    model: RouteModel,
    settings: SolverSettings,
    path: Path = ROUTE_BOOK_PATH,
    log: Callable[[str], None] = print,
) -> int:
    """Re-solve every (station, start line) warm-started from good station orders nearby.

    Seeds are the best orders of the same station (other start lines) and of adjacent
    stations: routes from neighbouring starts share most of their structure, so this is a
    cheap way to escape local optima of individual runs.  Returns the number of improvements.
    """
    data = _load(path)
    book = data["books"].get(network_fingerprint(model))
    if not book:
        raise ValueError("No route book for this network configuration; run precompute first")
    routes = book["routes"]
    neighbours: dict[str, set[str]] = {s: set() for s in model.network.stations}
    for seg in model.network.segments:
        neighbours[seg.a].add(seg.b)
        neighbours[seg.b].add(seg.a)

    def best_order(station: str) -> list[str]:
        entry = routes[station]
        return entry["per_line"][min(entry["per_line"], key=lambda ln: entry["per_line"][ln]["minutes"])]["order"]

    jobs: list[SolveJob] = []
    for station in sorted(routes):
        keep = set(target_stations(model, station))
        seeds = {tuple(best_order(station))}
        for nb in neighbours[station]:
            if nb in routes:
                # neighbour's order, with the neighbour itself visited first when it is a target
                order = [nb] if nb in keep else []
                order += [s for s in best_order(nb) if s in keep and s != nb]
                order += [s for s in sorted(keep) if s not in order]  # stations implied there, not here
                seeds.add(tuple(order))
        for line in routes[station]["per_line"]:
            for seed in seeds:
                jobs.append(
                    SolveJob(
                        model.network.as_of,
                        model.space.reversal_penalty_min,
                        model.data_path,
                        station,
                        line,
                        0.3,
                        False,
                        settings,
                        seed,
                        "seeded from nearby routes + GLS lambda=0.3",
                    )
                )
    log(f"Polishing {len(routes)} stations with {len(jobs)} seeded runs ...")
    results = run_jobs(model, jobs, settings)
    improved = 0
    for job, r in zip(jobs, results):
        entry = routes[job.start_station]
        current = entry["per_line"][job.start_line]
        if r.minutes < current["minutes"] - 1e-9:
            keep = set(target_stations(model, job.start_station))
            log(f"  {job.start_station} ({job.start_line}): {current['minutes']:g} -> {r.minutes:g} min")
            entry["per_line"][job.start_line] = {
                "minutes": r.minutes,
                "label": "polish: " + r.label,
                "order": station_order(model, job.start_station, r.walk, keep),
            }
            improved += 1
        entry["best_line"] = min(entry["per_line"], key=lambda ln: entry["per_line"][ln]["minutes"])
    _save(path, data)
    log(f"{improved} improvement(s)")
    return improved


def main_precompute(args) -> int:
    settings = SolverSettings(
        time_limit_s=args.time_limit,
        lambdas=tuple(args.lambdas),
        warm_start=not args.no_warm_start,
        workers=args.workers,
    )
    for as_of in args.as_of or [None]:
        for penalty in args.reversal_penalty or [None]:
            model = get_model(as_of, penalty, args.data)
            print(
                f"Route book for {model.network.as_of} ({len(model.network.stations)} stations), "
                f"reversal penalty {model.space.reversal_penalty_min:g} min -> {args.out}"
            )
            if not args.polish_only:
                precompute_book(
                    model,
                    settings,
                    Path(args.out),
                    stations=[model.network.find_station(s).name for s in args.stations]
                    if args.stations
                    else None,
                    exact_time_s=args.exact,
                    resume=not args.no_resume,
                )
            for _ in range(args.polish):
                if polish_book(model, settings, Path(args.out)) == 0:
                    break
    return 0
