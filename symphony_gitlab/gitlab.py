from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

from .models import Issue, MergeRequest, Project


class GitLabError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Transport(Protocol):
    def request(self, method: str, url: str, headers: dict[str, str] | None = None, json_body: Any = None) -> tuple[int, Any, dict[str, str]]:
        ...


class UrllibTransport:
    def request(self, method: str, url: str, headers: dict[str, str] | None = None, json_body: Any = None) -> tuple[int, Any, dict[str, str]]:
        data = None
        request_headers = dict(headers or {})
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                return response.status, body, dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return exc.code, body, dict(exc.headers.items())
        except urllib.error.URLError as exc:
            raise GitLabError("gitlab_network_error", f"gitlab_network_error: {exc.reason}") from exc


class GitLabClient:
    def __init__(self, base_url: str, api_token: str, api_version: str = "v4", transport: Transport | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.api_version = api_version
        self.transport = transport or UrllibTransport()

    def fetch_project(self, project: str | int) -> Project:
        encoded = urllib.parse.quote(str(project), safe="")
        data, _headers = self._request("GET", f"/projects/{encoded}")
        return _normalize_project(data)

    def fetch_candidate_issues(self, project_id: int | str, project_path: str, active_labels: list[str]) -> list[Issue]:
        issues: list[Issue] = []
        page = 1
        while True:
            query = {"state": "opened", "per_page": "100", "page": str(page)}
            if active_labels:
                query["labels"] = ",".join(active_labels)
            data, headers = self._request("GET", f"/projects/{project_id}/issues", query=query)
            if not isinstance(data, list):
                raise GitLabError("gitlab_malformed_response", "gitlab_malformed_response: expected issue list")
            issues.extend(_normalize_issue(item, project_path) for item in data)
            next_page = headers.get("X-Next-Page") or headers.get("x-next-page")
            if not next_page:
                break
            page = int(next_page)
        return issues

    def fetch_issue(self, project_id: int | str, issue_iid: int, project_path: str) -> Issue:
        data, _headers = self._request("GET", f"/projects/{project_id}/issues/{issue_iid}")
        return _normalize_issue(data, project_path)

    def list_issue_links(self, project_id: int | str, issue_iid: int, project_path: str) -> list[dict[str, Any]]:
        data, _headers = self._request("GET", f"/projects/{project_id}/issues/{issue_iid}/links")
        if not isinstance(data, list):
            raise GitLabError("gitlab_malformed_response", "gitlab_malformed_response: expected issue-link list")
        return data

    def fetch_issue_blockers(self, project_id: int | str, issue_iid: int, project_path: str, blocked_link_types: list[str]) -> list[dict[str, Any]]:
        blocked_types = {link_type.lower() for link_type in blocked_link_types}
        blockers: list[dict[str, Any]] = []
        for link in self.list_issue_links(project_id, issue_iid, project_path):
            if str(link.get("link_type", "")).lower() not in blocked_types:
                continue
            linked_issue = link.get("issue") or link.get("target_issue") or link.get("source_issue")
            if not isinstance(linked_issue, dict):
                continue
            iid = int(linked_issue["iid"])
            linked_project_path = linked_issue.get("references", {}).get("full") if isinstance(linked_issue.get("references"), dict) else None
            identifier = linked_project_path or f"{project_path}#{iid}"
            blockers.append(
                {
                    "id": linked_issue["id"],
                    "iid": iid,
                    "identifier": identifier,
                    "state": str(linked_issue.get("state", "")).lower(),
                    "labels": [str(label).lower() for label in linked_issue.get("labels", [])],
                    "web_url": linked_issue.get("web_url"),
                }
            )
        return blockers

    def add_issue_labels(self, project_id: int | str, issue_iid: int, labels: list[str]) -> None:
        self._request("PUT", f"/projects/{project_id}/issues/{issue_iid}", body={"add_labels": ",".join(labels)})

    def remove_issue_labels(self, project_id: int | str, issue_iid: int, labels: list[str]) -> None:
        self._request("PUT", f"/projects/{project_id}/issues/{issue_iid}", body={"remove_labels": ",".join(labels)})

    def replace_issue_labels(self, project_id: int | str, issue_iid: int, labels: list[str]) -> None:
        self._request("PUT", f"/projects/{project_id}/issues/{issue_iid}", body={"labels": ",".join(labels)})

    def create_issue_note(self, project_id: int | str, issue_iid: int, body: str) -> None:
        self._request("POST", f"/projects/{project_id}/issues/{issue_iid}/notes", body={"body": body})

    def create_branch(self, project_id: int | str, branch: str, ref: str) -> None:
        self._request("POST", f"/projects/{project_id}/repository/branches", body={"branch": branch, "ref": ref})

    def list_merge_requests(self, project_id: int | str, source_branch: str, target_branch: str) -> list[MergeRequest]:
        query = {"state": "opened", "source_branch": source_branch, "target_branch": target_branch}
        data, _headers = self._request("GET", f"/projects/{project_id}/merge_requests", query=query)
        if not isinstance(data, list):
            raise GitLabError("gitlab_malformed_response", "gitlab_malformed_response: expected merge-request list")
        return [_normalize_merge_request(item) for item in data]

    def create_merge_request(self, project_id: int | str, payload: dict[str, Any]) -> MergeRequest:
        data, _headers = self._request("POST", f"/projects/{project_id}/merge_requests", body=payload)
        return _normalize_merge_request(data)

    def update_merge_request(self, project_id: int | str, mr_iid: int, payload: dict[str, Any]) -> MergeRequest:
        data, _headers = self._request("PUT", f"/projects/{project_id}/merge_requests/{mr_iid}", body=payload)
        return _normalize_merge_request(data)

    def _request(self, method: str, path: str, query: dict[str, str] | None = None, body: Any = None) -> tuple[Any, dict[str, str]]:
        query_string = f"?{urllib.parse.urlencode(query)}" if query else ""
        url = f"{self.base_url}/api/{self.api_version}{path}{query_string}"
        status, response_body, response_headers = self.transport.request(method, url, headers={"PRIVATE-TOKEN": self.api_token}, json_body=body)
        if status >= 400:
            raise GitLabError(_map_status(status), _redact(f"{_map_status(status)}: {response_body}", self.api_token))
        if isinstance(response_body, (dict, list)):
            return response_body, response_headers
        if response_body in {None, ""}:
            return {}, response_headers
        try:
            return json.loads(response_body), response_headers
        except Exception as exc:
            raise GitLabError("gitlab_malformed_response", "gitlab_malformed_response: invalid JSON response") from exc


def _normalize_project(data: dict[str, Any]) -> Project:
    try:
        return Project(
            id=data["id"],
            path_with_namespace=data["path_with_namespace"],
            name=data.get("name"),
            web_url=data.get("web_url"),
            default_branch=data["default_branch"],
            ssh_url_to_repo=data.get("ssh_url_to_repo"),
            http_url_to_repo=data.get("http_url_to_repo"),
            visibility=data.get("visibility"),
        )
    except KeyError as exc:
        raise GitLabError("gitlab_malformed_response", f"gitlab_malformed_response: missing {exc.args[0]}") from exc


def _normalize_issue(data: dict[str, Any], project_path: str) -> Issue:
    labels = [str(label).lower() for label in data.get("labels", [])]
    iid = int(data["iid"])
    priority = data.get("priority")
    return Issue(
        id=data["id"],
        iid=iid,
        identifier=f"{project_path}#{iid}",
        project_id=data.get("project_id"),
        project_path=project_path,
        title=data.get("title", ""),
        description=data.get("description"),
        state=str(data.get("state", "")).lower(),
        labels=labels,
        priority=priority,
        weight=data.get("weight"),
        milestone=data.get("milestone"),
        assignees=data.get("assignees") or [],
        author=data.get("author"),
        branch_name=data.get("branch_name"),
        web_url=data.get("web_url"),
        blocked_by=data.get("blocked_by") or [],
        merge_request=data.get("merge_request"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


def _normalize_merge_request(data: dict[str, Any]) -> MergeRequest:
    return MergeRequest(
        id=data["id"],
        iid=int(data["iid"]),
        project_id=data["project_id"],
        title=data.get("title", ""),
        description=data.get("description"),
        state=str(data.get("state", "")).lower(),
        draft=bool(data.get("draft", data.get("work_in_progress", False))),
        source_branch=data.get("source_branch", ""),
        target_branch=data.get("target_branch", ""),
        web_url=data.get("web_url"),
        merge_status=data.get("merge_status"),
        pipeline_status=(data.get("head_pipeline") or {}).get("status") if isinstance(data.get("head_pipeline"), dict) else None,
    )


def _map_status(status: int) -> str:
    if status in {401, 403}:
        return "gitlab_auth_error" if status == 401 else "gitlab_permission_error"
    if status == 404:
        return "gitlab_not_found"
    if status == 409:
        return "gitlab_conflict"
    if status == 422 or status == 400:
        return "gitlab_validation_error"
    if status == 429:
        return "gitlab_rate_limited"
    if status >= 500:
        return "gitlab_server_error"
    return "gitlab_network_error"


def _redact(message: str, token: str) -> str:
    return message.replace(token, "[REDACTED]") if token else message
