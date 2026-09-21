"""Smoke tests for the hub. No module installs needed except where marked.

    python -m pytest tests -q
"""
import json, os, pathlib, tempfile, sys
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("STELTIC_HUB_DATA", tempfile.mkdtemp(prefix="stelticthub-test-"))

from steltic_hub.manifest import Manifest, ManifestError, load_catalog   # noqa: E402
from steltic_hub import config, jobs, runners                            # noqa: E402


# ---------------------------------------------------------------- catalog
def test_every_bundled_manifest_parses():
    cat = load_catalog(config.CATALOG_DIR)
    assert set(cat) == {"steltic", "steltic_cfs", "steltic_nonlinear", "steltic_grokbot", "steltic_variations",
                        "steltic_probabilistic", "steltic_admin"}
    for m in cat.values():
        assert m.tabs and m.name
        assert m.git or m.bundled
    v = cat["steltic_variations"]
    assert v.bundled == "steltic_variations" and (config.CATALOG_DIR / v.bundled / "steltic_module.json").is_file()
    assert v.server.get("requires") == ["steltic"] and v.env_vars["STELTIC_URL"] == "{server.steltic}"
    pr = cat["steltic_probabilistic"]
    assert pr.bundled == "steltic_probabilistic" and pr.needs == ["steltic", "steltic_nonlinear"]
    assert pr.env_vars["DDM_PYTHON"] == "{python.steltic_nonlinear}" and pr.env_vars["STELTIC_ENGINE_DIR"] == "{need.steltic}/steel_engine"
    for f in ("probabilistic/worker.py", "probabilistic/main.py", "probabilistic/ui/index.html", "requirements.txt"):
        assert (config.CATALOG_DIR / pr.bundled / f).is_file()


def test_no_module_specific_code_in_the_hub():
    """The hub must never BRANCH on a module id -- that is the whole modularity claim.

    Prose is fine (envs.py explains the steltic/steltic_cfs package collision at length); what
    must not exist is executable code that treats one module differently from another.
    """
    import ast, re
    offenders = []
    ids = ("steltic_cfs", "steltic_nonlinear", "steltic_grokbot", "grokbot", "nlrha", "pushover")

    for p in config.PKG.glob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        # every string literal that is not a docstring
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                d = ast.get_docstring(node, clean=False)
                if d is not None:
                    docstrings.add(d)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in docstrings:
                    continue
                for mid in ids:
                    if mid in node.value:
                        offenders.append(f"{p.name}:{node.lineno} literal {node.value[:40]!r}")

    for p in (config.PKG / "ui").glob("*.js"):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            code = re.sub(r"//.*$", "", line)
            if code.strip().startswith("*"):
                continue
            for mid in ids:
                if mid in code:
                    offenders.append(f"{p.name}:{i} {code.strip()[:60]}")

    assert not offenders, offenders


def test_tabs_declare_runnable_work_or_are_passive():
    for m in load_catalog(config.CATALOG_DIR).values():
        for t in m.tabs:
            if t.kind == "form":
                assert t.run, f"{m.id}/{t.id}: form tab with no run block"
            if t.kind == "viewers":
                assert m.viewers, f"{m.id}: viewers tab but no viewers declared"


def test_cli_tabs_only_reference_declared_fields():
    """A {f.x} in a command must correspond to a field, or the run silently gets an empty arg."""
    import re
    for m in load_catalog(config.CATALOG_DIR).values():
        for t in m.tabs:
            if not (t.run and t.run.kind == "cli"):
                continue
            ids = {f.id for f in t.fields}
            for ref in re.findall(r"\{f\.([A-Za-z0-9_]+)\}", json.dumps(t.run.command)):
                assert ref in ids, f"{m.id}/{t.id}: command references {{f.{ref}}} with no such field"


def test_needs_point_at_real_modules():
    cat = load_catalog(config.CATALOG_DIR)
    for m in cat.values():
        for n in m.needs:
            assert n in cat, f"{m.id} needs unknown module {n}"


def test_manifest_rejects_junk():
    with pytest.raises(ManifestError):
        Manifest.parse({"id": "x"})                       # no name
    with pytest.raises(ManifestError):
        Manifest.parse({"id": "x", "name": "X"})          # no tabs
    with pytest.raises(ManifestError):
        Manifest.parse({"id": "x", "name": "X", "schema": 99,
                        "tabs": [{"id": "a"}]})           # future schema
    with pytest.raises(ManifestError):
        Manifest.parse({"id": "x", "name": "X",
                        "tabs": [{"id": "a"}, {"id": "a"}]})   # duplicate tab ids


# ---------------------------------------------------------------- templating
def test_expand_substitutes_without_a_shell():
    ctx = {"job": "A", "job_dir": "/j/A", "f.brief": "rm -rf /; echo pwned"}
    out = runners.expand(["-m", "snl", "run", "{f.brief}", "--out", "{job_dir}"], ctx)
    assert out == ["-m", "snl", "run", "rm -rf /; echo pwned", "--out", "/j/A"]
    assert len(out) == 6                                  # one argv entry, not split by a shell


def test_cli_args_maps_fields_to_flags():
    m = load_catalog(config.CATALOG_DIR)["steltic_nonlinear"]
    tab = next(t for t in m.tabs if t.id == "run")
    args = runners.cli_args(tab, {"risk_category": "IV", "n_records": 11, "only": ""}, {})
    assert "--risk-category" in args and "IV" in args
    assert "--only" not in args                           # empty values are dropped


def test_flag_if_true_style():
    m = load_catalog(config.CATALOG_DIR)["steltic_nonlinear"]
    tab = next(t for t in m.tabs if t.id == "mesh")
    assert "--dry-run" in runners.cli_args(tab, {"dry_run": True}, {})
    assert "--dry-run" not in runners.cli_args(tab, {"dry_run": False}, {})


# ---------------------------------------------------------------- jobs
def test_job_names_are_sanitised():
    assert jobs.clean_name("../../etc/passwd") == "etc_passwd"
    assert jobs.clean_name("") == "Project"
    assert jobs.clean_name("27 High St!") == "27_High_St"


def test_job_paths_cannot_escape():
    name = "pytest_escape_tmp"
    jobs.job_dir(name)
    try:
        with pytest.raises(PermissionError):
            jobs.resolve_in_job(name, "../../../../etc/passwd")
        inside = jobs.resolve_in_job(name, "design/calc_package.json")
        assert name in str(inside)
    finally:
        jobs.delete_job(name)          # never leave a fake project in the user's list


# ---------------------------------------------------------------- isolation
def test_the_two_design_modules_would_collide_in_one_env():
    """The reason envs.py creates one venv per module. If this ever fails, the two repos have
    stopped sharing package names and the isolation could in principle be relaxed."""
    cat = load_catalog(config.CATALOG_DIR)
    a, b = cat["steltic"], cat["steltic_cfs"]
    assert a.install == b.install == ["-e", "."]
    from steltic_hub import envs
    assert envs.env_dir("steltic") != envs.env_dir("steltic_cfs")


def test_module_output_roots_do_not_overlap():
    cat = load_catalog(config.CATALOG_DIR)
    roots = {m.id: m.output_root for m in cat.values()}
    assert roots["steltic"] != roots["steltic_cfs"]


# ---------------------------------------------------------------- app
def test_app_imports_and_routes_exist():
    from steltic_hub.main import app
    paths = {getattr(r, "path", "") for r in app.routes}
    for p in ("/api/state", "/api/connection", "/api/run/{mod_id}/{tab_id}",
              "/out/{mod_id}/{job}/{path:path}", "/m/{mod_id}/{path:path}", "/healthz"):
        assert p in paths, f"missing route {p}"


# ---------------------------------------------------------------- local development
def test_templating_does_not_create_project_folders():
    """Resolving {job_dir} must not litter the projects list -- only a real run creates one."""
    before = {p.name for p in config.JOBS_DIR.iterdir()} if config.JOBS_DIR.exists() else set()
    jobs.job_path("NeverRun")
    after = {p.name for p in config.JOBS_DIR.iterdir()} if config.JOBS_DIR.exists() else set()
    assert before == after


def test_link_uses_the_working_copy_as_the_checkout(tmp_path):
    from steltic_hub.registry import Registry
    reg = Registry()
    src = tmp_path / "my_working_copy"
    src.mkdir()
    (src / "steltic_module.json").write_text(json.dumps({
        "schema": 1, "id": "linked_demo", "name": "Linked Demo",
        "tabs": [{"id": "run", "kind": "form",
                  "run": {"kind": "cli", "command": ["-c", "print(1)"]}}]}))
    reg.link("linked_demo", str(src))
    try:
        assert reg.checkout("linked_demo") == src.resolve()
        assert reg.local_source("linked_demo") == src.resolve()
        # linking a self-describing folder also registers it
        assert "linked_demo" in {m.id for m in reg.known()}
        assert reg.status("linked_demo")["linked"] == str(src.resolve())
        # never fetched
        assert reg.check_update("linked_demo")["behind"] is False
    finally:
        reg.unlink("linked_demo")
        reg.state.get("custom_catalog", {}).pop("linked_demo", None)
        reg.save()
    assert reg.local_source("linked_demo") is None
    assert src.exists(), "unlink must never touch the working copy"


