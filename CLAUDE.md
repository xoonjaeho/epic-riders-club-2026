# epic-riders-club-2026

Route map generator for EPIC RIDERS CLUB 2026 — 220 Gangwon + 17 EPIC cards. FastAPI local web app on port 8804.

## Run

```
./run.bat
```

Browse to http://127.0.0.1:8804.

## Architecture

```
[Browser] ── /api/build ──> [FastAPI main.py] ── Thread ──> [pipeline.run_pipeline]
   ▲                           │                                    │
   │                           ▼                                    ▼
   │      ┌─ /api/job/{jid}/events (SSE) ─┐         [build_motorcycle.py]
   │      │   stage: graph/matrix/tsp/poly/duration/verify/html      │
   └──────┘                                                          ▼
                                                       [verify_motorcycle.py]
                                                                     │
                                                                     ▼
                                                          [build_html.py]
                                                                     │
   /api/download/{jid} <── FileResponse ── out/EPIC RIDERS CLUB 2026.html
```

All three backend modules expose both a **function API** and a **CLI**:
- `build_motorcycle.build_routes(params: BuildParams, progress_cb)`
- `verify_motorcycle.verify(routes, params: VerifyParams, progress_cb)`
- `build_html.build_html(spots, routes, verify_report, owned, out_path, total_cards)`

CLI mode is kept for regression testing and debugging.

## Key Decisions

- Road data: Overpass API → in-house graph (no dependency on external routing services).
- Motorcycle-legal filter: exclude highway=motorway/_link, motor_vehicle=designated/no/private/permit, motorcycle=no, vehicle=no, access=no/private/military. For trunk, additionally exclude motorroad=yes / motor_vehicle=designated.
- Node dedup: coord-only (lat/lng at 1e-7 precision). Shared bridge endpoint nodes are preserved to keep connectivity — grade-separation is handled in verification, not graph building.
- Edge coalesce: when multiple ways share the same directed (u,v), pick the minimum length. **Required to avoid a scipy.sparse.csr_matrix bug that sums duplicate entries** (otherwise distances inflate by ~5%).
- Direction-aware edges: `oneway=yes|true|1|-1|reverse` and implicit `junction=roundabout` parsed in `parse_oneway`. Bidirectional ways emit both `(u,v)` and `(v,u)`; oneway ways emit only the legal direction. Dijkstra runs with `directed=True`. **Required** — without this, motorcycle routes can be returned that traverse oneway segments in reverse (safety hazard).
- Graph cache `schema_version` (currently `2`) auto-invalidates `mc_graph.json` + downstream `matrix_motorcycle.json` + `mc_dijkstra.npz` on schema bumps.
- Speed clamp 10–110 km/h: filters OSM maxspeed tag outliers.
- Verification false-positive filters:
  - grade-separation: bridge=yes / tunnel=yes / layer!=0
  - bearing difference > 30°: merge / cross / overpass
  - intersection length < 1m: noise
- Matrix is auto-invalidated by a `spots_key` SHA1 hash (detects EPIC coordinate changes).
- TSP: PATH_CHEAPEST_ARC seed + 3 metaheuristics in parallel; pick the shortest.
- start/end semantics:
  - loop: start only (end = start, closed TSP)
  - traverse / personal: free start; end is free (virtual node) or fixed
- Card number convention: zero-padded "001".

## Color Palette A

| Element | Hex |
|---------|-----|
| loop | `#e67e22` |
| traverse | `#8e44ad` |
| personal | `#2980b9` |
| owned | `#95a5a6` |
| visited | `#27ae60` |
| start | `#f1c40f` + ⭐ |
| end | `#2c3e50` + 🏁 |
| violation | `#e91e63` |

## Emoji Set A (UI labels)

🛣️ road distance · ⏱️ duration · 🃏 owned · 📍 visited · 🔄 reset · 🔁 loop · ↔️ traverse · 👤 personal · ⭐ start · 🏁 end

⭐/🏁 are used in the builder UI for endpoint selection. **Generated HTML** drops start/end emoji from map markers and the card list — start/end are surfaced via color cues only (yellow/dark marker background + left border stripe on list rows).

## Conventions

- Frontend: no `innerHTML`. Use DOM API (`createElement` / `textContent`) only — XSS prevention and passes the security hook.
- Marker labels and tooltips are built through the `el()` helper, inserting only trusted primitives (numbers, fixed class names) as text nodes.
- Generated HTML right-side panel stack (auto-repositioned by `repositionPanels()`): `#stats` → `#route-toggle` → `#card-list-box`. Card list is collapsible (header click), starts collapsed on mobile (≤600px), shows route order with cards sortable by `[번호 | 순서] × [↑ | ↓]` (sort state persisted in `epic-riders-2026-card-sort`). Sort button active background follows the current route color via `.card-list-box[data-route="..."] .sort-btn.active` selectors.
- Default selected route in generated HTML: `personal`. Route toggle order: `personal / traverse / loop`. `loop` card list hides the closing duplicate row so the count reads 220, not 221.
- LocalStorage keys (live builder UI, served from `127.0.0.1:8804`):
  - `erc-2026-owned` — owned card set
  - `erc-2026-epic-local` — EPIC coords mirror (backup of server PUT)
  - `erc-2026-endpoints` — start/end per route mode
- LocalStorage keys (generated single-file HTML, typically opened via `file://`):
  - `epic-riders-2026-owned` — owned card set
  - `epic-riders-2026-visited` — visited card set
  - `epic-riders-2026-card-sort` — card list panel sort state `{by:'card'|'order', dir:'asc'|'desc'}`
  - The two key spaces are intentionally separate — different origins, no collision possible, but state is also not shared between builder UI and the published map.

## Backend semantics

- `POST /api/build` — **single build at a time**. Returns HTTP 409 (`a build is already in progress`) if any prior job is still `pending`/`running`. Frontend must wait for the SSE `done` / `error` event before reposting; `static/app.js` also disables `#btn-build` for the duration as a UX guard. `tests/test_phase3.py` covers both layers.
- `BuildRequest.out_name` — bare filename only. Path separators (`/`, `\`), NUL byte, empty string, `.`, and `..` are rejected at the Pydantic validator (`main.py:_safe_out_name`) with HTTP 422. Output is always written under `out/`, never elsewhere. `tests/test_phase3.py::test_out_name_rejects_unsafe` is the regression guard — relaxing the validator must keep this passing.
- `JobManager` retains the last `MAX_JOBS = 50` jobs in memory; oldest `done`/`error` jobs are evicted first when the cap is reached. Active (`pending`/`running`) jobs are never evicted. Process restart clears everything — no on-disk job history.
- Each backend module (`build_motorcycle.py`, `verify_motorcycle.py`, `build_html.py`) ships a `main()` CLI that operates against the on-disk JSON artifacts. Useful for debugging a single stage in isolation — e.g. re-run only verify after tweaking buffer/bearing params, without re-running graph + matrix + TSP (~10 minutes saved).

## Testing

```
python -m pytest tests/ -v
```

`tests/test_phase{1..4}.py` cover the security/correctness regressions from the multi-LLM code review. Pytest discovers them automatically; no `pytest.ini` needed. `tests/` may grow over time but each test should remain narrow and assert observable behavior.

## Caching

See top-level README.md for sizes. Cache validity keys:
- Graph: `mc_graph.json` existence = valid (permanent unless explicitly rebuilt).
- Matrix: valid when `matrix_motorcycle.json`'s `spots_key` matches the current spots (EPIC coord changes invalidate automatically).
