"""Steltic Hub -- the FastAPI app behind the desktop window.

The hub is deliberately the only long-lived process that knows about *all* modules, and it
knows about them only through manifests. The desktop shell (Tauri/Electron/none) just opens
a window on this server, which is why the shell stays a swappable, late decision.
"""
from __future__ import annotations
import dataclasses
import asyncio, json, mimetypes, os, queue, subprocess, sys, threading, time, uuid, pathlib
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, envs, jobs, sac
from .manifest import ManifestError
from .registry import Registry, RegistryError
from .runners import ServerSupervisor, JobRuns, run_cli, run_http, sse, RunError, build_ctx, expand

REG = Registry()

# The LLM connection: typed once, kept in the hub's data folder on this PC (connection.json),
# loaded on start and handed to every module server that declares `credentials`. The design
# modules themselves only ever hold it in memory; the hub is the one place it is stored, and it
# goes nowhere except to the LLM provider the user chose.
_CONNECTION: dict | None = None


def _load_connection() -> dict | None:
    try:
        if config.CONNECTION_FILE.exists():
            d = json.loads(config.CONNECTION_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("model"):
                return d
    except Exception:
        pass
    return None


def _store_connection(conn: dict | None):
    try:
        if conn is None:
            config.CONNECTION_FILE.unlink(missing_ok=True)
            return
        tmp = config.CONNECTION_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(conn, indent=2), encoding="utf-8")
        try:
            import os
            os.chmod(tmp, 0o600)          # POSIX: owner-only; on Windows LOCALAPPDATA is per user already
        except Exception:
            pass
        tmp.replace(config.CONNECTION_FILE)
    except Exception:
        pass


_CONNECTION = _load_connection()
SUP = ServerSupervisor(REG, connection=lambda: _CONNECTION)

# ---------------------------------------------------------------- this process
# The hub is single-instance: a second launch raises a window on the running one. When the code on
# disk has moved on (a git pull, an edit in a checkout), that running hub is the OLD version and
# keeps being the one the window opens. So the hub knows when it started, can say whether any of
# its own source files is newer than that, and can hand its port to a fresh copy of itself.
STARTED = time.time()
_SOURCE_SUFFIXES = {".py", ".js", ".html", ".css", ".json"}


def source_stale(since: float | None = None) -> bool:
    """Is any hub source file (the package, its UI, the bundled catalog, pyproject.toml) newer than
    `since` (default: this process's start)? A cheap walk -- a few hundred files -- that stops at the
    first hit. __pycache__ is skipped; a stray .pyc never counts."""
    since = STARTED if since is None else since
    roots = [config.PKG]
    pyproject = config.PKG.parent / "pyproject.toml"
    if pyproject.exists():
        roots.append(pyproject)
    stack = list(roots)
    while stack:
        p = stack.pop()
        try:
            if p.is_dir():
                if p.name == "__pycache__":
                    continue
                stack.extend(p.iterdir())
            elif p.suffix in _SOURCE_SUFFIXES and p.stat().st_mtime > since:
                return True
        except OSError:
            continue
    return False


def hub_info() -> dict:
    return {"version": __import__("steltic_hub").__version__, "pid": os.getpid(), "started": STARTED,
            "stale": source_stale(), "restartable": getattr(app.state, "server", None) is not None,
            "source": str(config.PKG.parent)}


def _request_exit() -> bool:
    """Ask the uvicorn server the CLI handed us to stop -- gracefully: the lifespan hook then stops
    every module server and retires the URL marker. False when not running under steltic-hub (tests)."""
    server = getattr(app.state, "server", None)
    if server is None:
        return False
    server.should_exit = True
    return True


def _spawn_replacement() -> int:
    """Start a fresh hub that waits for our port to free up, then takes it over (cli.py --replace).
    Detached from us so it survives our exit; the same interpreter (pythonw under the launcher, so no
    console flashes), the same environment, the same port."""
    argv = [sys.executable, "-m", "steltic_hub.cli", "--port", str(getattr(app.state, "port", 8300)),
            "--no-browser", "--replace", str(os.getpid())]
    kw: dict = {"close_fds": True, "cwd": str(config.DATA)}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP   # type: ignore[attr-defined]
    else:
        kw["start_new_session"] = True
        kw["stdin"] = kw["stdout"] = kw["stderr"] = subprocess.DEVNULL
    return subprocess.Popen(argv, **kw).pid
