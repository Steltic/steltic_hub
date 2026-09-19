"""Design variations module: library, metrics, scoring, eligibility and the API flow.

The HR Steel client is monkeypatched: no design server, no LLM, no network. The package
fixtures mirror the real HR Steel layout (report.html tables, design/calc_package.json,
design/member_schedule.csv, cfg.py, conversation.json).

    python -m pytest tests/test_variations.py -q
"""
import io, json, os, pathlib, sys, tempfile, time, zipfile
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MOD = ROOT / "steltic_hub" / "catalog" / "steltic_variations"
sys.path.insert(0, str(MOD))
os.environ["VARIATIONS_JOBS"] = tempfile.mkdtemp(prefix="variations-test-")
os.environ["STELTIC_URL"] = "http://127.0.0.1:1"          # never contacted: the client is patched

from variations import library, metrics as mx, scoring, steltic_client as hr, prompts   # noqa: E402
from variations import main as vm                                                       # noqa: E402


# ---------------------------------------------------------------- fixtures
REPORT = """<html><body>
<h2>Seismic force-resisting system (declared)</h2><p>SMF (AISC 341-22 E3 + AISC 358 RBS), R = 8</p>
<h3>Seismic design drift</h3>
<table><tr><th>Story</th><th>δe X %</th><th>δ X %</th><th>δe Y %</th><th>δ Y %</th><th>≤1.00%</th></tr>
<tr><td>1</td><td>0.10</td><td>0.55</td><td>0.09</td><td>0.50</td><td>OK</td></tr>
<tr><td>2</td><td>0.14</td><td>0.77</td><td>0.12</td><td>0.66</td><td>OK</td></tr>
<tr><td>3</td><td>0.16</td><td>{dmax}</td><td>0.13</td><td>0.71</td><td>OK</td></tr>
<tr><td>4</td><td>0.12</td><td>0.66</td><td>0.10</td><td>0.55</td><td>OK</td></tr></table>
<h3>Wind drift</h3>
<table><tr><th>Story</th><th>drift X %</th><th>drift Y %</th><th>≤ 0.25%</th></tr>
<tr><td>1</td><td>0.05</td><td>0.04</td><td>OK</td></tr><tr><td>2</td><td>0.07</td><td>0.06</td><td>OK</td></tr></table>
<p>Design base shear V = C s W = 0.1009 × 13,476 = 1,360 kip</p>
<p>Wind base shear: X = 503 kip, Y = 402 kip</p>
<table><tr><th>Mode</th><th>T (s)</th><th>mX %</th><th>ΣmX %</th><th>mY %</th><th>ΣmY %</th></tr><tr><td>1</td><td>1.115</td><td>80</td><td>80</td><td>0</td><td>0</td></tr></table>
</body></html>"""

CALC = {"members": [{"id": "B1", "DC": 0.82}, {"id": "C1", "DC": "{dc}"}], "connections": [{"id": "RBS-1", "DC": 0.77}],
        "capacity_design": {"system": "SMF (AISC 341-22 E3 + AISC 358 RBS), R=8",
                            "redundancy": "rho = 1.0 DEMONSTRATED per 12.3.4.2(b): >= 2 moment bays each side of CM",
                            "SCWB": {"ratio": 1.13}},
        "framework_screen": {"torsion": {"Ax": 1.0, "classification": "none", "ratio_max": 1.0},
                             "soft_story": {"classification": "none"}}}

SCHEDULE = "ele_tag,member,section,length_in\n" + "\n".join(
    [f"{i},col,W14X132,168.0" for i in range(1, 25)] + [f"{i},beam,W24X76,360.0" for i in range(25, 61)])

CFG = "NX, NY = 6, 4\nSX = SY = 360.0\nHEIGHTS = [192.0] + [168.0]*3\nNF = 4\n"


