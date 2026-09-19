"""Probabilistic analysis -- the module server.

One project = one study of one design package. Everything lives in
<PROB_JOBS>/<project>/probabilistic/: package/ (the unpacked HR Steel design), probe.json (model
summary + combinations), realisations.json (the sampled inputs), results/r####.json (one per
realisation, written by the workers), and the outputs (summary.json, results.csv, report.html,
capacity_distribution.svg).

The analyses run in worker processes under the Nonlinear module's interpreter (DDM_PYTHON):
that environment holds openseespy, and the HR Steel engine is put on its path (STELTIC_ENGINE_DIR)
so the design's own elastic LRFD model is what gets perturbed and re-analysed. This server only
samples, schedules, watches, re-checks the demands against the design's capacities and summarises.
"""
from __future__ import annotations
import io, json, os, pathlib, re, shutil, subprocess, sys, threading, time, zipfile
from typing import Optional

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import stats as S, variables as V

HERE = pathlib.Path(__file__).resolve().parent
UI = HERE / "ui"
WORKER = HERE / "worker.py"
JOBS = pathlib.Path(os.environ.get("PROB_JOBS") or (pathlib.Path.cwd() / "probabilistic_data")).resolve()
STELTIC_URL = (os.environ.get("STELTIC_URL") or "").rstrip("/")
DDM_PYTHON = os.environ.get("DDM_PYTHON") or ""
ENGINE_DIR = os.environ.get("STELTIC_ENGINE_DIR") or ""
KEEPALIVE = 20.0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEFAULT_OPTIONS = {"nseg": 6}          # beam sub-elements in the static model (HR Steel's default)

app = FastAPI(title="Probabilistic analysis")
app.mount("/static", StaticFiles(directory=str(UI)), name="static")


# ---------------------------------------------------------------- names, folders, state
def clean_name(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]", "", (s or "").strip())
    return s or "Project"


def study_dir(project: str) -> pathlib.Path:
    return JOBS / clean_name(project) / "probabilistic"


def package_dir(project: str) -> pathlib.Path:
    return study_dir(project) / "package"


def results_dir(project: str) -> pathlib.Path:
    return study_dir(project) / "results"


_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()


def _lock(project: str) -> threading.Lock:
    with _GUARD:
        return _LOCKS.setdefault(clean_name(project), threading.Lock())


def new_state() -> dict:
    return {"version": 1, "package": None, "settings": {"n": 100, "seed": 1, "combo": "", "workers": None, "variables": {}, "options": {}},
            "run": None, "updated_at": None}


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
            st = new_state(); st.update(json.loads(_read_text(p)))
            return st
        except Exception:
            pass
    return new_state()


def save_state(project: str, st: dict):
    d = study_dir(project); d.mkdir(parents=True, exist_ok=True)
    st["updated_at"] = time.time()
    tmp = d / "state.json.tmp"
    tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
    _replace(tmp, d / "state.json")


def update_state(project: str, fn):
    with _lock(project):
        st = load_state(project); fn(st); save_state(project, st)
        return st


def load_probe(project: str) -> Optional[dict]:
    p = study_dir(project) / "probe.json"
    if p.is_file():
        try:
            return json.loads(_read_text(p))
        except Exception:
            return None
    return None


def load_spec(project: str) -> Optional[dict]:
    p = study_dir(project) / "realisations.json"
    if p.is_file():
        try:
            return json.loads(_read_text(p))
        except Exception:
            return None
    return None


def load_results(project: str) -> list[dict]:
    d = results_dir(project)
    out = []
    if d.is_dir():
        for p in sorted(d.glob("r*.json")):
            try:
                out.append(json.loads(_read_text(p)))
            except Exception:
                pass
    return out


def default_workers() -> int:
    return max(1, min(4, (os.cpu_count() or 2) - 1))