RUNS = JobRuns()


@asynccontextmanager
async def _lifespan(_app):
    yield
    SUP.stop_all()          # never leave a module server (or an OpenSees run) orphaned
    for rid in list(RUNS.procs):
        try:
            await RUNS.cancel(rid)
        except Exception:
            pass
    # uvicorn re-raises the signal that stopped it once shutdown is complete, so nothing after
    # server.run() in cli.py gets a chance to run: the URL marker is retired here instead -- but only
    # our own marker: a replacement hub (/api/hub/restart) may already have written its URL there.
    try:
        mine = getattr(app.state, "url", None)
        if config.URL_FILE.exists() and (mine is None or config.URL_FILE.read_text(encoding="utf-8").strip() == mine):
            config.URL_FILE.unlink()
    except Exception:
        pass


app = FastAPI(title="Steltic Hub", lifespan=_lifespan)

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

# The hub binds to 127.0.0.1 with no authentication, which keeps other MACHINES out -- but not other
# web pages. A browser sends a "simple" cross-site request (a POST with a text/plain body, or with no
# body at all) without any CORS preflight, so a page on any site the user has open could fire
# `POST /api/connection` (redirecting the LLM key to its own endpoint), register a module from its own
# git URL and `POST /api/modules/<id>/install` it (`pip install -e .` runs arbitrary code), start runs,
# or delete projects. CORS only stops the page READING the reply; the side effect has happened.
#
# So every state-changing request must come from the hub's own page: the browser tells us where a
# request originated (`Origin`, sent on every cross-site request and on every fetch() POST; and
# `Sec-Fetch-Site`), and anything not from a loopback origin is refused. The Host check closes the
# DNS-rebinding variant (a hostname the attacker points at 127.0.0.1). "testserver" is the name
# Starlette's TestClient uses; it is not a resolvable public name.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "testserver"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _host_name(value: str) -> str:
    """`127.0.0.1:8300` -> `127.0.0.1`; `[::1]:8300` -> `[::1]`; `http://x:1/` -> `x`."""
    v = (value or "").strip().lower()
    if "://" in v:
        v = v.split("://", 1)[1]
    v = v.split("/", 1)[0]
    if v.startswith("["):                       # bracketed IPv6, maybe with a port
        return v.split("]", 1)[0] + "]"
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


def _cross_site_reason(request: Request) -> str | None:
    """Why this request must not be acted on -- or None when it plainly came from our own page."""
    host = _host_name(request.headers.get("host", ""))
    if host and host not in _LOOPBACK_HOSTS:
        return f"the hub only answers to loopback names, not {host!r}"
    if request.method in _SAFE_METHODS:
        return None
    origin = request.headers.get("origin")
    if origin is not None and _host_name(origin) not in _LOOPBACK_HOSTS:
        return f"cross-site request from {origin!r} refused"
    site = (request.headers.get("sec-fetch-site") or "").lower()
    if site == "cross-site":
        return "cross-site request refused"
    return None


