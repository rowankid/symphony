from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from .models import WorkflowDefinition


class WorkflowError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def load_workflow(path: str | Path) -> WorkflowDefinition:
    workflow_path = Path(path)
    try:
        text = workflow_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowError("missing_workflow_file", f"missing_workflow_file: {workflow_path}") from exc

    try:
        config, prompt = _split_front_matter(text)
    except WorkflowError:
        raise
    except Exception as exc:
        raise WorkflowError("workflow_parse_error", f"workflow_parse_error: {exc}") from exc

    if not isinstance(config, dict):
        raise WorkflowError("workflow_front_matter_not_a_map", "workflow_front_matter_not_a_map")
    return WorkflowDefinition(config=config, prompt_template=prompt.strip(), path=workflow_path)


def _split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    end = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = index
            break
    if end is None:
        raise WorkflowError("workflow_parse_error", "workflow_parse_error: unterminated front matter")

    config_text = "\n".join(lines[1:end])
    prompt = "\n".join(lines[end + 1 :])
    return _parse_yaml_map(config_text), prompt


def _parse_yaml_map(text: str) -> Any:
    try:
        import yaml  # type: ignore
    except Exception:
        return _parse_minimal_yaml(text)

    try:
        loaded = yaml.safe_load(text) if text.strip() else {}
    except Exception as exc:
        raise WorkflowError("workflow_parse_error", f"workflow_parse_error: {exc}") from exc
    return {} if loaded is None else loaded


def _parse_minimal_yaml(text: str) -> Any:
    parsed = _MinimalYaml(text).parse()
    return {} if parsed is None else parsed


class _MinimalYaml:
    def __init__(self, text: str):
        self.lines = text.splitlines()
        self.index = 0

    def parse(self) -> Any:
        self._skip_blank()
        if self.index >= len(self.lines):
            return {}
        indent = self._indent(self.lines[self.index])
        if self.lines[self.index].lstrip().startswith("- "):
            return self._parse_list(indent)
        return self._parse_map(indent)

    def _parse_map(self, indent: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while self.index < len(self.lines):
            line = self.lines[self.index]
            if not line.strip() or line.lstrip().startswith("#"):
                self.index += 1
                continue
            current_indent = self._indent(line)
            if current_indent < indent:
                break
            if current_indent > indent:
                raise ValueError(f"unexpected indentation: {line}")
            content = line.strip()
            if content.startswith("- "):
                break
            if ":" not in content:
                raise ValueError(f"expected key/value line: {line}")
            key, raw = content.split(":", 1)
            key = key.strip()
            raw = raw.strip()
            self.index += 1
            if raw == "|":
                result[key] = self._parse_block_scalar(current_indent + 2)
            elif raw == "":
                result[key] = self._parse_nested(current_indent + 2)
            else:
                result[key] = self._parse_scalar(raw)
        return result

    def _parse_list(self, indent: int) -> list[Any]:
        result: list[Any] = []
        while self.index < len(self.lines):
            line = self.lines[self.index]
            if not line.strip() or line.lstrip().startswith("#"):
                self.index += 1
                continue
            current_indent = self._indent(line)
            if current_indent < indent:
                break
            if current_indent != indent or not line.lstrip().startswith("- "):
                break
            raw = line.strip()[2:].strip()
            self.index += 1
            result.append(self._parse_scalar(raw) if raw else self._parse_nested(indent + 2))
        return result

    def _parse_nested(self, indent: int) -> Any:
        self._skip_blank()
        if self.index >= len(self.lines) or self._indent(self.lines[self.index]) < indent:
            return {}
        if self.lines[self.index].lstrip().startswith("- "):
            return self._parse_list(indent)
        return self._parse_map(indent)

    def _parse_block_scalar(self, indent: int) -> str:
        block: list[str] = []
        while self.index < len(self.lines):
            line = self.lines[self.index]
            if line.strip() and self._indent(line) < indent:
                break
            block.append(line[indent:] if len(line) >= indent else "")
            self.index += 1
        return "\n".join(block).rstrip("\n")

    def _skip_blank(self) -> None:
        while self.index < len(self.lines) and not self.lines[self.index].strip():
            self.index += 1

    @staticmethod
    def _indent(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    @staticmethod
    def _parse_scalar(raw: str) -> Any:
        if raw in {"null", "Null", "NULL", "~"}:
            return None
        if raw in {"true", "True", "TRUE"}:
            return True
        if raw in {"false", "False", "FALSE"}:
            return False
        if raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1].strip()
            if not inner:
                return []
            return [_MinimalYaml._parse_scalar(part.strip()) for part in inner.split(",")]
        if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
            try:
                return ast.literal_eval(raw)
            except Exception:
                return raw[1:-1]
        try:
            return int(raw)
        except ValueError:
            return raw
