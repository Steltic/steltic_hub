"""Job folders: one per project, shared by every module.

This is the quiet payoff of a hub. Today a Steltic design lands in one app's data dir, then
you hand-carry a zip to the nonlinear bot and it unpacks somewhere else. Here `Ex22_SMF` is
ONE folder that HR Steel writes report.html into and SNL later writes pushover/ and nlrha/
into -- which is exactly the layout steltic_viewer_bundle.html already probes for. The viewer
strip lights up on its own as modules fill the folder in.
"""
from __future__ import annotations
import json, re, shutil, time, pathlib
from . import config

# Letters, digits, "_" and "-" only. This is deliberately the intersection of what every module
# accepts as a job name: the design servers keep exactly this set and DROP anything else, so a
# project name the hub allowed but a module rewrote would put that module's outputs in a folder
# the hub never looks in. Runs of other characters collapse to one underscore.
_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def clean_name(name: str) -> str:
    n = _SAFE.sub("_", (name or "").strip())[:80].strip("_-")
    return n or "Project"


def clean_filename(name: str) -> str:
    """Uploaded file names keep their extension dots; everything else is sanitised like a job
    name. Path separators can never survive (only the basename is used by the caller)."""
    base = pathlib.Path(name or "upload").name
    stem, dot, ext = base.rpartition(".")
    if not dot or not stem:
        return clean_name(base)
    ext = re.sub(r"[^A-Za-z0-9]+", "", ext)[:12]
    return clean_name(stem) + ("." + ext if ext else "")


def job_path(name: str) -> pathlib.Path:
    """The folder a project WOULD use. Does not create it -- templating a command must not
    litter the projects list with folders for runs that never happened."""
    return config.JOBS_DIR / clean_name(name)


def job_dir(name: str) -> pathlib.Path:
    d = job_path(name)
    d.mkdir(parents=True, exist_ok=True)
    return d


def exists(name: str) -> bool:
    return job_path(name).is_dir()


def resolve_in_job(name: str, rel: str) -> pathlib.Path:
    """Path inside a job folder, with traversal refused. Every file the UI can reach goes
    through here."""
    base = (config.JOBS_DIR / clean_name(name)).resolve()
    target = (base / (rel or "")).resolve()
    if target != base and base not in target.parents:
        raise PermissionError("path escapes the job folder")
    return target


def list_jobs() -> list[dict]:
    out = []
    for d in sorted(config.JOBS_DIR.iterdir()) if config.JOBS_DIR.exists() else []:
        if not d.is_dir():
            continue
        try:
            files = [p for p in d.rglob("*") if p.is_file()]
            out.append({
                "name": d.name,
                "files": len(files),
                "bytes": sum(p.stat().st_size for p in files),
                "mtime": max([d.stat().st_mtime] + [p.stat().st_mtime for p in files]),
                "viewers": sorted(p.relative_to(d).as_posix() for p in d.rglob("*viewer*.html")),
                "reports": sorted(p.relative_to(d).as_posix() for p in d.rglob("*report*.html")),
                "last_run": _last_run(d),
            })
        except Exception:
            continue
    return sorted(out, key=lambda j: j["mtime"], reverse=True)


def _last_run(d: pathlib.Path) -> dict | None:
    p = d / "hub_runs.jsonl"
    if not p.exists():
        return None
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(lines[-50:]):
            ev = json.loads(line)
            if ev.get("event") == "done":
                return {"module": ev.get("module"), "tab": ev.get("tab"), "t": ev.get("t"),
                        "ok": ev.get("ok", ev.get("rc") == 0)}
    except Exception:
        pass
    return None


def tree(name: str, max_entries: int = 4000) -> list[dict]:
    base = config.JOBS_DIR / clean_name(name)
    if not base.exists():
        return []
    out = []
    for p in sorted(base.rglob("*")):
        if len(out) >= max_entries:
            break
        rel = p.relative_to(base).as_posix()
        if "/.git/" in "/" + rel:
            continue
        out.append({"path": rel, "dir": p.is_dir(),
                    "bytes": (p.stat().st_size if p.is_file() else 0),
                    "mtime": p.stat().st_mtime})
    return out


def delete_job(name: str):
    shutil.rmtree(config.JOBS_DIR / clean_name(name), ignore_errors=True)


def stamp(job: str, event: dict):
    """Append to the job's cross-module history so the Runs history survives a restart."""
    p = job_dir(job) / "hub_runs.jsonl"
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.time(), **event}) + "\n")


def history(job: str, limit: int = 400) -> list[dict]:
    p = config.JOBS_DIR / clean_name(job) / "hub_runs.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows
