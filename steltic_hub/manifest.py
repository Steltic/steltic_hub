"""The module contract.

A Steltic module is any git repo that the hub can (a) install into a venv and (b) drive.
It describes itself in `steltic_module.json` at its repo root. The hub ships a *catalog*
of manifests for the modules that exist today, so none of those repos has to change to be
usable -- but a manifest committed to the repo always wins, which is how a module updates
its own UI without the hub being re-released.

Nothing in the hub branches on a module id. If you find yourself writing `if id ==
"steltic"` somewhere, the manifest is missing a field.
"""
from __future__ import annotations
import json, pathlib, sys
from dataclasses import dataclass, field
from typing import Any

SCHEMA = 1
MANIFEST_NAME = "steltic_module.json"

FIELD_TYPES = ("text", "textarea", "number", "select", "checkbox", "file", "files", "project",
               "attachments")


class ManifestError(ValueError):
    pass


@dataclass
class Field:
    """One control in a hub-rendered input tab."""
    id: str
    type: str = "text"              # see FIELD_TYPES
    label: str = ""
    placeholder: str = ""
    help: str = ""
    required: bool = False
    default: Any = None             # may contain templates ({job_dir}, {out.<module>} ...)
    options: list = field(default_factory=list)   # [{value,label}] for select
    rows: int = 6
    accept: str = ""                # for file inputs
    arg: str | None = None          # CLI flag this field maps to (cli runs only)
    arg_style: str = "flag"         # flag | positional | flag-if-true
    # select only: fetch something from the module's own server when a value is picked and
    # drop it into another field -- {"path": "/api/example/{value}", "key": "brief",
    # "target": "brief"}. This is how a select of example briefs fills the brief textarea
    # without the hub knowing what an "example" is.
    fills: dict = field(default_factory=dict)
    # attachments only: text-like files (.txt/.md/.pdf) are read in the browser and appended
    # to this field; images stay in the attachments value as [{name,type,data_url}].
    text_target: str = ""
    max_files: int = 0              # 0 = no limit (this is the user's own PC)
    max_mb: float = 0.0

    @staticmethod
    def parse(d: dict) -> "Field":
        if not isinstance(d, dict) or not d.get("id"):
            raise ManifestError(f"field needs an id: {d!r}")
        known = {k: v for k, v in d.items() if k in Field.__dataclass_fields__}
        f = Field(**known)
        if f.type not in FIELD_TYPES:
            raise ManifestError(f"field {f.id}: unknown type {f.type!r}")
        if f.arg_style not in ("flag", "positional", "flag-if-true"):
            raise ManifestError(f"field {f.id}: unknown arg_style {f.arg_style!r}")
        if f.fills and not isinstance(f.fills, dict):
            raise ManifestError(f"field {f.id}: fills must be an object")
        f.label = f.label or f.id.replace("_", " ").capitalize()
        return f


RETRY_CAP = 10          # an automatic retry is a work-around, not a schedule: ten attempts is already generous


def _parse_retry(d: Any) -> dict:
    """Validate and normalise `run.retry`; {} means this run never retries.

    A malformed block is refused rather than ignored, because its symptom would otherwise be a run
    that loops for an hour (or one that silently never retries), and neither is something the user
    would connect back to a typo in a manifest.
    """
    if not d:
        return {}
    if not isinstance(d, dict):
        raise ManifestError("run.retry must be an object {on, max, then_set}")
    on = d.get("on", "native_crash")
    if isinstance(on, list):
        # bool is an int in Python, and `[true]` here is a mistake, not an exit code
        if not on or any(isinstance(c, bool) or not isinstance(c, int) for c in on):
            raise ManifestError('run.retry.on must be "native_crash" or a non-empty list of exit codes')
        on = list(on)
    elif on != "native_crash":
        raise ManifestError(f'run.retry.on {on!r} must be "native_crash" or a list of exit codes')
    mx = d.get("max", 2)
    if isinstance(mx, bool) or not isinstance(mx, int):
        raise ManifestError(f"run.retry.max {mx!r} must be a whole number of attempts")
    if not 1 <= mx <= RETRY_CAP:
        raise ManifestError(f"run.retry.max {mx} must be between 1 and {RETRY_CAP}")
    then_set = d.get("then_set") or {}
    if not isinstance(then_set, dict):
        raise ManifestError("run.retry.then_set must be an object of field id -> value")
    if then_set and mx < 3:
        # The first retry repeats the command unchanged; then_set is applied from the third attempt
        # (see run_cli). With max 2 it would be read, accepted and never used.
        raise ManifestError(f"run.retry.then_set needs max >= 3 (it applies from the third attempt), not {mx}")
    return {"on": on, "max": mx, "then_set": then_set}


