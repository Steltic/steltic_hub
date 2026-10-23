"""Run a user's OpenSees model and record what it builds -- without OpenSees.

    python record.py <main file (.py | .tcl)> <out.json>

The script runs as written, in its own folder (a scratch copy), against a stand-in for OpenSees: every model
command (model, node, fix, mass, materials, sections with their fibres / patches / layers, geomTransf,
beamIntegration, element, rigidDiaphragm, equalDOF, rigidLink, timeSeries, pattern, load, eleLoad) is recorded;
analysis commands answer as a converged analysis would (analyze -> 0, the pseudo-time and the control
displacement advance, eigen returns plausible eigenvalues) so the script's own loops end; queries
(nodeCoord, getNodeTags, eleNodes, nodeMass ...) answer from what was recorded. Nothing is analysed.

  * OpenSeesPy (.py): `openseespy.opensees` (and the usual plotting helpers -- opsvis, vfo, opstool,
    openseespy.postprocessing) are replaced in sys.modules before the script runs.
  * OpenSees Tcl (.tcl): a real Tcl interpreter (Python's tkinter) with the OpenSees commands defined, so
    set / expr / foreach / proc / source behave exactly as in OpenSees. A `section Fiber ... { body }` or
    `pattern Plain ... { body }` body is evaluated with that section / pattern current.

A script that wipes and rebuilds (one model per record, a pushover after a gravity model ...) leaves several
models: each is kept at its `wipe`, and the one with the most elements is the model read. A script that stops
part-way (a missing ground-motion file, an exception after the model was built) still yields what it built;
where and why it stopped is reported. Stdlib only: this file runs under whichever interpreter has Tcl / numpy.
"""
from __future__ import annotations

import copy
import json
import math
import os
import runpy
import sys
import time
import traceback
import types

ANALYZE_BUDGET = 20000          # analyze calls after which the script is stopped (the model is long complete)
WALL_BUDGET_S = 120.0           # seconds of script time after the first analysis


class Stop(Exception):
    """Raised into the script to end it: an analysis loop that would not end, or an `exit`."""


