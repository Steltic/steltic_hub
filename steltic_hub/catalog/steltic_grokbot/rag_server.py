#!/usr/bin/env python3
"""Query file manager grounding bridge: the design agents' standards-search API, answered from the local corpus.

The HR Steel and CFS agents call ONE small HTTP API for spec grounding (see rag_v2/README.md in
their repos):

    POST /query   {"query": "...", "collection": "engineering_standards_A360",
                   "top_k": 5, "clause": "F2", "chapter": "F"}      -> {"results": [...]}

It was written for a vector database. This server answers the same API from the QFM workspace
instead -- the full-text + exact-id index that `scripts/build_index.py` builds over the licensed
PDFs you converted, plus the OpenSees documentation and worked-example collections that ship with
the module. No embeddings, no vector store, nothing leaves this PC.

Collections the agents ask for are mapped onto the corpus:

    engineering_standards_A360 / A341 / A358        -> AISC_360_22 / AISC_341_22 / AISC_358_22
    engineering_standards_S100 / S240 / S400        -> AISI_S100 / AISI_S240 / AISI_S400_20
    engineering_standards_ASCE7 / ASCE41 / A342     -> ASCE7 / ASCE_41_23 / AISC_342_22
    steel_design_examples                           -> the examples collection (ships with QFM)
    opensees_* / openseespy_documentation           -> the OpenSees collection (ships with QFM)

A `clause` filter becomes an exact section / equation / table lookup first; `chapter` keeps hits
from that chapter; everything else is full-text search with the corpus's own alias expansion. A
document that has not been converted yet answers with an empty `results` list -- the agents treat
that as "no hit", whereas a non-2xx would pause their run.

Runs in QFM's own environment (stdlib + sqlite3, like the rest of its scripts). Started by the hub:

    rag_server.py --root <workspace> --port <port>

GET /          a status page (what is converted, what the agents asked for lately)
GET /healthz   {"ok": true, ...}
"""
from __future__ import annotations
import argparse, html, json, re, sys, threading, time, traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------- collection mapping
SPEC = {
    "A360": "AISC_360_22", "A341": "AISC_341_22", "A358": "AISC_358_22", "A342": "AISC_342_22",
    "S100": "AISI_S100", "S240": "AISI_S240", "S400": "AISI_S400_20",
    "ASCE7": "ASCE7", "ASCE_7": "ASCE7", "ASCE41": "ASCE_41_23", "ASCE_41": "ASCE_41_23",
    "AISC_360_22": "AISC_360_22", "AISC_341_22": "AISC_341_22", "AISC_358_22": "AISC_358_22",
    "AISC_342_22": "AISC_342_22", "AISI_S100": "AISI_S100", "AISI_S240": "AISI_S240",
    "AISI_S400_20": "AISI_S400_20", "ASCE_41_23": "ASCE_41_23",
}
PHASE2 = {
    "steel_design_examples": ("examples", "steel_design_examples"),
    # CFS Steel's contract and its agent both name this collection; without it every worked-example
    # lookup that module makes comes back "unknown collection". The shipped corpus files its
    # examples under one name, so both spellings land on it.
    "cfs_design_examples": ("examples", "steel_design_examples"),
    "examples": ("examples", None),
    "opensees_buildings_3d": ("opensees", "opensees_buildings_3d"),
    "opensees_building_templates": ("opensees", "opensees_building_templates"),
    "openseespy_documentation": ("opensees", "openseespy_documentation"),
    "opensees_documentation": ("opensees", "opensees_documentation"),
    "opensees": ("opensees", None),
}


def map_collection(name: str):
    """-> ("spec", doc_stem) | ("phase2", group, source_collection | None) | None"""
    n = (name or "").strip()
    if not n:
        return ("spec", None)
    if n in PHASE2:
        g, src = PHASE2[n]
        return ("phase2", g, src)
    key = n[len("engineering_standards_"):] if n.startswith("engineering_standards_") else n
    key_u = key.upper()
    if key_u in SPEC:
        return ("spec", SPEC[key_u])
    if key in SPEC:
        return ("spec", SPEC[key])
    # e.g. "AISC 360-22" / "aisc360" -- let the corpus's own alias table decide
    try:
        from retrieval import resolve_doc     # noqa: F401  (present once sys.path is set)
        r = resolve_doc(key)
        if r and r != key:
            return ("spec", r)
    except Exception:
        pass
    return None


