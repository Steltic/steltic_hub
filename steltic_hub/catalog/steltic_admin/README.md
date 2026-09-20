# Admin — a module that runs the other modules

Admin is the hub's counterpart of the **Admin** bot in `steltic_grokbot`: the one you talk to about
the whole setup rather than about one building. It ships inside the hub (`catalog/steltic_admin/`,
like Design variations and Probabilistic analysis), gets its own Python environment on **Install**,
and has no knowledge the hub does not already publish through `/api/state`.

It never imports hub or module code and never touches a module's environment. Everything it does
goes through the hub's own HTTP API — the same routes the window uses — so a run started by Admin
is exactly a run started by a click: same manifest, same staging, same log, same history line in
the project folder.

```
   Admin (its own server, started by the hub on demand)
     |  POST /api/run/<module>/<tab> ... one at a time, stream held open until `done`
     v
   steltic_hub  --->  HR Steel / CFS / Nonlinear / Query file manager / ...
```

## Three jobs

### 1. Batch — "J1 to hr then nl, then J2 to cfs, then J3 to hr"

Type that on the **Batch** tab and press **Make plan**. The words are read by a small grammar
(`admin/grammar.py`, no model needed): one project per clause, then the modules in order, `then`
/ `;` / a new line between clauses, and where the design brief comes from:

| you write | it means |
|---|---|
| `J1 to hr then nl` | HR Steel designs J1, then Nonlinear runs on J1 (its package field defaults to that design) |
| `J1 (ex22) to hr` | design J1 from HR Steel's example brief ex22 (through the tab's own example picker) |
| `J1 (brief.md) to hr` | design J1 from `brief.md` in J1's project folder |
| `J1 to hr` | same — Admin reads `brief.md` / `brief.txt` from the project folder |
| `J2 to cfs only` | CFS Steel designs J2 |
| `J4 continue: make the exterior columns W24x146 and rerun` | HR Steel's Continue tab on J4 with that follow-up |
| `J5 cfs continue: …` | CFS's Continue tab |
| `Standards convert, index, audit` | Query file manager tabs (the Standards tab builds these for you) |

Words like *run*, *then*, *when all done*, *only*, *please* are ignored; anything Admin does not
recognise is listed as a warning, never guessed at. The full alias table is under *words Admin
understands* on the tab. With an LLM connection set, **Make plan with the model** hands the
instruction, the hub's module/tab/field listing and the alias table to the model instead, for
wording the grammar does not cover; the result is the same kind of plan.

A plan is a list of steps — `(project, module, tab, fields)` — shown as a table and editable as
JSON. **Check** validates it against what the hub says exists right now: module installed, its
`needs` installed, the tab runnable, its optional components present (the converter gate), required
fields filled, brief file present, LLM connection set for the design modules. **Save**, then
**Start**.

