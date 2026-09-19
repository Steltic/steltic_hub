/* Probabilistic analysis — three steps: model → run → results. */
'use strict';

const qs = new URLSearchParams(location.search);
const S = {
  project: (qs.get('project') || 'Project').replace(/[^A-Za-z0-9_-]/g, '') || 'Project',
  lib: null, me: null, proj: null, res: null, step: 1,
  log: [], es: null, lastSeq: 0, vars: null, n: null, seed: null, workers: null,
  sort: { key: 'member_max', dir: 'desc' }, gsort: { key: 'p_over_1', dir: 'desc' },
};
const $ = (s, r = document) => r.querySelector(s);
const enc = encodeURIComponent;
function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'style') n.style.cssText = v;
    else if (k.startsWith('on')) n[k] = v;
    else if (k === 'html') n.innerHTML = v;
    else if (k in n && k !== 'list' && k !== 'form') { try { n[k] = v; } catch (e) { n.setAttribute(k, v); } }
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) n.append(k.nodeType ? k : document.createTextNode(String(k)));
  return n;
}
function toast(msg, kind = '') { const t = el('div', { class: 'toast ' + kind }, msg); $('#toasts').append(t); setTimeout(() => t.remove(), kind === 'bad' ? 9000 : 4500); }
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: opts.raw ? {} : { 'Content-Type': 'application/json' }, ...opts,
    body: opts.raw ? opts.body : (opts.body !== undefined ? JSON.stringify(opts.body) : undefined) });
  const txt = await r.text(); let d; try { d = JSON.parse(txt); } catch (e) { d = { detail: txt }; }
  if (!r.ok) throw new Error((d && (d.detail || d.error)) || `${r.status} ${r.statusText}`);
  return d;
}
function openModal(title, body, buttons) { const m = $('#modal'); $('.mh', m).textContent = title; $('.mb', m).replaceChildren(...body); $('.mf', m).replaceChildren(...buttons); m.hidden = false; }
function closeModal() { $('#modal').hidden = true; }
const fmt = {
  n(v, d = 3) { if (v === null || v === undefined || Number.isNaN(v)) return '—'; if (typeof v !== 'number') return String(v); return Number.isInteger(v) ? String(v) : v.toFixed(d); },
  pct(v) { return v === null || v === undefined ? '—' : `${(100 * v).toFixed(0)}%`; },
  when(t) { return t ? new Date(t * 1000).toLocaleString() : ''; },
  dur(s) { if (s === null || s === undefined) return '—'; if (s < 90) return `${Math.round(s)} s`; if (s < 5400) return `${Math.round(s / 60)} min`; return `${(s / 3600).toFixed(1)} h`; },
};

/* ---------------------------------------------------------------- load */
async function load() {
  $('#projname').textContent = S.project;
  [S.lib, S.me, S.proj] = await Promise.all([api('/api/library'), api('/api/me'), api(`/api/project/${enc(S.project)}`)]);
  const sett = S.proj.settings || {};
  if (S.vars === null) S.vars = JSON.parse(JSON.stringify(sett.variables || {}));
  if (S.n === null) S.n = sett.n || S.lib.default_n;
  if (S.seed === null) S.seed = sett.seed || 1;
  if (S.workers === null) S.workers = sett.workers || S.me.default_workers;
  if (S.step === 1 && S.proj.probe) S.step = (S.proj.results.n_ok > 0) ? 3 : 2;
  if (S.proj.running) attachEvents();
  header(); render();
}
async function refresh() { S.proj = await api(`/api/project/${enc(S.project)}`); header(); }
function header() {
  const n = $('#status'); n.className = 'note'; const t = S.me;
  const bits = [];
  if (!t.engine_ok) { bits.push('HR Steel not installed'); n.classList.add('bad'); }
  if (!t.ddm_python_ok) { bits.push('Nonlinear (SNL) not installed'); n.classList.add('bad'); }
  if (t.engine_ok && t.ddm_python_ok) bits.push(`${t.cpu_count} CPU · ${t.default_workers} workers by default`);
  n.textContent = bits.join(' · ');
}

