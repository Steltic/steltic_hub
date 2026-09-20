/* Steltic Hub shell.
 *
 * This file renders whatever the manifests describe. It contains no module names, no field
 * lists and no command strings -- adding a module means adding a manifest, not editing this.
 */
'use strict';
const $ = (s, r = document) => r.querySelector(s);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'html') n.innerHTML = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (k === 'style') n.setAttribute('style', v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false) n.append(c.nodeType ? c : String(c));
  return n;
};
const fmtBytes = b => b > 1e9 ? (b / 1e9).toFixed(1) + ' GB' : b > 1e6 ? (b / 1e6).toFixed(1) + ' MB'
  : b > 1e3 ? (b / 1e3).toFixed(0) + ' kB' : b + ' B';
const fmtAge = t => { if (!t) return ''; const s = Date.now() / 1000 - t;
  return s < 90 ? 'just now' : s < 5400 ? Math.round(s / 60) + ' min ago'
    : s < 172800 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' d ago'; };
const fmtMs = ms => ms < 1000 ? ms + 'ms' : (ms / 1000).toFixed(1) + 's';
const fmtNum = n => Number(n || 0).toLocaleString();
const enc = encodeURIComponent;
const lsGet = (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* private mode etc. */ } };

const S = {
  modules: [], jobs: [], project: lsGet('steltic.project', ''),
  mod: null, tab: null, special: null, dataDir: '', conn: false, version: '',
  hub: {},           // the hub process: {version, pid, started, stale, restartable} (see /healthz)
  sac: null,         // Windows 11 Smart App Control: 'on' | 'evaluation' | 'off' | null (not Windows)
  runs: {},          // `${mod}.${tab}` -> live/finished run state (survives tab switches)
  forms: {},         // `${mod}.${tab}` -> in-memory field values (files/attachments live here only)
  installLogs: {},   // module id -> the log element of the last install/update
  optional: {},      // module id -> {group: present}
  fillStatus: {},
};
const runKey = (m, t) => `${m}.${t}`;

/* ------------------------------------------------------------------ toast + modal */
let toastTimer = null;
function toast(msg, kind = '') {
  const t = $('#toast'); t.textContent = msg; t.className = 'on ' + kind;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.className = '', 3500);
}
function openModal(title, bodyNodes, actions) {
  const sheet = $('#sheet'); sheet.innerHTML = '';
  sheet.append(el('h3', {}, title), ...bodyNodes, el('div', { class: 'actions' }, ...actions));
  $('#modal').classList.add('on');
  const first = sheet.querySelector('input,textarea,select'); if (first) setTimeout(() => first.focus(), 30);
}
const closeModal = () => $('#modal').classList.remove('on');
$('#modal').onclick = e => { if (e.target.id === 'modal') closeModal(); };
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

function askText(title, help, placeholder, initial = '') {
  return new Promise(resolve => {
    const input = el('input', { type: 'text', placeholder: placeholder || '' }); input.value = initial;
    const done = ok => { closeModal(); resolve(ok ? input.value.trim() : null); };
    input.onkeydown = e => { if (e.key === 'Enter') done(true); };
    openModal(title, [help ? el('p', { class: 'blurb' }, help) : null, el('div', { class: 'field' }, input)].filter(Boolean),
      [el('button', { class: 'primary', onclick: () => done(true) }, 'OK'),
       el('button', { class: 'ghost', onclick: () => done(false) }, 'Cancel')]);
  });
}
function askConfirm(title, text, okLabel = 'Yes', danger = false) {
  return new Promise(resolve => {
    const done = ok => { closeModal(); resolve(ok); };
    openModal(title, [el('p', { class: 'blurb' }, text)],
      [el('button', { class: danger ? 'ghost danger' : 'primary', onclick: () => done(true) }, okLabel),
       el('button', { class: 'ghost', onclick: () => done(false) }, 'Cancel')]);
  });
}

/* ------------------------------------------------------------------ SSE */
async function stream(url, opts, onEvent) {
  let resp;
  try { resp = await fetch(url, opts); }
  catch (e) { onEvent({ type: 'error', text: 'could not reach the hub: ' + e }); return null; }
  const ct = resp.headers.get('content-type') || '';
  if (!resp.ok && !ct.includes('text/event-stream')) {
    let text = '';
    try { const d = await resp.json(); text = d.detail || d.error || JSON.stringify(d); } catch (e) { try { text = await resp.text(); } catch (_) {} }
    onEvent({ type: 'error', text: `${resp.status}: ${text || resp.statusText}` });
    return null;
  }
  const reader = resp.body.getReader(), dec = new TextDecoder();
  let buf = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const raw = buf.slice(0, i); buf = buf.slice(i + 2);
      const data = raw.split('\n').filter(l => l.startsWith('data:')).map(l => l.slice(5).trim());
      if (!data.length) continue;                       // keepalive comment
      try { onEvent(JSON.parse(data.join('\n'))); } catch (e) { onEvent({ type: 'log', text: data.join('\n') }); }
    }
  }
  return resp;
}

/* ------------------------------------------------------------------ hub process */
// The hub is single-instance and lives on after the window closes. When its source files on disk
// are newer than its start (a git pull, an edit in a checkout), the window is showing the OLD
// version: the hub says so (/healthz stale) and can replace itself with a fresh process on the
// same port (/api/hub/restart); the page waits for the new pid and reloads.
let staleDismissed = null;   // pid whose banner the user closed (it stays closed until that hub is replaced)
function showStale(h) {
  let bar = $('#stalebar');
  if (!h || !h.stale || staleDismissed === h.pid) { if (bar) bar.remove(); return; }
  if (bar) return;
  bar = el('div', { id: 'stalebar' },
    el('span', {}, `The hub's code on disk changed since this hub started (hub ${h.version}, pid ${h.pid}) — you are looking at the old version.`),
    el('button', { class: 'primary small', onclick: () => restartHub() }, 'Restart hub'),
    el('button', { class: 'ghost small', title: 'Keep the running hub for now (Modules page → Restart hub later)',
                   onclick: () => { staleDismissed = h.pid; showStale(h); } }, 'Later'));
  document.body.append(bar);
}

async function restartHub() {
  if (Object.values(S.runs).some(r => r.status === 'running') && !confirm('A run is in progress and will be stopped. Restart the hub anyway?')) return;
  const old = (S.hub || {}).pid;
  let r;
  try { r = await fetch('/api/hub/restart', { method: 'POST' }); } catch (e) { toast('the hub did not answer', 'bad'); return; }
  if (!r.ok) { const d = await r.json().catch(() => ({})); toast(d.detail || 'restart refused', 'bad'); return; }
  toast('restarting the hub — the page reloads when the new one answers…');
  const t0 = Date.now();
  while (Date.now() - t0 < 90000) {
    await new Promise(res => setTimeout(res, 600));
    try {
      const p = await (await fetch('/healthz', { cache: 'no-store' })).json();
      if (p.ok && p.pid !== old) { location.reload(); return; }
    } catch (e) { /* the port is changing hands */ }
  }
  toast('the new hub did not come up within 90 s — see logs/hub.log in the data folder', 'bad');
}

async function pollHub() {
  try { const p = await (await fetch('/healthz', { cache: 'no-store' })).json(); if (p.ok) { S.hub = Object.assign({}, S.hub, p); showStale(p); } } catch (e) { }
}
setInterval(pollHub, 30000);

/* ------------------------------------------------------------------ boot */
async function refresh() {
  let d;
  try { d = await (await fetch('/api/state')).json(); }
  catch (e) { toast('the hub is not answering — is it still running?', 'bad'); return; }
  S.modules = d.modules; S.jobs = d.jobs; S.dataDir = d.data_dir; S.conn = d.connection; S.version = d.version || '';
  S.hub = d.hub || {}; showStale(S.hub);
  S.sac = d.smart_app_control || null;
  if (S.project && !S.jobs.some(j => j.name === S.project)) {
    // a project that no longer exists on disk (deleted, or a different data dir) must not stay active
    S.project = '';
  }
  if (!S.project && S.jobs.length) S.project = S.jobs[0].name;
  renderRail(); renderBar();
}

function renderBar() {
  $('#projectChip').textContent = S.project ? '◆ ' + S.project : 'No project';
  const c = $('#connChip');
  c.textContent = S.conn ? 'Connection ✓' : 'Connection';
  c.classList.toggle('on', S.conn);
  c.style.setProperty('--c', 'var(--ok)');
  const n = S.modules.filter(m => m.status.env_ready).length;
  const live = Object.values(S.runs).filter(r => r.status === 'running').length;
  $('#barnote').textContent = `${n}/${S.modules.length} modules installed` + (live ? ` · ${live} running` : '');
}

