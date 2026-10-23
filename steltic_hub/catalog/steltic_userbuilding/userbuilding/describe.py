"""A building typed into the page -> an OpenSees model -> HR Steel's building description.

The text box on step 1 takes either

  * OpenSees code (Tcl or OpenSeesPy, pasted): it is saved as a script and read by the OpenSees interpreter
    (opensees/) exactly as an uploaded file -- no language model involved; or
  * a description in plain words. The hub's model connection (llm.py) reads it into a small structured spec
    (SYSTEM below: the grid, the stories, member groups, brace patterns, and the design basis with the user's own
    words quoted); check() turns the spec into a building plan and lists what the words do not settle -- a
    section, which beams are moment-connected, which way the columns face -- as questions back to the user,
    instead of guessing; script() writes the plan as an OpenSeesPy script (kip-in, Z up, the AISC properties of
    the named shapes from HR's table); the OpenSees interpreter reads that script like any upload; and finish()
    gives the members their AISC names (the script carries the table's own properties, so each matches exactly),
    the grid lines and levels the names the text uses, and puts the basis values (SDS, loads, systems ...) in as
    proposals to accept, each with the words it came from. A value whose quote is not in the text is not taken.

The model never decides the structure: every member comes from a group the words state, and the 3-D model is
checked by the user before the suite runs (step 4).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from urllib.parse import quote as _urlquote

from .opensees import shapes as SH

SPEC = "steltic.building_words"
G_IN = 386.0886                  # in/s2
E_KSI, G_KSI = 29000.0, 11200.0

# ------------------------------------------------------------------------------------------- text or code
_PY_CODE = re.compile(r"^\s*(?:import\s+openseespy|from\s+openseespy|import\s+opensees\b|ops\.(?:model|node|element|wipe)\s*\("
                      r"|model\s*\(\s*['\"]basic)", re.M)
_TCL_CODE = re.compile(r"^\s*(?:model\s+(?:basic|BasicBuilder)\b|node\s+\d+\s+[-\d.]|element\s+\w+\s+\d+\s+\d+|"
                       r"geomTransf\s+\w+\s+\d+|fix\s+\d+\s+[01]\s+[01])", re.M)


def classify(text: str) -> str:
    """'py' or 'tcl' for pasted OpenSees code, 'words' for a description."""
    t = text or ""
    py, tcl = len(_PY_CODE.findall(t)), len(_TCL_CODE.findall(t))
    if py and py >= tcl:
        return "py"
    if tcl >= 2 or (tcl and "model" in t):
        return "tcl"
    return "words"


# ------------------------------------------------------------------------------------------- the prompt
BASIS_ITEMS = {
    "project.code": "project/code", "seismic.SDS": "seismic/SDS", "seismic.SD1": "seismic/SD1", "seismic.S1": "seismic/S1",
    "seismic.TL": "seismic/TL", "seismic.risk_category": "seismic/risk_category", "seismic.Ie": "seismic/Ie",
    "seismic.site_class": "seismic/site_class", "seismic.analysis": "seismic/analysis",
    "seismic.system.X": "seismic/system/X", "seismic.system.Y": "seismic/system/Y",
    "loads.D_psf": "loads/D_psf", "loads.L_psf": "loads/L_psf", "loads.Lr_psf": "loads/Lr_psf",
    "loads.snow_psf": "loads/snow_psf", "loads.clad_psf": "loads/clad_psf", "loads.floor_system": "loads/floor_system",
    "loads.deck_span": "loads/deck_span", "materials.W": "materials/W", "materials.HSS": "materials/HSS",
}
RESULT_ITEMS = ("user_results.T1_s.X", "user_results.T1_s.Y", "user_results.V_kip.X", "user_results.V_kip.Y",
                "user_results.W_kip", "user_results.drift_pct.X", "user_results.drift_pct.Y")
PATTERNS = ("chevron", "v", "x", "diagonal", "diagonal_reverse", "two_story_x", "eccentric")

SYSTEM = """You read a description of a steel building written in plain words and return it as ONE JSON object in
the format below. A program builds the structural model from your JSON and the engineer checks it afterwards.
Take only what the text says. Never invent a section, a dimension, a connection type, a column orientation, a load
or a seismic value: when the text does not say something, use null (or leave the group out) and put a short
question in "unclear".

Units: feet, kip, psf, in2. Convert metric (1 m = 3.2808 ft, 1 kN = 0.22481 kip, 1 kPa = 20.885 psf).

FORMAT
{
 "name": the building's name or null,
 "grid": {"x": {"labels": [...] | null, "bays_ft": [...] | null, "coords_ft": [...] | null},
          "y": {"labels": [...] | null, "bays_ft": [...] | null, "coords_ft": [...] | null}},
 "stories_ft": [story heights, from the ground up],
 "level_names": [names of floor 1 .. floor N as the text names them] | null,
 "base_name": the name of the base level | null,
 "supports": "fixed" | "pinned" | null,
 "diaphragm": "rigid" | "flexible" | "semi-rigid" | null,
 "columns": [{"section": "W14X90", "x": "all" | [labels], "y": "all" | [labels], "stories": "all" | [first, last],
              "strong_axis": "X" | "Y" | null, "omit": false}],
 "beams": [{"section": "W24X76", "lines": "all" | [labels], "along": "X" | "Y" | null, "bays": "all" | [[label, label], ...],
            "floors": "all" | [first, last], "connection": "moment" | "pinned" | null, "omit": false}],
 "braces": [{"section": "HSS8X8X1/2" | null, "brb_Asc_in2": null, "lines": [labels], "bays": [[label, label], ...],
             "stories": "all" | [first, last], "pattern": "chevron", "link_ft": null}],
 "seismic_weight_kip": [weight of floor 1 .. floor N] | null,
 "basis": {"<item>": {"value": ..., "quote": "<the words of the text it comes from, copied character for character>"}},
 "unclear": ["what the text leaves open, as a short question"]
}

