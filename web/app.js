/* Urban Green — single-page MapLibre client for the FastAPI backend under /api.
   No framework, no build step. Sections: constants & state, HTTP, formatting,
   DOM helpers, status strip, map (layers, scale, popups, drawing), streets,
   plans & scenarios, year dock & shade, rules, agent chat (SSE), boot. */
'use strict';

(() => {
  // ------------------------------------------------------------ constants
  const YEARS = [0, 5, 10, 20, 30];
  const MAX_YEAR = 30;
  const EARTH_RADIUS_M = 6378137;
  const TILE_PX = 512;                 // MapLibre world size at zoom 0
  const MAX_EXPR_ZOOM = 22;
  const ANIM_MS = 4000;
  const SHADE_DEBOUNCE_MS = 250;
  const SHADE_WHEN = { month: 7, day: 15, hour: 15 };
  const LAYER_KEYS = ['axis', 'carriageway', 'sidewalks', 'plantable', 'buildings', 'cycleways',
    'parking', 'junctions', 'existing_trees', 'corridor'];
  const LAYER_GROUPS = {
    corridor: ['corridor-line', 'axis-line'],
    axis: ['axis-line'],
    carriageway: ['carriageway-fill', 'carriageway-line'],
    sidewalks: ['sidewalks-fill'],
    plantable: ['plantable-fill', 'plantable-line'],
    buildings: ['buildings-fill', 'buildings-line'],
    cycleways: ['cycleways-line'],
    parking: ['parking-fill', 'parking-line'],
    junctions: ['junctions-circle'],
    existing_trees: ['existing-crowns', 'existing-marks'],
    crowns: ['crowns'],
    sites: ['sites-ring', 'sites'],
    shade: ['shade-fill'],
  };
  const VERDICTS = {
    valid: { label: 'No rule conflicts found', explanation: 'None of the evaluated rules failed. This is a planning check, not planting approval.' },
    conditional: { label: 'Recommendation not met', explanation: 'At least one recommendation is not met. No required rule failed. This position is included in the proposed plan, but the conflicts below need assessment.' },
    invalid: { label: 'Required rule not met', explanation: 'At least one required rule failed. This position is excluded from the proposed trees and their projected canopy.' },
  };
  const REDUCED_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)');
  const emptyFc = () => ({ type: 'FeatureCollection', features: [] });

  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const C = {
    ground: cssVar('--ground'), ink: cssVar('--ink'), muted: cssVar('--muted'), accent: cssVar('--accent'),
    crown: cssVar('--crown'), valid: cssVar('--valid'), cond: cssVar('--cond'), invalid: cssVar('--invalid'),
    shade: cssVar('--shade'), cycle: cssVar('--cycle'), existing: cssVar('--existing'),
    coolingLow: cssVar('--cooling-low'), coolingMid: cssVar('--cooling-mid'), coolingHigh: cssVar('--cooling-high'),
  };

  /** "#RRGGBB" + alpha -> "rgba(r,g,b,a)". */
  function alpha(hex, a) {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }

  // ------------------------------------------------------------ state
  const state = {
    config: null,
    loadingStreet: false,
    planning: false,
    updatingRules: false,
    dirty: false,
    cityId: null,
    basemapId: null,
    street: null,                 // StreetResponse currently on the map
    streets: new Map(),           // street_id -> StreetResponse
    scenarios: [],                // ScenarioResponse, insertion order
    activeScenarioId: null,
    siteIndex: new Map(),         // site_id -> decorated Feature of the active scenario
    pack: null,                   // active RulePack (with session overrides)
    year: MAX_YEAR,
    anim: 0,                      // requestAnimationFrame handle of the growth animation
    ppm0: 0,                      // pixels per metre at zoom 0 for the current centre latitude
    scaleRaf: 0,
    shade: { enabled: false, result: null, timer: 0, seq: 0, when: { ...SHADE_WHEN } },
    draw: { active: false, points: [] },
    pickingStreet: false,
    agent: { streaming: false },
    temperature: { seq: 0, pending: false, result: null, selectedId: '', mapVisible: false, savedLayers: [] },
    autoApply: { timer: 0, revision: 0, pending: false, running: false, failed: false, rules: {}, cooling: null },
    rulesEditorPinned: false,
    status: { sources: [], estimated: [], warnings: [], notes: [], error: null },
    hoverPopup: null,
    pinPopup: null,
  };

  const $ = (id) => document.getElementById(id);
  const activeScenario = () => state.scenarios.find((s) => s.scenario_id === state.activeScenarioId) || null;
  const streetScenarios = () => state.scenarios.filter((s) => s.street_id === state.street?.street_id);
  const ui = {};

  /** Cache every element the app touches, by id. */
  function collectUi() {
    const ids = ['map', 'header', 'city', 'search-form', 'search', 'search-btn', 'draw', 'demo-chips', 'street-meta',
      'status', 'col-left', 'plan-form', 'plan-meta', 'spacing', 'spacing-out', 'species', 'species-hint', 'label',
      'plan-btn', 'rules-card', 'rules-meta', 'rules-source', 'rules', 'rules-standard', 'rules-defaults', 'rules-summary',
      'rules-reset', 'rules-pending', 'restriction-preview', 'locked-rules', 'goto-trees', 'edit-restrictions',
      'scenarios', 'scenarios-meta', 'compare-btn',
      'export-link', 'basemaps', 'play', 'year', 'readout', 'shade', 'shade-info', 'agent', 'agent-meta', 'messages',
      'agent-offline', 'prompt-chips', 'chat-form', 'chat-input', 'send', 'agent-foot', 'compare-modal',
      'compare-close', 'compare-note', 'compare-table', 'workflow-status', 'data-status', 'data-summary',
      'plan-hint', 'result-empty', 'result-content', 'result-name', 'result-trees', 'result-cover', 'result-cover-was',
      'result-sidewalk', 'result-reading', 'result-vs', 'result-growth', 'result-guidance', 'flag-summary',
      'try-alternatives', 'plan-alternatives', 'copy-brief', 'compare-here',
      'compare-insight', 'view-plan', 'fit-street', 'map-hint', 'assistant-toggle', 'assistant-close'];
    for (const id of ids) ui[id.replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = $(id);
    ui.dock = document.querySelector('.dock');
    ui.colRight = document.querySelector('.col-right');
    ui.legendToggles = Array.from(document.querySelectorAll('.legend-item input[data-layer]'));
  }

  const motionMs = (ms) => (REDUCED_MOTION.matches ? 0 : ms);

  // ------------------------------------------------------------ HTTP
  let cachedSid = null;

  /** Client session id, persisted in localStorage when available. */
  function sessionId() {
    if (cachedSid) return cachedSid;
    let sid = null;
    try { sid = localStorage.getItem('canopy_sid'); } catch (_) { /* storage blocked */ }
    if (!sid) {
      sid = uuid();
      try { localStorage.setItem('canopy_sid', sid); } catch (_) { /* storage blocked */ }
    }
    cachedSid = sid;
    return sid;
  }

  /** RFC 4122 v4 UUID; crypto.randomUUID needs a secure context, so fall back to getRandomValues. */
  function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const h = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  }

  function requestHeaders(hasBody) {
    const headers = { 'X-Session-Id': sessionId(), Accept: 'application/json, text/event-stream' };
    if (hasBody) headers['Content-Type'] = 'application/json';
    return headers;
  }

  /** Error text from a failed response: {"error":{"message"}} first, then FastAPI's detail, then the status. */
  async function errorMessage(res) {
    let msg = `HTTP ${res.status}${res.statusText ? ` ${res.statusText}` : ''}`;
    try {
      const data = await res.json();
      if (data && data.error && data.error.message) msg = data.error.message;
      else if (data && data.detail) msg = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
    } catch (_) { /* body was not JSON */ }
    return msg;
  }

  /** JSON request to the backend; throws Error(message) on network failure or non-2xx. */
  async function api(path, { method = 'GET', body } = {}) {
    let res;
    try {
      res = await fetch(path, {
        method,
        headers: requestHeaders(body !== undefined),
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (err) {
      throw new Error(`Network error: ${err.message}`);
    }
    if (!res.ok) throw new Error(await errorMessage(res));
    return res.json();
  }

  // ------------------------------------------------------------ formatting
  const fmt = {
    /** 240 -> "0+240", 1240 -> "1+240" (km + metres, station style). */
    station(m) {
      let km = Math.floor(m / 1000);
      let rest = Math.round(m - km * 1000);
      if (rest === 1000) { km += 1; rest = 0; }
      return `${km}+${String(rest).padStart(3, '0')}`;
    },
    num(v, d = 1) { return v == null || Number.isNaN(Number(v)) ? '–' : Number(v).toFixed(d); },
    m(v, d = 2) { return v == null ? '–' : `${fmt.num(v, d)} m`; },
    pct(v, d = 1) { return v == null ? '–' : `${fmt.num(v, d)} %`; },
    int(v) { return v == null ? '–' : String(Math.round(Number(v))); },
    /** Thousands with a thin space: 1312 -> "1 312". */
    grouped(v) { return v == null ? '–' : Math.round(Number(v)).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' '); },
  };

  /** "Abschnitt 5.2" -> "§ 5.2"; other refs pass through with a § prefix when missing. */
  function shortRef(ref) {
    if (!ref) return '';
    const s = String(ref).replace(/^(Abschnitt|Section|Kapitel|Chapter)\s*/i, '');
    return s.startsWith('§') ? s : `§ ${s}`;
  }

  /** "2024-09" -> "09/2024". */
  function sourceDate(d) {
    const m = /^(\d{4})-(\d{2})/.exec(d || '');
    return m ? `${m[2]}/${m[1]}` : (d || '');
  }

  /** Citation shown next to a rule: "§ 5.2 Berlin 09/2024" or "planning default". */
  function ruleCitation(rule, pack) {
    if (rule.source_ref) return [shortRef(rule.source_ref), pack.jurisdiction, sourceDate(pack.source && pack.source.date)].filter(Boolean).join(' ');
    return rule.assumption ? 'planning default' : '';
  }

  // ------------------------------------------------------------ DOM helpers
  /** Tiny element builder; text goes through textContent, never innerHTML. */
  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? '' : String(v));
    }
    for (const c of [].concat(children)) if (c != null) node.append(c);
    return node;
  }

  const strong = (text) => el('b', { text });

  function setBusy(button, busy) {
    button.classList.toggle('busy', busy);
    button.disabled = busy;
  }

  // ------------------------------------------------------------ status strip
  function renderStatus() {
    const s = state.status;
    const box = ui.status;
    box.replaceChildren();
    if (s.error) {
      box.append(el('span', { class: 'pill pill-error' }, [
        el('span', { text: s.error }),
        el('button', { class: 'pill-x', type: 'button', 'aria-label': 'Dismiss error', text: '×', onclick: clearError }),
      ]));
    }
    for (const n of s.notes) box.append(el('span', { class: 'pill pill-note', text: n }));
    const data = ui.dataStatus;
    data.replaceChildren();
    for (const w of s.warnings) data.append(el('span', { class: 'pill pill-warn', text: w }));
    if (s.sources.length) {
      data.append(el('span', { class: 'sources' }, [strong('Sources '), document.createTextNode(s.sources.join(' · '))]));
    }
    if (s.estimated.length) data.append(el('span', { class: 'sources', text: `estimated: ${s.estimated.join(', ')}` }));
    ui.dataSummary.textContent = s.warnings.length || s.estimated.length ? 'Includes estimated data · view details' : 'Data sources and limitations';
    box.hidden = !box.childElementCount;
    if (s.error) ui.workflowStatus.textContent = 'Could not complete this step. Try again or choose another example street.';
  }

  function setError(message) {
    state.status.error = String(message);
    if (message.startsWith('Street:')) $('street-picker').open = true;
    renderStatus();
    ui.status.scrollIntoView({ behavior: REDUCED_MOTION.matches ? 'instant' : 'smooth', block: 'nearest' });
  }

  function clearError() {
    if (!state.status.error) return;
    state.status.error = null;
    renderStatus();
    renderWorkflow();
  }

  function addNote(message) {
    state.status.notes.push(message);
    renderStatus();
  }

  /** Attributions, basis and warnings of the street currently on the map. */
  function statusFromStreet(street) {
    const sources = [...new Set(Object.values(street.layer_sources || {}).filter(Boolean))];
    const estimated = Object.entries(street.layer_basis || {})
      .filter(([, basis]) => basis === 'estimated').map(([layer]) => layer);
    Object.assign(state.status, { sources, estimated, warnings: [...(street.warnings || [])], notes: [] });
    renderStatus();
  }

  // ------------------------------------------------------------ map
  const map = new maplibregl.Map({
    container: 'map',
    style: { version: 8, sources: {}, layers: [] },
    center: [8.5417, 47.3769],
    zoom: 12,
    maxZoom: 21,
    attributionControl: { compact: false },
  });
  map.addControl(new maplibregl.ScaleControl({ unit: 'metric', maxWidth: 120 }), 'bottom-left');
  const mapReady = new Promise((resolve) => map.on('load', resolve));

  /** Pixels per metre at zoom 0 for a latitude (512 px world at zoom 0; scale by 2^zoom). */
  function ppmAtZoom0(lat) {
    return TILE_PX / (2 * Math.PI * EARTH_RADIUS_M * Math.cos((lat * Math.PI) / 180));
  }

  /** Radius expression in pixels for a per-feature radius in metres.
      Exponential-base-2 zoom interpolation between 0 and 22 is exact (r * ppm0 * 2^zoom). */
  function radiusExpr(metres) {
    const p0 = state.ppm0 || ppmAtZoom0(map.getCenter().lat);
    return ['interpolate', ['exponential', 2], ['zoom'],
      0, ['*', metres, p0],
      MAX_EXPR_ZOOM, ['*', metres, p0 * 2 ** MAX_EXPR_ZOOM]];
  }

  const prop = (key) => ['to-number', ['get', key]];

  /** Crown radius (m) of a site at a fractional year: linear between the YEARS keys. */
  function crownMetresExpr(year) {
    const y = Math.min(MAX_YEAR, Math.max(0, year));
    let i = 0;
    while (i < YEARS.length - 2 && y > YEARS[i + 1]) i += 1;
    const [y0, y1] = [YEARS[i], YEARS[i + 1]];
    const t = (y - y0) / (y1 - y0);
    if (t <= 0) return prop(`c${y0}`);
    if (t >= 1) return prop(`c${y1}`);
    return ['+', ['*', prop(`c${y0}`), 1 - t], ['*', prop(`c${y1}`), t]];
  }

  function applyRadiusExpressions() {
    if (map.getLayer('existing-crowns')) map.setPaintProperty('existing-crowns', 'circle-radius', radiusExpr(prop('crown_r')));
    if (map.getLayer('crowns')) map.setPaintProperty('crowns', 'circle-radius', radiusExpr(crownMetresExpr(state.year)));
  }

  /** Recompute the metre scale when the centre latitude moved enough to matter (0.1 %). */
  function updateScale() {
    const ppm0 = ppmAtZoom0(map.getCenter().lat);
    if (state.ppm0 && Math.abs(ppm0 / state.ppm0 - 1) < 0.001) return;
    state.ppm0 = ppm0;
    applyRadiusExpressions();
  }

  function scheduleScale() {
    if (state.scaleRaf) return;
    state.scaleRaf = requestAnimationFrame(() => { state.scaleRaf = 0; updateScale(); });
  }

  /** Filled square for an existing trunk, drawn at 2x so it stays distinct from proposed rings. */
  function existingIcon() {
    const size = 28, pr = 2, s = 11, cx = size / 2, cy = size / 2;
    const canvas = document.createElement('canvas');
    canvas.width = size * pr;
    canvas.height = size * pr;
    const ctx = canvas.getContext('2d');
    ctx.scale(pr, pr);
    const x = cx - s / 2, y = cy - s / 2;
    ctx.fillStyle = alpha(C.ground, 0.85);
    ctx.fillRect(x - 1.6, y - 1.6, s + 3.2, s + 3.2);
    ctx.fillStyle = C.existing;
    ctx.fillRect(x, y, s, s);
    ctx.strokeStyle = alpha('#F4E6C8', 0.95);
    ctx.lineWidth = 1.3;
    ctx.strokeRect(x + 0.7, y + 0.7, s - 1.4, s - 1.4);
    return ctx.getImageData(0, 0, size * pr, size * pr);
  }

  /** Ring marker: solid (valid), dashed (conditional) or crossed (invalid); drawn at 2x. */
  function ringIcon(kind, colour) {
    const size = 28, pr = 2, r = 9.5, cx = size / 2, cy = size / 2;
    const canvas = document.createElement('canvas');
    canvas.width = size * pr;
    canvas.height = size * pr;
    const ctx = canvas.getContext('2d');
    ctx.scale(pr, pr);
    ctx.lineCap = 'round';
    const dash = kind === 'conditional' ? [(2 * Math.PI * r) / 18, (2 * Math.PI * r) / 18] : [];
    const ring = (width, style) => {
      ctx.beginPath();
      ctx.setLineDash(dash);
      ctx.lineWidth = width;
      ctx.strokeStyle = style;
      ctx.arc(cx, cy, r, 0, Math.PI * 2);
      ctx.stroke();
    };
    ring(3.5, alpha(C.ground, 0.8));   // halo for contrast on imagery
    ring(1.5, colour);
    if (kind === 'invalid') {
      const d = r + 2.5;
      ctx.setLineDash([]);
      for (const [w, style] of [[3.5, alpha(C.ground, 0.8)], [1.5, colour]]) {
        ctx.lineWidth = w;
        ctx.strokeStyle = style;
        ctx.beginPath();
        ctx.moveTo(cx - d, cy - d); ctx.lineTo(cx + d, cy + d);
        ctx.moveTo(cx + d, cy - d); ctx.lineTo(cx - d, cy + d);
        ctx.stroke();
      }
    }
    return ctx.getImageData(0, 0, size * pr, size * pr);
  }

  function addIcons() {
    map.addImage('existing-mark', existingIcon(), { pixelRatio: 2 });
    map.addImage('ring-valid', ringIcon('valid', C.valid), { pixelRatio: 2 });
    map.addImage('ring-conditional', ringIcon('conditional', C.cond), { pixelRatio: 2 });
    map.addImage('ring-invalid', ringIcon('invalid', C.invalid), { pixelRatio: 2 });
  }

  function addSources() {
    for (const id of [...LAYER_KEYS, 'sites', 'shade', 'draw', 'temperature', 'temperature-selected']) {
      map.addSource(id, { type: 'geojson', data: emptyFc() });
    }
  }

  function addLayers() {
    const line = (id, source, paint, extra = {}) => map.addLayer({ id, type: 'line', source, paint, ...extra });
    const fill = (id, source, paint, extra = {}) => map.addLayer({ id, type: 'fill', source, paint, ...extra });
    const hidden = { layout: { visibility: 'none' } };

    line('corridor-line', 'corridor', { 'line-color': C.muted, 'line-width': 1, 'line-dasharray': [4, 3], 'line-opacity': 0.75 });
    fill('carriageway-fill', 'carriageway', { 'fill-color': 'rgba(255,255,255,0.07)' });
    line('carriageway-line', 'carriageway', { 'line-color': 'rgba(255,255,255,0.3)', 'line-width': 1 });
    fill('sidewalks-fill', 'sidewalks', { 'fill-color': alpha(C.accent, 0.12), 'fill-outline-color': alpha(C.accent, 0.4) });
    fill('parking-fill', 'parking', { 'fill-color': 'rgba(232, 196, 110, 0.22)' });
    line('parking-line', 'parking', { 'line-color': 'rgba(232, 196, 110, 0.85)', 'line-width': 1.2 });
    fill('plantable-fill', 'plantable', { 'fill-color': alpha(C.accent, 0.22) }, hidden);
    line('plantable-line', 'plantable', { 'line-color': C.accent, 'line-width': 1, 'line-dasharray': [2, 2] }, hidden);
    fill('buildings-fill', 'buildings', { 'fill-color': alpha(C.ink, 0.16) });
    line('buildings-line', 'buildings', { 'line-color': alpha(C.ink, 0.45), 'line-width': 1 });
    line('axis-line', 'axis', { 'line-color': alpha(C.ink, 0.5), 'line-width': 1 });
    line('cycleways-line', 'cycleways', { 'line-color': C.cycle, 'line-width': 2, 'line-dasharray': [3, 2] });
    fill('shade-fill', 'shade', { 'fill-color': alpha(C.shade, 0.35), 'fill-outline-color': alpha(C.shade, 0.6) }, hidden);
    map.addLayer({
      id: 'existing-crowns', type: 'circle', source: 'existing_trees',
      paint: {
        'circle-radius': radiusExpr(prop('crown_r')),
        'circle-color': alpha(C.existing, 0.22),
        'circle-stroke-color': alpha(C.existing, 0.95),
        'circle-stroke-width': 1.6,
        'circle-pitch-alignment': 'map',
      },
    });
    map.addLayer({
      id: 'crowns', type: 'circle', source: 'sites',
      filter: ['!=', ['get', 'verdict'], 'invalid'],
      paint: {
        'circle-radius': radiusExpr(crownMetresExpr(state.year)),
        'circle-color': alpha(C.crown, 0.35),
        'circle-stroke-color': alpha(C.crown, 0.8),
        'circle-stroke-width': 1,
        'circle-pitch-alignment': 'map',
      },
    });
    map.addLayer({
      id: 'junctions-circle', type: 'circle', source: 'junctions',
      paint: { 'circle-radius': 3.5, 'circle-color': C.ground, 'circle-stroke-color': C.muted, 'circle-stroke-width': 1.5 },
    });
    map.addLayer({
      id: 'sites-ring', type: 'symbol', source: 'sites', minzoom: 15.5,
      layout: {
        'icon-image': ['match', ['get', 'verdict'], 'valid', 'ring-valid', 'conditional', 'ring-conditional', 'ring-invalid'],
        'icon-size': ['interpolate', ['linear'], ['zoom'], 13, 0.35, 15, 0.6, 17, 1, 19, 1.15],
        'icon-allow-overlap': true,
        'icon-ignore-placement': true,
      },
    });
    map.addLayer({
      id: 'sites', type: 'circle', source: 'sites',
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 13, 1.5, 15, 3, 17, 5, 19, 6],
        'circle-color': C.ground,
        'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 13, 0.8, 15, 1.4, 17, 2],
        'circle-stroke-color': ['match', ['get', 'verdict'], 'valid', C.valid, 'conditional', C.cond, C.invalid],
      },
    });
    map.addLayer({
      id: 'existing-marks', type: 'symbol', source: 'existing_trees',
      layout: {
        'icon-image': 'existing-mark',
        'icon-size': ['interpolate', ['linear'], ['zoom'], 13, 0.55, 15, 0.85, 17, 1.15, 19, 1.3],
        'icon-allow-overlap': true,
        'icon-ignore-placement': true,
      },
    });
    line('draw-line', 'draw', { 'line-color': C.accent, 'line-width': 2, 'line-dasharray': [2, 2] });
    map.addLayer({
      id: 'draw-points', type: 'circle', source: 'draw',
      filter: ['==', ['geometry-type'], 'Point'],
      paint: { 'circle-radius': 4, 'circle-color': C.accent, 'circle-stroke-color': C.ground, 'circle-stroke-width': 1.5 },
    });
    map.addLayer({
      id: 'temperature-points', type: 'circle', source: 'temperature',
      layout: { visibility: 'none' },
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 13, 3, 17, 6, 19, 9],
        'circle-color': ['interpolate', ['linear'], ['get', 'cooling_c'], 0, C.coolingLow, 0.25, C.coolingMid, 0.5, C.coolingHigh],
        'circle-stroke-color': C.ground, 'circle-stroke-width': 1,
      },
    });
    map.addLayer({
      id: 'temperature-selected', type: 'circle', source: 'temperature-selected',
      layout: { visibility: 'none' },
      paint: { 'circle-radius': 12, 'circle-color': 'transparent', 'circle-stroke-color': '#FFFFFF', 'circle-stroke-width': 2.5 },
    });
  }

  function setSourceData(id, fc) {
    const src = map.getSource(id);
    if (src) src.setData(fc || emptyFc());
  }

  /** Show/hide a logical layer group and keep the legend checkbox in sync. */
  function setLayerVisible(key, visible) {
    setTemperatureMapVisible(false);
    if (key === 'shade') { setShadeEnabled(Boolean(visible)); return; }
    const ids = LAYER_GROUPS[key];
    if (!ids) return;
    for (const id of ids) if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', visible ? 'visible' : 'none');
    const box = ui.legendToggles.find((cb) => cb.dataset.layer === key);
    if (box) box.checked = Boolean(visible);
  }

  function firstOverlayLayerId() {
    const ids = map.getStyle().layers.map((l) => l.id).filter((id) => id !== 'basemap');
    return ids[0];
  }

  /** Swap the raster basemap; the layer stays underneath every overlay. */
  function setBasemap(city, basemapId) {
    const bm = city.basemaps.find((b) => b.id === basemapId) || city.basemaps.find((b) => b.default) || city.basemaps[0];
    if (!bm) return;
    state.basemapId = bm.id;
    if (map.getLayer('basemap')) map.removeLayer('basemap');
    if (map.getSource('basemap')) map.removeSource('basemap');
    map.addSource('basemap', {
      type: 'raster', tiles: bm.tiles, tileSize: bm.tile_size || 256,
      attribution: bm.attribution || '', maxzoom: bm.max_zoom || 19,
    });
    map.addLayer({ id: 'basemap', type: 'raster', source: 'basemap', paint: { 'raster-fade-duration': motionMs(150) } }, firstOverlayLayerId());
    renderBasemapSwitch(city);
  }

  function renderBasemapSwitch(city) {
    ui.basemaps.replaceChildren(...city.basemaps.map((bm) => el('button', {
      type: 'button', text: bm.label, 'aria-pressed': String(bm.id === state.basemapId),
      onclick: () => setBasemap(city, bm.id),
    })));
  }

  /** Padding that keeps the fitted street clear of the floating panels. */
  function fitPadding() {
    return {
      top: document.querySelector('.map-toolbar').offsetHeight + 36,
      left: 30, right: 30, bottom: (state.temperature.mapVisible ? $('temperature-map-key').offsetHeight : ui.dock.offsetHeight) + 60,
    };
  }

  function fitToBbox(bbox) {
    if (!bbox || bbox.length < 4) return;
    map.resize();
    // Point inspection leaves camera padding behind; do not count it twice when fitting.
    map.setPadding({ top: 0, bottom: 0, left: 0, right: 0 });
    map.fitBounds([[bbox[0], bbox[1]], [bbox[2], bbox[3]]], { padding: fitPadding(), duration: motionMs(800), maxZoom: 18 });
  }

  function bboxOfPoints(fc) {
    let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
    for (const f of fc.features || []) {
      if (!f.geometry || f.geometry.type !== 'Point') continue;
      const [x, y] = f.geometry.coordinates;
      minx = Math.min(minx, x); miny = Math.min(miny, y); maxx = Math.max(maxx, x); maxy = Math.max(maxy, y);
    }
    return Number.isFinite(minx) ? [minx, miny, maxx, maxy] : null;
  }

  // -- popups
  function popup(persistent) {
    return new maplibregl.Popup({
      closeButton: persistent, closeOnClick: persistent, maxWidth: `${Math.min(360, ui.map.clientWidth - 40)}px`, offset: 14,
      className: persistent ? 'pop-pinned' : 'pop-hover',
    });
  }

  function ruleRequirement(rule) {
    const distance = `${fmt.num(rule.required_m, 2)} m`;
    const text = {
      building_crown: ['Mature crown too close to a building', `Keep at least ${distance} between the mature crown and a building.`],
      sidewalk_passage: ['Not enough clear sidewalk space', `Leave at least ${distance} of clear sidewalk beside the tree pit.`],
      junction: ['Too close to a junction', `Keep the trunk at least ${distance} from a junction.`],
      existing_tree: ['Too close to an existing tree', `Keep the new trunk at least ${distance} from an existing tree trunk.`],
      occupied_tree_position: ['An existing trunk occupies this position', 'A new tree cannot use the same mapped trunk position as an existing tree.'],
      parking_exclusion: ['Position is a parking bay', 'A new tree cannot stand on a mapped parking bay or paved parking surface.'],
      cycleway: ['Too close to a cycle path', `Keep the trunk at least ${distance} from the edge of a cycle path.`],
      carriageway_edge: ['Too close to the road', `Keep the trunk at least ${distance} from the road edge.`],
      plantable_surface: ['Position is outside a planting surface', 'Place the trunk on a sidewalk or verge, outside roads, parking and buildings.'],
    }[rule.rule_id];
    return text || [rule.label, rule.required_m == null ? rule.label : `${rule.label}: at least ${distance}.`];
  }

  function requirementSource(rule, definition, pack) {
    if (rule.rule_id === 'junction_exclusion') return 'Mandatory planning exclusion · 10 m along the street or to the trunk · not a surveyed sight triangle';
    if (rule.rule_id === 'occupied_tree_position') return 'Geometry safeguard · uses mapped trunk positions, not a city clearance standard';
    if (rule.rule_id === 'parking_exclusion') return 'Geometry safeguard · uses mapped parking bays and paved parking, not a city clearance standard';
    if (rule.assumption) return 'Editable planning default · not a cited city standard';
    return definition && pack ? `${definition.overridden ? 'Edited for this plan · based on ' : ''}${ruleCitation(definition, pack)}` : 'See the rule pack for its source';
  }

  function failedRuleCard(rule, definition, pack) {
    const [problem, requirement] = ruleRequirement(rule);
    const clearance = rule.measured_m == null ? rule.note || 'Clearance could not be calculated.'
      : rule.measured_m < 0 && rule.rule_id === 'building_crown'
        ? `The mature crown overlaps the building by ${fmt.num(-rule.measured_m, 2)} m.`
        : `Available here: ${fmt.num(rule.measured_m, 2)} m${rule.basis === 'estimated' ? ' (estimated)' : ''}.`;
    return el('li', { class: 'explicit-rule' }, [
      el('strong', { text: problem }),
      el('p', { text: `${rule.mode === 'should' ? 'Recommendation' : 'Required rule'}: ${requirement}` }),
      rule.required_m != null || rule.rule_id === 'occupied_tree_position' ? el('p', { text: clearance }) : null,
      el('p', { class: 'hint', text: requirementSource(rule, definition, pack) }),
    ]);
  }

  function ruleLine(r, ruleDef) {
    const cls = r.passed === true ? 'pass' : r.passed === false ? (r.mode === 'should' ? 'recommendation-fail' : 'fail') : 'na';
    const mark = r.passed === true ? '✓' : r.passed === false ? (r.mode === 'should' ? '!' : '✗') : '–';
    const threshold = r.mode === 'should' ? 'recommended' : 'required';
    let measure;
    if (r.rule_id === 'occupied_tree_position') {
      measure = r.passed ? 'no mapped trunk at this position' : 'position coincides with a mapped existing trunk';
    } else if (r.rule_id === 'parking_exclusion') {
      measure = r.passed ? 'not on a mapped parking bay' : 'position sits on a mapped parking bay';
    } else if (r.required_m == null) {
      measure = r.passed == null ? (r.note || 'not evaluated') : (r.passed ? 'on plantable surface' : 'off plantable surface');
    } else if (r.measured_m == null) {
      measure = `${r.note || 'not measurable'} · ≥ ${fmt.num(r.required_m, 2)} m ${threshold}`;
    } else {
      measure = `${fmt.num(r.measured_m, 2)} m available; at least ${fmt.num(r.required_m, 2)} m ${threshold}`;
    }
    const cite = r.rule_id === 'occupied_tree_position' || r.rule_id === 'parking_exclusion' ? 'geometry safeguard' : ruleDef && ruleDef.source_ref ? shortRef(ruleDef.source_ref) : (r.assumption ? 'planning default' : null);
    const meta = ` (${r.mode === 'should' ? 'recommendation' : 'required rule'}${cite ? `, ${cite}` : ''}${r.basis && r.basis !== 'measured' ? `, ${r.basis}` : ''})`;
    return el('li', { class: `rule-line ${cls}` }, [
      el('span', { class: 'mark', text: mark }),
      el('span', {}, [
        el('span', { class: 'lbl', text: `${r.label} — ` }),
        el('span', { class: 'meas', text: measure }),
        el('span', { class: 'meta', text: meta }),
      ]),
    ]);
  }

  /** Popup body for a proposed site: id, station, side, verdict and every rule result. */
  function siteCard(props, preferredRuleId) {
    const sc = activeScenario();
    const defs = new Map(((sc && sc.rules_used && sc.rules_used.rules) || []).map((r) => [r.id, r]));
    const verdict = VERDICTS[props.verdict] || VERDICTS.valid;
    const rules = props.rules || [];
    const failed = rules.filter((r) => r.passed === false).sort((a, b) => {
      if (preferredRuleId) {
        if (a.rule_id === preferredRuleId) return -1;
        if (b.rule_id === preferredRuleId) return 1;
      }
      return (a.mode === 'must' ? 0 : 1) - (b.mode === 'must' ? 0 : 1);
    });
    const unknown = rules.filter((r) => r.passed == null);
    const editableId = (preferredRuleId && defs.has(preferredRuleId))
      ? preferredRuleId
      : (failed.find((r) => defs.has(r.rule_id)) || {}).rule_id;
    return el('div', { class: 'pop' }, [
      el('div', { class: 'pop-head' }, [
        el('span', { class: 'pop-title pop-title-proposed', text: 'Proposed tree' }),
        el('span', { class: `verdict verdict-${props.verdict}`, text: verdict.label }),
      ]),
      el('p', { class: 'pop-explanation', text: verdict.explanation }),
      failed.length ? el('div', { class: 'site-conflicts' }, [
        el('b', { text: 'Why this position is flagged' }),
        el('ul', { class: 'pop-rules' }, failed.map((r) => failedRuleCard(r, defs.get(r.rule_id), sc && sc.rules_used))),
        editableId
          ? el('button', {
            type: 'button', class: 'btn btn-sm',
            text: 'Change this limit',
            onclick: () => focusRestriction(editableId),
          })
          : null,
      ]) : null,
      unknown.length ? el('p', { class: 'hint', text: `${unknown.length} checks could not be evaluated. Open all checks for the missing information.` }) : null,
      el('details', { class: 'site-checks' }, [
        el('summary', { text: `All ${rules.length} rule checks and sources` }),
        el('ul', { class: 'pop-rules' }, rules.map((r) => ruleLine(r, defs.get(r.rule_id)))),
      ]),
      el('div', { class: 'pop-meta', text: `${props.site_id} · ${Math.round(props.station_m)} m along the street · ${props.side} side · mature crown ${fmt.num(props.crown_d_mature_m, 0)} m wide` }),
    ]);
  }

  /** Popup body for an existing tree. */
  function treeCard(p) {
    const rows = [
      ['Species', p.species || p.genus || 'unknown'],
      ['Crown', `${fmt.m(p.crown_d_m, 1)}${p.crown_imputed ? ' (imputed)' : ''}`],
      ['Height', p.height_m != null ? fmt.m(p.height_m, 1) : '–'],
      ['Planted', p.planted_year != null ? String(p.planted_year) : '–'],
      ['Source', p.source || '–'],
    ];
    return el('div', { class: 'pop' }, [
      el('div', { class: 'pop-head' }, [el('span', { class: 'pop-title pop-title-existing', text: 'Existing tree' }), el('span', { class: 'pop-meta mono', text: p.id || '' })]),
      el('dl', { class: 'pop-rows' }, rows.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })])),
    ]);
  }

  function siteFeature(siteId) {
    return state.siteIndex.get(siteId) || null;
  }

  function showHover(feature) {
    const f = siteFeature(feature.properties.site_id);
    if (!f) return;
    if (!state.hoverPopup) state.hoverPopup = popup(false);
    state.hoverPopup.setLngLat(f.geometry.coordinates).setDOMContent(siteCard(f.properties)).addTo(map);
  }

  function hideHover() {
    if (state.hoverPopup) state.hoverPopup.remove();
  }

  function pinPopup(lngLat, content) {
    if (state.pinPopup) state.pinPopup.remove();
    state.pinPopup = popup(true).setLngLat(lngLat).setDOMContent(content).addTo(map);
  }

  /** Ease to a site of the active scenario and pin its rule card. */
  function focusSite(siteId, zoom, preferredRuleId) {
    const f = siteFeature(siteId);
    if (!f) return false;
    setTemperatureMapVisible(false);
    map.easeTo({ center: f.geometry.coordinates, zoom: Math.max(map.getZoom(), zoom || 18), duration: motionMs(700) });
    pinPopup(f.geometry.coordinates, siteCard(f.properties, preferredRuleId));
    return true;
  }

  function wireMapEvents() {
    map.on('zoom', scheduleScale);
    map.on('move', scheduleScale);
    map.on('mouseenter', 'temperature-points', () => { map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', 'temperature-points', () => { map.getCanvas().style.cursor = ''; hideHover(); });
    map.on('mousemove', 'temperature-points', (e) => {
      if (!state.temperature.mapVisible || !e.features.length) return;
      const p = e.features[0].properties;
      if (!state.hoverPopup) state.hoverPopup = popup(false);
      state.hoverPopup.setLngLat(e.lngLat).setDOMContent(temperaturePointCard(p)).addTo(map);
    });
    map.on('click', 'temperature-points', (e) => {
      if (!state.temperature.mapVisible || !e.features.length) return;
      hideHover();
      selectTemperaturePoint(e.features[0].properties.sample_id, { focusMap: true, pin: true });
    });
    map.on('mousemove', 'sites', (e) => {
      if (state.draw.active || state.pickingStreet || !e.features.length) return;
      map.getCanvas().style.cursor = 'pointer';
      showHover(e.features[0]);
    });
    map.on('mouseleave', 'sites', () => { map.getCanvas().style.cursor = ''; hideHover(); });
    map.on('click', 'sites', (e) => {
      if (state.draw.active || state.pickingStreet || !e.features.length) return;
      const f = siteFeature(e.features[0].properties.site_id);
      if (f) pinPopup(f.geometry.coordinates, siteCard(f.properties));
    });
    const onExistingEnter = () => { if (!state.draw.active && !state.pickingStreet) map.getCanvas().style.cursor = 'pointer'; };
    const onExistingLeave = () => { map.getCanvas().style.cursor = ''; hideHover(); };
    const onExistingClick = (e) => {
      if (state.draw.active || state.pickingStreet || !e.features.length) return;
      pinPopup(e.lngLat, treeCard(e.features[0].properties));
    };
    const onExistingMove = (e) => {
      if (state.draw.active || state.pickingStreet || !e.features.length) return;
      if (!state.hoverPopup) state.hoverPopup = popup(false);
      state.hoverPopup.setLngLat(e.lngLat).setDOMContent(treeCard(e.features[0].properties)).addTo(map);
    };
    map.on('mouseenter', 'existing-crowns', onExistingEnter);
    map.on('mouseleave', 'existing-crowns', () => { map.getCanvas().style.cursor = ''; });
    map.on('click', 'existing-crowns', (e) => {
      if (map.queryRenderedFeatures(e.point, { layers: ['existing-marks'] }).length) return;
      if (map.queryRenderedFeatures(e.point, { layers: ['sites', 'sites-ring'] }).length) return;
      onExistingClick(e);
    });
    map.on('mouseenter', 'existing-marks', onExistingEnter);
    map.on('mousemove', 'existing-marks', onExistingMove);
    map.on('mouseleave', 'existing-marks', onExistingLeave);
    map.on('click', 'existing-marks', onExistingClick);
    map.on('click', onDrawClick);
    map.on('click', async (e) => {
      if (!state.pickingStreet || state.draw.active || state.loadingStreet || state.planning || state.updatingRules || state.agent.streaming) return;
      if (map.getZoom() < 15) {
        map.easeTo({ center: e.lngLat, zoom: 16, duration: motionMs(500) });
        ui.mapHint.textContent = 'Zoomed in. Click the street you want to check.';
        return;
      }
      await loadStreetAndPlan({ city: state.cityId, point: [e.lngLat.lng, e.lngLat.lat] });
    });
    map.on('dblclick', onDrawDoubleClick);
  }

  // -- drawing a street axis
  function setStreetPicking(active) {
    if (active) setTemperatureMapVisible(false);
    state.pickingStreet = Boolean(active);
    ui.map.classList.toggle('picking-street', state.pickingStreet);
    map.getCanvas().style.cursor = '';
    if (state.pickingStreet) { hideHover(); if (state.pinPopup) state.pinPopup.remove(); }
    renderWorkflow();
  }

  function startDraw() {
    if (state.loadingStreet || state.planning || state.agent.streaming || !state.config) return;
    setTemperatureMapVisible(false);
    setStreetPicking(false);
    state.draw.active = true;
    state.draw.points = [];
    map.doubleClickZoom.disable();
    ui.map.classList.add('drawing');
    ui.draw.setAttribute('aria-pressed', 'true');
    $('draw-actions').hidden = false;
    document.querySelector('.map-workspace').classList.add('is-drawing');
    ui.draw.textContent = 'Cancel drawing';
    ui.mapHint.textContent = 'Click at least two points on the map. Then choose Finish drawing. Escape cancels.';
    ui.map.scrollIntoView({ behavior: REDUCED_MOTION.matches ? 'instant' : 'smooth', block: 'center' });
    hideHover();
    renderWorkflow();
    renderDraw();
  }

  function stopDraw() {
    state.draw.active = false;
    state.draw.points = [];
    map.doubleClickZoom.enable();
    ui.map.classList.remove('drawing');
    ui.draw.setAttribute('aria-pressed', 'false');
    $('draw-actions').hidden = true;
    document.querySelector('.map-workspace').classList.remove('is-drawing');
    ui.draw.textContent = 'Draw street';
    renderWorkflow();
    renderDraw();
  }

  function renderDraw() {
    const pts = state.draw.points;
    $('finish-draw').disabled = pts.length < 2;
    $('undo-draw').disabled = pts.length === 0;
    if (state.draw.active) ui.mapHint.textContent = `${pts.length} ${pts.length === 1 ? 'point' : 'points'} selected. Follow the street centreline; select at least two points, then Finish drawing.`;
    const features = pts.map((p) => ({ type: 'Feature', geometry: { type: 'Point', coordinates: p }, properties: {} }));
    if (pts.length >= 2) features.push({ type: 'Feature', geometry: { type: 'LineString', coordinates: pts }, properties: {} });
    setSourceData('draw', { type: 'FeatureCollection', features });
  }

  function onDrawClick(e) {
    if (!state.draw.active) return;
    state.draw.points.push([e.lngLat.lng, e.lngLat.lat]);
    renderDraw();
  }

  function onDrawDoubleClick(e) {
    if (!state.draw.active) return;
    e.preventDefault();
    finishDraw();
  }

  /** Drop the duplicate point the double-click leaves behind, then load the drawn axis. */
  async function finishDraw() {
    const pts = state.draw.points.filter((p, i, arr) => i === 0
      || Math.abs(p[0] - arr[i - 1][0]) > 1e-6 || Math.abs(p[1] - arr[i - 1][1]) > 1e-6);
    stopDraw();
    if (pts.length < 2) { setError('Draw at least two points to define a street axis.'); return; }
    const line = pts.map((p) => [Number(p[0].toFixed(7)), Number(p[1].toFixed(7))]);
    await loadStreetAndPlan({ city: state.cityId, line, name: 'Drawn street' });
  }

  // ------------------------------------------------------------ cities & streets
  const cityById = (id) => (state.config ? state.config.cities.find((c) => c.id === id) : null);
  /** Choose where to search or draw; street loading is an explicit user action. */
  function selectCity(cityId, { silent = false } = {}) {
    const city = cityById(cityId);
    if (!city) return;
    state.cityId = city.id;
    ui.city.value = city.id;
    ui.search.placeholder = `Street name in ${city.name}`;
    renderDemoChips(city);
    setBasemap(city, null);
    if (!silent) {
      if (state.draw.active) stopDraw();
      state.street = null;
      state.dirty = false;
      for (const key of LAYER_KEYS) setSourceData(key, emptyFc());
      clearScenarioView();
      $('tree-evidence').hidden = true;
      $('street-picker').open = true;
      ui.streetMeta.textContent = '';
      ui.search.value = '';
      Object.assign(state.status, { sources: [], estimated: [], warnings: [], notes: [], error: null });
      renderStatus();
      map.flyTo({ center: city.center, zoom: city.zoom, duration: motionMs(700) });
      setStreetPicking(true);
      renderWorkflow();
    }
  }

  function renderDemoChips(city) {
    ui.demoChips.replaceChildren(...city.demo_streets.map((name) => el('button', {
      type: 'button', class: 'chip', text: name, 'aria-pressed': 'false',
      onclick: () => { ui.search.value = name; loadStreetAndPlan({ city: state.cityId, query: name }); },
    })));
  }

  function markDemoChip(streetName) {
    for (const chip of ui.demoChips.children) {
      chip.setAttribute('aria-pressed', String(chip.textContent === streetName));
    }
  }

  /** POST /api/street. */
  async function loadStreet(req) {
    cancelAutoApply();
    state.loadingStreet = true;
    renderWorkflow();
    setBusy(ui.searchBtn, true);
    ui.draw.disabled = true;
    try {
      let street = await api('/api/street', { method: 'POST', body: req });
      const treeSource = street.layer_sources && street.layer_sources.existing_trees;
      const axis = street.layers.axis && street.layers.axis.features[0];
      // Named streets can retain an old OSM fallback after the city feed recovers.
      // Rebuild once with the same line; never recursively retry a drawn request.
      if (req.query && street.city === 'zurich' && treeSource && treeSource.includes('OpenStreetMap') && axis && axis.geometry.type === 'LineString') {
        ui.workflowStatus.textContent = 'Rechecking Zürich’s tree register for existing trees…';
        try {
          street = await api('/api/street', { method: 'POST', body: {
            city: street.city, line: axis.geometry.coordinates, name: street.name,
          } });
        } catch (_) { /* Keep the available street; its OSM fallback warning stays visible. */ }
      }
      applyStreet(street, { fit: true });
      clearError();
      return street;
    } catch (err) {
      setError(`Street: ${err.message}`);
      return null;
    } finally {
      setBusy(ui.searchBtn, false);
      state.loadingStreet = false;
      renderWorkflow();
    }
  }

  async function loadStreetAndPlan(req) {
    const street = await loadStreet(req);
    if (street) await runPlan(formParams());
    return street;
  }

  /** Put a StreetResponse on the map and clear any scenario view. */
  function applyStreet(street, { fit = true } = {}) {
    if (state.draw.active) stopDraw();
    setStreetPicking(false);
    $('street-picker').open = false;
    state.rulesEditorPinned = false;
    state.street = street;
    state.dirty = false;
    state.streets.set(street.street_id, street);
    if (street.city !== state.cityId && cityById(street.city)) selectCity(street.city, { silent: true });
    for (const key of LAYER_KEYS) {
      const fc = (street.layers && street.layers[key]) || emptyFc();
      setSourceData(key, key === 'existing_trees' ? withCrownRadius(fc) : fc);
    }
    clearScenarioView();
    statusFromStreet(street);
    renderStreetMeta(street);
    ui.search.value = street.name || '';
    markDemoChip(street.name);
    if (fit) fitToBbox(street.bbox);
  }

  /** Existing trees carry crown_d_m; the circle layer wants a radius property. */
  function withCrownRadius(fc) {
    return {
      type: 'FeatureCollection',
      features: (fc.features || []).map((f) => ({
        ...f, properties: { ...f.properties, crown_r: (Number(f.properties && f.properties.crown_d_m) || 0) / 2 },
      })),
    };
  }

  function renderStreetMeta(street) {
    const s = street.stats || {};
    ui.streetMeta.textContent = `${street.name || 'Street'} · ${fmt.grouped(street.length_m)} m`;
    $('tree-evidence').hidden = false;
    $('existing-tree-count').textContent = `${fmt.int(s.existing_trees)} existing trees loaded`;
    const source = street.layer_sources && street.layer_sources.existing_trees;
    const fallback = street.city === 'zurich' && source && source.includes('OpenStreetMap');
    $('existing-tree-source').textContent = fallback ? 'Zürich tree register unavailable. Using OpenStreetMap.' : `Source: ${source || 'No tree source available'}`;
    $('existing-tree-limit').textContent = fallback
      ? 'Some existing trees may be missing. An empty spot on the map is not proof of space for a new tree.'
      : 'The loaded trees are included in spacing checks and canopy estimates. The inventory may not contain every tree.';
    $('tree-evidence').classList.toggle('has-warning', Boolean(fallback));
    $('refresh-tree-data').hidden = !fallback;
    renderWorkflow();
  }

  // ------------------------------------------------------------ plans & scenarios
  const radioValue = (name) => { const r = ui.planForm.querySelector(`input[name="${name}"]:checked`); return r ? r.value : null; };

  /** PlanParams from the Plan card (offset, pit, pack from config defaults). */
  function formParams() {
    const d = (state.config && state.config.defaults) || {};
    const side = radioValue('side') || d.side || 'both';
    const mode = radioValue('mode') || d.mode || 'grid';
    const label = autoPlanName({
      spacing_m: Number(ui.spacing.value),
      side,
      species_id: ui.species.value || d.species_id,
      crown_diameter_m: Number($('crown-size').value),
      mode,
    });
    return {
      spacing_m: Number(ui.spacing.value),
      side,
      species_id: ui.species.value || d.species_id,
      crown_diameter_m: Number($('crown-size').value),
      mode,
      offset_from_edge_m: d.offset_from_edge_m ?? 1.0,
      pit_width_m: d.pit_width_m ?? 2.0,
      rule_pack_id: state.pack ? state.pack.id : d.rule_pack_id,
      label: ui.label.value.trim() || label.slice(0, 60),
    };
  }

  function updatePlanDraft() {
    const sc = activeScenario();
    const p = formParams();
    state.dirty = !sc || ['spacing_m', 'species_id', 'side', 'mode'].some(key => p[key] !== sc.params[key])
      || p.crown_diameter_m !== sc.species.mature_crown_d_m
      || Boolean(ui.label.value.trim() && ui.label.value.trim() !== sc.label);
    scheduleAutoApply();
    renderWorkflow();
  }

  function cancelAutoApply() {
    clearTimeout(state.autoApply.timer);
    Object.assign(state.autoApply, { pending: false, failed: false, rules: {}, cooling: null, revision: state.autoApply.revision + 1 });
  }

  /** Keep editing responsive; serialize saves and only display the latest revision. */
  function scheduleAutoApply(delay = 250) {
    if (!state.street) return;
    const auto = state.autoApply;
    if (state.temperature.result && !auto.cooling) {
      auto.cooling = { selectedId: state.temperature.selectedId, mapVisible: state.temperature.mapVisible };
    }
    auto.revision += 1;
    auto.pending = true;
    auto.failed = false;
    invalidateTemperature('Updating the plan…');
    clearTimeout(auto.timer);
    auto.timer = setTimeout(flushAutoApply, delay);
    renderWorkflow();
  }

  async function flushAutoApply() {
    const auto = state.autoApply;
    if (!auto.pending || auto.running || state.planning || state.loadingStreet || state.agent.streaming) return;
    if (!ui.planForm.checkValidity() || ui.rules.querySelector('input:invalid')) {
      auto.pending = false;
      auto.failed = true;
      renderWorkflow();
      return;
    }
    auto.pending = false;
    auto.running = true;
    const revision = auto.revision;
    const patches = auto.rules;
    auto.rules = {};
    renderWorkflow();
    try {
      for (const [ruleId, patch] of Object.entries(patches)) {
        state.updatingRules = true;
        const res = await api(`/api/rules/${encodeURIComponent(state.pack.id)}/${encodeURIComponent(ruleId)}`, { method: 'PUT', body: patch });
        setRulePack(res.pack || withOverrides(state.pack, { [ruleId]: patch }), { render: false });
        delete patches[ruleId];
      }
      state.updatingRules = false;
      if (revision !== auto.revision) return;
      renderRules();
      const result = await runPlan(formParams(), { replaceId: state.activeScenarioId, autoRevision: revision });
      if (revision !== auto.revision) return;
      if (!result) { auto.failed = true; return; }
      if (auto.cooling) {
        await compareTemperature({ restore: auto.cooling });
        if (revision === auto.revision) auto.cooling = null;
      }
    } catch (err) {
      // Keep unsaved edits for retry; newer edits to the same field take precedence.
      for (const [id, patch] of Object.entries(patches)) auto.rules[id] = { ...patch, ...auto.rules[id] };
      auto.failed = true;
      auto.pending = false;
      setError(`Could not apply changes: ${err.message}`);
    } finally {
      auto.running = false;
      state.updatingRules = false;
      renderWorkflow();
      if (auto.pending) auto.timer = setTimeout(flushAutoApply, 0);
    }
  }

  /** POST /api/plan for the current street; the new scenario becomes active. */
  async function runPlan(params, { replaceId = null, autoRevision = null } = {}) {
    if (!state.street) { setError('Explore a street before planning.'); return null; }
    if (state.planning) return null;
    state.planning = true;
    renderWorkflow();
    setBusy(ui.planBtn, true);
    try {
      const sc = await api('/api/plan', { method: 'POST', body: { street_id: state.street.street_id, ...params } });
      if (autoRevision !== null && autoRevision !== state.autoApply.revision) return null;
      addScenario(sc, { replaceId });
      selectScenario(sc.scenario_id, { fromAuto: autoRevision !== null });
      clearError();
      return sc;
    } catch (err) {
      if (autoRevision === null || autoRevision === state.autoApply.revision) setError(`Plan: ${err.message}`);
      return null;
    } finally {
      state.planning = false;
      setBusy(ui.planBtn, false);
      renderWorkflow();
    }
  }

  function addScenario(sc, { replaceId = null } = {}) {
    const same = state.scenarios.findIndex((s) => s.scenario_id === sc.scenario_id);
    const replaced = replaceId ? state.scenarios.findIndex((s) => s.scenario_id === replaceId) : -1;
    if (same !== -1) state.scenarios[same] = sc;
    else if (replaced !== -1) state.scenarios[replaced] = sc;
    else state.scenarios.push(sc);
    renderScenarios();
  }

  /** Flatten crown_d_by_year into c0…c30 radius properties the paint expression can read. */
  function decorateSites(fc) {
    return {
      type: 'FeatureCollection',
      features: (fc.features || []).map((f) => {
        const p = { ...f.properties };
        const byYear = p.crown_d_by_year || {};
        for (const y of YEARS) p[`c${y}`] = (Number(byYear[String(y)]) || 0) / 2;
        return { ...f, properties: p };
      }),
    };
  }

  /** Show a scenario on the map (loading its street first if it differs). */
  function selectScenario(scenarioId, { fromAuto = false } = {}) {
    const sc = state.scenarios.find((s) => s.scenario_id === scenarioId);
    if (!sc) return;
    if (!fromAuto) cancelAutoApply();
    if (!state.street || state.street.street_id !== sc.street_id) {
      const st = state.streets.get(sc.street_id);
      if (st) applyStreet(st, { fit: true });
    }
    state.activeScenarioId = sc.scenario_id;
    invalidateTemperature('Ready to compare this plan with the current trees.');
    invalidateShade();
    state.dirty = false;
    const params = sc.params || {};
    if (params.spacing_m != null) ui.spacing.value = String(params.spacing_m);
    if (params.species_id) ui.species.value = params.species_id;
    $('crown-size').value = String(sc.species?.mature_crown_d_m ?? params.crown_diameter_m ?? 10);
    for (const name of ['side', 'mode']) {
      for (const input of ui.planForm.querySelectorAll(`input[name="${name}"]`)) input.checked = input.value === params[name];
    }
    if (!fromAuto) ui.label.value = '';
    ui.spacingOut.textContent = `${fmt.num(ui.spacing.value, 1)} m`;
    renderSpeciesHint();
    const fc = decorateSites(sc.sites || emptyFc());
    state.siteIndex = new Map(fc.features.map((f) => [f.properties.site_id, f]));
    setSourceData('sites', fc);
    if (!state.street || state.street.street_id !== sc.street_id) fitToBbox(bboxOfPoints(fc));
    hideHover();
    if (state.pinPopup) state.pinPopup.remove();
    renderScenarios();
    renderPlanMeta(sc);
    renderYearReadout();
    renderExport();
    renderWorkflow();
    if (state.shade.enabled) fetchShade(); else setSourceData('shade', emptyFc());
  }

  function clearScenarioView() {
    cancelAutoApply();
    state.activeScenarioId = null;
    invalidateTemperature('Choose a street and create a plan first.');
    state.siteIndex = new Map();
    setSourceData('sites', emptyFc());
    invalidateShade();
    hideHover();
    if (state.pinPopup) state.pinPopup.remove();
    renderScenarios();
    renderPlanMeta(null);
    renderYearReadout();
    renderExport();
    renderShadeInfo(null);
  }

  const cover30 = (sc) => {
    const years = (sc.canopy && sc.canopy.years) || [];
    const y = years.find((r) => r.year === MAX_YEAR) || years[years.length - 1];
    return y ? y.cover_corridor_pct : null;
  };

  const existingCover = (sc) => {
    const v = sc && sc.canopy && sc.canopy.existing_cover_corridor_pct;
    return v == null ? null : Number(v);
  };

  const canopyYear = (sc, year) => {
    const years = (sc && sc.canopy && sc.canopy.years) || [];
    return years.find((r) => r.year === year) || null;
  };

  const sidewalk30 = (sc) => {
    const row = canopyYear(sc, MAX_YEAR);
    return row ? row.sidewalk_under_crown_pct : null;
  };

  const points = (delta) => `${delta > 0 ? '+' : ''}${fmt.num(delta, 1)} points`;

  function planHeadline(sc) {
    if (!sc || !sc.summary) return '';
    const now = existingCover(sc);
    const later = cover30(sc);
    const n = sc.summary.planted;
    if (n === 0) return now == null ? 'No proposed trees fit these settings.' : `No proposed trees fit. Cover stays at ${fmt.pct(now)}.`;
    if (now == null || later == null) return `${fmt.int(n)} proposed trees.`;
    const delta = later - now;
    if (delta <= 0.05) return `${fmt.int(n)} proposed trees; study-area cover stays near ${fmt.pct(now)}.`;
    return `${fmt.int(n)} trees would raise cover from ${fmt.pct(now)} to ${fmt.pct(later)} in 30 years.`;
  }

  function planReading(sc) {
    const street = state.street && state.street.street_id === sc.street_id ? state.street : state.streets.get(sc.street_id);
    const parts = [];
    if (street) {
      parts.push(`On ${street.name} (${fmt.grouped(street.length_m)} m), this plan proposes ${fmt.int(sc.summary.planted)} new trees.`);
    }
    const now = existingCover(sc);
    const later = cover30(sc);
    const sw = sidewalk30(sc);
    if (now != null && later != null) {
      const delta = later - now;
      parts.push(delta > 0.05
        ? `Study-area tree cover would go from ${fmt.pct(now)} with current trees to ${fmt.pct(later)} at year 30 (${points(delta)}).`
        : `Study-area tree cover stays near ${fmt.pct(now)} at year 30.`);
    }
    if (sw != null) {
      parts.push(`About ${fmt.pct(sw)} of sidewalk would sit under a crown at year 30 — that is cover, not afternoon shade.`);
    }
    const s = sc.summary;
    if (s.conditional || s.invalid) {
      parts.push(`${fmt.int(s.conditional)} positions need review; ${fmt.int(s.invalid)} are excluded and add no canopy.`);
    } else if (s.planted) {
      parts.push('No evaluated rule failed on the proposed positions. This is still a planning check, not planting approval.');
    }
    return parts.join(' ');
  }

  function planBriefing(sc) {
    const street = state.street && state.street.street_id === sc.street_id ? state.street : state.streets.get(sc.street_id);
    const pack = sc.rules_used;
    const now = existingCover(sc);
    const later = cover30(sc);
    const sw = sidewalk30(sc);
    const s = sc.summary;
    const source = street && street.layer_sources && street.layer_sources.existing_trees;
    const flags = failedReasonCounts(sc);
    const lines = [
      `Urban Green plan — ${street ? street.name : 'street'}`,
      street ? `${fmt.grouped(street.length_m)} m · ${fmt.int(street.stats && street.stats.existing_trees)} existing trees${source ? ` · ${source}` : ''}` : '',
      '',
      `Plan: ${displayPlanName(sc)}`,
      `Proposed trees: ${fmt.int(s.planted)} (no conflicts ${fmt.int(s.valid)} · needs review ${fmt.int(s.conditional)} · excluded ${fmt.int(s.invalid)})`,
      now != null && later != null ? `Tree cover: ${fmt.pct(now)} today → ${fmt.pct(later)} at 30 years` : `Tree cover at 30 years: ${fmt.pct(later)}`,
      sw != null ? `Sidewalk under crown at 30 years: ${fmt.pct(sw)}` : '',
      pack ? `Checks: ${pack.name}` : '',
    ];
    const other = previousStreetPlan(sc);
    if (other) lines.push(planDelta(sc, other));
    if (flags.length) {
      lines.push('', 'Most common flags:');
      for (const reason of flags.slice(0, 4)) {
        lines.push(`- ${ruleRequirement(reason.rule)[0]}: ${reason.included} amber, ${reason.count - reason.included} excluded`);
      }
    }
    lines.push('', 'Early planning estimate. Positions still need a site assessment, including underground utilities and root space.');
    return lines.filter((line, i, all) => line !== '' || all[i - 1] !== '').join('\n');
  }

  function samePlanParams(sc, params) {
    const p = sc.params || {};
    return p.spacing_m === params.spacing_m
      && p.side === params.side
      && p.mode === params.mode
      && p.species_id === params.species_id
      && Number(sc.species && sc.species.mature_crown_d_m) === Number(params.crown_diameter_m);
  }

  function failedReasonCounts(sc) {
    const reasons = new Map();
    for (const site of (sc.sites && sc.sites.features) || []) {
      const p = site.properties;
      if (p.verdict !== 'conditional' && p.verdict !== 'invalid') continue;
      for (const rule of p.rules || []) {
        if (rule.passed !== false) continue;
        const reason = reasons.get(rule.rule_id) || { rule, count: 0, included: 0, examples: [] };
        reason.count += 1;
        if (p.verdict === 'conditional') reason.included += 1;
        reason.examples.push(site);
        reasons.set(rule.rule_id, reason);
      }
    }
    const scored = Array.from(reasons.values()).sort((a, b) => b.count - a.count);
    for (const reason of scored) {
      reason.siteId = bestExampleSite(reason);
    }
    return scored;
  }

  function bestExampleSite(reason) {
    const ruleId = reason.rule.rule_id;
    const ranked = reason.examples.map((site) => {
      const fails = (site.properties.rules || []).filter((r) => r.passed === false);
      return {
        id: site.properties.site_id,
        score: (fails.length === 1 ? 0 : 8)
          + (site.properties.verdict === 'conditional' ? 0 : 3)
          + (fails.some((r) => r.rule_id !== ruleId && r.mode === 'must') ? 6 : 0),
      };
    }).sort((a, b) => a.score - b.score);
    return ranked[0] ? ranked[0].id : null;
  }

  function recommendedAlternativeKey(sc) {
    const s = sc.summary;
    if (s && s.planted === 0) return 'pack';
    if (s && s.invalid >= 15 && sc.params && sc.params.mode !== 'pack') return 'pack';
    const top = failedReasonCounts(sc)[0];
    if (!top) return null;
    const id = top.rule.rule_id;
    if (id === 'building_crown' || id === 'sidewalk_passage') return 'small';
    if (id === 'existing_tree' || id === 'junction' || id === 'junction_exclusion' || id === 'parking_exclusion' || id === 'plantable_surface') return 'pack';
    return null;
  }

  function planAlternatives(sc) {
    const p = sc.params || {};
    const crown = Number(sc.species && sc.species.mature_crown_d_m);
    const small = state.config && state.config.species.find((s) => s.id === 'carpinus_fastigiata');
    const specs = [];
    if (p.spacing_m > 6) {
      specs.push({
        key: 'denser',
        label: 'Closer trees · 6 m',
        name: 'Closer 6 m spacing',
        params: { spacing_m: 6, side: p.side, species_id: p.species_id, crown_diameter_m: crown, mode: p.mode },
      });
    }
    if (p.mode !== 'pack') {
      specs.push({
        key: 'pack',
        label: 'Fit around obstacles',
        name: 'Fit around obstacles',
        params: { spacing_m: p.spacing_m, side: p.side, species_id: p.species_id, crown_diameter_m: crown, mode: 'pack' },
      });
    }
    if (small && crown > Number(small.mature_crown_d_m) + 0.4) {
      specs.push({
        key: 'small',
        label: `Smaller trees · ${fmt.num(small.mature_crown_d_m, 0)} m hornbeam`,
        name: 'Narrow hornbeam',
        params: { spacing_m: p.spacing_m, side: p.side, species_id: small.id, crown_diameter_m: small.mature_crown_d_m, mode: p.mode },
      });
    }
    if (p.side === 'both') {
      specs.push({
        key: 'oneside',
        label: 'One side only',
        name: 'Left side only',
        params: { spacing_m: p.spacing_m, side: 'left', species_id: p.species_id, crown_diameter_m: crown, mode: p.mode },
      });
    }
    return specs.filter((spec) => !streetScenarios().some((other) => samePlanParams(other, spec.params)));
  }

  function applyPlanParams(params, label) {
    ui.spacing.value = String(params.spacing_m);
    ui.spacingOut.textContent = `${fmt.num(params.spacing_m, 1)} m`;
    if (params.species_id) ui.species.value = params.species_id;
    $('crown-size').value = String(params.crown_diameter_m);
    for (const name of ['side', 'mode']) {
      const input = ui.planForm.querySelector(`input[name="${name}"][value="${params[name]}"]`);
      if (input) input.checked = true;
    }
    ui.label.value = label || '';
    renderSpeciesHint();
    updatePlanDraft();
  }

  async function runAlternative(spec) {
    if (state.planning || state.loadingStreet || state.updatingRules) return;
    applyPlanParams(spec.params, spec.name);
    cancelAutoApply();
    const created = await runPlan({ ...formParams(), label: spec.name });
    if (created) {
      $('result-title').scrollIntoView({ behavior: REDUCED_MOTION.matches ? 'instant' : 'smooth', block: 'start' });
    }
  }

  function downloadBriefing(text, sc) {
    const street = state.street && state.street.street_id === sc.street_id ? state.street : state.streets.get(sc.street_id);
    const slug = (street && street.name ? street.name : 'plan').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
    const link = el('a', { href: url, download: `urban-green-${slug || 'plan'}.txt` });
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  async function copyBriefing() {
    const sc = activeScenario();
    if (!sc) return;
    const text = planBriefing(sc);
    let copied = false;
    try {
      await navigator.clipboard.writeText(text);
      copied = true;
    } catch (_) {
      const area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.left = '-9999px';
      document.body.append(area);
      area.select();
      copied = document.execCommand('copy');
      area.remove();
    }
    if (!copied) downloadBriefing(text, sc);
    ui.copyBrief.textContent = copied ? 'Briefing copied' : 'Briefing downloaded';
    ui.copyBrief.dataset.copied = 'true';
    window.setTimeout(() => {
      ui.copyBrief.textContent = 'Copy a short briefing';
      delete ui.copyBrief.dataset.copied;
    }, 2000);
  }

  function renderScenarios() {
    ui.scenariosMeta.textContent = state.scenarios.length ? String(state.scenarios.length) : '';
    const count = streetScenarios().length;
    ui.compareBtn.disabled = count < 2;
    ui.compareBtn.title = count < 2 ? 'Create a second plan for this street to compare alternatives' : 'Compare up to six plans for this street';
    $('compare-help').textContent = count < 2 ? 'Create a second plan for this street to compare alternatives.'
      : `${count} plans for ${state.street.name}. Compare ${count > 6 ? 'the selected plan and five recent alternatives' : 'these plans'} at 30 years.`;
    if (!state.scenarios.length) {
      ui.scenarios.replaceChildren(el('p', { class: 'hint', text: 'Create a plan, then change the settings to compare alternatives.' }));
      return;
    }
    ui.scenarios.replaceChildren(...state.scenarios.map((sc, i) => {
      const street = state.streets.get(sc.street_id);
      const foreign = !state.street || sc.street_id !== state.street.street_id;
      return el('button', {
        type: 'button', class: 'scenario', 'aria-pressed': String(sc.scenario_id === state.activeScenarioId),
        onclick: () => selectScenario(sc.scenario_id),
      }, [
        el('span', { class: 'scenario-label', text: displayPlanName(sc) }),
        el('span', { class: 'scenario-n mono', text: `#${i + 1}` }),
        el('span', { class: 'scenario-stats mono' }, [
          strong(fmt.int(sc.summary && sc.summary.planted)), document.createTextNode(' proposed · '),
          strong(fmt.pct(cover30(sc))),
          document.createTextNode(existingCover(sc) != null && cover30(sc) != null && cover30(sc) - existingCover(sc) > 0.05
            ? ` cover · ${points(cover30(sc) - existingCover(sc))}`
            : ' tree cover at 30 years'),
        ]),
        foreign ? el('span', { class: 'scenario-street', text: street ? street.name : sc.street_id }) : null,
      ]);
    }));
  }

  function renderPlanMeta(sc) {
    ui.resultEmpty.hidden = Boolean(sc);
    ui.resultContent.hidden = !sc;
    ui.planMeta.replaceChildren();
    $('review-reasons').replaceChildren();
    ui.planAlternatives.replaceChildren();
    ui.tryAlternatives.hidden = true;
    ui.flagSummary.replaceChildren();
    ui.flagSummary.hidden = true;
    ui.compareHere.hidden = true;
    ui.resultVs.hidden = true;
    if (!sc || !sc.summary) return;
    const s = sc.summary;
    const pack = sc.rules_used;
    const definitions = new Map(((pack && pack.rules) || []).map(r => [r.id, r]));
    const now = existingCover(sc);
    const later = cover30(sc);
    const sw = sidewalk30(sc);
    $('plan-rule-basis').textContent = pack ? `Checks use ${pack.name}, plus editable planning defaults.${state.street && state.street.city === 'zurich' && pack.jurisdiction === 'Berlin' ? ' This is not a Zürich-specific planting standard.' : ''}` : '';
    ui.resultName.textContent = displayPlanName(sc);
    ui.resultReading.textContent = planReading(sc);
    const other = previousStreetPlan(sc);
    ui.resultVs.hidden = !other;
    ui.resultVs.textContent = other ? planDelta(sc, other) : '';
    ui.resultTrees.textContent = fmt.int(s.planted);
    ui.resultCover.textContent = fmt.pct(later);
    ui.resultCoverWas.textContent = now == null || later == null ? ''
      : later - now > 0.05 ? `from ${fmt.pct(now)} today · ${points(later - now)}`
      : `from ${fmt.pct(now)} today`;
    const y10 = canopyYear(sc, 10);
    ui.resultGrowth.textContent = now != null && later != null && y10
      ? `Growth: ${fmt.pct(now)} today → ${fmt.pct(y10.cover_corridor_pct)} at 10 years → ${fmt.pct(later)} at 30 years.`
      : '';
    ui.resultSidewalk.textContent = sw == null ? ''
      : `${fmt.pct(sw)} of sidewalk would sit under a crown at year 30. Afternoon shade is a separate comparison below.`;
    if (state.street && state.street.street_id === sc.street_id && now != null) {
      const trees = state.street.stats && state.street.stats.existing_trees;
      $('existing-tree-count').textContent = `${fmt.int(trees)} existing trees · ${fmt.pct(now)} cover today`;
    }
    ui.planMeta.replaceChildren(...[
      ['valid', s.valid, 'clear'], ['conditional', s.conditional, 'to review'], ['invalid', s.invalid, 'excluded'],
    ].map(([verdict, count, label]) => el('span', { class: `result-verdict verdict-${verdict}`, text: `${count} ${label}` })));
    const reasons = failedReasonCounts(sc);
    if (reasons.length) $('review-reasons').replaceChildren(
      el('p', { class: 'hint', text: 'Amber positions remain proposed; red positions are excluded. A position can have more than one conflict.' }),
      ...reasons.map((reason) => {
        const editable = definitions.has(reason.rule.rule_id);
        return el('article', { class: 'review-reason' }, [
          el('strong', { text: ruleRequirement(reason.rule)[0] }),
          el('span', { text: ruleRequirement(reason.rule)[1] }),
          el('span', { class: 'hint', text: requirementSource(reason.rule, definitions.get(reason.rule.rule_id), pack) }),
          el('span', { class: 'hint', text: `${reason.included} amber · ${reason.count - reason.included} excluded` }),
          el('div', { class: 'review-reason-actions' }, [
            el('button', {
              type: 'button', class: 'btn btn-sm',
              onclick: () => inspectFlag(reason),
              text: 'Inspect an example',
            }),
            editable
              ? el('button', { type: 'button', class: 'btn btn-sm btn-ghost', onclick: () => focusRestriction(reason.rule.rule_id), text: 'Change this limit' })
              : null,
          ]),
        ]);
      }),
    );
    ui.flagSummary.replaceChildren();
    ui.flagSummary.hidden = !reasons.length;
    if (reasons.length) {
      ui.flagSummary.replaceChildren(...reasons.slice(0, 3).map((reason) => el('button', {
        type: 'button',
        class: 'flag-chip',
        text: `${ruleRequirement(reason.rule)[0]} · ${reason.included} amber · ${reason.count - reason.included} excluded`,
        onclick: () => inspectFlag(reason),
      })));
    }
    const alternatives = planAlternatives(sc);
    const recommended = recommendedAlternativeKey(sc);
    if (alternatives.length) {
      ui.tryAlternatives.hidden = false;
      ui.planAlternatives.replaceChildren(...alternatives.map((spec) => el('button', {
        type: 'button',
        class: spec.key === recommended ? 'chip chip-recommend' : 'chip',
        text: spec.key === recommended ? `${spec.label} · suggested` : spec.label,
        onclick: () => runAlternative(spec),
      })));
    }
    const peers = streetScenarios().length;
    ui.compareHere.hidden = peers < 2;
    ui.compareHere.textContent = peers > 2 ? 'Compare with your other plans' : 'Compare with your other plan';
    ui.resultGuidance.textContent = s.planted === 0
      ? 'No proposed trees fit these settings. Try a suggested plan below, or choose a smaller tree in Adjust the trees.'
      : `${s.conditional ? 'Amber rings are included in the proposed tree count and canopy estimate. Red rings are excluded. ' : ''}Click a flag or a coloured ring to see the measured clearance.`;
  }

  function renderWorkflow() {
    const applying = state.autoApply.pending || state.autoApply.running;
    const busy = state.loadingStreet || state.planning || state.updatingRules || state.agent.streaming || applying;
    const editingBusy = state.loadingStreet || state.agent.streaming || ((state.planning || state.updatingRules) && !state.autoApply.running);
    const rulesPending = activeScenario() && state.pack && JSON.stringify(state.pack.rules) !== JSON.stringify(activeScenario().rules_used?.rules);
    const hasPlan = Boolean(activeScenario());
    $('settings-card').hidden = !state.street;
    $('review-card').hidden = !hasPlan;
    $('temperature-card').hidden = !hasPlan;
    $('plans-card').hidden = !state.scenarios.length;
    ui.rulesCard.hidden = !state.street;
    if (ui.restrictionPreview) ui.restrictionPreview.hidden = Boolean(state.street);
    document.querySelector('.data-details').hidden = !state.street;
    ui.fitStreet.hidden = !state.street;
    $('back-to-plan').textContent = state.temperature.mapVisible ? 'Cooling details' : state.street ? 'Back to settings' : 'Back to street selection';
    $('growth-dock').hidden = !hasPlan || state.draw.active || state.pickingStreet || state.temperature.mapVisible;
    $('map-key').hidden = !state.street || state.draw.active || state.pickingStreet || state.temperature.mapVisible;
    $('map-layers').hidden = state.temperature.mapVisible;
    $('map-key-verdicts').hidden = !hasPlan;
    $('review-checks').hidden = !$('review-reasons').childElementCount;
    $('refresh-tree-data').disabled = busy;
    const canCompareTemperature = Boolean(activeScenario()) && !state.dirty && !rulesPending;
    $('temperature-btn').disabled = busy || !canCompareTemperature || state.temperature.pending;
    for (const input of $('temperature-form').querySelectorAll('input, select')) input.disabled = busy || !activeScenario();
    $('temperature-map').disabled = busy || !state.temperature.result;
    $('temperature-view').disabled = busy || !state.temperature.result;
    $('temperature-point').disabled = busy || !state.temperature.result;
    ui.city.disabled = busy || !state.config;
    ui.search.disabled = busy || !state.config;
    ui.searchBtn.disabled = busy || !state.config;
    ui.searchBtn.textContent = state.loadingStreet ? 'Loading…' : 'Explore street';
    ui.searchForm.hidden = false;
    $('street-selection-help').textContent = 'Click a street on the map, search by name, or draw your own section.';
    ui.draw.disabled = busy || !state.config;
    ui.draw.hidden = state.draw.active;
    $('pick-street').hidden = state.draw.active;
    $('pick-street').disabled = busy || !state.config;
    $('pick-street').setAttribute('aria-pressed', String(state.pickingStreet));
    $('pick-street').textContent = state.loadingStreet && state.pickingStreet ? 'Finding street…' : state.pickingStreet ? 'Cancel street selection' : 'Select street on map';
    $('pick-street').title = '';
    for (const chip of ui.demoChips.children) chip.disabled = busy;
    for (const scenario of ui.scenarios.querySelectorAll('button')) scenario.disabled = busy;
    ui.compareBtn.disabled = busy || streetScenarios().length < 2;
    ui.rules.inert = editingBusy;
    ui.rulesReset.disabled = busy;
    ui.gotoTrees.disabled = busy || !state.street;
    ui.editRestrictions.disabled = busy || !state.street;
    if (ui.rulesPending) {
      ui.rulesPending.hidden = !rulesPending;
      ui.rulesPending.textContent = rulesPending
        ? state.autoApply.failed ? 'Update failed. Retry to apply these restrictions.' : 'Updating the plan with these restrictions…'
        : '';
    }
    for (const chip of ui.planAlternatives.querySelectorAll('button')) chip.disabled = busy;
    for (const chip of ui.flagSummary.querySelectorAll('button')) chip.disabled = busy;
    ui.copyBrief.disabled = busy || !hasPlan;
    ui.compareHere.disabled = busy || streetScenarios().length < 2;
    ui.send.disabled = busy;
    for (const chip of ui.promptChips.children) chip.disabled = busy;
    for (const input of ui.planForm.querySelectorAll('input, select')) input.disabled = editingBusy || !state.street;
    ui.planBtn.disabled = busy || !state.street;
    ui.planBtn.textContent = applying ? 'Updating plan…' : state.autoApply.failed || state.dirty || rulesPending ? 'Retry update' : 'Duplicate plan';
    ui.planBtn.classList.toggle('btn-primary', !hasPlan || state.autoApply.failed);
    if (!activeScenario() && !state.planning) ui.planBtn.textContent = 'Create tree plan';
    ui.fitStreet.disabled = !state.street;
    for (const control of [ui.year, ui.play, ui.shade]) control.disabled = !activeScenario() || busy;
    ui.workflowStatus.textContent = state.loadingStreet ? 'Loading the street and checking available data…'
      : applying ? 'Updating the plan…'
      : state.planning ? 'Checking planting rules and estimating tree cover…'
      : state.updatingRules ? 'Updating the planting rules…'
      : state.agent.streaming ? 'The assistant is working on your request…'
      : state.status.error ? 'Could not complete this step. Try again or choose another example street.'
      : state.autoApply.failed ? 'Changes could not be applied. Check the values and retry.'
      : rulesPending || state.dirty ? 'Updating the plan…'
      : activeScenario() ? `Plan ready. ${planHeadline(activeScenario())}${streetScenarios().length > 1 ? ' Compare with your other plan.' : ''}`
      : state.street ? 'Street ready. Review the planting restrictions, then create a plan.' : 'Choose a street to start.';
    ui.planHint.textContent = state.autoApply.failed ? 'The map still shows the last completed plan. Check the values and retry.'
      : applying ? 'Changes apply automatically. Updating…'
      : hasPlan ? 'Changes apply automatically. Duplicate a plan to keep a comparison.'
      : 'Create a plan to check planting positions and estimate tree cover.';
    ui.planHint.hidden = !ui.planHint.textContent;
    ui.mapHint.textContent = state.loadingStreet && state.pickingStreet ? 'Finding the clicked street and loading its data…'
      : state.pickingStreet ? 'Click a street to start a plan.'
      : state.draw.active ? 'Click at least two points on the map. Then choose Finish drawing. Escape cancels.' : activeScenario()
      ? state.temperature.mapVisible ? `${state.street.name} · Select a sidewalk point to see its cooling.` : `${state.street.name} · Click a tree to inspect it.`
      : 'Explore a street to see possible tree positions.';
    ui.workflowStatus.classList.toggle('is-pending', busy || state.dirty || Boolean(rulesPending));
    ui.workflowStatus.hidden = !busy && !state.dirty && !rulesPending && !state.status.error;
    const sc = activeScenario();
    $('review-result').hidden = true;
    if (sc) {
      const now = existingCover(sc);
      const later = cover30(sc);
      $('review-result').textContent = now != null && later != null
        ? `${fmt.int(sc.summary.planted)} proposed · ${fmt.pct(now)} → ${fmt.pct(later)} cover · Review ↓`
        : `${fmt.int(sc.summary.planted)} proposed · ${fmt.pct(later)} cover at 30 years · Review ↓`;
    }
  }

  function toggleAssistant(open) {
    ui.agent.hidden = !open;
    ui.assistantToggle.setAttribute('aria-expanded', String(open));
    if (open) (ui.chatForm.hidden ? ui.assistantClose : ui.chatInput).focus();
    else ui.assistantToggle.focus();
  }

  function invalidateTemperature(message = 'Settings changed. Update the cooling comparison.') {
    setTemperatureMapVisible(false);
    state.temperature.seq += 1;
    state.temperature.pending = false;
    state.temperature.result = null;
    state.temperature.selectedId = '';
    setSourceData('temperature', emptyFc());
    setSourceData('temperature-selected', emptyFc());
    $('temperature-result').hidden = true;
    $('temperature-settings').open = true;
    $('temperature-settings-summary').textContent = 'Comparison settings';
    $('temperature-btn').textContent = 'Calculate cooling';
    $('temperature-status').textContent = message;
  }

  const temperatureSamples = () => state.temperature.result?.samples?.features || [];
  const temperatureLocation = (p) => `${p.side === 'left' ? 'Left' : 'Right'} sidewalk · ${fmt.int(p.station_m)} m from start`;
  const coolingText = (value) => value > 0 ? `${fmt.num(value, 2)}°C cooler` : '0.00°C change';

  function temperaturePointCard(p) {
    return el('div', { class: 'pop' }, [
      el('strong', { text: temperatureLocation(p) }),
      el('strong', { class: 'cooling-popup-value', text: coolingText(p.cooling_c) }),
      el('span', { text: `${fmt.num(p.reference_air_c, 2)}°C input → ${fmt.num(p.proposed_air_c, 2)}°C estimated` }),
      el('span', { class: 'hint', text: 'Air estimate from nearby canopy · not a measurement' }),
    ]);
  }

  /** Use a separate map mode so rule colors cannot be mistaken for temperature. */
  function setTemperatureMapVisible(visible) {
    const t = state.temperature;
    visible = Boolean(visible && t.result);
    if (visible === t.mapVisible) return;
    t.mapVisible = visible;
    hideHover();
    if (state.pinPopup) state.pinPopup.remove();
    if (visible) {
      stopAnimation();
      t.savedLayers = ['existing-crowns', 'existing-marks', 'crowns', 'sites', 'sites-ring', 'shade-fill', 'junctions-circle', 'parking-fill', 'parking-line']
        .filter(id => map.getLayer(id)).map(id => [id, map.getLayoutProperty(id, 'visibility') || 'visible']);
      for (const [id] of t.savedLayers) map.setLayoutProperty(id, 'visibility', 'none');
    } else {
      for (const [id, visibility] of t.savedLayers) if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', visibility);
      t.savedLayers = [];
    }
    if (map.getLayer('basemap')) {
      map.setPaintProperty('basemap', 'raster-saturation', visible ? -0.8 : 0);
      map.setPaintProperty('basemap', 'raster-brightness-max', visible ? 0.55 : 1);
    }
    for (const id of ['temperature-points', 'temperature-selected']) {
      if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', visible ? 'visible' : 'none');
    }
    $('temperature-map-key').hidden = !visible;
    document.querySelector('.map-workspace').classList.toggle('is-cooling', visible);
    $('temperature-view').setAttribute('aria-pressed', String(visible));
    renderWorkflow();
  }

  function showCoolingMap() {
    if (!state.temperature.result) return;
    if (state.draw.active) stopDraw();
    setStreetPicking(false);
    setTemperatureMapVisible(true);
    ui.map.scrollIntoView({ behavior: REDUCED_MOTION.matches ? 'instant' : 'smooth', block: 'center' });
    if (state.temperature.selectedId) selectTemperaturePoint(state.temperature.selectedId, { focusMap: true, pin: true });
    else fitToBbox(state.street.bbox);
  }

  function selectTemperaturePoint(id, { focusMap = false, pin = false } = {}) {
    const feature = temperatureSamples().find(f => f.properties.sample_id === id);
    state.temperature.selectedId = feature ? id : '';
    setSourceData('temperature-selected', { type: 'FeatureCollection', features: feature ? [feature] : [] });
    renderTemperatureReading();
    if (focusMap && feature) {
      setStreetPicking(false);
      setTemperatureMapVisible(true);
      map.easeTo({ center: feature.geometry.coordinates, padding: fitPadding(), zoom: Math.max(map.getZoom(), 17.5), duration: motionMs(400) });
    }
    if (state.pinPopup) state.pinPopup.remove();
    if (pin && feature) pinPopup(feature.geometry.coordinates, temperaturePointCard(feature.properties));
  }

  /** A small profile of real samples; the selector and map expose every point. */
  function renderTemperatureProfile() {
    const samples = temperatureSamples();
    const length = Math.max(1, state.street.length_m);
    $('temperature-point').replaceChildren(el('option', { value: '', text: 'Street average' }),
      ...samples.map(f => el('option', { value: f.properties.sample_id, text: temperatureLocation(f.properties) })));
    const rows = ['left', 'right'].map(side => {
      const sideSamples = samples.filter(f => f.properties.side === side).sort((a, b) => a.properties.station_m - b.properties.station_m);
      if (!sideSamples.length) return null;
      const count = Math.min(20, sideSamples.length);
      const preview = Array.from({ length: count }, (_, i) => sideSamples[Math.round(i * (sideSamples.length - 1) / Math.max(1, count - 1))]);
      return el('div', { class: 'cooling-profile-row' }, [
        el('span', { class: 'hint', text: side === 'left' ? 'Left' : 'Right' }),
        el('div', { class: 'cooling-profile-bars' }, preview.map(f => {
          const p = f.properties;
          const label = `${temperatureLocation(p)} · ${coolingText(p.cooling_c)}`;
          return el('button', {
            type: 'button', class: 'temperature-profile-point', 'data-sample-id': p.sample_id,
            'aria-label': label, title: label, 'aria-pressed': 'false',
            style: `--cooling-height: ${Math.max(5, Math.min(100, p.cooling_c / 0.5 * 100))}%; left: ${Math.max(0, Math.min(97, p.station_m / length * 97))}%`,
            onclick: () => selectTemperaturePoint(p.sample_id, { focusMap: true, pin: true }),
          });
        })),
      ]);
    }).filter(Boolean);
    $('temperature-profile').replaceChildren(...rows,
      el('p', { class: 'hint cooling-profile-labels', text: 'Street start → end · taller bars = more cooling' }));
  }

  function renderTemperatureReading() {
    const result = state.temperature.result;
    if (!result) return;
    const feature = temperatureSamples().find(f => f.properties.sample_id === state.temperature.selectedId);
    const p = feature ? feature.properties : result;
    $('temperature-location').textContent = feature ? temperatureLocation(p) : 'Street average';
    $('temperature-point').value = state.temperature.selectedId;
    $('temperature-average').hidden = !feature;
    $('temperature-before').textContent = `${fmt.num(p.reference_air_c, 2)}°C`;
    $('temperature-after').textContent = `${fmt.num(p.proposed_air_c, 2)}°C`;
    $('temperature-delta').textContent = coolingText(p.cooling_c);
    $('temperature-local-note').textContent = feature
      ? 'Based on added canopy within 10 m of this point. The same input temperature is used at every point.'
      : 'Cooling varies along the street. Select a sidewalk point below or on the map.';
    $('temperature-range').textContent = `Sensitivity: ${fmt.num(p.cooling_low_c, 2)}–${fmt.num(p.cooling_high_c, 2)}°C cooler (${fmt.num(p.proposed_air_low_c, 2)}–${fmt.num(p.proposed_air_high_c, 2)}°C). Not a forecast interval; actual cooling can fall outside it.`;
    $('temperature-canopy').textContent = `${feature ? 'Canopy within 10 m of this point' : `Mean canopy around ${result.sample_count} sidewalk points`}: ${fmt.pct(p.existing_local_canopy_pct)} → ${fmt.pct(p.proposed_local_canopy_pct)}.`;
    $('temperature-shade').textContent = feature
      ? `Tree shade here: ${p.existing_tree_shade ? 'shaded' : 'unshaded'} → ${p.proposed_tree_shade ? 'shaded' : 'unshaded'} · ${result.when}`
      : `Sidewalk in tree shade: ${fmt.pct(result.existing_sidewalk_shade_pct)} → ${fmt.pct(result.proposed_sidewalk_shade_pct)} · ${result.when}`;
    $('temperature-map-caption').textContent = `Estimated air cooling · year ${result.year}`;
    for (const button of $('temperature-profile').querySelectorAll('[data-sample-id]')) {
      button.setAttribute('aria-pressed', String(button.dataset.sampleId === state.temperature.selectedId));
    }
  }

  async function compareTemperature({ restore = null } = {}) {
    const scenario = activeScenario();
    if (!scenario || state.temperature.pending || (!restore && $('temperature-btn').disabled)) return;
    const form = $('temperature-form');
    if (!form.reportValidity()) return;
    const params = {
      scenario_id: scenario.scenario_id,
      reference_air_c: Number($('reference-air').value), year: Number($('temperature-year').value),
      month: Number($('temperature-month').value), day: Number($('temperature-day').value), hour: Number($('temperature-hour').value),
    };
    invalidateTemperature('Comparing current trees and proposed trees…');
    const seq = state.temperature.seq;
    state.temperature.pending = true;
    $('temperature-btn').textContent = 'Calculating comparison…';
    renderWorkflow();
    try {
      const result = await api('/api/temperature', { method: 'POST', body: params });
      if (seq !== state.temperature.seq || result.scenario_id !== state.activeScenarioId) return;
      state.temperature.result = result;
      $('temperature-settings').open = false;
      $('temperature-settings-summary').textContent = `Settings · ${fmt.num(result.reference_air_c, 1)}°C · year ${result.year}`;
      $('temperature-context').textContent = `${state.street.name} · year ${result.year}`;
      setSourceData('temperature', result.samples);
      renderTemperatureProfile();
      renderTemperatureReading();
      $('temperature-limitations').replaceChildren(...result.limitations.map(text => el('li', { text })));
      $('temperature-status').textContent = '';
      $('temperature-result').hidden = false;
      if (restore) {
        selectTemperaturePoint(restore.selectedId);
        setTemperatureMapVisible(restore.mapVisible);
      } else {
        setStreetPicking(false);
        setTemperatureMapVisible(true);
        fitToBbox(state.street.bbox);
        $('temperature-title').scrollIntoView({ behavior: REDUCED_MOTION.matches ? 'instant' : 'smooth', block: 'start' });
      }
    } catch (err) {
      if (seq === state.temperature.seq) $('temperature-status').textContent = `Could not compare temperatures: ${err.message}`;
    } finally {
      if (seq === state.temperature.seq) {
        state.temperature.pending = false;
        $('temperature-btn').textContent = state.temperature.result ? 'Update comparison' : 'Calculate cooling';
        renderWorkflow();
      }
    }
  }

  function renderExport() {
    const id = state.activeScenarioId;
    ui.exportLink.href = id ? `/api/export/${encodeURIComponent(id)}.geojson` : '#';
    ui.exportLink.setAttribute('aria-disabled', String(!id));
    if (id) ui.exportLink.setAttribute('download', `canopy-${id}.geojson`);
  }

  /** Compare the selected plan with recent alternatives for the same street. */
  async function openCompare() {
    const selected = activeScenario();
    const alternatives = streetScenarios().filter((s) => s !== selected).slice(-5);
    const ids = [...(selected ? [selected] : []), ...alternatives].map((s) => s.scenario_id);
    if (ids.length < 2) { setError('Create a second plan for this street to compare.'); return; }
    setBusy(ui.compareBtn, true);
    try {
      showCompare(await api('/api/compare', { method: 'POST', body: { scenario_ids: ids } }));
      clearError();
    } catch (err) {
      setError(`Compare: ${err.message}`);
    } finally {
      setBusy(ui.compareBtn, false);
    }
  }

  function compareInsight(res) {
    const rows = res.rows || [];
    if (rows.length < 2 || !res.best_by_cover) return '';
    const best = rows.find((r) => r.scenario_id === res.best_by_cover);
    const current = rows.find((r) => r.scenario_id === state.activeScenarioId) || rows[0];
    if (!best || !current) return '';
    const nameOf = (row) => {
      const local = state.scenarios.find((s) => s.scenario_id === row.scenario_id);
      return local ? displayPlanName(local) : row.label;
    };
    if (best.scenario_id === current.scenario_id) {
      const other = rows.find((r) => r.scenario_id !== best.scenario_id);
      const extraFlags = other && (best.conditional + best.invalid) > (other.conditional + other.invalid);
      return extraFlags
        ? `${nameOf(best)} has the highest 30-year tree cover (${fmt.pct(best.cover_corridor_pct_30)}), with more positions that need review or are excluded.`
        : `${nameOf(best)} has the highest 30-year tree cover (${fmt.pct(best.cover_corridor_pct_30)}).`;
    }
    const dCover = best.cover_corridor_pct_30 - current.cover_corridor_pct_30;
    const dTrees = best.planted - current.planted;
    const treeBit = dTrees === 0 ? ''
      : dTrees > 0 ? ` and ${fmt.int(dTrees)} more proposed trees`
      : ` and ${fmt.int(-dTrees)} fewer proposed trees`;
    return `${nameOf(best)} has ${points(dCover)} more cover than ${nameOf(current)}${treeBit}.`;
  }

  function showCompare(res) {
    const planName = (r) => {
      const local = state.scenarios.find((s) => s.scenario_id === r.scenario_id);
      return local ? displayPlanName(local) : r.label;
    };
    const coverGain = (r) => {
      const now = existingCover(activeScenario());
      return now == null ? fmt.pct(r.cover_corridor_pct_30) : `${fmt.pct(r.cover_corridor_pct_30)} (${points(r.cover_corridor_pct_30 - now)})`;
    };
    const cols = [
      ['Plan', planName],
      ['Street / data', (r) => `${r.street_name || state.streets.get(r.street_id)?.name || 'Unknown street'} · ${r.city_name || cityById(r.city)?.name || r.city || 'See street sources'}`],
      ['Proposed trees', (r) => fmt.int(r.planted)],
      ['Tree cover at 30 years', coverGain],
      ['Amber', (r) => fmt.int(r.conditional)], ['Excluded', (r) => fmt.int(r.invalid)],
    ];
    const details = [
      ['Plan', planName],
      ['Spacing', (r) => `${fmt.num(r.spacing_m, 1)} m`], ['Side', (r) => r.side],
      ['Species', (r) => speciesShort(r.species_id)], ['Placement', (r) => r.mode === 'pack' ? 'Fit around obstacles' : 'Even spacing'],
      ['Mature crown diameter', (r) => `${fmt.num(r.crown_diameter_m, 1)} m`],
      ['No conflicts', (r) => fmt.int(r.valid)],
      ['New crown area', (r) => `${fmt.grouped(r.new_crown_area_30_m2)} m²`],
      ['Street 30 y', (r) => fmt.pct(r.cover_street_pct_30)], ['Sidewalk 30 y', (r) => fmt.pct(r.sidewalk_under_crown_pct_30)],
      ['Existing-tree source', (r) => r.tree_source || 'See street sources'],
    ];
    const renderTable = (columns, table) => {
      const choose = async (id) => { if (await selectScenarioById(id)) closeCompare(); };
      const head = el('thead', {}, el('tr', {}, columns.map(([h]) => el('th', { scope: 'col', text: h }))));
      const body = el('tbody', {}, (res.rows || []).map((r) => el('tr', {
        class: [r.scenario_id === res.best_by_cover ? 'best' : '', r.scenario_id === state.activeScenarioId ? 'active' : ''].join(' ').trim(),
        tabindex: '0',
        onclick: () => choose(r.scenario_id),
        onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); choose(r.scenario_id); } },
      }, columns.map(([, get]) => el('td', { text: get(r) })))));
      table.replaceChildren(head, body);
    };
    renderTable(cols, ui.compareTable);
    renderTable(details, $('compare-details-table'));
    ui.compareNote.textContent = res.best_by_cover
      ? 'Estimates at year 30 for the same street. Highlighted row has the highest study area tree cover, including amber positions. Select a row to show it on the map.'
      : 'Estimates at year 30. Different streets or data snapshots are not ranked. Select a row to show it on the map.';
    ui.compareInsight.textContent = compareInsight(res);
    const baseline = activeScenario() && existingCover(activeScenario());
    if (baseline != null && res.best_by_cover) {
      ui.compareInsight.textContent = [ui.compareInsight.textContent, `Current trees cover ${fmt.pct(baseline)} of the study area.`]
        .filter(Boolean).join(' ');
    }
    ui.compareInsight.hidden = !ui.compareInsight.textContent;
    ui.compareModal.hidden = false;
    document.querySelector('.workspace').inert = true;
    document.querySelector('.app-header').inert = true;
    ui.agent.inert = true;
    ui.compareClose.focus();
  }

  function closeCompare() {
    if (ui.compareModal.hidden) return;
    ui.compareModal.hidden = true;
    document.querySelector('.workspace').inert = false;
    document.querySelector('.app-header').inert = false;
    ui.agent.inert = false;
    (ui.compareBtn.disabled ? ui.assistantClose : ui.compareBtn).focus();
  }

  function speciesShort(id) {
    const sp = state.config && state.config.species.find((s) => s.id === id);
    return sp ? sp.name_lat.replace(/\s*'.*$/, '') : id;
  }

  function speciesEnglish(id) {
    const sp = state.config && state.config.species.find((s) => s.id === id);
    return sp ? sp.name_en : speciesShort(id);
  }

  function autoPlanName(params, species) {
    const p = params || {};
    const sp = species || (state.config && state.config.species.find((s) => s.id === p.species_id));
    const name = (sp && sp.name_en) || speciesEnglish(p.species_id);
    const side = p.side === 'both' ? 'both sides' : `${p.side} side`;
    let label = `${name} · ${fmt.num(p.spacing_m, 1)} m · ${side}`;
    if (p.mode === 'pack') label += ' · fit around obstacles';
    const usual = sp && sp.mature_crown_d_m;
    if (p.crown_diameter_m != null && usual != null && Math.abs(Number(p.crown_diameter_m) - Number(usual)) > 0.4) {
      label += ` · ${fmt.num(p.crown_diameter_m, 0)} m crown`;
    }
    return label.slice(0, 60);
  }

  function displayPlanName(sc) {
    if (!sc) return '';
    const custom = (sc.label || '').trim();
    if (custom && !isGeneratedPlanName(custom)) return custom;
    return autoPlanName(sc.params || {}, sc.species);
  }

  function isGeneratedPlanName(name) {
    return / · (both|left|right)( sides?)? · (grid|pack)$/.test(name)
      || / m crown · /.test(name)
      || / · both sides$/.test(name)
      || / · fit around obstacles$/.test(name)
      || ['Closer 6 m spacing', 'Fit around obstacles', 'Narrow hornbeam', 'Left side only'].includes(name);
  }

  function previousStreetPlan(sc) {
    const list = streetScenarios();
    const i = list.findIndex((s) => s.scenario_id === sc.scenario_id);
    if (i > 0) return list[i - 1];
    return list.find((s) => s.scenario_id !== sc.scenario_id) || null;
  }

  function planDelta(sc, other) {
    if (!sc || !other || !sc.summary || !other.summary) return '';
    const trees = sc.summary.planted - other.summary.planted;
    const cover = (cover30(sc) ?? 0) - (cover30(other) ?? 0);
    const amber = sc.summary.conditional - other.summary.conditional;
    const excluded = sc.summary.invalid - other.summary.invalid;
    const bits = [];
    if (trees) bits.push(trees > 0 ? `${fmt.int(trees)} more trees` : `${fmt.int(-trees)} fewer trees`);
    if (Math.abs(cover) > 0.05) bits.push(`${points(cover)} cover`);
    if (amber) bits.push(amber > 0 ? `${fmt.int(amber)} more amber` : `${fmt.int(-amber)} fewer amber`);
    if (excluded) bits.push(excluded > 0 ? `${fmt.int(excluded)} more excluded` : `${fmt.int(-excluded)} fewer excluded`);
    if (!bits.length) bits.push('same tree count and cover');
    let note = `Compared with ${displayPlanName(other)}: ${bits.join(', ')}.`;
    const crownA = Number(sc.species && sc.species.mature_crown_d_m);
    const crownB = Number(other.species && other.species.mature_crown_d_m);
    if (Math.abs(trees) <= 2 && Math.abs(crownA - crownB) >= 1) {
      note += ' Tree count is almost unchanged because most exclusions are junctions and parking, not crown size.';
    }
    return note;
  }

  function inspectFlag(reason) {
    $('review-checks').open = true;
    ui.map.scrollIntoView({ behavior: REDUCED_MOTION.matches ? 'instant' : 'smooth', block: 'center' });
    focusSite(reason.siteId, 18, reason.rule.rule_id);
  }

  // ------------------------------------------------------------ year dock & shade
  /** Canopy cover (% of corridor) at a fractional year, linear between CanopyYear rows. */
  function coverAt(sc, year) {
    const rows = ((sc.canopy && sc.canopy.years) || []).slice().sort((a, b) => a.year - b.year);
    if (!rows.length) return null;
    if (year <= rows[0].year) return rows[0].cover_corridor_pct;
    for (let i = 1; i < rows.length; i += 1) {
      if (year <= rows[i].year) {
        const a = rows[i - 1], b = rows[i];
        const t = (year - a.year) / (b.year - a.year || 1);
        return a.cover_corridor_pct + (b.cover_corridor_pct - a.cover_corridor_pct) * t;
      }
    }
    return rows[rows.length - 1].cover_corridor_pct;
  }

  function renderYearReadout() {
    const sc = activeScenario();
    const year = Math.round(state.year);
    if (!sc) { ui.readout.replaceChildren(el('span', {}, [document.createTextNode('Year '), strong(String(year)), document.createTextNode(' · no plan yet')])); return; }
    const now = existingCover(sc);
    ui.readout.replaceChildren(
      document.createTextNode('Year '), strong(String(year)),
      document.createTextNode(' · tree cover '), strong(fmt.pct(coverAt(sc, state.year))),
      now == null ? document.createTextNode('') : document.createTextNode(` · ${fmt.pct(now)} today`),
      document.createTextNode(' · '), strong(fmt.int(sc.summary && sc.summary.planted)),
      document.createTextNode(' trees'),
    );
  }

  function setYear(year, { fromSlider = false } = {}) {
    state.year = Math.min(MAX_YEAR, Math.max(0, Number(year) || 0));
    if (!fromSlider) ui.year.value = String(state.year);
    if (map.getLayer('crowns')) map.setPaintProperty('crowns', 'circle-radius', radiusExpr(crownMetresExpr(state.year)));
    renderYearReadout();
    if (state.shade.enabled) { invalidateShade(); scheduleShade(); }
  }

  function stopAnimation() {
    if (state.anim) cancelAnimationFrame(state.anim);
    state.anim = 0;
    ui.play.setAttribute('aria-pressed', 'false');
    ui.play.setAttribute('aria-label', 'Play crown growth');
  }

  /** Grow crowns from the current year to 30 in ~4 s; reduced motion jumps straight there. */
  function togglePlay() {
    if (state.anim) { stopAnimation(); return; }
    if (REDUCED_MOTION.matches) { setYear(MAX_YEAR); return; }
    const from = state.year >= MAX_YEAR ? 0 : state.year;
    const duration = (ANIM_MS * (MAX_YEAR - from)) / MAX_YEAR;
    const start = performance.now();
    ui.play.setAttribute('aria-pressed', 'true');
    ui.play.setAttribute('aria-label', 'Pause crown growth');
    const step = (now) => {
      const y = from + (MAX_YEAR - from) * Math.min(1, (now - start) / duration);
      setYear(y);
      if (y < MAX_YEAR) state.anim = requestAnimationFrame(step); else stopAnimation();
    };
    state.anim = requestAnimationFrame(step);
  }

  function setShadeEnabled(on) {
    setTemperatureMapVisible(false);
    state.shade.enabled = on;
    ui.shade.setAttribute('aria-pressed', String(on));
    ui.shade.lastChild.textContent = on ? 'Hide shade' : 'Show shade';
    const box = ui.legendToggles.find((cb) => cb.dataset.layer === 'shade');
    if (box) box.checked = on;
    if (map.getLayer('shade-fill')) map.setLayoutProperty('shade-fill', 'visibility', on ? 'visible' : 'none');
    invalidateShade();
    if (on) fetchShade();
  }

  function invalidateShade() {
    clearTimeout(state.shade.timer);
    state.shade.seq += 1;
    state.shade.result = null;
    setSourceData('shade', emptyFc());
    renderShadeInfo(null);
  }

  function scheduleShade() {
    clearTimeout(state.shade.timer);
    state.shade.timer = setTimeout(fetchShade, SHADE_DEBOUNCE_MS);
  }

  /** POST /api/shade for the active scenario at the current (rounded) year. */
  async function fetchShade() {
    const sc = activeScenario();
    if (!sc || !state.shade.enabled) return;
    const seq = ++state.shade.seq;
    const body = { scenario_id: sc.scenario_id, year: Math.round(state.year), ...state.shade.when, include_existing: true };
    ui.shadeInfo.textContent = 'Calculating shade…';
    try {
      const res = await api('/api/shade', { method: 'POST', body });
      if (seq === state.shade.seq && state.shade.enabled && sc.scenario_id === state.activeScenarioId && body.year === Math.round(state.year)) applyShade(res);
    } catch (err) {
      if (seq === state.shade.seq) { renderShadeInfo(null); setError(`Shade: ${err.message}`); }
    }
  }

  function applyShade(res) {
    if (res.scenario_id !== state.activeScenarioId) return;
    invalidateShade();
    state.year = res.year;
    ui.year.value = String(res.year);
    if (map.getLayer('crowns')) map.setPaintProperty('crowns', 'circle-radius', radiusExpr(crownMetresExpr(state.year)));
    renderYearReadout();
    const when = /\d{4}-(\d{2})-(\d{2}) (\d{2}):(\d{2})/.exec(res.when || '');
    if (when) state.shade.when = { month: Number(when[1]), day: Number(when[2]), hour: Number(when[3]) + Number(when[4]) / 60 };
    state.shade.result = res;
    setSourceData('shade', res.shadows || emptyFc());
    renderShadeInfo(res);
  }

  function renderShadeInfo(res) {
    if (!res) { ui.shadeInfo.textContent = ''; return; }
    const when = String(res.when || '').replace(/^\d{4}-/, '').replace(' local', '');
    ui.shadeInfo.replaceChildren(
      document.createTextNode(`${when} · sun ${fmt.num(res.sun_elevation_deg, 0)}° · sidewalk `),
      strong(fmt.pct(res.shaded_sidewalk_pct)), document.createTextNode(' shaded'),
    );
  }

  // ------------------------------------------------------------ rules
  const RESTRICTION_SHORT = {
    carriageway_edge: 'Road',
    cycleway: 'Cycle path',
    building_crown: 'Buildings',
    plantable_surface: 'Plantable surface',
    existing_tree: 'Existing trees',
    junction: 'Junctions',
    sidewalk_passage: 'Sidewalk width',
  };
  const LOCKED_RESTRICTIONS = [
    {
      id: 'junction_exclusion',
      short: 'Junctions 10 m',
      title: '10 m from mapped junctions and crossings',
      copy: 'Closer positions are excluded, measured along the street or to the trunk. Conservative planning default, not a surveyed sight triangle.',
    },
    {
      id: 'parking_exclusion',
      short: 'Parking',
      title: 'Not on mapped parking',
      copy: 'A new tree cannot stand on a mapped parking bay or paved parking surface.',
    },
    {
      id: 'occupied_tree_position',
      short: 'Existing trunk',
      title: 'Not on an existing trunk',
      copy: 'A new tree cannot use the same mapped position as an existing tree.',
    },
  ];

  /** Copy a pack with session overrides applied client-side (harmless if the server already did). */
  function withOverrides(pack, overrides) {
    if (!overrides || !Object.keys(overrides).length) return pack;
    return {
      ...pack,
      rules: pack.rules.map((r) => {
        const o = overrides[r.id];
        if (!o) return r;
        return {
          ...r,
          min_distance_m: o.min_distance_m ?? r.min_distance_m,
          mode: o.mode ?? r.mode,
          enabled: o.enabled ?? r.enabled,
          overridden: true,
        };
      }),
    };
  }

  function setRulePack(pack, { render = true } = {}) {
    if (activeScenario() && JSON.stringify(state.pack?.rules) !== JSON.stringify(pack.rules)) {
      invalidateTemperature('Planting restrictions changed. Apply them before comparing cooling.');
    }
    state.pack = pack;
    if (render) renderRules();
  }

  function restrictionStrength(mode) {
    return mode === 'must' ? 'Required' : 'Recommended';
  }

  function restrictionCopy(rule) {
    const distance = rule.min_distance_m != null ? `${fmt.num(rule.min_distance_m, 2)} m` : null;
    const text = {
      carriageway_edge: `Keep the trunk at least ${distance} from the road edge.`,
      cycleway: `Keep the trunk at least ${distance} from a cycle path.`,
      building_crown: `Keep the mature crown at least ${distance} from a building.`,
      existing_tree: `Keep the new trunk at least ${distance} from an existing tree.`,
      junction: `Recommended clearance from a junction. The 10 m exclusion below always applies as well.`,
      sidewalk_passage: `Leave at least ${distance} of sidewalk beside the tree pit.`,
      plantable_surface: 'Place the trunk on a sidewalk or verge, not on the road, parking or a building.',
    }[rule.id];
    return text || rule.description || (distance ? `${rule.label}: at least ${distance}.` : rule.label);
  }

  function restrictionChip(rule) {
    const name = RESTRICTION_SHORT[rule.id] || rule.label;
    const value = !rule.enabled ? 'off'
      : rule.min_distance_m != null ? `${fmt.num(rule.min_distance_m, 2)} m · ${restrictionStrength(rule.mode).toLowerCase()}`
      : restrictionStrength(rule.mode).toLowerCase();
    return el('span', { class: `restriction-chip ${rule.enabled ? rule.mode : 'off'}` }, [
      el('span', { class: 'chip-mark', 'aria-hidden': 'true' }),
      el('button', { type: 'button', onclick: () => focusRestriction(rule.id), text: `${name} · ${value}` }),
    ]);
  }

  function lockedChip(item) {
    return el('span', { class: 'restriction-chip locked' }, [
      el('span', { class: 'chip-mark', 'aria-hidden': 'true' }),
      el('button', {
        type: 'button',
        onclick: () => {
          $('locked-rules-details').open = true;
          focusRestriction(item.id);
        },
        text: `${item.short} · always on`,
      }),
    ]);
  }

  function packPreview(pack) {
    const road = pack.rules.find((r) => r.id === 'carriageway_edge');
    const roadBit = road && road.enabled
      ? `${fmt.num(road.min_distance_m, 2)} m from the road (${restrictionStrength(road.mode).toLowerCase()})`
      : null;
    return `Default checks include ${[roadBit, '10 m from mapped junctions (always excluded)'].filter(Boolean).join(' and ')}. You can edit the limits after you choose a street.`;
  }

  function clearRestrictionFocus() {
    for (const node of document.querySelectorAll('.restriction.is-focused, .locked-item.is-focused')) {
      node.classList.remove('is-focused');
    }
  }

  function focusRestriction(ruleId) {
    ui.rulesCard.hidden = false;
    ui.rulesCard.open = true;
    state.rulesEditorPinned = true;
    if ($('rules-editor')) $('rules-editor').open = true;
    if (LOCKED_RESTRICTIONS.some((item) => item.id === ruleId)) $('locked-rules-details').open = true;
    const row = $(`restriction-${ruleId}`);
    const target = row || ui.rulesCard;
    target.scrollIntoView({ behavior: REDUCED_MOTION.matches ? 'instant' : 'smooth', block: 'center' });
    clearRestrictionFocus();
    if (row) {
      row.classList.add('is-focused');
      const input = row.querySelector('input[type="number"], input:not([disabled])');
      if (input) input.focus({ preventScroll: true });
      window.setTimeout(clearRestrictionFocus, 1600);
    }
  }

  function renderRules() {
    const focused = document.activeElement;
    const focusedRow = focused.closest?.('.restriction');
    const focusTarget = focusedRow ? { row: focusedRow.id, type: focused.type, value: focused.value } : null;
    const pack = state.pack;
    if (!pack) {
      ui.rulesStandard.replaceChildren();
      ui.rulesDefaults.replaceChildren();
      ui.rulesSummary.replaceChildren();
      ui.lockedRules.replaceChildren();
      ui.rulesMeta.textContent = '';
      ui.rulesSource.textContent = '';
      ui.restrictionPreview.textContent = '';
      return;
    }
    const active = pack.rules.filter((r) => r.enabled).length;
    const edited = pack.rules.filter((r) => r.overridden).length;
    ui.rulesMeta.textContent = edited ? `${active}/${pack.rules.length} on · ${edited} edited` : `${active}/${pack.rules.length} on`;
    const src = pack.source || {};
    ui.rulesSource.replaceChildren(
      src.url ? el('a', { href: src.url, target: '_blank', rel: 'noopener', text: pack.name }) : el('span', { text: pack.name }),
      document.createTextNode(src.publisher ? ` · ${src.publisher}` : ''),
    );
    ui.rulesSummary.replaceChildren(
      ...pack.rules.map((r) => restrictionChip(r)),
      ...LOCKED_RESTRICTIONS.map((item) => lockedChip(item)),
    );
    ui.rulesStandard.replaceChildren(...pack.rules.filter((r) => !r.assumption).map((r) => restrictionRow(r, pack)));
    ui.rulesDefaults.replaceChildren(...pack.rules.filter((r) => r.assumption).map((r) => restrictionRow(r, pack)));
    ui.lockedRules.replaceChildren(...LOCKED_RESTRICTIONS.map((item) => el('li', {
      class: 'locked-item', id: `restriction-${item.id}`,
    }, [
      el('strong', { text: item.title }),
      el('p', { class: 'restriction-copy', text: item.copy }),
    ])));
    ui.restrictionPreview.textContent = packPreview(pack);
    ui.restrictionPreview.hidden = Boolean(state.street);
    if (focusTarget) {
      const input = Array.from($(focusTarget.row)?.querySelectorAll('input') || [])
        .find(node => node.type === focusTarget.type && (node.type !== 'radio' || node.value === focusTarget.value));
      input?.focus({ preventScroll: true });
    }
  }

  function restrictionRow(rule, pack) {
    const distance = el('span', { class: 'restriction-distance' });
    if (rule.min_distance_m != null) {
      distance.append(
        el('span', { text: 'At least' }),
        el('input', {
          class: 'input', type: 'number', min: '0', max: '50', step: '0.05', value: String(rule.min_distance_m),
          'aria-label': `${rule.label}, minimum distance in metres`, disabled: !rule.enabled, required: true,
          oninput: (e) => {
            if (!e.target.validity.valid) { scheduleAutoApply(); return; }
            overrideRule(rule.id, { min_distance_m: Number(e.target.value) }, 250);
          },
        }),
        el('span', { text: 'm' }),
      );
    } else {
      distance.append(el('span', { class: 'tag', text: 'Yes or no' }));
    }
    const modeName = `restriction-mode-${rule.id}`;
    const mode = el('div', { class: 'seg seg-sm', role: 'group', 'aria-label': `${rule.label} strength` }, [
      el('label', { class: 'seg-item' }, [
        el('input', {
          type: 'radio', name: modeName, value: 'must', checked: rule.mode === 'must', disabled: !rule.enabled,
          onchange: () => overrideRule(rule.id, { mode: 'must' }),
        }),
        el('span', { text: 'Required' }),
      ]),
      el('label', { class: 'seg-item' }, [
        el('input', {
          type: 'radio', name: modeName, value: 'should', checked: rule.mode === 'should', disabled: !rule.enabled,
          onchange: () => overrideRule(rule.id, { mode: 'should' }),
        }),
        el('span', { text: 'Recommended' }),
      ]),
    ]);
    return el('li', { class: `restriction${rule.enabled ? '' : ' off'}`, id: `restriction-${rule.id}` }, [
      el('div', { class: 'restriction-head' }, [
        el('span', { class: 'restriction-title', text: rule.label }),
        el('label', { class: 'restriction-toggle' }, [
          el('input', {
            type: 'checkbox', checked: rule.enabled, 'aria-label': `Use ${rule.label}`,
            onchange: (e) => overrideRule(rule.id, { enabled: e.target.checked }),
          }),
          el('span', { text: 'Use this check' }),
        ]),
      ]),
      el('p', { class: 'restriction-copy', text: restrictionCopy(rule) }),
      el('div', { class: 'restriction-controls' }, [distance, mode]),
      el('div', { class: 'restriction-meta' }, [
        el('span', { text: ruleCitation(rule, pack) }),
        rule.assumption && ruleCitation(rule, pack) !== 'planning default' ? el('span', { class: 'tag', text: 'planning default' }) : null,
        rule.overridden ? el('span', { class: 'tag tag-edited', text: 'edited' }) : null,
      ]),
    ]);
  }

  /** Merge rapid edits and save them before calculating the next plan. */
  function overrideRule(ruleId, patch, delay = 0) {
    if (!state.pack || state.loadingStreet || state.agent.streaming) return;
    if (patch.enabled != null) {
      const row = $(`restriction-${ruleId}`);
      row.classList.toggle('off', !patch.enabled);
      for (const input of row.querySelectorAll('input:not([type="checkbox"])')) input.disabled = !patch.enabled;
    }
    state.autoApply.rules[ruleId] = { ...state.autoApply.rules[ruleId], ...patch };
    scheduleAutoApply(delay);
  }

  async function resetOverrides() {
    if (state.updatingRules || state.planning || state.loadingStreet || state.agent.streaming) return;
    const cooling = state.temperature.result
      ? { selectedId: state.temperature.selectedId, mapVisible: state.temperature.mapVisible }
      : state.autoApply.cooling;
    cancelAutoApply();
    state.autoApply.cooling = cooling;
    state.updatingRules = true;
    invalidateTemperature('Updating the planting restrictions…');
    renderWorkflow();
    setBusy(ui.rulesReset, true);
    try {
      await api('/api/rules/overrides', { method: 'DELETE' });
      await loadRulePack();
      clearError();
      scheduleAutoApply(0);
    } catch (err) {
      setError(`Rules: ${err.message}`);
    } finally {
      setBusy(ui.rulesReset, false);
      state.updatingRules = false;
      renderWorkflow();
    }
  }

  /** Active pack = config default pack, refreshed from GET /api/rules when that endpoint answers. */
  async function loadRulePack() {
    const cfg = state.config;
    const packId = (cfg.defaults && cfg.defaults.rule_pack_id) || (cfg.rule_packs[0] && cfg.rule_packs[0].id);
    let pack = cfg.rule_packs.find((p) => p.id === packId) || cfg.rule_packs[0] || null;
    try {
      const res = await api('/api/rules');
      const fresh = (res.packs || []).find((p) => p.id === packId);
      if (fresh) pack = withOverrides(fresh, res.overrides || {});
    } catch (_) { /* config copy is good enough */ }
    setRulePack(pack);
  }

  // ------------------------------------------------------------ agent chat
  /** Parse a text/event-stream body: multi-line data, CRLF, comments, no reconnect. */
  async function readSse(response, onEvent) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let eventName = 'message';
    let dataLines = [];
    const dispatch = async () => {
      if (dataLines.length) {
        const raw = dataLines.join('\n');
        let data = raw;
        try { data = JSON.parse(raw); } catch (_) { /* keep raw text */ }
        await onEvent(eventName, data);
      }
      eventName = 'message';
      dataLines = [];
    };
    const handleLine = async (line) => {
      if (line === '') { await dispatch(); return; }
      if (line.startsWith(':')) return;
      const idx = line.indexOf(':');
      const field = idx === -1 ? line : line.slice(0, idx);
      let value = idx === -1 ? '' : line.slice(idx + 1);
      if (value.startsWith(' ')) value = value.slice(1);
      if (field === 'event') eventName = value;
      else if (field === 'data') dataLines.push(value);
    };
    for (;;) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      let nl = buffer.indexOf('\n');
      while (nl !== -1) {
        await handleLine(buffer.slice(0, nl).replace(/\r$/, ''));
        buffer = buffer.slice(nl + 1);
        nl = buffer.indexOf('\n');
      }
      if (done) break;
    }
    if (buffer) await handleLine(buffer.replace(/\r$/, ''));
    await dispatch();
  }

  /** "plan_trees · spacing 8 m · both" from a tool name and its arguments. */
  function describeTool(name, args) {
    const parts = Object.entries(args || {})
      .filter(([, v]) => v != null && v !== '')
      .slice(0, 4)
      .map(([k, v]) => {
        const val = Array.isArray(v) ? v.join(', ') : typeof v === 'object' ? JSON.stringify(v) : String(v);
        if (/_m$/.test(k)) return `${k.replace(/_m$/, '').replace(/_/g, ' ')} ${val} m`;
        if (typeof v === 'string') return val;
        return `${k.replace(/_/g, ' ')} ${val}`;
      });
    return [name, ...parts].join(' · ');
  }

  function scrollChat() {
    ui.messages.scrollTop = ui.messages.scrollHeight;
  }

  function addUserMessage(text) {
    ui.messages.append(el('div', { class: 'msg-user', text }));
    scrollChat();
  }

  /** One assistant turn: interleaved prose paragraphs and tool trace rows. */
  function newTurn() {
    const root = el('div', { class: 'msg-assistant' });
    ui.messages.append(root);
    let textNode = null;
    let textBuffer = '';
    const traceDetails = el('details', { class: 'agent-actions' }, el('summary', { text: 'Planning actions' }));
    const rows = new Map();
    let lastRow = null;
    return {
      appendText(delta) {
        if (!textNode) { textNode = el('p', { class: 'msg-text' }); textBuffer = ''; root.append(textNode); }
        textBuffer += delta;
        textNode.replaceChildren(...textBuffer.split(/(\*\*[^*]+\*\*)/g).map(part => part.startsWith('**') && part.endsWith('**') ? el('strong', { text: part.slice(2, -2) }) : document.createTextNode(part)));
        scrollChat();
      },
      addTool(id, name, args) {
        textNode = null;
        const row = el('div', { class: 'trace' }, [
          el('span', { class: 'spinner', 'aria-hidden': 'true' }),
          el('span', { class: 'trace-name', text: describeTool(name, args) }),
          el('span', { class: 'trace-result' }),
        ]);
        rows.set(id || name, row);
        lastRow = row;
        if (!traceDetails.isConnected) root.append(traceDetails);
        traceDetails.append(row);
        scrollChat();
      },
      finishTool(data) {
        const row = rows.get(data.id) || rows.get(data.name) || lastRow;
        if (!row) return;
        const spinner = row.querySelector('.spinner');
        if (spinner) spinner.remove();
        row.classList.add(data.ok === false ? 'fail' : 'ok');
        const result = row.querySelector('.trace-result');
        result.textContent = data.summary || (data.ok === false ? 'failed' : 'done');
        if (data.payload_type === 'export' && data.payload && data.payload.url) {
          result.append(' ', el('a', { href: data.payload.url, download: '', text: 'download' }));
        }
        scrollChat();
      },
      error(message) {
        textNode = null;
        root.append(el('div', { class: 'trace fail' }, [el('span', { class: 'trace-result', text: message })]));
        scrollChat();
      },
      finish() {
        for (const s of root.querySelectorAll('.spinner')) s.remove();
        if (!root.childElementCount) root.remove();
      },
    };
  }

  /** Apply a tool result payload to the map and panels. */
  function applyAgentResult(data) {
    const p = data.payload;
    if (data.ok === false || !p) return;
    switch (data.payload_type) {
      case 'street': applyStreet(p, { fit: true }); break;
      case 'scenario': addScenario(p); selectScenario(p.scenario_id); break;
      case 'shade':
        if (!state.shade.enabled) setShadeEnabled(true);
        state.shade.seq += 1;
        applyShade(p);
        break;
      case 'compare': showCompare(p); break;
      case 'rules': if (p.rules) { setRulePack(p); renderWorkflow(); } break;
      case 'site': focusSite(p.site_id); break;
      default: break;
    }
  }

  async function applyUiEvent(ev) {
    switch (ev.action) {
      case 'fly_to': flyTo(ev.target, ev.zoom); break;
      case 'set_year': setYear(Number(ev.year)); break;
      case 'show_layer': setLayerVisible(ev.layer, ev.visible !== false); break;
      case 'select_scenario': await selectScenarioById(ev.scenario_id); break;
      case 'open_compare': break; // The following compare payload contains the requested plans.
      default: break;
    }
  }

  async function selectScenarioById(id) {
    if (!id) return false;
    if (!state.scenarios.some((s) => s.scenario_id === id)) {
      try { addScenario(await api(`/api/scenarios/${encodeURIComponent(id)}`)); } catch (err) { setError(`Scenario: ${err.message}`); return false; }
    }
    selectScenario(id);
    return state.activeScenarioId === id;
  }

  function flyTo(target, zoom) {
    if (target == null || target === 'street') { if (state.street) fitToBbox(state.street.bbox); return; }
    if (Array.isArray(target) && target.length >= 2) {
      map.flyTo({ center: [Number(target[0]), Number(target[1])], zoom: zoom || Math.max(map.getZoom(), 17), duration: motionMs(900) });
      return;
    }
    if (typeof target === 'string' && !focusSite(target, zoom)) setError(`Site ${target} is not in the scenario on the map.`);
  }

  async function handleAgentEvent(turn, name, data) {
    const d = data && typeof data === 'object' ? data : {};
    switch (name) {
      case 'text': if (typeof d.delta === 'string') turn.appendText(d.delta); break;
      case 'tool': turn.addTool(d.id, d.name, d.args); break;
      case 'result': turn.finishTool(d); applyAgentResult(d); break;
      case 'ui': await applyUiEvent(d); break;
      case 'done': renderQuota(d.turns_remaining_hour, d.usage); break;
      case 'error': turn.error(d.message || 'Agent error'); setError(`Agent: ${d.message || 'unknown error'}`); break;
      default: break;
    }
  }

  function renderQuota(turnsLeft, usage) {
    const bits = [];
    if (turnsLeft != null) bits.push(`${turnsLeft} agent turns left this hour`);
    ui.agentFoot.textContent = bits.join(' · ');
  }

  /** POST /api/agent and stream the turn; the rest of the UI stays live meanwhile. */
  async function sendChat(text) {
    const message = text.trim();
    if (!message || state.agent.streaming || state.loadingStreet || state.planning || state.updatingRules) return;
    if (state.draw.active) stopDraw();
    addUserMessage(message);
    ui.chatInput.value = '';
    state.agent.streaming = true;
    ui.promptChips.hidden = true;
    renderWorkflow();
    const turn = newTurn();
    const body = { message, street_id: state.street ? state.street.street_id : null, scenario_id: state.activeScenarioId };
    try {
      const res = await fetch('/api/agent', { method: 'POST', headers: requestHeaders(true), body: JSON.stringify(body) });
      if (!res.ok) throw new Error(await errorMessage(res));
      if (!res.body) throw new Error('Streaming is not supported by this browser.');
      await readSse(res, (name, data) => handleAgentEvent(turn, name, data));
    } catch (err) {
      turn.error(err.message);
      setError(`Agent: ${err.message}`);
    } finally {
      turn.finish();
      state.agent.streaming = false;
      renderWorkflow();
      ui.chatInput.focus();
    }
  }

  function renderAgent(agent) {
    const enabled = Boolean(agent && agent.enabled);
    ui.agentMeta.textContent = enabled ? 'Describe a change or ask which planting recommendations are not met.' : 'Currently unavailable';
    ui.agentOffline.hidden = enabled;
    ui.chatForm.hidden = !enabled;
    ui.promptChips.hidden = !enabled;
    if (!enabled) return;
    ui.promptChips.replaceChildren(...(agent.example_prompts || []).map((p) => el('button', {
      type: 'button', class: 'chip', text: p, title: p, onclick: () => sendChat(p),
    })));
    ui.messages.append(el('p', { class: 'msg-text msg-hello', text: 'I can help you explore a street, adjust your plan, or explain planting rules. Try a suggestion below or ask about the current plan.' }));
  }

  async function loadQuota() {
    try {
      const q = await api('/api/session');
      renderQuota(q.turns_remaining_hour, null);
    } catch (_) { /* quota line is optional */ }
  }

  // ------------------------------------------------------------ config & boot
  function renderSpeciesHint() {
    const sp = state.config && state.config.species.find((s) => s.id === ui.species.value);
    ui.speciesHint.textContent = sp
      ? `${sp.name_lat} · About ${fmt.num(sp.mature_crown_d_m, 0)} m wide and ${fmt.num(sp.mature_height_m, 0)} m tall when mature.`
      : '';
    const diameter = Number($('crown-size').value);
    $('crown-size-out').textContent = `${fmt.num(diameter, 1)} m`;
    $('crown-size').setAttribute('aria-valuetext', `${fmt.num(diameter, 1)} metres across the mature crown`);
    $('crown-assumption').textContent = sp && diameter !== sp.mature_crown_d_m
      ? `Custom planning size. Species height stays ${fmt.num(sp.mature_height_m, 0)} m; actual crown growth may differ. Choosing another species resets this size.`
      : 'Uses the species’ approximate mature size. The year slider shows growth towards this size.';
  }

  function renderConfig(cfg) {
    ui.city.replaceChildren(...cfg.cities.map((c) => el('option', { value: c.id, text: c.name })));
    $('city-field').hidden = cfg.cities.length <= 1;
    ui.species.replaceChildren(...[...cfg.species].sort((a, b) => a.mature_crown_d_m - b.mature_crown_d_m).map((sp) => el('option', {
      value: sp.id, text: `${sp.name_en} · ${fmt.num(sp.mature_crown_d_m, 0)} m crown`,
    })));
    const d = cfg.defaults || {};
    if (d.spacing_m != null) { ui.spacing.value = String(d.spacing_m); ui.spacingOut.textContent = `${fmt.num(d.spacing_m, 1)} m`; }
    if (d.species_id) ui.species.value = d.species_id;
    $('crown-size').value = String(d.crown_diameter_m ?? cfg.species.find((s) => s.id === ui.species.value)?.mature_crown_d_m ?? 10);
    for (const name of ['side', 'mode']) {
      const r = ui.planForm.querySelector(`input[name="${name}"][value="${d[name]}"]`);
      if (r) r.checked = true;
    }
    renderSpeciesHint();
    renderAgent(cfg.agent);
    document.title = `Urban Green · street-tree planning agent${cfg.version ? ` · v${cfg.version}` : ''}`;
  }

  function wireUi() {
    $('temperature-form').addEventListener('submit', (event) => { event.preventDefault(); compareTemperature(); });
    $('temperature-form').addEventListener('input', () => { invalidateTemperature(); renderWorkflow(); });
    $('go-temperature').addEventListener('click', () => {
      $('temperature-card').open = true;
      $('temperature-title').scrollIntoView({ behavior: REDUCED_MOTION.matches ? 'instant' : 'smooth', block: 'start' });
      (state.temperature.result ? $('temperature-point') : $('reference-air')).focus({ preventScroll: true });
    });
    $('temperature-point').addEventListener('change', (event) => selectTemperaturePoint(event.target.value, { focusMap: true, pin: true }));
    $('temperature-average').addEventListener('click', () => {
      selectTemperaturePoint('');
      if (state.temperature.mapVisible) fitToBbox(state.street.bbox);
    });
    $('temperature-view').addEventListener('click', showCoolingMap);
    $('temperature-map-close').addEventListener('click', () => setTemperatureMapVisible(false));
    $('temperature-map').addEventListener('click', () => {
      const result = state.temperature.result;
      if (!result || result.scenario_id !== state.activeScenarioId) return;
      setTemperatureMapVisible(false);
      stopAnimation();
      state.shade.when = { month: Number($('temperature-month').value), day: Number($('temperature-day').value), hour: Number($('temperature-hour').value) };
      setYear(result.year);
      setShadeEnabled(true);
      ui.map.scrollIntoView({ behavior: REDUCED_MOTION.matches ? 'instant' : 'smooth', block: 'center' });
      fitToBbox(state.street.bbox);
    });
    $('undo-draw').addEventListener('click', () => { state.draw.points.pop(); renderDraw(); });
    $('review-result').addEventListener('click', () => {
      $('review-card').scrollIntoView({ behavior: REDUCED_MOTION.matches ? 'instant' : 'smooth', block: 'start' });
      (ui.compareHere.hidden ? $('go-temperature') : ui.compareHere).focus({ preventScroll: true });
    });
    $('refresh-tree-data').addEventListener('click', () => {
      if (!state.street || state.loadingStreet || state.planning) return;
      const axis = state.street.layers.axis.features[0];
      if (!axis || axis.geometry.type !== 'LineString') { setError('Cannot retry without a street line. Explore the street again.'); return; }
      // Reuse the same street line to rebuild its data instead of reusing the cached OSM fallback.
      loadStreetAndPlan({ city: state.street.city, line: axis.geometry.coordinates, name: state.street.name });
    });
    $('show-existing').addEventListener('click', () => {
      setTemperatureMapVisible(false);
      const toggle = ui.legendToggles.find(cb => cb.dataset.layer === 'existing_trees');
      if (toggle) toggle.checked = true;
      setLayerVisible('existing_trees', true);
      ui.map.scrollIntoView({ behavior: REDUCED_MOTION.matches ? 'instant' : 'smooth', block: 'center' });
      fitToBbox(state.street && state.street.bbox);
    });
    $('finish-draw').addEventListener('click', finishDraw);
    $('cancel-draw').addEventListener('click', stopDraw);
    $('back-to-plan').addEventListener('click', () => {
      const target = state.temperature.mapVisible ? $('temperature-card') : state.street ? $('settings-card') : ui.header;
      if (target.tagName === 'DETAILS') target.open = true;
      target.scrollIntoView({ behavior: REDUCED_MOTION.matches ? 'instant' : 'smooth', block: 'start' });
      (state.temperature.mapVisible ? $('temperature-point') : state.street ? ui.spacing : ui.search).focus({ preventScroll: true });
    });
    ui.gotoTrees.addEventListener('click', () => {
      $('settings-card').scrollIntoView({ behavior: REDUCED_MOTION.matches ? 'instant' : 'smooth', block: 'start' });
      ui.spacing.focus({ preventScroll: true });
    });
    ui.editRestrictions.addEventListener('click', () => focusRestriction(state.pack && state.pack.rules[0] && state.pack.rules[0].id));
    $('rules-editor').addEventListener('toggle', () => { state.rulesEditorPinned = $('rules-editor').open; });
    ui.copyBrief.addEventListener('click', copyBriefing);
    ui.compareHere.addEventListener('click', openCompare);
    ui.assistantToggle.addEventListener('click', () => toggleAssistant(ui.agent.hidden));
    ui.assistantClose.addEventListener('click', () => toggleAssistant(false));
    ui.fitStreet.addEventListener('click', () => fitToBbox(state.street && state.street.bbox));
    ui.viewPlan.addEventListener('click', () => {
      setTemperatureMapVisible(false);
      ui.map.scrollIntoView({ behavior: REDUCED_MOTION.matches ? 'instant' : 'smooth', block: 'center' });
      fitToBbox(state.street && state.street.bbox);
      ui.map.focus({ preventScroll: true });
    });
    ui.planForm.addEventListener('input', updatePlanDraft);
    ui.planForm.addEventListener('change', () => {
      if (state.autoApply.pending) {
        clearTimeout(state.autoApply.timer);
        state.autoApply.timer = setTimeout(flushAutoApply, 0);
      }
    });
    ui.city.addEventListener('change', () => selectCity(ui.city.value));
    ui.searchForm.addEventListener('submit', (e) => {
      e.preventDefault();
      const q = ui.search.value.trim();
      if (q) loadStreetAndPlan({ city: state.cityId, query: q });
    });
    ui.draw.addEventListener('click', () => (state.draw.active ? stopDraw() : startDraw()));
    $('pick-street').addEventListener('click', () => {
      if (state.draw.active) stopDraw();
      setStreetPicking(!state.pickingStreet);
      if (state.pickingStreet) ui.map.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
    ui.spacing.addEventListener('input', () => { ui.spacingOut.textContent = `${fmt.num(ui.spacing.value, 1)} m`; });
    ui.species.addEventListener('change', () => {
      const species = state.config.species.find((s) => s.id === ui.species.value);
      $('crown-size').value = String(species.mature_crown_d_m);
      renderSpeciesHint();
      updatePlanDraft();
    });
    $('crown-size').addEventListener('input', renderSpeciesHint);
    ui.planForm.addEventListener('submit', (e) => {
      e.preventDefault();
      if (ui.planBtn.disabled) return;
      const pendingRules = activeScenario() && JSON.stringify(state.pack.rules) !== JSON.stringify(activeScenario().rules_used?.rules);
      if (state.autoApply.failed || state.dirty || pendingRules) scheduleAutoApply(0);
      else runPlan(formParams());
    });
    ui.rulesReset.addEventListener('click', resetOverrides);
    ui.compareBtn.addEventListener('click', openCompare);
    ui.compareClose.addEventListener('click', closeCompare);
    ui.compareModal.querySelector('[data-close]').addEventListener('click', closeCompare);
    ui.exportLink.addEventListener('click', (e) => { if (!state.activeScenarioId) e.preventDefault(); });
    ui.play.addEventListener('click', togglePlay);
    ui.year.addEventListener('input', () => { stopAnimation(); setYear(Number(ui.year.value), { fromSlider: true }); });
    ui.shade.addEventListener('click', () => setShadeEnabled(!state.shade.enabled));
    for (const cb of ui.legendToggles) cb.addEventListener('change', () => setLayerVisible(cb.dataset.layer, cb.checked));
    ui.chatForm.addEventListener('submit', (e) => { e.preventDefault(); sendChat(ui.chatInput.value); });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Tab' && !ui.compareModal.hidden) {
        const controls = Array.from(ui.compareModal.querySelectorAll('button, summary, [tabindex="0"]'))
          .filter(control => !control.disabled && control.getClientRects().length > 0);
        const first = controls[0], last = controls[controls.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
      if (e.key !== 'Escape') return;
      if (!ui.compareModal.hidden) { closeCompare(); return; }
      if (!ui.agent.hidden) { toggleAssistant(false); return; }
      if (state.draw.active) { stopDraw(); return; }
      if (state.pickingStreet) { setStreetPicking(false); return; }
      if (state.temperature.mapVisible) { setTemperatureMapVisible(false); return; }
      if (state.pinPopup) state.pinPopup.remove();
    });
    REDUCED_MOTION.addEventListener('change', () => { if (REDUCED_MOTION.matches && state.anim) { stopAnimation(); setYear(MAX_YEAR); } });
  }

  /** Prepare the map and controls; let the planner choose their own street. */
  async function boot() {
    collectUi();
    wireUi();
    renderStatus();
    renderWorkflow();
    await mapReady;
    addIcons();
    addSources();
    addLayers();
    wireMapEvents();
    updateScale();
    let cfg;
    try {
      cfg = await api('/api/config');
    } catch (err) {
      setError(`Cannot reach the Urban Green API: ${err.message}`);
      return;
    }
    state.config = cfg;
    renderConfig(cfg);
    await loadRulePack();
    loadQuota();
    const city = cfg.cities[0];
    if (!city) { setError('The server lists no cities.'); return; }
    selectCity(city.id, { silent: true });
    map.jumpTo({ center: city.center, zoom: city.zoom });
    setStreetPicking(true);
    ui.search.placeholder = `Street name in ${city.name}`;
    renderWorkflow();
    setYear(MAX_YEAR);
  }

  window.urbanGreen = { map, state };  // debugging / demo hooks (read-only use)
  boot();
})();