@app.middleware("http")
async def _no_stale(request: Request, call_next):
    why = _cross_site_reason(request)
    if why:
        return JSONResponse({"detail": why}, status_code=403)
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.startswith(("/static/", "/job/", "/out/")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def _threaded_sse(work):
    """Run a blocking install/update on a worker thread, streaming its log lines as SSE.

    git clone and a 400 MB openseespy install are blocking and slow; this keeps the UI live
    and gives the user the actual pip output instead of a spinner.
    """
    q: queue.Queue = queue.Queue()

    def log(msg=""):
        q.put({"type": "log", "text": str(msg)})

    def runner():
        try:
            result = work(log)
            q.put({"type": "done", "ok": True, "result": result})
        except Exception as e:
            q.put({"type": "error", "text": f"{type(e).__name__}: {e}"})
            q.put({"type": "done", "ok": False})
        finally:
            q.put(None)

    threading.Thread(target=runner, daemon=True).start()

    async def gen():
        loop = asyncio.get_running_loop()
        while True:
            fut = loop.run_in_executor(None, q.get)
            while True:
                try:
                    ev = await asyncio.wait_for(asyncio.shield(fut), timeout=config.KEEPALIVE)
                    break
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
            if ev is None:
                break
            yield sse(ev)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


def _sse_error(text: str, status: int = 200) -> StreamingResponse:
    """A run refused up front still answers as an event stream, so the UI has one code path."""
    return StreamingResponse(iter([sse({"type": "error", "text": text}),
                                   sse({"type": "done", "ok": False, "rc": -1})]),
                             media_type="text/event-stream", headers=_SSE_HEADERS, status_code=status)


# ---------------------------------------------------------------- state
@app.get("/api/state")
async def state():
    loop = asyncio.get_running_loop()
    known = REG.known()
    # status() may spawn interpreters (cached after the first time) and run git: off the loop.
    # The optional-component probe rides along for the same reason -- see _optional_present.
    statuses, optional = await loop.run_in_executor(
        None, lambda: ({m.id: _safe_status(m.id) for m in known},
                       {m.id: _optional_present(m) for m in known}))
    mods = []
    for m in known:
        d = m.to_json()
        present = optional.get(m.id) or {}
        for td, t in zip(d["tabs"], m.tabs):
            # What this tab's run needs and has not got. The UI gates its button on this, so a missing
            # multi-gigabyte download is something the user reads before pressing Run rather than the
            # answer /api/run gives them afterwards.
            td["missing_optional"] = [g for g in (t.requires_optional or []) if not present.get(g)]
        d["default_name"] = m.name
        d["name"] = REG.name(m.id)               # the user's own name for it, if they set one
        d["status"] = statuses[m.id]
        d["server_up"] = SUP.is_up(m.id)
        d["server_url"] = SUP.base_url(m.id) if SUP.is_up(m.id) else ""
        d["missing_needs"] = [n for n in m.needs if not REG.is_installed(n)]
        d["servers_used"] = {r: {"installed": REG.is_installed(r), "up": SUP.is_up(r),
                                 "name": REG.name(r)} for r in d.get("requires_servers", [])}
        mods.append(d)
    jobs_list = await loop.run_in_executor(None, jobs.list_jobs)
    return {"modules": mods, "jobs": jobs_list, "data_dir": str(config.DATA),
            "uv": envs.uv_path() or "", "connection": bool(_CONNECTION),
            # Keyed "<module>.<tab>", which is exactly the key the UI builds with runKey(). The old
            # shape was {run-uuid: True}: true, but useless to a client that knows modules and tabs.
            "running": RUNS.live_tabs(),
            "version": __import__("steltic_hub").__version__, "hub": hub_info(),
            "smart_app_control": sac.state()}          # Windows 11: 'on' blocks PyTorch / OpenSees (WinError 4551)


def _safe_status(mod_id: str) -> dict:
    try:
        return REG.status(mod_id)
    except Exception as e:
        return {"installed": False, "env_ready": False, "error": str(e)}


def _optional_present(m) -> dict:
    """Which optional components this module's tabs require, and whether each one is importable.

    /api/state is called after every run and every refresh, so this must stay cheap. It is: only
    groups a tab actually declares are looked at, envs.optional_present answers False without
    spawning anything while the module has no environment (so an uninstalled module never probes),
    and it caches its answer per (module, group) until an install invalidates it. The one interpreter
    call per group therefore happens at most once per hub run, inside this executor hop.
    """
    needed = {g for t in m.tabs for g in (t.requires_optional or [])}
    return {g: envs.optional_present(m, g) for g in needed}


@app.get("/api/modules/{mod_id}/check-update")
async def check_update(mod_id: str):
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, lambda: REG.check_update(mod_id))
    except RegistryError as e:
        raise HTTPException(404, str(e))


@app.post("/api/modules/{mod_id}/install")
async def install(mod_id: str, rebuild: bool = False):
    try:
        REG.manifest(mod_id)
    except RegistryError as e:
        raise HTTPException(404, str(e))

    def work(log):
        if rebuild:
            SUP.stop(mod_id)                  # the env is about to be deleted under it
        return REG.install(mod_id, log=log, force_env=rebuild).name
    return _threaded_sse(work)


