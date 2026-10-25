/* User-designed building -- five steps: file -> questions -> build -> confirm -> run. */
'use strict';

const qs = new URLSearchParams(location.search);
const S = {
  project: (qs.get('project') || 'Project').replace(/[^A-Za-z0-9_-]/g, '') || 'Project',
  step: 1, hr: null, intake: null, answers: {}, checks: null, run: null, next: 0, log: [], poll: null,
  os: null, osOpts: { units: '', up: '', main: '' }, osNeed: null, osCandidates: [], fileName: '',  // OpenSees intake
  text: null, textNeeds: null, llm: false, model: '', textRep: null,                                // the text box
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
  if (!r.ok && r.status !== 409 && r.status !== 202) {
    const det = d && (d.detail || d.error);
    const err = new Error((typeof det === 'string' ? det : (det && JSON.stringify(det))) || `${r.status} ${r.statusText}`);
    err.data = d; err.status = r.status; throw err;
  }
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
  try { const o = await api(`${P()}/opensees`); S.os = (o && o.units) ? o : null; } catch (e) { S.os = null; }
  try { const t = await api(`${P()}/text`); S.textRep = (t && t.text) ? t : null; } catch (e) { S.textRep = null; }
}
function render() {
  renderSteps();
  $('#projname').textContent = S.project;
  const m = $('#main');
  m.replaceChildren(el('div', { class: 'pane' }, [null, step1, step2, step3, step4, step5][S.step]()));
}