def make_package(building="B", dmax="0.91", dc=0.90, scale=1.0, brief="4-story office", extra=None):
    buf = io.BytesIO()
    sched = SCHEDULE
    if scale != 1.0:
        lines = sched.split("\n")
        sched = "\n".join([lines[0]] + [",".join(x.split(",")[:3] + [f"{float(x.split(',')[3]) * scale:.1f}"]) for x in lines[1:]])
    calc = json.loads(json.dumps(CALC).replace('"{dc}"', str(dc)))
    files = {"report.html": REPORT.replace("{dmax}", dmax), "viewer_3d.html": "<html>viewer</html>", "cfg.py": CFG,
             "design/calc_package.json": json.dumps(calc), "design/member_schedule.csv": sched,
             "conversation.json": json.dumps([{"role": "user", "content": f"HEAD\n\nBUILDING NAME: {building}\n\nDESIGN BRIEF:\n{brief}\n\nDesign this building now."}])}
    files.update(extra or {})
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in files.items():
            z.writestr(f"{building}/{k}", v)
    return buf.getvalue()


# ---------------------------------------------------------------- library
def test_library_has_ten_categories_and_generic_templates():
    assert len(library.CATEGORIES) == 10
    ids = [c["id"] for c in library.CATEGORIES]
    assert ids == ["problem", "core", "system", "perimeter", "outriggers", "geometry", "members", "bases", "seismic", "devices"]
    for c in library.CATEGORIES:
        assert c["label"] and c["blurb"] and len(c["templates"]) >= 4
        for title, change, why in c["templates"]:
            assert title and change and why
            # generalised: no grid names / levels / member sizes of one building
            assert not any(tok in change for tok in ("Level 14", "grid C", "W14X730", "HR08"))


def test_offline_plan_base_first_then_round_robin():
    plan = library.offline_plan(7, ["core", "system"])
    assert [v["id"] for v in plan] == [f"M{i:03d}" for i in range(1, 8)]
    assert plan[0]["change"] == "" and plan[0]["group"] == "reference"
    groups = [v["group"] for v in plan[1:]]
    assert groups[0] != groups[1] and set(groups) == {"Core / braced-frame configuration", "Lateral system type"}
    assert len({v["title"] for v in plan}) == 7
    assert len(library.offline_plan(1)) == 1
    assert len(library.offline_plan(300)) <= 1 + sum(len(c["templates"]) for c in library.CATEGORIES)


# ---------------------------------------------------------------- metrics
def test_metrics_read_from_a_package():
    files = mx.unzip(make_package())
    assert "report.html" in files and "design/calc_package.json" in files      # wrapping folder stripped
    m = mx.extract(files)
    assert m["n_stories"] == 4 and m["floor_area_sf"] == 6 * 30 * 4 * 30 * 4 and m["nx"] == 6 and m["ny"] == 4
    assert m["drift_limit_pct"] == 1.0 and m["drift_max_pct"] == 0.91 and m["drift_utilisation"] == 0.91
    assert m["drift_margin"] == pytest.approx(0.09) and m["drift_concentration_ratio"] > 1.0
    assert m["wind_drift_utilisation"] == pytest.approx(0.28)
    assert (m["Cs"], m["W_kip"], m["V_kip"], m["wind_V_kip"], m["T1_s"]) == (0.1009, 13476.0, 1360.0, 503.0, 1.115)
    assert m["dc_max"] == 0.9 and m["n_over"] == 0 and m["rho"] == 1.0 and m["torsion_Ax"] == 1.0
    assert m["n_columns"] == 24 and m["n_beams"] == 36 and m["n_braces"] == 0
    assert m["steel_tons"] > 0 and m["steel_psf"] > 0
    assert m["representable"] is True and m["is_dual"] is False
    assert m["system"].startswith("SMF")


def test_metrics_flag_failed_checks_and_unrepresentable_systems():
    m = mx.extract(mx.unzip(make_package(dc=1.07)))
    assert m["dc_max"] == 1.07 and m["n_over"] == 1
    r = mx.representability({"system": "Dual SMF + BRBF, R = 8"})
    assert r["representable"] is False and r["is_dual"] is True and "BRB" in r["not_representable_because"]
    assert mx.representability({"system": "Dual SMF + SCBF"})["representable"] is True
    assert mx.representability({})["representable"] is None


