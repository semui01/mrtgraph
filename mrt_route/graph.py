"""Graph models of the MRT network.

1. :func:`build_platform_graph` - the *two-layer platform graph* (networkx ``DiGraph``).
   One node per (station, line) platform, e.g. ``CityHall_NSL`` and ``CityHall_EWL``.
   ``ride`` edges join adjacent stations on the same line (in-train minutes) and
   ``transfer`` edges join the platforms of one interchange (walk + wait minutes).

2. :class:`SearchSpace` - the graph the optimiser actually searches.

   * ``mode="platform"`` uses the platform graph as-is (pure two-layer model), plus a
     start-only ``("board", P)`` node per platform whose only edges are rides on P's line
     (so "start on line L" means the first train you take is an L train).
   * ``mode="directional"`` (used when the reversal penalty is > 0) expands every platform
     into direction-aware states so that turning back costs time, as it does in reality:

       - ``("board", P)``    standing on platform P, free to take a train either way
       - ``("arr", P, Q)``   on a train that has just arrived at P coming from Q

     Staying on the train (``arr -> arr``) is free; going back the way you came requires
     ``arr(P, Q) -> board(P)`` (the reversal penalty) and a new ride; changing lines is
     ``arr(P, Q) -> board(P')`` with the interchange penalty.

Edge costs are integers in milliseconds, plus a 1 ms tie-breaker on every transfer and
reversal so that, among equally fast alternatives, the one with fewer changes wins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Hashable

import networkx as nx

from .network import Network

MS_PER_MINUTE = 60_000
CHANGE_TIEBREAK = 1  # +1 ms per transfer / reversal


def to_cost(minutes: float) -> int:
    return int(round(minutes * MS_PER_MINUTE))


def to_minutes(cost: int | float) -> float:
    """Convert a cost back to minutes (tie-breakers vanish after rounding)."""
    return round(float(cost) / MS_PER_MINUTE, 2)


def platform_id(network: Network, station: str, line: str) -> str:
    return f"{network.stations[station].key}_{line}"


def build_platform_graph(network: Network) -> nx.DiGraph:
    """Two-layer platform graph: platforms per line, ride edges and in-station transfer edges."""
    g = nx.DiGraph(as_of=network.as_of.isoformat())
    for st in network.stations.values():
        for line, code in st.codes:
            g.add_node(
                platform_id(network, st.name, line),
                kind="platform",
                station=st.name,
                line=line,
                code=code,
            )
    for seg in network.segments:
        u = platform_id(network, seg.a, seg.line)
        v = platform_id(network, seg.b, seg.line)
        attrs = dict(kind="ride", line=seg.line, minutes=seg.minutes, weight=to_cost(seg.minutes))
        g.add_edge(u, v, **attrs)
        g.add_edge(v, u, **attrs)
    for tr in network.transfers:
        u = platform_id(network, tr.station, tr.line_a)
        v = platform_id(network, tr.station, tr.line_b)
        attrs = dict(
            kind="transfer",
            minutes=tr.minutes,
            weight=to_cost(tr.minutes) + CHANGE_TIEBREAK,
            note=tr.note,
        )
        g.add_edge(u, v, **attrs)
        g.add_edge(v, u, **attrs)
    return g


def build_platform_search_graph(platforms: nx.DiGraph) -> nx.DiGraph:
    """Platform graph plus start-only board nodes (first move must be a ride on that line)."""
    g = platforms.copy()
    for p, d in platforms.nodes(data=True):
        g.add_node(("board", p), kind="board", platform=p, station=d["station"], line=d["line"])
    for u, v, d in platforms.edges(data=True):
        if d["kind"] == "ride":
            g.add_edge(("board", u), v, kind="ride", weight=d["weight"], minutes=d["minutes"])
    return g


def build_directional_graph(platforms: nx.DiGraph, reversal_penalty_min: float) -> nx.DiGraph:
    """Expand the platform graph into direction-aware states (see module docstring)."""
    if reversal_penalty_min <= 0:
        raise ValueError("The directional graph needs a positive reversal penalty")
    reverse_cost = to_cost(reversal_penalty_min) + CHANGE_TIEBREAK
    s = nx.DiGraph()
    ride_succ: dict[str, list[tuple[str, dict]]] = {p: [] for p in platforms}
    xfer_succ: dict[str, list[tuple[str, dict]]] = {p: [] for p in platforms}
    for u, v, d in platforms.edges(data=True):
        (ride_succ if d["kind"] == "ride" else xfer_succ)[u].append((v, d))

    for p, d in platforms.nodes(data=True):
        s.add_node(("board", p), kind="board", platform=p, station=d["station"], line=d["line"])
    for u, v, d in platforms.edges(data=True):
        if d["kind"] == "ride":
            vd = platforms.nodes[v]
            s.add_node(("arr", v, u), kind="arr", platform=v, came_from=u, station=vd["station"], line=vd["line"])

    for u, v, d in platforms.edges(data=True):
        if d["kind"] != "ride":
            continue
        # board at u, ride to v
        s.add_edge(("board", u), ("arr", v, u), kind="ride", weight=d["weight"], minutes=d["minutes"])
        here = ("arr", v, u)
        # stay on the train through v (any onward neighbour except going back to u)
        for w, dw in ride_succ[v]:
            if w != u:
                s.add_edge(here, ("arr", w, v), kind="ride", weight=dw["weight"], minutes=dw["minutes"])
        # alight and wait for a train in the opposite direction
        s.add_edge(here, ("board", v), kind="reverse", weight=reverse_cost, minutes=reversal_penalty_min)
        # change lines inside the interchange
        for v2, dt in xfer_succ[v]:
            s.add_edge(here, ("board", v2), kind="transfer", weight=dt["weight"], minutes=dt["minutes"])
    return s


@dataclass
class SearchSpace:
    """Indexed view of the graph being optimised over.

    ``targets[station]``   nodes whose visit counts as visiting the physical station.
    ``starts[(station, line)]`` nodes a journey may begin from when starting on ``line``.
    """

    network: Network
    platforms: nx.DiGraph
    graph: nx.DiGraph
    mode: str
    reversal_penalty_min: float
    nodes: list[Hashable] = field(default_factory=list)
    index: dict[Hashable, int] = field(default_factory=dict)
    station_of: list[str] = field(default_factory=list)
    platform_of: list[str] = field(default_factory=list)
    targets: dict[str, list[int]] = field(default_factory=dict)
    starts: dict[tuple[str, str], list[int]] = field(default_factory=dict)

    @classmethod
    def build(cls, network: Network, reversal_penalty_min: float | None = None) -> SearchSpace:
        penalty = network.reversal_penalty_min if reversal_penalty_min is None else float(reversal_penalty_min)
        if penalty < 0:
            raise ValueError("reversal penalty must be >= 0")
        platforms = build_platform_graph(network)
        if penalty > 0:
            graph = build_directional_graph(platforms, penalty)
            mode = "directional"
        else:
            graph = build_platform_search_graph(platforms)
            mode = "platform"
        space = cls(network, platforms, graph, mode, penalty)
        space._index()
        return space

    def _index(self) -> None:
        # deterministic ordering: by station, line, then node repr
        def sort_key(n: Hashable) -> tuple:
            d = self.graph.nodes[n]
            return (d["station"], d["line"], repr(n))

        self.nodes = sorted(self.graph.nodes, key=sort_key)
        self.index = {n: i for i, n in enumerate(self.nodes)}
        for i, n in enumerate(self.nodes):
            d = self.graph.nodes[n]
            self.station_of.append(d["station"])
            self.platform_of.append(n if d["kind"] == "platform" else d["platform"])
            is_target = d["kind"] in ("platform", "arr")
            is_start = d["kind"] == "board"
            if is_target:
                self.targets.setdefault(d["station"], []).append(i)
            if is_start:
                self.starts.setdefault((d["station"], d["line"]), []).append(i)

    # ------------------------------------------------------------------ helpers
    def start_nodes(self, station: str, line: str | None = None) -> list[int]:
        st = self.network.stations[station]
        lines = [line] if line else list(st.lines)
        out: list[int] = []
        for ln in lines:
            if (station, ln) not in self.starts:
                raise KeyError(f"{station} has no {ln} platform")
            out.extend(self.starts[(station, ln)])
        return out

    def edge_kind(self, i: int, j: int) -> str:
        return self.graph.edges[self.nodes[i], self.nodes[j]]["kind"]

    def edge_minutes(self, i: int, j: int) -> float:
        return float(self.graph.edges[self.nodes[i], self.nodes[j]]["minutes"])

    def describe(self) -> dict:
        return {
            "mode": self.mode,
            "reversal_penalty_min": self.reversal_penalty_min,
            "platform_nodes": self.platforms.number_of_nodes(),
            "platform_edges": self.platforms.number_of_edges(),
            "search_nodes": self.graph.number_of_nodes(),
            "search_edges": self.graph.number_of_edges(),
            "target_nodes": sum(len(v) for v in self.targets.values()),
            "stations": len(self.targets),
        }
