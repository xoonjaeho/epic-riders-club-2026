#!/usr/bin/env python
"""Motorcycle-legal road graph + matrix + TSP.

Exposes build_routes(params, progress_cb) for FastAPI integration.
CLI mode preserved for direct use.
"""
from __future__ import annotations
import argparse, json, math, sys, time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence
import requests
import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from shapely.geometry import Point
from shapely.strtree import STRtree
import polyline as polyline_lib

sys.stdout.reconfigure(encoding='utf-8')

ROOT = Path(__file__).parent
SPOTS_JSON = ROOT / 'spots.json'
OWNED_JSON = ROOT / 'owned.json'
CODEX_JSON = ROOT / 'codex_personal.json'
CACHE_DIR = ROOT / 'overpass_cache'
GRAPH_JSON = ROOT / 'mc_graph.json'
MATRIX_JSON = ROOT / 'matrix_motorcycle.json'
ROUTES_JSON = ROOT / 'routes_motorcycle.json'
DIJKSTRA_NPZ = ROOT / 'mc_dijkstra.npz'

OVERPASS = 'https://overpass-api.de/api/interpreter'
HEADERS = {'User-Agent': 'epic-riders/1.0 (motorcycle graph build)'}

BBOX = (36.9, 126.9, 38.7, 129.4)
NLAT_CHUNKS, NLNG_CHUNKS = 4, 4
SNAP_WARN_M = 200
SEOUL = (37.5665, 126.9780)

METAS_DEFAULT = ('GUIDED_LOCAL_SEARCH', 'SIMULATED_ANNEALING', 'TABU_SEARCH')


@dataclass
class BuildParams:
    """Parameters for build_routes()."""
    owned_cards: list[int] = field(default_factory=list)
    extra_spots: list[dict] = field(default_factory=list)  # EPIC: {card, name, lat, lng, addr?}
    enabled_modes: tuple = ('loop', 'traverse', 'personal')
    tsp_time_loop: int = 180
    tsp_time_traverse: int = 180
    tsp_time_personal: int = 120
    # start/end overrides: card numbers (None = auto Seoul-nearest)
    loop_start: Optional[int] = None
    traverse_start: Optional[int] = None
    traverse_end: Optional[int] = None  # None = open path (any end)
    personal_start: Optional[int] = None
    personal_end: Optional[int] = None
    metaheuristics: tuple = METAS_DEFAULT
    force_rebuild_graph: bool = False
    force_rebuild_matrix: bool = False


def _emit(progress_cb, stage, pct, msg=''):
    if progress_cb:
        try:
            progress_cb(stage, pct, msg)
        except Exception:
            pass


def haversine_m(lat1, lng1, lat2, lng2):
    R = 6371_000.0
    lat1r, lng1r = math.radians(lat1), math.radians(lng1)
    lat2r, lng2r = math.radians(lat2), math.radians(lng2)
    dlat, dlng = lat2r-lat1r, lng2r-lng1r
    h = math.sin(dlat/2)**2 + math.cos(lat1r)*math.cos(lat2r)*math.sin(dlng/2)**2
    return 2*R*math.asin(math.sqrt(h))


def chunked_bboxes():
    s,w,n,e = BBOX
    dlat = (n-s)/NLAT_CHUNKS
    dlng = (e-w)/NLNG_CHUNKS
    return [(s+i*dlat, w+j*dlng, s+(i+1)*dlat, w+(j+1)*dlng)
            for i in range(NLAT_CHUNKS) for j in range(NLNG_CHUNKS)]