@dataclass
class Run:
    """How a tab actually starts work.

    kind=cli   -> spawn `<module venv python> command...` in the job folder, stream stdout.
    kind=http  -> call the module's own server; `stream` picks plain JSON or SSE passthrough.
    """
    kind: str = "cli"
    command: list = field(default_factory=list)
    cwd: str = "{job_dir}"
    method: str = "POST"
    path: str = "/api/run"
    body: dict = field(default_factory=dict)
    stream: str = "sse"             # sse | json | lines
    env: dict = field(default_factory=dict)
    label: str = "Run"
    # http only: how to ask the module server to stop this run. {"method", "path", "body"}
    # with the same templates as `body`. Without it Stop closes the stream, which the module
    # servers already treat as a stop request.
    cancel: dict = field(default_factory=dict)
    # Copy or unpack inputs into place before running: [{"from": "...", "to": "..."}]. `from`
    # may be a folder (its contents are copied), a .zip (extracted, a single wrapping folder is
    # flattened) or a file. A `from` that is empty, missing, or already inside `to` is skipped.
    # This is how a project folder gathers another module's output without either module
    # knowing about the other.
    stage: list = field(default_factory=list)
    # What to tell the user when the process dies in native code (a bare NTSTATUS / signal exit, no
    # traceback) -- e.g. "Convert again: it resumes from the last finished chunk". The hub explains
    # the code; the module explains the next step, because only it knows whether a run resumes.
    crash_hint: str = ""
    # Press the button again by ourselves when the process dies the way `on` describes:
    # {"on": "native_crash" | [exit codes], "max": total attempts, "then_set": {field: value}}.
    # Only a module knows whether repeating a run is safe (the converter resumes from its last
    # finished chunk, so a retry repeats at most one window), so the manifest asks for it -- the
    # hub never retries on its own. See _parse_retry for the shape and the limits.
    retry: dict = field(default_factory=dict)
    # cli only: this run talks to the user's model. The hub refuses to start it without a
    # connection and hands the connection to the process as STELTIC_LLM_* environment variables
    # (see README, "A CLI run that talks to the model"), the way it pushes the same connection to a
    # module server's /api/creds. The process streams what the model says back as event lines.
    llm: bool = False

    @staticmethod
    def parse(d: dict) -> "Run":
        known = {k: v for k, v in (d or {}).items() if k in Run.__dataclass_fields__}
        r = Run(**known)
        if r.kind not in ("cli", "http", "none"):
            raise ManifestError(f"run.kind {r.kind!r} must be cli, http or none")
        if r.llm not in (True, False):
            raise ManifestError("run.llm must be true or false")
        if r.llm and r.kind != "cli":
            raise ManifestError("run.llm is for kind cli (an http run gets the connection through the module's credentials path)")
        if r.kind == "cli" and not r.command:
            raise ManifestError("run.kind cli needs a command")
        if r.stream not in ("sse", "json", "lines"):
            raise ManifestError(f"run.stream {r.stream!r} must be sse, json or lines")
        if r.cancel and not isinstance(r.cancel, dict):
            raise ManifestError("run.cancel must be an object {method, path, body}")
        for s in r.stage or []:
            if not isinstance(s, dict) or not s.get("from") or not s.get("to"):
                raise ManifestError(f"run.stage entries need from and to: {s!r}")
        r.retry = _parse_retry(r.retry)
        if r.retry and r.kind != "cli":
            # Only run_cli owns the process it would have to spawn again; a retry asked for anywhere
            # else would be read, accepted and then quietly never happen.
            raise ManifestError(f"run.retry needs kind cli, not {r.kind!r}")
        return r


