"""Values typed beside an uploaded file -> answers to what the file leaves open, as proposals to accept.

The file (template, .e2k, OpenSees model) builds the model; the text gives what the file does not: the seismic basis,
loads, systems, grades, a section the reader could not name, the engineer's own T1 / V / W. Two readers, both held
to the words actually typed:

  * `name = value` (or `name: value`) pieces are read directly, no model needed -- `SDS = 1.0`, `site class = D`,
    `system X = SMF`, `D = 90 psf`, `deck span = X`, `T1 X = 0.82 s`, or a question's id as step 2 shows it
    (`seismic.SDS = 1.0`);
  * sentences are read by the hub's model connection against the list of questions (SYSTEM), and each value must come
    with the words it was read from, found in the text, or it is not taken.

A value becomes a PROPOSAL on its question (HR's `_proposals[<path>]`), its reason quoting the words: HR shows it in
step 2 as a default, used only once accepted. A value for a question HR asks only later (R once the system is set,
Ie once the risk category is, the deck span once the floors are one-way) is proposed the same way and appears with its
question. A value for something the file already gives is not used -- the file wins, and the report says so. The
engineer's own results (T1, V, W, drift) go to the confirmation check, which only warns.
"""
from __future__ import annotations

import json
import re

ORIGIN = "from your text"

# Questions HR asks about the building as a whole, by id (path = the id with '/' for '.'): text can answer these even
# before HR asks them (HR asks R only once the system is known, ...). id -> (question, unit kind, options)
_BOTH = ("X", "Y")
KNOWN = {
    "project.code": ("Which code edition is the building designed to?", None, None),
    "seismic.SDS": ("Design spectral acceleration SDS (g)", "g", None),
    "seismic.SD1": ("Design spectral acceleration SD1 (g)", "g", None),
    "seismic.S1": ("Mapped spectral acceleration S1 (g)", "g", None),
    "seismic.TL": ("Long-period transition period TL (s)", "s", None),
    "seismic.Ie": ("Seismic importance factor Ie", "1", None),
    "seismic.risk_category": ("Risk Category", None, ["I", "II", "III", "IV"]),
    "seismic.site_class": ("Site class", None, ["A", "B", "BC", "C", "CD", "D", "DE", "E"]),
    "seismic.analysis": ("Linear analysis procedure (ELF or MRSA)", None, ["ELF", "MRSA"]),
    "loads.D_psf": ("Dead load D (psf; one value, or floor / roof, or per level)", "psf", None),
    "loads.L_psf": ("Floor live load L (psf; one value, or floor / roof, or per level)", "psf", None),
    "loads.Lr_psf": ("Roof live load Lr (psf)", "psf", None),
    "loads.snow_psf": ("Flat-roof snow load pf (psf)", "psf", None),
    "loads.clad_psf": ("Exterior wall (cladding) weight (psf)", "psf", None),
    "loads.parapet_ft": ("Parapet height above the roof (ft)", "ft", None),
    "loads.floor_system": ("Floor load distribution: one-way or two-way", None, ["one-way", "two-way"]),
    "loads.deck_span": ("One-way floors: does the deck span in X or Y?", None, ["X", "Y"]),
    "materials.W": ("W-shape steel grade", None, None),
    "materials.HSS": ("HSS steel grade", None, None),
    "supports.default": ("Column bases: fixed or pinned?", None, ["fixed", "pinned"]),
    "diaphragm": ("Floor diaphragms: rigid, flexible or semi-rigid?", None, ["rigid", "flexible", "semi-rigid"]),
    "details.doublers": ("Panel-zone doubler plates (or none)", None, None),
}
for _d in _BOTH:
    KNOWN["seismic.system.%s" % _d] = ("Seismic force-resisting system in the %s direction" % _d, None, None)
    KNOWN["seismic.R.%s" % _d] = ("Response modification coefficient R in the %s direction" % _d, "1", None)
    KNOWN["seismic.Cd.%s" % _d] = ("Deflection amplification factor Cd in the %s direction" % _d, "1", None)
    KNOWN["seismic.Om0.%s" % _d] = ("Overstrength factor Omega0 in the %s direction" % _d, "1", None)
    KNOWN["seismic.rho.%s" % _d] = ("Redundancy factor rho in the %s direction" % _d, "1", None)

