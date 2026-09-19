"""worker.py -- one process, a batch of Monte Carlo realisations of the LRFD design model.

Runs under the Nonlinear module's interpreter (openseespy, numpy) with the HR Steel engine on
sys.path -- never under the module server's own interpreter. Stdlib only on top of those.

    python worker.py probe --package DIR --engine DIR --out FILE
        -> model summary, section groups, the design's D/C table, the combination labels
    python worker.py run --package DIR --engine DIR --spec realisations.json --out DIR --ids 0,3,7
        -> <out>/r0000.json, r0003.json ... one per realisation, progress lines on stdout

A realisation is the design's own elastic analysis model (HR Steel's engine: the lumped-mass
dynamic model for the period and the ELF forces, the distributed static model for every ASCE 7-22
LRFD combination with P-Delta) with the sampled quantities substituted:
  * E for the whole building (one draw)
  * flange/web thickness per SECTION GROUP -> A, I, J of every member in the group
  * a story-by-story out-of-plumb profile in X and Y (node coordinates), accumulated up the height
  * optionally the dead load (mass and gravity) for the whole building
It writes the per-group DEMAND envelope (P, Mx, My, V and the governing combination), the period,
the base shear and the design drifts. Capacities are never recomputed: the server applies the
design's own AISC 360 capacities to these demands. Realisation 0 is the nominal model.
"""
import argparse, json, math, os, sys, time, traceback


def _setup(engine_dir):
    if engine_dir and engine_dir not in sys.path:
        sys.path.insert(0, engine_dir)
    os.environ.setdefault("STELTIC_ENGINE_DIR", engine_dir or "")


def load_cfg(package):
    """exec cfg.py the way HR Steel does (cwd = the package, engine importable) -> cfg dict."""
    import engine3d  # noqa: F401
    src = open(os.path.join(package, "cfg.py"), encoding="utf-8").read()
    ns = {"__name__": "steltic_cfg", "__file__": os.path.join(package, "cfg.py")}
    old = os.getcwd()
    try:
        os.chdir(package)
        exec(compile(src, "cfg.py", "exec"), ns)
    finally:
        os.chdir(old)
    cfg = ns.get("cfg")
    if not isinstance(cfg, dict):
        raise ValueError("cfg.py does not define a top-level `cfg = dict(...)`")
    return cfg


# ---------------------------------------------------------------- the perturbed engine
class Perturbation:
    """Patches the engine for ONE realisation: E, per-section (A, I, J) scaling, node lean.
    Restores everything on exit."""

    def __init__(self, sample, levels):
        self.sample = sample or {}
        self.levels = sorted(levels or [])
        self.saved = {}

    def _lean_at(self, z):
        lv = self.sample.get("lean") or {}
        if not lv or len(self.levels) < 2:
            return 0.0, 0.0
        ox = oy = 0.0
        for k, (a, b) in enumerate(zip(self.levels, self.levels[1:]), start=1):
            px, py = lv.get(str(k), (0.0, 0.0))
            if z >= b - 1e-6:
                ox += px * (b - a); oy += py * (b - a)
            elif z > a + 1e-6:
                ox += px * (z - a); oy += py * (z - a)
                break
            else:
                break
        return ox, oy

    def __enter__(self):
        import openseespy.opensees as ops
        import engine3d as E
        import static_model as SM
        s = self.sample
        self.saved = {"E": E.E, "EMOD": SM.EMOD, "Ipack": E.Ipack, "SM_Ipack": SM.Ipack, "node": ops.node}
        if s.get("E"):
            E.E = float(s["E"]); SM.EMOD = float(s["E"])
        thk = {k.upper(): float(v.get("thk", 1.0)) for k, v in (s.get("groups") or {}).items()}
        orig_ipack = self.saved["Ipack"]
        if any(abs(f - 1.0) > 1e-9 for f in thk.values()):
            def ipack(name):
                A, Ix, Iy, J = orig_ipack(name)
                f = thk.get(str(name).upper().strip(), 1.0)
                return A * f, Ix * f, Iy * f, J * f ** 3       # plate thickness: A, I ~ t ; J ~ t^3
            E.Ipack = ipack; SM.Ipack = ipack
        if s.get("lean"):
            real_node = self.saved["node"]
            lean_at = self._lean_at

            def node(tag, x, y, z, *rest):
                ox, oy = lean_at(float(z))
                return real_node(tag, x + ox, y + oy, z, *rest)
            ops.node = node
        return self

    def __exit__(self, *a):
        import openseespy.opensees as ops
        import engine3d as E
        import static_model as SM
        E.E = self.saved["E"]; SM.EMOD = self.saved["EMOD"]
        E.Ipack = self.saved["Ipack"]; SM.Ipack = self.saved["SM_Ipack"]
        ops.node = self.saved["node"]
        return False