// ------------------------------------------------------------------------------------------- 1 file
const OS_EXT = /\.(tcl|py|zip)$/i;
const UNIT_NAMES = ['kip-in', 'kip-ft', 'lb-in', 'lb-ft', 'kN-m', 'N-m', 'N-mm', 'kN-mm', 'kN-cm', 'tf-m', 'kgf-cm'];
function osOptions() {
  const o = S.osOpts;
  const need = S.osNeed;
  const units = el('select', { class: need === 'UNITS' ? 'need' : '', onchange: () => { o.units = units.value; } },
    el('option', { value: '' }, 'read from the steel modulus E'), UNIT_NAMES.map(u => el('option', { value: u, selected: o.units === u }, u)));
  const up = el('select', { onchange: () => { o.up = up.value; } },
    [['', 'from the model (diaphragms, supports, gravity)'], ['Z', 'Z is up'], ['Y', 'Y is up']].map(([v, t]) => el('option', { value: v, selected: o.up === v }, t)));
  const isZip = /\.zip$/i.test(S.fileName);
  let main = null;
  if (isZip) {
    main = S.osCandidates.length
      ? el('select', { class: need === 'MAIN' ? 'need' : '', onchange: () => { o.main = main.value; } },
          el('option', { value: '' }, '— choose the main script —'), S.osCandidates.map(c => el('option', { value: c, selected: o.main === c }, c)))
      : el('input', { type: 'text', value: o.main, placeholder: 'found automatically (the script nothing else sources)', onchange: () => { o.main = main.value.trim(); } });
  }
  return el('div', { class: 'osopts' },
    el('div', { class: 'kv', style: 'margin-bottom:6px' }, 'OpenSees model — the script is run against a recorder (nothing is analysed) and the building is read from the model it builds.'),
    el('div', { class: 'row' }, el('label', { class: 'kv' }, 'Units ', units), el('label', { class: 'kv' }, 'Vertical axis ', up),
      isZip ? el('label', { class: 'kv' }, 'Main file ', main) : null));
}
function osCard(r) {
  if (!r || !r.units) return null;
  const mem = r.members || {}; const elx = r.elements || {};
  const secs = (r.sections || []).map(x => el('tr', {},
    el('td', {}, x.name), el('td', {}, x.members), el('td', {}, x.source || ''),
    el('td', { class: (x.named || x.proposal) ? 'st-pass' : 'st-warn' }, x.named ? x.named + ' (named)' : (x.proposal ? `${x.proposal} (${x.error_pct}%)` : 'you give the shape')),
    el('td', {}, x.proposal ? (x.compared || '') : (x.closest ? `closest ${x.closest}${x.error_pct != null && x.error_pct < 1000 ? ` (${x.error_pct}%)` : ''}` : (x.compared || '')))));
  const unk = Object.entries(r.unknown_commands || {});
  const dsc = r.described;
  return el('div', { class: 'card tight' },
    el('h3', {}, dsc ? 'The model built from your description' : 'What was read from the OpenSees model'),
    dsc ? el('div', { class: 'kv', style: 'margin-bottom:6px' }, `Read by ${dsc.model} into an OpenSeesPy script, which the OpenSees interpreter read. `,
      el('a', { href: `${P()}/opensees/script` }, 'Download the script'), ' — check the 3-D model (step 4) before you confirm it.') : null,
    !dsc && r.typed ? el('div', { class: 'kv', style: 'margin-bottom:6px' }, `Pasted ${r.typed.language === 'tcl' ? 'OpenSees Tcl' : 'OpenSeesPy'} code. `, el('a', { href: `${P()}/opensees/script` }, 'Download it as a file')) : null,
    el('div', { class: 'kv' }, el('b', {}, r.upload || r.file || ''), r.main && r.main !== r.upload ? ` · main ${r.main}` : '',
      ` · ${r.language === 'tcl' ? 'OpenSees Tcl' : 'OpenSeesPy'}`, r.models > 1 ? ` · ${r.models} models built (the largest is read)` : ''),
    el('ul', { class: 'small' },
      el('li', {}, `Units: ${r.units.name} — ${r.units.how}`),
      el('li', {}, `Vertical axis: ${r.up.axis} — ${r.up.how}`),
      el('li', {}, `${mem.levels || 0} levels · grid ${(mem.grid || [0, 0]).join(' × ')} · ${mem.columns || 0} columns · ${mem.beams || 0} beams · ${mem.braces || 0} braces — from ${elx.lines || 0} line elements` +
        `${elx.connectors ? `, ${elx.connectors} connectors` : ''}${elx.collapsed_into_joints ? `, ${elx.collapsed_into_joints} collapsed into joints` : ''}`),
      r.masses && Object.keys(r.masses).length ? el('li', {}, 'Seismic weight from the masses: ' + Object.entries(r.masses).map(([k, v]) => `${k} ${v} kip`).join(', ')) : null,
      unk.length ? el('li', {}, 'Commands not known to the reader (ignored): ' + unk.map(([k, v]) => `${k} ×${v}`).join(', ')) : null),
    secs.length ? el('table', { class: 'ck' }, el('tr', {}, el('th', {}, 'Section in the model'), el('th', {}, 'Members'), el('th', {}, 'From'), el('th', {}, 'AISC shape'), el('th', {}, 'Compared')), secs) : null,
    el('div', { class: 'small', style: 'margin-top:6px' }, dsc
      ? 'The shapes you named are used as named. The values read from your words (SDS, loads, systems …) come back in step 2 as defaults to accept, each with the words they came from; nothing is used until you accept it.'
      : 'Proposed shapes, the dead load from the masses and the steel grade come back in step 2 as defaults to accept; nothing is used until you accept it.'));
}
// Step 1: the file, the text box, or both. With a file, the text gives what the file leaves open (values.py on the
// server: `name = value` lines read directly, sentences with the hub's model connection); without one, the text is
// the building -- plain words or pasted OpenSees code (describe.py).
const WORDS_PH = 'Describe the building, e.g.\n\n4 stories: 15 ft first story, 13 ft above. Grid 1-5 at 30 ft in X, A-D at 25 ft in Y. ' +
  'W14X132 columns in stories 1-2 and W14X90 above, strong axis in X. Moment frames on lines A and D with W24X76 beams; ' +
  'all other beams W18X35 with shear tabs. Chevron braces HSS8X8X1/2 on lines 1 and 5 between B and C, every story. ' +
  'Rigid diaphragms, fixed bases. SDS 1.0, SD1 0.6, S1 0.6, site class D, risk category II, SMF in X, SCBF in Y. ' +
  'Dead load 85 psf, live load 50 psf.\n\n— or paste an OpenSees Tcl or OpenSeesPy script.\n\n' +
  '— or attach a file above and give here what it leaves open.';
