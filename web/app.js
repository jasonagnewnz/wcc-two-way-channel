/* Two-way channel — browser app.
 *
 * No framework and no map library, on purpose. A CDN script tag is a single
 * point of failure that fails exactly when the venue wifi does, which is
 * during the demo. Everything here is served from the same process that
 * serves the API.
 *
 * The map is plain SVG: real WCC GeoJSON projected into a viewBox. That is
 * about eighty lines, and it cannot be broken by someone else's outage.
 */

'use strict';

// ── state ────────────────────────────────────────────────────────────────

const state = {
  meta: null,
  basemap: null,
  reports: [],
  cursor: 0,
  view: 'report',
  draft: { issue_type: 'flooding', severity: 'unknown', reporter_kind: 'resident',
           lat: null, lng: null },
  mine: [],          // reference codes held on this device
  openOps: null,     // which ops card is expanded
};

const MINE_KEY = 'wcc-two-way/my-reports';

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// ── api ──────────────────────────────────────────────────────────────────

async function api(path, options) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const text = await res.text();
  let body = {};
  try { body = text ? JSON.parse(text) : {}; } catch (_) { /* non-JSON */ }
  if (!res.ok) throw new Error(body.error || `${res.status} ${res.statusText}`);
  return body;
}

// ── small helpers ────────────────────────────────────────────────────────

/**
 * Escape for interpolation into HTML.
 *
 * INVARIANT: every value that reaches an innerHTML template in this file
 * passes through here first. Report titles and descriptions are typed by
 * members of the public into a form that anyone can submit to without an
 * account — if there is a hostile input surface in this prototype, it is
 * that one. Escaping both quote styles as well as the angle brackets means
 * the same function is safe in text position and inside a quoted attribute,
 * which is why every attribute interpolation below uses double quotes.
 *
 * If you add a template, escape the value. If you need real markup from a
 * user, you need a sanitiser, not this.
 */
function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function titleCase(slug) {
  return String(slug || '').replace(/-/g, ' ').replace(/^./, c => c.toUpperCase());
}

/** Wellington local time, which is what everyone in the room is on. */
function clock(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return d.toLocaleTimeString('en-NZ', {
    hour: '2-digit', minute: '2-digit', timeZone: 'Pacific/Auckland',
  });
}

function ago(iso) {
  if (!iso) return '';
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (isNaN(secs)) return '';
  if (secs < 60) return 'just now';
  if (secs < 3600) return `${Math.floor(secs / 60)} min ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)} h ago`;
  return `${Math.floor(secs / 86400)} d ago`;
}

function loadMine() {
  try { state.mine = JSON.parse(localStorage.getItem(MINE_KEY) || '[]'); }
  catch (_) { state.mine = []; }
}

function rememberMine(ref) {
  if (!state.mine.includes(ref)) {
    state.mine.unshift(ref);
    // The reference code is the only claim a reporter has on their report,
    // and localStorage is the only place it lives. Never silently drop one.
    try { localStorage.setItem(MINE_KEY, JSON.stringify(state.mine)); } catch (_) {}
  }
  updateMineCount();
}

function updateMineCount() {
  const pill = $('#mine-count');
  pill.hidden = state.mine.length === 0;
  pill.textContent = state.mine.length;
}

// ── map ──────────────────────────────────────────────────────────────────

const SVG_NS = 'http://www.w3.org/2000/svg';

class Map {
  /**
   * Equirectangular projection over a fixed extent. At the scale of one
   * harbour this is visually indistinguishable from a proper projection,
   * and it is arithmetic rather than a dependency.
   *
   * The cos(latitude) term matters: without it Wellington comes out
   * noticeably stretched east-west, because a degree of longitude here is
   * only about 0.75 of a degree of latitude on the ground.
   */
  constructor(el, extent) {
    this.el = el;
    const [w, s, e, n] = extent;
    this.w = w; this.s = s; this.e = e; this.n = n;

    const midLat = (s + n) / 2;
    this.width = 1000;
    const lonSpan = (e - w) * Math.cos(midLat * Math.PI / 180);
    this.height = Math.round(this.width * ((n - s) / lonSpan));

    this.svg = document.createElementNS(SVG_NS, 'svg');
    this.svg.setAttribute('viewBox', `0 0 ${this.width} ${this.height}`);
    this.svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
    this.svg.setAttribute('role', 'img');
    this.svg.setAttribute('aria-label', 'Map of Wellington showing tsunami evacuation zones, emergency hubs and community reports');

    this.layers = {};
    for (const name of ['zones', 'hubs', 'pins', 'pick']) {
      const g = document.createElementNS(SVG_NS, 'g');
      g.dataset.layer = name;
      this.svg.appendChild(g);
      this.layers[name] = g;
    }

    el.textContent = '';
    el.appendChild(this.svg);
  }

