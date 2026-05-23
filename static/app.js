// EPIC RIDERS CLUB 2026 — Map Builder frontend
'use strict';

const STATE = {
  spots: [],
  baseSpots: [],
  epic: [],
  ownedDefault: [],
  ownedSet: new Set(),
  total: 237,
  map: null,
  markers: new Map(),
  start: {loop: null, traverse: null, personal: null},
  end:   {loop: null, traverse: null, personal: null},
};

const STORAGE_OWNED = 'erc-2026-owned';
const STORAGE_EPIC_LOCAL = 'erc-2026-epic-local';
const STORAGE_ENDPOINTS = 'erc-2026-endpoints';

const $ = (s, p = document) => p.querySelector(s);
const $$ = (s, p = document) => Array.from(p.querySelectorAll(s));
const pad3 = (n) => String(n).padStart(3, '0');
// EPIC cards (221+) render as "EPIC 001-017"; others as "#001-220"
const isEpicCard = (c) => c >= 221;
const cardLabel = (c) => isEpicCard(c) ? ('EPIC ' + pad3(c - 220)) : ('#' + pad3(c));

// --- safe DOM helpers (no innerHTML on user data) ---
function el(tag, props = {}, children = []){
  const e = document.createElement(tag);
  for (const k in props){
    const v = props[k];
    if (k === 'class') e.className = v;
    else if (k === 'dataset') Object.assign(e.dataset, v);
    else if (k === 'text') e.textContent = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2).toLowerCase(), v);
    else e.setAttribute(k, v);
  }
  for (const c of (Array.isArray(children) ? children : [children])){
    if (c == null) continue;
    e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return e;
}

// ---------- init ----------
window.addEventListener('DOMContentLoaded', async () => {
  bindTabs();
  bindParams();
  bindBuild();
  bindTooltips();
  await loadCards();
  loadEndpoints();
  renderCardList();
  initMap();
  updateOwnedCount();
  updateEndpointLabels();
  await refreshCacheStatus();
});

// ---------- tooltips (click to toggle) ----------
function bindTooltips(){
  document.body.addEventListener('click', (e) => {
    const help = e.target.closest('.help');
    document.querySelectorAll('.tooltip').forEach(t => t.remove());
    if (!help) return;
    e.stopPropagation();
    const tip = help.dataset.tip || '';
    if (!tip) return;
    const tt = el('div', {class: 'tooltip', text: tip});
    document.body.appendChild(tt);
    const rect = help.getBoundingClientRect();
    const ttRect = tt.getBoundingClientRect();
    let top = rect.bottom + 8;
    let left = rect.left;
    if (left + ttRect.width > window.innerWidth - 8){
      left = Math.max(8, window.innerWidth - ttRect.width - 8);
    }
    if (top + ttRect.height > window.innerHeight - 8){
      top = rect.top - ttRect.height - 8;
    }
    tt.style.top = top + 'px';
    tt.style.left = left + 'px';
    // dismiss on Escape
    const onEsc = (ev) => { if (ev.key === 'Escape'){ tt.remove(); document.removeEventListener('keydown', onEsc); } };
    document.addEventListener('keydown', onEsc);
  });
}

// ---------- tabs (mobile) ----------
function bindTabs(){
  $$('.tabs .tab').forEach(b => b.addEventListener('click', () => {
    $$('.tabs .tab').forEach(x => x.classList.toggle('active', x === b));
    const target = b.dataset.pane;
    $$('.pane').forEach(p => p.classList.toggle('active', p.id === target));
    if (target === 'pane-map' && STATE.map) {
      setTimeout(() => STATE.map.invalidateSize(), 50);
    }
  }));
}

