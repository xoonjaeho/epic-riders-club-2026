"""Regression tests for Phase 1 review fixes (REVIEW.md findings #1, #2).

Run from project root:
    pip install pytest
    python -m pytest tests/test_phase1.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import build_html  # noqa: E402
import pipeline    # noqa: E402


def test_html_escapes_close_script_in_spots(tmp_path):
    """Finding #1: an injected </script> in card data must not terminate
    the inline <script> block. _js_safe encodes < > & and JS line terminators
    as \\uXXXX so the HTML parser cannot act on any byte in the JSON payload."""
    spots = [{
        'card': 1,
        'name': '</script><script>EVIL()</script>',
        'lat': 37.5,
        'lng': 127.0,
        'addr': '',
    }]
    out = tmp_path / 'out.html'
    build_html.build_html(spots, {}, {'routes': {}}, [], out, total_cards=1)
    body = out.read_text(encoding='utf-8')

    # Raw injection sequence must NOT appear inside the JSON payload.
    assert '</script><script>EVIL()' not in body
    # `<` and `>` are encoded as < / > in the JSON literal (literal
    # 6-char sequences in the file — Python does NOT decode them on read).
    assert r'\u003c/script\u003e' in body
    assert r'\u003cscript\u003eEVIL()' in body
    # HTML parser sees exactly two `</script>` tokens (Leaflet CDN + data block closing).
    assert body.count('</script>') == 2


def test_run_pipeline_async_catches_systemexit(monkeypatch):
    """Finding #2: SystemExit from the worker (BaseException, not Exception)
    must surface as a job error rather than leaving status stuck on 'running'."""
    def fail(*args, **kwargs):
        raise SystemExit('simulated unrecoverable failure')
    monkeypatch.setattr(pipeline, 'run_pipeline', fail)

    jid = pipeline.JOBS.create()
    pipeline.run_pipeline_async(jid, {})

    job = pipeline.JOBS.get(jid)
    assert job is not None
    assert job['status'] == 'error', f"expected 'error', got {job['status']}"
    assert 'SystemExit' in (job['error'] or '')
