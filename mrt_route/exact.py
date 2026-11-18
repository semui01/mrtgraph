"""Exact optimisation / optimality certificate with OR-Tools CP-SAT.

The GTSP + Guided Local Search pipeline (``solver.py``) is fast, but local search over the
metric closure of a sparse rail network can stall slightly above the optimum.  This module
solves the *same* problem exactly, as a generalised directed rural-postman model on the
search graph, and is used to polish a GLS route and to prove a lower bound.

Model (``x_a`` = times arc ``a`` is used, ``y_v`` = node visited, ``e_v`` = walk ends at ``v``)::

    min  sum_a cost_a * x_a
    s.t. out(v) - in(v) = [v = source] - e_v           (one Euler path from the source)
         sum_v e_v = 1,   e_v <= y_v <= in(v),   x_a <= U * y_head(a)
         sum_{v in station S} y_v >= 1                  (every station visited)
         x(delta_in(C)) >= 1  or  >= y_v                (connectivity cuts, added lazily)

Connectivity is enforced with a cutting-plane loop: solve, find the components of used
arcs that are not attached to the source, add cuts for them, repeat.  Cuts for every run of
consecutive non-interchange stations along a line are added up front.  When a round returns
a connected solution that CP-SAT proved optimal for the relaxation, it is optimal for the
full problem.  Typical run time is minutes, so this is an offline / on-demand tool.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from .model import RouteModel

MAX_TRAVERSALS = 4


@dataclass
class ExactResult:
    status: str  # OPTIMAL (proven) | FEASIBLE (time limit hit) | NO_SOLUTION
    cost: int | None  # best known walk cost (model units)
    lower_bound: int  # proven lower bound (model units)
    walk: list[int] | None  # search-space node indices of the best known walk
    rounds: int
    cuts: int
    wall_time_s: float
    improved: bool = False  # True when the exact search beat the supplied incumbent
    log: list[str] = field(default_factory=list)

    @property
    def proven_optimal(self) -> bool:
        return self.status == "OPTIMAL"

    @property
    def gap(self) -> float | None:
        if self.cost is None or self.cost == 0:
            return None
        return max(0.0, (self.cost - self.lower_bound) / self.cost)


def _station_runs(model: RouteModel, start_station: str) -> list[set[str]]:
    """Every contiguous run of non-junction stations along each route (junction = interchange/start)."""
    net = model.network
    junction = {s.name for s in net.stations.values() if s.is_interchange} | {start_station}
    runs: dict[frozenset[str], set[str]] = {}
    for route in net.routes:
        seq = list(route.stations)
        if route.loop:
            if not any(s in junction for s in seq):
                continue
            k = next(i for i, s in enumerate(seq) if s in junction)
            seq = seq[k:] + seq[:k] + [seq[k]]
        chains: list[list[str]] = [[]]
        for s in seq:
            if s in junction:
                chains.append([])
            else:
                chains[-1].append(s)
        for ch in chains:
            for i in range(len(ch)):
                for j in range(i, len(ch)):
                    run = set(ch[i : j + 1])
                    runs[frozenset(run)] = run
    return list(runs.values())


def detached_components(num_nodes: int, used_arcs: list[tuple[int, int]], source: int) -> list[set[int]]:
    """Weakly connected components of the used arcs that do not contain ``source``."""
    parent = list(range(num_nodes))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    touched = {source}
    for u, v in used_arcs:
        touched.update((u, v))
        ru, rv = find(u), find(v)
        if ru != rv:
            parent[ru] = rv
    comps: dict[int, set[int]] = defaultdict(set)
    for v in touched:
        comps[find(v)].add(v)
    root = find(source)
    return [c for r, c in comps.items() if r != root]


def euler_walk(arc_counts: dict[tuple[int, int], int], source: int) -> list[int]:
    """Hierholzer's algorithm: a directed Euler path through the arc multiset, from ``source``."""
    adj: dict[int, list[int]] = defaultdict(list)
    for (u, v), cnt in sorted(arc_counts.items()):
        adj[u].extend([v] * cnt)
    for u in adj:
        adj[u].reverse()
    stack, path = [source], []
    while stack:
        u = stack[-1]
        if adj[u]:
            stack.append(adj[u].pop())
        else:
            path.append(stack.pop())
    path.reverse()
    return path


