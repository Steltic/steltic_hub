"""Two-story steel building in OpenSeesPy, kip-ft, Z up -- plain Python (no numpy), for the always-run tests.

2 x 1 bays (30 ft, 25 ft), stories 14 ft and 12 ft. Moment frames along X on both long sides (W24X94 beams,
W14X90 columns, strong axis resisting X); gravity beams along Y (W18X40) pinned about their major axis with
-releasey. Rigid diaphragm per floor, masses on the master nodes, a gravity pattern, an eigen analysis -- then the
script wipes and builds a one-column check model, which the reader must ignore (it keeps the largest model).
"""
from openseespy.opensees import *          # noqa: F401,F403 -- the usual OpenSeesPy style

IN2, IN4 = 1.0 / 144.0, 1.0 / 20736.0
E = 29000.0 * 144.0                         # ksf
G = 11200.0 * 144.0
W14X90 = dict(A=26.5 * IN2, Ix=999.0 * IN4, Iy=362.0 * IN4, J=4.06 * IN4)
W24X94 = dict(A=27.7 * IN2, Ix=2700.0 * IN4, Iy=109.0 * IN4, J=5.26 * IN4)
W18X40 = dict(A=11.8 * IN2, Ix=612.0 * IN4, Iy=19.1 * IN4, J=0.81 * IN4)
xs, ys, zs = [0.0, 30.0, 60.0], [0.0, 25.0], [0.0, 14.0, 26.0]


def nid(i, j, k):
    return 100 * k + 10 * i + j + 1


def building():
    wipe()
    model("basic", "-ndm", 3, "-ndf", 6)
    for k, z in enumerate(zs):
        for i, x in enumerate(xs):
            for j, y in enumerate(ys):
                node(nid(i, j, k), x, y, z)
    for i in range(len(xs)):
        for j in range(len(ys)):
            fix(nid(i, j, 0), 1, 1, 1, 1, 1, 1)
    geomTransf("PDelta", 1, 0.0, 1.0, 0.0)        # columns: local z = global Y, the strong axis resists X
    geomTransf("Linear", 2, 0.0, 0.0, 1.0)        # beams: vecxz vertical, so local z is vertical and Iy is major
    e = 0
    for k in range(1, len(zs)):
        for i in range(len(xs)):
            for j in range(len(ys)):
                e += 1
                element("elasticBeamColumn", e, nid(i, j, k - 1), nid(i, j, k), W14X90["A"], E, G, W14X90["J"],
                        W14X90["Iy"], W14X90["Ix"], 1)
        for j in range(len(ys)):                  # moment-frame beams along X
            for i in range(len(xs) - 1):
                e += 1
                element("elasticBeamColumn", e, nid(i, j, k), nid(i + 1, j, k), W24X94["A"], E, G, W24X94["J"],
                        W24X94["Ix"], W24X94["Iy"], 2)
        for i in range(len(xs)):                  # gravity beams along Y, pinned about the major axis (local y)
            e += 1
            element("elasticBeamColumn", e, nid(i, 0, k), nid(i, 1, k), W18X40["A"], E, G, W18X40["J"],
                    W18X40["Ix"], W18X40["Iy"], 2, "-releasey", 3)
    for k in range(1, len(zs)):
        m = 9000 + k
        node(m, 30.0, 12.5, zs[k])
        fix(m, 0, 0, 1, 1, 1, 0)
        rigidDiaphragm(3, m, *[nid(i, j, k) for i in range(len(xs)) for j in range(len(ys))])
        w = 300.0 if k < len(zs) - 1 else 200.0
        mass(m, w / 32.174, w / 32.174, 0.0, 0.0, 0.0, w / 32.174 * (60.0 ** 2 + 25.0 ** 2) / 12.0)
    return e


n_ele = building()
timeSeries("Linear", 1)
pattern("Plain", 1, 1)
for t in range(1, n_ele + 1):
    a, b = eleNodes(t)
    if nodeCoord(a)[2] == nodeCoord(b)[2] and nodeCoord(a)[1] == nodeCoord(b)[1]:
        eleLoad("-ele", t, "-type", "-beamUniform", 0.0, -1.0)   # 1 kip/ft down on the X beams
constraints("Transformation"); numberer("RCM"); system("UmfPack")
test("NormDispIncr", 1.0e-8, 10); algorithm("Newton"); integrator("LoadControl", 0.1); analysis("Static")
ok = analyze(10)
periods = [6.283185307 / l ** 0.5 for l in eigen(2)]
print("gravity", ok, "T", periods)

# a one-column check model -- not the building
wipe()
model("basic", "-ndm", 3, "-ndf", 6)
node(1, 0.0, 0.0, 0.0); node(2, 0.0, 0.0, 14.0)
fix(1, 1, 1, 1, 1, 1, 1)
geomTransf("Linear", 1, 0.0, 1.0, 0.0)
element("elasticBeamColumn", 1, 1, 2, W14X90["A"], E, G, W14X90["J"], W14X90["Iy"], W14X90["Ix"], 1)
