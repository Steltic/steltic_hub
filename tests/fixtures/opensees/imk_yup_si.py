"""3-D steel moment-frame building in OpenSeesPy -- N, mm, s; Y is up.
Concentrated plasticity: elastic beams between rotational IMK springs at the column faces, rigid offsets from the
column centreline to the face, gravity beams pinned with translational-only zeroLength links. 3 x 2 bays, 3 stories.
"""
import numpy as np
import openseespy.opensees as ops
import opsvis as opsv

ops.wipe()
ops.model("basic", "-ndm", 3, "-ndf", 6)
E = 200000.0                         # MPa
xs = np.array([0.0, 8000.0, 16000.0, 24000.0])
zs = np.array([0.0, 7000.0, 14000.0])
ys = np.array([0.0, 4500.0, 8100.0, 11700.0])

# W360X122 columns (W14X82), W610X125 frame beams (W24X84), W410X46 gravity beams (W16X31): A mm2, I mm4
col = dict(A=15500.0, Iz=365e6, Iy=61.6e6, J=2.43e6)
fbeam = dict(A=15900.0, Iz=986e6, Iy=39.3e6, J=1.54e6)
gbeam = dict(A=5890.0, Iz=156e6, Iy=5.14e6, J=0.192e6)
G = 77000.0

def tag(i, k, lv):
    return 1000 * lv + 10 * i + k

for lv, y in enumerate(ys):
    for i, x in enumerate(xs):
        for k, z in enumerate(zs):
            ops.node(tag(i, k, lv), float(x), float(y), float(z))
ops.fixY(0.0, 1, 1, 1, 1, 1, 1)

ops.geomTransf("PDelta", 1, 0.0, 0.0, 1.0)     # columns (local x = +Y): web along global X -> strong axis resists X
ops.geomTransf("Linear", 2, 0.0, 0.0, 1.0)     # beams along X: local y = global Y (up), Iz is the major axis
ops.geomTransf("Linear", 3, 1.0, 0.0, 0.0)     # beams along Z: local y = -global Y

ele = 1
for lv in range(1, len(ys)):
    for i in range(len(xs)):
        for k in range(len(zs)):
            ops.element("elasticBeamColumn", ele, tag(i, k, lv - 1), tag(i, k, lv), col["A"], E, G, col["J"], col["Iy"], col["Iz"], 1)
            ele += 1

# IMK spring material for the frame beams (My, theta_p etc. made up but realistic)
ops.uniaxialMaterial("Elastic", 90, 1.0e15)                   # rigid
ops.uniaxialMaterial("Elastic", 91, 1.0e-6)                   # free (pin)
ops.uniaxialMaterial("ModIMKPeakOriented", 92, 1.0e13, 0.0015, 0.0015, 1.2e9, -1.2e9, 1.0, 1.0, 1.0, 1.0,
                     1.0, 1.0, 1.0, 1.0, 0.025, 0.025, 0.18, 0.18, 0.4, 0.4, 0.2, 0.2, 1.0, 1.0)
node_id = 90000
half_col = 182.0                                              # half the column depth, mm

def frame_beam(a, b, axis):
    """a -> b along X (axis 0) at a moment frame: rigid offset, IMK spring, elastic beam, IMK spring, rigid offset."""
    global ele, node_id
    pa, pb = np.array(ops.nodeCoord(a)), np.array(ops.nodeCoord(b))
    u = (pb - pa) / np.linalg.norm(pb - pa)
    fa, fb = pa + u * half_col, pb - u * half_col
    n = []
    for p in (fa, fa, fb, fb):
        node_id += 1
        ops.node(node_id, *map(float, p))
        n.append(node_id)
    ops.element("elasticBeamColumn", ele, a, n[0], 1.0e6, E, G, 1.0e9, 1.0e10, 1.0e10, 2); ele += 1     # rigid offset
    ops.element("zeroLength", ele, n[0], n[1], "-mat", 90, 90, 90, 90, 92, 90, "-dir", 1, 2, 3, 4, 5, 6); ele += 1
    ops.element("elasticBeamColumn", ele, n[1], n[2], fbeam["A"], E, G, fbeam["J"], fbeam["Iy"], fbeam["Iz"], 2); ele += 1
    ops.element("zeroLength", ele, n[2], n[3], "-mat", 90, 90, 90, 90, 92, 90, "-dir", 1, 2, 3, 4, 5, 6); ele += 1
    ops.element("elasticBeamColumn", ele, n[3], b, 1.0e6, E, G, 1.0e9, 1.0e10, 1.0e10, 2); ele += 1

def gravity_beam(a, b):
    """a -> b, pinned at both ends: links that carry the translations and the beam's torsion, nothing else."""
    global ele, node_id
    along_x = abs(ops.nodeCoord(b)[0] - ops.nodeCoord(a)[0]) > 1.0
    torsion = 4 if along_x else 6
    n = []
    for c in (a, b):
        node_id += 1
        ops.node(node_id, *ops.nodeCoord(c))
        n.append(node_id)
        ops.element("zeroLength", ele, c, node_id, "-mat", 90, 90, 90, 90, "-dir", 1, 2, 3, torsion); ele += 1
    ops.element("elasticBeamColumn", ele, n[0], n[1], gbeam["A"], E, G, gbeam["J"], gbeam["Iy"], gbeam["Iz"],
                2 if along_x else 3); ele += 1

for lv in range(1, len(ys)):
    for k in range(len(zs)):
        for i in range(len(xs) - 1):
            if k in (0, len(zs) - 1):
                frame_beam(tag(i, k, lv), tag(i + 1, k, lv), 0)
            else:
                gravity_beam(tag(i, k, lv), tag(i + 1, k, lv))
    for i in range(len(xs)):
        for k in range(len(zs) - 1):
            gravity_beam(tag(i, k, lv), tag(i, k + 1, lv))

# floors: rigid diaphragm (perpendicular to Y), mass on every column node -- tonnes (N s2/mm)
for lv in range(1, len(ys)):
    master = 80000 + lv
    ops.node(master, 12000.0, float(ys[lv]), 7000.0)
    ops.fix(master, 0, 1, 0, 1, 0, 1)
    slaves = [tag(i, k, lv) for i in range(len(xs)) for k in range(len(zs))]
    ops.rigidDiaphragm(2, master, *slaves)
    for s in slaves:
        m = 25.0 if lv < len(ys) - 1 else 18.0
        ops.mass(s, m, 0.0, m, 0.0, 0.0, 0.0)

opsv.plot_model()

ops.timeSeries("Linear", 1)
ops.pattern("Plain", 1, 1)
for lv in range(1, len(ys)):
    for i in range(len(xs)):
        for k in range(len(zs)):
            ops.load(tag(i, k, lv), 0.0, -250000.0, 0.0, 0.0, 0.0, 0.0)
ops.constraints("Transformation"); ops.numberer("RCM"); ops.system("BandGeneral")
ops.test("NormDispIncr", 1e-8, 20); ops.algorithm("Newton"); ops.integrator("LoadControl", 0.1); ops.analysis("Static")
ops.analyze(10)
ops.loadConst("-time", 0.0)
lam = ops.eigen(3)
print("T =", [2 * np.pi / np.sqrt(l) for l in lam])

# NLRHA: reads a ground motion that is not shipped with the model
gm = np.loadtxt("RSN953_NORTHR_MUL009.AT2", skiprows=4).flatten()
ops.timeSeries("Path", 2, "-dt", 0.01, "-values", *gm, "-factor", 9806.65)
ops.pattern("UniformExcitation", 2, 1, "-accel", 2)