/* ---------------------------------------------------------------- steps */
const STEPS = [[1, 'Model'], [2, 'Run'], [3, 'Results']];
function stepState(k) {
  const p = S.proj;
  if (k === 1) return p.package ? (p.probe ? `${p.probe.name}` : 'loaded') : '';
  if (k === 2) return p.running ? 'running…' : (p.results.n_ok ? `${p.results.n_ok} done` : '');
  if (k === 3) return p.results.n_ok ? `${p.results.n_ok} realisations` : '';
  return '';
}
function render() {
  $('#steps').replaceChildren(...STEPS.map(([k, t]) => el('div', { class: 'step' + (S.step === k ? ' on' : '') + (stepState(k) && S.step !== k ? ' done' : ''),
    onclick: () => { S.step = k; render(); } }, el('span', { class: 'k' }, k), el('span', {}, t), stepState(k) ? el('span', { class: 'st' }, stepState(k)) : null)));
  const main = $('#main'); main.replaceChildren();
  ({ 1: paneModel, 2: paneRun, 3: paneResults })[S.step](main);
  main.scrollTop = 0;
}

/* ---------------------------------------------------------------- about */
function aboutBox() {
  return el('div', { class: 'about' },
    el('h4', {}, 'What this module does — and why it is unusual'),
    el('p', {}, 'A design is checked on a ', el('b', {}, 'nominal'), ' building: plumb columns, handbook section properties, E = 29,000 ksi. The building that gets built is never that one. ',
      'Researchers in the Direct Design Method tradition (Rasmussen and co-workers in Australia, the SSRC / AISC advanced-analysis groups in the USA) study this with Monte Carlo simulation: ',
      'the statistical distributions of material, geometry and imperfection variables collected over decades in the literature are sampled to create many slightly different ',
      '“as-built” versions of one structure, normally to find the distribution of its ', el('b', {}, 'system capacity'), ' for reliability calibration.'),
    el('p', {}, 'This module applies that idea to ', el('b', {}, 'standard LRFD design practice'), ' instead. It samples the as-built variables that change the elastic demands — ',
      'modulus of elasticity, plate thickness per section group, a story-by-story out-of-plumb profile (and optionally the dead load) — builds each realisation in the design’s own ',
      'HR Steel analysis model, runs every ASCE 7-22 load combination with P-Δ and the ELF forces from that realisation’s own period, and then checks the resulting demands against the ',
      el('b', {}, 'design’s own AISC 360 capacities'), ', which are never recomputed. The question it answers: ',
      el('b', {}, 'if this building is built with real-world imperfections, does it still satisfy the design code it was designed to?')),
    el('p', {}, 'Because AISC 360 is a member-by-member check there is no single capacity factor to plot; the output is the ', el('b', {}, 'maximum demand-to-capacity ratio in the building'),
      ' — one distribution for members, one for connections — with the design’s own value marked, its statistical parameters, where the governing check moves to, and which ',
      'section groups cross 1.0 in some realisations. The ratio is ', el('b', {}, 'D/Rₙ: the factored demand over the nominal capacity'),
      ' — the resistance factor φ is taken out of the design’s φRₙ, so 1.0 means the demand reaches the nominal strength (the design’s LRFD ratio D/φRₙ is one click away). ',
      'A ratio above 1.0 here is ', el('b', {}, 'not a code requirement to do anything'),
      ' — the code is satisfied on the nominal model — it is a place to take a second look.'),
    el('p', {}, 'Not varied, on purpose: ', S.lib.not_varied.map(([k, why], i) => el('span', {}, i ? '; ' : '', el('b', {}, k), ' — ', why)), '.'));
}

