/* Steltic Admin -- the framed UI. Three views, picked by the URL hash the hub's tabs open:
 *   #batch      instruction -> plan -> run, with the steps and their logs
 *   #standards  PDF folder -> conversion queue
 *   #help       questions about how Steltic works
 */
'use strict';
const $ = (s, r = document) => r.querySelector(s);
function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'html') n.innerHTML = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false) n.append(c.nodeType ? c : String(c));
  return n;
}
function toast(msg, kind = '') {
  const t = el('div', { class: 'toast ' + kind }, msg); $('#toasts').append(t);
  setTimeout(() => t.remove(), kind === 'bad' ? 7000 : 3500);
}
const fmtAge = t => { if (!t) return ''; const s = Date.now() / 1000 - t;
  return s < 90 ? 'just now' : s < 5400 ? Math.round(s / 60) + ' min ago' : s < 172800 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' d ago'; };
const fmtDur = (a, b) => { if (!a) return ''; const s = Math.max(0, Math.round((b || Date.now() / 1000) - a));
  return s < 90 ? s + ' s' : s < 5400 ? Math.round(s / 60) + ' min' : (s / 3600).toFixed(1) + ' h'; };
const fmtBytes = b => b > 1e6 ? (b / 1e6).toFixed(1) + ' MB' : b > 1e3 ? (b / 1e3).toFixed(0) + ' kB' : b + ' B';
async function api(path, opts = {}) {
  const r = await fetch(path, opts.body !== undefined && !(opts.body instanceof FormData)
    ? { ...opts, headers: { 'content-type': 'application/json', ...(opts.headers || {}) }, body: JSON.stringify(opts.body) } : opts);
  let d = {};
  try { d = await r.json(); } catch (e) { }
  if (!r.ok) throw new Error(d.detail || d.error || r.statusText);
  return d;
}

const S = { me: null, hub: null, view: 'batch', plans: [], plan: null, step: null, poll: null, lastLog: '' };

/* ------------------------------------------------------------------ shell */
const VIEWS = [['batch', 'Batch'], ['standards', 'Standards'], ['help', 'Help']];
function renderNav() {
  const nav = $('#steps'); nav.innerHTML = '';
  VIEWS.forEach(([id, label], i) => nav.append(el('span', { class: 'step' + (S.view === id ? ' on' : ''), onclick: () => { location.hash = id; } },
    el('span', { class: 'k' }, i + 1), label)));
}
function route() {
  const h = (location.hash || '#batch').slice(1).split('?')[0];
  S.view = VIEWS.some(v => v[0] === h) ? h : 'batch';
  renderNav();
  const m = $('#main'); m.innerHTML = '';
  stopPoll();
  ({ batch: renderBatch, standards: renderStandards, help: renderHelp })[S.view](m);
}
window.addEventListener('hashchange', route);

async function loadMe() {
  try { S.me = await api('/api/me'); } catch (e) { S.me = null; }
  const c = $('#hubchip');
  if (S.me && S.me.hub_reachable) { c.textContent = 'hub ' + (S.me.hub.version || '') + (S.me.llm ? ' · model ' + S.me.model : ' · no model'); c.style.color = ''; }
  else { c.textContent = 'hub not reachable at ' + (S.me ? S.me.hub_url : '?'); c.style.color = 'var(--bad)'; }
}

