"""Logic contributed like data. A METHOD is a declarative recipe: inputs the platform
already serves, ordered steps in a small expression language, results with units, a
citation and the contributor's own worked examples. The gate checks it and re-runs the
examples; every pack runs approved methods at the assemble seam (mcp/methods_step.py)
and cites the result, so the arithmetic in an answer is the platform's.

Never code. A step is parsed with `ast` and interpreted here node by node over numpy
arrays: only the nodes and functions below exist — no eval, no attribute access on
Python objects, no imports, no loops — and every number is a float64, so no input can
make the server compute without bound.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import re

import numpy as np

from ..mcp.budget import once

MAX_EXPR = 600            # characters per step
MAX_NODES = 300           # syntax nodes per step
MAX_DEPTH = 30
MAX_STEPS = 40
MAX_INPUTS = 8
MAX_IN_LIST = 200
MAX_EXAMPLES = 10
MAX_EXAMPLE_ROWS = 1000
MAX_ROWS = 200_000        # rows a method reads per input; more is said, never hidden
TOLERANCE = 0.001         # an example's result may differ from its expected value by 0.1%

AGGREGATES = ("count", "sum", "mean", "min", "max", "median", "weighted_mean")
ELEMENTWISE = ("log", "log10", "exp", "sqrt", "abs", "round", "clip", "where",
               "minimum", "maximum")
FUNCTIONS = AGGREGATES + ELEMENTWISE + ("field",)
SKILL = "servirplatform://skill/contribute-logic"

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_NULLS = (None, "", "NA", "N/A", "NaN", "nan", "null", "None")


class MethodError(ValueError):
    """A method the platform cannot check or run; the message says why, in words a
    contributor can act on."""


def _pack_ids() -> list[str]:
    from ..mcp import packs
    return sorted(packs.PACKS)


METHOD_FIELDS = {
    "required": {
        "method": "a short snake_case name, e.g. mangrove_restorable_above_70 — answers cite it",
        "title": "what a reader sees beside the result, in the contributor's own words",
        "description": "what it computes and why, in the contributor's own words",
        "packs": "the domain packs that run it, e.g. [land-cover] — any of the platform's packs",
        "inputs": "alias -> {feed: <dataset>} or {table: <dataset>}: platform data it reads "
                  "(feeds_query names them); the rows used are the ones about the area asked",
        "steps": "ordered name -> expression, e.g. {selected: 'units.pct_restorable > threshold', "
                 "n_units: 'count(selected)', restorable_ha: "
                 "'sum(units.restorable_area_ha, where=selected)'}. A field is <alias>.<field> "
                 "(field(<alias>, 'Any Name') for other names); + - * / ** %, comparisons, "
                 "and/or/not, 'a if c else b', and " + ", ".join(FUNCTIONS[:-1])
                 + " (aggregates take where=)",
        "outputs": "step -> {unit: ...}: the results — each one number for the area",
        "source": "where the logic comes from: paper, DOI, equation or table number",
        "license": "the licence of that source, e.g. CC-BY-4.0",
        "examples": "worked examples with known numbers, the contributor's own: "
                    "[{rows: {<alias>: [{<field>: <value>, ...}]}, params: {...} (optional), "
                    "expect: {<output>: <number>}}] — the gate re-runs each and declines on a "
                    "mismatch",
    },
    "optional": {
        "params": "name -> {value: <number>, unit: ...}: constants the steps use (a threshold, "
                  "an equation's coefficients)",
        "countries": "where the logic holds (an equation fitted in one region); elsewhere it "
                     "is not run, and the answer says why",
        "vintage": "the year of the source",
        "usage_notes": "a few lines the analyst reads with every result (max 500 chars)",
    },
    "guide": (f"Contributing logic: read {SKILL} (or the contribute_logic prompt). Ask for "
              "the logic in the person's own words, or suggest one or two rules for them to "
              "accept or reject; find each input with feeds_query or platform_capabilities; "
              "read the steps back in plain language and get a yes; ask for (or suggest) a "
              "worked example and a source line, and submit only what they accepted; then "
              "submit kind=method. Do not compute the result yourself "
              "in the chat: once approved, the platform computes it in every answer of its "
              "pack, and that figure is the one to quote."),
}

_KNOWN = set(METHOD_FIELDS["required"]) | set(METHOD_FIELDS["optional"])
_ATTESTED_HERE = {
    "title": "the contributor's own words",
    "description": "the contributor's own words",
    "source": "the contributor's citation, or for a rule you suggested 'proposed by the assistant, accepted by the contributor'",
    "license": "the licence the source states",
    "examples": "worked numbers the contributor has checked (you may suggest an example; they check its numbers)",
}


# --------------------------------------------------------------------------- checking

class _Shape:
    """What a sub-expression is, known before any data: one number for the area
    (scalar), one value per row of an input (rows), or an input itself (rowset)."""
    __slots__ = ("kind", "alias", "text")

    def __init__(self, kind, alias=None, text=False):
        self.kind, self.alias, self.text = kind, alias, text


_BIN = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply,
        ast.Div: np.true_divide, ast.Pow: np.power, ast.Mod: np.mod}
_ORDER = {ast.Gt: np.greater, ast.GtE: np.greater_equal, ast.Lt: np.less,
          ast.LtE: np.less_equal}
_EQ = {ast.Eq: np.equal, ast.NotEq: np.not_equal}
_ALLOWED_TEXT = ("arithmetic + - * / ** %, comparisons, and/or/not, 'a if c else b', "
                 "lists after 'in', and the functions " + ", ".join(FUNCTIONS))


class _Checker:
    def __init__(self, scope: dict, aliases: set, label: str):
        self.scope, self.aliases, self.label = scope, aliases, label
        self.nodes = 0

    def fail(self, why: str):
        raise MethodError(f"step {self.label!r}: {why}")

    def merge(self, shapes, what: str) -> _Shape:
        rows = {s.alias for s in shapes if s.kind == "rows"}
        if any(s.kind == "rowset" for s in shapes):
            self.fail(f"an input by itself can only be counted, e.g. count({next(s.alias for s in shapes if s.kind == 'rowset')}) — {what} needs one of its fields")
        if len(rows) > 1:
            self.fail(f"{what} mixes the rows of {sorted(rows)}; rows of two inputs do not line "
                      "up — reduce each to one number with an aggregate first")
        return _Shape("rows", rows.pop()) if rows else _Shape("scalar")

    def check(self, node, depth=0) -> _Shape:
        self.nodes += 1
        if self.nodes > MAX_NODES or depth > MAX_DEPTH:
            self.fail(f"too long — at most {MAX_NODES} parts and {MAX_DEPTH} levels of nesting")
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool) or isinstance(v, (int, float)):
                return _Shape("scalar")
            if isinstance(v, str):
                return _Shape("scalar", text=True)
            self.fail(f"{v!r} is not a number or text")
        if isinstance(node, ast.Name):
            if node.id.startswith("_"):
                self.fail(f"{node.id!r} is not a name a method can use")
            if node.id in self.aliases:
                return _Shape("rowset", node.id)
            if node.id in self.scope:
                return self.scope[node.id]
            near = difflib.get_close_matches(node.id, [*self.scope, *self.aliases], n=3)
            self.fail(f"unknown name {node.id!r} — not a parameter, an earlier step or an input"
                      + (f"; did you mean {', '.join(near)}?" if near else ""))
        if isinstance(node, ast.Attribute):
            if not (isinstance(node.value, ast.Name) and node.value.id in self.aliases):
                self.fail("a dotted name must be <input>.<field>, e.g. units.pct_restorable — "
                          f"inputs: {', '.join(sorted(self.aliases))}")
            if node.attr.startswith("_"):
                self.fail(f"{node.attr!r} is not a field a method can read")
            return _Shape("rows", node.value.id)
        if isinstance(node, ast.BinOp):
            if type(node.op) not in _BIN:
                self.fail(f"the operator {type(node.op).__name__} is not allowed — {_ALLOWED_TEXT}")
            a, b = self.check(node.left, depth + 1), self.check(node.right, depth + 1)
            if a.text or b.text:
                self.fail("text can only be compared (==, !=, in), not used in arithmetic")
            return self.merge([a, b], "this arithmetic")
        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
                self.fail(f"the operator {type(node.op).__name__} is not allowed")
            s = self.check(node.operand, depth + 1)
            if s.text:
                self.fail("text can only be compared (==, !=, in)")
            return self.merge([s], "this sign")
        if isinstance(node, ast.BoolOp):
            return self.merge([self.check(v, depth + 1) for v in node.values], "and/or")
        if isinstance(node, ast.Compare):
            shapes = [self.check(node.left, depth + 1)]
            for op, right in zip(node.ops, node.comparators):
                if isinstance(op, (ast.In, ast.NotIn)):
                    if not (isinstance(right, (ast.List, ast.Tuple)) and right.elts
                            and len(right.elts) <= MAX_IN_LIST
                            and all(isinstance(e, ast.Constant)
                                    and isinstance(e.value, (int, float, str)) for e in right.elts)):
                        self.fail(f"'in' takes a list of up to {MAX_IN_LIST} numbers or texts, "
                                  "e.g. units.class in ['Estuary', 'Delta']")
                    self.nodes += len(right.elts)
                    continue
                if type(op) not in _ORDER and type(op) not in _EQ:
                    self.fail(f"the comparison {type(op).__name__} is not allowed")
                s = self.check(right, depth + 1)
                if type(op) in _ORDER and (s.text or shapes[0].text):
                    self.fail("text can only be compared with ==, != or in, not ordered")
                shapes.append(s)
            return self.merge(shapes, "this comparison")
        if isinstance(node, ast.IfExp):
            return self.merge([self.check(node.test, depth + 1), self.check(node.body, depth + 1),
                               self.check(node.orelse, depth + 1)], "'a if c else b'")
        if isinstance(node, ast.Call):
            return self.call(node, depth)
        self.fail(f"'{type(node).__name__}' is not allowed in a method — {_ALLOWED_TEXT}")

    def call(self, node: ast.Call, depth: int) -> _Shape:
        if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
            name = getattr(node.func, "id", None) or type(node.func).__name__
            self.fail(f"unknown function {name!r} — functions: {', '.join(FUNCTIONS)}")
        fn = node.func.id
        if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
            self.fail(f"{fn}() takes plain arguments")
        kws = {k.arg: k.value for k in node.keywords}
        args = node.args
        if fn == "field":
            if not (len(args) == 2 and not kws and isinstance(args[0], ast.Name)
                    and args[0].id in self.aliases and isinstance(args[1], ast.Constant)
                    and isinstance(args[1].value, str) and args[1].value):
                self.fail("field(<input>, '<field name>') names a field by its exact name")
            return _Shape("rows", args[0].id)
        if fn in AGGREGATES:
            bad = set(kws) - {"where"}
            if bad:
                self.fail(f"{fn}() takes only where= besides its values, not {sorted(bad)}")
            want = 2 if fn == "weighted_mean" else 1
            if len(args) != want:
                self.fail(f"{fn}() takes {want} value(s) and an optional where=")
            shapes = [self.check(a, depth + 1) for a in args]
            if fn == "count":
                if shapes[0].kind == "scalar":
                    self.fail("count() counts rows: count(<input>) or count(<condition on its rows>)")
                if kws:
                    self.fail("count() takes the condition itself: count(<condition>)")
                return _Shape("scalar")
            for s in shapes:
                if s.kind != "rows":
                    self.fail(f"{fn}() takes a field of an input's rows, e.g. "
                              f"{fn}(units.restorable_area_ha)")
                if s.text:
                    self.fail(f"{fn}() needs numbers, not text")
            parts = list(shapes)
            if "where" in kws:
                w = self.check(kws["where"], depth + 1)
                if w.kind == "rowset":
                    self.fail("where= takes a condition on the rows")
                parts.append(w)
            self.merge(parts, f"{fn}()")
            return _Shape("scalar")
        # elementwise
        if fn == "round":
            if kws or not 1 <= len(args) <= 2:
                self.fail("round(x) or round(x, <digits 0-12>)")
            if len(args) == 2 and not (isinstance(args[1], ast.Constant)
                                       and isinstance(args[1].value, int)
                                       and not isinstance(args[1].value, bool)
                                       and 0 <= args[1].value <= 12):
                self.fail("round()'s digits must be a whole number from 0 to 12")
            args = args[:1]
        else:
            if kws:
                self.fail(f"{fn}() takes plain arguments")
            want = {"clip": 3, "where": 3, "minimum": 2, "maximum": 2}.get(fn, 1)
            if len(args) != want:
                self.fail(f"{fn}() takes {want} argument(s)")
        shapes = [self.check(a, depth + 1) for a in args]
        if any(s.text for s in (shapes[1:] if fn == "where" else shapes)):
            self.fail(f"{fn}() needs numbers, not text")
        return self.merge(shapes, f"{fn}()")


_QUOTED = re.compile(r"""('[^']*'|"[^"]*")""")


