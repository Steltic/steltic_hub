"""The random variables of the study and the Monte Carlo sampler.

The study asks whether the building AS BUILT -- not quite plumb, not quite the tabulated
section, not quite the handbook stiffness -- still satisfies the design code's member checks
with the design's own capacities. Only quantities that change the ELASTIC DEMANDS are sampled;
what enters the capacity side (Fᵧ, residual stresses, member out-of-straightness) is
deliberately held at the design values (see NOT_VARIED).

Every variable carries its default distribution, the level it is sampled at (section group,
story, building) and where the default comes from. Everything is editable from the UI; the
defaults are a documented starting point, not a claim that they suit every building.
"""
from __future__ import annotations
import math
import numpy as np

NOMINAL_E = 29000.0

VARIABLES = [
    {"id": "E", "label": "Modulus of elasticity E", "level": "building", "kind": "scale", "dist": "lognormal",
     "mean": 1.00, "cov": 0.06, "unit": "× 29,000 ksi", "enabled": True,
     "what": "One draw for the whole building. Changes every member stiffness, so the period, the ELF base shear (where the period governs Cₛ), "
             "the P-Δ amplification and the design drifts move with it.",
     "source": "Galambos & Ravindra (1978), Properties of steel for use in LRFD, ASCE J. Struct. Div. 104(ST9): E mean 1.00 × nominal, COV 0.06."},
    {"id": "thk", "label": "Plate thickness (fabrication)", "level": "group", "kind": "scale", "dist": "lognormal",
     "mean": 1.00, "cov": 0.05, "unit": "× nominal tᶠ, tᵥ", "enabled": True,
     "what": "Flange and web thickness per section group (same section = same rolling): A and I scale with it, J with its cube. "
             "Alters the relative stiffness of the members, so forces redistribute between frames and between beams and columns.",
     "source": "Galambos & Ravindra (1978): fabrication factor on A and Z, mean 1.00, COV 0.05 (rolling tolerance, ASTM A6)."},
    {"id": "psi", "label": "Story out-of-plumb ψ", "level": "story", "kind": "abs", "dist": "normal",
     "mean": 0.0, "std": 1.0 / 1000.0, "unit": "rad, per story, X and Y", "enabled": True,
     "what": "Lean of each story in X and in Y, independent draws, accumulated up the height -- a random lean profile in the analysis "
             "geometry, so the gravity loads produce P-Δ sway moments the way they would in the erected frame (the design's notional-load "
             "or nominal-lean allowance is not removed).",
     "source": "Surveys: Beaulieu & Adams (1977, 1978); Lindner & Gietzelt (1984). Default σ = 1/1000 so that 2σ equals the H/500 erection "
               "tolerance (AISC Code of Standard Practice); mean zero. Replace with survey statistics for the fabricator in hand when available."},
    {"id": "dead", "label": "Dead load (mass and gravity)", "level": "building", "kind": "scale", "dist": "lognormal",
     "mean": 1.05, "cov": 0.10, "unit": "× nominal D", "enabled": False,
     "what": "One factor on the floor, roof and cladding dead load -- both the seismic weight and the gravity demands. OFF by default: "
             "the question asked here is about the structure as built under the code's nominal loads. Turn it on to see the load side too.",
     "source": "Ellingwood, Galambos, MacGregor & Cornell (1980), Development of a probability based load criterion for American National "
               "Standard A58, NBS SP 577: dead load mean 1.05 × nominal, COV 0.10."},
]
VAR_IDS = [v["id"] for v in VARIABLES]

NOT_VARIED = [
    ("Yield stress Fᵧ", "enters the AISC 360 capacities, which are held at the design values (the design's own φRₙ are reused, never recomputed)"),
    ("Residual stresses", "a capacity-side effect (column curve, plastic hinge onset); not part of an elastic LRFD demand model"),
    ("Member out-of-straightness δ₀", "covered on the capacity side by the column curve and B₁; a single elastic element per column cannot carry it"),
    ("Live, roof, wind and seismic load intensities", "the code's nominal values -- the question is whether the as-built structure still passes them"),
]

