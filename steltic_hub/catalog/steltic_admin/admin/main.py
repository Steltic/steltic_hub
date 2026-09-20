"""Admin -- the module server.

Started by the hub (uvicorn admin.main:app) with HUB_URL (where the hub answers), HUB_DATA (the hub's
data folder: module checkouts live under modules/, the Query file manager workspace under grokbot/),
HUB_JOBS (the projects folder), HUB_CATALOG (the hub's bundled manifests and modules) and ADMIN_DATA
(where plans, logs and the help cache go). The hub frames the UI at /#batch, /#standards and /#help
and pushes the user's LLM connection to /api/creds.

Three jobs:
  batch      plain-words instruction -> plan -> one hub run after another, watched and logged (plans.py);
             a step that stops, times out or pauses is continued from what its module saved
  standards  a folder of specification PDFs -> a conversion queue through the Query file manager (standards.py)
  help       questions about how Steltic works, answered from the code and docs with citations (help.py)
"""
from __future__ import annotations
import json, os, pathlib, time
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, grammar, help as helpdesk, llm, plans, standards
from .hub import HubClient, HubError

HERE = pathlib.Path(__file__).resolve().parent
UI = HERE / "ui"
HUB_URL = (os.environ.get("HUB_URL") or "http://127.0.0.1:8300").rstrip("/")
HUB_DATA = pathlib.Path(os.environ.get("HUB_DATA") or (pathlib.Path.cwd() / "hub_data")).resolve()
HUB_JOBS = pathlib.Path(os.environ.get("HUB_JOBS") or (HUB_DATA / "jobs")).resolve()
HUB_CATALOG = pathlib.Path(os.environ.get("HUB_CATALOG") or "").resolve() if os.environ.get("HUB_CATALOG") else None
ADMIN_DATA = pathlib.Path(os.environ.get("ADMIN_DATA") or (HUB_DATA / "admin")).resolve()
ADMIN_DATA.mkdir(parents=True, exist_ok=True)

hub = HubClient(HUB_URL)
store = plans.Store(ADMIN_DATA)
executor = plans.Executor(hub, store, HUB_JOBS)
github = helpdesk.GitHub(ADMIN_DATA / "github")

app = FastAPI(title="Steltic Admin")
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


# ---------------------------------------------------------------- housekeeping
@app.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__}


@app.post("/api/creds")
async def creds(request: Request):
    llm.set_creds(await request.json())
    return {"ok": True, "llm": llm.available()}


@app.get("/api/me")
def me():
    info = None
    try:
        info = hub.healthz()
    except HubError:
        pass
    return {"version": __version__, "hub_url": HUB_URL, "hub": info, "hub_reachable": bool(info),
            "llm": llm.available(), "model": (llm.creds() or {}).get("model"),
            "data": str(ADMIN_DATA), "jobs": str(HUB_JOBS), "hub_data": str(HUB_DATA),
            "running": executor.running, "aliases": grammar.describe_aliases()}


def _state() -> dict:
    try:
        return hub.state()
    except HubError as e:
        raise HTTPException(503, str(e))


@app.get("/api/hub/state")
def hub_state():
    """What can be run: modules with their runnable tabs and fields, projects, and what is missing."""
    st = _state()
    mods = []
    for m in st.get("modules") or []:
        tabs = []
        for t in m.get("tabs") or []:
            if not t.get("run"):
                continue
            tabs.append({"id": t["id"], "title": t["title"], "kind": t["run"].get("kind"), "label": t["run"].get("label"),
                         "missing_optional": t.get("missing_optional") or [],
                         "fields": [{"id": f["id"], "type": f["type"], "label": f["label"], "required": f.get("required", False),
                                     "has_default": f.get("has_default", False), "default": f.get("default"),
                                     "options": [o.get("value") for o in (f.get("options") or [])][:60],
                                     "fills": bool(f.get("fills"))} for f in t.get("fields") or []]})
        mods.append({"id": m["id"], "name": m["name"], "installed": bool((m.get("status") or {}).get("env_ready")),
                     "missing_needs": m.get("missing_needs") or [], "wants_credentials": m.get("wants_credentials"),
                     "server_up": m.get("server_up"), "tabs": tabs, "git": m.get("git"), "branch": m.get("branch"),
                     "linked": (m.get("status") or {}).get("linked") or ""})
    return {"modules": mods, "jobs": [j["name"] for j in st.get("jobs") or []], "connection": bool(st.get("connection")),
            "running": st.get("running") or {}, "hub": st.get("hub") or {}}