/* ------------------------------------------------------------------ batch */
function renderBatch(m) {
  const pane = el('div', { class: 'pane' });
  pane.append(el('h2', {}, 'Batch'),
    el('p', { class: 'lead' }, 'Say what to run. Admin turns it into a plan you can check and edit, then runs the steps one after another through the hub -- each one exactly as a click on that module tab would -- and keeps the log of every run here.'));

  // ---- instruction
  const ta = el('textarea', { rows: 3, placeholder: 'J1 to hr then nl; then J2 to cfs only; then J3 (ex22) to hr' });
  ta.value = localStorage.getItem('admin.instruction') || '';
  ta.oninput = () => localStorage.setItem('admin.instruction', ta.value);
  const msg = el('div', { class: 'note' });
  const problems = el('div');
  const mk = el('button', { class: 'primary', onclick: () => makePlan(false) }, 'Make plan');
  const mkLlm = el('button', { onclick: () => makePlan(true), title: 'Let the connected model write the plan (for wording the grammar does not know)' }, 'Make plan with the model');
  async function makePlan(withModel) {
    msg.textContent = 'thinking…'; problems.innerHTML = '';
    try {
      const d = await api(withModel ? '/api/plan/parse-llm' : '/api/plan/parse', { method: 'POST', body: { text: ta.value } });
      showProblems(problems, d.errors, d.warnings);
      S.plan = d.plan; S.step = null;
      msg.textContent = d.plan.steps.length ? `${d.plan.steps.length} step${d.plan.steps.length === 1 ? '' : 's'} -- check them below, then Save and Start` : '';
      paintPlan();
    } catch (e) { msg.textContent = ''; toast(e.message, 'bad'); }
  }
  pane.append(el('div', { class: 'card' },
    el('label', { class: 'f' }, 'Instruction'), ta,
    el('div', { class: 'row', style: 'margin-top:8px' }, mk, S.me && S.me.llm ? mkLlm : null, msg),
    problems,
    el('details', { style: 'margin-top:10px' }, el('summary', { class: 'note', style: 'cursor:pointer' }, 'words Admin understands'),
      cheatSheet())));

  // ---- the plan
  const planBox = el('div', { class: 'card' });
  pane.append(planBox);

  // ---- saved plans
  const listBox = el('div', { class: 'card' });
  pane.append(listBox);
  m.append(pane);

  async function refreshList() {
    try { const d = await api('/api/plans'); S.plans = d.plans; S.running = d.running; } catch (e) { S.plans = []; }
    listBox.innerHTML = '';
    listBox.append(el('h3', { style: 'margin-top:0' }, 'Plans'));
    if (!S.plans.length) { listBox.append(el('p', { class: 'note' }, 'No plans yet.')); return; }
    const box = el('div', { class: 'plans' });
    for (const p of S.plans) {
      const c = p.counts || {};
      box.append(el('div', { class: 'p' + (S.plan && S.plan.id === p.id ? ' on' : ''), onclick: () => openPlan(p.id) },
        el('span', { class: 'st ' + p.status }, p.status),
        el('span', { class: 't' }, p.title),
        el('span', { class: 'm' }, `${p.steps} steps · ${c.done || 0} done${c.failed ? ' · ' + c.failed + ' failed' : ''} · ${(p.projects || []).join(', ')} · ${fmtAge(p.updated)}`)));
    }
    listBox.append(box);
  }
  async function openPlan(id) {
    try { S.plan = await api('/api/plans/' + id); S.step = null; paintPlan(); refreshList(); } catch (e) { toast(e.message, 'bad'); }
    if (S.plan && S.plan.status === 'running') startPoll();
  }

  function paintPlan() {
    planBox.innerHTML = '';
    const p = S.plan;
    if (!p) { planBox.append(el('h3', { style: 'margin-top:0' }, 'Plan'), el('p', { class: 'note' }, 'Make a plan from an instruction, or open a saved one below.')); return; }
    const saved = S.plans.some(x => x.id === p.id);
    const head = el('div', { class: 'row' },
      el('h3', { style: 'margin:0' }, 'Plan'), el('span', { class: 'st ' + p.status }, p.status),
      el('span', { class: 'grow kv' }, el('b', {}, p.title), p.id ? ' · ' + p.id : ''),
    );
    const acts = el('div', { class: 'row', style: 'margin:10px 0' });
    const running = p.status === 'running';
    const canStart = !running && p.steps.some(s => s.status === 'pending');
    const canResume = !running && ['stopped', 'interrupted', 'failed'].includes(p.status);
    acts.append(...[
      el('button', { onclick: savePlan }, saved ? 'Save changes' : 'Save'),
      el('button', { class: 'primary', disabled: running || !saved, onclick: () => act('start'), title: saved ? '' : 'save first' }, 'Start'),
      canResume ? el('button', { onclick: () => act('resume', { retry_failed: false }) }, 'Resume') : null,
      canResume && p.steps.some(s => s.status === 'failed') ? el('button', { onclick: () => act('resume', { retry_failed: true }) }, 'Resume, retry failed') : null,
      running ? el('button', { class: 'danger', onclick: () => act('stop') }, 'Stop after this step (cancels the current run)') : null,
      saved && !running ? el('button', { class: 'ghost danger', onclick: async () => { if (!confirm('Delete this plan and its logs?')) return; await api('/api/plans/' + p.id, { method: 'DELETE' }); S.plan = null; paintPlan(); refreshList(); } }, 'Delete') : null,
      el('button', { class: 'ghost', onclick: async () => { try { const d = await api('/api/plan/validate', { method: 'POST', body: { plan: p } }); showProblems(probs, d.errors, d.warnings); if (!d.errors.length) toast('the plan can run', 'ok'); } catch (e) { toast(e.message, 'bad'); } } }, 'Check'),
    ].filter(Boolean));
    const probs = el('div');
    if (p.note) probs.append(el('div', { class: p.status === 'failed' ? 'err-list' : 'warn-list' }, ...p.note.split('\n').map(l => el('div', {}, l))));
    // steps table
    const tb = el('tbody');
    p.steps.forEach((s, i) => {
      const fields = Object.entries(s.fields || {}).map(([k, v]) => `${k}=${typeof v === 'string' ? (v.length > 40 ? v.slice(0, 40) + '…' : v) : JSON.stringify(v)}`).join('  ');
      tb.append(el('tr', { class: S.step === i ? 'sel' : '', onclick: () => { S.step = i; paintPlan(); loadLog(); } },
        el('td', {}, s.n), el('td', {}, s.project), el('td', {}, modName(s.module) + ' / ' + s.tab),
        el('td', { class: 'mono', style: 'font-size:11.5px;color:var(--dim)' }, s.label || fields),
        el('td', {}, el('span', { class: 'st ' + s.status }, s.status), s.attempts > 1 ? ` ×${s.attempts}` : ''),
        el('td', {}, s.started ? fmtDur(s.started, s.ended) : ''),
        el('td', { style: 'color:var(--bad);font-size:11.5px' }, s.note || (s.artifacts && s.artifacts.length ? el('span', { style: 'color:var(--ok)' }, s.artifacts.map(a => a.label || a.path).join(', ')) : ''))));
    });
    const table = el('table', { class: 'plan-steps' }, el('thead', {}, el('tr', {},
      el('th', {}, '#'), el('th', {}, 'project'), el('th', {}, 'module / tab'), el('th', {}, 'what'), el('th', {}, 'status'), el('th', {}, 'took'), el('th', {}, 'result'))), tb);
    // editor
    const ed = el('textarea', { class: 'mono', rows: 10, spellcheck: false });
    ed.value = JSON.stringify({ title: p.title, steps: p.steps.map(s => ({ project: s.project, module: s.module, tab: s.tab, fields: s.fields, on_fail: s.on_fail, label: s.label || undefined })) }, null, 1);
    const apply = el('button', { class: 'small', onclick: () => {
      try { const d = JSON.parse(ed.value); S.plan = { ...p, title: d.title || p.title, steps: (d.steps || []).map((s, i) => ({ ...s, n: i + 1, status: 'pending', fields: s.fields || {} })) }; paintPlan(); toast('applied -- Save to keep it'); }
      catch (e) { toast('not valid JSON: ' + e.message, 'bad'); }
    } }, 'Apply JSON');
    const details = el('details', { open: !p.steps.length }, el('summary', { class: 'note', style: 'cursor:pointer' }, 'edit the plan as JSON (fields: "@project" = brief.md in the project folder, "@example:ex22", "@file:name", or the value itself; on_fail: stop | skip_project | continue)'),
      ed, el('div', { class: 'row', style: 'margin-top:6px' }, apply));
    // log
    const logBox = el('div', { style: 'margin-top:12px' });
    planBox.append(head, acts, probs, table, el('div', { style: 'height:10px' }), details, logBox);
    S.logBox = logBox;
    if (S.step === null && running) { S.step = p.steps.findIndex(s => s.status === 'running'); if (S.step < 0) S.step = null; }
    if (S.step !== null) loadLog();

    async function savePlan() {
      try {
        const d = saved ? await api('/api/plans/' + p.id, { method: 'PUT', body: { plan: S.plan } }) : await api('/api/plans', { method: 'POST', body: { plan: S.plan } });
        S.plan = d.plan; toast('saved', 'ok'); await refreshList(); paintPlan();
      } catch (e) { toast(e.message, 'bad'); }
    }
    async function act(what, body) {
      try {
        const d = await api(`/api/plans/${p.id}/${what}`, { method: 'POST', body: body || {} });
        if (d.plan) S.plan = d.plan;
        if (what !== 'stop') startPoll(); else toast('stopping after the current run…');
        paintPlan(); refreshList();
      } catch (e) { toast(e.message, 'bad'); }
    }
  }

  async function loadLog() {
    const p = S.plan; if (!p || S.step === null || !S.logBox) return;
    const s = p.steps[S.step]; if (!s) return;
    try {
      const d = await api(`/api/plans/${p.id}/log/${s.n}?tail=400`);
      if (d.text === S.lastLog && S.logBox.firstChild) return;
      S.lastLog = d.text;
      S.logBox.innerHTML = '';
      S.logBox.append(el('div', { class: 'row', style: 'margin-bottom:4px' }, el('span', { class: 'note' }, `log of step ${s.n} -- ${s.project} → ${modName(s.module)} / ${s.tab}`), el('span', { class: 'sp' }),
        el('button', { class: 'small ghost', onclick: () => S.logBox.querySelector('.log').classList.toggle('tall') }, 'expand')),
        el('div', { class: 'log' }, d.text || '(nothing yet)'));
      const lg = S.logBox.querySelector('.log'); lg.scrollTop = lg.scrollHeight;
    } catch (e) { }
  }

  function startPoll() {
    stopPoll();
    S.poll = setInterval(async () => {
      if (!S.plan) return;
      try {
        const fresh = await api('/api/plans/' + S.plan.id);
        const changed = JSON.stringify(fresh.steps.map(s => [s.status, s.attempts, s.note])) !== JSON.stringify(S.plan.steps.map(s => [s.status, s.attempts, s.note])) || fresh.status !== S.plan.status;
        S.plan = fresh;
        if (changed) { paintPlan(); refreshList(); } else loadLog();
        if (fresh.status !== 'running') { stopPoll(); toast('plan ' + fresh.status, fresh.status === 'done' ? 'ok' : 'bad'); }
      } catch (e) { }
    }, 2500);
  }

  refreshList().then(() => {
    const q = (location.hash.split('?')[1] || '');
    const want = new URLSearchParams(q).get('plan');
    if (want) openPlan(want);
    else if (S.plans.some(p => p.status === 'running')) openPlan(S.plans.find(p => p.status === 'running').id);
    else paintPlan();
  });
}
function stopPoll() { if (S.poll) { clearInterval(S.poll); S.poll = null; } }
function modName(id) { const m = (S.hub && S.hub.modules || []).find(x => x.id === id); return m ? m.name : id; }
function showProblems(host, errors, warnings) {
  host.innerHTML = '';
  if (errors && errors.length) host.append(el('div', { class: 'err-list' }, el('div', {}, 'Cannot run yet:'), ...errors.map(e => el('div', {}, '• ' + e))));
  if (warnings && warnings.length) host.append(el('div', { class: 'warn-list' }, ...warnings.map(w => el('div', {}, '• ' + w))));
}
function cheatSheet() {
  const rows = (S.me && S.me.aliases) || [];
  const tb = el('tbody');
  for (const r of rows) tb.append(el('tr', {}, el('td', {}, r.aliases.map(a => el('code', {}, a)).flatMap((c, i) => i ? [' ', c] : [c])), el('td', {}, `${modName(r.module)} / ${r.tab}`)));
  return el('div', { class: 'cheat' },
    el('p', {}, 'One project per clause, then the modules in order; ', el('code', {}, 'then'), ', ', el('code', {}, ';'), ' or a new line separates clauses. ',
      'A brief for a design step: ', el('code', {}, 'J1 (ex22) to hr'), ' uses an example brief, ', el('code', {}, 'J1 (brief.md) to hr'), ' a file in the project folder; with neither, Admin reads ', el('code', {}, 'brief.md'), ' from the project folder. ',
      el('code', {}, 'J4 continue: <what to change>'), ' iterates on a finished design.'),
    el('table', {}, tb));
}

