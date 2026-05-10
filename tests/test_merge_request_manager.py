from pathlib import Path

from symphony_gitlab.config import load_effective_config
from symphony_gitlab.merge_request import MergeRequestManager
from symphony_gitlab.models import Issue, MergeRequest, Project, Workspace


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
  merge_request:
    labels: ["symphony"]
workspace:
  root: {tmp_path / "workspaces"}
---
Prompt
""",
        encoding="utf-8",
    )
    return load_effective_config(workflow_path, make_project())


def make_issue() -> Issue:
    return Issue(100, 7, "team/app#7", 42, "team/app", "Add tests", None, "opened", ["symphony:ready"], None, None, None, [], None, None, None, [], None, None, None)


def make_workspace(tmp_path: Path) -> Workspace:
    repo = tmp_path / "repo"
    repo.mkdir()
    return Workspace(tmp_path, "team_app_7", repo, 42, "team/app", "git@gitlab.example.com:team/app.git", "main", "symphony/7-add-tests", False, None, None)


class FakeGitLab:
    def __init__(self):
        self.updated = []
        self.existing = MergeRequest(200, 9, 42, "Old", None, "opened", True, "symphony/7-add-tests", "main", "https://gitlab.example.com/mr/9")

    def list_merge_requests(self, project_id, source_branch, target_branch):
        return [self.existing]

    def update_merge_request(self, project_id, mr_iid, payload):
        self.updated.append((project_id, mr_iid, payload))
        return MergeRequest(200, mr_iid, project_id, payload["title"], payload["description"], "opened", True, "symphony/7-add-tests", "main", "https://gitlab.example.com/mr/9")


def test_handoff_updates_existing_open_merge_request(tmp_path: Path):
    gitlab = FakeGitLab()
    mr = MergeRequestManager(make_config(tmp_path), gitlab).handoff(make_issue(), make_workspace(tmp_path))

    assert mr.title == "Resolve #7: Add tests"
    assert gitlab.updated[0][0:2] == (42, 9)
    assert gitlab.updated[0][2]["labels"] == "symphony"
