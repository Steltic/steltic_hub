"""The ten variation categories, with generic variation templates.

Generalised from a 100-model study of one tower: nothing here names a grid, a level or a
member size that a brief might not define. The templates do three jobs -- they are the
offline (MOCK) plan, they are the examples the LLM is shown so its variations are of this
kind, and they document what each category means to the user.
"""
from __future__ import annotations

CATEGORIES = [
    {"id": "problem", "label": "Establish the problem",
     "blurb": "Site, analysis procedure, redundancy, drift and comfort targets — what the reference design is up against.",
     "templates": [
         ("Benchmark: moderate-seismic site", "Change the site to moderate seismicity (SDC C-level SDS/SD1); everything else identical.", "Quantifies the high-seismic premium"),
         ("ELF instead of modal response spectrum", "Use the equivalent lateral force procedure; if not permitted for this height/period, stop and cite the clause.", "Analysis-procedure gate"),
         ("Redundancy factor forced to 1.3", "Take rho = 1.3 without demonstrating 12.3.4.2.", "Cost of skipping the redundancy check"),
         ("Site-specific hazard assumed", "Assume a Chapter 21 site-specific study reduces SDS and SD1 to the 80 % floor.", "Is a hazard study worth its fee in steel?"),
         ("Drift target 80 % of the limit", "Design to a story drift no greater than 80 % of the code allowable.", "Stiffness margin for nonlinear checks"),
         ("Drift target 60 % of the limit", "Design to a story drift no greater than 60 % of the code allowable.", "Stiffness margin for nonlinear checks"),
         ("Wind comfort relaxed", "Relax the wind-comfort acceleration target (seismic unchanged).", "Does wind serviceability still touch any member?"),
     ]},
    {"id": "core", "label": "Core / braced-frame configuration",
     "blurb": "Brace pattern, extent and placement of the braced core; extra braced lines; vertical combinations.",
     "templates": [
         ("Two-story X braces", "Configure the braced core as two-story X bracing.", "Tier behaviour, brace tonnage"),
         ("Single-story X braces", "Configure the braced core as single-story X bracing.", "Brace count vs unbalanced force"),
         ("Single diagonal, alternating", "Use single diagonal braces alternating direction story by story.", "Fewest braces; unbalanced force removed"),
         ("Core extended in the weak direction", "Extend the braced core to the full building depth in the weak direction.", "Weak-direction stiffness"),
         ("Core extended in the strong direction", "Extend the braced core to the full building length in the strong direction.", "Strong-direction stiffness"),
         ("Third braced line each direction", "Add one more braced line in each principal direction.", "Redundancy; rho = 1.0 assured"),
         ("Core relocated to one end", "Move the braced core to one end of the plan.", "Eccentric core: torsional irregularity and Ax"),
         ("Two half-cores at the ends", "Split the core into two half-cores at opposite ends of the plan.", "Torsional stiffness from separated cores"),
         ("Zipper columns at chevron mid-spans", "Add zipper columns at the chevron brace mid-spans (AISC 341 zipper-braced frame detailing).", "Unbalanced-force redistribution over height"),
         ("Braced core in one direction only", "Brace the core in the weak direction only; resist the strong direction with the perimeter moment frames alone.", "Per-direction system bookkeeping"),
         ("Braces terminate below the top floors", "Terminate the core braces several stories below the roof; moment frames alone above (vertical combination, 12.2.3.1).", "Open upper floors — penalties?"),
     ]},
    {"id": "system", "label": "Lateral system type",
     "blurb": "The seismic force-resisting system: SMF, SCBF, BRBF, EBF, SPSW, composite walls, dual systems; height-limit gates.",
     "templates": [
         ("Dual SMF + BRBF", "Use a dual system with buckling-restrained braced frames in the core (R = 8, Cd = 5, Om0 = 2.5).", "Higher R, softer core"),
         ("Dual SMF + EBF", "Use a dual system with eccentrically braced frames in the core (R = 8, Cd = 4, Om0 = 2.5), short links.", "Link-based ductility"),
         ("Dual SMF + SPSW", "Use a dual system with steel plate shear walls in the core (R = 8, Cd = 6.5, Om0 = 2.5).", "Wall-based core"),
         ("Dual SMF + composite plate shear wall", "Use a dual system with a concrete-filled composite plate shear wall core (C-PSW/CF, R = 8).", "SpeedCore-type core"),
         ("SCBF only", "Use special concentrically braced frames alone (R = 6); if a height limit applies, stop and cite the clause.", "Height-limit gate"),
         ("BRBF only", "Use buckling-restrained braced frames alone (R = 8); if a height limit applies, stop and cite the clause.", "Height-limit gate"),
         ("SMF only, all perimeter bays", "Use special moment frames alone on every perimeter bay (R = 8, no height limit).", "The pure moment-frame building"),
         ("SMF only, perimeter plus interior lines", "Use special moment frames on the perimeter and on interior column lines.", "Space-frame-like SMF"),
     ]},
    {"id": "perimeter", "label": "Perimeter frames",
     "blurb": "Which faces carry moment frames, spandrel depth, column orientation and section, share of base shear.",
     "templates": [
         ("Moment frames on the short faces only", "Place moment frames on the short faces only; long faces gravity.", "Which faces earn their connections"),
         ("Moment frames on the long faces only", "Place moment frames on the long faces only; short faces gravity.", "Which faces earn their connections"),
         ("Framed tube", "Close perimeter column spacing with deep spandrels and rigid connections on all faces (framed tube).", "Tube action"),
         ("Deeper spandrels", "Use the deepest practical spandrel beams on all faces.", "Depth for stiffness"),
         ("Spandrel depth capped", "Cap the spandrel depth to suit the window head.", "Architect's constraint"),
         ("Built-up box corner columns", "Use built-up box sections for the corner columns.", "Biaxial strong-column/weak-beam at corners"),
         ("Moment-frame columns strong axis in the weak direction", "Orient all moment-frame columns with their strong axis in the weak plan direction.", "Orientation lever"),
         ("Moment frames on the middle bays only", "Keep moment connections only on the middle bays of each face; pin the corner bays.", "Fewer connections"),
         ("Moment-frame share 40 %", "Size the moment frames for 40 % of the design base shear (dual system).", "Share tuning"),
         ("Moment-frame share 25 % minimum", "Size the moment frames for exactly the 25 % minimum (dual system); the core carries the rest.", "Minimum moment frame"),
     ]},
    {"id": "outriggers", "label": "Outriggers and belt trusses",
     "blurb": "Outrigger and belt levels, fused outriggers.",
     "templates": [
         ("Outriggers at the roof", "Add outrigger trusses core-to-perimeter at the roof.", "Roof outrigger"),
         ("Outriggers at about two-thirds height", "Add outrigger trusses core-to-perimeter at about 0.65 H.", "Optimum single outrigger"),
         ("Two outrigger levels", "Add outrigger trusses at mid-height and at the roof.", "Two-level outrigger"),
         ("Belt trusses only", "Add belt trusses at mid-height and at the roof (no outriggers).", "Belt action"),
         ("Fused outriggers", "Add outriggers at about 0.65 H with buckling-restrained diagonals as fuses.", "Capacity-protected outrigger"),
         ("Outrigger plus belt at one level", "Add outriggers and a belt truss at the same level near 0.65 H.", "Combined"),
     ]},
    {"id": "geometry", "label": "Geometry and mass",
     "blurb": "Story height, story count, plan proportions, bay layout, floor and cladding weight, lobby, setbacks, penthouse, basement.",
     "templates": [
         ("Story height reduced", "Reduce the typical story height by about 5 %.", "Height lever"),
         ("Story height increased", "Increase the typical story height by about 5 %.", "Height lever"),
         ("Fewer stories", "Remove two stories.", "Height lever"),
         ("More stories", "Add two stories.", "Height lever; residual-drift rule if it crosses 240 ft"),
         ("Narrower plan", "Reduce the plan width in the weak direction by about 12 %.", "Slenderness"),
         ("Longer plan", "Increase the plan length in the strong direction by about 25 %.", "Aspect ratio"),
         ("Column-free interior", "Remove the interior column line in the weak direction (two wider bays).", "Column-free units"),
         ("Lightweight concrete floors", "Use lightweight concrete floors (reduce floor dead load by about 15 psf).", "Mass down: seismic down, wind comfort worse"),
         ("Heavier slab", "Use a thicker normal-weight slab (add about 20 psf dead load).", "Mass up"),
         ("Heavy cladding", "Use precast cladding at about 30 psf.", "Mass up at the perimeter"),
         ("Taller lobby", "Increase the ground-floor height for a lobby.", "Soft-story screen"),
         ("Setback at the top floors", "Reduce the top four floors to about 75 % of the plan.", "Vertical irregularity / mass step"),
         ("Mechanical penthouse", "Add a framed mechanical penthouse on the core at the roof.", "Appendage"),
         ("One basement level", "Add one basement level with the lateral system continuous to it and a backstay at grade.", "Base definition and backstay effects"),
     ]},
    {"id": "members", "label": "Members and materials",
     "blurb": "Column family and grade, brace shapes, splice rhythm, beam standardisation.",
     "templates": [
         ("Columns from a shallower family", "Choose all columns from a shallower wide-flange family.", "Column depth lever"),
         ("Columns from a deeper family", "Choose all columns from a deeper wide-flange family (respect deep-column ductility limits).", "Column depth lever"),
         ("Built-up box core columns", "Use built-up box sections for the core columns.", "Overturning axial"),
         ("Higher-strength columns", "Use ASTM A913 Grade 65 for all columns.", "Material lever"),
         ("Grade 70 columns", "Use ASTM A913 Grade 70 for columns (verify AISC 341 A3.1).", "Material lever"),
         ("W-shape braces", "Use W-shapes instead of HSS for braces.", "Brace shape"),
         ("Round HSS braces", "Use round HSS for braces.", "b/t vs KL/r trade"),
         ("Built-up box braces at the base", "Use built-up box braces at the lower stories.", "Heavy-brace zone"),
         ("Column splices every three stories", "Change column sizes every three stories.", "Splice rhythm"),
         ("Constant-depth moment-frame beams", "Use one beam depth for all moment-frame beams at all levels.", "Connection standardisation"),
     ]},
    {"id": "bases", "label": "Bases and foundation stance",
     "blurb": "Pinned or fixed bases, grade beams, foundation-flexibility envelope.",
     "templates": [
         ("All bases pinned", "Pin all lateral-system column bases.", "Base condition"),
         ("Moment-frame bases pinned, braced-frame bases fixed", "Pin the moment-frame column bases; fix the braced-frame column bases.", "Mixed base condition"),
         ("Fixed bases with grade beams", "Fix all bases and tie the core columns with grade beams (uplift shared).", "Grade-beam tie"),
         ("Foundation-flexibility envelope", "Report pinned and fixed results as an envelope (12.13.3 scoped).", "Sensitivity, not a design"),
     ]},
    {"id": "seismic", "label": "Seismic design choices within the system",
     "blurb": "Strong-column ratios, connection types, doubler avoidance, brace slenderness, chevron beam margin, brace sizing rhythm, capacity-limited columns, collectors, torsion.",
     "templates": [
         ("SCWB ratio at least 1.5", "Require a strong-column/weak-beam ratio of at least 1.5 at every moment-frame joint.", "Mechanism control for nonlinear checks"),
         ("SCWB ratio at least 2.0", "Require a strong-column/weak-beam ratio of at least 2.0 at every moment-frame joint.", "Mechanism control"),
         ("WUF-W connections", "Use welded unreinforced flange-welded web connections instead of RBS.", "Full beam stiffness; panel-zone demand"),
         ("Bolted flange plate connections", "Use bolted flange plate moment connections.", "Field-bolted option"),
         ("No doubler plates", "Choose columns so that no panel-zone doubler plates are needed.", "Fabrication simplicity"),
         ("Stocky braces", "Size braces for a slenderness KL/r of about 50.", "Post-buckling reserve"),
         ("Slender braces", "Size braces for a slenderness KL/r of about 150 (not exceeding 200).", "Light braces"),
         ("Chevron beam margin", "Size chevron beams with a 1.25 factor on the unbalanced brace force.", "Beam robustness"),
         ("Brace sizes per story", "Change brace sizes every story (minimum weight).", "Minimum weight"),
         ("Brace sizes per four-story tier", "Use one brace size per four-story tier.", "Standardisation"),
         ("Conservative capacity-limited columns", "Design core columns to the full expected-strength sum of the braces with no 12.4.3 reduction.", "Conservative column chain"),
         ("Collectors to both bases", "Design collectors to the larger of the overstrength (Om0) and brace expected-strength forces; report both.", "Collector basis"),
         ("Accidental torsion with amplification", "Apply 5 % accidental torsion with Ax amplification where a torsional irregularity triggers it.", "Torsion bookkeeping"),
     ]},
    {"id": "devices", "label": "Devices",
     "blurb": "Dampers, base isolation, tuned mass dampers — scoped in the design, not verifiable with the current nonlinear tools.",
     "templates": [
         ("Fluid viscous dampers", "Add fluid viscous dampers in the core bays (Chapter 18), effective damping 10 % for strength.", "Supplemental damping"),
         ("Base isolation", "Base-isolate the building (Chapter 17); design the superstructure to the reduced demands.", "Isolation"),
         ("Tuned mass damper", "Add a tuned mass damper for wind comfort only (seismic unchanged).", "Wind comfort"),
         ("Buckling-restrained braces as damping devices", "Use buckling-restrained core braces with Chapter 18 damping credit scoped.", "Device-credited BRBs"),
     ]},
]

