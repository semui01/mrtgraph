"""Load, filter and validate the Singapore MRT network dataset.

The dataset (``data/sg_mrt_network.json``) is the single source of truth.  It lists, per line,
one or more *routes* (ordered stops with the in-train time to the next stop), the transfer
penalties inside official interchange stations, and approximate station coordinates.

Stops can carry an ``"opens": "YYYY-MM-DD"`` date.  When a network is loaded for an earlier
date those stops are skipped and the run times on either side are merged, so the dataset can
describe future extensions without changing today's answer.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from itertools import combinations
from pathlib import Path
from typing import Any
from collections.abc import Iterable

DEFAULT_DATA_PATH = Path(__file__).resolve().parent / "data" / "sg_mrt_network.json"


class NetworkDataError(ValueError):
    """The dataset is internally inconsistent (bad station, missing transfer, ...)."""


class StationNotFoundError(KeyError):
    """A user-supplied station name could not be resolved."""

    def __init__(self, query: str, suggestions: list[str]):
        self.query = query
        self.suggestions = suggestions
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        super().__init__(f"Unknown MRT station '{query}'.{hint}")

    def __str__(self) -> str:  # KeyError would otherwise repr() the message
        return self.args[0]


@dataclass(frozen=True)
class Line:
    id: str
    name: str
    color: str
    parent: str | None = None  # e.g. the Changi Airport branch (CGL) belongs to the EWL

    @property
    def family(self) -> str:
        """Public line this layer belongs to (CGL -> EWL)."""
        return self.parent or self.id


@dataclass(frozen=True)
class Station:
    name: str
    key: str  # CamelCase identifier used in graph node names, e.g. "CityHall"
    lat: float
    lon: float
    codes: tuple[tuple[str, str], ...]  # ((line_id, station_code), ...)
    approximate_location: bool = False

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(line for line, _ in self.codes)

    @property
    def is_interchange(self) -> bool:
        return len(self.codes) > 1

    def code_for(self, line_id: str) -> str:
        for line, code in self.codes:
            if line == line_id:
                return code
        raise KeyError(f"{self.name} is not served by {line_id}")

    @property
    def code_label(self) -> str:
        """All codes, deduplicated, e.g. 'NS24 / NE6 / CC1'."""
        seen: list[str] = []
        for _, code in self.codes:
            if code not in seen:
                seen.append(code)
        return " / ".join(seen)


@dataclass(frozen=True)
class Route:
    """An ordered service pattern of a line (a path, or a loop when ``loop`` is True)."""

    line: str
    name: str
    loop: bool
    stations: tuple[str, ...]
    minutes: tuple[float, ...]  # minutes[i] = time from stations[i] to the next stop

    def segments(self) -> Iterable[tuple[str, str, float]]:
        n = len(self.stations)
        last = n if self.loop else n - 1
        for i in range(last):
            yield self.stations[i], self.stations[(i + 1) % n], self.minutes[i]


@dataclass(frozen=True)
class Segment:
    line: str
    a: str
    b: str
    minutes: float
    route: str


@dataclass(frozen=True)
class Transfer:
    station: str
    line_a: str
    line_b: str
    minutes: float
    note: str = ""


@dataclass
class Network:
    lines: dict[str, Line]
    stations: dict[str, Station]
    routes: list[Route]
    segments: list[Segment]
    transfers: list[Transfer]
    reversal_penalty_min: float
    as_of: date
    meta: dict[str, Any]
    not_yet_open: list[dict[str, str]] = field(default_factory=list)
    _lookup: dict[str, str] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ queries
    @property
    def interchanges(self) -> list[Station]:
        return [s for s in self.stations.values() if s.is_interchange]

    def platform_count(self) -> int:
        return sum(len(s.codes) for s in self.stations.values())

    def transfer_minutes(self, station: str, line_a: str, line_b: str) -> float:
        for t in self.transfers:
            if t.station == station and {t.line_a, t.line_b} == {line_a, line_b}:
                return t.minutes
        raise KeyError((station, line_a, line_b))

    def find_station(self, query: str) -> Station:
        """Resolve a station by name, CamelCase key or code (case/punctuation-insensitive)."""
        if not self._lookup:
            self._build_lookup()
        norm = _normalise(query)
        if norm in self._lookup:
            return self.stations[self._lookup[norm]]
        names = list(self.stations)
        suggestions = difflib.get_close_matches(query.strip().title(), names, n=3, cutoff=0.55)
        if not suggestions:
            suggestions = [n for n in names if norm and norm in _normalise(n)][:3]
        raise StationNotFoundError(query, suggestions)

    def _build_lookup(self) -> None:
        for st in self.stations.values():
            self._lookup[_normalise(st.name)] = st.name
            self._lookup[_normalise(st.key)] = st.name
            for _, code in st.codes:
                if re.search(r"\d", code):  # bare "CG" (Tanah Merah branch platform) is ambiguous
                    self._lookup.setdefault(_normalise(code), st.name)

    def summary(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "stations": len(self.stations),
            "platform_nodes": self.platform_count(),
            "line_layers": len(self.lines),
            "interchanges": len(self.interchanges),
            "track_segments": len(self.segments),
            "transfer_links": len(self.transfers),
            "not_yet_open": self.not_yet_open,
        }


# ---------------------------------------------------------------------- helpers
def station_key(name: str) -> str:
    """'City Hall' -> 'CityHall', 'one-north' -> 'OneNorth'."""
    parts = re.split(r"[^0-9A-Za-z]+", name)
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def _normalise(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.lower())


def _parse_date(value: date | str | None) -> date:
    if value is None:
        return date.today()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _is_open(stop: dict[str, Any], as_of: date) -> bool:
    opens = stop.get("opens")
    return opens is None or date.fromisoformat(opens) <= as_of


def _filter_route(line_id: str, route: dict[str, Any], as_of: date) -> tuple[Route, list[dict[str, str]]]:
    """Drop stops that are not open yet, merging the run times around them."""
    stops = route["stops"]
    loop = bool(route.get("loop", False))
    name = route.get("name", line_id)
    if len(stops) < 2:
        raise NetworkDataError(f"{line_id} route '{name}' needs at least two stops")

    for i, stop in enumerate(stops):
        needs_time = loop or i < len(stops) - 1
        if needs_time:
            t = stop.get("next_min")
            if not isinstance(t, (int, float)) or t <= 0:
                raise NetworkDataError(
                    f"{line_id} route '{name}': stop '{stop.get('station')}' needs a positive next_min"
                )

    skipped = [
        {"line": line_id, "station": s["station"], "code": s.get("code", ""), "opens": s["opens"]}
        for s in stops
        if not _is_open(s, as_of)
    ]
    open_idx = [i for i, s in enumerate(stops) if _is_open(s, as_of)]
    if len(open_idx) < 2:
        raise NetworkDataError(f"{line_id} route '{name}' has fewer than two open stops on {as_of}")

    n = len(stops)
    stations: list[str] = []
    minutes: list[float] = []
    pairs = list(zip(open_idx, open_idx[1:] + ([open_idx[0]] if loop else [])))
    for i, j in pairs:
        # sum run times from stop i up to (but excluding) stop j, wrapping for loops
        total, k = 0.0, i
        while k != j:
            total += float(stops[k]["next_min"])
            k = (k + 1) % n
        stations.append(stops[i]["station"])
        minutes.append(round(total, 3))
    if not loop:
        stations.append(stops[open_idx[-1]]["station"])
    return Route(line_id, name, loop, tuple(stations), tuple(minutes)), skipped


# ------------------------------------------------------------------------ load
def load_raw(path: str | Path | None = None) -> dict[str, Any]:
    with open(path or DEFAULT_DATA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def load_network(
    path: str | Path | None = None,
    as_of: date | str | None = None,
    raw: dict[str, Any] | None = None,
) -> Network:
    """Build a validated :class:`Network` containing only stations open on ``as_of``.

    ``as_of`` defaults to today's date, so stations scheduled to open later (e.g. the
    TEL Stage 5 / DTL3e stations opening on 26 Dec 2026) appear automatically once open.
    """
    data = raw if raw is not None else load_raw(path)
    when = _parse_date(as_of)

    lines: dict[str, Line] = {}
    for spec in data["lines"]:
        if spec["id"] in lines:
            raise NetworkDataError(f"Duplicate line id {spec['id']}")
        lines[spec["id"]] = Line(spec["id"], spec["name"], spec.get("color", "#888888"), spec.get("parent"))
    for line in lines.values():
        if line.parent and line.parent not in lines:
            raise NetworkDataError(f"Line {line.id} has unknown parent {line.parent}")

    coords: dict[str, dict[str, Any]] = data["stations"]
    routes: list[Route] = []
    not_yet_open: list[dict[str, str]] = []
    codes: dict[str, dict[str, str]] = {}
    all_line_station_pairs: set[tuple[str, str]] = set()

    for spec in data["lines"]:
        for route_spec in spec["routes"]:
            seen_in_route: set[str] = set()
            for stop in route_spec["stops"]:
                st = stop["station"]
                if st not in coords:
                    raise NetworkDataError(f"Station '{st}' ({spec['id']}) has no entry in 'stations'")
                if st in seen_in_route:
                    raise NetworkDataError(f"Station '{st}' appears twice in {spec['id']} '{route_spec['name']}'")
                seen_in_route.add(st)
                all_line_station_pairs.add((spec["id"], st))
            route, skipped = _filter_route(spec["id"], route_spec, when)
            routes.append(route)
            not_yet_open.extend(skipped)
            open_codes = {s["station"]: s.get("code", "") for s in route_spec["stops"] if _is_open(s, when)}
            for st in route.stations:
                existing = codes.setdefault(st, {})
                if spec["id"] in existing and existing[spec["id"]] != open_codes[st]:
                    raise NetworkDataError(f"Conflicting {spec['id']} codes for '{st}'")
                existing[spec["id"]] = open_codes[st]

    # A stop that is open on one route and listed as unopened on another stays open.
    open_pairs = {(line, st) for st, by_line in codes.items() for line in by_line}
    not_yet_open = [s for s in not_yet_open if (s["line"], s["station"]) not in open_pairs]

    segments: list[Segment] = []
    seen_pairs: set[tuple[str, frozenset[str]]] = set()
    for route in routes:
        for a, b, minutes in route.segments():
            key = (route.line, frozenset((a, b)))
            if key in seen_pairs:
                raise NetworkDataError(f"Segment {a} - {b} is defined twice on {route.line}")
            seen_pairs.add(key)
            segments.append(Segment(route.line, a, b, minutes, route.name))

    stations: dict[str, Station] = {}
    keys: dict[str, str] = {}
    for name, by_line in codes.items():
        key = station_key(name)
        if key in keys:
            raise NetworkDataError(f"Stations '{name}' and '{keys[key]}' share the key {key}")
        keys[key] = name
        line_order = [lid for lid in lines if lid in by_line]
        c = coords[name]
        stations[name] = Station(
            name=name,
            key=key,
            lat=float(c["lat"]),
            lon=float(c["lon"]),
            codes=tuple((lid, by_line[lid]) for lid in line_order),
            approximate_location=bool(c.get("approximate", False)),
        )

    transfers: list[Transfer] = []
    for st_name, entries in data.get("interchanges", {}).items():
        for entry in entries:
            line_a, line_b = entry["lines"]
            for lid in (line_a, line_b):
                if lid not in lines:
                    raise NetworkDataError(f"Interchange '{st_name}' references unknown line {lid}")
                if (lid, st_name) not in all_line_station_pairs:
                    raise NetworkDataError(f"Interchange '{st_name}': {lid} does not serve this station")
            minutes = entry.get("min")
            if not isinstance(minutes, (int, float)) or minutes <= 0:
                raise NetworkDataError(f"Interchange '{st_name}' {line_a}/{line_b} needs a positive 'min'")
            st = stations.get(st_name)
            if st is None or line_a not in st.lines or line_b not in st.lines:
                continue  # one of the platforms is not open yet on this date
            transfers.append(Transfer(st_name, line_a, line_b, float(minutes), entry.get("note", "")))

    # Every pair of lines at an open interchange needs an explicit transfer time.
    have = {(t.station, frozenset((t.line_a, t.line_b))) for t in transfers}
    missing = [
        f"{st.name}: {a}/{b}"
        for st in stations.values()
        for a, b in combinations(st.lines, 2)
        if (st.name, frozenset((a, b))) not in have
    ]
    if missing:
        raise NetworkDataError("Missing interchange transfer times for " + "; ".join(missing))

    params = data.get("parameters", {})
    reversal = float(params.get("reversal_penalty_min", 0.0))
    if reversal < 0:
        raise NetworkDataError("reversal_penalty_min must be >= 0")

    network = Network(
        lines=lines,
        stations=stations,
        routes=routes,
        segments=segments,
        transfers=transfers,
        reversal_penalty_min=reversal,
        as_of=when,
        meta=data.get("meta", {}),
        not_yet_open=not_yet_open,
    )
    _check_connected(network)
    return network


def _check_connected(network: Network) -> None:
    """All stations must be mutually reachable using rides and in-station transfers only."""
    adj: dict[str, set[str]] = {name: set() for name in network.stations}
    for seg in network.segments:
        adj[seg.a].add(seg.b)
        adj[seg.b].add(seg.a)
    start = next(iter(adj))
    seen, stack = {start}, [start]
    while stack:
        for nxt in adj[stack.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    if len(seen) != len(adj):
        orphans = sorted(set(adj) - seen)
        raise NetworkDataError(f"Network is disconnected; unreachable: {', '.join(orphans[:10])}")