const VALUES_PH = 'What the file leaves open, e.g.\n\nSDS = 1.0, SD1 = 0.6, S1 = 0.6, TL = 8\nrisk category = II, site class = D\n' +
  'system X = SMF, system Y = SCBF\nD = 90 psf, L = 50 psf, Lr = 20 psf, snow = 0\ngrade = A992, bases = fixed, diaphragm = rigid\n' +
  'floor system = one-way, deck span = X, doublers = none\nT1 X = 1.25 s, V X = 410 kip, seismic weight = 6850 kip\n\n' +
  '— or in sentences ("risk category II on site class D, special moment frames both ways"), read with the hub\'s model connection.';
function step1() {
  const f = el('input', { type: 'file', accept: '.xlsx,.xlsm,.e2k,.$et,.json,.tcl,.py,.zip' });
  const optsBox = el('div', {}, OS_EXT.test(S.fileName) ? osOptions() : null);
  const ta = el('textarea', { class: 'describe', spellcheck: false, value: loadText() });
  const hint = el('div', { class: 'small', style: 'margin-top:4px' });
  const rep = el('input', { type: 'checkbox' });
  const hasFile = () => f.files.length > 0;
  const go = el('button', { class: 'primary' });
  const add = el('button', {}, 'Add these values to the building read already');
  const setUi = () => {
    const hf = hasFile(), t = ta.value.trim(), k = textKind(ta.value);
    go.textContent = hf && t ? 'Read the file and the text' : (hf ? 'Read the file' : 'Build from this text');
    ta.placeholder = hf ? VALUES_PH : WORDS_PH;
    add.style.display = (!hf && t && k === 'words' && mi()) ? '' : 'none';
    hint.textContent = hf
      ? (!t ? 'Optional: type what the file leaves open (the seismic basis, loads, systems, grades, your own T1 / V / W). Each value comes back in step 2 as a default to accept, quoting your words; the file\'s own values are never replaced.'
        : k !== 'words' ? 'The box holds OpenSees code: with a file attached it takes values, not a model. Attach the code as the file, or remove the file to build from the code.'
        : `With the file: 'name = value' lines are read directly${S.llm ? `, sentences by ${S.model || 'the hub\'s model connection'}` : '; sentences need the hub\'s model connection (none is set)'}. Each value comes back in step 2 as a default to accept, quoting your words.`)
      : !t ? (S.llm ? `Plain words are read with the hub's model connection (${S.model || 'set in the hub'}).` : 'Plain words need the hub\'s model connection (set it in the hub); pasted OpenSees code does not.')
      : k === 'words' ? (S.llm ? `Plain words: ${S.model || 'the model'} reads them into an OpenSeesPy script, and the OpenSees interpreter reads that. Anything the words leave open is asked, not guessed.` : 'Plain words need the hub\'s model connection, and none is set: set one in the hub, or paste an OpenSees script, or attach a file and give its open values as name = value lines.')
      : `Pasted ${k === 'tcl' ? 'OpenSees Tcl' : 'OpenSeesPy'} code: read by the OpenSees interpreter as it stands.`;
  };
  f.onchange = () => {
    const n = hasFile() ? f.files[0].name : '';
    if (n !== S.fileName) { S.osNeed = null; S.osCandidates = []; S.osOpts.main = ''; }
    S.fileName = n; optsBox.replaceChildren(OS_EXT.test(n) ? osOptions() : '');
    setUi();
  };
  ta.oninput = () => { S.text = ta.value; try { localStorage.setItem(TEXT_KEY(), ta.value); } catch (e) { /* private window */ } setUi(); };
  const done = async (r, what) => {
    S.intake = r; S.osNeed = null; S.osCandidates = []; S.textNeeds = null;
    if (r.text_read) S.textRep = r.text_read;
    await refresh(); S.step = 2; render();
    const tr = r.text_read;
    if (tr) {
      const n = (tr.values || []).length;
      toast(n ? `${what}: ${n} value${n === 1 ? '' : 's'} read from your text — accept them in step 2` : `${what}: nothing was taken from your text (see step 1)`, n ? 'ok' : 'bad');
    }
  };
  go.onclick = async () => {
    const hf = hasFile(), t = ta.value;
    if (!hf && !t.trim()) return toast('Choose a file, type the building, or both', 'bad');
    const label = go.textContent;
    go.disabled = true; add.disabled = true;
    try {
      if (hf) {
        const fd = new FormData(); fd.append('file', f.files[0]); if (rep.checked) fd.append('replace', '1');
        if (t.trim()) fd.append('text', t);
        if (OS_EXT.test(f.files[0].name)) { const o = S.osOpts; if (o.units) fd.append('units', o.units); if (o.up) fd.append('up', o.up); if (o.main) fd.append('main', o.main); }
        go.textContent = OS_EXT.test(f.files[0].name) ? 'Running the script…' : (t.trim() ? 'Reading the file and the text…' : 'Reading…');
        const r = await api(`${P()}/upload`, { method: 'POST', body: fd, raw: true });
        if (r.__status === 409) { toast(r.detail || 'refused', 'bad'); return; }
        await done(r, 'File read');
      } else {
        const k = textKind(t);
        go.textContent = k === 'words' ? 'Reading your description…' : 'Running the script…';
        S.textNeeds = null;
        const body = { text: t, replace: rep.checked };
        if (k !== 'words') { if (S.osOpts.units) body.units = S.osOpts.units; if (S.osOpts.up) body.up = S.osOpts.up; }
        const r = await api(`${P()}/describe`, { method: 'POST', body });
        if (r.__status === 409) { toast(r.detail || 'refused', 'bad'); return; }
        if (r.needs) { S.textNeeds = r; render(); toast('The description needs a little more before the model can be built', 'bad'); return; }
        S.textRep = null; await done(r, 'Built from the text');
      }
    } catch (e) {
      const d = e.data || {};
      if (d.code === 'UNITS') {
        S.osNeed = 'UNITS';
        if (!hf) { S.fileName = 'typed.' + textKind(t); toast('Choose the units of the script (under the file box): ' + e.message, 'bad'); render(); return; }
        toast('Choose the units of the model: ' + e.message, 'bad');
      } else if (d.code === 'MAIN') { S.osNeed = 'MAIN'; S.osCandidates = d.candidates || []; toast(e.message, 'bad'); }
      else toast(e.message, 'bad');
      if (d.code === 'UNITS' || d.code === 'MAIN') optsBox.replaceChildren(osOptions());
    } finally { go.disabled = false; add.disabled = false; go.textContent = label; }
  };
  add.onclick = async () => {
    add.disabled = true; go.disabled = true; const label = add.textContent; add.textContent = 'Reading the text…';
    try { await done(await api(`${P()}/values`, { method: 'POST', body: { text: ta.value } }), 'Values added'); }
    catch (e) { toast(e.message, 'bad'); }
    finally { add.disabled = false; go.disabled = false; add.textContent = label; }
  };
  setUi();
  const s = S.intake && S.intake.summary;
  const n = S.textNeeds;
  return el('div', {},
    el('div', { class: 'about' },
      el('p', {}, el('b', {}, 'A building you designed. '), 'HR Steel models it exactly as given: nothing is resized and nothing is invented. Anything key that your file does not settle comes back as a question; a proposed default is used only once you accept it, and every accepted default is printed in every report.'),
      el('p', {}, 'Give the ', el('b', {}, 'Steltic building template'), ' (Excel), an ', el('b', {}, 'ETABS .e2k'), ' text model, or an ', el('b', {}, 'OpenSees model'),
        ' — Tcl (.tcl), OpenSeesPy (.py), or a .zip of the model folder when the script sources other files — and type in the box what the file leaves open (SDS, loads, systems, your own T1 / V / W); or type the whole building in the box: paste OpenSees code, or describe it in plain words. Version 1 systems: SMF, IMF, OMF, SCBF, OCBF, BRBF, EBF and their dual combinations. Plate shear walls, STMF, partially restrained connections, sloped or plan-diagonal beams, several diaphragm types and per-member grades are refused, never simplified.'),
      el('p', {}, 'From an OpenSees script HR takes the geometry, sections, end releases, supports, diaphragms and masses — never its nonlinear springs, fibre materials or analyses: the Nonlinear module builds its own hinges from AISC 342. Sections are matched to AISC shapes by their properties and come back as defaults to accept.')),
    el('div', { class: 'card' },
      el('div', { class: 'row' }, el('a', { href: '/api/template', download: 'steltic_building_template.xlsx' }, 'Download the blank template'),
        S.hr && S.hr.state !== 'none' ? el('a', { href: `/api/template?project=${enc(S.project)}` }, 'Download what HR has read, as a filled template') : null),
      el('h3', {}, 'Your file'), el('div', { class: 'row' }, f), optsBox,
      el('h3', {}, 'Text'), ta, hint,
      el('div', { class: 'row', style: 'margin-top:8px' }, go, add),
      el('label', { class: 'kv', style: 'display:block;margin-top:8px' }, rep, ' replace a normal HR Steel design of the same name (it is archived, not deleted)'),
      n ? el('div', { class: 'needs' },
        el('div', { class: 'kv' }, el('b', {}, 'Before the model can be built, the description must also say:')),
        el('ul', { class: 'small' }, n.needs.map(x => el('li', {}, x))),
        (n.unclear || []).length ? el('div', { class: 'kv' }, `${n.model} also found these open:`) : null,
        (n.unclear || []).length ? el('ul', { class: 'small' }, n.unclear.map(x => el('li', {}, x))) : null,
        (n.not_taken || []).length ? el('ul', { class: 'small' }, n.not_taken.map(x => el('li', { class: 'st-warn' }, 'Not taken: ' + x))) : null,
        el('div', { class: 'small' }, 'Add it to the text and build again.')) : null),
    s ? el('div', { class: 'card tight' }, el('div', { class: 'kv' },
      el('b', {}, `${s.source && s.source.file || ''}`), ` · ${s.levels} levels · grid ${s.grid.x} × ${s.grid.y} · ${s.columns} columns · ${s.beams} beams · ${s.braces} braces`),
      (s.notes || []).filter(x => !/^read from your text/.test(x)).length ? el('ul', { class: 'small' }, s.notes.filter(x => !/^read from your text/.test(x)).map(x => el('li', {}, x))) : null) : null,
    textReadCard(S.textRep),
    osCard((S.intake && S.intake.opensees) || S.os));
}