_COMMENT = re.compile(r"^\s*(<!--.*?-->\s*)+", re.S)
_EQ = re.compile(r"^[A-Z]{1,2}\d+(?:\.\d+)*-\d+[a-z]?$", re.I)


def _clean(text: str) -> str:
    text = _COMMENT.sub("", text or "")          # chunk_id / meta header comments carry no content
    return text.strip()


def shape_hit(h: dict) -> dict:
    """One corpus hit -> the shape the agents render (text + a few meta keys)."""
    doc = h.get("doc") or h.get("source_collection") or h.get("collection") or ""
    sid = h.get("section_id") or h.get("eq_id") or h.get("table_id") or ""
    title = h.get("title") or ""
    part = h.get("part") or ""
    head = " ".join(x for x in (doc, sid, f"({part})" if part and part != "n/a" else "") if x)
    body = _clean(h.get("text") or h.get("snippet") or "")
    lines = [f"[{head}] {title}".strip()] if head or title else []
    lines.append(body)
    for n in (h.get("neighbors") or [])[:2]:
        if isinstance(n, dict) and n.get("text"):
            lines.append("")
            lines.append(f"-- {n.get('section_id') or ''} {n.get('title') or ''}".rstrip())
            lines.append(_clean(n["text"]))
    return {
        "text": "\n".join(lines).strip(),
        "score": h.get("score"),
        "source": doc,
        "section": sid,
        "title": title,
        "page": h.get("printed_label") or h.get("pdf_page"),
        "id": h.get("id"),
        "part": part,
        "authoritative": bool(h.get("authoritative")),
    }


