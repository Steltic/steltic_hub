"""The hub on its own Modules page: Check for updates / Update, like a module.

A real git "GitHub" (a bare repo) and a real checkout of it stand in for github.com/Steltic/steltic_hub and the
user's folder; the hub's package path is pointed at the checkout.

    python -m pytest tests/test_selfupdate.py -q
"""
import json
import pathlib
import subprocess

import pytest

from steltic_hub import config, selfupdate as SU


def _g(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _commit(repo, files: dict, msg: str):
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _g(repo, "add", "-A")
    _g(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """-> (upstream work tree that pushes to the 'GitHub' bare repo, the user's checkout)."""
    bare = tmp_path / "github.git"
    _g(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    up = tmp_path / "upstream"
    _g(tmp_path, "clone", "-q", str(bare), str(up))
    _g(up, "checkout", "-q", "-b", "main")
    _commit(up, {"pyproject.toml": "[project]\nname='steltic-hub'\n", "steltic_hub/__init__.py": '__version__ = "0.2.2"\n',
                 "steltic_hub/main.py": "# hub\n", "steltic_hub/catalog/steltic_admin/requirements.txt": "fastapi\n"}, "first")
    _g(up, "push", "-q", "origin", "main")
    co = tmp_path / "steltic_hub_checkout"
    _g(tmp_path, "clone", "-q", str(bare), str(co))
    monkeypatch.setattr(config, "PKG", co / "steltic_hub")
    monkeypatch.setattr(SU, "_version", lambda: SU._version_of((co / "steltic_hub" / "__init__.py").read_text()))
    return up, co


def _release(up, files, msg="new"):
    _commit(up, files, msg)
    _g(up, "push", "-q", "origin", "main")


def test_status_says_where_the_code_is(hub):
    up, co = hub
    st = SU.status()
    assert st["mode"] == "checkout" and st["source"] == str(co) and st["branch"] == "main"
    assert st["commit"] == _g(co, "rev-parse", "--short=8", "HEAD") and st["changed"] == []
    assert st["version"] == "0.2.2"


def test_check_for_updates(hub):
    up, co = hub
    d = SU.check()
    assert d["behind"] is False and d["behind_n"] == 0
    _release(up, {"steltic_hub/__init__.py": '__version__ = "0.2.3"\n'}, "hub 0.2.3: Hub card on the Modules page")
    d = SU.check()
    assert d["behind"] is True and d["behind_n"] == 1 and d["remote_version"] == "0.2.3"
    assert any("Hub card on the Modules page" in ln for ln in d["incoming"])
    assert _g(co, "rev-parse", "HEAD") != _g(up, "rev-parse", "HEAD"), "a check changes nothing"


def test_update_fast_forwards_and_keeps_the_users_edits(hub):
    up, co = hub
    _release(up, {"steltic_hub/__init__.py": '__version__ = "0.2.3"\n', "steltic_hub/main.py": "# hub 0.2.3\n"})
    (co / "steltic_hub" / "local.txt").write_text("mine")                 # untracked: never touched
    (co / "steltic_hub" / "__init__.py").write_text('__version__ = "0.2.2"  # my edit\n')
    assert SU.status()["changed"] == ["steltic_hub/__init__.py"]
    lines, called = [], []
    r = SU.update(log=lines.append, reinstall_bundled=lambda ids, log: called.append(ids))
    assert r["updated"] and r["restart_needed"] and r["version"] == "0.2.3" and r["stashed"]
    assert _g(co, "rev-parse", "HEAD") == _g(up, "rev-parse", "HEAD")
    assert (co / "steltic_hub" / "main.py").read_text() == "# hub 0.2.3\n"
    assert (co / "steltic_hub" / "local.txt").read_text() == "mine"
    assert r["stashed"] in _g(co, "stash", "list"), "the edit is kept aside, recoverable"
    assert called == [], "no bundled requirements changed"
    assert any("Restart the hub" in ln for ln in lines)
    # a second press: nothing to do
    r2 = SU.update(log=lines.append)
    assert r2["updated"] is False and r2["restart_needed"] is False


def test_a_checkout_with_its_own_commits_is_not_touched(hub):
    up, co = hub
    _release(up, {"steltic_hub/main.py": "# upstream\n"})
    _commit(co, {"steltic_hub/mine.py": "# local work\n"}, "my own commit")
    head = _g(co, "rev-parse", "HEAD")
    d = SU.check()
    assert d["behind"] and d["ahead_n"] == 1 and "commit(s) of its own" in d["reason"]
    with pytest.raises(SU.UpdateError, match="diverged"):
        SU.update(log=lambda s: None)
    assert _g(co, "rev-parse", "HEAD") == head


def test_new_dependencies_reinstall_the_hub_and_changed_bundled_modules(hub, monkeypatch):
    up, co = hub
    _release(up, {"pyproject.toml": "[project]\nname='steltic-hub'\ndependencies=['httpx']\n",
                  "steltic_hub/catalog/steltic_admin/requirements.txt": "fastapi\nhttpx\n"})
    cmds, called = [], []
    monkeypatch.setattr(SU.envs, "_stream", lambda cmd, **kw: (cmds.append([str(c) for c in cmd]), 0)[1])
    monkeypatch.setattr(SU.envs, "uv_path", lambda: "uv")
    r = SU.update(log=lambda s: None, reinstall_bundled=lambda ids, log: called.append(ids))
    assert r["updated"]
    assert cmds and cmds[0][:3] == ["uv", "pip", "install"] and cmds[0][-2:] == ["-e", str(co)]
    assert called == [["steltic_admin"]]


def test_a_failed_reinstall_says_how_to_recover(hub, monkeypatch):
    up, co = hub
    _release(up, {"pyproject.toml": "[project]\nname='steltic-hub'\ndependencies=['nope']\n"})
    monkeypatch.setattr(SU.envs, "_stream", lambda cmd, **kw: 1)
    monkeypatch.setattr(SU.envs, "uv_path", lambda: "uv")
    with pytest.raises(SU.UpdateError, match="Reinstall"):
        SU.update(log=lambda s: None)


def test_an_installed_package_checks_github_and_upgrades_with_pip(tmp_path, monkeypatch):
    pkg = tmp_path / "site-packages" / "steltic_hub"
    pkg.mkdir(parents=True)
    monkeypatch.setattr(config, "PKG", pkg)
    monkeypatch.setattr(SU, "_version", lambda: "0.2.2")

    class R:
        text = '__version__ = "0.2.4"\n'

        def raise_for_status(self):
            pass
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    d = SU.check()
    assert d["mode"] == "package" and d["behind"] and d["remote_version"] == "0.2.4"
    cmds = []
    monkeypatch.setattr(SU.envs, "_stream", lambda cmd, **kw: (cmds.append([str(c) for c in cmd]), 0)[1])
    monkeypatch.setattr(SU.envs, "uv_path", lambda: "uv")
    r = SU.update(log=lambda s: None)
    assert r["restart_needed"] and "--upgrade" in cmds[0] and any("git+https://github.com/Steltic/steltic_hub@main" in c for c in cmds[0])


def test_the_hub_routes(monkeypatch):
    from fastapi.testclient import TestClient
    from steltic_hub import main as M
    monkeypatch.setattr(SU, "status", lambda: {"version": "0.2.2", "mode": "checkout", "commit": "abc12345", "changed": []})
    monkeypatch.setattr(SU, "check", lambda: {"behind": True, "remote_version": "0.2.3"})

    def fake_update(log, reinstall_bundled=None):
        log("[hub] code updated")
        return {"updated": True, "restart_needed": True, "version": "0.2.3"}
    monkeypatch.setattr(SU, "update", fake_update)
    with TestClient(M.app) as c:
        st = c.get("/api/hub/status").json()
        assert st["mode"] == "checkout" and st["commit"] == "abc12345" and "pid" in st and "stale" in st
        assert c.get("/api/hub/check-update").json()["behind"] is True
        body = c.post("/api/hub/update").text
        events = [json.loads(l[5:]) for l in body.splitlines() if l.startswith("data:")]
        assert events[-1]["type"] == "done" and events[-1]["ok"] and events[-1]["result"]["restart_needed"]

    def refusing(log, reinstall_bundled=None):
        raise SU.UpdateError("this checkout has 1 commit(s) of its own")
    monkeypatch.setattr(SU, "update", refusing)
    with TestClient(M.app) as c:
        events = [json.loads(l[5:]) for l in c.post("/api/hub/update").text.splitlines() if l.startswith("data:")]
        assert events[0] == {"type": "error", "text": "this checkout has 1 commit(s) of its own"} and events[-1]["ok"] is False


def test_the_modules_page_has_a_hub_card():
    js = (pathlib.Path(__file__).resolve().parents[1] / "steltic_hub" / "ui" / "app.js").read_text(encoding="utf-8")
    assert "grid.append(hubCard())" in js and "/api/hub/check-update" in js and "/api/hub/update" in js
