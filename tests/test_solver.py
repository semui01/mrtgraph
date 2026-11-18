import pytest
from conftest import NOV

from mrt_route.exact import solve_exact
from mrt_route.graph import to_minutes
from mrt_route.model import get_model
from mrt_route.solver import (
    SolverSettings,
    best_representatives,
    build_cost_matrix,
    implied_stations,
    solve_each_start_line,
    solve_gtsp,
)

FAST = SolverSettings(time_limit_s=1.0, lambdas=(1.0,), warm_start=False, workers=1)


def walk_cost(model, walk):
    g, nodes = model.space.graph, model.space.nodes
    return sum(g.edges[nodes[a], nodes[b]]["weight"] for a, b in zip(walk, walk[1:]))


def test_implied_stations_are_articulation_points():
    model = get_model(NOV, 3.0)
    imp = implied_stations(model, "Bedok")
    assert {"Jurong East", "Tuas West Road", "Serangoon", "Marina Bay", "Botanic Gardens"} <= imp
    assert "Tuas Link" not in imp and "Bedok" not in imp
    assert "Tanah Merah" not in imp  # Expo is also reachable through the DTL


def test_cost_matrix_layout():
    model = get_model(NOV, 0.0)
    matrix, targets, clusters, _ = build_cost_matrix(model, "Bedok", "EWL")
    assert matrix.shape[0] == 2 + len(targets)
    assert (matrix[2:, 1] == 0).all()  # dummy end reachable for free (open path)
    assert matrix[0, 1] > matrix[0, 2:].max()  # an empty route is never chosen
    assert sum(len(c) for c in clusters) == len(targets)
    assert len(clusters) == 146 - 1 - len(implied_stations(model, "Bedok"))


@pytest.mark.parametrize("penalty", [0.0, 3.0])
def test_gtsp_route_covers_every_station(penalty):
    model = get_model(NOV, penalty)
    res = solve_gtsp(model, "Bedok", "EWL", FAST, lam=1.0)
    stations = {model.space.station_of[i] for i in res.walk}
    assert stations == set(model.network.stations)
    assert walk_cost(model, res.walk) == res.cost
    assert model.space.station_of[res.walk[0]] == "Bedok"
    assert 7 * 60 < res.minutes < 11 * 60
    assert res.trace and res.trace[-1][1] >= res.minutes - 0.01


def test_cluster_dp_is_optimal_for_order():
    model = get_model(NOV, 3.0)
    res = solve_gtsp(model, "Orchard", "TEL", FAST, lam=1.0)
    stations = [model.space.station_of[v] for v in res.visit_order]
    cost, order = best_representatives(model, "Orchard", "TEL", stations)
    assert cost == res.cost
    assert [model.space.station_of[v] for v in order] == stations


def test_each_start_line_is_evaluated():
    model = get_model(NOV, 0.0)
    best, runs = solve_each_start_line(model, "Jurong East", FAST)
    assert {r.start_line for r in best} == {"NSL", "EWL"}
    assert best[0].cost <= best[1].cost
    # one portfolio run per line, then the losing line is re-solved from the winner's order
    assert len(runs) == 3 and runs[-1].label.startswith("seeded")
    assert runs[-1].start_line == best[1].start_line


def test_hand_checkable_single_line(line_dataset):
    # A-B-C-D with 2/3/4 min, start at B, 3 min to turn back:
    # B->A (2) + turn (3) + A->D (9) = 14 beats B->D (7) + turn (3) + D->A (9) = 19
    model = get_model("2026-01-01", None, line_dataset)
    res = solve_gtsp(model, "B", "AAL", FAST, lam=1.0)
    assert res.minutes == pytest.approx(14.0, abs=0.01)
    flat = get_model("2026-01-01", 0.0, line_dataset)
    assert solve_gtsp(flat, "B", "AAL", FAST, lam=1.0).minutes == pytest.approx(11.0, abs=0.01)


def test_exact_solver_proves_hand_optimum(line_dataset):
    model = get_model("2026-01-01", None, line_dataset)
    ex = solve_exact(model, "B", None, time_limit_s=30)
    assert ex.proven_optimal and to_minutes(ex.cost) == pytest.approx(14.0, abs=0.01)
    assert {model.space.station_of[i] for i in ex.walk} == {"A", "B", "C", "D"}


@pytest.mark.parametrize("penalty", [0.0, 2.0])
def test_exact_matches_or_beats_gls_on_crossing_network(cross_dataset, penalty):
    path, _ = cross_dataset
    model = get_model("2026-01-01", penalty, path)
    gls = solve_gtsp(model, "A1", "AAL", FAST, lam=1.0)
    ex = solve_exact(model, "A1", "AAL", time_limit_s=60, hint_walk=gls.walk)
    assert ex.proven_optimal
    assert ex.cost <= gls.cost
    assert ex.lower_bound == ex.cost
    assert walk_cost(model, ex.walk) == ex.cost
    assert {model.space.station_of[i] for i in ex.walk} == set(model.network.stations)


def test_settings_validation():
    with pytest.raises(ValueError):
        SolverSettings(time_limit_s=0).validate()
    with pytest.raises(ValueError):
        SolverSettings(first_solution_strategy="NOPE").validate()
    with pytest.raises(ValueError):
        SolverSettings(lambdas=()).validate()
