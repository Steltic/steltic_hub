"""Per-module Python environments.

WHY ONE VENV PER MODULE (this is not a preference, it is forced):

  steltic      installs top-level packages: steltic, steel_engine, contract, frontend, test_buildings
  steltic_cfs  installs top-level packages: steltic, steel_engine, contract, frontend, test_buildings

Same names, different code (verified: main.py, config.py, agent.py and steel_engine/pipeline.py
all differ). Installed into one environment the second overwrites the first and you get a CFS
engine answering hot-rolled requests -- silently, with a plausible-looking report. So every
module gets its own venv and the hub never imports module code into its own process.

The same isolation buys three other things:
  * openseespy only ships wheels for CPython 3.10-3.12, while the hub itself can run on anything.
  * grokbot wants Docling pinned at 2.123.1 ("do not upgrade without revalidation") -- a pin that
    would otherwise fight every other module's resolver.
  * a module update that breaks its deps cannot take the app down with it.

uv provisions the interpreter too, so the user never installs Python and the
"uv ignores the upper Python bound" trap from the Steltic README cannot happen: we always
pass an explicit --python.
"""
from __future__ import annotations
import json, os, shutil, subprocess, sys, pathlib, time, urllib.request
from . import config
from .manifest import Manifest


class EnvError(RuntimeError):
    pass


# Windows: never let a helper interpreter flash a console window over the app.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def uv_path() -> str | None:
    """uv from PATH, or the copy we bootstrapped into the data dir."""
    p = shutil.which(config.UV_BIN)
    if p:
        return p
    local = config.DATA / "bin" / ("uv.exe" if sys.platform == "win32" else "uv")
    return str(local) if local.exists() else None


def ensure_uv(log=print) -> str:
    """Fetch a private uv if the machine has none. This is the whole first-run bootstrap:
    once uv exists it provisions CPython and every module venv."""
    p = uv_path()
    if p:
        return p
    dest_dir = config.DATA / "bin"
    dest_dir.mkdir(parents=True, exist_ok=True)
    log("[hub] no uv found -- fetching a private copy")
    env = dict(os.environ, UV_INSTALL_DIR=str(dest_dir), UV_NO_MODIFY_PATH="1")
    try:
        if sys.platform == "win32":
            script = dest_dir / "install-uv.ps1"
            urllib.request.urlretrieve("https://astral.sh/uv/install.ps1", script)
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                           check=True, env=env, creationflags=_NO_WINDOW)
        else:
            script = dest_dir / "install-uv.sh"
            urllib.request.urlretrieve("https://astral.sh/uv/install.sh", script)
            subprocess.run(["sh", str(script)], check=True, env=env)
    except Exception as e:
        raise EnvError(f"could not download uv ({e}) -- check your network, or install uv and put it on PATH")
    p = uv_path()
    if not p:
        raise EnvError("uv bootstrap finished but no uv binary appeared")
    return p


def env_dir(mod_id: str) -> pathlib.Path:
    return config.ENVS_DIR / mod_id


def python_bin(mod_id: str) -> pathlib.Path:
    d = env_dir(mod_id)
    return d / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def env_ready(mod_id: str) -> bool:
    return python_bin(mod_id).exists()


def _stream(cmd: list[str], cwd=None, env=None, log=print) -> int:
    log("$ " + " ".join(str(c) for c in cmd))
    try:
        proc = subprocess.Popen([str(c) for c in cmd], cwd=cwd and str(cwd), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                bufsize=1, errors="replace", creationflags=_NO_WINDOW)
    except OSError as e:
        log(f"[hub] could not start {cmd[0]}: {e}")
        return 127
    assert proc.stdout
    for line in proc.stdout:
        log(line.rstrip())
    return proc.wait()


def create_env(m: Manifest, root: pathlib.Path, log=print, force: bool = False) -> pathlib.Path:
    """Create (or rebuild) the module's venv and install it. Idempotent. `root` is the folder
    that holds the module's pyproject (the checkout, or its declared subdir)."""
    uv = ensure_uv(log=log)
    d = env_dir(m.id)
    py = m.python or config.DEFAULT_PYTHON
    if force and d.exists():
        log(f"[hub] removing existing env for {m.id}")
        shutil.rmtree(d, ignore_errors=True)
        if d.exists():
            raise EnvError(f"could not remove {d} -- is a run or the module server still using it?")
    if not python_bin(m.id).exists():
        log(f"[hub] creating Python {py} env for {m.id}")
        # --python <ver> is explicit on purpose: uv downloads that interpreter if missing, and an
        # explicit pin is what stops an install landing on 3.13 where openseespy cannot load.
        rc = _stream([uv, "venv", "--python", py, str(d)], log=log)
        if rc != 0:
            raise EnvError(f"could not create the Python {py} environment for {m.id}")
    install_into(m, root, log=log)
    return d


def install_into(m: Manifest, root: pathlib.Path, log=print):
    uv = ensure_uv(log=log)
    if not env_ready(m.id):
        raise EnvError(f"no environment for {m.id} -- install it first")
    env = dict(os.environ, VIRTUAL_ENV=str(env_dir(m.id)))
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    root = pathlib.Path(root)
    if m.install:
        if not root.is_dir():
            raise EnvError(f"module folder {root} does not exist")
        rc = _stream([uv, "pip", "install", "--python", str(python_bin(m.id)), *m.install],
                     cwd=root, env=env, log=log)
        if rc != 0:
            raise EnvError(f"dependency install failed for {m.id} -- see the log above")
    if m.extra_requirements:
        rc = _stream([uv, "pip", "install", "--python", str(python_bin(m.id)), *m.extra_requirements],
                     cwd=root, env=env, log=log)
        if rc != 0:
            raise EnvError(f"extra requirements failed for {m.id}")
    for cmd in (m.post_install or []):
        # argv list, no shell. Used by modules that need a writable workspace laid out (grokbot
        # copies its scripts + index next to a standards/ folder the user fills with licensed PDFs).
        rendered = [str(c).replace("{data_dir}", str(config.DATA)).replace("{module_dir}", str(root))
                    for c in cmd]
        rc = _stream([str(python_bin(m.id)), *rendered], cwd=root, env=env, log=log)
        if rc != 0:
            raise EnvError(f"post-install step failed for {m.id}: {' '.join(rendered)[:200]}")
    invalidate(m.id)