def query_chunk(bbox, idx):
    s,w,n,e = bbox
    q = f"""
[out:json][timeout:180];
(
  way["highway"~"^(primary|primary_link|secondary|secondary_link|tertiary|tertiary_link|unclassified|residential|service|track|living_street)$"]
     ["motorcycle"!="no"]["motor_vehicle"!="no"]["motor_vehicle"!="private"]["motor_vehicle"!="permit"]
     ["vehicle"!="no"]
     ["access"!="no"]["access"!="private"]["access"!="military"]
     ({s},{w},{n},{e});
  way["highway"~"^(trunk|trunk_link)$"]
     ["motorroad"!="yes"]["motor_vehicle"!="designated"]
     ["motorcycle"!="no"]["motor_vehicle"!="no"]["motor_vehicle"!="private"]["motor_vehicle"!="permit"]
     ["vehicle"!="no"]
     ["access"!="no"]["access"!="private"]["access"!="military"]
     ({s},{w},{n},{e});
);
out geom tags;
"""
    cache = CACHE_DIR / f'chunk_{idx:02d}.json'
    if cache.exists():
        print(f'  [{idx:02d}] cached')
        return json.loads(cache.read_text(encoding='utf-8'))
    print(f'  [{idx:02d}] bbox=({s:.2f},{w:.2f},{n:.2f},{e:.2f}) querying...')
    t0 = time.time()
    r = None
    for attempt in range(3):
        try:
            r = requests.post(OVERPASS, data={'data': q}, headers=HEADERS, timeout=240)
            if r.status_code == 200:
                break
            print(f'    attempt {attempt+1}: status {r.status_code}, retry 20s')
            time.sleep(20)
        except Exception as ex:
            print(f'    attempt {attempt+1}: {ex}, retry 20s')
            time.sleep(20)
    if r is None or r.status_code != 200:
        raise RuntimeError(f'overpass chunk {idx} failed')
    elems = r.json().get('elements', [])
    print(f'    {len(elems)} ways in {time.time()-t0:.1f}s')
    cache.write_text(json.dumps(elems, ensure_ascii=False), encoding='utf-8')
    time.sleep(2)
    return elems


def fetch_all_ways(progress_cb=None):
    CACHE_DIR.mkdir(exist_ok=True)
    boxes = chunked_bboxes()
    all_ways = []
    for i, bbox in enumerate(boxes):
        all_ways.extend(query_chunk(bbox, i))
        _emit(progress_cb, 'graph', (i+1) / len(boxes) * 0.5, f'overpass chunk {i+1}/{len(boxes)}')
    print(f'\ntotal ways: {len(all_ways)}')
    return all_ways


DEFAULT_SPEED_KMH = {
    'trunk': 80, 'trunk_link': 60,
    'primary': 70, 'primary_link': 50,
    'secondary': 60, 'secondary_link': 50,
    'tertiary': 50, 'tertiary_link': 40,
    'unclassified': 40,
    'residential': 30,
    'living_street': 20,
    'service': 20,
    'track': 20,
}

MAXSPEED_TYPE_TABLE = {
    'ko:urban': 50, 'ko:rural': 80, 'ko:living_street': 20, 'ko:motorway': 100,
    'kr:urban': 50, 'kr:rural': 80, 'kr:living_street': 20, 'kr:motorway': 100,
}


def parse_maxspeed_str(s):
    if not s:
        return None
    s = str(s).strip().lower()
    try:
        return int(float(s))
    except ValueError:
        pass
    if 'mph' in s:
        try:
            return int(float(s.replace('mph','').strip()) * 1.60934)
        except ValueError:
            return None
    return MAXSPEED_TYPE_TABLE.get(s)


def speed_from_tags(tags):
    s = parse_maxspeed_str(tags.get('maxspeed'))
    if s is not None:
        return s
    mt = tags.get('maxspeed:type')
    if mt:
        v = MAXSPEED_TYPE_TABLE.get(str(mt).strip().lower())
        if v is not None:
            return v
    return None


def parse_oneway(tags):
    """Return traversal direction: +1 forward (in OSM way order), -1 backward, 0 bidirectional.

    Handles `oneway=yes|true|1|-1|reverse|no|false|0` plus the OSM convention that
    `junction=roundabout` is implicitly oneway unless `oneway` is explicitly set.
    """
    ow = str(tags.get('oneway', '')).strip().lower()
    if ow in ('yes', 'true', '1'):
        return 1
    if ow in ('-1', 'reverse'):
        return -1
    if ow in ('no', 'false', '0'):
        return 0
    if str(tags.get('junction', '')).strip().lower() == 'roundabout':
        return 1
    return 0


