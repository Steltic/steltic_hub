"""User-designed building: a file and text together -- the text gives what the file leaves open.

values.py reads `name = value` lines directly and sentences through the hub's model connection (a stand-in here),
against the questions HR asks (and will ask); each value goes back to HR as a proposal quoting the words. HR Steel is a
stand-in with a small schema of its own: proposals turn a question into a default, R is asked once the system is set,
answers drop questions -- as HR's build_only.schema does.

    python -m pytest tests/test_userbuilding_text_values.py -q
"""
import copy, importlib, json, os, pathlib, sys

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MOD = ROOT / "steltic_hub" / "catalog" / "steltic_userbuilding"
FIX = ROOT / "tests" / "fixtures" / "opensees"
sys.path.insert(0, str(MOD))

from userbuilding import llm as ullm             # noqa: E402
from userbuilding import values as V             # noqa: E402
from userbuilding.opensees import shapes as SH   # noqa: E402


@pytest.fixture(autouse=True)
def table(monkeypatch):
    monkeypatch.setenv("STELTIC_ENGINE_DIR", str(FIX / "engine"))
    SH._SHAPES = None
    yield
    SH._SHAPES = None


# ------------------------------------------------------------------------------------------- a stand-in HR schema
def _set(d, path, v):
    cur = d
    segs = path.split("/")
    for s in segs[:-1]:
        if not isinstance(cur.get(s), dict):
            cur[s] = {}
        cur = cur[s]
    cur[segs[-1]] = v


def validate(d):
    """A few of HR's questions, the way HR asks them."""
    it = []
    se, lo = d.get("seismic") or {}, d.get("loads") or {}
    q = lambda iid, kind, **k: it.append(dict(id=iid, kind=kind, path=iid.replace(".", "/"), group="g", question=iid + "?",
                                              needed_for="x", **k))
    if not se.get("SDS"):
        q("seismic.SDS", "key")
    if not se.get("site_class"):
        q("seismic.site_class", "key", options=["A", "B", "BC", "C", "CD", "D", "DE", "E"])
    if not (se.get("system") or {}).get("X"):
        q("seismic.system.X", "key", options=["SCBF", "SMF"])
    elif (se.get("R") or {}).get("X") is None:
        q("seismic.R.X", "default", default=8.0, default_reason="ASCE 7-22 Table 12.2-1")
    if lo.get("D_psf") is None:
        q("loads.D_psf", "key")
    if lo.get("Lr_psf") is None:
        q("loads.Lr_psf", "default", default=20.0, default_reason="ASCE 7-22 Table 4.3-1")
    if (d.get("materials") or {}).get("W") not in ("A992", "A572 Gr 50"):
        q("materials.W", "key", options=["A572 Gr 50", "A992"])
    if not (d.get("supports") or {}).get("default"):
        q("supports.default", "key", options=["fixed", "pinned"])
    if not d.get("user_results"):
        q("user_results", "info")
    for i in it:
        pr = (d.get("_proposals") or {}).get(i["path"])
        if i["kind"] in ("key", "default") and pr:
            i.update(kind="default", default=pr["value"], default_reason=pr["reason"])
    return it


def apply_answers(raw, answers):
    d = copy.deepcopy(raw)
    done = set()
    for _ in range(3):
        items = {i["id"]: i for i in validate(d)}
        for iid, a in answers.items():
            i = items.get(iid)
            if i is None or iid in done:
                continue
            if a.get("accept") and i["kind"] == "default":
                _set(d, i["path"], i["default"]); done.add(iid)
            elif "value" in a:
                _set(d, i["path"], a["value"]); done.add(iid)
    return d


def mi_doc(d):
    items = validate(d)
    counts = {k: sum(1 for i in items if i["kind"] == k) for k in ("key", "default", "refused", "info")}
    return {"ready": counts["key"] == 0 and counts["default"] == 0, "counts": counts, "groups": [{"group": "g", "items": items}]}


E2K_RAW = {"schema": "steltic.building_description", "source": {"kind": "e2k", "file": "b.e2k"},
           "supports": {"default": "pinned"}, "seismic": {}, "loads": {}, "materials": {"W": "STEEL"}, "columns": [{"id": "C1"}],
           "_provenance": {},
           "_intake": {"notes": ["read from the .e2k"]}}