@app.post("/api/modules/{mod_id}/update")
async def update(mod_id: str):
    try:
        REG.manifest(mod_id)
    except RegistryError as e:
        raise HTTPException(404, str(e))

    def work(log):
        SUP.stop(mod_id)                      # a running server pins the old code
        return REG.update(mod_id, log=log).name
    return _threaded_sse(work)


async def _stop_server(mod_id: str):
    """Stopping waits for the process (up to a few seconds): keep that off the event loop."""
    await asyncio.get_running_loop().run_in_executor(None, lambda: SUP.stop(mod_id))


@app.delete("/api/modules/{mod_id}")
async def remove(mod_id: str, forget: bool = False):
    await _stop_server(mod_id)
    loop = asyncio.get_running_loop()
    try:
        if forget:
            await loop.run_in_executor(None, lambda: REG.remove_custom(mod_id))
        else:
            await loop.run_in_executor(None, lambda: REG.remove(mod_id))
    except RegistryError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"could not remove {mod_id}: {e}")
    return {"ok": True}


@app.post("/api/modules/{mod_id}/name")
async def rename_module(mod_id: str, request: Request):
    """Give a module your own display name (empty = back to the manifest's)."""
    try:
        REG.manifest(mod_id)
    except RegistryError as e:
        raise HTTPException(404, str(e))
    body = await request.json()
    return {"ok": True, "name": REG.rename(mod_id, body.get("name") or "")}


@app.post("/api/modules/{mod_id}/link")
async def link_module(mod_id: str, request: Request):
    """Point a module at a working copy on disk, so uncommitted changes can be tested."""
    body = await request.json()
    try:
        d = REG.link(mod_id, body.get("path") or "")
    except RegistryError as e:
        raise HTTPException(400, str(e))
    await _stop_server(mod_id)             # the old code is pinned by a running server
    return {"ok": True, "path": str(d)}


@app.delete("/api/modules/{mod_id}/link")
async def unlink_module(mod_id: str):
    await _stop_server(mod_id)
    REG.unlink(mod_id)
    return {"ok": True}


@app.post("/api/reload")
async def reload_catalog():
    """Re-read manifests without restarting. The edit-test loop for a manifest."""
    REG.reload_catalog()
    envs.invalidate()
    return {"ok": True, "modules": [m.id for m in REG.known()]}


@app.post("/api/modules/custom")
async def add_custom(request: Request):
    """Register a third-party module from a pasted manifest -- the 'add a new module' path."""
    try:
        raw = await request.json()
        m = REG.add_custom(raw)
    except ManifestError as e:
        raise HTTPException(400, f"manifest rejected: {e}")
    except RegistryError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(400, f"could not read that manifest: {e}")
    return {"ok": True, "id": m.id, "name": m.name}


@app.post("/api/modules/{mod_id}/optional/{group}")
async def install_optional(mod_id: str, group: str):
    try:
        m = REG.manifest(mod_id)
    except RegistryError as e:
        raise HTTPException(404, str(e))
    if group not in (m.optional or {}):
        raise HTTPException(404, f"{mod_id} has no optional component {group!r}")

    def work(log):
        envs.install_optional(m, group, log=log)
        return group
    return _threaded_sse(work)


@app.get("/api/modules/{mod_id}/optional")
async def optional_status(mod_id: str):
    try:
        m = REG.manifest(mod_id)
    except RegistryError as e:
        raise HTTPException(404, str(e))
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: {
        g: {"label": v.get("label", g), "present": envs.optional_present(m, g)}
        for g, v in (m.optional or {}).items()})


@app.post("/api/modules/{mod_id}/server/stop")
async def stop_server(mod_id: str):
    await _stop_server(mod_id)
    return {"ok": True}


