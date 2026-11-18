import networkx as nx
import pytest
from conftest import NOV

from mrt_route.graph import MS_PER_MINUTE, SearchSpace, build_directional_graph, build_platform_graph, to_minutes
from mrt_route.model import get_model
from mrt_route.network import load_network


@pytest.fixture(scope="module")
def net():
    return load_network(as_of=NOV)


@pytest.fixture(scope="module")
def platforms(net):
    return build_platform_graph(net)


def test_platform_nodes_are_named_station_line(platforms):
    assert "CityHall_NSL" in platforms and "CityHall_EWL" in platforms
    assert "DhobyGhaut_NEL" in platforms and "DhobyGhaut_CCL" in platforms
    assert platforms.number_of_nodes() == 175
    assert platforms.nodes["CityHall_NSL"]["code"] == "NS25"


def test_ride_edges_join_adjacent_stations_on_one_line(platforms):
    for u, v, d in platforms.edges(data=True):
        nu, nv = platforms.nodes[u], platforms.nodes[v]
        if d["kind"] == "ride":
            assert nu["line"] == nv["line"] == d["line"] and nu["station"] != nv["station"]
            assert platforms.has_edge(v, u) and platforms.edges[v, u]["minutes"] == d["minutes"]
        else:  # transfers stay inside one interchange: no out-of-station walking links
            assert d["kind"] == "transfer" and nu["station"] == nv["station"] and nu["line"] != nv["line"]


def test_transfer_penalties(platforms):
    assert platforms.edges["JurongEast_NSL", "JurongEast_EWL"]["minutes"] == 5  # cross-platform
    assert platforms.edges["TanahMerah_EWL", "TanahMerah_CGL"]["minutes"] == 5
    assert 7 <= platforms.edges["DhobyGhaut_NSL", "DhobyGhaut_CCL"]["minutes"] <= 10
    assert 7 <= platforms.edges["OutramPark_EWL", "OutramPark_TEL"]["minutes"] <= 10


def test_directional_graph_structure(platforms):
    g = build_directional_graph(platforms, 3.0)
    rides = sum(1 for *_, d in platforms.edges(data=True) if d["kind"] == "ride")
    assert sum(1 for _, d in g.nodes(data=True) if d["kind"] == "arr") == rides
    assert sum(1 for _, d in g.nodes(data=True) if d["kind"] == "board") == platforms.number_of_nodes()
    # staying on the train never turns back
    arr = ("arr", "Simei_EWL", "Tampines_EWL")
    assert not g.has_edge(arr, ("arr", "Tampines_EWL", "Simei_EWL"))
    assert g.has_edge(arr, ("arr", "TanahMerah_EWL", "Simei_EWL"))
    rev = g.edges[arr, ("board", "Simei_EWL")]
    assert rev["kind"] == "reverse" and rev["minutes"] == 3.0
    with pytest.raises(ValueError):
        build_directional_graph(platforms, 0)


def test_search_space_modes(net):
    flat = SearchSpace.build(net, 0)
    assert flat.mode == "platform" and sum(len(v) for v in flat.targets.values()) == 175
    dirs = SearchSpace.build(net, 3)
    assert dirs.mode == "directional"
    assert len(dirs.targets) == len(flat.targets) == 146
    assert len(dirs.start_nodes("Dhoby Ghaut")) == 3
    assert len(dirs.start_nodes("Dhoby Ghaut", "NEL")) == 1


def test_apsp_paths_match_distances():
    model = get_model(NOV, 3.0)
    sp, ap = model.space, model.apsp
    g = sp.graph
    start = sp.start_nodes("Bedok", "EWL")[0]
    for station in ("Changi Airport", "Tuas Link", "Punggol Coast", "Woodlands North"):
        j = min(sp.targets[station], key=lambda t: ap.dist[start, t])
        path = ap.path(start, j)
        cost = sum(g.edges[sp.nodes[a], sp.nodes[b]]["weight"] for a, b in zip(path, path[1:]))
        assert cost == ap.dist[start, j]
        assert sp.station_of[path[-1]] == station


def test_known_shortest_time_bedok_to_changi_airport():
    model = get_model(NOV, 3.0)
    sp, ap = model.space, model.apsp
    s = sp.start_nodes("Bedok")[0]
    best = min(ap.dist[s, t] for t in sp.targets["Changi Airport"])
    # 3 min Bedok->Tanah Merah, 5 min change, 3.5 + 4.5 min on the shuttle
    assert to_minutes(best) == pytest.approx(16.0, abs=0.01)


def test_apsp_agrees_with_networkx():
    model = get_model(NOV, 0.0)
    sp, ap = model.space, model.apsp
    src = sp.index["Pioneer_EWL"]
    ref = nx.single_source_dijkstra_path_length(sp.graph, sp.nodes[src], weight="weight")
    for node, d in ref.items():
        assert ap.dist[src, sp.index[node]] == d
    assert MS_PER_MINUTE == 60_000
