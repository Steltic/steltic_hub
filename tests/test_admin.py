"""The bundled Admin module: the instruction grammar, the standards queue, the plan executor against a
fake hub that speaks the real /api/run event stream, and the help corpus. No module installs needed.

    python -m pytest tests/test_admin.py -q
"""
import json, os, pathlib, socket, sys, tempfile, threading, time
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "steltic_hub" / "catalog" / "steltic_admin"))
os.environ.setdefault("STELTIC_HUB_DATA", tempfile.mkdtemp(prefix="stelticthub-test-"))
_ADMIN_TMP = pathlib.Path(tempfile.mkdtemp(prefix="steltic-admin-test-"))
os.environ.setdefault("ADMIN_DATA", str(_ADMIN_TMP / "admin"))
os.environ.setdefault("HUB_DATA", str(_ADMIN_TMP / "hubdata"))
os.environ.setdefault("HUB_JOBS", str(_ADMIN_TMP / "jobs"))

from admin import grammar, standards, plans, help as helpdesk      # noqa: E402
from admin.hub import HubClient, parse_sse                          # noqa: E402


# ---------------------------------------------------------------- the hub side
def test_admin_is_a_bundled_module_the_hub_can_load():
    from steltic_hub.manifest import load_catalog
    from steltic_hub import config
    cat = load_catalog(config.CATALOG_DIR)
    m = cat["steltic_admin"]
    assert m.bundled == "steltic_admin" and m.has_server and m.credentials
    assert [t.id for t in m.tabs] == ["batch", "standards", "help", "files"]
    assert m.env_vars["HUB_URL"] == "{hub_url}" and m.env_vars["ADMIN_DATA"] == "{data_dir}/admin"
    for f in ("admin/main.py", "admin/plans.py", "admin/ui/index.html", "requirements.txt"):
        assert (config.CATALOG_DIR / m.bundled / f).is_file()


def test_hub_url_template_reaches_module_servers():
    from steltic_hub import config, runners
    from steltic_hub.registry import Registry
    reg = Registry()
    m = reg.catalog["steltic_admin"]
    ctx = runners.base_ctx(m, "__server__", reg, port=8411)
    assert ctx["hub_url"].startswith("http://127.0.0.1:")
    env = runners.expand(m.env_vars, ctx)
    assert env["HUB_URL"] == ctx["hub_url"] and env["ADMIN_DATA"] == str(config.DATA) + "/admin"


# ---------------------------------------------------------------- grammar
def test_grammar_reads_the_batch_instruction_people_actually_type():
    r = grammar.parse("run J1 to hr then to nl, then when all done run J2 to cfs only, then J3 to hr")
    assert [(s["project"], s["module"], s["tab"]) for s in r["steps"]] == [
        ("J1", "steltic", "design"), ("J1", "steltic_nonlinear", "run"), ("J2", "steltic_cfs", "design"), ("J3", "steltic", "design")]
    assert r["steps"][0]["fields"] == {"brief": "@project"}
    assert not r["warnings"]


def test_grammar_brief_sources_continue_and_warnings():
    r = grammar.parse("J1 (ex22) to hr then nl; J2 with ex3 to cfs\nJ4 cfs continue: use thicker studs")
    briefs = [s["fields"].get("brief") for s in r["steps"]]
    assert briefs == ["@example:ex22", None, "@example:ex3", "use thicker studs"]
    assert r["steps"][3]["module"] == "steltic_cfs" and r["steps"][3]["tab"] == "continue"
    r = grammar.parse("J9 to hr and make it snappy")
    assert [s["tab"] for s in r["steps"]] == ["design"]
    assert any("snappy" in w for w in r["warnings"])
    assert grammar.parse("hr for J9")["steps"][0]["project"] == "J9"
    assert grammar.parse("just do something")["steps"] == []


# ---------------------------------------------------------------- standards
def test_standards_scan_guesses_stems_and_skips_converted(tmp_path):
    folder = tmp_path / "standards"; folder.mkdir()
    for n in ("AISC 360-22 Specification.pdf", "asce7-22.pdf", "AISI_S400-20.pdf", "mystery.pdf"):
        (folder / n).write_bytes(b"%PDF-1.4\n")
    root = tmp_path / "grokbot"; (root / "markdown").mkdir(parents=True)
    (root / "markdown" / "AISC_360_22.search.md").write_text("x")
    d = standards.scan(folder, root)
    by = {i["name"]: i for i in d["items"]}
    assert by["AISC 360-22 Specification.pdf"]["stem"] == "AISC_360_22" and by["AISC 360-22 Specification.pdf"]["converted"]
    assert by["asce7-22.pdf"]["stem"] == "ASCE7" and not by["asce7-22.pdf"]["converted"]
    assert by["AISI_S400-20.pdf"]["stem"] == "AISI_S400_20"
    assert by["mystery.pdf"]["stem"] == ""
    steps = standards.build_steps([{"pdf": by["asce7-22.pdf"]["pdf"], "stem": "ASCE7"}], str(folder))
    assert [s["tab"] for s in steps] == ["convert", "index", "audit"]
    assert steps[0]["fields"]["pdf"].endswith("asce7-22.pdf") and steps[0]["fields"]["stem"] == "ASCE7"
    assert steps[0]["on_fail"] == "continue" and steps[1]["on_fail"] == "stop"
    assert steps[2]["fields"]["pdf_dir"] == str(folder)


