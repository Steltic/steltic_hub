"""Plans: what to run, in what order, and what happened.

A plan is a list of steps, each one exactly what a click on a module tab would send the hub:
(module, tab, project, fields). Steps run one after another in ONE worker thread -- the modules
are heavy (OpenSees, Docling) and the design servers hold one conversation per building, so nothing
here runs two steps at once. The thread holds each run's event stream open until the hub says it is
done, writes the events to a log file, and records the outcome in the plan.

Everything lives under <ADMIN_DATA>: plans/<id>.json (the plan and its progress, rewritten after
every change) and logs/<id>/<n>.log (one per step). A restart of Admin's server finds a plan that
was running, marks it `interrupted`, and waits for the user to press Resume -- it never re-launches
a run on its own, because the run it was watching may still be going on the module server.

Field values may say where to get the value rather than what it is:
    @project            the project's brief.md / brief.txt (design steps default to this)
    @file:<name>        a file in the project folder -- its text for a text field, its path otherwise
    @example:<id>       one of the design servers' example briefs, through the tab's own `fills`
"""
from __future__ import annotations
import json, os, pathlib, re, threading, time, traceback, uuid
from .hub import HubClient, HubError

STEP_STATUS = ("pending", "running", "done", "failed", "skipped", "stopped")
PLAN_STATUS = ("draft", "running", "done", "failed", "stopped", "interrupted")
ON_FAIL = ("stop", "skip_project", "continue")
TEXT_FIELDS = ("text", "textarea")
BRIEF_NAMES = ("brief.md", "brief.txt", "brief.markdown", "BRIEF.md", "Brief.md")


def clean_name(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", (s or "").strip())[:80].strip("_-")
    return s or "Project"


def new_plan(title: str, steps: list[dict], source: str = "") -> dict:
    return {"id": time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4], "title": title or "Plan",
            "source": source, "created": time.time(), "updated": time.time(), "status": "draft",
            "note": "", "steps": [normalise_step(s, i + 1) for i, s in enumerate(steps)]}


def normalise_step(s: dict, n: int) -> dict:
    fields = s.get("fields") or {}
    if not isinstance(fields, dict):
        fields = {}
    tab = str(s.get("tab") or "")
    on_fail = s.get("on_fail") or ("continue" if tab == "convert" else "stop")
    if on_fail not in ON_FAIL:
        on_fail = "stop"
    return {"n": n, "project": clean_name(str(s.get("project") or "")), "module": str(s.get("module") or ""),
            "tab": tab, "fields": fields, "on_fail": on_fail, "label": str(s.get("label") or ""),
            "status": s.get("status") if s.get("status") in STEP_STATUS else "pending",
            "run_id": s.get("run_id"), "started": s.get("started"), "ended": s.get("ended"),
            "ok": s.get("ok"), "rc": s.get("rc"), "attempts": s.get("attempts") or 0,
            "note": str(s.get("note") or ""), "artifacts": s.get("artifacts") or []}


class Store:
    def __init__(self, root: pathlib.Path):
        self.root = root
        self.plans = root / "plans"
        self.logs = root / "logs"
        self.plans.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def path(self, plan_id: str) -> pathlib.Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", plan_id or ""):
            raise ValueError(f"bad plan id {plan_id!r}")
        return self.plans / f"{plan_id}.json"

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.plans.glob("*.json"), reverse=True):
            try:
                d = json.loads(_read(p))
                out.append(summary(d))
            except Exception:
                continue
        return out

    def load(self, plan_id: str) -> dict | None:
        p = self.path(plan_id)
        if not p.is_file():
            return None
        try:
            d = json.loads(_read(p))
        except Exception:
            return None
        d["steps"] = [normalise_step(s, i + 1) for i, s in enumerate(d.get("steps") or [])]
        return d

    def save(self, plan: dict):
        with self._lock:
            plan["updated"] = time.time()
            p = self.path(plan["id"])
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(plan, indent=1), encoding="utf-8")
            _replace(tmp, p)

    def delete(self, plan_id: str):
        p = self.path(plan_id)
        p.unlink(missing_ok=True)
        d = self.logs / plan_id
        if d.is_dir():
            for f in d.glob("*"):
                try:
                    f.unlink()
                except OSError:
                    pass
            try:
                d.rmdir()
            except OSError:
                pass

    def log_path(self, plan_id: str, n: int) -> pathlib.Path:
        d = self.logs / plan_id
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{int(n):02d}.log"

    def log_tail(self, plan_id: str, n: int, lines: int = 300) -> str:
        p = self.log_path(plan_id, n)
        if not p.is_file():
            return ""
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(txt.splitlines()[-lines:])