/* ---------------------------------------------------------------- 1. model */
function paneModel(main) {
  const p = S.proj;
  const busy = el('span', { class: 'hint' });
  const fromSteltic = el('button', { class: 'primary', disabled: !S.me.steltic_url });
  fromSteltic.textContent = 'Use this project’s HR Steel design';
  const setPackage = async (body) => {
    busy.textContent = 'reading the package and running the design model once…';
    try { S.proj = await api(`/api/project/${enc(S.project)}/package`, { method: 'POST', body }); toast('package loaded', 'ok'); render(); }
    catch (e) { toast(e.message, 'bad'); busy.textContent = ''; }
  };
  fromSteltic.onclick = () => setPackage({ source: 'steltic' });
  const fromZip = el('button', {}, 'A zip in this project ▾');
  fromZip.onclick = async () => {
    const d = await api(`/api/project/${enc(S.project)}/zips`);
    if (!d.zips.length) { toast('no zip files in this project’s folder (Design variations writes its selection to variations/selected/)', 'bad'); return; }
    const sel = el('select', {}, ...d.zips.map(z => el('option', { value: z.path }, `${z.path} (${(z.bytes / 1e6).toFixed(1)} MB)`)));
    openModal('Choose a design package', [el('div', { class: 'field' }, sel)],
      [el('button', { class: 'primary', onclick: () => { closeModal(); setPackage({ source: 'zip', path: sel.value }); } }, 'Use this package'),
       el('button', { class: 'ghost', onclick: closeModal }, 'Cancel')]);
  };
  const picker = el('input', { type: 'file', accept: '.zip', style: 'display:none' });
  picker.onchange = async () => {
    const f = picker.files[0]; if (!f) return;
    const fd = new FormData(); fd.append('file', f);
    busy.textContent = 'uploading and reading the package…';
    try { S.proj = await api(`/api/project/${enc(S.project)}/package/upload`, { method: 'POST', raw: true, body: fd }); toast('package loaded', 'ok'); render(); }
    catch (e) { toast(e.message, 'bad'); busy.textContent = ''; }
    picker.value = '';
  };
  const upload = el('button', { onclick: () => picker.click() }, 'Upload a package zip…');
  const pane = el('div', { class: 'pane' }, el('h2', {}, 'Probabilistic analysis'), aboutBox(),
    el('div', { class: 'card' }, el('h3', { style: 'margin-top:0' }, 'Input model'),
      el('p', { class: 'hint' }, 'A completed HR Steel design package: its cfg.py is the analysis model, its calc_package.json holds the capacities and D/C the design was checked with.'),
      el('div', { class: 'row' }, fromSteltic, fromZip, upload, picker, busy),
      p.package ? el('p', { class: 'kv', style: 'margin-top:8px' }, 'Loaded: ', el('b', {}, p.package.label), ` · ${p.package.files} files · ${fmt.when(p.package.loaded_at)}`) : null));
  if (p.probe) {
    const pr = p.probe, d = pr.design;
    const mrows = d.members.map(m => el('tr', {}, el('td', {}, m.id), el('td', {}, m.limit_state || ''), el('td', { class: 'num' }, fmt.n(m.DC))));
    pane.append(el('div', { class: 'card' },
      el('div', { class: 'stats' },
        tile('building', pr.name, `${pr.members} members · ${pr.stories} stories · ${pr.NX}×${pr.NY} bays`),
        tile('system', (pr.system || '?').split(',')[0], pr.risk_category ? `Risk Category ${pr.risk_category}` : ''),
        tile('load combinations', pr.n_combos, 'ASCE 7-22 LRFD, P-Δ'),
        tile('checked groups', `${d.n_members_dc} / ${d.members.length}`, `${d.n_connections_dc} connections with D/C`),
        tile('section groups', pr.groups.length, pr.groups.slice(0, 4).map(g => g.section).join(', ') + (pr.groups.length > 4 ? '…' : ''))),
      el('h3', {}, 'The design’s own checks (from calc_package.json)'),
      el('div', { class: 'tablewrap', style: 'max-height:320px' }, el('table', {}, el('thead', {}, el('tr', {}, el('th', {}, 'Group'), el('th', {}, 'Limit state'), el('th', { class: 'num' }, 'D/C'))), el('tbody', {}, ...mrows))),
      el('div', { class: 'row', style: 'margin-top:12px' }, el('button', { class: 'primary', onclick: () => { S.step = 2; render(); } }, 'Continue to Run →'))));
  }
  main.append(pane);
}
function tile(l, v, s) { return el('div', { class: 'stat' }, el('div', { class: 'l' }, l), el('div', { class: 'v' }, v === null || v === undefined ? '—' : v), s ? el('div', { class: 's' }, s) : null); }

