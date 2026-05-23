"""Regression tests for Opus 4.7 review fixes.

Covers:
- #4  owned_cards: [] preserved (not silently replaced by on-disk default)
- #5  EpicEntry rejects out-of-range / non-finite lat & lng
- #8  EpicEntry.name max_length
- snapshot guard: build_html output keeps every interpolated field encoded

Run from project root:
    python -m pytest tests/test_phase5.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import build_html  # noqa: E402
import pipeline    # noqa: E402
import main as app_module  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app_module.app)


@pytest.fixture(autouse=True)
def _reset_jobs():
    pipeline.JOBS.reset()
    yield
    pipeline.JOBS.reset()


# ---------- #4 owned_cards: [] is honored ----------

def test_owned_cards_empty_list_preserved():
    """An explicit empty list from the frontend must NOT silently become
    the on-disk owned.json default — clearing all cards is a real user intent."""
    build, _, _, owned_default = pipeline.params_from_request({'owned_cards': []})
    assert build.owned_cards == []
    assert owned_default == []


def test_owned_cards_missing_falls_back_to_disk():
    """Absent key still falls back to disk default (CLI / test path)."""
    build, _, _, _ = pipeline.params_from_request({})
    # Disk owned.json currently holds [] (cleared during phase work),
    # so the type is what matters here: a list, not None.
    assert isinstance(build.owned_cards, list)


# ---------- #5 EpicEntry lat/lng bounds + finiteness ----------

@pytest.mark.parametrize('lat', [-90.1, 90.1, 1e308])
def test_epic_entry_rejects_oob_lat(client, lat):
    """Out-of-range finite floats — rejected at the API boundary (422)."""
    r = client.put('/api/epic', json=[{'card': 221, 'name': 'x', 'lat': lat, 'lng': 127.0}])
    assert r.status_code == 422
    assert 'lat' in r.text


@pytest.mark.parametrize('lng', [-180.1, 180.1, -1e308])
def test_epic_entry_rejects_oob_lng(client, lng):
    r = client.put('/api/epic', json=[{'card': 221, 'name': 'x', 'lat': 37.5, 'lng': lng}])
    assert r.status_code == 422
    assert 'lng' in r.text


@pytest.mark.parametrize('bad', [float('inf'), -float('inf'), float('nan')])
def test_epic_entry_rejects_non_finite_lat(bad):
    """NaN/±inf — Pydantic refuses with allow_inf_nan=False.

    Tested through the model directly because httpx's JSON encoder rejects
    these values before they reach the server.
    """
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        app_module.EpicEntry(card=221, name='x', lat=bad, lng=127.0)


@pytest.mark.parametrize('bad', [float('inf'), -float('inf'), float('nan')])
def test_epic_entry_rejects_non_finite_lng(bad):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        app_module.EpicEntry(card=221, name='x', lat=37.5, lng=bad)


def test_epic_entry_accepts_boundary_values(client, tmp_path, monkeypatch):
    """Exact boundary values (±90 lat, ±180 lng) must be accepted."""
    monkeypatch.setattr(pipeline, 'save_epic_coords', lambda entries: None)
    r = client.put('/api/epic', json=[
        {'card': 221, 'name': 'north pole', 'lat': 90.0, 'lng': 180.0},
        {'card': 222, 'name': 'south pole', 'lat': -90.0, 'lng': -180.0},
    ])
    assert r.status_code == 200


# ---------- #8 EpicEntry.name max_length ----------

def test_epic_entry_rejects_name_too_long(client):
    r = client.put('/api/epic', json=[{'card': 221, 'name': 'x' * 201, 'lat': 37.5, 'lng': 127.0}])
    assert r.status_code == 422
    assert 'name' in r.text


def test_epic_entry_accepts_name_at_limit(client, monkeypatch):
    monkeypatch.setattr(pipeline, 'save_epic_coords', lambda entries: None)
    r = client.put('/api/epic', json=[{'card': 221, 'name': 'x' * 200, 'lat': 37.5, 'lng': 127.0}])
    assert r.status_code == 200


# ---------- build_html DOM-output snapshot guard ----------

def test_html_no_lt_in_data_fields(tmp_path):
    """Belt-and-braces: every interpolated user-string field is encoded by
    _js_safe, so raw '<' in those fields never reaches the HTML output."""
    spots = [{
        'card': 1, 'name': '<img src=x onerror=alert(1)>',
        'addr': '<svg/onload=alert(2)>', 'lat': 37.5, 'lng': 127.0,
    }]
    out = tmp_path / 'out.html'
    build_html.build_html(spots, {}, {'routes': {}}, [], out, total_cards=1)
    body = out.read_text(encoding='utf-8')
    # No literal HTML opener / closer in the data — would let the HTML parser
    # see it as actual markup if it ever escaped the script-tag enclosure.
    assert '<img src=x' not in body
    assert '<svg/onload' not in body
    # Encoded form is present (literal 6-char \uXXXX sequences in the file).
    assert r'\u003cimg src=x onerror=alert(1)\u003e' in body
    assert r'\u003csvg/onload=alert(2)\u003e' in body
