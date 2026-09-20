"""Design variations -- the module server.

One project = one study. Its state lives in <VARIATIONS_JOBS>/<project>/variations/state.json, next
to a folder per variation holding the HR Steel package, its report / viewer and the metrics read
from it. The hub starts this server with STELTIC_URL (HR Steel's address) and VARIATIONS_JOBS (the
hub's projects folder) and frames the UI at /?project=<name>.

Runs are background threads: closing the browser tab never kills a study, and /api/project/<p>/events
re-attaches to the live event stream from any sequence number.
"""
from __future__ import annotations
import csv, hashlib, io, json, os, pathlib, re, shutil, threading, time
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import library, llm, metrics as mx, prompts, scoring, steltic_client as hr

HERE = pathlib.Path(__file__).resolve().parent
UI = HERE / "ui"
JOBS = pathlib.Path(os.environ.get("VARIATIONS_JOBS") or (pathlib.Path.cwd() / "variations_data")).resolve()
STELTIC_URL = (os.environ.get("STELTIC_URL") or "").rstrip("/")
KEEPALIVE = 20.0

app = FastAPI(title="Design variations")
app.mount("/static", StaticFiles(directory=str(UI)), name="static")


@app.middleware("http")
async def _no_stale_assets(request, call_next):
    """Every module page fetches /static/app.js and /static/styles.css by the same absolute path, and
    the browser caches per origin (127.0.0.1:<port>). The hub now keeps one port per module, and this
    makes the page safe even if it did not: an asset is revalidated on every load."""
    resp = await call_next(request)
    if "cache-control" not in resp.headers:
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---------------------------------------------------------------- names, files, state
def clean_name(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]", "", (s or "").strip())
    return s or "Project"


def study_dir(project: str) -> pathlib.Path:
    return JOBS / clean_name(project) / "variations"


def var_dir(project: str, vid: str) -> pathlib.Path:
    if not re.fullmatch(r"M\d{3,4}", vid or ""):
        raise HTTPException(400, f"bad variation id {vid!r}")
    return study_dir(project) / vid


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock(project: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(clean_name(project), threading.Lock())


def new_state() -> dict:
    return {"version": 1, "base_brief": "", "base_source": "", "n": 10, "mode": "categories", "categories": [],
            "instructions": "", "plan": [], "plan_source": "", "plan_note": "", "results": {},
            "scoring": {"equation": scoring.DEFAULT_EQUATION, "source_text": "", "explanation": "",
                        "rates": dict(scoring.DEFAULT_RATES), "rules": dict(scoring.DEFAULT_ELIGIBILITY),
                        "scored_at": None, "summary": None},
            "selection": {"ids": [], "note": "", "recorded_at": None}, "updated_at": None}


def _replace(tmp, dst, tries=60):
    """os.replace that tolerates Windows: while another thread holds the target open for reading (the UI polling
    state.json), MoveFileEx fails with PermissionError (WinError 5 / 32). Readers hold the file for milliseconds,
    so retry briefly instead of losing the write -- on POSIX the first attempt always succeeds."""
    for i in range(tries):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.01)


def _read_text(path, tries=20):
    """Read a file another thread may be atomically replacing at this instant (Windows raises PermissionError)."""
    for i in range(tries):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.01)


def load_state(project: str) -> dict:
    p = study_dir(project) / "state.json"
    if p.is_file():
        try:
            d = json.loads(_read_text(p))
            base = new_state()
            base.update(d)
            for k in ("scoring", "selection"):
                merged = dict(new_state()[k]); merged.update(d.get(k) or {}); base[k] = merged
            return base
        except Exception:
            pass
    return new_state()


def save_state(project: str, st: dict):
    d = study_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    st["updated_at"] = time.time()
    tmp = d / "state.json.tmp"
    tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
    _replace(tmp, d / "state.json")


def update_state(project: str, fn):
    with _lock(project):
        st = load_state(project)
        r = fn(st)
        save_state(project, st)
        return r if r is not None else st


def change_hash(v: dict) -> str:
    return hashlib.sha1(((v.get("title") or "") + "\n" + (v.get("change") or "")).encode("utf-8")).hexdigest()[:10]