function renderRail() {
  const list = $('#modlist'); list.innerHTML = '';
  for (const m of S.modules) {
    const st = m.status || {};
    const bad = st.env_ready && st.env && st.env.ready === false;
    const running = m.tabs.some(t => (S.runs[runKey(m.id, t.id)] || {}).status === 'running');
    const sub = running ? 'running…'
      : !st.installed ? 'not installed'
      : !st.env_ready ? 'environment missing'
      : bad ? 'environment problem'
      : (m.missing_needs || []).length ? 'needs ' + m.missing_needs.join(', ')
      : (st.commit ? st.commit + ' · ' + fmtAge(st.updated_at) : 'ready');
    const node = el('div', {
      class: 'mod' + (st.env_ready && !bad ? ' ready' : '') + (bad ? ' err' : '') + (S.mod === m.id ? ' on' : '') + (running ? ' busy' : ''),
      style: `--c:${m.accent}`, onclick: () => selectModule(m.id), title: m.blurb
    }, el('span', { class: 'dot' }),
       el('span', { class: 't' }, el('span', { class: 'n' }, m.name), el('span', { class: 's' }, sub)));
    list.append(node);
  }
  for (const n of document.querySelectorAll('[data-special]')) {
    n.classList.toggle('on', S.special === n.dataset.special);
    n.onclick = () => showSpecial(n.dataset.special);
  }
}
function showSpecial(which) { S.special = which; S.mod = null; renderRail(); renderSpecial(); }

function selectModule(id, tabId) {
  S.mod = id; S.special = null;
  const m = S.modules.find(x => x.id === id);
  S.tab = m && m.tabs.length ? (tabId && m.tabs.some(t => t.id === tabId) ? tabId : m.tabs[0].id) : null;
  renderRail(); renderTabs(); renderBody();
}

function renderTabs() {
  const bar = $('#tabs'); bar.innerHTML = '';
  if (S.special || !S.mod) return;
  const m = S.modules.find(x => x.id === S.mod); if (!m) return;
  for (const t of m.tabs) {
    const r = S.runs[runKey(m.id, t.id)];
    bar.append(el('span', {
      class: 'chip ok' + (S.tab === t.id ? ' on' : '') + (r && r.status === 'running' ? ' busy' : ''), style: `--c:${m.accent}`,
      onclick: () => { S.tab = t.id; renderTabs(); renderBody(); }
    }, t.title, r && r.status === 'running' ? el('span', { class: 'spin' }) : null));
  }
  bar.append(el('span', { class: 'sp' }));
  if (m.status.installed) bar.append(el('span', { class: 'barnote' }, m.blurb.slice(0, 90)));
}

/* ------------------------------------------------------------------ body */
function renderBody() {
  const body = $('#body'); body.innerHTML = '';
  const m = S.modules.find(x => x.id === S.mod); if (!m) return;
  const t = m.tabs.find(x => x.id === S.tab); if (!t) return;

  // viewers and files only read a project folder, so they work for a module that is not installed
  // (or was removed) as long as its outputs are still on disk.
  if (!m.status.env_ready && t.kind !== 'files' && t.kind !== 'viewers') {
    body.append(el('div', { class: 'pane' },
      el('h2', {}, m.name),
      el('p', { class: 'blurb' }, m.blurb),
      el('div', { class: 'card' },
        el('h4', {}, 'This module is not installed yet'),
        el('p', { class: 'note' }, m.bundled
          ? 'This module ships with the hub. Installing only builds its private Python environment.'
          : 'Installing clones the repo into the hub\'s own folder and builds it a private Python ' +
            'environment. Nothing is written to your existing checkouts.'),
        el('div', { class: 'actions' },
          el('button', { class: 'primary', onclick: () => showSpecial('modules') }, 'Open Modules'),
          m.git ? el('a', { class: 'status', href: m.homepage || m.git, target: '_blank', rel: 'noopener' }, m.git) : null))));
    return;
  }
  ({ form: renderForm, embed: renderEmbed, viewers: renderViewers, files: renderFiles })[t.kind](body, m, t);
}

/* ---------------- form tab ---------------- */
function formState(m, t) {
  const key = runKey(m.id, t.id);
  if (!S.forms[key]) {
    let saved = {}; try { saved = JSON.parse(lsGet(`steltic.form.${key}`, '{}')) || {}; } catch (e) {}
    S.forms[key] = { saved, mem: {} };
  }
  return S.forms[key];
}

function renderForm(body, m, t) {
  const pane = el('div', { class: 'pane' });
  pane.append(el('h2', {}, t.title)); if (t.blurb) pane.append(el('p', { class: 'blurb' }, t.blurb));
  if ((m.missing_needs || []).length) {
    const names = m.missing_needs.map(n => (S.modules.find(x => x.id === n) || {}).name || n);
    pane.append(el('div', { class: 'card warn' },
      el('h4', {}, 'Install ' + names.join(' and ') + ' first'),
      el('p', { class: 'note' },
        `${m.name} runs against that module's engine, so a run started now would fail partway through.`),
      el('div', { class: 'actions' },
        el('button', { class: 'ghost', onclick: () => showSpecial('modules') }, 'Open Modules'))));
  }
  // A tab may need one of the module's optional components. /api/state says which of them are
  // missing, so the button can be disabled with an explanation and an Install right here -- the user
  // used to learn about the large download only from the refusal that came back after pressing Run.
  const unmet = t.missing_optional || [];
  if (unmet.length) pane.append(missingOptional(m, unmet));

  const key = runKey(m.id, t.id);
  const fs = formState(m, t);
  const vals = {}, rows = [], fieldApi = {};
  const persistable = f => !['file', 'files', 'attachments', 'project'].includes(f.type);
  const remember = (f, v) => {
    vals[f.id] = v;
    if (persistable(f)) { fs.saved[f.id] = v; lsSet(`steltic.form.${key}`, JSON.stringify(fs.saved)); }
    else fs.mem[f.id] = v;
  };

  for (const f of t.fields) {
    if (f.type === 'project') {
      vals[f.id] = S.project;
      rows.push(projectField(f, v => { vals[f.id] = v; }));
      continue;
    }
    let init;
    if (persistable(f)) init = fs.saved[f.id] !== undefined ? fs.saved[f.id] : (f.default !== null && f.default !== undefined ? f.default : '');
    else init = fs.mem[f.id] !== undefined ? fs.mem[f.id] : (f.type === 'attachments' ? [] : '');
    if (f.fills) init = '';                                  // a "load an example" select never sticks
    vals[f.id] = init;
    rows.push(fieldNode(f, init, v => remember(f, v), m, t, fieldApi));
  }
  // group short fields into rows of up to three
  let bucket = [];
  const flush = () => { if (bucket.length) { pane.append(el('div', { class: 'row', style: 'margin-bottom:16px' }, bucket)); bucket = []; } };
  for (const r of rows) {
    if (r.dataset.short === '1' && bucket.length < 3) bucket.push(r);
    else { flush(); pane.append(r); }
  }
  flush();

  // ----- run state: one per (module, tab); the log element survives tab switches -----
  let run = S.runs[key];
  if (!run) run = S.runs[key] = newRunState();
  const status = el('span', { class: 'status' });
  const runBtn = el('button', { class: 'primary' }, (t.run && t.run.label) || 'Run');
  const stopBtn = el('button', { class: 'ghost danger', style: 'display:none' }, 'Stop');
  const arts = el('div', { class: 'arts' });
  const paint = () => {
    const r = S.runs[key];
    status.className = 'status ' + ({ running: '', done: 'ok', failed: 'bad', cancelled: 'bad', paused: 'warn' }[r.status] || '');
    status.textContent = r.statusText || '';
    runBtn.disabled = r.status === 'running' || unmet.length > 0;
    stopBtn.style.display = (r.status === 'running' && t.run && t.run.can_cancel) ? '' : 'none';
    run.usageEl.style.display = r.usageText ? '' : 'none'; run.usageEl.textContent = r.usageText || '';
    run.reasonWrap.style.display = run.reasonEl.childNodes.length ? '' : 'none';
    run.lights.style.display = r.status === 'running' && run.hasLights ? '' : 'none';
  };
  run.paint = paint;

  if (t.run) {
    runBtn.onclick = async () => {
      const missing = t.fields.filter(f => f.required && !f.has_default && f.type !== 'checkbox' &&
        (vals[f.id] === undefined || vals[f.id] === null || String(vals[f.id]).trim() === '' || (Array.isArray(vals[f.id]) && !vals[f.id].length)));
      if (missing.length) { status.className = 'status bad'; status.textContent = 'Required: ' + missing.map(f => f.label).join(', '); return; }
      if (!S.project) { status.className = 'status bad'; status.textContent = 'Pick or create a project first'; return; }
      // a design agent's http run, or a CLI run the manifest marks llm, is a conversation with the user's model
      if (((m.wants_credentials && t.run.kind === 'http') || t.run.llm) && !S.conn) { openConnection(); return; }
      startRun(m, t, key, { ...vals }, paint, fieldApi);
    };
    stopBtn.onclick = async () => {
      const r = S.runs[key];
      if (!r.runId) return;
      stopBtn.disabled = true; r.statusText = 'stopping…'; paint();
      r.logLine('status', '· stop requested — the run halts at its next step');
      await fetch('/api/cancel/' + r.runId, { method: 'POST' });
      setTimeout(() => { stopBtn.disabled = false; }, 3000);
    };
  }

  const acts = el('div', { class: 'actions' }, t.run ? runBtn : null, t.run ? stopBtn : null, status);
  if ((t.artifacts && t.artifacts.length) || (t.links && t.links.length))
    acts.append(el('button', { class: 'ghost', onclick: () => showArtifacts(arts, m, t) }, 'Show outputs'));
  pane.append(acts, arts, run.lights, run.logWrap, run.reasonWrap, run.usageEl);
  body.append(pane);
  paint();
  showArtifacts(arts, m, t);
}

