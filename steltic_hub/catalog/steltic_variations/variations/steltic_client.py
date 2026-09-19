"""Talking to the HR Steel server (its own API, unchanged): design a building, download the
package, read the brief back out of a previous design."""
from __future__ import annotations
import json, re, time
import httpx


class DesignError(RuntimeError):
    pass


def run_design(base_url: str, building: str, brief: str, on_event, should_stop, resume: bool = False) -> dict:
    """POST /api/run and follow its SSE until done / paused / error. Returns the outcome dict.
    on_event(ev) sees every event; should_stop() polled between events asks the server to stop."""
    outcome = {"status": "failed", "reason": ""}
    stop_sent = False
    with httpx.Client(timeout=httpx.Timeout(None, connect=30.0)) as cx:
        with cx.stream("POST", base_url + "/api/run", json={"building": building, "brief": brief, "resume": resume}) as r:
            if r.status_code >= 400:
                txt = r.read().decode("utf-8", "replace")[:800]
                try:
                    txt = json.loads(txt).get("detail") or txt
                except Exception:
                    pass
                raise DesignError(f"HR Steel refused the run ({r.status_code}): {txt}")
            buf = ""
            for chunk in r.iter_text():
                buf += chunk
                while True:
                    i = buf.find("\n\n")
                    if i < 0:
                        break
                    block, buf = buf[:i], buf[i + 2:]
                    data = [ln[5:].strip() for ln in block.split("\n") if ln.startswith("data:")]
                    if not data:
                        continue
                    try:
                        ev = json.loads("\n".join(data))
                    except Exception:
                        continue
                    t = ev.get("type")
                    if t == "bundle":
                        continue
                    on_event(ev)
                    if t == "done":
                        outcome = {"status": "done", "reason": ""}
                    elif t == "paused":
                        outcome = {"status": "paused", "reason": ev.get("reason") or "paused"}
                    elif t == "error":
                        outcome = {"status": "failed", "reason": ev.get("text") or "error"}
                    if should_stop() and not stop_sent:
                        stop_sent = True
                        try:
                            cx.post(base_url + "/api/stop", json={"building": building}, timeout=10.0)
                        except Exception:
                            pass
    if stop_sent and outcome["status"] != "done":
        outcome = {"status": "stopped", "reason": "stopped by user"}
    return outcome


def download(base_url: str, building: str) -> bytes:
    with httpx.Client(timeout=httpx.Timeout(120.0, connect=20.0)) as cx:
        r = cx.get(base_url + f"/api/download/{building}")
        if r.status_code >= 400:
            raise DesignError(f"no package for {building} ({r.status_code})")
        return r.content


def stop(base_url: str, building: str):
    try:
        with httpx.Client(timeout=10.0) as cx:
            cx.post(base_url + "/api/stop", json={"building": building})
    except Exception:
        pass


def brief_from_package(files: dict) -> str:
    """The brief the design was started from: the first user message of conversation.json."""
    raw = files.get("conversation.json")
    if not raw:
        return ""
    try:
        conv = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return ""
    msgs = conv.get("messages") if isinstance(conv, dict) else conv
    if not isinstance(msgs, list):
        return ""
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
            if isinstance(c, str) and c.strip():
                # the agent's first turn is <contract head> BUILDING NAME: x  DESIGN BRIEF: <brief>  Design this building now.
                m = re.search(r"DESIGN BRIEF:\s*(.*?)\s*(?:Design this building now\.|\[framework note\]|$)", c, re.S)
                return (m.group(1) if m else c).strip()
    return ""


def healthy(base_url: str) -> bool:
    try:
        with httpx.Client(timeout=5.0) as cx:
            return cx.get(base_url + "/healthz").status_code < 500
    except Exception:
        return False
