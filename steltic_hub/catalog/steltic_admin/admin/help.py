"""Help: answer "how does Steltic work" from the code and docs, with the passages shown.

Where it looks (all read-only):
  * the hub's own source -- the folder the hub reports on /healthz (README, REVIEW, steltic_hub/*.py,
    the catalog manifests, the bundled modules)
  * every module checkout the hub holds (<data>/modules/<id>), or the working copy it is linked to:
    README and the other .md files, docs/, contract/, skills/, prompts/, and the .py files
  * on request, the public GitHub repos the manifests name (raw.githubusercontent.com), so a module
    that is not installed here, or the latest main, can still be asked about. Fetched files are
    cached under <ADMIN_DATA>/github for a day.

Retrieval is plain term matching over ~60-line chunks (a path hit counts extra); no embeddings, no
vector store, nothing leaves the machine except the GitHub fetches the user asked for. With an LLM
connection the top passages go to the model with the instruction to answer from them only and cite
`path:lines`; without one the passages themselves are the answer.
"""
from __future__ import annotations
import json, math, pathlib, re, time
import httpx
from . import llm

DOC_EXT = {".md", ".markdown", ".txt", ".rst"}
CODE_EXT = {".py", ".json", ".toml", ".js", ".ps1", ".bat", ".yaml", ".yml"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", "target", "selftest-data",
             "records", "engineering_rag_phase2", "patches", "Claude outputs", ".pytest_cache", "hubenv", "envs"}
DOC_DIRS = ("docs", "contract", "skills", "prompts", "doc")
MAX_FILE = 600_000
CHUNK_LINES, OVERLAP = 60, 10
STOP = set("""a an the and or of to in on for with is are be by as at from that this these those it its into
how what why when where which who does do did can could should would will may might not no yes you your
me my we our i if then than so such via about over under between per each any all some more most less
use used using make makes made run runs running work works working file files code module modules hub
steltic""".split())
TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_\-\.]{1,}")


def terms(q: str) -> list[str]:
    out = []
    for t in TOKEN.findall(q or ""):
        t = t.lower().strip(".-")
        if len(t) < 3 or t in STOP:
            continue
        out.append(t)
    return list(dict.fromkeys(out))


class Corpus:
    """Files worth reading, chunked, with a small cache keyed on mtime."""

    def __init__(self):
        self._chunks: dict[str, list[dict]] = {}       # file path -> chunks
        self._stamp: dict[str, float] = {}

    def add_tree(self, root: pathlib.Path, source: str, code: bool = True, limit: int = 1500):
        if not root or not root.is_dir():
            return 0
        n = 0
        for p in _walk(root, code):
            if n >= limit:
                break
            n += self._add_file(p, source, root)
        return n

    def _add_file(self, p: pathlib.Path, source: str, root: pathlib.Path) -> int:
        try:
            st = p.stat()
        except OSError:
            return 0
        if st.st_size > MAX_FILE:
            return 0
        key = str(p)
        if self._stamp.get(key) == st.st_mtime and key in self._chunks:
            return 1
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return 0
        rel = p.relative_to(root).as_posix() if root in p.parents or p == root else p.name
        self._chunks[key] = list(_chunk(text, f"{source}:{rel}", str(p)))
        self._stamp[key] = st.st_mtime
        return 1

    def add_text(self, label: str, path: str, text: str):
        self._chunks[path] = list(_chunk(text, label, path))
        self._stamp[path] = time.time()

    def search(self, question: str, k: int = 10) -> list[dict]:
        qs = terms(question)
        if not qs:
            return []
        all_chunks = [c for cs in self._chunks.values() for c in cs]
        if not all_chunks:
            return []
        # document frequency per term, over chunks
        df = {t: 0 for t in qs}
        for c in all_chunks:
            low = c["low"]
            for t in qs:
                if t in low:
                    df[t] += 1
        n = len(all_chunks)
        scored = []
        for c in all_chunks:
            s = 0.0
            hit = 0
            low, path_low = c["low"], c["label"].lower()
            for t in qs:
                cnt = low.count(t)
                if cnt:
                    hit += 1
                    s += (1 + math.log(cnt)) * math.log(1 + n / (1 + df[t]))
                if t in path_low:
                    s += 2.0
            if s > 0:
                # a passage that covers several of the question's terms beats one that repeats a single one
                s *= (hit / len(qs)) ** 0.5
                # docs beat code for "how does X work"; code beats docs when the question names an identifier
                if c["label"].endswith((".md", ".txt", ".rst")):
                    s *= 1.25
                scored.append((s, c))
        scored.sort(key=lambda x: -x[0])
        out, seen_files = [], {}
        for s, c in scored:
            if seen_files.get(c["path"], 0) >= 3:
                continue
            seen_files[c["path"]] = seen_files.get(c["path"], 0) + 1
            out.append({"label": c["label"], "path": c["path"], "lines": c["lines"], "text": c["text"], "score": round(s, 2)})
            if len(out) >= k:
                break
        return out


