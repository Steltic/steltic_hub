"""`steltic-hub` -- start the local server and open the window.

The hub binds to 127.0.0.1 and has no authentication, exactly like the modules it supervises.
Nothing here should ever be exposed to a network.
"""
from __future__ import annotations
import argparse, os, socket, sys, threading, time, webbrowser


def _ensure_streams():
    """pythonw.exe runs the process with NO stdout and NO stderr -- both are literally None.

    `print()` survives that (CPython returns early when sys.stdout is None), which is why it
    looks harmless. uvicorn does not: its logging config binds handlers to ext://sys.stdout,
    resolves that to None, and the server fails during startup -- before it binds the port, and
    with nowhere to report why. The launcher then just watches a health check time out.

    Give the process real streams before uvicorn is imported. The file doubles as the crash log
    for a start that fails for any other reason.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return None
    from . import config
    path = config.LOGS_DIR / "hub.log"
    try:
        f = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
    except Exception:
        f = open(os.devnull, "w")
        path = None
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f
    return path


def _port_free(port: int) -> bool:
    """Is nobody listening on 127.0.0.1:port? A connect probe, deliberately not a bind: for up to a
    minute after a hub exits its port is full of TIME_WAIT sockets, and a plain bind fails on them --
    which is exactly when a successor (--replace, or a relaunch after a stale hub stopped) needs the
    port. uvicorn itself binds with SO_REUSEADDR, so TIME_WAIT never stops the real server."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return False
    except OSError:
        return True


def _free_port(preferred: int) -> int:
    if _port_free(preferred):
        return preferred
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
        except OSError:
            raise SystemExit("no free port")


def _hub_info(url: str) -> dict | None:
    """The /healthz answer of a Steltic Hub at this URL (None: nothing, or not a hub, there)."""
    try:
        import urllib.request, json
        with urllib.request.urlopen(url + "/healthz", timeout=2) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return d if bool(d.get("ok")) and "modules" in d else None
    except Exception:
        return None


def _hub_at(url: str) -> bool:
    """Is a Steltic Hub already answering at this URL?"""
    return _hub_info(url) is not None