def test_link_rejects_a_mismatched_id(tmp_path):
    from steltic_hub.registry import Registry, RegistryError
    reg = Registry()
    src = tmp_path / "wc"; src.mkdir()
    (src / "steltic_module.json").write_text(json.dumps({
        "schema": 1, "id": "actual_id", "name": "X",
        "tabs": [{"id": "a", "kind": "files"}]}))
    with pytest.raises(RegistryError):
        reg.link("what_i_typed", str(src))


def test_link_rejects_a_missing_directory():
    from steltic_hub.registry import Registry, RegistryError
    with pytest.raises(RegistryError):
        Registry().link("steltic", "/no/such/path/anywhere")


# ---------------------------------------------------------------- windowless start
def test_survives_having_no_stdout_or_stderr():
    """pythonw.exe (the launcher's default) gives the process sys.stdout is None AND
    sys.stderr is None. uvicorn's DefaultFormatter calls sys.stdout.isatty() while building its
    logging config, so dictConfig raises and the server dies before it binds the port -- with no
    console to report it. Caught on Windows, reproduced here, and this is the guard."""
    import logging.config
    from uvicorn.config import LOGGING_CONFIG
    from steltic_hub import cli

    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout = None
    sys.stderr = None
    try:
        # without the fix, this is the exact failure
        with pytest.raises(ValueError):
            logging.config.dictConfig(dict(LOGGING_CONFIG))
        # with it, logging configures cleanly and both streams are real files
        path = cli._ensure_streams()
        assert sys.stdout is not None and sys.stderr is not None
        logging.config.dictConfig(dict(LOGGING_CONFIG))
        assert path is not None and pathlib.Path(path).name == "hub.log"
    finally:
        sys.stdout, sys.stderr = real_out, real_err


def test_ensure_streams_is_a_no_op_with_a_console():
    from steltic_hub import cli
    assert cli._ensure_streams() is None


def test_declared_needs_are_checked_before_a_run():
    """steltic_nonlinear points STELTIC_ENGINE_DIR at {need.steltic}/steel_engine. With HR Steel
    missing that expands to a path that does not exist and the DDM step fails ~40 minutes in, so
    the hub refuses the run up front instead."""
    from steltic_hub.main import app
    from fastapi.testclient import TestClient
    from steltic_hub.registry import Registry

    reg = Registry()
    if reg.is_installed("steltic"):
        pytest.skip("HR Steel is installed here, so there is nothing to refuse")
    with TestClient(app) as c:
        r = c.post("/api/run/steltic_nonlinear/run",
                   json={"job": "NeedsCheck", "fields": {"package": "x.zip"}})
        body = r.text
    assert "needs HR Steel" in body, body
    assert '"ok": false' in body.lower()


def test_optional_component_is_checked_before_a_run(monkeypatch):
    """The Query file manager's Convert tab runs Docling, an optional component (a large ML stack). Without it the
    run used to die on `ModuleNotFoundError: docling` inside the script; the hub now refuses up front and names the
    install button."""
    from steltic_hub import envs, main as M
    from steltic_hub.registry import Registry
    from fastapi.testclient import TestClient
    m = Registry().manifest("steltic_grokbot")
    conv = next(t for t in m.tabs if t.id == "convert")
    assert conv.requires_optional == ["converter"] and "converter" in m.optional
    monkeypatch.setattr(M.REG, "is_installed", lambda mid: True)
    monkeypatch.setattr(envs, "optional_present", lambda m_, g: False)
    with TestClient(M.app) as c:
        body = c.post("/api/run/steltic_grokbot/convert", json={"job": "OptCheck", "fields": {"pdf": "A360-22.pdf"}}).text
    assert "PDF converter (Docling 2.123.1)" in body and "Modules tab" in body and '"ok": false' in body.lower()


def test_optional_probe_checks_every_import(monkeypatch, tmp_path):
    """Docling imports fine without onnxruntime and then fails on the first OCR page (`ImportError: onnxruntime is not
    installed.`), so the converter group probes docling, rapidocr AND onnxruntime, and the group installs the
    docling[rapidocr] extra that brings the runtime along."""
    from steltic_hub import envs
    from steltic_hub.registry import Registry
    m = Registry().manifest("steltic_grokbot")
    conv = m.optional["converter"]
    assert conv["requirements"] == ["docling[rapidocr]==2.123.1"]
    assert envs.probe_imports(conv) == ["docling", "rapidocr", "onnxruntime"]
    assert envs.probe_imports({"probe_import": "docling, onnxruntime"}) == ["docling", "onnxruntime"]
    assert envs.probe_imports({}) == []
    # the probe is one interpreter call importing every name; a missing one makes the group "absent"
    seen = {}
    monkeypatch.setattr(envs, "env_ready", lambda mid: True)
    monkeypatch.setattr(envs, "python_bin", lambda mid: sys.executable)
    class R: returncode = 1
    def fake_run(cmd, **kw):
        seen["code"] = cmd[-1]; return R()
    monkeypatch.setattr(envs.subprocess, "run", fake_run)
    envs._OPTIONAL_CACHE.pop((m.id, "converter"), None)
    assert envs.optional_present(m, "converter") is False
    assert seen["code"] == "import docling, rapidocr, onnxruntime"
    envs._OPTIONAL_CACHE.pop((m.id, "converter"), None)


def test_healthz_says_who_is_running_and_whether_the_code_moved_on(monkeypatch, tmp_path):
    """The hub is single-instance: a relaunch raises a window on the running process -- which, after a git pull or
    an edit, is the OLD version. /healthz now carries pid + started + stale so the launcher (and the window's
    banner) can tell, and restart instead of reusing it."""
    from steltic_hub import main as M
    from fastapi.testclient import TestClient
    with TestClient(M.app) as c:
        h = c.get("/healthz").json()
    assert h["ok"] and h["pid"] == os.getpid() and h["started"] <= __import__("time").time()
    assert isinstance(h["stale"], bool) and h["restartable"] is False        # no server handle under TestClient
    # staleness = any source file newer than the start time (pyproject.toml included), __pycache__ ignored
    assert M.source_stale(since=0) is True
    assert M.source_stale(since=__import__("time").time() + 60) is False
    pyc = M.config.PKG / "__pycache__" / "zz_probe.pyc"
    try:
        pyc.parent.mkdir(exist_ok=True); pyc.write_bytes(b"x")
        assert M.source_stale(since=__import__("time").time() + 60) is False
    finally:
        pyc.unlink(missing_ok=True)
    # touching a source file makes the running process stale
    monkeypatch.setattr(M, "STARTED", __import__("time").time() + 60)
    with TestClient(M.app) as c:
        assert c.get("/healthz").json()["stale"] is False and c.get("/api/state").json()["hub"]["stale"] is False
    monkeypatch.setattr(M, "STARTED", 0.0)
    with TestClient(M.app) as c:
        assert c.get("/healthz").json()["stale"] is True


def test_restart_spawns_a_successor_then_stops(monkeypatch):
    """/api/hub/restart: spawn `steltic_hub.cli --replace <pid>` on the same port (detached), then set the uvicorn
    server's should_exit so the lifespan hook stops module servers and frees the port for the successor.
    /api/hub/shutdown is the same without the successor. Both refuse when no server handle exists."""
    from steltic_hub import main as M
    from fastapi.testclient import TestClient
    with TestClient(M.app) as c:
        assert c.post("/api/hub/restart").status_code == 409 and c.post("/api/hub/shutdown").status_code == 409
    class Server: should_exit = False
    spawned = {}
    class P:
        pid = 4242
    def fake_popen(argv, **kw):
        spawned["argv"] = argv; spawned["kw"] = kw; return P()
    monkeypatch.setattr(M.subprocess, "Popen", fake_popen)
    M.app.state.server, M.app.state.port, M.app.state.url = Server(), 8377, "http://127.0.0.1:8377"
    try:
        with TestClient(M.app) as c:
            r = c.post("/api/hub/restart").json()
            assert r["ok"] and r["pid"] == os.getpid() and r["successor"] == 4242
            assert c.get("/healthz").json()["restartable"] is True
        argv = spawned["argv"]
        assert argv[0] == sys.executable and argv[1:3] == ["-m", "steltic_hub.cli"]
        assert argv[argv.index("--port") + 1] == "8377" and argv[argv.index("--replace") + 1] == str(os.getpid())
        assert "--no-browser" in argv and spawned["kw"]["close_fds"] is True
        deadline = __import__("time").time() + 5
        while not M.app.state.server.should_exit and __import__("time").time() < deadline:
            __import__("time").sleep(0.05)
        assert M.app.state.server.should_exit is True                       # after the 0.3 s grace
        M.app.state.server.should_exit = False
        with TestClient(M.app) as c:
            assert c.post("/api/hub/shutdown").json()["ok"] is True
        assert M.app.state.server.should_exit is True
    finally:
        del M.app.state.server; del M.app.state.port; del M.app.state.url


def test_shutdown_only_retires_its_own_url_marker(monkeypatch, tmp_path):
    """The successor may already have written hub.url by the time the old hub's lifespan hook runs."""
    from steltic_hub import main as M
    from fastapi.testclient import TestClient
    marker = tmp_path / "hub.url"
    monkeypatch.setattr(M.config, "URL_FILE", marker)
    marker.write_text("http://127.0.0.1:8300", encoding="utf-8")
    M.app.state.url = "http://127.0.0.1:8300"
    try:
        with TestClient(M.app):
            pass
        assert not marker.exists()                                          # ours: retired
        marker.write_text("http://127.0.0.1:8300", encoding="utf-8")
        M.app.state.url = "http://127.0.0.1:8301"                            # the marker is the successor's
        with TestClient(M.app):
            pass
        assert marker.read_text(encoding="utf-8") == "http://127.0.0.1:8300"
    finally:
        del M.app.state.url


