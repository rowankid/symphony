import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from symphony_gitlab.agent import AgentRunner, AgentRunError
from symphony_gitlab.config import load_effective_config
from symphony_gitlab.models import Issue, Project, Workspace


def make_issue() -> Issue:
    return Issue(
        id=100,
        iid=7,
        identifier="team/app#7",
        project_id=42,
        project_path="team/app",
        title="Add tests",
        description="Body",
        state="opened",
        labels=["symphony:ready"],
        priority=None,
        weight=None,
        milestone=None,
        assignees=[],
        author=None,
        branch_name="symphony/7-add-tests",
        web_url=None,
        blocked_by=[],
        merge_request=None,
        created_at=None,
        updated_at=None,
    )


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


def test_agent_runner_launches_command_in_repository_path_and_passes_prompt(tmp_path: Path):
    recorder = tmp_path / "record.json"
    repo = tmp_path / "repo"
    repo.mkdir()
    command = (
        f"{sys.executable} -c \"import json, os, sys; "
        f"open({str(recorder)!r}, 'w').write(json.dumps({{'cwd': os.getcwd(), 'prompt': sys.stdin.read()}}))\""
    )
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text(
        f"""---
gitlab:
  base_url: https://gitlab.example.com
  api_token: token
  project: team/app
codex:
  command: {command}
---
Issue {{{{ issue.identifier }}}} on {{{{ repository.work_branch }}}}
""",
        encoding="utf-8",
    )
    config = load_effective_config(workflow_path, make_project())
    workspace = Workspace(
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

    result = AgentRunner(config).run_once(make_issue(), make_project(), workspace)

    assert result.exit_code == 0
    recorded = json.loads(recorder.read_text(encoding="utf-8"))
    assert recorded["cwd"] == str(repo)
    assert recorded["prompt"] == "Issue team/app#7 on symphony/7-add-tests"


def test_agent_runner_raises_on_nonzero_exit(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text(
        f"""---
gitlab:
  base_url: https://gitlab.example.com
  api_token: token
  project: team/app
codex:
  command: "{sys.executable} -c 'import sys; sys.exit(5)'"
---
Prompt
""",
        encoding="utf-8",
    )
    config = load_effective_config(workflow_path, make_project())
    workspace = Workspace(
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

    with pytest.raises(AgentRunError):
        AgentRunner(config).run_once(make_issue(), make_project(), workspace)


def test_cli_validate_errors_on_missing_default_workflow(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, "-m", "symphony_gitlab", "--validate-only"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode != 0
    assert "missing_workflow_file" in result.stderr


def test_cli_status_json_outputs_redacted_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text(
        f"""---
gitlab:
  base_url: https://gitlab.example.com
  api_token: secret-token
  project: team/app
workspace:
  root: {tmp_path / "workspaces"}
---
Prompt
""",
        encoding="utf-8",
    )
    script = (
        "import sys;"
        "from symphony_gitlab.models import Project;"
        "import symphony_gitlab.service as s;"
        "s.GitLabClient=lambda *a, **k: type('C', (), {'fetch_project': lambda self, project: Project(42, 'team/app', 'App', None, 'main', 'ssh', 'https', None)})();"
        "from symphony_gitlab.cli import main;"
        "raise SystemExit(main([sys.argv[1], '--status-json']))"
    )

    result = subprocess.run(
        [sys.executable, "-c", script, str(workflow_path)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 0
    assert '"project_path": "team/app"' in result.stdout
    assert "secret-token" not in result.stdout