def rows_of(st: dict) -> list[dict]:
    """The plan joined with its results, in plan order -- what scoring and the UI consume."""
    out = []
    for v in st.get("plan") or []:
        r = dict(st.get("results", {}).get(v["id"]) or {})
        row = {"id": v["id"], "title": v.get("title", ""), "group": v.get("group", ""), "change": v.get("change", ""),
               "why": v.get("why", ""), "status": r.get("status", "pending"), "reason": r.get("reason", ""),
               "metrics": dict(r.get("metrics") or {}), "building": r.get("building", ""),
               "designed_at": r.get("designed_at"), "llm_read": r.get("llm_read"),
               "stale": bool(r.get("hash")) and r.get("hash") != change_hash(v),
               "files": r.get("files") or []}
        out.append(row)
    return out


# ---------------------------------------------------------------- variation brief
def variation_brief(base: str, v: dict) -> str:
    base = (base or "").strip()
    if not (v.get("change") or "").strip():
        return base
    return (f"{base}\n\n=== DESIGN VARIATION {v['id']}: {v.get('title', '')} ===\n"
            f"Change ONLY what is stated here relative to the brief above; keep everything else identical.\n"
            f"{v['change'].strip()}\n"
            + (f"Purpose of this variation: {v['why'].strip()}\n" if (v.get('why') or '').strip() else "")
            + "If this variation is NOT PERMITTED by the governing standard, stop before designing and state the "
              "clause; do not substitute a different system.")


def building_name(project: str, vid: str) -> str:
    return clean_name(f"{clean_name(project)}_{vid}")


# ---------------------------------------------------------------- metrics after a design
def _estimate_moment_conn(m: dict) -> Optional[int]:
    """Only used without an LLM: perimeter frames, every bay, both ends, every level."""
    sysname = " ".join(str(m.get(k) or "") for k in ("system", "system_declared")).lower()
    if not sysname:
        return None
    if "smf" not in sysname and "moment" not in sysname and "imf" not in sysname and "omf" not in sysname:
        return 0
    nx, ny, nf = m.get("nx"), m.get("ny"), m.get("n_stories")
    if not (nx and ny and nf):
        return None
    return int(2 * (nx + ny) * 2 * nf)


def read_package(project: str, vid: str, data: bytes, log) -> dict:
    files = mx.unzip(data)
    m = mx.extract(files)
    d = var_dir(project, vid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "package.zip").write_bytes(data)
    saved = ["package.zip"]
    for name in ("report.html", "viewer_3d.html"):
        if name in files:
            (d / name).write_bytes(files[name]); saved.append(name)
    llm_read = None
    if llm.available() and files.get("report.html"):
        try:
            log("reading the report for the values the package does not state")
            llm_read = llm.chat_json(prompts.READ_SYSTEM, prompts.read_user(mx.report_excerpt(files), m), max_tokens=1500)
            for k in ("n_moment_conn", "smf_share_pct", "wind_comfort_mg"):
                v = llm_read.get(k)
                if isinstance(v, (int, float)) and (m.get(k) is None):
                    m[k] = v
            sx, sy = llm_read.get("system_X"), llm_read.get("system_Y")
            if sx or sy:
                m["system_llm"] = f"{sx or '?'} / {sy or '?'}"
                if not m.get("system"):
                    m["system"] = m["system_llm"]
            m.update(mx.representability(m))
            if isinstance(llm_read.get("is_dual"), bool):
                m["is_dual"] = llm_read["is_dual"]
        except Exception as e:
            log(f"report reading skipped: {e}")
            llm_read = {"error": str(e)}
    if m.get("n_moment_conn") is None:
        est = _estimate_moment_conn(m)
        if est is not None:
            m["n_moment_conn"] = est
            m["n_moment_conn_estimated"] = True
    (d / "metrics.json").write_text(json.dumps(m, indent=1), encoding="utf-8")
    return {"metrics": m, "files": saved, "llm_read": llm_read}


