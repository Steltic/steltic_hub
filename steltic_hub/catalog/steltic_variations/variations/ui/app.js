/* Design variations — one page, five steps: base brief → variations → design → score → results. */
'use strict';

const qs = new URLSearchParams(location.search);
const S = {
  project: (qs.get('project') || 'Project').replace(/[^A-Za-z0-9_-]/g, '') || 'Project',
  lib: null, proj: null, me: null, step: 1,
  log: [], es: null, lastSeq: 0, liveText: {},
  queue: new Set(), sort: { key: 'rank', dir: 'asc' }, sel: new Set(), cols: null,
  scoreText: null, rules: null, rates: null,
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
function toast(msg, kind = '') {
  const t = el('div', { class: 'toast ' + kind }, msg);
  $('#toasts').append(t); setTimeout(() => t.remove(), kind === 'bad' ? 9000 : 4500);
}
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined });
  let d = null; const txt = await r.text();
  try { d = JSON.parse(txt); } catch (e) { d = { detail: txt }; }
  if (!r.ok) throw new Error((d && (d.detail || d.error)) || `${r.status} ${r.statusText}`);
  return d;
}
function openModal(title, body, buttons) {
  const m = $('#modal'); $('.mh', m).textContent = title;
  $('.mb', m).replaceChildren(...body); $('.mf', m).replaceChildren(...buttons); m.hidden = false;
}
function closeModal() { $('#modal').hidden = true; }
const fmt = {
  num(v, d = 2) { if (v === null || v === undefined || v === '' || Number.isNaN(v)) return '—'; if (typeof v !== 'number') return String(v);
    if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(2) + ' M'; if (Math.abs(v) >= 10000) return Math.round(v).toLocaleString(); return Number.isInteger(v) ? String(v) : v.toFixed(d); },
  money(v) { if (v === null || v === undefined) return '—'; return '$' + (Math.abs(v) >= 1e6 ? (v / 1e6).toFixed(2) + ' M' : Math.round(v).toLocaleString()); },
  when(t) { return t ? new Date(t * 1000).toLocaleString() : ''; },
};
const STATUS = { pending: ['not designed', 'dim'], running: ['designing…', 'run'], done: ['done', 'ok'], failed: ['failed', 'bad'],
  np: ['not permitted', 'bad'], paused: ['paused', 'warn'], stopped: ['stopped', 'warn'] };
function statusPill(r) { const [t, k] = STATUS[r.status] || [r.status, '']; return el('span', { class: 'pill ' + k, title: r.reason || '' }, t); }

/* ---------------------------------------------------------------- load */
async function load() {
  $('#projname').textContent = S.project;
  [S.lib, S.me, S.proj] = await Promise.all([api('/api/library'), api('/api/me'), api(`/api/project/${enc(S.project)}`)]);
  if (S.rules === null) S.rules = { ...S.lib.default_rules, ...(S.proj.scoring.rules || {}) };
  if (S.rates === null) S.rates = { ...S.lib.default_rates, ...(S.proj.scoring.rates || {}) };
  if (S.scoreText === null) S.scoreText = S.proj.scoring.source_text || '';
  if (!S.sel.size) (S.proj.selection.ids || []).forEach(i => S.sel.add(i));
  if (!S.cols) S.cols = new Set(['modelled_cost', 'net_value', 'steel_tons', 'steel_psf', 'drift_utilisation', 'drift_margin',
    'drift_concentration_ratio', 'n_moment_conn', 'dc_max', 'T1_s', 'V_kip']);
  if (S.step === 1 && S.proj.plan.length) S.step = S.proj.rows.some(r => r.status === 'done') ? (S.proj.scoring.scored_at ? 5 : 4) : 3;
  if (S.proj.running) attachEvents();
  header(); render();
}
async function refresh() { S.proj = await api(`/api/project/${enc(S.project)}`); header(); }
function header() {
  const n = $('#status'); n.className = 'note';
  const bits = [];
  if (!S.me.steltic_url) { bits.push('HR Steel not installed'); n.classList.add('bad'); }
  else if (!S.me.steltic_up) bits.push('HR Steel server idle');
  bits.push(S.me.llm ? `model ${S.me.model}` : 'no LLM connection — built-in library only');
  n.textContent = bits.join(' · ');
}

