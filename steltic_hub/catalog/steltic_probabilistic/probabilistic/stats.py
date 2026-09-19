"""D/C recomputation with the design's own capacities, the statistics of the sample, the
governing-check comparison, the plots and the written outputs."""
from __future__ import annotations
import csv, html, io, json, math, pathlib, re, time
import numpy as np

PALETTE = {"bars": "#4a8de0", "lognormal": "#b8861f", "normal": "#9b6fe0", "base": "#d95f52", "limit": "#8b95a3",
           "ink": "#dde3ea", "dim": "#8b95a3", "grid": "#2c333e", "surface": "#181c22"}


# ---------------------------------------------------------------- capacities as recorded by the design
def _find_key(cap: dict, *patterns) -> float | None:
    """A capacity value whose key matches one of the regex patterns (case-insensitive)."""
    for pat in patterns:
        rx = re.compile(pat, re.I)
        for k, v in (cap or {}).items():
            if rx.fullmatch(str(k).replace(" ", "")) and isinstance(v, (int, float)) and v > 0:
                return float(v)
    return None


def caps_of(member: dict) -> dict:
    cap = member.get("capacity") or {}
    return {"Pc": _find_key(cap, r"phi_?pn(_kip)?", r"phi_?pc(_kip)?", r"phi_?p(_kip)?"),
            "Pt": _find_key(cap, r"phi_?pt(_kip)?", r"phi_?tn(_kip)?"),
            "Mcx": _find_key(cap, r"phi_?mnx?(_kipin)?", r"phi_?mcx?(_kipin)?", r"phi_?mp(x)?(_kipin)?"),
            "Mcy": _find_key(cap, r"phi_?mny(_kipin)?", r"phi_?mcy(_kipin)?"),
            "Vc": _find_key(cap, r"phi_?vn(_kip)?", r"phi_?vc(_kip)?")}


# ---------------------------------------------------------------- the two capacity bases
# The design's package records phi*Rn and D/(phi*Rn). The study reports, by default, the FACTORED
# demand over the NOMINAL capacity, D/Rn = phi * D/(phi*Rn): the resistance factor is taken out so
# that 1.0 means "the demand reaches the nominal strength", not "the LRFD check is exactly met".
# The phi values below are AISC 360-22's for the checks HR Steel's agent records; they are the
# assumptions used to undo the factoring and are shown with every result.
PHI_MEMBER = {"Pc": 0.90, "Pt": 0.90, "Mcx": 0.90, "Mcy": 0.90, "Vc": 1.00}     # E1/D2/F1: 0.90; G1 rolled I-shapes: 1.00
PHI_NOTES = [("flexure, compression, tension yielding, interaction", 0.90, "AISC 360-22 F1, E1, D2, H1"),
             ("shear, rolled I-shapes", 1.00, "AISC 360-22 G2.1(a)"),
             ("bolts, welds, block shear, rupture, bearing, anchorage", 0.75, "AISC 360-22 J3, J2, J4"),
             ("panel zone, plate yielding, CJP developing the member, base plate bending", 0.90, "AISC 360-22 J10.6, J4.1, J1.4, J8"),
             ("concrete bearing under a base plate or shear lug", 0.65, "AISC 360-22 J8")]
BASES = {"nominal": "D/Rₙ — factored demand over NOMINAL capacity (φ removed)",
         "design": "D/φRₙ — the design's own LRFD ratio"}
_CONN_065 = re.compile(r"concrete bearing|shear lug|\bj8\b", re.I)
_CONN_075 = re.compile(r"bolt|weld|block shear|rupture|bearing|anchor|net section|fillet|shear tab|single-plate", re.I)
_CONN_090 = re.compile(r"panel|yield|cjp|base plate|plate bending|develops", re.I)


def phi_member(limit_state: str | None) -> float:
    """phi behind a recorded member D/C (used only for groups whose capacities are not all recorded)."""
    t = (limit_state or "").lower()
    if "shear" in t and not any(w in t for w in ("flex", "bend", "moment", "interaction", "compress", "h1", "f2", "f3", "e3")):
        return 1.00
    return 0.90


def phi_connection(limit_state: str | None, ctype: str | None) -> float:
    t = f"{limit_state or ''} {ctype or ''}"
    if _CONN_065.search(t):
        return 0.65
    if _CONN_075.search(t):
        return 0.75
    if _CONN_090.search(t):
        return 0.90
    return 0.75


def caps_basis(c: dict, basis: str) -> dict:
    """The capacities in the chosen basis: as recorded (phi*Rn) or nominal (Rn = phi*Rn / phi)."""
    if basis != "nominal":
        return c
    return {k: (v / PHI_MEMBER[k] if v else v) for k, v in c.items()}


def dc_exact(kind: str, d: dict, c: dict) -> float | None:
    """AISC 360 check with the given capacities; None when a needed capacity is not recorded."""
    comp, tens, Mz, My, V = (float(d.get(q) or 0.0) for q in ("comp", "tens", "Mz", "My", "V"))
    ref = max(comp, tens, Mz, My, V, 1e-9)
    comp, tens, Mz, My, V = (0.0 if abs(q) < 1e-6 * ref else q for q in (comp, tens, Mz, My, V))   # numerical dust
    if kind == "beam":
        parts = []
        if c.get("Mcx") and Mz:
            parts.append(Mz / c["Mcx"])
        if c.get("Vc") and V:
            parts.append(V / c["Vc"])
        if My and not c.get("Mcy"):
            return None
        if My and c.get("Mcy"):
            parts.append(My / c["Mcy"])
        return max(parts) if parts else None
    if kind == "brace":
        parts = []
        if c.get("Pc") and comp:
            parts.append(comp / c["Pc"])
        if tens:
            if c.get("Pt"):
                parts.append(tens / c["Pt"])
            elif c.get("Pc") and not comp:
                parts.append(tens / c["Pc"])
        return max(parts) if parts else None
    # column: H1-1 interaction with the major/minor capacities that are recorded
    Pc = c.get("Pc")
    if not Pc:
        return None
    pr = comp / Pc
    mterm = 0.0
    if Mz:
        if not c.get("Mcx"):
            return None
        mterm += Mz / c["Mcx"]
    if My:
        if not c.get("Mcy"):
            return None
        mterm += My / c["Mcy"]
    dc = (pr + 8.0 / 9.0 * mterm) if pr >= 0.2 else (pr / 2.0 + mterm)
    if tens and c.get("Pt"):
        dc = max(dc, tens / c["Pt"] + mterm)
    if c.get("Vc") and V:
        dc = max(dc, V / c["Vc"])
    return dc


_REL = {"beam": ("Mz", "V", "My"), "col": ("comp", "Mz", "My", "tens"), "brace": ("comp", "tens")}