def toolchain() -> dict:
    return {"ddm_python": DDM_PYTHON, "ddm_python_ok": bool(DDM_PYTHON) and pathlib.Path(DDM_PYTHON).exists(),
            "engine_dir": ENGINE_DIR, "engine_ok": bool(ENGINE_DIR) and (pathlib.Path(ENGINE_DIR) / "engine3d.py").exists(),
            "steltic_url": STELTIC_URL, "cpu_count": os.cpu_count() or 1, "default_workers": default_workers()}


def _need_toolchain():
    t = toolchain()
    if not t["ddm_python_ok"]:
        raise HTTPException(400, "the Nonlinear (SNL) module is not installed -- its environment (openseespy) runs the analyses. Install it from the Modules tab.")
    if not t["engine_ok"]:
        raise HTTPException(400, "HR Steel is not installed -- its engine is the analysis model. Install it from the Modules tab.")


# ---------------------------------------------------------------- package
def _unzip_into(data: bytes, dest: pathlib.Path) -> int:
    z = zipfile.ZipFile(io.BytesIO(data))
    infos = [i for i in z.infolist() if not i.is_dir() and ".." not in i.filename and not i.filename.startswith(("/", "\\"))]
    if not infos:
        raise ValueError("the zip is empty")
    tops = {i.filename.replace("\\", "/").split("/", 1)[0] for i in infos if "/" in i.filename.replace("\\", "/")}
    wrap = None
    if len(tops) == 1 and all("/" in i.filename.replace("\\", "/") for i in infos):
        wrap = tops.pop()
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for i in infos:
        rel = i.filename.replace("\\", "/")
        if wrap and rel.startswith(wrap + "/"):
            rel = rel[len(wrap) + 1:]
        if not rel:
            continue
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(z.read(i)); n += 1
    return n


