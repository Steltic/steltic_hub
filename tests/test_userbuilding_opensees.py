"""User-designed building: OpenSees models (uploaded or pasted) and buildings described in plain words.

The OpenSees interpreter (userbuilding/opensees) runs a script against a recorder and reads the building from the
model it builds; the text box (userbuilding/describe.py) turns plain words into an OpenSeesPy script through the
hub's model connection (a stand-in here) and reads that the same way. HR Steel is a stand-in that records what it
was sent. The AISC table is a 106-shape extract of HR's (tests/fixtures/opensees/engine/aisc_shapes.csv).

    python -m pytest tests/test_userbuilding_opensees.py -q

The Tcl fixture needs a Python with Tcl (tkinter) and the IMK fixture one with numpy: the tests look for them in
this Python, NL_PYTHON, STELTIC_TEST_TCL_PYTHON / STELTIC_TEST_NUMPY_PYTHON, and skip when there is none.
"""
import importlib, json, os, pathlib, subprocess, sys, zipfile

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MOD = ROOT / "steltic_hub" / "catalog" / "steltic_userbuilding"
FIX = ROOT / "tests" / "fixtures" / "opensees"
sys.path.insert(0, str(MOD))

from userbuilding import describe as DS          # noqa: E402
from userbuilding import llm as ullm             # noqa: E402
from userbuilding import opensees as OS          # noqa: E402
from userbuilding.opensees import convert as CV  # noqa: E402
from userbuilding.opensees import shapes as SH   # noqa: E402


@pytest.fixture(autouse=True)
def table(monkeypatch):
    monkeypatch.setenv("STELTIC_ENGINE_DIR", str(FIX / "engine"))
    SH._SHAPES = None
    yield
    SH._SHAPES = None


def _python_with(code, *env_names):
    cands = [sys.executable] + [os.environ.get(n) for n in env_names] + [os.environ.get("NL_PYTHON")]
    for c in cands:
        if c and os.path.exists(c):
            try:
                if subprocess.run([c, "-c", code], capture_output=True, timeout=60).returncode == 0:
                    return c
            except (OSError, subprocess.SubprocessError):
                pass
    return None


TK_PY = _python_with("import tkinter; tkinter.Tcl().eval('expr 1')", "STELTIC_TEST_TCL_PYTHON")
NP_PY = _python_with("import numpy", "STELTIC_TEST_NUMPY_PYTHON")


def _props(d):
    return {k: v["value"] for k, v in (d.get("_proposals") or {}).items()}


def _secmap(d):
    return {k.split("/", 1)[1]: v for k, v in _props(d).items() if k.startswith("section_map/")}


# ------------------------------------------------------------------------------------------- units, main file
@pytest.mark.parametrize("E,name", [(29000.0, "kip-in"), (29000.0 * 144.0, "kip-ft"), (2.0e8, "kN-m"),
                                    (200000.0, "N-mm"), (2.0e11, "N-m"), (29.0e6, "lb-in")])
def test_units_from_the_steel_modulus(E, name):
    m = {"materials": {"1": {"type": "Elastic", "args": [E]}}}
    assert CV.detect_units(m, [])[0] == name


def test_no_steel_modulus_asks_for_units():
    with pytest.raises(CV.IntakeError, match="^UNITS"):
        CV.detect_units({"materials": {}}, [])
    with pytest.raises(CV.IntakeError, match="^UNITS"):
        CV.detect_units({"materials": {"1": {"type": "Elastic", "args": [1.0]}}}, [])


def test_no_model_and_2d_models_are_refused():
    with pytest.raises(CV.IntakeError, match="built no model"):
        CV.convert({"model": None, "stopped": {"why": "NameError: x"}})
    with pytest.raises(CV.IntakeError, match="2-D model"):
        CV.convert({"model": {"ndm": 2, "elements": {"1": {"type": "elasticBeamColumn", "nodes": [1, 2], "args": []}},
                              "nodes": {"1": [0, 0], "2": [0, 1]}}})
    with pytest.raises(CV.IntakeError, match="NO_TCL"):
        CV.convert({"error": "NO_TCL"})


