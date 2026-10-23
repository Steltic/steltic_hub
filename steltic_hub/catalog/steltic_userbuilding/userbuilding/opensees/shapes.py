"""Section properties of a recorded OpenSees member, and the AISC shape they match.

OpenSees knows no section names: an elasticBeamColumn carries A, Iy, Iz (and J), a fibre section carries
patches, a WFSection carries d, tw, bf, tf, a truss carries A. Properties are worked out in the model's units,
converted to inches, and compared with the AISC shapes table HR Steel uses (steel_engine/aisc_shapes.csv).

A match is a PROPOSAL, never data: HR Steel shows it with its reason ("A, Ix, Iy and J within 0.4 % of
W14X90") and uses it only once the user accepts it. It is proposed only when the best shape fits every known
property within MATCH_TOL and no other shape fits about as well (else the user is asked to name the section).
"""
from __future__ import annotations

import csv
import math
import os
import pathlib

MATCH_TOL = 0.03          # largest relative difference on any compared property
CLEAR_MARGIN = 0.004      # the runner-up must be at least this much worse
_SHAPES = None


def shapes_table():
    global _SHAPES
    if _SHAPES is None:
        _SHAPES = []
        eng = os.environ.get("STELTIC_ENGINE_DIR") or ""
        for p in (pathlib.Path(eng) / "aisc_shapes.csv", pathlib.Path(__file__).with_name("aisc_shapes.csv")):
            if p.exists():
                with open(p, newline="") as f:
                    for r in csv.DictReader(f):
                        def fl(k):
                            try:
                                return float(r.get(k) or "nan")
                            except ValueError:
                                return float("nan")
                        _SHAPES.append(dict(label=r["AISC_Manual_Label"], type=r["Type"], A=fl("A"), Ix=fl("Ix"), Iy=fl("Iy"),
                                            J=fl("J"), d=fl("d"), bf=fl("bf"), tw=fl("tw"), tf=fl("tf")))
                break
    return _SHAPES


# ---------------------------------------------------------------- properties of a section (model units)
def _poly(pts):
    """Area and second moments (about the origin) of a polygon [(y, z)...]: (A, Sy, Sz, Iyy=int z^2, Izz=int y^2)."""
    A = Sy = Sz = Iyy = Izz = 0.0
    n = len(pts)
    for k in range(n):
        y0, z0 = pts[k]
        y1, z1 = pts[(k + 1) % n]
        c = y0 * z1 - y1 * z0
        A += c
        Sz += (y0 + y1) * c          # -> int y dA * 6
        Sy += (z0 + z1) * c          # -> int z dA * 6
        Izz += (y0 * y0 + y0 * y1 + y1 * y1) * c
        Iyy += (z0 * z0 + z0 * z1 + z1 * z1) * c
    A /= 2.0
    s = 1.0 if A >= 0 else -1.0
    return abs(A), s * Sy / 6.0, s * Sz / 6.0, s * Iyy / 12.0, s * Izz / 12.0