# ---------------------------------------------------------------- a fake hub
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


BEHAVIOUR = {"flaky_fail": 0, "continue": []}     # scripted by the tests: how many flaky runs fail, what each continue does


def _fake_hub(state_modules, log: list):
    """The routes Admin uses, answered the way the real hub answers them."""
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse
    app = FastAPI()
    cancelled = set()
    saved = set()                                   # jobs whose design got as far as a save (what a continue needs)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "modules": len(state_modules), "version": "test", "source": str(ROOT)}

    @app.get("/api/state")
    def state():
        return {"modules": state_modules, "jobs": [{"name": "J1"}], "connection": True, "running": {}, "hub": {"source": str(ROOT)}}

    @app.post("/api/jobs/{job}")
    def job(job: str):
        return {"ok": True, "name": job}

    @app.post("/api/cancel/{run_id}")
    def cancel(run_id: str):
        cancelled.add(run_id); return {"ok": True}

    @app.get("/m/{mod}/api/example/{ex}")
    def example(mod: str, ex: str):
        return {"brief": f"brief of {ex} from {mod}"}

    @app.post("/api/run/{mod}/{tab}")
    async def run(mod: str, tab: str, request: Request):
        body = await request.json()
        log.append({"module": mod, "tab": tab, "job": body["job"], "fields": body["fields"]})
        rid = f"r{len(log)}"

        def sse(ev):
            return "data: " + json.dumps(ev) + "\n\n"

        async def gen():
            import asyncio
            yield sse({"type": "start", "run_id": rid, "module": mod, "tab": tab, "cmd": f"python -m {mod} {tab}"})
            if tab == "slow" or (tab == "design" and body["fields"].get("brief") == "slow"):
                saved.add(body["job"])
                for i in range(40):
                    if rid in cancelled:
                        if tab == "design":                       # HR Steel: Stop saves the conversation and pauses
                            yield sse({"type": "paused", "reason": "stopped by user", "detail": "Progress is saved"})
                        yield sse({"type": "done", "ok": False, "cancelled": True, "rc": -1, "job": body["job"], "artifacts": []})
                        return
                    yield sse({"type": "log", "text": f"tick {i}"})
                    yield ": ping\n\n"
                    await asyncio.sleep(0.05)
            yield sse({"type": "token", "text": "hel"}); yield sse({"type": "token", "text": "lo"})
            yield sse({"type": "log", "text": "working"})
            if tab == "fail":
                yield sse({"type": "error", "text": "boom"})
                yield sse({"type": "done", "ok": False, "rc": 1, "job": body["job"], "artifacts": []})
            elif tab == "flaky":                                  # a design whose model server is away for a while
                saved.add(body["job"])
                if BEHAVIOUR["flaky_fail"] > 0:
                    BEHAVIOUR["flaky_fail"] -= 1
                    yield sse({"type": "status", "text": "LLM call failed (ReadTimeout); retry 8/8 in 60s"})
                    yield sse({"type": "error", "text": "LLM call failed: LLM API 502: upstream connect error"})
                    yield sse({"type": "done", "ok": False, "job": body["job"], "artifacts": [], "end": True})
                else:
                    yield sse({"type": "done", "ok": True, "job": body["job"], "artifacts": [{"label": "Report", "path": "report.html"}], "end": True})
            elif tab == "pauser":                                 # HR Steel's loop guard
                saved.add(body["job"])
                yield sse({"type": "paused", "reason": "'run_python' repeated 3x with no progress", "detail": "hit Continue"})
                yield sse({"type": "done", "ok": False, "job": body["job"], "artifacts": [], "end": True})
            elif tab == "budget":                                 # nothing a continue would heal
                saved.add(body["job"])
                yield sse({"type": "error", "text": "call budget reached (200 model calls) -- run aborted"})
                yield sse({"type": "done", "ok": False, "job": body["job"], "artifacts": [], "end": True})
            elif tab == "continue":                               # HR Steel's Continue: resume from conversation.json
                what = BEHAVIOUR["continue"].pop(0) if BEHAVIOUR["continue"] else "ok"
                if body["job"] not in saved or what == "409":
                    yield sse({"type": "error", "text": f"HR Steel refused the run (409): nothing to resume for '{body['job']}', and your browser holds no saved copy of it."})
                    yield sse({"type": "done", "ok": False, "job": body["job"], "artifacts": [], "end": True})
                elif what == "paused":
                    yield sse({"type": "paused", "reason": "'run_python' repeated 3x with no progress", "detail": "hit Continue"})
                    yield sse({"type": "done", "ok": False, "job": body["job"], "artifacts": [], "end": True})
                elif what.startswith("error:"):
                    yield sse({"type": "error", "text": what[6:]})
                    yield sse({"type": "done", "ok": False, "job": body["job"], "artifacts": [], "end": True})
                else:
                    yield sse({"type": "status", "text": f"resumed '{body['job']}' from saved conversation (41 messages)"})
                    yield sse({"type": "done", "ok": True, "job": body["job"], "artifacts": [{"label": "Report", "path": "report.html"}], "end": True})
            else:
                yield sse({"type": "done", "ok": True, "rc": 0, "job": body["job"], "artifacts": [{"label": "Report", "path": "report.html"}]})
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"X-Run-Id": rid})

    return app