/* ------------------------------------------------------------------ standards */
function renderStandards(m) {
  const pane = el('div', { class: 'pane' });
  pane.append(el('h2', {}, 'Standards'),
    el('p', { class: 'lead' }, 'The specification PDFs you are licensed for, converted through the Query file manager one after another. Each conversion takes hours; the queue is a plan like any other, so it survives a restart and shows every run\'s log on the Batch tab.'));
  const folder = el('input', { type: 'text', placeholder: 'folder with the PDFs' });
  const msg = el('span', { class: 'note' });
  const table = el('div');
  const acts = el('div', { class: 'row', style: 'margin-top:10px' });
  let scan = null;
  async function doScan() {
    msg.textContent = 'scanning…'; table.innerHTML = ''; acts.innerHTML = '';
    try { scan = await api('/api/standards/scan?folder=' + encodeURIComponent(folder.value.trim())); }
    catch (e) { msg.textContent = ''; toast(e.message, 'bad'); return; }
    if (!folder.value.trim()) folder.value = scan.folder;
    msg.textContent = scan.exists ? `${scan.items.length} PDF${scan.items.length === 1 ? '' : 's'} · converted so far: ${scan.converted.length ? scan.converted.join(', ') : 'none'}` : 'that folder does not exist';
    if (!scan.exists) return;
    if (!scan.qfm_installed) table.append(el('div', { class: 'err-list' }, 'Query file manager is not installed -- Modules page → Install.'));
    else if (scan.converter_missing && scan.converter_missing.length) table.append(el('div', { class: 'err-list' }, 'The PDF converter (Docling) is not installed in Query file manager\'s environment -- Modules page → Query file manager → Install PDF converter (a large download, once). The queue cannot start until it is.'));
    const tb = el('tbody');
    for (const it of scan.items) {
      const cb = el('input', { type: 'checkbox' }); cb.checked = !it.converted && !!it.stem; it._cb = cb;
      const sel = el('select', {}); it._sel = sel;
      sel.append(el('option', { value: '' }, '(from the file name)'));
      for (const s of scan.stems) sel.append(el('option', { value: s.stem, selected: s.stem === it.stem }, `${s.stem} — ${s.label}`));
      tb.append(el('tr', {}, el('td', {}, cb), el('td', {}, it.name, el('div', { class: 'note' }, fmtBytes(it.bytes))), el('td', {}, sel),
        el('td', {}, it.converted ? el('span', { class: 'st done' }, 'converted') : it.stem ? '' : el('span', { class: 'st failed' }, 'stem?'))));
    }
    table.append(el('table', { class: 'plan-steps stdtable' }, el('thead', {}, el('tr', {}, el('th', {}, 'queue'), el('th', {}, 'PDF'), el('th', {}, 'canonical stem'), el('th', {}, ''))), tb));
    const reb = el('input', { type: 'checkbox' }); reb.checked = true;
    const aud = el('input', { type: 'checkbox' }); aud.checked = true;
    const chunk = el('input', { type: 'number', value: 5, min: 1, max: 20 });
    acts.append(
      el('label', { class: 'note' }, reb, ' rebuild the index afterwards'), el('label', { class: 'note' }, aud, ' audit the corpus afterwards'),
      el('label', { class: 'note' }, 'pages per chunk ', chunk),
      el('button', { class: 'primary', disabled: !scan.qfm_installed, onclick: async () => {
        const items = scan.items.filter(it => it._cb.checked).map(it => ({ pdf: it.pdf, stem: it._sel.value }));
        if (!items.length) { toast('tick at least one PDF', 'bad'); return; }
        try {
          const d = await api('/api/standards/plan', { method: 'POST', body: { folder: scan.folder, items, rebuild_index: reb.checked, audit: aud.checked, chunk_pages: Number(chunk.value) || 5 } });
          if (d.errors && d.errors.length) { toast('queued, but it cannot start yet: ' + d.errors[0], 'bad'); }
          else toast('queued as a plan -- opening it on the Batch tab', 'ok');
          location.hash = 'batch?plan=' + d.id;
        } catch (e) { toast(e.message, 'bad'); }
      } }, 'Queue the conversions'));
  }
  pane.append(el('div', { class: 'card' },
    el('label', { class: 'f' }, 'Folder'), el('div', { class: 'row' }, el('div', { class: 'grow' }, folder), el('button', { onclick: doScan }, 'Scan'), msg),
    el('p', { class: 'note', style: 'margin-top:6px' }, 'Leave empty for the Query file manager\'s own standards folder. Stems are guessed from the file names -- correct any that are wrong; the skills\' retrieval ids depend on them.'),
    table, acts));
  m.append(pane);
  doScan();
}

