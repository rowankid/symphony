import os
from pathlib import Path

import pytest

from symphony_gitlab.config import ConfigError, load_effective_config
from symphony_gitlab.models import Project
from symphony_gitlab.template import TemplateRenderError, render_prompt
from symphony_gitlab.workflow import WorkflowError, load_workflow


def test_load_workflow_splits_yaml_front_matter_and_prompt(tmp_path: Path):
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text(
        """---
gitlab:
  base_url: https://gitlab.example.com/
  api_token: $GITLAB_API_TOKEN
  project: group/project
agent:
  max_concurrent_agents: 3
---

Work on {{ issue.identifier }}.
""",
        encoding="utf-8",
    )

    workflow = load_workflow(workflow_path)

    assert workflow.config["gitlab"]["base_url"] == "https://gitlab.example.com/"
    assert workflow.config["agent"]["max_concurrent_agents"] == 3
    assert workflow.prompt_template == "Work on {{ issue.identifier }}."


def test_load_workflow_rejects_non_map_front_matter(tmp_path: Path):
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text("---\n- one\n- two\n---\nBody\n", encoding="utf-8")

    with pytest.raises(WorkflowError) as excinfo:
        load_workflow(workflow_path)

    assert excinfo.value.code == "workflow_front_matter_not_a_map"


def test_effective_config_applies_defaults_and_resolves_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text(
        """---
gitlab:
  base_url: https://gitlab.example.com/
  api_token: $GITLAB_API_TOKEN
  project: group/project
  clone_protocol: https
workspace:
  root: ./workspaces
---
Prompt
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("GITLAB_API_TOKEN", "secret-token")
    project = Project(
        id=123,
        path_with_namespace="group/project",
        name="Project",
        web_url="https://gitlab.example.com/group/project",
        default_branch="main",
        ssh_url_to_repo="git@gitlab.example.com:group/project.git",
        http_url_to_repo="https://gitlab.example.com/group/project.git",
        visibility="private",
    )

    config = load_effective_config(workflow_path, project)

    assert config.gitlab.base_url == "https://gitlab.example.com"
    assert config.gitlab.api_token == "secret-token"
    assert config.gitlab.repository.base_branch == "main"
    assert config.gitlab.repository.worktree_subdir == "repo"
    assert config.gitlab.issue.active_labels == ["symphony:ready"]
    assert config.workspace.root == (tmp_path / "workspaces").resolve()
    assert config.polling.interval_ms == 30000
    assert config.codex.command == "codex app-server"


def test_effective_config_rejects_missing_token_after_env_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text(
        """---
gitlab:
  base_url: https://gitlab.example.com
  api_token: $MISSING_TOKEN
  project: group/project
---
Prompt
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    project = Project(
        id=123,
        path_with_namespace="group/project",
        name=None,
        web_url=None,
        default_branch="main",
        ssh_url_to_repo="git@gitlab.example.com:group/project.git",
        http_url_to_repo=None,
        visibility=None,
    )

    with pytest.raises(ConfigError) as excinfo:
        load_effective_config(workflow_path, project)

    assert "gitlab.api_token" in str(excinfo.value)


def test_prompt_template_renders_known_variables_and_rejects_unknowns():
    context = {
        "issue": {"identifier": "group/project#7", "title": "Add tests"},
        "project": {"path_with_namespace": "group/project"},
        "repository": {"work_branch": "symphony/7-add-tests"},
        "merge_request": None,
        "attempt": None,
    }

    rendered = render_prompt("Handle {{ issue.identifier }} on {{ repository.work_branch }}.", context)

    assert rendered == "Handle group/project#7 on symphony/7-add-tests."

    with pytest.raises(TemplateRenderError):
        render_prompt("{{ issue.missing }}", context)