GRID
- grid.x are the grid lines at positions along X (each runs in the Y direction); grid.y the lines at positions along Y
  (each runs in X). When the text does not say which direction is X, the first plan dimension it gives is X.
- Give bays_ft (the bay widths, from the first line on) or coords_ft (the line positions). Labels as the text names the
  lines (e.g. 1..5 and A..D). When the text names no labels use null: the lines are then called X1, X2 ... (from the
  smallest x) and Y1, Y2 ... and you refer to them by those names. The two families never share a label.

STORIES AND FLOORS
- Story 1 runs from the base to floor 1 (the first floor above the base); story k from floor k-1 to floor k; the roof
  is floor N. Write typical stories out one by one ("5 stories, 16 ft first then 13 ft" = [16, 13, 13, 13, 13]).
- A text that calls the first floor above the base 'Level 2' or '2nd floor' means floor 1 here; put the names the
  text uses in level_names. Ranges [first, last] are inclusive story or floor numbers.

MEMBER GROUPS
- A later group overrides an earlier one where they overlap: put the general statement first ("all other beams
  W18X35, pinned") and the specific ones after it.
- Columns stand at grid intersections; x and y choose the lines ("all", or labels).
- Beams lie on grid lines between neighbouring intersections. "lines" are the lines they lie on: beams on a grid.y
  line run along X, beams on a grid.x line run along Y. With "lines": "all" give "along" ("X", "Y", or null for both).
  "bays" are spans named by the crossing lines at their ends: [["1","2"]] is the span from line 1 to line 2; a pair
  further apart covers every span between them; "all" is every span.
- connection: "moment" for moment-connected beams (moment frames), "pinned" for simple shear connections (shear tabs,
  clip angles, gravity beams). If the text does not say, null -- do not decide it.
- strong_axis: the direction the column's strong axis resists (web parallel to it). If the text does not say, null.
- Braces: "lines" are the frame lines the braced bays lie on, "bays" the braced spans, "stories" the stories.
  pattern: chevron = inverted V (the two braces meet at mid-span of the beam above); v = V (they meet at mid-span of
  the beam below); x = two crossing diagonals in one story; diagonal = one brace from the bottom of the first line of
  the bay to the top of the second; diagonal_reverse = the other way; two_story_x = an X over two stories (a chevron in
  the lower story and a V above, from the first story of the range on); eccentric = EBF with a link of link_ft at
  mid-span of the beam above. A buckling-restrained brace: brb_Asc_in2 = the core area, section null.
- "omit": true removes members an earlier group placed (setbacks, openings).
- Sections: AISC names as the text writes them (W14X90, HSS8X8X1/2, HSS10X10X5/8, PIPE8STD ...).

BASIS -- only what the text states, each with the exact words it comes from:
project.code ("ASCE 7-22" | "ASCE 7-16"), seismic.SDS, seismic.SD1, seismic.S1, seismic.TL, seismic.risk_category
("I" | "II" | "III" | "IV"), seismic.Ie, seismic.site_class ("A" "B" "BC" "C" "CD" "D" "DE" "E"), seismic.analysis
("ELF" | "MRSA"), seismic.system.X and seismic.system.Y ("SMF" "IMF" "OMF" "SCBF" "OCBF" "BRBF" "EBF" "DUAL_SMF_SCBF"
"DUAL_SMF_EBF" "DUAL_SMF_BRBF" "DUAL_IMF_SCBF"), loads.D_psf, loads.L_psf (a number, or {"floor": psf, "roof": psf}),
loads.Lr_psf, loads.snow_psf, loads.clad_psf, loads.floor_system ("one-way" | "two-way"), loads.deck_span ("X" | "Y"),
materials.W ("A992" "A572 Gr 50" "A36"), materials.HSS ("A500 Gr C" "A500 Gr B" "A1085"), and the engineer's own results
user_results.T1_s.X, user_results.T1_s.Y, user_results.V_kip.X, user_results.V_kip.Y, user_results.W_kip,
user_results.drift_pct.X, user_results.drift_pct.Y.

EXAMPLE
Text: "Office, 3 stories: 15 ft first story, 13 ft above. Grid 1-4 at 30 ft east-west, A-C at 25 ft north-south.
W14X90 columns, W14X132 in the first story, strong axis east-west. Moment frames on lines A and C with W24X76 beams,
all other beams W16X31 on shear tabs. Chevron braces HSS6X6X3/8 on lines 1 and 4 between A and B, every story.
Rigid diaphragm, fixed bases. SDS = 1.0, SD1 = 0.6, site class D. Dead load 90 psf."
JSON:
{"name": "Office",
 "grid": {"x": {"labels": ["1","2","3","4"], "bays_ft": [30,30,30], "coords_ft": null},
          "y": {"labels": ["A","B","C"], "bays_ft": [25,25], "coords_ft": null}},
 "stories_ft": [15,13,13], "level_names": null, "base_name": null, "supports": "fixed", "diaphragm": "rigid",
 "columns": [{"section": "W14X90", "x": "all", "y": "all", "stories": "all", "strong_axis": "X", "omit": false},
             {"section": "W14X132", "x": "all", "y": "all", "stories": [1,1], "strong_axis": "X", "omit": false}],
 "beams": [{"section": "W16X31", "lines": "all", "along": null, "bays": "all", "floors": "all", "connection": "pinned", "omit": false},
           {"section": "W24X76", "lines": ["A","C"], "along": null, "bays": "all", "floors": "all", "connection": "moment", "omit": false}],
 "braces": [{"section": "HSS6X6X3/8", "brb_Asc_in2": null, "lines": ["1","4"], "bays": [["A","B"]], "stories": "all",
             "pattern": "chevron", "link_ft": null}],
 "seismic_weight_kip": null,
 "basis": {"seismic.SDS": {"value": 1.0, "quote": "SDS = 1.0"}, "seismic.SD1": {"value": 0.6, "quote": "SD1 = 0.6"},
           "seismic.site_class": {"value": "D", "quote": "site class D"}, "loads.D_psf": {"value": 90, "quote": "Dead load 90 psf"}},
 "unclear": ["Which seismic force-resisting system is it in each direction (SMF in X, SCBF in Y?)", "What is the live load?"]}

Return only the JSON object."""


def read_words(text: str, chat_json) -> dict:
    """The model's reading of the text (one call; a second asking for bare JSON when the first is not JSON)."""
    try:
        spec = chat_json(SYSTEM, "Text:\n" + text, max_tokens=8000, temperature=0.0)
    except ValueError:
        spec = chat_json(SYSTEM, "Text:\n" + text + "\n\nReturn only the JSON object, nothing else.", max_tokens=8000,
                         temperature=0.0)
    if not isinstance(spec, dict):
        raise ValueError("the model did not return a JSON object")
    spec["format"] = SPEC
    return spec


# ------------------------------------------------------------------------------------------- spec -> plan
def _norm_text(s):
    s = str(s or "").replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    s = s.replace("–", "-").replace("—", "-").replace("×", "x")
    return re.sub(r"\s+", " ", s).strip().lower()


def _shape_db():
    db = {}
    for r in SH.shapes_table():
        db[r["label"].upper()] = r
    return db


def shape_label(name, db=None):
    """AISC label for a section name as typed ('w14x90', 'W 14 X 90', 'HSS8x8x.5'), or None."""
    db = db if db is not None else _shape_db()
    s = re.sub(r"\s+", "", str(name or "")).upper().replace("×", "X")
    if not s:
        return None
    if s in db:
        return db[s]["label"]
    m = re.match(r"^(HSS[\d.]+X[\d.]+X)(0?\.\d+)$", s)
    if m:
        t = float(m.group(2))
        for frac in ("1/8", "3/16", "1/4", "5/16", "3/8", "7/16", "1/2", "5/8", "3/4", "7/8"):
            a, b = frac.split("/")
            if abs(float(a) / float(b) - t) < 0.002 and (m.group(1) + frac) in db:
                return db[m.group(1) + frac]["label"]
    return None


def _num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _axis(g, fam, needs):
    """[(label, coord_ft)] of one grid family."""
    g = g or {}
    coords = g.get("coords_ft")
    bays = g.get("bays_ft")
    if isinstance(coords, list) and len(coords) >= 2 and all(_num(c) is not None for c in coords):
        cs = [float(c) for c in coords]
    elif isinstance(bays, list) and bays and all((_num(b) or 0) > 0 for b in bays):
        cs = [0.0]
        for b in bays:
            cs.append(cs[-1] + float(b))
    else:
        needs.append("the grid in %s: give the bay widths (e.g. '3 bays of 30 ft in %s') or the grid line positions" % (fam, fam))
        return []
    if any(b <= a for a, b in zip(cs, cs[1:])):
        needs.append("the grid lines in %s must be given in increasing order with no repeats" % fam)
        return []
    labels = g.get("labels")
    if isinstance(labels, list) and len(labels) == len(cs) and all(str(x).strip() for x in labels):
        labs = [str(x).strip() for x in labels]
        if len(set(labs)) != len(labs):
            needs.append("the grid lines in %s repeat a label (%s)" % (fam, ", ".join(labs)))
    else:
        labs = ["%s%d" % (fam, i + 1) for i in range(len(cs))]
    return list(zip(labs, [round(c, 6) for c in cs]))


def _list(v):
    """A label list as the model may write it: ["A", "D"], "A, D", "A and D", 3."""
    if isinstance(v, (int, float)):
        return [str(v).rstrip("0").rstrip(".") if isinstance(v, float) else str(v)]
    if isinstance(v, str):
        return [x for x in re.split(r"\s*(?:,|;|\band\b|&)\s*", v.strip()) if x]
    return [str(x).strip() for x in v or []]


def _pair(pr):
    """A span as the model may write it: ["A", "B"], "A-B", "A to B"."""
    if isinstance(pr, str):
        parts = [x for x in re.split(r"\s*(?:-|\u2013|\bto\b|/)\s*", pr.strip()) if x]
        return parts if len(parts) == 2 else pr
    if isinstance(pr, (list, tuple)):
        return [str(x).strip() for x in pr]
    return pr


def _range(v, n, what, needs, where):
    if v in (None, "all", "All", "ALL", []):
        return list(range(1, n + 1))
    if isinstance(v, (int, float)):
        v = [v, v]
    if isinstance(v, list) and len(v) == 2 and all(_num(x) is not None for x in v):
        a, b = int(_num(v[0])), int(_num(v[1]))
        a, b = min(a, b), max(a, b)
        if 1 <= a and b <= n:
            return list(range(a, b + 1))
    needs.append("%s: the %s %s are not between 1 and %d" % (where, what, json.dumps(v), n))
    return []


def check(spec: dict, text: str = ""):
    """spec -> (plan, needs, warnings). needs: what the words must still say before a model can be built."""
    needs, warns, not_taken = [], [], []
    db = _shape_db()
    if not db:
        needs.append("the AISC shapes table (HR Steel's steel_engine/aisc_shapes.csv) was not found: start this page "
                     "from the hub")
    gx = _axis((spec.get("grid") or {}).get("x"), "X", needs)
    gy = _axis((spec.get("grid") or {}).get("y"), "Y", needs)
    xl, yl = dict(gx), dict(gy)
    if set(xl) & set(yl):
        needs.append("the grid labels %s are used in both directions: give the two families different labels"
                     % ", ".join(sorted(set(xl) & set(yl))))
    hs = spec.get("stories_ft")
    if not (isinstance(hs, list) and hs and all((_num(h) or 0) > 0 for h in hs)):
        needs.append("the story heights (e.g. '4 stories, 15 ft first story, 13 ft typical')")
        hs = []
    hs = [float(h) for h in hs]
    elev = [0.0]
    for h in hs:
        elev.append(round(elev[-1] + h, 6))
    n = len(hs)
    used = {}

    def section(raw, where):
        lab = shape_label(raw, db) if db else None
        if lab is None:
            if raw in (None, ""):
                needs.append("%s: the section" % where)
            else:
                needs.append("%s: '%s' is not an AISC shape name" % (where, raw))
            return None
        r = db[lab.upper()]
        if not all(_num(r.get(k)) for k in ("A", "Ix", "Iy")):
            needs.append("%s: the AISC table has no A / Ix / Iy for %s" % (where, lab))
            return None
        used[lab] = dict(A=r["A"], Ix=r["Ix"], Iy=r["Iy"], J=r["J"] if _num(r.get("J")) else round(0.01 * r["A"], 4),
                         type=r.get("type"))
        return lab

    def labels(v, fam, where):
        allowed = xl if fam == "X" else yl
        if v in (None, "all", "All", "ALL"):
            return list(allowed)
        out = []
        for s in _list(v):
            if s in allowed:
                out.append(s)
            else:
                needs.append("%s: there is no grid line %s in %s (lines %s)" % (where, s, fam, ", ".join(allowed)))
        return out

    # ---- columns {(x label, y label, story): (section, strong axis)}
    cols = {}
    for k, c in enumerate(spec.get("columns") or [], 1):
        where = "column group %d" % k
        omit = bool(c.get("omit"))
        sec = None if omit else section(c.get("section"), where + " (%s)" % (c.get("section") or "no section"))
        sa = str(c.get("strong_axis") or "").upper() or None
        if not omit and sa not in ("X", "Y"):
            needs.append("%s (%s): which way the columns' strong axis points -- X or Y (e.g. 'columns with the strong "
                         "axis in X')" % (where, c.get("section") or "?"))
        for x in labels(c.get("x"), "X", where):
            for y in labels(c.get("y"), "Y", where):
                for s in _range(c.get("stories"), n, "stories", needs, where):
                    if omit:
                        cols.pop((x, y, s), None)
                    elif sec:
                        cols[(x, y, s)] = (sec, sa)
    if not cols and n and gx and gy:
        needs.append("the columns: their section and where they stand (e.g. 'W14X90 columns at every grid intersection')")

    # ---- beams {(floor, along, line label, i label, j label): (section, connection)}
    beams = {}
    for k, b in enumerate(spec.get("beams") or [], 1):
        where = "beam group %d" % k
        omit = bool(b.get("omit"))
        sec = None if omit else section(b.get("section"), where + " (%s)" % (b.get("section") or "no section"))
        conn = str(b.get("connection") or "").lower() or None
        if not omit and conn not in ("moment", "pinned"):
            needs.append("%s (%s): whether these beams are moment-connected or pinned" % (where, b.get("section") or "?"))
        lines = b.get("lines")
        fams = []
        if lines in (None, "all", "All", "ALL"):
            al = str(b.get("along") or "").upper()
            fams = [("Y", None)] if al == "X" else ([("X", None)] if al == "Y" else [("Y", None), ("X", None)])
        else:
            for s in _list(lines):
                if s in yl:
                    fams.append(("Y", s))
                elif s in xl:
                    fams.append(("X", s))
                else:
                    needs.append("%s: there is no grid line %s" % (where, s))
        floors = _range(b.get("floors"), n, "floors", needs, where)
        for fam, line in fams:
            on = [line] if line else list(yl if fam == "Y" else xl)
            cross = gx if fam == "Y" else gy                      # beams on a Y line span between X lines
            cl = [c[0] for c in cross]
            spans = set()
            bays = b.get("bays")
            if bays in (None, "all", "All", "ALL"):
                spans = set(range(len(cross) - 1))
            else:
                for pr in [_pair(x) for x in (bays if isinstance(bays, list) else [bays])]:
                    if isinstance(pr, list) and len(pr) == 2 and all(str(p).strip() in cl for p in pr):
                        i0, i1 = sorted(cl.index(str(p).strip()) for p in pr)
                        spans.update(range(i0, i1))
                    else:
                        needs.append("%s: the span %s is not two grid lines crossing line %s" % (where, json.dumps(pr), line or "the lines"))
            for ln in on:
                for f in floors:
                    for i in sorted(spans):
                        key = (f, "X" if fam == "Y" else "Y", ln, cl[i], cl[i + 1])
                        if omit:
                            beams.pop(key, None)
                        elif sec:
                            beams[key] = (sec, conn)

    # ---- braces [(story, frame along, line, a, b, pattern, section | None, Asc | None, link_ft)]
    braces = []
    for k, br in enumerate(spec.get("braces") or [], 1):
        where = "brace group %d" % k
        asc = _num(br.get("brb_Asc_in2"))
        sec = None
        if asc is None or asc <= 0:
            asc = None
            sec = section(br.get("section"), where + " (%s)" % (br.get("section") or "no section"))
        pat = str(br.get("pattern") or "").lower().replace("-", "_").replace(" ", "_")
        pat = {"inverted_v": "chevron", "x_brace": "x", "single_diagonal": "diagonal", "ebf": "eccentric",
               "2_story_x": "two_story_x", "multi_story_x": "two_story_x"}.get(pat, pat)
        if pat not in PATTERNS:
            needs.append("%s: the brace pattern (chevron, V, X, single diagonal, two-story X or eccentric)" % where)
            continue
        link = _num(br.get("link_ft"))
        if pat == "eccentric" and not (link and link > 0):
            needs.append("%s: the link length of the eccentric braces" % where)
            continue
        stories = _range(br.get("stories"), n, "stories", needs, where)
        if pat == "two_story_x":
            if len(stories) % 2:
                needs.append("%s: a two-story X needs an even number of stories (got %s)" % (where, stories))
                continue
        for ln in _list(br.get("lines")):
            if ln in yl:
                along, cross = "X", gx
            elif ln in xl:
                along, cross = "Y", gy
            else:
                needs.append("%s: there is no grid line %s" % (where, ln))
                continue
            cl = [c[0] for c in cross]
            bays = br.get("bays") or []
            for pr in [_pair(x) for x in (bays if isinstance(bays, list) else [bays])]:
                if not (isinstance(pr, list) and len(pr) == 2 and all(str(p).strip() in cl for p in pr) and str(pr[0]).strip() != str(pr[1]).strip()):
                    needs.append("%s: the bay %s is not two grid lines crossing line %s" % (where, json.dumps(pr), ln))
                    continue
                a, b = sorted((str(p).strip() for p in pr), key=cl.index)
                span = dict(cross)[b] - dict(cross)[a]
                if pat == "eccentric" and link >= span:
                    needs.append("%s: the link (%g ft) is not shorter than the bay (%g ft)" % (where, link, span))
                    continue
                if pat == "two_story_x":
                    for s in stories[0::2]:
                        braces.append((s, along, ln, a, b, "chevron", sec, asc, None))
                        braces.append((s + 1, along, ln, a, b, "v", sec, asc, None))
                else:
                    for s in stories:
                        braces.append((s, along, ln, a, b, pat, sec, asc, link))
        if not br.get("lines") or not br.get("bays"):
            needs.append("%s: on which grid lines and in which bays the braces are" % where)

    # ---- the rest
    sup = str(spec.get("supports") or "").lower() or None
    sup = sup if sup in ("fixed", "pinned") else None
    dia = str(spec.get("diaphragm") or "").lower().replace(" ", "-") or None
    dia = {"semirigid": "semi-rigid"}.get(dia, dia)
    dia = dia if dia in ("rigid", "flexible", "semi-rigid") else None
    ws = spec.get("seismic_weight_kip")
    weights = None
    if isinstance(ws, list) and len(ws) == n and all((_num(w) or 0) > 0 for w in ws):
        weights = [float(w) for w in ws]
    elif ws not in (None, [], ""):
        warns.append("the seismic weights %s are not one positive value per floor (%d floors): not used" % (json.dumps(ws), n))
    names = spec.get("level_names")
    floor_names = None
    if isinstance(names, list) and len(names) == n and len({str(x).strip() for x in names}) == n and all(str(x).strip() for x in names):
        floor_names = [str(x).strip() for x in names]
    elif names not in (None, []):
        warns.append("the level names %s do not name each of the %d floors once: the levels are called L1 .. L%d"
                     % (json.dumps(names), n, n))
    base_name = str(spec.get("base_name") or "").strip() or "Base"
    if floor_names and base_name in floor_names:
        base_name = "Base"

    tn = _norm_text(text)
    basis = {}
    for k, v in (spec.get("basis") or {}).items():
        if k not in BASIS_ITEMS and k not in RESULT_ITEMS:
            continue
        if not isinstance(v, dict) or v.get("value") in (None, ""):
            continue
        q = str(v.get("quote") or "")
        if text and (not q.strip() or _norm_text(q) not in tn):
            not_taken.append("%s = %s (the words it was read from, '%s', are not in your text)" % (k, json.dumps(v.get("value")), q))
            continue
        basis[k] = (v.get("value"), q)

    # ---- what the plan leaves out
    if cols and n:
        for f in range(1, n + 1):
            at = {(x, y) for (x, y, s) in cols if s in (f, f + 1)}
            missing = []
            for (lab, c) in gy:
                for (a, ca), (b, cb) in zip(gx, gx[1:]):
                    if (a, lab) in at and (b, lab) in at and (f, "X", lab, a, b) not in beams:
                        missing.append("%s-%s on %s" % (a, b, lab))
            for (lab, c) in gx:
                for (a, ca), (b, cb) in zip(gy, gy[1:]):
                    if (lab, a) in at and (lab, b) in at and (f, "Y", lab, a, b) not in beams:
                        missing.append("%s-%s on %s" % (a, b, lab))
            if missing:
                warns.append("floor %d: no beam between the columns %s%s -- say which beams go there if this is not an "
                             "opening" % (f, ", ".join(missing[:8]), " and %d more" % (len(missing) - 8) if len(missing) > 8 else ""))
    unclear = [str(u) for u in (spec.get("unclear") or []) if str(u).strip()]
    plan = dict(gx=gx, gy=gy, elev=elev, n=n, cols=cols, beams=beams, braces=braces, supports=sup, diaphragm=dia,
                weights=weights, floor_names=floor_names, base_name=base_name, sections=used, basis=basis,
                name=str(spec.get("name") or "").strip() or None, unclear=unclear, not_taken=not_taken,
                warnings=_dedupe(warns))
    return plan, _dedupe(needs), _dedupe(warns)


def _dedupe(xs):
    out = []
    for x in xs:
        if x not in out:
            out.append(x)
    return out


# ------------------------------------------------------------------------------------------- plan -> OpenSeesPy
def script(plan: dict, text: str = "", model: str = "") -> str:
    """The plan as an OpenSeesPy script (kip, in; Z up). It builds the model only -- no analysis."""
    xs, ys, elev = dict(plan["gx"]), dict(plan["gy"]), plan["elev"]
    nodes = {}

    def nid(x_ft, y_ft, z_ft):
        k = (round(x_ft, 6), round(y_ft, 6), round(z_ft, 6))
        if k not in nodes:
            nodes[k] = len(nodes) + 1
        return nodes[k]

    def pt(along, line, s_ft):
        """Plan point of a position s_ft along a frame line."""
        return (s_ft, ys[line]) if along == "X" else (xs[line], s_ft)

    # split points on beams: chevron / V work points and link ends {(floor, along, line, a, b): {s_ft}}
    splits = {}
    brace_el = []                                    # (P, Q, section, Asc, frame along, comment)
    for (s, along, ln, a, b, pat, sec, asc, link) in plan["braces"]:
        cross = xs if along == "X" else ys
        ca, cb = cross[a], cross[b]
        mid = round((ca + cb) / 2.0, 6)
        z0, z1 = elev[s - 1], elev[s]
        tag = "%s %s-%s story %d %s" % (ln, a, b, s, pat)
        if pat == "chevron":
            ends = [((ca, z0), (mid, z1)), ((cb, z0), (mid, z1))]
            splits.setdefault((s, along, ln, a, b), set()).add(mid)
        elif pat == "v":
            ends = [((ca, z1), (mid, z0)), ((cb, z1), (mid, z0))]
            if s - 1 >= 1:
                splits.setdefault((s - 1, along, ln, a, b), set()).add(mid)
        elif pat == "x":
            ends = [((ca, z0), (cb, z1)), ((cb, z0), (ca, z1))]
        elif pat == "diagonal":
            ends = [((ca, z0), (cb, z1))]
        elif pat == "diagonal_reverse":
            ends = [((cb, z0), (ca, z1))]
        else:                                        # eccentric
            e = link / 2.0
            ends = [((ca, z0), (round(mid - e, 6), z1)), ((cb, z0), (round(mid + e, 6), z1))]
            splits.setdefault((s, along, ln, a, b), set()).update({round(mid - e, 6), round(mid + e, 6)})
        for (s0, za), (s1, zb) in ends:
            P, Q = pt(along, ln, s0) + (za,), pt(along, ln, s1) + (zb,)
            brace_el.append((P, Q, sec, asc, along, tag))

    secs = plan["sections"]
    out = ['"""%s -- OpenSeesPy model written by Steltic from a description in plain words%s.' % (
        plan.get("name") or "Building", (" (read by %s)" % model) if model else ""),
        "",
        "Units kip, in, s; Z up. It builds the model only (no analysis); the Steltic OpenSees interpreter reads it into",
        "HR Steel's building description. Sections carry the AISC table's own properties. The description:",
        ""]
    for ln_ in (text or "").strip().splitlines():
        out.append("    " + ln_.replace('"""', "'''").replace("\\", "/"))
    out += ['"""', "import openseespy.opensees as ops", "", "E, G = %g, %g                # ksi" % (E_KSI, G_KSI),
            "ft = 12.0                         # in", "", "SECTIONS = {                      # in2, in4 (AISC shapes table)"]
    for lab in sorted(secs):
        p = secs[lab]
        out.append('    "%s": dict(A=%r, Ix=%r, Iy=%r, J=%r),' % (lab, p["A"], p["Ix"], p["Iy"], p["J"]))
    out += ["}", "",
            "def column(name):                 # local z = the strong axis' bending axis: Iz = Ix",
            "    s = SECTIONS[name]",
            '    return s["A"], E, G, s["J"], s["Iy"], s["Ix"]',
            "",
            "def beam(name):                   # vecxz vertical: local z is vertical, gravity bends about local y: Iy = Ix",
            "    s = SECTIONS[name]",
            '    return s["A"], E, G, s["J"], s["Ix"], s["Iy"]',
            "",
            "def brace(name):",
            "    s = SECTIONS[name]",
            '    return s["A"], E, G, s["J"], s["Iy"], s["Ix"]',
            "",
            "",
            "ops.wipe()",
            'ops.model("basic", "-ndm", 3, "-ndf", 6)',
            'ops.geomTransf("PDelta", 1, 0.0, 1.0, 0.0)    # columns, strong axis resisting X',
            'ops.geomTransf("PDelta", 2, 1.0, 0.0, 0.0)    # columns, strong axis resisting Y',
            'ops.geomTransf("Linear", 3, 0.0, 0.0, 1.0)    # beams',
            'ops.geomTransf("Linear", 4, 0.0, 1.0, 0.0)    # braces in frames along X',
            'ops.geomTransf("Linear", 5, 1.0, 0.0, 0.0)    # braces in frames along Y',
            'ops.uniaxialMaterial("Elastic", 1, E)          # buckling-restrained brace cores',
            ""]
    el = []
    # columns
    for (x, y, s), (sec, sa) in sorted(plan["cols"].items(), key=lambda kv: (kv[0][2], kv[0][1], kv[0][0])):
        i, j = nid(xs[x], ys[y], elev[s - 1]), nid(xs[x], ys[y], elev[s])
        el.append('ops.element("elasticBeamColumn", {t}, %d, %d, *column("%s"), %d)    # column %s/%s story %d'
                  % (i, j, sec, 1 if sa == "X" else 2, x, y, s))
    # beams, chained through their split points
    for (f, along, ln, a, b), (sec, conn) in sorted(plan["beams"].items()):
        cross = xs if along == "X" else ys
        pts = sorted({cross[a], cross[b]} | splits.get((f, along, ln, a, b), set()))
        for k, (s0, s1) in enumerate(zip(pts, pts[1:])):
            P, Q = pt(along, ln, s0), pt(along, ln, s1)
            i, j = nid(P[0], P[1], elev[f]), nid(Q[0], Q[1], elev[f])
            rel = ""
            if conn == "pinned":
                code = (1 if k == 0 else 0) + (2 if k == len(pts) - 2 else 0)
                rel = ', "-releasey", %d' % code if code else ""
            el.append('ops.element("elasticBeamColumn", {t}, %d, %d, *beam("%s"), 3%s)    # beam %s %s-%s floor %d%s'
                      % (i, j, sec, rel, ln, a, b, f, " (%s)" % conn if k == 0 else ""))
    # braces
    for P, Q, sec, asc, along, tag in brace_el:
        i, j = nid(*P), nid(*Q)
        if asc:
            el.append('ops.element("truss", {t}, %d, %d, %r, 1)    # BRB %s, Asc %g in2' % (i, j, asc, tag, asc))
        else:
            el.append('ops.element("elasticBeamColumn", {t}, %d, %d, *brace("%s"), %d, "-releasey", 3, "-releasez", 3)    # brace %s'
                      % (i, j, sec, 4 if along == "X" else 5, tag))
    out.append("# nodes: x, y, z in inches")
    for (x, y, z), t in sorted(nodes.items(), key=lambda kv: kv[1]):
        out.append("ops.node(%d, %s * ft, %s * ft, %s * ft)" % (t, _f(x), _f(y), _f(z)))
    out.append("")
    if plan["supports"]:
        out.append("# %s bases" % plan["supports"])
    else:
        out.append("# bases: the description does not say fixed or pinned -- fixed here only to mark the base; HR asks")
    fx = "1, 1, 1, 0, 0, 0" if plan["supports"] == "pinned" else "1, 1, 1, 1, 1, 1"
    for (x, y, z), t in sorted(nodes.items(), key=lambda kv: kv[1]):
        if abs(z) < 1e-9:
            out.append("ops.fix(%d, %s)" % (t, fx))
    out.append("")
    out.append("# members")
    for k, line in enumerate(el, 1):
        out.append(line.replace("{t}", str(k)))
    out.append("")
    # floors: diaphragms and masses
    master = 100000
    for f in range(1, plan["n"] + 1):
        z = elev[f]
        at = [(t, x, y) for (x, y, zz), t in nodes.items() if abs(zz - z) < 1e-9]
        if not at:
            continue
        w = plan["weights"][f - 1] if plan["weights"] else None
        x0, x1 = min(a[1] for a in at), max(a[1] for a in at)
        y0, y1 = min(a[2] for a in at), max(a[2] for a in at)
        if plan["diaphragm"] == "rigid":
            m = master + f
            cx, cy = sum(a[1] for a in at) / len(at), sum(a[2] for a in at) / len(at)
            out.append("# floor %d: rigid diaphragm%s" % (f, (", seismic weight %g kip" % w) if w else ""))
            out.append("ops.node(%d, %s * ft, %s * ft, %s * ft)" % (m, _f(cx), _f(cy), _f(z)))
            out.append("ops.fix(%d, 0, 0, 1, 1, 1, 0)" % m)
            out.append("ops.rigidDiaphragm(3, %d, %s)" % (m, ", ".join(str(a[0]) for a in sorted(at))))
            if w:
                ms = w / G_IN
                irot = ms * (((x1 - x0) * 12.0) ** 2 + ((y1 - y0) * 12.0) ** 2) / 12.0
                out.append("ops.mass(%d, %r, %r, 0.0, 0.0, 0.0, %r)    # W / g, kip-s2/in" % (m, round(ms, 6), round(ms, 6), round(irot, 3)))
        elif w:
            ms = w / G_IN / len(at)
            out.append("# floor %d: seismic weight %g kip over its %d nodes" % (f, w, len(at)))
            for t, _x, _y in sorted(at):
                out.append("ops.mass(%d, %r, %r, 0.0, 0.0, 0.0, 0.0)" % (t, round(ms, 6), round(ms, 6)))
        out.append("")
    return "\n".join(out) + "\n"


def _f(v):
    return ("%.6f" % v).rstrip("0").rstrip(".") if v != int(v) else "%d.0" % v


# ------------------------------------------------------------------------------------------- description touches
def finish(desc: dict, report: dict, plan: dict, model: str = "", text: str = "", script_name: str = "") -> dict:
    """Name the sections, label the grid and levels as the text does, put the basis in as proposals."""
    props = desc.setdefault("_proposals", {})
    notes = desc.setdefault("_intake", {}).setdefault("notes", [])
    # ---- sections: the script carries the table's own properties, so each property set is one named shape
    raw2lab, raw2brb = {}, {}
    used = plan["sections"]
    for row in report.get("sections") or []:
        raw = row.get("name")
        a, ix, iy = _num(row.get("A_in2")), _num(row.get("Ix_in4")), _num(row.get("Iy_in4"))
        if raw and raw.startswith("OpenSees truss") and a:
            raw2brb[raw] = a
            continue
        if not (raw and a and ix and iy):
            continue
        hit = [lab for lab, p in used.items()
               if abs(p["A"] - a) <= 0.005 * p["A"] and abs(p["Ix"] - ix) <= 0.005 * p["Ix"] and abs(p["Iy"] - iy) <= 0.005 * p["Iy"]]
        if len(hit) == 1:
            raw2lab[raw] = hit[0]
    for kind in ("columns", "beams", "braces"):
        for mbr in desc.get(kind) or []:
            raw = mbr.get("section")
            if raw in raw2lab:
                mbr["section"] = raw2lab[raw]
            elif kind == "braces" and raw in raw2brb:
                mbr["section"] = None
                mbr["brb"] = {"Asc_in2": round(raw2brb[raw], 4)}
    for raw in list(raw2lab) + list(raw2brb):
        props.pop("section_map/" + _urlquote(str(raw), safe=""), None)
    left = [r.get("name") for r in report.get("sections") or [] if r.get("name") not in raw2lab and r.get("name") not in raw2brb]
    if left:
        notes.append("sections not recognised as the named shapes: %s" % "; ".join(left))
    for row in report.get("sections") or []:
        if row.get("name") in raw2lab:
            row["named"] = raw2lab[row["name"]]
        elif row.get("name") in raw2brb:
            row["named"] = "BRB Asc %g in2" % raw2brb[row["name"]]

    # ---- grid labels and level names as the text has them
    xmap = _label_map(desc["grid"]["x"], plan["gx"])
    ymap = _label_map(desc["grid"]["y"], plan["gy"])
    lmap = {}
    lv = desc.get("levels") or []
    if len(lv) == plan["n"] + 1 and all(abs(l["elev_ft"] - e) < 0.05 for l, e in zip(lv, plan["elev"])):
        lmap[lv[0]["name"]] = plan["base_name"]
        for k in range(1, len(lv)):
            lmap[lv[k]["name"]] = (plan["floor_names"] or ["L%d" % (i + 1) for i in range(plan["n"])])[k - 1]
    elif lv:
        notes.append("the model's levels (%s) are not the %d floors described: the level names are the reader's"
                     % (", ".join(l["name"] for l in lv), plan["n"]))
    _relabel(desc, xmap, ymap, lmap)

    # ---- what the plan says beyond the script
    if plan["diaphragm"] in ("flexible", "semi-rigid"):
        desc["diaphragm"] = plan["diaphragm"]
        desc.setdefault("_provenance", {})["diaphragm"] = {"source": "file", "note": "from your description"}
    elif plan["diaphragm"] is None:
        desc["diaphragm"] = None
    if not plan["supports"]:
        desc["supports"] = {}                     # the script fixes the base only to mark it: HR asks
    if plan.get("name"):
        desc.setdefault("project", {})["name"] = plan["name"]
    ur = desc.get("user_results") or {}
    for k, (v, q) in plan["basis"].items():
        if k in RESULT_ITEMS:
            parts = k.split(".")[1:]
            val = _num(v)
            if val is None:
                continue
            if len(parts) == 1:
                ur[parts[0]] = val
            else:
                ur.setdefault(parts[0], {})[parts[1]] = val
            continue
        path = BASIS_ITEMS[k]
        val = v
        if isinstance(v, dict):
            val = {lmap.get(kk, kk): vv for kk, vv in v.items()}
        props[path] = {"value": val, "reason": 'from your description: "%s"' % q.strip()}
    if ur:
        desc["user_results"] = ur
        desc.setdefault("_provenance", {})["user_results"] = {"source": "file", "note": "from your description (and the seismic weights)"}

    # ---- where it came from
    src = desc.setdefault("source", {})
    src.update(kind="opensees", file=script_name or src.get("file"), described=True,
               text_sha256=hashlib.sha256((text or "").encode("utf-8")).hexdigest())
    lead = ["built from your description in plain words%s: it became the OpenSeesPy script shown under step 1, which "
            "the OpenSees interpreter read -- check the 3-D model before you confirm it" % ((" (read by %s)" % model) if model else "")]
    lead += ["check: %s" % x for x in plan.get("warnings") or []]
    lead += ["not taken from the description: %s" % x for x in plan.get("not_taken") or []]
    lead += ["the description leaves open: %s" % x for x in plan.get("unclear") or []]
    desc["_intake"]["notes"] = lead + [x for x in notes if not x.startswith("load pattern")]
    return desc


def _label_map(grid_entries, plan_axis):
    out = {}
    for g in grid_entries:
        for lab, c in plan_axis:
            if abs(g["coord_ft"] - c) < 0.01:
                out[g["label"]] = lab
    return out


def _relabel(desc, xmap, ymap, lmap):
    def pt(d):
        if not isinstance(d, dict):
            return
        if isinstance(d.get("x"), str) and d["x"] in xmap:
            d["x"] = xmap[d["x"]]
        if isinstance(d.get("y"), str) and d["y"] in ymap:
            d["y"] = ymap[d["y"]]
        for k in ("level", "base_level", "top_level"):
            if isinstance(d.get(k), str) and d[k] in lmap:
                d[k] = lmap[d[k]]

    for g in desc["grid"]["x"]:
        g["label"] = xmap.get(g["label"], g["label"])
    for g in desc["grid"]["y"]:
        g["label"] = ymap.get(g["label"], g["label"])
    for l in desc.get("levels") or []:
        l["name"] = lmap.get(l["name"], l["name"])
    for kind in ("columns", "beams", "braces"):
        for m in desc.get(kind) or []:
            pt(m)
            pt(m.get("i"))
            pt(m.get("j"))
            if isinstance(m.get("id"), str) and "@" in m["id"]:
                a, b = m["id"].split("@", 1)
                m["id"] = "%s@%s" % (a, lmap.get(b, b))
    for p in (desc.get("supports") or {}).get("at") or []:
        pt(p)
    for k in ("diaphragm_exclude", "openings"):
        for p in desc.get(k) or []:
            pt(p)
    for p in (desc.get("details") or {}).get("doublers") or []:
        pt(p)
    for v in (desc.get("_proposals") or {}).values():
        if isinstance(v, dict) and isinstance(v.get("value"), dict):
            v["value"] = {lmap.get(k, k): x for k, x in v["value"].items()}
