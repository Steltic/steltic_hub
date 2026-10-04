# Review fixes — Steltic Hub (steltic_hub)

October 2026. These changes fix the engine, gate, report, contract and hub defects found in the October 2026 review of the Steltic repos (30 test buildings, 6 nonlinear runs and a static review). Every Critical and High item was re-checked by an independent pass against the 2022 editions and hand calculations.

**Out of scope, deferred to a separate RAG/retrieval change set:**
- the grounding server and the search tool's escalation ladder;
- grokbot conversion and indexing;
- collection names and retrieval-only contract text;
- the nonlinear Collect retrieval and validators. Component values for nonlinear (SNL) runs are verified manually by the user with the RAG and supplied in the hinge-params file.

**Effect on existing designs.** Most of these fixes make demands larger, which is the correct direction: the old values were unconservative. Designs produced before these changes should be re-run, and some will now fail gates they used to pass. That is expected.

Item IDs (HR-xx, CFS-xx, NL-xx, HUB-xx) refer to the review's verified bug register.


## Workstream `hub`

### Status

| ID | Status | Files | What changed | Tests | Before → after |
|---|---|---|---|---|---|
| HUB-01 | FIXED | `steltic/steltic/main.py`, `steltic_cfs/steltic/main.py`, `steltic_hub/runners.py`, `steltic_hub/main.py`, `catalog/{steltic_admin/admin,steltic_variations/variations,steltic_probabilistic/probabilistic}/local_guard.py` + their `main.py` | **Modules.** A new `_local_only` middleware does three things. It requires a loopback Host on every request; `STELTIC_ALLOWED_HOSTS` adds names. It refuses a state-changing request that has a non-loopback or `null` Origin, or `Sec-Fetch-Site: cross-site`. If a request sends `X-Steltic-Hub-Token`, the value must match `STELTIC_HUB_TOKEN`. CFS applies the guard only when DEMO=0. **Hub.** It creates one random token per launch and passes it in the env of each server it starts. It sends the token on the credential push, health check, http runs, Stop and the `/m/` proxy. The proxy replaces any token header the page sent. The same guard was added to the bundled Admin, Variations and Probabilistic servers. | `steltic/tests/test_local_only_guard.py` (5), `steltic_cfs/tests/test_local_only_guard.py` (5), `steltic_hub/tests/test_review_fixes_hub.py::test_bundled_module_servers_carry_the_same_local_guard`, `::test_local_guard_refuses_cross_site_and_rebinding_and_checks_the_token`, `::test_the_hub_hands_each_server_a_token_and_sends_it_back` (a real server process), `::test_the_proxy_sends_the_hubs_token_never_the_browsers` | **HR**, pristine: a text/plain `POST /api/creds` with `Origin: https://evil.example` returned **200** and changed base_url. `GET /api/me` with `Host: attacker.example` returned **200**. Now both return **403**. **CFS** (listed as UNVERIFIED in the register) behaves the same on pristine: 200 before, so it was exploitable. Now **403**. |
| HUB-04 | FIXED | `steltic_hub/runners.py` (stage_inputs), `manifest.py` (validation), `catalog/steltic_nonlinear.json`, `jobs.py` and `main.py` (listing order), README | **Record.** Folder and zip staging writes `.hub_staged.json` with a content signature of each package file (sha256, or size+CRC for zips). **Re-stage.** Files the new package dropped are moved aside. When a `design_files` file differs from the package *staged last time* (not from the project copy), the stage entry's `supersede` outputs and the earlier design's files move aside together. An edited project copy is moved aside before a changed package file replaces it. Everything goes to `_superseded/<YYYYmmdd-HHMMSS>/`; nothing is deleted, and each move is logged. **Projects with no record** move differing files aside once and log a warning; their outputs are not moved. **Nonlinear manifest:** every design-package stage declares `design_files` and `supersede`. | `test_a_new_design_package_supersedes_the_old_one_and_its_analyses`, `test_an_edited_project_copy_is_kept_before_a_new_package_replaces_it`, `test_what_the_module_adds_to_a_package_file_is_not_a_new_design`, `test_a_project_staged_before_the_record_existed_is_handled`, `test_zip_packages_are_tracked_the_same_way`, `test_stage_globs_are_validated`, `test_every_nonlinear_design_package_stage_declares_what_it_supersedes` | **Synthetic repro** (review harness): pristine leaves `design/old_only.json` and `pushover/po.json` next to the new cfg. Now they are in `_superseded/<t>/` with the old cfg.py. **Real data:** copies of saved SNL job Ex22 and HR Ex22 in the review harness. Pristine overlays 127 files and keeps all SNL results. The fix's first stage finds no record, moves aside SNL's extended `design/calc_package.json` and warns. A re-stage after SNL edits calc_package moves nothing. A simulated HR redesign moves the 151 MB of results (pushover, nlrha, ddm, hinge params, review…) plus the old cfg and calc_package into one snapshot. |
| HUB-05 | FIXED | `steltic_hub/runners.py` (ServerSupervisor), `main.py` (RUNS passed in) | If a dependency becomes available while a hub run is using the server, the restart is deferred and logged. A run counts as using the server if it is the module's own run, an http run of a module whose server `requires` it, or a CLI run whose command or env names `{server.<id>}`. The run that asked for the server does not count. The first `ensure()` after the server goes idle restarts it. | `test_a_dependency_becoming_available_waits_for_the_run_to_finish` | Before: `stop()` was called right away, with no check of RUNS. After: no stop while `hr.design` or `loop.design` is live. The stop happens once those runs are idle. |
| HUB-06 | FIXED | `steltic_hub/runners.py` (field_values, stage_inputs) | An absolute file-field path is accepted only if it equals the field's expanded default or lies under `config.DATA`. Outside those, staged fields are refused. A field that is not staged may still name one existing **file**, never a folder. This keeps Admin's standards queue working: it converts PDFs from a folder the user picked. A stage whose `to` lies inside its `from` folder is refused; before, the copy recursed. | `test_absolute_file_fields_are_confined`, `test_a_folder_is_never_staged_into_itself` | Before: `package=/` or `package=$HOME` was accepted and its whole tree copied into the project. After: RunError "is outside the project and the hub's data folder". |

