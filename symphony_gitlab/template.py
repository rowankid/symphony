from __future__ import annotations

import re
from typing import Any


class TemplateRenderError(Exception):
    pass


_EXPRESSION = re.compile(r"{{\s*([^{}]+?)\s*}}")


def render_prompt(template: str, context: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        expression = match.group(1).strip()
        if "|" in expression:
            raise TemplateRenderError(f"unknown filter in expression: {expression}")
        value = _lookup(expression, context)
        if value is None:
            return ""
        return str(value)

    try:
        return _EXPRESSION.sub(replace, template)
    except TemplateRenderError:
        raise
    except Exception as exc:
        raise TemplateRenderError(str(exc)) from exc


def _lookup(expression: str, context: dict[str, Any]) -> Any:
    if not expression:
        raise TemplateRenderError("empty expression")
    value: Any = context
    for part in expression.split("."):
        part = part.strip()
        if not part:
            raise TemplateRenderError(f"invalid expression: {expression}")
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise TemplateRenderError(f"unknown variable: {expression}")
    return value
