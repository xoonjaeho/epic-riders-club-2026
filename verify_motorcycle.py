#!/usr/bin/env python
"""Motorcycle legality post-verification.

Exposes verify(routes_data, params, progress_cb) for FastAPI integration.
CLI mode preserved.
"""
from __future__ import annotations
import argparse, json, math, sys, time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import requests
import polyline as polyline_lib
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

sys.stdout.reconfigure(encoding='utf-8')

ROOT = Path(__file__).parent
OVERPASS = 'https://overpass-api.de/api/interpreter'

SAMPLE_INTERVAL_M = 1000
VIOLATION_RADIUS_M = 30
PADDING_DEG = 0.05
DEFAULT_BUFFER_M = 3.0
DEFAULT_BEARING_THRESHOLD = 30.0

M_PER_DEG_LAT = 111_000.0
M_PER_DEG_LNG = 111_000.0 * math.cos(math.radians(38))


@dataclass
class VerifyParams:
    """Parameters for verify()."""
    mode: str = 'intersect'  # 'intersect' | 'point' | 'off'
    buffer_m: float = DEFAULT_BUFFER_M
    bearing_threshold_deg: float = DEFAULT_BEARING_THRESHOLD
    use_grade_separation_filter: bool = True
    sample_interval_m: float = SAMPLE_INTERVAL_M
    violation_radius_m: float = VIOLATION_RADIUS_M


def _emit(cb, stage, pct, msg=''):
    if cb:
        try:
            cb(stage, pct, msg)
        except Exception:
            pass


def haversine(a, b):
    R = 6371_000.0
    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    lat2, lng2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlng = lat2-lat1, lng2-lng1
    h = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlng/2)**2
    return 2*R*math.asin(math.sqrt(h))


def sample_polyline(pts, interval_m):
    out = [pts[0]]
    acc = 0.0
    for i in range(1, len(pts)):
        seg = haversine(pts[i-1], pts[i])
        if seg == 0:
            continue
        acc += seg
        while acc >= interval_m:
            t = 1 - (acc - interval_m) / seg
            lat = pts[i-1][0] + t * (pts[i][0] - pts[i-1][0])
            lng = pts[i-1][1] + t * (pts[i][1] - pts[i-1][1])
            out.append((lat, lng))
            acc -= interval_m
    out.append(pts[-1])
    return out


def fetch_restricted_ways(bbox):
    s, w, n, e = bbox
    q = f"""
[out:json][timeout:180];
(
  way["highway"="motorway"]({s},{w},{n},{e});
  way["highway"="motorway_link"]({s},{w},{n},{e});
  way["motorcycle"="no"]({s},{w},{n},{e});
  way["motor_vehicle"="no"]({s},{w},{n},{e});
);
out geom tags;
"""
    print(f'  overpass restricted bbox=({s:.2f},{w:.2f},{n:.2f},{e:.2f}) ...')
    t0 = time.time()
    headers = {
        'User-Agent': 'epic-riders-verifier/1.0',
        'Accept': 'application/json',
    }
    r = requests.post(OVERPASS, data={'data': q}, headers=headers, timeout=240)
    if r.status_code != 200:
        print(f'    response {r.status_code}: {r.text[:300]}')
    r.raise_for_status()
    ways = r.json().get('elements', [])
    print(f'    {len(ways)} restricted ways in {time.time()-t0:.1f}s')
    return ways


def fetch_elevated_ways(bbox):
    s, w, n, e = bbox
    q = f"""
[out:json][timeout:180];
(
  way["bridge"="yes"]({s},{w},{n},{e});
  way["tunnel"="yes"]({s},{w},{n},{e});
  way["layer"~"^-?[1-9]"]({s},{w},{n},{e});
);
out geom tags;
"""
    print(f'  overpass elevated bbox=({s:.2f},{w:.2f},{n:.2f},{e:.2f}) ...')
    t0 = time.time()
    headers = {
        'User-Agent': 'epic-riders-verifier/1.0',
        'Accept': 'application/json',
    }
    r = requests.post(OVERPASS, data={'data': q}, headers=headers, timeout=240)
    if r.status_code != 200:
        print(f'    response {r.status_code}: {r.text[:300]}')
    r.raise_for_status()
    elems = r.json().get('elements', [])
    print(f'    {len(elems)} elevated/depressed ways in {time.time()-t0:.1f}s')
    return elems


