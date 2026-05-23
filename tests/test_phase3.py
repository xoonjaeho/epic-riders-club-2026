"""Regression tests for Phase 3 review fixes (REVIEW.md findings #3, #4, #5).

Run from project root:
    python -m pytest tests/test_phase3.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pipeline  # noqa: E402
import main as app_module  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app_module.app)


@pytest.fixture(autouse=True)
def _reset_jobs():
    """Clear JobManager state between tests to keep them independent."""
    pipeline.JOBS.reset()
    yield
    pipeline.JOBS.reset()


# ---------- Finding #3: out_name path traversal ----------

@pytest.mark.parametrize('bad', [
    '../foo.html',
    r'..\foo.html',
    '/etc/passwd',
    r'C:\Windows\foo.html',
    'sub/dir/foo.html',
    '',
    '.',
    '..',
    'a\x00b.html',
])
def test_out_name_rejects_unsafe(client, bad):
    r = client.post('/api/build', json={'out_name': bad})
    assert r.status_code == 422, f'expected 422 for {bad!r}, got {r.status_code}'


def test_out_name_accepts_bare_filename(client, monkeypatch):
    # Stop the actual pipeline from running — we only test that validation passes.
    monkeypatch.setattr(pipeline, 'run_pipeline_async', lambda *a, **kw: None)
    r = client.post('/api/build', json={'out_name': 'my-route.html'})
    assert r.status_code == 200
    assert 'job_id' in r.json()


# ---------- Finding #4: concurrent build guard ----------

def test_jobmanager_rejects_second_build_while_active():
    jid1 = pipeline.JOBS.create()
    assert jid1 is not None
    jid2 = pipeline.JOBS.create()
    assert jid2 is None, 'second create() must return None while first is pending/running'


def test_jobmanager_allows_new_build_after_previous_done():
    jid1 = pipeline.JOBS.create()
    pipeline.JOBS.set_result(jid1, {'out_path': 'fake'})
    jid2 = pipeline.JOBS.create()
    assert jid2 is not None and jid2 != jid1


def test_jobmanager_allows_new_build_after_previous_error():
    jid1 = pipeline.JOBS.create()
    pipeline.JOBS.set_error(jid1, 'simulated')
    jid2 = pipeline.JOBS.create()
    assert jid2 is not None


def test_build_endpoint_returns_409_when_busy(client, monkeypatch):
    monkeypatch.setattr(pipeline, 'run_pipeline_async', lambda *a, **kw: None)
    r1 = client.post('/api/build', json={})
    assert r1.status_code == 200
    # Without finishing the first job, second POST must be rejected.
    r2 = client.post('/api/build', json={})
    assert r2.status_code == 409


# ---------- Build input validation (empty mode / meta) ----------

def test_build_endpoint_rejects_empty_enabled_modes(client):
    r = client.post('/api/build', json={'params': {'enabled_modes': []}})
    assert r.status_code == 400
    assert 'enabled_modes' in r.text


def test_build_endpoint_rejects_empty_metaheuristics(client):
    r = client.post('/api/build', json={'params': {'metaheuristics': []}})
    assert r.status_code == 400
    assert 'metaheuristics' in r.text


# ---------- Finding #5: list_ids thread-safety ----------

def test_list_ids_returns_snapshot():
    pipeline.JOBS.create()
    pipeline.JOBS.set_result(list(pipeline.JOBS._jobs.keys())[0], {'out_path': 'x'})
    pipeline.JOBS.create()
    ids = pipeline.JOBS.list_ids()
    assert isinstance(ids, list)
    assert len(ids) == 2


def test_status_endpoint_uses_list_ids(client):
    r = client.get('/api/status')
    assert r.status_code == 200
    body = r.json()
    assert 'jobs' in body
    assert isinstance(body['jobs'], list)