def build_graph(ways):
    """Coord-based node dedup + directional edge storage with oneway support.

    Edge tuple shape unchanged ([u, v, length, speed]) but (u,v) ordering now
    reflects traversal direction. Bidirectional ways emit both (u,v) and (v,u);
    oneway ways emit only the legal direction. Per-direction min-length coalescing.
    """
    coord_to_idx = {}
    node_coords = []
    edges_dict = {}
    dup_count = 0
    oneway_segments = 0
    for way in ways:
        geom = way.get('geometry', [])
        if len(geom) < 2:
            continue
        tags = way.get('tags', {}) or {}
        highway = tags.get('highway','')
        speed = speed_from_tags(tags)
        if speed is None:
            speed = DEFAULT_SPEED_KMH.get(highway, 30)
        speed = max(10, min(int(speed), 110))
        direction = parse_oneway(tags)
        last_idx = None
        last_pt = None
        for pt in geom:
            key = (round(pt['lat']*1e7), round(pt['lon']*1e7))
            if key not in coord_to_idx:
                coord_to_idx[key] = len(node_coords)
                node_coords.append([pt['lat'], pt['lon']])
            idx = coord_to_idx[key]
            if last_idx is not None and last_idx != idx:
                d = haversine_m(last_pt['lat'], last_pt['lon'], pt['lat'], pt['lon'])
                if direction == 1:
                    pairs = ((last_idx, idx),)
                    oneway_segments += 1
                elif direction == -1:
                    pairs = ((idx, last_idx),)
                    oneway_segments += 1
                else:
                    pairs = ((last_idx, idx), (idx, last_idx))
                for ek in pairs:
                    if ek in edges_dict:
                        dup_count += 1
                        cur_d, _ = edges_dict[ek]
                        if d < cur_d:
                            edges_dict[ek] = (d, speed)
                    else:
                        edges_dict[ek] = (d, speed)
            last_idx = idx
            last_pt = pt
    edges = [[u, v, length, speed] for (u,v), (length, speed) in edges_dict.items()]
    speeds = [e[3] for e in edges]
    print(f'graph: {len(node_coords)} nodes, {len(edges)} directed edges '
          f'(coalesced {dup_count} dups, {oneway_segments} oneway segments), '
          f'speed range {min(speeds)}-{max(speeds)} km/h (clamped 10-110)')
    return node_coords, edges


GRAPH_SCHEMA_VERSION = 2   # v1: undirected (normalized) edges  v2: directional + oneway


def _invalidate_downstream():
    for p in (DIJKSTRA_NPZ, MATRIX_JSON):
        if p.exists():
            p.unlink()


def stage_graph(progress_cb=None, force_rebuild=False):
    if not force_rebuild and GRAPH_JSON.exists():
        try:
            cached = json.loads(GRAPH_JSON.read_text(encoding='utf-8'))
        except Exception:
            cached = None
        if cached and cached.get('schema_version') == GRAPH_SCHEMA_VERSION:
            print('graph cached, loading')
            _emit(progress_cb, 'graph', 1.0, 'cached')
            return cached['nodes'], cached['edges']
        old_v = cached.get('schema_version', 1) if cached else 'unreadable'
        print(f'graph schema {old_v} -> {GRAPH_SCHEMA_VERSION}: rebuilding')
        force_rebuild = True
    if force_rebuild and GRAPH_JSON.exists():
        GRAPH_JSON.unlink()
    if force_rebuild:
        _invalidate_downstream()
    print('\n=== stage 1: overpass + graph ===')
    _emit(progress_cb, 'graph', 0.0, 'fetching overpass')
    ways = fetch_all_ways(progress_cb=progress_cb)
    _emit(progress_cb, 'graph', 0.6, 'building graph')
    nodes, edges = build_graph(ways)
    _emit(progress_cb, 'graph', 0.9, 'writing cache')
    GRAPH_JSON.write_text(json.dumps({
        'schema_version': GRAPH_SCHEMA_VERSION,
        'nodes': nodes,
        'edges': edges,
    }), encoding='utf-8')
    print(f'graph cached -> {GRAPH_JSON.name} ({GRAPH_JSON.stat().st_size/1024/1024:.1f} MB)')
    _emit(progress_cb, 'graph', 1.0, 'done')
    return nodes, edges