def test_brief_is_read_back_from_the_agents_first_turn():
    files = mx.unzip(make_package(brief="6-story office\n180 x 120 ft"))
    assert hr.brief_from_package(files) == "6-story office\n180 x 120 ft"
    assert hr.brief_from_package({"conversation.json": b'{"messages":[{"role":"user","content":"plain"}]}'}) == "plain"
    assert hr.brief_from_package({}) == ""


# ---------------------------------------------------------------- scoring
def test_equation_parsing_is_whitelisted():
    assert scoring.looks_like_equation(scoring.DEFAULT_EQUATION)
    assert scoring.looks_like_equation("S = 0.5·drift_margin + 0.5×(1 − steel_tons/steel_tons_max)")
    assert not scoring.looks_like_equation("cost matters most, then drift")
    assert not scoring.looks_like_equation("S = foo + 1")
    with pytest.raises(scoring.EquationError):
        scoring.compile_equation("__import__('os').system('x')")
    with pytest.raises(scoring.EquationError):
        scoring.compile_equation("S = drift_margin.real")
    with pytest.raises(scoring.EquationError):
        scoring.compile_equation("S = 1 + unknown_metric")
    tree, names = scoring.compile_equation("S = max(0, 1 - modelled_cost/modelled_cost_max) ** 2")
    assert names == {"max", "modelled_cost", "modelled_cost_max"}


def _rows():
    def row(i, **m):
        base = {"steel_tons": 1000.0, "floor_area_sf": 100000.0, "drift_utilisation": 0.8, "drift_margin": 0.2,
                "drift_concentration_ratio": 1.2, "n_moment_conn": 200, "n_braces": 0, "dc_max": 0.9, "n_over": 0,
                "rho": 1.0, "torsion_Ax": 1.0, "torsion_class": "none", "representable": True, "is_dual": False, "system": "SMF"}
        base.update(m)
        return {"id": f"M{i:03d}", "title": f"v{i}", "status": "done", "metrics": base}
    return [row(1), row(2, steel_tons=800.0, n_moment_conn=120), row(3, dc_max=1.05, n_over=2),
            row(4, drift_utilisation=1.1, drift_margin=-0.1), row(5, system="Dual SMF + BRBF", representable=False, is_dual=True),
            row(6, is_dual=True, smf_share_pct=18.0, system="Dual SMF + SCBF"), row(7, torsion_class="1b extreme"),
            row(8, rho=None), row(9, wind_comfort_mg=22.0), {"id": "M010", "title": "np", "status": "np", "metrics": {}}]


def test_eligibility_reasons_match_the_study_rule():
    rows = _rows()
    out = scoring.score_rows(rows, scoring.DEFAULT_EQUATION, scoring.DEFAULT_ELIGIBILITY, scoring.DEFAULT_RATES)
    by = {r["id"]: r for r in rows}
    assert by["M001"]["eligible"] and by["M002"]["eligible"]
    assert "exceeds 1.0" in by["M003"]["ineligible"][0] and "2 check(s) fail" in by["M003"]["ineligible"][1]
    assert "drift utilisation 1.10 > 1.0" in by["M004"]["ineligible"][0]
    assert "not verifiable" in by["M005"]["ineligible"][0]
    assert "moment-frame share 18% < 25%" in by["M006"]["ineligible"][0]
    assert "Type 1b" in by["M007"]["ineligible"][0]
    assert "rho not shown" in by["M008"]["ineligible"][0]
    assert "wind comfort 22 mg > 15 mg" in by["M009"]["ineligible"][0]
    assert by["M010"]["ineligible"][0] == "not permitted"
    assert out["n_eligible"] == 2 and out["n_scored"] == 2
    # cheaper + fewer connections wins
    assert by["M002"]["rank"] == 1 and by["M001"]["rank"] == 2 and by["M003"]["rank"] is None
    assert by["M002"]["metrics"]["modelled_cost"] == 800 * 4500 + 120 * 6000
    assert by["M002"]["metrics"]["net_value"] == 100000 * 400 - by["M002"]["metrics"]["modelled_cost"]
    assert out["rankings"]["modelled_cost"][0] == "M002"