class FakeHR:
    def __init__(self):
        self.calls, self.raw, self.answers = [], {}, {}

    def __call__(self, method, path, timeout=120.0, **kw):
        self.calls.append((method, path, kw))
        req = httpx.Request(method, "http://hr" + path)
        ok = lambda body: httpx.Response(200, json=body, request=req)
        if path == "/api/build_only/intake":
            if kw.get("json"):
                b, raw = kw["json"]["building"], copy.deepcopy(kw["json"]["description"])
            else:
                b, raw = kw["data"]["building"], copy.deepcopy(E2K_RAW)
            self.raw[b] = raw
            mi = mi_doc(apply_answers(raw, self.answers.get(b, {})))
            return ok({"building": b, "summary": {"source": raw.get("source"), "notes": (raw.get("_intake") or {}).get("notes")},
                       "missing_items": mi})
        if path.startswith("/api/report/") and path.endswith("/_intake/description_raw.json"):
            b = path.split("/")[3]
            return ok(self.raw[b]) if b in self.raw else httpx.Response(404, json={"detail": "no"}, request=req)
        if path.startswith("/api/build_only/status/"):
            b = path.rsplit("/", 1)[1]
            if b not in self.raw:
                return httpx.Response(404, json={"detail": "no"}, request=req)
            return ok({"building": b, "state": "needs_input", "missing_items": mi_doc(apply_answers(self.raw[b], self.answers.get(b, {})))})
        if path == "/api/build_only/answers":
            b = kw["json"]["building"]
            cur = self.answers.setdefault(b, {})
            for k, v in kw["json"]["answers"].items():
                if v is None:
                    cur.pop(k, None)
                else:
                    cur[k] = v
            return ok({"building": b, "missing_items": mi_doc(apply_answers(self.raw[b], cur))})
        if path.startswith("/api/report/") and path.endswith("/answers.json"):
            b = path.split("/")[3]
            return ok(self.answers[b]) if b in self.answers else httpx.Response(404, json={"detail": "no"}, request=req)
        return httpx.Response(404, json={"detail": "?"}, request=req)

    def intakes(self):
        return [kw for m, p, kw in self.calls if p == "/api/build_only/intake"]


@pytest.fixture
def ub(monkeypatch, tmp_path):
    for k, v in (("HR_JOBS", tmp_path / "hr_jobs"), ("UB_JOBS", tmp_path / "ub_jobs"), ("STELTIC_URL", "http://127.0.0.1:9")):
        os.environ.setdefault(k, str(v))
    um = importlib.import_module("userbuilding.main")
    from fastapi.testclient import TestClient
    hr = FakeHR()
    monkeypatch.setattr(um, "_hr", hr)
    monkeypatch.setattr(um, "NL_PYTHON", sys.executable)
    monkeypatch.setattr(um, "UB_JOBS", tmp_path / "ub_jobs")
    monkeypatch.setattr(um, "HR_JOBS", tmp_path / "hr_jobs")
    monkeypatch.setattr(ullm, "available", lambda: False)
    return TestClient(um.app), hr


# ------------------------------------------------------------------------------------------- values.py
def _items(raw=E2K_RAW):
    return V.questions(mi_doc(raw), raw)


def test_name_value_lines_answer_the_open_questions():
    text = ("SDS = 1.0, site class = d; system X = smf\nD = 4.31 kPa\nLr: 20 psf. R = 8\n"
            "T1 X = 1.25 s, V X = 410 kip, seismic weight = 6,850 kip\nbases = fixed\ndiaphragm = rigid")
    vals, notes, spans = V.read_pairs(text, _items(), E2K_RAW)
    got = {k: v for k, (v, w) in vals.items()}
    assert got["seismic.SDS"] == 1.0 and got["seismic.site_class"] == "D" and got["seismic.system.X"] == "SMF"
    assert got["loads.D_psf"] == pytest.approx(90.01, abs=0.01)                   # 4.31 kPa
    assert got["loads.Lr_psf"] == 20.0 and vals["loads.Lr_psf"][1] == "Lr: 20 psf"
    assert got["seismic.R.X"] == 8.0 and got["seismic.R.Y"] == 8.0                  # asked later, once the system is set
    assert got["user_results.T1_s.X"] == 1.25 and got["user_results.V_kip.X"] == 410.0 and got["user_results.W_kip"] == 6850.0
    assert "supports.default" not in got                                          # the file gives pinned: the file wins
    assert any("the file gives supports.default = \"pinned\"" in n for n in notes)
    assert got["diaphragm"] == "rigid"                                            # not in the file: asked later


def test_values_that_do_not_fit_are_not_taken():
    vals, notes, _ = V.read_pairs("SDS = high\nsite class = Q\nD = 90 bays\nGrid A-D: 4 bays\nsystem X = special moment frame",
                                  _items(), E2K_RAW)
    assert set(vals) == {"seismic.system.X"} and vals["seismic.system.X"][0] == "special moment frame"   # HR reads it
    assert any("'SDS = high'" in n and "not a number" in n for n in notes)
    assert any("'site class = Q'" in n and "is not one of" in n for n in notes)
    assert any("'bays' is not a unit for psf" in n for n in notes)
    assert not any("A-D" in n or "4 bays'" in n and "Grid" in n for n in notes)  # 'Grid A-D: 4 bays' is not D