def _num(v):
    """A command argument as a number when it is one (Tcl hands everything over as text)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip()
    try:
        if s.lstrip("+-").isdigit():
            return int(s)
        return float(s)
    except ValueError:
        return s


def _flat(args):
    out = []
    for a in args:
        if isinstance(a, (list, tuple)):
            out.extend(_flat(a))
        else:
            out.append(a)
    return out


class Recorder:
    def __init__(self, lang):
        self.lang = lang
        self.snapshots = []
        self.unknown = {}
        self.calls = {}
        self.events = []
        self.analyze_calls = 0
        self.first_analysis = None
        self.body_eval = None              # Tcl: evaluates a { body } argument
        self.reset()

    # ------------------------------------------------------------------ the model
    def reset(self):
        self.m = dict(ndm=None, ndf=None, nodes={}, fix={}, mass={}, materials={}, sections={}, transfs={},
                      integrations={}, elements={}, diaphragms=[], equaldofs=[], rigidlinks=[], timeseries={},
                      patterns={}, order=[])
        self.cur_section = None
        self.cur_pattern = None
        self.time = 0.0
        self.integrator = ("LoadControl", [1.0])
        self.disp = {}

    def snapshot(self, why):
        if self.m["elements"]:
            s = copy.deepcopy(self.m)
            s["_why"] = why
            self.snapshots.append(s)

    def note(self, text):
        if len(self.events) < 200:
            self.events.append(text)

    # ------------------------------------------------------------------ dispatch
    def call(self, name, args):
        self.calls[name] = self.calls.get(name, 0) + 1
        fn = getattr(self, "c_" + name, None)
        if fn is None:
            if name in _NOOP:
                return 0
            self.unknown[name] = self.unknown.get(name, 0) + 1
            return 0
        return fn([_num(a) for a in _flat(args)])

    # ------------------------------------------------------------------ model building
    def c_wipe(self, a):
        self.snapshot("wipe")
        self.reset()
        return 0

    def c_model(self, a):
        s = [str(x) for x in a]
        for k in ("-ndm", "-ndf"):
            if k in s and s.index(k) + 1 < len(a):
                self.m[k[1:]] = int(_num(a[s.index(k) + 1]))
        if self.m["ndm"] is None and len(a) >= 2 and isinstance(a[1], int):      # model basic 3 6
            self.m["ndm"] = a[1]
            if len(a) >= 3 and isinstance(a[2], int):
                self.m["ndf"] = a[2]
        if self.m["ndf"] is None and self.m["ndm"]:
            self.m["ndf"] = {1: 1, 2: 3, 3: 6}.get(self.m["ndm"])
        return 0

    def c_node(self, a):
        tag = int(a[0])
        ndm = self.m["ndm"] or 3
        coords = [float(x) for x in a[1:1 + ndm] if isinstance(x, (int, float))]
        self.m["nodes"][tag] = coords
        s = [str(x) for x in a]
        if "-mass" in s:
            i = s.index("-mass") + 1
            self.m["mass"][tag] = [float(x) for x in a[i:] if isinstance(x, (int, float))]
        return 0

    def c_fix(self, a):
        self.m["fix"][int(a[0])] = [int(x) for x in a[1:] if isinstance(x, (int, float))]
        return 0

    def _fix_coord(self, axis, a):
        tol = 1e-10
        s = [str(x) for x in a]
        if "-tol" in s:
            tol = float(a[s.index("-tol") + 1])
            a = a[:s.index("-tol")]
        c = float(a[0])
        flags = [int(x) for x in a[1:] if isinstance(x, (int, float))]
        for t, xyz in self.m["nodes"].items():
            if len(xyz) > axis and abs(xyz[axis] - c) <= tol:
                self.m["fix"][t] = flags
        return 0

    def c_fixX(self, a): return self._fix_coord(0, a)
    def c_fixY(self, a): return self._fix_coord(1, a)
    def c_fixZ(self, a): return self._fix_coord(2, a)

    def c_mass(self, a):
        self.m["mass"][int(a[0])] = [float(x) for x in a[1:] if isinstance(x, (int, float))]
        return 0

    def c_uniaxialMaterial(self, a):
        self.m["materials"][int(a[1])] = dict(type=str(a[0]), args=a[2:], kind="uniaxial")
        return 0

    def c_nDMaterial(self, a):
        self.m["materials"][("nd", int(a[1]))] = dict(type=str(a[0]), args=a[2:], kind="nd")
        return 0

    def c_section(self, a):
        typ, tag = str(a[0]), int(a[1])
        rest = list(a[2:])
        body = None
        if self.lang == "tcl" and rest and isinstance(rest[-1], str) and ("\n" in rest[-1] or rest[-1].strip().split(" ")[0] in _SECTION_BODY):
            body = rest.pop()
        self.m["sections"][tag] = dict(type=typ, args=rest, patches=[], layers=[], fibers=[])
        self.cur_section = tag
        if body is not None and self.body_eval:
            self.body_eval(body)
        return 0

    def _sec(self):
        if self.cur_section is None or self.cur_section not in self.m["sections"]:
            return None
        return self.m["sections"][self.cur_section]

    def c_patch(self, a):
        s = self._sec()
        if s is not None:
            s["patches"].append(dict(type=str(a[0]), args=a[1:]))
        return 0

    def c_layer(self, a):
        s = self._sec()
        if s is not None:
            s["layers"].append(dict(type=str(a[0]), args=a[1:]))
        return 0

    def c_fiber(self, a):
        s = self._sec()
        if s is not None:
            s["fibers"].append(a[:4])
        return 0

    def c_geomTransf(self, a):
        typ, tag = str(a[0]), int(a[1])
        vec = [float(x) for x in a[2:5] if isinstance(x, (int, float))] if (self.m["ndm"] or 3) == 3 else []
        self.m["transfs"][tag] = dict(type=typ, vecxz=vec if len(vec) == 3 else None, args=a[2:])
        return 0

    def c_beamIntegration(self, a):
        self.m["integrations"][int(a[1])] = dict(type=str(a[0]), args=a[2:])
        return 0

    def c_element(self, a):
        typ, tag = str(a[0]), int(a[1])
        nn = _NODES_PER_ELEMENT.get(typ.lower(), 2)
        nodes = [int(x) for x in a[2:2 + nn] if isinstance(x, (int, float))]
        self.m["elements"][tag] = dict(type=typ, nodes=nodes, args=a[2 + nn:])
        if self.first_analysis is None and len(self.m["elements"]) == 1:
            self.m["order"].append("elements")
        return 0

    def c_rigidDiaphragm(self, a):
        self.m["diaphragms"].append(dict(perp=int(a[0]), master=int(a[1]), slaves=[int(x) for x in a[2:]]))
        return 0

    def c_equalDOF(self, a):
        self.m["equaldofs"].append(dict(r=int(a[0]), c=int(a[1]), dofs=[int(x) for x in a[2:]]))
        return 0

    def c_rigidLink(self, a):
        self.m["rigidlinks"].append(dict(type=str(a[0]), r=int(a[1]), c=int(a[2])))
        return 0

    def c_timeSeries(self, a):
        self.m["timeseries"][int(a[1]) if len(a) > 1 and isinstance(a[1], int) else len(self.m["timeseries"])] = dict(type=str(a[0]))
        return 0

    def c_pattern(self, a):
        typ = str(a[0])
        tag = int(a[1]) if len(a) > 1 and isinstance(a[1], int) else len(self.m["patterns"]) + 1
        rest = list(a[2:])
        body = None
        if self.lang == "tcl" and rest and isinstance(rest[-1], str) and ("load" in rest[-1] or "\n" in rest[-1] or "sp " in rest[-1]):
            body = rest.pop()
        self.m["patterns"][tag] = dict(type=typ, args=rest, loads=[], eleloads=[], after_analysis=self.first_analysis is not None)
        self.cur_pattern = tag
        if body is not None and self.body_eval:
            self.body_eval(body)
        return 0

    def c_load(self, a):
        p = self.m["patterns"].get(self.cur_pattern)
        if p is not None:
            p["loads"].append([int(a[0]), [float(x) for x in a[1:] if isinstance(x, (int, float))]])
        return 0

    def c_eleLoad(self, a):
        p = self.m["patterns"].get(self.cur_pattern)
        if p is None:
            return 0
        s = [str(x) for x in a]
        eles = []
        if "-ele" in s:
            i = s.index("-ele") + 1
            while i < len(a) and isinstance(a[i], int):
                eles.append(a[i]); i += 1
        if "-range" in s:
            i = s.index("-range") + 1
            eles.extend(range(int(a[i]), int(a[i + 1]) + 1))
        typ = str(a[s.index("-type") + 1]) if "-type" in s else ""
        vals = [float(x) for x in a[s.index("-type") + 2:] if isinstance(x, (int, float))] if "-type" in s else []
        p["eleloads"].append(dict(eles=eles, type=typ, vals=vals))
        return 0

    # ------------------------------------------------------------------ analysis (answered, never run)
    def _analysis_started(self):
        if self.first_analysis is None:
            self.first_analysis = time.time()
            self.snapshot("first analysis")
            if self.snapshots:
                self.snapshots.pop()                 # the end-of-run model supersedes it unless a wipe comes first
        if time.time() - self.first_analysis > WALL_BUDGET_S:
            raise Stop("the script was still analysing %d s after its first analysis -- stopped (the model was complete)" % WALL_BUDGET_S)

    def c_integrator(self, a):
        self.integrator = (str(a[0]) if a else "", a[1:])
        return 0

    def c_analyze(self, a):
        self._analysis_started()
        self.analyze_calls += 1
        if self.analyze_calls > ANALYZE_BUDGET:
            raise Stop("%d analysis steps -- the analysis loop was stopped (the model was complete)" % ANALYZE_BUDGET)
        n = int(a[0]) if a and isinstance(a[0], (int, float)) else 1
        typ, ia = self.integrator
        if len(a) > 1 and isinstance(a[1], (int, float)):              # transient: analyze n dt
            self.time += n * float(a[1])
        elif typ == "LoadControl" and ia and isinstance(ia[0], (int, float)):
            self.time += n * float(ia[0])
        elif typ == "DisplacementControl" and len(ia) >= 3:
            node, dof, inc = int(ia[0]), int(ia[1]), float(ia[2])
            d = self.disp.setdefault(node, {})
            d[dof] = d.get(dof, 0.0) + n * inc
            self.time += n
        else:
            self.time += n
        return 0

    def c_eigen(self, a):
        self._analysis_started()
        n = next((int(x) for x in a if isinstance(x, (int, float))), 1)
        return [(2 * math.pi * (k + 1)) ** 2 for k in range(n)]

    def c_getTime(self, a): return self.time
    def c_setTime(self, a): self.time = float(a[0]) if a else 0.0; return 0
    def c_loadConst(self, a):
        s = [str(x) for x in a]
        if "-time" in s:
            self.time = float(a[s.index("-time") + 1])
        return 0

    def c_nodeCoord(self, a):
        c = self.m["nodes"].get(int(a[0]), [0.0, 0.0, 0.0])
        if len(a) > 1 and isinstance(a[1], int):
            return c[a[1] - 1] if 0 < a[1] <= len(c) else 0.0
        return list(c)

    def c_nodeDisp(self, a):
        d = self.disp.get(int(a[0]), {}) if a else {}
        if len(a) > 1 and isinstance(a[1], int):
            return d.get(a[1], 0.0)
        return [d.get(k + 1, 0.0) for k in range(self.m["ndf"] or 6)]

    c_nodeVel = c_nodeAccel = c_nodeReaction = c_nodeResponse = lambda self, a: (0.0 if len(a) > 1 else [0.0] * (self.m["ndf"] or 6))

    def c_nodeEigenvector(self, a):
        return 0.0 if len(a) > 2 else [0.0] * (self.m["ndf"] or 6)

    def c_getNodeTags(self, a): return sorted(self.m["nodes"])
    def c_getEleTags(self, a): return sorted(self.m["elements"])
    def c_eleNodes(self, a): return list((self.m["elements"].get(int(a[0])) or {}).get("nodes") or [])
    def c_nodeMass(self, a):
        m = self.m["mass"].get(int(a[0]), [0.0] * (self.m["ndf"] or 6))
        return m[a[1] - 1] if len(a) > 1 and isinstance(a[1], int) and a[1] <= len(m) else list(m)
    def c_getFixedNodes(self, a): return sorted(self.m["fix"])
    def c_getFixedDOFs(self, a): return [k + 1 for k, f in enumerate(self.m["fix"].get(int(a[0]), [])) if f]
    def c_eleResponse(self, a): return [0.0] * 12
    c_eleForce = c_basicForce = c_basicDeformation = c_sectionForce = c_sectionDeformation = c_eleResponse
    def c_testNorm(self, a): return [0.0]
    def c_testIter(self, a): return 1
    def c_systemSize(self, a): return 0
    def c_getNDM(self, a): return [self.m["ndm"] or 3]
    def c_getNDF(self, a): return [self.m["ndf"] or 6]
    def c_exit(self, a): raise Stop("exit")

    # ------------------------------------------------------------------ the result
    def result(self, stopped=None):
        self.snapshot("end of script")
        best = max(self.snapshots, key=lambda s: len(s["elements"]), default=None)
        out = dict(format="steltic.opensees_record", version=1, lang=self.lang, models=len(self.snapshots),
                   model=_jsonable(best) if best else None, stopped=stopped, unknown_commands=self.unknown,
                   calls=self.calls, events=self.events, analyze_calls=self.analyze_calls)
        return out


def _jsonable(m):
    def conv(o):
        if isinstance(o, dict):
            return {(("%s:%s" % k) if isinstance(k, tuple) else str(k)): conv(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [conv(x) for x in o]
        if isinstance(o, float) and not math.isfinite(o):
            return str(o)
        return o
    return conv(m)


# commands that change nothing a building description reads
_NOOP = {"recorder", "record", "constraints", "numberer", "system", "test", "algorithm", "analysis", "wipeAnalysis",
         "printModel", "print", "region", "rayleigh", "modalDamping", "modalProperties", "responseSpectrumAnalysis",
         "database", "logFile", "start", "stop", "reset", "remove", "setParameter", "parameter", "updateParameter",
         "addToParameter", "sensitivityAlgorithm", "domainChange", "partition", "barrier", "send", "recv",
         "setNumThreads", "getNumThreads", "setPrecision", "defaultUnits", "metaData", "version", "sp",
         "imposedMotion", "groundMotion", "geomTransfMesh", "mesh", "remesh", "block2D", "block3D", "frictionModel",
         "limitCurve", "damping", "setElementRayleighDampingFactors", "setNodeCoord", "setNodeDisp", "setNodeVel",
         "setNodeAccel", "setCreep", "updateElementDomain", "wipeReliability", "convertBinaryToText", "convertTextToBinary",
         "getEleClassTags", "getEleLoadTags", "getEleLoadData", "getLoadFactor", "nodeDOFs", "nodeBounds",
         "getPatterns", "getRVTags", "numFact", "numIter", "searchPeerNGA", "stripXML", "InitialStateAnalysis",
         "totalCPU", "solveCPU", "accelCPU", "numberer", "setStartNodeTag", "nodeRotation", "nodeUnbalance", "reactions"}

_SECTION_BODY = {"patch", "layer", "fiber"}
_NODES_PER_ELEMENT = {"zerolength": 2, "zerolengthsection": 2, "twonodelink": 2, "truss": 2, "corottruss": 2,
                      "trusssection": 2, "corottrusssection": 2, "elasticbeamcolumn": 2, "forcebeamcolumn": 2,
                      "dispbeamcolumn": 2, "nonlinearbeamcolumn": 2, "beamwithhinges": 2, "elastictimoshenkobeam": 2,
                      "modelasticbeam2d": 2, "mixedbeamcolumn": 2, "gradientinelasticbeamcolumn": 2,
                      "shellmitc4": 4, "shelldkgq": 4, "shellnldkgq": 4, "shellmitc9": 9, "asdshellq4": 4,
                      "stdbrick": 8, "bbarbrick": 8, "quad": 4, "tri31": 3, "joint2d": 5, "joint3d": 7,
                      "zerolengthcontact2d": 2, "elastomericbearingplasticity": 2, "flatsliderbearing": 2}


# ---------------------------------------------------------------- OpenSeesPy
class _Permissive(types.ModuleType):
    """A plotting helper the script imports (opsvis, vfo ...): any attribute, any call, nothing happens."""
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Anything()


class _Anything:
    def __call__(self, *a, **k):
        return _Anything()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Anything()

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return False


def _ops_module(rec):
    mod = types.ModuleType("openseespy.opensees")

    def make(name):
        def cmd(*args, **kw):
            return rec.call(name, list(args))
        cmd.__name__ = name
        return cmd

    names = [n[2:] for n in dir(Recorder) if n.startswith("c_")] + sorted(_NOOP)
    for n in names:
        setattr(mod, n, make(n))
    mod.__all__ = names
    mod.__getattr__ = lambda name: make(name) if not name.startswith("__") else (_ for _ in ()).throw(AttributeError(name))
    return mod


class _StubFinder:
    PREFIXES = ("opsvis", "vfo", "opstool", "openseespy.postprocessing", "opseestools", "opsvis3d")

    def find_spec(self, name, path=None, target=None):
        if name.startswith(self.PREFIXES):
            import importlib.machinery
            return importlib.machinery.ModuleSpec(name, self, is_package=True)
        return None

    def create_module(self, spec):
        m = _Permissive(spec.name)
        m.__path__ = []
        return m

    def exec_module(self, module):
        pass


class _FallbackFinder(_StubFinder):
    """Last in sys.meta_path: display / progress packages the script imports but this interpreter lacks become
    stand-ins (an installed one is used as it is). tqdm passes its iterable through, so a model loop still runs."""
    PREFIXES = ("matplotlib", "mpl_toolkits", "plotly", "pyvista", "vtk", "seaborn", "IPython", "tqdm", "rich",
                "openseespyvis", "pyqtgraph")

    def exec_module(self, module):
        if module.__name__.split(".")[0] == "tqdm":
            def tqdm(iterable=None, *a, **k):
                return iterable if iterable is not None else _Anything()
            module.tqdm = tqdm
            module.trange = lambda *a, **k: range(*[x for x in a if isinstance(x, int)])
            module.auto = module


def run_python(path, rec):
    ops = _ops_module(rec)
    pkg = types.ModuleType("openseespy")
    pkg.__path__ = []
    pkg.opensees = ops
    sys.modules["openseespy"] = pkg
    sys.modules["openseespy.opensees"] = ops
    sys.meta_path.insert(0, _StubFinder())
    sys.meta_path.append(_FallbackFinder())
    os.environ.setdefault("MPLBACKEND", "Agg")
    sys.argv = [path]
    sys.path.insert(0, os.path.dirname(os.path.abspath(path)))
    runpy.run_path(path, run_name="__main__")


# ---------------------------------------------------------------- OpenSees Tcl
def run_tcl(path, rec):
    try:
        import tkinter
    except ImportError:
        raise RuntimeError("NO_TCL")
    tcl = tkinter.Tcl()
    rec.body_eval = tcl.eval

    def make(name):
        def cmd(*args):
            r = rec.call(name, list(args))
            if isinstance(r, bool):
                return int(r)
            return r
        return cmd

    for n in [n[2:] for n in dir(Recorder) if n.startswith("c_")] + sorted(_NOOP):
        tcl.createcommand(n, make(n))
    # OpenSees commands the stand-in does not know are recorded, not fatal (Tcl calls `unknown` for them)
    tcl.createcommand("_steltic_unknown", lambda *a: rec.call(str(a[0]) if a else "?", []) and "" or "")
    tcl.eval("proc unknown {args} { _steltic_unknown {*}$args }")
    script = open(path, encoding="utf-8", errors="replace").read()
    tcl.eval("set argv {}; set argc 0; set argv0 {%s}" % os.path.basename(path))
    try:
        tcl.eval(script)
    except tkinter.TclError as e:
        info = ""
        try:
            info = tcl.eval("set ::errorInfo")
        except Exception:
            pass
        if "STELTIC_STOP" in str(e) or "Stop" in info and "Recorder" in info:
            raise Stop(str(e))
        raise RuntimeError("Tcl: %s\n%s" % (e, info[-1500:]))


def main(argv=None):
    argv = argv or sys.argv[1:]
    path, out = os.path.abspath(argv[0]), argv[1]
    lang = "tcl" if path.lower().endswith(".tcl") else "py"
    rec = Recorder(lang)
    stopped = None
    os.chdir(os.path.dirname(path))
    try:
        if lang == "py":
            run_python(path, rec)
        else:
            run_tcl(path, rec)
    except Stop as e:
        stopped = dict(kind="stopped", why=str(e))
    except SystemExit:
        stopped = None
    except RuntimeError as e:
        if str(e) == "NO_TCL":
            json.dump(dict(format="steltic.opensees_record", version=1, error="NO_TCL"), open(out, "w"))
            return 3
        stopped = dict(kind="error", why=str(e)[:3000])
    except BaseException as e:                                     # noqa: BLE001 -- the user's script failed
        tb = traceback.extract_tb(e.__traceback__)
        where = next((f for f in reversed(tb) if os.path.abspath(f.filename) == path), tb[-1] if tb else None)
        stopped = dict(kind="error", why="%s: %s" % (type(e).__name__, e),
                       where=("%s line %s" % (os.path.basename(where.filename), where.lineno)) if where else "")
    res = rec.result(stopped)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
