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


def _fake_hub(state_modules, log: list):
    """The routes Admin uses, answered the way the real hub answers them."""
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse
    app = FastAPI()
    cancelled = set()

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
            if tab == "slow":
                for i in range(40):
                    if rid in cancelled:
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
         "tabs": [{"id": "design", "title": "Design", "kind": "form", "run": {"kind": "http"}, "fields": fields, "missing_optional": []},
                  {"id": "fail", "title": "Fail", "kind": "form", "run": {"kind": "cli"}, "fields": [], "missing_optional": []},
                  {"id": "slow", "title": "Slow", "kind": "form", "run": {"kind": "cli"}, "fields": [], "missing_optional": []},
                  {"id": "app", "title": "Full UI", "kind": "embed", "run": None, "fields": []}]},
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
