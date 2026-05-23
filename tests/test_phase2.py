"""Regression tests for Phase 2 review fix (REVIEW.md finding #12: oneway).

Run from project root:
    python -m pytest tests/test_phase2.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import build_motorcycle  # noqa: E402


def test_parse_oneway_variants():
    p = build_motorcycle.parse_oneway
    assert p({'oneway': 'yes'}) == 1
    assert p({'oneway': 'true'}) == 1
    assert p({'oneway': '1'}) == 1
    assert p({'oneway': '-1'}) == -1
    assert p({'oneway': 'reverse'}) == -1
    assert p({'oneway': 'no'}) == 0
    assert p({'oneway': '0'}) == 0
    assert p({}) == 0
    # Implicit oneway via roundabout
    assert p({'junction': 'roundabout'}) == 1
    # Explicit oneway override beats roundabout default
    assert p({'junction': 'roundabout', 'oneway': 'no'}) == 0


def test_build_graph_respects_oneway():
    """Bidirectional way must yield both directions; oneway must yield only the legal one."""
    ways = [
        # Way 1: bidirectional A(0) -> B(1)
        {
            'geometry': [{'lat': 37.5, 'lon': 127.0}, {'lat': 37.51, 'lon': 127.0}],
            'tags': {'highway': 'primary'},
        },
        # Way 2: forward-oneway B(1) -> C(2)
        {
            'geometry': [{'lat': 37.51, 'lon': 127.0}, {'lat': 37.52, 'lon': 127.0}],
            'tags': {'highway': 'primary', 'oneway': 'yes'},
        },
        # Way 3: reverse-oneway D(3) -> E(4) in OSM order means traversal E -> D
        {
            'geometry': [{'lat': 37.6, 'lon': 127.0}, {'lat': 37.61, 'lon': 127.0}],
            'tags': {'highway': 'primary', 'oneway': '-1'},
        },
    ]
    nodes, edges = build_motorcycle.build_graph(ways)
    assert len(nodes) == 5
    edge_set = {(u, v) for u, v, _, _ in edges}
    # Way 1 (bidirectional): both directions present
    assert (0, 1) in edge_set
    assert (1, 0) in edge_set
    # Way 2 (oneway=yes): only forward
    assert (1, 2) in edge_set
    assert (2, 1) not in edge_set
    # Way 3 (oneway=-1): only reverse direction
    assert (4, 3) in edge_set
    assert (3, 4) not in edge_set


def test_oneway_blocks_wrong_way_routing():
    """End-to-end: dijkstra must not route through a oneway against its direction."""
    import numpy as np
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import dijkstra
    # Graph: A=0, B=1, C=2. A<->B bidirectional, B->C oneway. No direct A-C.
    # Going A -> C is legal (A->B->C). Going C -> A must be impossible.
    ways = [
        {
            'geometry': [{'lat': 37.5, 'lon': 127.0}, {'lat': 37.51, 'lon': 127.0}],
            'tags': {'highway': 'primary'},
        },
        {
            'geometry': [{'lat': 37.51, 'lon': 127.0}, {'lat': 37.52, 'lon': 127.0}],
            'tags': {'highway': 'primary', 'oneway': 'yes'},
        },
    ]
    _, edges = build_motorcycle.build_graph(ways)
    rows, cols, data = [], [], []
    for u, v, w, _ in edges:
        rows.append(u); cols.append(v); data.append(w)
    g = csr_matrix((data, (rows, cols)), shape=(3, 3))
    dist, _ = dijkstra(g, directed=True, indices=[0, 2], return_predecessors=True)
    # A -> C reachable
    assert np.isfinite(dist[0][2])
    # C -> A NOT reachable (would require wrong-way on the oneway segment)
    assert np.isinf(dist[1][0])