def fiber_props(sec):
    """A, Iy (int z^2 dA), Iz (int y^2 dA) about the centroid, and a recognised shape (I / box / round) with its
    dimensions, from a fibre section's patches, layers and fibres. None when it has none."""
    parts = []                                # (A, int y, int z, int z^2, int y^2, kind, geometry)
    rects = []
    for p in sec.get("patches") or []:
        t = str(p.get("type")).lower()
        a = [x for x in p.get("args") or []]
        try:
            if t in ("rect", "rectangular") and len(a) >= 7:
                yI, zI, yJ, zJ = (float(x) for x in a[3:7])
                pts = [(yI, zI), (yJ, zI), (yJ, zJ), (yI, zJ)]
                rects.append((min(yI, yJ), max(yI, yJ), min(zI, zJ), max(zI, zJ)))
            elif t in ("quad", "quadr", "quadrilateral") and len(a) >= 11:
                v = [float(x) for x in a[3:11]]
                pts = [(v[0], v[1]), (v[2], v[3]), (v[4], v[5]), (v[6], v[7])]
            elif t in ("circ", "circular") and len(a) >= 9:
                yc, zc, ri, ro = (float(x) for x in a[3:7])
                a0, a1 = (float(a[7]), float(a[8])) if len(a) >= 9 else (0.0, 360.0)
                if abs((a1 - a0) % 360.0) > 1e-6 and abs(a1 - a0) < 359.999:
                    return None
                A = math.pi * (ro * ro - ri * ri)
                I = math.pi / 4.0 * (ro ** 4 - ri ** 4)
                parts.append((A, A * yc, A * zc, I + A * zc * zc, I + A * yc * yc))
                sec["_circ"] = (2 * ro, ro - ri)
                continue
            else:
                return None
        except (TypeError, ValueError):
            return None
        A, Sy, Sz, Iyy, Izz = _poly(pts)
        parts.append((A, Sz, Sy, Iyy, Izz))
    for l in sec.get("layers") or []:
        a = l.get("args") or []
        try:
            if str(l.get("type")).lower() == "straight" and len(a) >= 7:
                n, af = int(a[1]), float(a[2])
                y0, z0, y1, z1 = (float(x) for x in a[3:7])
                for k in range(n):
                    f = k / (n - 1) if n > 1 else 0.5          # n fibres from the start point to the end point
                    y, z = y0 + f * (y1 - y0), z0 + f * (z1 - z0)
                    parts.append((af, af * y, af * z, af * z * z, af * y * y))
        except (TypeError, ValueError):
            return None
    for fb in sec.get("fibers") or []:
        try:
            y, z, af = float(fb[0]), float(fb[1]), float(fb[2])
        except (TypeError, ValueError, IndexError):
            return None
        parts.append((af, af * y, af * z, af * z * z, af * y * y))
    if not parts:
        return None
    A = sum(p[0] for p in parts)
    if A <= 0:
        return None
    yc = sum(p[1] for p in parts) / A
    zc = sum(p[2] for p in parts) / A
    Iy = sum(p[3] for p in parts) - A * zc * zc
    Iz = sum(p[4] for p in parts) - A * yc * yc
    out = dict(A=A, Iy=Iy, Iz=Iz)
    shape = _shape_of(rects, A)
    if shape:
        out.update(shape)
    if sec.get("_circ"):
        out.update(kind="round", D=sec["_circ"][0], t=sec["_circ"][1])
    return out


def _shape_of(rects, A):
    """Dimensions of an I-shape (three rectangles: two flanges and a web) or a box (four walls) built of rect
    patches. 'web_along' is the local axis the web runs along: 'y' means bending about z is the major axis."""
    if len(rects) == 3:
        for axis in ("y", "z"):
            # flanges: wide along the other axis, at the two extremes of `axis`; web: narrow, between them
            def ext(r):
                return (r[0], r[1]) if axis == "y" else (r[2], r[3])
            def wid(r):
                return (r[2], r[3]) if axis == "y" else (r[0], r[1])
            rs = sorted(rects, key=lambda r: ext(r)[0])
            f1, w, f2 = rs[0], rs[1], rs[2]
            d = ext(f2)[1] - ext(f1)[0]
            tf1, tf2 = ext(f1)[1] - ext(f1)[0], ext(f2)[1] - ext(f2)[0]
            bf = wid(f1)[1] - wid(f1)[0]
            tw = wid(w)[1] - wid(w)[0]
            if d > 0 and bf > tw > 0 and abs(tf1 - tf2) <= 0.05 * max(tf1, tf2) and abs((wid(f2)[1] - wid(f2)[0]) - bf) <= 0.05 * bf \
                    and ext(w)[0] >= ext(f1)[1] - 1e-9 * d and ext(w)[1] <= ext(f2)[0] + 1e-9 * d:
                return dict(kind="I", d=d, bf=bf, tf=tf1, tw=tw, web_along=axis)
    if len(rects) == 4:
        ys = [v for r in rects for v in r[:2]]
        zs = [v for r in rects for v in r[2:]]
        H, B = max(ys) - min(ys), max(zs) - min(zs)
        ts = sorted(min(r[1] - r[0], r[3] - r[2]) for r in rects)
        if H > 0 and B > 0 and ts[-1] - ts[0] <= 0.05 * ts[-1]:
            return dict(kind="box", H=H, B=B, t=ts[0])
    return None