/* ---------------------------------------------------------------- 2. run */
function varsEditor() {
  const rows = S.lib.variables.map(v => {
    const o = S.vars[v.id] || {};
    const on = el('input', { type: 'checkbox', checked: o.enabled !== undefined ? !!o.enabled : v.enabled, onchange: e => { S.vars[v.id] = { ...S.vars[v.id], enabled: e.target.checked }; } });
    const mean = el('input', { type: 'number', step: 'any', value: o.mean !== undefined ? o.mean : v.mean, onchange: e => { S.vars[v.id] = { ...S.vars[v.id], mean: Number(e.target.value) }; } });
    const isStd = v.std !== undefined;
    const disp = el('input', { type: 'number', step: 'any', value: o[isStd ? 'std' : 'cov'] !== undefined ? o[isStd ? 'std' : 'cov'] : (isStd ? v.std : v.cov),
      onchange: e => { S.vars[v.id] = { ...S.vars[v.id], [isStd ? 'std' : 'cov']: Number(e.target.value) }; } });
    return el('tr', {}, el('td', {}, on), el('td', {}, el('b', {}, v.label), el('div', { class: 'hint' }, `per ${v.level} · ${v.dist} · ${v.unit}`)),
      el('td', {}, mean), el('td', {}, disp, el('span', { class: 'hint', style: 'margin-left:4px' }, isStd ? 'σ' : 'COV')),
      el('td', { class: 'what' }, v.what), el('td', { class: 'src' }, v.source));
  });
  return el('table', { class: 'vars' }, el('thead', {}, el('tr', {}, el('th', {}, ''), el('th', {}, 'Variable'), el('th', {}, 'Mean'), el('th', {}, 'Dispersion'), el('th', {}, 'What it changes'), el('th', {}, 'Source of the default'))), el('tbody', {}, ...rows));
}
function paneRun(main) {
  const p = S.proj;
  if (!p.probe) { main.append(el('div', { class: 'pane' }, el('h2', {}, 'Run'), el('p', { class: 'lead' }, 'Load the input model first.'), el('button', { class: 'primary', onclick: () => { S.step = 1; render(); } }, '← Model'))); return; }
  const nIn = el('input', { type: 'number', min: 1, max: 5000, value: S.n, onchange: e => { S.n = Number(e.target.value); } });
  const seedIn = el('input', { type: 'number', min: 0, value: S.seed, onchange: e => { S.seed = Number(e.target.value); } });
  const wIn = el('input', { type: 'number', min: 1, max: 64, value: S.workers, onchange: e => { S.workers = Number(e.target.value); } });
  const run = el('button', { class: 'primary', disabled: p.running }, p.results.n_ok ? 'Start a new study' : 'Run');
  const cont = el('button', { disabled: p.running || !p.spec || p.results.n_ok >= (p.spec ? p.spec.n : 0) }, 'Continue the unfinished study');
  const stop = el('button', { class: 'danger', disabled: !p.running }, 'Stop');
  const start = async (body) => {
    try { await api(`/api/project/${enc(S.project)}/run`, { method: 'POST', body }); S.log = []; S.lastSeq = 0; await refresh(); attachEvents(); render(); }
    catch (e) { toast(e.message, 'bad'); }
  };
  run.onclick = () => {
    const go = () => start({ n: S.n, seed: S.seed, workers: S.workers, variables: S.vars });
    if (p.results.n_ok) openModal('Start a new study?', [el('p', { class: 'blurb' }, `The ${p.results.n_ok} realisations already analysed will be discarded and a new sample drawn (seed ${S.seed}).`)],
      [el('button', { class: 'primary', onclick: () => { closeModal(); go(); } }, 'Start new'), el('button', { class: 'ghost', onclick: closeModal }, 'Cancel')]);
    else go();
  };
  cont.onclick = () => start({ continue: true, workers: S.workers });
  stop.onclick = async () => { await api(`/api/project/${enc(S.project)}/stop`, { method: 'POST' }); toast('stopping the workers'); };
  const prog = p.progress; const done = prog ? prog.done : p.results.n_total; const total = prog ? prog.total : (p.spec ? p.spec.n + 1 : 0);
  const bar = el('div', { class: 'bar' }, el('i', { style: `width:${total ? 100 * done / total : 0}%` }));
  const eta = prog && prog.eta_seconds ? `≈ ${fmt.dur(prog.eta_seconds)} left (${fmt.dur(prog.mean_seconds)} per realisation on ${prog.workers} workers)` : '';
  main.append(el('div', { class: 'pane' }, el('h2', {}, 'Run'),
    el('p', { class: 'lead' }, 'Each realisation is one full LRFD analysis of the as-built model (all combinations, P-Δ, its own period and ELF forces) — a few seconds each on this size of building. Realisation 0 is the nominal design model; the workers run in the background and the study survives closing this tab.'),
    el('div', { class: 'card' },
      el('div', { class: 'row' }, el('label', { class: 'f', style: 'margin:0' }, 'Number of variations'), nIn,
        el('label', { class: 'f', style: 'margin:0 0 0 14px' }, 'Seed'), seedIn, el('label', { class: 'f', style: 'margin:0 0 0 14px' }, 'Workers'), wIn,
        el('span', { class: 'hint' }, `${S.me.cpu_count} CPU on this PC`)),
      el('div', { class: 'row', style: 'margin-top:12px' }, run, cont, stop, el('span', { class: 'sp' }), el('span', { class: 'hint' }, p.running ? eta : (p.run ? `last run ${p.run.status} ${fmt.when(p.run.finished_at || p.run.started_at)}` : ''))),
      bar, el('div', { class: 'hint' }, total ? `${done} of ${total} analysed${prog && prog.failed ? ` · ${prog.failed} failed` : ''}` : 'nothing run yet')),
    el('details', { class: 'card', open: !p.results.n_ok }, el('summary', {}, 'Random variables — defaults from the literature, editable'), varsEditor(),
      el('p', { class: 'hint', style: 'margin-top:8px' }, 'Lognormal variables are given by mean and COV; the out-of-plumb by mean and standard deviation (radians). Values are clamped to physical ranges. Changes apply to the next new study.')),
    el('div', { class: 'card' }, el('div', { class: 'row' }, el('h3', { style: 'margin:0' }, 'Log'), el('span', { class: 'sp' }), el('button', { class: 'small', onclick: () => { S.step = 3; render(); } }, 'Go to Results →')), el('div', { class: 'log', id: 'log' }))));
  paintLog();
}
function attachEvents() {
  if (S.es) { S.es.close(); S.es = null; }
  const es = new EventSource(`/api/project/${enc(S.project)}/events?since=${S.lastSeq}`); S.es = es;
  es.onmessage = async (m) => {
    let ev; try { ev = JSON.parse(m.data); } catch (e) { return; }
    if (ev.seq) S.lastSeq = ev.seq;
    if (ev.type === 'finished') { es.close(); S.es = null; if (!ev.idle) { S.log.push({ k: ev.stopped ? 'err' : 'ok', t: ev.stopped ? 'study stopped' : 'study finished' }); await refresh(); if (S.step === 2) render(); else paintLog(); } return; }
    if (ev.type === 'start') S.log.push({ k: 'l', t: `▶ realisation ${ev.id}${ev.nominal ? ' (the design model)' : ''} on worker ${ev.worker}` });
    else if (ev.type === 'done') {
      S.log.push({ k: ev.ok ? 'ok' : 'err', t: ev.ok ? `■ ${ev.id} done in ${fmt.dur(ev.seconds)} — T₁ ${fmt.n(ev.T1)} s, V ${fmt.n(ev.V_kip, 0)} kip` : `■ ${ev.id} failed: ${ev.error}` });
      if (ev.progress && S.proj) { S.proj.progress = ev.progress; const b = $('.bar i'); if (b) b.style.width = `${100 * ev.progress.done / ev.progress.total}%`; }
      if (ev.id === 0 || (ev.progress && ev.progress.done % 10 === 0)) { await refresh(); if (S.step === 2) { render(); return; } }
    } else S.log.push({ k: 'l', t: ev.text || '' });
    if (S.log.length > 800) S.log.splice(0, 200);
    paintLog();
  };
}
function paintLog() { const box = $('#log'); if (!box) return; const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30; box.replaceChildren(...S.log.map(l => el('div', { class: 'l ' + l.k }, l.t))); if (stick) box.scrollTop = box.scrollHeight; }

