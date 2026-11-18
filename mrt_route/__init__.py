"""All-stations Singapore MRT optimal route (generalised open-loop Steiner TSP).

Quick start::

    from mrt_route import plan_route
    plan = plan_route("Bedok")
    print(plan.summary["total_hm"], plan.summary["stations_covered"])

Pipeline: JSON dataset -> two-layer platform graph (networkx) -> optional direction-aware
expansion -> all-pairs shortest paths (Dijkstra) -> generalised TSP with OR-Tools
(Guided Local Search portfolio) -> step-by-step itinerary.  ``mrt_route.exact`` adds an
optional CP-SAT model that improves routes and proves optimality.
"""

from .graph import SearchSpace, build_directional_graph, build_platform_graph
from .itinerary import Step, build_itinerary, format_duration
from .model import RouteModel, get_model
from .network import Network, NetworkDataError, StationNotFoundError, load_network
from .planner import RoutePlan, plan_route
from .solver import SolverSettings, solve_each_start_line, solve_gtsp

__all__ = [
    "Network",
    "NetworkDataError",
    "RouteModel",
    "RoutePlan",
    "SearchSpace",
    "SolverSettings",
    "StationNotFoundError",
    "Step",
    "build_directional_graph",
    "build_itinerary",
    "build_platform_graph",
    "format_duration",
    "get_model",
    "load_network",
    "plan_route",
    "solve_each_start_line",
    "solve_gtsp",
]
