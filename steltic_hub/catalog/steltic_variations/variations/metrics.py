"""Metrics from one HR Steel design package (the zip its server produces).

Everything here is read from what the framework writes (report.html tables, calc_package.json,
member_schedule.csv, cfg.py). Values that only exist as prose in the report are left to the
LLM reader (main.py) and arrive tagged `llm_read` so the table can say where a number came from.
"""
from __future__ import annotations
import csv, html, io, json, math, re, zipfile
from .library import NOT_REPRESENTABLE_WORDS


def unzip(data: bytes) -> dict[str, bytes]:
    """{relative path (the <building>/ prefix stripped): bytes}"""
    out: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [i.filename.replace("\\", "/") for i in z.infolist() if not i.is_dir()]
        tops = {n.split("/", 1)[0] for n in names}
        wrap = len(tops) == 1 and all("/" in n for n in names)
        for i in z.infolist():
            if i.is_dir():
                continue
            n = i.filename.replace("\\", "/")
            rel = n.split("/", 1)[1] if wrap else n
            if rel and ".." not in rel.split("/"):
                out[rel] = z.read(i)
    return out


def _text(files: dict, name: str) -> str:
    b = files.get(name)
    return b.decode("utf-8", "replace") if b else ""


def _flat(html_text: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html_text, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return html.unescape(re.sub(r"\s+", " ", t))


# ---------------------------------------------------------------- steel weight
def section_weight_plf(sec: str) -> float | None:
    """lb/ft for a section name. W/HP/S/M/C/MC/L carry it in the name; HSS is computed from the
    wall thickness (3.4 lb per in^2 of area); unknown shapes -> None."""
    s = (sec or "").strip().upper().replace(" ", "")
    m = re.match(r"^(W|HP|S|M|C|MC|WT|MT|ST)\d+(?:\.\d+)?X(\d+(?:\.\d+)?)$", s)
    if m:
        return float(m.group(2))
    m = re.match(r"^HSS(\d+(?:\.\d+)?)X(\d+(?:\.\d+)?)X(\d+(?:/\d+)?(?:\.\d+)?)$", s)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        t = _frac(m.group(3))
        if t:
            td = 0.93 * t                                  # design wall thickness
            area = 2 * td * (a + b - 2 * td)
            return 3.4 * area
    m = re.match(r"^HSS(\d+(?:\.\d+)?)X(\d+(?:\.\d+)?)$", s)     # round: HSS10.000X0.500
    if m:
        d, t = float(m.group(1)), float(m.group(2))
        td = 0.93 * t
        return 3.4 * math.pi * (d - td) * td
    m = re.match(r"^PIPE(\d+(?:\.\d+)?)(STD|XS|XXS)?$", s)
    if m:
        d = float(m.group(1)); t = {"STD": 0.28, "XS": 0.5, "XXS": 0.9}.get(m.group(2) or "STD", 0.28)
        return 3.4 * math.pi * (d - t) * t
    m = re.match(r"^L(\d+(?:\.\d+)?)X(\d+(?:\.\d+)?)X(\d+(?:/\d+)?(?:\.\d+)?)$", s)
    if m:
        a, b, t = float(m.group(1)), float(m.group(2)), _frac(m.group(3))
        return 3.4 * t * (a + b - t) if t else None
    return None


def _frac(x: str) -> float | None:
    try:
        if "/" in x:
            n, d = x.split("/")
            return float(n) / float(d)
        return float(x)
    except Exception:
        return None


def schedule_metrics(files: dict) -> dict:
    txt = _text(files, "design/member_schedule.csv")
    if not txt:
        return {}
    rows = list(csv.DictReader(io.StringIO(txt)))
    tons = 0.0; unknown = set(); kinds: dict[str, int] = {}
    for r in rows:
        kind = (r.get("member") or "").strip().lower()
        kinds[kind] = kinds.get(kind, 0) + 1
        sec = r.get("section") or ""
        try:
            L = float(r.get("length_in") or 0) / 12.0
        except ValueError:
            L = 0.0
        w = section_weight_plf(sec)
        if w is None:
            unknown.add(sec)
        else:
            tons += w * L / 2000.0
    out = {"steel_tons": round(tons, 1) if rows else None,
           "n_columns": kinds.get("col", 0) + kinds.get("column", 0),
           "n_beams": kinds.get("beam", 0) + kinds.get("girder", 0),
           "n_braces": sum(v for k, v in kinds.items() if "brace" in k or k in ("diag", "diagonal")),
           "n_members": len(rows)}
    if unknown:
        out["unweighed_sections"] = sorted(unknown)[:10]
    return out


# ---------------------------------------------------------------- cfg.py geometry
def cfg_metrics(files: dict) -> dict:
    src = _text(files, "cfg.py")
    if not src:
        return {}
    out = {}
    def num(name):
        m = re.search(rf"^\s*{name}\s*=\s*([\d.]+)", src, re.M)
        return float(m.group(1)) if m else None
    nx, ny = num("NX"), num("NY")
    m = re.search(r"^\s*NX\s*,\s*NY\s*=\s*([\d.]+)\s*,\s*([\d.]+)", src, re.M)   # NX, NY = 6, 4
    if m:
        nx, ny = float(m.group(1)), float(m.group(2))
    m = re.search(r"^\s*SX\s*,\s*SY\s*=\s*([\d.]+)\s*,\s*([\d.]+)", src, re.M)
    if m:
        sx_, sy_ = float(m.group(1)), float(m.group(2))
    else:
        sx_ = sy_ = None
    m = re.search(r"^\s*SX\s*=\s*SY\s*=\s*([\d.]+)", src, re.M)
    sx = sy = float(m.group(1)) if m else None
    if sx is None:
        sx, sy = (sx_, sy_) if sx_ else (num("SX"), num("SY"))
    m = re.search(r"^\s*HEIGHTS\s*=\s*(.+)$", src, re.M)
    heights = None
    if m:
        try:
            heights = eval(m.group(1), {"__builtins__": {}}, {})    # a list literal of numbers, e.g. [192.0] + [168.0]*5
            heights = [float(h) for h in heights]
        except Exception:
            heights = None
    nf = num("NF") or (len(heights) if heights else None)
    if nx and ny:
        out["nx"] = int(nx); out["ny"] = int(ny)
    if nx and ny and sx and sy:
        out["plan_x_ft"] = round(nx * sx / 12.0, 1); out["plan_y_ft"] = round(ny * sy / 12.0, 1)
        if nf:
            out["floor_area_sf"] = round(nx * sx * ny * sy / 144.0 * nf, 0)
            out["n_stories"] = int(nf)
    if heights:
        out["height_ft"] = round(sum(heights) / 12.0, 1)
    return out


# ---------------------------------------------------------------- report.html
def report_metrics(files: dict) -> dict:
    t = _flat(_text(files, "report.html"))
    out: dict = {}
    if not t:
        return out
    m = re.search(r"Story δe X % δ X % δe Y % δ Y % ≤([\d.]+)%(.{0,2000})", t)
    if m:
        lim = float(m.group(1))
        rows = re.findall(r"(\d+) ([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+) (?:OK|NG|FAIL|EXCEEDS)", m.group(2))
        if rows:
            dx = [float(r[2]) for r in rows]; dy = [float(r[4]) for r in rows]
            allv = dx + dy
            out["drift_limit_pct"] = lim
            out["drift_max_pct"] = max(allv)
            out["drift_utilisation"] = round(max(allv) / lim, 3) if lim else None
            out["drift_margin"] = round(1 - max(allv) / lim, 3) if lim else None
            worst = dx if max(dx) >= max(dy) else dy
            mean = sum(worst) / len(worst)
            out["drift_concentration_ratio"] = round(max(worst) / mean, 3) if mean else None
            out["drift_profile_X"] = dx; out["drift_profile_Y"] = dy
    m = re.search(r"Story drift X % drift Y % ≤ ([\d.]+)%(.{0,1500})", t)
    if m:
        lim = float(m.group(1))
        rows = re.findall(r"(\d+) ([\d.]+) ([\d.]+) (?:OK|NG|FAIL|EXCEEDS)", m.group(2))
        if rows and lim:
            out["wind_drift_utilisation"] = round(max(max(float(r[1]), float(r[2])) for r in rows) / lim, 3)
    m = re.search(r"Design base shear V = C s W = ([\d.]+) × ([\d,]+) = ([\d,]+) kip", t)
    if m:
        out["Cs"] = float(m.group(1)); out["W_kip"] = float(m.group(2).replace(",", "")); out["V_kip"] = float(m.group(3).replace(",", ""))
    m = re.search(r"Wind base shear: X = ([\d,]+) kip, Y = ([\d,]+) kip", t)
    if m:
        out["wind_V_kip"] = max(float(m.group(1).replace(",", "")), float(m.group(2).replace(",", "")))
    m = re.search(r"Mode T \(s\) mX % ΣmX % mY % ΣmY % 1 ([\d.]+)", t)
    if m:
        out["T1_s"] = float(m.group(1))
    m = re.search(r"Seismic force-resisting system \(declared\)\s*(.{0,220}?)\s(?:R\s*=|Response|Seismic|Risk|$)", t)
    if m:
        out["system_declared"] = m.group(1).strip()[:200]
    m = re.search(r"NOT PERMITTED[^.]{0,200}", t)
    if m:
        out["np_text"] = m.group(0)[:220]
    return out


# ---------------------------------------------------------------- calc_package.json
def package_metrics(files: dict) -> dict:
    raw = _text(files, "design/calc_package.json")
    if not raw:
        return {}
    try:
        cp = json.loads(raw)
    except Exception:
        return {"package_error": "calc_package.json is not valid JSON"}
    out: dict = {}
    dcs = [x.get("DC") for x in cp.get("members", []) if isinstance(x, dict) and isinstance(x.get("DC"), (int, float))]
    cdcs = [x.get("DC") for x in cp.get("connections", []) if isinstance(x, dict) and isinstance(x.get("DC"), (int, float))]
    if dcs or cdcs:
        out["dc_max"] = round(max(dcs + cdcs), 3)
        out["n_over"] = sum(1 for v in dcs + cdcs if v > 1.0)
        out["n_checked_members"] = len(dcs)
    cd = cp.get("capacity_design") or {}
    sysname = str(cd.get("system") or "")
    if sysname:
        out["system"] = sysname[:200]
    red = cd.get("redundancy")
    if isinstance(red, dict) and isinstance(red.get("rho"), (int, float)):       # {"rho": 1.0, ...}
        out["rho"] = float(red["rho"])
    else:                                                                       # "rho = 1.0 DEMONSTRATED per 12.3.4.2(b) ..."
        m = re.search(r"rho\s*=\s*([\d.]+)", str(red or ""), re.I)
        if m:
            out["rho"] = float(m.group(1))
    scwb = cd.get("SCWB") or {}
    if isinstance(scwb, dict) and isinstance(scwb.get("ratio"), (int, float)):
        out["scwb_ratio"] = float(scwb["ratio"])
    fs = cp.get("framework_screen") or {}
    tor = fs.get("torsion") or {}
    if isinstance(tor, dict):
        if isinstance(tor.get("Ax"), (int, float)):
            out["torsion_Ax"] = float(tor["Ax"])
        if tor.get("classification") is not None:
            out["torsion_class"] = str(tor["classification"])
        if isinstance(tor.get("ratio_max"), (int, float)):
            out["torsion_ratio"] = float(tor["ratio_max"])
    ss = fs.get("soft_story") or {}
    if isinstance(ss, dict) and ss.get("classification") is not None:
        out["soft_story_class"] = str(ss["classification"])
    return out


def representability(m: dict) -> dict:
    """SMF / SCBF / dual SMF+SCBF / R=3 X-braced can go through the nonlinear tools; devices,
    BRB, EBF, SPSW and composite walls cannot."""
    text = " ".join(str(m.get(k) or "") for k in ("system", "system_declared", "system_llm")).lower()
    if not text.strip():
        return {"representable": None}
    bad = [w for w in NOT_REPRESENTABLE_WORDS if w.lower() in text]
    is_dual = "dual" in text
    return {"representable": not bad, "is_dual": is_dual,
            "not_representable_because": ", ".join(sorted(set(bad))) if bad else ""}


def extract(files: dict) -> dict:
    """All deterministic metrics for one package."""
    m: dict = {}
    m.update(cfg_metrics(files))
    m.update(schedule_metrics(files))
    m.update(report_metrics(files))
    m.update(package_metrics(files))
    if m.get("steel_tons") is not None and m.get("floor_area_sf"):
        m["steel_psf"] = round(m["steel_tons"] * 2000.0 / m["floor_area_sf"], 2)
    m.update(representability(m))
    m["has_report"] = "report.html" in files
    m["has_viewer"] = "viewer_3d.html" in files
    m["has_package"] = "design/calc_package.json" in files
    return m


def report_excerpt(files: dict, limit: int = 14000) -> str:
    """The parts of the report an LLM needs to answer the prose questions, trimmed."""
    t = _flat(_text(files, "report.html"))
    if not t:
        return ""
    keep = []
    for head in ("Design basis", "Seismic force-resisting system", "Lateral system", "Lateral-load design inputs",
                 "Horizontal force distribution", "Plan & vertical irregularity", "Seismic design drift", "Wind drift",
                 "NOT PERMITTED", "Connections"):
        i = t.find(head)
        if i >= 0:
            keep.append(t[i:i + 2200])
    out = "\n---\n".join(keep) if keep else t[:limit]
    return out[:limit]