def _probe(project: str) -> dict:
    _need_toolchain()
    out = study_dir(project) / "probe.json"
    cmd = [DDM_PYTHON, str(WORKER), "probe", "--package", str(package_dir(project)), "--engine", ENGINE_DIR, "--out", str(out)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise HTTPException(500, "reading the package timed out")
    if r.returncode != 0 or not out.is_file():
        tail = (r.stderr or r.stdout or "")[-1500:]
        raise HTTPException(400, "the package could not be analysed with the HR Steel engine: " + tail)
    probe = json.loads(out.read_text(encoding="utf-8"))
    for f in ("model_opensees.py", "cfg.py", "design/member_schedule.csv"):
        if not (package_dir(project) / f).is_file():
            raise HTTPException(400, f"the package has no {f} -- it is not a complete HR Steel design")
    return probe


# ---------------------------------------------------------------- the study (workers)
class Study(threading.Thread):
    def __init__(self, project: str, spec: dict, ids: list[int], workers: int):
        super().__init__(daemon=True, name=f"prob-{project}")
        self.project = clean_name(project)
        self.spec, self.ids, self.workers = spec, ids, max(1, workers)
        self.events: list[dict] = []
        self.seq = 0
        self.cv = threading.Condition()
        self.procs: list[subprocess.Popen] = []
        self.stop_flag = False
        self.finished = False
        self.status: dict[int, str] = {i: "pending" for i in ids}
        self.started_at = time.time()
        self.done_seconds: list[float] = []

    def emit(self, ev: dict):
        with self.cv:
            self.seq += 1
            self.events.append({"seq": self.seq, "t": time.time(), **ev})
            if len(self.events) > 5000:
                del self.events[:1000]
            self.cv.notify_all()

    def progress(self) -> dict:
        done = sum(1 for s in self.status.values() if s in ("done", "failed"))
        running = [i for i, s in self.status.items() if s == "running"]
        mean = (sum(self.done_seconds) / len(self.done_seconds)) if self.done_seconds else None
        remaining = len(self.ids) - done
        eta = (remaining * mean / self.workers) if (mean and remaining) else None
        return {"total": len(self.ids), "done": done, "running": running, "failed": sum(1 for s in self.status.values() if s == "failed"),
                "mean_seconds": mean, "eta_seconds": eta, "elapsed": time.time() - self.started_at, "workers": self.workers}

    def _reader(self, w: int, pr: subprocess.Popen, logf):
        for raw in pr.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            logf.write(f"[w{w}] {line}\n"); logf.flush()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            kind = ev.get("event"); i = ev.get("id")
            if kind == "start":
                self.status[i] = "running"
                self.emit({"type": "start", "id": i, "worker": w, "nominal": ev.get("nominal")})
            elif kind == "done":
                ok = bool(ev.get("ok"))
                self.status[i] = "done" if ok else "failed"
                if ok and isinstance(ev.get("seconds"), (int, float)):
                    self.done_seconds.append(float(ev["seconds"]))
                self.emit({"type": "done", "id": i, "worker": w, "ok": ok, "T1": ev.get("T1"), "V_kip": ev.get("V_kip"),
                           "seconds": ev.get("seconds"), "error": ev.get("error"), "progress": self.progress()})
            elif kind == "skip":
                self.status[i] = "done"

    def run(self):
        sd = study_dir(self.project)
        out = results_dir(self.project); out.mkdir(parents=True, exist_ok=True)
        spec_path = sd / "realisations.json"
        # id 0 (the design model) first and alone on worker 0 so its time is known early
        lanes = [[] for _ in range(self.workers)]
        order = sorted(self.ids)
        for k, i in enumerate(order):
            lanes[k % self.workers].append(i)
        self.emit({"type": "log", "text": f"{len(self.ids)} realisation(s) on {self.workers} worker(s); results in {out}"})
        logf = open(sd / "workers.log", "a", encoding="utf-8")
        threads = []
        try:
            for w, lane in enumerate(lanes):
                if not lane:
                    continue
                cmd = [DDM_PYTHON, str(WORKER), "run", "--package", str(package_dir(self.project)), "--engine", ENGINE_DIR,
                       "--spec", str(spec_path), "--out", str(out), "--ids", ",".join(str(i) for i in lane)]
                logf.write(f"=== {time.ctime()} worker {w}: {' '.join(cmd)}\n"); logf.flush()
                env = dict(os.environ, PYTHONUNBUFFERED="1", STELTIC_ENGINE_DIR=ENGINE_DIR)
                kw = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, cwd=str(sd))
                if sys.platform == "win32":
                    kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | _NO_WINDOW
                else:
                    kw["start_new_session"] = True
                pr = subprocess.Popen(cmd, **kw)
                self.procs.append(pr)
                t = threading.Thread(target=self._reader, args=(w, pr, logf), daemon=True); t.start(); threads.append(t)
            for t in threads:
                t.join()
            for pr in self.procs:
                pr.wait()
        except Exception as e:
            self.emit({"type": "log", "text": f"worker start failed: {type(e).__name__}: {e}"})
        finally:
            logf.close()
            for i, s in self.status.items():
                if s in ("pending", "running"):
                    self.status[i] = "pending"
            self.finished = True
            self.emit({"type": "finished", "stopped": self.stop_flag, "progress": self.progress()})
            with self.cv:
                self.cv.notify_all()
            update_state(self.project, lambda st: st.__setitem__("run", {**(st.get("run") or {}), "finished_at": time.time(),
                                                                          "status": "stopped" if self.stop_flag else "finished"}))

    def stop(self):
        self.stop_flag = True
        for pr in self.procs:
            if pr.poll() is not None:
                continue
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/PID", str(pr.pid), "/T", "/F"], capture_output=True, creationflags=_NO_WINDOW)
                else:
                    os.killpg(pr.pid, 15)
            except Exception:
                try:
                    pr.kill()
                except Exception:
                    pass


_STUDIES: dict[str, Study] = {}


def study(project: str) -> Optional[Study]:
    return _STUDIES.get(clean_name(project))


# ---------------------------------------------------------------- API: basics
@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/me")
async def me():
    t = toolchain()
    up = False
    if STELTIC_URL:
        try:
            with httpx.Client(timeout=3.0) as cx:
                up = cx.get(STELTIC_URL + "/healthz").status_code < 500
        except Exception:
            up = False
    return {**t, "steltic_up": up, "jobs": str(JOBS)}