# ---------------------------------------------------------------- plans
@app.post("/api/plan/parse")
async def plan_parse(request: Request):
    body = await request.json()
    text = str(body.get("text") or "")
    r = grammar.parse(text)
    plan = plans.new_plan(_title(text), r["steps"], source=text)
    errors, warnings = executor.validate(plan) if r["steps"] else (["nothing to run -- name a project and a module, e.g. J1 to hr then nl"], [])
    return {"plan": plan, "warnings": r["warnings"] + warnings, "errors": errors}


PLAN_SYSTEM = """You turn a structural engineer's instruction into a batch plan for the Steltic hub. Reply with JSON only:
{"steps": [{"project": "<name>", "module": "<module id>", "tab": "<tab id>", "fields": {...}}], "notes": ["..."]}
Rules: steps run in the order given, one at a time. Use only the modules and tabs listed, and only their fields.
Project names are letters, digits, _ and -. A design tab's "brief" field may be "@project" (the brief.md in the
project folder), "@example:<id>" (an example brief such as ex22) or "@file:<name>" (a file in the project folder),
or the literal brief text if the user wrote it. Do not invent field values the user did not give; leave a field out
to use its default. If the instruction is ambiguous say so in notes and still give your best plan."""


@app.post("/api/plan/parse-llm")
async def plan_parse_llm(request: Request):
    body = await request.json()
    text = str(body.get("text") or "")
    if not llm.available():
        raise HTTPException(400, "no LLM connection (or model MOCK) -- use Make plan, which needs no model")
    st = hub_state()
    listing = json.dumps([{"id": m["id"], "name": m["name"], "installed": m["installed"],
                           "tabs": [{"id": t["id"], "title": t["title"], "fields": [{k: f[k] for k in ("id", "type", "label", "required") } for f in t["fields"]]}
                                    for t in m["tabs"]]} for m in st["modules"]], indent=0)
    aliases = "\n".join(f"{r['aliases']} -> {r['module']}/{r['tab']}" for r in grammar.describe_aliases())
    user = f"MODULES AND TABS:\n{listing}\n\nWORDS PEOPLE USE:\n{aliases}\n\nPROJECTS THAT EXIST: {', '.join(st['jobs']) or '(none)'}\n\nINSTRUCTION:\n{text}"
    try:
        d = llm.chat_json(PLAN_SYSTEM, user, max_tokens=4000)
    except Exception as e:
        raise HTTPException(502, f"the model did not return a plan: {e}")
    steps = [s for s in (d.get("steps") or []) if isinstance(s, dict)]
    plan = plans.new_plan(_title(text), steps, source=text)
    errors, warnings = executor.validate(plan)
    return {"plan": plan, "warnings": [str(n) for n in (d.get("notes") or [])] + warnings, "errors": errors}


@app.post("/api/plan/validate")
async def plan_validate(request: Request):
    body = await request.json()
    plan = _plan_from_body(body.get("plan") or body)
    errors, warnings = executor.validate(plan)
    return {"errors": errors, "warnings": warnings, "plan": plan}


@app.get("/api/plans")
def plans_list():
    return {"plans": store.list(), "running": executor.running}


@app.post("/api/plans")
async def plans_create(request: Request):
    body = await request.json()
    plan = _plan_from_body(body.get("plan") or body)
    store.save(plan)
    return {"ok": True, "id": plan["id"], "plan": plan}