def to_xy(lat, lng):
    return (lng * M_PER_DEG_LNG, lat * M_PER_DEG_LAT)


def _bearing_deg(p1, p2):
    dx = p2[0] - p1[0]; dy = p2[1] - p1[1]
    if dx == 0 and dy == 0:
        return 0.0
    return math.degrees(math.atan2(dy, dx)) % 180


def _angular_diff(a, b):
    d = abs(a - b) % 180
    return d if d <= 90 else 180 - d


def _segment_bearing_near(line, pt, max_dist=15.0):
    coords = list(line.coords)
    best_d = float('inf'); best_b = None
    for i in range(len(coords)-1):
        seg = LineString([coords[i], coords[i+1]])
        d = seg.distance(pt)
        if d < best_d:
            best_d = d
            best_b = _bearing_deg(coords[i], coords[i+1])
            if best_d == 0:
                break
    return best_b if best_d <= max_dist else None


def verify_route(name, encoded, ways, params: VerifyParams,
                 elevated_tree=None, elevated_lines=None):
    pts = polyline_lib.decode(encoded)
    total_km = sum(haversine(pts[i-1],pts[i]) for i in range(1,len(pts)))/1000
    print(f'\n  [{name}] decoded {len(pts)} pts, total {total_km:.1f} km')

    if params.mode == 'point':
        samples = sample_polyline(pts, params.sample_interval_m)
        print(f'    sampled {len(samples)} pts @ {params.sample_interval_m}m')
    else:
        print(f'    intersect mode, buffer {params.buffer_m:.0f}m')

    way_lines = []
    way_meta = []
    for w in ways:
        geom = w.get('geometry') or []
        if len(geom) < 2:
            continue
        coords = [to_xy(n['lat'], n['lon']) for n in geom]
        way_lines.append(LineString(coords))
        tags = w.get('tags', {})
        kind = ('motorway' if tags.get('highway','').startswith('motorway') else
                'motorcycle=no' if tags.get('motorcycle')=='no' else
                'motor_vehicle=no' if tags.get('motor_vehicle')=='no' else
                'other')
        try:
            layer = int(tags.get('layer','0'))
        except ValueError:
            layer = 0
        way_meta.append({
            'kind': kind,
            'name': tags.get('name','') or tags.get('ref',''),
            'bridge': tags.get('bridge') == 'yes',
            'tunnel': tags.get('tunnel') == 'yes',
            'layer': layer,
        })

    tree = STRtree(way_lines) if way_lines else None

    if params.mode == 'intersect':
        route_xy = [to_xy(lat, lng) for lat, lng in pts]
        route_line = LineString(route_xy)
        route_buf = route_line.buffer(params.buffer_m)
        route_segments = [LineString([route_xy[i], route_xy[i+1]]) for i in range(len(route_xy)-1)]
        route_seg_tree = STRtree(route_segments) if route_segments else None

        def route_bearing_at(pt_xy):
            if route_seg_tree is None:
                return None
            pt = Point(pt_xy)
            cands = route_seg_tree.query(pt.buffer(20))
            best_d = float('inf'); best_b = None
            for ci in cands:
                seg = route_segments[int(ci)]
                d = seg.distance(pt)
                if d < best_d:
                    best_d = d
                    c = list(seg.coords)
                    best_b = _bearing_deg(c[0], c[1])
            return best_b if best_d <= 20 else None

        violations_per_way = []
        if tree is not None:
            cand_idx = tree.query(route_buf)
            for wi in cand_idx:
                inter = way_lines[wi].intersection(route_buf)
                if inter.is_empty:
                    continue
                length_m = inter.length
                if length_m < 1.0:
                    continue
                if inter.geom_type == 'LineString':
                    sub_lines = [inter]
                elif inter.geom_type == 'MultiLineString':
                    sub_lines = list(inter.geoms)
                elif inter.geom_type == 'GeometryCollection':
                    sub_lines = [g for g in inter.geoms if g.geom_type in ('LineString','MultiLineString')]
                else:
                    sub_lines = []
                polylines = []
                for ls in sub_lines:
                    if ls.geom_type == 'MultiLineString':
                        for sub in ls.geoms:
                            if sub.length < 1.0:
                                continue
                            latlng = [(y/M_PER_DEG_LAT, x/M_PER_DEG_LNG) for x,y in sub.coords]
                            polylines.append(polyline_lib.encode(latlng))
                    else:
                        if ls.length < 1.0:
                            continue
                        latlng = [(y/M_PER_DEG_LAT, x/M_PER_DEG_LNG) for x,y in ls.coords]
                        polylines.append(polyline_lib.encode(latlng))
                centroid = inter.centroid if hasattr(inter,'centroid') else inter
                meta = way_meta[wi]
                self_elevated = meta['bridge'] or meta['tunnel'] or meta['layer'] != 0
                nearby_elevated = False
                nearby_elevated_kind = None
                if params.use_grade_separation_filter and elevated_tree is not None and elevated_lines is not None:
                    cands = elevated_tree.query(inter.buffer(5))
                    for ei in cands:
                        ev = elevated_lines[ei]
                        if ev['line'].distance(inter) < 5.0 and ev['layer'] != meta['layer']:
                            nearby_elevated = True
                            nearby_elevated_kind = ev['kind']
                            break
                way_b = _segment_bearing_near(way_lines[wi], centroid, max_dist=20.0)
                route_b = route_bearing_at((centroid.x, centroid.y))
                bearing_diff = _angular_diff(way_b, route_b) if (way_b is not None and route_b is not None) else None
                bearing_filter = bearing_diff is not None and bearing_diff > params.bearing_threshold_deg
                filtered = self_elevated or nearby_elevated or bearing_filter
                violations_per_way.append({
                    'way_idx': int(wi),
                    'kind': meta['kind'],
                    'name': meta['name'] or '(unnamed)',
                    'length_m': float(length_m),
                    'centroid_latlng': [float(centroid.y/M_PER_DEG_LAT), float(centroid.x/M_PER_DEG_LNG)],
                    'polylines': polylines,
                    'self_bridge': meta['bridge'],
                    'self_tunnel': meta['tunnel'],
                    'self_layer': meta['layer'],
                    'nearby_elevated': nearby_elevated,
                    'nearby_kind': nearby_elevated_kind,
                    'bearing_diff_deg': bearing_diff,
                    'bearing_filter': bearing_filter,
                    'filtered': filtered,
                })
        violations_per_way.sort(key=lambda v: -v['length_m'])
        total_violation_m = sum(v['length_m'] for v in violations_per_way)
        kept = [v for v in violations_per_way if not v['filtered']]
        kept_m = sum(v['length_m'] for v in kept)
        print(f'    violations: {len(violations_per_way)} ways, {total_violation_m:.1f}m total')
        print(f'    after filter: {len(kept)} ways, {kept_m:.1f}m ({kept_m/1000:.3f} km)')
        return {
            'route': name,
            'mode': 'intersect',
            'buffer_m': params.buffer_m,
            'violation_ways': violations_per_way,
            'violation_km_actual': total_violation_m / 1000.0,
            'violation_km_filtered': kept_m / 1000.0,
            'violation_count_filtered': len(kept),
        }

    # point mode
    violations = []
    for i, (lat, lng) in enumerate(samples):
        if tree is None:
            break
        p = Point(*to_xy(lat, lng))
        cand_idx = tree.query(p.buffer(params.violation_radius_m))
        for wi in cand_idx:
            d = way_lines[wi].distance(p)
            if d <= params.violation_radius_m:
                violations.append({
                    'sample_idx': i, 'lat': lat, 'lng': lng,
                    'way_idx': int(wi), 'dist_m': float(d),
                    'kind': way_meta[wi]['kind'],
                    'name': way_meta[wi]['name'],
                })
                break

    segments = []
    cur = None
    for v in violations:
        if cur and v['sample_idx'] == cur['end_idx'] + 1 and v['kind'] == cur['kind']:
            cur['end_idx'] = v['sample_idx']
            cur['names'].add(v['name'])
        else:
            if cur:
                segments.append(cur)
            cur = {
                'start_idx': v['sample_idx'], 'end_idx': v['sample_idx'],
                'kind': v['kind'], 'names': {v['name']},
                'start_lat': v['lat'], 'start_lng': v['lng'],
            }
    if cur:
        segments.append(cur)

    for s in segments:
        s['length_km'] = (s['end_idx'] - s['start_idx'] + 1) * params.sample_interval_m / 1000.0
        s['names'] = sorted(s['names'] - {''}) or ['(unnamed)']

    total_violation_km = sum(s['length_km'] for s in segments)
    print(f'    violations: {len(violations)} samples in {len(segments)} segments, ~{total_violation_km:.1f} km')
    return {
        'route': name,
        'mode': 'point',
        'total_samples': len(samples),
        'violation_samples': len(violations),
        'violation_segments': segments,
        'violation_km_approx': total_violation_km,
    }