@app.post("/api/modules/{mod_id}/server/start")
async def start_server(mod_id: str):
    try:
        return {"ok": True, "url": await SUP.ensure(mod_id)}
    except (RunError, RegistryError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/modules/{mod_id}/server")
async def server_status(mod_id: str):
    up = SUP.is_up(mod_id)
    return {"up": up, "url": SUP.base_url(mod_id) if up else ""}


# ---------------------------------------------------------------- LLM connection
@app.get("/api/connection")
async def get_connection():
    c = _CONNECTION or {}
    return {"set": bool(_CONNECTION), "base_url": c.get("base_url", ""), "model": c.get("model", ""),
            "reasoning": c.get("reasoning", "high"), "max_tokens": c.get("max_tokens", 32000),
            "provider": c.get("provider", ""), "has_key": bool(c.get("api_key")),
            "stored_at": str(config.CONNECTION_FILE)}


@app.post("/api/connection")
async def set_connection(request: Request):
    """Store the connection in memory and push it to every module server already running."""
    global _CONNECTION
    body = await request.json()
    model = (body.get("model") or "").strip()
    base = (body.get("base_url") or "").strip().rstrip("/")
    key = (body.get("api_key") or "").strip()
    if not key and _CONNECTION and _CONNECTION.get("api_key") and body.get("keep_key", True):
        key = _CONNECTION["api_key"]          # editing the model must not force retyping the key
    if model != "MOCK":
        if not base.startswith(("http://", "https://")):
            raise HTTPException(400, "base URL must start with http:// or https://")
        if not key:
            raise HTTPException(400, "API key required (or use model MOCK for an offline smoke test)")
        if not model:
            raise HTTPException(400, "model required")
    try:
        max_tokens = max(256, min(int(body.get("max_tokens") or 32000), 200000))
    except Exception:
        max_tokens = 32000
    _CONNECTION = {"base_url": base, "api_key": key, "model": model,
                   "reasoning": (body.get("reasoning") or "high").strip(),
                   "max_tokens": max_tokens, "provider": (body.get("provider") or "").strip()}
    _store_connection(_CONNECTION)
    pushed, failed = [], []
    for m in REG.known():
        if m.credentials and SUP.is_up(m.id):
            (pushed if await SUP.push_connection(m.id) else failed).append(m.id)
    return {"ok": True, "pushed": pushed, "failed": failed}


@app.delete("/api/connection")
async def clear_connection():
    global _CONNECTION
    _CONNECTION = None
    _store_connection(None)
    return {"ok": True}


# ---------------------------------------------------------------- runs
@app.post("/api/run/{mod_id}/{tab_id}")
async def run(mod_id: str, tab_id: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "expected a JSON body")
    job = jobs.clean_name(body.get("job") or "Project")
    fields = body.get("fields") or {}
    if not isinstance(fields, dict):
        raise HTTPException(400, "fields must be an object")
    try:
        m = REG.manifest(mod_id)
    except RegistryError as e:
        raise HTTPException(404, str(e))
    tab = next((t for t in m.tabs if t.id == tab_id), None)
    if not tab or not tab.run or tab.run.kind == "none":
        raise HTTPException(400, f"{mod_id}/{tab_id} has nothing to run")
    # A tab may carry more than one button (manifest `actions`); the body names which was pressed.
    # Swapping it into a copy of the tab keeps every path below -- fields, needs, optional
    # components, staging, the runners -- reading `tab.run` exactly as before.
    act = str(body.get("action") or "").strip()
    if act:
        chosen = next((a for a in tab.actions if a.id == act), None)
        if chosen is None:
            raise HTTPException(400, f"{mod_id}/{tab_id} has no action {act!r}")
        # ...and only the fields that action declared: the tab's fields belong to the main run, and
        # handing them to a different program is how `snl revise` got --site-class and exited 2.
        keep = tab.fields if chosen.fields == ["*"] else [f for f in tab.fields if f.id in chosen.fields]
        tab = dataclasses.replace(tab, run=chosen.run, fields=keep)
    # A module that declares `needs` gets those modules' checkout paths as {need.<id>} -- the
    # nonlinear module points its engine variable at {need.steltic}/steel_engine. Without the
    # dependency installed that expands to a path that does not exist, and the run fails deep
    # inside the DDM step rather than here. Refuse up front.
    missing = [n for n in m.needs if not REG.is_installed(n)]
    if missing:
        names = [REG.name(n) for n in missing]
        return _sse_error(f"{REG.name(mod_id)} needs {' and '.join(names)} installed first — it runs against that "
                          f"module's engine. Install it from the Modules tab, then run this again.")
    if not REG.is_installed(mod_id):
        return _sse_error(f"{REG.name(mod_id)} is not installed. Open the Modules tab and install it.")
    # A tab may need one of the module's optional components (a large ML stack such as the PDF converter's
    # Docling). Refuse here with the install path rather than letting the run die on an ImportError.
    for g in (tab.requires_optional or []):
        if not envs.optional_present(m, g):
            label = ((m.optional or {}).get(g) or {}).get("label", g)
            return _sse_error(f"{tab.title} needs the optional component \"{label}\", which is not installed in "
                              f"{REG.name(mod_id)}'s environment. Open the Modules tab, find {REG.name(mod_id)} and press "
                              f"\"Install {label}\" (a large download), then run this again.")
    for f in tab.fields:
        v = fields.get(f.id)
        if f.required and f.type != "project" and (v is None or v == "" or v == []) and f.default in (None, ""):
            return _sse_error(f"{f.label} is required")
    if (m.credentials and tab.run.kind == "http" and not _CONNECTION) or (tab.run.llm and not _CONNECTION):
        return _sse_error("Set your LLM connection first (the Connection button in the title bar).")
    # A run may declare files another step must have put in the project first (manifest
    # `run.requires`). The page greys the button while one is missing; this is the same check for
    # anything that drives runs without the page -- Admin's batch, a script, a stale tab.
    for q in (tab.run.requires or []):
        try:
            present = jobs.resolve_in_job(job, q["path"]).exists()
        except (PermissionError, OSError):
            present = False
        if not present:
            return _sse_error((q.get("missing") or "").strip()
                              or f"{tab.title} needs {q['path']} in the project first.")

    run_id = uuid.uuid4().hex[:12]
    RUNS.began(run_id, m.id, tab.id)       # so /api/state can say WHICH module is busy, not just that one is
    jobs.job_dir(job)                      # a real run is what creates the project folder
    if tab.run.kind == "cli":
        gen = run_cli(m, tab, job, fields, REG, RUNS, run_id, supervisor=SUP)
    else:
        gen = run_http(m, tab, job, fields, REG, SUP, RUNS, run_id)
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={**_SSE_HEADERS, "X-Run-Id": run_id})