  x(lng) { return (lng - this.w) / (this.e - this.w) * this.width; }
  y(lat) { return (this.n - lat) / (this.n - this.s) * this.height; }

  /** Inverse — for click-to-drop-a-pin. */
  latlng(clientX, clientY) {
    const box = this.svg.getBoundingClientRect();
    // The SVG letterboxes inside its box (preserveAspectRatio), so work out
    // the drawn rectangle before converting, or every pin lands offset.
    const scale = Math.min(box.width / this.width, box.height / this.height);
    const drawnW = this.width * scale;
    const drawnH = this.height * scale;
    const offX = (box.width - drawnW) / 2;
    const offY = (box.height - drawnH) / 2;

    const px = (clientX - box.left - offX) / scale;
    const py = (clientY - box.top - offY) / scale;
    if (px < 0 || py < 0 || px > this.width || py > this.height) return null;

    return {
      lng: this.w + (px / this.width) * (this.e - this.w),
      lat: this.n - (py / this.height) * (this.n - this.s),
    };
  }

  path(geometry) {
    const rings = geometry.type === 'Polygon' ? [geometry.coordinates]
                : geometry.type === 'MultiPolygon' ? geometry.coordinates
                : [];
    let d = '';
    for (const polygon of rings) {
      for (const ring of polygon) {
        ring.forEach(([lng, lat], i) => {
          d += `${i ? 'L' : 'M'}${this.x(lng).toFixed(1)} ${this.y(lat).toFixed(1)}`;
        });
        d += 'Z';
      }
    }
    return d;
  }

  drawBasemap(collection) {
    const zones = this.layers.zones;
    const hubs = this.layers.hubs;
    zones.textContent = '';
    hubs.textContent = '';

    for (const feature of (collection.features || [])) {
      const props = feature.properties || {};
      if (props.layer === 'tsunami-zone') {
        const el = document.createElementNS(SVG_NS, 'path');
        el.setAttribute('d', this.path(feature.geometry));
        const colour = ['red', 'orange', 'yellow'].includes(props.colour) ? props.colour : 'other';
        el.setAttribute('class', `zone-${colour}`);
        const label = [props.zone, props.location].filter(Boolean).join(' — ');
        if (label) {
          const t = document.createElementNS(SVG_NS, 'title');
          t.textContent = label;
          el.appendChild(t);
        }
        zones.appendChild(el);
      } else if (props.layer === 'hub') {
        const [lng, lat] = feature.geometry.coordinates;
        const el = document.createElementNS(SVG_NS, 'circle');
        el.setAttribute('cx', this.x(lng).toFixed(1));
        el.setAttribute('cy', this.y(lat).toFixed(1));
        el.setAttribute('r', '2.6');
        el.setAttribute('class', 'hub');
        const t = document.createElementNS(SVG_NS, 'title');
        t.textContent = `Emergency hub: ${props.name || 'unnamed'}${props.address ? ' — ' + props.address : ''}`;
        el.appendChild(t);
        hubs.appendChild(el);
      }
    }
  }

  drawPins(reports, { onClick } = {}) {
    const layer = this.layers.pins;
    layer.textContent = '';
    for (const report of reports) {
      if (report.lat == null || report.lng == null) continue;
      const g = document.createElementNS(SVG_NS, 'g');
      const circle = document.createElementNS(SVG_NS, 'circle');
      circle.setAttribute('cx', this.x(report.lng).toFixed(1));
      circle.setAttribute('cy', this.y(report.lat).toFixed(1));
      circle.setAttribute('r', '6');
      circle.setAttribute('class', `pin p-${report.status || 'received'}`);
      const t = document.createElementNS(SVG_NS, 'title');
      t.textContent = `${report.title} — ${report.status_label || ''}`;
      circle.appendChild(t);
      if (onClick) circle.addEventListener('click', () => onClick(report));
      g.appendChild(circle);
      layer.appendChild(g);
    }
  }

  drawDrop(lat, lng) {
    const layer = this.layers.pick;
    layer.textContent = '';
    if (lat == null || lng == null) return;
    const x = this.x(lng), y = this.y(lat);
    const halo = document.createElementNS(SVG_NS, 'circle');
    halo.setAttribute('cx', x.toFixed(1)); halo.setAttribute('cy', y.toFixed(1));
    halo.setAttribute('r', '13'); halo.setAttribute('class', 'pin-halo');
    const dot = document.createElementNS(SVG_NS, 'circle');
    dot.setAttribute('cx', x.toFixed(1)); dot.setAttribute('cy', y.toFixed(1));
    dot.setAttribute('r', '6.5'); dot.setAttribute('class', 'drop');
    layer.appendChild(halo);
    layer.appendChild(dot);
  }

