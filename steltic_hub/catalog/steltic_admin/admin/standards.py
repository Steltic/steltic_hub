"""The standards queue: a folder of licensed specification PDFs -> one Convert run each, in turn.

Converting a specification through the Query file manager takes hours (Docling at full accuracy),
and a site needs five to nine of them. This turns the folder into a plan: one `convert` step per PDF
that is not converted yet, each with its canonical stem, then `index` once, then `audit` once.
Everything else -- the retries on a native crash, the resume from the last finished chunk, the
converter gate -- is the hub's and the module's own behaviour; Admin only presses the buttons.

Canonical stems are fixed by the skills (retrieval ids depend on them); the table below guesses one
from a file name and the user confirms or corrects it in the UI before anything is queued.
"""
from __future__ import annotations
import pathlib, re

# (regex over the lower-cased file name, canonical stem, label)
STEMS = [
    (r"aisc[\s_\-]*360|a360|\b360[\s_\-]*(16|22)\b|specification for structural steel", "AISC_360_22", "AISC 360-22"),
    (r"aisc[\s_\-]*341|a341|\b341[\s_\-]*(16|22)\b|seismic provisions", "AISC_341_22", "AISC 341-22"),
    (r"aisc[\s_\-]*358|a358|\b358[\s_\-]*(16|22)\b|prequalified", "AISC_358_22", "AISC 358-22"),
    (r"aisc[\s_\-]*342|a342|\b342[\s_\-]*22\b", "AISC_342_22", "ANSI/AISC 342-22"),
    (r"asce[\s_\-]*(sei[\s_\-]*)?7\b|asce7|asce[\s_\-]*7[\s_\-]*(16|22)|minimum design loads", "ASCE7", "ASCE/SEI 7-22"),
    (r"asce[\s_\-]*(sei[\s_\-]*)?41\b|asce41|asce[\s_\-]*41[\s_\-]*(17|23)|seismic evaluation and retrofit", "ASCE_41_23", "ASCE/SEI 41-23"),
    (r"aisi[\s_\-]*s?100|s100", "AISI_S100", "AISI S100-16 (R2020) w/S3"),
    (r"aisi[\s_\-]*s?240|s240", "AISI_S240", "AISI S240-20"),
    (r"aisi[\s_\-]*s?400|s400", "AISI_S400_20", "AISI S400-20"),
]
KNOWN = {s: label for _, s, label in STEMS}


def guess_stem(filename: str) -> str:
    n = filename.lower()
    for rx, stem, _ in STEMS:
        if re.search(rx, n):
            return stem
    return ""


def converted_stems(grokbot_root: pathlib.Path) -> set[str]:
    """What the Query file manager already holds: the stems of the markdown it produced."""
    md = grokbot_root / "markdown"
    out: set[str] = set()
    if not md.is_dir():
        return out
    for p in md.glob("*.md"):
        stem = p.name
        for suffix in (".search.md", ".md"):
            if stem.endswith(suffix):
                stem = stem[:-len(suffix)]
                break
        out.add(stem)
    return out


def scan(folder: pathlib.Path, grokbot_root: pathlib.Path) -> dict:
    items = []
    if not folder.is_dir():
        return {"folder": str(folder), "exists": False, "items": [], "converted": sorted(converted_stems(grokbot_root))}
    have = converted_stems(grokbot_root)
    for p in sorted(folder.rglob("*.pdf")):
        stem = guess_stem(p.name)
        items.append({"pdf": str(p), "name": p.name, "bytes": p.stat().st_size, "stem": stem,
                      "label": KNOWN.get(stem, ""), "converted": bool(stem) and stem in have})
    return {"folder": str(folder), "exists": True, "items": items, "converted": sorted(have),
            "stems": [{"stem": s, "label": l} for _, s, l in STEMS]}


def build_steps(items: list[dict], folder: str, project: str = "Standards", rebuild_index: bool = True,
                audit: bool = True, chunk_pages: int = 5) -> list[dict]:
    steps = []
    for it in items:
        pdf, stem = str(it.get("pdf") or ""), str(it.get("stem") or "")
        if not pdf:
            continue
        fields = {"pdf": pdf, "stem": stem, "chunk_pages": chunk_pages, "profile": "auto"}
        steps.append({"project": project, "module": "steltic_grokbot", "tab": "convert", "fields": fields,
                      "on_fail": "continue", "label": f"convert {pathlib.Path(pdf).name} as {stem or '(from the file name)'}"})
    if rebuild_index and steps:
        steps.append({"project": project, "module": "steltic_grokbot", "tab": "index", "fields": {},
                      "on_fail": "stop", "label": "rebuild the index"})
    if audit and steps:
        steps.append({"project": project, "module": "steltic_grokbot", "tab": "audit", "fields": {"pdf_dir": folder},
                      "on_fail": "continue", "label": "audit the corpus"})
    return steps