def _reserved_fields(expr: str, aliases) -> str:
    """<input>.class (a feed's own field name) is not something Python can parse: a
    reserved word after a dot becomes field(<input>, 'class'). Text inside quotes is
    left alone."""
    import keyword
    if not aliases:
        return expr
    pat = re.compile(r"\b(" + "|".join(map(re.escape, sorted(aliases))) + r")\.("
                     + "|".join(keyword.kwlist) + r")\b")
    parts = _QUOTED.split(expr)
    return "".join(pat.sub(r"field(\1, '\2')", p) if i % 2 == 0 else p
                   for i, p in enumerate(parts))


def _parse(expr, label: str, aliases=()) -> ast.AST:
    if not isinstance(expr, str) or not expr.strip():
        raise MethodError(f"step {label!r}: an expression is a non-empty text")
    if len(expr) > MAX_EXPR:
        raise MethodError(f"step {label!r}: longer than {MAX_EXPR} characters — split it into steps")
    try:
        return ast.parse(_reserved_fields(expr.strip(), aliases), mode="eval").body
    except SyntaxError as exc:
        raise MethodError(f"step {label!r}: not an expression the platform can read "
                          f"({exc.msg}, at character {exc.offset})") from None


def compile_steps(m: dict) -> list[tuple[str, ast.AST, _Shape]]:
    """The steps checked in order against the parameters, the inputs and the steps
    before each one. Raises MethodError naming the first step that fails."""
    aliases = set((m.get("inputs") or {}).keys())
    scope = {p: _Shape("scalar") for p in (m.get("params") or {})}
    out = []
    steps = m.get("steps") or {}
    if not isinstance(steps, dict) or not steps:
        raise MethodError("steps must be an ordered mapping of name -> expression")
    if len(steps) > MAX_STEPS:
        raise MethodError(f"at most {MAX_STEPS} steps")
    for name, expr in steps.items():
        name = str(name)
        if not _NAME.match(name) or name in FUNCTIONS:
            raise MethodError(f"step name {name!r} must be snake_case and not a function's name")
        if name in scope or name in aliases:
            raise MethodError(f"step name {name!r} is already a parameter, a step or an input")
        node = _parse(expr, name, aliases)
        shape = _Checker(scope, aliases, name).check(node)
        if shape.kind == "rowset":
            raise MethodError(f"step {name!r}: an input by itself is not a value — count it, or "
                              "name one of its fields")
        scope[name] = shape
        out.append((name, node, shape))
    return out


