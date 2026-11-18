import copy
import json
from itertools import combinations

import pytest
from conftest import DEC, NOV

from mrt_route.network import NetworkDataError, StationNotFoundError, load_network, load_raw, station_key

LRT_STATIONS = {
    "Bakau",
    "Kangkar",
    "Ranggung",
    "Senja",
    "Bangkit",
    "Ten Mile Junction",
    "Soo Teck",
    "Punggol Point",
}


@pytest.fixture(scope="module")
def nov():
    return load_network(as_of=NOV)


@pytest.fixture(scope="module")
def dec():
    return load_network(as_of=DEC)


def test_network_size_november_2026(nov):
    assert len(nov.stations) == 146
    assert nov.platform_count() == 175
    assert len(nov.interchanges) == 26
    assert set(nov.lines) == {"NSL", "EWL", "CGL", "NEL", "CCL", "DTL", "TEL"}
    assert len(nov.transfers) == 32


def test_lines_have_expected_station_counts(nov):
    per_line = {ln: 0 for ln in nov.lines}
    for st in nov.stations.values():
        for ln in st.lines:
            per_line[ln] += 1
    assert per_line == {"NSL": 27, "EWL": 33, "CGL": 3, "NEL": 17, "CCL": 33, "DTL": 35, "TEL": 27}


def test_no_lrt_and_recent_openings_present(nov):
    assert not (LRT_STATIONS & set(nov.stations))
    for name in ("Keppel", "Cantonment", "Prince Edward Road", "Hume", "Punggol Coast"):
        assert name in nov.stations
    assert nov.stations["Bayfront"].code_for("CCL") == "CC34"
    assert nov.stations["Marina Bay"].code_for("CCL") == "CC33"
    # stations that are built but not open must not appear
    for name in ("Mount Pleasant", "Marina South", "Bukit Brown", "Founders' Memorial"):
        assert name not in nov.stations


def test_upcoming_stations_follow_opening_date(nov, dec):
    assert {s["station"] for s in nov.not_yet_open} == {"Xilin", "Sungei Bedok", "Bedok South"}
    assert len(dec.stations) == 149
    assert dec.stations["Sungei Bedok"].lines == ("DTL", "TEL")
    assert dec.transfer_minutes("Sungei Bedok", "DTL", "TEL") > 0
    assert not dec.not_yet_open


def test_every_interchange_pair_has_a_transfer(nov):
    for st in nov.interchanges:
        for a, b in combinations(st.lines, 2):
            assert nov.transfer_minutes(st.name, a, b) >= 5


def test_transfers_only_within_one_station(nov):
    for t in nov.transfers:
        st = nov.stations[t.station]
        assert t.line_a in st.lines and t.line_b in st.lines


def test_run_times_are_realistic(nov):
    times = [s.minutes for s in nov.segments]
    assert min(times) >= 1.5 and max(times) <= 5.0
    avg = sum(times) / len(times)
    assert 2.0 <= avg <= 3.0


def test_circle_line_is_a_loop_with_dhoby_ghaut_spur(nov):
    ccl = [r for r in nov.routes if r.line == "CCL"]
    assert any(r.loop for r in ccl)
    loop = next(r for r in ccl if r.loop)
    assert {"HarbourFront", "Keppel", "Marina Bay", "Bayfront", "Promenade"} <= set(loop.stations)
    spur = next(r for r in ccl if not r.loop)
    assert spur.stations[0] == "Dhoby Ghaut" and spur.stations[-1] == "Promenade"


@pytest.mark.parametrize(
    "query,expected",
    [
        ("Bedok", "Bedok"),
        ("bedok", "Bedok"),
        ("NS24", "Dhoby Ghaut"),
        ("cc1", "Dhoby Ghaut"),
        ("DhobyGhaut", "Dhoby Ghaut"),
        ("one north", "one-north"),
        ("CC34", "Bayfront"),
        ("  Orchard ", "Orchard"),
    ],
)
def test_find_station(nov, query, expected):
    assert nov.find_station(query).name == expected


def test_unknown_station_has_suggestions(nov):
    with pytest.raises(StationNotFoundError) as exc:
        nov.find_station("Bedokk")
    assert "Bedok" in exc.value.suggestions
    assert "Unknown MRT station" in str(exc.value)


def test_station_key():
    assert station_key("City Hall") == "CityHall"
    assert station_key("one-north") == "OneNorth"
    assert station_key("HarbourFront") == "HarbourFront"


def test_opening_dates_merge_run_times(cross_dataset):
    path, _ = cross_dataset
    early = load_network(path, as_of="2026-01-01")
    late = load_network(path, as_of="2030-01-01")
    assert "Z1" not in early.stations and "Z2" in late.stations
    assert len(late.stations) == len(early.stations) + 2


def test_infill_station_merges_neighbouring_times(cross_dataset, tmp_path):
    _, raw = cross_dataset
    raw = copy.deepcopy(raw)
    raw["lines"][0]["routes"][0]["stops"][1]["opens"] = "2030-01-01"  # A2 not open yet
    path = tmp_path / "infill.json"
    path.write_text(json.dumps(raw))
    net = load_network(path, as_of="2026-01-01")
    seg = next(s for s in net.segments if {s.a, s.b} == {"A1", "X"})
    assert seg.minutes == pytest.approx(4.0)


@pytest.mark.parametrize("breakage", ["missing_transfer", "zero_time", "no_coords", "bad_line", "duplicate"])
def test_invalid_data_is_rejected(cross_dataset, tmp_path, breakage):
    _, raw = cross_dataset
    raw = copy.deepcopy(raw)
    if breakage == "missing_transfer":
        raw["interchanges"] = {}
    elif breakage == "zero_time":
        raw["lines"][0]["routes"][0]["stops"][0]["next_min"] = 0
    elif breakage == "no_coords":
        del raw["stations"]["A1"]
    elif breakage == "bad_line":
        raw["interchanges"]["X"][0]["lines"] = ["AAL", "QQL"]
    elif breakage == "duplicate":
        raw["lines"][0]["routes"][0]["stops"][3]["station"] = "A1"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(NetworkDataError):
        load_network(path, as_of="2026-01-01")


def test_bundled_dataset_is_well_formed():
    raw = load_raw()
    assert raw["parameters"]["reversal_penalty_min"] >= 0
    for line in raw["lines"]:
        for route in line["routes"]:
            codes = [s["code"] for s in route["stops"]]
            assert len(codes) == len(set(codes))
