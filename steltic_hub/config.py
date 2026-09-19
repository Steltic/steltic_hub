"""Paths and settings for the Steltic Hub.

The hub owns NOTHING module-specific. Everything it knows about a module comes from that
module's manifest (steltic_module.json), so adding a module is publishing a repo, not
editing this app.
"""
from __future__ import annotations
import os, pathlib, sys

PKG = pathlib.Path(__file__).resolve().parent
UI_DIR = PKG / "ui"
CATALOG_DIR = PKG / "catalog"


def _default_data_dir() -> pathlib.Path:
    env = os.environ.get("STELTIC_HUB_DATA")
    if env:
        return pathlib.Path(env)
    if sys.platform == "win32":
        root = pathlib.Path(os.environ.get("LOCALAPPDATA") or (pathlib.Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        root = pathlib.Path.home() / "Library" / "Application Support"
    else:
        root = pathlib.Path(os.environ.get("XDG_DATA_HOME") or (pathlib.Path.home() / ".local" / "share"))
    # The same folder the Windows launcher and the Tauri shell use (they set STELTIC_HUB_DATA to
    # it explicitly). One root for every entry point, so a hub started from a terminal sees the
    # modules the launcher installed instead of quietly provisioning a second copy.
    return root / "Steltic"


DATA = _default_data_dir()
MODULES_DIR = DATA / "modules"      # one git checkout per module
ENVS_DIR = DATA / "envs"            # one venv per module -- MANDATORY, see envs.py
JOBS_DIR = DATA / "jobs"            # one folder per project, shared across modules
STATE_FILE = DATA / "state.json"
LOGS_DIR = DATA / "logs"
URL_FILE = DATA / "hub.url"         # written on start: the URL the hub actually bound (see cli.py)
CONNECTION_FILE = DATA / "connection.json"   # the user's LLM connection, kept on this PC (see main.py)

for _d in (DATA, MODULES_DIR, ENVS_DIR, JOBS_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Port window for module servers the hub supervises.
PORT_BASE = int(os.environ.get("STELTIC_HUB_PORT_BASE", "8410"))
PORT_SPAN = 40

# uv is the provisioning tool: it installs its own CPython, so the user never picks one.
UV_BIN = os.environ.get("STELTIC_HUB_UV", "uv")
DEFAULT_PYTHON = os.environ.get("STELTIC_HUB_PYTHON", "3.12")

HEALTH_TIMEOUT = float(os.environ.get("STELTIC_HUB_HEALTH_TIMEOUT", "120"))


def hub_url() -> str:
    """The URL this hub answers on -- the `{hub_url}` template. Read from the marker cli.py writes
    at start (the port actually bound, which may differ from the preferred one), falling back to the
    preferred port. This is how a module whose server orchestrates other modules (through the hub's
    own /api/run) finds the hub without the hub knowing anything about that module."""
    try:
        u = URL_FILE.read_text(encoding="utf-8").strip()
        if u.startswith(("http://", "https://")):
            return u
    except OSError:
        pass
    return f"http://127.0.0.1:{os.environ.get('STELTIC_HUB_PORT', '8300')}"

# Seconds of silence after which a streaming run emits an SSE comment so the connection is
# provably alive (the module servers do the same every 20 s).
KEEPALIVE = float(os.environ.get("STELTIC_HUB_KEEPALIVE", "20"))
