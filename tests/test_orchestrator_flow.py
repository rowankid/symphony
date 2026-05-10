from pathlib import Path

from symphony_gitlab.agent import AgentRunError, AgentRunResult
from symphony_gitlab.config import load_effective_config
from symphony_gitlab.models import Issue, MergeRequest, Project, Workspace
from symphony_gitlab.orchestrator import Orchestrator, OrchestratorState, RetryEntry


def make_project() -> Project:
    return Project(
        id=42,
        path_with_namespace="team/app",
        name="App",
        web_url=None,
        default_branch="main",
        ssh_url_to_repo="git@gitlab.example.com:team/app.git",
        http_url_to_repo="https://gitlab.example.com/team/app.git",
        visibility=None,
    )


def make_config(tmp_path: Path):
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text(
        f"""---
gitlab:
  base_url: https://gitlab.example.com
  api_token: token
  project: team/app
workspace:
  root: {tmp_path / "workspaces"}
---
Prompt
""",
        encoding="utf-8",
    )
    return load_effective_config(workflow_path, make_project())


def make_issue(**overrides) -> Issue:
    values = {
        "id": 100,
        "iid": 7,
        "identifier": "team/app#7",
        "project_id": 42,
        "project_path": "team/app",
        "title": "Add tests",
        "description": None,
        "state": "opened",
        "labels": ["symphony:ready"],
        "priority": None,
        "weight": None,
        "milestone": None,
        "assignees": [],
        "author": None,
        "branch_name": None,
        "web_url": None,
        "blocked_by": [],
        "merge_request": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": None,
    }
    values.update(overrides)
    return Issue(**values)


def make_workspace(tmp_path: Path) -> Workspace:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return Workspace(
        path=tmp_path,
        workspace_key="team_app_7",
        repository_path=repo,
        project_id=42,
        project_path="team/app",
        remote_url="git@gitlab.example.com:team/app.git",
        base_branch="main",
        work_branch="symphony/7-add-tests",
        created_now=False,
        commit_sha_before_run=None,
        commit_sha_after_run=None,
    )


class FakeGitLab:
    def __init__(self, issue: Issue):
        self.issue = issue
        self.added = []
        self.removed = []
        self.notes = []
        self.blockers = {}

    def fetch_candidate_issues(self, project_id, project_path, active_labels):
        return [self.issue]

    def fetch_issue(self, project_id, issue_iid, project_path):
        return self.issue

    def fetch_issue_blockers(self, project_id, issue_iid, project_path, blocked_link_types):
        return self.blockers.get(issue_iid, [])

    def add_issue_labels(self, project_id, issue_iid, labels):
        self.added.append(labels)
        existing = set(self.issue.labels)
        existing.update(labels)
        self.issue.labels = list(existing)

    def remove_issue_labels(self, project_id, issue_iid, labels):
        self.removed.append(labels)
        removed = set(labels)
        self.issue.labels = [label for label in self.issue.labels if label not in removed]

    def create_issue_note(self, project_id, issue_iid, body):
        self.notes.append(body)


class FakeWorkspaceManager:
    def __init__(self, workspace, push_error=None):
        self.workspace = workspace
        self.push_error = push_error
        self.before = 0
        self.after = 0
        self.pushed = 0
        self.cleaned = []

    def prepare(self, issue, project):
        return self.workspace

    def run_before_run_hook(self, repository_path):
        self.before += 1

    def run_after_run_hook(self, repository_path):
        self.after += 1

    def push_work_branch(self, workspace):
        self.pushed += 1
        if self.push_error:
            raise self.push_error
        return True

    def cleanup(self, workspace):
        self.cleaned.append(workspace.path)


class FakeRunHandle:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeAgent:
    def __init__(self, fail=False, result=None):
        self.fail = fail
        self.result = result
        self.attempts = []

    def run_once(self, issue, project, workspace, attempt=None):
        self.attempts.append(attempt)
        if self.fail:
            raise AgentRunError("boom")
        return self.result or AgentRunResult(exit_code=0, stdout="ok", stderr="")


