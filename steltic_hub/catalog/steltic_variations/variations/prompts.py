"""Prompts. Kept short and literal: the model is asked for JSON with named keys, nothing else."""
from __future__ import annotations
import json
from .library import examples_for_prompt
from .scoring import METRICS, METRIC_NAMES

PLAN_SYSTEM = """You are a senior structural engineer planning a design-variation study of a steel building.
The building is described by a BASE BRIEF. You must propose N design variations. Each variation is designed as a
separate job by an AISC 360/341 steel-design agent that reads brief text only, so every variation must be
expressible as a short instruction that changes ONLY what it states relative to the base brief and keeps
everything else identical. Never reference grid names, levels or member sizes that the base brief does not define.
No duplicates. Each variation carries a one-line 'why' (what question it answers).
M001 is always the base brief unchanged (title 'Base design as briefed', group 'reference', change '').
Where a variation is likely NOT PERMITTED by ASCE 7-22 / AISC 341-22 (height limits, analysis-procedure gates),
still include it when it answers a question, and say in 'why' that the design agent should confirm the clause.
Return ONLY JSON: {"variations": [{"id": "M001", "title": "...", "group": "...", "change": "...", "why": "..."}]}
Ids are M001, M002, ... in order. 'group' is the category label the variation belongs to."""


def plan_user(base_brief: str, n: int, mode: str, categories: list[str], instructions: str) -> str:
    parts = [f"BASE BRIEF:\n{base_brief.strip()}\n", f"N = {n} variations in total (including M001).\n"]
    if mode == "categories":
        parts.append("Draw the variations ONLY from these categories, spread evenly across them:\n" + examples_for_prompt(categories))
    elif mode == "instructions":
        parts.append("The designer's instructions for the study:\n" + instructions.strip() + "\n\n"
                     "For reference, the kinds of variation such studies use:\n" + examples_for_prompt(None, per=2))
    else:
        parts.append("Choose freely across all categories to give the widest useful spread for this building:\n" + examples_for_prompt(None, per=3))
    parts.append("\nReturn the JSON now.")
    return "\n".join(parts)


READ_SYSTEM = """You read a steel-design report and answer with numbers that are STATED in it. Never guess: use null
when the report does not state a value. Return ONLY JSON with these keys:
{"system_X": "SFRS in the X direction (short)", "system_Y": "...", "is_dual": true/false,
 "n_moment_conn": integer or null (total number of moment connections in the building; count from the frames'
   bays x levels x 2 ends when the report gives those, else null),
 "smf_share_pct": number or null (moment frames' share of base shear in a dual system),
 "wind_comfort_mg": number or null (peak wind acceleration in milli-g, if reported),
 "not_permitted": true/false (the report says the system/procedure is NOT PERMITTED),
 "np_clause": "clause cited" or "",
 "np_list": ["other scope items the report flags as not permitted or not verified"],
 "notes": "one sentence on anything a reviewer must know"}"""


def read_user(excerpt: str, metrics: dict) -> str:
    known = {k: metrics.get(k) for k in ("system", "system_declared", "drift_max_pct", "drift_limit_pct", "V_kip",
                                         "n_stories", "plan_x_ft", "plan_y_ft", "n_beams", "n_columns", "n_braces") if metrics.get(k) is not None}
    return f"Already extracted from the package (do not contradict):\n{json.dumps(known)}\n\nREPORT EXCERPT:\n{excerpt}\n\nReturn the JSON now."


SCORE_SYSTEM = """You turn a designer's wording about how design variations should be ranked into ONE score
equation. Higher score = better. Use ONLY these metric names (a higher value of each is not necessarily better --
read the description) and, for normalisation, <name>_max or <name>_min taken over the eligible variations:
""" + "\n".join(f"- {n}: {d} [{u}]" for n, d, u in METRICS) + """
Allowed operators: + - * / ** parentheses, min(), max(), abs(), sqrt(). Numbers only as constants.
Return ONLY JSON: {"equation": "S = ...", "explanation": "one or two sentences on the weights chosen"}"""


def score_user(text: str, default_equation: str) -> str:
    return (f"The default equation is:\n{default_equation}\n\nThe designer wrote:\n{text.strip()}\n\n"
            "If the text is already an equation, return it normalised. Otherwise translate the intent into an "
            "equation with weights that sum to about 1. Return the JSON now.")