// The card that stands in for a run button this tab cannot honour yet: what is missing, why this tab
// needs it (the manifest's own words), and the Modules page's install stream brought here so the
// user never has to go looking for it.
function missingOptional(m, groups) {
  const log = el('div', { class: 'log' });
  const cards = groups.map(g => {
    const o = (m.optional || {})[g] || {};
    const label = o.label || g;
    return el('div', { class: 'card warn' },
      el('h4', {}, label + ' is not installed'),
      el('p', { class: 'note' }, 'This tab cannot run without it, so its button is disabled. Installing puts it ' +
        'in ' + m.name + '\'s own environment — a large download that runs once and touches nothing else.'),
      o.help ? el('p', { class: 'note' }, o.help) : null,
      (o.requirements || []).length ? el('p', { class: 'meta' }, o.requirements.join('  ')) : null,
      el('div', { class: 'actions' },
        el('button', { class: 'primary', onclick: e => runInstall(`/api/modules/${m.id}/optional/${g}`, log, e.target) },
          'Install ' + label),
        el('button', { class: 'ghost', onclick: () => showSpecial('modules') }, 'Open Modules')));
  });
  return el('div', {}, cards, log);
}

function newRunState() {
  const logEl = el('div', { class: 'log' });
  const reasonEl = el('div', { class: 'log reason' });
  const mkExpand = target => {
    const b = el('button', { class: 'logbtn', title: 'Expand' }, '⤢');
    b.onclick = () => { const ex = target.classList.toggle('expanded'); b.textContent = ex ? '⤡' : '⤢'; };
    return b;
  };
  const r = {
    status: 'idle', statusText: '', runId: null, usageText: '', tokenLine: null, reasonLine: null, hasLights: false,
    logEl, reasonEl,
    logWrap: el('div', { class: 'logwrap' }, mkExpand(logEl), logEl),
    reasonWrap: el('div', { class: 'logwrap', style: 'display:none' }, el('div', { class: 'loghead' }, 'Model reasoning'), mkExpand(reasonEl), reasonEl),
    usageEl: el('div', { class: 'usage', style: 'display:none' }),
    lights: el('div', { class: 'lights', style: 'display:none' },
      ...[['thinking', 'Thinking'], ['rag', 'Searching standards'], ['python', 'Running Python'], ['opensees', 'Running OpenSees']]
        .map(([k, l]) => el('span', { class: 'light', 'data-k': k }, el('i'), l))),
    paint: () => {},
  };
  r.logLine = (cls, text) => {
    const d = el('div', { class: cls || '' }, text); logEl.append(d);
    while (logEl.childNodes.length > 4000) logEl.removeChild(logEl.firstChild);
    logEl.scrollTop = logEl.scrollHeight; return d;
  };
  r.setLights = keys => {
    const on = new Set(keys || []);
    r.lights.querySelectorAll('.light').forEach(n => n.classList.toggle('on', on.has(n.dataset.k)));
  };
  return r;
}

function appendToken(r, text) {
  if (!r.tokenLine) { r.tokenLine = r.logLine('tok', ''); r.tokenLine._txt = el('span'); r.tokenLine.append(r.tokenLine._txt, el('span', { class: 'cursor' })); }
  r.tokenLine._txt.textContent += text;
  r.logEl.scrollTop = r.logEl.scrollHeight;
}
function appendReason(r, text) {
  if (!r.reasonLine) { r.reasonLine = el('div', { class: 'rtok' }); r.reasonLine._txt = el('span'); r.reasonLine.append(r.reasonLine._txt); r.reasonEl.append(r.reasonLine); r.reasonWrap.style.display = ''; }
  r.reasonLine._txt.textContent += text;
  while (r.reasonEl.textContent.length > 120000 && r.reasonEl.firstChild && r.reasonEl.firstChild !== r.reasonLine) r.reasonEl.removeChild(r.reasonEl.firstChild);
  r.reasonEl.scrollTop = r.reasonEl.scrollHeight;
}
function endStreamingLines(r, ev) {
  if (ev.type !== 'token' && r.tokenLine) { const c = r.tokenLine.querySelector('.cursor'); if (c) c.remove(); r.tokenLine = null; }
  if (ev.type !== 'reasoning' && r.reasonLine) r.reasonLine = null;
}
function updateLights(r, ev) {
  switch (ev.type) {
    case 'token': case 'reasoning': r.hasLights = true; r.setLights(['thinking']); break;
    case 'tool': {
      r.hasLights = true;
      const n = ev.name || '';
      if (/python|exec|run_code/i.test(n)) {
        const k = ['python'];
        if (/opensees|ops\.|pipeline|design|report|preview|build|analy|eigen|recorder|modal/i.test((ev.code || '') + ' ' + (ev.title || ''))) k.push('opensees');
        r.setLights(k);
      } else if (/search|standard|rag|query/i.test(n)) r.setLights(['rag']);
      else r.setLights(['thinking']);
      break;
    }
    case 'tool_result': r.setLights(['thinking']); break;
    case 'assistant': case 'done': case 'error': case 'paused': r.setLights([]); break;
  }
}

// Renders one event from a module. CLI modules only send log/start/done; the design agents send the
// richer vocabulary (token/reasoning/tool/tool_result/milestone/status/usage/paused/assistant).
function handleEvent(r, ev, ctx) {
  endStreamingLines(r, ev);
  updateLights(r, ev);
  switch (ev.type) {
    case 'start':
      r.runId = ev.run_id; r.statusText = 'running…';
      if (ev.cmd) r.logLine('c', '$ ' + ev.cmd);
      break;
    case 'log':         r.logLine('', ev.text === undefined ? '' : ev.text); break;
    // the hub is about to spawn the same run again after a native crash: still one run, still
    // running, but the status word should say why nothing is happening for a few seconds
    case 'retry':       r.statusText = `retrying (${ev.attempt}/${ev.max})`; break;
    case 'status':      r.logLine('status', '· ' + (ev.text || '')); break;
    case 'token':       appendToken(r, ev.text || ''); break;
    case 'reasoning':   appendReason(r, ev.text || ''); break;
    case 'tool':        r.logLine('tool', `▶ ${ev.step !== undefined ? 'step ' + ev.step + ' · ' : ''}${ev.title || ev.name || 'tool'}`); break;
    case 'tool_result': r.logLine('res', '↳ ' + (ev.summary || '') + (ev.ms ? '  (' + fmtMs(ev.ms) + ')' : '')); break;
    case 'milestone':   r.logLine('milestone', '▸ ' + (ev.text || '')); break;
    case 'assistant':   r.logLine('assistant', ev.text || ''); break;
    case 'warning':     r.logLine('w', '⚠ ' + (ev.text || '')); break;
    case 'error':       r.logLine('e', '✖ ' + (ev.text || 'error')); r.sawError = true; break;
    case 'paused':
      r.logLine('w', '⏸ paused — ' + (ev.reason || 'loop guard'));
      if (ev.detail) r.logLine('w', '   ' + ev.detail);
      r.status = 'paused';
      r.statusText = /stopped/.test(ev.reason || '') ? 'stopped — your files are saved; use the Continue tab to resume'
                                                     : 'paused — review the log, then use the Continue tab with a fix or instruction';
      break;
    case 'usage':
      r.usageText = `last call: input ${fmtNum(ev.last_in)} · output ${fmtNum(ev.last_out)}      |      cumulative: input ${fmtNum(ev.cum_in)} · output ${fmtNum(ev.cum_out)}`;
      break;
    case 'done':
      if (ev.end || ev.rc !== undefined || ev.artifacts) {          // the hub's own end-of-run event
        if (ev.cancelled) { r.status = 'cancelled'; r.statusText = 'stopped'; r.logLine('w', 'stopped'); }
        else if (ev.ok === false) {
          if (r.status !== 'paused') {
            r.status = 'failed'; r.statusText = ev.rc !== undefined && ev.rc !== -1 ? `failed (exit ${ev.rc})` : 'failed';
            r.logLine('e', ev.rc !== undefined && ev.rc !== -1 ? `exited ${ev.rc}` : 'finished with errors');
          }
        } else if (r.status !== 'paused') { r.status = 'done'; r.statusText = 'done'; r.logLine('g', 'finished'); }
        if (ev.artifacts && ev.artifacts.length && ctx.onArtifacts) ctx.onArtifacts(ev.artifacts);
      } else {
        r.logLine('g', '✓ design complete');                     // the module's own completion event
        r.status = 'done'; r.statusText = 'done — use the Continue tab to iterate on this design';
      }
      break;
    default:
      if (ev.text) r.logLine('', ev.text);
  }
}

