# Steltic Hub

One window over every Steltic module. The hub installs each module from GitHub into its own
Python environment, renders its input tabs from a manifest, runs it, and frames its viewers —
so `steltic_viewer_bundle.html` stops being a results-only page and becomes the app.

```
   Tauri window / Edge --app / any browser          <- swappable, ~0 logic
                    |
          steltic_hub (FastAPI, 127.0.0.1)          <- registry, venvs, jobs, proxy, UI
         /          |           |          \
   steltic     steltic_cfs   nonlinear    grokbot   <- one git checkout + one venv each
   (server)     (server)       (CLI)     (CLI + the
      ^ \__________|_____________________ grounding server: the design agents'
      |                                     standards search, answered from
   variations  <- bundled in the hub:       the Query file manager corpus)
   (server)       plans N variations, has HR Steel design each, scores and ranks
      ^
   admin       <- bundled in the hub: batch plans across projects and modules through
   (server)       the hub's own /api/run, the standards conversion queue, and help
```

## Why this shape

**The shell is the least important decision, so it is made last.** Everything the app does lives
in a local FastAPI server. The desktop shell only opens a window at its URL. That is why
`Steltic.bat` (a chromeless Edge window) and the Tauri build in `tauri/` are interchangeable, and
why a plain browser tab is a valid third option with zero packaging work.

**One venv per module is forced, not preferred.** `steltic` and `steltic_cfs` both install
top-level packages named `steltic`, `steel_engine`, `contract`, `frontend` and `test_buildings`,
with *different* code in each. In a shared environment the second install silently overwrites the
first and you get a CFS engine answering hot-rolled requests, producing a plausible-looking and
wrong report. Separate environments also let openseespy stay pinned to CPython 3.10–3.12 while
the hub itself runs on anything, and let grokbot hold Docling at 2.123.1 without fighting anyone
else's resolver.

**The hub contains no module-specific code.** No `if module == "steltic"` anywhere. Everything —
tabs, fields, commands, viewers, where outputs land — comes from a manifest. Adding a module is
publishing a repo; updating one is `git pull` plus a reinstall into its own venv.

## Install and run

```powershell
git clone https://github.com/Steltic/steltic_hub
cd steltic_hub
windows\Steltic.bat            # first run fetches uv + Python + the hub, then opens the window
```

Nothing needs to be installed first — no Python, no uv, no Docker. First run pulls about 20 MB;
each module you install from the Modules tab pulls its own dependencies (openseespy-based modules
are ~400 MB each).

Cross-platform / developer run:

```bash
pip install -e .
steltic-hub                    # http://127.0.0.1:8300
steltic-hub --bootstrap        # headless: provision every module, then exit
steltic-hub --restart          # stop the hub already on the port and start this one in its place
```

### After a git pull or an edit: the hub restarts itself

The hub is single-instance and outlives its window: closing the Edge window leaves the hidden
`pythonw` server running, and a relaunch normally just raises a new window on it. After a `git pull`
or an edit in a checkout that running process is the *old* code. So the hub knows when it started
and reports on `/healthz` whether any of its source files (package, UI, bundled catalog,
`pyproject.toml`) is newer than that — `stale: true` — together with its `pid`:

- `Steltic.bat` (and `steltic-hub` from a terminal) checks the running hub first. Fresh → raise the
  window as before. Stale, or launched with `-Restart` / `--restart` → ask it to stop
  (`POST /api/hub/shutdown`, graceful: module servers and runs go with it) and start a new one.
- The window shows a banner (“the code on disk changed since this hub started”) with a **Restart
  hub** button; the Modules page has the same button next to the version. That is
  `POST /api/hub/restart`: the hub spawns its successor (`steltic-hub --replace <pid>`, which
  waits for the port), stops, and the page reloads when a different pid answers.
- A checkout whose `pyproject.toml` is newer than the last install gets `pip install -e .` again on
  launch (a new dependency is the one thing an editable install does not pick up by itself).
  `-Reinstall` still rebuilds the environment from scratch.

The self-test exercises the in-place restart (step 6b).

### Only the hub's own page may drive it