def dc_scaled(kind: str, base: dict, new: dict, dc_base: float) -> tuple[float, str]:
    """Base D/C scaled by the largest increase among the demand components that size the member."""
    best, which = 1.0, "unchanged"
    for q in _REL.get(kind, ("comp", "Mz")):
        b, n = float(base.get(q) or 0.0), float(new.get(q) or 0.0)
        if b > 1e-9:
            r = n / b
            if r > best:
                best, which = r, q
    return dc_base * best, which


def member_dcs(design_members: list[dict], base_groups: dict, new_groups: dict, basis: str = "nominal") -> dict:
    """{group id: {dc, dc_base, method, driver, combo, story, ratio, phi}} for one realisation, in
    the chosen basis. `base_groups` are realisation 0's demands (the same pipeline): they validate
    the exact formula against the recorded D/C and are what the scaled fallback is measured from.
    `dc_base` is the design's own value in the same basis."""
    out = {}
    for m in design_members:
        gid, dc0 = m.get("id"), m.get("DC")
        if not isinstance(dc0, (int, float)) or gid not in new_groups:
            continue
        kind = m.get("kind") or new_groups[gid].get("kind")
        c = caps_of(m)
        b, n = base_groups.get(gid) or {}, new_groups[gid]
        method, driver = "scaled", ""
        dc = dc_base = None
        phi = phi_member(m.get("limit_state"))
        if b:
            e0 = dc_exact(kind, b, c)                                    # design basis, validates the formula
            if e0 is not None and abs(e0 - dc0) <= max(0.02, 0.03 * dc0):
                cb = caps_basis(c, basis)
                e0b, e1b = dc_exact(kind, b, cb), dc_exact(kind, n, cb)
                if e0b is not None and e1b is not None:
                    fix = dc0 / e0                                       # the package's rounding, kept
                    dc, dc_base, method = e1b * fix, e0b * fix, "exact"
                    driver = "AISC 360 check with the recorded φRₙ" + (" / φ" if basis == "nominal" else "")
        if dc is None:
            f = phi if basis == "nominal" else 1.0
            dc_base = dc0 * f
            if b:
                dc, driver = dc_scaled(kind, b, n, dc_base)
            else:
                dc, driver = dc_base, "no base"
        out[gid] = {"dc": dc, "dc_base": dc_base, "dc_recorded": dc0, "ratio": (dc / dc_base if dc_base else None), "method": method,
                    "driver": driver, "phi": (phi if method != "exact" else None), "combo": n.get("combo"), "story": n.get("story_max"),
                    "kind": kind, "limit_state": m.get("limit_state"), "section": m.get("section") or n.get("section"),
                    "role": m.get("role") or n.get("role")}
    return out


CAPACITY_DESIGNED = re.compile(r"mpr|^vh(_|$)|expected|^ry|omega|om0|1\.1", re.I)


def connection_dcs(design_conns: list[dict], base_h: dict, new_h: dict, basis: str = "nominal") -> dict:
    """Connection D/C scaled by the change in the demand that sizes it (the capacity-designed
    demands -- M_pr, V_h, R_y-based -- do not change with the analysis model). In the nominal
    basis the recorded D/(phi*Rn) is multiplied by the phi of the recorded limit state."""
    out = {}
    for c in design_conns:
        cid, dc_rec = c.get("id") or "", c.get("DC")
        if not isinstance(dc_rec, (int, float)):
            continue
        phi = phi_connection(c.get("limit_state"), c.get("type"))
        dc0 = dc_rec * (phi if basis == "nominal" else 1.0)
        text = " ".join(str(x) for x in (cid, c.get("type"), c.get("limit_state"), c.get("section"))).lower()
        best, which = 1.0, "unchanged"
        mapped = []
        for key in (c.get("demand") or {}):
            k = str(key)
            if CAPACITY_DESIGNED.search(k):
                mapped.append((k, "capacity-designed, unchanged")); continue
            kl = k.lower()
            handle = None
            if "brace" in text or "gusset" in text:
                handle = "brace_P"
            elif "collector" in text or "drag" in text or "fpx" in kl:
                handle = "V_base"
            elif "base" in text or "anchor" in text or "splice" in text or (("column" in text or "col" in text) and "beam" not in text):
                if kl.startswith("p") or "axial" in kl:
                    handle = "col_T" if "tens" in kl else "col_P"
                elif kl.startswith("v"):
                    handle = "V_base" if "base" in text else "col_V"
                elif kl.startswith("m"):
                    handle = "col_M"
            else:                                           # beam-to-column and everything else beam-sized
                if kl.startswith("v"):
                    handle = "gravity_beam_V" if ("gravity" in text or "shear" in text or "simple" in text) else "beam_V"
                elif kl.startswith("m"):
                    handle = "beam_M"
                elif kl.startswith("p") or "axial" in kl:
                    handle = "col_P"
            if handle and float(base_h.get(handle) or 0.0) > 1e-9:
                r = float(new_h.get(handle) or 0.0) / float(base_h[handle])
                mapped.append((k, handle))
                if r > best:
                    best, which = r, f"{k} ← {handle}"
        out[cid] = {"dc": dc0 * best, "dc_base": dc0, "dc_recorded": dc_rec, "phi": phi, "ratio": best, "driver": which, "mapped": mapped,
                    "type": c.get("type"), "limit_state": c.get("limit_state")}
    return out