def _walk(root: pathlib.Path, code: bool):
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name in SKIP_DIRS or p.name.startswith("."):
                    continue
                stack.append(p)
            elif p.suffix.lower() in DOC_EXT or (code and p.suffix.lower() in CODE_EXT):
                yield p


def _chunk(text: str, label: str, path: str):
    lines = text.splitlines()
    i = 0
    while i < len(lines) or i == 0:
        part = lines[i:i + CHUNK_LINES]
        body = "\n".join(part)
        if body.strip():
            yield {"label": label, "path": path, "lines": f"{i + 1}-{i + len(part)}", "text": body, "low": body.lower()}
        if i + CHUNK_LINES >= len(lines):
            break
        i += CHUNK_LINES - OVERLAP


# ---------------------------------------------------------------- GitHub
class GitHub:
    """Public repos only, unauthenticated, cached on disk for a day."""

    def __init__(self, cache: pathlib.Path):
        self.cache = cache
        self.cache.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def parse_repo(url: str) -> tuple[str, str] | None:
        m = re.match(r"https?://github\.com/([^/\s]+)/([^/\s#?]+?)(?:\.git)?/?$", (url or "").strip())
        return (m.group(1), m.group(2)) if m else None

    def tree(self, owner: str, repo: str, branch: str = "main") -> list[dict]:
        p = self.cache / f"{owner}__{repo}__{branch}.tree.json"
        if p.is_file() and time.time() - p.stat().st_mtime < 86400:
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
        r = httpx.get(f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1", timeout=30.0,
                      headers={"Accept": "application/vnd.github+json", "User-Agent": "steltic-admin"})
        r.raise_for_status()
        items = [{"path": t["path"], "size": t.get("size", 0)} for t in r.json().get("tree", []) if t.get("type") == "blob"]
        p.write_text(json.dumps(items), encoding="utf-8")
        return items

    def file(self, owner: str, repo: str, branch: str, path: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.\-]+", "_", path)
        p = self.cache / f"{owner}__{repo}__{branch}" / safe
        if p.is_file() and time.time() - p.stat().st_mtime < 86400:
            return p.read_text(encoding="utf-8", errors="replace")
        r = httpx.get(f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}", timeout=60.0,
                      headers={"User-Agent": "steltic-admin"})
        r.raise_for_status()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(r.text, encoding="utf-8")
        return r.text

    def pull_into(self, corpus: Corpus, owner: str, repo: str, branch: str, question: str, max_files: int = 40) -> int:
        """Docs always; code files only when the question names something in their path."""
        try:
            items = self.tree(owner, repo, branch)
        except Exception:
            return 0
        qs = terms(question)
        picked = []
        for it in items:
            path = it["path"]
            low = path.lower()
            if any(part in SKIP_DIRS for part in path.split("/")):
                continue
            ext = pathlib.Path(path).suffix.lower()
            if ext in DOC_EXT or path in ("steltic_module.json", "pyproject.toml"):
                picked.append(path)
            elif ext in CODE_EXT and any(t in low for t in qs):
                picked.append(path)
        n = 0
        for path in picked[:max_files]:
            try:
                text = self.file(owner, repo, branch, path)
            except Exception:
                continue
            corpus.add_text(f"github:{owner}/{repo}:{path}", f"https://github.com/{owner}/{repo}/blob/{branch}/{path}", text)
            n += 1
        return n


# ---------------------------------------------------------------- the answer
SYSTEM = """You are Admin, the help desk of the Steltic hub (a desktop app that runs steel-design modules:
HR Steel, CFS Steel, Nonlinear (SNL), Query file manager, Design variations, Probabilistic analysis, Admin).
Answer the user's question from the EXCERPTS below and nothing else. Be concrete and short: say what the
code or document actually does, name the file and the option or endpoint involved, and cite each fact as
(label lines a-b) using the labels exactly as given. If the excerpts do not answer the question, say so and
say which module or file would be the place to look. Never invent behaviour, flags, file names or numbers."""


def answer(question: str, excerpts: list[dict]) -> dict:
    if not excerpts:
        return {"answer": "Nothing in the code or docs on this PC matches that question. Try other words, or turn on GitHub in the scope.",
                "model": None}
    if not llm.available():
        joined = "\n\n".join(f"### {e['label']} lines {e['lines']}\n{e['text']}" for e in excerpts[:6])
        return {"answer": "No LLM connection is set (or the model is MOCK), so here are the passages that match your question, best first:\n\n" + joined,
                "model": None}
    user = "QUESTION:\n" + question.strip() + "\n\nEXCERPTS:\n" + "\n\n".join(
        f"[{e['label']} lines {e['lines']}]\n{e['text']}" for e in excerpts)
    try:
        txt = llm.chat(SYSTEM, user, max_tokens=2500, temperature=0.1, json_mode=False)
    except Exception as e:
        joined = "\n\n".join(f"### {x['label']} lines {x['lines']}\n{x['text']}" for x in excerpts[:6])
        return {"answer": f"The model call failed ({type(e).__name__}: {e}); the matching passages:\n\n" + joined, "model": None}
    return {"answer": txt.strip(), "model": (llm.creds() or {}).get("model")}
