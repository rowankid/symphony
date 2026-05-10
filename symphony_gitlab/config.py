from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import Project
from .workflow import load_workflow


class ConfigError(Exception):
    pass


@dataclass(slots=True)
class IssueConfig:
    active_labels: list[str] = field(default_factory=lambda: ["symphony:ready"])
    allow_unlabeled_open_issues: bool = False
    running_label: str = "symphony:running"
    review_label: str = "symphony:review"
    error_label: str = "symphony:error"
    terminal_labels: list[str] = field(default_factory=lambda: ["symphony:done", "symphony:cancelled"])
    closed_is_terminal: bool = True
    blocked_link_types: list[str] = field(default_factory=lambda: ["is_blocked_by"])
    priority_labels: dict[str, int] = field(default_factory=dict)
    claim_ttl_ms: int = 3600000
    post_lifecycle_notes: bool = True


@dataclass(slots=True)
class RepositoryConfig:
    base_branch: str
    branch_template: str = "symphony/{{ issue.iid }}-{{ issue.slug }}"
    worktree_subdir: str = "repo"
    require_origin_match: bool = True
    push_work_branch: bool = True
    reuse_existing_branch: bool = True


@dataclass(slots=True)
class MergeRequestConfig:
    create: bool = True
    draft: bool = True
    title_template: str = "Resolve #{{ issue.iid }}: {{ issue.title }}"
    description_template: str | None = None
    target_branch: str | None = None
    remove_source_branch: bool = True
    squash: bool | None = None
    labels: list[str] = field(default_factory=list)
    assign_to_issue_assignees: bool = False


@dataclass(slots=True)
class GitLabConfig:
    base_url: str
    api_token: str
    project: str | int
    clone_protocol: str
    clone_depth: int | None
    allow_insecure_tls: bool
    issue: IssueConfig
    repository: RepositoryConfig
    merge_request: MergeRequestConfig
    selected_clone_url: str
    api_version: str = "v4"


@dataclass(slots=True)
class PollingConfig:
    interval_ms: int = 30000


@dataclass(slots=True)
class WorkspaceConfig:
    root: Path
    cleanup_terminal_workspaces: bool = True
    preserve_failed_workspaces: bool = True


@dataclass(slots=True)
class HooksConfig:
    after_create: str | None = None
    after_clone: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    before_remove: str | None = None
    timeout_ms: int = 60000


@dataclass(slots=True)
class AgentConfig:
    max_concurrent_agents: int = 10
    max_turns: int = 20
    max_retry_backoff_ms: int = 300000
    max_concurrent_agents_by_label: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class CodexConfig:
    command: str = "codex app-server"
    approval_policy: str | None = None
    thread_sandbox: str | None = None
    turn_sandbox_policy: str | None = None
    turn_timeout_ms: int = 3600000
    read_timeout_ms: int = 5000
    stall_timeout_ms: int = 300000


@dataclass(slots=True)
class ObservabilityConfig:
    log_gitlab_api_errors: bool = True
    redact_gitlab_token: bool = True
    emit_issue_notes: bool = True
    status_snapshot: bool = True
    log_path: Path | None = None


@dataclass(slots=True)
class EffectiveConfig:
    workflow_path: Path
    prompt_template: str
    gitlab: GitLabConfig
    polling: PollingConfig
    workspace: WorkspaceConfig
    hooks: HooksConfig
    agent: AgentConfig
    codex: CodexConfig
    observability: ObservabilityConfig