def _num(v):
    if v in _NULLS:
        return None
    if isinstance(v, bool):
        return float(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def validate(manifest) -> list[str]:
    """Every problem with a method manifest, without touching the network: the schema,
    every step's syntax and shape, and the contributor's worked examples re-run."""
    if not isinstance(manifest, dict):
        return ["manifest must be a mapping — the method fields, or the same as YAML/JSON text"]
    m = dict(manifest)
    problems = []
    unknown = set(m) - _KNOWN
    if unknown:
        problems.append(f"unknown fields {sorted(unknown)} — the fields are: "
                        + ", ".join(sorted(_KNOWN)))
    for k in METHOD_FIELDS["required"]:
        v = m.get(k)
        if v in (None, "", [], {}):
            base = f"missing required field '{k}'"
            if k in _ATTESTED_HERE:
                base += (f" — {_ATTESTED_HERE[k]}: ASK the contributor for it, or suggest one for "
                         "them to accept; never submit one they have not accepted")
            problems.append(base)
    name = str(m.get("method") or "")
    if name and not _NAME.match(name):
        problems.append(f"method {name!r} must be snake_case: lowercase letters, digits and _")
    packs = m.get("packs")
    if packs not in (None, "", []):
        if isinstance(packs, str):
            packs = [packs]
        if not isinstance(packs, list):
            problems.append("packs must be a list of pack names")
        else:
            known = _pack_ids()
            bad = [p for p in packs if p not in known]
            if bad:
                problems.append(f"unknown pack(s) {bad} — the platform's packs are: "
                                + ", ".join(known))
    inputs = m.get("inputs")
    if inputs not in (None, "", {}):
        if not isinstance(inputs, dict):
            problems.append("inputs must map an alias to {feed: <dataset>} or {table: <dataset>}")
        else:
            if len(inputs) > MAX_INPUTS:
                problems.append(f"at most {MAX_INPUTS} inputs")
            for alias, ref in inputs.items():
                if not _NAME.match(str(alias)) or str(alias) in FUNCTIONS:
                    problems.append(f"input alias {alias!r} must be snake_case and not a function's name")
                if not (isinstance(ref, dict) and len(ref) == 1
                        and next(iter(ref)) in ("feed", "table")
                        and str(next(iter(ref.values())) or "").strip()):
                    problems.append(f"input {alias!r} must be {{feed: <dataset>}} or "
                                    "{table: <dataset>} — raster layers and class areas as "
                                    "inputs are not supported yet")
    params = m.get("params")
    if params not in (None, {}):
        if not isinstance(params, dict):
            problems.append("params must map a name to {value: <number>, unit: ...}")
        else:
            for p, spec in params.items():
                if not _NAME.match(str(p)) or str(p) in FUNCTIONS:
                    problems.append(f"parameter {p!r} must be snake_case and not a function's name")
                if not (isinstance(spec, dict) and _is_number(spec.get("value"))):
                    problems.append(f"parameter {p!r} must be {{value: <number>, unit: ...}}")
                elif not str(spec.get("unit") or "").strip():
                    problems.append(f"parameter {p!r} has no unit — say what its number is in "
                                    "(percent, ha, kg, 1 for a plain number)")
    compiled = None
    if m.get("steps") not in (None, "", {}) and not any("inputs must" in p or "params must" in p
                                                        for p in problems):
        try:
            compiled = compile_steps(m)
        except MethodError as exc:
            problems.append(str(exc))
    outputs = m.get("outputs")
    if outputs not in (None, "", {}):
        if not isinstance(outputs, dict):
            problems.append("outputs must map a step to {unit: ...}")
        elif compiled is not None:
            shapes = {n: s for n, _, s in compiled}
            for o, spec in outputs.items():
                if o not in shapes:
                    problems.append(f"output {o!r} is not a step — steps: {', '.join(shapes)}")
                    continue
                if shapes[o].kind != "scalar":
                    problems.append(f"output {o!r} is one value per row — an output is one "
                                    "number for the area: wrap it in sum(), mean(), count() …")
                if shapes[o].text:
                    problems.append(f"output {o!r} is text — an output is a number")
                if not (isinstance(spec, dict) and str(spec.get("unit") or "").strip()):
                    problems.append(f"output {o!r} needs {{unit: ...}} — the unit its number is in")
    countries = m.get("countries")
    if countries not in (None, "", []) and not (isinstance(countries, list)
                                                and all(str(c).strip() for c in countries)):
        problems.append("countries must be a list of country names")
    from . import notes
    problems += notes.validate(m.get("usage_notes"))
    if not problems:
        problems += _check_examples(m)
    return problems


def _check_examples(m: dict) -> list[str]:
    ex = m.get("examples")
    if not isinstance(ex, list) or not ex:
        return ["examples must be a list of worked examples: [{rows: {...}, expect: {...}}]"]
    if len(ex) > MAX_EXAMPLES:
        return [f"at most {MAX_EXAMPLES} worked examples"]
    aliases = list((m.get("inputs") or {}).keys())
    outputs = list((m.get("outputs") or {}).keys())
    problems = []
    for i, e in enumerate(ex, 1):
        if not isinstance(e, dict) or not isinstance(e.get("rows"), dict) \
                or not isinstance(e.get("expect"), dict) or not e["expect"]:
            problems.append(f"example {i}: must be {{rows: {{<alias>: [...]}}, expect: {{<output>: <number>}}}}")
            continue
        missing = [a for a in aliases if a not in e["rows"]]
        if missing:
            problems.append(f"example {i}: rows are missing input(s) {missing} (an empty list is fine)")
            continue
        if any(not isinstance(e["rows"][a], list) or len(e["rows"][a]) > MAX_EXAMPLE_ROWS
               or not all(isinstance(r, dict) for r in e["rows"][a]) for a in aliases):
            problems.append(f"example {i}: each input's rows are a list of up to "
                            f"{MAX_EXAMPLE_ROWS} records")
            continue
        unknown = [k for k in e["expect"] if k not in outputs]
        if unknown:
            problems.append(f"example {i}: expects {unknown}, which are not outputs ({', '.join(outputs)})")
            continue
        over = e.get("params") or {}
        if not isinstance(over, dict) or any(k not in (m.get("params") or {}) or not _is_number(v)
                                             for k, v in over.items()):
            problems.append(f"example {i}: params must give numbers for declared parameters")
            continue
        try:
            got = evaluate(m, {a: e["rows"][a] for a in aliases}, params=over)
        except MethodError as exc:
            problems.append(f"example {i}: could not be computed — {exc}")
            continue
        for k, want in e["expect"].items():
            if not _is_number(want):
                problems.append(f"example {i}: expected {k} must be a number")
                continue
            have = got.get(k)
            if have is None or abs(have - want) > max(1e-9, TOLERANCE * abs(want)):
                problems.append(f"example {i}: {k} came out {_fmt(have)}, the example expects "
                                f"{_fmt(want)} — the steps do not reproduce the worked example; "
                                "check the steps (or the example) with the contributor")
    return problems


# --------------------------------------------------------------------------- running

class _Env:
    def __init__(self, m: dict, rows: dict, params: dict | None):
        self.inputs = rows
        self.values = {p: float(s["value"]) for p, s in (m.get("params") or {}).items()}
        for k, v in (params or {}).items():
            self.values[k] = float(v)
        self.cols: dict = {}

    def column(self, alias: str, field: str):
        key = (alias, field)
        if key not in self.cols:
            recs = self.inputs.get(alias) or []
            vals = [r.get(field) for r in recs]
            if recs and all(field not in r for r in recs):
                names = sorted({k for r in recs[:50] for k in r})
                near = difflib.get_close_matches(field, names, n=3)
                raise MethodError(f"input {alias!r} has no field {field!r}"
                                  + (f" — did you mean {', '.join(near)}?" if near else
                                     f" — its fields: {', '.join(names[:30])}"))
            nums = [_num(v) for v in vals]
            if all(n is not None or v in _NULLS for n, v in zip(nums, vals)):
                self.cols[key] = np.array([np.nan if n is None else n for n in nums], dtype=float)
            else:
                self.cols[key] = np.array(["" if v is None else str(v) for v in vals], dtype=object)
        return self.cols[key]


def _isnum(a) -> bool:
    return not (isinstance(a, str) or (isinstance(a, np.ndarray) and a.dtype == object))


def _numeric(a, what):
    if not _isnum(a):
        raise MethodError(f"{what} is text; text can only be compared (==, !=, in)")
    return a


def _bool(a):
    return np.asarray(a, dtype=bool) if isinstance(a, np.ndarray) else bool(a)


def _eval(node, env: _Env):
    if isinstance(node, ast.Constant):
        v = node.value
        return v if isinstance(v, (bool, str)) else float(v)
    if isinstance(node, ast.Name):
        return env.values[node.id] if node.id in env.values else ("__rowset__", node.id)
    if isinstance(node, ast.Attribute):
        return env.column(node.value.id, node.attr)
    if isinstance(node, ast.BinOp):
        a = _numeric(_eval(node.left, env), "a value in this arithmetic")
        b = _numeric(_eval(node.right, env), "a value in this arithmetic")
        return _BIN[type(node.op)](a, b)
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, env)
        if isinstance(node.op, ast.Not):
            return np.logical_not(v)
        v = _numeric(v, "a signed value")
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BoolOp):
        vals = [_bool(_eval(v, env)) for v in node.values]
        fn = np.logical_and if isinstance(node.op, ast.And) else np.logical_or
        out = vals[0]
        for v in vals[1:]:
            out = fn(out, v)
        return out
    if isinstance(node, ast.Compare):
        left = _eval(node.left, env)
        result = True
        for op, right in zip(node.ops, node.comparators):
            if isinstance(op, (ast.In, ast.NotIn)):
                choices = [e.value for e in right.elts]
                if isinstance(left, np.ndarray):
                    if left.dtype == object:
                        hit = np.isin(left, [str(c) for c in choices])
                    else:
                        hit = np.isin(left, [float(c) for c in choices if not isinstance(c, str)])
                else:
                    hit = left in choices
                part = np.logical_not(hit) if isinstance(op, ast.NotIn) else hit
                rhs = left
            else:
                rhs = _eval(right, env)
                if type(op) in _ORDER:
                    part = _ORDER[type(op)](_numeric(left, "an ordered value"),
                                            _numeric(rhs, "an ordered value"))
                elif _isnum(left) != _isnum(rhs):
                    part = np.zeros(len(left), dtype=bool) if isinstance(left, np.ndarray) else \
                        (np.zeros(len(rhs), dtype=bool) if isinstance(rhs, np.ndarray) else False)
                    if type(op) is ast.NotEq:
                        part = np.logical_not(part)
                else:
                    part = _EQ[type(op)](left, rhs)
            result = np.logical_and(result, part)
            left = rhs
        return _bool(result)
    if isinstance(node, ast.IfExp):
        return np.where(_bool(_eval(node.test, env)), _eval(node.body, env), _eval(node.orelse, env))
    if isinstance(node, ast.Call):
        return _call(node, env)
    raise MethodError(f"'{type(node).__name__}' is not allowed")       # pragma: no cover


