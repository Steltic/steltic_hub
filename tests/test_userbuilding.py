"""User-designed building module: the page relays to HR Steel's build-only API, runs the Nonlinear module's
confirmation, refuses to start the suite before a confirmation, and starts it through the hub (Collect, then Run)
exactly as the Nonlinear module's own buttons do. HR Steel, the Nonlinear interpreter and the hub are stand-ins.

    python -m pytest tests/test_userbuilding.py -q
"""
import json, os, pathlib, sys, tempfile, time

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MOD = ROOT / "steltic_hub" / "catalog" / "steltic_userbuilding"
sys.path.insert(0, str(MOD))
TMP = pathlib.Path(tempfile.mkdtemp(prefix="userbuilding-test-"))
os.environ["HR_JOBS"] = str(TMP / "hr_jobs")
os.environ["UB_JOBS"] = str(TMP / "ub_jobs")
os.environ["STELTIC_URL"] = "http://127.0.0.1:9"
os.environ["HUB_URL"] = "http://127.0.0.1:8"
os.environ["NL_PYTHON"] = sys.executable

from userbuilding import main as um          # noqa: E402
from userbuilding import hub as uh           # noqa: E402
from fastapi.testclient import TestClient    # noqa: E402

client = TestClient(um.app)


class FakeHR:
    """Records what the page sent to HR Steel and answers like its build-only API."""
    def __init__(self):
        self.calls = []

    def __call__(self, method, path, timeout=120.0, **kw):
        self.calls.append((method, path, kw))
        req = httpx.Request(method, "http://hr" + path)
        if path == "/api/build_only/intake":
            return httpx.Response(200, json={"building": kw["data"]["building"], "summary": {"columns": 4},
                                             "missing_items": {"ready": False, "counts": {"key": 1, "default": 0, "refused": 0, "info": 0},
                                                               "groups": []}}, request=req)
        if path == "/api/build_only/answers":
            return httpx.Response(200, json={"missing_items": {"ready": True, "counts": {"key": 0, "default": 0, "refused": 0, "info": 0}}}, request=req)
        if path == "/api/build_only/build":
            return httpx.Response(202, json={"state": "running"}, request=req)
        if path.startswith("/api/build_only/status/"):
            return httpx.Response(404, json={"detail": "no job"}, request=req)
        if path == "/api/build_only/template":
            return httpx.Response(200, content=b"PK\x03\x04xlsx", headers={"content-type": "application/vnd.ms-excel",
                                                                            "content-disposition": 'attachment; filename="t.xlsx"'}, request=req)
        return httpx.Response(404, json={"detail": "?"}, request=req)


@pytest.fixture
def hr(monkeypatch):
    f = FakeHR()
    monkeypatch.setattr(um, "_hr", f)
    return f


def test_the_page_and_health():
    assert client.get("/healthz").json() == {"ok": True}
    assert "User-designed building" in client.get("/").text


def test_upload_answers_build_are_relayed_to_hr(hr):
    r = client.post("/api/project/My Bldg!/upload", files={"file": ("b.e2k", b"$ CONTROLS\n", "text/plain")})
    assert r.status_code == 200 and r.json()["missing_items"]["counts"]["key"] == 1
    m, path, kw = hr.calls[-1]
    assert path == "/api/build_only/intake" and kw["data"]["building"] == "MyBldg" and kw["files"]["file"][0] == "b.e2k"
    r = client.post("/api/project/MyBldg/answers", json={"answers": {"seismic.SDS": {"value": 1.0}}})
    assert r.json()["missing_items"]["ready"]
    assert hr.calls[-1][2]["json"] == {"building": "MyBldg", "answers": {"seismic.SDS": {"value": 1.0}}}
    assert client.post("/api/project/MyBldg/build").status_code == 202
    assert client.get("/api/project/MyBldg/hr").json()["state"] == "none"
    t = client.get("/api/template")
    assert t.status_code == 200 and t.content.startswith(b"PK") and "t.xlsx" in t.headers["content-disposition"]


def test_only_package_files_are_offered(hr):
    assert client.get("/api/project/X/file/cfg.py").status_code == 404
    assert client.get("/api/project/X/file/../../etc/passwd").status_code in (404, 400)


def _fake_snl(monkeypatch, ok=True):
    class P:
        def __init__(self, out):
            self.stdout, self.stderr, self.returncode = out, "", 0
    calls = []

    def run(args, timeout=300):
        calls.append(args)
        j = pathlib.Path(args[1])
        res = {"ok": ok, "checks": [{"id": "complete", "label": "Complete", "status": "pass" if ok else "fail", "detail": "", "hard": True}],
               "warnings": [], "accepted_defaults": []}
        if "--check" not in args and ok:
            (j / "nl_basis.json").write_text(json.dumps({"confirmed": True, "by": "me", "at": "now"}))
            res["confirmed"] = True
        return P(json.dumps(res))
    monkeypatch.setattr(um, "_snl", run)
    return calls


