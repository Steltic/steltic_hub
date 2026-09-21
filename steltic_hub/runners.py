"""Starting module servers and executing runs.

Two run kinds, both streamed to the browser as SSE:

  cli   spawn `<module venv python> <command>` in the job folder and stream stdout.
        Used by the nonlinear module (python -m snl run ...) and the document converter.
  http  call the module's own FastAPI server and relay its event stream.
        Used by the design agents, whose agent loop already streams events.

Every analysis runs in its own OS process. That is not just tidiness: openseespy is a process
singleton, so in-process orchestration would be wrong even if the hub imported module code --
which it never does.
"""
from __future__ import annotations
import asyncio, codecs, json, os, re, shlex, shutil, signal, subprocess, sys, time, pathlib, zipfile
import httpx
from . import config, envs, exitcodes, jobs, sac
from .manifest import Manifest, Tab, Run


class RunError(RuntimeError):
    pass


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


# ---------------------------------------------------------------- templating
_TEMPLATE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.-]*)\}")


def expand(value, ctx: dict):
    """Substitute {job}, {job_dir}, {port}, {module_dir}, {data_dir}, {need.<id>}, {out.<id>} and
    {f.<field>} in one pass. Unknown placeholders are left exactly as written.

    Values are substituted, never shell-evaluated -- commands are lists and go to Popen without
    a shell, so a field containing `; rm -rf` is an argument, not a command. One pass means a
    field VALUE that happens to contain braces is never expanded a second time.
    """
    if isinstance(value, str):
        return _TEMPLATE.sub(lambda mt: _render(ctx[mt.group(1)]) if mt.group(1) in ctx else mt.group(0), value)
    if isinstance(value, list):
        return [expand(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: expand(v, ctx) for k, v in value.items()}
    return value


def _render(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return os.pathsep.join(_render(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v)
    return str(v)


def _empty(v) -> bool:
    return v is None or v is False or (isinstance(v, str) and v.strip() == "") \
        or (isinstance(v, (list, dict)) and not v)


def base_ctx(m: Manifest, job: str, registry, port: int | None = None) -> dict:
    ctx = {
        "job": jobs.clean_name(job),
        "job_dir": str(jobs.job_path(job)),      # resolved, not created -- see jobs.job_path
        "jobs_dir": str(config.JOBS_DIR),
        "module_dir": str(registry.module_root(m.id)),
        "checkout_dir": str(registry.checkout(m.id)),
        "data_dir": str(config.DATA),
        "catalog_dir": str(config.CATALOG_DIR),   # assets shipped next to the bundled manifests
        "hub_url": config.hub_url(),              # for a module server that drives other modules through the hub
        "port": port or "",
    }
    for need in m.needs:
        ctx[f"need.{need}"] = str(registry.module_root(need))
        # {python.<need>}: that module's own interpreter -- for a module that runs jobs inside a
        # needed module's environment (openseespy and friends) instead of installing them twice
        if envs.env_ready(need):
            ctx[f"python.{need}"] = str(envs.python_bin(need))
    # {out.<id>}: where module <id> keeps its outputs for THIS project. This is how one module's
    # inputs can default to another module's results without either knowing the other exists.
    for other in registry.known():
        try:
            ctx[f"out.{other.id}"] = str(pathlib.Path(expand(other.output_root, ctx)))
        except Exception:
            pass
    return ctx


def field_values(tab: Tab | None, job: str, fields: dict, ctx: dict) -> dict:
    """Resolve what the browser sent into the values a run actually uses.

    * empty values fall back to the field's default; defaults may use templates
    * file / files values are names inside the project folder (that is where uploads land) and
      become absolute paths, so a command running elsewhere (cwd = a module workspace) still
      finds them; a default that already expanded to an absolute path is kept as is
    * attachments stay structured ([{name,type,data_url}]) for http bodies
    """
    out: dict = {}
    fields = fields or {}
    for f in (tab.fields if tab else []):
        raw = fields.get(f.id)
        if _empty(raw):
            val = expand(f.default, ctx) if isinstance(f.default, str) else f.default
            if f.type == "checkbox":
                val = bool(val) if val not in (None, "") else False
        else:
            val = raw
        if f.type in ("file", "files") and not _empty(val):
            names = val if isinstance(val, list) else ([val] if f.type == "file" else str(val).split(os.pathsep))
            paths = []
            for n in names:
                n = str(n).strip()
                if not n:
                    continue
                p = pathlib.Path(n)
                if p.is_absolute():
                    paths.append(str(p))
                else:
                    try:
                        paths.append(str(jobs.resolve_in_job(job, n)))
                    except PermissionError:
                        raise RunError(f"{f.label}: {n!r} is not inside the project folder")
            val = paths[0] if f.type == "file" and paths else (paths if f.type == "files" else "")
        if f.type == "number" and isinstance(val, str) and val.strip():
            try:
                val = float(val) if "." in val or "e" in val.lower() else int(val)
            except ValueError:
                raise RunError(f"{f.label}: {val!r} is not a number")
        out[f.id] = val
    # anything the browser sent that the manifest does not declare is passed through untouched
    for k, v in fields.items():
        out.setdefault(k, v)
    return out


def build_ctx(m: Manifest, job: str, fields: dict, registry, port: int | None = None,
              tab: Tab | None = None) -> dict:
    ctx = base_ctx(m, job, registry, port=port)
    vals = field_values(tab, job, fields, ctx)
    for k, v in vals.items():
        ctx[f"f.{k}"] = "" if v is None else v
    ctx["_fields"] = vals
    return ctx


def cli_args(tab: Tab, fields: dict, ctx: dict) -> list[str]:
    """Turn declared fields into CLI flags, for fields that asked to be flags."""
    extra: list[str] = []
    vals = ctx.get("_fields") or field_values(tab, ctx.get("job", ""), fields, ctx)
    for f in tab.fields:
        if not f.arg:
            continue
        val = vals.get(f.id)
        if _empty(val):
            continue
        if f.arg_style == "flag-if-true":
            if val is True or str(val).lower() in ("true", "1", "yes", "on"):
                extra.append(f.arg)
        elif f.arg_style == "positional":
            extra.append(_render(val))
        else:
            if isinstance(val, list):
                extra.append(f.arg)
                extra.extend(_render(v) for v in val)
            else:
                extra.extend([f.arg, _render(val)])
    return extra


# ---------------------------------------------------------------- staging inputs
def _extract_zip(src: pathlib.Path, dst: pathlib.Path, log):
    def _safe(n: str) -> bool:
        return bool(n) and not n.startswith("/") and ".." not in n.split("/") and ":" not in n.split("/")[0]

    with zipfile.ZipFile(src) as z:
        pairs = [(i, i.filename.replace("\\", "/")) for i in z.infolist() if not i.is_dir()]
        pairs = [(i, n) for i, n in pairs if _safe(n)]                  # traversal entries never count
        names = [n for _, n in pairs]
        tops = {n.split("/", 1)[0] for n in names}
        # a zip that wraps everything in one top folder is flattened, so its files sit in the
        # project root (the layout the viewers and the other modules probe for)
        wrap = len(tops) == 1 and all("/" in n for n in names)
        base = dst.resolve()
        n_written = 0
        for info, name in pairs:
            rel = name.split("/", 1)[1] if wrap else name
            if not rel:
                continue
            target = (base / rel).resolve()
            if base != target and base not in target.parents:
                continue                                                  # zip-slip guard
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as f, open(target, "wb") as g:
                shutil.copyfileobj(f, g)
            n_written += 1
    log(f"[hub] unpacked {src.name}: {n_written} files into {dst}")


def stage_inputs(run: Run, ctx: dict, log) -> None:
    """Copy or unpack declared inputs into place before a run (see Run.stage)."""
    for s in run.stage or []:
        src = expand(s.get("from", ""), ctx).strip()
        dst = expand(s.get("to", ""), ctx).strip()
        label = s.get("label") or "input"
        if not src or not dst:
            if s.get("required"):
                raise RunError(s.get("missing") or f"{label}: nothing to use -- choose a file first")
            continue
        srcp, dstp = pathlib.Path(src), pathlib.Path(dst)
        if not srcp.exists():
            if s.get("required"):
                raise RunError(s.get("missing") or f"{label} not found: {src}")
            log(f"[hub] {label}: {src} not present -- skipped")
            continue
        dstp.mkdir(parents=True, exist_ok=True)
        try:
            rs, rd = srcp.resolve(), dstp.resolve()
        except OSError:
            rs, rd = srcp, dstp
        if srcp.is_dir():
            if rs == rd or rd in rs.parents:
                log(f"[hub] {label}: {src} is already the project folder")
                continue
            n = 0
            for p in rs.rglob("*"):
                if p.is_dir():
                    continue
                rel = p.relative_to(rs)
                t = rd / rel
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, t)
                n += 1
            log(f"[hub] {label}: copied {n} files from {src}")
        elif zipfile.is_zipfile(srcp):
            _extract_zip(srcp, dstp, log)
        else:
            if rs.parent == rd:
                continue                                                  # already inside the project
            shutil.copy2(rs, rd / rs.name)
            log(f"[hub] {label}: copied {rs.name}")


# ---------------------------------------------------------------- server supervision
class ServerSupervisor:
    """Starts a module's own web app on demand and keeps it alive for embed tabs and http runs."""

    def __init__(self, registry, connection=None):
        self.registry = registry
        self.procs: dict[str, subprocess.Popen] = {}
        self.ports: dict[str, int] = {}
        self.started: dict[str, float] = {}
        self.deps: dict[str, dict] = {}      # mod_id -> {required id: base url | None} at start time
        self._locks: dict[str, asyncio.Lock] = {}
        # callable -> the one in-memory LLM connection, or None. Held by the hub, pushed to each
        # module server after it comes up, so a key is typed once and never touches disk.
        self.connection = connection or (lambda: None)

    def _lock(self, mod_id: str) -> asyncio.Lock:
        lk = self._locks.get(mod_id)
        if lk is None:
            lk = self._locks[mod_id] = asyncio.Lock()
        return lk

    # ---- ports: one origin per module, for as long as the data folder lives
    #
    # Every module page references its assets by the same absolute paths (/static/app.js,
    # /static/styles.css, /api/...). The browser caches those per ORIGIN, and the origin is
    # 127.0.0.1:<port>. Hand a port that once served Design variations to Admin and the Admin page
    # loads Variations' cached app.js -- "The variations module could not load: Cannot set
    # properties of null" inside the Admin tab, and the Feedback tab blank for the same reason.
    # So a port, once given to a module, stays that module's: remembered in PORTS_FILE across hub
    # restarts, never reassigned to another module while it is on record.
    def _saved_ports(self) -> dict:
        try:
            d = json.loads(config.PORTS_FILE.read_text(encoding="utf-8"))
            return {str(k): int(v) for k, v in d.items() if isinstance(v, int)}
        except Exception:
            return {}

    def _save_ports(self, saved: dict):
        try:
            config.PORTS_FILE.write_text(json.dumps(saved, indent=1, sort_keys=True), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _port_free(p: int) -> bool:
        import socket
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return True
            except OSError:
                return False

    def _alloc_port(self, mod_id: str) -> int:
        saved = self._saved_ports()
        live = {p for mid, p in self.ports.items() if mid != mod_id and self.is_up(mid)}
        window = range(config.PORT_BASE, config.PORT_BASE + config.PORT_SPAN)
        mine = saved.get(mod_id)
        if mine in window and mine not in live and self._port_free(mine):
            self.ports[mod_id] = mine
            return mine
        owned = {p for mid, p in saved.items() if mid != mod_id}
        # first a port no module has ever had, then (only if the window is exhausted) one whose
        # owner is not running -- that owner gets a fresh port of its own next time
        for spare_owned in (False, True):
            for p in window:
                if p in live or (p in owned and not spare_owned) or not self._port_free(p):
                    continue
                saved[mod_id] = p
                for mid, q in list(saved.items()):
                    if q == p and mid != mod_id:
                        del saved[mid]
                self._save_ports(saved)
                self.ports[mod_id] = p
                return p
        raise RunError("no free port for a module server")

    def is_up(self, mod_id: str) -> bool:
        pr = self.procs.get(mod_id)
        return bool(pr and pr.poll() is None)

    def base_url(self, mod_id: str) -> str:
        return f"http://127.0.0.1:{self.ports[mod_id]}"

    def requires(self, mod_id: str) -> list[str]:
        """Other modules whose servers this module's server talks to (manifest `server.requires`)."""
        try:
            m = self.registry.manifest(mod_id)
        except Exception:
            return []
        return [r for r in (m.server.get("requires") or []) if r != mod_id]

    async def _ensure_required(self, mod_id: str, seen: frozenset) -> dict:
        """Start the servers this one depends on (those that are installed) and return their URLs.
        A dependency that is not installed maps to None: the module then starts without it."""
        out: dict = {}
        for req in self.requires(mod_id):
            if req in seen:
                continue
            try:
                if self.registry.is_installed(req) and self.registry.manifest(req).has_server:
                    out[req] = await self._ensure(req, seen | {mod_id})
                else:
                    out[req] = None
            except RunError:
                out[req] = None
        return out

    async def ensure(self, mod_id: str) -> str:
        """Idempotent start + health wait. Returns the module's base URL."""
        return await self._ensure(mod_id, frozenset())

    async def _ensure(self, mod_id: str, seen: frozenset) -> str:
        m = self.registry.manifest(mod_id)
        if not m.has_server:
            raise RunError(f"module {mod_id!r} has no server")
        deps = await self._ensure_required(mod_id, seen)
        async with self._lock(mod_id):
            if self.is_up(mod_id):
                # A dependency that was missing when this server started (so it runs without it)
                # and is available now: restart so the new run gets it.
                was = self.deps.get(mod_id, {})
                newly = [r for r, url in deps.items() if url and was.get(r) != url]
                if not newly:
                    return self.base_url(mod_id)
                self._log_line(mod_id, f"[hub] {', '.join(newly)} available now -- restarting {self.registry.name(mod_id)} to use it")
                self.stop(mod_id)
            if not envs.env_ready(mod_id):
                raise RunError(f"{self.registry.name(mod_id)} is not installed yet -- install it from the Modules tab")
            missing = [n for n in m.needs if not self.registry.is_installed(n)]
            if missing:
                raise RunError(f"{self.registry.name(mod_id)} needs {', '.join(self.registry.name(n) for n in missing)} installed first")
            port = self._alloc_port(mod_id)
            ctx = base_ctx(m, "__server__", self.registry, port=port)
            for req, url in deps.items():
                if url:
                    ctx[f"server.{req}"] = url
            cmd = [str(envs.python_bin(mod_id)), *expand(m.server["command"], ctx)]
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            env.pop("PYTHONHOME", None)
            env["PYTHONUNBUFFERED"] = "1"
            skipped = []
            for k, v in list(expand(m.env_vars, ctx).items()) + list(expand(m.server.get("env", {}), ctx).items()):
                v = str(v)
                if "{server." in v or "{python." in v:
                    skipped.append(k)            # points at a server / interpreter that is not installed: leave it unset
                    continue
                env[k] = v
            cwd = expand(m.server.get("cwd", "{module_dir}"), ctx)
            if not pathlib.Path(cwd).is_dir():
                raise RunError(f"{self.registry.name(mod_id)}: working folder {cwd} does not exist -- reinstall the module")
            self.deps[mod_id] = deps
            logp = config.LOGS_DIR / f"{mod_id}.server.log"
            with open(logp, "ab", buffering=0) as logf:
                logf.write(f"\n=== start {time.ctime()} :: {' '.join(cmd)} ===\n".encode())
                for req, url in deps.items():
                    logf.write((f"[hub] uses {req} at {url}\n" if url else
                                f"[hub] {req} is not installed -- starting without it\n").encode())
                if skipped:
                    logf.write(f"[hub] left unset: {', '.join(skipped)}\n".encode())
                try:
                    if sys.platform == "win32":
                        pr = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=logf, stderr=subprocess.STDOUT,
                                              stdin=subprocess.DEVNULL,
                                              creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | _NO_WINDOW)
                    else:
                        pr = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=logf, stderr=subprocess.STDOUT,
                                              stdin=subprocess.DEVNULL, start_new_session=True)
                except OSError as e:
                    raise RunError(f"could not start {self.registry.name(mod_id)}: {e}")
            self.procs[mod_id] = pr
            self.started[mod_id] = time.time()
            await self._wait_healthy(mod_id, m)
            await self.push_connection(mod_id)
            return self.base_url(mod_id)

    async def push_connection(self, mod_id: str) -> bool:
        """Hand the module server the user's LLM connection, if it declared where to take it."""
        m = self.registry.manifest(mod_id)
        conn = self.connection()
        if not m.credentials or not conn or not self.is_up(mod_id):
            return False
        url = self.base_url(mod_id) + m.credentials.get("path", "/api/creds")
        try:
            async with httpx.AsyncClient(timeout=20.0) as cx:
                r = await cx.request(m.credentials.get("method", "POST"), url, json=conn)
                return r.status_code < 400
        except Exception:
            return False

    async def _wait_healthy(self, mod_id: str, m: Manifest):
        health = m.server.get("health", "/healthz")
        url = self.base_url(mod_id) + health
        deadline = time.time() + config.HEALTH_TIMEOUT
        async with httpx.AsyncClient(timeout=5.0) as cx:
            while time.time() < deadline:
                pr = self.procs.get(mod_id)
                if pr and pr.poll() is not None:
                    tail = self._log_tail(mod_id)
                    self.procs.pop(mod_id, None)
                    raise RunError(f"{self.registry.name(mod_id)} server exited immediately (code {pr.returncode}).\n{tail}")
                try:
                    r = await cx.get(url)
                    if r.status_code < 500:
                        return
                except Exception:
                    pass
                await asyncio.sleep(0.4)
        self.stop(mod_id)
        raise RunError(f"{self.registry.name(mod_id)} server did not become healthy within {config.HEALTH_TIMEOUT:.0f}s.\n"
                       f"{self._log_tail(mod_id)}")

    def _log_line(self, mod_id: str, text: str):
        try:
            with open(config.LOGS_DIR / f"{mod_id}.server.log", "ab", buffering=0) as f:
                f.write((text + "\n").encode())
        except Exception:
            pass

    def _log_tail(self, mod_id: str, n: int = 25) -> str:
        p = config.LOGS_DIR / f"{mod_id}.server.log"
        if not p.exists():
            return ""
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        return "\n".join(lines)

    def stop(self, mod_id: str):
        pr = self.procs.pop(mod_id, None)
        if not pr or pr.poll() is not None:
            return
        _terminate(pr)

    def stop_all(self):
        for mid in list(self.procs):
            self.stop(mid)


def _terminate(pr: subprocess.Popen, grace: float = 8.0):
    """Ask nicely (the process group gets the signal, so children go too), then kill the tree.

    Windows: CTRL_BREAK reaches the group only when the hub itself owns a console. Started
    windowless by the launcher (pythonw) it does not, so the fallback is `taskkill /T`, which
    takes the whole tree -- a bare kill() would orphan an OpenSees child mid-analysis.
    """
    if pr.poll() is not None:
        return
    if sys.platform == "win32":
        sent = False
        try:
            pr.send_signal(signal.CTRL_BREAK_EVENT)
            sent = True
        except Exception:
            pass
        if sent and grace > 0:
            try:
                pr.wait(timeout=grace)
                return
            except Exception:
                pass
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pr.pid)],
                           capture_output=True, creationflags=_NO_WINDOW, timeout=15)
        except Exception:
            pass
        try:
            pr.wait(timeout=5)
        except Exception:
            try:
                pr.kill()
            except Exception:
                pass
        return
    try:
        os.killpg(os.getpgid(pr.pid), signal.SIGTERM)
    except Exception:
        try:
            pr.terminate()
        except Exception:
            pass
    try:
        pr.wait(timeout=max(grace, 0.2))
    except Exception:
        try:
            os.killpg(os.getpgid(pr.pid), signal.SIGKILL)
        except Exception:
            try:
                pr.kill()
            except Exception:
                pass
        try:
            pr.wait(timeout=5)
        except Exception:
            pass