@app.get("/api/library")
async def library():
    return {"variables": V.VARIABLES, "not_varied": V.NOT_VARIED, "options": DEFAULT_OPTIONS, "default_n": 100}


# ---------------------------------------------------------------- API: project
def _payload(project: str) -> dict:
    st = load_state(project)
    probe = load_probe(project)
    spec = load_spec(project)
    s = study(project)
    results = load_results(project)
    n_ok = sum(1 for r in results if r.get("ok") and r.get("id") != 0)
    base = next((r for r in results if r.get("id") == 0), None)
    p_small = None
    if probe:
        p_small = {k: probe.get(k) for k in ("name", "members", "stories", "Fy_nominal", "risk_category", "system", "NX", "NY", "drift_limit")}
        p_small["groups"] = probe.get("groups", [])
        p_small["n_combos"] = len(probe.get("combos", []))
        d = probe.get("design") or {}
        p_small["design"] = {"members": [{k: m.get(k) for k in ("id", "kind", "role", "section", "DC", "limit_state")} for m in d.get("members", [])],
                             "connections": [{k: c.get(k) for k in ("id", "type", "DC", "limit_state")} for c in d.get("connections", [])],
                             "n_members_dc": d.get("n_members_dc"), "n_connections_dc": d.get("n_connections_dc")}
    return {"project": clean_name(project), "package": st.get("package"), "settings": st.get("settings"), "run": st.get("run"),
            "probe": p_small, "spec": ({"n": spec["n"], "seed": spec["seed"], "variables": spec.get("variables"), "options": spec.get("options")} if spec else None),
            "results": {"n_ok": n_ok, "n_failed": sum(1 for r in results if not r.get("ok")), "n_total": len(results),
                        "base": ({"ok": base.get("ok"), "T1": base.get("T1"), "V_kip": base.get("V_kip"), "seconds": base.get("seconds"),
                                  "error": base.get("error")} if base else None),
                        "status": {str(r["id"]): ("done" if r.get("ok") else "failed") for r in results}},
            "running": bool(s and not s.finished), "progress": (s.progress() if s else None), "toolchain": toolchain()}


@app.get("/api/project/{project}")
async def get_project(project: str):
    return _payload(project)


@app.get("/api/project/{project}/zips")
async def project_zips(project: str):
    root = JOBS / clean_name(project)
    out = []
    if root.is_dir():
        for p in sorted(root.rglob("*.zip")):
            rel = p.relative_to(root).as_posix()
            if rel.startswith("probabilistic/"):
                continue
            out.append({"path": rel, "bytes": p.stat().st_size})
    return {"zips": out[:200]}


def _install_package(project: str, data: bytes, source: str, label: str) -> dict:
    try:
        n = _unzip_into(data, package_dir(project))
    except zipfile.BadZipFile:
        raise HTTPException(400, "that is not a zip file")
    except ValueError as e:
        raise HTTPException(400, str(e))
    probe = _probe(project)
    if not (probe.get("design") or {}).get("n_members_dc"):
        raise HTTPException(400, "the package's calc_package.json holds no member D/C -- the design was not completed by HR Steel (its agent fills the "
                                 "capacities); design it first, then load it here")

    def fn(st):
        st["package"] = {"source": source, "label": label, "files": n, "loaded_at": time.time(), "name": probe.get("name")}
        st["run"] = None
    update_state(project, fn)
    # a new package invalidates old realisations
    for p in (study_dir(project) / "realisations.json",):
        if p.exists():
            p.unlink()
    shutil.rmtree(results_dir(project), ignore_errors=True)
    return _payload(project)


