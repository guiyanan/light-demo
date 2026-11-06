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


# R8a：大模型提炼的消费者需求（1–5 条，每条 1–2 句原话）。note 用提示词里的笔记代号，如 "N3"
NEEDS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["needs"],
    "properties": {
        "needs": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {
                "type": "object",
                "required": ["title", "description", "type", "quotes"],
                "properties": {
                    "title": {"type": "string", "minLength": 1},
                    "description": {"type": "string", "minLength": 1},
                    "type": {"enum": ["正面", "负面", "期待"]},
                    "quotes": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 2,
                        "items": {
                            "type": "object",
                            "required": ["text", "source", "note"],
                            "properties": {
                                "text": {"type": "string", "minLength": 1},
                                "source": {"enum": ["正文", "评论"]},
                                "note": {"type": "string", "pattern": "^N[0-9]+$"},
                            },
                        },
                    },
                },
            },
        }
    },
}


# R9：3 个新品概念。营养为每 100g 估算值，「AI 估算」标注由程序统一加上
_NUM = {"type": "number", "minimum": 0}
CONCEPTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["concepts"],
    "properties": {
        "concepts": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "required": ["name", "tagline", "target", "ingredients", "packaging", "spec", "nutrition",
                             "need_ids", "image_prompt"],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "tagline": {"type": "string", "minLength": 1},
                    "target": {"type": "string", "minLength": 1},
                    "ingredients": {"type": "array", "minItems": 1, "maxItems": 10,
                                    "items": {"type": "string", "minLength": 1}},
                    "packaging": {"type": "string", "minLength": 1},
                    "spec": {"type": "string", "minLength": 1},
                    "nutrition": {
                        "type": "object",
                        "required": ["protein_g", "carbs_g", "sugar_g", "fat_g", "energy_kcal"],
                        "properties": {
                            "protein_g": {**_NUM, "maximum": 30},
                            "carbs_g": {**_NUM, "maximum": 60},
                            "sugar_g": {**_NUM, "maximum": 60},
                            "fat_g": {**_NUM, "maximum": 40},
                            "energy_kcal": {**_NUM, "maximum": 600},
                        },
                    },
                    "need_ids": {"type": "array", "minItems": 1, "maxItems": 3,
                                 "items": {"type": "integer", "minimum": 1, "maximum": 5}},
                    "image_prompt": {"type": "string", "minLength": 1},
                },
            },
        }
    },
}