@dataclass
class Tab:
    """One tab inside a module.

    kind=form    hub renders `fields` and drives `run`  -- this is what turns a
                 results-only viewer into an app with inputs.
    kind=embed   iframe the module's own UI (zero hub work; always current).
    kind=viewers the viewer-bundle strip: sibling HTML files probed in a job folder.
    kind=files   browse/download the job folder.
    """
    id: str
    title: str = ""
    kind: str = "form"
    fields: list = field(default_factory=list)
    run: Run | None = None
    src: str = "/"                  # embed: path on the module server
    artifacts: list = field(default_factory=list)   # [{label, path, accent}] files under the output root
    links: list = field(default_factory=list)       # [{label, path}] paths on the module server, e.g. a download
    blurb: str = ""
    requires_optional: list = field(default_factory=list)   # optional-component groups (manifest `optional`) a run needs

    @staticmethod
    def parse(d: dict) -> "Tab":
        if not isinstance(d, dict) or not d.get("id"):
            raise ManifestError(f"tab needs an id: {d!r}")
        t = Tab(id=d["id"], title=d.get("title") or d["id"].capitalize(),
                kind=d.get("kind", "form"), src=d.get("src", "/"),
                artifacts=d.get("artifacts") or [], links=d.get("links") or [],
                blurb=d.get("blurb", ""), requires_optional=list(d.get("requires_optional") or []))
        if any(not isinstance(g, str) for g in t.requires_optional):
            raise ManifestError(f"tab {t.id}: requires_optional must be a list of optional-component group names")
        if t.kind not in ("form", "embed", "viewers", "files"):
            raise ManifestError(f"tab {t.id}: unknown kind {t.kind!r}")
        t.fields = [Field.parse(f) for f in (d.get("fields") or [])]
        ids = [f.id for f in t.fields]
        if len(ids) != len(set(ids)):
            raise ManifestError(f"tab {t.id}: duplicate field ids")
        t.run = Run.parse(d["run"]) if d.get("run") else None
        for f in t.fields:
            if f.fills and f.fills.get("target") and f.fills["target"] not in ids:
                raise ManifestError(f"tab {t.id}: field {f.id} fills unknown field {f.fills['target']!r}")
            if f.text_target and f.text_target not in ids:
                raise ManifestError(f"tab {t.id}: field {f.id} targets unknown field {f.text_target!r}")
        for k in (t.run.retry.get("then_set") if t.run else None) or {}:
            # a retry override on a field that does not exist would change nothing, silently
            if k not in ids:
                raise ManifestError(f"tab {t.id}: run.retry.then_set names unknown field {k!r}")
        return t