async function startRun(m, t, key, vals, paint, fieldApi) {
  const r = S.runs[key];
  r.logEl.innerHTML = ''; r.reasonEl.innerHTML = ''; r.tokenLine = null; r.reasonLine = null;
  r.status = 'running'; r.statusText = 'starting…'; r.runId = null; r.usageText = ''; r.sawError = false; r.hasLights = false;
  r.setLights([]);
  renderBar(); renderRail(); renderTabs(); paint();
  const project = S.project;
  const ctx = { onArtifacts: found => {
    // the hub reports which declared outputs now exist: light those chips up straight away
    const host = $('#body .arts'); if (!host || S.mod !== m.id || S.tab !== t.id) return;
    const ok = new Set(found.map(a => a.path));
    for (const a of host.querySelectorAll('a.chip')) {
      const path = a.dataset.path; if (!path || !ok.has(path)) continue;
      a.className = 'chip ok'; a.href = `/out/${m.id}/${enc(project)}/${path}`; a.title = path;
    }
  } };
  try {
    await stream(`/api/run/${m.id}/${t.id}`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ job: project, fields: vals })
    }, ev => { handleEvent(r, ev, ctx); r.paint(); });
  } catch (e) {
    r.logLine('e', '✖ connection to the hub lost: ' + e);
    r.logLine('status', '· a design run keeps going on the module server until it finishes or is stopped; check the Files tab, or use Continue');
    r.status = 'failed'; r.statusText = 'connection lost';
  }
  if (r.status === 'running') { r.status = r.sawError ? 'failed' : 'done'; r.statusText = r.sawError ? 'failed' : 'done'; }
  r.runId = null; r.setLights([]);
  if (r.status === 'done' && fieldApi.clearAttachments) fieldApi.clearAttachments();
  r.paint();
  await refresh();
  // the pane may have been re-rendered for another tab in the meantime; only repaint what is on screen
  if (S.mod === m.id && S.tab === t.id) {
    renderTabs();
    const host = $('#body .arts'); if (host) showArtifacts(host, m, t);
  }
}

function projectField(f, onChange) {
  const sel = el('select', {});
  const fill = () => {
    sel.innerHTML = '';
    const names = S.jobs.map(j => j.name);
    if (S.project && !names.includes(S.project)) names.unshift(S.project);
    for (const n of names) {
      const j = S.jobs.find(x => x.name === n);
      sel.append(el('option', { value: n, selected: n === S.project }, j ? `${n} — ${j.files} file${j.files === 1 ? '' : 's'}` : n));
    }
    if (!names.length) sel.append(el('option', { value: '' }, 'no projects yet — create one'));
    sel.append(el('option', { value: '__new' }, '+ New project…'));
  };
  fill();
  sel.onchange = async () => {
    if (sel.value === '__new') { const n = await newProject(); fill(); if (n) onChange(n); return; }
    setProject(sel.value); onChange(sel.value);
  };
  const n = el('div', { class: 'field' },
    el('label', {}, f.label, f.required ? el('span', { class: 'req' }, '*') : null),
    el('div', { class: 'row' }, el('div', { style: 'flex:1' }, sel),
      el('button', { class: 'ghost', onclick: async () => { const nn = await newProject(); fill(); if (nn) onChange(nn); } }, 'New')),
    f.help ? el('div', { class: 'help' }, f.help) : null);
  n.dataset.short = '0';
  return n;
}

async function newProject() {
  const name = await askText('New project', 'Names the folder every module writes into for this building. Letters, digits, _ and - are kept; anything else becomes _.', 'e.g. 27_High_St');
  if (!name) return null;
  const r = await fetch(`/api/jobs/${enc(name)}`, { method: 'POST' });
  const d = await r.json();
  if (!r.ok) { toast(d.detail || 'could not create the project', 'bad'); return null; }
  await refresh();
  setProject(d.name);
  if (S.special === 'jobs') renderSpecial(); else if (S.mod) renderBody();
  return d.name;
}

function setProject(v) { S.project = v; lsSet('steltic.project', v); renderBar(); }

function fieldNode(f, init, onChange, m, t, fieldApi) {
  let input;
  const short = ['number', 'select', 'checkbox', 'text'].includes(f.type);
  const label = () => el('label', {}, f.label, f.required && !f.has_default ? el('span', { class: 'req' }, '*') : null);
  const help = () => f.help ? el('div', { class: 'help' }, f.help) : null;

  if (f.type === 'textarea') {
    input = el('textarea', { rows: f.rows || 6, placeholder: f.placeholder || '' });
    input.value = init || '';
    input.oninput = () => onChange(input.value);
    fieldApi[f.id] = { set: v => { input.value = v; onChange(v); }, get: () => input.value };
  } else if (f.type === 'select') {
    input = el('select', {});
    for (const o of f.options || []) input.append(el('option', { value: o.value, selected: String(o.value) === String(init) }, o.label));
    const note = el('span', { class: 'status' });
    if (f.fills) {
      // e.g. an example-brief picker: fetch from the module's own server and drop it into another field
      input.onchange = async () => {
        const v = input.value; if (!v) return;
        const target = fieldApi[f.fills.target];
        note.className = 'status'; note.textContent = 'loading… (starting the module server if needed)';
        try {
          const path = (f.fills.path || '').replace('{value}', enc(v));
          const r = await fetch(`/m/${m.id}${path}`);
          const d = await r.json();
          if (!r.ok) throw new Error(d.detail || d.error || r.statusText);
          const text = f.fills.key ? d[f.fills.key] : d;
          if (text === undefined) throw new Error('unexpected reply');
          if (target) target.set(typeof text === 'string' ? text : JSON.stringify(text, null, 2));
          note.className = 'status ok'; note.textContent = `loaded ${input.options[input.selectedIndex].text} — edit it if you like`;
        } catch (e) { note.className = 'status bad'; note.textContent = 'could not load: ' + (e.message || e); }
        input.selectedIndex = 0;                 // so the same one can be re-picked
        onChange('');
      };
      const n = el('div', { class: 'field' }, label(), input, note, help());
      n.dataset.short = '0'; return n;
    }
    input.onchange = () => onChange(input.value);
  } else if (f.type === 'checkbox') {
    input = el('input', { type: 'checkbox' }); input.checked = !!init;
    input.onchange = () => onChange(input.checked);
    const n = el('div', { class: 'field' }, el('label', { class: 'check' }, input, el('span', {}, f.label)), help());
    n.dataset.short = '1'; return n;
  } else if (f.type === 'file' || f.type === 'files') {
    return fileField(f, init, onChange, label, help);
  } else if (f.type === 'attachments') {
    return attachmentsField(f, init, onChange, label, help, fieldApi);
  } else {
    input = el('input', { type: f.type === 'number' ? 'number' : 'text', placeholder: f.placeholder || '', step: f.type === 'number' ? 'any' : null });
    input.value = init === null || init === undefined ? '' : init;
    input.oninput = () => onChange(f.type === 'number' ? (input.value === '' ? '' : Number(input.value)) : input.value);
  }
  const n = el('div', { class: 'field' }, label(), input, help());
  n.dataset.short = short ? '1' : '0';
  return n;
}

