/* User-designed building -- five steps: file -> questions -> build -> confirm -> run. */
'use strict';

const qs = new URLSearchParams(location.search);
const S = {
  project: (qs.get('project') || 'Project').replace(/[^A-Za-z0-9_-]/g, '') || 'Project',
  step: 1, hr: null, intake: null, answers: {}, checks: null, run: null, next: 0, log: [], poll: null,
  opts: loadOpts(),            // step 5's records / site class / opt-out: kept across the 3-second re-render
};
// Step 5's choices live here, not in the inputs: the page re-renders every 3 s while a build or a run is going,
// and an input rebuilt from its default lost the user's choice (the opt-out box came back ticked for Collect).
function loadOpts() {
  const d = { n_records: 11, site_class: 'D', skip_collect: false };
  try { return Object.assign(d, JSON.parse(localStorage.getItem('steltic.ub.run') || '{}')); } catch (e) { return d; }
}
function saveOpts() { try { localStorage.setItem('steltic.ub.run', JSON.stringify(S.opts)); } catch (e) { /* private window */ } }
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
  if (!r.ok && r.status !== 409 && r.status !== 202) throw new Error((d && (d.detail || d.error)) || `${r.status} ${r.statusText}`);
  d.__status = r.status;
  return d;
}
const P = () => `/api/project/${enc(S.project)}`;

// ------------------------------------------------------------------------------------------- state
function mi() { return (S.hr && S.hr.missing_items) || (S.intake && S.intake.missing_items) || null; }
function hrState() { return (S.hr && S.hr.state) || 'none'; }
function built() { return hrState() === 'built'; }
function confirmed() { return !!(S.checks && S.checks.nl_basis && S.checks.nl_basis.confirmed); }

const STEPS = [
  { k: 1, t: 'Your file', st: () => mi() ? 'read' : '' },
  { k: 2, t: 'Questions', st: () => { const m = mi(); if (!m) return ''; return m.ready ? 'none open' : `${m.counts.key} key · ${m.counts.default} default · ${m.counts.refused} refused`; } },
  { k: 3, t: 'Build (HR)', st: () => ({ running: 'building…', built: 'built', failed: 'failed', needs_input: 'needs input' }[hrState()] || '') },
  { k: 4, t: 'Confirm', st: () => confirmed() ? 'confirmed' : '' },
  { k: 5, t: 'Nonlinear suite', st: () => (S.run && S.run.state && S.run.state !== 'none') ? S.run.state : '' },
];
function renderSteps() {
  $('#steps').replaceChildren(...STEPS.map(s => el('div', { class: 'step' + (S.step === s.k ? ' on' : '') + (s.st() && s.k < S.step ? ' done' : ''),
    onclick: () => { S.step = s.k; render(); } }, el('span', { class: 'k' }, s.k), s.t, el('span', { class: 'st' }, s.st()))));
}
async function refresh() {
  try { S.hr = await api(`${P()}/hr`); } catch (e) { S.hr = null; $('#status').textContent = e.message; $('#status').className = 'note bad'; }
  if (built()) { try { S.checks = await api(`${P()}/checks`); } catch (e) { S.checks = null; } }
  try { const r = await api(`${P()}/run?since=0`); S.run = r; S.log = r.events || []; S.next = r.next || 0; } catch (e) { /* none yet */ }
}
function render() {
  renderSteps();
  $('#projname').textContent = S.project;
  const m = $('#main');
  m.replaceChildren(el('div', { class: 'pane' }, [null, step1, step2, step3, step4, step5][S.step]()));
}