@dataclass
class Manifest:
    id: str
    name: str
    schema: int = SCHEMA
    blurb: str = ""
    accent: str = "#cfe3ff"
    order: int = 100
    version: str = ""
    homepage: str = ""
    # source
    git: str = ""
    branch: str = "main"
    subdir: str = ""
    bundled: str = ""                       # a module whose code ships INSIDE the hub: catalog/<bundled>/
    # environment
    python: str = ""                        # "" -> hub default
    install: list = field(default_factory=lambda: ["-e", "."])
    extra_requirements: list = field(default_factory=list)
    post_install: list = field(default_factory=list)   # argv lists run in the checkout after install
    # Heavy, rarely-needed dependencies stay OUT of the base install and are fetched on demand.
    # grokbot's Docling is the case in point: querying the corpus needs only stdlib + sqlite3,
    # while converting a PDF pulls a pinned ML stack nobody should wait for at install time.
    optional: dict = field(default_factory=dict)   # {group: {label, requirements, help, probe_import}}
    env_vars: dict = field(default_factory=dict)
    needs: list = field(default_factory=list)   # ids of modules whose checkout path we export
    # A module that needs the user's LLM connection declares where to push it. The hub holds ONE
    # connection in memory for every module, so the key is typed once and never written to disk.
    credentials: dict = field(default_factory=dict)   # {path, method}
    # server (optional -- CLI-only modules omit it)
    server: dict = field(default_factory=dict)  # {command, health, ready_log, cwd, env}
    # Where this module's outputs for a project land. Default = the hub's shared job folder;
    # modules with their own data layout (steltic keeps jobs under its DATA_DIR) declare theirs
    # so the hub can still serve their viewers and reports without patching the module.
    output_root: str = "{job_dir}"
    tabs: list = field(default_factory=list)
    viewers: list = field(default_factory=list) # [{id,label,accent,path}] for viewers tabs

    @property
    def has_server(self) -> bool:
        return bool(self.server.get("command"))

    @staticmethod
    def parse(d: dict, origin: str = "") -> "Manifest":
        if not isinstance(d, dict):
            raise ManifestError(f"{origin}: manifest must be a JSON object")
        try:
            schema = int(d.get("schema", SCHEMA))
        except (TypeError, ValueError):
            raise ManifestError(f"{origin}: schema must be an integer")
        if schema > SCHEMA:
            raise ManifestError(f"{origin}: manifest schema {schema} is newer than this hub "
                                f"(supports {SCHEMA}) -- update Steltic Hub")
        for req in ("id", "name"):
            if not d.get(req) or not isinstance(d.get(req), str):
                raise ManifestError(f"{origin}: manifest missing {req!r}")
        mid = d["id"]
        if not all(c.isalnum() or c in "_-" for c in mid):
            raise ManifestError(f"{origin}: module id {mid!r} may only contain letters, digits, _ and -")
        src = d.get("source") or {}
        env = d.get("env") or {}
        try:
            m = Manifest(
                id=mid, name=d["name"], schema=schema,
                blurb=d.get("blurb", ""), accent=d.get("accent", "#cfe3ff"),
                order=int(d.get("order", 100)), version=str(d.get("version", "")),
                homepage=d.get("homepage", "") or src.get("url", ""),
                git=src.get("url", ""), branch=src.get("branch") or "main", subdir=src.get("subdir", ""),
                bundled=str(src.get("bundled") or ""),
                python=env.get("python", ""), install=env.get("install", ["-e", "."]),
                extra_requirements=env.get("extra_requirements") or [],
                post_install=env.get("post_install") or [],
                optional=env.get("optional") or {},
                env_vars=d.get("env_vars") or {}, needs=d.get("needs") or [],
                credentials=d.get("credentials") or {},
                server=d.get("server") or {},
                output_root=(d.get("output") or {}).get("root", "{job_dir}") or "{job_dir}",
                viewers=d.get("viewers") or [],
            )
        except (TypeError, ValueError) as e:
            raise ManifestError(f"{origin}: {e}")
        if not isinstance(m.install, list) or not isinstance(m.needs, list):
            raise ManifestError(f"{origin}: env.install and needs must be lists")
        m.tabs = [Tab.parse(t) for t in (d.get("tabs") or [])]
        if not m.tabs:
            raise ManifestError(f"{origin}: module {m.id!r} declares no tabs")
        ids = [t.id for t in m.tabs]
        if len(ids) != len(set(ids)):
            raise ManifestError(f"{origin}: duplicate tab ids in {m.id!r}")
        for t in m.tabs:
            if t.kind == "form" and not t.run:
                raise ManifestError(f"{origin}: tab {t.id!r} is a form with nothing to run")
            if t.run and t.run.kind == "http" and not m.has_server:
                raise ManifestError(f"{origin}: tab {t.id!r} runs over http but the module has no server")
            if t.kind == "embed" and not m.has_server:
                raise ManifestError(f"{origin}: tab {t.id!r} embeds a server the module does not declare")
            for g in t.requires_optional:
                if g not in (m.optional or {}):
                    raise ManifestError(f"{origin}: tab {t.id!r} requires optional component {g!r}, which env.optional does not declare")
        if m.id in m.needs:
            raise ManifestError(f"{origin}: module {m.id!r} cannot need itself")
        if m.bundled and not all(c.isalnum() or c in "_-" for c in m.bundled):
            raise ManifestError(f"{origin}: source.bundled must be a plain folder name")
        req = m.server.get("requires") if isinstance(m.server, dict) else None
        if req is not None and not isinstance(req, list):
            raise ManifestError(f"{origin}: server.requires must be a list of module ids")
        return m

    @staticmethod
    def load(path: pathlib.Path) -> "Manifest":
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))    # -sig: a Windows editor's BOM is not an error
        except (json.JSONDecodeError, OSError) as e:
            raise ManifestError(f"{path.name}: invalid JSON ({e})")
        return Manifest.parse(raw, origin=str(path.name))

    def to_json(self) -> dict:
        """What the UI gets. Structure only -- no filesystem paths leak to the browser."""
        return {
            "id": self.id, "name": self.name, "blurb": self.blurb, "accent": self.accent,
            "order": self.order, "version": self.version, "homepage": self.homepage,
            "git": self.git, "branch": self.branch, "has_server": self.has_server,
            "bundled": bool(self.bundled),
            "requires_servers": [r for r in (self.server.get("requires") or []) if r != self.id],
            "needs": self.needs, "wants_credentials": bool(self.credentials),
            "own_output_root": self.output_root != "{job_dir}",
            "optional": {k: {"label": v.get("label", k), "help": v.get("help", ""),
                             "requirements": v.get("requirements", [])}
                         for k, v in (self.optional or {}).items()},
            "viewers": self.viewers,
            "tabs": [{
                "id": t.id, "title": t.title, "kind": t.kind, "src": t.src,
                "blurb": t.blurb, "artifacts": t.artifacts, "links": t.links,
                "run": ({"kind": t.run.kind, "label": t.run.label, "llm": bool(t.run.llm),
                         "can_cancel": t.run.kind == "cli" or bool(t.run.cancel)} if t.run else None),
                "fields": [{
                    "id": f.id, "type": f.type, "label": f.label, "placeholder": f.placeholder,
                    "help": f.help, "required": f.required,
                    # a file default is a server-side template (e.g. another module's output
                    # folder) -- the browser only ever sees the placeholder that describes it
                    "default": None if f.type in ("file", "files", "attachments") else f.default,
                    "has_default": f.default not in (None, ""),
                    "options": f.options, "rows": f.rows, "accept": f.accept,
                    "fills": f.fills or None, "text_target": f.text_target,
                    "max_files": f.max_files or 0, "max_mb": f.max_mb or 0,
                } for f in t.fields],
            } for t in self.tabs],
        }