def test_floor_and_roof_parts_and_doublers():
    raw = dict(E2K_RAW, details={})
    items = V.questions(mi_doc(raw), raw)
    vals, notes, _ = V.read_pairs("D floor = 90 psf, D roof = 25 psf\ndoublers = none", items, raw)
    assert vals["loads.D_psf"][0] == {"floor": 90.0, "roof": 25.0} and "D roof = 25 psf" in vals["loads.D_psf"][1]
    assert vals["details.doublers"][0] == []


def test_a_question_id_works_as_a_name():
    vals, _, _ = V.read_pairs("seismic.SDS = 1.2", _items(), E2K_RAW)
    assert vals["seismic.SDS"][0] == 1.2


def test_one_value_for_both_directions_counts_as_given():
    raw = dict(E2K_RAW, seismic={"system": "SMF", "R": 8})
    ids = {i["id"] for i in V.questions({"groups": []}, raw)}
    assert "seismic.system.X" not in ids and "seismic.R.Y" not in ids and "seismic.Cd.X" in ids


def test_sentences_need_the_model_connection():
    vals, rep = V.read("SDS = 1.0\nThe site is class D and the frames are SMF both ways.", _items(), E2K_RAW, None)
    assert set(vals) == {"seismic.SDS"}
    assert any(n.startswith('not read: "The site is class D') and "model connection" in n for n in rep["notes"])
    _, rep = V.read("SDS = 1.0\nfoo = 3", _items(), E2K_RAW, None)
    assert any("not understood: 'foo = 3'" in n for n in rep["notes"]) and not any("model connection" in n for n in rep["notes"])


def test_sentences_through_the_model_are_held_to_the_words():
    seen = {}

    def chat_json(system, user, **kw):
        seen["user"] = user
        return {"values": {"seismic.site_class": {"value": "d", "quote": "the site is class D"},
                           "seismic.system.X": {"value": "SMF", "quote": "special moment frames in X"},
                           "loads.D_psf": {"value": 95, "quote": "dead load of 95 psf"},           # not in the text
                           "seismic.SDS": {"value": 9.9, "quote": "SDS = 1.0"},                   # read already
                           "nonsense.id": {"value": 1, "quote": "SDS"}},
                "unclear": ["is the roof dead load the same?"], "not_used": ["the architect's name"]}
    text = "SDS = 1.0\nThe site is class D, with special moment frames in X."
    vals, rep = V.read(text, _items(), E2K_RAW, chat_json, "stand-in")
    got = {k: v for k, (v, w) in vals.items()}
    assert got == {"seismic.SDS": 1.0, "seismic.site_class": "D", "seismic.system.X": "SMF"}
    assert "seismic.SDS" not in seen["user"].split("Note:")[0]                     # not asked again
    assert "- seismic.site_class:" in seen["user"] and "[options: A, B, BC" in seen["user"]
    assert any("loads.D_psf = 95" in n and "not in your text" in n for n in rep["notes"])
    assert rep["model"] == "stand-in" and rep["unclear"] and rep["not_used"]
    kinds = {v["id"]: v["kind"] for v in rep["values"]}
    assert kinds["seismic.SDS"] == "proposal"


def test_the_model_failing_keeps_the_name_value_lines():
    def chat_json(system, user, **kw):
        raise httpx.ConnectError("down")
    vals, rep = V.read("SDS = 1.0\nsite class D", _items(), E2K_RAW, chat_json, "m")
    assert set(vals) == {"seismic.SDS"} and any("could not read the sentences" in n for n in rep["notes"])


def test_apply_proposes_quotes_and_strip_takes_an_earlier_text_out():
    items = _items()
    vals, _ = V.read("SDS = 1.0, R = 8, T1 X = 1.2 s", items, E2K_RAW, None)
    d = V.apply(copy.deepcopy(E2K_RAW), vals, items)
    assert d["_proposals"]["seismic/SDS"] == {"value": 1.0, "reason": 'from your text: "SDS = 1.0"'}
    assert d["_proposals"]["seismic/R/X"]["value"] == 8.0
    assert d["user_results"] == {"T1_s": {"X": 1.2}} and "from your text" in d["_provenance"]["user_results"]["note"]
    assert d["_intake"]["notes"][0].startswith("read from your text") and d["_intake"]["notes"][1] == "read from the .e2k"
    # the stand-in HR offers it as a default; accepting the system unlocks R, proposed from the text too
    m = mi_doc(d)
    sds = [i for i in m["groups"][0]["items"] if i["id"] == "seismic.SDS"][0]
    assert sds["kind"] == "default" and sds["default"] == 1.0 and sds["default_reason"].startswith("from your text")
    assert V.from_text(d)
    s = V.strip(copy.deepcopy(d))
    assert not V.from_text(s) and s["_proposals"] == {} and s["user_results"] is None and s["_intake"]["notes"] == ["read from the .e2k"]