The hub binds to 127.0.0.1 with no authentication, which keeps other machines out but not other web
pages: a browser sends a "simple" cross-site request (a POST with no body, or a `text/plain` one)
without any CORS preflight, so a page on any site you had open could have set your LLM connection to
its own endpoint, registered a module from its own git URL and installed it. Every state-changing
request is therefore refused unless the browser says it came from a loopback origin
(`Origin` / `Sec-Fetch-Site`), and every request must carry a loopback `Host` (DNS rebinding). The
Edge window, the Tauri shell, a browser tab on the hub's URL, the launcher and module servers are
all unaffected.

### Windows 11 Smart App Control

Smart App Control refuses to load executables and DLLs that are neither code-signed nor known in
Microsoft's reputation graph. PyTorch (the PDF converter), OpenSees (the design engines) and
onnxruntime are unsigned, so with it **on** they fail at import with
`OSError: [WinError 4551] An Application Control policy has blocked this file`. A fresh Windows 11
runs it in *evaluation* mode, where nothing is blocked, and switches itself on — typically at a
reboot — so a converter that worked yesterday fails today. There is no per-file allow-list; the only
settings are On and Off, and Off is one-way (it cannot be re-enabled without resetting Windows).

The hub does not change the setting. It reports it (`steltic-hub doctor`, `/api/state`, a notice at
the top of the Modules page when it is on or in evaluation) and, when a run's output shows the 4551
block, adds one line saying what happened and where the switch is: Windows Security → App & browser
control → Smart App Control settings → Off. The hub itself, Query, and the MOCK design model run
with it on.

## Testing locally before you push

Nothing needs to be committed to be exercised end to end. A module can be pointed at a working
copy on disk; the hub then uses that directory as the checkout, installs it with
`pip install -e .`, and never fetches, resets or writes to it.

### The fast loop (seconds, no OpenSees)