def test_cli_waits_for_a_port_to_free():
    import socket, threading
    from steltic_hub import cli
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1); port = s.getsockname()[1]
    assert cli._port_free(port) is False
    threading.Timer(0.4, s.close).start()
    assert cli._wait_port_free(port, 5) is True
    assert cli._hub_info("http://127.0.0.1:1") is None and cli._ask_hub_to_stop("http://127.0.0.1:1") is False


def test_smart_app_control_is_reported_and_explained(monkeypatch, tmp_path):
    """Windows 11 Smart App Control blocks unsigned native libraries (PyTorch, OpenSees) with WinError 4551 --
    a 40-line traceback ending in `OSError: [WinError 4551] An Application Control policy has blocked this file`.
    The hub reads the policy state, reports it on /api/state and doctor, and a run whose output shows the
    block gets one explanatory error line before its done event."""
    from steltic_hub import sac, main as M
    from fastapi.testclient import TestClient
    assert sac.state() is None or sac.state() in ("off", "on", "evaluation")   # None off Windows
    assert sac.looks_blocked('OSError: [WinError 4551] An Application Control policy has blocked this file. Error loading "torch_python.dll"')
    assert not sac.looks_blocked("wrote design_criteria_16_1_4.docx")
    assert "Smart App Control settings" in sac.hint("on") and "one-way" in sac.hint("on")
    assert "evaluation" in sac.hint("evaluation") and "WDAC" in sac.hint("off")
    monkeypatch.setattr(sac, "state", lambda: "on")
    with TestClient(M.app) as c:
        assert c.get("/api/state").json()["smart_app_control"] == "on"
    # a CLI run whose module prints the 4551 traceback: the stream carries the explanation, then done
    from steltic_hub import runners
    from steltic_hub.manifest import Manifest
    import asyncio
    script = tmp_path / "blocked.py"
    script.write_text('import sys\nprint("loading torch")\nprint("OSError: [WinError 4551] An Application Control policy has blocked this file.")\nsys.exit(1)\n', encoding="utf-8")
    m = Manifest.parse({"schema": 1, "id": "sacmod", "name": "SAC", "env": {"python": "3.12", "install": []},
                        "tabs": [{"id": "run", "kind": "form", "run": {"kind": "cli", "cwd": "{job_dir}", "command": [str(script)]}}]}, "t")
    class Reg:
        def is_installed(self, mid): return True
        def name(self, mid): return "SAC"
        def module_root(self, mid): return tmp_path
    monkeypatch.setattr(runners.envs, "python_bin", lambda mid: pathlib.Path(sys.executable))
    monkeypatch.setattr(runners.envs, "env_ready", lambda mid: True)
    monkeypatch.setattr(runners, "build_ctx", lambda *a, **k: {"job_dir": str(tmp_path), "jobs_dir": str(tmp_path), "module_dir": str(tmp_path),
                                                                "data_dir": str(tmp_path), "job": "J", "port": "0", "needs": {}, "python": {}, "out": {}, "server": {}, "f": {}})
    async def collect():
        out = []
        async for ev in runners.run_cli(m, m.tabs[0], "J", {}, Reg(), runners.JobRuns(), "r1"):
            out.append(ev)
        return out
    events = asyncio.run(collect())
    texts = "\n".join(e if isinstance(e, str) else str(e) for e in events)
    assert "WinError 4551" in texts and "Smart App Control settings" in texts
    assert texts.index("Smart App Control settings") > texts.index("WinError 4551")
    assert '"type": "done"' in texts and texts.rindex('"type": "done"') > texts.index("Smart App Control settings")


def test_requires_optional_must_name_a_declared_group():
    from steltic_hub.manifest import Manifest, ManifestError
    base = {"schema": 1, "id": "x", "name": "X", "env": {"python": "3.12", "install": [], "optional": {"conv": {"label": "C", "requirements": ["c"], "probe_import": "c"}}},
            "tabs": [{"id": "t", "kind": "form", "run": {"kind": "cli", "command": ["-c", "pass"]}, "requires_optional": ["conv"]}]}
    Manifest.parse(base, "t")
    bad = json.loads(json.dumps(base)); bad["tabs"][0]["requires_optional"] = ["nope"]
    with pytest.raises(ManifestError):
        Manifest.parse(bad, "t")


def test_state_reports_missing_dependencies():
    from steltic_hub.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        mods = {m["id"]: m for m in c.get("/api/state").json()["modules"]}
    assert "missing_needs" in mods["steltic_nonlinear"]
    assert mods["steltic"]["missing_needs"] == []      # HR Steel needs nothing


# ---------------------------------------------------------------- templating (single pass, typed values)
def test_expand_is_single_pass_and_leaves_unknown_placeholders():
    ctx = {"job": "A", "f.brief": "keep {job} literal", "flag": True, "n": 0}
    assert runners.expand("{f.brief}", ctx) == "keep {job} literal"      # a value is never re-expanded
    assert runners.expand("{nope}/{job}", ctx) == "{nope}/A"
    assert runners.expand(["{flag}", "{n}"], ctx) == ["true", "0"]
    assert runners.expand({"k": {"v": "{job}"}}, ctx) == {"k": {"v": "A"}}


def test_out_templates_point_at_each_modules_output_root():
    from steltic_hub.registry import Registry
    reg = Registry()
    m = load_catalog(config.CATALOG_DIR)["steltic_nonlinear"]
    ctx = runners.base_ctx(m, "Tower", reg)
    assert ctx["out.steltic_nonlinear"] == str(jobs.job_path("Tower"))
    assert pathlib.Path(ctx["out.steltic"]) == config.DATA / "modules_data" / "steltic" / "sessions" / "local" / "jobs" / "Tower"
    assert ctx["need.steltic"] == str(reg.module_root("steltic"))


def test_python_template_names_a_needed_modules_interpreter(monkeypatch):
    """{python.<need>}: the interpreter of a needed module's environment, present only once that
    environment exists -- a module that runs its jobs inside another module's venv uses it."""
    from steltic_hub.registry import Registry
    from steltic_hub import envs
    reg = Registry()
    m = load_catalog(config.CATALOG_DIR)["steltic_probabilistic"]
    ctx = runners.base_ctx(m, "Tower", reg)
    assert "python.steltic_nonlinear" not in ctx                          # not installed here
    monkeypatch.setattr(envs, "env_ready", lambda mid: mid == "steltic_nonlinear")
    ctx = runners.base_ctx(m, "Tower", reg)
    assert ctx["python.steltic_nonlinear"] == str(envs.python_bin("steltic_nonlinear"))
    assert "python.steltic" not in ctx
    env = runners.expand(m.env_vars, ctx)
    assert env["DDM_PYTHON"] == str(envs.python_bin("steltic_nonlinear")) and env["STELTIC_ENGINE_DIR"].endswith("steel_engine")


def test_field_values_resolve_files_inside_the_project_and_keep_zero():
    from steltic_hub.registry import Registry
    m = load_catalog(config.CATALOG_DIR)["steltic_nonlinear"]
    tab = next(t for t in m.tabs if t.id == "run")
    ctx = runners.build_ctx(m, "FV", {"package": "pkg.zip", "n_records": 0, "dt": "", "parallel": "3"},
                            Registry(), tab=tab)
    vals = ctx["_fields"]
    assert pathlib.Path(vals["package"]) == (jobs.job_path("FV") / "pkg.zip").resolve()   # uploads land in the project folder
    assert vals["n_records"] == 0                                        # 0 is a value, not "empty"
    assert vals["dt"] == 0.01                                            # "" -> the field default
    assert vals["parallel"] == 3
    # the package default is another module's output folder
    ctx2 = runners.build_ctx(m, "FV", {}, Registry(), tab=tab)
    assert ctx2["_fields"]["package"] == ctx2["out.steltic"]
    with pytest.raises(runners.RunError):
        runners.build_ctx(m, "FV", {"package": "../../etc/passwd"}, Registry(), tab=tab)


def test_cli_args_keeps_zero_and_expands_lists():
    from steltic_hub.manifest import Tab
    tab = Tab.parse({"id": "t", "run": {"kind": "cli", "command": ["x"]}, "fields": [
        {"id": "tol", "type": "number", "arg": "--tol"},
        {"id": "many", "type": "files", "arg": "--in"},
        {"id": "on", "type": "checkbox", "arg": "--on", "arg_style": "flag-if-true"},
        {"id": "pos", "type": "text", "arg": "ignored", "arg_style": "positional"}]})
    ctx = {"job": "J", "_fields": {"tol": 0, "many": ["/a", "/b"], "on": True, "pos": "P"}}
    assert runners.cli_args(tab, {}, ctx) == ["--tol", "0", "--in", "/a", "/b", "--on", "P"]
    ctx["_fields"] = {"tol": "", "many": [], "on": False, "pos": None}
    assert runners.cli_args(tab, {}, ctx) == []