// ---------- card data ----------
async function loadCards(){
  const res = await fetch('/api/cards');
  if (!res.ok) {
    alert('카드 데이터 로드 실패: ' + res.status);
    return;
  }
  const d = await res.json();
  STATE.baseSpots = d.spots || [];
  STATE.epic = d.epic || [];
  STATE.ownedDefault = d.owned_default || [];
  STATE.total = d.total || 237;
  try {
    const local = JSON.parse(localStorage.getItem(STORAGE_EPIC_LOCAL) || 'null');
    if (Array.isArray(local)) {
      const byCard = new Map(local.map(e => [e.card, e]));
      STATE.epic = STATE.epic.map(e => byCard.has(e.card) ? {...e, ...byCard.get(e.card)} : e);
    }
  } catch {}
  STATE.spots = STATE.baseSpots.concat(STATE.epic);
  $('#card-total').textContent = STATE.total;
  $('#card-total-2').textContent = STATE.total;
  const ownedStored = localStorage.getItem(STORAGE_OWNED);
  const initial = ownedStored ? JSON.parse(ownedStored) : STATE.ownedDefault;
  STATE.ownedSet = new Set(initial);
}

function saveOwned(){ localStorage.setItem(STORAGE_OWNED, JSON.stringify([...STATE.ownedSet])); }
function updateOwnedCount(){ $('#owned-count').textContent = STATE.ownedSet.size; }

// ---------- card list render (DOM API, no innerHTML on user data) ----------
function renderCardList(filterText = ''){
  const root = $('#card-list');
  root.replaceChildren();
  const f = (filterText || '').toLowerCase().trim();
  const matches = (s) => {
    if (!f) return true;
    const label = cardLabel(s.card).toLowerCase();
    return label.includes(f)
        || pad3(s.card).includes(f)
        || (s.name || '').toLowerCase().includes(f);
  };
  STATE.baseSpots.forEach((s) => { if (matches(s)) root.appendChild(renderRow(s, false)); });
  STATE.epic.forEach((s) => { if (matches(s)) root.appendChild(renderRow(s, true)); });
}

function renderRow(s, isEpic){
  const id = 'cb-' + s.card;
  const cb = el('input', {type:'checkbox', id, 'data-card': String(s.card)});
  cb.checked = STATE.ownedSet.has(s.card);
  cb.addEventListener('change', () => {
    if (cb.checked) STATE.ownedSet.add(s.card); else STATE.ownedSet.delete(s.card);
    saveOwned();
    updateOwnedCount();
    refreshMarkerForCard(s.card);
  });
  const lbl = el('label', {for: id}, [
    el('span', {class: isEpic ? 'num epic' : 'num', text: cardLabel(s.card)}),
    el('span', {class:'nm', text: isEpic ? '' : (s.name || '')}),
  ]);
  const row = el('div', {class: 'card-row' + (isEpic ? ' epic-row' : '')}, [cb, lbl]);
  if (isEpic){
    const latInp = el('input', {type:'number', step:'any', placeholder:'위도',
                                'data-coord':'lat', 'data-card': String(s.card)});
    latInp.value = (s.lat != null) ? s.lat : '';
    const lngInp = el('input', {type:'number', step:'any', placeholder:'경도',
                                'data-coord':'lng', 'data-card': String(s.card)});
    lngInp.value = (s.lng != null) ? s.lng : '';
    [latInp, lngInp].forEach(inp => {
      inp.addEventListener('change', onEpicCoordChange);
      inp.addEventListener('blur', onEpicCoordChange);
    });
    row.appendChild(el('div', {class:'coords'}, [latInp, lngInp]));
  }
  return row;
}

async function onEpicCoordChange(e){
  const card = parseInt(e.target.dataset.card, 10);
  const ent = STATE.epic.find(x => x.card === card);
  if (!ent) return;
  const v = e.target.value === '' ? null : parseFloat(e.target.value);
  ent[e.target.dataset.coord] = (v == null || Number.isNaN(v)) ? null : v;
  STATE.spots = STATE.baseSpots.concat(STATE.epic);
  localStorage.setItem(STORAGE_EPIC_LOCAL, JSON.stringify(STATE.epic));
  refreshMarkerForCard(card);
  try {
    await fetch('/api/epic', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(STATE.epic),
    });
  } catch (err) {
    console.warn('EPIC server save failed', err);
  }
}

