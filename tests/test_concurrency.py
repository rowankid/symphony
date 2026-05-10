import threading
import time
from pathlib import Path

from symphony_gitlab.config import load_effective_config
from symphony_gitlab.models import Issue, MergeRequest, Project, Workspace
from symphony_gitlab.orchestrator import OrchestratorState
from symphony_gitlab.concurrent import ConcurrentDispatcher


def make_project() -> Project:
    return Project(42, "team/app", "App", None, "main", "git@gitlab.example.com:team/app.git", "https://gitlab.example.com/team/app.git", None)


def make_config(tmp_path: Path):
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text(
        f"""---
gitlab:
  base_url: https://gitlab.example.com
  api_token: token
  project: team/app
agent:
  max_concurrent_agents: 2
workspace:
  root: {tmp_path / "workspaces"}
---
Prompt
""",
        encoding="utf-8",
    )
    return load_effective_config(workflow_path, make_project())


def make_issue(iid: int) -> Issue:
    return Issue(iid, iid, f"team/app#{iid}", 42, "team/app", f"Issue {iid}", None, "opened", ["symphony:ready"], None, None, None, [], None, None, None, [], None, "2026-01-01T00:00:00Z", None)


class FakeGitLab:
    def __init__(self, issues):
        self.issues = issues
        self.added = []
        self.removed = []
        self.notes = []

    def fetch_candidate_issues(self, project_id, project_path, active_labels):
        return self.issues

    def fetch_issue_blockers(self, project_id, issue_iid, project_path, blocked_link_types):
        return []

    def add_issue_labels(self, project_id, issue_iid, labels):
        self.added.append((issue_iid, labels))

    def remove_issue_labels(self, project_id, issue_iid, labels):
        self.removed.append((issue_iid, labels))

    def create_issue_note(self, project_id, issue_iid, body):
        self.notes.append((issue_iid, body))


class FakeWorkspaceManager:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path

    def prepare(self, issue, project):
        repo = self.tmp_path / f"repo-{issue.iid}"
        repo.mkdir(exist_ok=True)
        return Workspace(repo, f"team_app_{issue.iid}", repo, 42, "team/app", "remote", "main", f"symphony/{issue.iid}", False, None, None)

    def run_before_run_hook(self, repository_path):
        pass

    def run_after_run_hook(self, repository_path):
        pass

    def push_work_branch(self, workspace):
        return True


class BlockingAgent:
    def __init__(self):
        self.started = 0
        self.release = threading.Event()
        self.lock = threading.Lock()

    def run_once(self, issue, project, workspace, attempt=None):
        with self.lock:
            self.started += 1
        self.release.wait(timeout=5)
        from symphony_gitlab.agent import AgentRunResult

        return AgentRunResult(0, "", "")


class FakeMergeRequests:
    def handoff(self, issue, workspace):
        return MergeRequest(issue.iid, issue.iid, 42, "MR", None, "opened", True, workspace.work_branch, "main", "url")


def test_concurrent_dispatcher_respects_max_concurrent_agents(tmp_path: Path):
    config = make_config(tmp_path)
    state = OrchestratorState(make_project(), 30000, 2)
    agent = BlockingAgent()
    dispatcher = ConcurrentDispatcher(config, state, FakeGitLab([make_issue(1), make_issue(2), make_issue(3)]), FakeWorkspaceManager(tmp_path), agent, FakeMergeRequests())

    dispatcher.poll_once()
    deadline = time.time() + 2
    while agent.started < 2 and time.time() < deadline:
        time.sleep(0.01)

    assert agent.started == 2
    assert len(state.running) == 2
    agent.release.set()
    dispatcher.shutdown(wait=True)
