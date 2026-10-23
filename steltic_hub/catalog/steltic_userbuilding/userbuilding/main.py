"""User-designed building -- the module server.

A user brings a steel building they designed. This page walks it through:
  1. intake      the file (Steltic building template .xlsx, ETABS .e2k, or a description .json) goes to HR Steel's
                 build-only intake (POST {STELTIC_URL}/api/build_only/intake). An OpenSees model (Tcl .tcl,
                 OpenSeesPy .py, or a .zip of the model folder) is read here first (userbuilding/opensees: the script
                 runs against a recorder, nothing is analysed) and its building description goes to the same intake.
                 The text box takes pasted OpenSees code (read the same way) or the building in plain words: the
                 hub's model connection reads the words into a spec, describe.py writes it as an OpenSeesPy script,
                 and the OpenSees interpreter reads that (describe.py);
  2. questions   HR's missing_items.json: key items are questions, proposed defaults need acceptance, refused
                 items are outside version 1 -- answers go back to HR (/api/build_only/answers);
  3. build       HR builds the model as given (/api/build_only/build; no redesign, nothing invented);
  4. confirm     the Nonlinear module's confirmation checks on HR's package (`snl confirm --check`), the 3-D model,
                 the user's own T1 / V / W compared (warning only); Confirm writes nl_basis.json;
  5. run         through the hub, as a click would: the Nonlinear module's Collect specification values, then Run
                 analyses (the hub stages HR's package -- provenance.json and nl_basis.json with it -- into the
                 project and keeps the run history). "Do not collect specification values now" skips Collect and
                 runs with the Nonlinear tab's own opt-out (--no-collect): the reports come back marked UNVERIFIED
                 and say the values were not collected.
Nothing here models, sizes or analyses anything itself; HR Steel and the Nonlinear module do, with their own rules.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import threading
import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import describe as DS
from . import llm
from . import local_guard
from . import opensees as OS
from .hub import HubClient, HubError

HERE = pathlib.Path(__file__).resolve().parent
UI = HERE / "ui"
STELTIC_URL = (os.environ.get("STELTIC_URL") or "").rstrip("/")
HUB_URL = (os.environ.get("HUB_URL") or "").rstrip("/")
HR_JOBS = pathlib.Path(os.environ.get("HR_JOBS") or (pathlib.Path.cwd() / "hr_jobs"))
NL_PYTHON = os.environ.get("NL_PYTHON") or ""
NL_DIR = os.environ.get("NL_DIR") or ""
UB_JOBS = pathlib.Path(os.environ.get("UB_JOBS") or (pathlib.Path.cwd() / "ub_jobs"))
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

app = FastAPI(title="User-designed building")
local_guard.install(app)
app.mount("/static", StaticFiles(directory=str(UI)), name="static")


@app.middleware("http")
async def _no_stale_assets(request, call_next):
    resp = await call_next(request)
    if "cache-control" not in resp.headers:
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def clean_name(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]", "", (s or "").strip())
    return s or "Project"


def _hr_headers():
    """Nothing: like the other bundled modules this server calls HR Steel as a local client (no Origin header, no
    hub token -- the hub's per-launch token is the hub's own, not this module's to forward)."""
    return {}


def _hr(method: str, path: str, timeout: float = 120.0, **kw):
    if not STELTIC_URL:
        raise HTTPException(503, "HR Steel's server address is not set (STELTIC_URL): start this page from the hub")
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=20.0)) as cx:
            r = cx.request(method, STELTIC_URL + path, headers=_hr_headers(), **kw)
    except httpx.HTTPError as ex:
        raise HTTPException(502, "HR Steel did not answer: %s" % ex)
    return r


def _relay(r):
    ctype = r.headers.get("content-type", "")
    if "json" in ctype:
        return JSONResponse(r.json(), status_code=r.status_code)
    return Response(r.content, status_code=r.status_code, media_type=ctype or "application/octet-stream",
                    headers={k: v for k, v in r.headers.items() if k.lower() == "content-disposition"})