# ---------------------------------------------------------------- staging inputs
def test_stage_copies_folders_unpacks_zips_and_refuses_escapes(tmp_path):
    import zipfile
    from steltic_hub.manifest import Run
    src = tmp_path / "design"; (src / "design").mkdir(parents=True)
    (src / "report.html").write_text("r"); (src / "design" / "calc.json").write_text("{}")
    dst = tmp_path / "job"
    log = []
    run = Run.parse({"kind": "cli", "command": ["x"], "stage": [{"from": "{src}", "to": "{dst}", "required": True}]})
    runners.stage_inputs(run, {"src": str(src), "dst": str(dst)}, log.append)
    assert (dst / "report.html").read_text() == "r" and (dst / "design" / "calc.json").exists()

    z = tmp_path / "Pkg.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Pkg/cfg.py", "cfg")                        # one wrapping folder -> flattened
        zf.writestr("Pkg/design/x.csv", "1")
        zf.writestr("../evil.txt", "no")                        # zip-slip -> dropped
    dst2 = tmp_path / "job2"
    runners.stage_inputs(run, {"src": str(z), "dst": str(dst2)}, log.append)
    assert (dst2 / "cfg.py").read_text() == "cfg" and (dst2 / "design" / "x.csv").exists()
    assert not (tmp_path / "evil.txt").exists() and not (dst2 / "evil.txt").exists()

    # a zip already inside the project is unpacked in place; a folder that IS the project is a no-op
    runners.stage_inputs(run, {"src": str(dst2), "dst": str(dst2)}, log.append)
    assert any("already the project folder" in l for l in log)
    with pytest.raises(runners.RunError):
        runners.stage_inputs(run, {"src": str(tmp_path / "missing"), "dst": str(dst2)}, log.append)
    optional = Run.parse({"kind": "cli", "command": ["x"], "stage": [{"from": "{src}", "to": "{dst}"}]})
    runners.stage_inputs(optional, {"src": "", "dst": str(dst2)}, log.append)   # empty -> skipped quietly


# ---------------------------------------------------------------- names shared with the modules
def test_job_names_match_what_the_design_servers_keep():
    """The design servers keep only [A-Za-z0-9_-] and DROP the rest. A name the hub allowed but a
    module rewrote would put that module's outputs in a folder the hub never looks in."""
    for raw in ("Ex7a.v2", "27 High St!", "Café tower", "a/b\\c", "..hidden"):
        n = jobs.clean_name(raw)
        assert n == "".join(c for c in n if c.isalnum() or c in "-_"), n
    assert jobs.clean_name("Ex7a.v2") == "Ex7a_v2"
    assert jobs.clean_filename("My Tower (1).zip") == "My_Tower_1.zip"
    assert jobs.clean_filename("../../x.json") == "x.json"
    assert jobs.clean_filename("noext") == "noext"


# ---------------------------------------------------------------- SSE relay
def test_sse_parser_survives_split_multibyte_and_comments():
    p = runners._SSEParser()
    payload = ("data: " + json.dumps({"type": "token", "text": "ünï"}) + "\n\n").encode("utf-8")
    evs = list(p.feed(payload[:9])) + list(p.feed(payload[9:12])) + list(p.feed(payload[12:]))
    assert evs == [{"type": "token", "text": "ünï"}]
    assert list(p.feed(b": ping\n\n")) == [None]
    assert list(p.feed(b"data: not json\n\n")) == [{"type": "log", "text": "not json"}]


# ---------------------------------------------------------------- catalog contract
def test_design_modules_can_be_stopped_and_fill_their_briefs():
    cat = load_catalog(config.CATALOG_DIR)
    for mid in ("steltic", "steltic_cfs"):
        m = cat[mid]
        for t in m.tabs:
            if t.run and t.run.kind == "http":
                assert t.run.cancel.get("path"), f"{mid}/{t.id}: an http run needs a cancel spec for Stop"
        design = next(t for t in m.tabs if t.id == "design")
        fills = [f for f in design.fields if f.fills]
        assert fills and fills[0].fills["target"] in {f.id for f in design.fields}
        assert any(f.type == "attachments" for f in design.fields)


def test_manifest_rejects_dangling_references():
    with pytest.raises(ManifestError):
        Manifest.parse({"id": "x", "name": "X", "tabs": [{"id": "a", "kind": "form",
                        "run": {"kind": "cli", "command": ["x"]},
                        "fields": [{"id": "s", "type": "select", "fills": {"target": "nope"}}]}]})
    with pytest.raises(ManifestError):
        Manifest.parse({"id": "x", "name": "X", "tabs": [{"id": "a", "kind": "form"}]})   # form, nothing to run
    with pytest.raises(ManifestError):
        Manifest.parse({"id": "x", "name": "X", "tabs": [{"id": "a", "kind": "embed"}]})  # embed, no server
    with pytest.raises(ManifestError):
        Manifest.parse({"id": "bad id!", "name": "X", "tabs": [{"id": "a", "kind": "files"}]})


# ---------------------------------------------------------------- API
def test_projects_can_be_created_and_required_fields_are_checked():
    from steltic_hub.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        r = c.post("/api/jobs/My%20Tower%20(2)")
        assert r.json()["name"] == "My_Tower_2"
        assert "My_Tower_2" in [j["name"] for j in c.get("/api/state").json()["jobs"]]
        body = c.post("/api/run/steltic/design", json={"job": "My_Tower_2", "fields": {"brief": ""}}).text
        assert "is required" in body or "not installed" in body
        st = c.get("/api/state").json()
        assert "server_url" in st["modules"][0] and "running" in st
        c.delete("/api/jobs/My_Tower_2")
        assert "My_Tower_2" not in [j["name"] for j in c.get("/api/state").json()["jobs"]]