USER_RESULTS = {
    "user_results.T1_s.X": ("Your own analysis: fundamental period T1 in X (s)", "s"),
    "user_results.T1_s.Y": ("Your own analysis: fundamental period T1 in Y (s)", "s"),
    "user_results.V_kip.X": ("Your own analysis: design base shear V in X (kip)", "kip"),
    "user_results.V_kip.Y": ("Your own analysis: design base shear V in Y (kip)", "kip"),
    "user_results.W_kip": ("Your own analysis: effective seismic weight W (kip)", "kip"),
    "user_results.drift_pct.X": ("Your own analysis: maximum story drift in X (% of the story height)", "%"),
    "user_results.drift_pct.Y": ("Your own analysis: maximum story drift in Y (% of the story height)", "%"),
}

# `name = value` names (lower case, single spaces) -> the question ids they answer. `id#part` is one part of a
# floor / roof load.
_XY = lambda k: ["%s.X" % k, "%s.Y" % k]
NAMES = {
    "sds": ["seismic.SDS"], "sd1": ["seismic.SD1"], "s1": ["seismic.S1"], "tl": ["seismic.TL"], "ie": ["seismic.Ie"],
    "importance factor": ["seismic.Ie"],
    "risk category": ["seismic.risk_category"], "site class": ["seismic.site_class"],
    "analysis": ["seismic.analysis"], "analysis procedure": ["seismic.analysis"],
    "code": ["project.code"], "code edition": ["project.code"], "design code": ["project.code"],
    "d": ["loads.D_psf"], "dead": ["loads.D_psf"], "dead load": ["loads.D_psf"],
    "l": ["loads.L_psf"], "live": ["loads.L_psf"], "live load": ["loads.L_psf"],
    "lr": ["loads.Lr_psf"], "roof live": ["loads.Lr_psf"], "roof live load": ["loads.Lr_psf"],
    "snow": ["loads.snow_psf"], "snow load": ["loads.snow_psf"], "pf": ["loads.snow_psf"],
    "cladding": ["loads.clad_psf"], "cladding weight": ["loads.clad_psf"], "clad": ["loads.clad_psf"],
    "parapet": ["loads.parapet_ft"], "parapet height": ["loads.parapet_ft"],
    "floor system": ["loads.floor_system"], "deck span": ["loads.deck_span"],
    "grade": ["materials.W"], "steel grade": ["materials.W"], "w grade": ["materials.W"], "hss grade": ["materials.HSS"],
    "supports": ["supports.default"], "bases": ["supports.default"], "column bases": ["supports.default"],
    "diaphragm": ["diaphragm"], "diaphragms": ["diaphragm"], "doublers": ["details.doublers"],
    "t1 x": ["user_results.T1_s.X"], "t1 y": ["user_results.T1_s.Y"],
    "v x": ["user_results.V_kip.X"], "v y": ["user_results.V_kip.Y"],
    "seismic weight": ["user_results.W_kip"], "w kip": ["user_results.W_kip"],
    "drift x": ["user_results.drift_pct.X"], "drift y": ["user_results.drift_pct.Y"],
}
for _n, _k in (("system", "seismic.system"), ("sfrs", "seismic.system"), ("r", "seismic.R"), ("cd", "seismic.Cd"),
               ("omega0", "seismic.Om0"), ("om0", "seismic.Om0"), ("\u03c90", "seismic.Om0"),
               ("rho", "seismic.rho"), ("\u03c1", "seismic.rho")):
    NAMES[_n] = _XY(_k)
    for _d in _BOTH:
        NAMES["%s %s" % (_n, _d.lower())] = ["%s.%s" % (_k, _d)]
for _n, _k in (("d", "loads.D_psf"), ("dead load", "loads.D_psf"), ("l", "loads.L_psf"), ("live load", "loads.L_psf")):
    for _part in ("floor", "roof"):
        NAMES["%s %s" % (_n, _part)] = ["%s#%s" % (_k, _part)]
        NAMES["%s %s" % (_part, _n)] = ["%s#%s" % (_k, _part)]

