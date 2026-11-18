"""High-level API: plan the all-stations route from a starting station."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .exact import solve_exact
from .graph import to_minutes
from .itinerary import Step, build_itinerary, format_duration, format_itinerary_text, summarise
from .model import RouteModel, get_model
from .solver import (
    GTSPResult,
    SolverSettings,
    best_representatives,
    expand_walk,
    solve_each_start_line,
    target_stations,
)

ROUTE_BOOK_PATH = Path(__file__).resolve().parent / "data" / "route_book.json"


@dataclass
class RoutePlan:
    start_station: str
    start_line: str
    as_of: str
    reversal_penalty_min: float
    mode: str
    summary: dict[str, Any]
    steps: list[Step]
    per_line: list[dict[str, Any]]
    runs: list[dict[str, Any]]
    solver: dict[str, Any]
    optimality: dict[str, Any] | None = None
    network: dict[str, Any] = field(default_factory=dict)

    @property
    def total_minutes(self) -> float:
        return self.summary["total_minutes"]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["steps"] = [s.to_dict() for s in self.steps]
        return d

    def to_text(self, network) -> str:
        return format_report(self, network)


def network_fingerprint(model: RouteModel) -> str:
    """Hash of everything that affects route costs (stations, segments, transfers, penalty)."""
    net = model.network
    payload = {
        "stations": sorted(net.stations),
        "segments": sorted((s.line, s.a, s.b, s.minutes) for s in net.segments),
        "transfers": sorted((t.station, t.line_a, t.line_b, t.minutes) for t in net.transfers),
        "reversal": model.space.reversal_penalty_min,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def _run_dict(r: GTSPResult) -> dict[str, Any]:
    return {
        "line": r.start_line,
        "label": r.label,
        "minutes": r.minutes,
        "wall_time_s": r.wall_time_s,
        "status": r.status,
    }


# ------------------------------------------------------------------------- route book
class RouteBook:
    """Precomputed best routes (station order per start line) for one network configuration."""

    def __init__(self, path: str | Path = ROUTE_BOOK_PATH):
        self.path = Path(path)
        self.books: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.books = data.get("books", {})

    def lookup(self, model: RouteModel, station: str) -> dict[str, Any] | None:
        book = self.books.get(network_fingerprint(model))
        return None if book is None else book["routes"].get(station)

    def configurations(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in b.items() if k != "routes"} for b in self.books.values()]


def result_from_order(
    model: RouteModel, station: str, line: str, order_names: list[str], label: str
) -> GTSPResult:
    _, order = best_representatives(model, station, line, order_names)
    cost, walk = expand_walk(model, station, line, order)
    return GTSPResult(station, line, cost, walk, order, 0.0, "PRECOMPUTED", label)


# ---------------------------------------------------------------------------- planning
def plan_route(
    start: str,
    *,
    as_of: date | str | None = None,
    reversal_penalty_min: float | None = None,
    settings: SolverSettings | None = None,
    data_path: str | Path | None = None,
    exact_time_s: float = 0.0,
    use_route_book: bool = True,
    route_book: RouteBook | None = None,
    progress=None,
) -> RoutePlan:
    """Compute the fastest route that visits every MRT station, starting at ``start``.

    Every line serving the start station is evaluated as the first train taken; the fastest
    is returned.  ``exact_time_s > 0`` additionally runs the CP-SAT model to improve the
    route and prove a lower bound (optimality gap).
    """
    t0 = time.perf_counter()
    settings = (settings or SolverSettings()).validate()
    model = get_model(as_of, reversal_penalty_min, data_path)
    net = model.network
    station = net.find_station(start).name
    lines = list(net.stations[station].lines)

    cached = (route_book or RouteBook()).lookup(model, station) if use_route_book else None
    per_line_results: list[GTSPResult] = []
    if cached and set(cached["per_line"]) == set(lines):
        try:
            for line, entry in cached["per_line"].items():
                r = result_from_order(model, station, line, entry["order"], entry.get("label", "route book"))
                if abs(r.minutes - entry["minutes"]) > 0.01:
                    raise ValueError("stale route book entry")
                per_line_results.append(r)
        except (KeyError, ValueError, IndexError):
            per_line_results, cached = [], None
    else:
        cached = None
    if cached:
        per_line_results.sort(key=lambda r: (r.cost, lines.index(r.start_line)))
        runs = [_run_dict(r) for r in per_line_results]
        source = "route book (precomputed offline)"
    else:
        per_line_results, all_runs = solve_each_start_line(model, station, settings)
        runs = [_run_dict(r) for r in all_runs]
        source = "live solve"

    best = per_line_results[0]
    optimality = None
    if exact_time_s > 0:
        exact = solve_exact(
            model, station, None, time_limit_s=exact_time_s, hint_walk=best.walk, progress=progress
        )
        if exact.walk is not None and exact.cost is not None and exact.cost < best.cost:
            first_line = model.space.graph.nodes[model.space.nodes[exact.walk[0]]]["line"]
            improved = GTSPResult(
                station,
                first_line,
                exact.cost,
                exact.walk,
                [],
                exact.wall_time_s,
                exact.status,
                "CP-SAT exact (improved on GLS)",
            )
            # the improved route replaces that start line's entry in the comparison
            per_line_results = [r for r in per_line_results if r.start_line != first_line] + [improved]
            per_line_results.sort(key=lambda r: (r.cost, lines.index(r.start_line)))
            best = per_line_results[0]
        optimality = {
            "status": exact.status,
            "proven_optimal": exact.proven_optimal,
            "lower_bound_minutes": to_minutes(exact.lower_bound),
            "best_minutes": to_minutes(exact.cost) if exact.cost is not None else None,
            "gap_percent": round(100 * exact.gap, 2) if exact.gap is not None else None,
            "improved_heuristic": exact.improved,
            "rounds": exact.rounds,
            "cuts": exact.cuts,
            "wall_time_s": exact.wall_time_s,
            "log": exact.log,
        }
    elif cached and cached.get("exact") and cached["exact"].get("best_minutes") == to_minutes(best.cost):
        optimality = cached["exact"]

    steps = build_itinerary(net, model.space, best.walk)
    summary = summarise(net, steps)
    summary["model_cost_minutes"] = to_minutes(best.cost)

    per_line = [
        {
            "line": r.start_line,
            "line_name": net.lines[r.start_line].name,
            "minutes": r.minutes,
            "hm": format_duration(r.minutes),
            "best": r is per_line_results[0],
            "method": r.label,
        }
        for r in per_line_results
    ]
    return RoutePlan(
        start_station=station,
        start_line=best.start_line,
        as_of=net.as_of.isoformat(),
        reversal_penalty_min=model.space.reversal_penalty_min,
        mode=model.space.mode,
        summary=summary,
        steps=steps,
        per_line=per_line,
        runs=runs,
        solver={
            "source": source,
            "method": best.label,
            "metaheuristic": settings.metaheuristic,
            "first_solution_strategy": settings.first_solution_strategy,
            "time_limit_s_per_run": settings.time_limit_s,
            "lambdas": list(settings.lambdas),
            "warm_start": settings.warm_start,
            "targets": len(target_stations(model, station)),
            "wall_time_s": round(time.perf_counter() - t0, 2),
            "trace": best.trace,
            "fingerprint": network_fingerprint(model),
        },
        optimality=optimality,
        network=model.describe(),
    )


def format_report(plan: RoutePlan, network) -> str:
    s = plan.summary
    out = [
        f"All-stations MRT route from {plan.start_station}",
        "=" * 64,
        f"Network date        : {plan.as_of}  ({s['stations_total']} stations, "
        f"{plan.network.get('platform_nodes')} platform nodes)",
        f"Model               : {plan.mode} graph, reversal penalty {plan.reversal_penalty_min:g} min",
        f"Total travel time   : {s['total_hm']}  ({s['total_minutes']:g} min)",
        f"Stations covered    : {s['stations_covered']} / {s['stations_total']}",
        f"Start line          : {plan.start_line}   End station: {s['end_station']}",
        f"Rides / transfers   : {s['ride_legs']} legs, {s['transfers']} transfers "
        f"({s['transfer_minutes']:g} min), {s['reversals']} reversals ({s['reversal_minutes']:g} min)",
        f"In-train time       : {s['in_train_minutes']:g} min over {s['stops_travelled']} stops "
        f"({s['revisited_stops']} repeat passes)",
    ]
    if len(plan.per_line) > 1:
        out.append(
            "Start-line comparison: "
            + ", ".join(f"{p['line']} {p['hm']}{' (best)' if p['best'] else ''}" for p in plan.per_line)
        )
    out.append(
        f"Solver              : {plan.solver['source']}; best run: {plan.solver['method']} "
        f"({plan.solver['wall_time_s']} s)"
    )
    if plan.optimality:
        o = plan.optimality
        if o.get("proven_optimal"):
            out.append(
                f"Optimality          : proven optimal (CP-SAT lower bound {o['lower_bound_minutes']:g} min)"
            )
        elif o.get("gap_percent") is not None:
            out.append(
                f"Optimality          : within {o['gap_percent']}% of optimal "
                f"(lower bound {o['lower_bound_minutes']:g} min)"
            )
    out.append("")
    out.append("Itinerary (clock = cumulative h:mm at the end of each step)")
    out.append("-" * 64)
    out.append(format_itinerary_text(network, plan.steps))
    return "\n".join(out)
