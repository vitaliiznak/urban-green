/* Allee — single-page MapLibre client for the FastAPI backend under /api.
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
    'junctions', 'existing_trees', 'corridor'];
  const LAYER_GROUPS = {
    corridor: ['corridor-line', 'axis-line'],
    axis: ['axis-line'],
    carriageway: ['carriageway-fill', 'carriageway-line'],
    sidewalks: ['sidewalks-fill'],
    plantable: ['plantable-fill', 'plantable-line'],
    buildings: ['buildings-fill', 'buildings-line'],
    cycleways: ['cycleways-line'],
    junctions: ['junctions-circle'],
    existing_trees: ['existing-crowns', 'existing-trunks'],
    crowns: ['crowns'],
    sites: ['sites-ring', 'sites'],
    shade: ['shade-fill'],
  };
  const REDUCED_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)');
  const emptyFc = () => ({ type: 'FeatureCollection', features: [] });

  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const C = {
    ground: cssVar('--ground'), ink: cssVar('--ink'), muted: cssVar('--muted'), accent: cssVar('--accent'),
    crown: cssVar('--crown'), valid: cssVar('--valid'), cond: cssVar('--cond'), invalid: cssVar('--invalid'),
    shade: cssVar('--shade'), cycle: cssVar('--cycle'), existing: cssVar('--existing'),
  };

  /** "#RRGGBB" + alpha -> "rgba(r,g,b,a)". */
  function alpha(hex, a) {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }

  // ------------------------------------------------------------ state
  const state = {
    config: null,
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
    shade: { enabled: false, result: null, timer: 0, seq: 0 },
    draw: { active: false, points: [] },
    agent: { streaming: false },
    status: { sources: [], estimated: [], warnings: [], notes: [], error: null },
    hoverPopup: null,
    pinPopup: null,
  };

  const $ = (id) => document.getElementById(id);
  const activeScenario = () => state.scenarios.find((s) => s.scenario_id === state.activeScenarioId) || null;
  const ui = {};

  /** Cache every element the app touches, by id. */
  function collectUi() {
    const ids = ['map', 'header', 'city', 'search-form', 'search', 'search-btn', 'draw', 'demo-chips', 'street-meta',
      'status', 'col-left', 'plan-form', 'plan-meta', 'spacing', 'spacing-out', 'species', 'species-hint', 'label',
      'plan-btn', 'rules-meta', 'rules-source', 'rules', 'rules-reset', 'scenarios', 'scenarios-meta', 'compare-btn',
      'export-link', 'basemaps', 'play', 'year', 'readout', 'shade', 'shade-info', 'agent', 'agent-meta', 'messages',
      'agent-offline', 'prompt-chips', 'chat-form', 'chat-input', 'send', 'agent-foot', 'compare-modal',
      'compare-close', 'compare-note', 'compare-table'];
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
    for (const w of s.warnings) box.append(el('span', { class: 'pill pill-warn', text: w }));
    if (s.sources.length) {
      box.append(el('span', { class: 'sources' }, [strong('Sources '), document.createTextNode(s.sources.join(' · '))]));
    }
    if (s.estimated.length) box.append(el('span', { class: 'sources', text: `estimated: ${s.estimated.join(', ')}` }));
    if (!box.childElementCount) box.append(el('span', { class: 'sources', text: 'Loading…' }));
  }

  function setError(message) {
    state.status.error = String(message);
    renderStatus();
  }

  function clearError() {
    if (!state.status.error) return;
    state.status.error = null;
    renderStatus();
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
    map.addImage('ring-valid', ringIcon('valid', C.valid), { pixelRatio: 2 });
    map.addImage('ring-conditional', ringIcon('conditional', C.cond), { pixelRatio: 2 });
    map.addImage('ring-invalid', ringIcon('invalid', C.invalid), { pixelRatio: 2 });
  }

  function addSources() {
    for (const id of [...LAYER_KEYS, 'sites', 'shade', 'draw']) {
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
        'circle-color': alpha(C.existing, 0.25),
        'circle-stroke-color': alpha(C.existing, 0.9),
        'circle-stroke-width': 1,
        'circle-pitch-alignment': 'map',
      },
    });
    map.addLayer({
      id: 'existing-trunks', type: 'circle', source: 'existing_trees',
      paint: { 'circle-radius': 2.5, 'circle-color': C.existing },
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
    line('draw-line', 'draw', { 'line-color': C.accent, 'line-width': 2, 'line-dasharray': [2, 2] });
    map.addLayer({
      id: 'draw-points', type: 'circle', source: 'draw',
      filter: ['==', ['geometry-type'], 'Point'],
      paint: { 'circle-radius': 4, 'circle-color': C.accent, 'circle-stroke-color': C.ground, 'circle-stroke-width': 1.5 },
    });
  }

  function setSourceData(id, fc) {
    const src = map.getSource(id);
    if (src) src.setData(fc || emptyFc());
  }

  /** Show/hide a logical layer group and keep the legend checkbox in sync. */
  function setLayerVisible(key, visible) {
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
    if (window.innerWidth <= 900) return 24;
    const hdr = ui.header.getBoundingClientRect();
    const left = ui.colLeft.getBoundingClientRect().right + 16;
    const right = window.innerWidth - ui.colRight.getBoundingClientRect().left + 16;
    const top = hdr.bottom + 16;
    const bottom = window.innerHeight - ui.dock.getBoundingClientRect().top + 16;
    if (left + right > window.innerWidth * 0.8 || top + bottom > window.innerHeight * 0.8) return 24;
    return { top: Math.round(top), left: Math.round(left), right: Math.round(right), bottom: Math.round(bottom) };
  }

  function fitToBbox(bbox) {
    if (!bbox || bbox.length < 4) return;
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
      closeButton: persistent, closeOnClick: persistent, maxWidth: '360px', offset: 14,
      className: persistent ? 'pop-pinned' : 'pop-hover',
    });
  }

  function ruleLine(r, ruleDef) {
    const cls = r.passed === true ? 'pass' : r.passed === false ? 'fail' : 'na';
    const mark = r.passed === true ? '✓' : r.passed === false ? '✗' : '–';
    let measure;
    if (r.required_m == null) {
      measure = r.passed == null ? (r.note || 'not evaluated') : (r.passed ? 'on plantable surface' : 'off plantable surface');
    } else if (r.measured_m == null) {
      measure = `${r.note || 'not measurable'} · ≥ ${fmt.num(r.required_m, 2)} m required`;
    } else {
      measure = `${fmt.num(r.measured_m, 2)} m vs ≥ ${fmt.num(r.required_m, 2)} m`;
    }
    const cite = ruleDef && ruleDef.source_ref ? shortRef(ruleDef.source_ref) : (r.assumption ? 'planning default' : null);
    const meta = ` (${r.mode}${cite ? `, ${cite}` : ''}${r.basis && r.basis !== 'measured' ? `, ${r.basis}` : ''})`;
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
  function siteCard(props) {
    const sc = activeScenario();
    const defs = new Map(((sc && sc.rules_used && sc.rules_used.rules) || []).map((r) => [r.id, r]));
    return el('div', { class: 'pop' }, [
      el('div', { class: 'pop-head' }, [
        el('span', { class: 'pop-title mono', text: props.site_id }),
        el('span', { class: `verdict verdict-${props.verdict}`, text: props.verdict }),
      ]),
      el('div', { class: 'pop-meta mono', text: `station ${fmt.station(props.station_m)} · ${props.side} · edge ${fmt.m(props.edge_distance_m, 2)} · crown ${fmt.num(props.crown_d_mature_m, 0)} m mature` }),
      el('ul', { class: 'pop-rules' }, (props.rules || []).map((r) => ruleLine(r, defs.get(r.rule_id)))),
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
      el('div', { class: 'pop-head' }, [el('span', { class: 'pop-title', text: 'Existing tree' }), el('span', { class: 'pop-meta mono', text: p.id || '' })]),
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
  function focusSite(siteId, zoom) {
    const f = siteFeature(siteId);
    if (!f) return false;
    map.easeTo({ center: f.geometry.coordinates, zoom: Math.max(map.getZoom(), zoom || 18), duration: motionMs(700) });
    pinPopup(f.geometry.coordinates, siteCard(f.properties));
    return true;
  }

  function wireMapEvents() {
    map.on('zoom', scheduleScale);
    map.on('move', scheduleScale);
    map.on('mousemove', 'sites', (e) => {
      if (state.draw.active || !e.features.length) return;
      map.getCanvas().style.cursor = 'pointer';
      showHover(e.features[0]);
    });
    map.on('mouseleave', 'sites', () => { map.getCanvas().style.cursor = ''; hideHover(); });
    map.on('click', 'sites', (e) => {
      if (state.draw.active || !e.features.length) return;
      const f = siteFeature(e.features[0].properties.site_id);
      if (f) pinPopup(f.geometry.coordinates, siteCard(f.properties));
    });
    map.on('mouseenter', 'existing-crowns', () => { if (!state.draw.active) map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', 'existing-crowns', () => { map.getCanvas().style.cursor = ''; });
    map.on('click', 'existing-crowns', (e) => {
      if (state.draw.active || !e.features.length) return;
      if (map.queryRenderedFeatures(e.point, { layers: ['sites'] }).length) return;
      pinPopup(e.lngLat, treeCard(e.features[0].properties));
    });
    map.on('click', onDrawClick);
    map.on('dblclick', onDrawDoubleClick);
  }

  // -- drawing a street axis
  function startDraw() {
    state.draw.active = true;
    state.draw.points = [];
    map.doubleClickZoom.disable();
    ui.map.classList.add('drawing');
    ui.draw.setAttribute('aria-pressed', 'true');
    ui.draw.textContent = 'Click points · double-click to finish · Esc cancels';
    hideHover();
    renderDraw();
  }

  function stopDraw() {
    state.draw.active = false;
    state.draw.points = [];
    map.doubleClickZoom.enable();
    ui.map.classList.remove('drawing');
    ui.draw.setAttribute('aria-pressed', 'false');
    ui.draw.textContent = 'Draw street';
    renderDraw();
  }

  function renderDraw() {
    const pts = state.draw.points;
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
    await loadStreetAndPlan({ city: state.cityId, line, name: 'Drawn street' }, { fallback: false });
  }

  // ------------------------------------------------------------ cities & streets
  const cityById = (id) => (state.config ? state.config.cities.find((c) => c.id === id) : null);
  const firstDemoStreet = (cityId) => { const c = cityById(cityId); return c && c.demo_streets.length ? c.demo_streets[0] : null; };

  /** Make a city current: select, demo chips, basemap. Loads its first demo street unless silent. */
  function selectCity(cityId, { silent = false } = {}) {
    const city = cityById(cityId);
    if (!city) return;
    state.cityId = city.id;
    ui.city.value = city.id;
    renderDemoChips(city);
    setBasemap(city, null);
    if (!silent) loadStreetAndPlan({ city: city.id, query: firstDemoStreet(city.id) }, { fallback: true });
  }

  function renderDemoChips(city) {
    ui.demoChips.replaceChildren(...city.demo_streets.map((name) => el('button', {
      type: 'button', class: 'chip', text: name, 'aria-pressed': 'false',
      onclick: () => { ui.search.value = name; loadStreetAndPlan({ city: state.cityId, query: name }, { fallback: false }); },
    })));
  }

  function markDemoChip(streetName) {
    for (const chip of ui.demoChips.children) {
      chip.setAttribute('aria-pressed', String(chip.textContent === streetName));
    }
  }

  /** POST /api/street. On failure of a default street, fall back to the offline demo city. */
  async function loadStreet(req, { fallback = true } = {}) {
    setBusy(ui.searchBtn, true);
    ui.draw.disabled = true;
    try {
      const street = await api('/api/street', { method: 'POST', body: req });
      applyStreet(street, { fit: true });
      clearError();
      return street;
    } catch (err) {
      if (fallback && req.city !== 'demo' && cityById('demo')) {
        const cityName = (cityById(req.city) || { name: req.city }).name;
        selectCity('demo', { silent: true });
        const demo = await loadStreet({ city: 'demo', query: firstDemoStreet('demo') || 'Demo street' }, { fallback: false });
        if (demo) addNote(`${cityName}: ${err.message}. Showing the offline demo street instead.`);
        return demo;
      }
      setError(`Street: ${err.message}`);
      return null;
    } finally {
      setBusy(ui.searchBtn, false);
      ui.draw.disabled = false;
    }
  }

  async function loadStreetAndPlan(req, opts) {
    const street = await loadStreet(req, opts);
    if (street) await runPlan(formParams());
    return street;
  }

  /** Put a StreetResponse on the map and clear any scenario view. */
  function applyStreet(street, { fit = true } = {}) {
    state.street = street;
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
    const parts = [
      [`${street.name || 'Street'} · ${street.city_name || street.city}`],
      [fmt.grouped(street.length_m), ' m'],
      [fmt.int(s.existing_trees), ` existing trees${s.existing_trees_crown_imputed ? ` (${fmt.int(s.existing_trees_crown_imputed)} crowns imputed)` : ''}`],
      [fmt.int(s.junctions), ' junctions'],
      [fmt.grouped(s.cycleway_m), ' m cycleway'],
    ];
    ui.streetMeta.replaceChildren(...parts.flatMap(([b, rest], i) => [
      i ? document.createTextNode(' · ') : null,
      i ? strong(b) : el('span', { text: b }),
      rest ? document.createTextNode(rest) : null,
    ]).filter((node) => node != null));
  }

  // ------------------------------------------------------------ plans & scenarios
  const radioValue = (name) => { const r = ui.planForm.querySelector(`input[name="${name}"]:checked`); return r ? r.value : null; };

  /** PlanParams from the Plan card (offset, pit, pack from config defaults). */
  function formParams() {
    const d = (state.config && state.config.defaults) || {};
    return {
      spacing_m: Number(ui.spacing.value),
      side: radioValue('side') || d.side || 'both',
      species_id: ui.species.value || d.species_id,
      mode: radioValue('mode') || d.mode || 'grid',
      offset_from_edge_m: d.offset_from_edge_m ?? 1.0,
      pit_width_m: d.pit_width_m ?? 2.0,
      rule_pack_id: state.pack ? state.pack.id : d.rule_pack_id,
      label: ui.label.value.trim() || null,
    };
  }

  /** POST /api/plan for the current street; the new scenario becomes active. */
  async function runPlan(params, { replaceId = null } = {}) {
    if (!state.street) { setError('Load a street before planning.'); return null; }
    setBusy(ui.planBtn, true);
    try {
      const sc = await api('/api/plan', { method: 'POST', body: { street_id: state.street.street_id, ...params } });
      addScenario(sc, { replaceId });
      selectScenario(sc.scenario_id);
      clearError();
      return sc;
    } catch (err) {
      setError(`Plan: ${err.message}`);
      return null;
    } finally {
      setBusy(ui.planBtn, false);
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
  function selectScenario(scenarioId) {
    const sc = state.scenarios.find((s) => s.scenario_id === scenarioId);
    if (!sc) return;
    if (!state.street || state.street.street_id !== sc.street_id) {
      const st = state.streets.get(sc.street_id);
      if (st) applyStreet(st, { fit: true });
    }
    state.activeScenarioId = sc.scenario_id;
    const fc = decorateSites(sc.sites || emptyFc());
    state.siteIndex = new Map(fc.features.map((f) => [f.properties.site_id, f]));
    setSourceData('sites', fc);
    if (!state.street || state.street.street_id !== sc.street_id) fitToBbox(bboxOfPoints(fc));
    hideHover();
    renderScenarios();
    renderPlanMeta(sc);
    renderYearReadout();
    renderExport();
    if (state.shade.enabled) fetchShade(); else setSourceData('shade', emptyFc());
  }

  function clearScenarioView() {
    state.activeScenarioId = null;
    state.siteIndex = new Map();
    setSourceData('sites', emptyFc());
    setSourceData('shade', emptyFc());
    state.shade.result = null;
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

  function renderScenarios() {
    ui.scenariosMeta.textContent = state.scenarios.length ? String(state.scenarios.length) : '';
    ui.compareBtn.disabled = state.scenarios.length === 0;
    if (!state.scenarios.length) {
      ui.scenarios.replaceChildren(el('p', { class: 'hint', text: 'Plans you run appear here. Click one to show it on the map.' }));
      return;
    }
    ui.scenarios.replaceChildren(...state.scenarios.map((sc, i) => {
      const street = state.streets.get(sc.street_id);
      const foreign = state.street && sc.street_id !== state.street.street_id;
      return el('button', {
        type: 'button', class: 'scenario', 'aria-pressed': String(sc.scenario_id === state.activeScenarioId),
        onclick: () => selectScenario(sc.scenario_id),
      }, [
        el('span', { class: 'scenario-label', text: sc.label || sc.scenario_id }),
        el('span', { class: 'scenario-n mono', text: `#${i + 1}` }),
        el('span', { class: 'scenario-stats mono' }, [
          strong(fmt.int(sc.summary && sc.summary.planted)), document.createTextNode(' trees · '),
          strong(fmt.pct(cover30(sc))), document.createTextNode(' corridor at 30 y'),
        ]),
        foreign ? el('span', { class: 'scenario-street', text: street ? street.name : sc.street_id }) : null,
      ]);
    }));
  }

  function renderPlanMeta(sc) {
    if (!sc || !sc.summary) { ui.planMeta.textContent = ''; return; }
    const s = sc.summary;
    ui.planMeta.textContent = `${s.valid} valid · ${s.conditional} cond. · ${s.invalid} invalid`;
  }

  function renderExport() {
    const id = state.activeScenarioId;
    ui.exportLink.href = id ? `/api/export/${encodeURIComponent(id)}.geojson` : '#';
    ui.exportLink.setAttribute('aria-disabled', String(!id));
    if (id) ui.exportLink.setAttribute('download', `canopy-${id}.geojson`);
  }

  /** POST /api/compare over the last six scenarios and open the table modal. */
  async function openCompare() {
    const ids = state.scenarios.slice(-6).map((s) => s.scenario_id);
    if (!ids.length) { setError('Run a plan first, then compare.'); return; }
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

  function showCompare(res) {
    const cols = [
      ['Scenario', (r) => r.label], ['Spacing', (r) => `${fmt.num(r.spacing_m, 1)} m`], ['Side', (r) => r.side],
      ['Species', (r) => speciesShort(r.species_id)], ['Mode', (r) => r.mode], ['Planted', (r) => fmt.int(r.planted)],
      ['Valid', (r) => fmt.int(r.valid)], ['Cond.', (r) => fmt.int(r.conditional)], ['Invalid', (r) => fmt.int(r.invalid)],
      ['New crown 30 y', (r) => `${fmt.grouped(r.new_crown_area_30_m2)} m²`], ['Corridor 30 y', (r) => fmt.pct(r.cover_corridor_pct_30)],
      ['Street 30 y', (r) => fmt.pct(r.cover_street_pct_30)], ['Sidewalk 30 y', (r) => fmt.pct(r.sidewalk_under_crown_pct_30)],
    ];
    const head = el('thead', {}, el('tr', {}, cols.map(([h]) => el('th', { scope: 'col', text: h }))));
    const body = el('tbody', {}, (res.rows || []).map((r) => el('tr', {
      class: [r.scenario_id === res.best_by_cover ? 'best' : '', r.scenario_id === state.activeScenarioId ? 'active' : ''].join(' ').trim(),
      tabindex: '0',
      onclick: () => { selectScenario(r.scenario_id); closeCompare(); },
      onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectScenario(r.scenario_id); closeCompare(); } },
    }, cols.map(([, get]) => el('td', { text: get(r) })))));
    ui.compareTable.replaceChildren(head, body);
    ui.compareNote.textContent = res.best_by_cover
      ? 'Figures at year 30. Highlighted row has the best corridor cover. Click a row to show it on the map.'
      : 'Figures at year 30. Click a row to show it on the map.';
    ui.compareModal.hidden = false;
    ui.compareClose.focus();
  }

  function closeCompare() {
    if (ui.compareModal.hidden) return;
    ui.compareModal.hidden = true;
    ui.compareBtn.focus();
  }

  function speciesShort(id) {
    const sp = state.config && state.config.species.find((s) => s.id === id);
    return sp ? sp.name_lat.replace(/\s*'.*$/, '') : id;
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
    ui.readout.replaceChildren(
      document.createTextNode('Year '), strong(String(year)),
      document.createTextNode(' · canopy '), strong(fmt.pct(coverAt(sc, state.year))),
      document.createTextNode(' of corridor · '), strong(fmt.int(sc.summary && sc.summary.planted)),
      document.createTextNode(' trees'),
    );
  }

  function setYear(year, { fromSlider = false } = {}) {
    state.year = Math.min(MAX_YEAR, Math.max(0, Number(year) || 0));
    if (!fromSlider) ui.year.value = String(state.year);
    if (map.getLayer('crowns')) map.setPaintProperty('crowns', 'circle-radius', radiusExpr(crownMetresExpr(state.year)));
    renderYearReadout();
    if (state.shade.enabled) scheduleShade();
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
    state.shade.enabled = on;
    ui.shade.setAttribute('aria-pressed', String(on));
    const box = ui.legendToggles.find((cb) => cb.dataset.layer === 'shade');
    if (box) box.checked = on;
    if (map.getLayer('shade-fill')) map.setLayoutProperty('shade-fill', 'visibility', on ? 'visible' : 'none');
    if (on) fetchShade(); else { clearTimeout(state.shade.timer); renderShadeInfo(null); }
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
    const body = { scenario_id: sc.scenario_id, year: Math.round(state.year), ...SHADE_WHEN, include_existing: true };
    try {
      const res = await api('/api/shade', { method: 'POST', body });
      if (seq === state.shade.seq && state.shade.enabled) applyShade(res);
    } catch (err) {
      if (seq === state.shade.seq) setError(`Shade: ${err.message}`);
    }
  }

  function applyShade(res) {
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

  function setRulePack(pack) {
    state.pack = pack;
    renderRules();
  }

  function renderRules() {
    const pack = state.pack;
    if (!pack) { ui.rules.replaceChildren(); ui.rulesMeta.textContent = ''; ui.rulesSource.textContent = ''; return; }
    const active = pack.rules.filter((r) => r.enabled).length;
    ui.rulesMeta.textContent = `${active}/${pack.rules.length} active`;
    const src = pack.source || {};
    ui.rulesSource.replaceChildren(
      src.url ? el('a', { href: src.url, target: '_blank', rel: 'noopener', text: pack.name }) : el('span', { text: pack.name }),
      document.createTextNode(src.publisher ? ` · ${src.publisher}` : ''),
    );
    ui.rules.replaceChildren(...pack.rules.map((r) => ruleRow(r, pack)));
  }

  function ruleRow(rule, pack) {
    const other = rule.mode === 'must' ? 'should' : 'must';
    const badge = el('button', {
      type: 'button', class: `badge badge-${rule.mode}`, text: rule.mode,
      title: `Click to make this rule ${other}`, 'aria-label': `${rule.mode}; click to make it ${other}`,
      onclick: () => overrideRule(rule.id, { mode: other }),
    });
    const value = el('span', { class: 'rule-value' });
    if (rule.min_distance_m != null) {
      const input = el('input', {
        class: 'input', type: 'number', min: '0', max: '50', step: '0.05', value: String(rule.min_distance_m),
        'aria-label': `${rule.label}, minimum distance in metres`, disabled: !rule.enabled,
        onchange: (e) => {
          const v = Number(e.target.value);
          if (!Number.isFinite(v) || v < 0 || v > 50) { e.target.value = String(rule.min_distance_m); return; }
          overrideRule(rule.id, { min_distance_m: v });
        },
      });
      value.append(el('span', { text: '≥' }), input, el('span', { text: 'm' }));
    } else {
      value.append(el('span', { class: 'tag', text: 'yes / no' }));
    }
    const onOff = el('input', {
      class: 'rule-on', type: 'checkbox', checked: rule.enabled, 'aria-label': `${rule.label} enabled`,
      onchange: (e) => overrideRule(rule.id, { enabled: e.target.checked }),
    });
    const meta = el('span', { class: 'rule-meta' }, [
      el('span', { text: ruleCitation(rule, pack) }),
      rule.assumption ? el('span', { class: 'tag', text: 'planning default' }) : null,
      rule.overridden ? el('span', { class: 'tag tag-edited', text: 'edited' }) : null,
    ]);
    return el('li', { class: `rule${rule.enabled ? '' : ' off'}`, title: rule.quote || rule.description || '' }, [
      el('div', { class: 'rule-main' }, [onOff, el('span', { class: 'rule-label', text: rule.label }), badge, value]),
      meta,
    ]);
  }

  /** PUT /api/rules/{pack}/{rule}; on success re-run the active plan under the new rules. */
  async function overrideRule(ruleId, patch) {
    if (!state.pack) return;
    try {
      const res = await api(`/api/rules/${encodeURIComponent(state.pack.id)}/${encodeURIComponent(ruleId)}`, { method: 'PUT', body: patch });
      setRulePack(res.pack || withOverrides(state.pack, { [ruleId]: patch }));
      clearError();
      await replanActive();
    } catch (err) {
      setError(`Rule: ${err.message}`);
      renderRules();
    }
  }

  async function resetOverrides() {
    setBusy(ui.rulesReset, true);
    try {
      await api('/api/rules/overrides', { method: 'DELETE' });
      await loadRulePack();
      clearError();
      await replanActive();
    } catch (err) {
      setError(`Rules: ${err.message}`);
    } finally {
      setBusy(ui.rulesReset, false);
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

  /** Re-run the active scenario with its own params; the new one replaces it in the list. */
  async function replanActive() {
    const sc = activeScenario();
    if (!sc || !state.street || sc.street_id !== state.street.street_id) return;
    const p = sc.params || {};
    await runPlan({
      spacing_m: p.spacing_m, side: p.side, species_id: p.species_id, mode: p.mode,
      offset_from_edge_m: p.offset_from_edge_m, pit_width_m: p.pit_width_m,
      rule_pack_id: p.rule_pack_id, label: p.label || null,
    }, { replaceId: sc.scenario_id });
  }

  // ------------------------------------------------------------ agent chat
  /** Parse a text/event-stream body: multi-line data, CRLF, comments, no reconnect. */
  async function readSse(response, onEvent) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let eventName = 'message';
    let dataLines = [];
    const dispatch = () => {
      if (dataLines.length) {
        const raw = dataLines.join('\n');
        let data = raw;
        try { data = JSON.parse(raw); } catch (_) { /* keep raw text */ }
        onEvent(eventName, data);
      }
      eventName = 'message';
      dataLines = [];
    };
    const handleLine = (line) => {
      if (line === '') { dispatch(); return; }
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
        handleLine(buffer.slice(0, nl).replace(/\r$/, ''));
        buffer = buffer.slice(nl + 1);
        nl = buffer.indexOf('\n');
      }
      if (done) break;
    }
    if (buffer) handleLine(buffer.replace(/\r$/, ''));
    dispatch();
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
    const rows = new Map();
    let lastRow = null;
    return {
      appendText(delta) {
        if (!textNode) { textNode = el('p', { class: 'msg-text' }); root.append(textNode); }
        textNode.textContent += delta;
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
        root.append(row);
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
      case 'rules': if (p.rules) setRulePack(p); break;
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
      case 'open_compare': openCompare(); break;
      default: break;
    }
  }

  async function selectScenarioById(id) {
    if (!id) return;
    if (!state.scenarios.some((s) => s.scenario_id === id)) {
      try { addScenario(await api(`/api/scenarios/${encodeURIComponent(id)}`)); } catch (err) { setError(`Scenario: ${err.message}`); return; }
    }
    selectScenario(id);
  }

  function flyTo(target, zoom) {
    if (target == null || target === 'street') { if (state.street) fitToBbox(state.street.bbox); return; }
    if (Array.isArray(target) && target.length >= 2) {
      map.flyTo({ center: [Number(target[0]), Number(target[1])], zoom: zoom || Math.max(map.getZoom(), 17), duration: motionMs(900) });
      return;
    }
    if (typeof target === 'string' && !focusSite(target, zoom)) setError(`Site ${target} is not in the scenario on the map.`);
  }

  function handleAgentEvent(turn, name, data) {
    const d = data && typeof data === 'object' ? data : {};
    switch (name) {
      case 'text': if (typeof d.delta === 'string') turn.appendText(d.delta); break;
      case 'tool': turn.addTool(d.id, d.name, d.args); break;
      case 'result': turn.finishTool(d); applyAgentResult(d); break;
      case 'ui': applyUiEvent(d); break;
      case 'done': renderQuota(d.turns_remaining_hour, d.usage); break;
      case 'error': turn.error(d.message || 'Agent error'); setError(`Agent: ${d.message || 'unknown error'}`); break;
      default: break;
    }
  }

  function renderQuota(turnsLeft, usage) {
    const bits = [];
    if (turnsLeft != null) bits.push(`${turnsLeft} agent turns left this hour`);
    if (usage && usage.input_tokens != null) bits.push(`${fmt.grouped(usage.input_tokens)} in / ${fmt.grouped(usage.output_tokens)} out tokens`);
    ui.agentFoot.textContent = bits.join(' · ');
  }

  /** POST /api/agent and stream the turn; the rest of the UI stays live meanwhile. */
  async function sendChat(text) {
    const message = text.trim();
    if (!message || state.agent.streaming) return;
    addUserMessage(message);
    ui.chatInput.value = '';
    state.agent.streaming = true;
    ui.send.disabled = true;
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
      ui.send.disabled = false;
      ui.chatInput.focus();
    }
  }

  function renderAgent(agent) {
    const enabled = Boolean(agent && agent.enabled);
    ui.agentMeta.textContent = enabled ? [agent.provider, agent.model].filter(Boolean).join(' · ') : 'offline';
    ui.agentOffline.hidden = enabled;
    ui.chatForm.hidden = !enabled;
    ui.promptChips.hidden = !enabled;
    if (!enabled) return;
    ui.promptChips.replaceChildren(...(agent.example_prompts || []).map((p) => el('button', {
      type: 'button', class: 'chip', text: p, title: p, onclick: () => sendChat(p),
    })));
    ui.messages.append(el('p', { class: 'msg-text msg-hello', text: 'Ask for a street, a spacing, a species or a rule change. Every number comes from a tool call you can see below.' }));
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
      ? `${sp.name_de} · ${sp.name_en} · ${sp.size_class} · mature crown ${fmt.num(sp.mature_crown_d_m, 0)} m · height ${fmt.num(sp.mature_height_m, 0)} m`
      : '';
  }

  function renderConfig(cfg) {
    ui.city.replaceChildren(...cfg.cities.map((c) => el('option', { value: c.id, text: c.name })));
    ui.species.replaceChildren(...cfg.species.map((sp) => el('option', {
      value: sp.id, text: `${sp.name_lat} · ${sp.size_class} · ${fmt.num(sp.mature_crown_d_m, 0)} m crown`,
    })));
    const d = cfg.defaults || {};
    if (d.spacing_m != null) { ui.spacing.value = String(d.spacing_m); ui.spacingOut.textContent = `${fmt.num(d.spacing_m, 1)} m`; }
    if (d.species_id) ui.species.value = d.species_id;
    for (const name of ['side', 'mode']) {
      const r = ui.planForm.querySelector(`input[name="${name}"][value="${d[name]}"]`);
      if (r) r.checked = true;
    }
    renderSpeciesHint();
    renderAgent(cfg.agent);
    document.title = `Allee · street-tree planning agent${cfg.version ? ` · v${cfg.version}` : ''}`;
  }

  function wireUi() {
    ui.city.addEventListener('change', () => selectCity(ui.city.value));
    ui.searchForm.addEventListener('submit', (e) => {
      e.preventDefault();
      const q = ui.search.value.trim();
      if (q) loadStreetAndPlan({ city: state.cityId, query: q }, { fallback: false });
    });
    ui.draw.addEventListener('click', () => (state.draw.active ? finishDraw() : startDraw()));
    ui.spacing.addEventListener('input', () => { ui.spacingOut.textContent = `${fmt.num(ui.spacing.value, 1)} m`; });
    ui.species.addEventListener('change', renderSpeciesHint);
    ui.planForm.addEventListener('submit', (e) => { e.preventDefault(); runPlan(formParams()); });
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
      if (e.key !== 'Escape') return;
      if (!ui.compareModal.hidden) { closeCompare(); return; }
      if (state.draw.active) { stopDraw(); return; }
      if (state.pinPopup) state.pinPopup.remove();
    });
    REDUCED_MOTION.addEventListener('change', () => { if (REDUCED_MOTION.matches && state.anim) { stopAnimation(); setYear(MAX_YEAR); } });
  }

  /** First frame: config -> first city -> its first demo street -> default plan -> year 30. */
  async function boot() {
    collectUi();
    wireUi();
    renderStatus();
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
      setError(`Cannot reach the Allee API: ${err.message}`);
      return;
    }
    state.config = cfg;
    renderConfig(cfg);
    await loadRulePack();
    loadQuota();
    const city = cfg.cities[0];
    if (!city) { setError('The server lists no cities.'); return; }
    selectCity(city.id, { silent: true });
    const street = await loadStreet({ city: city.id, query: firstDemoStreet(city.id) }, { fallback: true });
    if (street) await runPlan(formParams());
    setYear(MAX_YEAR);
  }

  window.allee = { map, state };  // debugging / demo hooks (read-only use)
  boot();
})();