def test_rules_can_be_relaxed_and_equation_reweighted():
    rows = _rows()
    rules = {**scoring.DEFAULT_ELIGIBILITY, "representable_only": False, "wind_comfort_max_mg": None, "smf_share_min_pct": None}
    out = scoring.score_rows(rows, "S = 1 - steel_tons/steel_tons_max", rules, {"steel_per_ton": 1.0})
    by = {r["id"]: r for r in rows}
    assert by["M005"]["eligible"] and by["M006"]["eligible"] and by["M009"]["eligible"]
    assert out["n_eligible"] == 5
    assert by["M002"]["score"] == pytest.approx(0.2) and by["M001"]["score"] == 0.0


def test_missing_metric_is_reported_not_crashed():
    rows = [{"id": "M001", "title": "a", "status": "done", "metrics": {"steel_tons": 10.0, "floor_area_sf": 1.0, "rho": 1, "torsion_Ax": 1}}]
    scoring.score_rows(rows, "S = drift_margin", {}, {})
    assert rows[0]["eligible"] and rows[0]["score"] is None and "drift_margin not available" in rows[0]["score_note"]


# ---------------------------------------------------------------- prompts
def test_prompts_carry_the_brief_and_the_chosen_mode():
    u = prompts.plan_user("brief text", 12, "categories", ["core"], "")
    assert "brief text" in u and "N = 12" in u and "Core / braced-frame" in u and "Lateral system type" not in u
    u = prompts.plan_user("b", 5, "instructions", [], "compare cores")
    assert "compare cores" in u
    assert "M001" in prompts.PLAN_SYSTEM and "n_moment_conn" in prompts.READ_SYSTEM
    assert all(n in prompts.SCORE_SYSTEM for n in scoring.METRIC_NAMES)


# ---------------------------------------------------------------- API flow (HR Steel patched)
@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    calls = {"runs": [], "stops": []}

    def fake_run(base_url, building, brief, on_event, should_stop, resume=False):
        calls["runs"].append((building, brief))
        on_event({"type": "status", "text": f"designing {building}"})
        on_event({"type": "token", "text": "hello "}); on_event({"type": "token", "text": "world"})
        on_event({"type": "tool", "name": "run_python", "title": "run_python: model"})
        on_event({"type": "tool_result", "name": "run_python", "summary": "ok", "ms": 100})
        if building.endswith("M003"):
            on_event({"type": "paused", "reason": "NOT PERMITTED: ASCE 7-22 Table 12.2-1 height limit"})
            return {"status": "paused", "reason": "NOT PERMITTED: ASCE 7-22 Table 12.2-1 height limit"}
        if building.endswith("M004"):
            on_event({"type": "error", "text": "engine crashed"})
            return {"status": "failed", "reason": "engine crashed"}
        if should_stop():
            return {"status": "stopped", "reason": "stopped by user"}
        on_event({"type": "done"})
        return {"status": "done", "reason": ""}

    def fake_download(base_url, building):
        if building.endswith("M004"):
            raise hr.DesignError("no package")
        k = int(building[-3:]) if building[-3:].isdigit() else 0
        return make_package(building, scale=1.0 + (k % 3) * 0.1, brief="4-story office brief")

    monkeypatch.setattr(vm.hr, "run_design", fake_run)
    monkeypatch.setattr(vm.hr, "download", fake_download)
    monkeypatch.setattr(vm.hr, "healthy", lambda url: True)
    monkeypatch.setattr(vm.hr, "stop", lambda url, b: calls["stops"].append(b))
    vm.llm.set_creds({"model": "MOCK", "base_url": "x", "api_key": "k"})
    c = TestClient(vm.app)
    c.calls = calls
    return c