# ---------------------------------------------------------------- distributions
def _phi(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def lognormal_fit(x: np.ndarray) -> dict:
    lx = np.log(x)
    mu, s = float(lx.mean()), (float(lx.std(ddof=1)) if len(x) > 1 else 0.0)
    return {"mu": mu, "sigma": s, "median": math.exp(mu), "mean": math.exp(mu + 0.5 * s * s),
            "cov": math.sqrt(math.exp(s * s) - 1.0) if s > 0 else 0.0}


def lognormal_cdf(x, mu, s):
    if x <= 0:
        return 0.0
    if s <= 0:
        return 1.0 if x >= math.exp(mu) else 0.0
    return _phi((math.log(x) - mu) / s)


def normal_cdf(x, m, s):
    if s <= 0:
        return 1.0 if x >= m else 0.0
    return _phi((x - m) / s)


def ks_test(x: np.ndarray, cdf) -> dict:
    xs = np.sort(x); n = len(xs)
    if n < 3:
        return {"D": None, "p": None}
    F = np.array([cdf(v) for v in xs])
    D = float(max(np.max(np.arange(1, n + 1) / n - F), np.max(F - np.arange(0, n) / n)))
    lam = (math.sqrt(n) + 0.12 + 0.11 / math.sqrt(n)) * D
    p = 1.0 if lam < 1e-9 else float(min(1.0, max(0.0, 2.0 * sum((-1) ** (k - 1) * math.exp(-2.0 * k * k * lam * lam) for k in range(1, 101)))))
    return {"D": round(D, 4), "p": round(p, 4)}


def _const(x: np.ndarray) -> bool:
    return len(x) == 0 or float(np.std(x)) <= 1e-7 * max(abs(float(np.mean(x))), 1e-12)


def spearman(a: np.ndarray, b: np.ndarray) -> float | None:
    if len(a) < 4 or _const(a) or _const(b):
        return None
    ra = np.argsort(np.argsort(a)).astype(float); rb = np.argsort(np.argsort(b)).astype(float)
    c = np.corrcoef(ra, rb)[0, 1]
    return None if np.isnan(c) else float(c)


def describe(x: np.ndarray, base: float | None, limit: float = 1.0) -> dict:
    n = len(x)
    if n == 0:
        return {}
    mean, std = float(x.mean()), (float(x.std(ddof=1)) if n > 1 else 0.0)
    if _const(x):
        std = 0.0                                   # numerical dust on an invariant quantity is not scatter
    st = {"n": n, "mean": mean, "std": std, "cov": (std / mean if mean else None), "median": float(np.median(x)),
          "min": float(x.min()), "max": float(x.max()), "p5": float(np.percentile(x, 5)), "p95": float(np.percentile(x, 95)),
          "skewness": (float(((x - mean) ** 3).mean() / std ** 3) if std > 0 and n > 2 else 0.0),
          "ci95_mean": ([mean - 1.96 * std / math.sqrt(n), mean + 1.96 * std / math.sqrt(n)] if n > 1 else None),
          "p_over_limit": float((x > limit).mean()), "limit": limit, "invariant": std == 0.0}
    if np.all(x > 0) and std > 0:
        ln = lognormal_fit(x)
        st["fit_lognormal"] = {**ln, "ks": ks_test(x, lambda v: lognormal_cdf(v, ln["mu"], ln["sigma"]))}
        st["p_over_limit_lognormal"] = 1.0 - lognormal_cdf(limit, ln["mu"], ln["sigma"])
        st["p95_lognormal"] = math.exp(ln["mu"] + 1.645 * ln["sigma"])
    if std > 0:
        st["fit_normal"] = {"mean": mean, "std": std, "ks": ks_test(x, lambda v: normal_cdf(v, mean, std))}
    if base is not None:
        st.update({"base": base, "ratio_mean": (mean / base if base else None), "p_over_base": float((x > base).mean()),
                   "p95_over_base": (st["p95"] / base if base else None)})
    return st


def histogram(x: np.ndarray, marks: list[float]):
    n = len(x)
    if n == 0:
        return {"edges": [], "counts": []}
    q75, q25 = np.percentile(x, [75, 25])
    iqr = q75 - q25
    k = int(math.ceil((x.max() - x.min()) / (2 * iqr / n ** (1 / 3)))) if (iqr > 0 and n >= 8) else int(math.ceil(math.sqrt(n)))
    k = max(6, min(24, k))
    lo, hi = float(x.min()), float(x.max())
    for m in marks:
        if m is not None:
            lo, hi = min(lo, m), max(hi, m)
    pad = 0.04 * (hi - lo or 0.1)
    edges = np.linspace(lo - pad, hi + pad, k + 1)
    counts, _ = np.histogram(x, bins=edges)
    return {"edges": [float(e) for e in edges], "counts": [int(c) for c in counts]}


# ---------------------------------------------------------------- the analysis
def analyse(spec: dict, results: list[dict], probe: dict, basis: str = "nominal") -> dict:
    basis = "design" if basis == "design" else "nominal"
    by = {r["id"]: r for r in results}
    base = by.get(0)
    ok = sorted([r for r in results if r.get("ok") and r["id"] != 0 and r.get("groups")], key=lambda r: r["id"])
    failed = [r for r in results if not r.get("ok")]
    design = (probe or {}).get("design") or {}
    dmembers, dconns = design.get("members") or [], design.get("connections") or []
    out = {"n_planned": int(spec.get("n") or 0), "n_done": len(ok), "n_failed": len(failed), "seed": spec.get("seed"),
           "basis": basis, "basis_label": BASES[basis], "ratio_label": ("D/Rₙ" if basis == "nominal" else "D/φRₙ"),
           "phi_notes": PHI_NOTES,
           "failed": [{"id": r["id"], "error": r.get("error")} for r in failed][:20], "base": None, "rows": [],
           "members": None, "connections": None, "drift": None, "groups": [], "conn_table": [], "governing": None,
           "correlations": [], "assessment": [], "time": {"seconds_total": sum(float(r.get("seconds") or 0) for r in results),
                                                          "seconds_mean": (float(np.mean([r["seconds"] for r in ok])) if ok else None)},
           "n_design_members": len(dmembers), "n_design_connections": len(dconns)}
    if not (base and base.get("ok")):
        out["base"] = {"error": (base or {}).get("error")} if base else None
        return out
    bg, bh = base["groups"], base.get("handles") or {}
    m0 = member_dcs(dmembers, bg, bg, basis)              # the design itself, in the chosen basis
    c0 = connection_dcs(dconns, bh, bh, basis)
    gov0 = max(m0.items(), key=lambda kv: kv[1]["dc"])[0] if m0 else None
    govc0 = max(c0.items(), key=lambda kv: kv[1]["dc"])[0] if c0 else None
    out["base"] = {"member_max": (m0[gov0]["dc"] if gov0 else None), "governing": gov0, "governing_limit_state": (m0[gov0]["limit_state"] if gov0 else None),
                   "governing_combo": (bg.get(gov0) or {}).get("combo") if gov0 else None,
                   "member_max_recorded": (m0[gov0]["dc_recorded"] if gov0 else None),
                   "connection_max": (c0[govc0]["dc"] if govc0 else None), "governing_connection": govc0,
                   "connection_max_recorded": (c0[govc0]["dc_recorded"] if govc0 else None),
                   "drift_ratio": (base.get("drift") or {}).get("ratio"), "drift_limit": (base.get("drift") or {}).get("limit"),
                   "T1": base.get("T1"), "V_kip": base.get("V_kip"), "W_kip": base.get("W_kip"), "Cs": base.get("Cs"), "seconds": base.get("seconds"),
                   "methods": {gid: v["method"] for gid, v in m0.items()},
                   "n_exact": sum(1 for v in m0.values() if v["method"] == "exact"), "n_scaled": sum(1 for v in m0.values() if v["method"] != "exact")}
    per_group = {gid: [] for gid in m0}
    per_conn = {cid: [] for cid in c0}
    gov_counts, govc_counts, combo_counts = {}, {}, {}
    for r in ok:
        md = member_dcs(dmembers, bg, r["groups"], basis)
        cd = connection_dcs(dconns, bh, r.get("handles") or {}, basis)
        gov = max(md.items(), key=lambda kv: kv[1]["dc"])[0] if md else None
        govc = max(cd.items(), key=lambda kv: kv[1]["dc"])[0] if cd else None
        for gid, v in md.items():
            per_group.setdefault(gid, []).append(v["dc"])
        for cid, v in cd.items():
            per_conn.setdefault(cid, []).append(v["dc"])
        if gov:
            gov_counts[gov] = gov_counts.get(gov, 0) + 1
            cb = md[gov]["combo"] or "?"; combo_counts[cb] = combo_counts.get(cb, 0) + 1
        if govc:
            govc_counts[govc] = govc_counts.get(govc, 0) + 1
        s = r.get("sample_summary") or {}
        out["rows"].append({"id": r["id"], "member_max": (round(md[gov]["dc"], 6) if gov else None), "governing": gov,
                            "governing_combo": (md[gov]["combo"] if gov else None), "governing_story": (md[gov]["story"] if gov else None),
                            "n_over": sum(1 for v in md.values() if v["dc"] > 1.0),
                            "connection_max": (cd[govc]["dc"] if govc else None), "governing_connection": govc,
                            "drift_ratio": (r.get("drift") or {}).get("ratio"), "T1": r.get("T1"), "V_kip": r.get("V_kip"), "Cs": r.get("Cs"),
                            "seconds": r.get("seconds"), "E_factor": s.get("E_factor"), "thk_mean": s.get("thk_mean"), "thk_min": s.get("thk_min"),
                            "lean_top": s.get("lean_top"), "psi_max": s.get("psi_max"), "dead": s.get("dead"),
                            "groups": {gid: round(v["dc"], 4) for gid, v in md.items()}})
    if not ok:
        return out
    xm = np.array([r["member_max"] for r in out["rows"] if r["member_max"] is not None], dtype=float)
    xc = np.array([r["connection_max"] for r in out["rows"] if r["connection_max"] is not None], dtype=float)
    xd = np.array([r["drift_ratio"] for r in out["rows"] if r["drift_ratio"] is not None], dtype=float)
    b = out["base"]
    if len(xm):
        out["members"] = {"stats": describe(xm, b["member_max"]), "histogram": histogram(xm, [b["member_max"], 1.0])}
    if len(xc):
        out["connections"] = {"stats": describe(xc, b["connection_max"]), "histogram": histogram(xc, [b["connection_max"], 1.0])}
    if len(xd):
        out["drift"] = {"stats": describe(xd, b["drift_ratio"]), "histogram": histogram(xd, [b["drift_ratio"], 1.0])}
    n = len(ok)
    # ---- per group: where to take a second look
    groups = []
    for gid, vals in per_group.items():
        v = np.array(vals, dtype=float)
        if not len(v):
            continue
        d0 = m0.get(gid) or {}
        groups.append({"id": gid, "kind": d0.get("kind"), "role": d0.get("role"), "section": d0.get("section"), "limit_state": d0.get("limit_state"),
                       "dc_base": d0.get("dc_base"), "dc_recorded": d0.get("dc_recorded"), "phi": d0.get("phi"),
                       "mean": float(v.mean()), "max": float(v.max()), "min": float(v.min()), "p95": float(np.percentile(v, 95)),
                       "p_over_1": float((v > 1.0).mean()), "p_over_base": float((v > (d0.get("dc_base") or 0)).mean()),
                       "change_mean": (float(v.mean()) / d0["dc_base"] - 1.0) if d0.get("dc_base") else None,
                       "governs_share": gov_counts.get(gid, 0) / n, "method": d0.get("method"), "driver": d0.get("driver"),
                       "combo_base": (bg.get(gid) or {}).get("combo")})
    groups.sort(key=lambda g: (-g["p_over_1"], -g["max"]))
    out["groups"] = groups
    conns = []
    for cid, vals in per_conn.items():
        v = np.array(vals, dtype=float)
        if not len(v):
            continue
        d0 = c0.get(cid) or {}
        conns.append({"id": cid, "type": d0.get("type"), "limit_state": d0.get("limit_state"), "dc_base": d0.get("dc_base"),
                      "dc_recorded": d0.get("dc_recorded"), "phi": d0.get("phi"),
                      "mean": float(v.mean()), "max": float(v.max()), "p95": float(np.percentile(v, 95)), "p_over_1": float((v > 1.0).mean()),
                      "governs_share": govc_counts.get(cid, 0) / n, "mapped": d0.get("mapped")})
    conns.sort(key=lambda g: (-g["p_over_1"], -g["max"]))
    out["conn_table"] = conns
    out["governing"] = {"base": gov0, "counts": sorted(gov_counts.items(), key=lambda kv: -kv[1]),
                        "same_as_base_share": gov_counts.get(gov0, 0) / n if gov0 else None,
                        "combos": sorted(combo_counts.items(), key=lambda kv: -kv[1]), "base_combo": b["governing_combo"],
                        "connections": sorted(govc_counts.items(), key=lambda kv: -kv[1]), "base_connection": govc0,
                        "n_over_any": sum(1 for r in out["rows"] if r["n_over"] > 0) / n,
                        "n_over_mean": float(np.mean([r["n_over"] for r in out["rows"]]))}
    # ---- what drives the scatter
    corr = []
    keys = [("E_factor", "E"), ("thk_mean", "mean thickness factor"), ("thk_min", "thinnest section group"), ("lean_top", "resultant lean at the top"),
            ("psi_max", "largest story lean"), ("dead", "dead load factor")]
    summaries = [r.get("sample_summary") or {} for r in ok]
    for key, label in keys:
        v = [s.get(key) for s in summaries]
        if all(isinstance(t, (int, float)) for t in v) and len(set(v)) > 1:
            rho = spearman(np.array(v, dtype=float), xm)
            if rho is not None:
                corr.append({"key": key, "label": label, "rho": round(rho, 3)})
    if gov0:
        sec = (m0[gov0].get("section") or "").upper()
        v = [s.get(f"thk[{sec}]") for s in summaries]
        if all(isinstance(t, (int, float)) for t in v) and len(set(v)) > 1:
            rho = spearman(np.array(v, dtype=float), xm)
            if rho is not None:
                corr.append({"key": f"thk[{sec}]", "label": f"thickness of {sec} (the governing group)", "rho": round(rho, 3)})
    corr.sort(key=lambda c: -abs(c["rho"]))
    out["correlations"] = corr
    out["assessment"] = assessment(out)
    return out


def _pct(v):
    return f"{100 * v:.0f}%"


def assessment(a: dict) -> list[str]:
    b, m, g = a.get("base") or {}, a.get("members"), a.get("governing") or {}
    if not m:
        return ["No completed realisation yet."]
    st = m["stats"]; n = st["n"]
    R = a.get("ratio_label") or "D/Rₙ"
    nominal = a.get("basis", "nominal") == "nominal"
    lines = []
    if nominal:
        lines.append(f"Ratios are {R}: the FACTORED demand of each ASCE 7-22 combination over the NOMINAL capacity Rₙ -- the resistance factor "
                     f"φ is taken out of the design's recorded φRₙ (φ = 0.90 for flexure, compression, tension and interaction; 1.00 for shear of "
                     f"rolled I-shapes; 0.75 for bolts, welds, block shear and rupture; 0.90 for panel zones, plate yielding and CJP welds). "
                     f"1.0 therefore means the demand reaches the nominal strength, not that the LRFD check is exactly met; the design's own "
                     f"maximum LRFD ratio was {b.get('member_max_recorded', 0):.3f}.")
    lines.append(f"{n} as-built realisations were analysed with the design's own AISC 360 capacities. The maximum member {R} in the building is "
                 f"{st['mean']:.3f} on average (COV {100 * st['cov']:.1f}%, range {st['min']:.3f}–{st['max']:.3f}, 95th percentile {st['p95']:.3f}) "
                 f"against {b['member_max']:.3f} for the design as drawn.")
    if st.get("invariant") and g.get("base"):
        lines.append(f"The maximum does not move at all: the governing group {g['base']} is sized by a demand that the as-built variables cannot change "
                     f"(a statically determinate gravity moment or shear). The scatter is in the other groups -- the range chart and the group table below "
                     f"show it; the maximum-{R} histogram is a single bar by construction.")
    if st["p_over_limit"] > 0:
        lines.append(f"{_pct(st['p_over_limit'])} of the realisations have at least one member group over {R} = 1.0 "
                     f"(lognormal estimate {100 * st.get('p_over_limit_lognormal', 0):.1f}%); on average {g.get('n_over_mean', 0):.1f} group(s) "
                     f"exceed 1.0 in such a building. Nothing is required by the code on that account -- the design was checked on the nominal "
                     f"model -- but those are the members to take a second look at.")
    else:
        lines.append(f"No realisation puts any member group over {R} = 1.0 (lognormal estimate {100 * st.get('p_over_limit_lognormal', 0):.2f}%): "
                     f"the design's margins absorb the as-built scatter. {_pct(st['p_over_base'])} of realisations exceed the design's own maximum.")
    if g.get("base"):
        same = g.get("same_as_base_share") or 0.0
        lines.append(f"The governing check stays with {g['base']} ({b.get('governing_limit_state') or 'recorded check'}, {b.get('governing_combo')}) in "
                     f"{_pct(same)} of realisations" + ("." if same >= 0.8 else " -- the critical member moves around the building; see the table."))
        others = [(gid, c) for gid, c in g["counts"] if gid != g["base"]][:3]
        if others:
            lines.append("Other groups that govern: " + ", ".join(f"{gid} in {_pct(c / n)}" for gid, c in others) + ".")
    c = a.get("connections")
    if c:
        cs = c["stats"]
        lines.append(f"Connections: maximum {R} {cs['mean']:.3f} on average against {b['connection_max']:.3f} in the design; "
                     f"{_pct(cs['p_over_limit'])} of realisations exceed 1.0. A connection's ratio is the design's, scaled by the change in the demand "
                     f"that sizes it; capacity-designed demands (Mₚᵣ, Vₕ, Rᵧ-based) do not change with the as-built model.")
    d = a.get("drift")
    if d:
        ds = d["stats"]
        lines.append(f"Design story drift: {ds['mean']:.3f} of the allowable on average (design {b['drift_ratio']:.3f}); "
                     f"{_pct(ds['p_over_limit'])} of realisations exceed the limit.")
    look = [x for x in a.get("groups", []) if x["p_over_1"] > 0][:5]
    if look:
        lines.append("Where to look: " + "; ".join(f"{x['id']} (over 1.0 in {_pct(x['p_over_1'])}, up to {x['max']:.3f}, design {x['dc_base']:.3f})" for x in look) + ".")
    else:
        top = sorted(a.get("groups", []), key=lambda x: -(x["change_mean"] or 0))[:3]
        if top:
            lines.append("Groups whose demand grows most with the as-built scatter: " +
                         "; ".join(f"{x['id']} ({x['change_mean']:+.1%} on average, up to {x['max']:.3f})" for x in top) + ".")
    corr = a.get("correlations") or []
    if corr:
        lines.append(f"What drives the scatter of the maximum member {R} (Spearman rank correlation): " +
                     ", ".join(f"{c['label']} ρ = {c['rho']:+.2f}" for c in corr[:3]) + ".")
    if b.get("n_scaled"):
        lines.append(f"{b['n_exact']} member group(s) are re-checked with the AISC 360 formula and the recorded capacities; {b['n_scaled']} carry the "
                     f"design's ratio scaled by the largest growth of their demand components (the package records their D/C but not every capacity "
                     f"the check used) -- conservative.")
    if a.get("n_failed"):
        lines.append(f"{a['n_failed']} realisation(s) failed to analyse and are excluded; see the run log.")
    return lines


# ---------------------------------------------------------------- the plot (SVG)
def svg_histogram(block: dict | None, title: str, xlabel: str, base_label: str = "design", width: int = 820, height: int = 320,
                  limit_label: str = "1.0") -> str:
    P = PALETTE
    font = "font-family=\"Segoe UI, system-ui, -apple-system, sans-serif\""
    if not block or not block.get("histogram") or not block["histogram"].get("counts"):
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="100" viewBox="0 0 {width} 100">'
                f'<text x="16" y="56" fill="{P["dim"]}" font-size="13" {font}>{html.escape(title)}: no completed realisations yet.</text></svg>')
    st, h = block["stats"], block["histogram"]
    ml, mr, mt, mb = 56, 20, 40, 56
    W, H = width - ml - mr, height - mt - mb
    edges, counts = h["edges"], h["counts"]
    x0, x1 = edges[0], edges[-1]
    n = st["n"]; bw = edges[1] - edges[0]
    xs = np.linspace(x0, x1, 160)
    curves = {}
    ln = st.get("fit_lognormal")
    if ln and ln.get("sigma", 0) > 0:
        xx = np.maximum(xs, 1e-9)
        curves["lognormal"] = np.exp(-0.5 * ((np.log(xx) - ln["mu"]) / ln["sigma"]) ** 2) / (xx * ln["sigma"] * math.sqrt(2 * math.pi)) * n * bw
    no = st.get("fit_normal")
    if no and no.get("std", 0) > 0:
        curves["normal"] = np.exp(-0.5 * ((xs - no["mean"]) / no["std"]) ** 2) / (no["std"] * math.sqrt(2 * math.pi)) * n * bw
    ymax = max([max(counts)] + [float(c.max()) for c in curves.values()]) * 1.12
    ymax = max(1.0, ymax)
    sx = lambda v: ml + (v - x0) / (x1 - x0) * W
    sy = lambda v: mt + H - v / ymax * H
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">',
         f'<rect x="0" y="0" width="{width}" height="{height}" fill="{P["surface"]}"/>',
         f'<text x="{ml}" y="16" fill="{P["ink"]}" font-size="13" font-weight="600" {font}>{html.escape(title)}</text>']
    step = max(1, int(round(ymax / 4)))
    while step * 4 < ymax * 0.5:
        step *= 2
    yt = 0
    while yt <= ymax:
        y = sy(yt)
        o.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{ml + W}" y2="{y:.1f}" stroke="{P["grid"]}" stroke-width="1"/>')
        o.append(f'<text x="{ml - 8}" y="{y + 4:.1f}" text-anchor="end" fill="{P["dim"]}" font-size="11" {font}>{yt}</text>')
        yt += step
    slot = W / len(counts); thick = min(24.0, slot - 2.0)
    for i, c in enumerate(counts):
        if c <= 0:
            continue
        xa, xb = edges[i], edges[i + 1]
        cx = (sx(xa) + sx(xb)) / 2; x = cx - thick / 2
        top, bot = sy(c), sy(0); r = min(4.0, (bot - top) / 2, thick / 2)
        d = (f"M{x:.1f},{bot:.1f} V{top + r:.1f} Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f} H{x + thick - r:.1f} "
             f"Q{x + thick:.1f},{top:.1f} {x + thick:.1f},{top + r:.1f} V{bot:.1f} Z")
        o.append(f'<path d="{d}" fill="{P["bars"]}"><title>{xa:.3f}–{xb:.3f}: {c} realisation{"s" if c != 1 else ""} ({100 * c / n:.0f}%)</title></path>')
    for name, ys in curves.items():
        pts = " ".join(f"{sx(v):.1f},{sy(min(float(y), ymax)):.1f}" for v, y in zip(xs, ys))
        o.append(f'<polyline points="{pts}" fill="none" stroke="{P[name]}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')

    def vline(v, color, label, dy, dash=""):
        if v is None or not (x0 <= v <= x1):
            return
        x = sx(v)
        o.append(f'<line x1="{x:.1f}" y1="{mt}" x2="{x:.1f}" y2="{mt + H}" stroke="{color}" stroke-width="2"{dash}/>')
        anchor = "end" if x > ml + W * 0.66 else "start"
        tx = x - 6 if anchor == "end" else x + 6
        o.append(f'<text x="{tx:.1f}" y="{mt + dy}" text-anchor="{anchor}" fill="{P["ink"]}" font-size="11.5" {font}>{html.escape(label)}</text>')
    vline(st.get("base"), P["base"], f"{base_label} {st['base']:.3f}" if st.get("base") is not None else "", 14)
    vline(1.0, P["limit"], f"{limit_label} = 1.0", 30)
    vline(st["mean"], P["dim"], f"mean {st['mean']:.3f}", 46)
    o.append(f'<line x1="{ml}" y1="{mt + H}" x2="{ml + W}" y2="{mt + H}" stroke="{P["grid"]}" stroke-width="1"/>')
    for i in range(9):
        v = x0 + (x1 - x0) * i / 8
        o.append(f'<text x="{sx(v):.1f}" y="{mt + H + 18}" text-anchor="middle" fill="{P["dim"]}" font-size="11" {font}>{v:.2f}</text>')
    o.append(f'<text x="{ml + W / 2:.1f}" y="{height - 8}" text-anchor="middle" fill="{P["dim"]}" font-size="12" {font}>{html.escape(xlabel)}</text>')
    items = [("bars", "realisations", "rect")]
    if "lognormal" in curves: items.append(("lognormal", "lognormal fit", "line"))
    if "normal" in curves: items.append(("normal", "normal fit", "line"))
    items.append(("base", base_label, "line"))
    x = ml + W - 4
    for key, label, kind in reversed(items):
        tw = 7 * len(label) + 26; x -= tw; y = mt - 14
        if kind == "rect":
            o.append(f'<rect x="{x}" y="{y - 5}" width="10" height="10" fill="{P[key]}"/>')
        else:
            o.append(f'<line x1="{x}" y1="{y}" x2="{x + 12}" y2="{y}" stroke="{P[key]}" stroke-width="2"/>')
        o.append(f'<text x="{x + 16}" y="{y + 4}" fill="{P["dim"]}" font-size="11" {font}>{label}</text>')
    o.append("</svg>")
    return "\n".join(o)