@app.post("/api/project/{project}/package")
async def set_package(project: str, request: Request):
    body = await request.json()
    s = study(project)
    if s and not s.finished:
        raise HTTPException(409, "stop the running study first")
    src = body.get("source")
    if src == "steltic":
        if not STELTIC_URL:
            raise HTTPException(400, "HR Steel is not installed")
        try:
            with httpx.Client(timeout=httpx.Timeout(180.0, connect=20.0)) as cx:
                r = cx.get(STELTIC_URL + f"/api/download/{clean_name(project)}")
        except Exception as e:
            raise HTTPException(502, f"HR Steel unreachable: {e}")
        if r.status_code >= 400:
            raise HTTPException(404, f"this project has no HR Steel design yet ({r.status_code}) -- design it on the HR Steel tab first")
        return _install_package(project, r.content, "steltic", f"HR Steel design of {clean_name(project)}")
    if src == "zip":
        rel = (body.get("path") or "").replace("\\", "/")
        root = (JOBS / clean_name(project)).resolve()
        p = (root / rel).resolve()
        if root not in p.parents or not p.is_file():
            raise HTTPException(400, "pick a zip inside this project's folder")
        return _install_package(project, p.read_bytes(), "zip", rel)
    raise HTTPException(400, "source must be steltic or zip (or upload a file)")


@app.post("/api/project/{project}/package/upload")
async def upload_package(project: str, file: UploadFile = File(...)):
    s = study(project)
    if s and not s.finished:
        raise HTTPException(409, "stop the running study first")
    data = await file.read()
    return _install_package(project, data, "upload", file.filename or "upload.zip")


# ---------------------------------------------------------------- API: run
@app.post("/api/project/{project}/run")
async def run_study(project: str, request: Request):
    body = await request.json()
    _need_toolchain()
    s = study(project)
    if s and not s.finished:
        raise HTTPException(409, "this study is already running")
    probe = load_probe(project)
    if not probe or not package_dir(project).is_dir():
        raise HTTPException(400, "load the design package first")
    st = load_state(project)
    sett = dict(st.get("settings") or {})
    cont = bool(body.get("continue"))
    spec = load_spec(project) if cont else None
    if not spec:
        try:
            n = max(1, min(int(body.get("n") or sett.get("n") or 100), 5000))
            seed = int(body.get("seed") if body.get("seed") not in (None, "") else (sett.get("seed") or 1))
        except Exception:
            raise HTTPException(400, "n and seed must be whole numbers")
        variables = V.merged(body.get("variables") or sett.get("variables") or {})
        if not any(v.get("enabled", True) for v in variables):
            raise HTTPException(400, "enable at least one random variable")
        options = {**DEFAULT_OPTIONS, **{k: v for k, v in (body.get("options") or {}).items() if k in DEFAULT_OPTIONS}}
        try:
            options["nseg"] = max(1, min(int(options["nseg"]), 20))
        except Exception:
            options["nseg"] = DEFAULT_OPTIONS["nseg"]
        try:
            spec = V.build_spec(probe, n, seed, options, variables)
        except ValueError as e:
            raise HTTPException(400, str(e))
        sd = study_dir(project); sd.mkdir(parents=True, exist_ok=True)
        (sd / "realisations.json").write_text(json.dumps(spec), encoding="utf-8")
        shutil.rmtree(results_dir(project), ignore_errors=True)
        for p in ("summary.json", "results.csv", "report.html", "dc_members.svg", "dc_connections.svg"):
            if (sd / p).exists():
                (sd / p).unlink()
        sett.update({"n": n, "seed": seed, "variables": body.get("variables") or sett.get("variables") or {}, "options": options})
    workers = body.get("workers") or sett.get("workers") or default_workers()
    try:
        workers = max(1, min(int(workers), 64))
    except Exception:
        workers = default_workers()
    sett["workers"] = workers
    have = {r["id"] for r in load_results(project) if r.get("ok")}
    ids = [r["id"] for r in spec["realisations"] if r["id"] not in have]
    if not ids:
        raise HTTPException(400, "every realisation is done -- start a new study to run more")
    s = Study(project, spec, ids, workers)
    _STUDIES[clean_name(project)] = s

    def fn(st2):
        st2["settings"] = sett
        st2["run"] = {"started_at": time.time(), "finished_at": None, "status": "running", "ids": len(ids), "workers": workers}
    update_state(project, fn)
    s.start()
    return {"ok": True, "ids": len(ids), "workers": workers, "n": spec["n"]}


