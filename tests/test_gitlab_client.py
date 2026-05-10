import json

import pytest

from symphony_gitlab.gitlab import GitLabClient, GitLabError


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.responses = []

    def queue(self, status, body, headers=None):
        self.responses.append((status, body, headers or {}))

    def request(self, method, url, headers=None, json_body=None):
        self.calls.append((method, url, headers or {}, json_body))
        status, body, response_headers = self.responses.pop(0)
        return status, body, response_headers


def test_fetch_project_normalizes_required_fields():
    transport = FakeTransport()
    transport.queue(
        200,
        {
            "id": 42,
            "path_with_namespace": "team/app",
            "name": "App",
            "web_url": "https://gitlab.example.com/team/app",
            "default_branch": "main",
            "ssh_url_to_repo": "git@gitlab.example.com:team/app.git",
            "http_url_to_repo": "https://gitlab.example.com/team/app.git",
            "visibility": "private",
        },
    )
    client = GitLabClient("https://gitlab.example.com/", "secret", transport=transport)

    project = client.fetch_project("team/app")

    assert project.id == 42
    assert project.path_with_namespace == "team/app"
    assert project.default_branch == "main"
    assert transport.calls[0][1] == "https://gitlab.example.com/api/v4/projects/team%2Fapp"
    assert transport.calls[0][2]["PRIVATE-TOKEN"] == "secret"


def test_fetch_candidate_issues_paginates_and_normalizes_labels():
    transport = FakeTransport()
    transport.queue(
        200,
        [
            {
                "id": 1,
                "iid": 3,
                "project_id": 42,
                "title": "First",
                "description": None,
                "state": "opened",
                "labels": ["Symphony:Ready", "Priority::High"],
                "weight": 5,
                "web_url": "https://gitlab.example.com/team/app/-/issues/3",
                "created_at": "2026-01-02T00:00:00Z",
                "updated_at": "2026-01-03T00:00:00Z",
            }
        ],
        {"X-Next-Page": "2"},
    )
    transport.queue(
        200,
        [
            {
                "id": 2,
                "iid": 4,
                "project_id": 42,
                "title": "Second",
                "description": "Body",
                "state": "opened",
                "labels": ["symphony:ready"],
                "weight": None,
                "web_url": "https://gitlab.example.com/team/app/-/issues/4",
            }
        ],
        {"X-Next-Page": ""},
    )
    client = GitLabClient("https://gitlab.example.com", "secret", transport=transport)

    issues = client.fetch_candidate_issues(42, "team/app", active_labels=["symphony:ready"])

    assert [issue.identifier for issue in issues] == ["team/app#3", "team/app#4"]
    assert issues[0].labels == ["symphony:ready", "priority::high"]
    assert "labels=symphony%3Aready" in transport.calls[0][1]
    assert "page=2" in transport.calls[1][1]


def test_gitlab_error_mapping_redacts_token_from_message():
    transport = FakeTransport()
    transport.queue(401, {"message": "bad token secret"}, {})
    client = GitLabClient("https://gitlab.example.com", "secret", transport=transport)

    with pytest.raises(GitLabError) as excinfo:
        client.fetch_project("team/app")

    assert excinfo.value.code == "gitlab_auth_error"
    assert "secret" not in str(excinfo.value)


def test_label_and_note_writes_use_narrow_issue_endpoints():
    transport = FakeTransport()
    transport.queue(200, {"labels": ["symphony:running"]})
    transport.queue(201, {"body": "Started"})
    client = GitLabClient("https://gitlab.example.com", "secret", transport=transport)

    client.add_issue_labels(42, 7, ["symphony:running"])
    client.create_issue_note(42, 7, "Started")

    assert transport.calls[0][0] == "PUT"
    assert transport.calls[0][3] == {"add_labels": "symphony:running"}
    assert transport.calls[1][0] == "POST"
    assert transport.calls[1][3] == {"body": "Started"}


def test_replace_labels_and_branch_create_use_gitlab_endpoints():
    transport = FakeTransport()
    transport.queue(200, {"labels": ["symphony:running"]})
    transport.queue(201, {"name": "symphony/7-work"})
    client = GitLabClient("https://gitlab.example.com", "secret", transport=transport)

    client.replace_issue_labels(42, 7, ["symphony:running"])
    client.create_branch(42, "symphony/7-work", "main")

    assert transport.calls[0][0] == "PUT"
    assert transport.calls[0][3] == {"labels": "symphony:running"}
    assert transport.calls[1][0] == "POST"
    assert transport.calls[1][1] == "https://gitlab.example.com/api/v4/projects/42/repository/branches"
    assert transport.calls[1][3] == {"branch": "symphony/7-work", "ref": "main"}


def test_update_merge_request_uses_existing_mr_iid():
    transport = FakeTransport()
    transport.queue(
        200,
        {
            "id": 200,
            "iid": 9,
            "project_id": 42,
            "title": "Updated",
            "state": "opened",
            "draft": True,
            "source_branch": "symphony/7-work",
            "target_branch": "main",
            "web_url": "https://gitlab.example.com/team/app/-/merge_requests/9",
        },
    )
    client = GitLabClient("https://gitlab.example.com", "secret", transport=transport)

    mr = client.update_merge_request(42, 9, {"title": "Updated"})

    assert transport.calls[0][0] == "PUT"
    assert transport.calls[0][1] == "https://gitlab.example.com/api/v4/projects/42/merge_requests/9"
    assert transport.calls[0][3] == {"title": "Updated"}
    assert mr.title == "Updated"


def test_fetch_issue_blockers_uses_issue_links_and_normalizes_blocker_refs():
    transport = FakeTransport()
    transport.queue(
        200,
        [
            {
                "link_type": "is_blocked_by",
                "issue": {
                    "id": 9,
                    "iid": 2,
                    "project_id": 42,
                    "title": "Blocking work",
                    "state": "opened",
                    "labels": ["Backend"],
                    "web_url": "https://gitlab.example.com/team/app/-/issues/2",
                },
            },
            {
                "link_type": "relates_to",
                "issue": {
                    "id": 10,
                    "iid": 3,
                    "project_id": 42,
                    "title": "Related work",
                    "state": "opened",
                    "labels": [],
                },
            },
        ],
    )
    client = GitLabClient("https://gitlab.example.com", "secret", transport=transport)

    blockers = client.fetch_issue_blockers(42, 7, "team/app", blocked_link_types=["is_blocked_by"])

    assert blockers == [
        {
            "id": 9,
            "iid": 2,
            "identifier": "team/app#2",
            "state": "opened",
            "labels": ["backend"],
            "web_url": "https://gitlab.example.com/team/app/-/issues/2",
        }
    ]


def test_malformed_json_response_maps_to_stable_error():
    class BadJsonTransport:
        def request(self, method, url, headers=None, json_body=None):
            return 200, "{not json", {"Content-Type": "application/json"}

    client = GitLabClient("https://gitlab.example.com", "secret", transport=BadJsonTransport())

    with pytest.raises(GitLabError) as excinfo:
        client.fetch_project("team/app")

    assert excinfo.value.code == "gitlab_malformed_response"