def svg_groups(a: dict, width: int = 820) -> str:
    """One row per member group: the D/C range over the realisations (bar min..max, dot at the
    mean), the design's own value, and the D/C = 1.0 line. This is the 'where to look' picture."""
    P = PALETTE
    font = "font-family=\"Segoe UI, system-ui, -apple-system, sans-serif\""
    groups = [g for g in (a.get("groups") or []) if g.get("dc_base") is not None]
    if not groups:
        return ""
    groups = sorted(groups, key=lambda g: -max(g["max"], g["dc_base"]))
    rowh, ml, mr, mt = 22, 190, 24, 34
    height = mt + rowh * len(groups) + 40
    W = width - ml - mr
    xmax = max(1.05, max(max(g["max"], g["dc_base"]) for g in groups) * 1.06)
    sx = lambda v: ml + v / xmax * W
    R = a.get("ratio_label") or "D/C"
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(R)} range per member group">',
         f'<rect x="0" y="0" width="{width}" height="{height}" fill="{P["surface"]}"/>',
         f'<text x="{ml}" y="16" fill="{P["ink"]}" font-size="13" font-weight="600" {font}>{html.escape(R)} per member group: range over the realisations, mean, and the design</text>']
    for i in range(0, 11):
        v = xmax * i / 10
        o.append(f'<line x1="{sx(v):.1f}" y1="{mt}" x2="{sx(v):.1f}" y2="{mt + rowh * len(groups)}" stroke="{P["grid"]}" stroke-width="1"/>')
        o.append(f'<text x="{sx(v):.1f}" y="{mt + rowh * len(groups) + 16}" text-anchor="middle" fill="{P["dim"]}" font-size="11" {font}>{v:.2f}</text>')
    if xmax >= 1.0:
        o.append(f'<line x1="{sx(1.0):.1f}" y1="{mt - 4}" x2="{sx(1.0):.1f}" y2="{mt + rowh * len(groups)}" stroke="{P["limit"]}" stroke-width="2"/>')
        near_edge = sx(1.0) > width - 90
        o.append(f'<text x="{sx(1.0) + (-4 if near_edge else 4):.1f}" y="{mt - 6}" text-anchor="{"end" if near_edge else "start"}" '
                 f'fill="{P["ink"]}" font-size="11" {font}>{html.escape(R)} = 1.0</text>')
    for k, g in enumerate(groups):
        y = mt + rowh * k + rowh / 2
        o.append(f'<text x="{ml - 8}" y="{y + 4:.1f}" text-anchor="end" fill="{P["ink"]}" font-size="11.5" {font}>{html.escape(g["id"])}</text>')
        x0, x1 = sx(g["min"]), sx(g["max"])
        o.append(f'<rect x="{x0:.1f}" y="{y - 5:.1f}" width="{max(2.0, x1 - x0):.1f}" height="10" rx="2" fill="{P["bars"]}" opacity="0.9">'
                 f'<title>{html.escape(g["id"])}: {g["min"]:.3f}–{g["max"]:.3f}, mean {g["mean"]:.3f}, design {g["dc_base"]:.3f}, over 1.0 in {100 * g["p_over_1"]:.0f}%</title></rect>')
        o.append(f'<circle cx="{sx(g["mean"]):.1f}" cy="{y:.1f}" r="4" fill="{P["ink"]}" stroke="{P["surface"]}" stroke-width="2"/>')
        xb = sx(g["dc_base"])
        o.append(f'<path d="M{xb:.1f},{y - 8:.1f} L{xb + 5:.1f},{y:.1f} L{xb:.1f},{y + 8:.1f} L{xb - 5:.1f},{y:.1f} Z" fill="{P["base"]}" stroke="{P["surface"]}" stroke-width="1.5"/>')
    y = height - 8
    o.append(f'<rect x="{ml}" y="{y - 9}" width="12" height="8" rx="2" fill="{P["bars"]}"/><text x="{ml + 16}" y="{y}" fill="{P["dim"]}" font-size="11" {font}>range over the realisations</text>')
    o.append(f'<circle cx="{ml + 200}" cy="{y - 5}" r="4" fill="{P["ink"]}"/><text x="{ml + 208}" y="{y}" fill="{P["dim"]}" font-size="11" {font}>mean</text>')
    o.append(f'<path d="M{ml + 260},{y - 11} L{ml + 265},{y - 5} L{ml + 260},{y + 1} L{ml + 255},{y - 5} Z" fill="{P["base"]}"/><text x="{ml + 272}" y="{y}" fill="{P["dim"]}" font-size="11" {font}>design</text>')
    o.append("</svg>")
    return "\n".join(o)