# ---------------------------------------------------------------- background runs
class Study(threading.Thread):
    def __init__(self, project: str, ids: list[str]):
        super().__init__(daemon=True, name=f"study-{project}")
        self.project = clean_name(project)
        self.ids = ids
        self.events: list[dict] = []
        self.seq = 0
        self.stop_flag = False
        self.current: Optional[str] = None
        self.finished = False
        self.cv = threading.Condition()
        self._text_buf = ""
        self._text_at = 0.0

    def emit(self, ev: dict):
        with self.cv:
            self.seq += 1
            ev = {"seq": self.seq, "t": time.time(), **ev}
            self.events.append(ev)
            if len(self.events) > 4000:
                del self.events[:1000]
            self.cv.notify_all()

    def log(self, text: str, vid: Optional[str] = None):
        self.emit({"type": "log", "id": vid or self.current, "text": text})

    def _flush_text(self, force=False):
        if self._text_buf and (force or len(self._text_buf) > 240 or time.time() - self._text_at > 0.6):
            self.emit({"type": "text", "id": self.current, "text": self._text_buf})
            self._text_buf = ""
            self._text_at = time.time()

    def on_event(self, ev: dict):
        t = ev.get("type")
        if t == "token":
            if not self._text_buf:
                self._text_at = time.time()
            self._text_buf += ev.get("text") or ""
            self._flush_text()
            return
        self._flush_text(force=True)
        if t in ("status", "milestone"):
            self.log(ev.get("text") or "")
        elif t == "tool":
            self.emit({"type": "tool", "id": self.current, "text": ev.get("title") or ev.get("name") or "tool"})
        elif t == "tool_result":
            ms = ev.get("ms")
            self.emit({"type": "tool_result", "id": self.current, "text": f"{ev.get('name') or ''}: {ev.get('summary') or ''}"
                       + (f"  ({ms/1000:.1f}s)" if isinstance(ms, (int, float)) else "")})
        elif t == "assistant":
            self.emit({"type": "text", "id": self.current, "text": (ev.get("text") or "")[-600:]})
        elif t == "paused":
            self.log(f"paused: {ev.get('reason') or ''}")
        elif t == "error":
            self.log(f"error: {ev.get('text') or ''}")
        elif t == "usage":
            self.emit({"type": "usage", "id": self.current, "cum_in": ev.get("cum_in"), "cum_out": ev.get("cum_out")})

    def set_result(self, vid: str, **fields):
        def fn(st):
            r = dict(st["results"].get(vid) or {})
            r.update(fields)
            st["results"][vid] = r
        update_state(self.project, fn)

    def run(self):
        try:
            self._run()
        except Exception as e:
            self.emit({"type": "log", "id": None, "text": f"study stopped: {type(e).__name__}: {e}"})
        finally:
            self.finished = True
            self.emit({"type": "finished", "stopped": self.stop_flag})
            with self.cv:
                self.cv.notify_all()

    def _run(self):
        st = load_state(self.project)
        base = st.get("base_brief") or ""
        plan = {v["id"]: v for v in st.get("plan") or []}
        todo = [i for i in self.ids if i in plan]
        self.emit({"type": "start", "ids": todo, "text": f"designing {len(todo)} variation(s) with HR Steel at {STELTIC_URL}"})
        for n, vid in enumerate(todo, 1):
            if self.stop_flag:
                self.set_result(vid, status="pending", reason="skipped: study stopped")
                continue
            v = plan[vid]
            self.current = vid
            bname = building_name(self.project, vid)
            self.set_result(vid, status="running", reason="", building=bname, hash=change_hash(v), started_at=time.time())
            self.emit({"type": "variation", "id": vid, "status": "running", "n": n, "of": len(todo), "title": v.get("title", "")})
            try:
                outcome = hr.run_design(STELTIC_URL, bname, variation_brief(base, v), self.on_event, lambda: self.stop_flag)
                self._flush_text(force=True)
            except Exception as e:
                outcome = {"status": "failed", "reason": f"{type(e).__name__}: {e}"}
            status, reason = outcome["status"], outcome.get("reason") or ""
            has_package = status in ("done", "paused")        # a paused run still leaves a package to read
            if status == "paused" and re.search(r"not\s+permitted", reason, re.I):
                status = "np"
            got = {}
            if has_package:
                try:
                    self.log("downloading the package", vid)
                    data = hr.download(STELTIC_URL, bname)
                    got = read_package(self.project, vid, data, lambda t: self.log(t, vid))
                    m = got["metrics"]
                    if status == "done" and (got.get("llm_read") or {}).get("not_permitted") is True:
                        status, reason = "np", (got["llm_read"].get("np_clause") or "report states NOT PERMITTED")
                    elif status == "done" and (m.get("np_text") or "").strip() and m.get("drift_utilisation") is None:
                        # the report says NOT PERMITTED and holds no drift check: the agent stopped at the gate
                        status, reason = "np", m["np_text"][:300]
                    self.log(f"metrics: steel {m.get('steel_tons')} t, drift util {m.get('drift_utilisation')}, "
                             f"D/C max {m.get('dc_max')}, system {m.get('system') or m.get('system_declared') or '?'}", vid)
                except Exception as e:
                    hint = (" -- model MOCK: HR Steel's mock run writes no design package; set a real model to design"
                            if (llm.creds() or {}).get("model") == "MOCK" else "")
                    if status == "done":
                        status, reason = "failed", f"package could not be read: {e}{hint}"
                    self.log(f"package not read: {e}{hint}", vid)
            self.set_result(vid, status=status, reason=reason, designed_at=time.time(), metrics=got.get("metrics") or {},
                            files=got.get("files") or [], llm_read=got.get("llm_read"))
            self.emit({"type": "variation", "id": vid, "status": status, "reason": reason, "n": n, "of": len(todo),
                       "metrics": got.get("metrics") or {}})
            self.current = None
        self.emit({"type": "log", "id": None, "text": "study finished" if not self.stop_flag else "study stopped"})