# unit kind -> {unit as typed (lower case): factor to the question's unit}
_UNITS = {
    "psf": {"": 1.0, "psf": 1.0, "lb/ft2": 1.0, "kpa": 20.885, "kn/m2": 20.885, "pa": 0.020885},
    "kip": {"": 1.0, "kip": 1.0, "kips": 1.0, "k": 1.0, "kn": 0.22481},
    "s": {"": 1.0, "s": 1.0, "sec": 1.0, "seconds": 1.0, "second": 1.0},
    "g": {"": 1.0, "g": 1.0},
    "ft": {"": 1.0, "ft": 1.0, "feet": 1.0, "'": 1.0, "m": 3.28084, "in": 1.0 / 12.0},
    "%": {"": 1.0, "%": 1.0},
    "1": {"": 1.0},
}


def _number(v, kind):
    if isinstance(v, bool):
        raise ValueError("%s is not a number" % json.dumps(v))
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"(?<=\d),(?=\d{3}\b)", "", str(v).strip())
    m = re.match(r"^([-+]?\d+(?:\.\d+)?|[-+]?\.\d+)\s*([a-z%'/0-9-]*)\s*$", s, re.I)
    if not m:
        raise ValueError("%s is not a number" % json.dumps(v))
    unit = m.group(2).lower()
    f = _UNITS.get(kind or "1", _UNITS["1"]).get(unit)
    if f is None:
        raise ValueError("'%s' is not a unit for %s" % (m.group(2), {"1": "a factor"}.get(kind, kind)))
    return round(float(m.group(1)) * f, 6)


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "").replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")
                  .replace("\u2018", "'").replace("\u2013", "-").replace("\u2014", "-")).strip().lower()


def get_path(d, path):
    """The value at a plain path; a single value given for both directions (`system: "SMF"`, `R: 8`) counts for X and
    for Y."""
    cur = d
    for s in path.split("/"):
        if not isinstance(cur, dict):
            return cur if s in _BOTH and not isinstance(cur, list) else None
        cur = cur.get(s)
        if cur is None:
            return None
    return cur


def _given(v):
    return v not in (None, "", {}, [])


def questions(missing_items: dict, raw: dict, earlier: list = None) -> list:
    """What text can answer: the open key and default questions; the questions an earlier text answered (`earlier`,
    its report's values: answered in step 2 since, they are no longer open, but HR asked them); the building-wide
    questions HR asks only later (kind 'later') where the file does not give them; the engineer's own results (kind
    'result') where the file gives none."""
    out, seen = [], set()
    for g in (missing_items or {}).get("groups") or []:
        for it in g.get("items") or []:
            if it.get("kind") in ("key", "default") and it.get("id") not in seen:
                q = dict(it)
                k = KNOWN.get(q["id"])
                if k and not q.get("options") and k[2]:
                    q["options"] = k[2]
                out.append(q)
                seen.add(q["id"])
    for v in earlier or []:
        if v.get("id") and v.get("path") and v["id"] not in seen and v.get("kind") != "your own result":
            out.append(dict(id=v["id"], kind="default" if v.get("kind") == "proposal" else "later", path=v["path"],
                            question=v.get("question"), options=v.get("options")))
            seen.add(v["id"])
    raw = raw or {}
    for iid, (q, unit, opts) in KNOWN.items():
        path = iid.replace(".", "/")
        if iid not in seen and not _given(get_path(raw, path)):
            out.append(dict(id=iid, kind="later", path=path, question=q, options=opts))
            seen.add(iid)
    for iid, (q, unit) in USER_RESULTS.items():
        path = iid.replace(".", "/")
        if not _given(get_path(raw, path)):
            out.append(dict(id=iid, kind="result", path=path, question=q))
    return out


def _unit_kind(iid):
    k = KNOWN.get(iid)
    if k:
        return k[1]
    r = USER_RESULTS.get(iid)
    return r[1] if r else None


