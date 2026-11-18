import json

import pytest
from conftest import NOV

from mrt_route.cli import main
from mrt_route.model import get_model
from mrt_route.network import StationNotFoundError
from mrt_route.planner import RouteBook, network_fingerprint, plan_route
from mrt_route.precompute import precompute_book
from mrt_route.solver import SolverSettings

FAST = SolverSettings(time_limit_s=1.0, lambdas=(1.0,), warm_start=False, workers=1)


@pytest.fixture(scope="module")
def plan():
    return plan_route("Tampines", as_of=NOV, settings=FAST, use_route_book=False)


def test_plan_summary(plan):
    s = plan.summary
    assert s["all_covered"] and s["stations_covered"] == s["stations_total"] == 146
    assert s["total_minutes"] == pytest.approx(plan.steps[-1].end_min)
    assert {p["line"] for p in plan.per_line} == {"EWL", "DTL"}
    assert sum(p["best"] for p in plan.per_line) == 1
    assert plan.start_line == plan.per_line[0]["line"]


def test_itinerary_is_consistent(plan):
    net = get_model(NOV).network
    steps = plan.steps
    assert steps[0].kind == "start" and steps[-1].kind == "finish"
    clock = 0.0
    visited = {plan.start_station}
    adjacent = {(s.line, frozenset((s.a, s.b))) for s in net.segments}
    for st in steps[1:-1]:
        assert st.start_min == pytest.approx(clock, abs=0.02)
        clock = st.end_min
        if st.kind == "ride":
            for a, b in zip(st.stations, st.stations[1:]):
                assert (st.line, frozenset((a, b))) in adjacent
            assert set(st.new_stations) <= set(st.stations) and not (set(st.new_stations) & visited)
            visited |= set(st.new_stations)
        elif st.kind == "transfer":
            assert net.stations[st.station].is_interchange
            assert net.transfer_minutes(st.station, st.line, st.to_line) == st.minutes
        else:
            assert st.kind == "reverse" and st.minutes == net.reversal_penalty_min
    assert visited == set(net.stations)
    total = sum(st.minutes for st in steps)
    assert total == pytest.approx(plan.summary["total_minutes"], abs=0.05)


def test_plan_to_dict_is_json_serialisable(plan):
    data = json.loads(json.dumps(plan.to_dict()))
    assert data["start_station"] == "Tampines" and len(data["steps"]) == len(plan.steps)


def test_unknown_station():
    with pytest.raises(StationNotFoundError):
        plan_route("Atlantis", as_of=NOV, settings=FAST, use_route_book=False)


def test_route_book_roundtrip_and_stale_detection(tmp_path):
    book_path = tmp_path / "book.json"
    model = get_model(NOV, 0.0)
    precompute_book(model, FAST, book_path, stations=["Bishan"], log=lambda *_: None)
    book = RouteBook(book_path)
    entry = book.lookup(model, "Bishan")
    assert entry and set(entry["per_line"]) == {"NSL", "CCL"}
    p = plan_route("Bishan", as_of=NOV, reversal_penalty_min=0.0, settings=FAST, route_book=book)
    assert p.solver["source"].startswith("route book")
    assert p.total_minutes == pytest.approx(min(e["minutes"] for e in entry["per_line"].values()))
    # tamper with the stored time: the entry must be ignored and the route solved live
    data = json.loads(book_path.read_text())
    fp = network_fingerprint(model)
    data["books"][fp]["routes"]["Bishan"]["per_line"]["NSL"]["minutes"] = 1.0
    book_path.write_text(json.dumps(data))
    p2 = plan_route("Bishan", as_of=NOV, reversal_penalty_min=0.0, settings=FAST, route_book=RouteBook(book_path))
    assert p2.solver["source"] == "live solve"


def test_cli_route_text_and_json(tmp_path, capsys):
    out = tmp_path / "plan.json"
    code = main(
        [
            "Pasir Ris",
            "--as-of",
            NOV,
            "--live",
            "--time-limit",
            "1",
            "--lambdas",
            "1",
            "--workers",
            "1",
            "--no-warm-start",
            "--json",
            str(out),
        ]
    )
    assert code == 0
    text = capsys.readouterr().out
    assert "Total travel time" in text and "Stations covered    : 146 / 146" in text and "START" in text
    assert json.loads(out.read_text())["summary"]["stations_covered"] == 146


def test_cli_unknown_station(capsys):
    assert main(["route", "Nowhere", "--as-of", NOV]) == 2
    assert "Unknown MRT station" in capsys.readouterr().err


def test_cli_lists_stations(capsys):
    assert main(["stations", "--as-of", NOV]) == 0
    out = capsys.readouterr().out
    assert "Dhoby Ghaut" in out and "146 stations" in out
