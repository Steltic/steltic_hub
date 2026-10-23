"""An OpenSees model (OpenSees Tcl or OpenSeesPy) -> HR Steel's building description.

    read_opensees(upload, work, main=None, units=None, up=None) -> (description, report)

The upload is a .tcl or .py file, or a .zip of the model folder (the main script with the files it sources /
imports). It is copied / extracted into work/model, and record.py runs the main script there in a separate
interpreter -- the user's script never runs inside this server -- with OpenSees replaced by a recorder: the model
the script builds is written to work/record.json without anything being analysed. convert.py then reads members,
levels, grids, releases, supports, the diaphragm and the masses from that record (see convert.py).

Interpreters: an OpenSeesPy script runs under the Nonlinear module's Python (NL_PYTHON: numpy, scipy and
matplotlib are there) when it is known, else under this server's own; a Tcl script needs a Python with Tcl
(tkinter), the first of the two that has it.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import zipfile

from . import convert as CV                      # the module stays reachable as opensees.convert
from .convert import UNITS, IntakeError

__all__ = ["read_opensees", "IntakeError", "UNITS", "SCRIPT_EXT", "UPLOAD_EXT", "pick_main"]

HERE = pathlib.Path(__file__).resolve().parent
RECORD = HERE / "record.py"
SCRIPT_EXT = (".py", ".tcl")
UPLOAD_EXT = SCRIPT_EXT + (".zip",)
TIMEOUT_S = 300                       # record.py stops a script's analyses itself after 120 s; this is the backstop
MAX_UNZIPPED = 300 * 1024 * 1024
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_tk_cache: dict = {}


def _has_tk(py: str) -> bool:
    if py not in _tk_cache:
        try:
            r = subprocess.run([py, "-c", "import tkinter; tkinter.Tcl().eval('expr 1')"], capture_output=True,
                               timeout=60, creationflags=_NO_WINDOW)
            _tk_cache[py] = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _tk_cache[py] = False
    return _tk_cache[py]


def interpreters(lang: str, nl_python: str | None = None) -> list:
    """Pythons to run record.py under, best first."""
    out = []
    for c in (nl_python if nl_python is not None else os.environ.get("NL_PYTHON"), sys.executable):
        if c and c not in out and (os.path.isfile(c) or shutil.which(c)):
            out.append(c)
    if lang == "tcl":
        out = [c for c in out if _has_tk(c)]
    return out


# ---------------------------------------------------------------- the upload
def unpack(upload: pathlib.Path, dest: pathlib.Path) -> list:
    """Copy / extract the upload into dest; -> the scripts (paths relative to dest, '/' separated)."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if upload.suffix.lower() == ".zip":
        try:
            zf = zipfile.ZipFile(upload)
        except zipfile.BadZipFile:
            raise IntakeError("the .zip file cannot be opened")
        with zf:
            total = 0
            root = dest.resolve()
            for zi in zf.infolist():
                if zi.is_dir():
                    continue
                name = zi.filename.replace("\\", "/")
                target = (dest / name).resolve()
                if root not in target.parents:
                    raise IntakeError("the .zip file has an entry outside its folder (%s)" % zi.filename)
                if name.startswith("__MACOSX/") or any(q.startswith(".") for q in name.split("/")):
                    continue                                  # macOS resource forks, .git, .DS_Store
                total += zi.file_size
                if total > MAX_UNZIPPED:
                    raise IntakeError("the .zip file unpacks to more than %d MB" % (MAX_UNZIPPED // 2 ** 20))
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(zi) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
    else:
        shutil.copy2(upload, dest / upload.name)
    return sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*")
                  if p.is_file() and p.suffix.lower() in SCRIPT_EXT and "__pycache__" not in p.parts)


_TCL_SOURCE = re.compile(r"^\s*source\s+[\"{]?([^\s\"}]+)", re.M)
_PY_IMPORT = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.M)
_PY_RUN = re.compile(r"""(?:open|run_path|exec_file|execfile|eval_file)\s*\(\s*[rbuf]*["']([^"']+\.(?:py|tcl))["']""")
_MAIN_NAMES = ("main", "model", "run", "build", "building", "analysis")