# ------------------------------------------------------------------------------------------- the routes
TEXT = "SDS = 1.0\nsite class = D\nsystem X = SMF, R = 8\nD = 90 psf\nT1 X = 1.2 s"


def test_a_file_and_text_together(ub):
    client, hr = ub
    r = client.post("/api/project/FileText/upload", data={"text": TEXT}, files={"file": ("b.e2k", b"$ CONTROLS\n", "text/plain")})
    assert r.status_code == 200, r.text
    first, second = hr.intakes()
    assert "data" in first and second["json"]["building"] == "FileText" and second["json"]["replace"] is False
    desc = second["json"]["description"]
    assert desc["source"]["kind"] == "e2k"                                        # HR's own reading, given back
    assert desc["_proposals"]["seismic/SDS"]["reason"] == 'from your text: "SDS = 1.0"'
    assert desc["_proposals"]["seismic/R/X"]["value"] == 8.0 and desc["user_results"] == {"T1_s": {"X": 1.2}}
    out = r.json()
    assert out["text_read"]["applied"] is True and {v["id"] for v in out["text_read"]["values"]} >= {"seismic.SDS", "seismic.R.X"}
    items = {i["id"]: i for i in out["missing_items"]["groups"][0]["items"]}  # the second intake's questions
    assert items["seismic.SDS"]["kind"] == "default" and items["loads.D_psf"]["default"] == 90.0
    assert "user_results" not in items
    assert client.get("/api/project/FileText/text").json()["values"] == out["text_read"]["values"]
    # accepting the text's defaults: the system unlocks R, which carries the text's value
    acc = {i: {"accept": True} for i, it in items.items() if it["kind"] == "default" and it["default_reason"].startswith("from your text")}
    r = client.post("/api/project/FileText/answers", json={"answers": acc})
    left = {i["id"]: i for i in r.json()["missing_items"]["groups"][0]["items"]}
    assert left["seismic.R.X"]["default"] == 8.0 and left["seismic.R.X"]["default_reason"] == 'from your text: "R = 8"'
    # the file again without text: the text's report goes
    client.post("/api/project/FileText/upload", files={"file": ("b.e2k", b"$ CONTROLS\n", "text/plain")})
    assert client.get("/api/project/FileText/text").json() == {}


def test_code_in_the_text_box_beside_a_file_is_refused(ub):
    client, hr = ub
    code = (FIX / "two_story_kipft.py").read_text()
    r = client.post("/api/project/FileCode/upload", data={"text": code}, files={"file": ("b.e2k", b"$ CONTROLS\n", "text/plain")})
    assert r.status_code == 422 and r.json()["code"] == "TEXT_IS_CODE" and not hr.calls


def test_text_that_gives_nothing_sends_the_file_once(ub):
    client, hr = ub
    r = client.post("/api/project/FileNothing/upload", data={"text": "bases = fixed"}, files={"file": ("b.e2k", b"x", "text/plain")})
    assert r.status_code == 200 and len(hr.intakes()) == 1
    tr = r.json()["text_read"]
    assert tr["values"] == [] and any("the file gives supports.default" in n for n in tr["notes"])


def test_an_opensees_model_and_text(ub):
    client, hr = ub
    r = client.post("/api/project/OsText/upload", data={"text": "SDS = 1.0\nsite class = D\nD = 90 psf"},
                    files={"file": ("two_story_kipft.py", (FIX / "two_story_kipft.py").read_bytes(), "text/x-python")})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["opensees"]["units"]["name"] == "kip-ft" and out["text_read"]["applied"] is True
    first, second = hr.intakes()
    assert first["json"]["description"]["source"]["kind"] == "opensees"
    props = second["json"]["description"]["_proposals"]
    assert props["seismic/SDS"]["value"] == 1.0 and props["seismic/site_class"]["value"] == "D"
    assert len(second["json"]["description"]["columns"]) == 12