def test_the_main_script_of_a_zip(tmp_path):
    z = tmp_path / "m.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("proj/run_all.tcl", "source frame.tcl\nsource sections.tcl\n")
        zf.writestr("proj/frame.tcl", "source sections.tcl\n")
        zf.writestr("proj/sections.tcl", "# helpers\n")
        zf.writestr("__MACOSX/proj/._frame.tcl", "x")
    scripts = OS.unpack(z, tmp_path / "x")
    assert scripts == ["proj/frame.tcl", "proj/run_all.tcl", "proj/sections.tcl"]
    assert OS.pick_main(tmp_path / "x", scripts) == "proj/run_all.tcl"
    assert OS.pick_main(tmp_path / "x", scripts, main="frame.tcl") == "proj/frame.tcl"
    with pytest.raises(OS.IntakeError, match="^MAIN"):
        OS.pick_main(tmp_path / "x", scripts, main="other.tcl")
    with zipfile.ZipFile(tmp_path / "two.zip", "w") as zf:
        zf.writestr("a_model.py", "x = 1\n")
        zf.writestr("b_model.py", "y = 2\n")
    sc = OS.unpack(tmp_path / "two.zip", tmp_path / "y")
    with pytest.raises(OS.IntakeError, match="^MAIN: .*a_model.py, b_model.py"):
        OS.pick_main(tmp_path / "y", sc)