# verify_env spawns the module interpreter and imports openseespy, which drags numpy/scipy in
# and takes seconds. /api/state is called after every run and every refresh, so the answer is
# cached per environment and only recomputed when the interpreter changes (a rebuild, an
# update) or an explicit invalidate().
_VERIFY_CACHE: dict[str, tuple[float, dict]] = {}


def invalidate(mod_id: str | None = None):
    """Forget what we know about a module's environment (or every environment).

    Both caches go: the interpreter check and the optional-component probes. A Rebuild env or a
    Remove + Install replaces the interpreter, and a group that was importable in the old one (the
    converter's Docling, installed on demand) is not in the new one -- keeping its cached `present`
    let the Convert tab's gate open on an environment that would die on the first import.
    """
    if mod_id is None:
        _VERIFY_CACHE.clear()
        _OPTIONAL_CACHE.clear()
    else:
        _VERIFY_CACHE.pop(mod_id, None)
        for key in [k for k in _OPTIONAL_CACHE if k[0] == mod_id]:
            _OPTIONAL_CACHE.pop(key, None)


def verify_env(m: Manifest, cached: bool = True) -> dict:
    """Report the interpreter a module actually got, plus whether openseespy imports.

    This is the check the Steltic README asks users to do by hand. Doing it here means a bad
    environment is a red dot in the UI instead of a cryptic failure twenty minutes into a run.
    """
    py = python_bin(m.id)
    if not py.exists():
        return {"ready": False, "reason": "not installed"}
    try:
        stamp = py.stat().st_mtime
    except OSError:
        stamp = 0.0
    hit = _VERIFY_CACHE.get(m.id)
    if cached and hit and hit[0] == stamp:
        return dict(hit[1])
    code = ("import sys,json\n"
            "v='%d.%d.%d'%sys.version_info[:3]\n"
            "o=None\n"
            "try:\n import openseespy.opensees as _o; o=True\nexcept Exception: o=False\n"
            "print(json.dumps({'python':v,'openseespy':o}))")
    try:
        out = subprocess.run([str(py), "-c", code], capture_output=True, text=True, timeout=120,
                             creationflags=_NO_WINDOW)
        info = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as e:
        info = {"ready": False, "reason": f"interpreter check failed: {e}"}
        _VERIFY_CACHE[m.id] = (stamp, info)
        return dict(info)
    major_minor = tuple(int(x) for x in info["python"].split(".")[:2])
    info["ready"] = True
    if info.get("openseespy") is False and major_minor >= (3, 13):
        info["ready"] = False
        info["reason"] = (f"Python {info['python']}: openseespy ships wheels for 3.10-3.12 only. "
                          "Rebuild this module's environment.")
    info["checked_at"] = time.time()
    _VERIFY_CACHE[m.id] = (stamp, info)
    return dict(info)


def remove_env(mod_id: str):
    shutil.rmtree(env_dir(mod_id), ignore_errors=True)
    invalidate(mod_id)


def install_optional(m: Manifest, group: str, log=print):
    """Install one declared optional dependency group into the module's venv."""
    spec = (m.optional or {}).get(group)
    if not spec:
        raise EnvError(f"{m.id} declares no optional group {group!r}")
    reqs = spec.get("requirements") or []
    if not reqs:
        return
    if not env_ready(m.id):
        raise EnvError(f"install {m.name} first")
    uv = ensure_uv(log=log)
    log(f"[hub] installing {spec.get('label', group)} into {m.id}")
    rc = _stream([uv, "pip", "install", "--python", str(python_bin(m.id)), *reqs], log=log)
    if rc != 0:
        raise EnvError(f"could not install {group} for {m.id}")
    _OPTIONAL_CACHE.pop((m.id, group), None)
    log(f"[hub] {spec.get('label', group)} ready")


_OPTIONAL_CACHE: dict[tuple[str, str], bool] = {}


def probe_imports(spec: dict) -> list[str]:
    """`probe_import` may be one import name or a list -- every name must import for the group to count as
    present (Docling imports fine without onnxruntime, then fails at the first OCR page)."""
    probe = spec.get("probe_import")
    if not probe:
        return []
    names = probe if isinstance(probe, list) else str(probe).split(",")
    return [n.strip() for n in names if n and n.strip()]


def optional_present(m: Manifest, group: str) -> bool:
    """Is the group importable in the module venv? Checked by import name, not package name."""
    spec = (m.optional or {}).get(group) or {}
    probe = probe_imports(spec)
    if not probe or not env_ready(m.id):
        return False
    key = (m.id, group)
    if key in _OPTIONAL_CACHE:
        return _OPTIONAL_CACHE[key]
    try:
        r = subprocess.run([str(python_bin(m.id)), "-c", "import " + ", ".join(probe)],
                           capture_output=True, timeout=120, creationflags=_NO_WINDOW)
        ok = r.returncode == 0
    except Exception:
        ok = False
    _OPTIONAL_CACHE[key] = ok
    return ok
