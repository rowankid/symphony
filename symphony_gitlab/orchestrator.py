from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import time
from typing import Any

from .config import EffectiveConfig
from .models import Issue, Project


@dataclass(slots=True)
class OrchestratorState:
    project: Project
    poll_interval_ms: int
    max_concurrent_agents: int
    running: dict[int | str, Any] = field(default_factory=dict)
    claimed: set[int | str] = field(default_factory=set)
    retry_attempts: dict[int | str, Any] = field(default_factory=dict)
    completed: set[int | str] = field(default_factory=set)
    codex_totals: dict[str, int | float] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "seconds_running": 0})
    codex_rate_limits: dict[str, Any] | None = None


@dataclass(slots=True)
class RetryEntry:
    issue_id: int | str
    issue_iid: int
    identifier: str
    attempt: int
    due_at_ms: int
    error: str | None


class Orchestrator:
    def __init__(self, config: EffectiveConfig, state: OrchestratorState, gitlab, workspace_manager, agent_runner, merge_requests):
        self.config = config
        self.state = state
        self.gitlab = gitlab
        self.workspace_manager = workspace_manager
        self.agent_runner = agent_runner
        self.merge_requests = merge_requests

    def poll_once(self) -> None:
        self.reconcile_running_issues()
        self._run_due_retries()
        issues = self.gitlab.fetch_candidate_issues(
            self.state.project.id,
            self.state.project.path_with_namespace,
            self.config.gitlab.issue.active_labels,
        )
        for issue in sort_issues_for_dispatch(issues, self.config):
            issue = self._attach_blockers(issue)
            if not should_dispatch(issue, self.config, self.state):
                continue
            self._run_issue(issue, attempt=None)
            if len(self.state.running) >= self.state.max_concurrent_agents:
                break

    def reconcile_running_issues(self) -> None:
        for issue_id, entry in list(self.state.running.items()):
            issue = entry.get("issue")
            if issue is None:
                continue
            try:
                refreshed = self.gitlab.fetch_issue(issue.project_id, issue.iid, issue.project_path)
            except Exception:
                continue
            refreshed = self._attach_blockers(refreshed)
            if _is_terminal(refreshed, self.config):
                self._terminate_running_issue(issue_id, entry, cleanup_workspace=self.config.workspace.cleanup_terminal_workspaces)
            elif not self._eligible_for_continuation(refreshed):
                self._terminate_running_issue(issue_id, entry, cleanup_workspace=False)
            else:
                entry["issue"] = refreshed

    def _terminate_running_issue(self, issue_id: int | str, entry: dict[str, Any], cleanup_workspace: bool) -> None:
        handle = entry.get("handle")
        if handle is not None and hasattr(handle, "cancel"):
            handle.cancel()
        if cleanup_workspace and entry.get("workspace") is not None:
            self.workspace_manager.cleanup(entry["workspace"])
        self.state.running.pop(issue_id, None)
        self.state.claimed.discard(issue_id)

    def _run_due_retries(self) -> None:
        now_ms = _now_ms()
        for issue_id, retry in list(self.state.retry_attempts.items()):
            if retry.due_at_ms > now_ms or len(self.state.running) >= self.state.max_concurrent_agents:
                continue
            try:
                issue = self.gitlab.fetch_issue(self.state.project.id, retry.issue_iid, self.state.project.path_with_namespace)
            except Exception:
                continue
            issue = self._attach_blockers(issue)
            if not self._should_retry(issue):
                self.state.retry_attempts.pop(issue_id, None)
                self.state.claimed.discard(issue_id)
                continue
            self.state.retry_attempts.pop(issue_id, None)
            self._run_issue(issue, attempt=retry.attempt)

    def _attach_blockers(self, issue: Issue) -> Issue:
        if not self.config.gitlab.issue.blocked_link_types or not hasattr(self.gitlab, "fetch_issue_blockers"):
            return issue
        try:
            issue.blocked_by = self.gitlab.fetch_issue_blockers(
                issue.project_id,
                issue.iid,
                issue.project_path,
                self.config.gitlab.issue.blocked_link_types,
            )
        except Exception:
            issue.blocked_by = [{"state": "opened", "labels": []}]
        return issue

    def _run_issue(self, issue: Issue, attempt: int | None = None) -> None:
        self.state.claimed.add(issue.id)
        self.gitlab.add_issue_labels(issue.project_id, issue.iid, [self.config.gitlab.issue.running_label])
        self.gitlab.remove_issue_labels(issue.project_id, issue.iid, [self.config.gitlab.issue.error_label])
        self._note(issue, f"Symphony started work on {issue.identifier}.")
        self.state.running[issue.id] = {"issue": issue, "started_at": time.time()}
        try:
            workspace = self.workspace_manager.prepare(issue, self.state.project)
            self.workspace_manager.run_before_run_hook(workspace.repository_path)
            agent_result = self.agent_runner.run_once(issue, self.state.project, workspace, attempt=attempt)
            self._record_agent_result(agent_result)
            self.workspace_manager.run_after_run_hook(workspace.repository_path)
            self.workspace_manager.push_work_branch(workspace)
            merge_request = self.merge_requests.handoff(issue, workspace)
            self.gitlab.remove_issue_labels(issue.project_id, issue.iid, [self.config.gitlab.issue.running_label])
            self.gitlab.remove_issue_labels(issue.project_id, issue.iid, self.config.gitlab.issue.active_labels)
            self.gitlab.add_issue_labels(issue.project_id, issue.iid, [self.config.gitlab.issue.review_label])
            if merge_request and merge_request.web_url:
                self._note(issue, f"Symphony prepared merge request: {merge_request.web_url}")
            else:
                self._note(issue, "Symphony completed the run; no merge request was created.")
            self.state.completed.add(issue.id)
            self.state.claimed.discard(issue.id)
        except Exception as exc:
            self.workspace_manager.run_after_run_hook(getattr(locals().get("workspace", None), "repository_path", None)) if "workspace" in locals() else None
            self.gitlab.remove_issue_labels(issue.project_id, issue.iid, [self.config.gitlab.issue.running_label])
            self.gitlab.add_issue_labels(issue.project_id, issue.iid, [self.config.gitlab.issue.error_label])
            retry = RetryEntry(
                issue_id=issue.id,
                issue_iid=issue.iid,
                identifier=issue.identifier,
                attempt=(attempt or 0) + 1,
                due_at_ms=_now_ms() + compute_retry_delay_ms((attempt or 0) + 1, self.config.agent.max_retry_backoff_ms),
                error=str(exc),
            )
            self.state.retry_attempts[issue.id] = retry
            self._note(issue, f"Symphony run failed; retry scheduled: {_redact(str(exc), self.config.gitlab.api_token)}")
        finally:
            self.state.running.pop(issue.id, None)

    def _note(self, issue: Issue, body: str) -> None:
        if self.config.observability.emit_issue_notes:
            self.gitlab.create_issue_note(issue.project_id, issue.iid, body)

    def _record_agent_result(self, result: Any) -> None:
        self.state.codex_totals["input_tokens"] = int(self.state.codex_totals.get("input_tokens", 0)) + int(getattr(result, "input_tokens", 0) or 0)
        self.state.codex_totals["output_tokens"] = int(self.state.codex_totals.get("output_tokens", 0)) + int(getattr(result, "output_tokens", 0) or 0)
        self.state.codex_totals["total_tokens"] = int(self.state.codex_totals.get("total_tokens", 0)) + int(getattr(result, "total_tokens", 0) or 0)

    def _should_retry(self, issue: Issue) -> bool:
        if _is_terminal(issue, self.config):
            return False
        if _has_open_blocker(issue, self.config):
            return False
        return True

    def _eligible_for_continuation(self, issue: Issue) -> bool:
        labels = {label.lower() for label in issue.labels}
        if _is_terminal(issue, self.config):
            return False
        if self.config.gitlab.issue.running_label in labels:
            return True
        if self.config.gitlab.issue.allow_unlabeled_open_issues:
            return issue.state.lower() == "opened"
        return bool(labels.intersection(self.config.gitlab.issue.active_labels))