def test_a_zip_entry_outside_its_folder_is_refused(tmp_path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../../outside.py", "print(1)\n")
    with pytest.raises(OS.IntakeError, match="outside its folder"):
        OS.unpack(z, tmp_path / "x")
    assert not (tmp_path.parent / "outside.py").exists()


# ------------------------------------------------------------------------------------------- the fixtures
def test_openseespy_kip_ft_building(tmp_path):
    """Plain OpenSeesPy, kip-ft, Z up; the script wipes and builds a one-column check model at the end."""
    d, r = OS.read_opensees(FIX / "two_story_kipft.py", tmp_path, nl_python=sys.executable)
    assert r["units"]["name"] == "kip-ft" and r["up"]["axis"] == "Z" and r["models"] == 2
    assert [l["elev_ft"] for l in d["levels"]] == [0.0, 14.0, 26.0]
    assert [g["coord_ft"] for g in d["grid"]["x"]] == [0.0, 30.0, 60.0]
    assert [g["coord_ft"] for g in d["grid"]["y"]] == [0.0, 25.0]
    assert len(d["columns"]) == 12 and {c["strong_axis"] for c in d["columns"]} == {"X"}
    rel = sorted((b.get("release") or "none") for b in d["beams"])
    assert rel == ["both"] * 6 + ["none"] * 8                     # gravity beams pinned by -releasey, frames fixed
    assert d["supports"] == {"default": "fixed"} and d["diaphragm"] == "rigid"
    assert d["user_results"]["W_kip"] == pytest.approx(500.0, rel=1e-3)
    assert sorted(_secmap(d).values()) == ["W14X90", "W18X40", "W24X94"]
    assert _props(d)["loads/D_psf"] == {"L1": pytest.approx(200.0, rel=1e-3), "L2": pytest.approx(133.3, rel=1e-3)}
    notes = " ".join(d["_intake"]["notes"])
    assert "wipes and rebuilds" in notes and "L1 120 kip" in notes
    assert json.loads((tmp_path / "conversion.json").read_text())["main"] == "two_story_kipft.py"


def test_chosen_units_that_disagree_with_the_modulus_are_flagged(tmp_path):
    d, r = OS.read_opensees(FIX / "two_story_kipft.py", tmp_path, units="kip-in", nl_python=sys.executable)
    assert r["units"]["name"] == "kip-in" and "reads as kip-ft" in r["units"]["how"]
    assert any("units kip-in were chosen" in n for n in d["_intake"]["notes"])


@pytest.mark.skipif(NP_PY is None, reason="no Python with numpy to run the OpenSeesPy IMK fixture")
def test_openseespy_y_up_si_concentrated_plasticity(tmp_path):
    """N-mm, Y up, IMK springs at the beam ends, pinned gravity beams through zeroLength links, numpy and opsvis,
    and a ground motion file that is not there: the model built before the script stops is read."""
    d, r = OS.read_opensees(FIX / "imk_yup_si.py", tmp_path, nl_python=NP_PY)
    assert r["units"]["name"] == "N-mm" and r["up"]["axis"] == "Y"
    assert r["stopped"]["kind"] == "error" and "RSN953" in r["stopped"]["why"]
    assert len(d["levels"]) == 4 and len(d["columns"]) == 36 and {c["strong_axis"] for c in d["columns"]} == {"X"}
    assert not any(c.get("release") for c in d["columns"])       # beam hinges belong to the beams, not the columns
    rel = [(b.get("release") or "none") for b in d["beams"]]
    assert rel.count("both") == 33 and rel.count("none") == 18
    assert sorted(_secmap(d).values()) == ["W14X82", "W16X31", "W24X84"]
    notes = " ".join(d["_intake"]["notes"])
    assert "ModIMKPeakOriented" in notes and "stopped at imk_yup_si.py" in notes
    assert d["grid"]["y"][-1]["coord_ft"] == 0.0                  # plan Y = -(model Z), no -0.0


@pytest.mark.skipif(TK_PY is None, reason="no Python with Tcl (tkinter) to run the Tcl fixture")
def test_opensees_tcl_fibre_building(tmp_path):
    d, r = OS.read_opensees(FIX / "smf_scbf_fiber.tcl", tmp_path, nl_python=TK_PY)
    assert r["language"] == "tcl" and r["units"]["name"] == "kip-in" and r["up"]["axis"] == "Z"
    assert [l["elev_ft"] for l in d["levels"]] == [0.0, 15.0, 28.0]
    assert len(d["columns"]) == 18 and len(d["braces"]) == 4
    assert sorted(set(_secmap(d).values())) == ["W14X90", "W18X40", "W24X94"]
    assert d["diaphragm"] == "rigid" and d["supports"] == {"default": "fixed"}
    assert not any(n.startswith("load pattern") and " 0 kip" in n for n in d["_intake"]["notes"])


def test_tcl_without_tcl_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(OS, "_has_tk", lambda py: False)
    with pytest.raises(OS.IntakeError, match="NO_TCL"):
        OS.read_opensees(FIX / "smf_scbf_fiber.tcl", tmp_path, nl_python=sys.executable)


# ------------------------------------------------------------------------------------------- plain words
WORDS = ("Office, 3 stories: 15 ft first story, 13 ft above (Level 2, Level 3, Roof). Grid 1-4 at 30 ft east-west, "
         "A-C at 25 ft north-south. W14X90 columns, W14X132 in the first story, strong axis east-west. Moment frames on "
         "lines A and C with W24X76 beams, all other beams W16X31 on shear tabs. Chevron braces HSS8X8X1/2 on lines 1 and 4 "
         "between A and B, every story. Rigid diaphragm, fixed bases. SDS = 1.0, SD1 = 0.6, site class D. Dead load 90 psf. "
         "Seismic weight 800 kip per floor, roof 600 kip. T1 = 1.1 s in X.")
SPEC = {"name": "Office",
        "grid": {"x": {"labels": ["1", "2", "3", "4"], "bays_ft": [30, 30, 30]}, "y": {"labels": ["A", "B", "C"], "bays_ft": [25, 25]}},
        "stories_ft": [15, 13, 13], "level_names": ["Level 2", "Level 3", "Roof"], "supports": "fixed", "diaphragm": "rigid",
        "columns": [{"section": "W14X90", "x": "all", "y": "all", "stories": "all", "strong_axis": "X"},
                    {"section": "W14X132", "x": "all", "y": "all", "stories": [1, 1], "strong_axis": "X"}],
        "beams": [{"section": "W16X31", "lines": "all", "bays": "all", "floors": "all", "connection": "pinned"},
                  {"section": "W24X76", "lines": "A, C", "bays": "all", "floors": "all", "connection": "moment"}],
        "braces": [{"section": "HSS8X8X1/2", "lines": ["1", "4"], "bays": ["A-B"], "stories": "all", "pattern": "chevron"}],
        "seismic_weight_kip": [800, 800, 600],
        "basis": {"seismic.SDS": {"value": 1.0, "quote": "SDS = 1.0"}, "seismic.site_class": {"value": "D", "quote": "site class D"},
                  "loads.D_psf": {"value": 90, "quote": "Dead load 90 psf"},
                  "loads.L_psf": {"value": 50, "quote": "live load 50 psf"},           # not in the text: not taken
                  "user_results.T1_s.X": {"value": 1.1, "quote": "T1 = 1.1 s in X"}},
        "unclear": ["Which seismic force-resisting system is it in each direction?"]}


def test_text_or_code():
    assert DS.classify(WORDS) == "words"
    assert DS.classify((FIX / "two_story_kipft.py").read_text()) == "py"
    assert DS.classify((FIX / "imk_yup_si.py").read_text()) == "py"
    assert DS.classify((FIX / "smf_scbf_fiber.tcl").read_text()) == "tcl"
    assert DS.classify("a 3 story building with a node at every grid point") == "words"


def test_what_the_words_must_still_say():
    bad = json.loads(json.dumps(SPEC))
    bad["beams"][0]["connection"] = None
    bad["columns"][0]["strong_axis"] = None
    bad["columns"][1]["section"] = "W14X999"
    bad["braces"][0]["lines"] = ["Q"]
    plan, needs, warns = DS.check(bad, WORDS)
    txt = " | ".join(needs)
    assert "moment-connected or pinned" in txt and "strong axis" in txt and "'W14X999' is not an AISC shape" in txt
    assert "no grid line Q" in txt
    _p, needs, _w = DS.check({}, "")
    assert any("story heights" in n for n in needs) and any("grid in X" in n for n in needs)


def _words_to_description(spec, text, tmp_path):
    plan, needs, warns = DS.check(spec, text)
    assert needs == []
    src = DS.script(plan, text, "stand-in model")
    f = tmp_path / "described_model.py"
    f.write_text(src)
    d, r = OS.read_opensees(f, tmp_path / "w", nl_python=sys.executable)
    return DS.finish(d, r, plan, "stand-in model", text, "described_model.py"), r, plan, warns


def test_words_become_the_building(tmp_path):
    d, r, plan, warns = _words_to_description(SPEC, WORDS, tmp_path)
    assert warns == []
    assert [g["label"] for g in d["grid"]["x"]] == ["1", "2", "3", "4"] and [g["label"] for g in d["grid"]["y"]] == ["A", "B", "C"]
    assert [l["name"] for l in d["levels"]] == ["Base", "Level 2", "Level 3", "Roof"]
    assert [l["elev_ft"] for l in d["levels"]] == [0.0, 15.0, 28.0, 41.0]
    cols = {(c["x"], c["y"], c["base_level"]): c for c in d["columns"]}
    assert len(cols) == 36 and cols[("1", "A", "Base")]["section"] == "W14X132" and cols[("1", "A", "Level 2")]["section"] == "W14X90"
    assert {c["strong_axis"] for c in d["columns"]} == {"X"}
    mf = [b for b in d["beams"] if b["section"] == "W24X76"]
    assert len(mf) == 18 and all((b.get("release") or "none") == "none" and b["i"]["y"] in ("A", "C") for b in mf)
    grav = [b for b in d["beams"] if b["section"] == "W16X31"]
    assert {b.get("release") for b in grav} == {"both", "I", "J"}          # the chevron bays are split at mid-span
    assert len(d["braces"]) == 12 and {b["section"] for b in d["braces"]} == {"HSS8X8X1/2"}
    assert not _secmap(d)                                                 # every section is named, none proposed
    assert d["supports"] == {"default": "fixed"} and d["diaphragm"] == "rigid"
    assert d["user_results"] == {"W_kip": pytest.approx(2200.0, rel=1e-3), "T1_s": {"X": 1.1}}
    p = d["_proposals"]
    assert p["seismic/SDS"] == {"value": 1.0, "reason": 'from your description: "SDS = 1.0"'}
    assert p["loads/D_psf"]["value"] == 90 and "loads/L_psf" not in p
    notes = " ".join(d["_intake"]["notes"])
    assert "not taken from the description: loads.L_psf" in notes and "seismic force-resisting system" in notes
    assert d["source"]["described"] and d["project"]["name"] == "Office"
    assert all(row.get("named") for row in r["sections"])


def test_words_with_ebf_brb_v_setback_and_no_supports(tmp_path):
    spec = {"grid": {"x": {"bays_ft": [30, 30, 30, 30]}, "y": {"coords_ft": [0, 25, 50]}},
            "stories_ft": [16, 13, 13, 13], "supports": None, "diaphragm": "rigid",
            "columns": [{"section": "W14X120", "x": "all", "y": "all", "stories": "all", "strong_axis": "Y"},
                        {"section": "W14X120", "x": ["X5"], "y": "all", "stories": [4, 4], "omit": True}],
            "beams": [{"section": "W18X35", "lines": "all", "bays": "all", "floors": "all", "connection": "pinned"},
                      {"section": "W18X35", "lines": ["Y1", "Y2", "Y3"], "bays": [["X4", "X5"]], "floors": [4, 4], "omit": True},
                      {"section": "W18X35", "lines": ["X5"], "bays": "all", "floors": [4, 4], "omit": True},
                      {"section": "W21X50", "lines": ["Y1", "Y3"], "bays": [["X2", "X3"]], "floors": "all", "connection": "pinned"}],
            "braces": [{"brb_Asc_in2": 6.5, "lines": ["X1", "X5"], "bays": [["Y1", "Y2"]], "stories": [1, 2], "pattern": "two_story_x"},
                       {"section": "W10X49", "lines": ["Y1", "Y3"], "bays": [["X2", "X3"]], "stories": "all", "pattern": "eccentric", "link_ft": 3},
                       {"section": "HSS8X8X1/2", "lines": ["X1"], "bays": [["Y2", "Y3"]], "stories": [1, 1], "pattern": "v"}]}
    d, r, plan, warns = _words_to_description(spec, "", tmp_path)
    assert [l["name"] for l in d["levels"]] == ["Base", "L1", "L2", "L3", "L4"]
    assert [g["label"] for g in d["grid"]["x"]] == ["X1", "X2", "X3", "X4", "X5"]
    assert len(d["columns"]) == 57 and {c["strong_axis"] for c in d["columns"]} == {"Y"}
    assert sum(1 for b in d["beams"] if b.get("link")) == 8                # one link per EBF bay per floor
    brb = [b for b in d["braces"] if b.get("brb")]
    assert len(brb) == 8 and all(b["brb"] == {"Asc_in2": 6.5} and b["section"] is None for b in brb)
    assert len(d["braces"]) == 26 and not d.get("_unsupported")
    assert d["supports"] == {}                                            # not said: HR asks
    assert not _secmap(d)


def test_a_quote_that_is_not_in_the_text_is_not_taken():
    plan, needs, warns = DS.check(SPEC, WORDS)
    assert "loads.L_psf" not in plan["basis"] and "seismic.SDS" in plan["basis"]
    assert plan["not_taken"] and "live load 50 psf" in plan["not_taken"][0]


# ------------------------------------------------------------------------------------------- the routes
class FakeHR:
    def __init__(self):
        self.calls = []

    def __call__(self, method, path, timeout=120.0, **kw):
        self.calls.append((method, path, kw))
        req = httpx.Request(method, "http://hr" + path)
        if path == "/api/build_only/intake":
            b = (kw.get("json") or {}).get("building") or (kw.get("data") or {}).get("building")
            return httpx.Response(200, json={"building": b, "summary": {"columns": 1}, "missing_items": {
                "ready": False, "counts": {"key": 1, "default": 0, "refused": 0, "info": 0}, "groups": []}}, request=req)
        return httpx.Response(404, json={"detail": "?"}, request=req)


@pytest.fixture
def ub(monkeypatch, tmp_path):
    """The page's server, imported when first needed (test_userbuilding.py sets the environment it reads at import
    when it runs first) and pointed at this test's folders and a stand-in HR."""
    for k, v in (("HR_JOBS", tmp_path / "hr_jobs"), ("UB_JOBS", tmp_path / "ub_jobs"), ("STELTIC_URL", "http://127.0.0.1:9")):
        os.environ.setdefault(k, str(v))
    um = importlib.import_module("userbuilding.main")
    from fastapi.testclient import TestClient
    hr = FakeHR()
    monkeypatch.setattr(um, "_hr", hr)
    monkeypatch.setattr(um, "NL_PYTHON", sys.executable)
    monkeypatch.setattr(um, "UB_JOBS", tmp_path / "ub_jobs")
    monkeypatch.setattr(um, "HR_JOBS", tmp_path / "hr_jobs")
    return TestClient(um.app), hr


NO_E = """import openseespy.opensees as ops
ops.model('basic', '-ndm', 3, '-ndf', 6)
n = 0
for z in (0.0, 12.0):
    for x in (0.0, 20.0):
        for y in (0.0, 20.0):
            n += 1
            ops.node(n, x, y, z)
ops.geomTransf('Linear', 1, 0.0, 1.0, 0.0)
ops.geomTransf('Linear', 2, 0.0, 0.0, 1.0)
for k in range(1, 5):
    ops.fix(k, 1, 1, 1, 1, 1, 1)
    ops.element('elasticBeamColumn', k, k, k + 4, 0.2, 1.0, 0.4, 0.001, 0.02, 0.05, 1)
for t, (a, b) in enumerate(((5, 6), (7, 8), (5, 7), (6, 8)), 5):
    ops.element('elasticBeamColumn', t, a, b, 0.2, 1.0, 0.4, 0.001, 0.1, 0.01, 2)
"""


def test_upload_an_openseespy_file(ub):
    client, hr = ub
    r = client.post("/api/project/OsOne/upload", files={"file": ("two_story_kipft.py", (FIX / "two_story_kipft.py").read_bytes(), "text/x-python")})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["opensees"]["units"]["name"] == "kip-ft" and out["opensees"]["main"] == "two_story_kipft.py"
    m, path, kw = hr.calls[-1]
    assert path == "/api/build_only/intake" and kw["json"]["building"] == "OsOne" and kw["json"]["replace"] is False
    desc = kw["json"]["description"]
    assert desc["schema"] == "steltic.building_description" and desc["source"]["kind"] == "opensees" and len(desc["columns"]) == 12
    assert client.get("/api/project/OsOne/opensees").json()["units"]["name"] == "kip-ft"
    s = client.get("/api/project/OsOne/opensees/script")
    assert s.status_code == 200 and b"openseespy" in s.content and "two_story_kipft.py" in s.headers["content-disposition"]
    # a template / .e2k upload afterwards: the OpenSees report no longer applies
    client.post("/api/project/OsOne/upload", files={"file": ("b.e2k", b"$ CONTROLS\n", "text/plain")})
    assert client.get("/api/project/OsOne/opensees").json() == {}


def test_units_are_asked_when_the_modulus_does_not_say(ub):
    client, hr = ub
    r = client.post("/api/project/OsUnits/upload", files={"file": ("frame.py", NO_E.encode(), "text/x-python")})
    assert r.status_code == 422 and r.json()["code"] == "UNITS" and "kip-in" in r.json()["units"]
    assert not hr.calls
    r = client.post("/api/project/OsUnits/upload", data={"units": "kip-ft"}, files={"file": ("frame.py", NO_E.encode(), "text/x-python")})
    assert r.status_code == 200, r.text
    assert r.json()["opensees"]["units"] == {"name": "kip-ft", "how": "chosen"}


def test_a_zip_with_two_candidate_scripts_asks_for_the_main_one(ub, tmp_path):
    client, hr = ub
    z = tmp_path / "two.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("first_model.py", NO_E)
        zf.writestr("second_model.py", NO_E)
    r = client.post("/api/project/OsZip/upload", files={"file": ("two.zip", z.read_bytes(), "application/zip")})
    assert r.status_code == 422 and r.json()["code"] == "MAIN" and r.json()["candidates"] == ["first_model.py", "second_model.py"]
    r = client.post("/api/project/OsZip/upload", data={"main": "second_model.py", "units": "kip-ft"},
                    files={"file": ("two.zip", z.read_bytes(), "application/zip")})
    assert r.status_code == 200 and r.json()["opensees"]["main"] == "second_model.py"


def test_pasted_code_needs_no_model(ub, monkeypatch):
    client, hr = ub
    monkeypatch.setattr(ullm, "available", lambda: False)
    r = client.post("/api/project/OsTyped/describe", json={"text": (FIX / "two_story_kipft.py").read_text()})
    assert r.status_code == 200, r.text
    assert r.json()["opensees"]["typed"]["language"] == "py"
    assert hr.calls[-1][2]["json"]["description"]["source"]["file"] == "typed_model.py"


def test_words_without_a_model_connection(ub, monkeypatch):
    client, hr = ub
    monkeypatch.setattr(ullm, "available", lambda: False)
    r = client.post("/api/project/OsWords/describe", json={"text": WORDS})
    assert r.status_code == 422 and r.json()["code"] == "NO_LLM" and not hr.calls


def test_words_through_the_model(ub, monkeypatch):
    client, hr = ub
    seen = {}

    def chat_json(system, user, **kw):
        seen["system"], seen["user"] = system, user
        return json.loads(json.dumps(spec_box["spec"]))
    spec_box = {"spec": json.loads(json.dumps(SPEC))}
    spec_box["spec"]["beams"][0]["connection"] = None
    monkeypatch.setattr(ullm, "available", lambda: True)
    monkeypatch.setattr(ullm, "creds", lambda: {"model": "stand-in-model"})
    monkeypatch.setattr(ullm, "chat_json", chat_json)
    # first: the words leave the gravity beams' connection open -> asked, nothing sent to HR
    r = client.post("/api/project/OsWords/describe", json={"text": WORDS})
    assert r.status_code == 200 and any("moment-connected or pinned" in n for n in r.json()["needs"]) and not hr.calls
    assert seen["user"].endswith(WORDS) and "Return only the JSON object" in seen["system"]
    # then the full description
    spec_box["spec"] = SPEC
    r = client.post("/api/project/OsWords/describe", json={"text": WORDS, "replace": True})
    assert r.status_code == 200, r.text
    rep = r.json()["opensees"]
    assert rep["described"]["model"] == "stand-in-model" and rep["described"]["text"] == WORDS
    kw = hr.calls[-1][2]["json"]
    assert kw["replace"] is True and kw["description"]["levels"][-1]["name"] == "Roof"
    assert {c["section"] for c in kw["description"]["columns"]} == {"W14X90", "W14X132"}
    s = client.get("/api/project/OsWords/opensees/script")
    assert s.status_code == 200 and b"SECTIONS = {" in s.content and b"W24X76" in s.content
    assert client.get("/api/project/OsWords/opensees").json()["described"]["model"] == "stand-in-model"


def test_the_hub_pushes_the_model_connection(ub):
    client, _hr = ub
    try:
        r = client.post("/api/creds", json={"base_url": "http://127.0.0.1:1/v1", "model": "m", "api_key": "k"})
        assert r.json() == {"ok": True, "llm": True}
        assert client.get("/api/me").json()["model"] == "m"
        assert client.post("/api/creds", json={"model": "MOCK", "base_url": "x"}).json()["llm"] is False
    finally:
        ullm.set_creds(None)