# ---------------------------------------------------------------- run execution
class JobRuns:
    """Live runs, keyed by run id, so the UI can cancel them.

    CLI runs hold the process; http runs hold a coroutine that asks the module server to stop.
    """

    def __init__(self):
        self.procs: dict[str, subprocess.Popen] = {}
        self.http: dict[str, dict] = {}
        self.cancelled: set[str] = set()
        # run id -> "<module>.<tab>". The run id is a uuid, so /api/state could only ever say THAT
        # SOMETHING was running, never what -- which is why the rail's "running..." was computed from
        # the browser's own run map and went dark on a reload, or for a run begun in another window.
        self.where: dict[str, str] = {}

    def began(self, run_id: str, mod_id: str, tab_id: str) -> None:
        self.where[run_id] = f"{mod_id}.{tab_id}"

    def ended(self, run_id: str) -> None:
        self.where.pop(run_id, None)

    def live_tabs(self) -> dict[str, bool]:
        """Every "<module>.<tab>" with a run in flight, in the key the UI already builds itself."""
        return {v: True for v in self.where.values()}

    async def cancel(self, run_id: str) -> bool:
        pr = self.procs.get(run_id)
        if pr is not None:
            # A retrying run sits between attempts with its dead process still registered. Stop then
            # has nothing to kill, but must still mark the run cancelled -- otherwise the wait would
            # end in a fresh attempt and the button would look broken.
            self.cancelled.add(run_id)
            if pr.poll() is None:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, lambda: _terminate(pr, grace=3.0))
            return True
        h = self.http.get(run_id)
        if h:
            self.cancelled.add(run_id)
            spec, url = h["spec"], h["base_url"]
            if spec:
                try:
                    async with httpx.AsyncClient(timeout=10.0) as cx:
                        await cx.request(spec.get("method", "POST"), url + spec.get("path", ""),
                                         json=spec.get("body") or {})
                except Exception:
                    pass
            if not spec:
                self._drop_stream(h)     # no stop endpoint: dropping the stream is the stop signal
            else:
                # The stop request went out. A server that honours it ends its stream within a
                # second or two and the hub's done event follows. One that answers ok and keeps
                # streaming (CFS Steel's /api/stop cleared its own cancel flag while releasing the
                # run's quota slot) would leave the button looking dead -- so after STOP_GRACE the
                # stream is dropped, which every module server already treats as a stop request.
                def _later():
                    if self.http.get(run_id) is h:
                        self._drop_stream(h)
                asyncio.get_running_loop().call_later(config.STOP_GRACE, _later)
            return True
        return False

    @staticmethod
    def _drop_stream(h: dict):
        """Close the hub's side of the module's stream. run_http sees the read fail, and because the
        run is marked cancelled it ends with a proper done/cancelled event (the browser shows
        "stopped", not "connection lost"); the module server sees a disconnect, which is its stop."""
        resp = h.get("resp")
        if resp is not None:
            asyncio.ensure_future(resp.aclose())
        else:
            task = h.get("task")          # the stream has not opened yet: cancel the request itself
            if task and not task.done():
                task.cancel()


