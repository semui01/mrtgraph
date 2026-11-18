"""Offline all-pairs shortest paths over a :class:`~mrt_route.graph.SearchSpace`.

Dijkstra is run from every node (non-negative integer weights) and the result is stored as
two dense ``n x n`` matrices: travel cost and the predecessor of ``j`` on the shortest
``i -> j`` path.  Paths are rebuilt on demand by walking the predecessor row backwards,
which keeps memory at O(n^2) instead of storing every path explicitly.
"""

from __future__ import annotations

import time

import networkx as nx
import numpy as np

from .graph import SearchSpace

UNREACHABLE = np.iinfo(np.int64).max // 4


class AllPairsShortestPaths:
    def __init__(self, space: SearchSpace):
        self.space = space
        n = len(space.nodes)
        started = time.perf_counter()
        dist = np.full((n, n), UNREACHABLE, dtype=np.int64)
        pred = np.full((n, n), -1, dtype=np.int32)
        index = space.index
        graph = space.graph
        for i, source in enumerate(space.nodes):
            preds, dists = nx.dijkstra_predecessor_and_distance(graph, source, weight="weight")
            row_d = dist[i]
            row_p = pred[i]
            for node, d in dists.items():
                row_d[index[node]] = d
            for node, plist in preds.items():
                if plist:
                    row_p[index[node]] = index[plist[0]]
        self.dist = dist
        self.pred = pred
        self.build_seconds = time.perf_counter() - started

    @property
    def size(self) -> int:
        return self.dist.shape[0]

    def cost(self, i: int, j: int) -> int:
        return int(self.dist[i, j])

    def path(self, i: int, j: int) -> list[int]:
        """Node indices of the shortest path i -> j (inclusive)."""
        if i == j:
            return [i]
        if self.dist[i, j] >= UNREACHABLE:
            raise ValueError(f"No path from {self.space.nodes[i]} to {self.space.nodes[j]}")
        out = [j]
        row = self.pred[i]
        cur = j
        for _ in range(self.size):
            cur = int(row[cur])
            out.append(cur)
            if cur == i:
                out.reverse()
                return out
        raise RuntimeError("Predecessor chain did not terminate")  # pragma: no cover