def _rows_of(v, what: str):
    if isinstance(v, tuple) and v and v[0] == "__rowset__":
        raise MethodError(f"{what} needs a field of the input, not the input itself")
    return v


def _call(node: ast.Call, env: _Env):
    fn = node.func.id
    kws = {k.arg: k.value for k in node.keywords}
    if fn == "field":
        return env.column(node.args[0].id, node.args[1].value)
    if fn == "count":
        v = _eval(node.args[0], env)
        if isinstance(v, tuple) and v and v[0] == "__rowset__":
            return float(len(env.inputs.get(v[1]) or []))
        if not (isinstance(v, np.ndarray) and v.dtype == bool):
            raise MethodError("count() takes an input or a condition on its rows")
        return float(np.count_nonzero(v))
    if fn in AGGREGATES:
        vals = [_numeric(_rows_of(_eval(a, env), f"{fn}()"), f"a value in {fn}()") for a in node.args]
        x = np.asarray(vals[0], dtype=float)
        mask = np.ones(x.shape, dtype=bool) if x.ndim else np.ones(1, dtype=bool)
        if "where" in kws:
            w = _bool(_eval(kws["where"], env))
            mask = mask & (w if isinstance(w, np.ndarray) else bool(w))
        ok = mask & np.isfinite(x)
        if fn == "weighted_mean":
            wt = np.asarray(vals[1], dtype=float)
            ok = ok & np.isfinite(wt)
            den = float(wt[ok].sum())
            return float((x[ok] * wt[ok]).sum() / den) if den else float("nan")
        sel = x[ok]
        if fn == "sum":
            return float(sel.sum())
        if not sel.size:
            return float("nan")
        return float({"mean": np.mean, "min": np.min, "max": np.max, "median": np.median}[fn](sel))
    args = [_eval(a, env) for a in node.args]
    if fn == "where":
        return np.where(_bool(args[0]), _numeric(args[1], "where()'s value"),
                        _numeric(args[2], "where()'s value"))
    args = [_numeric(_rows_of(a, f"{fn}()"), f"{fn}()'s value") for a in args]
    if fn == "round":
        digits = int(node.args[1].value) if len(node.args) == 2 else 0
        return np.round(args[0], digits)
    return {"log": np.log, "log10": np.log10, "exp": np.exp, "sqrt": np.sqrt, "abs": np.abs,
            "clip": np.clip, "minimum": np.minimum, "maximum": np.maximum}[fn](*args)


