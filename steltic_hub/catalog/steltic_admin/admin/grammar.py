"""Plain-words batch instructions -> a plan, with no model in the loop.

    J1 to hr then nl; then J2 to cfs only; then J3 to hr
    run J1 (ex22) to hr then to nl, when all done run J2 with ex3 to cfs
    Tower_A -> hr, nl
    J4 continue: make the exterior columns W24x146 and rerun

A clause names a project (anything that is not a known word: `J1`, `Tower_A`, `27_High_St`), one or
more module aliases in the order they should run, and optionally where the design brief comes from --
`(ex22)` / `[ex22]` / `with ex22` for one of the design servers' example briefs, `(brief.md)` for a
file in the project folder. A clause with no project continues the previous one ("then to nl").
Words like run / then / when all done / only / please are ignored; anything else unrecognised is
reported as a warning rather than guessed at.

The aliases below are the only module knowledge in Admin, and they are DATA (the UI shows the table
and the model path is given it as text), not code paths: a new runnable tab appears in the plan
editor and in the model path from the hub's own manifest listing without touching this file.
"""
from __future__ import annotations
import re

# alias -> (module id, tab id). Runnable tabs only (kind form with a run); the servers' own UIs
# (variations, probabilistic) are not driven through the hub's /api/run and are not here.
ALIASES: dict[str, tuple[str, str]] = {
    "hr": ("steltic", "design"), "hrs": ("steltic", "design"), "hrsteel": ("steltic", "design"),
    "steltic": ("steltic", "design"), "steel": ("steltic", "design"), "design": ("steltic", "design"),
    "cfs": ("steltic_cfs", "design"), "cfssteel": ("steltic_cfs", "design"), "coldformed": ("steltic_cfs", "design"),
    "nl": ("steltic_nonlinear", "run"), "snl": ("steltic_nonlinear", "run"), "nonlinear": ("steltic_nonlinear", "run"),
    "non-linear": ("steltic_nonlinear", "run"), "nlrha": ("steltic_nonlinear", "run"), "pushover": ("steltic_nonlinear", "run"),
    "inspect": ("steltic_nonlinear", "inspect"), "hazard": ("steltic_nonlinear", "hazard"),
    "criteria": ("steltic_nonlinear", "criteria"), "compare": ("steltic_nonlinear", "compare"), "mesh": ("steltic_nonlinear", "mesh"),
    "qfm": ("steltic_grokbot", "ask"), "query": ("steltic_grokbot", "ask"), "ask": ("steltic_grokbot", "ask"),
    "corpus": ("steltic_grokbot", "corpus"), "convert": ("steltic_grokbot", "convert"), "index": ("steltic_grokbot", "index"),
    "reindex": ("steltic_grokbot", "index"), "audit": ("steltic_grokbot", "audit"),
}
# "continue" is relative: it means the continue tab of the design module named before it (HR Steel by default)
CONTINUE = "continue"
# the words a clause may contain that mean nothing on their own
FILLER = {"run", "then", "to", "the", "a", "an", "and", "when", "all", "done", "only", "please", "next",
          "after", "that", "it", "also", "them", "on", "with", "using", "use", "brief", "project", "for",
          "first", "second", "third", "finally", "lastly", "go", "send", "do", "through", "into", "again",
          "of", "in", "as", "is", "are", "be", "start", "kick", "off", "job", "jobs", "building", "buildings",
          "->", "=>", ">", "-", "+", "&", "module", "modules", "app", "via", "up", "same", "this", "these", "those",
          "etc", "etc.", "so", "too", "now", "ok", "okay"}
CLAUSE_SPLIT = re.compile(r"(?:\s*;\s*|\s*\n+\s*|\s*\bthen\b\s*|\s*\bafter that\b\s*"
                          r"|\s*\bwhen (?:all|that|it|they)(?: is| are)? (?:done|finished|complete)\b\s*|\s*\bnext\b\s*)", re.I)
BRIEF_PAREN = re.compile(r"[\(\[]\s*([A-Za-z0-9_.\-]+)\s*[\)\]]")
EXAMPLE = re.compile(r"^(?:ex|example)[_-]?(\d+[a-z]?|redesign)$", re.I)
PROJECT = re.compile(r"^[A-Za-z0-9_-]+$")