def _ub_dir(p: str) -> pathlib.Path:
    d = UB_JOBS / clean_name(p) / "userbuilding"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hr_job(p: str) -> pathlib.Path:
    return HR_JOBS / clean_name(p)


# ------------------------------------------------------------------------------------------- pages
@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def index():
    return (UI / "index.html").read_text(encoding="utf-8")


@app.get("/api/me")
async def me():
    up = False
    if STELTIC_URL:
        try:
            with httpx.Client(timeout=3.0) as cx:
                up = cx.get(STELTIC_URL + "/healthz", headers=_hr_headers()).status_code < 500
        except Exception:
            up = False
    return {"steltic_url": STELTIC_URL, "steltic_up": up, "hub_url": HUB_URL, "nl_python": bool(NL_PYTHON),
            "hr_jobs": str(HR_JOBS), "llm": llm.available(), "model": (llm.creds() or {}).get("model")}


@app.get("/api/template")
def template(project: str = ""):
    return _relay(_hr("GET", "/api/build_only/template", params={"building": clean_name(project)} if project else None))


# ------------------------------------------------------------------------------------------- 1-3 HR
@app.post("/api/project/{p}/upload")
async def upload(p: str, request: Request):
    form = await request.form()
    up = form.get("file")
    if up is None or not getattr(up, "filename", None):
        raise HTTPException(400, "choose a file")
    data = await up.read()
    replace = str(form.get("replace") or "").lower() in ("1", "true", "yes")
    name = pathlib.Path(up.filename.replace("\\", "/")).name
    if name.lower().endswith(OS.UPLOAD_EXT):
        return await run_in_threadpool(_opensees_upload, p, name, data, form, replace)
    r = await run_in_threadpool(_hr, "POST", "/api/build_only/intake", 600.0,
                                data={"building": clean_name(p), "replace": "1" if replace else ""},
                                files={"file": (name, data, "application/octet-stream")})
    if r.status_code < 400:
        _os_report(p).unlink(missing_ok=True)            # the OpenSees report of an earlier upload no longer applies
    return _relay(r)


# ------------------------------------------------------------------------------------------- OpenSees models
def _os_dir(p: str) -> pathlib.Path:
    return _ub_dir(p) / "opensees"


def _os_report(p: str) -> pathlib.Path:
    return _os_dir(p) / "conversion.json"


def _os_choice(form, key, allowed):
    v = str(form.get(key) or "").strip()
    return v if v in allowed else None


def _opensees_upload(p, name, data, form, replace, finish=None):
    """An OpenSees model -> a building description (userbuilding/opensees) -> HR's build-only intake, as JSON.
    finish(desc, report) -> (desc, report) runs before the intake (the text box's touches)."""
    d = _os_dir(p)
    inbox = d / "upload"
    if inbox.exists():
        for f in inbox.iterdir():
            if f.is_file():
                f.unlink()
    inbox.mkdir(parents=True, exist_ok=True)
    src = inbox / name
    src.write_bytes(data)
    units = _os_choice(form, "units", OS.UNITS)
    vert = _os_choice(form, "up", ("Z", "Y"))
    main = str(form.get("main") or "").strip() or None
    try:
        desc, rep = OS.read_opensees(src, d, main=main, units=units, up=vert, nl_python=NL_PYTHON or None)
        if finish:
            desc, rep = finish(desc, rep)
            _os_report(p).write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    except OS.IntakeError as ex:
        msg = str(ex)
        code = msg.split(":", 1)[0] if msg.split(":", 1)[0] in ("UNITS", "MAIN", "NO_TCL") else ""
        body = {"code": code, "units": list(OS.UNITS)}
        if code == "NO_TCL":
            body["detail"] = ("an OpenSees Tcl script needs a Python with Tcl (tkinter), and neither this page's Python nor "
                              "the Nonlinear module's has it: save the model as OpenSeesPy (.py), or reinstall Python with "
                              "Tcl/Tk")
        elif code == "MAIN":
            body["detail"] = msg.split(":", 1)[1].strip()
            try:
                body["candidates"] = sorted(
                    q.relative_to(d / "model").as_posix() for q in (d / "model").rglob("*")
                    if q.is_file() and q.suffix.lower() in OS.SCRIPT_EXT)
            except OSError:
                body["candidates"] = []
        elif code == "UNITS":
            body["detail"] = msg.split(":", 1)[1].strip()
        else:
            body["detail"] = "could not read %s as an OpenSees building: %s" % (name, msg)
        return JSONResponse(body, status_code=422)
    r = _hr("POST", "/api/build_only/intake", timeout=600.0,
            json={"building": clean_name(p), "description": desc, "replace": replace})
    if r.status_code >= 400:
        _os_report(p).unlink(missing_ok=True)            # HR did not take it: the page keeps showing what HR has
        return _relay(r)
    out = r.json()
    out["opensees"] = rep
    return JSONResponse(out)