_COMPILED: dict[str, list] = {}


def _digest(m: dict) -> str:
    return hashlib.sha256(json.dumps({k: m.get(k) for k in ("inputs", "params", "steps")},
                                     sort_keys=True, default=str).encode()).hexdigest()


def evaluate(m: dict, rows: dict, params: dict | None = None) -> dict[str, float]:
    """The method's outputs over these input rows (alias -> list of records), each one
    finite number. Raises MethodError when a step cannot be computed on these rows."""
    key = _digest(m)
    if key not in _COMPILED:
        if len(_COMPILED) > 256:
            _COMPILED.clear()
        _COMPILED[key] = compile_steps(m)
    env = _Env(m, rows, params)
    with np.errstate(all="ignore"):
        for name, node, _shape in _COMPILED[key]:
            try:
                env.values[name] = _eval(node, env)
            except MethodError as exc:
                raise MethodError(f"step {name!r}: {exc}") from None
            except (ValueError, TypeError, IndexError) as exc:
                raise MethodError(f"step {name!r} could not be computed on these rows "
                                  f"({type(exc).__name__}: {exc})") from None
    out = {}
    for o in (m.get("outputs") or {}):
        v = np.asarray(env.values.get(o), dtype=float)
        if v.size != 1 or not np.isfinite(v.item()):
            why = "no rows matched" if v.size == 1 else "it is one value per row"
            raise MethodError(f"output {o!r} is not a number for these rows ({why})")
        out[o] = float(v.item())
    return out