While it runs: one step at a time, in a worker thread inside Admin's server; each step's event stream
(the design agents' tokens, tool calls and milestones; a CLI run's stdout; the hub's retries) is
written to `admin/logs/<plan>/<n>.log`, the step's status, run id, attempts, exit code and
artifacts to `admin/plans/<plan>.json`. The tab polls and shows the current step's log. **Stop**
cancels the current run through the hub (`POST /api/cancel`) and ends the plan after it; **Resume**
runs the steps that are still pending (and, with *retry failed steps*, the failed ones again).

### A step is not one run

A design that was stopped, that died when the model server went away, or that paused itself has
hours of work saved in the project, and the module says which tab picks that up: `run.continues`
in its manifest (HR Steel's and CFS's *Continue* resume a *Design* from `conversation.json`, and
pressing it with no fields is a plain resume). Admin uses it, so a step is continued rather than
started over:

| what ended the run | what Admin does |
|---|---|
| **Stop**, an Admin restart | the step is `stopped`; **Resume** continues it from where it stopped (*start them over* ignores the saved progress) |
| the model server or the hub unreachable, a timeout, a 5xx / 429, the module's own "LLM call failed" after its retries | **waits for the server** — checks the hub and `GET <base_url>/models` every 30 s, as long as it takes (option `wait_for_llm`, on by default; Stop ends the wait) — then continues; a step whose module has no continuing tab is run again from the start; between attempts at least 30 s, then 60, 120, 300 |
| the module paused (loop guard, empty turn, no progress), or an error a Continue may heal (a provider 400 on a turn HR Steel now repairs, a tool crash) | continues by itself, `auto_continue` times (default 3), then the step is `failed` |
| an error nothing will heal (call budget reached, bad key or model, the hub refused the run) | `failed`; the step's `on_fail` decides what the plan does |
| the module answers a continue with "nothing to resume" (it died before its first save) | the step is started over, once |

The step row shows `↻n` (continued n times) and `⏳n` (waited n times), and `via continue` when the
last run was the continuing tab. Both options live in the plan's JSON (`options`).

Failure policy per step (`on_fail`): `stop` (default — a failed HR design makes the Nonlinear step
meaningless), `skip_project` (skip the rest of that project's steps, carry on with other projects),
`continue` (the standards queue uses this: one bad PDF must not block the others).

A plan left `running` when Admin's server goes away (a hub restart takes every module server with
it) is marked **interrupted** when Admin comes back; it is not restarted on its own, because the
run it was watching may still be going on the module server. Check the project, then Resume.

### 2. Standards — the conversion queue

Converting a specification through the Query file manager's converter takes hours, and a site needs
five to nine of them. The **Standards** tab scans a folder (default: the Query file manager's own
`grokbot/documents/standards`), guesses each PDF's canonical stem from its file name (`AISC_360_22`,
`ASCE7`, `AISI_S400_20` … — correct any it got wrong; the skills' retrieval ids depend on them),
marks the ones already converted (`grokbot/markdown/<stem>*.md`), and **Queue the conversions**
turns the ticked rows into a plan: one `convert` step per PDF (`on_fail: continue`), then `index`,
then `audit`. It is an ordinary plan, so it runs, logs, stops and resumes like any other, and the
hub's own converter behaviour applies unchanged — the native-crash retries, the resume from the last
finished chunk, the refusal until the converter component is installed (Admin says so before you
queue anything).

### 3. Help — "how does Steltic work?"

The **Help** tab answers questions from the code and docs: the hub's own source (the folder the hub
reports on `/healthz`), every module checkout the hub holds under `modules/` (or the working copy
it is linked to) — READMEs, `docs/`, `contract/`, `skills/`, `prompts/`, and the `.py` files — and,
if you choose *this PC + GitHub* or *GitHub only*, the public repos the manifests name
(`raw.githubusercontent.com`, cached under `admin/github/` for a day; docs always, code files only
when the question names something in their path).

Retrieval is plain term matching over 60-line chunks, no embeddings, nothing leaves the machine
except the GitHub fetches you asked for. With an LLM connection the ten best passages go to the model
with the instruction to answer from them only and cite `label lines a-b`; the passages are shown
under the answer. Without a model (or with MOCK) the passages themselves are the answer. Questions
and answers are kept in `admin/help_log.jsonl` and listed under the box.

## Where things are

Everything under `<data folder>/admin/` (the hub's Files tab for Admin lists it):

```
admin/plans/<id>.json      the plan and its progress
admin/logs/<id>/<n>.log    one log per step
admin/help_log.jsonl       questions and answers
admin/github/              cached GitHub fetches
```

## Running it

Modules page → **Admin** → Install (fastapi + uvicorn + httpx into its own environment, a few
seconds). Opening any Admin tab starts its server. The hub passes it `HUB_URL` (`{hub_url}`, a
template added for this: the URL the hub actually bound), `HUB_DATA`, `HUB_JOBS`, `HUB_CATALOG`
and `ADMIN_DATA`, and pushes the LLM connection to `/api/creds` like any other module that
declares `credentials`.

```
python -m pytest tests/test_admin.py -q      # 15 tests: grammar, standards, executor against a fake hub, help, routes
```

The executor test runs a fake hub that speaks the real `/api/run` event stream, so the whole path —
validate, resolve `@project` / `@example`, run, log, `on_fail`, stop, resume, interrupted — is
exercised without installing a module.

## Not in this version

* **Two runs at once.** One step at a time, one plan at a time, on purpose: the design servers hold
  one conversation per building, OpenSees is a process singleton, and the converter eats the machine.
  A `concurrency` setting could be added to the plan later; the executor is a single thread now.
* **Variations / Probabilistic.** Their work is driven through their own servers' APIs, not the hub's
  `/api/run`, so a plan cannot include them yet (the grammar has no alias for them). Driving them
  means Admin calling their `/api/project/<p>/…` routes directly — a later step.
* **Runs that outlive the hub.** The hub kills a run when the client that started it disconnects and
  a hub restart stops every module server. Admin keeps its stream open for the whole run, so a
  browser reload or a closed window does not stop a batch — but **Restart hub** does. Do not restart
  the hub mid-batch; Admin marks the plan interrupted if that happens.
* **Per-step field help in the table.** Fields are edited as JSON; the hub's own tab shows what each
  one means.