def solve_exact(
    model: RouteModel,
    start_station: str,
    start_line: str | None = None,
    time_limit_s: float = 300.0,
    hint_walk: list[int] | None = None,
    workers: int | None = None,
    progress=None,
) -> ExactResult:
    """Prove optimality of (or improve on) ``hint_walk`` for a start station / line.

    ``start_line=None`` lets the model choose the best line at the start station.
    ``progress`` is an optional callable receiving a log line after every round.
    """
    started = time.perf_counter()
    deadline = started + time_limit_s
    space = model.space
    g = space.graph
    idx = space.index
    n = len(space.nodes)
    source = n
    station_of = space.station_of + ["<source>"]

    arcs: list[tuple[int, int, int]] = [(idx[u], idx[v], int(d["weight"])) for u, v, d in g.edges(data=True)]
    for s in space.start_nodes(start_station, start_line):
        arcs.append((source, s, 0))
    in_arcs: list[list[int]] = [[] for _ in range(n + 1)]
    out_arcs: list[list[int]] = [[] for _ in range(n + 1)]
    for k, (u, v, _) in enumerate(arcs):
        out_arcs[u].append(k)
        in_arcs[v].append(k)
    arc_index = {(u, v): k for k, (u, v, _) in enumerate(arcs)}

    nodes_of_station: dict[str, set[int]] = defaultdict(set)
    for i in range(n):
        nodes_of_station[station_of[i]].add(i)
    required = {st: set(members) for st, members in space.targets.items() if st != start_station}

    def entering(node_set: set[int]) -> tuple[int, ...]:
        return tuple(sorted(k for v in node_set for k in in_arcs[v] if arcs[k][0] not in node_set))

    # cuts: (arc ids entering C, None => rhs 1 | node v => rhs y_v)
    cuts: dict[tuple[tuple[int, ...], int | None], None] = {}
    for run in _station_runs(model, start_station):
        cset = set().union(*(nodes_of_station[s] for s in run))
        cuts[(entering(cset), None)] = None

    incumbent_cost: int | None = None
    incumbent_walk: list[int] | None = None
    hint: dict[int, int] = {}
    if hint_walk:
        counts: dict[int, int] = defaultdict(int)
        prev = source
        for node in hint_walk:
            counts[arc_index[(prev, node)]] += 1
            prev = node
        hint = dict(counts)
        incumbent_cost = sum(arcs[k][2] * c for k, c in hint.items())
        incumbent_walk = list(hint_walk)
    initial_cost = incumbent_cost

    lower_bound, rounds, proven = 0, 0, False
    log: list[str] = []
    workers = workers or max(1, min(8, os.cpu_count() or 1))

    def note(line: str) -> None:
        log.append(line)
        if progress:
            progress(line)

    while time.perf_counter() < deadline - 0.2:
        rounds += 1
        m = cp_model.CpModel()
        x = [m.NewIntVar(0, 1 if u == source else MAX_TRAVERSALS, f"x{k}") for k, (u, _, _) in enumerate(arcs)]
        y = [m.NewBoolVar(f"y{v}") for v in range(n + 1)]
        e = [m.NewBoolVar(f"e{v}") for v in range(n + 1)]
        m.Add(y[source] == 1)
        m.Add(e[source] == 0)
        m.AddExactlyOne(e)
        for v in range(n + 1):
            out_sum = sum(x[k] for k in out_arcs[v])
            in_sum = sum(x[k] for k in in_arcs[v])
            m.Add(out_sum - in_sum == (1 if v == source else 0) - e[v])
            if v != source:
                m.Add(in_sum >= y[v])
                m.AddImplication(e[v], y[v])
        for k, (_, v, _) in enumerate(arcs):
            m.Add(x[k] <= MAX_TRAVERSALS * y[v])
        for members in required.values():
            m.AddBoolOr([y[v] for v in members])
        for arc_ids, rhs_node in cuts:
            m.Add(sum(x[k] for k in arc_ids) >= (1 if rhs_node is None else y[rhs_node]))
        objective = sum(c * x[k] for k, (_, _, c) in enumerate(arcs))
        if incumbent_cost is not None:
            m.Add(objective <= incumbent_cost)
        m.Minimize(objective)
        for k, val in hint.items():
            m.AddHint(x[k], val)

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = max(0.5, deadline - time.perf_counter())
        solver.parameters.num_workers = workers
        status = solver.Solve(m)
        if status == cp_model.INFEASIBLE:
            # Nothing at or below the incumbent satisfies the relaxation: incumbent is optimal.
            if incumbent_cost is not None:
                proven, lower_bound = True, incumbent_cost
            note(f"round {rounds}: relaxation infeasible below incumbent -> incumbent optimal")
            break
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            note(f"round {rounds}: {solver.StatusName(status)} (time limit)")
            break
        if status == cp_model.OPTIMAL:
            lower_bound = max(lower_bound, int(round(solver.ObjectiveValue())))
        else:
            lower_bound = max(lower_bound, int(solver.BestObjectiveBound()))
        values = [solver.Value(v) for v in x]
        used = {k: c for k, c in enumerate(values) if c > 0}
        hint = used
        obj = int(round(solver.ObjectiveValue()))

        bad = detached_components(n + 1, [(arcs[k][0], arcs[k][1]) for k in used], source)
        note(
            f"round {rounds}: relaxation {obj / 60000:.1f} min, bound {lower_bound / 60000:.1f} min, "
            f"{solver.StatusName(status).lower()}, {len(bad)} detached component(s)"
        )
        if not bad:
            walk = euler_walk({(arcs[k][0], arcs[k][1]): c for k, c in used.items()}, source)[1:]
            if incumbent_cost is None or obj < incumbent_cost:
                incumbent_cost, incumbent_walk = obj, walk
            if status == cp_model.OPTIMAL:
                proven, lower_bound = True, obj
                break
            continue
        for comp in bad:
            # Prefer the station closure of the component (a stronger, reusable cut) when the
            # current solution violates it; otherwise use the component itself, which no used
            # arc enters, so its cut is always violated.
            closure = set().union(*(nodes_of_station[station_of[v]] for v in comp if v != source))
            closure.discard(source)
            for cset in (closure, comp):
                arc_ids = entering(cset)
                if sum(values[k] for k in arc_ids) >= 1:
                    continue
                if any(members <= cset for members in required.values()):
                    cuts[(arc_ids, None)] = None
                else:
                    for v in sorted(cset & comp):
                        cuts[(arc_ids, v)] = None
                break

    if proven:
        status_name = "OPTIMAL"
    elif incumbent_walk is not None:
        status_name = "FEASIBLE"
    else:
        status_name = "NO_SOLUTION"
    if incumbent_cost is not None:
        lower_bound = min(lower_bound, incumbent_cost)
    return ExactResult(
        status=status_name,
        cost=incumbent_cost,
        lower_bound=lower_bound,
        walk=incumbent_walk,
        rounds=rounds,
        cuts=len(cuts),
        wall_time_s=round(time.perf_counter() - started, 2),
        improved=initial_cost is not None and incumbent_cost is not None and incumbent_cost < initial_cost,
        log=log,
    )