# ------------------------------------------------------------------------------------------- the text box
MAX_TEXT = 100_000


@app.post("/api/creds")
async def creds(request: Request):
    """The hub pushes the user's model connection here (manifest `credentials`); the text box reads words with it."""
    llm.set_creds(await request.json())
    return {"ok": True, "llm": llm.available()}


@app.post("/api/project/{p}/describe")
async def describe(p: str, request: Request):
    """The text box: pasted OpenSees code, or the building in plain words."""
    body = await request.json()
    text = str(body.get("text") or "")
    if not text.strip():
        raise HTTPException(400, "type the building, or paste an OpenSees script")
    if len(text) > MAX_TEXT:
        raise HTTPException(413, "the text is longer than %d characters: upload the model as a file" % MAX_TEXT)
    replace = bool(body.get("replace"))
    d = _os_dir(p)
    d.mkdir(parents=True, exist_ok=True)
    (d / "typed.txt").write_text(text, encoding="utf-8")
    kind = DS.classify(text)
    if kind in ("py", "tcl"):
        opts = {k: body.get(k) for k in ("units", "up")}

        def typed(desc, rep):
            rep["typed"] = {"text": text, "language": kind}
            return desc, rep
        return await run_in_threadpool(_opensees_upload, p, "typed_model." + kind, text.encode("utf-8"), opts, replace, typed)
    return await run_in_threadpool(_describe_words, p, text, replace)


def _describe_words(p, text, replace):
    if not llm.available():
        return JSONResponse({"code": "NO_LLM", "detail": (
            "Reading a building in plain words needs the hub's model connection, and none is set: set one in the hub "
            "(the same connection Admin and Design variations use). The file upload and pasted OpenSees code work "
            "without it.")}, status_code=422)
    model = (llm.creds() or {}).get("model") or "the model"
    d = _os_dir(p)
    try:
        spec = DS.read_words(text, llm.chat_json)
    except llm.NotConfigured as ex:
        return JSONResponse({"code": "NO_LLM", "detail": str(ex)}, status_code=422)
    except (httpx.HTTPError, ValueError) as ex:
        return JSONResponse({"code": "LLM", "detail": "the model (%s) could not read the description: %s" % (model, ex)},
                            status_code=502)
    (d / "spec.json").write_text(json.dumps(spec, indent=1, default=str), encoding="utf-8")
    plan, needs, warns = DS.check(spec, text)
    if needs:
        return JSONResponse({"needs": needs, "warnings": warns, "unclear": plan["unclear"],
                             "not_taken": plan["not_taken"], "model": model, "spec": spec})
    src = DS.script(plan, text, model)

    def finish(desc, rep):
        desc = DS.finish(desc, rep, plan, model, text, "described_model.py")
        rep["described"] = {"model": model, "warnings": warns, "unclear": plan["unclear"], "not_taken": plan["not_taken"],
                            "text": text}
        return desc, rep
    return _opensees_upload(p, "described_model.py", src.encode("utf-8"), {}, replace, finish)


