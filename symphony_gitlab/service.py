from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigError, EffectiveConfig, load_effective_config
from .agent import AgentRunner
from .concurrent import ConcurrentDispatcher
from .gitlab import GitLabClient
from .merge_request import MergeRequestManager
from .models import Project
from .observability import JsonLogger, build_status_snapshot
from .orchestrator import Orchestrator, OrchestratorState
from .workspace import WorkspaceManager
from .workflow import WorkflowError


@dataclass(slots=True)
class ServiceStatus:
    config: EffectiveConfig
    project: Project
    workflow_mtime: float
    state: OrchestratorState


class SymphonyService:
    def __init__(self, workflow_path: Path, logger: logging.Logger | None = None):
        self.workflow_path = workflow_path
        self.logger = logger or logging.getLogger("symphony_gitlab")
        self.status: ServiceStatus | None = None
        self.dispatcher: ConcurrentDispatcher | None = None
        self.json_logger: JsonLogger | None = None

    def validate_startup(self) -> ServiceStatus:
        workflow_probe = _load_raw_gitlab_settings(self.workflow_path)
        client = GitLabClient(workflow_probe["base_url"], workflow_probe["api_token"])
        project = client.fetch_project(workflow_probe["project"])
        config = load_effective_config(self.workflow_path, project)
        previous_state = self.status.state if self.status and str(self.status.project.id) == str(project.id) else None
        state = previous_state or OrchestratorState(
            project=project,
            poll_interval_ms=config.polling.interval_ms,
            max_concurrent_agents=config.agent.max_concurrent_agents,
        )
        state.poll_interval_ms = config.polling.interval_ms
        state.max_concurrent_agents = config.agent.max_concurrent_agents
        status = ServiceStatus(config=config, project=project, workflow_mtime=self.workflow_path.stat().st_mtime, state=state)
        old_status = self.status
        self.status = status
        if old_status is not None:
            self.dispatcher = None
        self._configure_json_logger(status.config)
        self._emit("startup_validated", project_id=project.id, project_path=project.path_with_namespace)
        return status

    def poll_once(self) -> None:
        status = self.status or self.validate_startup()
        gitlab = GitLabClient(status.config.gitlab.base_url, status.config.gitlab.api_token, status.config.gitlab.api_version)
        if self.dispatcher is None:
            self.dispatcher = ConcurrentDispatcher(
                config=status.config,
                state=status.state,
                gitlab=gitlab,
                workspace_manager=WorkspaceManager(status.config),
                agent_runner=AgentRunner(status.config),
                merge_requests=MergeRequestManager(status.config, gitlab),
            )
        self._emit("poll_started", project_id=status.project.id, project_path=status.project.path_with_namespace)
        self.dispatcher.poll_once()
        self._emit("poll_finished", project_id=status.project.id, project_path=status.project.path_with_namespace, running_count=len(status.state.running), retry_count=len(status.state.retry_attempts))

    def status_snapshot(self) -> dict:
        status = self.status or self.validate_startup()
        return build_status_snapshot(status.config, status.state)

    def _configure_json_logger(self, config: EffectiveConfig) -> None:
        log_path = config.observability.log_path or (config.workspace.root / "symphony.jsonl")
        self.json_logger = JsonLogger(log_path, secrets=[config.gitlab.api_token])

    def _emit(self, event: str, **fields) -> None:
        if self.json_logger is not None:
            self.json_logger.emit(event, **fields)

    def reload_if_changed(self) -> bool:
        if self.status is None:
            self.validate_startup()
            return True
        mtime = self.workflow_path.stat().st_mtime
        if mtime == self.status.workflow_mtime:
            return False
        try:
            self.validate_startup()
            self.logger.info("workflow_reloaded", extra={"event": "workflow_reloaded"})
            return True
        except Exception as exc:
            self.logger.error("workflow_reload_failed", extra={"event": "workflow_reload_failed", "error": str(exc)})
            return False

    def run_forever(self) -> None:
        self.validate_startup()
        while True:
            self.reload_if_changed()
            self.poll_once()
            time.sleep(self.status.config.polling.interval_ms / 1000 if self.status else 30)


def _load_raw_gitlab_settings(workflow_path: Path) -> dict[str, str | int]:
    from .workflow import load_workflow

    workflow = load_workflow(workflow_path)
    gitlab = workflow.config.get("gitlab") if isinstance(workflow.config.get("gitlab"), dict) else {}
    base_url = str(gitlab.get("base_url", "")).rstrip("/")
    token_value = str(gitlab.get("api_token", ""))
    if token_value.startswith("$"):
        import os

        token_value = os.environ.get(token_value[1:], "")
    project = gitlab.get("project")
    if not base_url:
        raise ConfigError("gitlab.base_url is required")
    if not token_value:
        raise ConfigError("gitlab.api_token is required")
    if project is None or project == "":
        raise ConfigError("gitlab.project is required")
    return {"base_url": base_url, "api_token": token_value, "project": project}