def snap_spots(spots, node_coords):
    print('\n=== stage 2: snap spots ===')
    pts = [Point(c[1], c[0]) for c in node_coords]
    tree = STRtree(pts)
    snapped = []
    far = []
    for s in spots:
        sp = Point(s['lng'], s['lat'])
        nearest_idx = int(tree.nearest(sp))
        node_lat, node_lng = node_coords[nearest_idx]
        d = haversine_m(s['lat'], s['lng'], node_lat, node_lng)
        snapped.append({'node_idx': nearest_idx, 'snap_dist_m': d})
        if d > SNAP_WARN_M:
            far.append((s['card'], s.get('name',''), d))
    print(f'snapped: max {max(x["snap_dist_m"] for x in snapped):.1f}m, mean {sum(x["snap_dist_m"] for x in snapped)/len(snapped):.1f}m')
    if far:
        print(f'  {len(far)} spots with snap > {SNAP_WARN_M}m:')
        for c, n, d in sorted(far, key=lambda x:-x[2])[:10]:
            print(f'    #{c} {(n or "")[:25]}: {d:.0f}m')
    return snapped


def _spots_cache_key(spots):
    """Hash of the (card, lat, lng) tuple list in *current order*.

    Order-sensitive: the matrix is position-indexed (`matrix[i][j]` = distance
    from `spots[i]` to `spots[j]`), so reordering spots must invalidate the
    cache even when values are unchanged. Reordering forces a ~90s rebuild —
    cheap compared to the silent miscorrelation a cache hit would cause.
    """
    import hashlib
    tuples = [(s['card'], round(s['lat'], 6), round(s['lng'], 6)) for s in spots]
    payload = json.dumps(tuples)
    return hashlib.sha1(payload.encode('utf-8')).hexdigest()[:16]


def stage_matrix(node_coords, edges, snapped, spots, progress_cb=None, force=False):
    spots_key = _spots_cache_key(spots)
    if not force and MATRIX_JSON.exists() and DIJKSTRA_NPZ.exists():
        cached = json.loads(MATRIX_JSON.read_text(encoding='utf-8'))
        if cached.get('spots_key') == spots_key:
            print('\n=== stage 3: matrix (cached) ===')
            _emit(progress_cb, 'matrix', 1.0, 'cached')
            return cached
        print(f'\n=== stage 3: matrix (spots changed: {cached.get("spots_key")} -> {spots_key}) ===')
    print('\n=== stage 3: matrix (dijkstra) ===')
    n_nodes = len(node_coords)
    rows, cols, data = [], [], []
    for edge in edges:
        u, v, w = edge[0], edge[1], edge[2]
        rows.append(u); cols.append(v); data.append(w)
    graph_csr = csr_matrix((data, (rows, cols)), shape=(n_nodes, n_nodes))
    src_array = np.array([x['node_idx'] for x in snapped])
    print(f'  dijkstra: {len(src_array)} sources on {n_nodes} nodes (directed)...')
    _emit(progress_cb, 'matrix', 0.1, f'dijkstra {len(src_array)} sources')
    t0 = time.time()
    dist_arr, pred = dijkstra(graph_csr, directed=True, indices=src_array, return_predecessors=True)
    print(f'  done in {time.time()-t0:.1f}s')
    _emit(progress_cb, 'matrix', 0.8, 'building matrix')
    n = len(snapped)
    dest_nodes = src_array
    matrix = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            matrix[i][j] = dist_arr[i][dest_nodes[j]]
    inf_count = int(np.isinf(matrix).sum())
    unreachable_sample: list = []
    if inf_count:
        print(f'  WARNING: {inf_count} unreachable pairs')
        inf_mask = np.isinf(matrix)
        rs, cs = np.where(inf_mask)
        for k in range(min(len(rs), 20)):
            i, j = int(rs[k]), int(cs[k])
            if i != j:
                unreachable_sample.append([int(spots[i]['card']), int(spots[j]['card'])])
        matrix[inf_mask] = 1e9
    payload = {
        'distance': matrix.tolist(), 'n': n, 'snapped': snapped, 'spots_key': spots_key,
        'unreachable_count': inf_count, 'unreachable_sample': unreachable_sample,
    }
    MATRIX_JSON.write_text(json.dumps(payload), encoding='utf-8')
    print(f'  matrix -> {MATRIX_JSON.name}')
    np.savez_compressed(DIJKSTRA_NPZ, dist=dist_arr, pred=pred, snapped=src_array)
    print(f'  dijkstra arrays -> {DIJKSTRA_NPZ.name}')
    _emit(progress_cb, 'matrix', 1.0, 'done')
    return payload