/* ---------------------------------------------------------------- steps */
const STEPS = [[1, 'Base brief'], [2, 'Variations'], [3, 'Design'], [4, 'Score'], [5, 'Results']];
function stepState(k) {
  const p = S.proj;
  if (k === 1) return p.base_brief ? 'set' : '';
  if (k === 2) return p.plan.length ? `${p.plan.length} planned` : '';
  if (k === 3) { const d = p.rows.filter(r => r.status === 'done').length; return p.running ? 'running…' : (d ? `${d}/${p.rows.length} designed` : ''); }
  if (k === 4) return p.scoring.scored_at ? 'applied' : '';
  if (k === 5) return p.selection.recorded_at ? `${p.selection.ids.length} selected` : '';
  return '';
}
function render() {
  $('#steps').replaceChildren(...STEPS.map(([k, t]) => el('div', { class: 'step' + (S.step === k ? ' on' : '') + (stepState(k) && S.step !== k ? ' done' : ''),
    onclick: () => { S.step = k; render(); } }, el('span', { class: 'k' }, k), el('span', {}, t), stepState(k) ? el('span', { class: 'st' }, stepState(k)) : null)));
  const main = $('#main'); main.replaceChildren();
  ({ 1: paneBase, 2: panePlan, 3: paneRun, 4: paneScore, 5: paneResults })[S.step](main);
  main.scrollTop = 0;
}

/* ---------------------------------------------------------------- 1. base brief */
function paneBase(main) {
  const ta = el('textarea', { style: 'min-height:260px', placeholder: 'The building brief every variation starts from — the same text you would give HR Steel.' });
  ta.value = S.proj.base_brief || '';
  const save = async () => {
    try { S.proj = await api(`/api/project/${enc(S.project)}/base`, { method: 'POST', body: { brief: ta.value, source: 'typed' } }); toast('base brief saved', 'ok'); S.step = 2; render(); }
    catch (e) { toast(e.message, 'bad'); }
  };
  const fromDesign = async () => {
    try {
      const d = await api(`/api/project/${enc(S.project)}/base/from-design`);
      ta.value = d.brief; toast('brief loaded from this project’s HR Steel design — save it to continue', 'ok');
    } catch (e) { toast(e.message, 'bad'); }
  };
  main.append(el('div', { class: 'pane' },
    el('h2', {}, 'Base brief'),
    el('p', { class: 'lead' }, 'Every variation is this brief plus one stated change, designed as its own HR Steel job named ',
      el('code', {}, `${S.project}_M0xx`), '. Paste a brief, or take the one HR Steel already designed for this project.'),
    el('div', { class: 'card' }, ta,
      el('div', { class: 'row', style: 'margin-top:10px' },
        el('button', { class: 'primary', onclick: save }, 'Save brief and continue'),
        el('button', { onclick: fromDesign, disabled: !S.me.steltic_url }, 'Load from this project’s HR Steel design'),
        el('span', { class: 'hint' }, S.proj.base_source ? `source: ${S.proj.base_source}` : '')))));
}