function fileField(f, init, onChange, label, help) {
  const multi = f.type === 'files';
  const cur = () => multi ? (Array.isArray(init) ? init : (init ? String(init).split(/[;:]/) : [])) : (init || '');
  const name = el('span', { class: 'filename' });
  const clear = el('button', { class: 'ghost small', title: 'Clear' }, '✕');
  const paint = () => {
    const v = cur();
    const has = multi ? v.length : !!v;
    name.textContent = has ? (multi ? v.join(', ') : v) : (f.placeholder ? f.placeholder : 'no file chosen');
    name.classList.toggle('empty', !has);
    clear.style.display = has ? '' : 'none';
  };
  const set = v => { init = v; onChange(v); paint(); };
  clear.onclick = () => set(multi ? [] : '');
  const picker = el('input', { type: 'file', accept: f.accept || '', style: 'display:none', multiple: multi });
  picker.onchange = async () => {
    if (!picker.files.length) return;
    if (!S.project) { name.textContent = 'pick a project first'; return; }
    name.textContent = 'uploading…';
    const fd = new FormData();
    for (const file of picker.files) fd.append('files', file);
    try {
      const r = await fetch(`/api/jobs/${enc(S.project)}/upload`, { method: 'POST', body: fd });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || 'upload failed');
      const paths = (d.files || []).map(x => x.path);
      set(multi ? paths : (paths[0] || ''));
    } catch (e) { name.textContent = 'upload failed: ' + (e.message || e); }
    picker.value = '';
  };
  const existing = el('button', { class: 'ghost small', title: 'Pick a file already in the project folder' }, 'in project ▾');
  existing.onclick = async () => {
    if (!S.project) { name.textContent = 'pick a project first'; return; }
    const d = await (await fetch(`/api/jobs/${enc(S.project)}/tree`)).json();
    const exts = (f.accept || '').split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
    const files = (d.entries || []).filter(e => !e.dir && (!exts.length || exts.some(x => e.path.toLowerCase().endsWith(x))));
    if (!files.length) { toast('no matching files in this project yet', 'bad'); return; }
    const sel = el('select', {}, ...files.map(e => el('option', { value: e.path }, `${e.path} (${fmtBytes(e.bytes)})`)));
    openModal(`Choose from ${S.project}`, [el('div', { class: 'field' }, sel)],
      [el('button', { class: 'primary', onclick: () => { set(multi ? [...cur(), sel.value] : sel.value); closeModal(); } }, 'Use this file'),
       el('button', { class: 'ghost', onclick: closeModal }, 'Cancel')]);
  };
  paint();
  const n = el('div', { class: 'field' }, label(),
    el('div', { class: 'filebox' },
      el('button', { class: 'ghost', onclick: () => picker.click() }, 'Choose file…'), existing, picker, name, clear),
    help());
  n.dataset.short = '0'; return n;
}

/* ---- attachments: text/markdown/pdf into a target field, images sent along as data URLs ---- */
const IMG_RE = /\.(png|jpe?g|gif|webp)$/i, TXT_RE = /\.(txt|md|markdown|text)$/i, PDF_RE = /\.pdf$/i;
const readFile = (file, how) => new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = rej; r[how](file); });
let pdfjsP = null;
function loadPdfjs() {
  if (pdfjsP) return pdfjsP;
  pdfjsP = new Promise((res, rej) => {
    const s = document.createElement('script');
    s.src = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js';
    s.onload = () => { window.pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js'; res(window.pdfjsLib); };
    s.onerror = () => { pdfjsP = null; rej(new Error('pdf.js could not be loaded (offline?)')); };
    document.head.appendChild(s);
  });
  return pdfjsP;
}
async function pdfText(file) {
  const lib = await loadPdfjs();
  const doc = await lib.getDocument({ data: await readFile(file, 'readAsArrayBuffer') }).promise;
  const pages = [];
  for (let p = 1; p <= doc.numPages; p++) {
    const tc = await (await doc.getPage(p)).getTextContent();
    pages.push(tc.items.map(it => it.str).join(' '));
  }
  try { doc.destroy(); } catch (e) {}
  return pages.join('\n\n').replace(/[ \t]+/g, ' ').trim();
}
function attachmentsField(f, init, onChange, label, help, fieldApi) {
  let items = Array.isArray(init) ? init.slice() : [];
  const maxFiles = f.max_files || 0, maxBytes = (f.max_mb || 0) * 1024 * 1024;   // 0 = no limit
  const list = el('div', { class: 'filelist' });
  const note = el('span', { class: 'status' });
  const paint = () => {
    list.innerHTML = '';
    items.forEach((im, i) => list.append(el('span', { class: 'filechip' }, el('b', {}, im.name), el('span', { class: 'sz' }, fmtBytes(im.size || 0)),
      el('span', { class: 'x', title: 'remove', onclick: () => { items.splice(i, 1); onChange(items.slice()); paint(); } }, '✕'))));
  };
  fieldApi.clearAttachments = () => { items = []; onChange([]); paint(); note.textContent = ''; };
  const picker = el('input', { type: 'file', accept: f.accept || '', multiple: true, style: 'display:none' });
  picker.onchange = async () => {
    const files = Array.from(picker.files || []); picker.value = '';
    if (!files.length) return;
    if (maxFiles && files.length > maxFiles) { note.textContent = `max ${maxFiles} files at once`; return; }
    const chunks = [], names = []; let msg = '';
    for (const file of files) {
      if (maxBytes && file.size > maxBytes) { msg = `"${file.name}" exceeds ${f.max_mb} MB — skipped`; continue; }
      const isImg = IMG_RE.test(file.name) || /^image\//.test(file.type);
      const isPdf = PDF_RE.test(file.name) || file.type === 'application/pdf';
      const isTxt = TXT_RE.test(file.name) || /^text\//.test(file.type) || file.type === '';
      if (isImg) {
        if (maxFiles && items.length >= maxFiles) { msg = `max ${maxFiles} attachments`; continue; }
        items.push({ name: file.name, type: file.type || 'image/*', data_url: await readFile(file, 'readAsDataURL'), size: file.size });
      } else if (isPdf) {
        try {
          const txt = await pdfText(file);
          if (txt) { chunks.push(`# ${file.name}\n` + txt); names.push(file.name); }
          else msg = `"${file.name}" has no extractable text (scanned PDF?) — skipped`;
        } catch (e) { msg = `"${file.name}": ${e.message || 'could not be read as a PDF'} — skipped`; }
      } else if (isTxt) {
        chunks.push((files.length > 1 ? `# ${file.name}\n` : '') + await readFile(file, 'readAsText')); names.push(file.name);
      } else msg = `"${file.name}" type not supported — skipped`;
    }
    const target = f.text_target && fieldApi[f.text_target];
    if (chunks.length && target) {
      const cur = (target.get() || '').trim();
      target.set((cur ? cur + '\n\n' : '') + chunks.join('\n\n'));
    }
    onChange(items.slice());
    note.textContent = (names.length ? 'loaded ' + names.join(', ') + (msg ? ' · ' : '') : '') + msg;
    paint();
  };
  paint();
  const n = el('div', { class: 'field' }, label(),
    el('div', { class: 'filebox' }, el('button', { class: 'ghost', onclick: () => picker.click() }, 'Add files…'), picker, note),
    list, help());
  n.dataset.short = '0'; return n;
}

function showArtifacts(host, m, t) {
  host.innerHTML = '';
  const artifacts = t.artifacts || [], links = t.links || [];
  if (!S.project || (!artifacts.length && !links.length)) return;
  const project = S.project;
  fetch(`/api/out/${m.id}/${enc(project)}`).then(r => r.json()).then(d => {
    if (S.project !== project) return;
    host.innerHTML = '';
    const have = new Set((d.entries || []).map(e => e.path));
    for (const a of artifacts) {
      const ok = have.has(a.path);
      host.append(el('a', {
        class: 'chip ' + (ok ? 'ok' : 'na'), style: `--c:${a.accent || m.accent}`, 'data-path': a.path,
        href: ok ? `/out/${m.id}/${enc(project)}/${a.path}` : null,
        target: '_blank', rel: 'noopener', title: ok ? a.path : a.path + ' — not produced yet'
      }, a.label));
    }
    for (const l of links) {
      host.append(el('a', {
        class: 'chip ok', style: `--c:${m.accent}`, target: '_blank', rel: 'noopener',
        href: `/m/${m.id}${(l.path || '').replace('{job}', enc(project))}`, title: l.help || l.path
      }, l.label + ' ↗'));
    }
  }).catch(() => {});
}

/* ---------------- viewers tab ---------------- */
function renderViewers(body, m, t) {
  const wrap = el('div', { class: 'viewwrap' });
  const bar = el('div', { class: 'viewbar' });
  const note = el('span', { class: 'barnote', style: 'margin-left:8px' }, 'looking for viewers…');
  const frame = el('iframe', { title: 'viewer' });
  const open = el('a', { class: 'chip ok', target: '_blank', rel: 'noopener', style: 'display:none' }, 'Open ↗');
  const empty = el('div', { class: 'empty' },
    el('div', {}, 'No viewer in this project yet.',
      el('br'), el('span', { style: 'font-size:11.5px;color:var(--dimmer)' },
        (m.viewers || []).map(v => v.path).join(' · '))));
  wrap.append(bar, frame, empty);
  frame.style.display = 'none';
  body.append(el('div', { class: 'pane full' }, wrap));

  if (!S.project) { note.textContent = 'pick a project first'; bar.append(note); return; }
  const project = S.project;
  fetch(`/api/out/${m.id}/${enc(project)}`).then(r => r.json()).then(d => {
    const have = new Set((d.entries || []).map(e => e.path));
    let first = null;
    for (const v of (m.viewers || [])) {
      const ok = have.has(v.path);
      const chip = el('span', {
        class: 'chip ' + (ok ? 'ok' : 'na'), style: `--c:${v.accent}`,
        title: ok ? v.path : v.path + ' — not in this project',
        onclick: ok ? () => {
          for (const c of bar.querySelectorAll('.chip')) c.classList.remove('on');
          chip.classList.add('on');
          const url = `/out/${m.id}/${enc(project)}/${v.path}`;
          frame.src = url; open.href = url; open.style.display = '';
          frame.style.display = ''; empty.style.display = 'none';
        } : null
      }, v.label);
      if (ok && !first) first = chip;
      bar.append(chip);
    }
    const n = (m.viewers || []).filter(v => have.has(v.path)).length;
    note.textContent = n ? `${n} of ${(m.viewers || []).length} viewers in this project · greyed = not produced yet`
                         : 'no viewers in this project yet';
    bar.append(el('span', { class: 'sp' }), note, open);
    if (first) first.click();
  }).catch(() => { note.textContent = 'could not list this project'; bar.append(note); });
}

/* ---------------- files tab ---------------- */
function renderFiles(body, m, t) {
  const pane = el('div', { class: 'pane' });
  pane.append(el('h2', {}, 'Files'),
    el('p', { class: 'blurb' }, S.project ? `Everything ${m.name} has produced for ${S.project}.` : 'Pick a project first.'));
  body.append(pane);
  if (!S.project) return;
  const project = S.project;
  fetch(`/api/out/${m.id}/${enc(project)}`).then(r => r.json()).then(d => {
    if (!d.root_exists || !(d.entries || []).length) {
      pane.append(el('p', { class: 'note' }, m.own_output_root
        ? 'Nothing here yet — this module keeps its outputs in its own data folder and has not produced anything for this project.'
        : 'Nothing here yet.')); return;
    }
    const tb = el('tbody');
    for (const e of d.entries) {
      tb.append(el('tr', {},
        el('td', { class: 'name' },
          el('a', { href: `/out/${m.id}/${enc(project)}/${e.path}`, target: '_blank', rel: 'noopener' }, e.path)),
        el('td', {}, fmtBytes(e.bytes)), el('td', {}, fmtAge(e.mtime))));
    }
    pane.append(el('p', { class: 'meta', style: 'margin-bottom:10px' }, `${d.entries.length} files · ${fmtBytes(d.entries.reduce((a, e) => a + e.bytes, 0))}`),
      el('table', {}, el('thead', {}, el('tr', {},
        el('th', {}, 'File'), el('th', {}, 'Size'), el('th', {}, 'Modified'))), tb));
  });
}

/* ---------------- embed tab ---------------- */
function renderEmbed(body, m, t) {
  // The module's page references /static and /api by absolute path, so it must be served by its own
  // server (own origin) rather than proxied under the hub -- otherwise its links resolve against the hub.
  const wrap = el('div', { class: 'viewwrap' });
  const frame = el('iframe', { title: m.name, style: 'display:none' });
  const note = el('span', { class: 'barnote' }, 'starting the module server…');
  const open = el('a', { class: 'chip ok', target: '_blank', rel: 'noopener', style: 'display:none' }, 'Open ↗');
  const reload = el('span', { class: 'chip ok', style: 'display:none', onclick: () => { try { frame.contentWindow.location.reload(); } catch (e) { frame.src = frame.src; } } }, 'Reload');
  const empty = el('div', { class: 'empty' }, el('div', {}, 'Starting ', m.name, '…', el('br'),
    el('span', { style: 'font-size:11.5px;color:var(--dimmer)' }, 'first start can take a minute while the engine loads')));
  wrap.append(el('div', { class: 'viewbar' }, note, el('span', { class: 'sp' }), reload, open), frame, empty);
  body.append(el('div', { class: 'pane full' }, wrap));
  fetch(`/api/modules/${m.id}/server/start`, { method: 'POST' }).then(async r => {
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'could not start the module server');
    const url = d.url + (t.src || '/').replace('{job}', enc(S.project || ''));
    frame.src = url; open.href = url;
    frame.style.display = ''; empty.style.display = 'none'; open.style.display = ''; reload.style.display = '';
    note.textContent = t.blurb || 'the module\'s own interface';
    refresh();
  }).catch(e => { note.textContent = ''; empty.innerHTML = ''; empty.append(el('div', { class: 'e', style: 'white-space:pre-wrap;text-align:left' }, String(e.message || e))); });
}

