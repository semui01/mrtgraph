import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

NOV = "2026-11-18"  # snapshot date the dataset describes
DEC = "2026-12-26"  # TEL Stage 5 + DTL3e opening day


def _stations(names):
    # spread stations on a grid so coordinates are distinct
    return {n: {"lat": 1.30 + 0.01 * (i // 6), "lon": 103.80 + 0.01 * (i % 6)} for i, n in enumerate(names)}


@pytest.fixture
def line_dataset(tmp_path):
    """Single line A-B-C-D (2, 3, 4 min); hand-checkable optimum."""
    raw = {
        "meta": {},
        "parameters": {"reversal_penalty_min": 3.0},
        "lines": [
            {
                "id": "AAL",
                "name": "A Line",
                "color": "#cc0000",
                "routes": [
                    {
                        "name": "A-D",
                        "loop": False,
                        "stops": [
                            {"station": "A", "code": "A1", "next_min": 2},
                            {"station": "B", "code": "A2", "next_min": 3},
                            {"station": "C", "code": "A3", "next_min": 4},
                            {"station": "D", "code": "A4"},
                        ],
                    }
                ],
            }
        ],
        "interchanges": {},
        "stations": _stations("ABCD"),
    }
    path = tmp_path / "line.json"
    path.write_text(json.dumps(raw))
    return path


@pytest.fixture
def cross_dataset(tmp_path):
    """Two lines crossing at X, plus a branch line that opens later (opening-date logic)."""
    names = ["A1", "A2", "X", "A4", "A5", "B1", "B2", "B4", "Z1", "Z2"]
    raw = {
        "meta": {},
        "parameters": {"reversal_penalty_min": 2.0},
        "lines": [
            {
                "id": "AAL",
                "name": "A Line",
                "color": "#cc0000",
                "routes": [
                    {
                        "name": "A",
                        "loop": False,
                        "stops": [
                            {"station": "A1", "code": "A1", "next_min": 2},
                            {"station": "A2", "code": "A2", "next_min": 2},
                            {"station": "X", "code": "A3", "next_min": 3},
                            {"station": "A4", "code": "A4", "next_min": 2},
                            {"station": "A5", "code": "A5"},
                        ],
                    }
                ],
            },
            {
                "id": "BBL",
                "name": "B Line",
                "color": "#0000cc",
                "routes": [
                    {
                        "name": "B",
                        "loop": False,
                        "stops": [
                            {"station": "B1", "code": "B1", "next_min": 2},
                            {"station": "B2", "code": "B2", "next_min": 2},
                            {"station": "X", "code": "B3", "next_min": 2.5},
                            {"station": "B4", "code": "B4", "next_min": 1.5},
                            {"station": "Z1", "code": "B5", "opens": "2030-01-01", "next_min": 2},
                            {"station": "Z2", "code": "B6", "opens": "2030-01-01"},
                        ],
                    }
                ],
            },
        ],
        "interchanges": {"X": [{"lines": ["AAL", "BBL"], "min": 5, "note": "test"}]},
        "stations": _stations(names),
    }
    path = tmp_path / "cross.json"
    path.write_text(json.dumps(raw))
    return path, raw
