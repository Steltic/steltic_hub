"""Hub fixes from the 2026-10 review: HUB-01 (module servers' guard + the hub's per-launch token),
HUB-04 (package staging supersedes, never overlays), HUB-05 (no restart under a live run) and HUB-06
(absolute file-field paths).

    python -m pytest tests/test_review_fixes_hub.py -q
"""
import asyncio, os, pathlib, socket, sys, tempfile, time, zipfile
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STELTIC_HUB_DATA", tempfile.mkdtemp(prefix="stelticthub-test-"))

from steltic_hub.manifest import Manifest, ManifestError, Run, load_catalog   # noqa: E402
from steltic_hub import config, runners                                       # noqa: E402

CATALOG = config.CATALOG_DIR
GUARDED = ("steltic_admin/admin", "steltic_variations/variations", "steltic_probabilistic/probabilistic",
           "steltic_userbuilding/userbuilding")


# ================================================================ HUB-01
def _guard_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("local_guard_t", CATALOG / GUARDED[0] / "local_guard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bundled_module_servers_carry_the_same_local_guard():
    """Admin drives the hub's own /api/run from its server, so a cross-site POST to Admin would reach
    the hub WITHOUT the page's Origin -- around the hub's guard. Every server the hub ships is guarded."""
    copies = {(CATALOG / g / "local_guard.py").read_text(encoding="utf-8") for g in GUARDED}
    assert len(copies) == 1, "the three copies of local_guard.py must stay identical"
    for g in GUARDED:
        src = (CATALOG / g / "main.py").read_text(encoding="utf-8")
        assert "local_guard.install(app)" in src, g


def test_local_guard_refuses_cross_site_and_rebinding_and_checks_the_token(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    lg = _guard_module()
    app = FastAPI()
    lg.install(app)
    hits = []

    @app.post("/api/creds")
    def creds():
        hits.append(1)
        return {"ok": True}

    @app.get("/api/me")
    def me():
        return {"ok": True}

    monkeypatch.delenv("STELTIC_HUB_TOKEN", raising=False)
    c = TestClient(app)
    for h in ({"origin": "https://evil.example"}, {"origin": "null"}, {"sec-fetch-site": "cross-site"}):
        assert c.post("/api/creds", content="{}", headers={"content-type": "text/plain", **h}).status_code == 403
    assert not hits
    assert c.get("/api/me", headers={"host": "attacker.example"}).status_code == 403          # DNS rebinding
    assert c.post("/api/creds", headers={"origin": "http://127.0.0.1:8412", "sec-fetch-site": "same-origin"}).status_code == 200
    assert c.post("/api/creds").status_code == 200                                              # curl, the hub
    monkeypatch.setenv("STELTIC_HUB_TOKEN", "tok")
    assert c.post("/api/creds", headers={"x-steltic-hub-token": "tok"}).status_code == 200
    assert c.post("/api/creds", headers={"x-steltic-hub-token": "nope"}).status_code == 403


FAKE_SERVER = r'''
import json, os, sys
sys.path.insert(0, sys.argv[2])
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import local_guard
app = FastAPI()
local_guard.install(app)
seen = []

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.post("/api/creds")
async def creds(request: Request):
    seen.append({"path": "creds", "token": request.headers.get("x-steltic-hub-token")})
    return {"ok": True}

@app.post("/api/run")
async def run(request: Request):
    seen.append({"path": "run", "token": request.headers.get("x-steltic-hub-token")})
    def gen():
        yield "data: " + json.dumps({"type": "done", "ok": True}) + "\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")

@app.api_route("/api/echo", methods=["GET", "POST"])
async def echo(request: Request):
    seen.append({"path": "echo", "token": request.headers.get("x-steltic-hub-token"),
                 "origin": request.headers.get("origin")})
    return {"ok": True}

@app.get("/seen")
def get_seen():
    return {"seen": seen, "env_token": os.environ.get("STELTIC_HUB_TOKEN")}

uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
'''


def _free_port_range(n=4):
    for base in range(49500, 49900, n):
        ok = True
        for p in range(base, base + n):
            with socket.socket() as s:
                try:
                    s.bind(("127.0.0.1", p))
                except OSError:
                    ok = False
                    break
        if ok:
            return base
    pytest.skip("no free ports")


@pytest.fixture
def live_module(monkeypatch, tmp_path):
    """A real module server process started by ServerSupervisor, guarded like the real ones."""
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    m = Manifest.parse({"schema": 1, "id": "fakemod", "name": "Fake", "env": {"python": "3.12", "install": []},
                        "server": {"command": [str(script), "{port}", str(CATALOG / GUARDED[0])], "health": "/healthz",
                                   "cwd": str(tmp_path)},
                        "credentials": {"path": "/api/creds", "method": "POST"},
                        "tabs": [{"id": "design", "kind": "form",
                                  "run": {"kind": "http", "path": "/api/run", "stream": "sse"}}]}, "t")

    class Reg:
        def manifest(self, mid): return m
        def is_installed(self, mid): return True
        def name(self, mid): return "Fake"
        def module_root(self, mid): return tmp_path
        def checkout(self, mid): return tmp_path
        def known(self): return [m]

    monkeypatch.setattr(config, "PORTS_FILE", tmp_path / "ports.json")
    monkeypatch.setattr(config, "PORT_BASE", _free_port_range())
    monkeypatch.setattr(config, "PORT_SPAN", 4)
    monkeypatch.setattr(runners.envs, "env_ready", lambda mid: True)
    monkeypatch.setattr(runners.envs, "python_bin", lambda mid: pathlib.Path(sys.executable))
    runs = runners.JobRuns()
    sup = runners.ServerSupervisor(Reg(), connection=lambda: {"base_url": "http://127.0.0.1:9/v1", "api_key": "k",
                                                              "model": "MOCK"}, runs=runs)
    yield sup, m, runs
    sup.stop_all()


def test_the_hub_hands_each_server_a_token_and_sends_it_back(live_module, monkeypatch, tmp_path):
    import httpx
    sup, m, runs = live_module
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    url = asyncio.run(sup.ensure("fakemod"))
    seen = httpx.get(url + "/seen").json()
    assert seen["env_token"] == sup.token and len(sup.token) >= 32
    assert {"path": "creds", "token": sup.token} in seen["seen"]           # the credential push got through the guard

    async def drive():
        return [ev async for ev in runners.run_http(m, m.tabs[0], "J", {}, sup.registry, sup, runs, "r1")]
    out = asyncio.run(drive())
    assert any('"done"' in e for e in out)
    assert {"path": "run", "token": sup.token} in httpx.get(url + "/seen").json()["seen"]
    # the module's guard is live: a cross-site POST straight to the module is refused
    r = httpx.post(url + "/api/creds", content="{}", headers={"content-type": "text/plain", "origin": "https://evil.example"})
    assert r.status_code == 403
    # ... and a forged token is refused, whatever the Origin says
    assert httpx.post(url + "/api/echo", headers={"x-steltic-hub-token": "forged"}).status_code == 403
    # another launch of the hub has another token
    assert runners.ServerSupervisor(None).token != sup.token


def test_the_proxy_sends_the_hubs_token_never_the_browsers(live_module, monkeypatch):
    import httpx
    from fastapi.testclient import TestClient
    from steltic_hub import main as M
    sup, m, runs = live_module
    url = asyncio.run(sup.ensure("fakemod"))

    async def fake_ensure(mod_id, exclude_run=None):
        return url
    monkeypatch.setattr(M.SUP, "ensure", fake_ensure)
    with TestClient(M.app) as c:
        r = c.post("/m/fakemod/api/echo", headers={"origin": "http://127.0.0.1:8300", "x-steltic-hub-token": "from-page"})
        assert r.status_code == 403            # this hub's token is not the fake server's: the proxy replaced the page's
    monkeypatch.setattr(M.SUP, "token", sup.token)
    with TestClient(M.app) as c:
        r = c.post("/m/fakemod/api/echo", headers={"origin": "http://127.0.0.1:8300", "x-steltic-hub-token": "from-page"})
        assert r.status_code == 200
    last = httpx.get(url + "/seen").json()["seen"][-1]
    assert last == {"path": "echo", "token": sup.token, "origin": "http://127.0.0.1:8300"}


# ================================================================ HUB-05
class _FakeProc:
    def __init__(self): self.alive = True
    def poll(self): return None if self.alive else 0


def test_a_dependency_becoming_available_waits_for_the_run_to_finish(monkeypatch, tmp_path):
    """HR Steel started without the standards server; it is installed later. The next ensure (a second
    project's Design, a proxied page request) used to stop HR Steel at once -- killing the first
    project's design mid-run. Now the restart waits until no run uses the server."""
    srv = {"command": ["x"], "health": "/healthz"}

    def mod(mid, **server):
        return Manifest.parse({"schema": 1, "id": mid, "name": mid, "env": {"python": "3.12", "install": []},
                               "server": {**srv, **server},
                               "tabs": [{"id": "design", "kind": "form", "run": {"kind": "http", "path": "/api/run"}},
                                        {"id": "cli", "kind": "form", "run": {"kind": "cli", "command": ["-m", "x"]}}]}, "t")
    mods = {"hr": mod("hr", requires=["rag"]), "rag": mod("rag"), "loop": mod("loop", requires=["hr"]),
            "other": mod("other")}

    class Reg:
        def manifest(self, mid): return mods[mid]
        def is_installed(self, mid): return True
        def name(self, mid): return mid
        def known(self): return list(mods.values())

    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    runs = runners.JobRuns()
    sup = runners.ServerSupervisor(Reg(), runs=runs)
    sup.procs["hr"] = _FakeProc()
    sup.ports["hr"] = 8411
    sup.deps["hr"] = {"rag": None}                       # started while the standards server was missing
    stopped = []
    monkeypatch.setattr(sup, "stop", lambda mid: stopped.append(mid) or sup.procs.pop(mid, None))

    async def deps(mod_id, seen, exclude_run=None):
        return {"rag": "http://127.0.0.1:8420"} if mod_id == "hr" else {}
    monkeypatch.setattr(sup, "_ensure_required", deps)
    monkeypatch.setattr(runners.envs, "env_ready", lambda mid: False)   # the restart itself stops at "not installed"

    runs.began("r1", "hr", "design")                     # project A's design is running
    assert asyncio.run(sup.ensure("hr")) == "http://127.0.0.1:8411"
    assert stopped == [] and "deferred" in (tmp_path / "hr.server.log").read_text()
    # the run that asks is not "using" the server yet -- but another live run still is
    runs.began("r2", "hr", "design")
    assert asyncio.run(sup.ensure("hr", exclude_run="r2")) == "http://127.0.0.1:8411" and stopped == []
    runs.ended("r1"); runs.ended("r2")
    # a run of a module whose server drives HR Steel's (the Nonlinear loop) counts too, its CLI tab does not
    runs.began("r3", "loop", "design")
    assert sup._busy_runs("hr") == ["loop.design"]
    asyncio.run(sup.ensure("hr"))
    assert stopped == []
    runs.ended("r3")
    runs.began("r4", "loop", "cli")
    runs.began("r5", "other", "design")
    assert sup._busy_runs("hr") == []
    # idle: the deferred restart happens now
    with pytest.raises(runners.RunError):
        asyncio.run(sup.ensure("hr"))
    assert stopped == ["hr"]


# ================================================================ HUB-04
STAGE = {"from": "{src}", "to": "{dst}", "required": True, "label": "design package",
         "design_files": ["cfg.py", "model_*.py", "design/*"],
         "supersede": ["pushover", "nlrha", "four_analyses.html", "hinge_params*.json"]}


def _pkg(root: pathlib.Path, files: dict):
    if root.exists():
        import shutil
        shutil.rmtree(root)
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def _all_files(root: pathlib.Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_text() for p in root.rglob("*")
            if p.is_file() and p.name != runners.STAGE_RECORD}


def test_a_new_design_package_supersedes_the_old_one_and_its_analyses(tmp_path):
    run = Run.parse({"kind": "cli", "command": ["x"], "stage": [STAGE]})
    src, dst = tmp_path / "hr_job", tmp_path / "project"
    ctx = {"src": str(src), "dst": str(dst)}
    log = []
    _pkg(src, {"cfg.py": "W14X68", "model_opensees.py": "m1", "design/old_only.json": "{}", "report.html": "r1",
               "figs/a.png": "a"})
    runners.stage_inputs(run, ctx, log.append)
    assert (dst / runners.STAGE_RECORD).exists() and not (dst / runners.SUPERSEDED_DIR).exists()
    # the analyses of design 1, and a file the user dropped into the project
    (dst / "pushover").mkdir(); (dst / "pushover" / "po.json").write_text("po1")
    (dst / "four_analyses.html").write_text("cmp1")
    (dst / "hinge_params_collected.json").write_text("h1")
    (dst / "my_notes.txt").write_text("mine")
    before = _all_files(dst)

    # re-staging the same package changes nothing and moves nothing
    runners.stage_inputs(run, ctx, log.append)
    assert _all_files(dst) == before and not (dst / runners.SUPERSEDED_DIR).exists()

    # HR Steel only re-rendered its report: not a design change -- the analyses stay
    time.sleep(1.1)
    (src / "report.html").write_text("r1b")
    runners.stage_inputs(run, ctx, log.append)
    assert (dst / "report.html").read_text() == "r1b" and (dst / "pushover" / "po.json").exists()
    assert not (dst / runners.SUPERSEDED_DIR).exists()

    # HR Steel redesigned: new sections, design/old_only.json gone, figs/a.png gone
    _pkg(src, {"cfg.py": "W14X90", "model_opensees.py": "m2", "design/new.json": "{}", "report.html": "r2"})
    log.clear()
    runners.stage_inputs(run, ctx, log.append)
    now = _all_files(dst)
    assert now["cfg.py"] == "W14X90" and now["design/new.json"] == "{}" and now["my_notes.txt"] == "mine"
    for gone in ("design/old_only.json", "figs/a.png", "pushover/po.json", "four_analyses.html",
                 "hinge_params_collected.json"):
        assert gone not in now, gone                    # nothing of design 1 is left beside design 2
    aside = list((dst / runners.SUPERSEDED_DIR).iterdir())
    assert len(aside) == 1
    kept = _all_files(aside[0])
    # ... and nothing was deleted: design 1 and its analyses are together in _superseded/<time>/
    assert kept["cfg.py"] == "W14X68" and kept["model_opensees.py"] == "m1" and kept["design/old_only.json"] == "{}"
    assert kept["figs/a.png"] == "a" and kept["pushover/po.json"] == "po1" and kept["four_analyses.html"] == "cmp1"
    assert kept["hinge_params_collected.json"] == "h1" and "my_notes.txt" not in kept
    text = "\n".join(log)
    assert "moved" in text and "nothing deleted" in text and "re-run the analyses" in text


def test_an_edited_project_copy_is_kept_before_a_new_package_replaces_it(tmp_path):
    run = Run.parse({"kind": "cli", "command": ["x"], "stage": [STAGE]})
    src, dst = tmp_path / "hr_job", tmp_path / "project"
    ctx = {"src": str(src), "dst": str(dst)}
    _pkg(src, {"cfg.py": "A", "report.html": "r"})
    runners.stage_inputs(run, ctx, lambda *_: None)
    time.sleep(1.1)
    (dst / "report.html").write_text("edited by hand")
    (src / "report.html").write_text("r2")                      # a new report; the design is the same
    runners.stage_inputs(run, ctx, lambda *_: None)
    assert (dst / "report.html").read_text() == "r2"
    kept = _all_files(next((dst / runners.SUPERSEDED_DIR).iterdir()))
    assert kept == {"report.html": "edited by hand"}


def test_what_the_module_adds_to_a_package_file_is_not_a_new_design(tmp_path):
    """Seen on the saved Ex22 job: the Nonlinear module writes its DDM results into the project's
    design/calc_package.json. That project copy now differs from HR Steel's -- but HR Steel's design did
    not change, so a re-stage must not supersede hours of analyses (it copies the package file over the
    project copy, as it always has)."""
    run = Run.parse({"kind": "cli", "command": ["x"], "stage": [STAGE]})
    src, dst = tmp_path / "hr_job", tmp_path / "project"
    ctx = {"src": str(src), "dst": str(dst)}
    _pkg(src, {"cfg.py": "A", "design/calc_package.json": '{"members": 1}'})
    runners.stage_inputs(run, ctx, lambda *_: None)
    (dst / "design" / "calc_package.json").write_text('{"members": 1, "ddm_analysis": {}}')
    (dst / "pushover").mkdir(); (dst / "pushover" / "p.json").write_text("p")
    log = []
    runners.stage_inputs(run, ctx, log.append)
    assert (dst / "pushover" / "p.json").exists() and not (dst / runners.SUPERSEDED_DIR).exists()
    assert (dst / "design" / "calc_package.json").read_text() == '{"members": 1}'
    assert not any("moved" in l for l in log)


def test_a_project_staged_before_the_record_existed_is_handled(tmp_path):
    """Projects staged by an older hub have no record: project files that differ from the new package are
    kept aside (once); the analyses stay -- whether the design changed is unknown (the project's copy may
    differ only by what the module added) -- and the run log says to re-run them if it did."""
    run = Run.parse({"kind": "cli", "command": ["x"], "stage": [STAGE]})
    src, dst = tmp_path / "hr_job", tmp_path / "project"
    _pkg(dst, {"cfg.py": "OLD", "report.html": "old", "nlrha/r.json": "nl-old", "upload.zip": "u"})
    _pkg(src, {"cfg.py": "NEW", "report.html": "new"})
    log = []
    runners.stage_inputs(run, {"src": str(src), "dst": str(dst)}, log.append)
    now = _all_files(dst)
    assert now["cfg.py"] == "NEW" and now["nlrha/r.json"] == "nl-old" and now["upload.zip"] == "u"
    kept = _all_files(next((dst / runners.SUPERSEDED_DIR).iterdir()))
    assert kept == {"cfg.py": "OLD", "report.html": "old"}
    assert any("warning" in l and "cfg.py" in l and "nlrha" in l for l in log)
    # from now on the record exists: the next redesign supersedes the results
    _pkg(src, {"cfg.py": "NEWER", "report.html": "new"})
    runners.stage_inputs(run, {"src": str(src), "dst": str(dst)}, log.append)
    assert "nlrha/r.json" not in _all_files(dst)


def test_zip_packages_are_tracked_the_same_way(tmp_path):
    run = Run.parse({"kind": "cli", "command": ["x"], "stage": [STAGE]})
    dst = tmp_path / "project"

    def mkzip(name, files):
        z = tmp_path / name
        with zipfile.ZipFile(z, "w") as zf:
            for rel, text in files.items():
                zf.writestr("Pkg/" + rel, text)
        return z
    z1 = mkzip("p1.zip", {"cfg.py": "A", "design/x.json": "1", "extra.txt": "e"})
    runners.stage_inputs(run, {"src": str(z1), "dst": str(dst)}, lambda *_: None)
    (dst / "pushover").mkdir(); (dst / "pushover" / "p.json").write_text("p")
    runners.stage_inputs(run, {"src": str(z1), "dst": str(dst)}, lambda *_: None)    # same zip: no-op
    assert not (dst / runners.SUPERSEDED_DIR).exists()
    z2 = mkzip("p2.zip", {"cfg.py": "B", "design/x.json": "1"})
    runners.stage_inputs(run, {"src": str(z2), "dst": str(dst)}, lambda *_: None)
    now = _all_files(dst)
    assert now["cfg.py"] == "B" and "extra.txt" not in now and "pushover/p.json" not in now
    kept = _all_files(next((dst / runners.SUPERSEDED_DIR).iterdir()))
    assert kept == {"cfg.py": "A", "extra.txt": "e", "pushover/p.json": "p"}


def test_stage_globs_are_validated():
    def mk(**kw):
        return Run.parse({"kind": "cli", "command": ["x"], "stage": [{"from": "{a}", "to": "{b}", **kw}]})
    assert mk(supersede=["pushover", "hinge*.json"], design_files=["cfg.py"]).stage[0]["supersede"]
    for bad in ({"supersede": "pushover"}, {"supersede": ["../x"]}, {"supersede": ["/abs"]}, {"supersede": ["*"]},
                {"supersede": ["**"]}, {"design_files": [3]}, {"supersede": ["C:/x"]}):
        with pytest.raises(ManifestError):
            mk(**bad)


def test_every_nonlinear_design_package_stage_declares_what_it_supersedes():
    m = load_catalog(CATALOG)["steltic_nonlinear"]
    n = 0
    for t in m.tabs:
        for r in [t.run, *[a.run for a in t.actions]]:
            for s in (r.stage if r else []) or []:
                if s.get("from") == "{f.package}":
                    assert "cfg.py" in s["design_files"] and {"pushover", "nlrha"} <= set(s["supersede"])
                    n += 1
    assert n >= 6


# ================================================================ HUB-06
def test_absolute_file_fields_are_confined(monkeypatch, tmp_path):
    from steltic_hub.registry import Registry
    m = load_catalog(CATALOG)["steltic_nonlinear"]
    tab = next(t for t in m.tabs if t.id == "run")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "pkg.zip").write_bytes(b"PK")
    # the package is staged (copied into the project): outside the data folder it is refused
    for bad in (str(outside), str(outside / "pkg.zip"), "/", str(pathlib.Path.home())):
        with pytest.raises(runners.RunError, match="outside the project"):
            runners.build_ctx(m, "AbsJ", {"package": bad}, Registry(), tab=tab)
    # the field's own default (HR Steel's output folder) and anything under the data folder are fine
    ctx = runners.build_ctx(m, "AbsJ", {}, Registry(), tab=tab)
    assert ctx["_fields"]["package"] == ctx["out.steltic"]
    assert runners.build_ctx(m, "AbsJ", {"package": ctx["out.steltic"]}, Registry(), tab=tab)
    inside = config.DATA / "somewhere" / "pkg.zip"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_bytes(b"PK")
    assert runners.build_ctx(m, "AbsJ", {"package": str(inside)}, Registry(), tab=tab)["_fields"]["package"] == str(inside)
    # a field that is only read (Admin's standards queue converts PDFs from a folder the user picked)
    # may name one existing file outside -- never a folder, never a file that is not there
    conv = load_catalog(CATALOG)["steltic_grokbot"]
    ctab = next(t for t in conv.tabs if t.id == "convert")
    pdf = outside / "A360.pdf"
    pdf.write_bytes(b"%PDF")
    assert runners.build_ctx(conv, "Std", {"pdf": str(pdf)}, Registry(), tab=ctab)["_fields"]["pdf"] == str(pdf)
    for bad in (str(outside), str(outside / "missing.pdf")):
        with pytest.raises(runners.RunError):
            runners.build_ctx(conv, "Std", {"pdf": bad}, Registry(), tab=ctab)


def test_a_folder_is_never_staged_into_itself(tmp_path):
    run = Run.parse({"kind": "cli", "command": ["x"], "stage": [{"from": "{src}", "to": "{dst}"}]})
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("a")
    with pytest.raises(runners.RunError, match="into itself"):
        runners.stage_inputs(run, {"src": str(src), "dst": str(src / "project")}, lambda *_: None)
    assert [p.name for p in src.iterdir()] == ["a.txt"]
