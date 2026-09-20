"""The hub, as Admin sees it.

Admin never imports hub code and never touches a module's environment. Everything it does goes
through the hub's own HTTP API -- the same routes the hub's window uses -- so a run started by Admin
is exactly a run started by a click: same manifest, same staging, same log, same history line in the
project folder. The hub kills a run when the client that started it goes away, so a run's SSE stream
is held open here, in Admin's own process, from the first event to the `done` event.

Synchronous on purpose: the plan executor is a thread, and the handlers that call these are plain
`def` routes (FastAPI runs them in its thread pool).
"""
from __future__ import annotations
import json, mimetypes, pathlib, time
from typing import Callable, Iterator
import httpx


class HubError(RuntimeError):
    pass


def parse_sse(lines: Iterator[str]) -> Iterator[dict | None]:
    """Server-sent events as the hub emits them: `data: {...}` blocks separated by a blank line;
    a `: ping` comment (the keepalive) comes out as None."""
    buf: list[str] = []
    for line in lines:
        line = line.rstrip("\r")
        if line == "":
            if not buf:
                continue
            data = [ln[5:].strip() for ln in buf if ln.startswith("data:")]
            buf = []
            if not data:
                yield None
                continue
            try:
                yield json.loads("\n".join(data))
            except Exception:
                yield {"type": "log", "text": "\n".join(data)}
            continue
        buf.append(line)
    if buf:
        data = [ln[5:].strip() for ln in buf if ln.startswith("data:")]
        if data:
            try:
                yield json.loads("\n".join(data))
            except Exception:
                yield {"type": "log", "text": "\n".join(data)}


class HubClient:
    def __init__(self, base_url: str):
        self.base = (base_url or "http://127.0.0.1:8300").rstrip("/")

    # ---------------- reads
    def _get(self, path: str, timeout: float = 30.0) -> dict:
        try:
            r = httpx.get(self.base + path, timeout=timeout)
        except Exception as e:
            raise HubError(f"the hub at {self.base} did not answer: {e}")
        if r.status_code >= 400:
            raise HubError(f"{path}: {r.status_code} {_detail(r)}")
        return r.json()

    def _post(self, path: str, body=None, timeout: float = 60.0) -> dict:
        try:
            r = httpx.post(self.base + path, json=body, timeout=timeout)
        except Exception as e:
            raise HubError(f"the hub at {self.base} did not answer: {e}")
        if r.status_code >= 400:
            raise HubError(f"{path}: {r.status_code} {_detail(r)}")
        return r.json()

    def healthz(self) -> dict:
        return self._get("/healthz", timeout=5.0)

    def reachable(self) -> bool:
        try:
            return bool(self.healthz().get("ok"))
        except Exception:
            return False

    def state(self) -> dict:
        """/api/state: every module with its tabs, fields, install status and unmet needs; every project."""
        return self._get("/api/state", timeout=120.0)

    def outputs(self, module: str, project: str) -> list[dict]:
        try:
            return self._get(f"/api/out/{module}/{project}", timeout=60.0).get("entries") or []
        except HubError:
            return []

    def proxy_get(self, module: str, path: str) -> dict:
        """GET a module server's own API through the hub (starts that server on demand)."""
        return self._get(f"/m/{module}{path}", timeout=180.0)

    # ---------------- writes
    def create_project(self, name: str) -> str:
        return self._post(f"/api/jobs/{name}")["name"]

    def cancel(self, run_id: str) -> bool:
        try:
            return bool(self._post(f"/api/cancel/{run_id}", timeout=30.0).get("ok"))
        except HubError:
            return False

    def upload(self, project: str, path: pathlib.Path) -> str:
        """Copy a file into the project folder the way the window's Choose file… does; returns its name there."""
        mt, _ = mimetypes.guess_type(path.name)
        with open(path, "rb") as f:
            r = httpx.post(f"{self.base}/api/jobs/{project}/upload",
                           files=[("files", (path.name, f, mt or "application/octet-stream"))], timeout=600.0)
        if r.status_code >= 400:
            raise HubError(f"upload of {path.name} failed: {r.status_code} {_detail(r)}")
        files = r.json().get("files") or []
        if not files:
            raise HubError(f"upload of {path.name}: the hub saved nothing")
        return files[0]["path"]

    def run(self, module: str, tab: str, project: str, fields: dict,
            on_event: Callable[[dict], None], should_stop: Callable[[], bool] | None = None) -> dict:
        """Start a run and stay on its stream until it ends. Returns the outcome:
        {ok, rc, cancelled, run_id, artifacts, errors: [text...], attempts}.

        `on_event` sees every event (logs, the design agents' tokens and tool calls, retries).
        `should_stop` is polled between events; when it turns true the run is cancelled through the hub
        and the stream is followed to its end, so the outcome is the hub's, not a guess."""
        out = {"ok": False, "rc": None, "cancelled": False, "run_id": None, "artifacts": [], "errors": [],
               "attempts": 1, "ended": False, "paused": None}
        cancelled_by_us = False
        try:
            with httpx.Client(timeout=httpx.Timeout(None, connect=30.0)) as cx:
                with cx.stream("POST", f"{self.base}/api/run/{module}/{tab}",
                               json={"job": project, "fields": fields}) as resp:
                    if resp.status_code >= 400 and "text/event-stream" not in (resp.headers.get("content-type") or ""):
                        txt = resp.read().decode("utf-8", "replace")[:1000]
                        try:
                            txt = json.loads(txt).get("detail") or txt
                        except Exception:
                            pass
                        out["errors"].append(f"the hub refused the run ({resp.status_code}): {txt}")
                        return out
                    out["run_id"] = resp.headers.get("x-run-id")
                    for ev in parse_sse(resp.iter_lines()):
                        if ev is None:
                            pass                                   # keepalive
                        else:
                            t = ev.get("type")
                            if t == "start" and ev.get("run_id"):
                                out["run_id"] = ev["run_id"]
                            elif t == "error":
                                out["errors"].append(str(ev.get("text") or "error"))
                            elif t == "retry":
                                out["attempts"] = int(ev.get("attempt") or out["attempts"])
                            elif t == "paused":          # the module saved its state and stopped: why, for the executor
                                out["paused"] = str(ev.get("reason") or "paused") + ((" -- " + str(ev["detail"])) if ev.get("detail") else "")
                            elif t == "done" and (ev.get("end") or "rc" in ev or "artifacts" in ev):
                                out["ok"] = bool(ev.get("ok"))
                                out["rc"] = ev.get("rc")
                                out["cancelled"] = bool(ev.get("cancelled"))
                                out["artifacts"] = ev.get("artifacts") or []
                                out["attempts"] = int(ev.get("attempts") or out["attempts"])
                                out["ended"] = True
                            try:
                                on_event(ev)
                            except Exception:
                                pass
                        if should_stop and not cancelled_by_us and should_stop() and out["run_id"]:
                            cancelled_by_us = True
                            self.cancel(out["run_id"])
        except HubError:
            raise
        except Exception as e:
            out["errors"].append(f"lost the hub's stream: {type(e).__name__}: {e}")
        if cancelled_by_us:
            out["cancelled"] = True
            out["ok"] = False
        if not out["ended"] and not out["errors"] and not out["cancelled"]:
            out["errors"].append("the stream ended without a done event")
        return out


def _detail(r: httpx.Response) -> str:
    try:
        d = r.json()
        return str(d.get("detail") or d.get("error") or d)[:400]
    except Exception:
        return (r.text or "")[:400]


def wait_for(cond: Callable[[], bool], timeout: float, step: float = 0.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return cond()
