"""Scores, eligibility and rankings.

The score is a formula over the metrics of each variation. The default is the study's own:

    S = 0.35*(1 - modelled_cost/modelled_cost_max) + 0.20*drift_margin
      + 0.15*(1 - drift_concentration_ratio/2.0) + 0.15*(net_value/net_value_max)
      + 0.15*(1 - n_moment_conn/n_moment_conn_max)

Any metric name may be used; `<name>_max` / `<name>_min` are taken over the eligible rows. The
expression is evaluated with a small AST walker -- names, numbers, + - * / ** unary minus, min,
max, abs, sqrt, parentheses -- never with eval().
"""
from __future__ import annotations
import ast, math, re

DEFAULT_EQUATION = ("S = 0.35*(1 - modelled_cost/modelled_cost_max) + 0.20*drift_margin "
                    "+ 0.15*(1 - drift_concentration_ratio/2.0) + 0.15*(net_value/net_value_max) "
                    "+ 0.15*(1 - n_moment_conn/n_moment_conn_max)")

METRICS = [
    # (name, description, unit)
    ("modelled_cost", "steel tonnage x rate + moment connections x rate + braces x rate", "$"),
    ("revenue_proxy", "gross floor area x revenue rate", "$"),
    ("net_value", "revenue_proxy - modelled_cost", "$"),
    ("steel_tons", "structural steel from the member schedule", "ton"),
    ("steel_psf", "steel weight per square foot of gross floor area", "psf"),
    ("floor_area_sf", "gross floor area, all levels", "sf"),
    ("drift_utilisation", "max design story drift / allowable", "-"),
    ("drift_margin", "1 - drift_utilisation", "-"),
    ("drift_max_pct", "max design story drift, Cd*de/Ie", "%"),
    ("drift_concentration_ratio", "max story drift / mean story drift", "-"),
    ("wind_drift_utilisation", "max wind story drift / h/400", "-"),
    ("dc_max", "highest member demand/capacity ratio", "-"),
    ("n_over", "members or connections with D/C > 1.0", "count"),
    ("n_moment_conn", "moment connections (framework count, LLM-read when not in the package)", "count"),
    ("n_braces", "braces in the member schedule", "count"),
    ("n_columns", "columns in the member schedule", "count"),
    ("n_beams", "beams in the member schedule", "count"),
    ("V_kip", "design seismic base shear", "kip"),
    ("W_kip", "seismic weight", "kip"),
    ("Cs", "seismic response coefficient", "-"),
    ("T1_s", "fundamental period", "s"),
    ("wind_V_kip", "larger wind base shear", "kip"),
    ("torsion_Ax", "accidental torsion amplification", "-"),
    ("rho", "redundancy factor", "-"),
    ("scwb_ratio", "strong-column / weak-beam ratio (reported)", "-"),
    ("smf_share_pct", "moment-frame share of base shear (dual systems)", "%"),
    ("wind_comfort_mg", "peak wind acceleration if reported", "milli-g"),
]
METRIC_NAMES = [m[0] for m in METRICS]

DEFAULT_ELIGIBILITY = {
    "require_done": True,
    "require_checks_pass": True,             # dc_max <= 1.0 and n_over == 0
    "drift_utilisation_max": 1.0,
    "smf_share_min_pct": 25.0,                # dual systems only; null share = not checked
    "require_rho_ax_applied": True,           # rho and Ax present in the package
    "no_extreme_torsion": True,               # no Type 1b
    "wind_comfort_max_mg": 15.0,              # only when reported
    "representable_only": True,               # SMF / SCBF / dual SMF+SCBF / R=3 X-braced
}

DEFAULT_RATES = {"steel_per_ton": 4500.0, "per_moment_conn": 6000.0, "per_brace": 2500.0,
                 "revenue_per_sf": 400.0}


class EquationError(ValueError):
    pass


_ALLOWED_FUNCS = {"min": min, "max": max, "abs": abs, "sqrt": math.sqrt, "log": math.log, "exp": math.exp}


def normalise_equation(text: str) -> str:
    """'S = ...' or a bare expression -> the expression. Accepts the unicode dot and x for times."""
    t = (text or "").strip()
    t = t.replace("·", "*").replace("×", "*").replace("−", "-").replace("–", "-").replace("^", "**")
    t = re.sub(r"^\s*[A-Za-z_]\w*\s*=\s*", "", t)          # strip "S ="
    return t.strip()


