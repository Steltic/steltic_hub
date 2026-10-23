"""The hub updating itself from the Modules page: Check for updates / Update, like any module.

Where the hub's code lives decides how it is updated:

* A GIT CHECKOUT (the install guide clones github.com/Steltic/steltic_hub and the launcher runs it as an
  editable install). `git fetch` the branch, then FAST-FORWARD to it. Unlike a module's checkout, which is
  the hub's private clone and is simply reset, this folder is the user's: nothing in it is thrown away.
  Tracked files that were edited are put aside with `git stash` first (named, recoverable with
  `git stash pop`), untracked files are left alone, and a checkout that has commits of its own is not
  touched -- the user updates that one with git. Then, when the update changed pyproject.toml, the hub's
  environment is reinstalled (new dependencies), and so is the environment of every installed bundled
  module whose requirements changed (their code is part of the hub and moves with it).
* AN INSTALLED PACKAGE (`pip install steltic-hub`, no checkout): `uv pip install --upgrade` from the repo.

Either way the hub that is running is still the OLD code afterwards. The page then offers Restart hub,
which is the existing /api/hub/restart: a fresh process on the same port, the window reloads.
"""
from __future__ import annotations

import datetime
import pathlib
import re
import subprocess
import sys

from . import config, envs

REPO = "https://github.com/Steltic/steltic_hub"
BRANCH = "main"
RAW_INIT = "https://raw.githubusercontent.com/Steltic/steltic_hub/{branch}/steltic_hub/__init__.py"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
_VERSION = re.compile(r"""__version__\s*=\s*["']([^"']+)["']""")


class UpdateError(RuntimeError):
    pass


def source_root() -> pathlib.Path:
    """The folder that holds the hub's code: the checkout root, or site-packages for an installed package."""
    return config.PKG.parent


def is_checkout(root: pathlib.Path | None = None) -> bool:
    root = root or source_root()
    return (root / ".git").exists() and (root / "pyproject.toml").exists()


def _git(args: list[str], root: pathlib.Path, timeout: float = 120) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, errors="replace",
                              timeout=timeout, creationflags=_NO_WINDOW)
    except FileNotFoundError:
        raise UpdateError("git is not available -- install Git for Windows (git-scm.com) and try again")
    except subprocess.TimeoutExpired:
        raise UpdateError("git %s took longer than %d s -- check your network" % (args[0], timeout))


def _out(args: list[str], root: pathlib.Path) -> str:
    try:
        r = _git(args, root, timeout=30)
    except UpdateError:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _version_of(text: str) -> str:
    m = _VERSION.search(text or "")
    return m.group(1) if m else ""


def _vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4])


def _version() -> str:
    from . import __version__
    return __version__


def _tracked_changes(root: pathlib.Path) -> list[str]:
    """Tracked files with uncommitted edits (untracked files are not ours to touch, and never in the way)."""
    txt = _out(["diff", "--name-only", "HEAD"], root)            # staged and unstaged, against the commit
    return [ln.strip() for ln in txt.splitlines() if ln.strip()]


def _branch(root: pathlib.Path) -> str:
    b = _out(["rev-parse", "--abbrev-ref", "HEAD"], root)
    return b if b and b != "HEAD" else BRANCH


def _remote(root: pathlib.Path) -> str:
    """`origin` when the checkout has one, else the repo URL itself (a fetch needs no configured remote)."""
    return "origin" if _out(["remote", "get-url", "origin"], root) else REPO


# ---------------------------------------------------------------- what the page shows
def status() -> dict:
    """Cheap (no network): what is running and where it came from."""
    root = source_root()
    st = {"version": _version(), "source": str(root), "repo": REPO, "mode": "checkout" if is_checkout(root) else "package"}
    if st["mode"] == "checkout":
        st.update(commit=_out(["rev-parse", "--short=8", "HEAD"], root), branch=_branch(root),
                  remote=_out(["remote", "get-url", "origin"], root) or REPO, changed=_tracked_changes(root))
    return st


