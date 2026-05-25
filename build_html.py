#!/usr/bin/env python
"""Generate final HTML deliverable.

Exposes build_html(spots, routes, verify_report, owned_default, out_path, options)
for FastAPI integration. CLI mode preserved.

Palette A:
  loop      #e67e22 (orange)        — contrasts with map's green background
  traverse  #8e44ad (purple)        — distinguished from loop
  personal  #2980b9 (blue)
  owned     #95a5a6 (neutral grey)
  visited   #27ae60 (green)
  start     #f1c40f (yellow)        + ⭐
  end       #2c3e50 (dark)          + 🏁
  violation #e91e63 (pink)

Emoji A (UI labels):
  🛣️ road distance  ⏱️ duration  🃏 owned  📍 visited  🔄 reset
  🔁 loop  ↔️ traverse  👤 personal  ⭐ start  🏁 end
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from typing import Optional

sys.stdout.reconfigure(encoding='utf-8')

ROOT = Path(__file__).parent
SPOTS_JSON = ROOT / 'spots.json'
OWNED_JSON = ROOT / 'owned.json'
ROUTES_JSON = ROOT / 'routes_motorcycle.json'
DEFAULT_VERIFY_JSON = ROOT / 'verify_routes_motorcycle_intersect3m.json'
DEFAULT_OUT_HTML = Path.home() / 'Desktop' / 'EPIC RIDERS CLUB 2026.html'

HTML = r'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>EPIC RIDERS CLUB 2026</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
 html,body{height:100%;margin:0;font-family:'Pretendard','Apple SD Gothic Neo','Malgun Gothic',sans-serif}
 #map{height:100%}
 .stats{position:absolute;top:14px;right:14px;background:#fff;padding:12px 16px;
        border-radius:10px;box-shadow:0 6px 20px rgba(0,0,0,.18);z-index:1000;font-size:14px;line-height:1.6}
 .stats h1{margin:0 0 8px 0;font-size:18px;font-weight:700}
 .stats h1 a{color:inherit;text-decoration:none}
 .stats h1 a:hover{text-decoration:underline}
 .stats .row{display:flex;justify-content:space-between;gap:14px}
 .stats .lbl{color:#555}
 .stats .val{color:#000;font-weight:600;text-align:right;font-variant-numeric:tabular-nums}
 .stats .reset{font-size:11px;background:#eee;border:1px solid #ccc;color:#444;
               border-radius:4px;padding:2px 6px;cursor:pointer;margin-left:6px}
 .stats .reset:hover{background:#ddd}
 .route-toggle{position:absolute;right:14px;background:#fff;padding:6px;border-radius:10px;
               box-shadow:0 6px 20px rgba(0,0,0,.18);z-index:1000;display:flex;gap:4px;align-items:center}
 .route-toggle button{padding:6px 14px;border:1px solid #ddd;background:#fafafa;border-radius:6px;
                      font-size:13px;font-weight:600;cursor:pointer;color:#444}
 .route-toggle button.active{color:#fff;border-color:transparent}
 .route-toggle button.active[data-route="loop"]{background:#e67e22}
 .route-toggle button.active[data-route="traverse"]{background:#8e44ad}
 .route-toggle button.active[data-route="personal"]{background:#2980b9}
 .marker-label{background:#fff;border:1px solid #888;border-radius:14px;padding:2px 7px;font-size:11px;
               font-weight:600;color:#222;white-space:nowrap;box-shadow:0 1px 3px rgba(0,0,0,.25)}
 .marker-label.owned{background:#95a5a6;color:#fff;border-color:#7f8c8d}
 .marker-label.visited{background:#27ae60;color:#fff;border-color:#1e874b;opacity:.9}
 .marker-label.start{background:#f1c40f;color:#222;border-color:#d4ac0d;font-size:12px;padding:3px 9px;font-weight:700}
 .marker-label.end{background:#2c3e50;color:#fff;border-color:#1a252f;font-size:12px;padding:3px 9px;font-weight:700}
 .popup-card{font-size:14px;line-height:1.5;min-width:0}
 .popup-card h3{margin:0 0 6px;font-size:18px;font-weight:700}
 .popup-card .addr{color:#444}
 .popup-card .coords{font-family:Consolas,monospace;color:#666;font-size:12px}
 .popup-card .links{display:flex;flex-wrap:nowrap;gap:6px;margin-top:8px}
 .popup-card .links a{flex:1;text-align:center;text-decoration:none;color:#fff;background:#3498db;
                      padding:7px 0;border-radius:6px;font-size:14px;font-weight:600;white-space:nowrap}
 .popup-card .links a.naver{background:#03c75a}
 .popup-card .links a.kakao{background:#fee500;color:#191919}
 .popup-card .actions{display:flex;gap:6px;margin-top:6px}
 .popup-card .actions button{flex:1;border:none;color:#fff;padding:8px;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer}
 .popup-card .actions button.own{background:#7f8c8d}
 .popup-card .actions button.own.active{background:#34495e}
 .popup-card .actions button.visit{background:#27ae60}
 .popup-card .actions button.visit.active{background:#196f3d}
 .popup-card .legs{margin:6px 0;font-size:13px;color:#333;line-height:1.5}
 .popup-card .legs .leg-row{display:flex;justify-content:space-between;gap:8px}
 .popup-card .legs .leg-lbl{color:#666}
 .popup-card .legs .leg-val{font-weight:600;font-variant-numeric:tabular-nums}
 .v-popup{font-size:13px;line-height:1.5;min-width:140px}
 .v-popup b{color:#e91e63}
 .leg-chip{background:#fff;border:1px solid #888;border-radius:10px;
           padding:1px 6px;font-size:10px;font-weight:600;color:#333;
           white-space:nowrap;box-shadow:0 1px 2px rgba(0,0,0,.25);
           pointer-events:none;font-variant-numeric:tabular-nums}
</style>
</head>
<body>
<div id="map"></div>
<div class="stats" id="stats">
  <h1><a href="https://epic-riders.cnr-korea.com/" target="_blank">EPIC RIDERS CLUB 2026</a></h1>
  <div class="row"><span class="lbl">🛣️ 도로 거리</span><span class="val" id="distVal">—</span></div>
  <div class="row"><span class="lbl">⏱️ 예상 시간</span><span class="val" id="durVal">—</span></div>
  <div class="row"><span class="lbl">🃏 보유</span><span class="val"><span id="ownedCount">0</span> / __TOTAL_CARDS__ <button class="reset" onclick="resetOwned()">🔄 초기화</button></span></div>
  <div class="row"><span class="lbl">📍 방문</span><span class="val"><span id="visitedCount">0</span> / __TOTAL_CARDS__ <button class="reset" onclick="resetVisited()">🔄 초기화</button></span></div>
</div>
<div class="route-toggle" id="route-toggle">
  <button data-route="loop" class="active">🔁 loop</button>
  <button data-route="traverse">↔️ traverse</button>
  <button data-route="personal">👤 personal</button>
</div>
<script>
const SPOTS = __SPOTS__;
const ROUTES = __ROUTES__;
const VIOLATIONS = __VIOLATIONS__;
const OWNED_DEFAULT = __OWNED_DEFAULT__;
const ROUTE_COLOR = {loop:'#e67e22', traverse:'#8e44ad', personal:'#2980b9'};
const STORAGE_KEY_VISITED = 'epic-riders-2026-visited';
const STORAGE_KEY_OWNED   = 'epic-riders-2026-owned';

let currentRoute = Object.keys(ROUTES)[0] || 'loop';
const map = L.map('map', {zoomControl: true}).setView([37.8, 128.3], 8);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 18, attribution: '© OpenStreetMap'}).addTo(map);

let polyLayer = null;
let vLayer = null;
let chipLayer = null;
// Tuned to 12: at zoom 11 (~76m/px @ lat 38°) 220-leg loops have midpoints
// that overlap in mountainous regions (설악·태백). 12 (~38m/px) clears those.
const CHIP_MIN_ZOOM = 12;
const markerByIdx = new Map();

const visited = new Set(JSON.parse(localStorage.getItem(STORAGE_KEY_VISITED) || '[]'));
const ownedStored = localStorage.getItem(STORAGE_KEY_OWNED);
const owned = new Set(ownedStored ? JSON.parse(ownedStored) : OWNED_DEFAULT);
function saveVisited(){ localStorage.setItem(STORAGE_KEY_VISITED, JSON.stringify([...visited])); }
function saveOwned(){ localStorage.setItem(STORAGE_KEY_OWNED, JSON.stringify([...owned])); }

function decodePolyline(str){
  let index=0,lat=0,lng=0,coords=[];
  while(index < str.length){
    let b,shift=0,result=0;
    do{ b=str.charCodeAt(index++)-63; result|=(b&0x1f)<<shift; shift+=5; }while(b>=0x20);
    lat += (result&1)?~(result>>1):(result>>1);
    shift=0;result=0;
    do{ b=str.charCodeAt(index++)-63; result|=(b&0x1f)<<shift; shift+=5; }while(b>=0x20);
    lng += (result&1)?~(result>>1):(result>>1);
    coords.push([lat*1e-5, lng*1e-5]);
  }
  return coords;
}

function fmtKm(m){ return (m/1000).toFixed(0).replace(/\B(?=(\d{3})+(?!\d))/g, ',') + ' km'; }
function fmtTime(s){ const h=Math.floor(s/3600), m=Math.floor((s%3600)/60); return h+'시간 '+m+'분'; }
function fmtLegTime(s){
  // Short leg duration: "0분", "12분", "1시간 5분". <30s rounds to 0분 (no leg / unreachable).
  if (!s || s < 30) return '0분';
  const m = Math.round(s/60);
  if (m < 60) return m + '분';
  const h = Math.floor(m/60), rem = m % 60;
  return rem === 0 ? (h + '시간') : (h + '시간 ' + rem + '분');
}
function pad3(n){ return String(n).padStart(3, '0'); }
function cardLabel(c){ return c >= 221 ? ('EPIC ' + pad3(c - 220)) : ('#' + pad3(c)); }

function legsAroundCard(idx){
  // Returns {prev, next} legs adjacent to this card on currentRoute.
  // - loop: closing leg is "prev" for the start card; outgoing is "next".
  // - traverse/personal: start has prev=null; end has next=null.
  // Assumes each idx appears at most once in order, except the loop closing
  // duplicate (order[0] == order[N-1]). TSP guarantees this today; relax with
  // a lastPos branch if routes ever revisit cards.
  const r = ROUTES[currentRoute];
  if (!r || !r.legs || !r.legs.length) return {prev: null, next: null};
  const order = r.order;
  const isLoop = order.length >= 2 && order[0] === order[order.length-1];
  const firstPos = order.indexOf(idx);
  if (firstPos < 0) return {prev: null, next: null};
  let prev = null, next = null;
  if (firstPos > 0) prev = r.legs[firstPos - 1];
  else if (isLoop) prev = r.legs[r.legs.length - 1];
  if (firstPos < order.length - 1) next = r.legs[firstPos];
  return {prev, next};
}

// DOM builder helper — same contract as the live builder UI's el():
// every text value goes through textContent, every attribute via setAttribute.
// No string concatenation that mixes user data with markup.
function el(tag, props, content){
  const node = document.createElement(tag);
  if (props){
    for (const k in props){
      const v = props[k];
      if (v == null) continue;
      if (k === 'class') node.className = v;
      else node.setAttribute(k, v);
    }
  }
  if (Array.isArray(content)){
    for (const c of content){
      if (c == null) continue;
      node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    }
  } else if (typeof content === 'string'){
    node.textContent = content;
  } else if (content){
    node.appendChild(content);
  }
  return node;
}

function makeIcon(idx, visitNum){
  const card = SPOTS[idx].card;
  const route = ROUTES[currentRoute];
  const isStart = route && route.order && idx === route.order[0];
  const lastIdx = route && route.order ? route.order[route.order.length-1] : null;
  const isLoop = route && route.order && route.order[0] === lastIdx;
  const isEnd = !isLoop && idx === lastIdx;
  const isVisited = visited.has(card);
  const isOwned = owned.has(card);
  let cls = 'marker-label';
  if (isVisited) cls += ' visited';
  else if (isOwned) cls += ' owned';
  if (isStart) cls += ' start';
  else if (isEnd) cls += ' end';
  const prefix = isStart ? '⭐ ' : isEnd ? '🏁 ' : '';
  const label = prefix + visitNum + ' ' + cardLabel(card);
  return L.divIcon({className:'', html: el('div', {class: cls}, label), iconSize:null});
}

function makePopup(idx){
  const s = SPOTS[idx];
  const isVisited = visited.has(s.card);
  const isOwned = owned.has(s.card);
  // Naver: desktop fallback uses /p/search/{address} — Naver has no
  // documented web URL that drops a pin at given lat/lng directly, so we
  // search by full address (more precise than searching by name).
  // Mobile clicks are intercepted by the delegated handler below and
  // rewritten to `nmap://place?lat=&lng=&name=` for exact app-side coords.
  const naverWebUrl = 'https://map.naver.com/p/search/' + encodeURIComponent(s.addr || s.name || '');
  const kakaoUrl = 'https://map.kakao.com/link/to/'+encodeURIComponent(s.addr||'')+','+s.lat+','+s.lng;
  const cardUrl = 'https://epic-riders.cnr-korea.com/epicrideclub/find/address/';
  const {prev, next} = legsAroundCard(idx);
  // Unreachable legs (backend marks mid_lat=null) get a distinct "도달불가"
  // label so they don't look like a near-zero duration. Reachable legs use
  // fmtLegTime — including short ones that round down to "0분".
  const legRow = leg => el('div', {class:'leg-row'}, [
    el('span', {class:'leg-lbl'}, '⏱️ ' + cardLabel(SPOTS[leg.from_idx].card)
                                  + ' → ' + cardLabel(SPOTS[leg.to_idx].card)),
    el('span', {class:'leg-val'}, leg.mid_lat == null ? '도달불가' : fmtLegTime(leg.duration_s)),
  ]);
  const legRows = [];
  if (prev) legRows.push(legRow(prev));
  if (next) legRows.push(legRow(next));
  return el('div', {class:'popup-card'}, [
    el('h3', null, cardLabel(s.card) + ' ' + (s.name || '')),
    el('div', {class:'addr'}, s.addr || ''),
    el('div', {class:'coords'}, s.lat.toFixed(6) + ', ' + s.lng.toFixed(6)),
    legRows.length ? el('div', {class:'legs'}, legRows) : null,
    el('div', {class:'links'}, [
      el('a', {href: cardUrl, target: '_blank'}, '카드'),
      el('a', {
        class: 'naver',
        href: naverWebUrl,
        target: '_blank',
        'data-act': 'naver-open',
        'data-lat': String(s.lat),
        'data-lng': String(s.lng),
        'data-name': s.name || '',
      }, '네이버지도'),
      el('a', {class:'kakao', href: kakaoUrl, target: '_blank'}, '카카오맵'),
    ]),
    el('div', {class:'actions'}, [
      el('button', {class: isOwned ? 'own active' : 'own', 'data-act':'own', 'data-card': String(s.card)}, '🃏 보유'),
      el('button', {class: isVisited ? 'visit active' : 'visit', 'data-act':'visit', 'data-card': String(s.card)}, '📍 방문'),
    ]),
  ]);
}

function refreshMarker(idx){
  const m = markerByIdx.get(idx);
  if (!m) return;
  const route = ROUTES[currentRoute];
  const visitNum = route.order.indexOf(idx) + 1;
  m.setIcon(makeIcon(idx, visitNum > 0 ? visitNum : '·'));
  m.setPopupContent(makePopup(idx));
}

function refreshAllMarkers(){
  SPOTS.forEach((_, idx) => refreshMarker(idx));
  updateProgress();
}

function updateProgress(){
  document.getElementById('visitedCount').textContent = visited.size;
  document.getElementById('ownedCount').textContent = owned.size;
}

window.toggleOwned = function(card){
  if (owned.has(card)) owned.delete(card); else owned.add(card);
  saveOwned();
  SPOTS.forEach((s, i) => { if (s.card === card) refreshMarker(i); });
  updateProgress();
};
window.toggleVisited = function(card){
  if (visited.has(card)) visited.delete(card); else visited.add(card);
  saveVisited();
  SPOTS.forEach((s, i) => { if (s.card === card) refreshMarker(i); });
  updateProgress();
};
window.resetOwned = function(){
  if (!confirm('보유 카드를 기본값(' + OWNED_DEFAULT.length + '장)으로 복원할까?')) return;
  owned.clear(); OWNED_DEFAULT.forEach(c => owned.add(c)); saveOwned(); refreshAllMarkers();
};
window.resetVisited = function(){
  if (!confirm('방문 기록을 초기화할까?')) return;
  visited.clear(); saveVisited(); refreshAllMarkers();
};

function buildChipLayer(r){
  // One non-interactive divIcon marker per leg, anchored at the leg midpoint.
  // Legs with no midpoint (unreachable) or <30s duration are skipped.
  if (!r || !r.legs) return null;
  const group = L.featureGroup();
  r.legs.forEach(leg => {
    if (leg.mid_lat == null || leg.mid_lng == null) return;
    if (!leg.duration_s || leg.duration_s < 30) return;
    const chip = L.divIcon({
      className: '',
      html: el('div', {class:'leg-chip'}, fmtLegTime(leg.duration_s)),
      iconSize: null,
    });
    L.marker([leg.mid_lat, leg.mid_lng], {icon: chip, interactive: false, keyboard: false})
      .addTo(group);
  });
  return group;
}

function syncChipVisibility(){
  if (!chipLayer) return;
  const want = map.getZoom() >= CHIP_MIN_ZOOM;
  const has = map.hasLayer(chipLayer);
  if (want && !has) chipLayer.addTo(map);
  else if (!want && has) map.removeLayer(chipLayer);
}

function applyRoute(key){
  if (!ROUTES[key]) return;
  currentRoute = key;
  const r = ROUTES[key];
  if (polyLayer) { map.removeLayer(polyLayer); polyLayer = null; }
  if (vLayer)    { map.removeLayer(vLayer);    vLayer = null; }
  if (chipLayer) { map.removeLayer(chipLayer); chipLayer = null; }
  const pts = decodePolyline(r.polyline);
  const color = ROUTE_COLOR[key] || '#666';
  polyLayer = L.polyline(pts, {color, weight:5, opacity:.75}).addTo(map);
  chipLayer = buildChipLayer(r);
  syncChipVisibility();

  const vs = VIOLATIONS[key] || [];
  if (vs.length){
    const group = L.featureGroup();
    vs.forEach(v => {
      v.polylines.forEach(enc => {
        const pp = decodePolyline(enc);
        if (pp.length < 2) return;
        const line = L.polyline(pp, {color:'#e91e63', weight:6, opacity:.9});
        line.bindPopup(el('div', {class:'v-popup'}, [
          el('b', null, v.kind),
          document.createElement('br'),
          document.createTextNode(v.name),
          document.createElement('br'),
          document.createTextNode('length: ' + v.length_m.toFixed(0) + 'm'),
        ]));
        group.addLayer(line);
      });
    });
    vLayer = group.addTo(map);
  }

  document.getElementById('distVal').textContent = fmtKm(r.distance_m);
  document.getElementById('durVal').textContent = fmtTime(r.duration_s || (r.distance_m / (50 * 1000 / 3600)));

  document.querySelectorAll('#route-toggle button[data-route]').forEach(b => b.classList.toggle('active', b.dataset.route === key));

  const orderSet = new Set(r.order);
  SPOTS.forEach((s, idx) => {
    let m = markerByIdx.get(idx);
    if (!orderSet.has(idx)) {
      if (m) { map.removeLayer(m); markerByIdx.delete(idx); }
      return;
    }
    const visitNum = r.order.indexOf(idx) + 1;
    if (!m) {
      m = L.marker([s.lat, s.lng], {icon: makeIcon(idx, visitNum)}).bindPopup(makePopup(idx), {maxWidth:400, autoPan:true});
      m.addTo(map);
      markerByIdx.set(idx, m);
    } else {
      m.setIcon(makeIcon(idx, visitNum));
      m.setPopupContent(makePopup(idx));
    }
  });
  updateProgress();
}

document.addEventListener('click', e => {
  const btn = e.target.closest('[data-act]');
  if (!btn) return;
  const act = btn.dataset.act;
  if (act === 'own' || act === 'visit'){
    const card = parseInt(btn.dataset.card, 10);
    if (act === 'own') window.toggleOwned(card);
    else window.toggleVisited(card);
  } else if (act === 'naver-open'){
    // iOS / Android: intercept and route through nmap:// custom scheme
    // for app-side precise coords. Desktop falls through to the default
    // <a href> (modern web URL) so map.naver.com opens in a new tab.
    if (/iPhone|iPad|iPod|Android/i.test(navigator.userAgent)){
      e.preventDefault();
      const lat = btn.dataset.lat;
      const lng = btn.dataset.lng;
      const name = encodeURIComponent(btn.dataset.name || '');
      location.href = 'nmap://place?lat=' + lat + '&lng=' + lng + '&name=' + name + '&appname=xoonjaeho.github.io';
    }
  }
});

document.getElementById('route-toggle').addEventListener('click', e => {
  const b = e.target.closest('button[data-route]');
  if (b) applyRoute(b.dataset.route);
});

function repositionToggle(){
  const stats = document.getElementById('stats');
  const toggle = document.getElementById('route-toggle');
  toggle.style.top = (stats.offsetTop + stats.offsetHeight + 8) + 'px';
}
new ResizeObserver(repositionToggle).observe(document.getElementById('stats'));
window.addEventListener('load', repositionToggle);
map.on('zoomend', syncChipVisibility);

window.addEventListener('storage', e => {
  if (e.key === STORAGE_KEY_VISITED) {
    visited.clear();
    JSON.parse(e.newValue || '[]').forEach(c => visited.add(c));
    refreshAllMarkers();
  } else if (e.key === STORAGE_KEY_OWNED) {
    owned.clear();
    JSON.parse(e.newValue || '[]').forEach(c => owned.add(c));
    refreshAllMarkers();
  }
});

applyRoute(currentRoute);
</script>
</body>
</html>
'''


def _js_safe(obj) -> str:
    """JSON encode for embedding inside an inline <script> block.

    Escapes every HTML-significant byte (< > &) and the two JS line terminators
    (U+2028, U+2029) as \\uXXXX sequences. The resulting JSON literal is valid
    JS and produces the same Python value at runtime, but contains no character
    the HTML parser can act on — no </script> breakout, no <!-- ... <script>
    comment-state interaction, no premature termination of a string literal.
    """
    s = json.dumps(obj, ensure_ascii=False)
    s = s.replace('<', r'\u003c')
    s = s.replace('>', r'\u003e')
    s = s.replace('&', r'\u0026')
    s = s.replace(' ', r'\u2028')
    s = s.replace(' ', r'\u2029')
    return s


def build_html(spots: list, routes: dict, verify_report: dict,
               owned_default: list, out_path: Path,
               total_cards: Optional[int] = None) -> Path:
    """Render the HTML deliverable.

    spots:          list of {card, name, addr?, lat, lng}
    routes:         {loop?,traverse?,personal?: {order, distance_m, duration_s, polyline, ...}}
    verify_report:  {routes: {name: {violation_ways: [...]}}}
    owned_default:  list[int] card numbers
    out_path:       output HTML file path
    total_cards:    label denominator (defaults to len(spots))
    """
    # consolidate violations per route, excluding filtered false positives
    violations = {}
    for name in routes.keys():
        rep = verify_report.get('routes', {}).get(name, {})
        ways = rep.get('violation_ways', [])
        kept = [v for v in ways if v.get('polylines') and not v.get('filtered')]
        violations[name] = [
            {
                'kind': v['kind'],
                'name': v['name'],
                'length_m': v['length_m'],
                'polylines': v.get('polylines', []),
            }
            for v in kept
        ]
        filtered_count = sum(1 for v in ways if v.get('filtered'))
        print(f'  {name}: {len(violations[name])} kept overlays ({filtered_count} filtered)')

    denom = total_cards if total_cards is not None else len(spots)
    body = (HTML
            .replace('__TOTAL_CARDS__', str(denom))
            .replace('__SPOTS__', _js_safe(spots))
            .replace('__ROUTES__', _js_safe(routes))
            .replace('__VIOLATIONS__', _js_safe(violations))
            .replace('__OWNED_DEFAULT__', _js_safe(sorted(set(owned_default)))))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding='utf-8')
    print(f'\nhtml -> {out_path} ({out_path.stat().st_size/1024:.0f} KB)')
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--verify', default=str(DEFAULT_VERIFY_JSON))
    ap.add_argument('--out', default=str(DEFAULT_OUT_HTML))
    ap.add_argument('--total-cards', type=int, default=None,
                    help='label denominator (default: spot count)')
    args = ap.parse_args()
    spots = json.loads(SPOTS_JSON.read_text(encoding='utf-8'))
    routes = json.loads(ROUTES_JSON.read_text(encoding='utf-8'))
    verify_report = json.loads(Path(args.verify).read_text(encoding='utf-8'))
    owned = json.loads(OWNED_JSON.read_text(encoding='utf-8'))
    build_html(spots, routes, verify_report, owned, Path(args.out), total_cards=args.total_cards)


if __name__ == '__main__':
    main()