def sse(ev: dict) -> str:
    return "data: " + json.dumps(ev) + "\n\n"


KEEPALIVE = ": ping\n\n"


def _run_env(m: Manifest, tab: Tab, ctx: dict, connection: dict | None = None) -> dict:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    for k, v in list(expand(m.env_vars, ctx).items()) + list(expand(tab.run.env, ctx).items()):
        v = str(v)
        if "{server." in v or "{python." in v:
            continue        # names a server / interpreter that is not installed: left unset, as for a server
        env[k] = v
    if tab.run.llm and connection:
        # The same connection the hub pushes to a module server's /api/creds, for a process instead:
        # the key goes into the child's environment and nowhere else.
        env.update(llm_env(connection))
    return env


def llm_env(connection: dict) -> dict:
    """The user's LLM connection as the STELTIC_LLM_* variables a `run.llm` CLI process reads."""
    c = connection or {}
    return {"STELTIC_LLM_BASE_URL": str(c.get("base_url") or ""), "STELTIC_LLM_API_KEY": str(c.get("api_key") or ""),
            "STELTIC_LLM_MODEL": str(c.get("model") or ""), "STELTIC_LLM_PROVIDER": str(c.get("provider") or ""),
            "STELTIC_LLM_REASONING": str(c.get("reasoning") or ""), "STELTIC_LLM_MAX_TOKENS": str(c.get("max_tokens") or "")}