def looks_like_equation(text: str) -> bool:
    t = normalise_equation(text)
    if not t or "\n" in t.strip():
        return False
    try:
        tree = ast.parse(t, mode="eval")
    except SyntaxError:
        return False
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    ok = set(METRIC_NAMES) | {f"{m}_max" for m in METRIC_NAMES} | {f"{m}_min" for m in METRIC_NAMES} | set(_ALLOWED_FUNCS)
    return bool(names) and names <= ok


def compile_equation(text: str):
    """-> (expr_ast, names_used). Raises EquationError with a readable message."""
    t = normalise_equation(text)
    if not t:
        raise EquationError("empty equation")
    try:
        tree = ast.parse(t, mode="eval")
    except SyntaxError as e:
        raise EquationError(f"not a valid expression: {e.msg} at position {e.offset}")
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
                raise EquationError("only min(), max(), abs(), sqrt(), log(), exp() may be called")
        elif isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Load,
                               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.USub, ast.UAdd, ast.IfExp,
                               ast.Compare, ast.Gt, ast.Lt, ast.GtE, ast.LtE, ast.Eq, ast.NotEq, ast.BoolOp,
                               ast.And, ast.Or)):
            continue
        else:
            raise EquationError(f"'{type(node).__name__}' is not allowed in a score equation")
    base = set(METRIC_NAMES) | set(_ALLOWED_FUNCS)
    for n in names:
        stem = n[:-4] if n.endswith(("_max", "_min")) else n
        if n not in base and stem not in METRIC_NAMES:
            raise EquationError(f"unknown metric '{n}' (metrics: {', '.join(METRIC_NAMES)})")
    return tree, names


def _eval(node, env: dict):
    if isinstance(node, ast.Expression):
        return _eval(node.body, env)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return float(node.value)
        raise EquationError("only numbers are allowed as constants")
    if isinstance(node, ast.Name):
        if node.id in _ALLOWED_FUNCS:
            return _ALLOWED_FUNCS[node.id]
        v = env.get(node.id)
        if v is None:
            raise KeyError(node.id)
        return float(v)
    if isinstance(node, ast.BinOp):
        a, b = _eval(node.left, env), _eval(node.right, env)
        if isinstance(node.op, ast.Add): return a + b
        if isinstance(node.op, ast.Sub): return a - b
        if isinstance(node.op, ast.Mult): return a * b
        if isinstance(node.op, ast.Div): return a / b if b != 0 else float("nan")
        if isinstance(node.op, ast.Pow): return a ** b
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, env)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.Call):
        f = _ALLOWED_FUNCS[node.func.id]
        return float(f(*[_eval(a, env) for a in node.args]))
    if isinstance(node, ast.IfExp):
        return _eval(node.body, env) if _eval(node.test, env) else _eval(node.orelse, env)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, env)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, env)
            ok = {ast.Gt: left > right, ast.Lt: left < right, ast.GtE: left >= right, ast.LtE: left <= right,
                  ast.Eq: left == right, ast.NotEq: left != right}[type(op)]
            if not ok:
                return 0.0
            left = right
        return 1.0
    if isinstance(node, ast.BoolOp):
        vals = [_eval(v, env) for v in node.values]
        return float(all(vals) if isinstance(node.op, ast.And) else any(vals))
    raise EquationError(f"unsupported expression element {type(node).__name__}")


def cost_metrics(m: dict, rates: dict) -> dict:
    r = {**DEFAULT_RATES, **(rates or {})}
    out = dict(m)
    tons, mc, br, area = m.get("steel_tons"), m.get("n_moment_conn"), m.get("n_braces"), m.get("floor_area_sf")
    if tons is not None:
        out["modelled_cost"] = (tons * r["steel_per_ton"] + (mc or 0) * r["per_moment_conn"]
                                + (br or 0) * r["per_brace"])
    else:
        out["modelled_cost"] = None
    out["revenue_proxy"] = area * r["revenue_per_sf"] if area is not None else None
    out["net_value"] = (out["revenue_proxy"] - out["modelled_cost"]
                        if out["revenue_proxy"] is not None and out["modelled_cost"] is not None else None)
    return out