def load_effective_config(workflow_path: str | Path, project: Project) -> EffectiveConfig:
    workflow = load_workflow(workflow_path)
    base_dir = workflow.path.parent.resolve()
    raw = workflow.config
    gitlab_raw = _dict(raw.get("gitlab"))
    issue_raw = _dict(gitlab_raw.get("issue"))
    repo_raw = _dict(gitlab_raw.get("repository"))
    mr_raw = _dict(gitlab_raw.get("merge_request"))

    base_url = str(_required(gitlab_raw, "base_url", "gitlab.base_url")).rstrip("/")
    api_token = _resolve_env(str(_required(gitlab_raw, "api_token", "gitlab.api_token")))
    if not api_token:
        raise ConfigError("gitlab.api_token is required")
    project_value = _required(gitlab_raw, "project", "gitlab.project")
    clone_protocol = str(gitlab_raw.get("clone_protocol", "ssh"))
    if clone_protocol not in {"ssh", "https"}:
        raise ConfigError("gitlab.clone_protocol must be ssh or https")
    selected_clone_url = project.ssh_url_to_repo if clone_protocol == "ssh" else project.http_url_to_repo
    if not selected_clone_url:
        raise ConfigError(f"project has no clone URL for {clone_protocol}")

    active_labels = _list(issue_raw.get("active_labels", ["symphony:ready"]))
    allow_unlabeled = bool(issue_raw.get("allow_unlabeled_open_issues", False))
    if not active_labels and not allow_unlabeled:
        raise ConfigError("gitlab.issue.active_labels is required unless unlabeled issues are allowed")

    issue = IssueConfig(
        active_labels=[label.lower() for label in active_labels],
        allow_unlabeled_open_issues=allow_unlabeled,
        running_label=str(issue_raw.get("running_label", "symphony:running")).lower(),
        review_label=str(issue_raw.get("review_label", "symphony:review")).lower(),
        error_label=str(issue_raw.get("error_label", "symphony:error")).lower(),
        terminal_labels=[label.lower() for label in _list(issue_raw.get("terminal_labels", ["symphony:done", "symphony:cancelled"]))],
        closed_is_terminal=bool(issue_raw.get("closed_is_terminal", True)),
        blocked_link_types=_list(issue_raw.get("blocked_link_types", ["is_blocked_by"])),
        priority_labels={str(k).lower(): int(v) for k, v in _dict(issue_raw.get("priority_labels")).items()},
        claim_ttl_ms=int(issue_raw.get("claim_ttl_ms", 3600000)),
        post_lifecycle_notes=bool(issue_raw.get("post_lifecycle_notes", True)),
    )

    repository = RepositoryConfig(
        base_branch=str(repo_raw.get("base_branch") or project.default_branch),
        branch_template=str(repo_raw.get("branch_template", "symphony/{{ issue.iid }}-{{ issue.slug }}")),
        worktree_subdir=str(repo_raw.get("worktree_subdir", "repo")),
        require_origin_match=bool(repo_raw.get("require_origin_match", True)),
        push_work_branch=bool(repo_raw.get("push_work_branch", True)),
        reuse_existing_branch=bool(repo_raw.get("reuse_existing_branch", True)),
    )
    merge_request = MergeRequestConfig(
        create=bool(mr_raw.get("create", True)),
        draft=bool(mr_raw.get("draft", True)),
        title_template=str(mr_raw.get("title_template", "Resolve #{{ issue.iid }}: {{ issue.title }}")),
        description_template=mr_raw.get("description_template"),
        target_branch=mr_raw.get("target_branch") or repository.base_branch,
        remove_source_branch=bool(mr_raw.get("remove_source_branch", True)),
        squash=mr_raw.get("squash"),
        labels=_list(mr_raw.get("labels", [])),
        assign_to_issue_assignees=bool(mr_raw.get("assign_to_issue_assignees", False)),
    )
    gitlab = GitLabConfig(
        base_url=base_url,
        api_token=api_token,
        project=project_value,
        api_version=str(gitlab_raw.get("api_version", "v4")),
        clone_protocol=clone_protocol,
        clone_depth=_clone_depth(gitlab_raw.get("clone_depth", 1)),
        allow_insecure_tls=bool(gitlab_raw.get("allow_insecure_tls", False)),
        issue=issue,
        repository=repository,
        merge_request=merge_request,
        selected_clone_url=selected_clone_url,
    )

    polling_raw = _dict(raw.get("polling"))
    workspace_raw = _dict(raw.get("workspace"))
    hooks_raw = _dict(raw.get("hooks"))
    agent_raw = _dict(raw.get("agent"))
    codex_raw = _dict(raw.get("codex"))
    observability_raw = _dict(raw.get("observability"))

    workspace_root_raw = workspace_raw.get("root", str(Path(tempfile.gettempdir()) / "symphony_gitlab_workspaces"))
    workspace_root = _resolve_path(str(workspace_root_raw), base_dir)
    hooks = HooksConfig(
        after_create=hooks_raw.get("after_create"),
        after_clone=hooks_raw.get("after_clone"),
        before_run=hooks_raw.get("before_run"),
        after_run=hooks_raw.get("after_run"),
        before_remove=hooks_raw.get("before_remove"),
        timeout_ms=int(hooks_raw.get("timeout_ms", 60000)),
    )
    if hooks.timeout_ms <= 0:
        raise ConfigError("hooks.timeout_ms must be positive")

    return EffectiveConfig(
        workflow_path=workflow.path,
        prompt_template=workflow.prompt_template,
        gitlab=gitlab,
        polling=PollingConfig(interval_ms=int(polling_raw.get("interval_ms", 30000))),
        workspace=WorkspaceConfig(
            root=workspace_root,
            cleanup_terminal_workspaces=bool(workspace_raw.get("cleanup_terminal_workspaces", True)),
            preserve_failed_workspaces=bool(workspace_raw.get("preserve_failed_workspaces", True)),
        ),
        hooks=hooks,
        agent=AgentConfig(
            max_concurrent_agents=int(agent_raw.get("max_concurrent_agents", 10)),
            max_turns=int(agent_raw.get("max_turns", 20)),
            max_retry_backoff_ms=int(agent_raw.get("max_retry_backoff_ms", 300000)),
            max_concurrent_agents_by_label={str(k).lower(): int(v) for k, v in _dict(agent_raw.get("max_concurrent_agents_by_label")).items() if int(v) > 0},
        ),
        codex=CodexConfig(
            command=str(codex_raw.get("command", "codex app-server")),
            approval_policy=codex_raw.get("approval_policy"),
            thread_sandbox=codex_raw.get("thread_sandbox"),
            turn_sandbox_policy=codex_raw.get("turn_sandbox_policy"),
            turn_timeout_ms=int(codex_raw.get("turn_timeout_ms", 3600000)),
            read_timeout_ms=int(codex_raw.get("read_timeout_ms", 5000)),
            stall_timeout_ms=int(codex_raw.get("stall_timeout_ms", 300000)),
        ),
        observability=ObservabilityConfig(
            log_gitlab_api_errors=bool(observability_raw.get("log_gitlab_api_errors", True)),
            redact_gitlab_token=bool(observability_raw.get("redact_gitlab_token", True)),
            emit_issue_notes=bool(observability_raw.get("emit_issue_notes", issue.post_lifecycle_notes)),
            status_snapshot=bool(observability_raw.get("status_snapshot", True)),
            log_path=_resolve_path(str(observability_raw["log_path"]), base_dir) if observability_raw.get("log_path") else None,
        ),
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _required(mapping: dict[str, Any], key: str, name: str) -> Any:
    value = mapping.get(key)
    if value is None or value == "":
        raise ConfigError(f"{name} is required")
    return value


def _resolve_env(value: str) -> str:
    if value.startswith("$") and value.count("$") == 1:
        return os.environ.get(value[1:], "")
    return value


def _resolve_path(value: str, base_dir: Path) -> Path:
    expanded = os.path.expanduser(os.path.expandvars(value))
    path = Path(expanded)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _clone_depth(value: Any) -> int | None:
    if value in {None, 0, "0", "null"}:
        return None
    depth = int(value)
    if depth < 0:
        raise ConfigError("gitlab.clone_depth must be non-negative")
    return depth