@pytest.fixture(scope="module")
def fake_hub():
    import uvicorn
    fields = [{"id": "job", "type": "project", "label": "Project", "required": True},
              {"id": "brief", "type": "textarea", "label": "Brief", "required": True, "has_default": False},
              {"id": "examples", "type": "select", "label": "Example", "fills": {"path": "/api/example/{value}", "key": "brief", "target": "brief"}}]
    mods = [
        {"id": "steltic", "name": "HR Steel", "status": {"env_ready": True, "installed": True}, "missing_needs": [], "wants_credentials": True,
         "tabs": [{"id": "design", "title": "Design", "kind": "form", "run": {"kind": "http", "continues": None}, "fields": fields, "missing_optional": []},
                  {"id": "continue", "title": "Continue", "kind": "form", "run": {"kind": "http", "continues": "design"}, "missing_optional": [],
                   "fields": [{"id": "job", "type": "project", "label": "Project", "required": True},
                              {"id": "brief", "type": "textarea", "label": "Instruction", "required": False, "has_default": False}]},
                  {"id": "fail", "title": "Fail", "kind": "form", "run": {"kind": "cli"}, "fields": [], "missing_optional": []},
                  {"id": "slow", "title": "Slow", "kind": "form", "run": {"kind": "cli"}, "fields": [], "missing_optional": []},
                  {"id": "flaky", "title": "Flaky design", "kind": "form", "run": {"kind": "http"}, "fields": [], "missing_optional": []},
                  {"id": "pauser", "title": "Pausing design", "kind": "form", "run": {"kind": "http"}, "fields": [], "missing_optional": []},
                  {"id": "budget", "title": "Budget", "kind": "form", "run": {"kind": "http"}, "fields": [], "missing_optional": []},
                  {"id": "app", "title": "Full UI", "kind": "embed", "run": None, "fields": []}]},
        {"id": "steltic_x", "name": "X design", "status": {"env_ready": True, "installed": True}, "missing_needs": [], "wants_credentials": True,
         "tabs": [{"id": "flaky", "title": "Flaky (no continue tab)", "kind": "form", "run": {"kind": "http"}, "fields": [], "missing_optional": []}]},
        {"id": "steltic_nonlinear", "name": "Nonlinear (SNL)", "status": {"env_ready": True}, "missing_needs": [], "wants_credentials": False,
         "tabs": [{"id": "run", "title": "Run", "kind": "form", "run": {"kind": "cli"}, "missing_optional": [],
                   "fields": [{"id": "job", "type": "project", "label": "Project", "required": True},
                              {"id": "package", "type": "file", "label": "Package", "required": False, "has_default": True}]}]},
        {"id": "steltic_grokbot", "name": "Query file manager", "status": {"env_ready": True}, "missing_needs": [], "wants_credentials": False,
         "tabs": [{"id": "convert", "title": "Convert PDF", "kind": "form", "run": {"kind": "cli"}, "missing_optional": ["converter"],
                   "fields": [{"id": "pdf", "type": "file", "label": "PDF", "required": True}]}]},
        {"id": "not_installed", "name": "Absent", "status": {"env_ready": False}, "missing_needs": [], "tabs": [{"id": "x", "title": "X", "kind": "form", "run": {"kind": "cli"}, "fields": []}]},
    ]
    log: list = []
    BEHAVIOUR["mods"] = mods                          # so a test can point steltic's Continue tab at another tab
    app = _fake_hub(mods, log)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True); th.start()
    hub = HubClient(f"http://127.0.0.1:{port}")
    deadline = time.time() + 15
    while time.time() < deadline and not hub.reachable():
        time.sleep(0.1)
    assert hub.reachable()
    yield hub, log
    server.should_exit = True