@app.post("/api/cancel/{run_id}")
async def cancel(run_id: str):
    return {"ok": await RUNS.cancel(run_id)}


# ---------------------------------------------------------------- jobs
@app.get("/api/jobs")
async def list_jobs_api():
    return {"jobs": jobs.list_jobs()}


@app.post("/api/jobs/{job}")
async def job_create(job: str):
    """Create an (empty) project folder so it shows up everywhere immediately."""
    name = jobs.clean_name(job)
    jobs.job_dir(name)
    return {"ok": True, "name": name}


@app.get("/api/jobs/{job}/tree")
async def job_tree(job: str):
    return {"job": jobs.clean_name(job), "entries": jobs.tree(job),
            "history": jobs.history(job)}


@app.delete("/api/jobs/{job}")
async def job_delete(job: str):
    jobs.delete_job(job)
    return {"ok": True}


@app.post("/api/jobs/{job}/upload")
async def job_upload(job: str, files: list[UploadFile] = File(...)):
    """Files the user drops into an input tab land in the job folder, so a manifest can
    reference them as {f.<field>} paths without the hub knowing what they are."""
    d = jobs.job_dir(job)
    saved = []
    for f in files:
        name = jobs.clean_filename(f.filename or "upload")
        target = d / name
        size = 0
        with open(target, "wb") as out:              # no size cap: this is the user's own disk
            while True:
                chunk = await f.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                out.write(chunk)
        saved.append({"name": name, "bytes": size, "path": name})
    return {"ok": True, "files": saved}


@app.get("/job/{job}/{path:path}")
async def job_file(job: str, path: str = ""):
    """Serve anything inside a job folder. This is what makes the viewer bundle work over
    http:// instead of file:// -- same relative sibling paths, so the probe logic is unchanged."""
    try:
        target = jobs.resolve_in_job(job, path)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    if target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        raise HTTPException(404, f"{path} not found in job {job}")
    mt, _ = mimetypes.guess_type(target.name)
    return FileResponse(str(target), media_type=mt or "application/octet-stream")


def _output_root(mod_id: str, job: str) -> pathlib.Path:
    m = REG.manifest(mod_id)
    ctx = build_ctx(m, job, {}, REG)
    return pathlib.Path(expand(m.output_root, ctx)).resolve()