def solve_tsp(matrix, *, start, end=None, closed=False,
              metaheuristic='GUIDED_LOCAL_SEARCH', time_limit_s=120):
    """closed=True: loop (start==end). end is ignored.
    closed=False, end=None: open path, any end (virtual node).
    closed=False, end=int: open path, fixed end.
    """
    from ortools.constraint_solver import pywrapcp, routing_enums_pb2 as ENUM
    n = len(matrix)
    use_virtual_end = (not closed) and (end is None)
    if closed:
        mgr = pywrapcp.RoutingIndexManager(n, 1, start)
    elif use_virtual_end:
        mgr = pywrapcp.RoutingIndexManager(n+1, 1, [start], [n])
    else:
        mgr = pywrapcp.RoutingIndexManager(n, 1, [start], [end])
    routing = pywrapcp.RoutingModel(mgr)
    def cost(i, j):
        fi, fj = mgr.IndexToNode(i), mgr.IndexToNode(j)
        if use_virtual_end and (fi == n or fj == n):
            return 0
        return int(matrix[fi][fj])
    cb = routing.RegisterTransitCallback(cost)
    routing.SetArcCostEvaluatorOfAllVehicles(cb)
    p = pywrapcp.DefaultRoutingSearchParameters()
    p.first_solution_strategy = ENUM.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    p.local_search_metaheuristic = getattr(ENUM.LocalSearchMetaheuristic, metaheuristic)
    p.time_limit.seconds = time_limit_s
    sol = routing.SolveWithParameters(p)
    if not sol:
        raise RuntimeError(f'TSP unsolved (closed={closed}, meta={metaheuristic})')
    order = []
    idx = routing.Start(0)
    total = 0
    while not routing.IsEnd(idx):
        node = mgr.IndexToNode(idx)
        if use_virtual_end and node == n:
            pass
        else:
            order.append(node)
        nxt = sol.Value(routing.NextVar(idx))
        total += routing.GetArcCostForVehicle(idx, nxt, 0)
        idx = nxt
    if closed:
        order.append(start)
    elif not use_virtual_end:
        order.append(end)
    return order, total


def order_distance(matrix, order):
    return sum(matrix[order[i]][order[i+1]] for i in range(len(order)-1))


def _seoul_nearest(spots, candidates):
    return min(candidates, key=lambda i: haversine_m(SEOUL[0], SEOUL[1], spots[i]['lat'], spots[i]['lng']))


def _resolve_idx(spots, card_num):
    """Find spot index by card number. Return None if not found."""
    if card_num is None:
        return None
    for i, s in enumerate(spots):
        if s['card'] == card_num:
            return i
    return None