def should_dispatch(issue: Issue, config: EffectiveConfig, state: OrchestratorState) -> bool:
    labels = {label.lower() for label in issue.labels}
    issue_state = issue.state.lower()
    if str(issue.project_id) != str(state.project.id):
        return False
    if issue_state != "opened":
        return False
    if config.gitlab.issue.closed_is_terminal and issue_state == "closed":
        return False
    if labels.intersection(config.gitlab.issue.terminal_labels):
        return False
    if config.gitlab.issue.running_label in labels and not _has_stale_running_label(issue, config):
        return False
    if not config.gitlab.issue.allow_unlabeled_open_issues and not labels.intersection(config.gitlab.issue.active_labels):
        return False
    if issue.id in state.running or issue.id in state.retry_attempts or issue.id in state.claimed:
        return False
    if len(state.running) >= state.max_concurrent_agents:
        return False
    if _has_open_blocker(issue, config):
        return False
    return True


def sort_issues_for_dispatch(issues: list[Issue], config: EffectiveConfig) -> list[Issue]:
    return sorted(issues, key=lambda issue: (_priority(issue, config), issue.created_at or "", issue.iid))


def compute_retry_delay_ms(attempt: int, cap_ms: int) -> int:
    return min(cap_ms, 1000 * (2 ** max(0, attempt - 1)))


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


def _redact(message: str, token: str) -> str:
    return message.replace(token, "[REDACTED]") if token else message


def _is_terminal(issue: Issue, config: EffectiveConfig) -> bool:
    labels = {label.lower() for label in issue.labels}
    return (config.gitlab.issue.closed_is_terminal and issue.state.lower() == "closed") or bool(labels.intersection(config.gitlab.issue.terminal_labels))


def _has_stale_running_label(issue: Issue, config: EffectiveConfig) -> bool:
    if not issue.updated_at:
        return False
    try:
        updated = datetime.fromisoformat(issue.updated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    age_ms = (datetime.now(timezone.utc) - updated).total_seconds() * 1000
    return age_ms >= config.gitlab.issue.claim_ttl_ms


def _priority(issue: Issue, config: EffectiveConfig) -> int:
    label_priorities = [config.gitlab.issue.priority_labels[label] for label in issue.labels if label in config.gitlab.issue.priority_labels]
    if label_priorities:
        return min(label_priorities)
    if issue.priority is not None:
        return issue.priority
    if issue.weight is not None:
        return issue.weight
    return 2**31 - 1


def _has_open_blocker(issue: Issue, config: EffectiveConfig) -> bool:
    terminal = set(config.gitlab.issue.terminal_labels)
    for blocker in issue.blocked_by:
        state = str(blocker.get("state", "")).lower()
        labels = {str(label).lower() for label in blocker.get("labels", [])}
        if state != "closed" and not labels.intersection(terminal):
            return True
    return False