@app.post("/api/project/{project}/stop")
async def stop_study(project: str):
    s = study(project)
    if not s or s.finished:
        return {"ok": True, "running": False}
    s.stop()
    return {"ok": True, "running": True}


@app.get("/api/project/{project}/events")
async def events(project: str, since: int = 0):
    s = study(project)

    def gen():
        if not s:
            yield "data: " + json.dumps({"type": "finished", "idle": True}) + "\n\n"
            return
        last, beat = since, time.time()
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
                yield "data: " + json.dumps({"type": "finished", "stopped": s.stop_flag, "progress": s.progress()}) + "\n\n"
                return
            if time.time() - beat > KEEPALIVE:
                beat = time.time()
                yield ": keepalive\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- API: results
_CACHE: dict[str, tuple] = {}


def _basis(project: str) -> str:
    return "design" if (load_state(project).get("settings") or {}).get("basis") == "design" else "nominal"


def _analysis(project: str) -> Optional[dict]:
    spec = load_spec(project)
    if not spec:
        return None
    d = results_dir(project)
    basis = _basis(project)
    stamp = (basis,) + tuple(sorted((p.name, p.stat().st_mtime_ns) for p in d.glob("r*.json"))) if d.is_dir() else (basis,)
    key = clean_name(project)
    hit = _CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    results = load_results(project)
    probe = load_probe(project)
    a = S.analyse(spec, results, probe, basis)
    a["variables"] = [{**next((v for v in V.VARIABLES if v["id"] == sv["id"]), {}), **sv} for sv in (spec.get("variables") or [])]
    a["options"] = spec.get("options")
    if a.get("members"):
        try:
            S.write_outputs(study_dir(project), a, clean_name(project), probe)
        except Exception as e:
            a["write_error"] = str(e)
    _CACHE[key] = (stamp, a)
    return a


@app.get("/api/project/{project}/results")
async def results(project: str):
    a = _analysis(project)
    if not a:
        return {"analysis": None, "plots": {}, "basis": _basis(project), "bases": S.BASES}
    return {"analysis": a, "plots": S.plots(a), "basis": a["basis"], "bases": S.BASES}


@app.post("/api/project/{project}/basis")
async def set_basis(project: str, request: Request):
    """Which capacity the ratios use: 'nominal' (D/Rn, phi removed -- the default) or 'design' (D/phiRn)."""
    body = await request.json()
    basis = "design" if body.get("basis") == "design" else "nominal"
    update_state(project, lambda st: st["settings"].__setitem__("basis", basis))
    return {"ok": True, "basis": basis}


@app.get("/api/project/{project}/plot/{which}.svg")
async def plot(project: str, which: str):
    a = _analysis(project) or {}
    p = S.plots(a) if a else {}
    if which not in ("members", "connections", "drift", "groups"):
        raise HTTPException(404, "no such plot")
    return Response(p.get(which) or S.svg_histogram(None, which, ""), media_type="image/svg+xml")


@app.get("/api/project/{project}/file/{name}")
async def study_file(project: str, name: str):
    if name not in ("report.html", "results.csv", "summary.json", "realisations.json", "workers.log", "probe.json", "dc_members.svg", "dc_connections.svg", "dc_groups.svg"):
        raise HTTPException(404, "no such file")
    p = study_dir(project) / name
    if not p.is_file():
        raise HTTPException(404, f"{name} not written yet")
    return FileResponse(str(p))


@app.get("/api/project/{project}/realisation/{rid}")
async def realisation(project: str, rid: int):
    p = results_dir(project) / ("r%04d.json" % rid)
    if not p.is_file():
        raise HTTPException(404, "no result for that realisation")
    r = json.loads(p.read_text(encoding="utf-8"))
    spec = load_spec(project) or {}
    sample = next((x.get("sample") for x in spec.get("realisations", []) if x["id"] == rid), None)
    return {"result": r, "sample": sample}


@app.get("/", response_class=HTMLResponse)
async def index():
    return (UI / "index.html").read_text(encoding="utf-8")