def _fmt(v) -> str:
    if v is None:
        return "nothing"
    v = float(v)
    if v == int(v) and abs(v) < 1e15:
        return f"{int(v):,}"
    if abs(v) >= 100:
        return f"{v:,.1f}"
    return f"{v:.4g}"


def fmt(v) -> str:
    return _fmt(v)


# --------------------------------------------------------------------------- registry

_LANDED: dict = {"sig": None, "rows": {}}


def _dir():
    from ..config import get_settings
    return get_settings().methods_conf_dir


def landed() -> dict[str, dict]:
    """Approved methods, read from their files (one per method); re-read when a file
    changes, so every worker sees a landing without a restart."""
    import yaml
    d = _dir()
    files = sorted(d.glob("*.yml")) if d.is_dir() else []
    sig = tuple((p.name, p.stat().st_mtime_ns) for p in files)
    if sig != _LANDED["sig"]:
        rows = {}
        for p in files:
            try:
                row = yaml.safe_load(p.read_text()) or {}
            except Exception:
                continue
            if isinstance(row, dict) and row.get("method"):
                rows[str(row["method"])] = row
        _LANDED.update(sig=sig, rows=rows)
    return dict(_LANDED["rows"])


def reload() -> None:
    _LANDED["sig"] = None


def staged_visible() -> dict[str, dict]:
    """Staged methods the current caller may see: its own, or all for a reviewer."""
    from . import identity, staging
    staging._ensure_staged_loaded()
    caller = identity.current()
    return {k: staging._as_preview(v) for k, v in staging.STAGED_METHODS.items()
            if caller.may_see(v.get("staged_by"))}