class FakeMergeRequests:
    def __init__(self):
        self.calls = 0

    def handoff(self, issue, workspace):
        self.calls += 1
        return MergeRequest(
            id=200,
            iid=9,
            project_id=42,
            title="MR",
            description=None,
            state="opened",
            draft=True,
            source_branch=workspace.work_branch,
            target_branch="main",
            web_url="https://gitlab.example.com/team/app/-/merge_requests/9",
        )


def test_poll_once_claims_issue_runs_agent_and_marks_review(tmp_path: Path):
    config = make_config(tmp_path)
    project = make_project()
    state = OrchestratorState(project=project, poll_interval_ms=30000, max_concurrent_agents=1)
    gitlab = FakeGitLab(make_issue())

    workspace_manager = FakeWorkspaceManager(make_workspace(tmp_path))
    Orchestrator(
        config=config,
        state=state,
        gitlab=gitlab,
        workspace_manager=workspace_manager,
        agent_runner=FakeAgent(),
        merge_requests=FakeMergeRequests(),
    ).poll_once()

    assert workspace_manager.pushed == 1
    assert gitlab.added == [["symphony:running"], ["symphony:review"]]
    assert gitlab.removed == [["symphony:error"], ["symphony:running"], ["symphony:ready"]]
    assert any("merge request" in note.lower() for note in gitlab.notes)
    assert state.completed == {100}
    assert state.claimed == set()


def test_poll_once_marks_error_and_schedules_retry_on_agent_failure(tmp_path: Path):
    config = make_config(tmp_path)
    project = make_project()
    state = OrchestratorState(project=project, poll_interval_ms=30000, max_concurrent_agents=1)
    gitlab = FakeGitLab(make_issue())

    Orchestrator(
        config=config,
        state=state,
        gitlab=gitlab,
        workspace_manager=FakeWorkspaceManager(make_workspace(tmp_path)),
        agent_runner=FakeAgent(fail=True),
        merge_requests=FakeMergeRequests(),
    ).poll_once()

    assert ["symphony:error"] in gitlab.added
    assert ["symphony:running"] in gitlab.removed
    assert 100 in state.retry_attempts
    assert state.claimed == {100}


def test_poll_once_skips_issue_with_open_blocker_from_gitlab_links(tmp_path: Path):
    config = make_config(tmp_path)
    project = make_project()
    state = OrchestratorState(project=project, poll_interval_ms=30000, max_concurrent_agents=1)
    gitlab = FakeGitLab(make_issue())
    gitlab.blockers[7] = [{"id": 9, "iid": 2, "identifier": "team/app#2", "state": "opened", "labels": []}]
    workspace_manager = FakeWorkspaceManager(make_workspace(tmp_path))

    Orchestrator(
        config=config,
        state=state,
        gitlab=gitlab,
        workspace_manager=workspace_manager,
        agent_runner=FakeAgent(),
        merge_requests=FakeMergeRequests(),
    ).poll_once()

    assert gitlab.added == []
    assert workspace_manager.before == 0
    assert state.claimed == set()


def test_poll_once_marks_error_when_branch_push_fails_before_merge_request(tmp_path: Path):
    config = make_config(tmp_path)
    project = make_project()
    state = OrchestratorState(project=project, poll_interval_ms=30000, max_concurrent_agents=1)
    gitlab = FakeGitLab(make_issue())
    workspace_manager = FakeWorkspaceManager(make_workspace(tmp_path), push_error=RuntimeError("push failed"))
    merge_requests = FakeMergeRequests()

    Orchestrator(
        config=config,
        state=state,
        gitlab=gitlab,
        workspace_manager=workspace_manager,
        agent_runner=FakeAgent(),
        merge_requests=merge_requests,
    ).poll_once()

    assert workspace_manager.pushed == 1
    assert merge_requests.calls == 0
    assert ["symphony:error"] in gitlab.added
    assert 100 in state.retry_attempts