def _ask_hub_to_stop(url: str) -> bool:
    """POST /api/hub/shutdown on a running hub (graceful: its module servers go with it).
    False for a hub too old to have the route."""
    try:
        import urllib.request
        req = urllib.request.Request(url + "/api/hub/shutdown", method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def _wait_port_free(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_free(port):
            return True
        time.sleep(0.25)
    return _port_free(port)


def main():
    log_path = _ensure_streams()

    ap = argparse.ArgumentParser(prog="steltic-hub", description="Steltic Hub -- local module shell")
    ap.add_argument("--port", type=int, default=int(os.environ.get("STELTIC_HUB_PORT", "8300")))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--bootstrap", action="store_true",
                    help="fetch uv and provision module environments, then exit (first-run setup)")
    ap.add_argument("--restart", action="store_true",
                    help="if a hub is already running on the port, stop it and start this one in its place")
    ap.add_argument("--replace", type=int, metavar="PID", default=0,
                    help="(internal) started by a hub that is shutting down: wait for its port, then take over")
    ap.add_argument("cmd", nargs="?", help="doctor | link <id> <path> | unlink <id> | forget <id> | install <id>...")
    ap.add_argument("rest", nargs="*")
    args = ap.parse_args()

    if args.cmd == "doctor":
        return doctor()
    if args.cmd == "link":
        if len(args.rest) != 2:
            print("usage: steltic-hub link <module-id> <path-to-working-copy>"); return 2
        return link_cmd(args.rest[0], args.rest[1])
    if args.cmd == "unlink":
        if not args.rest:
            print("usage: steltic-hub unlink <module-id>"); return 2
        from .registry import Registry
        r = Registry(); r.unlink(args.rest[0])
        print("[hub] unlinked (your files are untouched)"); return 0
    if args.cmd == "forget":
        if not args.rest:
            print("usage: steltic-hub forget <module-id>   (a registered/linked module; built-ins cannot be forgotten)"); return 2
        from .registry import Registry, RegistryError
        r = Registry()
        try:
            r.remove_custom(args.rest[0])
        except RegistryError as e:
            print(f"[hub] {e}"); return 2
        print(f"[hub] forgot {args.rest[0]} (a linked working copy is untouched)"); return 0
    if args.cmd == "install":
        if not args.rest:
            print("usage: steltic-hub install <module-id> [<module-id> ...]"); return 2
        from .registry import Registry, RegistryError
        from .envs import EnvError
        r = Registry()
        rc = 0
        for mid in args.rest:
            try:
                r.install(mid)
            except (RegistryError, EnvError) as e:
                print(f"[hub] {mid}: {e}"); rc = 1
        return rc
    if args.cmd:
        print(f"[hub] unknown command {args.cmd!r} (doctor | link | unlink | forget | install)"); return 2
    if args.bootstrap:
        return bootstrap()

    if args.host not in ("127.0.0.1", "localhost"):
        print("[hub] refusing to bind off-localhost: the hub has no authentication and holds your LLM key")
        raise SystemExit(2)

    from . import config
    preferred = f"http://127.0.0.1:{args.port}"
    if args.replace:
        # Spawned by /api/hub/restart: the old hub releases the port as it stops (its module servers
        # go down in its shutdown hook). Take the port over rather than falling back to a free one.
        print(f"[hub] replacing pid {args.replace}: waiting for port {args.port}")
        if not _wait_port_free(args.port, 90):
            print(f"[hub] port {args.port} never came free -- giving up")
            return 1
    elif not _port_free(args.port):
        running = _hub_info(preferred)
        if running and (args.restart or running.get("stale")):
            # The running hub is older than the code on disk (or a restart was asked for): stop it
            # and start this one in its place, instead of raising a window on the stale process.
            why = "restart requested" if args.restart else "its source files changed since it started"
            print(f"[hub] a hub is running on {preferred} (pid {running.get('pid', '?')}) -- {why}; replacing it")
            if not _ask_hub_to_stop(preferred):
                print("[hub] that hub predates /api/hub/shutdown -- stop it yourself (or run windows\\Steltic.bat -Restart)")
                return 1
            if not _wait_port_free(args.port, 60):
                print(f"[hub] port {args.port} never came free -- giving up")
                return 1
        elif running:
            # Single instance: a second launch just raises a window on the running hub.
            print(f"[hub] already running on {preferred}")
            if not args.no_browser:
                webbrowser.open(preferred)
            return 0
    port = _free_port(args.port)
    url = f"http://127.0.0.1:{port}"
    if port != args.port:
        print(f"[hub] port {args.port} is in use -- using {port} instead")
    try:
        # The launcher reads this to find the window URL, whatever port was actually free.
        config.URL_FILE.write_text(url, encoding="utf-8")
    except Exception:
        pass
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"[hub] serving on {url}")
    if log_path:
        print(f"[hub] no console (pythonw): logging to {log_path}")
    import uvicorn
    from .main import app
    # A Server handle rather than uvicorn.run(): the app keeps it so /api/hub/shutdown and
    # /api/hub/restart can stop this process gracefully (should_exit -> lifespan hook -> module
    # servers stopped, URL marker retired) -- pythonw has no console to send a Ctrl-C to.
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    app.state.server, app.state.port, app.state.url = server, port, url
    try:
        server.run()
    except BaseException:
        import traceback
        traceback.print_exc()          # goes to hub.log when started windowless
        raise
    finally:
        # normally already gone (the app's shutdown hook removes it -- uvicorn re-raises the
        # stopping signal, so this line is reached only on a startup failure)
        try:
            if config.URL_FILE.exists() and config.URL_FILE.read_text(encoding="utf-8").strip() == url:
                config.URL_FILE.unlink()
        except Exception:
            pass