def eligibility(row: dict, rules: dict) -> list[str]:
    """-> reasons the row is NOT eligible (empty list = eligible). Missing numbers count as 'not
    shown', which fails only the rules that need them."""
    r = {**DEFAULT_ELIGIBILITY, **(rules or {})}
    m = row.get("metrics") or {}
    why = []
    if r.get("require_done") and row.get("status") != "done":
        why.append({"failed": "run failed", "np": "not permitted", "paused": "run paused", "pending": "not designed yet",
                    "running": "still running"}.get(row.get("status"), f"status {row.get('status')}"))
    if r.get("require_checks_pass"):
        if m.get("dc_max") is not None and m["dc_max"] > 1.0:
            why.append(f"a member/connection D/C of {m['dc_max']:.2f} exceeds 1.0")
        if (m.get("n_over") or 0) > 0:
            why.append(f"{m['n_over']} check(s) fail")
    du = m.get("drift_utilisation")
    if r.get("drift_utilisation_max") is not None and du is not None and du > r["drift_utilisation_max"]:
        why.append(f"drift utilisation {du:.2f} > {r['drift_utilisation_max']}")
    share = m.get("smf_share_pct")
    if r.get("smf_share_min_pct") is not None and m.get("is_dual") and share is not None and share < r["smf_share_min_pct"]:
        why.append(f"moment-frame share {share:.0f}% < {r['smf_share_min_pct']:.0f}%")
    if r.get("require_rho_ax_applied") and row.get("status") == "done":
        if m.get("rho") is None:
            why.append("rho not shown in the package")
        if m.get("torsion_Ax") is None:
            why.append("Ax not shown in the package")
    if r.get("no_extreme_torsion") and str(m.get("torsion_class") or "").lower().startswith(("1b", "extreme")):
        why.append("Type 1b extreme torsional irregularity")
    wc = m.get("wind_comfort_mg")
    if r.get("wind_comfort_max_mg") is not None and wc is not None and wc > r["wind_comfort_max_mg"]:
        why.append(f"wind comfort {wc:.0f} mg > {r['wind_comfort_max_mg']:.0f} mg")
    if r.get("representable_only") and m.get("representable") is False:
        why.append("not verifiable with the current nonlinear tools (" + (m.get("system") or "system") + ")")
    return why


def score_rows(rows: list[dict], equation: str, rules: dict, rates: dict) -> dict:
    """Annotate every row with cost metrics, eligibility, score and ranks."""
    tree, names = compile_equation(equation)
    for row in rows:
        row["metrics"] = cost_metrics(row.get("metrics") or {}, rates)
        row["ineligible"] = eligibility(row, rules)
        row["eligible"] = not row["ineligible"]
    elig = [r for r in rows if r["eligible"]]
    agg: dict = {}
    for n in names:
        if n.endswith("_max") or n.endswith("_min"):
            stem, fn = n[:-4], (max if n.endswith("_max") else min)
            vals = [r["metrics"].get(stem) for r in elig if isinstance(r["metrics"].get(stem), (int, float))]
            agg[n] = fn(vals) if vals else None
    for row in rows:
        row["score"] = None
        row["score_note"] = ""
        if not row["eligible"]:
            continue
        env = {**{k: v for k, v in row["metrics"].items() if isinstance(v, (int, float))}, **agg}
        try:
            s = _eval(tree, env)
            row["score"] = None if (s != s) else round(float(s), 4)     # NaN -> None
            if row["score"] is None:
                row["score_note"] = "division by zero in the equation"
        except KeyError as e:
            row["score_note"] = f"{e.args[0]} not available for this variation"
        except Exception as e:
            row["score_note"] = f"{type(e).__name__}: {e}"
    scored = sorted([r for r in rows if r["score"] is not None], key=lambda r: -r["score"])
    for i, r in enumerate(scored, 1):
        r["rank"] = i
    for r in rows:
        r.setdefault("rank", None)
    rankings = {}
    for key, reverse in (("modelled_cost", False), ("steel_psf", False), ("drift_margin", True),
                         ("drift_concentration_ratio", False), ("n_moment_conn", False), ("net_value", True)):
        have = [r for r in rows if isinstance(r["metrics"].get(key), (int, float))]
        rankings[key] = [r["id"] for r in sorted(have, key=lambda r: r["metrics"][key], reverse=reverse)]
    return {"equation": normalise_equation(equation), "names": sorted(names), "aggregates": agg,
            "rankings": rankings, "n_eligible": len(elig), "n_scored": len(scored)}
