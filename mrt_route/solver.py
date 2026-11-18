"""Generalised (set) TSP solver built on Google OR-Tools (Guided Local Search).

Formulation
-----------
* Every physical station except the start is a *cluster* of graph nodes (its platforms, or
  its direction-aware arrival states).  ``AddDisjunction(cluster)`` with no penalty makes
  exactly one node of each cluster mandatory: visiting *any* platform visits the station.
* Distances are the precomputed all-pairs shortest-path costs (metric closure), so moving
  between two chosen nodes may pass through other stations; those pass-throughs are free
  coverage, which makes the GTSP optimum equal to the optimum walk (Steiner TSP).
* Stations that every complete route must pass through anyway (articulation points of the
  network seen from the start, e.g. Jurong East ... Tuas West Road on the way to Tuas Link)
  are dropped as targets.  This is exact and shrinks the search space by ~25 %.
* Node 0 is a virtual *source*: its distance to node ``j`` is the cheapest way to reach ``j``
  after boarding a train of the chosen start line at the start station.
* Node 1 is a dummy *end* depot reachable from every node at cost 0, turning the closed tour
  into an open path that finishes wherever is best.
* Search: PATH_CHEAPEST_ARC first solution + Guided Local Search under a time limit.  Because
  GLS is sensitive to its penalty factor (lambda) on this degenerate metric, a small
  *portfolio* of lambdas runs in parallel, plus (with reversal penalties) a run warm-started
  from the two-layer solution.  Every run ends with an exact dynamic-programming pass that
  re-picks the best node inside each cluster for the station order found.
* At an interchange every line is solved as the first train taken; afterwards each line
  that lost is re-solved once more, warm-started from the winning route's station order.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import threading
import time
from concurrent.futures import Executor, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field, replace
from datetime import date

import networkx as nx
import numpy as np
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from .graph import to_minutes
from .model import RouteModel, get_model
from .paths import UNREACHABLE

SOURCE, SINK = 0, 1

FIRST_SOLUTION_STRATEGIES = (
    "PATH_CHEAPEST_ARC",
    "PATH_MOST_CONSTRAINED_ARC",
    "GLOBAL_CHEAPEST_ARC",
    "LOCAL_CHEAPEST_ARC",
    "LOCAL_CHEAPEST_INSERTION",
    "PARALLEL_CHEAPEST_INSERTION",
    "SEQUENTIAL_CHEAPEST_INSERTION",
    "SAVINGS",
    "CHRISTOFIDES",
    "AUTOMATIC",
)
METAHEURISTICS = ("GUIDED_LOCAL_SEARCH", "SIMULATED_ANNEALING", "TABU_SEARCH", "GREEDY_DESCENT", "AUTOMATIC")

_STATUS_NAMES = {
    v.number: v.name.removeprefix("ROUTING_")
    for v in routing_enums_pb2.RoutingSearchStatus.DESCRIPTOR.enum_types[0].values
}


@dataclass
class SolverSettings:
    """Search configuration.  ``time_limit_s`` applies to each portfolio run (they run in parallel)."""

    time_limit_s: float = 6.0
    lambdas: tuple[float, ...] = (0.3, 1.0, 2.5)
    warm_start: bool = True
    first_solution_strategy: str = "PATH_CHEAPEST_ARC"
    metaheuristic: str = "GUIDED_LOCAL_SEARCH"
    workers: int | None = None  # parallel processes; None = min(CPU count, runs); 1 = in-process
    log_search: bool = False

    def validate(self) -> SolverSettings:
        if not (0.1 <= self.time_limit_s <= 3600):
            raise ValueError("time_limit_s must be between 0.1 and 3600 seconds")
        if not self.lambdas or any(not (0 < lam <= 100) for lam in self.lambdas):
            raise ValueError("lambdas must be a non-empty list of values in (0, 100]")
        if self.first_solution_strategy not in FIRST_SOLUTION_STRATEGIES:
            raise ValueError(f"Unknown first solution strategy {self.first_solution_strategy}")
        if self.metaheuristic not in METAHEURISTICS:
            raise ValueError(f"Unknown metaheuristic {self.metaheuristic}")
        return self


@dataclass
class GTSPResult:
    start_station: str
    start_line: str
    cost: int  # objective in model cost units (ms + change tie-breakers)
    walk: list[int]  # search-space node indices of the complete journey (first = boarding node)
    visit_order: list[int]  # chosen representative node per target station, in visiting order
    wall_time_s: float
    status: str
    label: str = ""  # portfolio member, e.g. "GLS lambda=1.0"
    trace: list[tuple[float, float]] = field(default_factory=list)  # (seconds, minutes)

    @property
    def minutes(self) -> float:
        return to_minutes(self.cost)


# --------------------------------------------------------------------------- model pieces
def implied_stations(model: RouteModel, start_station: str) -> set[str]:
    """Stations every complete route must pass through anyway (so they need not be targets).

    In the station graph (rides only - transfers stay inside a station) an articulation
    point S other than the start separates some stations from the start: they can only be
    reached through S, and at least one of them stays a target, so visiting S is implied.
    """
    g = nx.Graph()
    g.add_nodes_from(model.network.stations)
    g.add_edges_from((seg.a, seg.b) for seg in model.network.segments)
    return set(nx.articulation_points(g)) - {start_station}


def target_stations(model: RouteModel, start_station: str, reduce: bool = True) -> list[str]:
    skip = implied_stations(model, start_station) if reduce else set()
    return [s for s in sorted(model.space.targets) if s != start_station and s not in skip]


def build_cost_matrix(model: RouteModel, start_station: str, start_line: str, reduce: bool = True):
    """Return ``(matrix, target_nodes, clusters, best_start_for_target)`` for OR-Tools."""
    space, dist = model.space, model.apsp.dist
    starts = np.array(space.start_nodes(start_station, start_line), dtype=np.int64)
    clusters: list[list[int]] = []
    targets: list[int] = []
    for station in target_stations(model, start_station, reduce):
        members = space.targets[station]
        clusters.append(list(range(2 + len(targets), 2 + len(targets) + len(members))))
        targets.extend(members)
    t = np.array(targets, dtype=np.int64)

    src_block = dist[np.ix_(starts, t)]
    src_row = src_block.min(axis=0)
    best_start = starts[src_block.argmin(axis=0)]

    m = 2 + len(t)
    matrix = np.zeros((m, m), dtype=np.int64)
    matrix[2:, 2:] = dist[np.ix_(t, t)]
    matrix[SOURCE, 2:] = src_row
    finite = matrix[matrix < UNREACHABLE]
    big = int(finite.max()) * 4 + 1 if finite.size else 1
    matrix[matrix >= UNREACHABLE] = big
    matrix[SOURCE, SINK] = big  # an empty route is never allowed
    matrix[2:, SINK] = 0  # open path: finishing anywhere is free
    matrix[SINK, :] = 0
    matrix[:, SOURCE] = 0
    np.fill_diagonal(matrix, 0)
    return matrix, t, clusters, best_start


def best_representatives(
    model: RouteModel, start_station: str, start_line: str, stations: list[str]
) -> tuple[int, list[int]]:
    """Exact DP: for a fixed station order, pick the cheapest node in every cluster.

    This is a shortest path through a layered graph (one layer per station), O(n k^2).
    """
    space, dist = model.space, model.apsp.dist
    starts = space.start_nodes(start_station, start_line)
    layer = space.targets[stations[0]]
    cost = {v: min(int(dist[s, v]) for s in starts) for v in layer}
    back: list[dict[int, int]] = []
    for st in stations[1:]:
        nxt = space.targets[st]
        new_cost, ptr = {}, {}
        for v in nxt:
            u = min(cost, key=lambda u: cost[u] + int(dist[u, v]))
            new_cost[v], ptr[v] = cost[u] + int(dist[u, v]), u
        cost = new_cost
        back.append(ptr)
    last = min(cost, key=cost.get)
    seq = [last]
    for ptr in reversed(back):
        seq.append(ptr[seq[-1]])
    seq.reverse()
    return cost[last], seq


def expand_walk(model: RouteModel, start_station: str, start_line: str, order: list[int]) -> tuple[int, list[int]]:
    """Full node walk for a visiting order (boarding node chosen optimally); returns (cost, walk)."""
    space, apsp = model.space, model.apsp
    starts = space.start_nodes(start_station, start_line)
    first = min(starts, key=lambda s: int(apsp.dist[s, order[0]]))
    walk = apsp.path(first, order[0])
    cost = int(apsp.dist[first, order[0]])
    for a, b in zip(order, order[1:]):
        walk.extend(apsp.path(a, b)[1:])
        cost += int(apsp.dist[a, b])
    return cost, walk


def station_order(model: RouteModel, start_station: str, walk: list[int], keep: set[str]) -> list[str]:
    """Stations of ``keep`` in order of first visit along a walk."""
    seen: set[str] = set()
    out: list[str] = []
    for i in walk:
        st = model.space.station_of[i]
        if st in keep and st not in seen and st != start_station:
            seen.add(st)
            out.append(st)
    return out


# --------------------------------------------------------------------------------- search
def solve_gtsp(
    model: RouteModel,
    start_station: str,
    start_line: str,
    settings: SolverSettings | None = None,
    lam: float | None = None,
    initial_order: list[int] | None = None,
    label: str = "",
) -> GTSPResult:
    """One OR-Tools run (first solution or ``initial_order`` -> Guided Local Search -> DP polish)."""
    settings = (settings or SolverSettings()).validate()
    started = time.perf_counter()
    matrix, targets, clusters, _ = build_cost_matrix(model, start_station, start_line)
    position = {int(t): 2 + k for k, t in enumerate(targets)}

    manager = pywrapcp.RoutingIndexManager(matrix.shape[0], 1, [SOURCE], [SINK])
    routing = pywrapcp.RoutingModel(manager)
    transit = routing.RegisterTransitMatrix(matrix.tolist())
    routing.SetArcCostEvaluatorOfAllVehicles(transit)
    for cluster in clusters:
        # no penalty => exactly one member of the cluster must be visited
        routing.AddDisjunction([manager.NodeToIndex(n) for n in cluster])

    trace: list[tuple[float, float]] = []

    def on_solution() -> None:
        trace.append((round(time.perf_counter() - started, 3), to_minutes(routing.CostVar().Max())))

    routing.AddAtSolutionCallback(on_solution)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = getattr(
        routing_enums_pb2.FirstSolutionStrategy, settings.first_solution_strategy
    )
    params.local_search_metaheuristic = getattr(routing_enums_pb2.LocalSearchMetaheuristic, settings.metaheuristic)
    if lam is not None:
        params.guided_local_search_lambda_coefficient = lam
    params.time_limit.FromMilliseconds(int(settings.time_limit_s * 1000))
    params.log_search = settings.log_search

    if initial_order:
        routing.CloseModelWithParameters(params)
        route = [manager.NodeToIndex(position[v]) for v in initial_order if v in position]
        initial = routing.ReadAssignmentFromRoutes([route], True)
        solution = routing.SolveFromAssignmentWithParameters(initial, params) if initial else None
    else:
        solution = routing.SolveWithParameters(params)
    if solution is None:
        raise RuntimeError(f"OR-Tools found no solution from {start_station} ({start_line})")

    order: list[int] = []
    index = routing.Start(0)
    while not routing.IsEnd(index):
        node = manager.IndexToNode(index)
        if node >= 2:
            order.append(int(targets[node - 2]))
        index = solution.Value(routing.NextVar(index))
    gls_cost = int(solution.ObjectiveValue())

    # Exact cluster re-optimisation for the station order found (never worse).
    stations = [model.space.station_of[v] for v in order]
    _, order = best_representatives(model, start_station, start_line, stations)
    cost, walk = expand_walk(model, start_station, start_line, order)
    if cost > gls_cost:  # pragma: no cover - DP is exact for the order, so this cannot happen
        raise RuntimeError("Cluster re-optimisation made the route worse")
    covered = {model.space.station_of[i] for i in walk}
    if covered != set(model.network.stations):  # pragma: no cover - defensive
        raise RuntimeError(f"Route misses stations: {sorted(set(model.network.stations) - covered)[:5]}")

    return GTSPResult(
        start_station=start_station,
        start_line=start_line,
        cost=cost,
        walk=walk,
        visit_order=order,
        wall_time_s=round(time.perf_counter() - started, 3),
        status=_STATUS_NAMES.get(int(routing.status()), str(routing.status())),
        label=label or (f"GLS lambda={lam}" if lam is not None else "GLS"),
        trace=trace,
    )


def solve_warm_started(
    model: RouteModel, start_station: str, start_line: str, settings: SolverSettings, lam: float = 0.3
) -> GTSPResult:
    """Two-layer model first (fast, robust), lifted into direction-aware states, then GLS again."""
    started = time.perf_counter()
    half = replace(settings, time_limit_s=max(0.1, settings.time_limit_s / 2))
    flat_model = get_model(model.network.as_of, 0.0, model_data_path(model))
    flat = solve_gtsp(flat_model, start_station, start_line, half, lam=1.0)
    keep = set(target_stations(model, start_station))
    order_stations = station_order(flat_model, start_station, flat.walk, keep)
    _, seed = best_representatives(model, start_station, start_line, order_stations)
    res = solve_gtsp(
        model,
        start_station,
        start_line,
        half,
        lam=lam,
        initial_order=seed,
        label=f"warm start (two-layer) + GLS lambda={lam}",
    )
    res.wall_time_s = round(time.perf_counter() - started, 3)
    return res


def model_data_path(model: RouteModel) -> str | None:
    return model.data_path


# ----------------------------------------------------------------------------- portfolio
@dataclass(frozen=True)
class SolveJob:
    as_of: date
    reversal_penalty_min: float
    data_path: str | None
    start_station: str
    start_line: str
    lam: float
    warm: bool
    settings: SolverSettings
    seed_order: tuple[str, ...] = ()  # station order to warm-start from (cross-line seeding)
    label: str = ""


def run_job(job: SolveJob) -> GTSPResult:
    """Top-level (picklable) worker entry point; models are cached per process."""
    model = get_model(job.as_of, job.reversal_penalty_min, job.data_path)
    return _execute(model, job)


def _execute(model: RouteModel, job: SolveJob) -> GTSPResult:
    if job.seed_order:
        _, seed = best_representatives(model, job.start_station, job.start_line, list(job.seed_order))
        return solve_gtsp(
            model,
            job.start_station,
            job.start_line,
            job.settings,
            lam=job.lam,
            initial_order=seed,
            label=job.label or f"seeded GLS lambda={job.lam}",
        )
    if job.warm:
        return solve_warm_started(model, job.start_station, job.start_line, job.settings, job.lam)
    return solve_gtsp(model, job.start_station, job.start_line, job.settings, lam=job.lam)


_pool: ProcessPoolExecutor | None = None
_pool_size = 0
_pool_lock = threading.Lock()


def _get_pool(size: int) -> Executor:
    global _pool, _pool_size
    with _pool_lock:
        if _pool is None or _pool_size < size:
            if _pool is not None:
                _pool.shutdown(wait=False, cancel_futures=True)
            _pool = ProcessPoolExecutor(max_workers=size, mp_context=mp.get_context("spawn"))
            _pool_size = size
        return _pool


def shutdown_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown(wait=True, cancel_futures=True)
            _pool = None


def portfolio_jobs(
    model: RouteModel, start_station: str, lines: list[str], settings: SolverSettings
) -> list[SolveJob]:
    jobs: list[SolveJob] = []
    for line in lines:
        for lam in settings.lambdas:
            jobs.append(
                SolveJob(
                    model.network.as_of,
                    model.space.reversal_penalty_min,
                    model_data_path(model),
                    start_station,
                    line,
                    lam,
                    False,
                    settings,
                )
            )
        if settings.warm_start and model.space.mode == "directional":
            jobs.append(
                SolveJob(
                    model.network.as_of,
                    model.space.reversal_penalty_min,
                    model_data_path(model),
                    start_station,
                    line,
                    0.3,
                    True,
                    settings,
                )
            )
    return jobs


def solve_each_start_line(
    model: RouteModel,
    start_station: str,
    settings: SolverSettings | None = None,
) -> tuple[list[GTSPResult], list[GTSPResult]]:
    """Run the portfolio for every line at ``start_station``.

    Returns ``(best_per_line sorted fastest first, all_runs)``.
    """
    settings = (settings or SolverSettings()).validate()
    lines = list(model.network.stations[start_station].lines)
    runs = run_jobs(model, portfolio_jobs(model, start_station, lines, settings), settings)

    def best_by_line(results: list[GTSPResult]) -> dict[str, GTSPResult]:
        best: dict[str, GTSPResult] = {}
        for r in results:
            if r.start_line not in best or r.cost < best[r.start_line].cost:
                best[r.start_line] = r
        return best

    best = best_by_line(runs)
    if len(lines) > 1:
        # Cross-line seeding: re-solve every other start line from the winning route's station
        # order, so one unlucky line's portfolio cannot distort the start-line comparison.
        winner = min(best.values(), key=lambda r: r.cost)
        order = tuple(station_order(model, start_station, winner.walk, set(target_stations(model, start_station))))
        seeded = [
            SolveJob(
                model.network.as_of,
                model.space.reversal_penalty_min,
                model_data_path(model),
                start_station,
                line,
                0.3,
                False,
                settings,
                order,
                "seeded from best start line + GLS lambda=0.3",
            )
            for line in lines
            if line != winner.start_line
        ]
        runs += run_jobs(model, seeded, settings)
        best = best_by_line(runs)
    ranked = sorted(best.values(), key=lambda r: (r.cost, lines.index(r.start_line)))
    return ranked, runs


def run_jobs(model: RouteModel, jobs: list[SolveJob], settings: SolverSettings) -> list[GTSPResult]:
    """Run solve jobs in the process pool (or in-process when ``settings.workers == 1``)."""
    workers = settings.workers if settings.workers is not None else min(len(jobs), os.cpu_count() or 1)
    if workers <= 1 or len(jobs) == 1:
        return [_execute(model, j) for j in jobs]
    try:
        return list(_get_pool(workers).map(run_job, jobs))
    except BrokenProcessPool:  # a worker died (e.g. OOM-killed): rebuild the pool once
        shutdown_pool()
        return list(_get_pool(workers).map(run_job, jobs))