def _fetch(root: pathlib.Path, log=None) -> str:
    branch = _branch(root)
    r = _git(["fetch", _remote(root), branch], root, timeout=180)
    if log:
        for ln in (r.stdout + r.stderr).splitlines():
            if ln.strip():
                log(ln.rstrip())
    if r.returncode != 0:
        raise UpdateError("could not fetch %s from %s: %s" % (branch, _remote(root), (r.stderr or r.stdout).strip()[-300:]))
    return branch


def _compare(root: pathlib.Path) -> dict:
    """HEAD against what the last fetch brought (FETCH_HEAD)."""
    local, remote = _out(["rev-parse", "HEAD"], root), _out(["rev-parse", "FETCH_HEAD"], root)
    behind = int(_out(["rev-list", "--count", "HEAD..FETCH_HEAD"], root) or 0)
    ahead = int(_out(["rev-list", "--count", "FETCH_HEAD..HEAD"], root) or 0)
    incoming = [ln for ln in _out(["log", "--format=%h %s", "-n", "15", "HEAD..FETCH_HEAD"], root).splitlines() if ln.strip()]
    return {"local": local[:8], "remote": remote[:8], "behind_n": behind, "ahead_n": ahead, "incoming": incoming,
            "remote_version": _version_of(_out(["show", "FETCH_HEAD:steltic_hub/__init__.py"], root)),
            "changed": _tracked_changes(root)}


def check() -> dict:
    """Is there a newer hub? -> {"behind": bool, "reason"?: str, ...}. Network: one git fetch (or one GET)."""
    root = source_root()
    if is_checkout(root):
        try:
            branch = _fetch(root)
        except UpdateError as e:
            return {"behind": False, "reason": str(e), "mode": "checkout"}
        d = _compare(root)
        d.update(mode="checkout", branch=branch, version=_version(), behind=d["behind_n"] > 0)
        if d["ahead_n"] and d["behind_n"]:
            d["reason"] = ("this checkout has %d commit(s) of its own that are not on GitHub -- update it with git "
                           "(git pull) rather than from here" % d["ahead_n"])
        return d
    import httpx
    try:
        r = httpx.get(RAW_INIT.format(branch=BRANCH), timeout=20.0, follow_redirects=True)
        r.raise_for_status()
    except Exception as e:                                        # noqa: BLE001
        return {"behind": False, "reason": "could not reach GitHub: %s" % e, "mode": "package"}
    rv = _version_of(r.text)
    return {"behind": bool(rv) and _vtuple(rv) > _vtuple(_version()), "remote_version": rv, "version": _version(),
            "mode": "package"}


