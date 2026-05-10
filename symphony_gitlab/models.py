from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Project:
    id: int | str
    path_with_namespace: str
    name: str | None
    web_url: str | None
    default_branch: str
    ssh_url_to_repo: str | None
    http_url_to_repo: str | None
    visibility: str | None


@dataclass(slots=True)
class MergeRequest:
    id: int | str
    iid: int
    project_id: int | str
    title: str
    description: str | None
    state: str
    draft: bool
    source_branch: str
    target_branch: str
    web_url: str | None
    merge_status: str | None = None
    pipeline_status: str | None = None


@dataclass(slots=True)
class Issue:
    id: int | str
    iid: int
    identifier: str
    project_id: int | str
    project_path: str
    title: str
    description: str | None
    state: str
    labels: list[str]
    priority: int | None
    weight: int | None
    milestone: dict[str, Any] | None
    assignees: list[dict[str, Any]]
    author: dict[str, Any] | None
    branch_name: str | None
    web_url: str | None
    blocked_by: list[dict[str, Any]] = field(default_factory=list)
    merge_request: MergeRequest | dict[str, Any] | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(slots=True)
class WorkflowDefinition:
    config: dict[str, Any]
    prompt_template: str
    path: Path


@dataclass(slots=True)
class Workspace:
    path: Path
    workspace_key: str
    repository_path: Path
    project_id: int | str
    project_path: str
    remote_url: str
    base_branch: str
    work_branch: str
    created_now: bool
    commit_sha_before_run: str | None
    commit_sha_after_run: str | None


def to_context(value: Any) -> Any:
    if is_dataclass(value):
        return {k: to_context(v) for k, v in asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: to_context(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_context(v) for v in value]
    return value