def test_values_for_the_building_read_already(ub):
    client, hr = ub
    assert client.post("/api/project/NoBuilding/values", json={"text": "SDS = 1.0"}).status_code == 409
    client.post("/api/project/Again/upload", data={"text": "SDS = 1.0\nD = 90 psf"}, files={"file": ("b.e2k", b"x", "text/plain")})
    r = client.post("/api/project/Again/values", json={"text": "SDS = 1.2"})
    assert r.status_code == 200, r.text
    props = hr.intakes()[-1]["json"]["description"]["_proposals"]
    assert props == {"seismic/SDS": {"value": 1.2, "reason": 'from your text: "SDS = 1.2"'}}   # the earlier D is gone
    assert client.get("/api/project/Again/text").json()["text"] == "SDS = 1.2"
    # no values at all now: the earlier text's are still taken out
    client.post("/api/project/Again/values", json={"text": "bases = fixed"})
    assert hr.intakes()[-1]["json"]["description"]["_proposals"] == {}
    assert client.post("/api/project/Again/values", json={"text": "import openseespy.opensees as ops\nops.model('basic')"}).status_code == 422


def test_building_from_words_clears_the_text_report(ub, monkeypatch):
    client, hr = ub
    client.post("/api/project/Words/upload", data={"text": "SDS = 1.0"}, files={"file": ("b.e2k", b"x", "text/plain")})
    assert client.get("/api/project/Words/text").json()["values"]
    client.post("/api/project/Words/describe", json={"text": (FIX / "two_story_kipft.py").read_text()})
    assert client.get("/api/project/Words/text").json() == {}


def test_an_accept_does_not_carry_over_to_a_changed_value(ub):
    client, hr = ub
    client.post("/api/project/Stale/upload", data={"text": "SDS = 1.0\nLr = 25 psf\nD = 90 psf"}, files={"file": ("b.e2k", b"x", "text/plain")})
    r = client.post("/api/project/Stale/answers", json={"answers": {"seismic.SDS": {"accept": True}, "loads.Lr_psf": {"accept": True},
                                                                    "loads.D_psf": {"value": 95}}})
    left = {i["id"] for i in r.json()["missing_items"]["groups"][0]["items"]}
    assert not left & {"seismic.SDS", "loads.Lr_psf", "loads.D_psf"}
    # SDS changes, Lr goes (HR's own 20 psf would come back), D changes but was answered with a value of its own
    r = client.post("/api/project/Stale/values", json={"text": "SDS = 1.2\nD = 100 psf"})
    tr = r.json()["text_read"]
    assert any(n.startswith("accepted before, and changed or gone in this text") and "loads.Lr_psf, seismic.SDS" in n for n in tr["notes"])
    assert hr.answers["Stale"] == {"loads.D_psf": {"value": 95}}
    items = {i["id"]: i for i in r.json()["missing_items"]["groups"][0]["items"]}
    assert items["seismic.SDS"]["default"] == 1.2 and items["loads.Lr_psf"]["default"] == 20.0
    # the same text again: nothing changed, the accept stays
    client.post("/api/project/Stale/answers", json={"answers": {"seismic.SDS": {"accept": True}}})
    client.post("/api/project/Stale/values", json={"text": "SDS = 1.2\nD = 100 psf"})
    assert hr.answers["Stale"]["seismic.SDS"] == {"accept": True}
    # the file again without text: the text's values are gone, and the accept with them
    r = client.post("/api/project/Stale/upload", files={"file": ("b.e2k", b"x", "text/plain")})
    assert "seismic.SDS" not in hr.answers["Stale"]
    assert {i["id"]: i["kind"] for i in r.json()["missing_items"]["groups"][0]["items"]}["seismic.SDS"] == "key"


def test_a_question_answered_since_is_still_asked_of_the_next_text(ub):
    client, hr = ub
    client.post("/api/project/Since/upload", data={"text": "grade = A992"}, files={"file": ("b.e2k", b"x", "text/plain")})
    r = client.post("/api/project/Since/answers", json={"answers": {"materials.W": {"accept": True}}})
    assert "materials.W" not in {i["id"] for i in r.json()["missing_items"]["groups"][0]["items"]}
    # the file says STEEL (a grade HR does not know): the question was asked, and the next text still answers it
    r = client.post("/api/project/Since/values", json={"text": "grade = A992\nSDS = 1.0"})
    tr = r.json()["text_read"]
    assert {v["id"] for v in tr["values"]} == {"materials.W", "seismic.SDS"} and not any("STEEL" in n for n in tr["notes"])
    assert hr.answers["Since"]["materials.W"] == {"accept": True}
    assert tr["values"][0]["path"] == "materials/W" and tr["values"][0]["options"] == ["A572 Gr 50", "A992"]