`windows\Run-SelfTest.bat` writes a throwaway smoke module (one manifest, one stdlib script) into
`selftest-data\`, links it, runs it through the hub and checks the stream, the artifacts, the
failure path and the traversal guard. Nothing outside `selftest-data\` is touched, and the product
ships no example module.

### The real loop (your own module changes)

```powershell
$env:STELTIC_HUB_DATA = "$PWD\.devdata"
steltic-hub link steltic C:\code\steltic       # your working copy, uncommitted changes and all
steltic-hub install steltic
steltic-hub
```

Or from inside the app: **Modules → Use local copy…**. A linked module shows `linked` and, when
your working tree is dirty, `uncommitted`.

| you changed | what to do |
|---|---|
| module Python code | nothing — `-e .` means the next run picks it up |
| the module's `steltic_module.json` | nothing — a linked checkout's manifest is re-read per request |
| the module's dependencies | **Update** (reinstalls in place; never pulls, never touches your git state) |
| the hub's own `catalog/*.json` | **Reload manifests** |
| hub Python code | restart `steltic-hub` |
| hub UI (`ui/*.js`, `ui/*.css`) | hard-refresh the window (`Ctrl+Shift+R`) |

`Remove` on a linked module deletes only its environment. Your working copy is never touched.

The Modules tab keeps the last install/update log of every module on screen, and a module with a
run in progress refuses Update / Rebuild / Remove until the run is stopped.

### Checking the install

```bash
steltic-hub doctor
```

Prints the data dir, whether uv and git are present, the port, and per module: its source
(linked or git), checkout, which manifest won, the interpreter it actually got, and whether
openseespy imports. That last one is the check the Steltic README asks users to do by hand —
if a module landed on 3.13, `doctor` says so instead of you finding out twenty minutes into a run.

```bash
python -m pytest tests -q
```

No installs needed for the hub's own tests. Includes one test that parses the hub's own AST to prove no code
branches on a module id, and one that asserts `steltic` and `steltic_cfs` still get separate environments.
`tests/test_probabilistic.py` (the bundled Probabilistic-analysis module) needs `numpy` and is skipped without it
(`pip install pytest numpy`); the Windows self-test installs both.

### Keeping it away from your real data

Everything — checkouts, environments, projects, state — lives under one directory. Set
`STELTIC_HUB_DATA` to a scratch path and your installed Steltic setup is untouched; delete the
directory to reset completely. The launcher, the Tauri shell and a bare `steltic-hub` all default
to `%LOCALAPPDATA%\Steltic` (`~/.local/share/Steltic` on Linux, `~/Library/Application
Support/Steltic` on macOS) when the variable is unset. The hub writes the URL it actually bound
to `hub.url` in that folder, which is how the launcher finds it when port 8300 is taken.

## The module manifest

A module describes itself in `steltic_module.json` at its repo root. The hub also ships a catalog
of manifests for the four existing repos, so none of them has to change — but a manifest in the
checkout always wins, which is how a module ships new tabs without a hub release.

```json
{
  "schema": 1,
  "id": "my_module",
  "name": "My Module",
  "accent": "#cfe3ff",
  "source": { "url": "https://github.com/me/my_module", "branch": "main" },
  "env": { "python": "3.12", "install": ["-e", "."] },
  "needs": ["steltic"],
  "env_vars": { "STELTIC_ENGINE_DIR": "{need.steltic}/steel_engine" },
  "output": { "root": "{job_dir}" },
  "tabs": [{
    "id": "run", "title": "Run", "kind": "form",
    "run": { "kind": "cli", "cwd": "{job_dir}",
             "stage": [{ "from": "{f.input}", "to": "{job_dir}", "required": true }],
             "command": ["-m", "my_module", "run", "{job_dir}"] },
    "fields": [
      { "id": "job",   "type": "project", "label": "Project", "required": true },
      { "id": "input", "type": "file",    "label": "Input",   "accept": ".zip",
        "default": "{out.steltic}", "placeholder": "this project's HR Steel design" },
      { "id": "tol",   "type": "number",  "label": "Tolerance", "arg": "--tol", "default": 0.1 }
    ],
    "artifacts": [{ "label": "Report", "path": "report.html" }]
  }]
}
```

Paste that into **Modules → Add a module** and it is installable. No hub release, no code change.

| tab `kind` | what it does |
|---|---|
| `form` | hub renders `fields` and drives `run` — this is what adds inputs to a results-only module |
| `embed` | frames the module's own UI, served by its own server (the hub starts it on demand); stays current on every update |
| `viewers` | the viewer-bundle chip strip over a project folder, greying what is not produced yet |
| `files` | browse and open everything the module wrote for a project |

| `run.kind` | behaviour |
|---|---|
| `cli` | spawns `<module venv python> command…` in the job folder, streams stdout as SSE; Stop kills the process tree |
| `http` | calls the module's own API and relays its event stream; `run.cancel` (`{method, path, body}`) is what Stop calls |

A CLI run's stdout is log lines — except a line that is one JSON object with a `type` from the
design agents' vocabulary (`token`, `reasoning`, `tool`, `tool_result`, `milestone`, `status`,
`usage`, `warning`, `assistant`, `error`, `paused`, `artifact`), which the hub relays as that event.
That is how a process that talks to the model gets the same streamed model-output line, separate
model-reasoning box, activity lights and usage strip the agent servers get, with no server of its
own. A CLI run that does talk to the model says so with `"llm": true`: the hub then refuses to
start it without a connection and hands the connection to the process as `STELTIC_LLM_BASE_URL`,
`STELTIC_LLM_API_KEY`, `STELTIC_LLM_MODEL`, `STELTIC_LLM_PROVIDER`, `STELTIC_LLM_REASONING` and
`STELTIC_LLM_MAX_TOKENS` — the key reaches that process's environment and nothing else. A CLI run
whose command or `run.env` names `{server.<module_id>}` has that module's server started first
and its address substituted (the Nonlinear module's Review tab reaches the standards server this
way); a module that is not installed leaves the variable unset.

`run.stage` copies inputs into place before the command runs: each `{from, to}` entry may name a
folder (its contents are copied), a `.zip` (unpacked, a single wrapping folder is flattened) or a
file. An empty or missing `from` is skipped unless the entry is `required`, in which case the run
refuses with `missing` as the message. This is how the Nonlinear module picks up the HR Steel
design of the active project without either module knowing about the other.

Templates available in commands, bodies, env vars, stage entries and field defaults: `{job}`,
`{job_dir}`, `{jobs_dir}`, `{module_dir}`, `{data_dir}`, `{catalog_dir}`, `{port}`,
`{need.<module_id>}`, `{python.<module_id>}` (the interpreter of a needed module's environment,
for a module that runs its jobs inside that environment instead of installing openseespy twice),
`{out.<module_id>}` (that module's output folder for the active project), `{server.<module_id>}`
(the address of another module's server, which the hub starts first — for a module server when
`server.requires` names it, for a CLI run when its command or `run.env` uses it), `{hub_url}` (the URL the hub itself answers on, for a module whose server drives other modules
through the hub's own API — Admin is the one that does) and `{f.<field_id>}`.
Values are substituted into an argv list and never handed to a shell; a field value is never
expanded a second time.

Field types: `text`, `textarea`, `number`, `select`, `checkbox`, `file`, `files`, `project`,
`attachments`. A field with `arg` becomes a CLI flag (`arg_style`: `flag`, `positional`,
`flag-if-true`). `file` values are uploads into the project folder (or a file already there) and
reach the command as absolute paths. A `select` with `fills` (`{path, key, target}`) fetches
`path` from the module server when a value is picked and drops `key` of the reply into the
`target` field — that is how the example-brief pickers work. `attachments` reads `.txt`/`.md`/`.pdf`
files into `text_target` in the browser and sends images along as `[{name, type, data_url}]`.
A tab's `links` (`{label, path}`) are opened on the module server, e.g. a download endpoint.

`env.optional` declares components the module can do without (`{group: {label, requirements, probe_import, help}}`)
— the Query file manager's PDF converter is Docling, a large ML stack — and the Modules page offers an
*Install <label>* button per group. `probe_import` is one import name or a list; the group counts as present only
when every name imports (Docling imports without `onnxruntime` and then fails on the first OCR page, so the converter
group installs `docling[rapidocr]` and probes `docling`, `rapidocr` and `onnxruntime`). A tab that cannot run without
a group lists it in `requires_optional`; the hub then refuses the run with the install path instead of letting the
script die on an ImportError. A run that dies in native code — a bare exit such as 3221225477 (0xC0000005,
access violation) with no traceback — gets one line saying what the code means, plus the tab's own next step
from `run.crash_hint` when the manifest gives one (the PDF converter's: run again, it resumes from the last
finished chunk).

A tab whose run says `"continues": "<tab id>"` picks up an interrupted run of that tab (same
module, same `run.kind`) from the state that run saved in the project — HR Steel's and CFS's
*Continue* resume a *Design* from `conversation.json`, and pressing it with no fields is a plain
resume. The hub does nothing with the key beyond validating and publishing it; it is for whatever
drives runs on the user's behalf. Admin's batch reads it: a step stopped, timed out or paused is
continued through that tab instead of started over (see *Admin*).

Project names are reduced to `[A-Za-z0-9_-]` on purpose: that is exactly the set the design
servers keep, so a module never writes to a folder the hub is not looking in.

## Projects

One folder per building, shared across modules. A design lands there, the nonlinear packages land
beside it, and the viewer strip lights up as each appears — the layout
`steltic_viewer_bundle.html` already probes for. Modules that keep their own data layout (steltic
keeps jobs under its `DATA_DIR`) declare `output.root` and the hub serves from there instead.

## The LLM connection

Typed once in the title bar and saved on this PC (`connection.json` in the data folder), so it
is there the next time the app opens. It is pushed to each module server that declares
`credentials` when it starts (and again before every run, so a restarted server never runs
without it), and goes nowhere else than to the provider you named. **Forget** in the dialog
deletes it. Set the model to `MOCK` to drive the whole pipeline offline.

## Spec grounding

The design agents ground their clauses through one small HTTP API (`RAG_API_URL`, see
`rag_v2/README.md` in their repos) that was written for a hosted vector database. The hub answers
that API locally instead: the **Query file manager** module ships `rag_server.py`
(`catalog/steltic_grokbot/`), which serves the agents' `POST /query` from the full-text and
exact-id index over the licensed PDFs you converted, plus the OpenSees documentation and worked
examples that come with the module. No embeddings, no vector store, nothing leaves the machine.

HR Steel and CFS declare `server.requires: ["steltic_grokbot"]` and
`RAG_API_URL: "{server.steltic_grokbot}/query"`: when they start, the hub starts the grounding
server first and passes its address. The Nonlinear module's **Review** tab does the same for one
CLI run (`run.env.RAG_API_URL`), so its model grounds the clauses it cites without the module
keeping a server up for it. With Query file manager not installed the variable is left
unset and the agents run the way their repos do without a RAG (clauses from memory, flagged for
verification); install it later and the design servers restart with it on their next run. The
module's **Grounding** tab shows what the agents asked and what they got back. Until you convert
your own specification PDFs, only the OpenSees and worked-example collections answer.

## Design variations

A bundled module (`catalog/steltic_variations/`, no clone; its venv is fastapi + uvicorn + httpx)
that turns one brief into a design-variation study:

1. **Base brief** — paste it, or load the one HR Steel already designed for this project (read
   back from the package's `conversation.json`).
2. **Variations** — the number, and one of three ways to define them: tick any of ten categories
   (establish the problem, core configuration, lateral system, perimeter frames, outriggers,
   geometry and mass, members and materials, bases, seismic design choices, devices) and the
   model spreads the variations across them; describe the study in words and the model turns it
   into a list; or let the model propose the whole list. Every variation is `title / change /
   why`, editable in place; M001 is always the base unchanged. Without an LLM connection the
   built-in template library (87 generic variations) is used instead of the model.
3. **Design** — each variation goes to HR Steel as its own building (`<project>_M0xx`) with the
   base brief plus a "change ONLY this" block; the package is downloaded when it finishes and
   its metrics are read (`design/member_schedule.csv` → tonnage, `report.html` → drifts, base
   shear, period, `design/calc_package.json` → D/C, ρ, Ax, torsion; the model reads the report
   for what the package does not state, such as the moment-connection count). Runs are
   background threads — closing the tab does not stop a study.
4. **Score** — the eligibility criteria (status DONE, every check passing, drift utilisation
   ≤ 1.0, SMF share ≥ 25 % for dual systems, ρ and Ax applied, no Type 1b, wind comfort ≤ 15 mg,
   representable in the nonlinear tools) with editable thresholds; the default score equation
   with a Copy button, and a box where you paste / modify it, write your own `S = …` over any
   of the 27 metrics, or describe what matters in words for the model to write the equation
   (shown back before it is used). Equations are evaluated by a whitelisted AST walker, never
   `eval`.
5. **Results** — the criteria and equation stated at the top, a table whose column headers sort
   (max → min, click again to flip), top three highlighted, ineligible rows dimmed with the
   reason, links to each variation's report / viewer / package, and **Record selection**: the
   chosen variations are written to `<project>/variations/selection.json` and
   `<project>/selected_variations.json`, their packages copied to `variations/selected/`, so
   the Nonlinear module's "in project ▾" picker (and any other module) can take them up.

`variations/results.csv` and `results.json` hold the full table. The module talks to HR Steel
only through its HTTP API (`server.requires: ["steltic"]`, `STELTIC_URL`), so it needs HR Steel
installed but never touches its data folder.

## Probabilistic analysis

A bundled module (`catalog/steltic_probabilistic/`) that asks a question standard practice never
asks: **if this building is built with real-world imperfections, does it still satisfy the design
code it was designed to?** The idea comes from the Direct Design Method literature (Rasmussen and
co-workers; the SSRC / AISC advanced-analysis groups), where Monte Carlo samples of material,
geometry and imperfection variables are used to find the distribution of a frame's *system
capacity* for reliability calibration. Here the same sampling is applied to the design's own
**elastic LRFD model** instead:

1. **Model** — a completed HR Steel package (this project's design, a zip in the project such as a
   Design variations finalist, or an upload). Its `cfg.py` is the analysis model; its
   `calc_package.json` holds the capacities and D/C the design was checked with.
2. **Run** — the number of as-built variations (default 100) and the random variables, each with
   its default distribution and source: E (Galambos & Ravindra 1978), plate thickness per section
   group (fabrication factor, same source), a story-by-story out-of-plumb profile in X and Y
   (Beaulieu & Adams; Lindner & Gietzelt; σ = 1/1000 so 2σ is the H/500 erection tolerance), and
   optionally the dead load (Ellingwood et al. 1980; off by default). Fᵧ, residual stresses and
   member out-of-straightness are deliberately *not* varied: they live on the capacity side, and
   the capacities are held at the design values. Each realisation is one full LRFD analysis of
   the perturbed model — every ASCE 7-22 combination with P-Δ, the ELF forces from its own period
   — run by HR Steel's engine inside the Nonlinear module's environment (`{python.steltic_nonlinear}`,
   a new generic hub template), a few seconds each; workers run in the background.
3. **Results** — the ratio is **D/Rₙ: the factored demand over the nominal capacity**, the
   resistance factor φ taken out of the design's recorded φRₙ (φ values stated on the page and in
   the report; the design's own D/φRₙ is one click away). Where the package records the capacities
   a check used, the AISC 360 check is recomputed exactly; where it does not, the recorded ratio is
   scaled by the largest growth of its demand components (marked, conservative). Outputs: the
   distribution of the maximum member ratio in the building and of the maximum connection ratio,
   each with the design's value marked, fitted lognormal / normal curves and KS tests, the
   statistical parameters, the design story drift against the allowable, where the governing check
   moves to, a per-group range chart and table (where to take a second look), a Spearman
   rank-correlation table of what drives the scatter, a sortable realisation table, and
   `report.html` / `results.csv` / `summary.json` in `<project>/probabilistic/`. A ratio above 1.0
   is a place to look, not a code requirement to act — the code is satisfied on the nominal model.

## Admin

A bundled module (`catalog/steltic_admin/`; fastapi + uvicorn + httpx in its own venv) that runs
the other modules for you, the way the **Admin** bot in `steltic_grokbot` sets up and directs the
other bots. It knows nothing the hub does not publish on `/api/state`, and every run it starts goes
through `POST /api/run/…` exactly as a click on that tab would.

* **Batch** — `J1 to hr then nl; then J2 to cfs; then J3 (ex22) to hr` becomes a plan of
  `(project, module, tab, fields)` steps, checked against what is installed, then run one after
  another with every run's stream logged to `admin/logs/`. Stop, Resume, a failure policy per
  step, and a plan interrupted by a hub restart is marked so rather than restarted blind. A step
  is not one run: a design that was stopped, timed out or paused is continued from what its module
  saved (the tab the manifest marks `continues`), a model-server outage is waited out for as long
  as it takes rather than failing the step, and a pause is continued a few times by itself.
* **Standards** — a folder of licensed specification PDFs becomes a queue of Query file manager
  conversions (one `convert` per PDF with its canonical stem, then `index`, then `audit`): the
  24-hour setup job, unattended.
* **Help** — questions about how Steltic works, answered from the hub source, the module checkouts
  on this PC and (on request) the public GitHub repos, with the passages it used.

`catalog/steltic_admin/README.md` has the details; `tests/test_admin.py` runs the whole path
against a fake hub.

## Nonlinear (SNL): Feedback, Site hazard, Design criteria

The Nonlinear module's manifest (`catalog/steltic_nonlinear.json`) now declares a **server** —
`snl.loop_server:app`, started by the hub with `STELTIC_URL = {server.steltic}`,
`SNL_JOBS = {jobs_dir}` and the engine dir — and three new tabs, all served by the module's own
repository (`steltic_nonlinear` 0.2, branch `feedback-loops`):

* **Feedback** (embed) — once the Chapter 16 run is complete: what the analyses measured, three
  loops back to HR Steel (design drift to the measured response under ASCE 7-22 §16.1.2, resize by
  system role, mechanism shaping through SCWB and panel zones), each with a reviewed change set and
  the exact brief HR Steel's design agent receives through its *Continue* path; the re-design
  streams live; the Nonlinear module re-verifies the candidate with the same analyses (Chapter 16
  suite, DDM `--only` on the governing combination, the re-push); one button makes a verified
  candidate the **design of record** — HR Steel's job is replaced (the previous design archived as
  `<project>__<timestamp>` by `POST /api/restore/<job>?archive=1`), the hub project's package and
  analyses move to `archive/<timestamp>/`, `design/design_of_record.json` is written. Everything a
  loop did lives in `<project>/feedback/<loop id>/`.
* **Site hazard** (form) — `nlrha hazard`: the USGS ASCE 7-22 multi-period MCE_R spectrum and the
  USGS NSHM disaggregation for the site, conditional spectra, a near-fault screen; then the Run
  tab's *Scaling target* (site-specific MCE_R or conditional spectrum), *Record library folder(s)*
  (PEER `.AT2` or CSV pairs indexed on the fly, ranked for M / R consistency), *Pulse-type share*.
* **Design criteria** (form) — `nlrha criteria`: the §16.1.4 design criteria draft (.docx + .html),
  also written at the end of every Run.
* **Review** (form, `run.llm`) — `snl review`: the model reads what the run measured (the Chapter 16
  acceptance, the pushover mechanism, the DDM check, the §16.1.4 draft), looks the governing clauses
  up in the Query file manager's corpus through `RAG_API_URL`, and writes `review.md` / `review.html`
  in the project folder — what passed, what is marginal, what to change and why, each clause cited
  from the passage it read (`review_transcript.json` keeps them). It streams as the design agents
  do: model text on the run line, the model's reasoning in its own box, one line per standards
  search. Needs `steltic_nonlinear` 0.3 (the `review` command) and the LLM connection.

The module's install spec is now `-e .[hub]` (fastapi/uvicorn for the tab server): a hub that
already holds a Nonlinear environment needs **Update** on the Modules page once. HR Steel
(`steltic` 0.2.2, branch `feedback-loops`) carries the matching pieces: the `drift_relief_16_1_2`
cfg key and its preflight / consistency rules, the restore archive flag, the design-of-record row
in the report, and the agent contract for the three briefs.

## Names

A module's name comes from its manifest, and you can rename it from the Modules tab (✎ next to
the name) — the display name is yours, kept in `state.json`, and the manifest is untouched.

## Runs

A run's log stays with its tab: switch to Viewers or another module while a 90-minute NLRHA is
going and the stream is still there when you come back. The design agents' events render the way
their own UI renders them — streamed model text on one line, tool calls and results, milestones,
token usage, a separate model-reasoning box — and **Stop** asks the module server to halt (a CLI
run's process tree is killed). A server that acknowledges the stop but keeps streaming gets its
stream dropped by the hub after `STELTIC_HUB_STOP_GRACE` seconds (8 by default), which every
module server treats as a stop — so Stop ends the run either way, and the tab says *stopped*.
Both the hub and the module servers send a keepalive comment every 20 s of silence, so a long
quiet stretch is never mistaken for a dead connection.

Each module server keeps the port it was first given (`ports.json` in the data folder) and no port
is ever handed to a second module: every module page loads `/static/app.js` by the same path, and
the browser caches per origin, so a port that changed hands would serve one module's script inside
another's page.

## Shell options, and what each costs

Windows, free/OSS distribution, runtime bootstrapped on first run.

| Option | Installer | Build toolchain | Notes |
|---|---|---|---|
| **Edge `--app=` window** (shipped) | ~30 KB of script | none | WebView2 is already on every supported Windows build — the same engine Tauri uses. Zero packaging. No Add/Remove Programs entry, no auto-update. |
| **Tauri v2** (scaffolded in `tauri/`) | 6–10 MB | Rust + MSVC build tools | Real NSIS installer, updater plugin, proper app identity. WebView2 means the three.js viewers behave exactly as in the Edge window. |
| **Electron** | ~150 MB before any Python | Node | Mature `electron-builder` + `electron-updater`. The 150 MB is hard to justify when the shell has ~60 lines of logic and the Python payload dwarfs it anyway. |
| **Browser tab** | none | none | `steltic-hub` in a terminal, open the URL. Always works; ships today. |
| **pywebview + PyInstaller** | 15–40 MB | Python only | One language end to end, but freezing openseespy's native binaries is its own long fight. Not recommended here. |

Recurring costs, Windows-only, free download:

| Item | Cost | Needed? |
|---|---|---|
| GitHub Releases hosting | $0 | — |
| Code signing — Azure Trusted Signing | ~$10/month | Strongly recommended. Without it SmartScreen warns on every download until reputation builds. |
| Code signing — OV certificate | $150–300/year | Alternative; needs an HSM/hardware token since 2023. |
| Code signing — EV certificate | $400+/year | Not worth it. Microsoft removed EV's instant SmartScreen bypass in 2024, so it behaves like OV now. |
| Apple Developer Program | $99/year | Only if macOS is added later. |

So: **$0 to ship, ~$120/year to ship without a SmartScreen warning.** Note that Azure Trusted
Signing validates individual developers in the USA and Canada only; organizations are also
supported in the EU and UK.

The shell choice is reversible at any time because no product logic lives in it.

## Layout

```
steltic_hub/
  manifest.py    the module contract          registry.py  git checkout + manifest resolution
  envs.py        per-module venvs via uv      runners.py   server supervision + run execution
  jobs.py        shared project folders       main.py      FastAPI app
  catalog/       manifests for the four existing repos, plus assets a manifest needs
                 (steltic_grokbot/rag_server.py: the grounding server) and the bundled
                 Design variations (steltic_variations/), Probabilistic analysis
                 (steltic_probabilistic/) and Admin (steltic_admin/) modules
  ui/            the shell (index.html, app.js, styles.css)
windows/         first-run bootstrap + launcher
tauri/           optional native shell
```

## License

MIT.
