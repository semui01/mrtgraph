import time

import pytest

from webapp.app import create_app


@pytest.fixture(scope="module")
def client():
    app = create_app()
    app.testing = True
    return app.test_client()


def test_index_page(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"MRT All-Stations Planner" in res.data


def test_network_endpoint(client):
    data = client.get("/api/network").get_json()
    assert len(data["stations"]) >= 146
    assert {ln["id"] for ln in data["lines"]} >= {"NSL", "EWL", "NEL", "CCL", "DTL", "TEL"}
    assert data["defaults"]["reversal_penalty_min"] == 3.0
    upcoming = client.get("/api/network?upcoming=1").get_json()
    assert len(upcoming["stations"]) == 149


def test_solve_validation(client):
    assert client.post("/api/solve", json={}).status_code == 400
    res = client.post("/api/solve", json={"start": "Bedokk"})
    assert res.status_code == 404 and "Bedok" in res.get_json()["suggestions"]
    assert client.post("/api/solve", json={"start": "Bedok", "time_limit": 999}).status_code == 400
    assert client.post("/api/solve", json={"start": "Bedok", "reversal_penalty": -1}).status_code == 400


def test_solve_job_lifecycle(client):
    res = client.post("/api/solve", json={"start": "Changi Airport", "live": True, "time_limit": 1})
    assert res.status_code == 200
    job = res.get_json()
    deadline = time.time() + 120
    while job["state"] in ("queued", "running") and time.time() < deadline:
        time.sleep(0.3)
        job = client.get(f"/api/jobs/{job['job_id']}").get_json()
    assert job["state"] == "done", job
    result = job["result"]
    assert result["start_station"] == "Changi Airport"
    assert result["summary"]["all_covered"]
    assert result["steps"][0]["kind"] == "start"
    # identical request is served from the cache
    again = client.post("/api/solve", json={"start": "changi airport", "live": True, "time_limit": 1}).get_json()
    assert again["job_id"] == job["job_id"] and again["state"] == "done"


def test_unknown_job(client):
    assert client.get("/api/jobs/doesnotexist").status_code == 404