_STUDIES: dict[str, Study] = {}


def study(project: str) -> Optional[Study]:
    return _STUDIES.get(clean_name(project))


# ---------------------------------------------------------------- API: server, library
@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/api/creds")
async def creds(request: Request):
    llm.set_creds(await request.json())
    return {"ok": True, "llm": llm.available()}


@app.get("/api/me")
async def me():
    c = llm.creds() or {}
    return {"llm": llm.available(), "has_creds": bool(c), "model": c.get("model", ""), "steltic_url": STELTIC_URL,
            "steltic_up": bool(STELTIC_URL) and hr.healthy(STELTIC_URL), "jobs": str(JOBS)}


@app.get("/api/library")
async def get_library():
    return {"categories": [{"id": c["id"], "label": c["label"], "blurb": c["blurb"],
                            "examples": [t[0] for t in c["templates"][:4]], "count": len(c["templates"])}
                           for c in library.CATEGORIES],
            "default_equation": scoring.DEFAULT_EQUATION,
            "metrics": [{"name": n, "description": d, "unit": u} for n, d, u in scoring.METRICS],
            "default_rules": scoring.DEFAULT_ELIGIBILITY, "default_rates": scoring.DEFAULT_RATES,
            "representable": list(library.REPRESENTABLE)}


# ---------------------------------------------------------------- API: project
def scored_rows(st: dict) -> list[dict]:
    """Rows with eligibility / score / rank applied when a score has been applied to this study."""
    rows = rows_of(st)
    sc = st["scoring"]
    if sc.get("scored_at"):
        try:
            scoring.score_rows(rows, sc["equation"], sc.get("rules") or {}, sc.get("rates") or {})
        except scoring.EquationError:
            pass
    return rows


def _project_payload(project: str) -> dict:
    st = load_state(project)
    s = study(project)
    return {"project": clean_name(project), **{k: st[k] for k in ("base_brief", "base_source", "n", "mode", "categories",
                                                                  "instructions", "plan", "plan_source", "plan_note",
                                                                  "scoring", "selection", "updated_at")},
            "rows": scored_rows(st), "running": bool(s and not s.finished), "current": (s.current if s else None),
            "llm": llm.available(), "steltic_url": STELTIC_URL}


@app.get("/api/project/{project}")
async def get_project(project: str):
    return _project_payload(project)


@app.post("/api/project/{project}/base")
async def set_base(project: str, request: Request):
    body = await request.json()
    text = (body.get("brief") or "").strip()
    if not text:
        raise HTTPException(400, "the base brief is empty")

    def fn(st):
        st["base_brief"] = text
        st["base_source"] = body.get("source") or "typed"
    update_state(project, fn)
    return _project_payload(project)


@app.get("/api/project/{project}/base/from-design")
async def base_from_design(project: str):
    """The brief HR Steel designed THIS project from (its conversation.json)."""
    if not STELTIC_URL:
        raise HTTPException(400, "HR Steel is not installed -- install it from the Modules tab")
    try:
        data = hr.download(STELTIC_URL, clean_name(project))
    except hr.DesignError as e:
        raise HTTPException(404, f"this project has no HR Steel design yet ({e})")
    except Exception as e:
        raise HTTPException(502, f"HR Steel unreachable: {e}")
    try:
        files = mx.unzip(data)
    except Exception as e:
        raise HTTPException(502, f"package unreadable: {e}")
    brief = hr.brief_from_package(files)
    if not brief:
        raise HTTPException(404, "the design package holds no brief (conversation.json missing)")
    return {"brief": brief, "files": len(files)}