def plots(a: dict) -> dict:
    R = a.get("ratio_label") or "D/C"
    what = "factored demand / nominal capacity, φ removed" if a.get("basis", "nominal") == "nominal" else "the design's LRFD ratio D/φRₙ"
    return {"members": svg_histogram(a.get("members"), f"Maximum member {R} in the building", f"max {R} over the member groups — {what}", limit_label=R),
            "connections": svg_histogram(a.get("connections"), f"Maximum connection {R} in the building", f"max {R} over the designed connections — {what}", limit_label=R),
            "drift": svg_histogram(a.get("drift"), "Design story drift / allowable", "max design story drift as a fraction of the ASCE 7 allowable", limit_label="drift / allowable"),
            "groups": svg_groups(a)}


# ---------------------------------------------------------------- written outputs
ROW_COLS = ["id", "member_max", "governing", "governing_combo", "governing_story", "n_over", "connection_max", "governing_connection",
            "drift_ratio", "T1", "V_kip", "Cs", "E_factor", "thk_mean", "thk_min", "lean_top", "psi_max", "dead", "seconds"]


def write_outputs(study_dir: pathlib.Path, a: dict, project: str, probe: dict | None):
    study_dir.mkdir(parents=True, exist_ok=True)
    (study_dir / "summary.json").write_text(json.dumps({k: v for k, v in a.items() if k != "rows"}, indent=1, default=str), encoding="utf-8")
    gids = sorted({g for r in a.get("rows") or [] for g in (r.get("groups") or {})})
    buf = io.StringIO(); w = csv.writer(buf); w.writerow(ROW_COLS + [f"DC[{g}]" for g in gids])
    for r in a.get("rows") or []:
        w.writerow([r.get(k) for k in ROW_COLS] + [(r.get("groups") or {}).get(g) for g in gids])
    (study_dir / "results.csv").write_text(buf.getvalue(), encoding="utf-8")
    p = plots(a)
    (study_dir / "dc_members.svg").write_text(p["members"], encoding="utf-8")
    (study_dir / "dc_connections.svg").write_text(p["connections"], encoding="utf-8")
    (study_dir / "dc_groups.svg").write_text(p.get("groups", ""), encoding="utf-8")
    (study_dir / "report.html").write_text(report_html(a, project, probe, p), encoding="utf-8")


