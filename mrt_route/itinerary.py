"""Turn a solved node walk into a human-readable, step-by-step itinerary."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .graph import SearchSpace
from .network import Network


@dataclass
class Step:
    index: int
    kind: str  # "start" | "ride" | "transfer" | "reverse" | "finish"
    station: str  # where the step begins
    to_station: str  # where the step ends
    line: str | None = None  # line ridden / line being left
    to_line: str | None = None  # line joined (transfers and the start)
    direction: str | None = None  # e.g. "towards Marina South Pier"
    stations: list[str] = field(default_factory=list)  # ride: every station incl. both ends
    stops: int = 0
    minutes: float = 0.0
    start_min: float = 0.0
    end_min: float = 0.0
    new_stations: list[str] = field(default_factory=list)
    covered_after: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fmt_clock(minutes: float) -> str:
    total = int(round(minutes))
    return f"{total // 60}:{total % 60:02d}"


def format_duration(minutes: float) -> str:
    total = int(round(minutes))
    h, m = divmod(total, 60)
    if h and m:
        return f"{h} h {m} min"
    return f"{h} h" if h else f"{m} min"


class Headings:
    """Direction labels per (line, from_station, to_station)."""

    def __init__(self, network: Network):
        self.labels: dict[tuple[str, str, str], str] = {}
        self.route_of: dict[tuple[str, str, str], str] = {}
        for route in network.routes:
            st = route.stations
            if route.loop:
                ccw = _signed_area([network.stations[s] for s in st]) > 0
                fwd, back = ("anticlockwise", "clockwise") if ccw else ("clockwise", "anticlockwise")
            else:
                fwd, back = f"towards {st[-1]}", f"towards {st[0]}"
            for a, b, _ in route.segments():
                self.labels[(route.line, a, b)] = fwd
                self.labels[(route.line, b, a)] = back
                self.route_of[(route.line, a, b)] = route.name
                self.route_of[(route.line, b, a)] = route.name

    def label(self, line: str, a: str, b: str) -> str:
        return self.labels.get((line, a, b), "")


def _signed_area(stations) -> float:
    pts = [(s.lon, s.lat) for s in stations]
    return 0.5 * sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))


@dataclass
class Move:
    kind: str  # ride | transfer | reverse
    a: str  # from platform node id
    b: str  # to platform node id
    minutes: float


def walk_to_moves(space: SearchSpace, walk: list[int]) -> list[Move]:
    """Platform-level moves along a search-space walk (implicit reversals made explicit)."""
    moves: list[Move] = []
    for i, j in zip(walk, walk[1:]):
        kind = space.edge_kind(i, j)
        a, b = space.platform_of[i], space.platform_of[j]
        minutes = space.edge_minutes(i, j)
        if kind == "ride" and moves and moves[-1].kind == "ride" and moves[-1].a == b and moves[-1].b == a:
            # Turning back with no modelled penalty (platform mode): make it visible as a 0-min step.
            moves.append(Move("reverse", a, a, 0.0))
        moves.append(Move(kind, a, b, minutes))
    return moves


def build_itinerary(network: Network, space: SearchSpace, walk: list[int]) -> list[Step]:
    pg = space.platforms
    headings = Headings(network)
    moves = walk_to_moves(space, walk)

    first = space.platform_of[walk[0]]
    start_station = pg.nodes[first]["station"]
    start_line = pg.nodes[first]["line"]
    visited = {start_station}
    steps: list[Step] = [
        Step(
            index=0,
            kind="start",
            station=start_station,
            to_station=start_station,
            to_line=start_line,
            new_stations=[start_station],
            covered_after=1,
            note=f"Start at {start_station} and board the {network.lines[start_line].name}",
        )
    ]
    clock = 0.0
    transfer_notes = {(t.station, frozenset((t.line_a, t.line_b))): t.note for t in network.transfers}

    def station(p: str) -> str:
        return pg.nodes[p]["station"]

    def line(p: str) -> str:
        return pg.nodes[p]["line"]

    k = 0
    while k < len(moves):
        mv = moves[k]
        if mv.kind == "ride":
            ln = line(mv.a)
            seq = [station(mv.a), station(mv.b)]
            minutes = mv.minutes
            k += 1
            while k < len(moves) and moves[k].kind == "ride" and line(moves[k].a) == ln:
                seq.append(station(moves[k].b))
                minutes += moves[k].minutes
                k += 1
            new = [s for s in seq[1:] if s not in visited]
            visited.update(new)
            steps.append(
                Step(
                    index=len(steps),
                    kind="ride",
                    station=seq[0],
                    to_station=seq[-1],
                    line=ln,
                    direction=headings.label(ln, seq[0], seq[1]),
                    stations=seq,
                    stops=len(seq) - 1,
                    minutes=round(minutes, 2),
                    start_min=round(clock, 2),
                    end_min=round(clock + minutes, 2),
                    new_stations=new,
                    covered_after=len(visited),
                )
            )
            clock += minutes
        elif mv.kind == "transfer":
            st = station(mv.a)
            note = transfer_notes.get((st, frozenset((line(mv.a), line(mv.b)))), "")
            steps.append(
                Step(
                    index=len(steps),
                    kind="transfer",
                    station=st,
                    to_station=st,
                    line=line(mv.a),
                    to_line=line(mv.b),
                    minutes=mv.minutes,
                    start_min=round(clock, 2),
                    end_min=round(clock + mv.minutes, 2),
                    covered_after=len(visited),
                    note=note,
                )
            )
            clock += mv.minutes
            k += 1
        else:  # reverse
            st = station(mv.a)
            steps.append(
                Step(
                    index=len(steps),
                    kind="reverse",
                    station=st,
                    to_station=st,
                    line=line(mv.a),
                    to_line=line(mv.a),
                    minutes=mv.minutes,
                    start_min=round(clock, 2),
                    end_min=round(clock + mv.minutes, 2),
                    covered_after=len(visited),
                    note="Alight and take the next train in the opposite direction",
                )
            )
            clock += mv.minutes
            k += 1

    last = steps[-1].to_station
    steps.append(
        Step(
            index=len(steps),
            kind="finish",
            station=last,
            to_station=last,
            start_min=round(clock, 2),
            end_min=round(clock, 2),
            covered_after=len(visited),
            note=f"All {len(visited)} stations visited",
        )
    )
    return steps


def summarise(network: Network, steps: list[Step]) -> dict[str, Any]:
    rides = [s for s in steps if s.kind == "ride"]
    transfers = [s for s in steps if s.kind == "transfer"]
    reversals = [s for s in steps if s.kind == "reverse"]
    covered = steps[-1].covered_after
    total = steps[-1].end_min
    passes = sum(len(s.stations) - 1 for s in rides)
    return {
        "total_minutes": round(total, 2),
        "total_hm": format_duration(total),
        "stations_covered": covered,
        "stations_total": len(network.stations),
        "all_covered": covered == len(network.stations),
        "end_station": steps[-1].station,
        "ride_legs": len(rides),
        "stops_travelled": passes,
        "revisited_stops": passes - (covered - 1),
        "in_train_minutes": round(sum(s.minutes for s in rides), 2),
        "transfers": len(transfers),
        "transfer_minutes": round(sum(s.minutes for s in transfers), 2),
        "reversals": len(reversals),
        "reversal_minutes": round(sum(s.minutes for s in reversals), 2),
        "lines_used": sorted({s.line for s in rides if s.line}),
    }


def format_itinerary_text(network: Network, steps: list[Step], width: int = 100) -> str:
    """Plain-text itinerary table for the CLI."""
    lines: list[str] = []
    for s in steps:
        clock = _fmt_clock(s.end_min)
        if s.kind == "start":
            code = network.stations[s.station].code_label
            lines.append(f"{'0:00':>6}  START     {s.station} ({code}) - board {s.to_line}")
        elif s.kind == "ride":
            new = f"+{len(s.new_stations)} new" if s.new_stations else "no new"
            head = (
                f"{clock:>6}  RIDE {s.line:<4} {s.station} -> {s.to_station}  "
                f"[{s.stops} stop{'s' if s.stops != 1 else ''}, {s.minutes:g} min, {new}, {s.covered_after} covered]"
            )
            lines.append(head)
            if s.direction:
                lines.append(f"{'':>16}{s.direction}")
            if s.stops > 1:
                via = ", ".join(s.stations[1:-1])
                wrapped = _wrap(f"via {via}", width - 16)
                lines.extend(f"{'':>16}{w}" for w in wrapped)
        elif s.kind == "transfer":
            lines.append(
                f"{clock:>6}  TRANSFER  at {s.station}: {s.line} -> {s.to_line}  [{s.minutes:g} min]"
                + (f"  ({s.note})" if s.note else "")
            )
        elif s.kind == "reverse":
            lines.append(f"{clock:>6}  REVERSE   at {s.station} on {s.line}  [{s.minutes:g} min]")
        else:
            lines.append(f"{clock:>6}  FINISH    at {s.station} - {s.note}")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    out, cur = [], ""
    for word in text.split(" "):
        if cur and len(cur) + 1 + len(word) > width:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}" if cur else word
    if cur:
        out.append(cur)
    return out