/* ---------------------------------------------------------------- 2. variations */
function panePlan(main) {
  const p = S.proj;
  let mode = p.mode || 'categories';
  const cats = new Set(p.categories || []);
  const nIn = el('input', { type: 'number', min: 1, max: 400, value: p.n || 10 });
  const instr = el('textarea', { placeholder: 'e.g. "Compare braced cores against perimeter moment frames, include two BRB options and one with a site-specific hazard study; keep the floor plate as briefed."' });
  instr.value = p.instructions || '';
  const catBox = el('div', { class: 'cats' });
  const instrBox = el('div', {}, el('label', { class: 'f' }, 'Instructions for the study'), instr);
  const autoBox = el('p', { class: 'hint' }, 'The model chooses across all ten categories for the widest useful spread for this building.');
  const choices = el('div', { class: 'choices' });
  const paintMode = () => {
    choices.replaceChildren(...[
      ['categories', 'Pick categories', 'Choose any number of the ten categories; the model spreads the variations across them.'],
      ['instructions', 'Describe the study', 'Type what you want explored; the model turns it into a variation list.'],
      ['auto', 'Let the model decide', 'The model proposes the whole list from the brief alone.'],
    ].map(([id, t, s]) => el('div', { class: 'choice' + (mode === id ? ' on' : ''), onclick: () => { mode = id; paintMode(); } }, el('b', {}, t), el('span', {}, s))));
    catBox.hidden = mode !== 'categories'; instrBox.hidden = mode !== 'instructions'; autoBox.hidden = mode !== 'auto';
  };
  catBox.replaceChildren(...S.lib.categories.map(c => {
    const cb = el('input', { type: 'checkbox', checked: cats.has(c.id) });
    const n = el('label', { class: 'cat' + (cats.has(c.id) ? ' on' : '') }, cb,
      el('div', {}, el('b', {}, c.label), el('span', {}, c.blurb), el('i', {}, 'e.g. ' + c.examples.join(' · '))));
    cb.onchange = () => { cb.checked ? cats.add(c.id) : cats.delete(c.id); n.classList.toggle('on', cb.checked); };
    return n;
  }));
  paintMode();
  const go = el('button', { class: 'primary' }, 'Create variation list');
  go.onclick = async () => {
    go.disabled = true; go.textContent = S.me.llm ? 'asking the model…' : 'building the list…';
    try {
      S.proj = await api(`/api/project/${enc(S.project)}/plan`, { method: 'POST',
        body: { n: Number(nIn.value), mode, categories: [...cats], instructions: instr.value } });
      toast(`${S.proj.plan.length} variations planned`, 'ok'); render();
    } catch (e) { toast(e.message, 'bad'); go.disabled = false; go.textContent = 'Create variation list'; }
  };
  const pane = el('div', { class: 'pane' },
    el('h2', {}, 'Variations'),
    el('p', { class: 'lead' }, 'M001 is always the base brief unchanged. Every other variation changes one stated thing and says why — the design agent reads only that text.'),
    el('div', { class: 'card' },
      el('div', { class: 'row' }, el('label', { class: 'f', style: 'margin:0' }, 'Number of variations'), nIn, el('span', { class: 'hint' }, 'including M001')),
      choices, catBox, instrBox, autoBox,
      el('div', { class: 'row', style: 'margin-top:12px' }, go,
        p.plan.length ? el('span', { class: 'hint' }, 'creating a new list replaces the current one; designs already made are kept by id and flagged if their text changed') : null)));
  if (p.plan.length) pane.append(planTable());
  main.append(pane);
}
function planTable() {
  const p = S.proj;
  const rows = p.plan.map(v => ({ ...v }));
  const resultsById = Object.fromEntries(p.rows.map(r => [r.id, r]));
  const tbody = el('tbody');
  const cell = (r, k, cls = '') => el('td', { class: cls, contenteditable: 'plaintext-only', oninput: e => { r[k] = e.target.textContent; dirty(true); } }, r[k] || '');
  const paint = () => tbody.replaceChildren(...rows.map((r, i) => el('tr', {},
    el('td', { class: 'id' }, r.id), cell(r, 'group'), cell(r, 'title'), cell(r, 'change'), cell(r, 'why'),
    el('td', {}, resultsById[r.id] ? statusPill(resultsById[r.id]) : ''),
    el('td', {}, el('button', { class: 'ghost small', title: 'remove', onclick: () => { rows.splice(i, 1); renumber(); paint(); dirty(true); } }, '✕')))));
  const renumber = () => rows.forEach((r, i) => { r.id = 'M' + String(i + 1).padStart(3, '0'); });
  const save = el('button', { class: 'primary', disabled: true }, 'Save edits');
  const dirty = (on) => { save.disabled = !on; };
  save.onclick = async () => {
    try { S.proj = await api(`/api/project/${enc(S.project)}/plan`, { method: 'PUT', body: { plan: rows } }); toast('plan saved', 'ok'); render(); }
    catch (e) { toast(e.message, 'bad'); }
  };
  paint();
  return el('div', { class: 'card' },
    el('div', { class: 'row' }, el('h3', { style: 'margin:0' }, `Plan — ${rows.length} variations`),
      el('span', { class: 'hint' }, p.plan_source ? `from ${p.plan_source}` : ''), el('span', { class: 'sp' }),
      el('button', { class: 'small', onclick: () => { rows.push({ id: '', group: '', title: 'New variation', change: '', why: '' }); renumber(); paint(); dirty(true); } }, '+ Add row'),
      save, el('button', { class: 'small', onclick: () => { S.step = 3; render(); } }, 'Go to Design →')),
    p.plan_note ? el('p', { class: 'hint', style: 'margin:6px 0' }, p.plan_note) : null,
    el('p', { class: 'hint' }, 'Cells are editable — click to change a title, change or why. Ids renumber on save.'),
    el('div', { class: 'tablewrap', style: 'margin-top:8px' }, el('table', { class: 'plan' },
      el('thead', {}, el('tr', {}, el('th', {}, 'Id'), el('th', {}, 'Group'), el('th', {}, 'Title'), el('th', {}, 'Change (relative to the base brief)'), el('th', {}, 'Why'), el('th', {}, 'Status'), el('th', {}))),
      tbody)));
}