def _alias(word: str) -> tuple[str, str] | None:
    w = word.lower().strip(".,:")
    if w in ALIASES:
        return ALIASES[w]
    w2 = w.replace(" ", "").replace("_", "")
    return ALIASES.get(w2)


def parse(text: str) -> dict:
    """-> {"steps": [{project, module, tab, fields}], "warnings": [...], "clauses": [...]}"""
    steps: list[dict] = []
    warnings: list[str] = []
    clauses = [c.strip() for c in CLAUSE_SPLIT.split(text or "") if c and c.strip()]
    project: str | None = None

    def flush(proj, mods, brief_src, follow, raw):
        nonlocal project
        if not mods:
            return
        if not proj:
            warnings.append(f"no project named before “{raw}” (start with the project name, e.g. J1 to hr)")
            return
        project = proj
        for mod, tab in mods:
            fields: dict = {}
            if tab == "design":
                fields["brief"] = brief_src or "@project"
            elif tab == CONTINUE:
                fields["brief"] = follow or ""
            steps.append({"project": proj, "module": mod, "tab": tab, "fields": fields})

    for raw in clauses:
        clause = raw
        brief_src = None
        for m in BRIEF_PAREN.finditer(clause):
            brief_src = _brief_source(m.group(1))
        clause = BRIEF_PAREN.sub(" ", clause)
        follow = None
        mc = re.search(r"\bcontinue\s*:\s*(.+)$", clause, re.I | re.S)
        if mc:
            follow = mc.group(1).strip()
            clause = clause[:mc.start()] + " continue"
        words = [w for w in re.split(r"[\s,]+", clause) if w]
        mods: list[tuple[str, str]] = []
        proj = project
        named = False
        i = 0
        while i < len(words):
            w = words[i]
            lw = w.lower().strip(".,:")
            two = (lw + " " + words[i + 1].lower().strip(".,:")) if i + 1 < len(words) else None
            if lw == CONTINUE:
                base = mods[-1][0] if mods and mods[-1][1] == "design" else "steltic"
                if mods and mods[-1][1] == "design":
                    mods[-1] = (base, CONTINUE)          # "J4 cfs continue: …" is CFS's continue tab, not a design + a continue
                else:
                    mods.append((base, CONTINUE))
                i += 1; continue
            if two and _alias(two):
                mods.append(_alias(two)); i += 2; continue
            al = _alias(lw)
            if al:
                mods.append(al); i += 1; continue
            if not lw or lw in FILLER:
                i += 1; continue
            if EXAMPLE.match(lw):
                brief_src = _brief_source(lw); i += 1; continue
            tok = w.strip(".,:")
            if PROJECT.match(tok):
                # a name is a project only if a module follows it somewhere in the clause; otherwise it
                # is a stray word ("J9 to hr and make it snappy" must not become project "make")
                later = any(_alias(x.lower().strip(".,:")) or x.lower().strip(".,:") == CONTINUE for x in words[i + 1:])
                if named and mods:
                    if later:                              # "J1 to hr, J2 to cfs": a second project starts a new chain
                        flush(proj, mods, brief_src, follow, raw)
                        mods, brief_src = [], None
                        proj = tok
                        i += 1; continue
                elif not named:                            # the first name in a clause is its project ("hr for J9" too)
                    proj = tok; named = True
                    i += 1; continue
            warnings.append(f"ignored {w!r} in “{raw}”")
            i += 1
        if not mods and raw.strip():
            warnings.append(f"no module named in “{raw}” (say hr, cfs, nl …)")
        flush(proj, mods, brief_src, follow, raw)
    return {"steps": steps, "warnings": warnings, "clauses": clauses}


def _brief_source(token: str) -> str:
    t = token.strip()
    m = EXAMPLE.match(t)
    if m:
        return "@example:" + ("redesign" if m.group(1).lower() == "redesign" else "ex" + m.group(1).lower())
    return "@file:" + t


def describe_aliases() -> list[dict]:
    """For the UI's cheat sheet: one row per (module, tab) with the words that reach it."""
    by: dict[tuple[str, str], list[str]] = {}
    for a, mt in ALIASES.items():
        by.setdefault(mt, []).append(a)
    rows = [{"module": m, "tab": t, "aliases": sorted(a, key=len)} for (m, t), a in sorted(by.items())]
    rows.append({"module": "(the design module named before it)", "tab": CONTINUE, "aliases": ["continue: <follow-up>"]})
    return rows
