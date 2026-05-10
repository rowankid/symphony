import subprocess
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

from symphony_gitlab.config import EffectiveConfig, load_effective_config
from symphony_gitlab.models import Issue, Project
from symphony_gitlab.orchestrator import OrchestratorState, compute_retry_delay_ms, should_dispatch, sort_issues_for_dispatch
from symphony_gitlab.workspace import WorkspaceError, WorkspaceManager, render_branch_name, sanitize_workspace_key


def make_project(tmp_path: Path) -> Project:
    return Project(
        id=42,
        path_with_namespace="team/app",
        name="App",
        web_url="https://gitlab.example.com/team/app",
        default_branch="main",
        ssh_url_to_repo=str(tmp_path / "remote.git"),
        http_url_to_repo="https://gitlab.example.com/team/app.git",
        visibility="private",
    )


def make_config(tmp_path: Path, project: Project) -> EffectiveConfig:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text(
        f"""---
gitlab:
  base_url: https://gitlab.example.com
  api_token: token
  project: team/app
  clone_protocol: ssh
workspace:
  root: {tmp_path / "workspaces"}
---
Prompt
""",
        encoding="utf-8",
    )
    return load_effective_config(workflow_path, project)


def make_issue(**overrides) -> Issue:
    values = {
        "id": 100,
        "iid": 7,
        "identifier": "team/app#7",
        "project_id": 42,
        "project_path": "team/app",
        "title": "Add payment webhook!",
        "description": None,
        "state": "opened",
        "labels": ["symphony:ready"],
        "priority": None,
        "weight": None,
        "milestone": None,
        "assignees": [],
        "author": None,
        "branch_name": None,
        "web_url": "https://gitlab.example.com/team/app/-/issues/7",
        "blocked_by": [],
        "merge_request": None,
        "created_at": "2026-01-02T00:00:00Z",
        "updated_at": None,
    }
    values.update(overrides)
    return Issue(**values)


def test_sanitizers_create_safe_workspace_keys_and_branch_names():
    issue = make_issue(title="Fix ../ unsafe: title")

    assert sanitize_workspace_key("team/app#7") == "team_app_7"
    assert render_branch_name("symphony/{{ issue.iid }}-{{ issue.slug }}", issue) == "symphony/7-fix-unsafe-title"

    with pytest.raises(WorkspaceError):
        render_branch_name("/bad/{{ issue.iid }}", issue)


def test_prepare_workspace_clones_remote_and_checks_out_work_branch(tmp_path: Path):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, stdout=subprocess.PIPE)
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True, stdout=subprocess.PIPE)
    subprocess.run(
        ["git", "-C", str(seed), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
        check=True,
        stdout=subprocess.PIPE,
    )
    subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(remote)], check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], check=True, stdout=subprocess.PIPE)

    project = make_project(tmp_path)
    config = make_config(tmp_path, project)
    workspace = WorkspaceManager(config).prepare(make_issue(), project)

    assert workspace.path == (tmp_path / "workspaces" / "team_app_7").resolve()
    assert workspace.repository_path == workspace.path / "repo"
    assert workspace.work_branch == "symphony/7-add-payment-webhook"
    current_branch = subprocess.run(
        ["git", "-C", str(workspace.repository_path), "branch", "--show-current"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    assert current_branch == workspace.work_branch


def test_push_work_branch_pushes_committed_branch_changes(tmp_path: Path):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, stdout=subprocess.PIPE)
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True, stdout=subprocess.PIPE)
    subprocess.run(
        ["git", "-C", str(seed), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
        check=True,
        stdout=subprocess.PIPE,
    )
    subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(remote)], check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], check=True, stdout=subprocess.PIPE)
    project = make_project(tmp_path)
    config = make_config(tmp_path, project)
    manager = WorkspaceManager(config)
    workspace = manager.prepare(make_issue(), project)
    (workspace.repository_path / "feature.txt").write_text("feature\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace.repository_path), "add", "feature.txt"], check=True, stdout=subprocess.PIPE)
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace.repository_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "feature",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )

    pushed = manager.push_work_branch(workspace)

    assert pushed
    remote_branch = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/symphony/7-add-payment-webhook"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    assert len(remote_branch) == 40


def test_prepare_workspace_rejects_mismatched_origin(tmp_path: Path):
    project = make_project(tmp_path)
    config = make_config(tmp_path, project)
    repo_path = config.workspace.root / "team_app_7" / "repo"
    repo_path.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo_path)], check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "-C", str(repo_path), "remote", "add", "origin", "https://evil.example.com/repo.git"], check=True)

    with pytest.raises(WorkspaceError) as excinfo:
        WorkspaceManager(config).prepare(make_issue(), project)

    assert "origin" in str(excinfo.value)


def test_dispatch_eligibility_and_sorting_respect_labels_blockers_and_priority(tmp_path: Path):
    project = make_project(tmp_path)
    config = make_config(tmp_path, project)
    state = OrchestratorState(project=project, poll_interval_ms=30000, max_concurrent_agents=1)

    ready = make_issue(id=1, iid=10, title="Ready", weight=10, created_at="2026-01-03T00:00:00Z")
    priority = make_issue(id=2, iid=11, title="Priority", labels=["symphony:ready", "urgent"], created_at="2026-01-04T00:00:00Z")
    blocked = make_issue(id=3, iid=12, title="Blocked", blocked_by=[{"state": "opened", "labels": []}])
    running = make_issue(id=4, iid=13, title="Running", labels=["symphony:ready", "symphony:running"])
    config.gitlab.issue.priority_labels["urgent"] = 1

    assert should_dispatch(ready, config, state)
    assert not should_dispatch(blocked, config, state)
    assert not should_dispatch(running, config, state)
    assert [issue.id for issue in sort_issues_for_dispatch([ready, priority], config)] == [2, 1]


def test_stale_running_label_can_be_reclaimed_when_no_local_run(tmp_path: Path):
    project = make_project(tmp_path)
    config = make_config(tmp_path, project)
    state = OrchestratorState(project=project, poll_interval_ms=30000, max_concurrent_agents=1)
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    fresh_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    stale = make_issue(id=5, iid=15, labels=["symphony:ready", "symphony:running"], updated_at=stale_time)
    fresh = make_issue(id=6, iid=16, labels=["symphony:ready", "symphony:running"], updated_at=fresh_time)

    assert should_dispatch(stale, config, state)
    assert not should_dispatch(fresh, config, state)


def test_retry_backoff_uses_exponential_growth_with_cap():
    assert compute_retry_delay_ms(attempt=1, cap_ms=300000) == 1000
    assert compute_retry_delay_ms(attempt=4, cap_ms=300000) == 8000
    assert compute_retry_delay_ms(attempt=20, cap_ms=300000) == 300000