def test_poll_once_runs_due_retry_and_releases_retry_claim_on_success(tmp_path: Path):
    config = make_config(tmp_path)
    project = make_project()
    state = OrchestratorState(project=project, poll_interval_ms=30000, max_concurrent_agents=1)
    state.claimed.add(100)
    state.retry_attempts[100] = RetryEntry(
        issue_id=100,
        issue_iid=7,
        identifier="team/app#7",
        attempt=2,
        due_at_ms=0,
        error="previous failure",
    )
    gitlab = FakeGitLab(make_issue())
    agent = FakeAgent()

    Orchestrator(
        config=config,
        state=state,
        gitlab=gitlab,
        workspace_manager=FakeWorkspaceManager(make_workspace(tmp_path)),
        agent_runner=agent,
        merge_requests=FakeMergeRequests(),
    ).poll_once()

    assert agent.attempts == [2]
    assert state.retry_attempts == {}
    assert state.claimed == set()
    assert state.completed == {100}


def test_poll_once_records_agent_session_and_token_totals(tmp_path: Path):
    config = make_config(tmp_path)
    project = make_project()
    state = OrchestratorState(project=project, poll_interval_ms=30000, max_concurrent_agents=1)
    gitlab = FakeGitLab(make_issue())
    agent_result = AgentRunResult(exit_code=0, stdout="", stderr="", session_id="thread-1-turn-1", input_tokens=5, output_tokens=7, total_tokens=12)

    Orchestrator(
        config=config,
        state=state,
        gitlab=gitlab,
        workspace_manager=FakeWorkspaceManager(make_workspace(tmp_path)),
        agent_runner=FakeAgent(result=agent_result),
        merge_requests=FakeMergeRequests(),
    ).poll_once()

    assert state.codex_totals["input_tokens"] == 5
    assert state.codex_totals["output_tokens"] == 7
    assert state.codex_totals["total_tokens"] == 12


def test_reconcile_terminal_running_issue_cancels_and_cleans_workspace(tmp_path: Path):
    config = make_config(tmp_path)
    project = make_project()
    running_issue = make_issue(state="closed")
    state = OrchestratorState(project=project, poll_interval_ms=30000, max_concurrent_agents=1)
    workspace = make_workspace(tmp_path)
    handle = FakeRunHandle()
    state.running[100] = {"issue": make_issue(), "workspace": workspace, "handle": handle}
    state.claimed.add(100)
    gitlab = FakeGitLab(running_issue)
    workspace_manager = FakeWorkspaceManager(workspace)

    Orchestrator(
        config=config,
        state=state,
        gitlab=gitlab,
        workspace_manager=workspace_manager,
        agent_runner=FakeAgent(),
        merge_requests=FakeMergeRequests(),
    ).reconcile_running_issues()

    assert handle.cancelled
    assert state.running == {}
    assert state.claimed == set()
    assert workspace_manager.cleaned == [workspace.path]


def test_reconcile_issue_losing_eligibility_cancels_without_cleanup(tmp_path: Path):
    config = make_config(tmp_path)
    project = make_project()
    inactive_issue = make_issue(labels=["triage"])
    state = OrchestratorState(project=project, poll_interval_ms=30000, max_concurrent_agents=1)
    workspace = make_workspace(tmp_path)
    handle = FakeRunHandle()
    state.running[100] = {"issue": make_issue(), "workspace": workspace, "handle": handle}
    state.claimed.add(100)
    gitlab = FakeGitLab(inactive_issue)
    workspace_manager = FakeWorkspaceManager(workspace)

    Orchestrator(
        config=config,
        state=state,
        gitlab=gitlab,
        workspace_manager=workspace_manager,
        agent_runner=FakeAgent(),
        merge_requests=FakeMergeRequests(),
    ).reconcile_running_issues()

    assert handle.cancelled
    assert state.running == {}
    assert state.claimed == set()
    assert workspace_manager.cleaned == []