$('#card-search').addEventListener('input', (e) => renderCardList(e.target.value));
$('#btn-reset-owned').addEventListener('click', () => {
  if (!confirm(`보유 카드를 기본값(${STATE.ownedDefault.length}장)으로 복원할까?`)) return;
  STATE.ownedSet = new Set(STATE.ownedDefault);
  saveOwned();
  updateOwnedCount();
  renderCardList($('#card-search').value);
  refreshAllMarkers();
});

// ---------- map (Leaflet) ----------
function initMap(){
  const map = L.map('map').setView([37.8, 128.3], 8);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 18, attribution: '© OpenStreetMap'}).addTo(map);
  STATE.map = map;
  refreshAllMarkers();
  setTimeout(() => map.invalidateSize(), 100);
}

function makeIconHtml(card){
  const isOwned = STATE.ownedSet.has(card);
  const targetMode = $('#se-target-mode').value;
  const isStart = STATE.start[targetMode] === card;
  const isEnd = STATE.end[targetMode] === card;
  let cls = 'marker-label';
  if (isStart) cls += ' start';
  else if (isEnd) cls += ' end';
  else if (isOwned) cls += ' owned';
  const prefix = isStart ? '⭐ ' : isEnd ? '🏁 ' : '';
  // Built from primitives only (card number int + class string with no user input)
  const wrap = el('div', {class: cls, text: prefix + cardLabel(card)});
  return wrap.outerHTML;
}

function refreshMarkerForCard(card){
  const m = STATE.markers.get(card);
  const s = STATE.spots.find(x => x.card === card);
  if (!s || s.lat == null || s.lng == null){
    if (m) { STATE.map.removeLayer(m); STATE.markers.delete(card); }
    return;
  }
  const icon = L.divIcon({className: '', html: makeIconHtml(card), iconSize: null});
  if (m){
    m.setLatLng([s.lat, s.lng]);
    m.setIcon(icon);
  } else {
    const newM = L.marker([s.lat, s.lng], {icon});
    const tipDiv = el('div', {}, [
      el('strong', {text: cardLabel(s.card)}),
      ' ',
      el('span', {text: s.name || ''}),
    ]);
    newM.bindTooltip(tipDiv, {direction: 'top'});
    newM.on('click', () => onMarkerClick(s.card));
    newM.addTo(STATE.map);
    STATE.markers.set(card, newM);
  }
}

function refreshAllMarkers(){
  STATE.spots.forEach(s => refreshMarkerForCard(s.card));
}

function onMarkerClick(card){
  const mode = $('#se-target-mode').value;
  const isStart = STATE.start[mode] === card;
  const isEnd = STATE.end[mode] === card;
  if (!isStart && !isEnd){
    STATE.start[mode] = card;
  } else if (isStart){
    STATE.start[mode] = null;
    if (mode !== 'loop') STATE.end[mode] = card;
  } else if (isEnd){
    STATE.end[mode] = null;
  }
  saveEndpoints();
  updateEndpointLabels();
  refreshAllMarkers();
}

function saveEndpoints(){
  localStorage.setItem(STORAGE_ENDPOINTS, JSON.stringify({start: STATE.start, end: STATE.end}));
}
function loadEndpoints(){
  try {
    const d = JSON.parse(localStorage.getItem(STORAGE_ENDPOINTS) || 'null');
    if (d && d.start && d.end){
      STATE.start = d.start;
      STATE.end = d.end;
    }
  } catch {}
}

function updateEndpointLabels(){
  const mode = $('#se-target-mode').value;
  const s = STATE.start[mode];
  const e = STATE.end[mode];
  $('#lbl-start').textContent = s != null ? cardLabel(s) : '자동 (서울 최근접)';
  if (mode === 'loop'){
    $('#lbl-end').textContent = s != null ? (cardLabel(s) + ' (= 시작)') : '시작과 동일';
  } else {
    $('#lbl-end').textContent = e != null ? cardLabel(e) : '자유 (모든 카드 방문)';
  }
}

