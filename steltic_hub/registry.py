"""Installed modules: git checkout + venv + manifest resolution.

Update policy: `git fetch && git reset --hard origin/<branch>` in the module's own checkout,
then re-install into its own venv. Because the checkout is private to the hub (under the data
dir, not the user's own clones), a hard reset is safe and a half-finished update can always be
repaired by reinstalling.

Manifest precedence, highest first:
  1. steltic_module.json in the checkout   -- the module describes itself; new tabs/fields ship
                                              with the module and need no hub release
  2. the hub's bundled catalog             -- so today's four repos work unmodified

LOCAL SOURCES (testing before you push):
A module can be pointed at a working copy on disk instead of a git URL. The hub then uses that
directory AS the checkout -- it is never copied, reset or written to -- and installs it with
`pip install -e .`, so edits to module code are live on the next run and edits to the module's
steltic_module.json show up on the next catalog reload. Nothing needs to be committed, let alone
pushed, to be exercised end to end.
"""
from __future__ import annotations
import json, shutil, subprocess, sys, time, pathlib
from . import config, envs
from .manifest import Manifest, ManifestError, MANIFEST_NAME, load_catalog

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class RegistryError(RuntimeError):
    pass


def _git(args: list[str], cwd=None, log=print) -> int:
    log("$ git " + " ".join(args))
    try:
        proc = subprocess.Popen(["git", *args], cwd=cwd and str(cwd), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1, errors="replace",
                                creationflags=_NO_WINDOW)
    except OSError as e:
        log(f"[hub] git is not available ({e}) -- install Git for Windows (git-scm.com) and retry")
        return 127
    assert proc.stdout
    for line in proc.stdout:
        log(line.rstrip())
    return proc.wait()


def _git_out(args: list[str], cwd) -> str:
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                              text=True, timeout=30, creationflags=_NO_WINDOW).stdout.strip()
    except Exception:
        return ""


def _rmtree(path: pathlib.Path):
    """rmtree that survives Windows read-only files (git objects are read-only)."""
    import stat

    def onerror(func, p, exc_info):
        try:
            import os
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    shutil.rmtree(path, onerror=onerror)