def coerce(item: dict, value):
    """value as the question takes it; ValueError with the reason when it does not fit."""
    iid = item["id"]
    kind = _unit_kind(iid)
    if kind:
        if isinstance(value, dict):                       # floor / roof, or per level
            if not value:
                raise ValueError("no value")
            return {str(k): _number(v, kind) for k, v in value.items()}
        return _number(value, kind)
    if iid == "details.doublers":
        if isinstance(value, list):
            return value
        if str(value).strip().lower() in ("none", "no", "0", "[]", "nil", "no doublers", "not used", "n/a"):
            return []
        raise ValueError("doublers are given in step 2 (level, column, thickness), or 'none'")
    if isinstance(value, (dict, list)):
        raise ValueError("%s is not a single value" % json.dumps(value))
    s = str(value).strip().strip('"').strip()
    if not s:
        raise ValueError("no value")
    opts = item.get("options")
    if opts:
        key = lambda x: re.sub(r"[\s_\-]+", "", str(x)).upper()
        hit = [o for o in opts if key(o) == key(s)]
        if hit:
            return hit[0]
        if not iid.startswith("seismic.system"):          # HR reads 'special moment frame', 'dual SMF+SCBF' ...
            raise ValueError("'%s' is not one of %s" % (s, ", ".join(str(o) for o in opts)))
    return s


# ------------------------------------------------------------------------------------------- `name = value`
_DELIM = r"(?=\s*(?:[\n;]|(?<!\d),|,(?!\d{3}\b)|\.\s|\.?$))"     # 6,850 kip is one value
_VALUE = r"((?:[^\n;,]|(?<=\d),(?=\d{3}\b))+?)"
_ANY_PAIR = re.compile(r"(?<![\w.])([A-Za-z\u0370-\u03ff][\w .\u0370-\u03ff]{0,30}?)\s*=\s*%s%s" % (_VALUE, _DELIM), re.M)


def _names(items):
    names = dict(NAMES)
    for it in items:                                    # a question's own id works as a name (section_map.W27, ...)
        names[it["id"].lower()] = [it["id"]]
    return names


def read_pairs(text: str, items: list, raw: dict = None):
    """-> ({id: (value, words)}, notes, spans): the `name = value` / `name: value` pieces this page knows."""
    by_id = {it["id"]: it for it in items}
    names = _names(items)
    keys = sorted(names, key=len, reverse=True)
    pat = re.compile(r"(?<![\w.])(%s)\s*([:=])\s*%s%s" % (
        "|".join(r"\s+".join(re.escape(w) for w in k.split(" ")) for k in keys), _VALUE, _DELIM), re.I | re.M)
    vals, parts, notes, spans = {}, {}, [], []
    for m in pat.finditer(text):
        name = re.sub(r"\s+", " ", m.group(1)).lower()
        raw_v = m.group(3).strip()
        words = m.group(0).strip()
        got, why, bad = [], [], False
        for iid in names.get(name) or []:
            base, _, part = iid.partition("#")
            it = by_id.get(base)
            if it is None:
                v = get_path(raw or {}, base.replace(".", "/"))
                why.append(("not used: '%s' -- the file gives %s = %s, and the file's value is used" % (words, base, json.dumps(v)))
                           if _given(v) else ("not used: '%s' -- nothing asks for %s" % (words, base)))
                continue
            try:
                got.append((base, part, coerce(it, raw_v)))
            except ValueError as ex:
                why.append("not taken: '%s' -- %s" % (words, ex))
                bad = True
        if len(name) <= 2 and m.group(2) == ":" and not got and (bad or not re.match(r"[-+.\d]", raw_v)):
            continue                                    # 'Grid A-D: 4 bays' is not the dead load: left to the sentences
        spans.append(m.span())
        notes += why
        for base, part, v in got:
            if part:
                parts.setdefault(base, {})[part] = (v, words)
            else:
                vals[base] = (v, words)
    for base, pv in parts.items():
        pw = [w for _, w in pv.values()]
        if base in vals and not isinstance(vals[base][0], dict):
            notes.append("not taken: '%s' -- %s is also given as one value" % ("; ".join(pw), base))
            continue
        cur = dict(vals[base][0]) if base in vals else {}
        cur.update({k: v for k, (v, _) in pv.items()})
        vals[base] = (cur, "; ".join(([vals[base][1]] if base in vals else []) + pw))
    return vals, notes, spans