# ---------------------------------------------------------------- the update
def update(log=print, reinstall_bundled=None) -> dict:
    """Bring the hub's code up to date. `reinstall_bundled(mod_ids, log)` rebuilds the environments of the
    bundled modules whose requirements changed (main.py passes the registry's update). Raises UpdateError
    with the reason when it will not (or cannot) update; nothing is left half-done in that case."""
    root = source_root()
    if not is_checkout(root):
        return _update_package(log)
    before = _out(["rev-parse", "HEAD"], root)
    log("[hub] checking %s for a newer hub (%s)" % (_remote(root), _branch(root)))
    _fetch(root, log)
    d = _compare(root)
    if d["behind_n"] == 0:
        log("[hub] already up to date (%s, hub %s)" % (d["local"], _version()))
        return {"updated": False, "restart_needed": False, "version": _version(), "commit": d["local"]}
    if d["ahead_n"]:
        raise UpdateError("this checkout (%s) has %d commit(s) of its own that are not on GitHub, and GitHub has %d "
                          "new one(s): the two have diverged, so the hub will not merge them for you. Update it with "
                          "git (git pull) in %s." % (d["local"], d["ahead_n"], d["behind_n"], root))
    log("[hub] %d new commit(s), hub %s -> %s:" % (d["behind_n"], _version(), d["remote_version"] or "?"))
    for ln in d["incoming"]:
        log("    " + ln)
    files = [f for f in _out(["diff", "--name-only", "HEAD", "FETCH_HEAD"], root).splitlines() if f.strip()]
    stashed = ""
    if d["changed"]:
        stashed = "steltic-hub self-update %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        log("[hub] you have edited %d tracked file(s) in this checkout: %s" % (len(d["changed"]), ", ".join(d["changed"][:8])
                                                                              + (" ..." if len(d["changed"]) > 8 else "")))
        r = _git(["stash", "push", "-m", stashed], root)
        if r.returncode != 0:
            raise UpdateError("could not put your edits aside (git stash): %s -- nothing was changed" % (r.stderr or r.stdout).strip()[-300:])
        log("[hub] your edits are kept aside as the stash \"%s\" (git stash list; git stash pop brings them back)" % stashed)
    r = _git(["merge", "--ff-only", "FETCH_HEAD"], root)
    for ln in (r.stdout + r.stderr).splitlines():
        if ln.strip():
            log(ln.rstrip())
    if r.returncode != 0:
        if stashed:
            _git(["stash", "pop"], root)
        raise UpdateError("the fast-forward to %s failed (see the log above) -- nothing was changed" % d["remote"])
    after = _out(["rev-parse", "HEAD"], root)
    new_version = _version_of((root / "steltic_hub" / "__init__.py").read_text(encoding="utf-8", errors="replace"))
    log("[hub] code updated %s -> %s (hub %s)" % (before[:8], after[:8], new_version or "?"))
    if "pyproject.toml" in files:
        _reinstall_hub(root, log)
    bundled = sorted({m.group(1) for f in files
                      for m in [re.match(r"steltic_hub/catalog/([^/]+)/(?:requirements\.txt|pyproject\.toml)$", f)] if m})
    if bundled and reinstall_bundled:
        log("[hub] requirements changed for bundled module(s) %s -- reinstalling their environments" % ", ".join(bundled))
        reinstall_bundled(bundled, log)
    log("[hub] done. Restart the hub to run the new version.")
    return {"updated": True, "restart_needed": True, "from": before[:8], "to": after[:8], "version": new_version,
            "stashed": stashed, "files": len(files)}


def _uv(log) -> str:
    uv = envs.uv_path()
    if not uv:
        uv = envs.ensure_uv(log=log)
    return uv


def _reinstall_hub(root: pathlib.Path, log) -> None:
    """pyproject.toml changed: refresh the hub's own environment (new dependencies) -- as the launcher would
    on its next start -- and stamp it so the launcher does not do it again."""
    log("[hub] pyproject.toml changed -- refreshing the hub environment")
    rc = envs._stream([_uv(log), "pip", "install", "--python", sys.executable, "-e", str(root)], log=log)
    if rc != 0:
        raise UpdateError("the code is updated, but refreshing the hub environment failed (see the log above). "
                          "Close Steltic and start it with windows\\Steltic.bat -Reinstall (or unix/steltic.sh --reinstall).")
    stamp = pathlib.Path(sys.prefix) / "steltic-hub.installed"
    if stamp.exists():
        try:
            stamp.write_text(datetime.datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
        except OSError:
            pass


def _update_package(log) -> dict:
    log("[hub] this hub is an installed package (no git checkout at %s): upgrading it from %s" % (source_root(), REPO))
    spec = "steltic-hub @ git+%s@%s" % (REPO, BRANCH)
    rc = envs._stream([_uv(log), "pip", "install", "--python", sys.executable, "--upgrade", spec], log=log)
    if rc != 0:
        raise UpdateError("the upgrade failed (see the log above)")
    log("[hub] done. Restart the hub to run the new version.")
    return {"updated": True, "restart_needed": True, "version": "", "mode": "package"}