$('#se-target-mode').addEventListener('change', () => {
  updateEndpointLabels();
  refreshAllMarkers();
});

// ---------- params ----------
function bindParams(){
  $('#hd-advanced').addEventListener('click', (e) => {
    // ignore clicks on the help icon (let body handler open tooltip)
    if (e.target.closest('.help')) return;
    const p = $('#advanced-pane');
    p.classList.toggle('hidden');
    $('#hd-advanced').classList.toggle('open', !p.classList.contains('hidden'));
  });
  $('#btn-rebuild-graph').addEventListener('click', async () => {
    if (!confirm('그래프/매트릭스 캐시를 삭제할까? 다음 생성 시 ~10-20분 소요.')) return;
    const r = await fetch('/api/rebuild-graph', {method: 'POST'});
    if (r.ok){ alert('캐시 삭제 완료'); await refreshCacheStatus(); }
    else alert('실패: ' + r.status);
  });
}

function collectParams(){
  const enabled_modes = $$('input[data-mode]').filter(i => i.checked).map(i => i.dataset.mode);
  const metaheuristics = $$('input[data-meta]').filter(i => i.checked).map(i => i.dataset.meta);
  const verifyMode = ($$('input[name="verify-mode"]').find(i => i.checked) || {}).value || 'intersect';
  return {
    enabled_modes,
    tsp_time_loop: +$('#p-tsp-loop').value,
    tsp_time_traverse: +$('#p-tsp-traverse').value,
    tsp_time_personal: +$('#p-tsp-personal').value,
    loop_start: STATE.start.loop,
    traverse_start: STATE.start.traverse,
    traverse_end: STATE.end.traverse,
    personal_start: STATE.start.personal,
    personal_end: STATE.end.personal,
    verify_mode: verifyMode,
    verify_buffer_m: +$('#p-verify-buffer').value,
    verify_bearing_threshold_deg: +$('#p-bearing-thr').value,
    verify_grade_separation: $('#p-grade-sep').checked,
    verify_sample_interval_m: +$('#p-sample-interval').value,
    verify_violation_radius_m: +$('#p-violation-radius').value,
    metaheuristics,
    force_rebuild_graph: $('#p-force-graph').checked,
    force_rebuild_matrix: $('#p-force-matrix').checked,
  };
}

// ---------- build ----------
function bindBuild(){
  $('#btn-build').addEventListener('click', triggerBuild);
  $('#btn-dlg-close').addEventListener('click', () => $('#dlg-build').close());
}

