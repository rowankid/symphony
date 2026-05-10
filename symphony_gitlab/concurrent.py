from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

from .config import EffectiveConfig
from .orchestrator import Orchestrator, OrchestratorState, should_dispatch, sort_issues_for_dispatch


class ConcurrentDispatcher:
    def __init__(self, config: EffectiveConfig, state: OrchestratorState, gitlab, workspace_manager, agent_runner, merge_requests):
        self.config = config
        self.state = state
        self.gitlab = gitlab
        self.workspace_manager = workspace_manager
        self.agent_runner = agent_runner
        self.merge_requests = merge_requests
        self._executor = ThreadPoolExecutor(max_workers=config.agent.max_concurrent_agents)
        self._lock = threading.Lock()

    def poll_once(self) -> None:
        orchestrator = self._orchestrator()
        orchestrator.reconcile_running_issues()
        issues = self.gitlab.fetch_candidate_issues(self.state.project.id, self.state.project.path_with_namespace, self.config.gitlab.issue.active_labels)
        for issue in sort_issues_for_dispatch(issues, self.config):
            with self._lock:
                issue = orchestrator._attach_blockers(issue)
                if not should_dispatch(issue, self.config, self.state):
                    continue
                if len(self.state.running) >= self.state.max_concurrent_agents:
                    break
                self.state.claimed.add(issue.id)
                self.state.running[issue.id] = {"issue": issue, "started_at": 0}
            self._executor.submit(self._run_background, issue)

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)

    def _run_background(self, issue) -> None:
        # _run_issue is the single lifecycle implementation; pre-added running state is overwritten.
        self._orchestrator()._run_issue(issue, attempt=None)

    def _orchestrator(self) -> Orchestrator:
        return Orchestrator(self.config, self.state, self.gitlab, self.workspace_manager, self.agent_runner, self.merge_requests)