def pick_main(folder: pathlib.Path, scripts: list, main: str | None = None) -> str:
    """The script to run: the one asked for, the only one, or the one no other script sources / imports."""
    if not scripts:
        raise IntakeError("no OpenSees script found: give a .tcl or .py file (or a .zip with one)")
    if main:
        m = main.replace("\\", "/").strip("/")
        hit = [s for s in scripts if s == m] or [s for s in scripts if s.rsplit("/", 1)[-1] == m.rsplit("/", 1)[-1]]
        if len(hit) == 1:
            return hit[0]
        raise IntakeError("MAIN: %s is not one of the scripts in the .zip file (%s)" % (main, ", ".join(scripts)))
    if len(scripts) == 1:
        return scripts[0]
    used = set()
    for s in scripts:
        try:
            txt = (folder / s).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for ref in _TCL_SOURCE.findall(txt) + _PY_RUN.findall(txt):
            used.add(ref.replace("\\", "/").rsplit("/", 1)[-1].lower())
        for a, b in _PY_IMPORT.findall(txt):
            mod = (a or b).split(".")[0]
            used.add(mod.lower() + ".py")
    top = [s for s in scripts if s.rsplit("/", 1)[-1].lower() not in used]
    if len(top) > 1:                                 # several stand alone: a shallow one with a main-like name
        shallow = min(s.count("/") for s in top)
        top = [s for s in top if s.count("/") == shallow]
        named = [s for s in top if any(n in s.rsplit("/", 1)[-1].lower() for n in _MAIN_NAMES)]
        if len(named) == 1:
            top = named
    if len(top) == 1:
        return top[0]
    raise IntakeError("MAIN: the .zip file has several scripts that could be the model (%s): choose the main file"
                      % ", ".join(top or scripts))


# ---------------------------------------------------------------- record + convert
def record(script: pathlib.Path, out: pathlib.Path, nl_python: str | None = None, timeout: float = TIMEOUT_S) -> dict:
    """Run record.py on the script; -> the record (dict). Raises IntakeError when no interpreter can run it."""
    lang = "tcl" if script.suffix.lower() == ".tcl" else "py"
    pys = interpreters(lang, nl_python)
    if not pys:
        raise IntakeError("NO_TCL" if lang == "tcl" else "no Python interpreter found to run the script")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")}
    env.update(MPLBACKEND="Agg", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    log = out.with_suffix(".log")
    last = None
    for py in pys:
        if out.exists():
            out.unlink()
        try:
            with open(log, "w", encoding="utf-8", errors="replace") as lf:
                subprocess.run([py, str(RECORD), str(script.resolve()), str(out.resolve())], cwd=str(script.parent),
                               stdout=lf, stderr=subprocess.STDOUT, timeout=timeout, env=env, creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            last = IntakeError("the script ran for more than %d s without finishing its model (an endless loop before "
                               "the first analysis?)" % timeout)
            continue
        except OSError as ex:
            last = IntakeError("could not start %s: %s" % (py, ex))
            continue
        try:
            rec = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            tail = log.read_text(encoding="utf-8", errors="replace")[-1500:] if log.exists() else ""
            last = IntakeError("the recorder failed under %s: %s" % (py, tail.strip() or "no output"))
            continue
        if rec.get("error") == "NO_TCL":
            last = IntakeError("NO_TCL")
            continue
        rec["interpreter"] = py
        return rec
    raise last or IntakeError("the script could not be run")


def _sha256(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def read_opensees(upload, work, main: str | None = None, units: str | None = None, up: str | None = None,
                  nl_python: str | None = None, timeout: float = TIMEOUT_S):
    """The uploaded model -> (description, report). Files: work/model/ (the scripts as run), work/record.json,
    work/record.log (the script's own output) and work/conversion.json (the report)."""
    upload, work = pathlib.Path(upload), pathlib.Path(work)
    work.mkdir(parents=True, exist_ok=True)
    if upload.suffix.lower() not in UPLOAD_EXT:
        raise IntakeError("give an OpenSees Tcl script (.tcl), an OpenSeesPy script (.py) or a .zip of the model folder")
    if units and units not in UNITS:
        raise IntakeError("UNITS: %s is not one of %s" % (units, ", ".join(UNITS)))
    if up and up not in ("Z", "Y"):
        raise IntakeError("the vertical axis is Z or Y, not %s" % up)
    folder = work / "model"
    scripts = unpack(upload, folder)
    main_rel = pick_main(folder, scripts, main)
    script = folder / main_rel
    rec = record(script, work / "record.json", nl_python=nl_python, timeout=timeout)
    try:
        desc, report = CV.convert(rec, file_name=main_rel.rsplit("/", 1)[-1], sha256=_sha256(upload), units=units, up=up)
    except IntakeError as ex:
        msg = str(ex)
        if "built no model" in msg and "ModuleNotFoundError" in msg:
            msg += (" -- the script imports a package the Python running it lacks (%s); run this page from the hub so "
                    "the Nonlinear module's Python, which has numpy / scipy / matplotlib, runs OpenSeesPy scripts"
                    % rec.get("interpreter"))
            raise IntakeError(msg)
        raise
    report.update(main=main_rel, scripts=scripts, language=rec.get("lang"), upload=upload.name,
                  interpreter=rec.get("interpreter"), analyze_calls=rec.get("analyze_calls"), models=rec.get("models"))
    desc["source"]["file"] = upload.name if upload.suffix.lower() == ".zip" else main_rel.rsplit("/", 1)[-1]
    if upload.suffix.lower() == ".zip":
        desc["source"]["main"] = main_rel
    (work / "conversion.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    return desc, report