/* ------------------------------------------------------------------ special panes */
function renderSpecial() {
  $('#tabs').innerHTML = '';
  const body = $('#body'); body.innerHTML = '';
  if (S.special === 'modules') renderModulesPane(body);
  else renderJobsPane(body);
}

function renderModulesPane(body) {
  const pane = el('div', { class: 'pane' });
  pane.append(el('h2', {}, 'Modules'),
    el('p', { class: 'blurb' },
      'Each module is a git repo with its own private Python environment. Updating pulls the repo ' +
      'and reinstalls into that environment only, so one module can never break another — and a ' +
      'module that ships a newer steltic_module.json gains its new tabs without updating the hub.'));
  pane.append(el('div', { class: 'actions', style: 'margin:0 0 18px' },
    el('button', { class: 'ghost', onclick: async e => {
      e.target.textContent = 'reloading…';
      await fetch('/api/reload', { method: 'POST' });
      await refresh(); renderSpecial();
    } }, 'Reload manifests'),
    el('span', { class: 'status' },
      'Picks up edits to a manifest without restarting — the edit-test loop for tabs and fields.')));
  if (S.sac === 'on' || S.sac === 'evaluation') pane.append(sacNotice(S.sac));
  const grid = el('div', { class: 'grid' });
  for (const m of S.modules) grid.append(moduleCard(m));
  pane.append(grid, el('hr'),
    el('h3', {}, 'Add a module'),
    el('p', { class: 'blurb' },
      'Paste a steltic_module.json. Any repo that describes itself becomes a module here — the hub ' +
      'has no list of allowed modules and needs no release to learn a new one.'),
    (() => {
      const ta = el('textarea', { rows: 8, placeholder: '{\n  "schema": 1,\n  "id": "my_module",\n  "name": "My Module",\n  "source": { "url": "https://github.com/…" },\n  "tabs": [ … ]\n}' });
      const msg = el('span', { class: 'status' });
      return el('div', {}, ta, el('div', { class: 'actions' },
        el('button', { class: 'primary', onclick: async () => {
          let raw; try { raw = JSON.parse(ta.value); } catch (e) { msg.className = 'status bad'; msg.textContent = 'not valid JSON: ' + e.message; return; }
          const r = await fetch('/api/modules/custom', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(raw) });
          const d = await r.json();
          if (!r.ok) { msg.className = 'status bad'; msg.textContent = d.detail || 'rejected'; return; }
          msg.className = 'status ok'; msg.textContent = `${d.name} registered — install it above`;
          await refresh(); renderSpecial();
        } }, 'Register module'), msg));
    })(),
    el('p', { class: 'note' }, 'Data folder: ' + S.dataDir + (S.version ? ' · hub ' + S.version : '') + (S.hub && S.hub.pid ? ' · pid ' + S.hub.pid : ''),
      ' · ',
      el('button', { class: 'ghost small', title: 'Stop this hub and start a fresh one on the same port — after a git pull or an edit to the hub code',
                     disabled: !(S.hub && S.hub.restartable), onclick: () => restartHub() }, 'Restart hub')));
  body.append(pane);
}

// Windows 11 Smart App Control refuses unsigned native libraries -- PyTorch (PDF converter), OpenSees
// (design engines), onnxruntime -- with WinError 4551. It has no allow-list, only On/Off, and Off is
// one-way. Evaluation mode blocks nothing but can switch itself on at a reboot. Said once, up front.
function sacNotice(state) {
  const on = state === 'on';
  return el('div', { class: 'card warn', style: 'margin:0 0 18px;padding:12px 16px' },
    el('div', { style: 'font-weight:600;margin-bottom:4px' },
      on ? 'Windows Smart App Control is on — the native modules cannot run on this PC'
         : 'Windows Smart App Control is in evaluation mode'),
    el('div', { class: 'note', style: 'margin:0' },
      on ? 'It blocks unsigned libraries (PyTorch for the PDF converter, OpenSees for the design engines, onnxruntime) with WinError 4551 and has no per-file allow-list. ' +
           'Query, the hub and the MOCK design model still work. To run everything: Windows Security → App & browser control → Smart App Control settings → Off (one-way: it cannot be re-enabled without resetting Windows).'
         : 'Nothing is blocked yet, but it can switch itself on at a reboot, after which PyTorch / OpenSees / onnxruntime fail with WinError 4551. ' +
           'If you want the PDF converter or the real design engines on this PC, set it to Off now (Windows Security → App & browser control → Smart App Control settings); Off is one-way.'));
}