def report_html(a: dict, project: str, probe: dict | None, p: dict) -> str:
    b, g = a.get("base") or {}, a.get("governing") or {}
    P = PALETTE

    def f3(v):
        return "—" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))

    def stat_rows(block, label):
        if not block:
            return ""
        st = block["stats"]
        rows = [("realisations", st["n"]), ("design value", f3(st.get("base"))), ("mean", f3(st["mean"])), ("standard deviation", f3(st["std"])),
                ("COV", f"{100 * st['cov']:.1f}%"), ("median", f3(st["median"])), ("min / max", f"{f3(st['min'])} / {f3(st['max'])}"),
                ("5th / 95th percentile", f"{f3(st['p5'])} / {f3(st['p95'])}"), ("skewness", f3(st["skewness"])),
                ("P(> 1.0)", f"{_pct(st['p_over_limit'])} empirical" + (f", {100 * st['p_over_limit_lognormal']:.2f}% lognormal" if "p_over_limit_lognormal" in st else "")),
                ("P(> design value)", _pct(st["p_over_base"]) if "p_over_base" in st else "—")]
        ln = st.get("fit_lognormal")
        if ln:
            rows.append(("lognormal fit", f"μₗₙ = {ln['mu']:.4f}, σₗₙ = {ln['sigma']:.4f}; KS D = {ln['ks'].get('D')}, p = {ln['ks'].get('p')}"))
        no = st.get("fit_normal")
        if no:
            rows.append(("normal fit", f"μ = {no['mean']:.4f}, σ = {no['std']:.4f}; KS D = {no['ks'].get('D')}, p = {no['ks'].get('p')}"))
        return f"<h3>{html.escape(label)}</h3><table>" + "".join(f"<tr><td>{html.escape(str(k))}</td><td class=n>{html.escape(str(v))}</td></tr>" for k, v in rows) + "</table>"

    n = (a.get("members") or {}).get("stats", {}).get("n", 0)
    gov_rows = "".join(f"<tr><td>{html.escape(gid)}{' (design)' if gid == g.get('base') else ''}</td><td class=n>{c}</td><td class=n>{_pct(c / n) if n else ''}</td></tr>"
                       for gid, c in g.get("counts", []))
    combo_rows = "".join(f"<tr><td><code>{html.escape(cb)}</code></td><td class=n>{c}</td><td class=n>{_pct(c / n) if n else ''}</td></tr>" for cb, c in g.get("combos", []))
    group_rows = "".join(f"<tr><td>{html.escape(x['id'])}</td><td>{html.escape(str(x.get('limit_state') or ''))}</td><td class=n>{f3(x['dc_base'])}</td>"
                         f"<td class=n>{f3(x['mean'])}</td><td class=n>{f3(x['p95'])}</td><td class=n>{f3(x['max'])}</td><td class=n>{_pct(x['p_over_1'])}</td>"
                         f"<td class=n>{_pct(x['governs_share'])}</td><td>{x['method']}</td></tr>" for x in a.get("groups", []))
    conn_rows = "".join(f"<tr><td>{html.escape(x['id'])}</td><td>{html.escape(str(x.get('type') or ''))}</td><td class=n>{f3(x['dc_base'])}</td>"
                        f"<td class=n>{f3(x['mean'])}</td><td class=n>{f3(x['max'])}</td><td class=n>{_pct(x['p_over_1'])}</td>"
                        f"<td>{html.escape(', '.join(f'{k} ← {h}' for k, h in (x.get('mapped') or [])))}</td></tr>" for x in a.get("conn_table", []))
    corr_rows = "".join(f"<tr><td>{html.escape(c['label'])}</td><td class=n>{c['rho']:+.2f}</td></tr>" for c in (a.get("correlations") or [])[:10])
    var_rows = "".join(f"<tr><td>{html.escape(v.get('label', v['id']))}</td><td>{v['level']}</td><td>{v['dist']}</td><td class=n>{v.get('mean')}</td>"
                       f"<td class=n>{v.get('cov') if v.get('cov') is not None else ('σ = ' + str(v.get('std')))}</td><td>{'on' if v.get('enabled', True) else 'off'}</td></tr>"
                       for v in (a.get("variables") or []))
    real_rows = "".join(f"<tr><td class=n>{r['id']}</td><td class=n>{f3(r['member_max'])}</td><td>{html.escape(str(r.get('governing') or ''))}</td>"
                        f"<td class=n>{r.get('n_over')}</td><td class=n>{f3(r.get('connection_max'))}</td><td class=n>{f3(r.get('drift_ratio'))}</td>"
                        f"<td class=n>{f3(r.get('T1'))}</td><td class=n>{f3(r.get('E_factor'))}</td><td class=n>{f3(r.get('thk_mean'))}</td>"
                        f"<td class=n>{(1 / r['lean_top']) if r.get('lean_top') else 0:.0f}</td></tr>" for r in a.get("rows") or [])
    model = probe or {}
    R = a.get("ratio_label") or "D/C"
    phi_rows = "".join(f"<tr><td>{html.escape(w)}</td><td class=n>{v:.2f}</td><td>{html.escape(src)}</td></tr>" for w, v, src in (a.get("phi_notes") or []))
    basis_box = (f"<div class=box><p><b>Ratios are {R}: factored demand over NOMINAL capacity.</b> The design's package records φRₙ and D/φRₙ; "
                 f"here the resistance factor is taken out (Rₙ = φRₙ / φ), so 1.0 means the factored demand reaches the nominal strength, not that the "
                 f"LRFD check is exactly met. The design's own maximum LRFD ratio was {f3(b.get('member_max_recorded'))} (members) and "
                 f"{f3(b.get('connection_max_recorded'))} (connections). The φ values assumed to undo the factoring:</p>"
                 f"<table><tr><th>check</th><th>φ</th><th>clause</th></tr>{phi_rows}</table></div>"
                 if a.get("basis", "nominal") == "nominal" else
                 f"<div class=box><p><b>Ratios are {R}: the design's own LRFD ratio</b> (factored demand over φRₙ), recomputed for each realisation.</p></div>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Probabilistic analysis — {html.escape(project)}</title>
<style>body{{background:{P['surface']};color:{P['ink']};font:14px/1.5 'Segoe UI',system-ui,sans-serif;margin:0;padding:28px 36px;max-width:1100px}}
h1{{font-size:20px;color:#fff;margin:0 0 4px}} h2{{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:{P['dim']};margin:28px 0 8px}}
h3{{font-size:13px;color:{P['ink']};margin:14px 0 4px}} p{{max-width:900px}} .dim{{color:{P['dim']};font-size:12.5px}}
table{{border-collapse:collapse;font-size:12.5px;margin:6px 0}} td,th{{padding:5px 10px;border-bottom:1px solid {P['grid']};text-align:left;vertical-align:top}}
th{{color:{P['dim']};font-size:11px;text-transform:uppercase;letter-spacing:.06em}} td.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.box{{border:1px solid {P['grid']};border-radius:8px;padding:14px 18px;margin:10px 0}} .assess li{{margin:4px 0}}
code{{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}} svg{{max-width:100%;height:auto}}</style></head><body>
<h1>Probabilistic analysis — {html.escape(project)}</h1>
<p class=dim>{html.escape(str(model.get('name') or ''))} · {model.get('members', '?')} members · {model.get('stories', '?')} stories · {html.escape(str(model.get('system') or ''))}
· seed {a.get('seed')} · {time.strftime('%Y-%m-%d %H:%M')}</p>
<div class=box><p>Monte Carlo variation of the as-built structure: modulus of elasticity, plate thickness per section group and a story-by-story
out-of-plumb profile (and optionally the dead load) are drawn from published statistical distributions; each realisation is analysed by the design's
own elastic LRFD model -- every ASCE 7-22 combination with P-Δ, the ELF forces from its own period -- and the resulting demands are checked
against the design's own AISC 360 capacities, which are never recomputed. The question answered is whether the building as built still satisfies the
member checks it was designed to; a ratio above 1.0 here is a place to look, not a code requirement to act.</p></div>
{basis_box}
<h2>Maximum member {R}</h2>{p['members']}
<h2>{R} per member group</h2>{p.get('groups', '')}
<h2>Maximum connection {R}</h2>{p['connections']}
<h2>Assessment</h2><ul class=assess>{''.join('<li>' + html.escape(s) + '</li>' for s in a.get('assessment') or [])}</ul>
<h2>Statistical parameters</h2>{stat_rows(a.get('members'), f'Maximum member {R}')}{stat_rows(a.get('connections'), f'Maximum connection {R}')}{stat_rows(a.get('drift'), 'Design story drift / allowable')}
<h2>Governing check</h2>
<p class=dim>Design: {html.escape(str(g.get('base') or ''))} — {html.escape(str(b.get('governing_limit_state') or ''))}, combination <code>{html.escape(str(b.get('governing_combo') or ''))}</code></p>
<table><tr><th>governing group</th><th>runs</th><th>share</th></tr>{gov_rows}</table>
<table><tr><th>governing combination</th><th>runs</th><th>share</th></tr>{combo_rows}</table>
<h2>Member groups — where to take a second look</h2>
<table><tr><th>group</th><th>check</th><th>design {R}</th><th>mean</th><th>95th pct</th><th>max</th><th>over 1.0 in</th><th>governs in</th><th>method</th></tr>{group_rows}</table>
<h2>Connections</h2>
<table><tr><th>connection</th><th>type</th><th>design {R}</th><th>mean</th><th>max</th><th>over 1.0 in</th><th>demand scaled by</th></tr>{conn_rows}</table>
<h2>What drives the scatter</h2><table><tr><th>variable</th><th>Spearman ρ with max member {R}</th></tr>{corr_rows}</table>
<h2>Random variables</h2><table><tr><th>variable</th><th>sampled per</th><th>distribution</th><th>mean</th><th>COV / σ</th><th></th></tr>{var_rows}</table>
<h2>Realisations</h2><table><tr><th>#</th><th>max member {R}</th><th>governing</th><th>groups &gt; 1</th><th>max conn. {R}</th><th>drift / allow.</th><th>T₁ (s)</th><th>E</th><th>thk</th><th>H/lean</th></tr>{real_rows}</table>
</body></html>"""