_SERVER_REF = re.compile(r"\{server\.([A-Za-z0-9_-]+)\}")


def servers_referenced(m: Manifest, tab: Tab) -> list[str]:
    """Module ids whose server URL this CLI run's command or `run.env` asks for ({server.<id>}).

    Module-wide `env_vars` are not scanned on purpose: a server reference there is for the module's
    own server (the Nonlinear module's STELTIC_URL = {server.steltic} is for its Feedback server) and
    resolving it for every CLI run would start HR Steel for a pushover that never talks to it. For a
    CLI run it stays unset, as it does for a server whose dependency is not installed."""
    text = json.dumps([tab.run.env, tab.run.command, tab.run.cwd])
    out: list[str] = []
    for mid in _SERVER_REF.findall(text):
        if mid != m.id and mid not in out:
            out.append(mid)
    return out


# A CLI module may print structured events, one JSON object per line, in the vocabulary the design
# agents stream (token, reasoning, tool, tool_result, milestone, status, usage, warning, assistant,
# error, paused, artifact). The hub relays such a line as that event instead of a log line, so a
# process that talks to the model gets the same model-output and model-reasoning boxes, lights and
# usage strip as an agent server does. Anything else printed is a log line, as before.
CLI_EVENT_TYPES = frozenset({"token", "reasoning", "tool", "tool_result", "milestone", "status", "usage",
                             "warning", "assistant", "error", "paused", "artifact"})