@once
def for_pack(pack: str) -> dict[str, dict]:
    """Every method a pack runs for the current caller: approved ones, and the caller's
    own staged ones (a contributor sees their method in a real answer before review)."""
    rows = {k: v for k, v in landed().items() if pack in (v.get("packs") or [])}
    for k, v in staged_visible().items():
        if k not in rows and pack in (v.get("packs") or []):
            rows[k] = v
    return rows


@once
def catalog() -> dict[str, dict]:
    """What capabilities lists: every method the caller can use, by name."""
    out = {}
    for src, rows in (("approved", landed()), ("staged — awaiting review", staged_visible())):
        for k, v in rows.items():
            out.setdefault(k, {"title": v.get("title"), "packs": v.get("packs"),
                               "inputs": {a: next(iter(r.values())) for a, r in (v.get("inputs") or {}).items()},
                               "outputs": {o: (s or {}).get("unit") for o, s in (v.get("outputs") or {}).items()},
                               "steps": v.get("steps"), "params": render_params(v) or None,
                               "holds_for": v.get("countries") or "anywhere its inputs cover",
                               "contributor": v.get("contributor_label"), "status": src})
    return out


def name_taken(name: str, contributor_id: str | None = None) -> str | None:
    from . import staging
    if (_dir() / f"{name}.yml").exists():
        return (f"a method named {name!r} is already on the platform — choose another name, or "
                "withdraw that one first (contribute_status action='withdraw')")
    staging._ensure_staged_loaded()
    row = staging.STAGED_METHODS.get(name)
    if row is not None:
        return (f"a method named {name!r} is already staged (contribution "
                f"{row.get('contribution_id')}) — choose another name or withdraw it first")
    return None