def element_props(m, el, conv_len):
    """-> dict(A, I_major, I_minor, J?, major='y'|'z', dims..., src) in INCHES, or dict(A) for a truss, or None.
    `conv_len` turns a model length into inches."""
    t = el["type"].lower()
    a = el.get("args") or []
    ndm = m.get("ndm") or 3
    L2, L4 = conv_len ** 2, conv_len ** 4
    num = [x for x in a if isinstance(x, (int, float))]

    def done(A, Iy=None, Iz=None, J=None, extra=None, src=""):
        out = dict(A=A * L2, src=src)
        if Iy is not None and Iz is not None:
            if Iz >= Iy:
                out.update(I_major=Iz * L4, I_minor=Iy * L4, major="z")
            else:
                out.update(I_major=Iy * L4, I_minor=Iz * L4, major="y")
        if J is not None and J > 0:
            out["J"] = J * L4
        for k, v in (extra or {}).items():
            out[k] = v * conv_len if k in ("d", "bf", "tf", "tw", "H", "B", "t", "D") else v
        return out

    if t == "elasticbeamcolumn":
        if ndm == 3 and len(num) >= 7:
            A, E, G, J, Iy, Iz = num[:6]
            return done(A, Iy, Iz, J, src="elasticBeamColumn")
        if ndm == 3 and len(num) >= 2 and not _has_float_props(num):      # section form: secTag transfTag
            return section_props(m, int(num[0]), conv_len, "elasticBeamColumn section")
        if ndm == 2 and len(num) >= 4:
            return done(num[0], None, None, src="elasticBeamColumn")
        return None
    if t == "elastictimoshenkobeam" and ndm == 3 and len(num) >= 6:
        E, G, A, J, Iy, Iz = num[:6]
        return done(A, Iy, Iz, J, src="elasticTimoshenkoBeam")
    if t in ("truss", "corottruss"):
        if len(num) >= 2:                                       # truss tag i j A matTag
            return done(float(num[0]), src=el["type"])
        if len(num) == 1:                                       # truss tag i j secTag
            return section_props(m, int(num[0]), conv_len, el["type"])
        return None
    if t in ("trusssection", "corottrusssection") and num:
        return section_props(m, int(num[0]), conv_len, el["type"])
    if t in ("forcebeamcolumn", "dispbeamcolumn", "nonlinearbeamcolumn", "mixedbeamcolumn", "beamwithhinges",
             "gradientinelasticbeamcolumn"):
        sec = _beam_section_tag(m, el)
        if sec is not None:
            return section_props(m, sec, conv_len, el["type"])
    return None


def _has_float_props(num):
    return any(isinstance(x, float) and not float(x).is_integer() for x in num[:6])