# systems the current nonlinear tools can represent (finalist eligibility); everything else is
# listed separately as "not verifiable with the current nonlinear tools"
REPRESENTABLE = ("SMF", "SCBF", "dual SMF + SCBF", "R = 3 X-braced")
NOT_REPRESENTABLE_WORDS = ("BRB", "buckling-restrained", "EBF", "eccentric", "SPSW", "plate shear wall",
                           "C-PSW", "composite plate", "isolat", "damper", "tuned mass", "TMD", "viscous")


def category(cid: str) -> dict | None:
    return next((c for c in CATEGORIES if c["id"] == cid), None)


def offline_plan(n: int, categories: list[str] | None = None, instructions: str = "") -> list[dict]:
    """A deterministic plan for MOCK / no-LLM use: M001 is the base, then templates round-robin
    over the chosen categories (all ten when none are chosen)."""
    chosen = [c for c in CATEGORIES if not categories or c["id"] in categories] or CATEGORIES
    out = [{"id": "M001", "title": "Base design as briefed", "group": "reference",
            "change": "", "why": "Reference"}]
    pools = [list(c["templates"]) for c in chosen]
    labels = [c["label"] for c in chosen]
    i = 0
    while len(out) < max(1, n) and any(pools):
        k = i % len(pools)
        if pools[k]:
            title, change, why = pools[k].pop(0)
            out.append({"id": f"M{len(out) + 1:03d}", "title": title, "group": labels[k],
                        "change": change, "why": why})
        i += 1
    return out[:max(1, n)]


def examples_for_prompt(categories: list[str] | None = None, per: int = 4) -> str:
    chosen = [c for c in CATEGORIES if not categories or c["id"] in categories] or CATEGORIES
    lines = []
    for c in chosen:
        lines.append(f"- {c['label']}: {c['blurb']}")
        for title, change, why in c["templates"][:per]:
            lines.append(f"    * {title} -- {change} (why: {why})")
    return "\n".join(lines)