  highlight(lat, lng) {
    if (lat == null) return;
    this.drawDrop(lat, lng);
    setTimeout(() => { this.layers.pick.textContent = ''; }, 2600);
  }
}

let reportMap = null;
let opsMap = null;

// ── views ────────────────────────────────────────────────────────────────

function showView(name) {
  state.view = name;
  $$('.tab').forEach(t => t.classList.toggle('is-active', t.dataset.view === name));
  $$('.view').forEach(v => v.classList.toggle('is-active', v.id === `view-${name}`));
  if (name === 'mine') renderMine();
  if (name === 'ops') renderOps();
}

// ── report form ──────────────────────────────────────────────────────────

function chipGroup(container, values, key, labels) {
  container.textContent = '';
  for (const value of values) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chip';
    btn.textContent = (labels && labels[value]) || titleCase(value);
    btn.setAttribute('aria-pressed', String(state.draft[key] === value));
    btn.addEventListener('click', () => {
      state.draft[key] = value;
      $$('.chip', container).forEach(c => c.setAttribute('aria-pressed', 'false'));
      btn.setAttribute('aria-pressed', 'true');
    });
    container.appendChild(btn);
  }
}

function setLocation(lat, lng, label) {
  state.draft.lat = lat;
  state.draft.lng = lng;
  const out = $('#location-readout');
  if (lat == null) {
    out.textContent = 'No location set';
    out.classList.remove('set');
  } else {
    out.textContent = label || `${lat.toFixed(5)}, ${lng.toFixed(5)}`;
    out.classList.add('set');
  }
  if (reportMap) reportMap.drawDrop(lat, lng);
}

function initReportForm() {
  const meta = state.meta;
  chipGroup($('#issue-types'), meta.issue_types, 'issue_type');
  // Ordered by how bad it is, with the default first. The API returns the
  // severities sorted alphabetically (Extreme, Minor, Moderate, Severe,
  // Unknown), which presents a severity scale in an order that means
  // nothing. Anything the server sends that is not in this list is appended
  // rather than dropped.
  const SEVERITY_ORDER = ['unknown', 'minor', 'moderate', 'severe', 'extreme'];
  const severities = [
    ...SEVERITY_ORDER.filter(s => meta.severities.includes(s)),
    ...meta.severities.filter(s => !SEVERITY_ORDER.includes(s)),
  ];
  chipGroup($('#severities'), severities, 'severity', {
    unknown: "Don't know", minor: 'Minor', moderate: 'Moderate',
    severe: 'Serious', extreme: 'Extreme',
  });
  chipGroup($('#reporter-kinds'), ['resident', 'community-group', 'hub'], 'reporter_kind', {
    resident: 'A resident', 'community-group': 'A community group', hub: 'An Emergency Hub',
  });

  $('#locate').addEventListener('click', () => {
    if (!navigator.geolocation) {
      return void showError('#report-error', 'This browser will not share a location. Tap the map instead.');
    }
    $('#locate').disabled = true;
    $('#locate').textContent = 'Locating...';
    navigator.geolocation.getCurrentPosition(
      pos => {
        setLocation(pos.coords.latitude, pos.coords.longitude, 'Your current location');
        $('#locate').disabled = false;
        $('#locate').textContent = 'Use my location';
      },
      () => {
        showError('#report-error', 'Could not get your location. Tap the map to place a pin instead.');
        $('#locate').disabled = false;
        $('#locate').textContent = 'Use my location';
      },
      { enableHighAccuracy: true, timeout: 8000 },
    );
  });

  $('#report-form').addEventListener('submit', submitReport);
  $('#receipt-again').addEventListener('click', () => {
    $('#receipt').hidden = true;
    $('#report-form').hidden = false;
    $('#report-form').reset();
    setLocation(null, null);
  });
  $('#receipt-track').addEventListener('click', () => showView('mine'));
}

function showError(sel, message) {
  const el = $(sel);
  el.textContent = message;
  el.hidden = false;
  setTimeout(() => { el.hidden = true; }, 7000);
}