def _beam_section_tag(m, el):
    """The section a beam-column integrates (the interior section of a hinge integration)."""
    a = el.get("args") or []
    s = [str(x) for x in a]
    t = el["type"].lower()
    num = [x for x in a if isinstance(x, (int, float))]
    if "-sections" in s:
        i = s.index("-sections") + 1
        tags = [x for x in a[i:] if isinstance(x, int)]
        return tags[len(tags) // 2] if tags else None
    if t == "beamwithhinges" and len(num) >= 5:
        return int(num[0])
    if t == "nonlinearbeamcolumn" or (t in ("forcebeamcolumn", "dispbeamcolumn") and len(num) >= 3 and
                                      str(num[1]) in [str(k) for k in (m.get("sections") or {})] and
                                      str(num[0]) not in [str(k) for k in (m.get("integrations") or {})]):
        return int(num[1]) if len(num) >= 2 else None              # legacy: numIntgrPts secTag transfTag
    if len(num) >= 2:                                           # transfTag integrationTag
        integ = (m.get("integrations") or {}).get(str(int(num[1])))
        if integ:
            ia = [x for x in integ.get("args") or [] if isinstance(x, (int, float))]
            it = integ.get("type", "").lower()
            if it.startswith("hinge") and len(ia) >= 5:          # HingeRadau secI lpI secJ lpJ secInterior
                return int(ia[4])
            if it in ("userdefined", "fixedlocation", "lowordertype", "midDistance".lower()) and len(ia) >= 2:
                n = int(ia[0]); tags = ia[1:1 + n]
                return int(tags[len(tags) // 2]) if tags else None
            if ia:
                return int(ia[0])
    return None


def section_props(m, tag, conv_len, src):
    sec = (m.get("sections") or {}).get(str(tag))
    if not sec:
        return None
    t = sec.get("type", "").lower()
    a = [x for x in sec.get("args") or []]
    num = [x for x in a if isinstance(x, (int, float))]
    L2, L4 = conv_len ** 2, conv_len ** 4
    src = "%s, section %s %s" % (src, sec.get("type"), tag)

    def out(A, Iy=None, Iz=None, J=None, dims=None):
        o = dict(A=A * L2, src=src)
        if Iy is not None and Iz is not None:
            if Iz >= Iy:
                o.update(I_major=Iz * L4, I_minor=Iy * L4, major="z")
            else:
                o.update(I_major=Iy * L4, I_minor=Iz * L4, major="y")
        if J:
            o["J"] = J * L4
        for k, v in (dims or {}).items():
            o[k] = v * conv_len if isinstance(v, (int, float)) and k not in ("kind",) else v
        return o

    if t == "elastic":
        if len(num) >= 6 and (m.get("ndm") or 3) == 3:
            E, A, Iz, Iy, G, J = num[:6]
            return out(A, Iy, Iz, J)
        if len(num) >= 3:
            return out(num[1])
    if t in ("wfsection2d", "wfsection3d", "wsection2d", "wsection3d") and len(num) >= 5:
        d, tw, bf, tf = num[1:5]
        A = 2 * bf * tf + (d - 2 * tf) * tw
        Iz = (bf * d ** 3 - (bf - tw) * (d - 2 * tf) ** 3) / 12.0
        Iy = (2 * tf * bf ** 3 + (d - 2 * tf) * tw ** 3) / 12.0
        return out(A, Iy, Iz, None, dict(kind="I", d=d, bf=bf, tf=tf, tw=tw))
    if t == "aggregator":
        s = [str(x) for x in a]
        if "-section" in s:
            return section_props(m, int(a[s.index("-section") + 1]), conv_len, src)
        return None
    if t in ("fiber", "fibersection", "fibersec", "ndfiber"):
        fp = fiber_props(sec)
        if not fp:
            return None
        dims = {k: v for k, v in fp.items() if k in ("d", "bf", "tf", "tw", "H", "B", "t", "D")}
        dims["kind"] = fp.get("kind")
        s = [str(x) for x in a]
        J = None
        if "-GJ" in s:
            J = None                                              # GJ only (no J): not compared
        o = out(fp["A"], fp["Iy"], fp["Iz"], J, dims)
        if fp.get("web_along"):
            o["major"] = "z" if fp["web_along"] == "y" else "y"
        return o
    return None


# ---------------------------------------------------------------- the AISC shape it is
def _rel(a, b):
    return abs(a - b) / b if b and math.isfinite(b) and b > 0 and a is not None else None


def match(props):
    """-> (label, worst relative difference, what was compared, runner-up) or (None, ...) when nothing fits
    clearly. Members whose only property is the area (trusses) are never matched: too many shapes share an area."""
    if not props or "I_major" not in props:
        return None, None, "area only", None
    kind = props.get("kind")
    fam = {"I": ("W", "HP", "M", "S"), "box": ("HSS",), "round": ("HSS", "PIPE")}.get(kind)
    best = []
    for s in shapes_table():
        if fam and s["type"] not in fam:
            continue
        if s["type"] not in ("W", "HP", "M", "S", "HSS", "PIPE"):
            continue
        if not all(math.isfinite(s[k]) for k in ("A", "Ix", "Iy")):
            continue
        errs = [_rel(props["A"], s["A"]), _rel(props["I_major"], s["Ix"]), _rel(props["I_minor"], s["Iy"])]
        what = ["A", "Ix", "Iy"]
        if kind == "I":
            for k in ("d", "bf", "tf", "tw"):
                if props.get(k) and math.isfinite(s[k]):
                    errs.append(_rel(props[k], s[k])); what.append(k)
        errs = [e for e in errs if e is not None]
        if errs:
            best.append((max(errs), s["label"], what))
    best.sort()
    if not best:
        return None, None, "", None
    top = best[0]
    second = best[1] if len(best) > 1 else None
    if top[0] <= MATCH_TOL and (second is None or second[0] - top[0] >= CLEAR_MARGIN):
        what = "/".join(top[2])
        s = next(x for x in shapes_table() if x["label"] == top[1])
        if props.get("J") and math.isfinite(s["J"]) and s["J"] > 0:
            jr = abs(props["J"] - s["J"]) / s["J"]
            what += "; J %s" % ("agrees" if jr <= MATCH_TOL else "differs by %.0f %% (torsion is not used to identify the shape)" % (100 * jr))
        return top[1], top[0], what, second
    return None, top[0], "/".join(top[2]), top