@app.get("/api/out/{mod_id}/{job}")
async def out_listing(mod_id: str, job: str):
    """What this module has actually produced for this project -- drives the viewer strip
    and the files tab without the hub knowing what any of the files mean."""
    try:
        root = _output_root(mod_id, job)
    except RegistryError as e:
        raise HTTPException(404, str(e))
    if not root.exists():
        return {"root_exists": False, "entries": []}

    def scan():
        entries = []
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root).as_posix()
            if p.is_dir() or rel.startswith(".git/") or "/.git/" in rel:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            entries.append({"path": rel, "bytes": st.st_size, "mtime": st.st_mtime})
            if len(entries) >= 4000:
                break
        return entries
    entries = await asyncio.get_running_loop().run_in_executor(None, scan)
    return {"root_exists": True, "entries": entries}


@app.get("/out/{mod_id}/{job}/{path:path}")
async def out_file(mod_id: str, job: str, path: str = ""):
    """Serve a module's output over http:// with sibling paths intact.

    The viewer bundle probes for sibling files (viewer_3d.html, pushover/..., nlrha/...)
    and iframes whichever exist. Because this endpoint preserves those relative paths, the exact
    same file works unchanged inside the app -- the bundle keeps being the framing, it just gains
    tabs around it.
    """
    try:
        root = _output_root(mod_id, job)
    except RegistryError as e:
        raise HTTPException(404, str(e))
    target = (root / (path or "")).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(403, "path escapes the output folder")
    if target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        raise HTTPException(404, f"{path} not found")
    mt, _ = mimetypes.guess_type(target.name)
    return FileResponse(str(target), media_type=mt or "application/octet-stream")


# ---------------------------------------------------------------- module proxy
@app.api_route("/m/{mod_id}/{path:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy(mod_id: str, path: str, request: Request):
    """Reverse-proxy a module server's API so the shell can call it same-origin (a select that
    loads example briefs, a download link). Starts the server on demand.

    Whole module UIs are NOT framed through here: their pages reference /static and /api by
    absolute path, which would resolve against the hub. Embed tabs open the module's own
    origin instead (see /api/modules/{id}/server/start).
    """
    try:
        base = await SUP.ensure(mod_id)
    except (RunError, RegistryError) as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    url = f"{base}/{path}"
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length", "accept-encoding", "connection")}
    body = await request.body()
    cx = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=20.0))
    req = cx.build_request(request.method, url, headers=headers, content=body,
                           params=request.query_params)
    try:
        resp = await cx.send(req, stream=True)
    except Exception as e:
        await cx.aclose()
        return JSONResponse({"error": f"module server unreachable: {e}"}, status_code=502)

    async def body_iter():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await cx.aclose()

    drop = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    out = {k: v for k, v in resp.headers.items() if k.lower() not in drop}
    out["Cache-Control"] = "no-cache"
    return StreamingResponse(body_iter(), status_code=resp.status_code, headers=out,
                             media_type=resp.headers.get("content-type"))


@app.get("/healthz")
async def healthz():
    return {"ok": True, "modules": len(REG.known()), **hub_info()}


@app.post("/api/hub/shutdown")
async def hub_shutdown():
    """Stop this hub gracefully (module servers and runs included). The launcher calls it when the
    running hub is older than the code on disk, then starts a fresh one."""
    if not _request_exit():
        raise HTTPException(409, "not running under steltic-hub (no server handle to stop)")
    return {"ok": True, "pid": os.getpid()}


@app.post("/api/hub/restart")
async def hub_restart():
    """Replace this hub with a fresh process on the same port: spawn the successor (it waits for the
    port), then stop. The window polls /healthz until a different pid answers and reloads."""
    if getattr(app.state, "server", None) is None:
        raise HTTPException(409, "not running under steltic-hub (no server handle to stop)")
    new_pid = _spawn_replacement()
    threading.Timer(0.3, _request_exit).start()          # let this response leave first
    return {"ok": True, "pid": os.getpid(), "successor": new_pid}


# ---------------------------------------------------------------- UI
app.mount("/static", StaticFiles(directory=str(config.UI_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(config.UI_DIR / "index.html"))