def summary(d: dict) -> dict:
    steps = d.get("steps") or []
    counts = {k: 0 for k in STEP_STATUS}
    for s in steps:
        counts[s.get("status") or "pending"] = counts.get(s.get("status") or "pending", 0) + 1
    return {"id": d.get("id"), "title": d.get("title"), "status": d.get("status"), "created": d.get("created"),
            "updated": d.get("updated"), "steps": len(steps), "counts": counts, "note": d.get("note", ""),
            "projects": sorted({s.get("project") for s in steps if s.get("project")})}


# ---------------------------------------------------------------- the executor
class Executor:
    def __init__(self, hub: HubClient, store: Store, jobs_dir: pathlib.Path):
        self.hub = hub
        self.store = store
        self.jobs = jobs_dir
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running: str | None = None            # plan id
        self._stop_flag: set[str] = set()
        self._current_run: dict[str, str] = {}
        self._recover()

    def _recover(self):
        """A plan left `running` by a previous Admin process is not running any more (this process just
        started). Mark it so the user sees it and decides."""
        for s in self.store.list():
            if s.get("status") == "running":
                plan = self.store.load(s["id"])
                if not plan:
                    continue
                plan["status"] = "interrupted"
                plan["note"] = "Admin's server restarted while this plan was running. The step that was in progress may still be running on its module server, or may have been stopped with the hub. Check the project, then Resume."
                for st in plan["steps"]:
                    if st["status"] == "running":
                        st["status"] = "stopped"; st["ended"] = time.time(); st["note"] = "interrupted by an Admin restart"
                self.store.save(plan)

    @property
    def running(self) -> str | None:
        return self._running if self._thread and self._thread.is_alive() else None

    def current_run(self, plan_id: str) -> str | None:
        return self._current_run.get(plan_id)

    # ---------- validation (against what the hub says exists right now)
    def validate(self, plan: dict, state: dict | None = None) -> tuple[list[str], list[str]]:
        errors, warnings = [], []
        try:
            state = state or self.hub.state()
        except HubError as e:
            return [str(e)], []
        mods = {m["id"]: m for m in state.get("modules") or []}
        if not plan.get("steps"):
            errors.append("the plan has no steps")
        for st in plan.get("steps") or []:
            who = f"step {st['n']} ({st['project']} → {st['module']}/{st['tab']})"
            m = mods.get(st["module"])
            if not m:
                errors.append(f"{who}: no module {st['module']!r} in this hub"); continue
            if not st.get("project"):
                errors.append(f"{who}: no project"); continue
            status = m.get("status") or {}
            if not status.get("env_ready"):
                errors.append(f"{who}: {m['name']} is not installed (Modules page → Install)"); continue
            if m.get("missing_needs"):
                errors.append(f"{who}: {m['name']} needs {', '.join(m['missing_needs'])} installed first")
            tab = next((t for t in m.get("tabs") or [] if t["id"] == st["tab"]), None)
            if not tab:
                errors.append(f"{who}: {m['name']} has no tab {st['tab']!r} (tabs: {', '.join(t['id'] for t in m.get('tabs') or [])})"); continue
            if not tab.get("run"):
                errors.append(f"{who}: the {tab['title']} tab is not something the hub can run (kind {tab.get('kind')})"); continue
            if tab.get("missing_optional"):
                errors.append(f"{who}: {m['name']} is missing the optional component(s) {', '.join(tab['missing_optional'])} -- Modules page → Install")
            if m.get("wants_credentials") and tab["run"].get("kind") == "http" and not state.get("connection"):
                errors.append(f"{who}: {m['name']} needs the LLM connection (title bar → Connection)")
            for f in tab.get("fields") or []:
                v = st["fields"].get(f["id"])
                if f["type"] == "project":
                    continue
                if f.get("required") and not f.get("has_default") and _empty(v):
                    errors.append(f"{who}: {f['label']} is required")
                    continue
                if isinstance(v, str) and v.startswith("@"):
                    problem = self._check_source(st["project"], v, f)
                    if problem:
                        errors.append(f"{who}: {f['label']}: {problem}")
            unknown = [k for k in st["fields"] if k not in {f["id"] for f in tab.get("fields") or []}]
            if unknown:
                warnings.append(f"{who}: fields not declared by the tab are passed through untouched: {', '.join(unknown)}")
        return errors, warnings

    def _check_source(self, project: str, v: str, field: dict) -> str | None:
        if v == "@project":
            if not any((self.jobs / project / n).is_file() for n in BRIEF_NAMES):
                return f"no brief.md / brief.txt in the project folder ({self.jobs / project}) -- put the brief there, or write (ex22) / (brief.md) in the instruction, or type it into the plan"
            return None
        if v.startswith("@file:"):
            name = v[6:].strip()
            p = pathlib.Path(name)
            if not p.is_absolute():
                p = self.jobs / project / name
            if not p.is_file():
                return f"{p} does not exist"
            return None
        if v.startswith("@example:"):
            if not field.get("fills_target_of"):
                pass                                   # checked when the step runs (needs the module server)
            return None
        return f"unknown source {v!r} (use @project, @file:<name> or @example:<id>)"

    # ---------- resolving values when a step starts
    def resolve_fields(self, step: dict, tab: dict) -> dict:
        out = {}
        by_id = {f["id"]: f for f in tab.get("fields") or []}
        for k, v in (step.get("fields") or {}).items():
            f = by_id.get(k) or {"id": k, "type": "text"}
            if isinstance(v, str) and v.startswith("@"):
                out[k] = self._source_value(step, tab, f, v)
            else:
                out[k] = v
        return out

    def _source_value(self, step: dict, tab: dict, field: dict, v: str):
        project = step["project"]
        if v == "@project":
            for n in BRIEF_NAMES:
                p = self.jobs / project / n
                if p.is_file():
                    return p.read_text(encoding="utf-8", errors="replace")
            raise RuntimeError(f"no brief.md / brief.txt in {self.jobs / project}")
        if v.startswith("@file:"):
            name = v[6:].strip()
            p = pathlib.Path(name)
            if not p.is_absolute():
                p = self.jobs / project / name
            if not p.is_file():
                raise RuntimeError(f"{p} does not exist")
            if field.get("type") in TEXT_FIELDS:
                return p.read_text(encoding="utf-8", errors="replace")
            return str(p)                              # a file field: the hub takes an absolute path
        if v.startswith("@example:"):
            ex = v[9:].strip()
            filler = next((f for f in tab.get("fields") or [] if (f.get("fills") or {}).get("target") == field["id"]), None)
            if not filler:
                raise RuntimeError(f"the {tab['title']} tab has no example picker that fills {field['id']!r}")
            path = (filler["fills"].get("path") or "").replace("{value}", ex)
            d = self.hub.proxy_get(step["module"], path)
            key = filler["fills"].get("key")
            text = d.get(key) if key else d
            if text is None:
                raise RuntimeError(f"example {ex!r} not found on {step['module']}")
            return text if isinstance(text, str) else json.dumps(text)
        raise RuntimeError(f"unknown source {v!r}")

    # ---------- lifecycle
    def start(self, plan_id: str, retry_failed: bool = False) -> dict:
        with self._lock:
            if self.running:
                raise RuntimeError(f"plan {self.running} is running -- one plan at a time (stop it, or wait)")
            plan = self.store.load(plan_id)
            if not plan:
                raise RuntimeError("no such plan")
            errors, _ = self.validate(plan)
            if errors:
                raise RuntimeError("the plan cannot start:\n- " + "\n- ".join(errors))
            for st in plan["steps"]:
                if st["status"] in ("stopped", "skipped") or (retry_failed and st["status"] == "failed"):
                    st.update(status="pending", note="", run_id=None, started=None, ended=None, ok=None, rc=None)
                elif st["status"] == "running":
                    st.update(status="pending", note="")
            if not any(st["status"] == "pending" for st in plan["steps"]):
                raise RuntimeError("nothing left to run (every step is done or failed -- Resume with retry to run failed steps again)")
            plan["status"] = "running"; plan["note"] = ""
            self.store.save(plan)
            self._stop_flag.discard(plan_id)
            self._running = plan_id
            self._thread = threading.Thread(target=self._run, args=(plan_id,), daemon=True, name=f"plan-{plan_id}")
            self._thread.start()
            return plan

    def stop(self, plan_id: str) -> bool:
        self._stop_flag.add(plan_id)
        rid = self._current_run.get(plan_id)
        if rid:
            self.hub.cancel(rid)
        return True

    def _run(self, plan_id: str):
        try:
            self._run_steps(plan_id)
        except Exception:
            plan = self.store.load(plan_id)
            if plan:
                plan["status"] = "failed"
                plan["note"] = "Admin's worker crashed:\n" + traceback.format_exc()[-2000:]
                self.store.save(plan)
        finally:
            self._current_run.pop(plan_id, None)
            self._stop_flag.discard(plan_id)

    def _run_steps(self, plan_id: str):
        plan = self.store.load(plan_id)
        state = self.hub.state()
        mods = {m["id"]: m for m in state.get("modules") or []}
        skip_projects: set[str] = set()
        final = "done"
        for st in plan["steps"]:
            if plan_id in self._stop_flag:
                final = "stopped"; break
            if st["status"] != "pending":
                continue
            if st["project"] in skip_projects:
                st.update(status="skipped", note="an earlier step of this project failed"); self.store.save(plan); continue
            m = mods.get(st["module"]) or {}
            tab = next((t for t in m.get("tabs") or [] if t["id"] == st["tab"]), {})
            log = self.store.log_path(plan_id, st["n"])
            st.update(status="running", started=time.time(), ended=None, note="", run_id=None, ok=None, rc=None)
            self.store.save(plan)
            with open(log, "a", encoding="utf-8", errors="replace") as lf:
                writer = _LogWriter(lf)
                writer.line(f"=== {time.ctime()} :: {st['project']} -> {m.get('name', st['module'])} / {tab.get('title', st['tab'])} ===")
                try:
                    project = self.hub.create_project(st["project"])
                    fields = self.resolve_fields(st, tab)
                    for k, v in fields.items():
                        shown = (v if isinstance(v, str) else json.dumps(v))
                        writer.line(f"[admin] {k} = {shown[:160]!r}{' …' if isinstance(shown, str) and len(shown) > 160 else ''}")
                    outcome = self.hub.run(st["module"], st["tab"], project, fields,
                                           on_event=lambda ev: self._on_event(plan, st, ev, writer),
                                           should_stop=lambda: plan_id in self._stop_flag)
                except HubError as e:
                    outcome = {"ok": False, "rc": None, "cancelled": False, "errors": [str(e)], "artifacts": [], "attempts": 1, "run_id": None}
                    writer.line(f"✖ {e}")
                except Exception as e:
                    outcome = {"ok": False, "rc": None, "cancelled": False, "errors": [f"{type(e).__name__}: {e}"], "artifacts": [], "attempts": 1, "run_id": None}
                    writer.line(f"✖ {type(e).__name__}: {e}")
                writer.flush_tokens()
                self._current_run.pop(plan_id, None)
                st.update(ended=time.time(), ok=bool(outcome.get("ok")), rc=outcome.get("rc"),
                          attempts=outcome.get("attempts") or 1, artifacts=outcome.get("artifacts") or [],
                          run_id=outcome.get("run_id") or st.get("run_id"))
                if outcome.get("cancelled"):
                    st["status"] = "stopped"; st["note"] = "stopped"
                    writer.line("■ stopped")
                elif outcome.get("ok"):
                    st["status"] = "done"
                    writer.line("✓ done")
                else:
                    st["status"] = "failed"
                    st["note"] = "; ".join(outcome.get("errors") or []) or (f"exit {outcome.get('rc')}" if outcome.get("rc") is not None else "failed")
                    writer.line(f"✖ failed: {st['note']}")
            self.store.save(plan)
            if st["status"] == "stopped":
                final = "stopped"; break
            if st["status"] == "failed":
                if st["on_fail"] == "stop":
                    final = "failed"; break
                if st["on_fail"] == "skip_project":
                    skip_projects.add(st["project"])
                final = "failed" if final != "stopped" else final
        else:
            if plan_id in self._stop_flag:
                final = "stopped"
        if final == "done" and any(s["status"] == "failed" for s in plan["steps"]):
            final = "failed"
        plan["status"] = final
        if final == "stopped":
            plan["note"] = "stopped by the user; Resume runs the remaining steps"
        elif final == "failed":
            bad = [f"step {s['n']} ({s['project']} → {s['module']}/{s['tab']}): {s['note']}" for s in plan["steps"] if s["status"] == "failed"]
            plan["note"] = "\n".join(bad)
        self.store.save(plan)

    def _on_event(self, plan: dict, st: dict, ev: dict, writer: "_LogWriter"):
        t = ev.get("type")
        if t == "start":
            if ev.get("run_id"):
                st["run_id"] = ev["run_id"]
                self._current_run[plan["id"]] = ev["run_id"]
                self.store.save(plan)
            writer.line("$ " + str(ev.get("cmd") or ""))
        elif t == "token":
            writer.token(ev.get("text") or "")
        elif t == "reasoning":
            pass                                               # the model's scratchpad; the hub window shows it
        elif t == "usage":
            pass
        elif t == "log":
            writer.line(str(ev.get("text") if ev.get("text") is not None else ""))
        elif t == "status":
            writer.line("· " + str(ev.get("text") or ""))
        elif t == "milestone":
            writer.line("▸ " + str(ev.get("text") or ""))
        elif t == "tool":
            writer.line(f"▶ {'step ' + str(ev['step']) + ' · ' if ev.get('step') is not None else ''}{ev.get('title') or ev.get('name') or 'tool'}")
        elif t == "tool_result":
            writer.line("↳ " + str(ev.get("summary") or "") + (f"  ({ev['ms']} ms)" if ev.get("ms") else ""))
        elif t == "assistant":
            writer.line(str(ev.get("text") or ""))
        elif t == "warning":
            writer.line("⚠ " + str(ev.get("text") or ""))
        elif t == "error":
            writer.line("✖ " + str(ev.get("text") or "error"))
        elif t == "retry":
            writer.line(f"[hub] retrying: attempt {ev.get('attempt')} of {ev.get('max')}")
            st["attempts"] = int(ev.get("attempt") or st.get("attempts") or 1)
            self.store.save(plan)
        elif t == "paused":
            writer.line("⏸ paused -- " + str(ev.get("reason") or "") + (" " + str(ev["detail"]) if ev.get("detail") else ""))
        elif t == "done":
            if not (ev.get("end") or "rc" in ev or "artifacts" in ev):
                writer.line("✓ the module reports the design complete")
        elif ev.get("text"):
            writer.line(str(ev["text"]))


class _LogWriter:
    """Streamed model text (one `token` event per word) goes on one line; everything else is a line."""

    def __init__(self, f):
        self.f = f
        self.tok: list[str] = []

    def token(self, s: str):
        self.tok.append(s)
        if len(self.tok) > 400:
            self.flush_tokens(newline=False)

    def flush_tokens(self, newline: bool = True):
        if self.tok:
            self.f.write("".join(self.tok))
            self.tok = []
            if newline:
                self.f.write("\n")
            self.f.flush()

    def line(self, s: str):
        self.flush_tokens()
        self.f.write(time.strftime("[%H:%M:%S] ") + s.rstrip("\n") + "\n")
        self.f.flush()


def _empty(v) -> bool:
    return v is None or v is False or (isinstance(v, str) and v.strip() == "") or (isinstance(v, (list, dict)) and not v)


def _read(p: pathlib.Path, tries: int = 20) -> str:
    for i in range(tries):
        try:
            return p.read_text(encoding="utf-8")
        except PermissionError:                        # Windows: the writer is replacing it this instant
            if i == tries - 1:
                raise
            time.sleep(0.01)
    return ""


def _replace(tmp: pathlib.Path, dst: pathlib.Path, tries: int = 60):
    for i in range(tries):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.01)