Out of scope and untouched: HUB-02, HUB-03, HUB-07, HUB-08 (RAG/grounding), HUB-09 (another agent), `job_tools.py`, `rag_server.py`.

Test results:
- hub: 127 passed, 1 skipped (was 113/1; 14 new)
- steltic: 23 passed (was 18)
- steltic_cfs: 22 passed (was 17)

### Behaviour changes users will notice

- **Module servers refuse non-loopback requests.** HR, CFS (local), Admin, Variations and Probabilistic return 403 for any request whose Host is not loopback, and for cross-site state-changing requests. Reaching a server by LAN IP or hostname now needs `STELTIC_ALLOWED_HOSTS`. CFS DEMO=1 (Cloud Run) is unchanged.
- **New folders in Nonlinear projects.** `_superseded/<time>/` folders appear when HR Steel's design changes. After a redesign, the earlier pushover, NLRHA, DDM, Collect (hinge_params_collected.json), review and hazard results move there. The Run gate then asks for Collect again, which is correct for new sections. The run log says what moved and where.
- **Projects staged by the old hub** get a one-time `_superseded` copy of any differing package files. They also get a warning, not a supersede, when design files differ.
- **Package copies are overwritten as before.** A re-stage with an unchanged package still overwrites module-extended package files with the package version, as it always did (for example SNL's `ddm_analysis` key in design/calc_package.json).
- **Absolute paths outside the data folder are refused for the design package** (and any staged field). Admin `@file:/elsewhere/pkg.zip` for a package now fails; uploads and `{out.steltic}` are unaffected.
- **Deferred restart is visible in the server log.** Restarting HR Steel when the standards server appears now waits for running designs. Until then it keeps running without the new dependency, and the server log says "restart … deferred".

### Follow-ups

- **The token is a positive credential, not mandatory.** A request with no token and no browser headers (curl, another module's server) is still accepted. That is because the snl loop_server and variations call HR's `/api/run` without it. A strict mode needs snl's HR client to send `X-Steltic-Hub-Token` (steltic_nonlinear is not in my worktrees). The browser-side protection does not depend on the token.
- **HUB-05 only sees hub-started runs.** A design started from a module's own embedded page is invisible to the hub. A module "busy" endpoint would close this.
- **HUB-04 does not check for a live run on the same project.** If HR's design changed and two Nonlinear tabs start at once on the same project, the second stage could move outputs from under the first run.
- **The supersede list is hand-written.** The `supersede` list in steltic_nonlinear.json comes from SNL's output names. It should be kept in step with SNL, or SNL could declare its outputs itself.
- **rag_server has no Host check** (part of HUB-01's original note). It is out of scope under the RAG deferral.
