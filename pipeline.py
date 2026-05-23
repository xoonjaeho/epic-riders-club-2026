"""Orchestrator wrapping build_motorcycle + verify_motorcycle + build_html.

Used by main.py (FastAPI). Provides a single run_pipeline(req, progress_cb)
that takes a frontend request dict and produces an HTML file.
"""
from __future__ import annotations
import json, time, uuid
from pathlib import Path
from threading import Lock
from typing import Callable, Optional

from build_motorcycle import (
    BuildParams, build_routes, get_spots,
    OWNED_JSON, ROUTES_JSON, GRAPH_JSON, MATRIX_JSON, DIJKSTRA_NPZ,
)
from verify_motorcycle import VerifyParams, verify
from build_html import build_html

ROOT = Path(__file__).parent
DATA_DIR = ROOT / 'data'
EPIC_COORDS_JSON = DATA_DIR / 'epic_coords.json'
OUT_DIR = ROOT / 'out'
TOTAL_CARDS = 237   # 220 public + 17 EPIC

DATA_DIR.mkdir(exist_ok=True)
OUT_DIR.mkdir(exist_ok=True)


# ---------- EPIC coords persistence ----------
_epic_lock = Lock()


def load_epic_coords() -> list[dict]:
    """Return EPIC card entries as [{card, name, lat, lng}, ...]."""
    if not EPIC_COORDS_JSON.exists():
        return _seed_epic_template()
    with _epic_lock:
        return json.loads(EPIC_COORDS_JSON.read_text(encoding='utf-8'))


def save_epic_coords(entries: list[dict]) -> None:
    """Persist EPIC card entries. Each entry: {card:int, name:str, lat?:float, lng?:float}."""
    with _epic_lock:
        EPIC_COORDS_JSON.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding='utf-8')


def _seed_epic_template() -> list[dict]:
    """Default 17 EPIC entries without coordinates (#221..#237)."""
    return [{'card': 220 + i, 'name': f'EPIC {i:03d}', 'lat': None, 'lng': None}
            for i in range(1, 18)]


# ---------- request → params ----------
def params_from_request(req: dict) -> tuple[BuildParams, VerifyParams, Path, list[int]]:
    """Map frontend request dict to BuildParams + VerifyParams + out_path + owned_default."""
    p = req.get('params', {}) or {}
    extras = req.get('epic_extras', [])
    extras_valid = [e for e in extras
                    if e.get('lat') is not None and e.get('lng') is not None]
    # Honor explicit empty list — user clearing all owned cards must NOT fall
    # back to the on-disk default. Only fall back when the field is absent.
    owned = req.get('owned_cards')
    if owned is None:
        owned = json.loads(OWNED_JSON.read_text(encoding='utf-8'))

    build = BuildParams(
        owned_cards=list(owned),
        extra_spots=extras_valid,
        enabled_modes=tuple(p.get('enabled_modes', ['loop', 'traverse', 'personal'])),
        tsp_time_loop=int(p.get('tsp_time_loop', 180)),
        tsp_time_traverse=int(p.get('tsp_time_traverse', 180)),
        tsp_time_personal=int(p.get('tsp_time_personal', 120)),
        loop_start=_opt_int(p.get('loop_start')),
        traverse_start=_opt_int(p.get('traverse_start')),
        traverse_end=_opt_int(p.get('traverse_end')),
        personal_start=_opt_int(p.get('personal_start')),
        personal_end=_opt_int(p.get('personal_end')),
        metaheuristics=tuple(p.get('metaheuristics',
                                   ['GUIDED_LOCAL_SEARCH', 'SIMULATED_ANNEALING', 'TABU_SEARCH'])),
        force_rebuild_graph=bool(p.get('force_rebuild_graph', False)),
        force_rebuild_matrix=bool(p.get('force_rebuild_matrix', False)),
    )
    vparams = VerifyParams(
        mode=p.get('verify_mode', 'intersect'),
        buffer_m=float(p.get('verify_buffer_m', 3.0)),
        bearing_threshold_deg=float(p.get('verify_bearing_threshold_deg', 30.0)),
        use_grade_separation_filter=bool(p.get('verify_grade_separation', True)),
        sample_interval_m=float(p.get('verify_sample_interval_m', 1000.0)),
        violation_radius_m=float(p.get('verify_violation_radius_m', 30.0)),
    )
    out_name = req.get('out_name', 'EPIC RIDERS CLUB 2026.html')
    out_path = OUT_DIR / out_name
    return build, vparams, out_path, list(owned)