def stage_tsp(spots, matrix_data, params: BuildParams, progress_cb=None):
    print('\n=== stage 4: TSP ===')
    matrix = matrix_data['distance']
    metas = params.metaheuristics
    routes = {}

    def best_of(meta_list, solver_fn, label):
        best = None
        for i, meta in enumerate(meta_list):
            t0 = time.time()
            order, dist = solver_fn(meta)
            elapsed = time.time() - t0
            print(f'  {meta:25s} {dist/1000:7.1f} km  ({elapsed:.0f}s)')
            _emit(progress_cb, 'tsp', None, f'{label} {meta} {dist/1000:.1f}km')
            if best is None or dist < best['dist']:
                best = {'meta': meta, 'order': order, 'dist': dist}
        return best

    n_modes = len(params.enabled_modes)
    mode_done = 0

    if 'loop' in params.enabled_modes:
        loop_start = _resolve_idx(spots, params.loop_start)
        if loop_start is None:
            loop_start = _seoul_nearest(spots, range(len(spots)))
        print(f'\n[loop] start: idx {loop_start} = card #{spots[loop_start]["card"]} {spots[loop_start].get("name","")}')
        loop_best = best_of(
            metas,
            lambda m: solve_tsp(matrix, closed=True, start=loop_start, metaheuristic=m,
                                time_limit_s=params.tsp_time_loop),
            'loop')
        routes['loop'] = {
            'order': loop_best['order'], 'distance_m': loop_best['dist'], 'meta': loop_best['meta'],
            'start_card': spots[loop_start]['card'],
            'end_card': spots[loop_start]['card'],
        }
        mode_done += 1
        _emit(progress_cb, 'tsp', mode_done / n_modes, 'loop done')

    if 'traverse' in params.enabled_modes:
        trav_start = _resolve_idx(spots, params.traverse_start)
        if trav_start is None:
            trav_start = _seoul_nearest(spots, range(len(spots)))
        trav_end = _resolve_idx(spots, params.traverse_end)
        end_msg = f'card #{spots[trav_end]["card"]}' if trav_end is not None else '(any)'
        print(f'\n[traverse] start: idx {trav_start} = card #{spots[trav_start]["card"]}, end: {end_msg}')
        trav_best = best_of(
            metas,
            lambda m: solve_tsp(matrix, closed=False, start=trav_start, end=trav_end,
                                metaheuristic=m, time_limit_s=params.tsp_time_traverse),
            'traverse')
        routes['traverse'] = {
            'order': trav_best['order'], 'distance_m': trav_best['dist'], 'meta': trav_best['meta'],
            'start_card': spots[trav_start]['card'],
            'end_card': spots[trav_end]['card'] if trav_end is not None else spots[trav_best['order'][-1]]['card'],
        }
        mode_done += 1
        _emit(progress_cb, 'tsp', mode_done / n_modes, 'traverse done')

    if 'personal' in params.enabled_modes:
        owned_set = set(params.owned_cards)
        owned_idx = [i for i, s in enumerate(spots) if s['card'] in owned_set]
        if not owned_idx:
            print('\n[personal] no owned cards — skip')
        else:
            print(f'\n[personal] {len(owned_idx)} owned cards')
            pers_start_full = _resolve_idx(spots, params.personal_start)
            if pers_start_full is None or pers_start_full not in owned_idx:
                pers_start_full = _seoul_nearest(spots, owned_idx)
            pers_start = owned_idx.index(pers_start_full)
            pers_end_full = _resolve_idx(spots, params.personal_end)
            pers_end = owned_idx.index(pers_end_full) if (pers_end_full in owned_idx) else None
            end_msg = f'card #{spots[pers_end_full]["card"]}' if pers_end is not None else '(any)'
            print(f'  start: card #{spots[pers_start_full]["card"]}, end: {end_msg}')
            sub_mat = [[matrix[i][j] for j in owned_idx] for i in owned_idx]
            pers_best = best_of(
                metas,
                lambda m: (lambda o, d: ([owned_idx[k] for k in o], d))(
                    *solve_tsp(sub_mat, closed=False, start=pers_start, end=pers_end,
                               metaheuristic=m, time_limit_s=params.tsp_time_personal)),
                'personal')
            end_card_resolved = (spots[pers_end_full]['card'] if pers_end is not None
                                 else spots[pers_best['order'][-1]]['card'])
            routes['personal'] = {
                'order': pers_best['order'], 'distance_m': pers_best['dist'], 'meta': pers_best['meta'],
                'owned_cards': sorted(owned_set),
                'start_card': spots[pers_start_full]['card'],
                'end_card': end_card_resolved,
            }
            # codex comparison (optional, only if codex_personal.json exists and modes include personal)
            if CODEX_JSON.exists():
                try:
                    codex = json.loads(CODEX_JSON.read_text(encoding='utf-8'))
                    codex_cards = codex.get('order_cards', [])
                    codex_idx = [next((i for i, s in enumerate(spots) if s['card'] == c), None)
                                 for c in codex_cards]
                    if all(x is not None for x in codex_idx):
                        codex_dist = order_distance(matrix, codex_idx)
                        print(f'  codex order: {codex_dist/1000:.1f} km vs mine {pers_best["dist"]/1000:.1f} km '
                              f'(delta {(codex_dist - pers_best["dist"])/1000:+.1f})')
                        routes['personal']['codex_compare'] = {
                            'codex_distance_m': codex_dist,
                            'codex_order_indices': codex_idx,
                            'codex_order_cards': codex_cards,
                        }
                except Exception as ex:
                    print(f'  codex compare skipped: {ex}')
            mode_done += 1
            _emit(progress_cb, 'tsp', mode_done / n_modes, 'personal done')

    return routes