def test_sse_parser_matches_the_hubs_stream():
    lines = ["data: {\"type\": \"log\", \"text\": \"a\"}", "", ": ping", "", "data: {\"type\": \"done\", \"ok\": true}", ""]
    out = list(parse_sse(iter(lines)))
    assert out == [{"type": "log", "text": "a"}, None, {"type": "done", "ok": True}]


def test_hub_client_runs_and_reports_the_outcome(fake_hub):
    hub, log = fake_hub
    seen = []
    out = hub.run("steltic", "design", "J1", {"brief": "x"}, on_event=seen.append)
    assert out["ok"] and out["rc"] == 0 and out["run_id"] and out["artifacts"][0]["path"] == "report.html"
    assert [e["type"] for e in seen][:2] == ["start", "token"]
    out = hub.run("steltic", "fail", "J1", {}, on_event=lambda e: None)
    assert not out["ok"] and out["rc"] == 1 and out["errors"] == ["boom"]


def _executor(tmp_path, hub):
    jobs = tmp_path / "jobs"; jobs.mkdir(exist_ok=True)
    return plans.Executor(hub, plans.Store(tmp_path / "admin"), jobs), jobs


def _wait(ex, plan_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = ex.store.load(plan_id)
        if p["status"] != "running":
            return p
        time.sleep(0.1)
    raise AssertionError("plan did not finish")


def test_validation_names_what_is_missing(fake_hub, tmp_path):
    hub, _ = fake_hub
    ex, jobs = _executor(tmp_path, hub)
    plan = plans.new_plan("t", [
        {"project": "J1", "module": "steltic", "tab": "design", "fields": {"brief": "@project"}},
        {"project": "J1", "module": "steltic", "tab": "app", "fields": {}},
        {"project": "J1", "module": "nope", "tab": "x", "fields": {}},
        {"project": "J1", "module": "not_installed", "tab": "x", "fields": {}},
        {"project": "S", "module": "steltic_grokbot", "tab": "convert", "fields": {"pdf": "C:/x.pdf"}},
        {"project": "J1", "module": "steltic", "tab": "design", "fields": {}},
    ])
    errors, warnings = ex.validate(plan)
    joined = "\n".join(errors)
    assert "no brief.md" in joined                       # @project with nothing in the folder
    assert "not something the hub can run" in joined     # an embed tab
    assert "no module 'nope'" in joined
    assert "is not installed" in joined
    assert "optional component" in joined                # the converter gate, before pressing anything
    assert "Brief is required" in joined
    (jobs / "J1").mkdir(); (jobs / "J1" / "brief.md").write_text("4-story office")
    errors, _ = ex.validate(plans.new_plan("t", [{"project": "J1", "module": "steltic", "tab": "design", "fields": {"brief": "@project"}}]))
    assert errors == []


def test_a_plan_runs_its_steps_in_order_and_keeps_the_logs(fake_hub, tmp_path):
    hub, log = fake_hub
    ex, jobs = _executor(tmp_path, hub)
    (jobs / "J1").mkdir(); (jobs / "J1" / "brief.md").write_text("4-story office, SMF")
    r = grammar.parse("J1 to hr then nl; J2 (ex22) to hr")
    plan = plans.new_plan("batch", r["steps"], source="…")
    ex.store.save(plan)
    n0 = len(log)
    ex.start(plan["id"])
    p = _wait(ex, plan["id"])
    assert p["status"] == "done" and [s["status"] for s in p["steps"]] == ["done", "done", "done"]
    sent = log[n0:]
    assert [(s["module"], s["tab"], s["job"]) for s in sent] == [("steltic", "design", "J1"), ("steltic_nonlinear", "run", "J1"), ("steltic", "design", "J2")]
    assert sent[0]["fields"]["brief"] == "4-story office, SMF"          # @project -> the file's text
    assert sent[2]["fields"]["brief"] == "brief of ex22 from steltic"   # @example -> the tab's own fills endpoint
    assert p["steps"][0]["artifacts"][0]["path"] == "report.html" and p["steps"][0]["run_id"]
    text = ex.store.log_tail(plan["id"], 1)
    assert "hello" in text and "working" in text and "✓ done" in text   # tokens joined on one line, then the log lines


def test_failure_policy_stop_skip_project_continue(fake_hub, tmp_path):
    hub, log = fake_hub
    ex, jobs = _executor(tmp_path, hub)
    mk = lambda on_fail: plans.new_plan("f", [
        {"project": "A", "module": "steltic", "tab": "fail", "fields": {}, "on_fail": on_fail},
        {"project": "A", "module": "steltic_nonlinear", "tab": "run", "fields": {}},
        {"project": "B", "module": "steltic_nonlinear", "tab": "run", "fields": {}}])
    for on_fail, expect in (("stop", ["failed", "pending", "pending"]),
                            ("skip_project", ["failed", "skipped", "done"]),
                            ("continue", ["failed", "done", "done"])):
        plan = mk(on_fail); ex.store.save(plan); ex.start(plan["id"])
        p = _wait(ex, plan["id"])
        assert [s["status"] for s in p["steps"]] == expect, on_fail
        assert p["status"] == "failed" and "boom" in p["note"]
    # resume with retry runs the failed step again (it fails again here) and the rest
    plan = mk("stop"); ex.store.save(plan); ex.start(plan["id"]); p = _wait(ex, plan["id"])
    ex.start(plan["id"], retry_failed=True); p = _wait(ex, plan["id"])
    assert [s["status"] for s in p["steps"]] == ["failed", "pending", "pending"]
    plan = ex.store.load(plan["id"]); plan["steps"][0]["on_fail"] = "continue"; ex.store.save(plan)
    ex.start(plan["id"], retry_failed=False); p = _wait(ex, plan["id"])
    assert [s["status"] for s in p["steps"]] == ["failed", "done", "done"]


def test_stop_cancels_the_current_run_through_the_hub(fake_hub, tmp_path):
    hub, log = fake_hub
    ex, jobs = _executor(tmp_path, hub)
    plan = plans.new_plan("s", [{"project": "A", "module": "steltic", "tab": "slow", "fields": {}},
                               {"project": "A", "module": "steltic_nonlinear", "tab": "run", "fields": {}}])
    ex.store.save(plan); ex.start(plan["id"])
    assert ex.running == plan["id"]
    deadline = time.time() + 10
    while time.time() < deadline and not ex.current_run(plan["id"]):
        time.sleep(0.05)
    with pytest.raises(RuntimeError):
        ex.start(plan["id"])                      # one plan at a time
    ex.stop(plan["id"])
    p = _wait(ex, plan["id"])
    assert p["status"] == "stopped" and [s["status"] for s in p["steps"]] == ["stopped", "pending"]
    ex.start(plan["id"]); p = _wait(ex, plan["id"])     # Resume: the stopped step runs again, then the rest
    assert [s["status"] for s in p["steps"]] == ["done", "done"]


def test_a_plan_left_running_by_a_dead_admin_is_marked_interrupted(tmp_path):
    store = plans.Store(tmp_path / "a")
    plan = plans.new_plan("x", [{"project": "A", "module": "m", "tab": "t", "fields": {}}])
    plan["status"] = "running"; plan["steps"][0]["status"] = "running"; store.save(plan)
    plans.Executor(HubClient("http://127.0.0.1:1"), store, tmp_path / "jobs")
    p = store.load(plan["id"])
    assert p["status"] == "interrupted" and p["steps"][0]["status"] == "stopped" and "restarted" in p["note"]


def test_validate_without_a_hub_says_so(tmp_path):
    ex = plans.Executor(HubClient("http://127.0.0.1:1"), plans.Store(tmp_path / "a"), tmp_path / "jobs")
    errors, _ = ex.validate(plans.new_plan("x", [{"project": "A", "module": "m", "tab": "t", "fields": {}}]))
    assert errors and "did not answer" in errors[0]


# ---------------------------------------------------------------- a step is not one run
def _probe_seq(*answers):
    """A model-server probe that plays `answers` then repeats the last one."""
    seq = list(answers)
    def probe():
        return seq.pop(0) if len(seq) > 1 else seq[0]
    return probe


@pytest.fixture
def fast_waits(monkeypatch):
    monkeypatch.setattr(plans, "WAIT_CHECK", 0.05)
    monkeypatch.setattr(plans, "WAIT_FLOOR", (0.1, 0.1))
    BEHAVIOUR["flaky_fail"] = 0; BEHAVIOUR["continue"] = []
    yield
    BEHAVIOUR["flaky_fail"] = 0; BEHAVIOUR["continue"] = []


def test_classify_tells_a_server_outage_from_a_pause_from_a_dead_end():
    c = plans.classify
    assert c({"errors": ["LLM call failed: LLM API 502: upstream connect error"]}) == "transient"
    assert c({"errors": ["LLM call failed: "]}) == "retryable"                     # HR Steel's str(ReadTimeout) is empty
    assert c({"errors": ["lost the hub's stream: ReadError: "]}) == "transient"
    assert c({"errors": ["the hub at http://127.0.0.1:8300 did not answer: ConnectError"]}) == "transient"
    assert c({"errors": ["CFS Steel refused the run (429): you already have a run in progress"]}) == "transient"
    assert c({"errors": ["LLM call failed: LLM API 400: Invalid JSON in tool call arguments: '{'"]}) == "retryable"
    assert c({"errors": ["call budget reached (200 model calls) -- run aborted"]}) == "final"
    assert c({"errors": ["HR Steel refused the run (400): set your LLM base-url + API key in Settings first"]}) == "final"
    assert c({"errors": [], "paused": "'run_python' repeated 3x with no progress"}) == "paused"
    assert c({"errors": [], "paused": "cannot access the RAG API, restart the RAG server then click Continue."}) == "transient"
    assert c({"errors": [], "rc": 1}) == "retryable"


def test_resume_continues_a_stopped_design_where_it_stopped(fake_hub, tmp_path, fast_waits):
    hub, log = fake_hub
    ex, jobs = _executor(tmp_path, hub)
    plan = plans.new_plan("s", [{"project": "C1", "module": "steltic", "tab": "design", "fields": {"brief": "slow"}},
                               {"project": "C1", "module": "steltic_nonlinear", "tab": "run", "fields": {}}])
    ex.store.save(plan); ex.start(plan["id"])
    deadline = time.time() + 10
    while time.time() < deadline and not ex.current_run(plan["id"]):
        time.sleep(0.05)
    n0 = len(log)
    ex.stop(plan["id"])
    p = _wait(ex, plan["id"])
    assert p["status"] == "stopped" and p["steps"][0]["status"] == "stopped"
    assert p["steps"][0]["resume"] is True and "continues it" in p["steps"][0]["note"]
    ex.start(plan["id"]); p = _wait(ex, plan["id"])                      # Resume: the Continue tab, no fields, then the rest
    assert [s["status"] for s in p["steps"]] == ["done", "done"] and p["status"] == "done"
    sent = [(r["module"], r["tab"], r["job"], r["fields"]) for r in log[n0:]]
    assert sent == [("steltic", "continue", "C1", {}), ("steltic_nonlinear", "run", "C1", {})]
    assert p["steps"][0]["ran_tab"] == "continue" and p["steps"][0]["resume"] is False
    text = ex.store.log_tail(plan["id"], 1)
    assert "continues the Design run from where it stopped" in text and "resumed 'C1' from saved conversation" in text
    # ... unless the user asks for a fresh start
    ex.store.save(plan); ex.start(plan["id"]); time.sleep(0.3); ex.stop(plan["id"]); _wait(ex, plan["id"])
    n1 = len(log)
    ex.start(plan["id"], fresh=True); p = _wait(ex, plan["id"])
    assert log[n1]["tab"] == "design" and log[n1]["fields"]["brief"] == "slow" and p["steps"][0]["status"] == "done"


def test_a_server_outage_is_waited_out_then_the_design_continues(fake_hub, tmp_path, fast_waits):
    hub, log = fake_hub
    ex, jobs = _executor(tmp_path, hub)
    ex.probe = _probe_seq(False, False, True)                            # the model server: down, down, back
    BEHAVIOUR["flaky_fail"] = 1
    plan = plans.new_plan("w", [{"project": "W1", "module": "steltic", "tab": "flaky", "fields": {}}])
    # the fake hub continues a "flaky" step through steltic's Continue tab: point the tab at it
    ex.store.save(plan); n0 = len(log)
    mods = {m["id"]: m for m in BEHAVIOUR["mods"]}
    mods["steltic"]["tabs"][1]["run"]["continues"] = "flaky"
    ex.start(plan["id"]); p = _wait(ex, plan["id"])
    mods["steltic"]["tabs"][1]["run"]["continues"] = "design"
    st = p["steps"][0]
    assert st["status"] == "done" and st["waited"] == 1 and st["continued"] == 0 and st["ran_tab"] == "continue"
    assert [(r["tab"], r["fields"]) for r in log[n0:]] == [("flaky", {}), ("continue", {})]
    text = ex.store.log_tail(plan["id"], 1)
    assert "LLM API 502" in text and "waiting for the model server" in text and "continuing from where it stopped" in text
    assert p["status"] == "done"


def test_a_module_without_a_continuing_tab_is_run_again_after_the_wait(fake_hub, tmp_path, fast_waits):
    hub, log = fake_hub
    ex, jobs = _executor(tmp_path, hub)
    ex.probe = _probe_seq(None)                                          # Admin holds no connection: it just waits
    BEHAVIOUR["flaky_fail"] = 2
    plan = plans.new_plan("w", [{"project": "W2", "module": "steltic_x", "tab": "flaky", "fields": {}}])
    ex.store.save(plan); n0 = len(log)
    ex.start(plan["id"]); p = _wait(ex, plan["id"])
    st = p["steps"][0]
    assert st["status"] == "done" and st["waited"] == 2 and st["ran_tab"] == "flaky"
    assert [r["tab"] for r in log[n0:]] == ["flaky", "flaky", "flaky"]
    # and with wait_for_llm off the outage is a plain failure
    BEHAVIOUR["flaky_fail"] = 1
    plan = plans.new_plan("w", [{"project": "W3", "module": "steltic_x", "tab": "flaky", "fields": {}}], options={"wait_for_llm": False})
    ex.store.save(plan); ex.start(plan["id"]); p = _wait(ex, plan["id"])
    assert p["steps"][0]["status"] == "failed" and "LLM API 502" in p["steps"][0]["note"] and p["steps"][0]["waited"] == 0


def test_a_pause_is_continued_by_itself_a_bounded_number_of_times(fake_hub, tmp_path, fast_waits):
    hub, log = fake_hub
    ex, jobs = _executor(tmp_path, hub)
    mods = {m["id"]: m for m in BEHAVIOUR["mods"]}
    mods["steltic"]["tabs"][1]["run"]["continues"] = "pauser"
    try:
        BEHAVIOUR["continue"] = ["paused", "paused", "ok"]
        plan = plans.new_plan("p", [{"project": "P1", "module": "steltic", "tab": "pauser", "fields": {}}], options={"auto_continue": 3})
        ex.store.save(plan); n0 = len(log); ex.start(plan["id"]); p = _wait(ex, plan["id"])
        st = p["steps"][0]
        assert st["status"] == "done" and st["continued"] == 3 and [r["tab"] for r in log[n0:]] == ["pauser", "continue", "continue", "continue"]
        assert "continuing from where it stopped (1 of 3)" in ex.store.log_tail(plan["id"], 1)
        BEHAVIOUR["continue"] = ["paused", "paused", "paused"]
        plan = plans.new_plan("p", [{"project": "P2", "module": "steltic", "tab": "pauser", "fields": {}}], options={"auto_continue": 2})
        ex.store.save(plan); n0 = len(log); ex.start(plan["id"]); p = _wait(ex, plan["id"])
        st = p["steps"][0]
        assert st["status"] == "failed" and st["continued"] == 2 and "after 2 continues" in st["note"] and "no progress" in st["note"]
        assert [r["tab"] for r in log[n0:]] == ["pauser", "continue", "continue"]
        assert st["resume"] is True                                       # Resume with retry continues it again
    finally:
        mods["steltic"]["tabs"][1]["run"]["continues"] = "design"


def test_a_dead_end_is_not_continued_and_nothing_to_resume_starts_over_once(fake_hub, tmp_path, fast_waits):
    hub, log = fake_hub
    ex, jobs = _executor(tmp_path, hub)
    mods = {m["id"]: m for m in BEHAVIOUR["mods"]}
    mods["steltic"]["tabs"][1]["run"]["continues"] = "budget"
    try:
        plan = plans.new_plan("b", [{"project": "B1", "module": "steltic", "tab": "budget", "fields": {}}])
        ex.store.save(plan); n0 = len(log); ex.start(plan["id"]); p = _wait(ex, plan["id"])
        st = p["steps"][0]
        assert st["status"] == "failed" and st["continued"] == 0 and "call budget" in st["note"] and [r["tab"] for r in log[n0:]] == ["budget"]
    finally:
        mods["steltic"]["tabs"][1]["run"]["continues"] = "design"
    # a step marked resumable whose module has nothing saved: the continue is refused (409), the step starts over
    (jobs / "N1").mkdir(); (jobs / "N1" / "brief.md").write_text("fresh")
    plan = plans.new_plan("n", [{"project": "N1", "module": "steltic", "tab": "design", "fields": {"brief": "@project"}}])
    plan["steps"][0].update(status="stopped", run_id="r-old")
    ex.store.save(plan); n0 = len(log)
    BEHAVIOUR["continue"] = ["409"]
    ex.start(plan["id"]); p = _wait(ex, plan["id"])
    assert [(r["tab"], r["fields"].get("brief")) for r in log[n0:]] == [("continue", None), ("design", "fresh")]
    assert p["steps"][0]["status"] == "done" and "starting the step over" in ex.store.log_tail(plan["id"], 1)


def test_stop_ends_a_wait_for_the_server(fake_hub, tmp_path, fast_waits):
    hub, log = fake_hub
    ex, jobs = _executor(tmp_path, hub)
    ex.probe = _probe_seq(False)                                          # never comes back
    BEHAVIOUR["flaky_fail"] = 1
    plan = plans.new_plan("w", [{"project": "W4", "module": "steltic_x", "tab": "flaky", "fields": {}},
                               {"project": "W4", "module": "steltic_nonlinear", "tab": "run", "fields": {}}])
    ex.store.save(plan); ex.start(plan["id"])
    deadline = time.time() + 10
    while time.time() < deadline and "waiting for the model server" not in (ex.store.load(plan["id"])["steps"][0]["note"] or ""):
        time.sleep(0.05)
    assert ex.running == plan["id"]
    ex.stop(plan["id"]); p = _wait(ex, plan["id"])
    assert p["status"] == "stopped" and [s["status"] for s in p["steps"]] == ["stopped", "pending"]
    assert "stopped while waiting" in p["steps"][0]["note"]
    BEHAVIOUR["flaky_fail"] = 0
    ex.start(plan["id"]); p = _wait(ex, plan["id"])                       # Resume runs it again (no continuing tab here)
    assert [s["status"] for s in p["steps"]] == ["done", "done"]


# ---------------------------------------------------------------- help
def test_help_corpus_finds_the_passage_and_answers_without_a_model(tmp_path):
    root = tmp_path / "repo"; (root / "docs").mkdir(parents=True); (root / ".git").mkdir()
    (root / "README.md").write_text("# Thing\n\nThe nonlinear module picks up the HR Steel design through run.stage and {out.steltic}.\n")
    (root / "docs" / "other.md").write_text("Unrelated text about lunch.\n")
    (root / "code.py").write_text("def stage_inputs(run, ctx, log):\n    '''copies the design package'''\n")
    (root / ".git" / "secret.md").write_text("nonlinear nonlinear nonlinear")
    c = helpdesk.Corpus()
    assert c.add_tree(root, "repo") == 3
    hits = c.search("how does the nonlinear module pick up the HR Steel design?")
    assert hits and hits[0]["label"] == "repo:README.md" and "run.stage" in hits[0]["text"]
    assert all(".git" not in h["label"] for h in hits)
    ans = helpdesk.answer("q", hits)
    assert ans["model"] is None and "run.stage" in ans["answer"]
    assert helpdesk.answer("q", [])["answer"].startswith("Nothing")
    assert helpdesk.GitHub.parse_repo("https://github.com/Steltic/steltic_cfs") == ("Steltic", "steltic_cfs")
    assert helpdesk.GitHub.parse_repo("https://github.com/Steltic/steltic.git") == ("Steltic", "steltic")


# ---------------------------------------------------------------- the server
def test_admin_server_routes(tmp_path):
    from fastapi.testclient import TestClient
    from admin import main
    c = TestClient(main.app)
    assert c.get("/healthz").json()["ok"]
    assert c.get("/").status_code == 200
    me = c.get("/api/me").json()
    assert me["hub_url"] and "aliases" in me and me["llm"] is False
    d = c.post("/api/plan/parse", json={"text": "J1 to hr then nl"}).json()
    assert len(d["plan"]["steps"]) == 2 and d["errors"]           # no hub behind it in this test -> says so
    r = c.post("/api/plans", json={"plan": d["plan"]}).json()
    assert r["ok"] and c.get("/api/plans/" + r["id"]).json()["steps"][1]["module"] == "steltic_nonlinear"
    assert c.get("/api/plans").json()["plans"][0]["id"] == r["id"]
    assert c.post(f"/api/plans/{r['id']}/stop").status_code == 409
    assert c.get(f"/api/plans/{r['id']}/log/1").json()["text"] == ""
    assert c.delete("/api/plans/" + r["id"]).json()["ok"]
    assert c.get("/api/plans/" + r["id"]).status_code == 404
    assert c.post("/api/creds", json={"model": "MOCK"}).json()["llm"] is False
    assert c.post("/api/plan/parse-llm", json={"text": "x"}).status_code == 400
    s = c.get("/api/standards/scan", params={"folder": str(tmp_path / "nowhere")}).json()
    assert s["exists"] is False and "default_folder" in s
    assert c.post("/api/standards/plan", json={"items": []}).status_code == 400
    assert c.post("/api/standards/plan", json={"items": [{"pdf": "a.pdf", "stem": "NOT_A_STEM"}]}).status_code == 400
    assert c.post("/api/help", json={"question": ""}).status_code == 400
    assert c.get("/api/help/history").json()["items"] == []