def _wait_idle(client, project, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not client.get(f"/api/project/{project}").json()["running"]:
            return
        time.sleep(0.05)
    raise AssertionError("study did not finish")


def test_api_flow_plan_run_score_select(client):
    p = "Study1"
    assert client.get("/api/me").json()["has_creds"] is True
    # base brief: from the project's design (patched download) or typed
    d = client.get(f"/api/project/{p}/base/from-design").json()
    assert d["brief"] == "4-story office brief"
    r = client.post(f"/api/project/{p}/base", json={"brief": "4-story office, SMF", "source": "typed"})
    assert r.status_code == 200 and r.json()["base_brief"] == "4-story office, SMF"
    # plan without an LLM: library, note says so in instructions mode
    r = client.post(f"/api/project/{p}/plan", json={"n": 5, "mode": "categories", "categories": ["core", "system"]})
    assert r.status_code == 200 and len(r.json()["plan"]) == 5 and "library" in r.json()["plan_source"]
    assert client.post(f"/api/project/{p}/plan", json={"n": 5, "mode": "categories", "categories": []}).status_code == 400
    r = client.post(f"/api/project/{p}/plan", json={"n": 5, "mode": "instructions", "instructions": "compare cores"})
    assert "without an LLM" in r.json()["plan_note"]
    # edit the plan
    plan = r.json()["plan"]
    plan[1]["title"] = "Edited title"
    r = client.put(f"/api/project/{p}/plan", json={"plan": plan})
    assert r.json()["plan"][1]["title"] == "Edited title" and "edited" in r.json()["plan_source"]
    # run all pending
    r = client.post(f"/api/project/{p}/run", json={})
    assert r.status_code == 200 and r.json()["ids"] == ["M001", "M002", "M003", "M004", "M005"]
    assert client.post(f"/api/project/{p}/run", json={}).status_code in (400, 409)     # already running or nothing left
    _wait_idle(client, p)
    st = client.get(f"/api/project/{p}").json()
    by = {r["id"]: r for r in st["rows"]}
    assert by["M001"]["status"] == "done" and by["M002"]["status"] == "done" and by["M005"]["status"] == "done"
    assert by["M003"]["status"] == "np" and "NOT PERMITTED" in by["M003"]["reason"]
    assert by["M004"]["status"] == "failed" and by["M004"]["reason"] == "engine crashed"
    assert by["M001"]["metrics"]["steel_tons"] > 0 and by["M001"]["files"] == ["package.zip", "report.html", "viewer_3d.html"]
    assert by["M001"]["metrics"]["n_moment_conn_estimated"] is True          # no LLM: estimated and flagged
    # the variation brief carries the base + the change block, M001 the base only
    runs = dict(client.calls["runs"])
    assert runs[f"{p}_M001"] == "4-story office, SMF"
    assert runs[f"{p}_M002"].startswith("4-story office, SMF\n\n=== DESIGN VARIATION M002: Edited title ===")
    # events are re-attachable after the fact
    ev = client.get(f"/api/project/{p}/events?since=0").text
    assert '"type": "start"' in ev and '"type": "finished"' in ev and "hello world" in ev
    # files
    assert client.get(f"/api/project/{p}/file/M001/report.html").status_code == 200
    assert client.get(f"/api/project/{p}/file/M004/package.zip").status_code == 404
    assert client.get(f"/api/project/{p}/file/../M001/report.html").status_code in (400, 404)
    # score: default, typed, words without LLM, bad
    r = client.post(f"/api/project/{p}/score", json={"text": ""})
    assert r.status_code == 200
    sc = r.json()["scoring"]
    assert sc["equation_source"] == "default" and sc["summary"]["n_eligible"] == 3 and sc["summary"]["n_scored"] == 3
    ranked = sorted([x for x in r.json()["rows"] if x["rank"]], key=lambda x: x["rank"])
    assert ranked[0]["metrics"]["modelled_cost"] <= ranked[-1]["metrics"]["modelled_cost"]
    r = client.post(f"/api/project/{p}/score", json={"text": "S = 0.5*drift_margin + 0.5*(1 - steel_tons/steel_tons_max)",
                                                     "rules": {"drift_utilisation_max": 0.5}, "rates": {"steel_per_ton": 5000}})
    assert r.status_code == 200 and r.json()["scoring"]["equation_source"] == "typed"
    assert r.json()["scoring"]["summary"]["n_eligible"] == 0 and r.json()["scoring"]["rates"]["steel_per_ton"] == 5000.0
    assert client.post(f"/api/project/{p}/score", json={"text": "cost matters most"}).status_code == 400
    assert client.post(f"/api/project/{p}/score", json={"text": "S = 1 + nope"}).status_code == 400
    client.post(f"/api/project/{p}/score", json={"text": ""})
    # the study folder holds the results for people and other modules
    sd = vm.study_dir(p)
    assert (sd / "results.csv").is_file() and (sd / "results.json").is_file() and (sd / "state.json").is_file()
    # select
    r = client.post(f"/api/project/{p}/select", json={"ids": ["M002", "M001"], "note": "two lightest"})
    assert r.status_code == 200
    sel = r.json()["selection"]["selected"]
    assert [s["id"] for s in sel] == ["M002", "M001"] and all(s["rank"] for s in sel)
    assert (sd / "selected" / pathlib.Path(sel[0]["package"]).name).is_file()
    rec = json.loads((vm.JOBS / p / "selected_variations.json").read_text())
    assert rec["note"] == "two lightest" and rec["selected"][0]["package"].startswith("variations/selected/")
    assert client.post(f"/api/project/{p}/select", json={"ids": ["M099"]}).status_code == 400
    assert client.get(f"/api/project/{p}").json()["selection"]["ids"] == ["M002", "M001"]
    # re-run only what is not done
    r = client.post(f"/api/project/{p}/run", json={})
    assert r.json()["ids"] == ["M003", "M004"]
    _wait_idle(client, p)
    # drop a result
    assert client.delete(f"/api/project/{p}/variation/M005").status_code == 200
    assert client.get(f"/api/project/{p}").json()["rows"][4]["status"] == "pending"


def test_run_refuses_without_plan_or_creds(client):
    p = "Study2"
    assert client.post(f"/api/project/{p}/run", json={}).status_code == 400
    client.post(f"/api/project/{p}/base", json={"brief": "x"})
    client.post(f"/api/project/{p}/plan", json={"n": 2, "mode": "auto"})
    vm.llm.set_creds({})
    r = client.post(f"/api/project/{p}/run", json={})
    assert r.status_code == 400 and "connection" in r.json()["detail"]


def test_stop_ends_the_study_early(client, monkeypatch):
    p = "Study3"
    client.post(f"/api/project/{p}/base", json={"brief": "x"})
    client.post(f"/api/project/{p}/plan", json={"n": 6, "mode": "auto"})
    gate = {"go": False}
    orig = vm.hr.run_design

    def slow_run(base_url, building, brief, on_event, should_stop, resume=False):
        for _ in range(200):
            if should_stop():
                return {"status": "stopped", "reason": "stopped by user"}
            time.sleep(0.01)
        return orig(base_url, building, brief, on_event, should_stop, resume)
    monkeypatch.setattr(vm.hr, "run_design", slow_run)
    client.post(f"/api/project/{p}/run", json={})
    time.sleep(0.1)
    r = client.post(f"/api/project/{p}/stop")
    assert r.json()["running"] is True and client.calls["stops"] == [f"{p}_M001"]
    _wait_idle(client, p)
    rows = client.get(f"/api/project/{p}").json()["rows"]
    assert rows[0]["status"] == "stopped" and all(x["status"] == "pending" for x in rows[1:])
    assert "skipped" in rows[1]["reason"]


def test_llm_paths_plan_read_and_interpret(client, monkeypatch):
    """With a model: the plan comes from it (validated), the report is read for the values the
    package does not state, and a description in words becomes an equation shown back."""
    p = "Study4"
    canned = {"plan": json.dumps({"variations": [
        {"id": "M001", "title": "Base design as briefed", "group": "reference", "change": "", "why": "Reference"},
        {"id": "M002", "title": "Two-story X braces", "group": "core", "change": "Configure the core as two-story X.", "why": "tonnage"},
        {"id": "M003", "title": "Two-story X braces", "group": "core", "change": "Configure the core as two-story X.", "why": "duplicate"},
        {"id": "M004", "title": "SCBF only", "group": "system", "change": "Use SCBF alone (R = 6).", "why": "height gate"}]}),
        "read": json.dumps({"system_X": "SMF", "system_Y": "SMF", "is_dual": False, "n_moment_conn": 96, "smf_share_pct": None,
                            "wind_comfort_mg": 9.5, "not_permitted": False, "np_clause": "", "np_list": [], "notes": ""}),
        "score": '```json\n{"equation": "S = 0.7*(1 - modelled_cost/modelled_cost_max) + 0.3*drift_margin", "explanation": "cost first"}\n```'}
    seen = []

    def fake_chat(system, user, **kw):
        seen.append(system[:40])
        if system.startswith("You are a senior structural engineer"):
            return canned["plan"]
        if system.startswith("You read a steel-design report"):
            return canned["read"]
        return canned["score"]
    monkeypatch.setattr(vm.llm, "available", lambda: True)
    monkeypatch.setattr(vm.llm, "chat", fake_chat)
    vm.llm.set_creds({"model": "test-model", "base_url": "http://x", "api_key": "k"})
    client.post(f"/api/project/{p}/base", json={"brief": "4-story office"})
    r = client.post(f"/api/project/{p}/plan", json={"n": 4, "mode": "instructions", "instructions": "cores vs frames"})
    d = r.json()
    assert d["plan_source"] == "test-model" and [v["id"] for v in d["plan"]] == ["M001", "M002", "M003"]   # duplicate dropped
    assert d["plan"][2]["title"] == "SCBF only" and "3 distinct" in d["plan_note"]
    client.post(f"/api/project/{p}/run", json={"ids": ["M001"]})
    _wait_idle(client, p)
    row = client.get(f"/api/project/{p}").json()["rows"][0]
    assert row["status"] == "done" and row["metrics"]["n_moment_conn"] == 96 and "n_moment_conn_estimated" not in row["metrics"]
    assert row["metrics"]["wind_comfort_mg"] == 9.5 and row["llm_read"]["system_X"] == "SMF"
    r = client.post(f"/api/project/{p}/score", json={"text": "cost matters most, then drift margin"})
    assert r.status_code == 200
    sc = r.json()["scoring"]
    assert sc["equation"] == "0.7*(1 - modelled_cost/modelled_cost_max) + 0.3*drift_margin"
    assert sc["equation_source"] == "interpreted by test-model" and sc["explanation"] == "cost first"
    assert sc["source_text"] == "cost matters most, then drift margin"
    assert r.json()["rows"][0]["score"] == pytest.approx(0.7 * 0 + 0.3 * 0.09, abs=1e-3)
    assert any(s.startswith("You turn a designer") for s in seen)


def test_state_write_survives_windows_replace_race(monkeypatch):
    """On Windows os.replace raises PermissionError while the UI holds state.json open for a poll; save_state must
    retry rather than lose the write (the study thread's final status is what the stop test reads back)."""
    real = os.replace; calls = {"n": 0}
    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(5, "Access is denied")
        return real(src, dst)
    monkeypatch.setattr(os, "replace", flaky)
    vm.update_state("RaceStudy", lambda st: st.__setitem__("base_brief", "x"))
    assert calls["n"] == 3 and vm.load_state("RaceStudy")["base_brief"] == "x"