// ------------------------------------------------------------------------------------------- 1 file
function step1() {
  const f = el('input', { type: 'file', accept: '.xlsx,.xlsm,.e2k,.$et,.json' });
  const rep = el('input', { type: 'checkbox' });
  const go = el('button', { class: 'primary', onclick: async () => {
    if (!f.files.length) return toast('Choose a file first', 'bad');
    const fd = new FormData(); fd.append('file', f.files[0]); if (rep.checked) fd.append('replace', '1');
    go.disabled = true; go.textContent = 'Reading…';
    try {
      const r = await api(`${P()}/upload`, { method: 'POST', body: fd, raw: true });
      if (r.__status === 409) { toast(r.detail || 'refused', 'bad'); return; }
      S.intake = r; await refresh(); S.step = 2; render();
    } catch (e) { toast(e.message, 'bad'); } finally { go.disabled = false; go.textContent = 'Read the file'; }
  } }, 'Read the file');
  const s = S.intake && S.intake.summary;
  return el('div', {},
    el('div', { class: 'about' },
      el('p', {}, el('b', {}, 'A building you designed. '), 'HR Steel models it exactly as given: nothing is resized and nothing is invented. Anything key that your file does not settle comes back as a question; a proposed default is used only once you accept it, and every accepted default is printed in every report.'),
      el('p', {}, 'Give the ', el('b', {}, 'Steltic building template'), ' (Excel) or an ', el('b', {}, 'ETABS .e2k'), ' text model. Version 1 systems: SMF, IMF, OMF, SCBF, OCBF, BRBF, EBF and their dual combinations. Plate shear walls, STMF, partially restrained connections, sloped or plan-diagonal beams, several diaphragm types and per-member grades are refused, never simplified.')),
    el('div', { class: 'card' },
      el('div', { class: 'row' }, el('a', { href: '/api/template', download: 'steltic_building_template.xlsx' }, 'Download the blank template'),
        S.hr && S.hr.state !== 'none' ? el('a', { href: `/api/template?project=${enc(S.project)}` }, 'Download what HR has read, as a filled template') : null),
      el('h3', {}, 'Your file'), el('div', { class: 'row' }, f, go),
      el('label', { class: 'kv', style: 'display:block;margin-top:8px' }, rep, ' replace a normal HR Steel design of the same name (it is archived, not deleted)')),
    s ? el('div', { class: 'card tight' }, el('div', { class: 'kv' },
      el('b', {}, `${s.source && s.source.file || ''}`), ` · ${s.levels} levels · grid ${s.grid.x} × ${s.grid.y} · ${s.columns} columns · ${s.beams} beams · ${s.braces} braces`),
      (s.notes || []).length ? el('ul', { class: 'small' }, s.notes.map(n => el('li', {}, n))) : null) : null);
}