/* ---------------------------------------------------------------- 3. design */
function paneRun(main) {
  const p = S.proj;
  const pending = p.rows.filter(r => r.status !== 'done' || r.stale);
  const list = el('div', { class: 'runlist' });
  const paintList = () => list.replaceChildren(...p.rows.map(r => {
    const cb = el('input', { type: 'checkbox', checked: S.queue.has(r.id), onchange: e => { e.target.checked ? S.queue.add(r.id) : S.queue.delete(r.id); paintButtons(); } });
    return el('label', { class: 'rv ' + r.status + (S.queue.has(r.id) ? ' q' : ''), title: (r.reason || r.change || '').slice(0, 300) },
      cb, el('span', { class: 'd' }), el('span', { class: 't' }, `${r.id} ${r.title}`), r.stale ? el('span', { class: 'pill warn', title: 'text changed since it was designed' }, 'changed') : statusPill(r));
  }));
  const runPending = el('button', { class: 'primary' });
  const runChecked = el('button', {});
  const stop = el('button', { class: 'danger' }, 'Stop');
  const paintButtons = () => {
    runPending.textContent = `Design pending (${pending.length})`; runPending.disabled = p.running || !pending.length;
    runChecked.textContent = `Design checked (${S.queue.size})`; runChecked.disabled = p.running || !S.queue.size;
    stop.disabled = !p.running;
  };
  const start = async (body) => {
    try { await api(`/api/project/${enc(S.project)}/run`, { method: 'POST', body }); S.log = []; S.liveText = {}; S.lastSeq = 0; await refresh(); attachEvents(); render(); }
    catch (e) { toast(e.message, 'bad'); }
  };
  runPending.onclick = () => start({});
  runChecked.onclick = () => start({ ids: [...S.queue] });
  stop.onclick = async () => { await api(`/api/project/${enc(S.project)}/stop`, { method: 'POST' }); toast('stopping after the current step'); };
  paintList(); paintButtons();
  const done = p.rows.filter(r => r.status === 'done').length;
  const bar = el('div', { class: 'progress' }, el('i', { style: `width:${p.rows.length ? 100 * done / p.rows.length : 0}%` }));
  const logBox = el('div', { class: 'log', id: 'log' });
  main.append(el('div', { class: 'pane' },
    el('h2', {}, 'Design the variations'),
    el('p', { class: 'lead' }, 'Each variation is sent to HR Steel as its own building (', el('code', {}, `${S.project}_M0xx`),
      ') one after another; the package is downloaded when it finishes and its metrics are read. Closing this tab does not stop a study — reopen it to re-attach.'),
    !S.me.steltic_url ? el('div', { class: 'card', style: 'border-color:var(--bad)' }, 'HR Steel is not installed. Install it from the Modules tab — it designs the variations.') : null,
    !S.me.has_creds ? el('div', { class: 'card', style: 'border-color:var(--warn)' }, 'No LLM connection: HR Steel cannot design without one. Set it with the Connection button in the title bar.') : null,
    S.me.has_creds && !S.me.llm ? el('div', { class: 'card tight' }, el('span', { class: 'hint' }, 'Model MOCK: HR Steel produces its mock design for each variation (useful to test the study); the plan and the report reading use the built-in library.')) : null,
    el('div', { class: 'card' }, el('div', { class: 'row' }, runPending, runChecked, stop, el('span', { class: 'sp' }),
      el('span', { class: 'hint' }, `${done} of ${p.rows.length} designed`)), bar, list),
    el('div', { class: 'card' }, el('div', { class: 'row' }, el('h3', { style: 'margin:0' }, 'Log'), el('span', { class: 'sp' }),
      el('button', { class: 'small', onclick: () => { S.step = 4; render(); } }, 'Go to Score →')), logBox)));
  paintLog();
}
function attachEvents() {
  if (S.es) { S.es.close(); S.es = null; }
  const es = new EventSource(`/api/project/${enc(S.project)}/events?since=${S.lastSeq}`);
  S.es = es;
  es.onmessage = async (m) => {
    let ev; try { ev = JSON.parse(m.data); } catch (e) { return; }
    if (ev.seq) S.lastSeq = ev.seq;
    if (ev.type === 'finished') {
      es.close(); S.es = null;
      if (!ev.idle) { await refresh(); if (S.step === 3) render(); else paintLog(); }
      return;
    }
    if (ev.type === 'text') { S.liveText[ev.id] = ((S.liveText[ev.id] || '') + ev.text).slice(-800); const last = S.log[S.log.length - 1];
      if (last && last.live === ev.id) last.t = S.liveText[ev.id]; else S.log.push({ k: 'text', live: ev.id, t: S.liveText[ev.id] }); }
    else if (ev.type === 'variation') {
      if (ev.status === 'running') { S.liveText[ev.id] = ''; S.log.push({ k: 'var', t: `▶ ${ev.id} ${ev.title}  (${ev.n}/${ev.of})` }); }
      else { S.log.push({ k: ev.status === 'done' ? 'ok' : 'err', t: `■ ${ev.id} ${ev.status}${ev.reason ? ': ' + ev.reason : ''}` }); await refresh(); if (S.step === 3) { render(); return; } }
    }
    else if (ev.type === 'tool' || ev.type === 'tool_result') S.log.push({ k: 'tool', t: (ev.type === 'tool' ? '→ ' : '← ') + ev.text });
    else if (ev.type === 'usage') { /* quiet */ }
    else S.log.push({ k: /^(error|paused)/.test(ev.text || '') ? 'err' : 'l', t: (ev.id ? ev.id + ' ' : '') + (ev.text || '') });
    if (S.log.length > 600) S.log.splice(0, 100);
    paintLog();
  };
  es.onerror = () => { /* the browser retries; a finished study answers with `finished` and we close */ };
}
function paintLog() {
  const box = $('#log'); if (!box) return;
  const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  box.replaceChildren(...S.log.map(l => el('div', { class: 'l ' + l.k }, l.t)));
  if (stick) box.scrollTop = box.scrollHeight;
}

