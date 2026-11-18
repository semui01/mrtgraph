"""A compiled, ready-to-solve model: network + search graph + all-pairs shortest paths."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .graph import SearchSpace
from .network import Network, load_network
from .paths import AllPairsShortestPaths


@dataclass
class RouteModel:
    network: Network
    space: SearchSpace
    apsp: AllPairsShortestPaths
    data_path: str | None = None  # dataset file (None = bundled dataset); used by worker processes

    @classmethod
    def build(
        cls,
        as_of: date | str | None = None,
        reversal_penalty_min: float | None = None,
        data_path: str | Path | None = None,
        network: Network | None = None,
    ) -> RouteModel:
        net = network or load_network(data_path, as_of=as_of)
        space = SearchSpace.build(net, reversal_penalty_min)
        path = str(Path(data_path).resolve()) if data_path else None
        return cls(net, space, AllPairsShortestPaths(space), path)

    def describe(self) -> dict:
        info = self.network.summary()
        info.update(self.space.describe())
        info["apsp_seconds"] = round(self.apsp.build_seconds, 3)
        return info


_cache: dict[tuple, RouteModel] = {}
_lock = threading.Lock()


def get_model(
    as_of: date | str | None = None,
    reversal_penalty_min: float | None = None,
    data_path: str | Path | None = None,
) -> RouteModel:
    """Memoised :meth:`RouteModel.build` (the APSP precomputation runs once per configuration)."""
    when = as_of if isinstance(as_of, date) else (date.fromisoformat(as_of) if as_of else date.today())
    path = str(Path(data_path).resolve()) if data_path else None
    key = (when, None if reversal_penalty_min is None else float(reversal_penalty_min), path)
    with _lock:
        model = _cache.get(key)
        if model is None:
            model = RouteModel.build(when, reversal_penalty_min, path)
            _cache[key] = model
            while len(_cache) > 16:  # tiny FIFO bound
                _cache.pop(next(iter(_cache)))
        return model