/* ------------------------------------------------------------------ help */
function renderHelp(m) {
  const pane = el('div', { class: 'pane' });
  pane.append(el('h2', {}, 'Help'),
    el('p', { class: 'lead' }, 'Ask how Steltic works. Admin searches the hub\'s own source and the module checkouts on this PC -- READMEs, contracts, skills, code -- and, if you tick GitHub, the public repos too, then answers from the passages it found and shows them.'));
  const q = el('textarea', { rows: 3, placeholder: 'e.g. how does the Nonlinear module pick up the HR Steel design of the same project?' });
  const scope = el('select', {}, el('option', { value: 'local' }, 'this PC'), el('option', { value: 'both' }, 'this PC + GitHub'), el('option', { value: 'github' }, 'GitHub only'));
  const msg = el('span', { class: 'note' });
  const out = el('div');
  const hist = el('div', { class: 'hist' });
  async function ask() {
    if (!q.value.trim()) return;
    msg.textContent = 'searching…'; out.innerHTML = '';
    try {
      const d = await api('/api/help', { method: 'POST', body: { question: q.value, scope: scope.value } });
      msg.textContent = d.model ? 'answered by ' + d.model : (S.me && S.me.llm ? '' : 'no model connected -- showing the matching passages');
      out.append(el('div', { class: 'card' }, el('div', { class: 'answer' }, d.answer)));
      if (d.excerpts.length) {
        const det = el('details', {}, el('summary', { class: 'note', style: 'cursor:pointer' }, `${d.excerpts.length} passages used · searched ${d.sources.map(s => s.source + (s.files !== undefined ? ' (' + s.files + ' files)' : '')).join(', ')}`));
        for (const e of d.excerpts) det.append(el('div', { class: 'excerpt' }, el('div', { class: 'h' }, `${e.label} · lines ${e.lines}`), el('pre', {}, e.text)));
        out.append(det);
      }
      loadHist();
    } catch (e) { msg.textContent = ''; toast(e.message, 'bad'); }
  }
  q.onkeydown = e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) ask(); };
  async function loadHist() {
    try {
      const d = await api('/api/help/history');
      hist.innerHTML = '';
      if (!d.items.length) return;
      hist.append(el('h3', {}, 'Earlier questions'));
      for (const it of d.items) hist.append(el('div', {}, el('span', { class: 'q', onclick: () => { q.value = it.question; scope.value = it.scope || 'local'; ask(); } }, it.question), el('span', { class: 'note' }, '  · ' + fmtAge(it.t))));
    } catch (e) { }
  }
  pane.append(el('div', { class: 'card' }, el('label', { class: 'f' }, 'Question'), q,
    el('div', { class: 'row', style: 'margin-top:8px' }, el('button', { class: 'primary', onclick: ask }, 'Ask'), scope, msg)), out, hist);
  m.append(pane);
  loadHist();
}

/* ------------------------------------------------------------------ boot */
(async () => {
  await loadMe();
  try { S.hub = await api('/api/hub/state'); } catch (e) { S.hub = null; }
  route();
})();