/* ---------------------------------------------------------------- 4. score */
const RULES = [
  ['require_done', 'Design finished (status DONE)'],
  ['require_checks_pass', 'Every code check passing (no D/C > 1.0, no failing checks)'],
  ['drift_utilisation_max', 'Drift utilisation ≤', 'num'],
  ['smf_share_min_pct', 'Moment-frame share ≥ (%, dual systems only)', 'num'],
  ['require_rho_ax_applied', 'ρ and Ax applied (both stated in the package)'],
  ['no_extreme_torsion', 'No Type 1b extreme torsional irregularity'],
  ['wind_comfort_max_mg', 'Wind comfort ≤ (milli-g, when reported)', 'num'],
  ['representable_only', 'Representable in the nonlinear tools (SMF, SCBF, dual SMF+SCBF, R=3 X-braced)'],
];
function rulesEditor() {
  return el('div', { class: 'rules' }, ...RULES.map(([k, label, kind]) => {
    if (kind === 'num') {
      const on = el('input', { type: 'checkbox', checked: S.rules[k] !== null && S.rules[k] !== undefined && S.rules[k] !== false });
      const v = el('input', { type: 'number', step: 'any', value: S.rules[k] ?? S.lib.default_rules[k], disabled: !on.checked });
      on.onchange = () => { v.disabled = !on.checked; S.rules[k] = on.checked ? Number(v.value) : null; };
      v.onchange = () => { S.rules[k] = Number(v.value); };
      return el('label', { class: 'rule' }, on, el('span', {}, label), v);
    }
    const on = el('input', { type: 'checkbox', checked: !!S.rules[k], onchange: e => { S.rules[k] = e.target.checked; } });
    return el('label', { class: 'rule' }, on, el('span', {}, label));
  }));
}
function criteriaPills(rules) {
  const r = rules || S.rules; const out = [];
  if (r.require_done) out.push('status DONE');
  if (r.require_checks_pass) out.push('every code check passing');
  if (r.drift_utilisation_max != null) out.push(`drift utilisation ≤ ${r.drift_utilisation_max}`);
  if (r.smf_share_min_pct != null) out.push(`SMF share ≥ ${r.smf_share_min_pct} % (dual systems)`);
  if (r.require_rho_ax_applied) out.push('ρ and Ax applied');
  if (r.no_extreme_torsion) out.push('no Type 1b');
  if (r.wind_comfort_max_mg != null) out.push(`wind comfort ≤ ${r.wind_comfort_max_mg} mg`);
  if (r.representable_only) out.push('representable in nonlinear tools');
  return el('div', { class: 'crit' }, ...out.map(t => el('span', { class: 'pill ok' }, t)));
}
function paneScore(main) {
  const p = S.proj;
  const eq = S.lib.default_equation;
  const copy = el('button', { class: 'small', onclick: async () => { try { await navigator.clipboard.writeText(eq); toast('equation copied', 'ok'); } catch (e) { ta.value = eq; toast('copied into the box'); } } }, 'Copy');
  const ta = el('textarea', { class: 'mono', style: 'min-height:90px', placeholder: 'Leave empty to use the default. Paste the equation and change the weights, write your own S = … with the metric names below, or describe what matters in words (e.g. "cost matters most, then drift margin; ignore connections") and the model writes the equation.' });
  ta.value = S.scoreText || ''; ta.oninput = () => { S.scoreText = ta.value; };
  const ratesBox = el('div', { class: 'row' }, ...Object.entries(S.lib.default_rates).map(([k, d]) => {
    const inp = el('input', { type: 'number', step: 'any', value: S.rates[k] ?? d, onchange: e => { S.rates[k] = Number(e.target.value); } });
    return el('label', { class: 'rule' }, el('span', {}, { steel_per_ton: '$ per ton of steel', per_moment_conn: '$ per moment connection', per_brace: '$ per brace', revenue_per_sf: '$ revenue per sf' }[k] || k), inp);
  }));
  const apply = el('button', { class: 'primary' }, 'Apply scoring and rank');
  const out = el('div');
  apply.onclick = async () => {
    apply.disabled = true; apply.textContent = 'scoring…';
    try {
      const d = await api(`/api/project/${enc(S.project)}/score`, { method: 'POST', body: { text: S.scoreText || '', rules: S.rules, rates: S.rates } });
      S.proj.scoring = d.scoring; S.proj.rows = d.rows;
      const sc = d.scoring;
      out.replaceChildren(el('div', { class: 'card', style: 'border-color:var(--ok)' },
        el('div', { class: 'kv' }, 'Equation used (', sc.equation_source, '):'), el('pre', { class: 'mono', style: 'color:var(--accent);margin:6px 0;white-space:pre-wrap' }, 'S = ' + sc.equation),
        sc.explanation ? el('div', { class: 'kv' }, sc.explanation) : null,
        el('div', { class: 'kv' }, `${sc.summary.n_eligible} eligible of ${d.rows.length}; ${sc.summary.n_scored} scored`),
        el('div', { class: 'row', style: 'margin-top:8px' }, el('button', { class: 'primary', onclick: () => { S.step = 5; render(); } }, 'See results →'))));
      toast('scored', 'ok');
    } catch (e) { toast(e.message, 'bad'); }
    apply.disabled = false; apply.textContent = 'Apply scoring and rank';
  };
  main.append(el('div', { class: 'pane' },
    el('h2', {}, 'Score'),
    el('p', { class: 'lead' }, 'Only eligible variations are scored and ranked; the rest are listed with the reason. The default score is the one used in the reference study — the weights are stated so you can re-weight them.'),
    el('div', { class: 'card' }, el('h3', { style: 'margin-top:0' }, 'Eligibility criteria'), rulesEditor()),
    el('div', { class: 'card' }, el('h3', { style: 'margin-top:0' }, 'Default score equation'),
      el('div', { class: 'eq' }, el('pre', {}, eq), copy),
      el('p', { class: 'hint', style: 'margin-top:6px' }, 'Weights: 0.35 cost · 0.20 drift margin · 0.15 drift concentration · 0.15 net value · 0.15 moment connections. Each term is 0–1, higher is better; *_max is taken over the eligible variations.'),
      el('h3', {}, 'Your scoring requirements'), ta,
      el('h3', {}, 'Cost model'), ratesBox,
      el('p', { class: 'hint' }, 'modelled_cost = steel tons × rate + moment connections × rate + braces × rate; revenue_proxy = gross floor area × rate; net_value = revenue − cost.'),
      el('div', { class: 'row', style: 'margin-top:12px' }, apply,
        p.scoring.scored_at ? el('span', { class: 'hint' }, `last applied ${fmt.when(p.scoring.scored_at)}`) : null)),
    out,
    el('div', { class: 'card' }, el('h3', { style: 'margin-top:0' }, 'Metrics you can use'),
      el('div', { class: 'metrics' }, ...S.lib.metrics.map(m => el('div', {}, el('code', {}, m.name), ` — ${m.description} [${m.unit}]`))))));
}