function moduleCard(m) {
  const st = m.status || {};
  const env = st.env || {};
  const badges = [];
  if (!st.installed) badges.push(el('span', { class: 'pill' }, 'not installed'));
  if ((m.missing_needs || []).length) badges.push(el('span', { class: 'pill warn' }, 'needs ' + m.missing_needs.join(', ')));
  else if (env.ready === false) badges.push(el('span', { class: 'pill bad' }, 'env problem'));
  else if (st.env_ready) badges.push(el('span', { class: 'pill ok' }, 'ready'));
  if (env.python) badges.push(el('span', { class: 'pill' }, 'py ' + env.python));
  if (env.openseespy === true) badges.push(el('span', { class: 'pill ok' }, 'openseespy'));
  if (m.has_server) badges.push(el('span', { class: 'pill' + (m.server_up ? ' ok' : '') }, m.server_up ? 'server up' : 'server'));
  if (st.has_local_manifest && !st.manifest_problem) badges.push(el('span', { class: 'pill' }, 'own manifest'));
  if (st.manifest_problem) badges.push(el('span', { class: 'pill bad', title: st.manifest_problem }, 'manifest ignored'));
  if (st.linked) badges.push(el('span', { class: 'pill warn' }, 'linked'));
  if (st.dirty) badges.push(el('span', { class: 'pill warn' }, 'uncommitted'));
  if (st.custom) badges.push(el('span', { class: 'pill' }, 'custom'));
  if (m.bundled) badges.push(el('span', { class: 'pill', title: 'the code ships inside the hub and updates with it' }, 'bundled'));
  for (const [rid, r] of Object.entries(m.servers_used || {})) {
    badges.push(el('span', { class: 'pill' + (r.installed ? ' ok' : ' warn'),
      title: r.installed ? `starts ${r.name} first and uses its server` : `install ${r.name} and this module will use its server` },
      (r.installed ? 'uses ' : 'can use ') + r.name));
  }
  const rename = el('span', { class: 'rename', title: 'Rename this module (only what you see — the manifest is untouched)', onclick: async () => {
    const v = await askText(`Rename ${m.name}`, `Shown everywhere in the hub instead of "${m.default_name}". Leave empty to go back to the module's own name.`, m.default_name, m.name === m.default_name ? '' : m.name);
    if (v === null) return;
    const r = await fetch(`/api/modules/${m.id}/name`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ name: v }) });
    const d = await r.json();
    if (!r.ok) { toast(d.detail || 'could not rename', 'bad'); return; }
    await refresh(); renderSpecial();
  } }, '✎');

  const log = S.installLogs[m.id] || (S.installLogs[m.id] = el('div', { class: 'log' }));
  const busy = Object.keys(S.runs).some(k => k.startsWith(m.id + '.') && S.runs[k].status === 'running');
  const guard = async (what) => {
    if (busy) { toast(`${m.name} has a run in progress — stop it before you ${what}`, 'bad'); return false; }
    return true;
  };
  const card = el('div', { class: 'card', style: `border-left:3px solid ${m.accent}` },
    el('h4', {}, m.name, rename, badges),
    m.name !== m.default_name ? el('p', { class: 'meta' }, `module: ${m.default_name}`) : null,
    el('p', { class: 'note' }, m.blurb),
    el('p', { class: 'meta', style: 'margin-top:8px' },
      st.linked ? `working copy: ${st.linked}` : m.bundled ? 'part of the hub' : `${m.git || '(no git source)'}  ${st.commit || ''}`),
    env.reason ? el('p', { class: 'note', style: 'color:var(--bad)' }, env.reason) : null,
    st.manifest_problem ? el('p', { class: 'note', style: 'color:var(--warn)' }, 'steltic_module.json in the checkout was ignored: ' + st.manifest_problem) : null,
    st.error ? el('p', { class: 'note', style: 'color:var(--bad)' }, st.error) : null,
    el('div', { class: 'actions' },
      st.installed
        ? el('button', { class: 'ghost', onclick: async e => { if (await guard('update')) runInstall(`/api/modules/${m.id}/update`, log, e.target); } }, 'Update')
        : el('button', { class: 'primary', onclick: e => runInstall(`/api/modules/${m.id}/install`, log, e.target) }, 'Install'),
      st.installed && !st.linked && !m.bundled ? el('button', { class: 'ghost', onclick: async e => {
        e.target.textContent = 'checking…';
        try {
          const d = await (await fetch(`/api/modules/${m.id}/check-update`)).json();
          e.target.textContent = d.behind ? `update available (${d.remote})` : (d.reason ? d.reason : 'up to date');
        } catch (err) { e.target.textContent = 'check failed'; }
      } }, 'Check for updates') : null,
      st.installed ? el('button', { class: 'ghost', onclick: async e => {
        if (await guard('rebuild') && await askConfirm('Rebuild environment', `Delete and rebuild the Python environment of ${m.name}? Its checkout and your projects are kept.`, 'Rebuild'))
          runInstall(`/api/modules/${m.id}/install?rebuild=true`, log, e.target);
      } }, 'Rebuild env') : null,
      m.server_up ? el('button', { class: 'ghost', onclick: async () => {
        await fetch(`/api/modules/${m.id}/server/stop`, { method: 'POST' }); await refresh(); renderSpecial();
      } }, 'Stop server') : null,
      m.has_server && st.env_ready && !m.server_up ? el('button', { class: 'ghost', onclick: async e => {
        e.target.textContent = 'starting…'; e.target.disabled = true;
        const r = await fetch(`/api/modules/${m.id}/server/start`, { method: 'POST' });
        const d = await r.json();
        if (!r.ok) toast(d.detail || 'could not start', 'bad');
        await refresh(); renderSpecial();
      } }, 'Start server') : null,
      st.linked
        ? el('button', { class: 'ghost', onclick: async () => {
            if (!await guard('unlink')) return;
            await fetch(`/api/modules/${m.id}/link`, { method: 'DELETE' });
            await refresh(); renderSpecial();
          } }, 'Unlink working copy')
        : m.bundled ? null : el('button', { class: 'ghost', onclick: async () => {
            const path = await askText(`Use a local copy of ${m.name}`,
              'Path to your working copy. The hub installs it in place (pip install -e .) and never writes to it, so you can test uncommitted changes.', 'C:\\code\\my_module');
            if (!path) return;
            const r = await fetch(`/api/modules/${m.id}/link`, {
              method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ path }) });
            const d = await r.json();
            if (!r.ok) { toast(d.detail || 'could not link that path', 'bad'); return; }
            toast('linked — now Install to build its environment', 'ok');
            await refresh(); renderSpecial();
          } }, 'Use local copy…'),
      st.installed ? el('button', { class: 'ghost danger', onclick: async () => {
        if (!await guard('remove')) return;
        const msg = st.linked
          ? `Unlink ${m.name} and delete its environment? Your working copy is untouched.`
          : m.bundled ? `Remove the environment of ${m.name}? Its code stays with the hub; project folders are kept.`
          : `Remove ${m.name}? Its checkout and environment are deleted. Project folders are kept.`;
        if (!await askConfirm(st.linked ? 'Remove environment' : 'Remove module', msg, 'Remove', true)) return;
        const r = await fetch(`/api/modules/${m.id}`, { method: 'DELETE' });
        if (!r.ok) { const d = await r.json().catch(() => ({})); toast(d.detail || 'could not remove', 'bad'); }
        await refresh(); renderSpecial();
      } }, st.linked || m.bundled ? 'Remove env' : 'Remove') : null,
      st.custom && !st.installed ? el('button', { class: 'ghost danger', onclick: async () => {
        if (!await askConfirm('Forget module', `Forget the registered module ${m.name}? Nothing on disk is touched.`, 'Forget', true)) return;
        await fetch(`/api/modules/${m.id}?forget=true`, { method: 'DELETE' });
        await refresh(); renderSpecial();
      } }, 'Forget') : null),
    Object.keys(m.optional || {}).length ? optionalSection(m, log) : null,
    log);
  return card;
}