CLAMPS = {"E": (0.7, 1.3), "thk": (0.8, 1.2), "psi": (-1.0 / 100.0, 1.0 / 100.0), "dead": (0.6, 1.5)}


def variable(vid: str) -> dict:
    return next(v for v in VARIABLES if v["id"] == vid)


def merged(overrides: dict | None) -> list[dict]:
    """The variable table with the user's overrides (mean / cov / std / enabled) applied."""
    out = []
    o = overrides or {}
    for v in VARIABLES:
        d = dict(v)
        u = o.get(v["id"]) or {}
        for k in ("mean", "cov", "std", "enabled"):
            if k in u and u[k] is not None:
                d[k] = (bool(u[k]) if k == "enabled" else float(u[k]))
        out.append(d)
    return out


def _draw(rng, v: dict, size=None):
    lo, hi = CLAMPS[v["id"]]
    if v["dist"] == "normal":
        std = v.get("std")
        if std is None:
            std = abs(v["mean"]) * float(v.get("cov") or 0.0)
        x = rng.normal(v["mean"], std, size)
    else:
        m = float(v["mean"]); cov = float(v.get("cov") or 0.0)
        if m <= 0:
            raise ValueError(f"{v['id']}: a lognormal mean must be positive")
        s = math.sqrt(math.log(1.0 + cov * cov)) if cov > 0 else 0.0
        mu = math.log(m) - 0.5 * s * s
        x = rng.lognormal(mu, s, size)
    return np.clip(x, lo, hi)


def sample_realisation(rng, probe: dict, variables: list[dict]) -> dict:
    """One realisation: the numbers the worker substitutes into the analysis model."""
    on = {v["id"]: v for v in variables if v.get("enabled", True)}
    groups = {}
    if "thk" in on:
        for sec in probe.get("sections", []):
            groups[sec] = {"thk": float(_draw(rng, on["thk"]))}
    E = float(_draw(rng, on["E"])) * NOMINAL_E if "E" in on else NOMINAL_E
    lean = {}
    if "psi" in on:
        for k in range(1, int(probe.get("stories") or 1) + 1):
            lean[str(k)] = [float(_draw(rng, on["psi"])), float(_draw(rng, on["psi"]))]
    dead = float(_draw(rng, on["dead"])) if "dead" in on else 1.0
    summary = {"E": E, "E_factor": E / NOMINAL_E, "dead": dead}
    if groups:
        thks = [d["thk"] for d in groups.values()]
        summary["thk_mean"] = sum(thks) / len(thks); summary["thk_min"] = min(thks)
        for sec, d in groups.items():
            summary[f"thk[{sec}]"] = d["thk"]
    if lean:
        levels = probe.get("levels") or []
        hs = [b - a for a, b in zip(levels, levels[1:])] if len(levels) > 1 else [1.0] * len(lean)
        ox = sum(lean[str(k + 1)][0] * hs[k] for k in range(min(len(hs), len(lean))))
        oy = sum(lean[str(k + 1)][1] * hs[k] for k in range(min(len(hs), len(lean))))
        H = sum(hs) or 1.0
        summary["lean_top"] = math.hypot(ox, oy) / H            # resultant lean at the top, fraction of H
        summary["psi_max"] = max(max(abs(a), abs(b)) for a, b in lean.values())
    return {"groups": groups, "E": E, "lean": lean, "dead": dead, "summary": summary}


def build_spec(probe: dict, n: int, seed: int, options: dict, variables: list[dict]) -> dict:
    """realisations.json: id 0 = the nominal design model, 1..n = Monte Carlo draws."""
    rng = np.random.default_rng(int(seed))
    reals = [{"id": 0, "nominal": True}]
    for i in range(1, int(n) + 1):
        reals.append({"id": i, "nominal": False, "sample": sample_realisation(rng, probe, variables)})
    return {"version": 2, "seed": int(seed), "n": int(n), "options": options,
            "variables": [{k: v[k] for k in ("id", "dist", "mean", "cov", "std", "enabled", "level", "kind") if k in v} for v in variables],
            "realisations": reals}