async function submitReport(event) {
  event.preventDefault();
  const form = event.target;
  const data = new FormData(form);
  const btn = $('#submit-btn');

  const payload = {
    title: (data.get('title') || '').trim(),
    description: (data.get('description') || '').trim(),
    place_name: (data.get('place_name') || '').trim() || null,
    issue_type: state.draft.issue_type,
    severity: state.draft.severity,
    reporter_kind: state.draft.reporter_kind,
    lat: state.draft.lat,
    lng: state.draft.lng,
    media_urls: (data.get('media_url') || '').trim() ? [data.get('media_url').trim()] : [],
  };

  if (!payload.title) return void showError('#report-error', 'Say what is happening, even in a few words.');

  btn.disabled = true;
  btn.textContent = 'Sending...';
  try {
    const result = await api('/api/reports', { method: 'POST', body: JSON.stringify(payload) });
    rememberMine(result.reference);
    $('#receipt-ref').textContent = result.reference;
    $('#receipt-context').textContent = '';
    $('#report-form').hidden = true;
    $('#receipt').hidden = false;
    window.scrollTo({ top: 0, behavior: 'smooth' });
    // Hazard context is looked up in the background, so ask again shortly.
    setTimeout(() => fillReceiptContext(result.reference), 3500);
    refresh(true);
  } catch (err) {
    showError('#report-error', err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Send to WCC';
  }
}

async function fillReceiptContext(reference) {
  try {
    const view = await api(`/api/reports/${reference}`);
    if (view.hazard_summary) {
      $('#receipt-context').innerHTML =
        `<strong>About this location</strong><br>${esc(view.hazard_summary)}` +
        `<br><span class="code">Inferred from WCC hazard-planning layers, not verified on the ground.</span>`;
    }
  } catch (_) { /* context is a nicety; never surface its failure */ }
}

// ── my reports ───────────────────────────────────────────────────────────

async function renderMine() {
  const list = $('#mine-list');
  if (!state.mine.length) {
    list.innerHTML = '<p class="empty">Nothing yet. Reports you send from this device show up here.</p>';
    return;
  }

  const views = await Promise.all(state.mine.map(async ref => {
    try { return await api(`/api/reports/${ref}`); }
    catch (_) { return null; }
  }));

  list.innerHTML = views.filter(Boolean).map(view => {
    const r = view.report;
    const status = view.status || 'received';
    return `
      <article class="report s-${esc(status)}">
        <div class="meta">
          <span class="badge b-${esc(status)}">${esc(view.status_label)}</span>
          <span class="code">${esc(r.id)}</span>
          <span>${esc(ago(r.created_at))}</span>
        </div>
        <h3>${esc(r.title)}</h3>
        ${r.description ? `<p class="body">${esc(r.description)}</p>` : ''}
        ${view.hazard_summary ? `<p class="body"><em>${esc(view.hazard_summary)}</em></p>` : ''}
        <ol class="timeline">
          ${view.timeline.map((t, i) => `
            <li class="${i === view.timeline.length - 1 ? 'now' : ''}">
              <strong>${esc(t.label)}</strong>
              <span class="when">${esc(clock(t.at))} · ${esc(t.actor)}</span>
              ${t.note ? `<span class="note">${esc(t.note)}</span>` : ''}
            </li>`).join('')}
        </ol>
      </article>`;
  }).join('');
}

function initLookup() {
  $('#lookup-form').addEventListener('submit', async event => {
    event.preventDefault();
    const input = event.target.reference;
    const ref = (input.value || '').trim().toUpperCase();
    if (!ref) return;
    try {
      await api(`/api/reports/${ref}`);
      rememberMine(ref);
      input.value = '';
      renderMine();
    } catch (err) {
      showError('#lookup-error', err.message);
    }
  });
}

// ── WCC ops ──────────────────────────────────────────────────────────────

function groupLabel(groupId, reports) {
  const size = reports.filter(r => r.group_id === groupId).length;
  return size > 1 ? `${size} related reports` : null;
}

function renderOps() {
  const reports = state.reports.slice().reverse();
  $('#ops-counter').textContent =
    `${reports.length} report${reports.length === 1 ? '' : 's'}`;

  if (opsMap) {
    opsMap.drawPins(reports, {
      onClick: report => {
        state.openOps = report.id;
        renderOps();
        const card = document.getElementById(`ops-${report.id}`);
        if (card) {
          card.scrollIntoView({ behavior: 'smooth', block: 'center' });
          card.classList.add('is-flash');
          setTimeout(() => card.classList.remove('is-flash'), 1500);
        }
      },
    });
  }

  const list = $('#ops-list');
  if (!reports.length) {
    list.innerHTML = '<p class="empty">No reports yet. Send one from the “Report an issue” tab.</p>';
    return;
  }

  list.innerHTML = reports.map(r => {
    const status = r.status || 'received';
    const related = groupLabel(r.group_id, state.reports);
    return `
      <article class="report s-${esc(status)}" id="ops-${esc(r.id)}">
        <div class="meta">
          <span class="badge b-${esc(status)}">${esc(r.status_label)}</span>
          ${related ? `<span class="grouptag">${esc(related)}</span>` : ''}
          <span class="code">${esc(r.id)}</span>
          <span>${esc(ago(r.created_at))}</span>
        </div>
        <h3>${esc(r.title)}</h3>
        <div class="meta">
          <span>${esc(titleCase(r.issue_type))}</span>
          ${r.place_name ? `<span>· ${esc(r.place_name)}</span>` : ''}
          <span>· ${esc(titleCase((r.raw && r.raw.reporter_kind) || 'resident'))}</span>
          <span>· severity ${esc(r.severity)}</span>
        </div>
        ${r.description ? `<p class="body">${esc(r.description)}</p>` : ''}
        <div class="taps">
          ${state.meta.statuses.map(s => `
            <button class="btn tiny ${s === status ? '' : 'ghost'}"
                    data-ref="${esc(r.id)}" data-status="${esc(s)}"
                    ${s === status ? 'disabled' : ''}>
              ${esc(state.meta.status_labels[s] || s)}
            </button>`).join('')}
        </div>
      </article>`;
  }).join('');

  $$('#ops-list .taps button[data-status]').forEach(btn => {
    btn.addEventListener('click', () => tapStatus(btn.dataset.ref, btn.dataset.status, btn));
  });
}

async function tapStatus(reference, status, btn) {
  btn.disabled = true;
  try {
    await api(`/api/reports/${reference}/status`, {
      method: 'POST',
      body: JSON.stringify({ status, note: '', actor: 'wcc-staff' }),
    });
    await refresh(true);
  } catch (err) {
    showError('#lookup-error', err.message);
    btn.disabled = false;
  }
}

// ── polling ──────────────────────────────────────────────────────────────

/**
 * Ask only for what is new. /api/signals?since=<cursor> returns the tail of
 * the append-only log, so an idle page transfers a few bytes per tick rather
 * than the whole dataset. When the tail is non-empty, something changed and
 * the visible view re-renders.
 */
async function refresh(force = false) {
  try {
    const tail = await api(`/api/signals?since=${state.cursor}`);
    const changed = force || (tail.signals && tail.signals.length > 0);
    state.cursor = tail.cursor;
    if (!changed) return;

    const data = await api('/api/reports');
    state.reports = data.reports || [];

    if (state.view === 'ops') renderOps();
    if (state.view === 'mine') renderMine();
  } catch (_) {
    // Offline or the server restarted. Keep the last good render on screen
    // and try again on the next tick rather than blanking the page.
  }
}

// ── boot ─────────────────────────────────────────────────────────────────

async function boot() {
  loadMine();
  updateMineCount();

  $$('.tab').forEach(tab => {
    tab.addEventListener('click', () => showView(tab.dataset.view));
  });

  state.meta = await api('/api/meta');
  initReportForm();
  initLookup();

  reportMap = new Map($('#map-report'), state.meta.extent);
  opsMap = new Map($('#map-ops'), state.meta.extent);

  reportMap.svg.addEventListener('click', event => {
    const point = reportMap.latlng(event.clientX, event.clientY);
    if (point) setLocation(point.lat, point.lng);
  });

  $('#legend').innerHTML = `
    <span><i style="background:#d64545"></i><i style="background:#e08a2e"></i><i style="background:#e3c342"></i>
      &nbsp;Tsunami evacuation zone</span>
    <span><i style="background:var(--ink-3);border-radius:50%"></i>Emergency hub</span>`;

  try {
    state.basemap = await api('/api/basemap');
    reportMap.drawBasemap(state.basemap);
    opsMap.drawBasemap(state.basemap);
    $('#attrib').textContent = state.basemap.attribution || '';
  } catch (_) {
    $('#attrib').textContent = 'Basemap unavailable — run tools/fetch_basemap.py.';
  }

  await refresh(true);
  setInterval(refresh, 3000);
}

boot().catch(err => {
  // Built as DOM rather than a markup string: an error message can carry a
  // server-supplied fragment, and there is no reason for the failure path to
  // be the one place that parses HTML.
  const banner = document.createElement('p');
  banner.className = 'error';
  banner.style.margin = '16px';
  banner.textContent = `Could not start: ${err.message}`;
  document.body.prepend(banner);
});
