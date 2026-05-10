from __future__ import annotations

from .config import EffectiveConfig
from .models import Issue, MergeRequest, Workspace
from .template import render_prompt


class MergeRequestManager:
    def __init__(self, config: EffectiveConfig, gitlab):
        self.config = config
        self.gitlab = gitlab

    def handoff(self, issue: Issue, workspace: Workspace) -> MergeRequest | None:
        if not self.config.gitlab.merge_request.create:
            return None
        target = self.config.gitlab.merge_request.target_branch or workspace.base_branch
        payload = self._payload(issue, workspace, target)
        existing = self.gitlab.list_merge_requests(issue.project_id, workspace.work_branch, target)
        if existing:
            return self.gitlab.update_merge_request(issue.project_id, existing[0].iid, payload)
        return self.gitlab.create_merge_request(issue.project_id, payload)

    def _payload(self, issue: Issue, workspace: Workspace, target: str) -> dict:
        context = {
            "issue": {"id": issue.id, "iid": issue.iid, "title": issue.title, "identifier": issue.identifier},
            "repository": {"work_branch": workspace.work_branch, "base_branch": workspace.base_branch},
        }
        payload = {
            "source_branch": workspace.work_branch,
            "target_branch": target,
            "title": render_prompt(self.config.gitlab.merge_request.title_template, context),
            "description": self._description(issue),
            "remove_source_branch": self.config.gitlab.merge_request.remove_source_branch,
            "draft": self.config.gitlab.merge_request.draft,
        }
        if self.config.gitlab.merge_request.squash is not None:
            payload["squash"] = self.config.gitlab.merge_request.squash
        if self.config.gitlab.merge_request.labels:
            payload["labels"] = ",".join(self.config.gitlab.merge_request.labels)
        return payload

    def _description(self, issue: Issue) -> str:
        if self.config.gitlab.merge_request.description_template:
            return render_prompt(
                self.config.gitlab.merge_request.description_template,
                {"issue": {"iid": issue.iid, "title": issue.title, "identifier": issue.identifier, "web_url": issue.web_url}},
            )
        parts = [f"Resolves #{issue.iid}."]
        if issue.web_url:
            parts.append(f"Issue: {issue.web_url}")
        parts.append("Prepared by Symphony.")
        return "\n\n".join(parts)