def reconstruct_path(pred_row, src, dst):
    path = []
    cur = int(dst)
    while cur != src and cur >= 0:
        path.append(cur)
        nxt = int(pred_row[cur])
        if nxt == -9999:
            return None
        cur = nxt
    if cur == src:
        path.append(int(src))
        return list(reversed(path))
    return None


def stage_poly(routes, node_coords, progress_cb=None):
    print('\n=== stage 5: polylines ===')
    arrs = np.load(DIJKSTRA_NPZ)
    pred = arrs['pred']
    src_nodes = arrs['snapped']

    def build_poly(order_idx_list, closed=False):
        pts = []
        seq = list(order_idx_list) + ([order_idx_list[0]] if closed else [])
        for i in range(len(seq)-1):
            a, b = seq[i], seq[i+1]
            path = reconstruct_path(pred[a], int(src_nodes[a]), int(src_nodes[b]))
            if not path:
                path = [int(src_nodes[a]), int(src_nodes[b])]
            for n_idx in path:
                lat, lng = node_coords[n_idx]
                if pts and pts[-1] == (lat, lng):
                    continue
                pts.append((lat, lng))
        return polyline_lib.encode(pts)

    for key, r in routes.items():
        order = r['order']
        closed = (len(order) >= 2 and order[0] == order[-1])
        seq = order[:-1] if closed else order
        r['polyline'] = build_poly(seq, closed=closed)
        print(f'  [{key}] polyline {len(r["polyline"])} chars')
    _emit(progress_cb, 'poly', 1.0, 'done')
    return routes