# ---------------------------------------------------------------- API: plan
def _validate_plan(items, n: int) -> list[dict]:
    out, seen = [], set()
    for i, v in enumerate(items or []):
        if not isinstance(v, dict):
            continue
        vid = f"M{len(out) + 1:03d}"
        title = str(v.get("title") or "").strip() or f"Variation {vid}"
        change = str(v.get("change") or "").strip()
        key = (title.lower(), change.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": vid, "title": title[:160], "group": str(v.get("group") or "").strip()[:80],
                    "change": change[:2000], "why": str(v.get("why") or "").strip()[:400]})
    if not out or out[0]["change"]:
        out.insert(0, {"id": "M001", "title": "Base design as briefed", "group": "reference", "change": "", "why": "Reference"})
        for i, v in enumerate(out):
            v["id"] = f"M{i + 1:03d}"
    return out[:max(1, n)] if n else out


@app.post("/api/project/{project}/plan")
async def make_plan(project: str, request: Request):
    body = await request.json()
    st = load_state(project)
    base = (st.get("base_brief") or "").strip()
    if not base:
        raise HTTPException(400, "set the base brief first")
    try:
        n = max(1, min(int(body.get("n") or st.get("n") or 10), 400))
    except Exception:
        raise HTTPException(400, "number of variations must be a whole number")
    mode = body.get("mode") or "categories"
    if mode not in ("categories", "instructions", "auto"):
        raise HTTPException(400, "mode must be categories, instructions or auto")
    cats = [c for c in (body.get("categories") or []) if library.category(c)]
    instructions = (body.get("instructions") or "").strip()
    if mode == "categories" and not cats:
        raise HTTPException(400, "choose at least one category")
    if mode == "instructions" and not instructions:
        raise HTTPException(400, "type the instructions for the study")
    note, source = "", ""
    if llm.available():
        try:
            d = llm.chat_json(prompts.PLAN_SYSTEM, prompts.plan_user(base, n, mode, cats, instructions),
                              max_tokens=min(16000, 400 + 220 * n), temperature=0.4)
            plan = _validate_plan(d.get("variations"), n)
            source = f"{llm.creds().get('model')}"
            if len(plan) < n:
                note = f"the model proposed {len(plan)} distinct variations for N = {n}"
        except Exception as e:
            plan = library.offline_plan(n, cats if mode == "categories" else None, instructions)
            source = "library (model failed)"
            note = f"the model could not plan ({type(e).__name__}: {str(e)[:200]}); the built-in library was used instead"
    else:
        plan = library.offline_plan(n, cats if mode == "categories" else None, instructions)
        source = "library (no LLM connection)"
        if mode == "instructions":
            note = "without an LLM connection the instructions cannot be interpreted; the built-in library was used instead"
        elif mode == "auto":
            note = "no LLM connection: the built-in library was used"

    def fn(s):
        s.update({"n": n, "mode": mode, "categories": cats, "instructions": instructions, "plan": plan,
                  "plan_source": source, "plan_note": note})
    update_state(project, fn)
    return _project_payload(project)


@app.put("/api/project/{project}/plan")
async def edit_plan(project: str, request: Request):
    body = await request.json()
    items = body.get("plan")
    if not isinstance(items, list):
        raise HTTPException(400, "plan must be a list")
    s = study(project)
    if s and not s.finished:
        raise HTTPException(409, "stop the study before editing the plan")
    plan = _validate_plan(items, 0)

    def fn(st):
        st["plan"] = plan
        st["n"] = len(plan)
        st["plan_source"] = (st.get("plan_source") or "") + (" + edited" if "edited" not in (st.get("plan_source") or "") else "")
    update_state(project, fn)
    return _project_payload(project)