// ------------------------------------------------------------------------------------------- 2 questions
function answerInput(it) {
  const cur = S.answers[it.id];
  if (it.kind === 'refused' || it.kind === 'info') return null;
  const wrap = el('div', { class: 'ans' });
  let inp;
  if (it.options && it.options.length) {
    inp = el('select', {}, el('option', { value: '' }, '— choose —'), it.options.map(o => el('option', { value: String(o) }, String(o))));
  } else inp = el('input', { type: 'text', placeholder: it.kind === 'default' ? 'or give your own value' : 'your value' });
  if (cur && 'value' in cur) inp.value = typeof cur.value === 'object' ? JSON.stringify(cur.value) : String(cur.value);
  inp.onchange = () => { const v = inp.value.trim(); if (v === '') delete S.answers[it.id]; else S.answers[it.id] = { value: parseVal(v) }; };
  if (it.kind === 'default') {
    const acc = el('button', { class: 'small' + (cur && cur.accept ? ' primary' : ''), onclick: () => { S.answers[it.id] = { accept: true }; render(); } },
      'Accept ' + JSON.stringify(it.default));
    wrap.append(acc, inp, el('span', { class: 'kv' }, it.default_reason || ''));
  } else wrap.append(inp);
  return wrap;
}
function parseVal(v) {
  if (/^-?\d+(\.\d+)?$/.test(v)) return parseFloat(v);
  if (v === 'true' || v === 'false') return v === 'true';
  if (/^[\[{]/.test(v)) { try { return JSON.parse(v); } catch (e) { return v; } }
  return v;
}
function step2() {
  const m = mi();
  if (!m) return el('div', { class: 'card' }, 'Read a file first (step 1).');
  const save = el('button', { class: 'primary', onclick: async () => {
    save.disabled = true;
    try { const r = await api(`${P()}/answers`, { method: 'POST', body: { answers: S.answers } }); S.answers = {}; await refresh();
      toast(r.missing_items.ready ? 'Nothing open: HR can build the model' : 'Saved', 'ok'); render();
    } catch (e) { toast(e.message, 'bad'); } finally { save.disabled = false; }
  } }, 'Save answers');
  const groups = (m.groups || []).map(g => el('div', {}, el('div', { class: 'grp' }, g.group),
    g.items.map(it => el('div', { class: 'item ' + it.kind }, el('div', { class: 'q' }, el('span', { class: 'tag ' + it.kind }, it.kind), it.question),
      el('div', { class: 'why' }, 'needed for: ' + it.needed_for + (it.where ? ' · ' + it.where : '')), answerInput(it)))));
  return el('div', {},
    el('h2', {}, 'What HR needs before it can build'),
    el('p', { class: 'lead' }, 'Key items have no default: the build never starts without them. A proposed default is used only once you accept it. Refused items are outside version 1: change them in your model, or the building cannot be run.'),
    el('div', { class: 'counts' }, ['key', 'default', 'refused', 'info'].map(k => el('span', {}, `${m.counts[k]} ${k}`))),
    el('div', { class: 'row' }, save, m.ready ? el('button', { onclick: () => { S.step = 3; render(); } }, 'Go to Build →') : null),
    groups.length ? groups : el('div', { class: 'card' }, 'Nothing open.'));
}

// ------------------------------------------------------------------------------------------- 3 build
function step3() {
  const m = mi(); const st = S.hr || {};
  const go = el('button', { class: 'primary', disabled: !(m && m.ready) || hrState() === 'running', onclick: async () => {
    try { const r = await api(`${P()}/build`, { method: 'POST' }); if (r.__status === 409) { toast('Questions are still open (step 2)', 'bad'); return; }
      toast('HR is building the model…'); startPoll(); await refresh(); render(); } catch (e) { toast(e.message, 'bad'); }
  } }, built() ? 'Rebuild' : 'Build the model');
  const res = (st.status && st.status.result) || {};
  const pv = st.provenance || {};
  return el('div', {},
    el('h2', {}, 'HR Steel builds the model — build-only'),
    el('p', { class: 'lead' }, 'HR translates your description into its model one for one and runs its normal pipeline (model files, demands, sanity checks, report). Member D/C ratios are not computed for a user design.'),
    el('div', { class: 'row' }, go, el('span', { class: 'note' }, ({ running: 'building… (large buildings take several minutes)', built: 'built', failed: 'failed', needs_input: 'questions open', ready: 'ready to build' }[hrState()]) || '')),
    hrState() === 'failed' ? el('pre', { class: 'card', style: 'white-space:pre-wrap;color:var(--bad)' }, (st.status && (st.status.error || (st.status.result || {}).error)) || 'failed') : null,
    built() ? el('div', { class: 'card' },
      el('div', { class: 'kv' }, `T = ${(res.summary && res.summary.T || []).slice(0, 3).map(t => t.toFixed(3)).join(' / ')} s · V = ${res.summary && res.summary.V ? res.summary.V.toFixed(0) : '—'} kip · W = ${res.summary && res.summary.W ? res.summary.W.toFixed(0) : '—'} kip · model declared ${res.model_declared ? 'PASS' : 'FAIL'} · consistent ${res.model_consistent ? 'PASS' : 'FAIL'}`),
      (res.failed_checks || []).length ? el('div', { class: 'kv' }, 'HR sanity checks reported (informative, never fixed): ' + res.failed_checks.join(', ')) : null,
      el('h3', {}, 'Derived by rule (nothing invented)'), el('ul', { class: 'small' }, (pv.derived || []).slice(0, 40).map(d => el('li', {}, `${d.what} — ${d.rule}`))),
      el('h3', {}, 'Accepted defaults'), el('ul', { class: 'small' }, (pv.accepted_defaults || []).map(d => el('li', {}, `${d.item} = ${JSON.stringify(d.value)}`))),
      el('div', { class: 'row', style: 'margin-top:8px' }, el('a', { href: `${P()}/file/report.html`, target: '_blank' }, 'HR report ↗'), el('button', { onclick: () => { S.step = 4; render(); } }, 'Go to Confirm →'))) : null);
}

// ------------------------------------------------------------------------------------------- 4 confirm
function step4() {
  if (!built()) return el('div', { class: 'card' }, 'HR has not built the model yet (step 3).');
  const c = S.checks;
  const by = el('input', { type: 'text', placeholder: 'your name (printed in the reports)' });
  const note = el('textarea', { placeholder: 'optional note, printed in every report (e.g. why a difference from your own results is accepted)', style: 'min-height:60px' });
  const go = el('button', { class: 'primary', disabled: !(c && c.ok), onclick: async () => {
    try { const r = await api(`${P()}/confirm`, { method: 'POST', body: { by: by.value, note: note.value } });
      if (!r.confirmed) { toast('Not confirmed: a check fails', 'bad'); return; } toast('Confirmed', 'ok'); await refresh(); S.step = 5; render();
    } catch (e) { toast(e.message, 'bad'); } } }, 'Confirm the model');
  return el('div', {},
    el('h2', {}, 'Check the model, then confirm it'),
    el('p', { class: 'lead' }, 'The long runs start only on a confirmed model. Differences from your own T1, V and W are warnings: they do not block, and they are printed in every report.'),
    el('iframe', { class: 'viewer', src: `${P()}/file/viewer_3d.html` }),
    c ? el('table', { class: 'ck' }, el('tr', {}, el('th', {}, 'Check'), el('th', {}, 'Result'), el('th', {}, 'Detail')),
      c.checks.map(x => el('tr', {}, el('td', {}, x.label + (x.hard ? '' : ' (warning only)')), el('td', { class: 'st-' + x.status }, x.status.toUpperCase()), el('td', {}, x.detail)))) : el('div', { class: 'card' }, 'Checks unavailable.'),
    c && (c.accepted_defaults || []).length ? el('div', { class: 'kv', style: 'margin:8px 0' }, 'Accepted defaults: ' + c.accepted_defaults.map(d => `${d.item} = ${JSON.stringify(d.value)}`).join('; ')) : null,
    confirmed() ? el('div', { class: 'banner' }, `Confirmed by ${c.nl_basis.by} on ${c.nl_basis.at}. Reports will read: user-designed building — design basis supplied by the user, not checked by Steltic HR.`) : null,
    el('div', { class: 'card' }, el('label', { class: 'f' }, 'Confirmed by'), by, el('label', { class: 'f', style: 'margin-top:8px' }, 'Note'), note,
      el('div', { class: 'row', style: 'margin-top:10px' }, go)));
}

// ------------------------------------------------------------------------------------------- 5 run
function step5() {
  if (!confirmed()) return el('div', { class: 'card' }, 'Confirm the model first (step 4).');
  const o = S.opts;
  const nrec = el('input', { type: 'number', value: o.n_records, min: 1, onchange: () => { o.n_records = parseInt(nrec.value || '11', 10); saveOpts(); } });
  const site = el('select', { onchange: () => { o.site_class = site.value; saveOpts(); } },
    ['A', 'B', 'C', 'D', 'E'].map(x => el('option', { value: x, selected: x === o.site_class }, x)));
  const collected = !!(S.run && S.run.collected);
  const skip = el('input', { type: 'checkbox', checked: !!o.skip_collect, onchange: () => { o.skip_collect = skip.checked; saveOpts(); render(); } });
  const running = S.run && S.run.state === 'running';
  const go = el('button', { class: 'primary', disabled: running, onclick: async () => {
    try { await api(`${P()}/run`, { method: 'POST', body: { n_records: o.n_records, site_class: o.site_class, skip_collect: !!o.skip_collect } });
      toast('Started through the hub'); S.log = []; S.next = 0; startPoll(); await refresh(); render(); } catch (e) { toast(e.message, 'bad'); } } }, 'Run the full suite');
  const stop = el('button', { class: 'danger', disabled: !running, onclick: async () => { await api(`${P()}/stop`, { method: 'POST' }); toast('Stopping…'); } }, 'Stop');
  const log = el('div', { class: 'log' }, (S.log || []).slice(-600).map(e => el('div', { class: 'l' + (e.type === 'error' ? ' err' : '') }, `[${e.phase}] ${e.text}`)));
  setTimeout(() => { log.scrollTop = log.scrollHeight; }, 0);
  return el('div', {},
    el('h2', {}, 'Run the nonlinear suite'),
    el('p', { class: 'lead' }, 'Through the hub, exactly as the Nonlinear module\'s own buttons: Collect specification values (the component parameters from the standards), then Run analyses — pushover, NLRHA, DDM, the four-analyses sheet and the 16.1.4 design criteria. The hub copies HR\'s package (with provenance.json and nl_basis.json) into this project first.'),
    el('div', { class: 'card' },
      el('div', { class: 'row' }, el('label', { class: 'kv' }, 'Records ', nrec), el('label', { class: 'kv' }, 'Site class ', site), go, stop),
      el('label', { class: 'kv optout' }, skip, ' Do not collect specification values now — specification checks in the report will come back marked UNVERIFIED'),
      el('div', { class: 'small', style: 'margin-top:4px' }, o.skip_collect
        ? 'Collect is skipped and the analyses run on the repository placeholder parameters. For a PC without a standards corpus, or if you prefer not to add one; every report says the values were not collected.'
        : (collected ? 'Collect runs again first (this project already has collected values; they are replaced).'
                     : 'Collect runs first and reads the values from the standards corpus on this PC; if a group cannot be read, the run stops there.')),
      S.run && S.run.state && S.run.state !== 'none' ? el('div', { class: 'kv', style: 'margin-top:8px' }, `state: ${S.run.state}${S.run.phase ? ' · ' + S.run.phase : ''}${S.run.error ? ' · ' + S.run.error : ''}`) : null),
    log,
    S.run && S.run.state === 'done' ? el('p', { class: 'kv', style: 'margin-top:8px' }, 'Results are in this project: open the Nonlinear module\'s Viewers and Files tabs (four_analyses.html, the reports, the 16.1.4 document).' +
      (S.run.collect === false ? ' Specification values were not collected for this run: the reports are marked UNVERIFIED.' : '')) : null);
}

// ------------------------------------------------------------------------------------------- polling
function startPoll() {
  if (S.poll) return;
  S.poll = setInterval(async () => {
    const wasHr = hrState();
    try { S.hr = await api(`${P()}/hr`); } catch (e) { /* keep */ }
    try { const r = await api(`${P()}/run?since=${S.next}`); S.run = r; S.log = (S.log || []).concat(r.events || []); S.next = r.next || S.next; } catch (e) { /* none */ }
    if (wasHr === 'running' && built()) { try { S.checks = await api(`${P()}/checks`); } catch (e) { /* */ } toast('Model built', 'ok'); }
    const busy = hrState() === 'running' || (S.run && S.run.state === 'running');
    render();
    if (!busy) { clearInterval(S.poll); S.poll = null; }
  }, 3000);
}

(async () => {
  try { const me = await api('/api/me'); $('#status').textContent = me.steltic_up ? 'HR Steel connected' : 'HR Steel not reachable'; $('#status').className = 'note ' + (me.steltic_up ? 'ok' : 'bad'); } catch (e) { /* */ }
  await refresh();
  S.step = !mi() ? 1 : (!mi().ready ? 2 : (!built() ? 3 : (!confirmed() ? 4 : 5)));
  render();
  if (hrState() === 'running' || (S.run && S.run.state === 'running')) startPoll();
})();