@app.get("/api/plans/{plan_id}")
def plans_get(plan_id: str):
    plan = _load(plan_id)
    plan["running"] = executor.running == plan_id
    plan["current_run"] = executor.current_run(plan_id)
    return plan


@app.put("/api/plans/{plan_id}")
async def plans_update(plan_id: str, request: Request):
    old = _load(plan_id)
    if executor.running == plan_id:
        raise HTTPException(409, "the plan is running -- stop it before editing")
    body = await request.json()
    plan = _plan_from_body(body.get("plan") or body)
    plan["id"] = plan_id
    plan["created"] = old.get("created") or time.time()
    if plan.get("status") not in plans.PLAN_STATUS:
        plan["status"] = "draft"
    store.save(plan)
    return {"ok": True, "plan": plan}


@app.delete("/api/plans/{plan_id}")
def plans_delete(plan_id: str):
    _load(plan_id)
    if executor.running == plan_id:
        raise HTTPException(409, "the plan is running -- stop it first")
    store.delete(plan_id)
    return {"ok": True}


@app.post("/api/plans/{plan_id}/start")
def plans_start(plan_id: str):
    _load(plan_id)
    try:
        plan = executor.start(plan_id)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "plan": plan}


@app.post("/api/plans/{plan_id}/resume")
async def plans_resume(plan_id: str, request: Request):
    _load(plan_id)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    try:
        plan = executor.start(plan_id, retry_failed=bool(body.get("retry_failed")), fresh=bool(body.get("fresh")))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "plan": plan}


@app.post("/api/plans/{plan_id}/stop")
def plans_stop(plan_id: str):
    _load(plan_id)
    if executor.running != plan_id:
        raise HTTPException(409, "that plan is not running")
    executor.stop(plan_id)
    return {"ok": True}


@app.get("/api/plans/{plan_id}/log/{n}")
def plans_log(plan_id: str, n: int, tail: int = 300):
    _load(plan_id)
    return {"text": store.log_tail(plan_id, n, max(20, min(int(tail), 5000)))}