# ------------------------------------------------------------------------------------------- sentences
SYSTEM = """You read an engineer's note about a steel building whose structural model comes from a file. The note
gives values for some of the questions listed below. Return ONE JSON object:

{"values": {"<question id>": {"value": <the value>, "quote": "<the words of the note it comes from, copied character for character>"}},
 "unclear": ["what the note says that could answer a question but is ambiguous, as a short question back"],
 "not_used": ["what the note states that answers none of the listed questions"]}

Rules:
- Answer only the questions listed, by their id. A question the note does not answer is left out.
- Take only what the note states. Never infer, assume or fill in a typical value, and never take a value from the
  question's own text.
- Units: as the question asks (psf, g, s, kip, ft, %); convert metric (1 kPa = 20.885 psf, 1 kN = 0.22481 kip,
  1 m = 3.2808 ft).
- Where a question lists options, the value is one of the options exactly.
- A load may be one number, {"floor": psf, "roof": psf}, or {"<level name>": psf}.
- A section question asks for the AISC name of a section the file gives only by its properties; answer it only where
  the note names the shape for those members (e.g. "the columns are W14X82").
- One statement can answer two questions (e.g. "SMF both ways" answers the X and the Y system).
Return only the JSON object."""


def _item_line(it):
    s = "- %s: %s" % (it["id"], re.sub(r"\s+", " ", str(it.get("question") or ""))[:240])
    if it.get("options"):
        s += " [options: %s]" % ", ".join(str(o) for o in it["options"])
    return s


def read_sentences(text: str, items: list, chat_json, max_items: int = 160):
    """-> ({id: (value, words)}, unclear, not_used, notes) through the model; quotes checked against the text."""
    lines = [_item_line(it) for it in items[:max_items]]
    more = len(items) - max_items
    user = "Questions:\n%s%s\n\nNote:\n%s" % ("\n".join(lines), ("\n(and %d more of the same kinds)" % more) if more > 0 else "", text)
    try:
        out = chat_json(SYSTEM, user, max_tokens=6000, temperature=0.0)
    except ValueError:
        out = chat_json(SYSTEM, user + "\n\nReturn only the JSON object, nothing else.", max_tokens=6000, temperature=0.0)
    if not isinstance(out, dict):
        raise ValueError("the model did not return a JSON object")
    by_id = {it["id"]: it for it in items}
    tn = _norm(text)
    vals, notes = {}, []
    for iid, v in (out.get("values") or {}).items():
        it = by_id.get(iid)
        if it is None or not isinstance(v, dict) or v.get("value") in (None, ""):
            continue
        q = str(v.get("quote") or "")
        if not q.strip() or _norm(q) not in tn:
            notes.append("not taken: %s = %s -- the words it was read from (\"%s\") are not in your text"
                         % (iid, json.dumps(v.get("value")), q))
            continue
        try:
            vals[iid] = (coerce(it, v["value"]), q.strip())
        except ValueError as ex:
            notes.append("not taken: %s = %s -- %s (\"%s\")" % (iid, json.dumps(v.get("value")), ex, q.strip()))
    unclear = [str(u) for u in out.get("unclear") or [] if str(u).strip()]
    not_used = [str(u) for u in out.get("not_used") or [] if str(u).strip()]
    return vals, unclear, not_used, notes