def _opt_int(v):
    if v is None or v == '':
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------- pipeline ----------
def run_pipeline(req: dict, progress_cb: Optional[Callable] = None) -> dict:
    """Execute full pipeline. Returns {ok, out_path, routes_summary, verify_summary, elapsed_s}."""
    t0 = time.time()
    build, vparams, out_path, owned_default = params_from_request(req)
    spots = get_spots(build.extra_spots)

    _safe_emit(progress_cb, 'init', 0.0, f'spots={len(spots)} owned={len(build.owned_cards)}')
    routes = build_routes(build, progress_cb=progress_cb)
    diagnostics = routes.pop('_diagnostics', {})
    verify_report = verify(routes, vparams, progress_cb=progress_cb)
    _safe_emit(progress_cb, 'html', 0.0, 'rendering html')
    build_html(spots, routes, verify_report, owned_default, out_path,
               total_cards=TOTAL_CARDS)
    elapsed = time.time() - t0
    _safe_emit(progress_cb, 'done', 1.0, f'{elapsed:.1f}s')

    return {
        'ok': True,
        'out_path': str(out_path),
        'routes_summary': {k: {
            'distance_m': v['distance_m'],
            'duration_s': v.get('duration_s'),
            'meta': v.get('meta'),
            'start_card': v.get('start_card'),
            'end_card': v.get('end_card'),
            'spots_count': len(v.get('order', [])),
        } for k, v in routes.items()},
        'verify_summary': {k: {
            'mode': r.get('mode'),
            'violation_km_filtered': r.get('violation_km_filtered', 0.0),
            'violation_count_filtered': r.get('violation_count_filtered', 0),
        } for k, r in verify_report.get('routes', {}).items()},
        'diagnostics': diagnostics,
        'elapsed_s': elapsed,
    }


def _safe_emit(cb, stage, pct, msg):
    if cb:
        try:
            cb(stage, pct, msg)
        except Exception:
            pass


# ---------- job manager (background execution) ----------
MAX_JOBS = 50  # cap retained history; oldest finished jobs evicted past this


class JobManager:
    """In-memory job tracker for async pipeline runs (single-user app)."""
    def __init__(self):
        self._jobs: dict[str, dict] = {}
        self._lock = Lock()

    def create(self) -> Optional[str]:
        """Allocate a new job. Returns None if another build is still active
        (status pending/running) — single-user app, one build at a time.

        When the retention cap is reached, evict the oldest finished jobs first.
        """
        with self._lock:
            for j in self._jobs.values():
                if j['status'] in ('pending', 'running'):
                    return None
            if len(self._jobs) >= MAX_JOBS:
                finished = sorted(
                    (j for j in self._jobs.values() if j['status'] in ('done', 'error')),
                    key=lambda j: j['created_at'],
                )
                for old in finished:
                    if len(self._jobs) < MAX_JOBS:
                        break
                    del self._jobs[old['id']]
            jid = uuid.uuid4().hex[:12]
            self._jobs[jid] = {
                'id': jid,
                'status': 'pending',
                'events': [],
                'result': None,
                'error': None,
                'created_at': time.time(),
            }
        return jid

    def list_ids(self) -> list[str]:
        with self._lock:
            return list(self._jobs.keys())

    def reset(self) -> None:
        """Test-only seam: clear all jobs. Production code never calls this."""
        with self._lock:
            self._jobs.clear()

    def emit(self, jid: str, stage: str, pct, msg: str):
        with self._lock:
            job = self._jobs.get(jid)
            if not job:
                return
            job['events'].append({'stage': stage, 'pct': pct, 'msg': msg, 't': time.time()})

    def set_status(self, jid: str, status: str):
        with self._lock:
            if jid in self._jobs:
                self._jobs[jid]['status'] = status

    def set_result(self, jid: str, result: dict):
        with self._lock:
            if jid in self._jobs:
                self._jobs[jid]['result'] = result
                self._jobs[jid]['status'] = 'done'

    def set_error(self, jid: str, err: str):
        with self._lock:
            if jid in self._jobs:
                self._jobs[jid]['error'] = err
                self._jobs[jid]['status'] = 'error'

    def get(self, jid: str) -> dict | None:
        with self._lock:
            j = self._jobs.get(jid)
            return None if j is None else dict(j)

    def events_since(self, jid: str, n: int) -> list[dict]:
        with self._lock:
            job = self._jobs.get(jid)
            if not job:
                return []
            return list(job['events'][n:])


JOBS = JobManager()


def run_pipeline_async(jid: str, req: dict):
    """Background worker entry. Captures progress events into JOBS."""
    def cb(stage, pct, msg):
        JOBS.emit(jid, stage, pct, msg)
    JOBS.set_status(jid, 'running')
    try:
        result = run_pipeline(req, progress_cb=cb)
        JOBS.set_result(jid, result)
    except KeyboardInterrupt:
        raise
    except BaseException as ex:
        import sys, traceback
        # Full traceback goes to server logs; client gets only the short summary
        # (no filesystem paths, no internal call stack).
        traceback.print_exception(ex, file=sys.stderr)
        JOBS.set_error(jid, f'{type(ex).__name__}: {ex}')


# ---------- cache control ----------
def invalidate_graph_cache():
    """Force rebuild of graph + matrix on next run."""
    for p in (GRAPH_JSON, MATRIX_JSON, DIJKSTRA_NPZ):
        if p.exists():
            p.unlink()


def cache_status() -> dict:
    return {
        'graph': GRAPH_JSON.exists(),
        'matrix': MATRIX_JSON.exists(),
        'dijkstra': DIJKSTRA_NPZ.exists(),
        'routes': ROUTES_JSON.exists(),
    }