def verify(routes_data: dict, params: Optional[VerifyParams] = None,
           progress_cb=None) -> dict:
    """Run verification across loop/traverse/personal polylines. Returns report dict."""
    if params is None:
        params = VerifyParams()
    if params.mode == 'off':
        return {'routes': {k: {'route': k, 'mode': 'off', 'violation_ways': []}
                            for k in routes_data.keys()}}

    all_pts = []
    for r in routes_data.values():
        if not r.get('polyline'):
            continue
        all_pts.extend(polyline_lib.decode(r['polyline']))
    if not all_pts:
        return {'routes': {}}
    lats = [p[0] for p in all_pts]
    lngs = [p[1] for p in all_pts]
    bbox = (min(lats)-PADDING_DEG, min(lngs)-PADDING_DEG, max(lats)+PADDING_DEG, max(lngs)+PADDING_DEG)
    print(f'combined bbox: {bbox}')

    _emit(progress_cb, 'verify', 0.0, 'overpass restricted')
    ways = fetch_restricted_ways(bbox)
    _emit(progress_cb, 'verify', 0.3, f'{len(ways)} restricted ways')

    elevated_tree = None
    elevated_lines = None
    if params.mode == 'intersect' and params.use_grade_separation_filter:
        _emit(progress_cb, 'verify', 0.4, 'overpass elevated')
        elevated_ways = fetch_elevated_ways(bbox)
        elevated_lines = []
        for w in elevated_ways:
            geom = w.get('geometry') or []
            if len(geom) < 2:
                continue
            coords = [to_xy(n['lat'], n['lon']) for n in geom]
            tags = w.get('tags', {})
            kind = ('bridge' if tags.get('bridge')=='yes' else
                    'tunnel' if tags.get('tunnel')=='yes' else
                    f'layer={tags.get("layer","?")}')
            try:
                layer = int(tags.get('layer','0'))
            except ValueError:
                layer = 0
            elevated_lines.append({'line': LineString(coords), 'kind': kind, 'layer': layer})
        if elevated_lines:
            elevated_tree = STRtree([e['line'] for e in elevated_lines])
            print(f'  elevated_tree built with {len(elevated_lines)} ways')

    report = {'bbox': bbox, 'restricted_ways_total': len(ways), 'routes': {}}
    keys = list(routes_data.keys())
    for i, name in enumerate(keys):
        r = routes_data[name]
        if not r.get('polyline'):
            continue
        _emit(progress_cb, 'verify', 0.5 + 0.5 * i / max(1, len(keys)), f'verify {name}')
        report['routes'][name] = verify_route(
            name, r['polyline'], ways, params,
            elevated_tree=elevated_tree, elevated_lines=elevated_lines
        )
    _emit(progress_cb, 'verify', 1.0, 'done')
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--routes', default='routes_motorcycle.json')
    ap.add_argument('--out', default=None)
    ap.add_argument('--mode', choices=['point', 'intersect', 'off'], default='intersect')
    ap.add_argument('--buffer', type=float, default=DEFAULT_BUFFER_M)
    args = ap.parse_args()
    routes_path = ROOT / args.routes
    suffix = '' if args.mode == 'point' else f'_intersect{int(args.buffer)}m'
    verify_path = ROOT / (args.out or f'verify_{routes_path.stem}{suffix}.json')
    print(f'input : {routes_path.name}')
    print(f'output: {verify_path.name}')
    routes = json.loads(routes_path.read_text(encoding='utf-8'))
    params = VerifyParams(mode=args.mode, buffer_m=args.buffer)
    report = verify(routes, params)
    verify_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\nreport -> {verify_path.name}')

    print('\n=== summary ===')
    for name, rep in report.get('routes', {}).items():
        if rep.get('mode') == 'intersect':
            print(f'  {name:10s} raw {rep.get("violation_km_actual",0):5.3f} km / {len(rep.get("violation_ways",[]))} ways · '
                  f'filtered {rep.get("violation_km_filtered",0):5.3f} km / {rep.get("violation_count_filtered",0)} ways')
        else:
            print(f'  {name:10s} {rep.get("violation_km_approx",0):5.1f} km / {len(rep.get("violation_segments",[]))} segments')


if __name__ == '__main__':
    main()