class Registry:
    def __init__(self):
        self.catalog: dict[str, Manifest] = load_catalog(config.CATALOG_DIR)
        self.state: dict = self._load_state()
        self._status_cache: dict[str, tuple[float, dict]] = {}

    # ---------------- state ----------------
    def _load_state(self) -> dict:
        if config.STATE_FILE.exists():
            try:
                st = json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(st, dict):
                    st.setdefault("installed", {})
                    st.setdefault("custom_catalog", {})
                    st.setdefault("local_sources", {})
                    st.setdefault("names", {})
                    return st
            except Exception:
                pass
        return {"installed": {}, "custom_catalog": {}, "local_sources": {}, "names": {}}

    def save(self):
        tmp = config.STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        tmp.replace(config.STATE_FILE)

    # ---------------- names ----------------
    def name(self, mod_id: str) -> str:
        """What the user calls this module: their own name if they set one, else the manifest's."""
        custom = (self.state.get("names") or {}).get(mod_id)
        if custom:
            return custom
        try:
            return self.manifest(mod_id).name
        except RegistryError:
            return mod_id

    def rename(self, mod_id: str, name: str) -> str:
        """Set (or, with an empty name, clear) the user's display name for a module."""
        name = " ".join((name or "").split())[:60]
        names = self.state.setdefault("names", {})
        if name:
            names[mod_id] = name
        else:
            names.pop(mod_id, None)
        self.save()
        return self.name(mod_id)

    # ---------------- paths ----------------
    def local_source(self, mod_id: str) -> pathlib.Path | None:
        p = self.state.get("local_sources", {}).get(mod_id)
        return pathlib.Path(p) if p else None

    def bundled_dir(self, mod_id: str) -> pathlib.Path | None:
        """A module that ships inside the hub (source.bundled) lives under the catalog folder."""
        m = self.catalog.get(mod_id)
        if m and m.bundled:
            return config.CATALOG_DIR / m.bundled
        return None

    def checkout(self, mod_id: str) -> pathlib.Path:
        """Where this module's code lives: a linked working copy, the hub's own clone, or -- for a
        bundled module -- the folder inside the hub package."""
        return self.local_source(mod_id) or self.bundled_dir(mod_id) or (config.MODULES_DIR / mod_id)

    def module_root(self, mod_id: str) -> pathlib.Path:
        """The folder that holds the module itself: the checkout, or its declared `source.subdir`
        inside it. A linked working copy may point at either the repo or the subfolder."""
        co = self.checkout(mod_id)
        try:
            sub = self.manifest(mod_id).subdir
        except RegistryError:
            sub = ""
        if sub and (co / sub).is_dir():
            return co / sub
        return co

    def is_installed(self, mod_id: str) -> bool:
        return self.checkout(mod_id).exists() and envs.env_ready(mod_id)

    def link(self, mod_id: str, path: str) -> pathlib.Path:
        """Point a module at a working copy on disk. Used to test changes before pushing."""
        d = pathlib.Path(path).expanduser().resolve()
        if not d.is_dir():
            raise RegistryError(f"{d} is not a directory")
        known = {m.id for m in self.known()}
        local_manifest = d / MANIFEST_NAME
        if mod_id not in known:
            # Linking a directory that describes itself also REGISTERS it, so a brand-new module
            # can be developed from an uncommitted folder with no round trip through GitHub.
            if not local_manifest.exists():
                raise RegistryError(
                    f"{mod_id!r} is not a known module and {d} has no {MANIFEST_NAME} to describe it")
            try:
                raw = json.loads(local_manifest.read_text(encoding="utf-8-sig"))
                m = Manifest.parse(raw, origin=str(local_manifest))
            except (json.JSONDecodeError, ManifestError) as e:
                raise RegistryError(f"{local_manifest} is not a valid manifest: {e}")
            if m.id != mod_id:
                raise RegistryError(
                    f"{local_manifest.name} declares id {m.id!r}, but you asked to link {mod_id!r}")
            self.state.setdefault("custom_catalog", {})[mod_id] = raw
        self.state.setdefault("local_sources", {})[mod_id] = str(d)
        self.save()
        self._status_cache.pop(mod_id, None)
        return d

    def unlink(self, mod_id: str):
        """Go back to the hub's own clone. The working copy is left exactly as it was."""
        self.state.get("local_sources", {}).pop(mod_id, None)
        self.save()
        self._status_cache.pop(mod_id, None)

    # ---------------- manifests ----------------
    def manifest(self, mod_id: str) -> Manifest:
        """Checkout manifest wins over the bundled catalog."""
        co = self.checkout(mod_id)
        local = co / MANIFEST_NAME
        if local.exists():
            try:
                m = Manifest.load(local)
                if m.id == mod_id:
                    return m
            except ManifestError:
                pass                      # fall through to catalog; UI surfaces the warning
        m = self.catalog.get(mod_id) or self._custom(mod_id)
        if not m:
            raise RegistryError(f"no manifest for module {mod_id!r}")
        return m

    def manifest_problem(self, mod_id: str) -> str:
        """Why a checkout's own manifest was ignored, if it was."""
        local = self.checkout(mod_id) / MANIFEST_NAME
        if not local.exists():
            return ""
        try:
            m = Manifest.load(local)
        except ManifestError as e:
            return str(e)
        return "" if m.id == mod_id else f"declares id {m.id!r}, expected {mod_id!r}"

    def _custom(self, mod_id: str) -> Manifest | None:
        raw = self.state.get("custom_catalog", {}).get(mod_id)
        if not raw:
            return None
        try:
            return Manifest.parse(raw, origin="custom")
        except ManifestError:
            return None

    def known(self) -> list[Manifest]:
        out: dict[str, Manifest] = dict(self.catalog)
        for mid, raw in self.state.get("custom_catalog", {}).items():
            try:
                out[mid] = Manifest.parse(raw, origin="custom")
            except ManifestError:
                continue
        for mid in list(out):
            if self.checkout(mid).exists():
                try:
                    out[mid] = self.manifest(mid)
                except RegistryError:
                    pass
        return sorted(out.values(), key=lambda m: (m.order, m.name))

    # ---------------- lifecycle ----------------
    def install(self, mod_id: str, log=print, force_env: bool = False) -> Manifest:
        m = self.manifest(mod_id)
        local = self.local_source(mod_id)
        self._status_cache.pop(mod_id, None)
        if local:
            # A linked working copy is the source of truth: never fetched, never reset, never
            # written to. Only the venv is (re)built around it.
            if not local.is_dir():
                raise RegistryError(f"linked source {local} has gone away -- unlink or restore it")
            log(f"[hub] using linked working copy {local} (no clone, no fetch)")
            m = self.manifest(mod_id)
            envs.create_env(m, self.module_root(mod_id), log=log, force=force_env)
            self.state.setdefault("installed", {})[mod_id] = {
                "installed_at": time.time(), "commit": _git_out(["rev-parse", "HEAD"], local),
                "branch": _git_out(["rev-parse", "--abbrev-ref", "HEAD"], local) or "(local)",
                "git": str(local), "local": True,
            }
            self.save()
            log(f"[hub] {m.name} ready from your working copy")
            return m
        bundled = self.bundled_dir(mod_id)
        if bundled:
            # The code is part of the hub itself: nothing to fetch, only the environment to build.
            if not bundled.is_dir():
                raise RegistryError(f"bundled module folder {bundled} is missing -- reinstall the hub")
            log(f"[hub] {m.name} ships with the hub ({bundled}); building its environment")
            envs.create_env(m, self.module_root(mod_id), log=log, force=force_env)
            self.state.setdefault("installed", {})[mod_id] = {
                "installed_at": time.time(), "commit": "", "branch": "(bundled)", "git": "", "bundled": True,
            }
            self.save()
            log(f"[hub] {m.name} ready")
            return m
        if not m.git:
            raise RegistryError(f"module {mod_id!r} has no git source")
        co = self.checkout(mod_id)
        if co.exists() and (co / ".git").exists():
            self._pull(m, co, log=log)
        else:
            if co.exists():
                log(f"[hub] {co} is not a git checkout -- replacing it")
                _rmtree(co)
            log(f"[hub] cloning {m.git} ({m.branch})")
            config.MODULES_DIR.mkdir(parents=True, exist_ok=True)
            rc = _git(["clone", "--depth", "1", "--branch", m.branch, m.git, str(co)], log=log)
            if rc != 0:
                if co.exists():
                    _rmtree(co)
                raise RegistryError(f"clone failed for {mod_id} -- check the URL, the branch and your network")
        # the checkout may ship a newer manifest than the catalog: re-read before building the env
        m = self.manifest(mod_id)
        envs.create_env(m, self.module_root(mod_id), log=log, force=force_env)
        self.state.setdefault("installed", {})[mod_id] = {
            "installed_at": time.time(), "commit": _git_out(["rev-parse", "HEAD"], co),
            "branch": m.branch, "git": m.git,
        }
        self.save()
        log(f"[hub] {m.name} ready")
        return m

    def _pull(self, m: Manifest, co: pathlib.Path, log=print):
        log(f"[hub] updating {m.id}")
        if _git(["fetch", "--depth", "1", "origin", m.branch], cwd=co, log=log) != 0:
            raise RegistryError(f"fetch failed for {m.id} -- check your network")
        # FETCH_HEAD is what `fetch origin <branch>` is guaranteed to update, whatever the
        # remote-tracking configuration of a shallow clone looks like.
        if _git(["reset", "--hard", "FETCH_HEAD"], cwd=co, log=log) != 0:
            raise RegistryError(f"reset failed for {m.id}")

    def update(self, mod_id: str, log=print) -> Manifest:
        self._status_cache.pop(mod_id, None)
        if self.local_source(mod_id):
            # Reinstall in place: picks up new dependencies and a rewritten manifest without
            # touching your git state. `pip install -e .` means code edits were already live.
            log("[hub] linked working copy -- reinstalling in place, not pulling")
            m = self.manifest(mod_id)
            if not envs.env_ready(mod_id):
                return self.install(mod_id, log=log)
            envs.install_into(m, self.module_root(mod_id), log=log)
            self.state.setdefault("installed", {}).setdefault(mod_id, {})["updated_at"] = time.time()
            self.save()
            log(f"[hub] {m.name} reinstalled from your working copy")
            return m
        if self.bundled_dir(mod_id):
            log("[hub] bundled module -- reinstalling its environment (the code updates with the hub)")
            m = self.manifest(mod_id)
            if not envs.env_ready(mod_id):
                return self.install(mod_id, log=log)
            envs.install_into(m, self.module_root(mod_id), log=log)
            self.state.setdefault("installed", {}).setdefault(mod_id, {})["updated_at"] = time.time()
            self.save()
            log(f"[hub] {m.name} reinstalled")
            return m
        if not self.checkout(mod_id).exists() or not envs.env_ready(mod_id):
            return self.install(mod_id, log=log)
        m = self.manifest(mod_id)
        self._pull(m, self.checkout(mod_id), log=log)
        m = self.manifest(mod_id)                 # manifest may have changed in the update
        envs.install_into(m, self.module_root(mod_id), log=log)
        rec = self.state.setdefault("installed", {}).setdefault(mod_id, {})
        rec["commit"] = _git_out(["rev-parse", "HEAD"], self.checkout(mod_id))
        rec["updated_at"] = time.time()
        rec["branch"] = m.branch
        self.save()
        log(f"[hub] {m.name} updated")
        return m

    def remove(self, mod_id: str, log=print):
        self._status_cache.pop(mod_id, None)
        if self.local_source(mod_id):
            # Never delete a working copy the user pointed us at.
            log("[hub] unlinking the working copy (your files are untouched)")
            self.unlink(mod_id)
        elif self.bundled_dir(mod_id):
            log("[hub] bundled module -- removing its environment only (the code is part of the hub)")
        else:
            co = self.checkout(mod_id)
            if co.exists():
                _rmtree(co)
        envs.remove_env(mod_id)
        self.state.get("installed", {}).pop(mod_id, None)
        self.save()
        log(f"[hub] removed {mod_id}")

    def add_custom(self, raw: dict) -> Manifest:
        """Register a third-party module from a pasted manifest. This is the whole
        'add a new module' path: no hub release, no code change."""
        m = Manifest.parse(raw, origin="custom")
        if m.id in self.catalog:
            raise RegistryError(f"{m.id!r} is already a built-in module")
        if not m.git and not self.local_source(m.id):
            raise RegistryError("the manifest needs a source.url (or link a local folder instead)")
        self.state.setdefault("custom_catalog", {})[m.id] = raw
        self.save()
        return m

    def remove_custom(self, mod_id: str):
        """Forget a registered third-party module: its registration, link, install record and
        environment. A linked working copy on disk is never touched."""
        if mod_id in self.catalog:
            raise RegistryError(f"{mod_id!r} is a built-in module")
        if not self.local_source(mod_id):
            co = config.MODULES_DIR / mod_id
            if co.exists():
                _rmtree(co)
        envs.remove_env(mod_id)
        for key in ("custom_catalog", "local_sources", "installed", "names"):
            self.state.get(key, {}).pop(mod_id, None)
        self.save()
        self._status_cache.pop(mod_id, None)

    # ---------------- status ----------------
    def status(self, mod_id: str) -> dict:
        co = self.checkout(mod_id)
        rec = self.state.get("installed", {}).get(mod_id, {})
        local = self.local_source(mod_id)
        bundled = self.bundled_dir(mod_id) is not None
        st = {
            "installed": (co.exists() and envs.env_ready(mod_id)) if bundled else co.exists(),
            "env_ready": envs.env_ready(mod_id),
            "bundled": bundled,
            "commit": (rec.get("commit") or "")[:8],
            "branch": rec.get("branch", ""),
            "updated_at": rec.get("updated_at") or rec.get("installed_at"),
            "has_local_manifest": (co / MANIFEST_NAME).exists(),
            "manifest_problem": self.manifest_problem(mod_id),
            "linked": str(local) if local else "",
            "dirty": False,
            "custom": mod_id not in self.catalog,
        }
        if local:
            # `git status` on a large working copy is not free: remember it for a while.
            hit = self._status_cache.get(mod_id)
            if hit and time.time() - hit[0] < 30:
                st["dirty"] = hit[1].get("dirty", False)
            else:
                st["dirty"] = bool(_git_out(["status", "--porcelain"], local))
                self._status_cache[mod_id] = (time.time(), {"dirty": st["dirty"]})
        if st["env_ready"]:
            try:
                st["env"] = envs.verify_env(self.manifest(mod_id))
            except Exception as e:
                st["env"] = {"ready": False, "reason": str(e)}
        return st

    def reload_catalog(self):
        """Re-read the bundled catalog without restarting. Manifests in a linked checkout are
        already read per request; this picks up edits to the hub's own catalog/*.json."""
        self.catalog = load_catalog(config.CATALOG_DIR)
        self.state = self._load_state()
        self._status_cache.clear()

    def check_update(self, mod_id: str) -> dict:
        """Is the remote ahead? Cheap enough to run on demand from the Modules tab."""
        if self.local_source(mod_id):
            return {"behind": False, "reason": "linked to a local working copy"}
        if self.bundled_dir(mod_id):
            return {"behind": False, "reason": "ships with the hub -- updates with it"}
        co = self.checkout(mod_id)
        if not co.exists():
            return {"behind": False, "reason": "not installed"}
        m = self.manifest(mod_id)
        try:
            r = subprocess.run(["git", "fetch", "--depth", "1", "origin", m.branch],
                               cwd=str(co), capture_output=True, text=True, timeout=60,
                               creationflags=_NO_WINDOW)
            if r.returncode != 0:
                return {"behind": False, "reason": f"fetch failed: {(r.stderr or '').strip()[:200]}"}
        except Exception as e:
            return {"behind": False, "reason": f"fetch failed: {e}"}
        local = _git_out(["rev-parse", "HEAD"], co)
        remote = _git_out(["rev-parse", "FETCH_HEAD"], co)
        return {"behind": bool(local and remote and local != remote),
                "local": local[:8], "remote": remote[:8]}