def input_spec(ds: str) -> dict | None:
    """The registry row of a feed or table a method reads (a staged one the caller may
    see included), or None."""
    from ..mcp import registry
    from . import staging
    row = registry.FEEDS.get(ds)
    if row is not None and row.get("status") == "available":
        return row
    return staging.visible_staged_feed(ds)


def prepare(m: dict) -> dict:
    """The live check: every input is a feed or table the platform serves, every field a
    step reads is in its rows, and the method runs on them. Stores nothing."""
    from ..mcp import feeds, registry
    inputs, rows = {}, {}
    for alias, ref in (m.get("inputs") or {}).items():
        kind, ds = next(iter(ref.items()))
        ds = str(ds).strip()
        spec = input_spec(ds)
        if spec is None:
            from . import staging
            names = [*registry.FEEDS, *staging.staged_feeds_for_caller()]
            near = difflib.get_close_matches(ds, names, n=4)
            raise MethodError(f"input {alias!r} names {ds!r}, which is not a feed or table the "
                              "platform serves" + (f" — did you mean {', '.join(near)}?" if near
                                                  else " — feeds_query lists them"))
        res = feeds.query(ds, {"limit": 500})
        if res.get("status") != "ok":
            raise MethodError(f"input {alias!r} ({ds}) did not answer: "
                              f"{res.get('note') or res.get('status')}")
        recs = res.get("records") or []
        if not recs:
            raise MethodError(f"input {alias!r} ({ds}) answered with no rows")
        rows[alias] = recs
        inputs[alias] = {"kind": kind, "dataset": ds, "title": spec.get("title"),
                         "fields": sorted({k for r in recs[:50] for k in r})}
    try:
        sample = evaluate(m, rows)
    except MethodError as exc:
        raise MethodError(f"the method does not run on its inputs' real rows: {exc}") from None
    # The dry run proves the steps run on real rows; its numbers answer nothing (the rows
    # are placed in no area), so they are not handed out to be quoted (MGV-514).
    return {"inputs": inputs,
            "sample": {"over": {a: f"{len(r):,} rows read for the check (not placed in any area)"
                                for a, r in rows.items()},
                       "ran": "every step ran on these real rows without error; the results "
                              "for a place come from a question in its pack",
                       "outputs": sorted(sample)}}


def row_from(rec: dict) -> dict:
    """The method as the platform stores and serves it: the contributor's manifest and
    where it came from."""
    m = rec["manifest"]
    row = {k: m[k] for k in [*METHOD_FIELDS["required"], *METHOD_FIELDS["optional"]] if k in m}
    if isinstance(row.get("packs"), str):
        row["packs"] = [row["packs"]]
    row.update({"contributed": True, "contribution_id": rec.get("contribution_id"),
                "contributor_label": rec.get("contributor_label")})
    return row


def render_steps(row: dict) -> str:
    return "; ".join(f"{k} = {v}" for k, v in (row.get("steps") or {}).items())


def render_params(row: dict) -> str:
    return "; ".join(f"{k} {_fmt(s.get('value'))} {s.get('unit')}".strip()
                     for k, s in (row.get("params") or {}).items())