def bootstrap():
    """First-run provisioning, runnable headless (CI, or an installer's post-install step)."""
    from . import envs
    from .registry import Registry
    reg = Registry()
    envs.ensure_uv()
    ids = os.environ.get("STELTIC_HUB_BOOTSTRAP_MODULES", "").split(",")
    ids = [i.strip() for i in ids if i.strip()] or [m.id for m in reg.known()]
    failed = []
    for mid in ids:
        try:
            print(f"\n=== {mid} ===")
            reg.install(mid)
        except Exception as e:
            print(f"[hub] {mid} failed: {e}")
            failed.append(mid)
    print("\n[hub] bootstrap complete" + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


def doctor():
    """Print everything needed to diagnose a local install. Run this before asking why."""
    import shutil
    from . import config, envs
    from .registry import Registry

    def line(k, v): print(f"  {k:<22} {v}")

    print("\n== Steltic Hub doctor ==\n")
    line("hub version", __import__("steltic_hub").__version__)
    line("hub python", sys.version.split()[0])
    line("platform", sys.platform)
    line("data dir", config.DATA)
    line("uv", envs.uv_path() or "NOT FOUND (will be fetched on first install)")
    line("git", shutil.which("git") or "NOT FOUND -- module installs need git")
    if sys.platform == "win32":
        from . import sac
        st = sac.state()
        line("Smart App Control", {"on": "ON -- blocks PyTorch / OpenSees / onnxruntime (WinError 4551); see README",
                                   "evaluation": "evaluation -- nothing blocked yet, may switch itself ON at a reboot",
                                   "off": "off"}.get(st, st or "not present (Windows 10, or not a clean-install Win 11)"))

    port = int(os.environ.get("STELTIC_HUB_PORT", "8300"))
    if _port_free(port):
        line(f"port {port}", "free")
    else:
        line(f"port {port}", "IN USE by a running hub" if _hub_at(f"http://127.0.0.1:{port}")
             else "IN USE by another program (the hub will pick another port)")

    reg = Registry()
    print("\n== modules ==")
    problems = []
    for m in reg.known():
        st = reg.status(m.id)
        env = st.get("env", {})
        src = st["linked"] and f"LINKED -> {st['linked']}" or (m.git or "-")
        print(f"\n  {m.name}  [{m.id}]")
        line("  source", src)
        if st["linked"] and st["dirty"]:
            line("  working copy", "has uncommitted changes (that is the point)")
        line("  checkout", reg.checkout(m.id) if st["installed"] else "not installed")
        line("  manifest", "from checkout" if st["has_local_manifest"] and not st["manifest_problem"]
             else "from hub catalog")
        if st["manifest_problem"]:
            line("  manifest problem", st["manifest_problem"]); problems.append(m.id)
        line("  env", envs.env_dir(m.id) if st["env_ready"] else "not built")
        if st["env_ready"]:
            line("  python", env.get("python", "?"))
            line("  openseespy", env.get("openseespy"))
            if env.get("ready") is False:
                line("  PROBLEM", env.get("reason", "")); problems.append(m.id)
        missing = [n for n in m.needs if not reg.is_installed(n)]
        if missing:
            line("  needs", ", ".join(missing) + " (not installed)")
        line("  tabs", ", ".join(f"{t.id}({t.kind})" for t in m.tabs))

    print("\n== projects ==")
    from . import jobs
    jl = jobs.list_jobs()
    for j in jl[:12]:
        line(f"  {j['name']}", f"{j['files']} files, {j['bytes']/1e6:.1f} MB")
    if not jl:
        line("  (none)", "")

    print("\n" + ("Problems in: " + ", ".join(sorted(set(problems))) if problems else "No problems found.") + "\n")
    return 1 if problems else 0


def link_cmd(mod_id, path):
    from .registry import Registry, RegistryError
    reg = Registry()
    try:
        d = reg.link(mod_id, path)
    except RegistryError as e:
        print(f"[hub] {e}"); return 2
    print(f"[hub] {mod_id} -> {d}")
    print("[hub] now run:  steltic-hub install " + mod_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
