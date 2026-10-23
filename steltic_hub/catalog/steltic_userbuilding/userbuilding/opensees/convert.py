"""A recorded OpenSees model -> HR Steel's building description (steltic.building_description v1).

The description is what the template and the ETABS .e2k produce; HR Steel then asks for whatever it does not
settle, builds the model as given, and the Nonlinear module runs on it. The same rules as the .e2k reader:
what the model STATES is data (source "file"): geometry, members, end releases, supports, rigid diaphragms,
the masses. What has to be INTERPRETED is a proposal the user accepts or replaces: the AISC shape a set of
section properties matches, the dead load per level derived from the masses. What version 1 cannot carry is
refused, never simplified away.

How the model is read
  * Units: OpenSees has none. They are read from the steel modulus E the model uses (29,000 ksi = 200 GPa in
    whatever units the file is in) unless the user names them; the story heights are checked against them.
  * Vertical axis: Z unless the model says Y (rigidDiaphragm perpendicular direction, supports, gravity).
  * Levels: the elevations of the long horizontal members and the rigid-diaphragm nodes, the base at the
    supports. Column lines: the plan points of the long vertical members; the grid is their X and Y lines.
  * Work points: every node within SNAP of a level is put on it, and within SNAP of a column line (or a grid
    line) is put on it -- panel zones, rigid end offsets, gusset zones and spring nodes collapse into their
    joint, and members run work point to work point (as HR models them).
  * Members: vertical segments make one column per story per column line; horizontal segments at a level make
    beams from joint to joint; inclined segments make braces from level to level (through a cambered or
    X-crossing mid-point). The section of a member is that of its longest part.
  * End releases: an element's own -releasey / -releasez, or a member end that reaches its joint only through
    an equalDOF / zeroLength / rigidLink that leaves the major-axis rotation free (a pin). A rotational spring
    with real stiffness (an IMK hinge, a panel-zone spring) is a moment connection: NL models its own hinges.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

from . import shapes as SH

SCHEMA, VERSION = "steltic.building_description", 1

# model units: name -> (force -> kip, length -> inch)
UNITS = {"kip-in": (1.0, 1.0), "kip-ft": (1.0, 12.0), "lb-in": (1e-3, 1.0), "lb-ft": (1e-3, 12.0),
         "kN-m": (0.2248089, 39.370079), "N-m": (2.248089e-4, 39.370079), "N-mm": (2.248089e-4, 0.039370079),
         "kN-mm": (0.2248089, 0.039370079), "kN-cm": (0.2248089, 0.39370079), "tf-m": (2.204623, 39.370079),
         "kgf-cm": (2.204623e-3, 0.39370079)}
E_STEEL_KSI = 29000.0
G_IN_S2 = 386.0886
LONG_FT = 3.0              # a member shorter than this does not define a level or a column line
SNAP_FT = 2.0              # work-point snap (capped at 20 % of the shortest story / bay)
LINE_TYPES = {"elasticbeamcolumn", "forcebeamcolumn", "dispbeamcolumn", "nonlinearbeamcolumn", "beamwithhinges",
              "elastictimoshenkobeam", "mixedbeamcolumn", "gradientinelasticbeamcolumn", "modelasticbeam2d",
              "truss", "corottruss", "trusssection", "corottrusssection"}
TRUSS_TYPES = {"truss", "corottruss", "trusssection", "corottrusssection"}
CONNECTOR_TYPES = {"zerolength", "zerolengthsection", "twonodelink"}
REFUSED_TYPES = {"shellmitc4": "shell", "shelldkgq": "shell", "shellnldkgq": "shell", "shellmitc9": "shell",
                 "asdshellq4": "shell", "stdbrick": "solid", "bbarbrick": "solid", "quad": "plane", "tri31": "plane",
                 "joint2d": "joint", "joint3d": "joint", "elastomericbearingplasticity": "isolator",
                 "flatsliderbearing": "isolator", "singlefpbearing": "isolator", "triplefrictionpendulum": "isolator"}


class IntakeError(ValueError):
    """The file cannot become a building description at all (no model, a 2-D model, units unknown)."""


def _v(a, b):
    return [b[i] - a[i] for i in range(3)]


def _norm(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n > 0 else [0.0, 0.0, 0.0]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _dot(a, b):
    return sum(a[i] * b[i] for i in range(3))


def _cluster(values, tol):
    """Sorted cluster centres of a list of numbers (single-link within tol, centre = median)."""
    vs = sorted(values)
    out, cur = [], []
    for v in vs:
        if cur and v - cur[-1] > tol:
            out.append(cur[len(cur) // 2]); cur = []
        cur.append(v)
    if cur:
        out.append(cur[len(cur) // 2])
    return out


def _nearest(v, centres, tol):
    best = None
    for c in centres:
        d = abs(v - c)
        if d <= tol and (best is None or d < abs(v - best)):
            best = c
    return best


# ---------------------------------------------------------------- units and the vertical axis
def _e_values(m):
    """Steel moduli the model states (model units), most common first."""
    es = []
    for e in (m.get("elements") or {}).values():
        t = e["type"].lower(); a = [x for x in e.get("args") or [] if isinstance(x, (int, float))]
        if t == "elasticbeamcolumn" and len(a) >= 7:
            es.append(a[1])
        elif t == "elastictimoshenkobeam" and a:
            es.append(a[0])
    for mat in (m.get("materials") or {}).values():
        t = str(mat.get("type")).lower(); a = [x for x in mat.get("args") or [] if isinstance(x, (int, float))]
        if t in ("elastic", "elasticpp", "elasticmultilinear") and a:
            es.append(a[0])
        elif t in ("steel01", "steel02", "steel4", "steelmpf", "hardening", "reinforcingsteel", "steel01thermal",
                   "steel02thermal", "uniaxialjohnsoncook", "bilin", "modimkpeakoriented") and len(a) >= 2:
            if t in ("steel01", "steel02", "steel4", "steelmpf", "reinforcingsteel"):
                es.append(a[1])
        elif t in ("isotropic",) and a:
            pass
    for s in (m.get("sections") or {}).values():
        a = [x for x in s.get("args") or [] if isinstance(x, (int, float))]
        if str(s.get("type")).lower() == "elastic" and a:
            es.append(a[0])
    return [e for e in es if e and e > 0]


def detect_units(m, heights_model):
    """-> (name, how). The steel modulus decides; the story heights are the check."""
    es = _e_values(m)
    if not es:
        raise IntakeError("UNITS: the model states no steel modulus E (no elastic or steel material), so its units "
                          "cannot be read -- choose them")
    E = Counter(round(e, 6) for e in es).most_common(1)[0][0]
    hits = []
    for name, (F, L) in UNITS.items():
        ek = E * F / (L * L)
        if 0.85 <= ek / E_STEEL_KSI <= 1.15:
            hits.append((abs(ek / E_STEEL_KSI - 1.0), name, ek))
    if not hits:
        raise IntakeError("UNITS: the steel modulus in the file, E = %g, is 29,000 ksi / 200 GPa in none of the unit "
                          "systems this reader knows -- choose the units" % E)
    hits.sort()
    name = hits[0][1]
    how = "steel E = %g in the file = %.0f ksi in %s" % (E, hits[0][2], name)
    if heights_model:
        h = sorted(heights_model)[len(heights_model) // 2] * UNITS[name][1] / 12.0
        how += "; typical story height %.1f ft" % h
        if not (6.0 <= h <= 40.0):
            how += " (unusual -- check the units)"
    return name, how


def detect_up(m):
    """-> (axis index 2 for Z or 1 for Y, how)."""
    perps = Counter(d.get("perp") for d in m.get("diaphragms") or [])
    if perps:
        p = perps.most_common(1)[0][0]
        if p in (2, 3):
            return p - 1, "rigidDiaphragm perpendicular direction %d" % p
    nodes = {str(k): v for k, v in (m.get("nodes") or {}).items()}
    fixed = [nodes[str(t)] for t in (m.get("fix") or {}) if str(t) in nodes]
    if fixed:
        same = [ax for ax in (2, 1) if max(c[ax] for c in fixed) - min(c[ax] for c in fixed) < 1e-6 and
                min(c[ax] for c in fixed) <= min(c[ax] for c in nodes.values()) + 1e-6]
        if len(same) == 1:
            return same[0], "every support at the lowest %s" % "XYZ"[same[0]]
    grav = Counter()
    for p in (m.get("patterns") or {}).values():
        for node, vals in p.get("loads") or []:
            for ax in (1, 2):
                if len(vals) > ax and vals[ax] < 0:
                    grav[ax] += -vals[ax]
    if grav:
        ax = grav.most_common(1)[0][0]
        return ax, "gravity loads act in -%s" % "XYZ"[ax]
    return 2, "Z (no rigid diaphragm, supports or gravity load says otherwise)"


# ---------------------------------------------------------------- connectors (pins and springs)
def _mat_rot_free(mat, all_k):
    """True when a zeroLength material leaves its direction effectively free (a pin)."""
    if mat is None:
        return True
    t = str(mat.get("type")).lower()
    a = [x for x in mat.get("args") or [] if isinstance(x, (int, float))]
    if t in ("elastic", "elasticpp"):
        k = abs(a[0]) if a else 0.0
        big = max(all_k) if all_k else 0.0
        return k < 1e-6 or (big > 0 and k < 1e-4 * big)
    return False                                    # a nonlinear spring: a moment connection with a hinge


def connectors(m, to_bld):
    """Per original node: list of (other node, released building rotation axes {'X','Y','Z'}, what)."""
    mats = {str(k): v for k, v in (m.get("materials") or {}).items()}
    all_k = []
    for e in (m.get("elements") or {}).values():
        if e["type"].lower() == "zerolength":
            s = [str(x) for x in e.get("args") or []]
            if "-mat" in s:
                for x in e["args"][s.index("-mat") + 1:]:
                    if not isinstance(x, (int, float)):
                        break
                    mt = mats.get(str(int(x)))
                    if mt and str(mt.get("type")).lower() == "elastic" and mt.get("args"):
                        all_k.append(abs(float(mt["args"][0])))
    out = defaultdict(list)
    rot_axes = {4: "x", 5: "y", 6: "z"}

    def released(free_model_rot):
        return {to_bld["axis"][ax] for ax in free_model_rot}

    for c in m.get("equaldofs") or []:
        free = {rot_axes[d] for d in (4, 5, 6) if d not in c["dofs"]}
        out[c["r"]].append((c["c"], released(free), "equalDOF", {}))
        out[c["c"]].append((c["r"], released(free), "equalDOF", {}))
    for c in m.get("rigidlinks") or []:
        free = set() if c["type"].lower() == "beam" else {"x", "y", "z"}
        out[c["r"]].append((c["c"], released(free), "rigidLink " + c["type"], {}))
        out[c["c"]].append((c["r"], released(free), "rigidLink " + c["type"], {}))
    for e in (m.get("elements") or {}).values():
        t = e["type"].lower()
        if t not in CONNECTOR_TYPES or len(e["nodes"]) < 2:
            continue
        a = e.get("args") or []
        s = [str(x) for x in a]
        free = {"x", "y", "z"}
        springs = {}                                         # building rotation axis -> elastic stiffness (model units) or None
        if t == "zerolengthsection":
            free = set()                                    # a section: moment carried
        elif "-mat" in s and "-dir" in s:
            i, j = s.index("-mat") + 1, s.index("-dir") + 1
            mlist, dlist = [], []
            while i < len(a) and isinstance(a[i], (int, float)):
                mlist.append(int(a[i])); i += 1
            while j < len(a) and isinstance(a[j], (int, float)):
                dlist.append(int(a[j])); j += 1
            local = {4: "x", 5: "y", 6: "z"}
            orient = None
            if "-orient" in s:
                k = s.index("-orient") + 1
                v = [float(x) for x in a[k:k + 6] if isinstance(x, (int, float))]
                if len(v) == 6:
                    xl = _norm(v[:3]); yp = v[3:]
                    zl = _norm(_cross(xl, yp)); yl = _cross(zl, xl)
                    orient = {"x": xl, "y": yl, "z": zl}
            for mt, d in zip(mlist, dlist):
                if d in local and not _mat_rot_free(mats.get(str(mt)), all_k):
                    ax = local[d]
                    if orient:
                        vec = orient[ax]
                        ax = "xyz"[max(range(3), key=lambda q: abs(vec[q]))]
                    free.discard(ax)
                    mt_ = mats.get(str(mt)) or {}
                    k = (mt_.get("args") or [None])[0] if str(mt_.get("type")).lower() == "elastic" else None
                    springs[to_bld["axis"][ax]] = k
        out[e["nodes"][0]].append((e["nodes"][1], released(free), e["type"], springs))
        out[e["nodes"][1]].append((e["nodes"][0], released(free), e["type"], springs))
    return out


# ---------------------------------------------------------------- the conversion
def convert(rec, file_name="", sha256="", units=None, up=None):
    """-> (description, report). Raises IntakeError when no description can be made."""
    if rec.get("error") == "NO_TCL":
        raise IntakeError("NO_TCL")
    m = rec.get("model")
    if not m or not m.get("elements"):
        why = (rec.get("stopped") or {}).get("why")
        raise IntakeError("the script built no model%s" % ((" (it stopped: %s)" % why) if why else ""))
    if (m.get("ndm") or 3) != 3:
        raise IntakeError("this is a %d-D model (one frame). The nonlinear suite analyses the whole building in 3-D: "
                          "give the 3-D model (ndm 3, ndf 6)" % (m.get("ndm") or 0))
    notes, unsup, props = [], [], {}
    report = dict(file=file_name, lang=rec.get("lang"), models_in_script=rec.get("models"), stopped=rec.get("stopped"),
                  unknown_commands=rec.get("unknown_commands") or {}, analyze_calls=rec.get("analyze_calls"))
    nodes = {int(k): v for k, v in m["nodes"].items()}
    elements = {int(k): v for k, v in m["elements"].items()}

    # ---- vertical axis and the transformation to the building axes (X, Y plan; Z up)
    if up in ("Z", "Y"):
        upi, up_how = (2 if up == "Z" else 1), "chosen"
    else:
        upi, up_how = detect_up(m)
    if upi == 2:
        to_b = lambda c: (c[0], c[1], c[2])
        axis = {"x": "X", "y": "Y", "z": "Z"}
    else:                                                # Y up: building X = x, Y = -z, Z = y (right-handed)
        to_b = lambda c: (c[0], -c[2] + 0.0, c[1])
        axis = {"x": "X", "y": "Z", "z": "Y"}
    B = {t: to_b(c + [0.0] * (3 - len(c))) for t, c in nodes.items()}
    report["up"] = dict(axis="XYZ"[upi], how=up_how + ("; plan X = model X, plan Y = -(model Z)" if upi == 1 else ""))

    # ---- line elements and their geometry (model units for now)
    lines, refused_types, connector_n = {}, Counter(), 0
    for tag, e in elements.items():
        t = e["type"].lower()
        if t in LINE_TYPES and len(e["nodes"]) >= 2 and all(n in B for n in e["nodes"][:2]):
            lines[tag] = e
        elif t in CONNECTOR_TYPES:
            connector_n += 1
        else:
            refused_types[REFUSED_TYPES.get(t, e["type"])] += 1
    for kind, n in refused_types.items():
        unsup.append("%d %s element(s): version 1 models steel frame members only (columns, beams, braces)%s"
                     % (n, kind, " -- walls, slabs and isolators are not carried" if kind in ("shell", "solid", "isolator", "plane") else ""))
    if not lines:
        raise IntakeError("the model has no beam-column or truss elements to read members from")

    def geom(e):
        a, b = B[e["nodes"][0]], B[e["nodes"][1]]
        d = _v(a, b)
        L = math.sqrt(_dot(d, d))
        return a, b, d, L

    # ---- units (story heights from the vertical members, for the check)
    vert_h = []
    for e in lines.values():
        a, b, d, L = geom(e)
        if L > 0 and abs(d[2]) / L > 0.995:
            vert_h.append(L)
    if units in UNITS:
        uname, u_how = units, "chosen"
        try:
            auto, _h = detect_units(m, vert_h)
        except IntakeError:
            auto = None
        if auto and auto != uname and UNITS[auto] != UNITS[uname]:
            notes.append("units %s were chosen, but the steel modulus in the file reads as %s -- check the levels and "
                         "sections below" % (uname, auto))
            u_how += " (the steel modulus reads as %s)" % auto
    else:
        uname, u_how = detect_units(m, vert_h)
    F, Lin = UNITS[uname]
    ft = Lin / 12.0                                      # model length -> ft
    report["units"] = dict(name=uname, how=u_how)

    # ---- levels and column lines, from chains of collinear elements (a member may be subdivided)
    conn = connectors(m, {"axis": axis})
    member_nodes = {n for e in lines.values() for n in e["nodes"][:2]}
    fixed_nodes = [int(t) for t in (m.get("fix") or {}) if int(t) in B]
    near_members = [t for t in fixed_nodes if t in member_nodes or any(c[0] in member_nodes for c in conn.get(t, []))]
    base_z = min([B[t][2] for t in near_members] or [B[t][2] for t in fixed_nodes] or [min(c[2] for c in B.values())])
    h_ext = _chains(lines, geom, "h")
    long_h = [z for (z, extent) in h_ext if extent * ft >= LONG_FT]
    dia_z = [B[n][2] for dg in m.get("diaphragms") or [] if dg.get("perp") == upi + 1 for n in dg["slaves"] if n in B]
    zs = _cluster(long_h + dia_z + [base_z], 0.25 / ft)
    zs = sorted(set([base_z] + [z for z in zs if z > base_z + 0.25 / ft]))
    if dia_z:
        # the model's rigid diaphragms say where its floors are: framing at another height (a crane runway, a
        # mezzanine strut) is not a level -- except above the highest diaphragm, a roof modelled without one
        dz = _cluster(dia_z, 0.25 / ft)
        off = [z for z in zs[1:] if not any(abs(z - q) <= 0.25 / ft for q in dz) and z < max(dz)]
        if off:
            notes.append("framing at %s ft has no rigid diaphragm while the floors have one: it is not a level, and members "
                         "ending there are listed as not supported" % ", ".join("%.2f" % (z * ft) for z in off))
            zs = [z for z in zs if z not in off]
    if len(zs) < 2:
        raise IntakeError("no floor levels found: the model has no horizontal members longer than %.0f ft above its base" % LONG_FT)
    story_min = min(b - a for a, b in zip(zs, zs[1:]))
    col_pts = []
    for (xy, extent) in _chains(lines, geom, "v"):
        if extent >= 0.5 * story_min and not any(abs(xy[0] - p[0]) < 0.25 / ft and abs(xy[1] - p[1]) < 0.25 / ft for p in col_pts):
            col_pts.append(xy)
    gx = _cluster([p[0] for p in col_pts], 0.25 / ft)
    gy = _cluster([p[1] for p in col_pts], 0.25 / ft)
    if len(gx) < 2 or len(gy) < 2:
        raise IntakeError("the columns stand on %d X line(s) and %d Y line(s): a building needs at least two of each "
                          "(is this a single frame? give the whole building)" % (len(gx), len(gy)))
    bay_min = min([b - a for a, b in zip(gx, gx[1:])] + [b - a for a, b in zip(gy, gy[1:])])
    snap = min(SNAP_FT / ft, 0.2 * story_min, 0.2 * bay_min)
    report["snap_ft"] = round(snap * ft, 3)

    # ---- work points
    def wp(n):
        x, y, z = B[n]
        zl = _nearest(z, zs, snap)
        cp = None
        for p in col_pts:
            if math.hypot(x - p[0], y - p[1]) <= snap and (cp is None or math.hypot(x - p[0], y - p[1]) < math.hypot(x - cp[0], y - cp[1])):
                cp = p
        if cp:
            x, y = cp
        else:
            sx, sy = _nearest(x, gx, snap), _nearest(y, gy, snap)
            x = sx if sx is not None else x
            y = sy if sy is not None else y
        return (round(x, 6), round(y, 6), round(zl if zl is not None else z, 6))

    W = {n: wp(n) for n in member_nodes}
    level_of = {round(z, 6): k for k, z in enumerate(zs)}
    lvname = lambda k: "Base" if k == 0 else "L%d" % k
    xlab = {round(x, 6): "X%d" % (i + 1) for i, x in enumerate(gx)}
    ylab = {round(y, 6): "Y%d" % (i + 1) for i, y in enumerate(gy)}

    def plan(p):
        return {"x": xlab.get(round(p[0], 6), round(p[0] * ft, 6)), "y": ylab.get(round(p[1], 6), round(p[1] * ft, 6))}

    # ---- element end releases
    deg = Counter(n for e in lines.values() for n in e["nodes"][:2])
    transfs = {str(k): v for k, v in (m.get("transfs") or {}).items()}

    def local_axes(e):
        a, b = nodes[e["nodes"][0]], nodes[e["nodes"][1]]
        a = a + [0.0] * (3 - len(a)); b = b + [0.0] * (3 - len(b))
        x = _norm(_v(a, b))
        tr = transfs.get(str(_transf_tag(m, e)))
        vec = (tr or {}).get("vecxz")
        if not vec:
            return x, None, None
        y = _norm(_cross(vec, x))
        z = _cross(x, y)
        return x, y, z

    def to_bvec(v):
        return to_b(list(v))

    def release_flags(tag, e, props):
        """{end index: set of released building rotation axes} from the element's own -releasey / -releasez."""
        s = [str(x) for x in e.get("args") or []]
        out = {0: set(), 1: set()}
        x, y, z = local_axes(e)
        for flag, ax in (("-releasey", y), ("-releasez", z)):
            if flag in s and ax is not None:
                code = int(float(e["args"][s.index(flag) + 1]))
                bv = to_bvec(ax)
                g = "XYZ"[max(range(3), key=lambda q: abs(bv[q]))]
                if code in (1, 3):
                    out[0].add(g)
                if code in (2, 3):
                    out[1].add(g)
        return out

    def _vertical(e):
        P, Q = W[e["nodes"][0]], W[e["nodes"][1]]
        return abs(P[0] - Q[0]) < 1e-6 and abs(P[1] - Q[1]) < 1e-6

    off_vertical = {n for e in lines.values() if not _vertical(e) for n in e["nodes"][:2]}

    def end_release(tag, e, end, column=False):
        """Building rotation axes left free at this end by the element itself or by a pin connector. A column end ignores
        the connectors to beam / brace ends: a hinge between a column and a beam belongs to the beam (a roof column
        top whose beams are all pinned is not a pinned column)."""
        n = e["nodes"][end]
        rel = set(e.get("_rel", {}).get(end, set()))
        cs = [c for c in conn.get(n) or [] if not (column and c[0] in off_vertical)]
        if deg[n] <= 1 and cs:
            free = None
            for other, r, what, _sp in cs:
                free = set(r) if free is None else (free & set(r))
            rel |= (free or set())
        return rel

    # ---- sections
    sec_props = {}

    def section_of(tag, e):
        p = SH.element_props(m, e, Lin)
        if p is None:
            return None, None
        if "I_major" in p:
            nm = "OpenSees A=%s Ix=%s Iy=%s%s in" % (_g(p["A"]), _g(p["I_major"]), _g(p["I_minor"]),
                                                    (" J=%s" % _g(p["J"])) if p.get("J") else "")
        else:
            nm = "OpenSees truss A=%s in2" % _g(p["A"])
        sec_props.setdefault(nm, p)
        return nm, p

    seg = []                                             # (kind, P, Q, tag, element, section, props, length_ft)
    collapsed = 0
    kinds = Counter()
    for tag, e in lines.items():
        P, Q = W[e["nodes"][0]], W[e["nodes"][1]]
        if P == Q:
            collapsed += 1
            continue
        nm, p = section_of(tag, e)
        e["_rel"] = release_flags(tag, e, p)
        d = _v(P, Q)
        L = math.sqrt(_dot(d, d))
        t = e["type"].lower()
        if abs(d[2]) < 1e-6:
            kind = "tie" if t in TRUSS_TYPES else "beam"
        elif abs(d[0]) < 1e-6 and abs(d[1]) < 1e-6:
            kind = "post" if t in TRUSS_TYPES else "column"
        else:
            kind = "brace"
            if t not in TRUSS_TYPES and abs(d[2]) < math.tan(math.radians(20.0)) * math.hypot(d[0], d[1]):
                unsup.append("sloped member (OpenSees element %d, %.0f deg): version 1 models level beams and braces between "
                             "levels only" % (tag, math.degrees(math.atan2(abs(d[2]), math.hypot(d[0], d[1])))))
                continue
        kinds[kind] += 1
        seg.append(dict(kind=kind, P=P, Q=Q, tag=tag, e=e, sec=nm, props=p, L=L * ft, truss=t in TRUSS_TYPES))
    if kinds["tie"]:
        unsup.append("%d horizontal truss element(s) at a level (ties / diaphragm bracing): version 1 models them only as "
                     "braces between levels" % kinds["tie"])
    if kinds["post"]:
        unsup.append("%d vertical truss element(s) (posts / hangers): version 1 models columns as beam-columns" % kinds["post"])
    report["elements"] = dict(lines=len(lines), connectors=connector_n, collapsed_into_joints=collapsed,
                              by_type=dict(Counter(e["type"] for e in elements.values())))

    # ---- columns: level to level along each column line, broken where something frames in (a split-level column that
    # passes a level without a floor there stays one member, as it is in the model)
    joint_at = defaultdict(set)                          # level index -> plan points where beams / braces / a diaphragm connect
    for s_ in seg:
        if s_["kind"] in ("beam", "brace", "tie"):
            for q in (s_["P"], s_["Q"]):
                k = level_of.get(round(q[2], 6))
                if k is not None:
                    joint_at[k].add((q[0], q[1]))
    for dg in m.get("diaphragms") or []:
        for n in dg["slaves"]:
            if n in W and round(W[n][2], 6) in level_of:
                joint_at[level_of[round(W[n][2], 6)]].add((W[n][0], W[n][1]))
    by_line = defaultdict(list)
    for s_ in seg:
        if s_["kind"] == "column":
            by_line[(s_["P"][0], s_["P"][1])].append(s_)
    columns, col_at = [], defaultdict(set)              # col_at[level index] = plan points with a column joint there
    nc = 0
    for xy, ss in sorted(by_line.items()):
        ss = [dict(s_, z0=min(s_["P"][2], s_["Q"][2]), z1=max(s_["P"][2], s_["Q"][2]),
                   lo=0 if s_["P"][2] < s_["Q"][2] else 1) for s_ in ss]
        # the levels this column line reaches, and where it is a joint
        ends = {round(z, 6) for s_ in ss for z in (s_["z0"], s_["z1"])}
        stops = []
        for k, z in enumerate(zs):
            z = round(z, 6)
            inside = any(s_["z0"] - 1e-9 <= z <= s_["z1"] + 1e-9 for s_ in ss)
            if not inside:
                continue
            top_or_bottom = not any(s_["z0"] < z - 1e-9 for s_ in ss) or not any(s_["z1"] > z + 1e-9 for s_ in ss)
            if z in ends and (xy in joint_at[k] or top_or_bottom or k == 0):
                stops.append(k)
        for k0, k1 in zip(stops, stops[1:]):
            za, zb = round(zs[k0], 6), round(zs[k1], 6)
            parts = [s_ for s_ in ss if s_["z0"] < zb - 1e-9 and s_["z1"] > za + 1e-9]
            if not parts:
                continue
            cover = sum(min(s_["z1"], zb) - max(s_["z0"], za) for s_ in parts)
            if cover < (zb - za) * 0.999:
                unsup.append("column at %s: covers only %.0f %% of %s-%s -- version 1 models columns level to level"
                             % (_pt(plan(xy)), 100.0 * cover / (zb - za), lvname(k0), lvname(k1)))
                continue
            share = Counter()
            for s_ in parts:
                share[s_["sec"]] += min(s_["z1"], zb) - max(s_["z0"], za)
            main_sec, main_len = share.most_common(1)[0]
            if len(share) > 1 and sorted(share.values())[-2] > 0.15 * (zb - za):
                unsup.append("column at %s, %s-%s: the section changes within the story (%s) -- version 1 takes one section "
                             "per column per story" % (_pt(plan(xy)), lvname(k0), lvname(k1), "; ".join(sorted(share))))
            dom = max((s_ for s_ in parts if s_["sec"] == main_sec), key=lambda s_: s_["L"])
            nc += 1
            col = dict(id="C%d@%s" % (nc, lvname(k1)), x=plan(xy)["x"], y=plan(xy)["y"], base_level=lvname(k0),
                       top_level=lvname(k1), section=main_sec, _where="OpenSees element %d" % dom["tag"])
            sa = _strong_axis(dom, local_axes, to_bvec)
            if sa:
                col["strong_axis"] = sa
            elif dom["props"] and dom["props"].get("I_major") and dom["props"]["I_major"] > 1.05 * dom["props"]["I_minor"]:
                unsup.append("column at %s, %s-%s: its strong axis is skewed to the grid -- version 1 orients columns along X or Y"
                             % (_pt(plan(xy)), lvname(k0), lvname(k1)))
            bot = next((s_ for s_ in parts if abs(s_["z0"] - za) < 1e-6), None)
            top = next((s_ for s_ in parts if abs(s_["z1"] - zb) < 1e-6), None)
            rb = end_release(bot["tag"], bot["e"], bot["lo"], column=True) if bot else set()
            rt = end_release(top["tag"], top["e"], 1 - top["lo"], column=True) if top else set()
            pb, pt = {"X", "Y"} <= rb, {"X", "Y"} <= rt
            if pb or pt:
                col["release"] = "both" if pb and pt else ("base" if pb else "top")
            if (rb & {"X", "Y"} and not pb) or (rt & {"X", "Y"} and not pt):
                unsup.append("column at %s, %s-%s: an end is pinned about one axis only -- version 1 releases column ends "
                             "about both axes or neither" % (_pt(plan(xy)), lvname(k0), lvname(k1)))
            columns.append(col)
            col_at[k0].add(xy); col_at[k1].add(xy)

    col_base, col_top = defaultdict(set), defaultdict(set)
    lvi = {lvname(k): k for k in range(len(zs))}
    for c in columns:
        col_base[lvi[c["base_level"]]].add(_xy_of(c, xlab, ylab, ft))
        col_top[lvi[c["top_level"]]].add(_xy_of(c, xlab, ylab, ft))

    # ---- braces: inclined segments chained through mid-story points (camber nodes, X crossings)
    br_segs = [s_ for s_ in seg if s_["kind"] == "brace"]
    at_pt = defaultdict(list)
    for i, s_ in enumerate(br_segs):
        at_pt[s_["P"]].append(i); at_pt[s_["Q"]].append(i)
    used, braces, brace_ends = set(), [], defaultdict(set)
    nb = 0

    def other_end(s_, p):
        return s_["Q"] if s_["P"] == p else s_["P"]

    def straight_on(i, p):
        """The brace segment continuing segment i straight through mid-story point p, if any."""
        if round(p[2], 6) in level_of:
            return None
        s_ = br_segs[i]
        d0 = _norm(_v(other_end(s_, p), p))
        best = None
        for j in at_pt[p]:
            if j == i or j in used:
                continue
            d1 = _norm(_v(p, other_end(br_segs[j], p)))
            c = _dot(d0, d1)
            if c > math.cos(math.radians(4.0)) and (best is None or c > best[0]):
                best = (c, j)
        return best[1] if best else None

    for i, s_ in enumerate(br_segs):
        if i in used:
            continue
        chain, ends = [i], []
        used.add(i)
        for start in (s_["P"], s_["Q"]):
            cur, p = i, start
            while True:
                j = straight_on(cur, p)
                if j is None:
                    ends.append(p)
                    break
                used.add(j); chain.append(j)
                p = other_end(br_segs[j], p); cur = j
        a, b = sorted(ends, key=lambda q: q[2])
        share = Counter()
        for j in chain:
            share[br_segs[j]["sec"]] += br_segs[j]["L"]
        main_sec = share.most_common(1)[0][0]
        dom = max((br_segs[j] for j in chain if br_segs[j]["sec"] == main_sec), key=lambda s_: s_["L"])
        la, lb = level_of.get(round(a[2], 6)), level_of.get(round(b[2], 6))
        if not dom["truss"] and lb is not None and (b[0], b[1]) in col_base.get(lb, set()) and \
                (b[0], b[1]) not in col_top.get(lb, set()):
            unsup.append("inclined column (OpenSees element %d, %s): a column stands on its top end -- version 1 models "
                         "vertical columns only" % (dom["tag"], _pt(plan(b))))
            continue
        if la is None or lb is None:
            unsup.append("brace (OpenSees element %d): an end at %.2f ft is between levels -- version 1 places brace ends on "
                         "levels" % (dom["tag"], (a[2] if la is None else b[2]) * ft))
            continue
        if la == lb:
            unsup.append("brace (OpenSees element %d) lies within one level -- not a brace between levels" % dom["tag"])
            continue
        nb += 1
        pa, pb_ = plan(a), plan(b)
        braces.append(dict(id="D%d@%s" % (nb, lvname(lb)), i=dict(pa, level=lvname(la)), j=dict(pb_, level=lvname(lb)),
                           section=main_sec, _where="OpenSees element %d%s" % (dom["tag"], (" + %d" % (len(chain) - 1)) if len(chain) > 1 else "")))
        brace_ends[la].add((a[0], a[1])); brace_ends[lb].add((b[0], b[1]))

    # ---- beams: horizontal segments at a level, joint to joint
    bm_by_level = defaultdict(list)
    for s_ in seg:
        if s_["kind"] == "beam":
            k = level_of.get(round(s_["P"][2], 6))
            if k is None:
                unsup.append("horizontal member (OpenSees element %d) at %.2f ft is not on a level -- version 1 models beams "
                             "at levels" % (s_["tag"], s_["P"][2] * ft))
                continue
            bm_by_level[k].append(s_)
    beams = []
    nbm = 0
    for k, ss in sorted(bm_by_level.items()):
        lines_ = defaultdict(list)
        for s_ in ss:
            P, Q = s_["P"], s_["Q"]
            if abs(P[1] - Q[1]) < 1e-6:
                lines_[("X", P[1])].append(s_)
            elif abs(P[0] - Q[0]) < 1e-6:
                lines_[("Y", P[0])].append(s_)
            else:
                lines_[("D", s_["tag"])].append(s_)
        ends_here = Counter()
        for (dr, c), ls in lines_.items():
            for s_ in ls:
                ends_here[(s_["P"][0], s_["P"][1])] += 1; ends_here[(s_["Q"][0], s_["Q"][1])] += 1
        for (dr, c), ls in sorted(lines_.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
            if dr == "D":
                for s_ in ls:
                    nbm += 1
                    beams.append(_beam(nbm, k, s_["P"], s_["Q"], [s_], plan, lvname, end_release))
                continue
            ax = 0 if dr == "X" else 1
            ls = sorted(ls, key=lambda s_: min(s_["P"][ax], s_["Q"][ax]))
            # breakpoints: columns, beams of other lines, brace ends, released segment ends, gaps
            def breaks_at(p, s1, s2):
                xy = (p[0], p[1])
                if xy in col_at[k] or xy in brace_ends[k]:
                    return True
                if ends_here[xy] > 2:
                    return True                          # another beam frames in here
                if s1["sec"] != s2["sec"] and min(s1["L"], s2["L"]) > 0.25 * (s1["L"] + s2["L"]):
                    return True
                return False
            chain = [ls[0]]
            for s_ in ls[1:]:
                prev = chain[-1]
                pe = max([prev["P"], prev["Q"]], key=lambda q: q[ax])
                ps = min([s_["P"], s_["Q"]], key=lambda q: q[ax])
                if pe == ps and not breaks_at(pe, prev, s_):
                    chain.append(s_)
                    continue
                nbm += 1
                beams.append(_beam(nbm, k, None, None, chain, plan, lvname, end_release, ax=ax))
                chain = [s_]
            nbm += 1
            beams.append(_beam(nbm, k, None, None, chain, plan, lvname, end_release, ax=ax))
    _mark_links(beams, braces, notes, col_at, lvname)

    # ---- supports: the base work points, from the fix commands on the base nodes (directly, or through a connector)
    fix = {int(t): f for t, f in (m.get("fix") or {}).items()}
    K_RIGID = 1e8                                          # kip-in/rad: a base spring stiffer than this is a fixed base
    base_pts = defaultdict(set)
    for n in member_nodes:
        if W[n][2] != round(zs[0], 6):
            continue
        xy = (W[n][0], W[n][1])
        cand = [(n, None)] + [(c[0], c) for c in conn.get(n, [])]
        for c_, via in cand:
            f = fix.get(c_)
            if not f:
                continue
            f6 = (f + [0] * 6)[:6]
            if all(f6[:3]) and all(f6[3:]):
                kind = "fixed"
            elif all(f6[:3]) and not any(f6[3:]):
                kind = "pinned"
            else:
                base_pts[xy].add("partial restraint %s" % "".join(str(x) for x in f6))
                continue
            if via is not None and kind == "fixed":
                _o, rel, what, springs = via
                if {"X", "Y"} <= rel:
                    kind = "pinned"
                elif rel & {"X", "Y"}:
                    kind = "pinned about one axis (%s)" % what
                else:
                    soft = [ax for ax in ("X", "Y") if ax in springs and (springs[ax] is None or abs(springs[ax]) * F * Lin < K_RIGID)]
                    if soft:
                        kind = "rotational spring (%s about %s)" % (what, "/".join(soft))
            base_pts[xy].add(kind)
    sup_at = {}
    for xy, ks in base_pts.items():
        if ks <= {"fixed"} or ks == {"fixed", "pinned"}:
            sup_at[xy] = "fixed"
        elif ks <= {"pinned"}:
            sup_at[xy] = "pinned"
        else:
            unsup.append("support at %s: %s -- version 1 models fixed or pinned bases only (no springs, no partial restraint)"
                         % (_pt(plan(xy)), ", ".join(sorted(k_ for k_ in ks if k_ not in ("fixed", "pinned")))))
    base_cols = [c for c in columns if c["base_level"] == "Base"]
    supports = {}
    kinds_set = set(sup_at.values())
    if len(kinds_set) == 1:
        supports = {"default": kinds_set.pop()}
    elif kinds_set:
        supports = {"default": None, "at": [dict(plan(xy), fixity=v) for xy, v in sorted(sup_at.items())]}
    free = [c for c in base_cols if (c["x"], c["y"]) not in {(plan(xy)["x"], plan(xy)["y"]) for xy in sup_at}]
    if free and sup_at:
        unsup.append("%d base column(s) have no support at the base (e.g. at %s)" % (len(free), _pt(free[0])))

    # ---- diaphragms
    slaved = defaultdict(set)
    rd_levels = set()
    for dg in m.get("diaphragms") or []:
        if dg.get("perp") != upi + 1:
            unsup.append("rigidDiaphragm with perpendicular direction %d is not horizontal (vertical axis %s)" % (dg.get("perp"), "XYZ"[upi]))
            continue
        for n in dg["slaves"] + [dg["master"]]:
            if n in B:
                z = _nearest(B[n][2], zs, snap)
                if z is not None:
                    k = level_of[round(z, 6)]
                    rd_levels.add(k)
                    if n in W:
                        slaved[k].add((W[n][0], W[n][1]))
    floor_levels = sorted({k for k in bm_by_level} | {int(k_) for k_ in col_at if k_ > 0})
    diaphragm = None
    if rd_levels:
        diaphragm = "rigid"
        without = [lvname(k) for k in floor_levels if k > 0 and k not in rd_levels]
        if without:
            notes.append("no rigidDiaphragm at %s: those levels are taken as rigid too (version 1 takes one diaphragm type) "
                         "-- if they are not, say so in the questions" % ", ".join(without))
    else:
        notes.append("the model has no rigidDiaphragm: the diaphragm type is asked")
    excl = []
    for k in sorted(rd_levels):
        pts = set(col_at.get(k, set())) | {(b_["_P"][0], b_["_P"][1]) for b_ in beams if b_["_k"] == k} | \
              {(b_["_Q"][0], b_["_Q"][1]) for b_ in beams if b_["_k"] == k}
        if len(slaved[k] & pts) >= 0.5 * len(pts):            # the diaphragm names the joints: the others are out of it
            excl += [dict(plan(xy), level=lvname(k)) for xy in sorted(pts - slaved[k])]

    # ---- masses -> seismic weight per level; floor areas; the D proposal
    g_model = G_IN_S2 / Lin
    W_lvl = defaultdict(float)
    mass_n = 0
    for t, mv in (m.get("mass") or {}).items():
        t = int(t)
        if t not in B or not mv:
            continue
        mh = max([abs(x) for x in mv[:2]] + [0.0]) if upi == 2 else max(abs(mv[0]), abs(mv[2]) if len(mv) > 2 else 0.0)
        if mh <= 0:
            continue
        z = B[t][2]
        k = min(range(len(zs)), key=lambda q: abs(zs[q] - z))
        W_lvl[k] += mh * g_model * F
        mass_n += 1
    for tag, e in lines.items():                           # element mass per length (-mass)
        s = [str(x) for x in e.get("args") or []]
        if "-mass" in s:
            try:
                mpl = float(e["args"][s.index("-mass") + 1])
            except (ValueError, IndexError):
                continue
            a, b, d, L = geom(e)
            for zz in (a[2], b[2]):
                k = min(range(len(zs)), key=lambda q: abs(zs[q] - zz))
                W_lvl[k] += 0.5 * mpl * L * g_model * F
    area = _floor_areas(zs, gx, gy, col_at, beams, ft)
    Wtot = sum(W_lvl.values())
    if Wtot > 0:
        report["masses"] = {lvname(k): round(w, 1) for k, w in sorted(W_lvl.items())}
        user_results = {"W_kip": round(Wtot, 1)}
        Dp, why = {}, []
        for k in range(1, len(zs)):
            A = area.get(k)
            if A and W_lvl.get(k):
                Dp[lvname(k)] = round(1000.0 * W_lvl[k] / A, 1)
                why.append("%s: %.0f kip over %.0f ft2" % (lvname(k), W_lvl[k], A))
        if Dp:
            props["loads/D_psf"] = dict(value=Dp, reason="derived from the model's nodal masses (seismic weight = mass x g) "
                                        "over each level's framed floor area -- the masses usually carry the dead load and any "
                                        "partition / storage allowance as well; check before accepting: " + "; ".join(why))
        if W_lvl.get(0):
            notes.append("%.0f kip of mass sits at the base level (not part of the seismic weight above the base)" % W_lvl[0])
    else:
        user_results = None
        notes.append("the model has no nodal masses: the dead load per level is asked")
    report["floor_area_ft2"] = {lvname(k): round(a_, 1) for k, a_ in sorted(area.items())}

    # ---- gravity loads, for information (OpenSees does not say which part is dead and which live)
    grav = _gravity(m, B, zs, upi, lines, local_axes, to_bvec, F, ft)
    for pat, per in grav.items():
        per = {k: w for k, w in per.items() if w >= 0.5}
        if not per:
            continue
        txt = ", ".join("%s %.0f kip%s" % (lvname(k), w, (" (%.0f psf)" % (1000.0 * w / area[k])) if area.get(k) else "")
                        for k, w in sorted(per.items()))
        notes.append("load pattern %s applies gravity %s -- OpenSees does not separate dead from live: answer D and L "
                     "with the values your design used" % (pat, txt))

    # ---- nonlinear modelling in the script: read as geometry, not carried over
    mats_ = {str(k): v for k, v in (m.get("materials") or {}).items()}
    nl_springs, nl_mats, nl_elems = 0, Counter(), Counter()
    for e in (m.get("elements") or {}).values():
        t = e["type"].lower()
        if t == "zerolength":
            s = [str(x) for x in e.get("args") or []]
            if "-mat" in s:
                hit = False
                for x in e["args"][s.index("-mat") + 1:]:
                    if not isinstance(x, (int, float)):
                        break
                    mt = mats_.get(str(int(x))) or {}
                    if str(mt.get("type")).lower() not in ("elastic", ""):
                        nl_mats[str(mt.get("type"))] += 1
                        hit = True
                nl_springs += hit
        elif t in ("forcebeamcolumn", "dispbeamcolumn", "nonlinearbeamcolumn", "beamwithhinges", "mvlem"):
            nl_elems[e["type"]] += 1
    if nl_springs or nl_elems:
        what = []
        if nl_springs:
            what.append("%d zeroLength spring%s with %s" % (nl_springs, "s" if nl_springs > 1 else "",
                                                             ", ".join(sorted(nl_mats))))
        if nl_elems:
            what.append(", ".join("%d %s" % (n_, t_) for t_, n_ in sorted(nl_elems.items())))
        notes.append("the script models nonlinearity (%s): those are read as members and moment connections only -- the "
                     "nonlinear suite builds its own hinges and fibre sections from the sections and AISC 342, and does "
                     "not use the script's spring or material parameters" % "; ".join(what))

    # ---- steel grade from the steel materials (a proposal)
    fys = Counter()
    for mat in (m.get("materials") or {}).values():
        t = str(mat.get("type")).lower(); a = [x for x in mat.get("args") or [] if isinstance(x, (int, float))]
        if t in ("steel01", "steel02", "steel4", "steelmpf", "reinforcingsteel", "elasticpp") and a:
            fy = a[0] * F / (Lin * Lin) if t != "elasticpp" else (a[0] * a[1] * F / (Lin * Lin) if len(a) > 1 else None)
            if fy:
                fys[round(fy, 1)] += 1
    if fys:
        fy = fys.most_common(1)[0][0]
        grade = {36.0: "A36", 50.0: "A992", 55.0: "A992"}.get(round(fy))
        notes.append("steel materials in the model: Fy = %s ksi%s" % (", ".join("%g" % f for f in sorted(fys)),
                     " (55 ksi is Ry Fy = 1.1 x 50 of A992: the expected strength)" if 55.0 in [round(f) for f in fys] else ""))
        if grade and grade != "A992":
            props["materials/W"] = dict(value=grade, reason="the model's steel material yields at Fy = %g ksi" % fy)

    # ---- the description
    d = {"schema": SCHEMA, "version": VERSION, "source": {"kind": "opensees", "file": file_name, "sha256": sha256},
         "project": {"name": re.sub(r"\.(py|tcl)$", "", file_name or "OpenSees model", flags=re.I)},
         "seismic": {}, "grid": {"x": [{"label": xlab[round(x, 6)], "coord_ft": round(x * ft, 6)} for x in gx],
                                 "y": [{"label": ylab[round(y, 6)], "coord_ft": round(y * ft, 6)} for y in gy]},
         "levels": [{"name": lvname(k), "elev_ft": round(z * ft, 6)} for k, z in enumerate(zs)],
         "columns": columns, "beams": [{k_: v for k_, v in b_.items() if not k_.startswith("_")} for b_ in beams],
         "braces": braces, "supports": supports, "diaphragm": diaphragm, "loads": {}, "materials": {}, "details": {},
         "wind": None, "user_results": user_results, "_provenance": {}}
    for p_ in ("grid", "levels", "columns", "beams", "braces"):
        d["_provenance"][p_] = {"source": "file", "note": "OpenSees model (%s)" % file_name}
    if supports:
        d["_provenance"]["supports"] = {"source": "file", "note": "fix commands"}
    if diaphragm:
        d["_provenance"]["diaphragm"] = {"source": "file", "note": "rigidDiaphragm"}
    if user_results:
        d["_provenance"]["user_results"] = {"source": "file", "note": "W: the model's horizontal nodal masses x g"}
    if excl:
        d["diaphragm_exclude"] = excl
        d["_provenance"]["diaphragm_exclude"] = {"source": "file", "note": "joints not in their level's rigidDiaphragm"}

    # section proposals: the AISC shape each set of properties matches
    used_secs = Counter(mm["section"] for mm in columns + d["beams"] + braces if mm.get("section"))
    sec_rows = []
    for nm, n_ in used_secs.most_common():
        p = sec_props.get(nm) or {}
        lab, err, what, second = SH.match(p)
        row = dict(name=nm, members=n_, source=p.get("src"), A_in2=_g(p.get("A")), Ix_in4=_g(p.get("I_major")),
                   Iy_in4=_g(p.get("I_minor")), J_in4=_g(p.get("J")), proposal=lab,
                   error_pct=round(100 * err, 2) if err is not None else None, compared=what)
        if lab:
            props["section_map/" + _map_key(nm)] = dict(
                value=lab, reason="the member properties (%s) match %s within %.1f %%%s" % (
                    what, lab, 100 * err, ("; the next closest, %s, differs by %.1f %%" % (second[1], 100 * second[0])) if second else ""))
        elif p.get("I_major"):
            row["closest"] = second[1] if second else None
        sec_rows.append(row)
    report["sections"] = sec_rows
    report["members"] = dict(columns=len(columns), beams=len(beams), braces=len(braces), levels=len(zs), grid=[len(gx), len(gy)])
    d["_proposals"] = props
    d["_intake"] = dict(units=[uname, "derived" if units not in UNITS else "chosen"], up="XYZ"[upi], notes=_dedupe(notes),
                        floor_area_ft2=report["floor_area_ft2"], opensees=dict(units=report["units"], up=report["up"]))
    if unsup:
        d["_unsupported"] = _dedupe(unsup)
    stopped = rec.get("stopped")
    if stopped and stopped.get("kind") == "error":
        d["_intake"]["notes"].insert(0, "the script stopped at %s (%s) after building the model: the model as built up to there "
                                        "is read" % (stopped.get("where") or "its end", stopped.get("why", "")[:200]))
    if rec.get("models", 1) > 1:
        d["_intake"]["notes"].append("the script built %d models (it wipes and rebuilds): the one with the most elements is read"
                                     % rec["models"])
    if rec.get("unknown_commands"):
        d["_intake"]["notes"].append("commands this reader does not know were skipped: %s" % ", ".join(
            "%s (%d)" % kv for kv in sorted(rec["unknown_commands"].items())[:12]))
    return d, report


# ---------------------------------------------------------------- helpers
def _g(v):
    if v is None:
        return None
    if v == 0:
        return "0"
    return "%.4g" % v


def _map_key(raw):
    from urllib.parse import quote
    return quote(str(raw), safe="")


def _pt(p):
    return "%s/%s" % (p.get("x"), p.get("y"))


def _dedupe(lst, cap=60):
    out, seen = [], set()
    for x in lst:
        if x not in seen:
            seen.add(x); out.append(x)
    if len(out) > cap:
        out = out[:cap] + ["... and %d more" % (len(out) - cap)]
    return out


def _xy_of(c, xlab, ylab, ft):
    """The model-unit plan point of a description point (grid label or feet)."""
    inv_x = {v: k for k, v in xlab.items()}
    inv_y = {v: k for k, v in ylab.items()}
    x = inv_x[c["x"]] if c["x"] in inv_x else round(float(c["x"]) / ft, 6)
    y = inv_y[c["y"]] if c["y"] in inv_y else round(float(c["y"]) / ft, 6)
    return (x, y)


def _transf_tag(m, e):
    """The geomTransf tag of a beam-column, by element type and syntax (the numbers before the first -option)."""
    a = e.get("args") or []
    head = []
    for x in a:
        if isinstance(x, str) and x.startswith("-"):
            break
        head.append(x)
    num = [x for x in head if isinstance(x, (int, float))]
    t = e["type"].lower()
    secs = {str(k) for k in (m.get("sections") or {})}
    integ = {str(k) for k in (m.get("integrations") or {})}
    try:
        if t == "elasticbeamcolumn":
            return int(num[6]) if len(num) >= 7 else int(num[1])          # A E G J Iy Iz transf | secTag transf
        if t == "elastictimoshenkobeam":
            return int(num[8]) if len(num) >= 9 else int(num[-1])
        if t in ("forcebeamcolumn", "dispbeamcolumn", "mixedbeamcolumn", "gradientinelasticbeamcolumn"):
            if len(num) >= 3 and str(int(num[1])) in secs and str(int(num[1])) not in integ:
                return int(num[2])                                          # numIntgrPts secTag transf (legacy)
            return int(num[0])                                              # transf integration
        if t == "nonlinearbeamcolumn":
            return int(num[2])
        return int(num[-1])
    except (IndexError, TypeError, ValueError):
        return None


def _strong_axis(seg, local_axes, to_bvec):
    p = seg.get("props") or {}
    if not p.get("I_major") or not p.get("I_minor") or p["I_major"] <= 1.05 * p["I_minor"]:
        return None
    x, y, z = local_axes(seg["e"])
    if y is None:
        return None
    # bending about the major axis deflects along the other local axis: that is the direction it resists
    resist = y if p.get("major") == "z" else z
    bv = to_bvec(resist)
    if abs(bv[0]) > 0.95:
        return "X"
    if abs(bv[1]) > 0.95:
        return "Y"
    return None


def _beam(n, k, P, Q, chain, plan, lvname, end_release, ax=None):
    if ax is not None:
        pts = [q for s_ in chain for q in (s_["P"], s_["Q"])]
        P = min(pts, key=lambda q: q[ax]); Q = max(pts, key=lambda q: q[ax])
    share = Counter()
    for s_ in chain:
        share[s_["sec"]] += s_["L"]
    main_sec = share.most_common(1)[0][0]
    first = next(s_ for s_ in chain if P in (s_["P"], s_["Q"]))
    last = next(s_ for s_ in reversed(chain) if Q in (s_["P"], s_["Q"]))
    ei = 0 if first["P"] == P else 1
    ej = 0 if last["P"] == Q else 1
    major = "Y" if abs(Q[0] - P[0]) > abs(Q[1] - P[1]) else "X"      # major-axis bending of a beam along X is about Y
    ri = major in end_release(first["tag"], first["e"], ei)
    rj = major in end_release(last["tag"], last["e"], ej)
    rel = "both" if ri and rj else ("I" if ri else ("J" if rj else "none"))
    dom = max((s_ for s_ in chain if s_["sec"] == main_sec), key=lambda s_: s_["L"])
    b = dict(id="B%d@%s" % (n, lvname(k)), level=lvname(k), i=plan(P), j=plan(Q), section=main_sec, release=rel,
             _where="OpenSees element %d%s" % (dom["tag"], (" + %d" % (len(chain) - 1)) if len(chain) > 1 else ""),
             _k=k, _P=P, _Q=Q)
    others = [s_ for s_ in share if s_ != main_sec]
    if others:
        b["_note"] = "segments of other properties (%s) not carried" % ", ".join(others)
    return b


def _mark_links(beams, braces, notes, col_at, lvname):
    """EBF links, from the geometry: a beam segment between two brace work points that are not at columns (the
    mid-span link of a split-K frame), or one from a column to a brace work point that is clearly the short part of
    its beam line (at most 60 % of the segment beyond the brace point). Equal halves on either side of a brace point
    are a chevron, not a link; a brace that lands on a column does not make the beam next to it a link."""
    ends = defaultdict(set)
    for br in braces:
        for e in ("i", "j"):
            ends[br[e]["level"]].add((str(br[e]["x"]), str(br[e]["y"])))
    key = lambda p: (str(p["x"]), str(p["y"]))
    for b in beams:
        lv = b["level"]
        cols = col_at.get(b["_k"], set())
        ci, cj = (b["_P"][0], b["_P"][1]) in cols, (b["_Q"][0], b["_Q"][1]) in cols
        hi, hj = key(b["i"]) in ends[lv] and not ci, key(b["j"]) in ends[lv] and not cj
        L = math.dist(b["_P"][:2], b["_Q"][:2])
        if hi and hj:
            b["link"] = True
        elif (hi and cj) or (hj and ci):
            bp = b["i"] if hi else b["j"]
            nxt = [o for o in beams if o is not b and o["level"] == lv and key(bp) in (key(o["i"]), key(o["j"]))
                   and (abs(o["_P"][1] - o["_Q"][1]) < 1e-9) == (abs(b["_P"][1] - b["_Q"][1]) < 1e-9)]
            if nxt and all(L <= 0.6 * math.dist(o["_P"][:2], o["_Q"][:2]) for o in nxt):
                b["link"] = True
    n = sum(1 for b in beams if b.get("link"))
    if n:
        notes.append("%d beam segment(s) at brace work points are read as EBF links (e.g. %s): the geometry of an eccentrically "
                     "braced frame" % (n, next(b["id"] for b in beams if b.get("link"))))


def _chains(lines, geom, which):
    """Collinear chains of horizontal ("h") or vertical ("v") line elements joined at shared nodes.
    -> [(z, plan extent)] for "h", [((x, y), height)] for "v" (model units)."""
    els = []
    for tag, e in lines.items():
        a, b, d, L = geom(e)
        if L <= 0:
            continue
        if which == "h" and abs(d[2]) / L < 0.005:
            els.append((tag, e, a, b, _norm([d[0], d[1], 0.0])))
        elif which == "v" and abs(d[2]) / L > 0.995:
            els.append((tag, e, a, b, None))
    parent = {t: t for t, *_ in els}

    def find(t):
        while parent[t] != t:
            parent[t] = parent[parent[t]]; t = parent[t]
        return t
    at = defaultdict(list)
    for t, e, a, b, dv in els:
        for n in e["nodes"][:2]:
            at[n].append((t, dv))
    for n, lst in at.items():
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                (t1, d1), (t2, d2) = lst[i], lst[j]
                if which == "v" or abs(abs(_dot(d1, d2)) - 1.0) < 1e-4:
                    parent[find(t1)] = find(t2)
    groups = defaultdict(list)
    for t, e, a, b, dv in els:
        groups[find(t)].append((a, b, dv))
    out = []
    for g in groups.values():
        if which == "h":
            dv = g[0][2]
            proj = [_dot(p, dv) for a, b, _ in g for p in (a, b)]
            out.append(((g[0][0][2] + g[0][1][2]) / 2.0, max(proj) - min(proj)))
        else:
            zz = [p[2] for a, b, _ in g for p in (a, b)]
            out.append(((g[0][0][0], g[0][0][1]), max(zz) - min(zz)))
    return out


def _floor_areas(zs, gx, gy, col_at, beams, ft):
    """Per level: the grid cells whose four corners are joints at that level (ft2)."""
    out = {}
    for k in range(1, len(zs)):
        pts = set(col_at.get(k, set()))
        for b in beams:
            if b["_k"] == k:
                pts.add((b["_P"][0], b["_P"][1])); pts.add((b["_Q"][0], b["_Q"][1]))
        keys = {(round(p[0], 6), round(p[1], 6)) for p in pts}
        A = 0.0
        for i in range(len(gx) - 1):
            for j in range(len(gy) - 1):
                cs = [(gx[i], gy[j]), (gx[i + 1], gy[j]), (gx[i], gy[j + 1]), (gx[i + 1], gy[j + 1])]
                if all((round(c[0], 6), round(c[1], 6)) in keys for c in cs):
                    A += (gx[i + 1] - gx[i]) * (gy[j + 1] - gy[j]) * ft * ft
        if A:
            out[k] = A
    return out


def _gravity(m, B, zs, upi, lines, local_axes, to_bvec, F, ft):
    """Per load pattern defined before the first analysis: downward load per level (kip)."""
    out = {}
    for tag, p in (m.get("patterns") or {}).items():
        if p.get("after_analysis") or str(p.get("type", "")).lower() not in ("plain",):
            continue
        per = defaultdict(float)
        for node, vals in p.get("loads") or []:
            if node in B or int(node) in B:
                c = B[int(node)]
                v = vals + [0.0] * (3 - len(vals))
                fz = to_bvec(v[:3])[2]
                if fz < 0:
                    k = min(range(len(zs)), key=lambda q: abs(zs[q] - c[2]))
                    per[k] += -fz * F
        for el in p.get("eleloads") or []:
            if str(el.get("type")).lower() not in ("-beamuniform", "beamuniform"):
                continue
            vals = el.get("vals") or []
            for t in el.get("eles") or []:
                e = lines.get(int(t))
                if not e:
                    continue
                x, y, z = local_axes(e)
                if y is None:
                    continue
                a, b = B[e["nodes"][0]], B[e["nodes"][1]]
                L = math.dist(a, b)
                wy = vals[0] if vals else 0.0
                wz = vals[1] if len(vals) > 1 else 0.0
                wx = vals[2] if len(vals) > 2 else 0.0
                gl = [wy * y[q] + wz * z[q] + wx * x[q] for q in range(3)]
                fz = to_bvec(gl)[2] * L
                if fz < 0:
                    k = min(range(len(zs)), key=lambda q: abs(zs[q] - (a[2] + b[2]) / 2))
                    per[k] += -fz * F
        if per:
            out[tag] = per
    return out