def event_line(text: str) -> dict | None:
    s = text.strip()
    if len(s) < 12 or s[0] != "{" or s[-1] != "}" or '"type"' not in s:
        return None
    try:
        ev = json.loads(s)
    except ValueError:
        return None
    if not isinstance(ev, dict) or ev.get("type") not in CLI_EVENT_TYPES:
        return None
    return ev


def _found_artifacts(tab: Tab, root: pathlib.Path, ctx: dict) -> list:
    out = []
    for a in (tab.artifacts or []):
        rel = expand(a.get("path", ""), ctx)
        if rel and (root / rel).exists():
            out.append(a)
    return out


RETRY_DELAY = 5.0      # long enough to read the line and press Stop, short enough not to feel stuck


def _retry_wanted(spec: dict, rc: int | None) -> bool:
    """Does this exit code match the tab's `run.retry.on`?

    "native_crash" means any code exitcodes recognises -- the NTSTATUS table on Windows, signals on
    POSIX -- so a manifest does not have to carry that list, and an ordinary failure (exit 1, with a
    traceback the user can act on) never loops.
    """
    on = (spec or {}).get("on")
    if not on or rc is None:
        return False
    if on == "native_crash":
        return exitcodes.explain(rc) is not None
    return rc in on


async def _wait_between_attempts(runs: JobRuns, run_id: str, seconds: float) -> bool:
    """Pause before the next attempt; True if the run was cancelled while waiting. Stop has to take
    effect at once, so this polls instead of sleeping the whole delay in one piece."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if run_id in runs.cancelled:
            return True
        await asyncio.sleep(0.05)
    return run_id in runs.cancelled


async def run_cli(m: Manifest, tab: Tab, job: str, fields: dict, registry, runs: JobRuns, run_id: str,
                  supervisor: ServerSupervisor | None = None):
    """Spawn the module's CLI in the job folder and stream its output.

    A tab whose manifest declares `run.retry` may spawn its command more than once (see the attempt
    loop below). Every attempt stays inside this one SSE stream, so as far as the browser -- and the
    job history -- is concerned it is still one run, with one status, one log and one Stop button.
    """
    if not envs.env_ready(m.id):
        yield sse({"type": "error", "text": f"{registry.name(m.id)} is not installed. Open the Modules tab and install it."})
        yield sse({"type": "done", "ok": False, "rc": -1})
        return

    # {server.<id>} in the command or environment: that module's server is started first and its
    # URL substituted -- how a CLI run reaches the standards server without knowing its port. A
    # module that is not installed leaves the variable unset (see _run_env); one that is installed
    # but will not start is an error here, before anything is spawned.
    server_urls: dict = {}
    for mid in servers_referenced(m, tab) if supervisor is not None else []:
        try:
            if registry.is_installed(mid) and registry.manifest(mid).has_server:
                server_urls[f"server.{mid}"] = await supervisor.ensure(mid)
        except (RunError, Exception) as e:
            yield sse({"type": "error", "text": f"{registry.name(mid)} did not start: {e}"})
            yield sse({"type": "done", "ok": False, "rc": -1})
            return
    connection = supervisor.connection() if (supervisor is not None and tab.run.llm) else None

    def build(vals: dict):
        """Everything a spawn needs, from one set of field values. A retry that applies `then_set`
        rebuilds all of it, because an overridden field can reach the command, the cwd and the env."""
        ctx = build_ctx(m, job, vals, registry, tab=tab)
        ctx.update(server_urls)
        cmd = [str(envs.python_bin(m.id)), *expand(tab.run.command, ctx), *cli_args(tab, vals, ctx)]
        cwd = expand(tab.run.cwd or "{job_dir}", ctx)
        pathlib.Path(cwd).mkdir(parents=True, exist_ok=True)
        return ctx, cmd, cwd, _run_env(m, tab, ctx, connection)

    try:
        ctx, cmd, cwd, env = build(fields)
        out_root = pathlib.Path(expand(m.output_root, ctx))
    except RunError as e:
        yield sse({"type": "error", "text": str(e)})
        yield sse({"type": "done", "ok": False, "rc": -1})
        return

    yield sse({"type": "start", "run_id": run_id, "module": m.id, "tab": tab.id,
               "cmd": " ".join(shlex.quote(c) for c in cmd), "cwd": cwd})
    jobs.stamp(job, {"event": "start", "module": m.id, "tab": tab.id, "run_id": run_id})

    staged: list[str] = []
    try:
        stage_inputs(tab.run, ctx, staged.append)
    except RunError as e:
        for line in staged:
            yield sse({"type": "log", "text": line})
        yield sse({"type": "error", "text": str(e)})
        yield sse({"type": "done", "ok": False, "rc": -1, "job": jobs.clean_name(job)})
        jobs.stamp(job, {"event": "done", "module": m.id, "tab": tab.id, "rc": -1, "ok": False, "run_id": run_id})
        return
    for line in staged:
        yield sse({"type": "log", "text": line})

    retry = tab.run.retry or {}
    then_set = retry.get("then_set") or {}
    max_attempts = retry.get("max", 1) or 1
    attempt = 0
    prev_crash_line = None        # the last thing the previous attempt printed before it died
    loop = asyncio.get_running_loop()
    pr = None
    rc = None
    blocked = False
    cancelled = False
    try:
        while True:
            attempt += 1
            rc = None
            kw = dict(cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      stdin=subprocess.DEVNULL, text=True, bufsize=1, errors="replace", encoding="utf-8")
            try:
                if sys.platform == "win32":
                    pr = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | _NO_WINDOW, **kw)
                else:
                    pr = subprocess.Popen(cmd, start_new_session=True, **kw)
            except OSError as e:
                yield sse({"type": "error", "text": f"could not start the module: {e}"})
                yield sse({"type": "done", "ok": False, "rc": -1, "job": jobs.clean_name(job), "attempts": attempt})
                jobs.stamp(job, {"event": "done", "module": m.id, "tab": tab.id, "rc": -1, "ok": False, "run_id": run_id})
                return
            runs.procs[run_id] = pr

            last_line = ""
            while True:
                fut = loop.run_in_executor(None, pr.stdout.readline)
                while True:
                    try:
                        line = await asyncio.wait_for(asyncio.shield(fut), timeout=config.KEEPALIVE)
                        break
                    except asyncio.TimeoutError:
                        yield KEEPALIVE           # silence is not death: prove the stream is alive
                if not line:
                    break
                text = line.rstrip("\r\n")
                ev = event_line(text)
                yield sse(ev if ev is not None else {"type": "log", "text": text})
                if text.strip():
                    last_line = text          # where the crash happened, as far as the log can tell
                if not blocked and sac.looks_blocked(line):
                    blocked = True                # explained once, after the traceback, in the done event's wake
            rc = await loop.run_in_executor(None, pr.wait)
            try:
                pr.stdout.close()     # a retry rebinds `pr`, and this pipe would otherwise sit until the GC
            except Exception:
                pass
            cancelled = run_id in runs.cancelled
            if cancelled or attempt >= max_attempts or not _retry_wanted(retry, rc):
                break
            if last_line == prev_crash_line:
                # The same crash at the same point: the process state theory is wrong here, and a
                # third identical attempt would only spend the user's time. Stop and let the run's
                # crash_hint (below) ask for the change only a person can judge.
                yield sse({"type": "log", "text": "[hub] the crash has not moved (same last line) -- not retrying"})
                break
            prev_crash_line = last_line
            yield sse({"type": "log", "text": f"[hub] crash {exitcodes.code_name(rc)} at attempt {attempt} of "
                                              f"{max_attempts} -- retrying in {RETRY_DELAY:.0f} s (Stop cancels)"})
            yield sse({"type": "retry", "attempt": attempt + 1, "max": max_attempts, "rc": rc})
            if await _wait_between_attempts(runs, run_id, RETRY_DELAY):
                cancelled = True
                break
            if then_set and attempt + 1 == 3:
                # The first retry repeats the command unchanged, because a fresh process on its own
                # clears most native crashes. Only once that has failed too is the manifest's
                # work-around worth applying: it trades speed or quality for survival, so it is not
                # something to impose on the first sign of trouble.
                try:
                    ctx, cmd, cwd, env = build({**fields, **then_set})
                except RunError as e:
                    yield sse({"type": "error", "text": str(e)})
                    break
                yield sse({"type": "log", "text": "[hub] retrying with "
                                                  + ", ".join(f"{k}={v}" for k, v in then_set.items())})
            yield sse({"type": "start", "run_id": run_id, "module": m.id, "tab": tab.id,
                       "cmd": " ".join(shlex.quote(c) for c in cmd), "cwd": cwd})

        if blocked:
            yield sse({"type": "error", "text": sac.hint()})
        elif not cancelled and exitcodes.explain(rc, tab.run.crash_hint):
            yield sse({"type": "error", "text": exitcodes.explain(rc, tab.run.crash_hint)})   # a native crash has no traceback
        arts = _found_artifacts(tab, out_root, ctx)
        yield sse({"type": "done", "rc": rc, "ok": rc == 0 and not cancelled, "cancelled": cancelled,
                   "artifacts": arts, "job": jobs.clean_name(job), "attempts": attempt})
        jobs.stamp(job, {"event": "done", "module": m.id, "tab": tab.id, "rc": rc,
                         "ok": rc == 0 and not cancelled, "cancelled": cancelled, "run_id": run_id})
    finally:
        runs.procs.pop(run_id, None)
        runs.ended(run_id)
        runs.cancelled.discard(run_id)
        if pr is not None and pr.poll() is None:
            _terminate(pr, grace=2.0)
            if rc is None:
                jobs.stamp(job, {"event": "done", "module": m.id, "tab": tab.id, "rc": -1,
                                 "ok": False, "cancelled": True, "run_id": run_id})


class _SSEParser:
    """Incremental server-sent-events reader: bytes in, event dicts (or None for comments) out.
    Decodes UTF-8 across chunk boundaries, which a per-chunk decode silently corrupts."""

    def __init__(self):
        self.dec = codecs.getincrementaldecoder("utf-8")("replace")
        self.buf = ""

    def feed(self, chunk: bytes):
        self.buf += self.dec.decode(chunk)
        while True:
            i = self.buf.find("\n\n")
            if i < 0:
                return
            block, self.buf = self.buf[:i], self.buf[i + 2:]
            data = [ln[5:].strip() for ln in block.split("\n") if ln.startswith("data:")]
            if not data:
                yield None                    # a comment / keepalive
                continue
            try:
                yield json.loads("\n".join(data))
            except Exception:
                yield {"type": "log", "text": "\n".join(data)}


async def run_http(m: Manifest, tab: Tab, job: str, fields: dict, registry,
                   supervisor: ServerSupervisor, runs: JobRuns, run_id: str):
    """Drive the module's own HTTP API and relay its stream.

    The module's events are parsed and re-emitted one by one (never a raw byte relay), so a
    multibyte character split across chunks survives, oversized payloads meant for the module's
    own browser UI are dropped, and the run's outcome can be recorded in the project history.
    """
    try:
        base_url = await supervisor.ensure(m.id)
    except RunError as e:
        yield sse({"type": "error", "text": str(e)})
        yield sse({"type": "done", "ok": False})
        return
    if m.credentials and supervisor.connection():
        # idempotent: the server may have been (re)started before the key was typed
        await supervisor.push_connection(m.id)
    try:
        ctx = build_ctx(m, job, fields, registry, port=supervisor.ports.get(m.id), tab=tab)
        stage_inputs(tab.run, ctx, lambda *_: None)
    except RunError as e:
        yield sse({"type": "error", "text": str(e)})
        yield sse({"type": "done", "ok": False})
        return
    body = expand(tab.run.body or {}, ctx)
    for k, v in ctx["_fields"].items():      # fields not named in body are passed through by id
        body.setdefault(k, v)
    url = base_url + expand(tab.run.path, ctx)
    cancel_spec = expand(tab.run.cancel, ctx) if tab.run.cancel else None
    handle = {"spec": cancel_spec, "base_url": base_url, "task": asyncio.current_task(), "resp": None}
    runs.http[run_id] = handle
    yield sse({"type": "start", "run_id": run_id, "module": m.id, "tab": tab.id, "cmd": f"{tab.run.method} {url}"})
    jobs.stamp(job, {"event": "start", "module": m.id, "tab": tab.id, "run_id": run_id})
    outcome = {"ok": None, "seen": False}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=20.0)) as cx:
            async with cx.stream(tab.run.method, url, json=body) as resp:
                handle["resp"] = resp            # so Stop can drop the stream (JobRuns._drop_stream)
                if resp.status_code >= 400:
                    txt = (await resp.aread()).decode("utf-8", "replace")[:2000]
                    try:
                        txt = json.loads(txt).get("detail") or txt
                    except Exception:
                        pass
                    yield sse({"type": "error", "text": f"{registry.name(m.id)} refused the run ({resp.status_code}): {txt}"})
                    outcome["ok"] = False
                elif tab.run.stream == "sse":
                    parser = _SSEParser()
                    async for chunk in resp.aiter_bytes():
                        for ev in parser.feed(chunk):
                            if ev is None:
                                yield KEEPALIVE
                                continue
                            t = ev.get("type")
                            if t == "bundle":
                                continue      # a base64 zip for the module's own browser UI; the hub reads files directly
                            if t == "done":
                                outcome["ok"] = True
                            elif t in ("error", "paused"):
                                outcome["ok"] = False
                            outcome["seen"] = True
                            yield sse(ev)
                else:
                    async for line in resp.aiter_lines():
                        if line.strip():
                            yield sse({"type": "log", "text": line})
                    outcome["ok"] = True
    except asyncio.CancelledError:
        raise
    except Exception as e:
        if run_id not in runs.cancelled:      # a stream the hub dropped on purpose is not a failure
            yield sse({"type": "error", "text": f"{type(e).__name__}: {e}"})
        outcome["ok"] = False
    finally:
        runs.http.pop(run_id, None)
        runs.ended(run_id)
        cancelled = run_id in runs.cancelled
        runs.cancelled.discard(run_id)
        ok = outcome["ok"] if outcome["ok"] is not None else (outcome["seen"] and not cancelled)
        jobs.stamp(job, {"event": "done", "module": m.id, "tab": tab.id, "ok": bool(ok),
                         "cancelled": cancelled, "run_id": run_id})
    out_root = pathlib.Path(expand(m.output_root, ctx))
    yield sse({"type": "done", "ok": bool(ok), "cancelled": cancelled, "job": jobs.clean_name(job),
               "artifacts": _found_artifacts(tab, out_root, ctx), "end": True})