# ---------------------------------------------------------------- grounding bridge (a catalog asset)
def _bridge():
    import importlib.util
    p = config.CATALOG_DIR / "steltic_grokbot" / "rag_server.py"
    spec = importlib.util.spec_from_file_location("rag_server", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bridge_maps_the_agents_collections_onto_the_corpus():
    b = _bridge()
    assert b.map_collection("engineering_standards_A360") == ("spec", "AISC_360_22")
    assert b.map_collection("engineering_standards_S400") == ("spec", "AISI_S400_20")
    assert b.map_collection("steel_design_examples") == ("phase2", "examples", "steel_design_examples")
    assert b.map_collection("openseespy_documentation") == ("phase2", "opensees", "openseespy_documentation")
    assert b.map_collection("engineering_standards_ASCE41") == ("spec", "ASCE_41_23")
    assert b.map_collection("nonsense") is None


def test_bridge_shapes_hits_the_way_the_agents_render_them():
    b = _bridge()
    hit = {"doc": "AISC_360_22", "section_id": "F2.1", "title": "Yielding", "part": "standard",
           "text": "<!-- chunk_id: x --> <!-- meta: {} -->\nMn = Mp = Fy Zx (F2-1)", "printed_label": "16.1-47",
           "id": "F2.1", "authoritative": True, "score": -9.5, "neighbors": [{"section_id": "F2.2", "title": "LTB", "text": "..."}]}
    out = b.shape_hit(hit)
    assert out["text"].startswith("[AISC_360_22 F2.1 (standard)] Yielding\nMn = Mp")
    assert "chunk_id" not in out["text"] and "F2-1" in out["text"] and "-- F2.2 LTB" in out["text"]
    assert out["source"] == "AISC_360_22" and out["section"] == "F2.1" and out["page"] == "16.1-47"


def test_bridge_answers_empty_not_error_for_unknown_and_unconverted(tmp_path):
    """A non-2xx pauses the agents' run; anything the corpus cannot answer must be an empty list."""
    b = _bridge()
    root = tmp_path / "ws"
    (root / "scripts").mkdir(parents=True); (root / "documents" / "standards").mkdir(parents=True)
    (root / "scripts" / "retrieval.py").write_text(
        "class Corpus:\n"
        "    def __init__(self, root): self.doc_meta = {'opensees_documentation': {}}\n"
        "    def search(self, *a, **k): return {'found': False, 'hits': []}\n"
        "def resolve_doc(t): return t\n")
    br = b.Bridge(root, root / "scripts")
    assert br.query({"query": "x", "collection": "nonsense"})["results"] == []
    out = br.query({"query": "F2", "collection": "engineering_standards_A360", "clause": "F2"})
    assert out["results"] == [] and "AISC_360_22" in out["note"]
    out = br.query({"query": "eigen", "collection": "opensees_documentation"})
    assert out["results"] == [] and "missing" in out["note"]
    assert (root / "queue" / "agent_queries.jsonl").exists()
    # a stem the corpus holds under a name the fixed table does not know is still a valid collection
    (root / "scripts" / "retrieval.py").write_text(
        "class Corpus:\n"
        "    def __init__(self, root): self.doc_meta = {'IS_875_3': {}, 'ASCE_7_22': {}}\n"
        "    def search(self, *a, **k): return {'found': False, 'hits': []}\n"
        "def resolve_doc(t): return t\n")
    import sys as _sys
    _sys.modules.pop("retrieval", None)                 # the stub above is a different module now
    br = b.Bridge(root, root / "scripts")
    for name in ("engineering_standards_IS_875_3", "ASCE_7_22"):
        out = br.query({"query": "wind", "collection": name})
        assert "unknown collection" not in (out.get("note") or ""), name
    assert "unknown collection" in br.query({"query": "x", "collection": "engineering_standards_NOPE"})["note"]


def test_design_modules_ground_through_the_query_file_manager_server():
    cat = load_catalog(config.CATALOG_DIR)
    for mid in ("steltic", "steltic_cfs"):
        m = cat[mid]
        assert m.server.get("requires") == ["steltic_grokbot"]
        assert m.env_vars["RAG_API_URL"] == "{server.steltic_grokbot}/query"
    q = cat["steltic_grokbot"]
    assert q.name == "Query file manager" and q.has_server
    assert (config.CATALOG_DIR / "steltic_grokbot" / "rag_server.py").exists()
    assert any(t.kind == "embed" for t in q.tabs)


def test_server_dependencies_resolve_or_drop_the_variable():
    """{server.<id>} becomes the dependency's URL when it is up, and an env var that still points
    at a server that is not installed is left unset rather than passed as a broken URL."""
    from steltic_hub.registry import Registry
    from steltic_hub.runners import ServerSupervisor
    sup = ServerSupervisor(Registry())
    assert sup.requires("steltic") == ["steltic_grokbot"]
    assert sup.requires("steltic_grokbot") == []
    env_tpl = load_catalog(config.CATALOG_DIR)["steltic"].env_vars
    ctx = {"data_dir": "/d", "server.steltic_grokbot": "http://127.0.0.1:8411"}
    assert runners.expand(env_tpl, ctx)["RAG_API_URL"] == "http://127.0.0.1:8411/query"
    unresolved = runners.expand(env_tpl, {"data_dir": "/d"})["RAG_API_URL"]
    assert "{server." in unresolved                      # -> skipped by ServerSupervisor


# ---------------------------------------------------------------- names + connection on this PC
def test_modules_can_be_renamed_by_the_user():
    from steltic_hub.registry import Registry
    reg = Registry()
    try:
        assert reg.name("steltic_grokbot") == "Query file manager"
        assert reg.rename("steltic_grokbot", "  QFM  ") == "QFM"
        assert Registry().name("steltic_grokbot") == "QFM"           # persisted in state.json
        assert reg.rename("steltic_grokbot", "") == "Query file manager"
    finally:
        reg.rename("steltic_grokbot", "")


def test_connection_is_kept_on_this_pc_and_forgettable():
    from steltic_hub.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        r = c.post("/api/connection", json={"model": "MOCK"})
        assert r.status_code == 200
        assert config.CONNECTION_FILE.exists()
        assert json.loads(config.CONNECTION_FILE.read_text())["model"] == "MOCK"
        r = c.post("/api/connection", json={"model": "gpt-x", "base_url": "https://api.example.com/v1", "api_key": "k1"})
        assert r.status_code == 200
        r = c.post("/api/connection", json={"model": "gpt-y", "base_url": "https://api.example.com/v1"})   # no key typed
        assert r.status_code == 200 and json.loads(config.CONNECTION_FILE.read_text())["api_key"] == "k1"
        g = c.get("/api/connection").json()
        assert g["has_key"] and g["model"] == "gpt-y" and g["stored_at"]
        c.delete("/api/connection")
        assert not config.CONNECTION_FILE.exists()


def test_uploads_are_not_capped_and_names_come_from_the_registry():
    from steltic_hub.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        r = c.post("/api/jobs/UpTest/upload", files={"files": ("a b.json", b"{}" * 10, "application/json")})
        assert r.json()["files"][0]["name"] == "a_b.json"
        mods = {m["id"]: m for m in c.get("/api/state").json()["modules"]}
        assert mods["steltic_grokbot"]["default_name"] == "Query file manager"
        assert "servers_used" in mods["steltic"] and "steltic_grokbot" in mods["steltic"]["servers_used"]
        c.delete("/api/jobs/UpTest")


def test_native_crash_exit_codes_are_explained():
    """A PyTorch / pdfium crash ends the process with a bare NTSTATUS (3221225477 = 0xC0000005) or a signal;
    the run gets one sentence saying what that is, and the converter gets its resume hint."""
    from steltic_hub import exitcodes
    from steltic_hub.registry import Registry
    conv = next(t for t in Registry().manifest("steltic_grokbot").tabs if t.id == "convert")
    assert "resumes from the last finished chunk" in conv.run.crash_hint      # the module's own next step, in its manifest
    t = exitcodes.explain(3221225477, conv.run.crash_hint)
    assert "0xC0000005" in t and "access violation" in t and "Pages per chunk" in t
    t2 = exitcodes.explain(3221225477)
    assert "0xC0000005" in t2 and "Pages per chunk" not in t2
    assert "out-of-memory" in exitcodes.explain(-9) and "segmentation" in exitcodes.explain(-11)
    assert exitcodes.explain(0) is None and exitcodes.explain(1) is None and exitcodes.explain(3) is None and exitcodes.explain(None) is None


# ---------------------------------------------------------------- retrying a run that died in native code
# A process that dies the way a native library does. Windows hands back the raw NTSTATUS; a POSIX
# exit status is one byte and cannot carry 3221225477, so there the same event is a real SIGSEGV.
# exitcodes.explain recognises both, which is all `"on": "native_crash"` asks of a code.
_CRASH = "sys.exit(3221225477)" if sys.platform == "win32" else "os.kill(os.getpid(), signal.SIGSEGV)"


def _crash_script(tmp_path, name, body) -> pathlib.Path:
    p = tmp_path / name
    p.write_text("import os, pathlib, signal, sys, time\n" + body, encoding="utf-8")
    return p


def _retry_module(script, **run):
    """A one-tab CLI module standing in for the converter: whatever `script` does, with `run`'s manifest."""
    return Manifest.parse({"schema": 1, "id": "retrymod", "name": "Retry", "env": {"python": "3.12", "install": []},
                           "tabs": [{"id": "run", "kind": "form",
                                     "run": {"kind": "cli", "cwd": "{job_dir}", "command": [str(script)], **run}}]}, "t")


def _drive_run(monkeypatch, tmp_path, m, on_event=None):
    """Drive runners.run_cli against a stub module and collect the raw stream, as the SAC test does."""
    import asyncio
    class Reg:
        def is_installed(self, mid): return True
        def name(self, mid): return m.name
        def module_root(self, mid): return tmp_path
    monkeypatch.setattr(runners.envs, "python_bin", lambda mid: pathlib.Path(sys.executable))
    monkeypatch.setattr(runners.envs, "env_ready", lambda mid: True)
    monkeypatch.setattr(runners, "build_ctx", lambda *a, **k: {"job_dir": str(tmp_path), "jobs_dir": str(tmp_path),
                                                              "module_dir": str(tmp_path), "data_dir": str(tmp_path),
                                                              "job": "J", "port": "0", "needs": {}, "python": {},
                                                              "out": {}, "server": {}, "f": {}})
    monkeypatch.setattr(runners, "RETRY_DELAY", 0.05)     # the real wait is five seconds; the rule is what is under test
    runs = runners.JobRuns()
    async def collect():
        out = []
        async for ev in runners.run_cli(m, m.tabs[0], "J", {}, Reg(), runs, "r1"):
            out.append(ev)
            if on_event:
                await on_event(runs, ev)
        return out
    return asyncio.run(collect())


def _events(raw, kind=None):
    evs = [json.loads(e[6:]) for e in raw if e.startswith("data: ")]
    return [e for e in evs if kind is None or e.get("type") == kind]


def test_a_native_crash_is_retried_inside_the_same_run(monkeypatch, tmp_path):
    """The converter dies in PyTorch / pdfium with no traceback, and a fresh process usually gets past it
    (it resumes from its last finished chunk). A tab whose manifest asks for it presses the button again
    by itself -- in the same stream, so the browser still sees one run with one log and one Stop."""
    script = _crash_script(tmp_path, "flaky.py",
                           "marker = pathlib.Path(__file__).with_name('attempted')\n"
                           "print('Converting pages 1-5')\n"
                           "if marker.exists():\n"
                           "    print('conversion complete')\n"
                           "    sys.exit(0)\n"
                           "marker.write_text('x')\n" + _CRASH + "\n")
    assert runners.RETRY_DELAY == 5.0                      # five seconds, and Stop cuts it short
    raw = _drive_run(monkeypatch, tmp_path, _retry_module(script, retry={"on": "native_crash", "max": 3}))
    assert [e["attempt"] for e in _events(raw, "retry")] == [2]
    assert _events(raw, "retry")[0]["max"] == 3
    assert len(_events(raw, "start")) == 2                  # the re-spawn shows its command in the same log
    assert len({e["run_id"] for e in _events(raw, "start")}) == 1
    log = "\n".join(e["text"] for e in _events(raw, "log"))
    assert "at attempt 1 of 3" in log and "Stop cancels" in log and "conversion complete" in log
    done = _events(raw, "done")[-1]
    assert done["ok"] is True and done["rc"] == 0 and done["attempts"] == 2


def test_b_a_crash_that_does_not_move_is_not_retried_again(monkeypatch, tmp_path):
    """Retrying only helps while the crash moves. When the last line before the crash is the one that came
    before the previous crash, the fault is in that page, not in process state -- so the hub stops and says
    so once, with the tab's own crash_hint, rather than burning the remaining attempts."""
    script = _crash_script(tmp_path, "stuck.py", "print('Converting pages 40-45')\n" + _CRASH + "\n")
    m = _retry_module(script, retry={"on": "native_crash", "max": 3}, crash_hint="Convert again -- it resumes.")
    raw = _drive_run(monkeypatch, tmp_path, m)
    assert len(_events(raw, "retry")) == 1                  # one retry, then the give-up rule
    done = _events(raw, "done")[-1]
    assert done["attempts"] == 2 and done["ok"] is False
    log = "\n".join(e["text"] for e in _events(raw, "log"))
    assert "has not moved" in log
    hints = [e["text"] for e in _events(raw, "error") if "Convert again -- it resumes." in e["text"]]
    assert len(hints) == 1 and "No Python traceback" in hints[0]


def test_c_a_cancelled_run_is_never_retried(monkeypatch, tmp_path):
    """Stop means stop. A killed process exits with a code that looks exactly like a native crash (SIGTERM is
    in the same table), so the cancelled flag has to be checked before the retry rule, not after."""
    script = _crash_script(tmp_path, "slow.py",
                           "print('Converting pages 1-5', flush=True)\ntime.sleep(0.5)\n" + _CRASH + "\n")
    m = _retry_module(script, retry={"on": "native_crash", "max": 3}, crash_hint="Convert again -- it resumes.")
    async def stop_at_the_first_line(runs, ev):
        if '"type": "log"' in ev and "r1" not in runs.cancelled:
            assert await runs.cancel("r1") is True         # what POST /api/cancel does
    raw = _drive_run(monkeypatch, tmp_path, m, on_event=stop_at_the_first_line)
    assert not _events(raw, "retry") and len(_events(raw, "start")) == 1
    done = _events(raw, "done")[-1]
    assert done["cancelled"] is True and done["ok"] is False and done["attempts"] == 1
    assert not _events(raw, "error")                        # a stop is not a crash to explain


def test_d_a_malformed_retry_block_is_refused(monkeypatch):
    """A retry block is a loop the user cannot see into, so a typo in one is refused at load time rather
    than discovered as a run that repeats twenty times or never repeats at all."""
    from steltic_hub.registry import Registry
    base = {"schema": 1, "id": "x", "name": "X", "env": {"python": "3.12", "install": []},
            "tabs": [{"id": "t", "kind": "form",
                      "fields": [{"id": "chunk", "type": "number", "arg": "--chunk"}],
                      "run": {"kind": "cli", "command": ["-c", "pass"],
                              "retry": {"on": "native_crash", "max": 3, "then_set": {"chunk": 2}}}}]}
    assert Manifest.parse(base, "t").tabs[0].run.retry == {"on": "native_crash", "max": 3, "then_set": {"chunk": 2}}
    assert Manifest.parse(json.loads(json.dumps(base).replace('"retry"', '"unused"')), "t").tabs[0].run.retry == {}
    for bad in ("always", {"on": "sometimes"}, {"on": []}, {"on": ["3221225477"]}, {"on": [True]},
                {"on": "native_crash", "max": "3"}, {"on": "native_crash", "max": 0},
                {"on": "native_crash", "max": 11}, {"on": "native_crash", "max": 2, "then_set": [["chunk", 2]]},
                {"on": "native_crash", "max": 2, "then_set": {"chunk_pages": 2}}):
        d = json.loads(json.dumps(base))
        d["tabs"][0]["run"]["retry"] = bad
        with pytest.raises(ManifestError):
            Manifest.parse(d, "t")
    from steltic_hub.manifest import Run
    with pytest.raises(ManifestError):     # only a cli run owns a process the hub could spawn again
        Run.parse({"kind": "http", "path": "/api/run", "retry": {"on": "native_crash", "max": 2}})
    # and the tab this was written for asks for it, with the smaller windows as the second retry's work-around
    conv = next(t for t in Registry().manifest("steltic_grokbot").tabs if t.id == "convert")
    assert conv.run.retry == {"on": "native_crash", "max": 3, "then_set": {"chunk_pages": 2}}
    assert any(f.id == "chunk_pages" and f.arg for f in conv.fields)


def test_state_says_which_tabs_are_missing_an_optional_component(monkeypatch):
    """Pressing Convert and being refused is a poor way to learn that a 2 GB download is missing, so
    /api/state carries the answer per tab and the UI disables the button up front. The probe is the same
    cached one the Modules page uses, and a module with no environment answers False without spawning it."""
    from steltic_hub import envs, main as M
    from fastapi.testclient import TestClient
    def convert_and_corpus():
        with TestClient(M.app) as c:
            mods = {m["id"]: m for m in c.get("/api/state").json()["modules"]}
        tabs = {t["id"]: t for t in mods["steltic_grokbot"]["tabs"]}
        return tabs["convert"], tabs["corpus"]
    monkeypatch.setattr(envs, "optional_present", lambda m_, g: False)
    conv, corpus = convert_and_corpus()
    assert conv["missing_optional"] == ["converter"]
    assert corpus["missing_optional"] == []                 # a tab that requires nothing is never gated
    monkeypatch.setattr(envs, "optional_present", lambda m_, g: True)
    assert convert_and_corpus()[0]["missing_optional"] == []


# ---------------------------------------------------------------- 2026-09-18 review fixes
def test_state_changing_requests_must_come_from_the_hubs_own_page():
    """A web page on any site can send a "simple" cross-site POST (text/plain body, or no body) to
    127.0.0.1:8300 without a CORS preflight; the browser only hides the reply. Before this guard that
    could rewrite the LLM connection to an attacker's endpoint, register a module from any git URL and
    install it (pip install -e . runs code), start runs and delete projects."""
    from fastapi.testclient import TestClient
    from steltic_hub import main
    c = TestClient(main.app)
    evil = {"origin": "https://evil.example"}
    body = json.dumps({"base_url": "https://evil.example/v1", "api_key": "k", "model": "m"})
    r = c.post("/api/connection", content=body, headers={"content-type": "text/plain", **evil})
    assert r.status_code == 403 and "cross-site" in r.json()["detail"]
    assert c.post("/api/jobs/evilproj", headers=evil).status_code == 403
    assert c.post("/api/modules/custom", content="{}", headers={"content-type": "text/plain", "sec-fetch-site": "cross-site"}).status_code == 403
    assert c.post("/api/modules/x/install", headers={"origin": "null"}).status_code == 403
    # DNS rebinding: a hostname the attacker points at 127.0.0.1
    assert c.get("/api/connection", headers={"host": "evil.example"}).status_code == 403
    # the hub's own page (the Edge --app window, the Tauri webview, a browser tab) is untouched
    for origin in ("http://127.0.0.1:8300", "http://localhost:8300", "http://[::1]:8300"):
        assert c.post("/api/jobs/okproj", headers={"origin": origin}).status_code == 200
    assert c.post("/api/jobs/okproj2", headers={"sec-fetch-site": "same-origin"}).status_code == 200
    assert c.post("/api/jobs/okproj3").status_code == 200          # the launcher, curl, a module server: no Origin
    assert c.get("/api/state", headers=evil).status_code == 200    # reads stay readable (the browser hides the reply anyway)
    for j in ("okproj", "okproj2", "okproj3"):
        c.delete(f"/api/jobs/{j}")


def test_rebuilding_an_environment_forgets_its_optional_components(tmp_path, monkeypatch):
    """After Rebuild env / Remove + Install the interpreter is new, and the converter group that was
    importable in the old one is not in it. The cached `present` used to survive, so the Convert tab's
    gate stayed open and the run died on the first import instead of being refused up front."""
    from steltic_hub import envs
    m = Manifest.parse({"schema": 1, "id": "cachedemo", "name": "D", "source": {"url": "x"},
                        "env": {"optional": {"conv": {"label": "Conv", "requirements": ["r"], "probe_import": "json"}}},
                        "tabs": [{"id": "t", "kind": "form", "run": {"kind": "cli", "command": ["-c", "1"]}, "requires_optional": ["conv"]}]})
    monkeypatch.setattr(envs, "python_bin", lambda mid: tmp_path / "py")
    (tmp_path / "py").write_text("")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R: returncode = 0
        return R()
    monkeypatch.setattr(envs.subprocess, "run", fake_run)
    assert envs.optional_present(m, "conv") is True and len(calls) == 1
    assert envs.optional_present(m, "conv") is True and len(calls) == 1      # cached
    envs.invalidate("cachedemo")                                              # what install_into / remove_env call
    assert envs.optional_present(m, "conv") is True and len(calls) == 2      # probed again
    envs.invalidate()
    assert envs.optional_present(m, "conv") is True and len(calls) == 3


def test_a_then_set_that_could_never_apply_is_refused():
    base = {"schema": 1, "id": "r", "name": "R", "source": {"url": "x"}, "tabs": [{"id": "t", "kind": "form", "fields": [{"id": "n", "type": "number"}],
            "run": {"kind": "cli", "command": ["-c", "1"], "retry": {"on": "native_crash", "max": 2, "then_set": {"n": 2}}}}]}
    with pytest.raises(ManifestError, match="then_set needs max >= 3"):
        Manifest.parse(base)
    base["tabs"][0]["run"]["retry"]["max"] = 3
    assert Manifest.parse(base).tabs[0].run.retry["then_set"] == {"n": 2}


def test_hub_url_is_a_template_for_module_servers():
    from steltic_hub import config, runners
    from steltic_hub.registry import Registry
    reg = Registry()
    ctx = runners.base_ctx(reg.catalog["steltic"], "P", reg)
    assert ctx["hub_url"] == config.hub_url() and ctx["hub_url"].startswith("http://127.0.0.1:")
    assert runners.expand("{hub_url}/api/state", ctx) == ctx["hub_url"] + "/api/state"


# ---------------------------------------------------------------- one port per module, for good
def test_a_port_once_given_to_a_module_stays_its_own(monkeypatch, tmp_path):
    """Every module page asks for /static/app.js by the same path and the browser caches per origin,
    so a port that passes from Design variations to Admin serves Variations' script inside Admin's
    page ("The variations module could not load: Cannot set properties of null"). The supervisor
    therefore remembers each module's port and never reuses one for another module."""
    from steltic_hub.runners import ServerSupervisor
    monkeypatch.setattr(config, "PORTS_FILE", tmp_path / "ports.json")
    monkeypatch.setattr(config, "PORT_BASE", 49400)
    monkeypatch.setattr(config, "PORT_SPAN", 6)
    sup = ServerSupervisor(registry=None)
    a = sup._alloc_port("steltic_variations")
    b = sup._alloc_port("steltic_admin")
    assert a != b and {a, b} <= set(range(49400, 49406))
    # a restart of the hub: the map on disk gives each module the same port back, in any order
    sup2 = ServerSupervisor(registry=None)
    assert sup2._alloc_port("steltic_admin") == b
    assert sup2._alloc_port("steltic_variations") == a
    # a third module never gets a port on record for another, even though both are free right now
    c = sup2._alloc_port("steltic_probabilistic")
    assert c not in (a, b)
    saved = json.loads((tmp_path / "ports.json").read_text())
    assert saved == {"steltic_variations": a, "steltic_admin": b, "steltic_probabilistic": c}
    # only when the window is exhausted does an idle module's port change hands -- and the map says so
    monkeypatch.setattr(config, "PORT_SPAN", 3)
    d = sup2._alloc_port("steltic_grokbot")
    assert d in (a, b, c)
    saved = json.loads((tmp_path / "ports.json").read_text())
    assert saved["steltic_grokbot"] == d and d not in [v for k, v in saved.items() if k != "steltic_grokbot"]


def test_bundled_servers_never_let_the_browser_cache_a_stale_asset():
    """Belt and braces for the same fault: the three bundled servers mark every response no-cache."""
    for rel in ("steltic_admin/admin/main.py", "steltic_variations/variations/main.py",
                "steltic_probabilistic/probabilistic/main.py"):
        src = (config.CATALOG_DIR / rel).read_text(encoding="utf-8")
        assert "_no_stale_assets" in src and '"Cache-Control"' in src, rel


# ---------------------------------------------------------------- Stop on a server that keeps streaming
def test_stop_drops_a_stream_the_server_keeps_open_after_its_stop_endpoint(monkeypatch):
    """CFS Steel's /api/stop answers ok and then clears its own cancel flag while releasing the run's
    quota slot, so the run streams on and the hub's Stop looked dead. After the stop request the hub
    now waits STOP_GRACE and drops the stream, which the server treats as a disconnect = stop."""
    import asyncio
    monkeypatch.setattr(config, "STOP_GRACE", 0.05)
    posted, closed = [], []
    class Resp:
        async def aclose(self): closed.append(True)
    class CX:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def request(self, method, url, json=None): posted.append((method, url, json))
    monkeypatch.setattr(runners.httpx, "AsyncClient", CX)
    async def go():
        runs = runners.JobRuns()
        h = {"spec": {"method": "POST", "path": "/api/stop", "body": {"building": "J"}}, "base_url": "http://m",
             "task": asyncio.current_task(), "resp": Resp()}
        runs.http["r1"] = h
        assert await runs.cancel("r1") is True
        assert posted == [("POST", "http://m/api/stop", {"building": "J"})]
        assert not closed                    # the server gets its chance first
        await asyncio.sleep(0.15)
        assert closed == [True]              # ... and then the stream is dropped
        # a stream that ended by itself in the meantime is left alone
        closed.clear(); runs.http["r2"] = dict(h, resp=Resp())
        assert await runs.cancel("r2") is True
        runs.http.pop("r2")
        await asyncio.sleep(0.15)
        assert not closed
    asyncio.run(go())


# ---------------------------------------------------------------- a CLI run that talks to the model
def test_cli_event_lines_are_relayed_as_events_and_the_rest_stays_log(monkeypatch, tmp_path):
    """A CLI module may print one JSON event per line in the agents' vocabulary; the hub relays it as that
    event (so the browser shows model text, reasoning and tool lines the way it does for the design
    agents) and everything else as a log line. A hub-owned type (done, start) printed by a module stays a
    log line, and so does JSON that is not an event."""
    script = tmp_path / "talk.py"
    script.write_text("import json, os\n"
                      "print('starting')\n"
                      "print(json.dumps({'type': 'reasoning', 'text': 'thinking about drift'}))\n"
                      "print(json.dumps({'type': 'token', 'text': 'The storey-3 drift '}))\n"
                      "print(json.dumps({'type': 'tool', 'name': 'search_engineering_standards', 'title': 'ASCE 7 16.4.1.2'}))\n"
                      "print(json.dumps({'type': 'done', 'ok': True}))\n"
                      "print(json.dumps({'not': 'an event'}))\n"
                      "print('KEY=' + os.environ.get('STELTIC_LLM_API_KEY', '(unset)') + ' MODEL=' + os.environ.get('STELTIC_LLM_MODEL', '(unset)'))\n"
                      "print('RAG=' + os.environ.get('RAG_API_URL', '(unset)'))\n", encoding="utf-8")
    m = _retry_module(script, llm=True, env={"RAG_API_URL": "{server.steltic_grokbot}/query"})
    raw = _drive_run(monkeypatch, tmp_path, m)
    assert [e["type"] for e in _events(raw)][:6] == ["start", "log", "reasoning", "token", "tool", "log"]
    assert _events(raw, "reasoning")[0]["text"] == "thinking about drift"
    assert _events(raw, "tool")[0]["name"] == "search_engineering_standards"
    logs = [e["text"] for e in _events(raw, "log")]
    assert '{"type": "done", "ok": true}' in logs and '{"not": "an event"}' in logs
    # driven without a supervisor: no connection and no standards server to resolve -> unset, not a literal template
    assert "KEY=(unset) MODEL=(unset)" in logs and "RAG=(unset)" in logs
    assert runners.event_line("") is None and runners.event_line('{"type": "token"') is None


def test_a_cli_run_marked_llm_gets_the_connection_and_the_servers_it_names(monkeypatch, tmp_path):
    """run.llm hands the hub's one connection to the process as STELTIC_LLM_*; {server.<id>} in the run's
    environment starts that server and substitutes its address -- the same two things the hub does for an
    agent server, done for a process."""
    import asyncio
    script = tmp_path / "env.py"
    script.write_text("import os\nfor k in ('STELTIC_LLM_BASE_URL', 'STELTIC_LLM_API_KEY', 'STELTIC_LLM_MODEL', 'RAG_API_URL'):\n"
                      "    print(k + '=' + os.environ.get(k, '(unset)'))\n", encoding="utf-8")
    m = _retry_module(script, llm=True, env={"RAG_API_URL": "{server.steltic_grokbot}/query"})
    class Reg:
        def is_installed(self, mid): return True
        def name(self, mid): return mid
        def module_root(self, mid): return tmp_path
        def manifest(self, mid):
            return Manifest.parse({"schema": 1, "id": mid, "name": mid, "env": {"python": "3.12", "install": []},
                                   "server": {"command": ["x"]}, "tabs": [{"id": "t", "kind": "files"}]}, "t")
    class Sup:
        ports = {}
        def connection(self): return {"base_url": "https://llm.example/v1", "api_key": "sk-secret", "model": "m-1"}
        async def ensure(self, mid): return "http://127.0.0.1:8419"
    monkeypatch.setattr(runners.envs, "python_bin", lambda mid: pathlib.Path(sys.executable))
    monkeypatch.setattr(runners.envs, "env_ready", lambda mid: True)
    monkeypatch.setattr(runners, "build_ctx", lambda *a, **k: {"job_dir": str(tmp_path), "job": "J"})
    async def collect():
        return [ev async for ev in runners.run_cli(m, m.tabs[0], "J", {}, Reg(), runners.JobRuns(), "r1", supervisor=Sup())]
    logs = [e["text"] for e in _events(asyncio.run(collect()), "log")]
    assert "STELTIC_LLM_BASE_URL=https://llm.example/v1" in logs and "STELTIC_LLM_API_KEY=sk-secret" in logs
    assert "STELTIC_LLM_MODEL=m-1" in logs and "RAG_API_URL=http://127.0.0.1:8419/query" in logs
    assert runners.servers_referenced(m, m.tabs[0]) == ["steltic_grokbot"]


def test_run_llm_is_declared_only_where_it_means_something():
    base = {"schema": 1, "id": "x", "name": "X", "env": {"python": "3.12", "install": []},
            "tabs": [{"id": "t", "kind": "form", "run": {"kind": "http", "path": "/api/run", "llm": True}}]}
    with pytest.raises(ManifestError):
        Manifest.parse(base, "t")
    base["tabs"][0]["run"] = {"kind": "cli", "command": ["-m", "x"], "llm": "yes"}
    with pytest.raises(ManifestError):
        Manifest.parse(base, "t")
    base["tabs"][0]["run"] = {"kind": "cli", "command": ["-m", "x"], "llm": True}
    assert Manifest.parse(base, "t").to_json()["tabs"][0]["run"]["llm"] is True
    # the catalog: the Nonlinear module's Review tab is the one that uses it, and it names the standards server
    cat = load_catalog(config.CATALOG_DIR)
    review = next(t for t in cat["steltic_nonlinear"].tabs if t.id == "review")
    assert review.run.llm is True and review.run.env["RAG_API_URL"] == "{server.steltic_grokbot}/query"
    assert runners.servers_referenced(cat["steltic_nonlinear"], review) == ["steltic_grokbot"]
    assert [t.id for t in cat["steltic_nonlinear"].tabs if t.run and t.run.llm] == ["review"]


def test_run_continues_names_the_tab_a_resume_picks_up():
    """`run.continues` is how a driver (Admin's batch) knows which tab resumes an interrupted run instead
    of starting the step over. It must name another runnable tab of the same module, of the same kind."""
    srv = {"command": ["-m", "x"], "health": "/healthz"}
    def mk(run):
        return {"schema": 1, "id": "x", "name": "X", "env": {"python": "3.12", "install": []}, "server": srv,
                "tabs": [{"id": "design", "kind": "form", "run": {"kind": "http", "path": "/api/run"}},
                         {"id": "cont", "kind": "form", "run": run},
                         {"id": "app", "kind": "embed"}]}
    for bad in ({"kind": "http", "path": "/api/run", "continues": "nope"},        # no such tab
                {"kind": "http", "path": "/api/run", "continues": "cont"},        # itself
                {"kind": "http", "path": "/api/run", "continues": "app"},         # a tab with nothing to run
                {"kind": "cli", "command": ["-m", "x"], "continues": "design"},   # a cli run cannot resume an http one
                {"kind": "http", "path": "/api/run", "continues": 3}):
        with pytest.raises(ManifestError):
            Manifest.parse(mk(bad), "t")
    m = Manifest.parse(mk({"kind": "http", "path": "/api/run", "continues": "design"}), "t")
    j = m.to_json()["tabs"]
    assert j[1]["run"]["continues"] == "design" and j[0]["run"]["continues"] is None
    # the catalog: HR Steel's and CFS's Continue tabs resume their Design tabs
    cat = load_catalog(config.CATALOG_DIR)
    for mid in ("steltic", "steltic_cfs"):
        cont = next(t for t in cat[mid].tabs if t.id == "continue")
        assert cont.run.continues == "design" and cont.run.body.get("resume") is True
        assert [t.id for t in cat[mid].tabs if t.run and t.run.continues] == ["continue"]


def test_every_nonlinear_tab_that_reads_the_design_package_stages_it():
    """2026-09-21: Site hazard pressed on a project whose HR Steel design had never been staged died with
    `no model_opensees.py under <job_dir>` -- only Run and Inspect copied the design into the project folder.
    Hazard, Criteria and Mesh read the package too (periods, cfg, the model), so they stage it the same way."""
    cat = load_catalog(config.CATALOG_DIR)
    m = cat["steltic_nonlinear"]
    for tid in ("run", "inspect", "hazard", "criteria", "mesh"):
        t = next(t for t in m.tabs if t.id == tid)
        assert t.run.stage and t.run.stage[0]["from"] == "{f.package}" and t.run.stage[0]["required"], tid
        assert "HR Steel" in t.run.stage[0]["missing"], tid
        pkg = next(f for f in t.fields if f.id == "package")
        assert pkg.type == "file" and pkg.default == "{out.steltic}", tid
    for tid in ("review", "compare"):                     # these read what a Run wrote, not the design
        t = next(t for t in m.tabs if t.id == tid)
        assert not t.run.stage, tid


# ---------------------------------------------------------------- the rail's "running"
def test_state_says_which_module_and_tab_is_running_not_just_that_something_is():
    """`/api/state` used to report `{run-uuid: True}` -- true, and useless.

    The UI keys its own run map `"<module>.<tab>"` (runKey), so it could not match a uuid to a
    card and computed the rail's "running..." from its own local map instead. That map belongs to
    one browser tab: it went dark on a reload and never lit for a run started in another window.
    """
    runs = runners.JobRuns()
    assert runs.live_tabs() == {}
    runs.began("deadbeefcafe", "steltic", "design")
    runs.began("0123456789ab", "steltic_grokbot", "convert")
    assert runs.live_tabs() == {"steltic.design": True, "steltic_grokbot.convert": True}
    runs.ended("deadbeefcafe")
    assert runs.live_tabs() == {"steltic_grokbot.convert": True}
    runs.ended("deadbeefcafe")                       # ending twice is not an error
    runs.ended("never-started")
    assert runs.live_tabs() == {"steltic_grokbot.convert": True}


def test_state_reports_the_live_tabs_the_ui_can_match():
    from steltic_hub.main import app, RUNS
    from fastapi.testclient import TestClient
    RUNS.began("feedfacefeed", "steltic_nonlinear", "run")
    try:
        with TestClient(app) as c:
            running = c.get("/api/state").json()["running"]
        assert running.get("steltic_nonlinear.run") is True, running
        assert all("." in k for k in running), f"keys must be <module>.<tab>, got {list(running)}"
    finally:
        RUNS.ended("feedfacefeed")


def test_a_server_backed_module_is_visible_in_the_rail():
    """Admin, Design variations and Probabilistic do their work in a module server, not in a run,
    so the rail had nothing to light up. The card carries `has_server`/`server_up`; the rail now
    reads them, which is what this asserts is still being published."""
    from steltic_hub.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        mods = {m["id"]: m for m in c.get("/api/state").json()["modules"]}
    for mid in ("steltic_admin", "steltic_variations", "steltic_probabilistic"):
        assert mid in mods, f"{mid} missing from /api/state"
        assert mods[mid]["has_server"] is True, f"{mid} is server-backed"
        assert "server_up" in mods[mid], f"{mid} must publish server_up for the rail"
# ---------------------------------------------------------------- a tab with more than one button
def test_a_tab_may_carry_extra_buttons_beside_its_run():
    """`actions` lets one tab offer two things to do with the same fields and the same log pane.

    The Nonlinear Run tab needs it: `Run analyses` does the OpenSees work with no standards
    lookups, and `Revise reports` re-renders those reports with the live corpus behind them. Two
    tabs would make the engineer hop between them for one job.
    """
    srv = {"command": ["-m", "x"], "health": "/healthz"}
    def mk(actions):
        return {"schema": 1, "id": "x", "name": "X", "env": {"python": "3.12", "install": []}, "server": srv,
                "tabs": [{"id": "go", "kind": "form", "run": {"kind": "cli", "command": ["-m", "x"]},
                          "actions": actions}]}
    m = Manifest.parse(mk([{"id": "again", "label": "Again", "info": "after the first one",
                            "run": {"kind": "cli", "command": ["-m", "x", "again"]}}]), "t")
    a = m.tabs[0].actions[0]
    assert a.id == "again" and a.label == "Again" and a.info == "after the first one"
    assert a.run.command[-1] == "again"
    j = m.to_json()["tabs"][0]["actions"][0]
    assert j["id"] == "again" and j["label"] == "Again" and j["can_cancel"] is True
    for bad in ([{"run": {"kind": "cli", "command": ["-m", "x"]}}],                      # no id
                [{"id": "a"}],                                                            # nothing to run
                [{"id": "run", "run": {"kind": "cli", "command": ["-m", "x"]}}],          # the main button's name
                [{"id": "a", "run": {"kind": "cli", "command": ["-m", "x"]}},
                 {"id": "a", "run": {"kind": "cli", "command": ["-m", "x"]}}],            # duplicate
                [{"id": "a", "run": {"kind": "cli", "command": ["-m", "x"], "continues": "go"}}]):
        with pytest.raises(ManifestError):
            Manifest.parse(mk(bad), "t")
    # an action with no main run to sit beside is just a run
    with pytest.raises(ManifestError):
        Manifest.parse({"schema": 1, "id": "x", "name": "X", "env": {"python": "3.12", "install": []}, "server": srv,
                        "tabs": [{"id": "go", "kind": "form",
                                  "actions": [{"id": "a", "run": {"kind": "cli", "command": ["-m", "x"]}}]}]}, "t")


def test_the_nonlinear_run_tab_offers_revise_beside_run_analyses():
    """Run analyses does no standards lookups, so its reports mark every clause UNVERIFIED. Revise
    re-renders them against the review, grounded in the live corpus. Same tab, same project."""
    cat = load_catalog(config.CATALOG_DIR)
    run = next(t for t in cat["steltic_nonlinear"].tabs if t.id == "run")
    assert run.run.label == "Run analyses"
    rev = next(a for a in run.actions if a.id == "revise")
    assert rev.label == "Revise reports"
    assert rev.run.env.get("RAG_API_URL") == "{server.steltic_grokbot}/query", "it must query the live corpus"
    assert rev.run.llm is True
    assert "Review" in rev.info and "UNVERIFIED" in rev.info, "the note must say to run Review first"
    # and the Review tab it waits on still writes review.md into the same project folder
    review = next(t for t in cat["steltic_nonlinear"].tabs if t.id == "review")
    assert "review.md" in {a["path"] for a in review.artifacts} and review.run.cwd == "{job_dir}"