def test_confirm_and_the_run_gate(monkeypatch):
    j = pathlib.Path(os.environ["HR_JOBS"]) / "B1"
    j.mkdir(parents=True, exist_ok=True)
    assert client.get("/api/project/B1/checks").status_code == 409            # not built yet
    (j / "provenance.json").write_text("{}")
    calls = _fake_snl(monkeypatch)
    c = client.get("/api/project/B1/checks").json()
    assert c["ok"] and c["nl_basis"] is None and "--check" in calls[-1]
    assert client.post("/api/project/B1/run", json={}).status_code == 409    # never before a confirmation
    r = client.post("/api/project/B1/confirm", json={"by": "Pat", "note": "ok"})
    assert r.status_code == 200 and r.json()["confirmed"]
    assert calls[-1][-4:] == ["--by", "Pat", "--note", "ok"]


def test_a_failing_check_cannot_confirm(monkeypatch):
    j = pathlib.Path(os.environ["HR_JOBS"]) / "B2"
    j.mkdir(parents=True, exist_ok=True)
    (j / "provenance.json").write_text("{}")
    _fake_snl(monkeypatch, ok=False)
    assert client.post("/api/project/B2/confirm", json={}).status_code == 409


def test_run_goes_through_the_hub_collect_then_run(monkeypatch):
    j = pathlib.Path(os.environ["HR_JOBS"]) / "B3"
    j.mkdir(parents=True, exist_ok=True)
    (j / "nl_basis.json").write_text(json.dumps({"confirmed": True}))
    seen = []

    def fake_run(self, module, tab, project, fields, on_event, should_stop=None, action=None):
        seen.append((module, tab, project, dict(fields), action))
        on_event({"type": "log", "text": "%s started" % (action or "run")})
        return {"ok": True, "rc": 0, "errors": [], "cancelled": False, "artifacts": []}
    monkeypatch.setattr(uh.HubClient, "run", fake_run)
    r = client.post("/api/project/B3/run", json={"n_records": 3, "site_class": "C"})
    assert r.status_code == 202
    for _ in range(100):
        st = client.get("/api/project/B3/run").json()
        if st["state"] != "running":
            break
        time.sleep(0.05)
    assert st["state"] == "done", st
    assert [s[4] for s in seen] == ["collect", None]
    assert all(s[:3] == ("steltic_nonlinear", "run", "B3") for s in seen)
    assert seen[1][3] == {"job": "B3", "n_records": 3, "site_class": "C"}
    assert [e["text"] for e in st["events"]] == ["collect started", "run started"]
    assert (pathlib.Path(os.environ["UB_JOBS"]) / "B3" / "userbuilding" / "nl_run.json").exists()


def test_hub_client_sends_the_action(monkeypatch):
    sent = {}

    def handler(request):
        sent["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=b'data: {"type": "done", "ok": true, "rc": 0, "end": true}\n\n')
    real = httpx.Client
    monkeypatch.setattr(uh.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items() if k != "transport"}))
    out = uh.HubClient("http://hub").run("steltic_nonlinear", "run", "P", {"job": "P"}, lambda e: None, action="collect")
    assert out["ok"] and sent["body"] == {"job": "P", "fields": {"job": "P"}, "action": "collect"}


def test_do_not_collect_skips_collect_and_the_run_tab_opens_on_its_own_opt_out(monkeypatch):
    """The tick box on step 5 used to skip Collect and then the hub refused Run analyses ("Collect
    specification values first"): the run tab's gate knew nothing of the choice. Now the page sends the
    Nonlinear tab's own opt-out field, which opens the gate and passes --no-collect."""
    j = pathlib.Path(os.environ["HR_JOBS"]) / "B4"
    j.mkdir(parents=True, exist_ok=True)
    (j / "nl_basis.json").write_text(json.dumps({"confirmed": True}))
    seen = []

    def fake_run(self, module, tab, project, fields, on_event, should_stop=None, action=None):
        seen.append((action, dict(fields)))
        return {"ok": True, "rc": 0, "errors": [], "cancelled": False, "artifacts": []}
    monkeypatch.setattr(uh.HubClient, "run", fake_run)
    for body in ({"n_records": 3, "skip_collect": True}, {"n_records": 3, "collect": False}):      # new and old spelling
        seen.clear()
        assert client.post("/api/project/B4/run", json=body).status_code == 202
        for _ in range(100):
            st = client.get("/api/project/B4/run").json()
            if st["state"] != "running":
                break
            time.sleep(0.05)
        assert st["state"] == "done", st
        assert [a for a, _ in seen] == [None], "Collect is not run"
        assert seen[0][1] == {"job": "B4", "n_records": 3, "no_collect": True}
        assert st["collect"] is False and st["collected"] is False


def test_the_opt_out_field_opens_the_real_nonlinear_gate():
    """The field the page sends is the one the Nonlinear manifest names as the waiver of its gate."""
    import json as _j
    from steltic_hub.manifest import Manifest
    cat = ROOT / "steltic_hub" / "catalog" / "steltic_nonlinear.json"
    m = Manifest.parse(_j.loads(cat.read_text(encoding="utf-8")), "t")
    run = next(t for t in m.tabs if t.id == "run")
    assert run.run.requires[0]["waive"] == "no_collect"
    f = next(f for f in run.fields if f.id == "no_collect")
    assert f.type == "checkbox" and f.arg == "--no-collect" and f.arg_style == "flag-if-true"