# ---------------------------------------------------------------- API: run
@app.post("/api/project/{project}/run")
async def run_study(project: str, request: Request):
    body = await request.json()
    if not STELTIC_URL:
        raise HTTPException(400, "HR Steel is not installed -- install it from the Modules tab; the variations are designed by it")
    if not hr.healthy(STELTIC_URL):
        raise HTTPException(502, "HR Steel's server is not answering; open its Design tab once so the hub starts it")
    if not llm.creds():
        raise HTTPException(400, "set your LLM connection first (the Connection button in the title bar); HR Steel designs with it")
    s = study(project)
    if s and not s.finished:
        raise HTTPException(409, "this study is already running")
    st = load_state(project)
    if not st.get("plan"):
        raise HTTPException(400, "create the variation list first")
    if not (st.get("base_brief") or "").strip():
        raise HTTPException(400, "set the base brief first")
    want = body.get("ids")
    if want:
        ids = [v["id"] for v in st["plan"] if v["id"] in set(want)]
    else:
        ids = [r["id"] for r in rows_of(st) if r["status"] != "done" or r["stale"]]
        if body.get("all"):
            ids = [v["id"] for v in st["plan"]]
    if not ids:
        raise HTTPException(400, "every variation is designed already -- pick the ones to re-design, or run all")
    s = Study(project, ids)
    _STUDIES[clean_name(project)] = s
    s.start()
    return {"ok": True, "ids": ids}


@app.post("/api/project/{project}/stop")
async def stop_study(project: str):
    s = study(project)
    if not s or s.finished:
        return {"ok": True, "running": False}
    s.stop_flag = True
    if s.current:
        hr.stop(STELTIC_URL, building_name(project, s.current))
    return {"ok": True, "running": True}


@app.get("/api/project/{project}/events")
async def events(project: str, since: int = 0):
    """SSE of the study's events from seq > since; ends after `finished`. Re-attachable."""
    s = study(project)

    def gen():
        if not s:
            yield "data: " + json.dumps({"type": "finished", "stopped": False, "idle": True}) + "\n\n"
            return
        last = since
        last_beat = time.time()
        while True:
            with s.cv:
                pending = [e for e in s.events if e["seq"] > last]
                if not pending and not s.finished:
                    s.cv.wait(timeout=1.0)
                    pending = [e for e in s.events if e["seq"] > last]
            for e in pending:
                last = e["seq"]
                yield "data: " + json.dumps(e) + "\n\n"
                if e.get("type") == "finished":
                    return
            if s.finished and not pending:
                yield "data: " + json.dumps({"type": "finished", "stopped": s.stop_flag}) + "\n\n"
                return
            if time.time() - last_beat > KEEPALIVE:
                last_beat = time.time()
                yield ": keepalive\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- API: score, select
def _write_results(project: str, rows: list[dict], sc: dict):
    d = study_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    (d / "results.json").write_text(json.dumps({"scoring": sc, "rows": rows}, indent=1), encoding="utf-8")
    cols = ["rank", "id", "title", "group", "status", "eligible", "score"] + scoring.METRIC_NAMES + ["ineligible", "change"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get("rank"), r["id"], r["title"], r["group"], r["status"], r["eligible"], r.get("score")]
                   + [r["metrics"].get(k) for k in scoring.METRIC_NAMES]
                   + ["; ".join(r.get("ineligible") or []), r.get("change")])
    (d / "results.csv").write_text(buf.getvalue(), encoding="utf-8")


@app.post("/api/project/{project}/score")
async def score(project: str, request: Request):
    body = await request.json()
    text = (body.get("text") or "").strip()
    st = load_state(project)
    sc = dict(st["scoring"])
    rates = {**scoring.DEFAULT_RATES, **{k: float(v) for k, v in (body.get("rates") or {}).items()
                                        if k in scoring.DEFAULT_RATES and _num(v)}}
    rules = {**scoring.DEFAULT_ELIGIBILITY}
    for k, v in (body.get("rules") or {}).items():
        if k in rules:
            rules[k] = (bool(v) if isinstance(scoring.DEFAULT_ELIGIBILITY[k], bool)
                        else (None if v in (None, "", False) else float(v)))
    explanation = ""
    if not text or text == scoring.DEFAULT_EQUATION:
        equation, source = scoring.DEFAULT_EQUATION, "default"
    elif scoring.looks_like_equation(text):
        equation, source = text, "typed"
    else:
        # not an equation: an instruction in words -> the model writes the equation, shown back
        if not llm.available():
            try:
                scoring.compile_equation(text)
            except scoring.EquationError as e:
                raise HTTPException(400, "without an LLM connection the requirements must be written as an equation, "
                                         f"S = ... using the metric names listed below ({e})")
            equation, source = text, "typed"
        else:
            try:
                d = llm.chat_json(prompts.SCORE_SYSTEM, prompts.score_user(text, scoring.DEFAULT_EQUATION), max_tokens=800)
                equation = str(d.get("equation") or "").strip()
                explanation = str(d.get("explanation") or "").strip()
                source = f"interpreted by {llm.creds().get('model')}"
                scoring.compile_equation(equation)
            except scoring.EquationError as e:
                raise HTTPException(400, f"the model proposed '{equation}', which is not usable: {e}. Edit it and apply again.")
            except Exception as e:
                raise HTTPException(502, f"the model could not interpret the requirements: {e}")
    try:
        summary = scoring.score_rows(rows := rows_of(st), equation, rules, rates)
    except scoring.EquationError as e:
        raise HTTPException(400, str(e))
    sc.update({"equation": scoring.normalise_equation(equation), "equation_source": source, "source_text": text,
               "explanation": explanation, "rates": rates, "rules": rules, "scored_at": time.time(), "summary": summary})

    def fn(s):
        s["scoring"] = sc
        for r in rows:                       # keep the computed metrics (cost, value) with the results
            if r["id"] in s["results"]:
                s["results"][r["id"]]["metrics"] = r["metrics"]
    update_state(project, fn)
    _write_results(project, rows, sc)
    return {"scoring": sc, "rows": rows}