def _load(plan_id: str) -> dict:
    try:
        plan = store.load(plan_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not plan:
        raise HTTPException(404, "no such plan")
    return plan


def _plan_from_body(d: dict) -> dict:
    if not isinstance(d, dict):
        raise HTTPException(400, "expected a plan object")
    steps = d.get("steps")
    if not isinstance(steps, list):
        raise HTTPException(400, "a plan needs a steps list")
    plan = plans.new_plan(str(d.get("title") or "Plan"), steps, source=str(d.get("source") or ""), options=d.get("options"))
    if d.get("id"):
        plan["id"] = str(d["id"])
    if d.get("status") in plans.PLAN_STATUS:
        plan["status"] = d["status"]
    plan["note"] = str(d.get("note") or "")
    return plan


def _title(text: str) -> str:
    t = " ".join((text or "").split())
    return (t[:70] + "…") if len(t) > 70 else (t or "Plan")


# ---------------------------------------------------------------- standards
def _grokbot_root() -> pathlib.Path:
    return HUB_DATA / "grokbot"


@app.get("/api/standards/scan")
def standards_scan(folder: str = ""):
    f = pathlib.Path(folder).expanduser() if folder else _grokbot_root() / "documents" / "standards"
    d = standards.scan(f, _grokbot_root())
    d["default_folder"] = str(_grokbot_root() / "documents" / "standards")
    try:
        st = hub_state()
        qfm = next((m for m in st["modules"] if m["id"] == "steltic_grokbot"), None)
        conv = next((t for t in (qfm or {}).get("tabs") or [] if t["id"] == "convert"), None)
        d["qfm_installed"] = bool(qfm and qfm["installed"])
        d["converter_missing"] = list((conv or {}).get("missing_optional") or []) if conv else ["converter"]
    except HTTPException as e:
        d["qfm_installed"] = False; d["converter_missing"] = []; d["hub_error"] = str(e.detail)
    return d


@app.post("/api/standards/plan")
async def standards_plan(request: Request):
    body = await request.json()
    items = [it for it in (body.get("items") or []) if isinstance(it, dict) and it.get("pdf")]
    if not items:
        raise HTTPException(400, "no PDFs selected")
    for it in items:
        if it.get("stem") and it["stem"] not in standards.KNOWN:
            raise HTTPException(400, f"{it['stem']!r} is not a canonical stem")
    folder = str(body.get("folder") or "")
    steps = standards.build_steps(items, folder, project=plans.clean_name(str(body.get("project") or "Standards")),
                                  rebuild_index=bool(body.get("rebuild_index", True)), audit=bool(body.get("audit", True)),
                                  chunk_pages=int(body.get("chunk_pages") or 5))
    plan = plans.new_plan(f"Convert {len(items)} specification PDF{'s' if len(items) != 1 else ''}", steps,
                          source=f"standards folder {folder}")
    errors, warnings = executor.validate(plan)
    store.save(plan)
    return {"ok": True, "id": plan["id"], "plan": plan, "errors": errors, "warnings": warnings}


# ---------------------------------------------------------------- help
_corpus = helpdesk.Corpus()


def _local_roots(st: dict | None) -> list[tuple[pathlib.Path, str, bool]]:
    roots: list[tuple[pathlib.Path, str, bool]] = []
    src = ((st or {}).get("hub") or {}).get("source") or os.environ.get("HUB_SOURCE")
    if src and pathlib.Path(src).is_dir():
        roots.append((pathlib.Path(src), "hub", True))
    elif HUB_CATALOG and HUB_CATALOG.is_dir():
        roots.append((HUB_CATALOG, "hub/catalog", True))
    for m in (st or {}).get("modules") or []:
        linked = m.get("linked")
        co = pathlib.Path(linked) if linked else HUB_DATA / "modules" / m["id"]
        if co.is_dir():
            roots.append((co, m["id"], True))
    return roots


@app.post("/api/help")
async def help_ask(request: Request):
    body = await request.json()
    q = str(body.get("question") or "").strip()
    scope = str(body.get("scope") or "local")            # local | github | both
    if not q:
        raise HTTPException(400, "ask something")
    st = None
    try:
        st = hub_state()
    except HTTPException:
        pass
    sources = []
    if scope in ("local", "both"):
        for root, label, code in _local_roots(st):
            n = _corpus.add_tree(root, label, code=code)
            sources.append({"source": label, "path": str(root), "files": n})
    if scope in ("github", "both"):
        repos = {("Steltic", "steltic_hub", "main")}
        for m in (st or {}).get("modules") or []:
            pr = helpdesk.GitHub.parse_repo(m.get("git") or "")
            if pr:
                repos.add((pr[0], pr[1], m.get("branch") or "main"))
        for owner, repo, branch in sorted(repos):
            n = github.pull_into(_corpus, owner, repo, branch, q)
            sources.append({"source": f"github:{owner}/{repo}@{branch}", "files": n})
    excerpts = _corpus.search(q, k=int(body.get("k") or 10))
    ans = helpdesk.answer(q, excerpts)
    rec = {"t": time.time(), "question": q, "scope": scope, "model": ans.get("model"), "answer": ans["answer"],
           "excerpts": [{k: e[k] for k in ("label", "path", "lines", "score")} for e in excerpts]}
    try:
        with open(ADMIN_DATA / "help_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass
    return {"answer": ans["answer"], "model": ans.get("model"), "excerpts": excerpts, "sources": sources}


@app.get("/api/help/history")
def help_history(limit: int = 30):
    p = ADMIN_DATA / "help_log.jsonl"
    if not p.is_file():
        return {"items": []}
    rows = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return {"items": list(reversed(rows))}


# ---------------------------------------------------------------- UI
@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(str(UI / "index.html"))
