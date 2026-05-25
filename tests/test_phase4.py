"""Regression tests for Phase 4 review fixes (REVIEW.md findings #6, #7, #8, #10).

Run from project root:
    python -m pytest tests/test_phase4.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import build_motorcycle  # noqa: E402
import pipeline           # noqa: E402


@pytest.fixture(autouse=True)
def _reset_jobs():
    pipeline.JOBS.reset()
    yield
    pipeline.JOBS.reset()


# ---------- Finding #7: traceback must not leak into job error ----------

def test_error_message_omits_traceback(monkeypatch):
    def fail(*a, **kw):
        raise ValueError('boom')
    monkeypatch.setattr(pipeline, 'run_pipeline', fail)

    jid = pipeline.JOBS.create()
    pipeline.run_pipeline_async(jid, {})
    job = pipeline.JOBS.get(jid)

    assert job is not None
    assert job['status'] == 'error'
    assert job['error'] == 'ValueError: boom'
    assert 'Traceback' not in job['error']
    # No filesystem path should leak (Windows or POSIX).
    assert ':\\' not in job['error']
    assert '/site-packages/' not in job['error']


# ---------- Finding #8: JobManager retention cap with eviction ----------

def test_jobmanager_evicts_oldest_finished_when_capped():
    MAX = pipeline.MAX_JOBS
    for _ in range(MAX):
        jid = pipeline.JOBS.create()
        pipeline.JOBS.set_result(jid, {'out_path': 'x'})
    assert len(pipeline.JOBS._jobs) == MAX
    first_id = next(iter(pipeline.JOBS._jobs))

    new_jid = pipeline.JOBS.create()
    assert new_jid is not None
    assert len(pipeline.JOBS._jobs) == MAX
    assert first_id not in pipeline.JOBS._jobs
    assert new_jid in pipeline.JOBS._jobs


# ---------- Finding #6: unreachable pair diagnostics surfaced via routes ----------

def test_build_routes_attaches_unreachable_diagnostics(monkeypatch, tmp_path):
    """build_routes must propagate stage_matrix's unreachable diagnostics
    through routes['_diagnostics'] so the API result can surface a warning."""
    fake_matrix = {
        'distance': [[0, 100], [100, 0]],
        'n': 2,
        'snapped': [{'node_idx': 0, 'snap_dist_m': 0}, {'node_idx': 1, 'snap_dist_m': 0}],
        'spots_key': 'fake',
        'unreachable_count': 7,
        'unreachable_sample': [[1, 5], [3, 9]],
    }
    monkeypatch.setattr(build_motorcycle, 'ROUTES_JSON', tmp_path / 'routes.json')
    monkeypatch.setattr(build_motorcycle, 'stage_graph', lambda **kw: ([[0, 0], [1, 1]], []))
    monkeypatch.setattr(build_motorcycle, 'snap_spots', lambda spots, nc: fake_matrix['snapped'])
    monkeypatch.setattr(build_motorcycle, 'stage_matrix', lambda *a, **kw: fake_matrix)
    monkeypatch.setattr(build_motorcycle, 'stage_tsp',
                        lambda spots, m, p, progress_cb=None: {'loop': {'order': [0, 1, 0], 'distance_m': 200}})
    monkeypatch.setattr(build_motorcycle, 'stage_poly', lambda r, nc, progress_cb=None: r)
    monkeypatch.setattr(build_motorcycle, 'stage_duration', lambda r, e, nc, progress_cb=None: r)
    monkeypatch.setattr(build_motorcycle, 'get_spots',
                        lambda extras=(): [{'card': 1, 'lat': 0.0, 'lng': 0.0},
                                           {'card': 2, 'lat': 1.0, 'lng': 1.0}])

    params = build_motorcycle.BuildParams(enabled_modes=('loop',))
    routes = build_motorcycle.build_routes(params)
    assert '_diagnostics' in routes
    assert routes['_diagnostics']['unreachable_count'] == 7
    assert routes['_diagnostics']['unreachable_sample'] == [[1, 5], [3, 9]]


def test_stage_matrix_captures_inf_pairs(monkeypatch, tmp_path):
    """stage_matrix must record count + sample of unreachable pairs in its payload."""
    import numpy as np
    # Force fresh computation by pointing cache files into tmp.
    monkeypatch.setattr(build_motorcycle, 'MATRIX_JSON', tmp_path / 'matrix.json')
    monkeypatch.setattr(build_motorcycle, 'DIJKSTRA_NPZ', tmp_path / 'dijkstra.npz')
    # Patch dijkstra to return a distance array with one inf row -> isolated node.
    def fake_dijkstra(g, directed, indices, return_predecessors):
        n = g.shape[0]
        k = len(indices)
        dist = np.zeros((k, n))
        # Node 1 (index 1) is unreachable from node 0
        dist[0][1] = np.inf
        pred = np.full((k, n), -9999, dtype=np.int32)
        return dist, pred
    monkeypatch.setattr(build_motorcycle, 'dijkstra', fake_dijkstra)

    spots = [{'card': 1, 'lat': 0.0, 'lng': 0.0},
             {'card': 2, 'lat': 1.0, 'lng': 1.0}]
    snapped = [{'node_idx': 0, 'snap_dist_m': 0}, {'node_idx': 1, 'snap_dist_m': 0}]
    payload = build_motorcycle.stage_matrix(
        node_coords=[[0, 0], [1, 1]], edges=[], snapped=snapped, spots=spots,
    )
    assert payload['unreachable_count'] >= 1
    assert [1, 2] in payload['unreachable_sample']


# ---------- spots_key must be order-SENSITIVE ----------
# The matrix is position-indexed: matrix[i][j] is the distance from spots[i] to
# spots[j]. Reusing a cached matrix after spots order changes would silently
# miscorrelate distances with cards. Order-sensitivity forces a cache miss in
# that case (~90s rebuild) which is the correct, safe behavior.

def test_spots_cache_key_order_sensitive():
    spots_a = [
        {'card': 1, 'lat': 37.0, 'lng': 127.0},
        {'card': 2, 'lat': 38.0, 'lng': 128.0},
        {'card': 3, 'lat': 39.0, 'lng': 129.0},
    ]
    spots_b = list(reversed(spots_a))
    assert build_motorcycle._spots_cache_key(spots_a) != build_motorcycle._spots_cache_key(spots_b)


def test_spots_cache_key_changes_on_coord_edit():
    spots = [{'card': 1, 'lat': 37.0, 'lng': 127.0}]
    k1 = build_motorcycle._spots_cache_key(spots)
    spots[0]['lat'] = 37.01
    k2 = build_motorcycle._spots_cache_key(spots)
    assert k1 != k2


# ---------- stage_duration: per-leg invariants ----------
# Two regressions covered:
#   1. legs count == len(order) - 1 (loop's closing leg included).
#   2. Unreachable leg (pred=-9999) yields a graceful 0-filled entry with
#      mid_lat/mid_lng=None — frontend keys off mid_lat to render '도달불가'.

def _make_duration_fixture(tmp_path, monkeypatch, pred_array, src_nodes_array):
    """Wire a fake DIJKSTRA_NPZ at tmp_path with the given pred / snapped arrays."""
    import numpy as np
    dijkstra_npz = tmp_path / 'd.npz'
    np.savez(dijkstra_npz, pred=pred_array, snapped=src_nodes_array)
    monkeypatch.setattr(build_motorcycle, 'DIJKSTRA_NPZ', dijkstra_npz)


# Linear 3-node graph 0-1-2 (bidirectional). All edges 1000m @ 60 km/h
# → 60s / leg. No direct 0-2 edge: 0↔2 must traverse via node 1.
_LINEAR_NODE_COORDS = [[37.0, 127.0], [37.1, 127.1], [37.2, 127.2]]
_LINEAR_EDGES = [
    [0, 1, 1000.0, 60.0], [1, 0, 1000.0, 60.0],
    [1, 2, 1000.0, 60.0], [2, 1, 1000.0, 60.0],
]


def test_stage_duration_legs_length_matches_order(monkeypatch, tmp_path):
    import numpy as np
    pred = np.array([
        [-9999, 0,     1    ],
        [1,     -9999, 1    ],
        [1,     2,     -9999],
    ], dtype=np.int32)
    src_nodes = np.array([0, 1, 2], dtype=np.int64)
    _make_duration_fixture(tmp_path, monkeypatch, pred, src_nodes)

    routes = {
        'loop':     {'order': [0, 1, 2, 0], 'distance_m': 4000},
        'traverse': {'order': [0, 1, 2],    'distance_m': 2000},
    }
    out = build_motorcycle.stage_duration(routes, _LINEAR_EDGES, _LINEAR_NODE_COORDS)

    assert len(out['loop']['legs']) == 3       # closing leg counted
    assert len(out['traverse']['legs']) == 2   # open path
    for r in (out['loop'], out['traverse']):
        assert len(r['legs']) == len(r['order']) - 1
        for leg in r['legs']:
            assert leg['mid_lat'] is not None
            assert leg['distance_m'] > 0
            assert leg['duration_s'] > 0


def test_stage_duration_unreachable_leg_graceful(monkeypatch, tmp_path):
    import numpy as np
    # pred[2][0] = -9999 → node 0 is unreachable starting from node 2.
    pred = np.array([
        [-9999, 0,     1    ],
        [1,     -9999, 1    ],
        [-9999, 2,     -9999],
    ], dtype=np.int32)
    src_nodes = np.array([0, 1, 2], dtype=np.int64)
    _make_duration_fixture(tmp_path, monkeypatch, pred, src_nodes)

    routes = {'loop': {'order': [0, 1, 2, 0], 'distance_m': 4000}}
    out = build_motorcycle.stage_duration(routes, _LINEAR_EDGES, _LINEAR_NODE_COORDS)
    legs = out['loop']['legs']

    assert len(legs) == 3
    closing = legs[2]
    assert (closing['from_idx'], closing['to_idx']) == (2, 0)
    assert closing['mid_lat'] is None
    assert closing['mid_lng'] is None
    assert closing['distance_m'] == 0.0
    assert closing['duration_s'] == 0.0
    # Reachable legs unaffected.
    assert legs[0]['mid_lat'] is not None
    assert legs[1]['mid_lat'] is not None
    # Route duration_s only sums reachable legs (2 × 60s).
    assert out['loop']['duration_s'] == pytest.approx(120.0, rel=0.01)
