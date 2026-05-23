"""FastAPI entry for EPIC RIDERS CLUB 2026 Map Builder.

Run:  uvicorn main:app --host 127.0.0.1 --port 8804 --reload
Or:   run.bat
"""
from __future__ import annotations
import asyncio, json
from pathlib import Path
from threading import Thread

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from typing import Any, Optional

import pipeline
from build_motorcycle import OWNED_JSON, SPOTS_JSON

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / 'static'
INDEX_HTML = STATIC_DIR / 'index.html'

app = FastAPI(title='EPIC RIDERS CLUB 2026 Map Builder')

# Static files served at /static/*
app.mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static')


# ---------- models ----------
class EpicEntry(BaseModel):
    card: int
    name: str = Field(default='', max_length=200)
    # Bound coordinates and reject NaN/±inf — garbage values would persist to
    # data/epic_coords.json and contaminate the distance matrix.
    lat: Optional[float] = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    lng: Optional[float] = Field(default=None, ge=-180, le=180, allow_inf_nan=False)


class BuildRequest(BaseModel):
    owned_cards: list[int] = []
    epic_extras: list[EpicEntry] = []
    params: dict[str, Any] = {}
    out_name: str = 'EPIC RIDERS CLUB 2026.html'

    @field_validator('out_name')
    @classmethod
    def _safe_out_name(cls, v: str) -> str:
        if '/' in v or '\\' in v or '\x00' in v or v in ('', '.', '..'):
            raise ValueError('out_name must be a bare filename without path separators')
        return v


# ---------- routes ----------
@app.get('/', response_class=HTMLResponse)
def index():
    if not INDEX_HTML.exists():
        return HTMLResponse('<h1>index.html not found</h1>', status_code=500)
    return HTMLResponse(INDEX_HTML.read_text(encoding='utf-8'))


@app.get('/api/cards')
def get_cards():
    """Return 220 base spots + 17 EPIC entries (with or without coords) + owned default."""
    spots = json.loads(SPOTS_JSON.read_text(encoding='utf-8'))
    epic = pipeline.load_epic_coords()
    owned_default = json.loads(OWNED_JSON.read_text(encoding='utf-8'))
    return {
        'spots': spots,
        'epic': epic,
        'owned_default': sorted(set(owned_default)),
        'total': pipeline.TOTAL_CARDS,
    }


@app.put('/api/epic')
def put_epic(entries: list[EpicEntry]):
    """Save EPIC card entries (overwrites entire list)."""
    pipeline.save_epic_coords([e.model_dump() for e in entries])
    return {'ok': True, 'count': len(entries)}


@app.get('/api/status')
def get_status():
    return {
        'cache': pipeline.cache_status(),
        'jobs': pipeline.JOBS.list_ids(),
    }


@app.post('/api/rebuild-graph')
def rebuild_graph():
    pipeline.invalidate_graph_cache()
    return {'ok': True, 'cache': pipeline.cache_status()}


@app.post('/api/build')
def build(req: BuildRequest):
    """Kick off pipeline in background. Returns job_id."""
    body = req.model_dump()
    body['epic_extras'] = [e for e in body['epic_extras']
                            if e.get('lat') is not None and e.get('lng') is not None]
    # Reject empty selections at the boundary: an empty `enabled_modes` would
    # produce an HTML with zero routes; an empty `metaheuristics` while a mode
    # is enabled would crash stage_tsp with TypeError on best_of() returning None.
    p = body.get('params') or {}
    if 'enabled_modes' in p and not p['enabled_modes']:
        raise HTTPException(400, 'enabled_modes must contain at least one mode')
    if 'metaheuristics' in p and not p['metaheuristics']:
        raise HTTPException(400, 'metaheuristics must contain at least one metaheuristic')
    jid = pipeline.JOBS.create()
    if jid is None:
        raise HTTPException(409, 'a build is already in progress')
    t = Thread(target=pipeline.run_pipeline_async, args=(jid, body), daemon=True)
    t.start()
    return {'job_id': jid}


@app.get('/api/job/{jid}')
def get_job(jid: str):
    job = pipeline.JOBS.get(jid)
    if not job:
        raise HTTPException(404, f'job {jid} not found')
    return {
        'id': job['id'],
        'status': job['status'],
        'events': job['events'],
        'result': job['result'],
        'error': job['error'],
    }


@app.get('/api/job/{jid}/events')
async def job_events(jid: str, request: Request):
    """Server-Sent Events stream for job progress."""
    from fastapi.responses import StreamingResponse

    async def gen():
        last_n = 0
        while True:
            if await request.is_disconnected():
                break
            job = pipeline.JOBS.get(jid)
            if not job:
                yield f'event: error\ndata: {json.dumps({"err": "job not found"})}\n\n'
                return
            new_events = pipeline.JOBS.events_since(jid, last_n)
            for ev in new_events:
                yield f'event: progress\ndata: {json.dumps(ev)}\n\n'
            last_n += len(new_events)
            if job['status'] == 'done':
                yield f'event: done\ndata: {json.dumps(job["result"])}\n\n'
                return
            if job['status'] == 'error':
                yield f'event: error\ndata: {json.dumps({"err": job["error"]})}\n\n'
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type='text/event-stream')


@app.get('/api/download/{jid}')
def download(jid: str):
    job = pipeline.JOBS.get(jid)
    if not job:
        raise HTTPException(404, 'job not found')
    if job['status'] != 'done' or not job['result']:
        raise HTTPException(409, 'job not finished')
    out_path = Path(job['result']['out_path'])
    if not out_path.exists():
        raise HTTPException(410, 'output file missing')
    return FileResponse(str(out_path), media_type='text/html',
                        filename=out_path.name)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run('main:app', host='127.0.0.1', port=8804, reload=False)
