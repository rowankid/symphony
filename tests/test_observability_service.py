import json
from pathlib import Path

import pytest

from symphony_gitlab.config import load_effective_config
from symphony_gitlab.models import Project
from symphony_gitlab.observability import JsonLogger, build_status_snapshot, redact_secrets
from symphony_gitlab.orchestrator import OrchestratorState, RetryEntry
from symphony_gitlab.service import SymphonyService


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


def make_workflow(tmp_path: Path, token: str = "token") -> Path:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text(
        f"""---
gitlab:
  base_url: https://gitlab.example.com/
  api_token: {token}
  project: team/app
workspace:
  root: {tmp_path / "workspaces"}
---
Prompt
""",
        encoding="utf-8",
    )
    return workflow_path


def test_status_snapshot_contains_runtime_counts_without_token(tmp_path: Path):
    workflow_path = make_workflow(tmp_path, token="super-secret")
    config = load_effective_config(workflow_path, make_project())
    state = OrchestratorState(project=make_project(), poll_interval_ms=30000, max_concurrent_agents=2)
    state.running[100] = {"issue": {"identifier": "team/app#7"}}
    state.claimed.add(100)
    state.retry_attempts[101] = RetryEntry(101, 8, "team/app#8", 2, 12345, "super-secret failed")

    snapshot = build_status_snapshot(config, state)

    assert snapshot["project_path"] == "team/app"
    assert snapshot["running_count"] == 1
    assert snapshot["retry_count"] == 1
    assert "super-secret" not in json.dumps(snapshot)
    assert "[REDACTED]" in json.dumps(snapshot)


def test_json_logger_emits_structured_redacted_line(tmp_path: Path):
    log_path = tmp_path / "symphony.jsonl"
    logger = JsonLogger(log_path, secrets=["super-secret"])

    logger.emit("worker_failed", issue_identifier="team/app#7", error="token super-secret failed")

    line = log_path.read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    assert payload["event"] == "worker_failed"
    assert payload["issue_identifier"] == "team/app#7"
    assert payload["error"] == "token [REDACTED] failed"


def test_service_startup_emits_structured_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workflow_path = tmp_path / "WORKFLOW.md"
    log_path = tmp_path / "logs" / "symphony.jsonl"
    workflow_path.write_text(
        f"""---
gitlab:
  base_url: https://gitlab.example.com
  api_token: secret-token
  project: team/app
workspace:
  root: {tmp_path / "workspaces"}
observability:
  log_path: {log_path}
---
Prompt
""",
        encoding="utf-8",
    )
    project = make_project()
    monkeypatch.setattr("symphony_gitlab.service.GitLabClient", lambda *args, **kwargs: type("C", (), {"fetch_project": lambda self, project_id: project})())

    SymphonyService(workflow_path).validate_startup()

    payload = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert payload["event"] == "startup_validated"
    assert payload["project_path"] == "team/app"
    assert "secret-token" not in json.dumps(payload)


def test_reload_if_changed_keeps_last_known_good_config_on_invalid_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workflow_path = make_workflow(tmp_path)
    service = SymphonyService(workflow_path)
    project = make_project()

    monkeypatch.setattr("symphony_gitlab.service.GitLabClient", lambda *args, **kwargs: type("C", (), {"fetch_project": lambda self, project_id: project})())
    first = service.validate_startup()
    workflow_path.write_text("---\ngitlab:\n  base_url: ''\n---\nBroken\n", encoding="utf-8")

    assert not service.reload_if_changed()
    assert service.status is first
    assert service.status.config.gitlab.project == "team/app"


def test_valid_reload_rebuilds_dispatcher_for_new_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workflow_path = make_workflow(tmp_path)
    service = SymphonyService(workflow_path)
    project = make_project()
    monkeypatch.setattr("symphony_gitlab.service.GitLabClient", lambda *args, **kwargs: type("C", (), {"fetch_project": lambda self, project_id: project})())
    service.validate_startup()
    service.dispatcher = object()
    workflow_path.write_text(
        f"""---
gitlab:
  base_url: https://gitlab.example.com
  api_token: token
  project: team/app
polling:
  interval_ms: 45000
workspace:
  root: {tmp_path / "workspaces"}
---
Prompt
""",
        encoding="utf-8",
    )

    assert service.reload_if_changed()
    assert service.dispatcher is None
    assert service.status.config.polling.interval_ms == 45000


def test_redact_secrets_handles_git_credential_urls():
    redacted = redact_secrets("https://oauth2:abc123@gitlab.example.com/team/app.git failed", secrets=["abc123"])

    assert "abc123" not in redacted
    assert "oauth2:[REDACTED]@" in redacted