def stage_duration(routes, edges, node_coords, progress_cb=None):
    print('\n=== stage 6: duration ===')
    arrs = np.load(DIJKSTRA_NPZ)
    pred = arrs['pred']
    src_nodes = arrs['snapped']

    edge_info = {}
    for edge in edges:
        u, v, length, speed = edge[0], edge[1], edge[2], edge[3]
        edge_info[(u, v)] = (length, speed)

    def path_leg_details(pred_row, src, dst):
        """Walk predecessor chain dst->src, collect path nodes + per-edge length.

        Returns (distance_m, duration_s, mid_lat, mid_lng) or None when
        unreachable. Midpoint is the path node nearest to cumulative-length/2.
        """
        nodes_rev = [int(dst)]
        seg_lengths_rev = []
        total_dur = 0.0
        cur = int(dst)
        while cur != src and cur >= 0:
            nxt = int(pred_row[cur])
            if nxt == -9999:
                return None
            if (nxt, cur) in edge_info:
                length, speed = edge_info[(nxt, cur)]
                seg_lengths_rev.append(length)
                total_dur += length / (speed * 1000.0 / 3600.0)
            else:
                seg_lengths_rev.append(0.0)
            nodes_rev.append(nxt)
            cur = nxt
        if cur != src:
            return None
        nodes = list(reversed(nodes_rev))
        seg_lengths = list(reversed(seg_lengths_rev))
        total_len = sum(seg_lengths)
        if total_len <= 0 or len(nodes) < 2:
            mid_idx = len(nodes) // 2
        else:
            half = total_len / 2.0
            acc = 0.0
            mid_idx = 0
            for i, L in enumerate(seg_lengths):
                if acc + L >= half:
                    mid_idx = i if (half - acc) < ((acc + L) - half) else i + 1
                    break
                acc += L
            else:
                # Defensive — unreachable in practice: sum(seg_lengths) == total_len,
                # so acc + L >= half must trigger by the final iteration.
                mid_idx = len(nodes) - 1
        lat, lng = node_coords[nodes[mid_idx]]
        return total_len, total_dur, float(lat), float(lng)

    for key, r in routes.items():
        order = r['order']
        legs = []
        total_s = 0.0
        for i in range(len(order)-1):
            a, b = order[i], order[i+1]
            result = path_leg_details(pred[a], int(src_nodes[a]), int(src_nodes[b]))
            if result is None:
                print(f'  [{key}] leg {a}->{b}: unreachable')
                legs.append({'from_idx': a, 'to_idx': b, 'distance_m': 0.0,
                             'duration_s': 0.0, 'mid_lat': None, 'mid_lng': None})
                continue
            d_m, d_s, mid_lat, mid_lng = result
            total_s += d_s
            legs.append({'from_idx': a, 'to_idx': b,
                         'distance_m': round(d_m, 1),
                         'duration_s': round(d_s, 1),
                         'mid_lat': round(mid_lat, 7),
                         'mid_lng': round(mid_lng, 7)})
        r['legs'] = legs
        r['duration_s'] = total_s
        if total_s > 0:
            avg = (r['distance_m'] / 1000.0) / (total_s / 3600.0)
            print(f'  [{key}] {total_s/3600:.1f}h ({total_s/60:.0f} min)  avg {avg:.1f} km/h')
    _emit(progress_cb, 'duration', 1.0, 'done')
    return routes


def get_spots(extra_spots: Sequence[dict] = ()) -> list[dict]:
    """Base 220 spots + extra EPIC spots (only those with valid lat/lng)."""
    base = json.loads(SPOTS_JSON.read_text(encoding='utf-8'))
    valid_extra = [s for s in extra_spots
                   if isinstance(s.get('lat'), (int, float)) and isinstance(s.get('lng'), (int, float))]
    return base + valid_extra


def build_routes(params: Optional[BuildParams] = None, progress_cb=None) -> dict:
    """Top-level orchestrator. Returns routes dict, also writes ROUTES_JSON."""
    if params is None:
        params = BuildParams(owned_cards=json.loads(OWNED_JSON.read_text(encoding='utf-8')))

    spots = get_spots(params.extra_spots)

    node_coords, edges = stage_graph(progress_cb=progress_cb,
                                     force_rebuild=params.force_rebuild_graph)
    snapped = snap_spots(spots, node_coords)
    matrix_data = stage_matrix(node_coords, edges, snapped, spots,
                               progress_cb=progress_cb, force=params.force_rebuild_matrix)
    routes = stage_tsp(spots, matrix_data, params, progress_cb=progress_cb)
    routes = stage_poly(routes, node_coords, progress_cb=progress_cb)
    routes = stage_duration(routes, edges, node_coords, progress_cb=progress_cb)
    # Attach matrix-level diagnostics under a reserved key. Caller must pop
    # before passing routes to build_html (which iterates per-route entries).
    routes['_diagnostics'] = {
        'unreachable_count': int(matrix_data.get('unreachable_count', 0)),
        'unreachable_sample': matrix_data.get('unreachable_sample', []),
    }
    ROUTES_JSON.write_text(json.dumps(routes, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\nroutes -> {ROUTES_JSON.name}')
    return routes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force-graph', action='store_true')
    ap.add_argument('--force-matrix', action='store_true')
    args = ap.parse_args()
    owned = json.loads(OWNED_JSON.read_text(encoding='utf-8'))
    params = BuildParams(
        owned_cards=owned,
        force_rebuild_graph=args.force_graph,
        force_rebuild_matrix=args.force_matrix,
    )
    build_routes(params)


if __name__ == '__main__':
    main()