/* ---------------------------------------------------------------- 3. results */
function sortable(cols, rows, sortState, onSort, rowFn) {
  const head = el('tr', {}, ...cols.map(([k, label, kind]) => {
    const on = sortState.key === k;
    return el('th', { class: 'sortable' + (kind === 'num' ? ' num' : '') + (on ? ' sorted' : ''), onclick: () => onSort(on ? { key: k, dir: sortState.dir === 'asc' ? 'desc' : 'asc' } : { key: k, dir: kind === 'num' ? 'desc' : 'asc' }) },
      label, on ? el('span', { class: 'arrow' }, sortState.dir === 'asc' ? '▲' : '▼') : null);
  }));
  const d = sortState.dir === 'asc' ? 1 : -1;
  const sorted = [...rows].sort((a, b) => {
    let x = a[sortState.key], y = b[sortState.key];
    const nx = x === null || x === undefined, ny = y === null || y === undefined;
    if (nx && ny) return 0; if (nx) return 1; if (ny) return -1;
    return (typeof x === 'number' && typeof y === 'number') ? (x - y) * d : String(x).localeCompare(String(y)) * d;
  });
  return el('div', { class: 'tablewrap' }, el('table', {}, el('thead', {}, head), el('tbody', {}, ...sorted.map(rowFn))));
}
async function paneResults(main) {
  const p = S.proj;
  main.append(el('div', { class: 'pane' }, el('h2', {}, 'Results'), el('p', { class: 'hint' }, 'loading…')));
  let d;
  try { d = await api(`/api/project/${enc(S.project)}/results`); } catch (e) { toast(e.message, 'bad'); return; }
  if (S.step !== 3) return;
  const a = d.analysis; main.replaceChildren();
  if (!a || !a.members) {
    main.append(el('div', { class: 'pane' }, el('h2', {}, 'Results'), el('p', { class: 'lead' }, a && a.base && a.base.error ? `The design model itself failed: ${a.base.error}` : 'No completed realisation yet.'),
      el('button', { class: 'primary', onclick: () => { S.step = 2; render(); } }, '← Run'))); return;
  }
  const b = a.base, ms = a.members.stats, cs = a.connections ? a.connections.stats : null, ds = a.drift ? a.drift.stats : null, g = a.governing;
  const R = a.ratio_label || 'D/C', nominal = a.basis !== 'design';
  const plot = (svg) => { const w = el('div', { class: 'plot' }); w.innerHTML = svg; return w; };
  const setBasis = async (basis) => { try { await api(`/api/project/${enc(S.project)}/basis`, { method: 'POST', body: { basis } }); render(); } catch (e) { toast(e.message, 'bad'); } };
  const basisBox = el('div', { class: 'card tight' },
    el('div', { class: 'row' }, el('b', { style: 'font-size:12px;color:var(--dim)' }, 'CAPACITY BASIS'),
      el('label', { class: 'rule', style: 'padding:0' }, el('input', { type: 'radio', name: 'basis', checked: nominal, onchange: () => setBasis('nominal') }), el('span', {}, 'D/Rₙ — factored demand over nominal capacity (φ removed)')),
      el('label', { class: 'rule', style: 'padding:0' }, el('input', { type: 'radio', name: 'basis', checked: !nominal, onchange: () => setBasis('design') }), el('span', {}, 'D/φRₙ — the design’s own LRFD ratio'))),
    el('p', { class: 'hint', style: 'margin:6px 0 0' }, nominal
      ? `The design's package records φRₙ and D/φRₙ; the φ is taken out here (Rₙ = φRₙ / φ) so that 1.0 means the factored demand reaches the nominal strength, not that the LRFD check is exactly met. The design's own maximum LRFD ratio: ${fmt.n(b.member_max_recorded)} (members), ${fmt.n(b.connection_max_recorded)} (connections). φ assumed: ` +
        (a.phi_notes || []).map(([w, v]) => `${v.toFixed(2)} for ${w}`).join('; ') + '.'
      : 'Ratios are the design’s LRFD ratios D/φRₙ, recomputed for each realisation with the recorded φRₙ.'));
  const statRows = (st) => [['realisations', st.n], ['design value', fmt.n(st.base)], ['mean', fmt.n(st.mean)], ['std / COV', `${fmt.n(st.std)} / ${(100 * st.cov).toFixed(1)}%`],
    ['median', fmt.n(st.median)], ['min / max', `${fmt.n(st.min)} / ${fmt.n(st.max)}`], ['5th / 95th pct', `${fmt.n(st.p5)} / ${fmt.n(st.p95)}`], ['skewness', fmt.n(st.skewness)],
    ['P(> 1.0)', `${fmt.pct(st.p_over_limit)}${st.p_over_limit_lognormal !== undefined ? ` (lognormal ${(100 * st.p_over_limit_lognormal).toFixed(2)}%)` : ''}`], ['P(> design)', fmt.pct(st.p_over_base)],
    st.fit_lognormal ? ['lognormal fit', `μ ${st.fit_lognormal.mu.toFixed(4)}, σ ${st.fit_lognormal.sigma.toFixed(4)}; KS D ${st.fit_lognormal.ks.D}, p ${st.fit_lognormal.ks.p}`] : null,
    st.fit_normal ? ['normal fit', `μ ${st.fit_normal.mean.toFixed(4)}, σ ${st.fit_normal.std.toFixed(4)}; KS D ${st.fit_normal.ks.D}, p ${st.fit_normal.ks.p}`] : null].filter(Boolean);
  const statTable = (st, title) => el('div', { class: 'card' }, el('h3', { style: 'margin-top:0' }, title), el('table', {}, el('tbody', {}, ...statRows(st).map(([k, v]) => el('tr', {}, el('td', {}, k), el('td', { class: 'num' }, v))))));
  const pane = el('div', { class: 'pane' }, el('h2', {}, 'Results'), basisBox,
    el('div', { class: 'stats', style: 'margin-bottom:14px' },
      tile(`max member ${R}, design`, fmt.n(b.member_max), b.governing), tile('mean of the realisations', fmt.n(ms.mean), `COV ${(100 * ms.cov).toFixed(1)}% · 95th pct ${fmt.n(ms.p95)}`),
      tile('realisations over 1.0', fmt.pct(ms.p_over_limit), `${a.n_done} analysed${a.n_failed ? `, ${a.n_failed} failed` : ''}`),
      cs ? tile(`max connection ${R}`, `${fmt.n(cs.mean)}`, `design ${fmt.n(b.connection_max)} · over 1.0 in ${fmt.pct(cs.p_over_limit)}`) : null,
      ds ? tile('drift / allowable', fmt.n(ds.mean), `design ${fmt.n(b.drift_ratio)} · over in ${fmt.pct(ds.p_over_limit)}`) : null,
      tile('governing check stays', fmt.pct(g.same_as_base_share), `${g.base} in the design`)),
    el('div', { class: 'card' }, plot(d.plots.members)),
    d.plots.groups ? el('div', { class: 'card' }, plot(d.plots.groups)) : null,
    a.connections ? el('div', { class: 'card' }, plot(d.plots.connections)) : null,
    el('div', { class: 'card' }, el('h3', { style: 'margin-top:0' }, 'Assessment'), el('ul', { class: 'assess' }, ...a.assessment.map(s => el('li', {}, s)))),
    el('div', { class: 'row top' }, el('div', { class: 'grow' }, statTable(ms, `Maximum member ${R} — statistical parameters`)),
      cs ? el('div', { class: 'grow' }, statTable(cs, `Maximum connection ${R}`)) : null, ds ? el('div', { class: 'grow' }, statTable(ds, 'Design drift / allowable')) : null));
  // governing check
  pane.append(el('div', { class: 'card' }, el('h3', { style: 'margin-top:0' }, 'Governing check — the design vs the population'),
    el('p', { class: 'kv' }, 'Design: ', el('b', {}, `${g.base} — ${b.governing_limit_state || ''}`), ` under `, el('code', {}, b.governing_combo || '')),
    el('div', { class: 'row top' },
      el('div', { class: 'grow' }, el('table', {}, el('thead', {}, el('tr', {}, el('th', {}, 'Governing group'), el('th', { class: 'num' }, 'runs'), el('th', { class: 'num' }, 'share'))),
        el('tbody', {}, ...g.counts.map(([gid, c]) => el('tr', { class: gid === g.base ? 'top1' : '' }, el('td', {}, gid), el('td', { class: 'num' }, c), el('td', { class: 'num' }, fmt.pct(c / a.n_done))))))),
      el('div', { class: 'grow' }, el('table', {}, el('thead', {}, el('tr', {}, el('th', {}, 'Governing combination'), el('th', { class: 'num' }, 'runs'), el('th', { class: 'num' }, 'share'))),
        el('tbody', {}, ...g.combos.map(([cb, c]) => el('tr', {}, el('td', {}, el('code', {}, cb)), el('td', { class: 'num' }, c), el('td', { class: 'num' }, fmt.pct(c / a.n_done))))))))));
  // groups: where to look
  const gcols = [['id', 'Group'], ['limit_state', 'Check'], ['dc_recorded', 'LRFD D/φRₙ', 'num'], ['dc_base', `Design ${R}`, 'num'], ['mean', 'Mean', 'num'], ['p95', '95th pct', 'num'], ['max', 'Max', 'num'], ['p_over_1', 'Over 1.0 in', 'num'], ['governs_share', 'Governs in', 'num'], ['change_mean', 'Change', 'num'], ['method', 'Method']];
  pane.append(el('div', { class: 'card' }, el('h3', { style: 'margin-top:0' }, 'Member groups — where to take a second look'),
    el('p', { class: 'hint' }, `Click a header to sort. Highlighted rows cross ${R} = 1.0 in at least one realisation. “exact” = AISC 360 check recomputed with the capacities recorded in the package${nominal ? ' (each divided by its φ)' : ''}; “scaled” = the recorded ratio grown by the largest increase of its demand components (the package records no capacity for that term)${nominal ? ', with φ = 0.90 taken out (1.00 for a pure shear check)' : ''} — conservative.`),
    sortable(gcols, a.groups, S.gsort, s => { S.gsort = s; render(); }, r => el('tr', { class: r.p_over_1 > 0 ? 'top2' : '' },
      el('td', {}, r.id), el('td', {}, r.limit_state || ''), el('td', { class: 'num' }, fmt.n(r.dc_recorded)), el('td', { class: 'num' }, fmt.n(r.dc_base)), el('td', { class: 'num' }, fmt.n(r.mean)), el('td', { class: 'num' }, fmt.n(r.p95)),
      el('td', { class: 'num' }, fmt.n(r.max)), el('td', { class: 'num' }, fmt.pct(r.p_over_1)), el('td', { class: 'num' }, fmt.pct(r.governs_share)),
      el('td', { class: 'num' }, r.change_mean === null ? '—' : `${(100 * r.change_mean).toFixed(1)}%`), el('td', {}, el('span', { class: 'pill ' + (r.method === 'exact' ? 'ok' : 'warn') }, r.method + (r.phi && nominal ? ` φ ${r.phi.toFixed(2)}` : '')))))));
  if (a.conn_table && a.conn_table.length) {
    pane.append(el('div', { class: 'card' }, el('h3', { style: 'margin-top:0' }, 'Connections'),
      el('div', { class: 'tablewrap' }, el('table', {}, el('thead', {}, el('tr', {}, el('th', {}, 'Connection'), el('th', {}, 'Type'), el('th', { class: 'num' }, 'LRFD D/φRₙ'), nominal ? el('th', { class: 'num' }, 'φ') : null, el('th', { class: 'num' }, `Design ${R}`), el('th', { class: 'num' }, 'Mean'), el('th', { class: 'num' }, 'Max'), el('th', { class: 'num' }, 'Over 1.0 in'), el('th', {}, 'Demand scaled by'))),
        el('tbody', {}, ...a.conn_table.map(r => el('tr', { class: r.p_over_1 > 0 ? 'top2' : '' }, el('td', {}, r.id), el('td', {}, r.type || ''), el('td', { class: 'num' }, fmt.n(r.dc_recorded)), nominal ? el('td', { class: 'num' }, fmt.n(r.phi, 2)) : null, el('td', { class: 'num' }, fmt.n(r.dc_base)), el('td', { class: 'num' }, fmt.n(r.mean)),
          el('td', { class: 'num' }, fmt.n(r.max)), el('td', { class: 'num' }, fmt.pct(r.p_over_1)), el('td', { class: 'hint' }, (r.mapped || []).map(([k, h]) => `${k} ← ${h}`).join(', ')))))))));
  }
  if (a.correlations && a.correlations.length) {
    pane.append(el('div', { class: 'card' }, el('h3', { style: 'margin-top:0' }, `What drives the scatter of the maximum member ${R}`),
      el('table', {}, el('tbody', {}, ...a.correlations.map(c => el('tr', {}, el('td', {}, c.label), el('td', { class: 'num' }, `${c.rho >= 0 ? '+' : ''}${c.rho.toFixed(2)}`),
        el('td', {}, el('span', { class: 'rho', style: `width:${Math.abs(c.rho) * 160}px` }))))))));
  }
  const rcols = [['id', '#', 'num'], ['member_max', `Max member ${R}`, 'num'], ['governing', 'Governing'], ['governing_combo', 'Combination'], ['n_over', 'Groups > 1', 'num'], ['connection_max', `Max conn. ${R}`, 'num'], ['drift_ratio', 'Drift / allow.', 'num'],
    ['T1', 'T₁ (s)', 'num'], ['V_kip', 'V (kip)', 'num'], ['E_factor', 'E', 'num'], ['thk_mean', 'thk', 'num'], ['lean_top', 'lean', 'num'], ['dead', 'D', 'num']];
  pane.append(el('div', { class: 'card' }, el('div', { class: 'row' }, el('h3', { style: 'margin:0' }, 'Realisations'), el('span', { class: 'sp' }),
      el('a', { href: `/api/project/${enc(S.project)}/file/report.html`, target: '_blank' }, 'report.html'), el('a', { href: `/api/project/${enc(S.project)}/file/results.csv` }, 'results.csv'),
      el('a', { href: `/api/project/${enc(S.project)}/file/summary.json` }, 'summary.json'), el('a', { href: `/api/project/${enc(S.project)}/file/realisations.json` }, 'realisations.json')),
    el('p', { class: 'hint' }, 'E, thk and D are factors on nominal; lean is the resultant top-of-building lean as 1/H. Files are written to this project’s probabilistic/ folder.'),
    sortable(rcols, a.rows, S.sort, s => { S.sort = s; render(); }, r => el('tr', { class: r.member_max > 1 ? 'top2' : '' },
      el('td', { class: 'num' }, r.id), el('td', { class: 'num' }, fmt.n(r.member_max)), el('td', {}, r.governing || ''), el('td', {}, el('code', {}, r.governing_combo || '')), el('td', { class: 'num' }, r.n_over),
      el('td', { class: 'num' }, fmt.n(r.connection_max)), el('td', { class: 'num' }, fmt.n(r.drift_ratio)), el('td', { class: 'num' }, fmt.n(r.T1)), el('td', { class: 'num' }, fmt.n(r.V_kip, 0)),
      el('td', { class: 'num' }, fmt.n(r.E_factor)), el('td', { class: 'num' }, fmt.n(r.thk_mean)), el('td', { class: 'num' }, r.lean_top ? `1/${Math.round(1 / r.lean_top)}` : '—'), el('td', { class: 'num' }, fmt.n(r.dead, 2))))));
  main.append(pane);
}

load().catch(e => { document.body.append(el('div', { class: 'pane', style: 'padding:30px;color:var(--bad)' }, 'The module could not load: ' + e.message)); });