def read(text: str, items: list, raw: dict = None, chat_json=None, model: str = ""):
    """The text -> ({id: (value, words)}, report). `name = value` first; the model reads the rest when there is a
    connection (chat_json None = none)."""
    vals, notes, spans = read_pairs(text, items, raw)
    rest = text
    for a, b in sorted(spans, reverse=True):
        rest = rest[:a] + " " + rest[b:]
    left = re.sub(r"[\s,;.:\-]+", " ", rest).strip()
    report = dict(model=None, values=[], notes=list(notes), unclear=[], not_used=[])
    if len(left) > 2:
        todo = [it for it in items if it["id"] not in vals]
        if chat_json is None:
            odd = [m.group(0).strip() for m in _ANY_PAIR.finditer(rest)]
            for o in odd:
                report["notes"].append("not understood: '%s' -- not a name this page reads (e.g. SDS, site class, "
                                       "system X, D, L, deck span, T1 X, or a question's id from step 2)" % o)
            rest2 = re.sub(r"\s+", " ", _ANY_PAIR.sub(" ", rest)).strip(" ,;.:-")
            if len(re.sub(r"[\s,;.:\-]+", "", rest2)) > 2:
                report["notes"].append("not read: \"%s\" -- sentences need the hub's model connection, and none is set; "
                                       "give values as 'name = value' lines (SDS = 1.0, site class = D, system X = SMF), "
                                       "or set a connection in the hub" % (rest2[:160] + ("..." if len(rest2) > 160 else "")))
        elif todo:
            try:
                v2, unclear, not_used, n2 = read_sentences(text, todo, chat_json)
                report.update(model=model or "the model", unclear=unclear, not_used=not_used)
                report["notes"] += n2
                for k, v in v2.items():
                    vals.setdefault(k, v)
            except Exception as ex:                       # noqa: BLE001 -- the `name = value` pieces still count
                report["notes"].append("the model could not read the sentences in your text: %s" % ex)
    by_id = {it["id"]: it for it in items}
    for iid, (v, words) in vals.items():
        it = by_id[iid]
        report["values"].append(dict(id=iid, question=it.get("question"), value=v, words=words, path=it["path"],
                                     kind={"result": "your own result", "later": "asked later"}.get(it["kind"], "proposal"),
                                     **({"options": it["options"]} if it.get("options") else {})))
    return vals, report


def from_text(desc: dict) -> bool:
    """True when desc carries anything an earlier text put in."""
    props = desc.get("_proposals") if isinstance(desc.get("_proposals"), dict) else {}
    return any(str((v or {}).get("reason") or "").startswith(ORIGIN) for v in props.values()) or \
        ORIGIN in str(((desc.get("_provenance") or {}).get("user_results") or {}).get("note") or "")


def strip(desc: dict) -> dict:
    """desc without what an earlier text put in (its proposals, the results it gave, its note): the current text is
    then the whole story."""
    props = desc.get("_proposals") if isinstance(desc.get("_proposals"), dict) else None
    if props is not None:
        desc["_proposals"] = {k: v for k, v in props.items() if not str((v or {}).get("reason") or "").startswith(ORIGIN)}
    pv = desc.get("_provenance") or {}
    if ORIGIN in str((pv.get("user_results") or {}).get("note") or ""):
        desc["user_results"] = None
        pv.pop("user_results", None)
    intake = desc.get("_intake")
    if isinstance(intake, dict) and intake.get("notes"):
        intake["notes"] = [n for n in intake["notes"] if not str(n).startswith("read from your text")]
    return desc


def apply(desc: dict, vals: dict, items: list) -> dict:
    """The values into a raw description: a proposal on each question's path (HR offers it as a default to accept,
    now or once it asks), the engineer's own results into user_results."""
    by_id = {it["id"]: it for it in items}
    desc = strip(desc)
    props = desc.get("_proposals") if isinstance(desc.get("_proposals"), dict) else {}
    ur = desc.get("user_results") if isinstance(desc.get("user_results"), dict) else {}
    taken = []
    for iid, (v, words) in vals.items():
        it = by_id[iid]
        if it["kind"] == "result":
            parts = iid.split(".")[1:]
            if len(parts) == 1:
                ur[parts[0]] = v
            else:
                ur[parts[0]] = dict(ur.get(parts[0]) or {}, **{parts[1]: v})
        else:
            props[it["path"]] = {"value": v, "reason": '%s: "%s"' % (ORIGIN, words[:300])}
        taken.append("%s = %s" % (iid, json.dumps(v)))
    if props:
        desc["_proposals"] = props
    if ur:
        desc["user_results"] = ur
        desc.setdefault("_provenance", {})["user_results"] = {"source": "file", "note": "your own results, from your text"}
    if taken:
        intake = desc.setdefault("_intake", {})
        notes = [n for n in intake.get("notes") or [] if not str(n).startswith("read from your text")]
        intake["notes"] = ["read from your text (accept them in step 2; your own results go to the confirmation check): %s"
                           % "; ".join(taken)] + notes
    return desc