# ---------------------------------------------------------------- corpus access
class Bridge:
    def __init__(self, root: Path, scripts: Path):
        self.root = root
        self.scripts = scripts
        self.lock = threading.Lock()
        self._corpus = None
        self._stamp = None
        self.recent: list[dict] = []
        self.started = time.time()
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        self._preload()

    def _preload(self):
        """The status page survives a restart: the last queries come back from the log."""
        try:
            p = self.root / "queue" / "agent_queries.jsonl"
            if p.is_file():
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
                for ln in lines:
                    try:
                        rec = json.loads(ln)
                        if isinstance(rec, dict) and "query" in rec:
                            self.recent.append(rec)
                    except Exception:
                        pass
        except Exception:
            pass

    # the indexes change whenever the user converts a PDF or rebuilds: reload on any mtime change.
    # So does the retrieval code, whenever the hub's Update copies a new scripts/ into the workspace:
    # Python caches the imported module, so without this the server answered with the OLD query
    # builder until someone restarted it -- while the Modules page reported the module up to date.
    def _index_stamp(self):
        out = []
        for rel in ("indexes/sections.json", "indexes/documents.json", "indexes/equations.json",
                    "indexes/tables.json", "search/spec_fts.sqlite", "search/phase2_fts.sqlite"):
            p = self.root / rel
            try:
                out.append((rel, p.stat().st_mtime_ns))
            except OSError:
                out.append((rel, None))
        for name in ("retrieval.py", "pipeline_fixes.py"):
            p = self.scripts / name
            try:
                out.append((name, p.stat().st_mtime_ns))
            except OSError:
                out.append((name, None))
        return tuple(out)

    def _code_changed(self, old_stamp, new_stamp):
        return any(a != b for a, b in zip(old_stamp or (), new_stamp) if a[0].endswith(".py"))

    def corpus(self):
        stamp = self._index_stamp()
        with self.lock:
            if self._corpus is None or stamp != self._stamp:
                if self._corpus is not None and self._code_changed(self._stamp, stamp):
                    import importlib
                    for name in ("pipeline_fixes", "retrieval"):
                        if name in sys.modules:
                            importlib.reload(sys.modules[name])
                from retrieval import Corpus
                old = self._corpus
                self._corpus = Corpus(self.root)
                self._stamp = stamp
                if old is not None:
                    try:
                        old.close()          # the replaced corpus still holds the old FTS files open
                    except Exception:
                        pass
            return self._corpus

    def release(self):
        """Drop the FTS file handles, keeping the parsed JSON indexes (the expensive part) cached.

        Held between requests they make `Rebuild index` impossible on Windows: an open sqlite
        handle blocks the unlink, and the rebuild dies with WinError 32. Re-opening costs about a
        millisecond, and this server serves one request at a time, so it is released the moment a
        query is answered. Older corpora without close() are simply left alone."""
        c = self._corpus
        if c is None:
            return
        try:
            c.close_fts()
        except AttributeError:
            pass
        except Exception:
            pass

    # ----- what is on this machine -----
    def status(self) -> dict:
        std = self.root / "documents" / "standards"
        pdfs = sorted(p.name for p in std.rglob("*.pdf")) if std.is_dir() else []
        md = self.root / "markdown"
        converted = sorted(p.stem for p in md.glob("*.md")) if md.is_dir() else []
        spec_index = (self.root / "search" / "spec_fts.sqlite").is_file()
        p2_index = (self.root / "search" / "phase2_fts.sqlite").is_file()
        docs = []
        try:
            docs = sorted(str(k) for k in self.corpus().doc_meta)
        except Exception:
            pass
        self.release()
        return {"ok": True, "root": str(self.root), "pdfs": pdfs, "converted": converted,
                "spec_index": spec_index, "phase2_index": p2_index, "indexed_docs": docs,
                "queries": len(self.recent), "uptime_s": int(time.time() - self.started)}

    # ----- the agents' call -----
    def query(self, body: dict) -> dict:
        q = str(body.get("query") or "").strip()
        collection = str(body.get("collection") or body.get("doc") or "")
        clause = str(body.get("clause") or "").strip()
        chapter = str(body.get("chapter") or "").strip()
        # the retrieval policy's fields (skills/Skill_querying_PACKAGED.md): an exact type with the id
        # alone in `query`, provisions unless commentary is asked for, and how much context around it
        qtype = str(body.get("type") or "").strip().lower()
        if qtype in ("exact_section", "exact_equation", "exact_table", "id") and q and not clause:
            clause, q = q, ""
        want_commentary = bool(body.get("want_commentary"))
        try:
            neighbors = max(0, min(int(body.get("context_neighbors")), 2)) if body.get("context_neighbors") is not None else None
        except (TypeError, ValueError):
            neighbors = None
        try:
            top_k = max(1, min(int(body.get("top_k") or 5), 20))
        except (TypeError, ValueError):
            top_k = 5
        t0 = time.time()
        note = ""
        matched = ""
        hits: list[dict] = []
        target = map_collection(collection)
        if target is None and collection:
            # A document the user converted under a stem the fixed table does not know (IS_875_3, an
            # ASCE 7 converted as ASCE_7_22, ...) is still in the corpus: /healthz lists it under
            # indexed_docs, so an agent that read /healthz may ask for it by that name.
            key = collection[len("engineering_standards_"):] if collection.startswith("engineering_standards_") else collection
            try:
                if key in self.corpus().doc_meta:
                    target = ("spec", key)
            except Exception:
                pass
        try:
            if target is None:
                note = f"unknown collection {collection!r}"
            elif target[0] == "spec":
                hits, note, matched = self._spec(q, target[1], clause, chapter, top_k, qtype=qtype,
                                                 want_commentary=want_commentary, neighbors=neighbors)
            else:
                hits, note = self._phase2(q, target[1], target[2], top_k)
        except Exception as e:                       # never a 5xx: that pauses the agent's run
            note = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        self.release()                     # never hold the index between queries -- see release()
        results = [shape_hit(h) for h in hits[:top_k]]
        rec = {"t": time.time(), "collection": collection, "query": q, "clause": clause, "chapter": chapter,
               "type": qtype, "matched": matched,
               "hits": len(results), "ms": int((time.time() - t0) * 1000), "note": note}
        self.recent.append(rec)
        del self.recent[:-200]
        self._log(rec)
        out = {"results": results, "collection": collection, "count": len(results),
               "matched": matched}   # exact_section / exact_equation / exact_table / fts / auto / keyword / ""
        if note:
            out["note"] = note
        return out

    def _spec(self, q: str, doc: str | None, clause: str, chapter: str, top_k: int,
              qtype: str = "", want_commentary: bool = False, neighbors=None):
        c = self.corpus()
        if doc and doc not in c.doc_meta:
            return [], (f"{doc} is not in the corpus yet -- convert it on the Convert tab "
                        f"(canonical stem {doc}) and rebuild the index"), ""
        if not (self.root / "search" / "spec_fts.sqlite").is_file():
            return [], "no specification index yet -- convert a PDF and run Rebuild index on the Query file manager tabs", ""
        hits: list[dict] = []
        matched = ""                                # which lookup answered: the caller must know
        if clause:
            if qtype in ("exact_section", "exact_equation", "exact_table"):
                kinds = (qtype,)                     # the policy named the kind: no guessing
            else:
                kinds = (("exact_equation",) if _EQ.match(clause) else ()) + ("exact_section", "exact_table")
            for kind in kinds:
                r = c.search(kind, clause, doc=doc, want_commentary=want_commentary,
                             neighbors=1 if neighbors is None else neighbors, limit=top_k)
                if r.get("found") and r.get("hits"):
                    hits = r["hits"]
                    matched = kind
                    break
        if not hits and q:
            first = "keyword" if qtype == "keyword" else ("fts" if qtype == "fts" else "auto")
            r = c.search(first, q, doc=doc, want_commentary=want_commentary,
                         neighbors=0 if neighbors is None else neighbors, limit=max(top_k * 3, 12))
            hits = r.get("hits") or []
            matched = first if hits else ""
            if not hits and first != "keyword":
                r = c.search("keyword", q, doc=doc, want_commentary=want_commentary, neighbors=0, limit=max(top_k * 3, 12))
                hits = r.get("hits") or []
                matched = "keyword" if hits else ""
            pref = (clause or chapter).upper()
            if pref:
                narrowed = [h for h in hits if str(h.get("section_id") or "").upper().startswith(pref)]
                if narrowed:
                    hits = narrowed
        if not hits and clause and not q:
            r = c.search("keyword", clause, doc=doc, neighbors=0, limit=top_k)
            hits = r.get("hits") or []
            matched = "keyword" if hits else ""
        if want_commentary:
            return hits, "", matched
        # provisions only, unless the corpus has nothing else for this query
        prov = [h for h in hits if h.get("part") in (None, "", "standard", "n/a")]
        return (prov or hits), "", matched

    def _phase2(self, q: str, group: str, source: str | None, top_k: int):
        c = self.corpus()
        if not (self.root / "search" / "phase2_fts.sqlite").is_file():
            return [], "the OpenSees / examples index is missing -- reinstall Query file manager"
        want = max(top_k * 3, 12)
        r = c.search("fts", q, collection=group, neighbors=0, limit=want)
        hits = r.get("hits") or []
        if not hits:
            r = c.search("keyword", q, collection=group, neighbors=0, limit=want)
            hits = r.get("hits") or []
        if source:
            narrowed = [h for h in hits if (h.get("source_collection") or h.get("doc")) == source]
            if narrowed:
                hits = narrowed
        return hits, ""

    def _log(self, rec: dict):
        try:
            d = self.root / "queue"
            d.mkdir(parents=True, exist_ok=True)
            with open(d / "agent_queries.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass


# ---------------------------------------------------------------- http
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Query file manager — grounding</title><style>
body{{margin:0;padding:22px 26px;background:#14171c;color:#dde3ea;font:13px/1.55 'Segoe UI',system-ui,sans-serif}}
h1{{font-size:17px;margin:0 0 4px}} h2{{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:#5d6774;margin:22px 0 8px}}
.dim{{color:#8b95a3}} .ok{{color:#3fc1a0}} .bad{{color:#e06b5e}} .warn{{color:#e0b341}}
code,td{{font-family:ui-monospace,Consolas,monospace;font-size:11.5px}}
table{{border-collapse:collapse;width:100%}} td,th{{padding:5px 8px;border-bottom:1px solid #22272f;text-align:left;vertical-align:top}}
th{{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:#8b95a3}}
.pill{{display:inline-block;font-size:10px;letter-spacing:.07em;text-transform:uppercase;padding:2px 8px;border-radius:4px;border:1px solid #3a4656;color:#8b95a3;margin-right:6px}}
.pill.ok{{border-color:#3fc1a0;color:#3fc1a0}} .pill.warn{{border-color:#e0b341;color:#e0b341}}
</style></head><body>
<h1>Query file manager — grounding</h1>
<div class="dim">The design agents' standards-search endpoint, answered from this PC's corpus. Workspace: <code>{root}</code></div>
<h2>Corpus</h2>
<p><span class="pill {p2cls}">OpenSees docs + worked examples</span> <span class="pill {speccls}">specifications</span></p>
<p class="dim">Converted specifications: {converted}<br>PDFs in <code>documents/standards</code>: {pdfs}</p>
{hint}
<h2>Recent agent queries ({n})</h2>
<table><tr><th>when</th><th>collection</th><th>query</th><th>clause / chapter</th><th>hits</th><th>ms</th><th>note</th></tr>{rows}</table>
</body></html>"""


def render_page(b: Bridge) -> str:
    st = b.status()
    rows = []
    for r in reversed(b.recent[-50:]):
        when = time.strftime("%H:%M:%S", time.localtime(r["t"]))
        cls = "ok" if r["hits"] else ("warn" if not r["note"] else "bad")
        rows.append(f"<tr><td>{when}</td><td>{html.escape(r['collection'])}</td><td>{html.escape(r['query'][:120])}</td>"
                    f"<td>{html.escape(' / '.join(x for x in (r['clause'], r['chapter']) if x))}</td>"
                    f"<td class='{cls}'>{r['hits']}</td><td>{r['ms']}</td><td class='dim'>{html.escape(r['note'][:120])}</td></tr>")
    hint = ""
    if not st["spec_index"]:
        hint = ("<p class='warn'>No specification corpus yet: put your licensed PDFs in the standards folder, convert each "
                "one on the Convert tab with its canonical stem, then Rebuild index. Until then the agents get "
                "OpenSees and worked-example hits only and cite the specifications from memory.</p>")
    return PAGE.format(root=html.escape(st["root"]),
                       p2cls="ok" if st["phase2_index"] else "warn",
                       speccls="ok" if st["spec_index"] else "warn",
                       converted=html.escape(", ".join(st["converted"]) or "(none)"),
                       pdfs=html.escape(", ".join(st["pdfs"]) or "(none)"),
                       hint=hint, n=len(b.recent), rows="".join(rows) or "<tr><td colspan=7 class='dim'>none yet</td></tr>")


def make_handler(bridge: Bridge):
    class H(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str = "application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/healthz", "/api/status"):
                self._send(200, json.dumps(bridge.status()).encode("utf-8"))
            elif path == "/":
                self._send(200, render_page(bridge).encode("utf-8"), "text/html")
            elif path == "/api/recent":
                self._send(200, json.dumps(bridge.recent[-50:]).encode("utf-8"))
            else:
                self._send(404, b'{"error":"not found"}')

        def do_POST(self):
            path = urlparse(self.path).path
            if path not in ("/query", "/api/query"):
                self._send(404, b'{"error":"not found"}')
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                if not isinstance(body, dict):
                    body = {}
            except Exception as e:
                self._send(400, json.dumps({"results": [], "error": f"bad JSON body: {e}"}).encode("utf-8"))
                return
            out = bridge.query(body)
            self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"))

        def log_message(self, fmt, *args):      # one line per request in the hub's server log
            sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))
            sys.stdout.flush()
    return H


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Query file manager grounding bridge")
    ap.add_argument("--root", required=True, help="QFM workspace (the folder with documents/, indexes/, search/)")
    ap.add_argument("--scripts", default=None, help="folder holding retrieval.py (default: <root>/scripts)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    scripts = Path(a.scripts).resolve() if a.scripts else root / "scripts"
    if not (scripts / "retrieval.py").is_file():
        print(f"[qfm] retrieval.py not found in {scripts} -- reinstall Query file manager (its post-install lays the workspace out)")
        return 2
    for d in ("documents/standards", "queue", "markdown"):
        (root / d).mkdir(parents=True, exist_ok=True)
    bridge = Bridge(root, scripts)
    # Single-threaded on purpose: the corpus holds sqlite connections, which refuse to be used from
    # a thread other than the one that opened them. Queries take milliseconds; the agents wait.
    srv = HTTPServer((a.host, a.port), make_handler(bridge))
    print(f"[qfm] grounding bridge on http://{a.host}:{a.port}  workspace={root}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