// What the text beside the file gave: each value with the words it came from, and what was not used.
const VKIND = { proposal: 'a default to accept in step 2', 'asked later': 'a default to accept once step 2 asks it', 'your own result': 'to the confirmation check (warning only)' };
function fmtVal(v) { return typeof v === 'object' ? JSON.stringify(v) : String(v); }
function textReadCard(r) {
  if (!r || !r.text) return null;
  const vals = r.values || [];
  return el('div', { class: 'card tight' },
    el('h3', { style: 'margin-top:0' }, 'Read from your text', r.model ? el('span', { class: 'kv' }, ` · sentences read by ${r.model}`) : null),
    vals.length ? el('table', { class: 'ck' }, el('tr', {}, el('th', {}, 'Question'), el('th', {}, 'Value'), el('th', {}, 'Your words'), el('th', {}, 'Goes')),
      vals.map(v => el('tr', {}, el('td', {}, el('code', {}, v.id), el('div', { class: 'small' }, v.question || '')), el('td', {}, fmtVal(v.value)),
        el('td', {}, '“' + v.words + '”'), el('td', { class: 'small' }, VKIND[v.kind] || v.kind)))) : el('div', { class: 'kv' }, 'No value was taken from the text.'),
    (r.notes || []).length ? el('ul', { class: 'small' }, r.notes.map(x => el('li', { class: /^not taken|could not|did not/.test(x) ? 'st-warn' : '' }, x))) : null,
    (r.unclear || []).length ? el('ul', { class: 'small' }, r.unclear.map(x => el('li', { class: 'st-warn' }, 'Unclear: ' + x))) : null,
    (r.not_used || []).length ? el('ul', { class: 'small' }, r.not_used.map(x => el('li', {}, 'Not used: ' + x))) : null,
    vals.length && r.applied === false ? el('div', { class: 'small st-warn' }, 'HR did not take these values (see above).') : null,
    el('div', { class: 'small', style: 'margin-top:6px' }, 'Nothing from the text is used until you accept it in step 2, where each value shows the words it came from. Where the file gives a value, the file\'s is used.'));
}