/* ---------------------------------------------------------------- 5. results */
const FIXED = [['rank', '#', 'num'], ['id', 'Id'], ['title', 'Variation'], ['group', 'Group'], ['status', 'Status'], ['score', 'Score', 'num']];
function paneResults(main) {
  const p = S.proj; const sc = p.scoring;
  if (!sc.scored_at) { main.append(el('div', { class: 'pane' }, el('h2', {}, 'Results'), el('p', { class: 'lead' }, 'Apply a score first.'),
    el('button', { class: 'primary', onclick: () => { S.step = 4; render(); } }, '← Score'))); return; }
  const metricCols = [...S.cols].filter(c => S.lib.metrics.some(m => m.name === c));
  const cols = [...FIXED, ...metricCols.map(c => [c, c, 'num']), ['ineligible', 'Not eligible because'], ['links', 'Files']];
  const rows = p.rows.map(r => ({ ...r, ineligible: r.ineligible || [] }));
  const val = (r, k) => (k in r ? r[k] : r.metrics[k]);
  const sorted = [...rows].sort((a, b) => {
    const k = S.sort.key, d = S.sort.dir === 'asc' ? 1 : -1;
    let x = val(a, k), y = val(b, k);
    if (Array.isArray(x)) x = x.length; if (Array.isArray(y)) y = y.length;
    const nx = x === null || x === undefined || x === '', ny = y === null || y === undefined || y === '';
    if (nx && ny) return a.id < b.id ? -1 : 1; if (nx) return 1; if (ny) return -1;
    if (typeof x === 'number' && typeof y === 'number') return (x - y) * d;
    return String(x).localeCompare(String(y)) * d;
  });
  const head = el('tr', {}, el('th', {}, ''), ...cols.map(([k, label, kind]) => {
    if (k === 'links') return el('th', {}, label);
    const on = S.sort.key === k;
    return el('th', { class: 'sortable' + (kind === 'num' ? ' num' : '') + (on ? ' sorted' : ''), title: 'click to sort; click again to flip',
      onclick: () => { S.sort = on ? { key: k, dir: S.sort.dir === 'asc' ? 'desc' : 'asc' } : { key: k, dir: (kind === 'num' && k !== 'rank') ? 'desc' : 'asc' }; render(); } },
      label, on ? el('span', { class: 'arrow' }, S.sort.dir === 'asc' ? '▲' : '▼') : null);
  }));
  const body = el('tbody', {}, ...sorted.map(r => {
    const cb = el('input', { type: 'checkbox', checked: S.sel.has(r.id), onchange: e => { e.target.checked ? S.sel.add(r.id) : S.sel.delete(r.id); paintSel(); tr.classList.toggle('sel', e.target.checked); } });
    const tr = el('tr', { class: (r.rank && r.rank <= 3 ? ' top' + r.rank : '') + (!r.eligible ? ' inel' : '') + (S.sel.has(r.id) ? ' sel' : '') },
      el('td', {}, cb), ...cols.map(([k, , kind]) => {
        if (k === 'links') return el('td', {}, ...(r.files || []).filter(f => f !== 'package.zip').map(f => el('a', { href: `/api/project/${enc(S.project)}/file/${r.id}/${f}`, target: '_blank', style: 'margin-right:8px' }, f === 'report.html' ? 'report' : 'viewer')),
          (r.files || []).includes('package.zip') ? el('a', { href: `/api/project/${enc(S.project)}/file/${r.id}/package.zip` }, 'zip') : null);
        if (k === 'rank') return el('td', { class: 'num' }, r.rank ? (r.rank <= 3 ? el('span', { class: 'medal m' + r.rank }, r.rank) : r.rank) : '');
        if (k === 'id') return el('td', { class: 'id' }, r.id);
        if (k === 'status') return el('td', {}, statusPill(r), r.stale ? el('span', { class: 'pill warn', style: 'margin-left:4px' }, 'changed') : null);
        if (k === 'score') return el('td', { class: 'num', title: r.score_note || '' }, r.score === null || r.score === undefined ? (r.score_note ? '?' : '—') : r.score.toFixed(3));
        if (k === 'ineligible') return el('td', { class: 'why' }, r.ineligible.join('; '));
        if (k === 'title') return el('td', { class: 'title', title: r.change || '' }, r.title);
        if (k in r) return el('td', {}, r[k] === null || r[k] === undefined ? '' : String(r[k]));
        const v = r.metrics[k];
        const est = k === 'n_moment_conn' && r.metrics.n_moment_conn_estimated;
        return el('td', { class: 'num', title: est ? 'estimated: perimeter frames, every bay, both ends, every level (no LLM to read the report)' : '' },
          (k === 'modelled_cost' || k === 'net_value' || k === 'revenue_proxy') ? fmt.money(v) : fmt.num(v), est ? el('span', { class: 'est' }, '~') : null);
      }));
    return tr;
  }));
  const selNote = el('textarea', { style: 'min-height:60px', placeholder: 'Why these (optional) — recorded with the selection.' });
  selNote.value = p.selection.note || '';
  const selInfo = el('span', { class: 'hint' });
  const record = el('button', { class: 'primary' });
  const paintSel = () => { const s = S.proj.selection; record.textContent = `Record selection (${S.sel.size})`; record.disabled = !S.sel.size;
    selInfo.textContent = s.recorded_at ? `recorded ${fmt.when(s.recorded_at)}: ${s.ids.join(', ')}` : 'nothing recorded yet'; };
  record.onclick = async () => {
    try {
      const d = await api(`/api/project/${enc(S.project)}/select`, { method: 'POST', body: { ids: [...S.sel].sort(), note: selNote.value } });
      await refresh(); paintSel();
      toast(`selection recorded — ${d.selection.selected.length} package(s) copied to variations/selected/ for the other modules`, 'ok');
    } catch (e) { toast(e.message, 'bad'); }
  };
  paintSel();
  const colPicker = () => {
    const boxes = S.lib.metrics.map(m => { const cb = el('input', { type: 'checkbox', checked: S.cols.has(m.name), onchange: e => { e.target.checked ? S.cols.add(m.name) : S.cols.delete(m.name); } });
      return el('label', { class: 'rule' }, cb, el('span', {}, el('code', {}, m.name), ` — ${m.description}`)); });
    openModal('Columns', [el('div', { class: 'rules', style: 'grid-template-columns:1fr' }, ...boxes)],
      [el('button', { class: 'primary', onclick: () => { closeModal(); render(); } }, 'Apply'), el('button', { class: 'ghost', onclick: closeModal }, 'Cancel')]);
  };
  const top = rows.filter(r => r.rank && r.rank <= 3).sort((a, b) => a.rank - b.rank);
  main.append(el('div', { class: 'pane' },
    el('h2', {}, 'Results'),
    el('div', { class: 'card tight' },
      el('div', { class: 'row' }, el('b', { style: 'font-size:12px;color:var(--dim)' }, 'ELIGIBLE = '), criteriaPills(sc.rules)),
      el('div', { class: 'row', style: 'margin-top:6px' }, el('b', { style: 'font-size:12px;color:var(--dim)' }, 'SCORE = '), el('code', { style: 'color:var(--accent);font-size:12px' }, sc.equation),
        el('span', { class: 'hint' }, `(${sc.equation_source || 'default'}; ${sc.summary.n_eligible} eligible, ${sc.summary.n_scored} scored, applied ${fmt.when(sc.scored_at)})`))),
    top.length ? el('div', { class: 'card tight' }, el('div', { class: 'row' }, el('b', { style: 'font-size:12px;color:var(--dim)' }, 'TOP SCORERS'),
      ...top.map(r => el('span', { class: 'pill ok', style: 'text-transform:none;letter-spacing:0;font-size:12px' }, el('span', { class: 'medal m' + r.rank }, r.rank), `${r.id} ${r.title} — ${r.score.toFixed(3)}`)))) : null,
    el('div', { class: 'card' },
      el('div', { class: 'row', style: 'margin-bottom:8px' }, el('span', { class: 'hint' }, 'Click a column header to sort (max → min, click again for min → max). Dimmed rows are not eligible; ~ marks an estimated value.'),
        el('span', { class: 'sp' }), el('button', { class: 'small', onclick: colPicker }, 'Columns ▾'),
        el('a', { class: 'hint', href: '#', onclick: (e) => { e.preventDefault(); S.sort = { key: 'rank', dir: 'asc' }; render(); } }, 'reset sort')),
      el('div', { class: 'tablewrap' }, el('table', {}, el('thead', {}, head), body))),
    el('div', { class: 'card' }, el('h3', { style: 'margin-top:0' }, 'Your selection'),
      el('p', { class: 'hint' }, 'Tick the variations you take forward, then record them. The selection and each package are written into this project (', el('code', {}, 'variations/selected/'), ' and ', el('code', {}, 'selected_variations.json'), ') so the Nonlinear module and the other modules can pick them up with "in project ▾".'),
      selNote, el('div', { class: 'row', style: 'margin-top:8px' }, record, selInfo))));
}

load().catch(e => { document.body.append(el('div', { class: 'pane', style: 'padding:30px;color:var(--bad)' }, 'The variations module could not load: ' + e.message)); });