def _cfg_for(cfg, sample):
    import copy
    c = copy.copy(cfg)               # shallow: custom_build and friends shared, scalars replaced
    c.pop("present", None)           # re-probe the footprint under this realisation
    dl = (sample or {}).get("dead")
    if dl and abs(float(dl) - 1.0) > 1e-9:
        for k in ("D_floor", "D_roof", "clad"):
            if c.get(k) is not None:
                c[k] = float(c[k]) * float(dl)
        if c.get("extra_mass_floors"):
            c["extra_mass_floors"] = {k: v * float(dl) for k, v in c["extra_mass_floors"].items()}
    return c


def _roles(E, info0, cfg):
    NF = len(cfg["heights"])
    brace_lines = set()
    for (t, kind, sec, n1, n2) in info0["ele"]:
        if kind == "brace":
            for nd in (n1, n2):
                brace_lines.add(((nd % 100000) // 100, nd % 100))
    moment_lines = {((nd % 100000) // 100, nd % 100) for nd in info0.get("moment_nodes", set())}
    lateral = brace_lines | moment_lines

    def role(kind, n1, n2):
        if kind == "brace":
            return "brace"
        if kind == "beam":
            return "roof" if (n1 // 100000) >= NF else "floor"
        ij = ((n1 % 100000) // 100, n1 % 100)
        return "lateral_col" if ij in lateral else "gravity_col"
    return role


def analyse(package, engine_dir, sample, nseg=None):
    """One elastic LRFD analysis of the (perturbed) design -> group demand envelopes + globals."""
    import openseespy.opensees as ops
    import engine3d as E
    import design_pipeline as DP
    import static_model as SM
    cfg0 = load_cfg(package)
    cfg = _cfg_for(cfg0, sample)
    levels = [0.0]
    for h in cfg["heights"]:
        levels.append(levels[-1] + float(h))
    with Perturbation(sample, levels):
        E.clear_caches()
        t0 = time.time()
        cases = DP.combos(cfg)
        info0 = E.build(cfg, "PDelta")
        reg = {t: (kind, sec, n1, n2) for (t, kind, sec, n1, n2) in info0["ele"]}
        role = _roles(E, info0, cfg)
        glob = E.run(cfg)
        dl, _rho = E.drift_allowable(cfg)
        brace_lines = set()
        for (t, kind, sec, n1, n2) in info0["ele"]:
            if kind == "brace":
                for nd in (n1, n2):
                    brace_lines.add(((nd % 100000) // 100, nd % 100))
        moment_lines = {((nd % 100000) // 100, nd % 100) for nd in info0.get("moment_nodes", set())}
        lat_lines = brace_lines | moment_lines

        def _is_lat(t):
            k, sec, n1, n2 = reg[t]
            return True if k == "brace" else (((n1 % 100000) // 100, n1 % 100) in lat_lines)
        sec_sig = repr(sorted(reg[t][1] for t in reg))
        lat_sig = repr(sorted(reg[t][1] for t in reg if _is_lat(t)))
        determinate = str((cfg.get("model") or {}).get("joints", "")).lower() == "pinned"
        senv, _k = SM.demand_envelope(cfg, cases, nseg=int(nseg or cfg.get("demand_nseg", 6)),
                                      floor_system=cfg.get("floor_system", "one-way"), determinate=determinate,
                                      sec_sig=sec_sig, lat_sig=lat_sig, cache_dir=None)
        E.clear_caches()
    # per-element envelope, then the group envelope exactly as design_pipeline.design() forms it
    env = {}
    for t in reg:
        se = senv.get(frozenset((reg[t][2], reg[t][3])))
        env[t] = dict(comp=se["comp"], tens=se["tens"], Mz=se["Mz"], My=se["My"], V=se["V"], combo=se["combo"]) if se \
            else dict(comp=0.0, tens=0.0, Mz=0.0, My=0.0, V=0.0, combo="")
    groups = {}
    for t in reg:
        kind, sec, n1, n2 = reg[t]
        gid = "%s-%s" % (role(kind, n1, n2), sec)
        g = groups.setdefault(gid, dict(kind=kind, section=sec, role=role(kind, n1, n2), n=0, comp=0.0, tens=0.0, Mz=0.0, My=0.0, V=0.0,
                                        combo="", score=-1.0, story_max=None))
        e = env[t]; g["n"] += 1
        for q in ("comp", "tens", "Mz", "My", "V"):
            g[q] = max(g[q], e[q])
        sc = max(e["comp"], e["tens"]) if kind in ("col", "brace") else e["Mz"]
        if sc > g["score"]:
            g["score"] = sc; g["combo"] = e["combo"]
            g["story_max"] = (n1 // 100000) + (1 if kind == "col" else 0)
    for g in groups.values():
        g.pop("score", None)
    kinds = {"col": 0, "beam": 0, "brace": 0}
    for t in reg:
        kinds[reg[t][0]] = kinds.get(reg[t][0], 0) + 1
    # building-level demand handles for the connection scaling
    def _max(kind_set, q):
        vals = [env[t][q] for t in reg if reg[t][0] in kind_set]
        return max(vals) if vals else 0.0
    handles = {
        "beam_V": _max(("beam",), "V"), "beam_M": _max(("beam",), "Mz"),
        "gravity_beam_V": max([env[t]["V"] for t in reg if reg[t][0] == "beam" and reg[t][3] not in info0.get("moment_nodes", set())] or [0.0]),
        "col_P": _max(("col",), "comp"), "col_T": _max(("col",), "tens"), "col_M": _max(("col",), "Mz"), "col_V": _max(("col",), "V"),
        "brace_P": max(_max(("brace",), "comp"), _max(("brace",), "tens")),
        "V_base": float(glob.get("V") or 0.0),
    }
    return {"T1": glob.get("T1"), "Ta": glob.get("Ta"), "Cs": glob.get("Cs"), "V_kip": glob.get("V"), "W_kip": glob.get("W"),
            "drift": {"mdx": glob.get("mdx"), "mdy": glob.get("mdy"), "limit": dl, "ratio": (max(glob.get("mdx") or 0, glob.get("mdy") or 0) / dl if dl else None)},
            "checks": glob.get("checks"), "n_cases": len(cases), "n_members": len(reg), "kinds": kinds,
            "groups": groups, "handles": handles, "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- probe
def probe(a):
    _setup(a.engine)
    import engine3d as E
    import design_pipeline as DP
    cfg = load_cfg(a.package)
    info0 = E.build(cfg, "Linear")
    role = _roles(E, info0, cfg)
    groups = {}
    for (t, kind, sec, n1, n2) in info0["ele"]:
        gid = "%s-%s" % (role(kind, n1, n2), sec)
        d = groups.setdefault(gid, {"id": gid, "section": sec.upper(), "kind": kind, "role": role(kind, n1, n2), "n": 0})
        d["n"] += 1
    levels = [0.0]
    for h in cfg["heights"]:
        levels.append(levels[-1] + float(h))
    cases = DP.combos(cfg)
    pkg = {}
    cp = os.path.join(a.package, "design", "calc_package.json")
    if os.path.exists(cp):
        pkg = json.load(open(cp, encoding="utf-8"))
    members = []
    for m in pkg.get("members", []):
        inp = m.get("inputs") or {}
        members.append({"id": m.get("id"), "kind": inp.get("kind"), "role": inp.get("role"), "section": (inp.get("section") or "").upper(),
                        "DC": m.get("DC"), "limit_state": m.get("limit_state"), "capacity": m.get("capacity") or {},
                        "demands": {k: inp.get(k) for k in ("P_comp_kip", "P_tens_kip", "Mz_kipin", "My_kipin", "V_kip", "governing_combo")}})
    conns = []
    for c in pkg.get("connections", []):
        conns.append({"id": c.get("id"), "type": c.get("type"), "section": c.get("section"), "DC": c.get("DC"),
                      "limit_state": c.get("limit_state"), "demand": c.get("demand") or {}, "capacity": c.get("capacity") or {}})
    out = {"name": os.path.basename(a.package.rstrip("/\\")), "members": len(info0["ele"]), "stories": len(cfg["heights"]), "levels": levels,
           "NX": cfg.get("NX"), "NY": cfg.get("NY"), "Fy_nominal": float(cfg.get("Fy", 50.0)), "system": (pkg.get("capacity_design") or {}).get("system"),
           "risk_category": cfg.get("risk_category"), "groups": sorted(groups.values(), key=lambda g: (g["kind"], g["role"], g["section"])),
           "sections": sorted({g["section"] for g in groups.values()}),
           "combos": [{"label": c[0], "col_only": bool(c[5])} for c in cases],
           "design": {"members": members, "connections": conns, "n_members_dc": sum(1 for m in members if isinstance(m["DC"], (int, float))),
                      "n_connections_dc": sum(1 for c in conns if isinstance(c["DC"], (int, float)))},
           "drift_limit": E.drift_allowable(cfg)[0]}
    json.dump(out, open(a.out, "w"), indent=1)
    print("probe ok", flush=True)


# ---------------------------------------------------------------- run
def run(a):
    _setup(a.engine)
    spec = json.load(open(a.spec))
    opts = spec.get("options") or {}
    ids = [int(x) for x in a.ids.split(",") if x.strip() != ""] if a.ids else [r["id"] for r in spec["realisations"]]
    want = {r["id"]: r for r in spec["realisations"]}
    os.makedirs(a.out, exist_ok=True)
    for i in ids:
        r = want.get(i)
        if r is None:
            continue
        outp = os.path.join(a.out, "r%04d.json" % i)
        if os.path.exists(outp) and not a.force:
            print(json.dumps({"event": "skip", "id": i}), flush=True)
            continue
        print(json.dumps({"event": "start", "id": i, "nominal": bool(r.get("nominal"))}), flush=True)
        t0 = time.time()
        try:
            res = analyse(a.package, a.engine, {} if r.get("nominal") else (r.get("sample") or {}), nseg=opts.get("nseg"))
            out = {"id": i, "ok": True, "nominal": bool(r.get("nominal")), "sample_summary": (r.get("sample") or {}).get("summary"), **res}
        except Exception as e:
            out = {"id": i, "ok": False, "error": "%s: %s" % (type(e).__name__, e), "trace": traceback.format_exc()[-2000:],
                   "seconds": round(time.time() - t0, 1), "nominal": bool(r.get("nominal"))}
        tmp = outp + ".tmp"
        json.dump(out, open(tmp, "w"), indent=1, default=str)
        for _i in range(60):                       # Windows: a reader holding the file blocks the replace briefly
            try:
                os.replace(tmp, outp); break
            except PermissionError:
                if _i == 59:
                    raise
                time.sleep(0.01)
        print(json.dumps({"event": "done", "id": i, "ok": out.get("ok"), "T1": out.get("T1"), "V_kip": out.get("V_kip"),
                          "seconds": out.get("seconds"), "error": out.get("error")}), flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="probabilistic-worker")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("probe"); p.add_argument("--package", required=True); p.add_argument("--engine", default=None); p.add_argument("--out", required=True)
    r = sub.add_parser("run"); r.add_argument("--package", required=True); r.add_argument("--engine", default=None)
    r.add_argument("--spec", required=True); r.add_argument("--out", required=True); r.add_argument("--ids", default="")
    r.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "probe":
        return probe(a)
    if a.cmd == "run":
        return run(a)
    ap.print_help()


if __name__ == "__main__":
    main()