// The text box (no file): the building in plain words (read with the hub's model connection into an OpenSeesPy
// script, which the OpenSees interpreter reads), or pasted OpenSees code (read directly).
const TEXT_KEY = () => 'steltic.ub.text.' + S.project;
const PY_CODE = /^\s*(import\s+openseespy|from\s+openseespy|ops\.(model|node|element|wipe)\s*\()/m;
const TCL_CODE = /^\s*(model\s+(basic|BasicBuilder)\b|node\s+\d+\s+[-\d.]|element\s+\w+\s+\d+\s+\d+)/m;
function textKind(t) { return PY_CODE.test(t) ? 'py' : (TCL_CODE.test(t) ? 'tcl' : 'words'); }
function loadText() {
  if (S.text !== null) return S.text;
  try { S.text = localStorage.getItem(TEXT_KEY()) || ''; } catch (e) { S.text = ''; }
  if (!S.text && S.os) S.text = (S.os.described && S.os.described.text) || (S.os.typed && S.os.typed.text) || '';
  if (!S.text && S.textRep) S.text = S.textRep.text || '';
  return S.text;
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
function fromText(m) {
  return ((m && m.groups) || []).flatMap(g => g.items || []).filter(it => it.kind === 'default' && /^from your (text|description)/.test(it.default_reason || ''));
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
  const ft = fromText(m);
  const accAll = ft.length ? el('button', { class: 'primary', onclick: async () => {
    accAll.disabled = true; save.disabled = true;
    try {
      // the values HR asks only after these (R, Cd, Omega0, rho once the system is set; the deck span once the floors
      // are one-way) come back with the next answer: accepted in the same click, a few rounds at most
      let cur = m, n = 0; const seen = new Set();
      for (let round = 0; round < 5; round++) {
        const todo = fromText(cur).filter(it => !seen.has(it.id) && !(S.answers[it.id] && 'value' in S.answers[it.id]));
        if (!todo.length) break;
        const body = round === 0 ? Object.assign({}, S.answers) : {};
        todo.forEach(it => { body[it.id] = { accept: true }; seen.add(it.id); });
        if (round === 0) for (const [k, v] of Object.entries(S.answers)) if ('value' in v) body[k] = v;   // your own value wins
        const r = await api(`${P()}/answers`, { method: 'POST', body: { answers: body } });
        n += todo.length; cur = r.missing_items;
      }
      S.answers = {}; await refresh();
      toast(`${n} value${n === 1 ? '' : 's'} from your text accepted` + (cur && cur.ready ? ': nothing open, HR can build the model' : ''), 'ok'); render();
    } catch (e) { toast(e.message, 'bad'); } finally { accAll.disabled = false; save.disabled = false; }
  } }, `Accept the ${ft.length} value${ft.length === 1 ? '' : 's'} from your text`) : null;
  const groups = (m.groups || []).map(g => el('div', {}, el('div', { class: 'grp' }, g.group),
    g.items.map(it => el('div', { class: 'item ' + it.kind }, el('div', { class: 'q' }, el('span', { class: 'tag ' + it.kind }, it.kind), it.question),
      el('div', { class: 'why' }, 'needed for: ' + it.needed_for + (it.where ? ' · ' + it.where : '')), answerInput(it)))));
  return el('div', {},
    el('h2', {}, 'What HR needs before it can build'),
    el('p', { class: 'lead' }, 'Key items have no default: the build never starts without them. A proposed default is used only once you accept it. Refused items are outside version 1: change them in your model, or the building cannot be run.'),
    el('div', { class: 'counts' }, ['key', 'default', 'refused', 'info'].map(k => el('span', {}, `${m.counts[k]} ${k}`))),
    el('div', { class: 'row' }, accAll, save, m.ready ? el('button', { onclick: () => { S.step = 3; render(); } }, 'Go to Build →') : null),
    ft.length ? el('div', { class: 'small', style: 'margin-top:4px' }, 'The defaults marked “from your text” quote the words they came from. Accepting them also accepts the values of your text that HR asks only afterwards (R, Cd, Ω0, ρ once the system is set; the deck span once the floors are one-way).') : null,
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
  try { const me = await api('/api/me'); S.llm = !!me.llm; S.model = me.model || ''; $('#status').textContent = me.steltic_up ? 'HR Steel connected' : 'HR Steel not reachable'; $('#status').className = 'note ' + (me.steltic_up ? 'ok' : 'bad'); } catch (e) { /* */ }
  await refresh();
  S.step = !mi() ? 1 : (!mi().ready ? 2 : (!built() ? 3 : (!confirmed() ? 4 : 5)));
  render();
  if (hrState() === 'running' || (S.run && S.run.state === 'running')) startPoll();
})();
