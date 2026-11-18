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
from .solver import SolverSettings, solve_each_start_line, station_order, target_stations


def _load(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"books": {}}


def _save(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
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
            precompute_book(
                model,
                settings,
                Path(args.out),
                stations=[model.network.find_station(s).name for s in args.stations] if args.stations else None,
                exact_time_s=args.exact,
                resume=not args.no_resume,
            )
    return 0