def _num(v) -> bool:
    try:
        float(v); return True
    except Exception:
        return False


@app.get("/api/project/{project}/results")
async def results(project: str):
    st = load_state(project)
    return {"scoring": st["scoring"], "rows": scored_rows(st)}


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s or "").strip("_")[:40]


@app.post("/api/project/{project}/select")
async def select(project: str, request: Request):
    body = await request.json()
    ids = [i for i in (body.get("ids") or []) if isinstance(i, str)]
    st = load_state(project)
    known = {v["id"]: v for v in st.get("plan") or []}
    bad = [i for i in ids if i not in known]
    if bad:
        raise HTTPException(400, f"unknown variation(s): {', '.join(bad)}")
    rows = {r["id"]: r for r in scored_rows(st)}
    sel_dir = study_dir(project) / "selected"
    if sel_dir.exists():
        shutil.rmtree(sel_dir, ignore_errors=True)
    sel_dir.mkdir(parents=True, exist_ok=True)
    chosen = []
    for i in ids:
        r = rows[i]
        entry = {"id": i, "title": r["title"], "group": r["group"], "change": r["change"], "status": r["status"],
                 "score": r.get("score"), "rank": r.get("rank"), "building": r.get("building"),
                 "metrics": {k: r["metrics"].get(k) for k in scoring.METRIC_NAMES if r["metrics"].get(k) is not None},
                 "package": None, "report": None}
        src = var_dir(project, i)
        if (src / "package.zip").is_file():
            name = f"{i}_{slug(r['title'])}.zip"
            shutil.copy2(src / "package.zip", sel_dir / name)
            entry["package"] = f"variations/selected/{name}"
        if (src / "report.html").is_file():
            entry["report"] = f"variations/{i}/report.html"
        chosen.append(entry)
    record = {"project": clean_name(project), "recorded_at": time.time(), "note": (body.get("note") or "").strip()[:2000],
              "equation": st["scoring"].get("equation"), "rules": st["scoring"].get("rules"),
              "selected": chosen}
    (study_dir(project) / "selection.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
    (JOBS / clean_name(project) / "selected_variations.json").write_text(json.dumps(record, indent=1), encoding="utf-8")

    def fn(s):
        s["selection"] = {"ids": ids, "note": record["note"], "recorded_at": record["recorded_at"]}
    update_state(project, fn)
    return {"ok": True, "selection": record}


# ---------------------------------------------------------------- API: files, UI
@app.get("/api/project/{project}/file/{vid}/{name}")
async def var_file(project: str, vid: str, name: str):
    if name not in ("report.html", "viewer_3d.html", "package.zip", "metrics.json"):
        raise HTTPException(404, "no such file")
    p = var_dir(project, vid) / name
    if not p.is_file():
        raise HTTPException(404, f"{vid} has no {name} yet")
    return FileResponse(str(p), filename=(f"{clean_name(project)}_{vid}_design.zip" if name == "package.zip" else None))


@app.delete("/api/project/{project}/variation/{vid}")
async def drop_result(project: str, vid: str):
    s = study(project)
    if s and not s.finished and s.current == vid:
        raise HTTPException(409, "that variation is being designed right now")
    d = var_dir(project, vid)
    shutil.rmtree(d, ignore_errors=True)

    def fn(st):
        st["results"].pop(vid, None)
    update_state(project, fn)
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def index():
    return (UI / "index.html").read_text(encoding="utf-8")