@app.get("/api/project/{p}/opensees/script")
def opensees_script(p: str):
    """The script the last OpenSees reading ran: the one written from the description, or the user's own."""
    try:
        rep = json.loads(_os_report(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(404, "no OpenSees model read for this project")
    root = (_os_dir(p) / "model").resolve()
    f = (root / str(rep.get("main") or "")).resolve()
    if root not in f.parents or not f.is_file():
        raise HTTPException(404, "the script is not there any more")
    return Response(f.read_bytes(), media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % f.name})


@app.get("/api/project/{p}/opensees")
def opensees_report(p: str):
    """The conversion report of the last OpenSees upload ({} when the building came from another file)."""
    f = _os_report(p)
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@app.post("/api/project/{p}/answers")
async def answers(p: str, request: Request):
    body = await request.json()
    return _relay(_hr("POST", "/api/build_only/answers", json={"building": clean_name(p), "answers": body.get("answers") or {}}))


@app.post("/api/project/{p}/build")
def build(p: str):
    return _relay(_hr("POST", "/api/build_only/build", json={"building": clean_name(p)}))


@app.get("/api/project/{p}/hr")
def hr_status(p: str):
    r = _hr("GET", "/api/build_only/status/%s" % clean_name(p))
    if r.status_code == 404:
        return JSONResponse({"building": clean_name(p), "state": "none"})
    return _relay(r)


@app.get("/api/project/{p}/file/{name:path}")
def hr_file(p: str, name: str):
    """HR's package files (the 3-D viewer, the report) for the confirmation step."""
    if name not in ("viewer_3d.html", "report.html", "provenance.json", "missing_items.json", "building_description.json"):
        raise HTTPException(404, "not offered")
    return _relay(_hr("GET", "/api/report/%s/%s" % (clean_name(p), name)))


# ------------------------------------------------------------------------------------------- 4 confirm
def _snl(args, timeout=300):
    if not NL_PYTHON:
        raise HTTPException(503, "the Nonlinear module's interpreter is not known (NL_PYTHON): start this page from the hub")
    env = {**os.environ}
    if NL_DIR:
        env["PYTHONPATH"] = NL_DIR + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    p = subprocess.run([NL_PYTHON, "-m", "snl"] + args, capture_output=True, text=True, timeout=timeout, env=env,
                       cwd=NL_DIR or None, creationflags=_NO_WINDOW)
    return p


@app.get("/api/project/{p}/checks")
def checks(p: str):
    j = _hr_job(p)
    if not (j / "provenance.json").exists():
        raise HTTPException(409, "HR has not built this building yet")
    r = _snl(["confirm", str(j), "--check", "--json"])
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise HTTPException(500, "snl confirm failed: %s" % (r.stderr or r.stdout)[-1500:])
    nb = j / "nl_basis.json"
    out["nl_basis"] = json.loads(nb.read_text(encoding="utf-8")) if nb.exists() else None
    return out


@app.post("/api/project/{p}/confirm")
async def confirm(p: str, request: Request):
    body = await request.json()
    j = _hr_job(p)
    args = ["confirm", str(j), "--json"]
    if body.get("by"):
        args += ["--by", str(body["by"])[:120]]
    if body.get("note"):
        args += ["--note", str(body["note"])[:2000]]
    r = _snl(args)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise HTTPException(500, "snl confirm failed: %s" % (r.stderr or r.stdout)[-1500:])
    return JSONResponse(out, status_code=200 if out.get("confirmed") else 409)


# ------------------------------------------------------------------------------------------- 5 run (through the hub)
_RUNS = {}
_RLOCK = threading.Lock()


def _run_file(p):
    return _ub_dir(p) / "nl_run.json"


def _save_run(p, st):
    try:
        _run_file(p).write_text(json.dumps({k: v for k, v in st.items() if k != "stop"}, indent=1, default=str), encoding="utf-8")
    except OSError:
        pass


COLLECTED = "hinge_params_collected.json"          # what the Nonlinear module's Collect writes into the project


def _nl_worker(p, fields, do_collect):
    st = _RUNS[p]
    hub = HubClient(HUB_URL)

    def on_event(ev):
        t = ev.get("type")
        txt = ev.get("text") or ev.get("line") or ev.get("message") or ""
        if t in ("token", "reasoning", "usage"):
            return
        st["events"].append({"t": time.time(), "type": t, "text": str(txt)[:2000], "phase": st["phase"]})
        if len(st["events"]) > 4000:
            del st["events"][:1000]

    try:
        try:
            hub.create_project(p)              # the hub's project folder (a no-op when it exists)
        except HubError:
            pass
        if do_collect:
            st["phase"] = "collect"
            out = hub.run("steltic_nonlinear", "run", p, fields, on_event, should_stop=lambda: st.get("stop"),
                          action="collect")
            st["collect"] = {k: out.get(k) for k in ("ok", "rc", "errors", "cancelled")}
            if not out.get("ok"):
                st["state"] = "failed"
                st["error"] = "Collect specification values did not finish: %s" % ("; ".join(out.get("errors") or []) or "rc %s" % out.get("rc"))
                return
        st["phase"] = "run"
        run_fields = dict(fields)
        if not do_collect:
            # The Nonlinear Run tab opens without hinge_params_collected.json only when its own tick box says so
            # (manifest `waive`); without this field the hub refused the run with "Collect specification values
            # first" -- the opt-out on this page used to end there.
            run_fields["no_collect"] = True
        out = hub.run("steltic_nonlinear", "run", p, run_fields, on_event, should_stop=lambda: st.get("stop"))
        st["run"] = {k: out.get(k) for k in ("ok", "rc", "errors", "cancelled", "artifacts")}
        st["state"] = "done" if out.get("ok") else ("cancelled" if out.get("cancelled") else "failed")
        if not out.get("ok") and not out.get("cancelled"):
            st["error"] = "; ".join(out.get("errors") or []) or "rc %s" % out.get("rc")
    except HubError as ex:
        st["state"] = "failed"; st["error"] = str(ex)
    except Exception as ex:                                                 # noqa: BLE001
        st["state"] = "failed"; st["error"] = "%s: %s" % (type(ex).__name__, ex)
    finally:
        st["finished"] = time.time()
        _save_run(p, st)


@app.post("/api/project/{p}/run")
async def run_nl(p: str, request: Request):
    p = clean_name(p)
    body = await request.json()
    j = _hr_job(p)
    nb = j / "nl_basis.json"
    if not nb.exists():
        raise HTTPException(409, "confirm the model first (step 4)")
    if not HUB_URL:
        raise HTTPException(503, "the hub's address is not set (HUB_URL): start this page from the hub")
    with _RLOCK:
        cur = _RUNS.get(p)
        if cur and cur.get("state") == "running":
            return JSONResponse({"state": "running"}, status_code=202)
        fields = {"job": p}
        for k in ("n_records", "site_class", "only"):
            if body.get(k) not in (None, ""):
                fields[k] = body[k]
        # "Do not collect specification values now" (skip_collect); `collect: false` is the older page's spelling
        do_collect = not bool(body.get("skip_collect")) and bool(body.get("collect", True))
        _RUNS[p] = dict(state="running", phase="", started=time.time(), events=[], fields=fields, collect=do_collect)
    threading.Thread(target=_nl_worker, args=(p, fields, do_collect), daemon=True).start()
    return JSONResponse({"state": "running"}, status_code=202)


@app.post("/api/project/{p}/stop")
def stop_nl(p: str):
    st = _RUNS.get(clean_name(p))
    if st and st.get("state") == "running":
        st["stop"] = True
        return {"ok": True}
    return {"ok": False}


@app.get("/api/project/{p}/run")
def run_state(p: str, since: int = 0):
    p = clean_name(p)
    st = _RUNS.get(p)
    if st is None:
        f = _run_file(p)
        st = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"state": "none", "events": []}
    ev = st.get("events") or []
    return {k: v for k, v in st.items() if k not in ("events", "stop")} | {
        "events": ev[since:], "next": len(ev), "collected": (UB_JOBS / p / COLLECTED).exists()}