def load_catalog(catalog_dir: pathlib.Path) -> dict[str, Manifest]:
    out: dict[str, Manifest] = {}
    for p in sorted(catalog_dir.glob("*.json")):
        try:
            m = _load_catalog_entry(p, catalog_dir)
            out[m.id] = m
        except ManifestError as e:
            # A broken catalog file must not take the hub down, but it must not vanish silently
            # either -- that is a half-hour of "why is my module missing".
            print(f"[hub] ignoring catalog entry {p.name}: {e}", file=sys.stderr)
            continue
    return out


def _load_catalog_entry(p: pathlib.Path, catalog_dir: pathlib.Path) -> Manifest:
    """A catalog file is a full manifest -- or, for a module whose code ships inside the hub, a
    stub `{"id", "source": {"bundled": "<folder>"}}` whose real manifest is the module's own
    steltic_module.json in catalog/<folder>/. The module stays self-describing, so it can move
    to its own repo later without a rewrite."""
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as e:
        raise ManifestError(f"{p.name}: invalid JSON ({e})")
    src = raw.get("source") if isinstance(raw, dict) else None
    folder = (src or {}).get("bundled") if isinstance(src, dict) else None
    if folder and not raw.get("tabs"):
        inner = catalog_dir / str(folder) / MANIFEST_NAME
        try:
            inner_raw = json.loads(inner.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError) as e:
            raise ManifestError(f"{p.name}: bundled module manifest {inner} unreadable ({e})")
        inner_raw.setdefault("source", {})
        if isinstance(inner_raw["source"], dict):
            inner_raw["source"]["bundled"] = str(folder)
        if raw.get("id") and inner_raw.get("id") != raw["id"]:
            raise ManifestError(f"{p.name}: bundled manifest declares id {inner_raw.get('id')!r}, expected {raw['id']!r}")
        return Manifest.parse(inner_raw, origin=str(inner))
    return Manifest.parse(raw, origin=str(p.name))
