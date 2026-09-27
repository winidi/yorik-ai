"""calculate — arithmetic done by code, never by the model.

The chat test (2026-09-26) found the model computing "37,92 €/Monat"
for 551,07 / 12 and summing seven transfers to 3.650 instead of 3.850.
A small AST evaluator: numbers (German "551,07" or "1.234,50" too),
+ - * / // % **, parentheses, round/sum/min/max/abs. No names, no
attribute access, no calls beyond that list.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from typing import Any, Dict

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {"round": round, "sum": lambda *a: sum(a[0]) if len(a) == 1 and isinstance(a[0], (list, tuple)) else sum(a),
          "min": min, "max": max, "abs": abs}


def normalize(expression: str) -> str:
    """German amounts to Python: "1.234,50" → 1234.50, "551,07" → 551.07.
    Any other comma stays an argument separator."""
    expr = expression.replace("€", "").replace("EUR", "").replace("×", "*").replace("÷", "/")
    expr = re.sub(r"(?<=\d)\.(?=\d{3}(?!\d))(?=\d{3}(?:[,\s)*/+-]|$))", "", expr)   # thousands dots
    # decimal comma only for cent amounts ("551,07"), so "sum([1,2,3])"
    # and "max(3,4)" keep their argument commas
    expr = re.sub(r"(?<=\d),(?=\d{2}(?!\d))", ".", expr)
    return expr


def evaluate(expression: str) -> float:
    tree = ast.parse(normalize(expression), mode="eval")

    def ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("exponent too large")
            return _BIN[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](ev(node.operand))
        if isinstance(node, (ast.List, ast.Tuple)):
            return [ev(e) for e in node.elts]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS \
                and not node.keywords:
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        raise ValueError(f"not allowed in a calculation: {ast.dump(node)[:60]}")

    value = ev(tree)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or math.isnan(value) or math.isinf(value):
        raise ValueError("the calculation did not give a number")
    return float(value)


def german(value: float, decimals: int = 2) -> str:
    s = f"{value:,.{decimals}f}"
    return s.replace(",", "_").replace(".", ",").replace("_", ".")


async def execute(ctx, expression: str, decimals: int = 2) -> Dict[str, Any]:
    expression = (expression or "").strip()
    if not expression:
        raise ValueError("expression is required, e.g. 551.07 / 12")
    try:
        value = evaluate(expression)
    except ZeroDivisionError as exc:
        raise ValueError("division by zero") from exc
    except SyntaxError as exc:
        raise ValueError(f"cannot read {expression!r} as a calculation") from exc
    decimals = max(0, min(int(decimals if decimals is not None else 2), 10))
    return {"expression": expression, "result": round(value, decimals), "result_de": german(value, decimals),
            "_llm_hint": f"{expression} = {german(value, decimals)}. Use exactly this number."}
