# Steltic Hub — review against the original repos (2026-09-13)

The hub was read end to end and compared with the three apps it packages —
[steltic](https://github.com/Steltic/steltic), [steltic_cfs](https://github.com/Steltic/steltic_cfs),
[steltic_nonlinear](https://github.com/Steltic/steltic_nonlinear) — plus steltic_grokbot, which the
catalog also carries. Every finding below was fixed in this pass and is covered by the test suite
(`python -m pytest tests -q`, 33 tests) and by a live run of the hub against the real modules
(HR Steel + CFS installed through the hub, started, fed a MOCK design; Nonlinear installed and run
on the packaged Ex22 example through the Inspect / Compare / Mesh tabs).

## Bugs that broke the product

| # | Where | What was wrong | Fix |
|---|---|---|---|
| 1 | Full UI tab | The module's page was proxied under `/m/<id>/`, but its HTML loads `/static/app.js` and calls `/api/…` by absolute path — those resolved against the **hub**, so the frame ran the hub's own JS inside itself instead of the module. | Embed tabs now start the module server and frame its own origin (`http://127.0.0.1:<port>/`). The `/m/` proxy is kept for API calls only. |
| 2 | Stop button | `POST /api/cancel/<run>` only knew CLI processes; for HR Steel / CFS runs it did nothing. | Manifests declare `run.cancel` (`POST /api/stop {building}` — the endpoint the original UIs use); the hub calls it. CLI runs are killed as a process tree (`taskkill /T` on Windows, since a hub started by `pythonw` has no console to send CTRL_BREAK from). |
| 3 | Run log | The agents stream `token` events (one per word); each became its own line, `reasoning` tokens flooded the log, `tool` lines printed `undefined`, `tool_result`, `usage` and the `paused` reason were dropped, and the run was reported "finished" even after the module reported `paused`/`error`. | The log renders the agents' event vocabulary the way their own UI does: streamed text on one line with a cursor, tool call / result lines with timings, milestones, a separate model-reasoning box, token usage, activity lights, correct paused / failed / stopped status. |
| 4 | Switching tabs mid-run | Re-rendering the pane threw the live log away (the fetch kept running invisibly). | Run state and its log element live per (module, tab) and are re-attached; the tab chip shows a spinner; the rail says "running…". |
| 5 | Uploaded files | A file field's value was a bare name relative to the project folder, but Docs & Standards runs with `cwd = <data>/grokbot` — the PDF / plan file was never found. | File values are resolved to absolute paths inside the project folder (traversal refused) before they reach any command. |
| 6 | Project names | The hub kept `.` in names; the design servers strip it (`Ex7a.v2` → `Ex7av2`), so their outputs landed in a folder the hub never looked in. | Names are reduced to `[A-Za-z0-9_-]`, the exact set the servers keep. |
| 7 | New project | The "+ New project" path never created the folder, so the new project did not appear in the list while the chip claimed it was active. | `POST /api/jobs/<name>` creates it; the select and the chip agree. |
| 8 | Numeric fields | An empty number input was sent as `0`, and a genuine `0` was dropped by `val in (None, "", False)` (`0 == False`). | Empty → default; `0` is a value. |
| 9 | SSE relay | Module streams were relayed as raw chunks decoded per chunk — a multibyte character split across chunks was corrupted; the 30 MB base64 `bundle` event meant for the module's browser copy was forwarded to the hub UI. | Events are parsed incrementally and re-emitted; `bundle` is dropped (the hub reads files directly); outcome is recorded in the project history. |
| 10 | `/api/state` | Every refresh spawned each module's interpreter and imported openseespy (seconds each, up to 120 s timeouts) and ran `git status` on linked copies. | Environment checks are cached per interpreter; git status cached 30 s; status work runs off the event loop. |
| 11 | Server start race | Two concurrent requests (embed frame + example fetch) could spawn two servers for one module. | Per-module lock around start-and-health-wait. |
| 12 | Windowless start | Children spawned from `pythonw` had no `CREATE_NO_WINDOW`, so every module server / run flashed a console window. | All child processes are created without a window. |
| 13 | Templating | `expand()` did sequential `str.replace`, so a field value containing braces could be re-expanded by a later key. | Single-pass regex substitution; unknown placeholders stay literal. |
| 14 | Install log | The Modules pane re-rendered after an install and wiped the log the moment it finished (including the error you wanted to read). | Logs persist per module across re-renders; a module with a run in progress refuses Update / Rebuild / Remove. |
| 15 | Rebuild env | Rebuilding deleted the venv under a running module server. | The server is stopped first; a failed delete is reported instead of ignored. |
| 16 | Data folder | The hub defaulted to `%LOCALAPPDATA%\StelticHub`, the launcher to `…\Steltic`, and the Tauri shell set nothing — three entry points, two data folders. | One default (`Steltic`); the Tauri shell passes `STELTIC_HUB_DATA` / `STELTIC_HUB_UV` explicitly and calls the bootstrap with `-BootstrapOnly` so it no longer starts a second hub. |
| 17 | Launcher | If port 8300 was taken the hub silently chose another port and the launcher timed out; a failure inside the hidden PowerShell window waited forever on `Read-Host`. | The hub writes `hub.url`; the launcher reads it. Failures show a message box and open the log. |
| 18 | `prompt()` / `confirm()` / `alert()` | Browser dialogs (unreliable in app windows) were used for new project, link path, remove, delete. | In-app modals. |
| 19 | HTTP errors | `stream()` ignored non-OK JSON responses (a 400/404 produced silence). | Errors are surfaced in the log. |
| 20 | `git fetch` / reset | Reset targeted `origin/<branch>`; on a shallow clone the guaranteed ref is `FETCH_HEAD`. | Reset and update-check use `FETCH_HEAD`. |
| 21 | `subdir` modules | `{module_dir}` ignored `source.subdir` (used by the bundled example module), and a linked copy pointing at the subfolder itself would have re-appended it. | `Registry.module_root()` resolves it once, everywhere. |
| 22 | Catalog | A broken `catalog/*.json` vanished silently. | Logged to stderr; the Modules pane flags an ignored checkout manifest. |

## Functionality restored from the original apps

| Original feature | Hub before | Hub now |
|---|---|---|
| Example briefs (36 HR / 25 CFS) load into the brief | Worked via a hidden `examples`-id convention | Declared in the manifest (`fills`), generic for any module |
| Upload files: `.txt`/`.md` into the brief, PDF text extracted, images sent to the model | Missing | `attachments` field, identical limits (3 files, 5 MB), images passed as `images` |
| CFS **Analysis fidelity** selector (Auto / Tier 0 / 1 / 2) | Missing | Field on the CFS Design tab, plus the explainer link |
| Download the design package (.zip) | Missing (only via the broken Full UI) | Link on the Design and Continue tabs |
| Stop a running design | Broken | Works (see #2) |
| Streamed text, tool calls, results, timings, milestones, reasoning box, token usage, activity lights | Degraded (see #3) | Rendered like the original |
| Continue with follow-up instructions and files | Text only | Text + attachments |
| Keepalive during long silences | Relayed from the module; none for CLI runs | Both hub and modules ping every 20 s |
| Nonlinear: `--skip`, `--member-nseg`, `--post-cap-ratio`, `--no-block`, site-class choices, mesh ladder `--analyses`, `--early-abort-nc`, `--params`, `--steltic-engine` | Missing | Exposed |
| Nonlinear on the HR Steel design of the *same project* | Only by downloading a zip from the module UI and re-uploading it | The package field defaults to this project's HR Steel design (`{out.steltic}`); the run copies it into the project folder (`run.stage`), so the viewer strip lights up as the README promised. A zip still works, and Inspect uses the same folder as Run (it used to unpack into a sub-folder). |

## Manifest additions (all optional, all generic)

`run.cancel`, `run.stage`, `tab.links`, field `fills`, field type `attachments` (`text_target`,
`max_files`, `max_mb`), file-field `default` templates, and the `{out.<module>}` / `{jobs_dir}`
templates. The hub still contains no module-specific code (the AST test enforces it).

## Not changed on purpose

* HR Steel and CFS keep their own data layout (`DATA_DIR/sessions/local/jobs/<project>`), because
  their servers archive and restore that folder themselves. The hub serves it, and the Nonlinear
  module imports from it.
* A fresh Design run on a project that already holds a design archives the old one as
  `<project>__<timestamp>` — that is the module's own behaviour and is now explained in the tab.

---

# Second pass (same day): grounding, local posture, names, no example module

## Spec grounding for the design agents

The agents' `search_engineering_standards` tool calls `RAG_API_URL` — an API written for a hosted
vector database — and the hub started them without it, so every design ran in the "cite from
memory" mode. Meanwhile the Query file manager module held exactly the corpus they need, reachable
only by hand from its tabs. Now:

* `catalog/steltic_grokbot/rag_server.py` (bundled in the hub, runs in the module's own venv,
  stdlib only) answers `POST /query` in the agents' format from the corpus: `clause` → exact
  section / equation / table lookup, otherwise full-text search with the corpus's alias
  expansion, `chapter` narrowing, provisions preferred over commentary; the agents' collection
  names mapped onto the canonical stems (A360→AISC_360_22, S100→AISI_S100, … , examples, OpenSees).
  Anything the corpus cannot answer is an empty list, never an error — a non-2xx pauses the run.
* Hub: `server.requires` + `{server.<id>}`. HR Steel and CFS declare
  `RAG_API_URL: "{server.steltic_grokbot}/query"`; the hub starts the grounding server first and
  passes its address. Not installed → the variable is left unset (the module's own no-RAG mode);
  installed later → the design server restarts with it on its next run. The dependency shows on
  the Modules card ("uses Query file manager").
* A **Grounding** tab on Query file manager shows what the agents asked and what came back;
  every query is also logged to `queue/agent_queries.jsonl` for provenance.
* Verified live: HR Steel's process carries `RAG_API_URL=http://127.0.0.1:<port>/query`, and its
  own search tool (run inside its venv) gets OpenSees and worked-example hits from the bridge and
  an empty list for a specification that is not converted yet.

Note: the Grok Bot "skills" in the steltic_grokbot repo are prompts for Grok Bots on xAI's
platform and are not used by the local apps; their local equivalent is the `contract/` system
prompt each design repo already ships.

## Local-PC posture (this runs on the user's own machine, not a shared server)

* The LLM connection is saved on this PC (`connection.json` in the data folder, owner-only on
  POSIX) and reloaded on start; editing the model no longer forces retyping the key; **Forget**
  deletes it. Wording that promised "in memory only, never on disk" is gone.
* No hub-side upload caps: the 400 MB upload limit and the 3-files / 5 MB attachment limits are
  removed (a manifest can still set `max_files` / `max_mb` if a module needs them; the catalog
  does not). The design servers themselves still keep the first three images at up to 5 MB each
  — that limit lives in their repos (`_clean_images` in `steltic/main.py`), not in the hub.

## Names

* "Docs & Standards" is now **Query file manager**.
* Any module can be renamed from the Modules tab (✎): the display name is stored in `state.json`
  and used everywhere the hub names the module; the manifest is untouched.

## Example module removed

`examples/` is gone from the product, with its test and README sections. The Windows self-test
now writes its own throwaway smoke module into `selftest-data\` and forgets any old
`example_module` registration first (`steltic-hub forget <id>` is the new CLI command for that).

---

# Third pass: the Design variations module

A new module, bundled inside the hub (`catalog/steltic_variations/`), generalising the 100-model
variation study of one tower into a tool for any brief.

## What it does

* **Inputs**: the base brief (typed, or read back from this project's HR Steel package), the
  number of variations, and one of three ways to define them — pick from ten categories
  (Groups A–K of the study, made generic: no grid names, levels or member sizes of one
  building), describe the study in words, or let the model propose the list. The list is
  editable before anything runs; M001 is always the base unchanged.
* **Design**: each variation is one HR Steel job, `<project>_M0xx`, briefed as base + "change
  ONLY what is stated". Sequential, in a background thread that survives the browser tab; the
  study's event stream is re-attachable; Stop asks HR Steel to stop the current building and
  skips the rest. A run the agent ends with NOT PERMITTED is recorded as such (with the clause),
  not as a failure.
* **Metrics** are read deterministically from the package wherever the package states them
  (schedule → tonnage and member counts; report → drift table, wind drift, V/W/Cs, T1, wind
  base shear; calc_package → D/C, ρ, Ax, torsion class, SCWB) and the model reads the report
  for what it does not (moment-connection count, SMF share, wind comfort, dual/not). Without an
  LLM the connection count is estimated from the grid and flagged `~` in the table.
* **Eligibility and score** follow the study's Phase-2 rule and default equation exactly, both
  editable; a description in words is turned into an equation by the model and shown back.
  The equation evaluator is an AST whitelist (names, numbers, arithmetic, min/max/abs/sqrt,
  `<metric>_max/_min` over the eligible set) — `__import__('os')` and friends are rejected.
* **Results**: criteria and equation stated at the top, sortable columns, top three highlighted,
  ineligible rows dimmed with reasons, report / viewer / zip links, and a recorded selection
  written where the other modules look (`<project>/selected_variations.json`,
  `variations/selected/<id>_<title>.zip`).

## Hub side

Nothing module-specific: the module uses the generic bundled-module mechanism
(`source.bundled`), `server.requires` + `{server.steltic}` to find HR Steel, `{jobs_dir}` for
its state, and `credentials` to receive the connection. The Windows self-test now installs it
through the hub and starts its server (without HR Steel present, which is the harder case).

## Verified

* 13 module tests (`tests/test_variations.py`): library, metrics on a package in HR Steel's
  layout, whitelisted equations, every eligibility reason, re-weighting, the API flow with a
  patched HR Steel (plan → run → score → select → re-run only what is not done), stop.
* Live in the container: the module inside the hub, framed on its tab, run against the real HR
  Steel server in MOCK mode (the hub started HR Steel first and passed `STELTIC_URL`); and the
  full study against a stand-in HR Steel serving the Ex22 package with per-variation tonnage,
  including the NOT PERMITTED and failed paths, sorting, selection, and re-attaching to the
  event stream after a page reload. Playwright reported no page errors.
* Not verified here: a real LLM planning the list and reading the reports (no key in the
  container). The prompts ask for JSON with named keys and every LLM path has an offline
  fallback, so a bad answer degrades to the library / the estimate rather than to an error.

---

# Fourth pass: the Probabilistic analysis module, and a missing commit

## The self-test failure

`selftest-report.txt` showed `ignoring catalog entry steltic_variations.json: manifest missing
'name'` and 5 failed checks. The cause was not on the PC: the hub files that implement bundled
modules (`manifest.py`, `registry.py`, `runners.py`, `ui/app.js`) had been written and tested in
the working copy but never written back to `C:\Users\mikea\steltic_hub` -- only the module's own
files were. This pass writes them (a diff against the copies on the PC confirmed exactly those
four files differ), so the stub resolves, Design variations installs, and the self-test's step 11
runs.

## Probabilistic analysis

* **What it is**: Monte Carlo as-built variations of an HR Steel design -- E (one draw per
  building), plate thickness per section group, a story-by-story out-of-plumb profile in X and Y,
  optionally the dead load -- re-analysed with the design's own elastic LRFD model and checked
  against the design's own AISC 360 capacities, never recomputed. The DDM literature does this for
  GMNIA system capacity; this applies it to the member checks of standard practice, which is the
  novelty and is explained on the module's first screen. Fᵧ, residual stresses and member
  out-of-straightness are listed as deliberately not varied (capacity side).
* **Ratio basis**: D/Rₙ -- factored demand over NOMINAL capacity -- by default, as asked; the φ
  is taken out of the recorded φRₙ (0.90 flexure/compression/tension/interaction, 1.00 shear of
  rolled I-shapes, 0.75 bolts/welds/block shear/rupture/bearing, 0.90 panel zone/plate
  yielding/CJP, 0.65 concrete bearing), stated on the page, in the report and per row; the
  design's own D/φRₙ is a one-click alternative.
* **Analysis engine**: the worker runs under the Nonlinear module's interpreter (openseespy) with
  HR Steel's `steel_engine` on its path and patches, for one realisation at a time, `E`, `Ipack`
  (A, I ∝ t; J ∝ t³ per section group) and `ops.node` (the lean profile) before regenerating the
  ASCE 7-22 combinations, the period / ELF forces and the distributed static model's demand
  envelope exactly as `design_pipeline.design()` does. Realisation 0 is the nominal model; on
  Ex22 it reproduces the package's recorded group demands to the last digit, and a lean of H/250
  moves the roof by 4.13 in and raises the gravity-column moments by ~4.5 %, as it should. About
  6.5 s per realisation on the 558-member Ex22 (100 realisations ≈ 3 min on 4 workers).
* **Re-check**: per group, the AISC 360 check (flexure/shear, H1-1 interaction, brace axial) is
  recomputed with the recorded capacities when it reproduces the recorded D/C on realisation 0
  (11 of Ex22's 14 groups); otherwise the recorded ratio is scaled by the largest growth of its
  demand components and marked "scaled". Connections are scaled by the change in the demand that
  sizes them; capacity-designed demands (Mₚᵣ, Vₕ, Rᵧ) stay put.
* **Outputs**: two histograms (max member ratio, max connection ratio) with the design marked,
  lognormal/normal fits and KS tests; the statistical parameters; a per-group range chart and
  table; the governing check's movement; drift against the allowable; Spearman drivers; sortable
  realisation table; `report.html`, `results.csv`, `summary.json`, the SVGs. An invariant maximum
  (Ex22's governing floor beam is a determinate gravity check) is reported as such rather than
  fitted.
* **Hub side**: one generic addition -- `{python.<need>}`, the interpreter of a needed module's
  environment -- so the module borrows openseespy from Nonlinear instead of installing it twice.
  Nothing module-specific in the hub.
* **Verified**: 9 module tests (sampler, exact/scaled/basis re-check, connection mapping and φ,
  statistics and assessment, invariant case, the API flow with a stub worker, stop); 64 tests in
  all; live in the container against the real engine on Ex22 standalone (8 and 12 realisations,
  both bases, Playwright, no page errors) and inside the hub (bundled install, server start with
  `DDM_PYTHON`/`STELTIC_ENGINE_DIR` resolved, upload of the Ex22 package in the frame, a 6-run
  study, Files tab).

## Fifth pass -- feedback loops, site-specific hazard, the 16.1.4 document

* **Where the code lives**: the features belong to the modules, not the hub. `steltic_nonlinear`
  (branch `feedback-loops`, 0.2.0): `snl/feedback.py` (change-set builders), `snl/hr_client.py`,
  `snl/loop.py` (runner + promotion), `snl/loop_server.py` + `snl/loop_ui/` (the Feedback tab),
  `nlrha/site_hazard.py` (USGS design maps + disaggregation, conditional spectra, near-fault screen,
  user record libraries), `nlrha/design_criteria.py` + `nlrha/docx_writer.py` (the 16.1.4 draft),
  `nlrha/cli.py` (`hazard`, `library`, `criteria`, `--target`, `--records-set`, `--pulse-fraction`,
  `--sf-bounds`), `snl/cli.py` (`feedback`, pass-through, the criteria draft after every run).
  `steltic` (branch `feedback-loops`, 0.2.2): `preflight.py` / `consistency.py` (the
  `drift_relief_16_1_2` rules), `report.py` (Chapter 8 relief note, design-of-record row),
  `steltic/main.py` (`/api/restore?archive=1`, viewer whitelist), the agent contract.
* **Hub side**: only the Nonlinear manifest (server block, env vars, three tabs, Run-tab fields)
  and the docs. One fix outside the hub worth naming: `pushover.package_reader.locate` used to take
  the alphabetically first `model_opensees.py` under the folder, which after a loop run would have
  been `feedback/<id>/candidate/model_opensees.py`; it now prefers the folder named.
* **Verified**: 108 SNL tests (the three plans on Ex22, the loops end to end against a stand-in
  HR Steel server including user edits, pause / error / stop, promotion, the site hazard on canned
  USGS responses, the conditional spectrum, user libraries with PEER metadata and CSV pairs, the
  docx); 5 HR Steel tests; 41 hub tests. Live: `nlrha hazard` against the USGS services (Los
  Angeles; the design-maps call is instant, a disaggregation ~35 s), `nlrha scale --target cs`,
  a real mechanism loop with the re-push (202 s) and a real resize loop with `steltic_ddm --only
  1.4D` (87 s) against the stand-in server, the docx through LibreOffice, and the Feedback embed,
  Site hazard and Design criteria tabs inside the hub (Playwright, no page errors).

## Sixth pass -- the hub that restarted into its old self

* **The report**: "when I restart without first rebuilding, it opens an older version of the hub."
  Not a build problem -- the checkout is an editable install, so the code on disk was already the
  new one. The hub is single-instance and outlives its window: closing the Edge window leaves the
  hidden `pythonw` server running, `Steltic.bat` found it alive on the port and just raised a new
  window on it. Only the self-test, which kills whatever listens on its port first, ever started a
  fresh process -- hence "rebuilding fixes it".
* **The fix**: the hub records when it started (`main.STARTED`) and `/healthz` reports `pid`,
  `started` and `stale` (any source file -- package, UI, bundled catalog, `pyproject.toml` -- newer
  than the start; `__pycache__` ignored). `cli.py` runs uvicorn through a `Server` handle the app
  keeps, so `POST /api/hub/shutdown` stops it gracefully (lifespan: module servers, runs, URL
  marker) where a hidden process has no console to Ctrl-C, and `POST /api/hub/restart` spawns a
  detached successor (`--replace <pid>`, which waits for the port) before stopping. The launcher
  and the CLI replace a stale hub instead of deferring to it (`-Restart` / `--restart` force it;
  a hub too old to have the route is stopped by the pid on the port). The window shows a banner
  with a Restart button; the Modules page has the same button. A newer `pyproject.toml` re-runs
  `pip install -e` on launch.
* **One trap on the way**: the port probe was a `bind()`. For a minute after a hub exits its port
  is full of TIME_WAIT sockets and a plain bind fails on them -- exactly when the successor needs
  the port -- so the successor sat waiting and a third launch fell back to a random port. The probe
  is now a `connect()` (nobody listening = free); uvicorn binds with SO_REUSEADDR and never cared.
  The lifespan hook also retires `hub.url` only when it is its own -- the successor may already
  have written its URL there.
* **Verified**: 73 hub tests; live on Linux: relaunch defers to a fresh hub, replaces a stale one,
  `--restart` replaces a fresh one, the API restart hands the port over within a second, shutdown
  leaves no process and no marker; the banner + button in the window (Playwright: new pid, reload).
  Self-test step 6b does the in-place restart on Windows, where the detached `pythonw` spawn is the
  part that could not be exercised here.
