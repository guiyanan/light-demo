"""各步骤数据结构的 JSON Schema 与校验（R8a、R9）。"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator


def validate(data: Any, schema: dict[str, Any]) -> list[str]:
    """按 JSON Schema 校验，返回中文可读的错误列表（为空表示合格）。"""
    errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path))
    out = []
    for e in errors:
        where = "/".join(str(p) for p in e.path) or "（顶层）"
        out.append(f"{where}：{e.message}")
    return out