async function triggerBuild(){
  const buildBtn = $('#btn-build');
  if (buildBtn.disabled) return;
  const params = collectParams();
  if (!params.enabled_modes.length){
    alert('하나 이상의 라우트 모드를 선택해주세요 (🔁 loop / ↔️ traverse / 👤 personal).');
    return;
  }
  if (!params.metaheuristics.length){
    alert('하나 이상의 메타휴리스틱을 선택해주세요.');
    return;
  }
  const body = {
    owned_cards: [...STATE.ownedSet],
    epic_extras: STATE.epic,
    params,
    out_name: 'EPIC RIDERS CLUB 2026.html',
  };
  const dlg = $('#dlg-build');
  const log = $('#event-log');
  const progress = $('#progress-line');
  const dl = $('#dl-link');
  const closeBtn = $('#btn-dlg-close');
  log.textContent = '';
  progress.style.setProperty('--w', '0%');
  dl.classList.add('hidden');
  closeBtn.disabled = true;
  buildBtn.disabled = true;
  if (!dlg.open) dlg.showModal();

  const res = await fetch('/api/build', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  if (!res.ok){
    const errText = await res.text();
    log.textContent += `[error] ${res.status}: ${errText}\n`;
    closeBtn.disabled = false;
    buildBtn.disabled = false;
    return;
  }
  const {job_id} = await res.json();
  log.textContent += `job: ${job_id}\n`;
  attachEventSource(job_id, {log, progress, dl, closeBtn, buildBtn});
}

function attachEventSource(jid, ui){
  const es = new EventSource(`/api/job/${jid}/events`);
  const stageMap = {graph: 10, snap: 15, matrix: 35, tsp: 65, poly: 80, duration: 90, verify: 95, html: 98, done: 100};
  es.addEventListener('progress', (e) => {
    const ev = JSON.parse(e.data);
    const line = `[${ev.stage}] ${ev.pct != null ? (ev.pct*100).toFixed(0)+'%' : ''} ${ev.msg || ''}`;
    ui.log.textContent += line + '\n';
    ui.log.scrollTop = ui.log.scrollHeight;
    const base = stageMap[ev.stage] ?? 0;
    if (typeof ev.pct === 'number'){
      ui.progress.style.setProperty('--w', `${Math.min(99, base * (1 - 0.5*(1-ev.pct)))}%`);
    } else {
      ui.progress.style.setProperty('--w', `${base}%`);
    }
  });
  es.addEventListener('done', (e) => {
    const result = JSON.parse(e.data);
    ui.log.textContent += `\n=== DONE in ${result.elapsed_s.toFixed(1)}s ===\n`;
    Object.entries(result.routes_summary || {}).forEach(([k,v]) => {
      ui.log.textContent += `  ${k}: ${(v.distance_m/1000).toFixed(1)} km  ${(v.duration_s/3600).toFixed(1)}h  meta=${v.meta}\n`;
    });
    Object.entries(result.verify_summary || {}).forEach(([k,v]) => {
      ui.log.textContent += `  verify ${k}: ${v.violation_km_filtered.toFixed(3)} km / ${v.violation_count_filtered} ways\n`;
    });
    const diag = result.diagnostics || {};
    if (diag.unreachable_count > 0){
      ui.log.textContent += `\n⚠️ 도달 불가 카드 쌍 ${diag.unreachable_count}개 — 일부 leg에 1,000,000km 스텁이 포함될 수 있음\n`;
      if (Array.isArray(diag.unreachable_sample) && diag.unreachable_sample.length){
        const ex = diag.unreachable_sample.slice(0, 5)
          .map(p => `#${String(p[0]).padStart(3,'0')}↛#${String(p[1]).padStart(3,'0')}`).join(', ');
        ui.log.textContent += `  예시: ${ex}\n`;
      }
    }
    ui.progress.style.setProperty('--w', '100%');
    ui.dl.href = `/api/download/${jid}`;
    ui.dl.classList.remove('hidden');
    ui.closeBtn.disabled = false;
    ui.buildBtn.disabled = false;
    es.close();
    refreshCacheStatus();
    $('#status-result').textContent = `생성 완료: ${result.out_path}`;
  });
  es.addEventListener('error', (e) => {
    let errMsg = 'connection error';
    if (e.data){
      try { errMsg = JSON.parse(e.data).err || e.data; } catch { errMsg = e.data; }
    }
    ui.log.textContent += `\n[ERROR] ${errMsg}\n`;
    ui.closeBtn.disabled = false;
    ui.buildBtn.disabled = false;
    es.close();
  });
}

// ---------- status ----------
async function refreshCacheStatus(){
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    const c = d.cache || {};
    const items = [
      `그래프 ${c.graph ? '✅' : '⬜'}`,
      `매트릭스 ${c.matrix ? '✅' : '⬜'}`,
      `다익스트라 ${c.dijkstra ? '✅' : '⬜'}`,
      `루트 ${c.routes ? '✅' : '⬜'}`,
    ];
    $('#status-cache').textContent = '캐시: ' + items.join(' · ');
  } catch (e) {
    $('#status-cache').textContent = '캐시: (확인 실패)';
  }
}
