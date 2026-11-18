"""Flask web app for the all-stations MRT route planner.

Run with ``python -m mrt_route serve`` (development) or, for production::

    gunicorn -w 1 --threads 8 --timeout 0 -b 0.0.0.0:8000 "webapp.app:create_app()"

API
---
GET  /api/network?upcoming=0|1   stations, lines, segments, transfers (for the map and picker)
POST /api/solve                  {start, upcoming, reversal_penalty, time_limit, live, exact_seconds}
                                 -> {job_id, state, result?}; precomputed / cached answers return at once
GET  /api/jobs/<job_id>          job state, progress messages and the result when done
GET  /api/health                 liveness
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from flask import Flask, jsonify, render_template, request

from mrt_route.model import get_model
from mrt_route.network import StationNotFoundError, load_raw
from mrt_route.planner import RouteBook, plan_route
from mrt_route.solver import SolverSettings

UPCOMING_DATE = "2026-12-26"
MAX_TIME_LIMIT = 60.0
MAX_EXACT_SECONDS = 900.0


class JobManager:
    """Runs solves in background threads; keeps results in a small in-memory cache."""

    def __init__(self, workers: int = 2, ttl_s: float = 3600, max_cached: int = 256):
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="solve")
        self.jobs: dict[str, dict[str, Any]] = {}
        self.cache: dict[tuple, str] = {}
        self.lock = threading.Lock()
        self.ttl_s = ttl_s
        self.max_cached = max_cached

    def _gc(self) -> None:
        now = time.time()
        stale = [
            jid
            for jid, j in self.jobs.items()
            if j["state"] in ("done", "error") and now - j["finished"] > self.ttl_s
        ]
        for jid in stale:
            self.jobs.pop(jid, None)
        self.cache = {k: v for k, v in self.cache.items() if v in self.jobs}
        while len(self.cache) > self.max_cached:
            self.cache.pop(next(iter(self.cache)))

    def submit(self, key: tuple, fn) -> dict[str, Any]:
        with self.lock:
            self._gc()
            jid = self.cache.get(key)
            if jid and self.jobs.get(jid, {}).get("state") != "error":
                return self.jobs[jid]
            jid = uuid.uuid4().hex[:12]
            job = {
                "job_id": jid,
                "state": "queued",
                "progress": [],
                "result": None,
                "error": None,
                "submitted": time.time(),
                "finished": None,
            }
            self.jobs[jid] = job
            self.cache[key] = jid

        def run() -> None:
            job["state"] = "running"
            job["started"] = time.time()
            try:
                job["result"] = fn(lambda line: job["progress"].append(line))
                job["state"] = "done"
            except StationNotFoundError as exc:
                job["state"], job["error"] = "error", str(exc)
            except Exception as exc:  # pragma: no cover - surfaced to the client
                traceback.print_exc()
                job["state"], job["error"] = "error", f"{type(exc).__name__}: {exc}"
            finally:
                job["finished"] = time.time()

        self.pool.submit(run)
        return job

    def get(self, jid: str) -> dict[str, Any] | None:
        return self.jobs.get(jid)


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    out = {k: job[k] for k in ("job_id", "state", "progress", "error")}
    end = job.get("finished") or time.time()
    out["elapsed_s"] = round(end - job.get("started", job["submitted"]), 1)
    if job["state"] == "done":
        out["result"] = job["result"]
    return out


def network_payload(upcoming: bool) -> dict[str, Any]:
    model = get_model(UPCOMING_DATE if upcoming else None)
    net = model.network
    raw = load_raw()
    book = RouteBook()
    return {
        "as_of": net.as_of.isoformat(),
        "upcoming_date": UPCOMING_DATE,
        "summary": model.describe(),
        "meta": raw.get("meta", {}),
        "lines": [
            {"id": ln.id, "name": ln.name, "color": ln.color, "parent": ln.parent} for ln in net.lines.values()
        ],
        "stations": [
            {
                "name": s.name,
                "key": s.key,
                "lat": s.lat,
                "lon": s.lon,
                "codes": [list(c) for c in s.codes],
                "code_label": s.code_label,
                "interchange": s.is_interchange,
                "approximate_location": s.approximate_location,
            }
            for s in sorted(net.stations.values(), key=lambda s: s.name)
        ],
        "segments": [{"line": g.line, "a": g.a, "b": g.b, "minutes": g.minutes} for g in net.segments],
        "transfers": [
            {"station": t.station, "lines": [t.line_a, t.line_b], "minutes": t.minutes, "note": t.note}
            for t in net.transfers
        ],
        "routes": [
            {"line": r.line, "name": r.name, "loop": r.loop, "stations": list(r.stations)} for r in net.routes
        ],
        "not_yet_open": net.not_yet_open,
        "defaults": {
            "reversal_penalty_min": net.reversal_penalty_min,
            "time_limit_s": SolverSettings().time_limit_s,
            "lambdas": list(SolverSettings().lambdas),
        },
        "route_book": book.configurations(),
    }


def create_app(job_workers: int = 2) -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.json.sort_keys = False
    jobs = JobManager(workers=job_workers)
    app.config["JOBS"] = jobs

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True})

    @app.get("/api/network")
    def network():
        upcoming = request.args.get("upcoming", "0") in ("1", "true", "yes")
        return jsonify(network_payload(upcoming))

    @app.post("/api/solve")
    def solve():
        body = request.get_json(silent=True) or {}
        start = str(body.get("start", "")).strip()
        if not start:
            return jsonify({"error": "Please choose a starting station."}), 400
        upcoming = bool(body.get("upcoming", False))
        as_of = UPCOMING_DATE if upcoming else None
        try:
            model = get_model(as_of)
            station = model.network.find_station(start).name
            penalty = body.get("reversal_penalty")
            penalty = None if penalty in (None, "") else float(penalty)
            if penalty is not None and not (0 <= penalty <= 30):
                raise ValueError("reversal_penalty must be between 0 and 30 minutes")
            time_limit = float(body.get("time_limit") or SolverSettings().time_limit_s)
            if not (1 <= time_limit <= MAX_TIME_LIMIT):
                raise ValueError(f"time_limit must be between 1 and {MAX_TIME_LIMIT:g} seconds")
            exact_s = float(body.get("exact_seconds") or 0)
            if not (0 <= exact_s <= MAX_EXACT_SECONDS):
                raise ValueError(f"exact_seconds must be between 0 and {MAX_EXACT_SECONDS:g}")
            live = bool(body.get("live", False))
        except StationNotFoundError as exc:
            return jsonify({"error": str(exc), "suggestions": exc.suggestions}), 404
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

        settings = SolverSettings(time_limit_s=time_limit)
        key = (station, model.network.as_of.isoformat(), penalty, time_limit, live, exact_s)

        def work(progress):
            progress(f"Solving from {station} ...")
            plan = plan_route(
                station,
                as_of=as_of,
                reversal_penalty_min=penalty,
                settings=settings,
                exact_time_s=exact_s,
                use_route_book=not live,
                progress=progress,
            )
            result = plan.to_dict()
            result["optimality_note"] = _optimality_note(plan)
            return result

        job = jobs.submit(key, work)
        # Precomputed answers finish in well under a second: wait briefly to answer inline.
        deadline = time.time() + 1.5
        while job["state"] in ("queued", "running") and time.time() < deadline:
            time.sleep(0.05)
        return jsonify(public_job(job))

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Unknown or expired job"}), 404
        return jsonify(public_job(job))

    return app


def _optimality_note(plan) -> str:
    o = plan.optimality
    if not o:
        return "Best route found by the Guided Local Search portfolio (not proven optimal)."
    if o.get("proven_optimal"):
        return f"Proven optimal by CP-SAT (lower bound {o['lower_bound_minutes']:g} min)."
    if o.get("gap_percent") is not None:
        return f"Within {o['gap_percent']}% of optimal (CP-SAT lower bound {o['lower_bound_minutes']:g} min)."
    return "Optimality check did not finish."


if __name__ == "__main__":  # pragma: no cover
    create_app().run(port=8000, threaded=True)