function optionalSection(m, log) {
  const rows = Object.entries(m.optional).map(([g, o]) => {
    const btn = el('button', { class: 'ghost', onclick: e => runInstall(`/api/modules/${m.id}/optional/${g}`, log, e.target) }, 'Install ' + o.label);
    const state = el('span', { class: 'pill' }, '…');
    const row = el('div', { class: 'actions', style: 'margin-top:6px' }, btn, state, el('span', { class: 'status' }, o.help));
    row._group = g; row._btn = btn; row._state = state;
    return row;
  });
  const sec = el('div', {}, el('h3', { style: 'margin:18px 0 8px' }, 'Optional components'), rows);
  const paint = d => {
    for (const r of rows) {
      const p = d && d[r._group] && d[r._group].present;
      r._state.textContent = p ? 'installed' : 'not installed'; r._state.className = 'pill' + (p ? ' ok' : '');
      r._btn.textContent = (p ? 'Reinstall ' : 'Install ') + m.optional[r._group].label;
    }
  };
  if (S.optional[m.id]) paint(S.optional[m.id]);
  if (m.status.env_ready) fetch(`/api/modules/${m.id}/optional`).then(r => r.json()).then(d => { S.optional[m.id] = d; paint(d); }).catch(() => paint(null));
  else paint(null);
  return sec;
}

async function runInstall(url, log, btn) {
  const old = btn.textContent; btn.disabled = true; btn.textContent = 'working…';
  log.innerHTML = '';
  let ok = false;
  await stream(url, { method: 'POST' }, ev => {
    if (ev.type === 'log') log.append(el('div', {}, ev.text));
    else if (ev.type === 'error') log.append(el('div', { class: 'e' }, ev.text));
    else if (ev.type === 'done') { ok = !!ev.ok; log.append(el('div', { class: ev.ok ? 'g' : 'e' }, ev.ok ? 'done' : 'failed — see the log above')); }
    while (log.childNodes.length > 3000) log.removeChild(log.firstChild);
    log.scrollTop = log.scrollHeight;
  });
  btn.disabled = false; btn.textContent = old;
  toast(ok ? 'done' : 'failed — see the log', ok ? 'ok' : 'bad');
  await refresh();
  // the optional-component install also runs from a form tab's "not installed" card, where the
  // Modules pane is not what is on screen; re-rendering the tab is what lifts its gate, no reload
  if (S.special) renderSpecial(); else renderBody();
}

function renderJobsPane(body) {
  const pane = el('div', { class: 'pane' });
  pane.append(el('h2', {}, 'Projects'),
    el('p', { class: 'blurb' },
      'One folder per building, shared by every module. A design lands here, the nonlinear checks ' +
      'write their packages beside it, and the viewer strip lights up as each one appears.'),
    el('div', { class: 'actions', style: 'margin-top:0;margin-bottom:18px' },
      el('button', { class: 'primary', onclick: () => newProject() }, 'New project'),
      el('span', { class: 'status' }, 'Folder: ' + S.dataDir + (S.dataDir.includes('\\') ? '\\jobs' : '/jobs'))));
  const grid = el('div', { class: 'grid' });
  const modName = id => (S.modules.find(x => x.id === id) || {}).name || id;
  for (const j of S.jobs) {
    const lr = j.last_run;
    grid.append(el('div', { class: 'card' + (j.name === S.project ? ' active' : '') },
      el('h4', {}, j.name, j.name === S.project ? el('span', { class: 'pill ok' }, 'active') : null),
      el('p', { class: 'meta' }, `${j.files} file${j.files === 1 ? '' : 's'} · ${fmtBytes(j.bytes)} · ${fmtAge(j.mtime)}`),
      lr ? el('p', { class: 'note' }, `last run: ${modName(lr.module)} / ${lr.tab} — ${lr.ok ? 'ok' : 'failed'} · ${fmtAge(lr.t)}`) : null,
      el('div', { class: 'arts' },
        ...j.reports.map(p => el('a', { class: 'chip ok', href: `/job/${enc(j.name)}/${p}`, target: '_blank', rel: 'noopener', title: p }, p.split('/').pop())),
        ...j.viewers.map(p => el('a', { class: 'chip ok', style: '--c:var(--ok)', href: `/job/${enc(j.name)}/${p}`, target: '_blank', rel: 'noopener', title: p }, p.split('/').pop()))),
      el('div', { class: 'actions' },
        j.name !== S.project ? el('button', { class: 'ghost', onclick: () => { setProject(j.name); renderSpecial(); } }, 'Make active') : null,
        el('button', { class: 'ghost danger', onclick: async () => {
          const live = Object.values(S.runs).some(r => r.status === 'running');
          if (live) { toast('a run is in progress — stop it before deleting projects', 'bad'); return; }
          if (await askConfirm('Delete project', `Delete project ${j.name} and everything in its folder? Outputs a module keeps in its own data folder are not touched.`, 'Delete', true)) {
            await fetch(`/api/jobs/${enc(j.name)}`, { method: 'DELETE' });
            if (S.project === j.name) setProject('');
            await refresh(); renderSpecial();
          }
        } }, 'Delete'))));
  }
  if (!S.jobs.length) pane.append(el('p', { class: 'note' }, 'No projects yet.'));
  pane.append(grid);
  body.append(pane);
}

/* ------------------------------------------------------------------ connection modal */
async function openConnection() {
  let d = {};
  try { d = await (await fetch('/api/connection')).json(); } catch (e) {}
  const f = {};
  const mk = (id, label, val, type = 'text', ph = '', help = '') => {
    const i = el('input', { type, placeholder: ph }); i.value = val || '';
    i.oninput = () => f[id] = i.value; f[id] = val || '';
    return el('div', { class: 'field' }, el('label', {}, label), i, help ? el('div', { class: 'help' }, help) : null);
  };
  const reasoning = el('select', {});
  for (const [v, l] of [['off', 'Off'], ['low', 'Low'], ['medium', 'Medium'], ['high', 'High'], ['default', 'Model default']])
    reasoning.append(el('option', { value: v, selected: v === (d.reasoning || 'high') }, l));
  reasoning.onchange = () => f.reasoning = reasoning.value; f.reasoning = d.reasoning || 'high';
  const msg = el('span', { class: 'status' });

  openModal('LLM connection', [
    el('p', { class: 'blurb' },
      'Typed once and saved on this PC, so it is there the next time you open Steltic. Every module that ' +
      'needs it gets it when its server starts; it goes nowhere else than to the provider you name here.'),
    mk('base_url', 'API base URL', d.base_url, 'text', 'https://api.your-provider.com/v1',
       'Any OpenAI-compatible endpoint — e.g. OpenRouter https://openrouter.ai/api/v1, OpenAI https://api.openai.com/v1, Anthropic https://api.anthropic.com/v1, or a local vLLM.'),
    mk('api_key', 'API key', '', 'password', d.has_key ? '(saved — leave blank to keep the current key)' : 'sk-…'),
    mk('model', 'Model', d.model, 'text', 'model id per your provider, e.g. deepseek/deepseek-v4-pro',
       'Set this to MOCK to drive the whole pipeline offline with no LLM.'),
    el('div', { class: 'row' },
      el('div', { class: 'field', style: 'flex:1' }, el('label', {}, 'Reasoning effort'), reasoning),
      mk('max_tokens', 'Max output tokens', d.max_tokens || 32000, 'number')),
    mk('provider', 'Provider', d.provider, 'text', 'leave blank for auto',
       'Pin an inference provider so prompt caching survives between calls — auto-routing between providers breaks the cache and costs real money.'),
    d.stored_at ? el('p', { class: 'note' }, 'Saved in ' + d.stored_at) : null,
  ].filter(Boolean), [
    el('button', { class: 'primary', onclick: async () => {
      msg.className = 'status'; msg.textContent = 'saving…';
      const r = await fetch('/api/connection', { method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ ...f, max_tokens: Number(f.max_tokens) || 32000 }) });
      const dd = await r.json();
      if (!r.ok) { msg.className = 'status bad'; msg.textContent = dd.detail || 'rejected'; return; }
      msg.className = 'status ok';
      msg.textContent = 'saved' + (dd.pushed.length ? ` · pushed to ${dd.pushed.join(', ')}` : '') + (dd.failed && dd.failed.length ? ` · NOT accepted by ${dd.failed.join(', ')}` : '');
      S.conn = true; renderBar(); setTimeout(closeModal, 900);
    } }, 'Save connection'),
    d.set ? el('button', { class: 'ghost danger', onclick: async () => {
      await fetch('/api/connection', { method: 'DELETE' }); S.conn = false; renderBar(); closeModal(); toast('connection removed from this PC');
    } }, 'Forget') : null,
    el('button', { class: 'ghost', onclick: closeModal }, 'Cancel'), msg,
  ].filter(Boolean));
}

/* ------------------------------------------------------------------ wiring */
$('#connChip').onclick = openConnection;
$('#modulesChip').onclick = () => showSpecial('modules');
$('#projectChip').onclick = () => showSpecial('jobs');
window.addEventListener('beforeunload', e => {
  if (Object.values(S.runs).some(r => r.status === 'running')) { e.preventDefault(); e.returnValue = ''; }
});

refresh().then(() => {
  const ready = S.modules.find(m => m.status.env_ready);
  if (ready) selectModule(ready.id);
  else showSpecial('modules');
});
