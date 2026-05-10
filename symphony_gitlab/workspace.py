from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

from .config import EffectiveConfig
from .models import Issue, Project, Workspace
from .template import render_prompt


class WorkspaceError(Exception):
    pass


def sanitize_workspace_key(identifier: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", identifier)


def slugify_branch_component(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.lower()).strip("-._/")
    slug = slug.replace("..", "-")
    slug = re.sub(r"-+", "-", slug)
    return slug or "issue"


def render_branch_name(template: str, issue: Issue) -> str:
    context = {
        "issue": {
            "id": issue.id,
            "iid": issue.iid,
            "title": issue.title,
            "identifier": issue.identifier,
            "slug": slugify_branch_component(issue.title),
        }
    }
    branch = render_prompt(template, context)
    branch = re.sub(r"[^A-Za-z0-9._/-]", "-", branch)
    if ".." in branch or branch.startswith("/") or branch.endswith("/") or any(ord(ch) < 32 for ch in branch):
        raise WorkspaceError(f"unsafe branch name: {branch}")
    return branch


class WorkspaceManager:
    def __init__(self, config: EffectiveConfig):
        self.config = config

    def prepare(self, issue: Issue, project: Project) -> Workspace:
        root = self.config.workspace.root.resolve()
        workspace_key = sanitize_workspace_key(issue.identifier)
        workspace_path = (root / workspace_key).resolve()
        if not _is_relative_to(workspace_path, root):
            raise WorkspaceError("workspace path escaped workspace root")
        created_now = not workspace_path.exists()
        workspace_path.mkdir(parents=True, exist_ok=True)
        if created_now:
            self._run_hook("after_create", workspace_path)

        repository_path = (workspace_path / self.config.gitlab.repository.worktree_subdir).resolve()
        if not _is_relative_to(repository_path, workspace_path):
            raise WorkspaceError("repository path escaped issue workspace")

        remote_url = self.config.gitlab.selected_clone_url
        if not repository_path.exists():
            self._clone(remote_url, repository_path)
            self._run_hook("after_clone", repository_path)
        elif not (repository_path / ".git").exists():
            raise WorkspaceError(f"existing repository path is not a Git repository: {repository_path}")

        if self.config.gitlab.repository.require_origin_match:
            origin = self._git(repository_path, "remote", "get-url", "origin").strip()
            if _normalize_remote(origin) != _normalize_remote(remote_url):
                raise WorkspaceError(f"origin remote does not match configured GitLab remote: {origin}")

        base_branch = self.config.gitlab.repository.base_branch
        work_branch = render_branch_name(self.config.gitlab.repository.branch_template, issue)
        self._fetch(repository_path, base_branch, work_branch)
        self._checkout_work_branch(repository_path, base_branch, work_branch)
        before_sha = self._git(repository_path, "rev-parse", "HEAD").strip()

        return Workspace(
            path=workspace_path,
            workspace_key=workspace_key,
            repository_path=repository_path,
            project_id=project.id,
            project_path=project.path_with_namespace,
            remote_url=remote_url,
            base_branch=base_branch,
            work_branch=work_branch,
            created_now=created_now,
            commit_sha_before_run=before_sha,
            commit_sha_after_run=None,
        )

    def cleanup(self, workspace: Workspace) -> None:
        self._run_hook("before_remove", workspace.repository_path, best_effort=True)
        shutil.rmtree(workspace.path, ignore_errors=True)

    def run_before_run_hook(self, repository_path: Path) -> None:
        self._run_hook("before_run", repository_path)

    def run_after_run_hook(self, repository_path: Path) -> None:
        if repository_path is not None:
            self._run_hook("after_run", repository_path, best_effort=True)

    def push_work_branch(self, workspace: Workspace) -> bool:
        if not self.config.gitlab.repository.push_work_branch:
            return False
        status = self._git(workspace.repository_path, "status", "--porcelain").strip()
        if status:
            raise WorkspaceError("repository has uncommitted changes; refusing merge-request handoff")
        after_sha = self._git(workspace.repository_path, "rev-parse", "HEAD").strip()
        workspace.commit_sha_after_run = after_sha
        if workspace.commit_sha_before_run == after_sha and self._remote_branch_exists(workspace.repository_path, workspace.work_branch):
            return False
        if workspace.commit_sha_before_run == after_sha and not self._remote_branch_exists(workspace.repository_path, workspace.work_branch):
            return False
        self._git(workspace.repository_path, "push", "origin", f"{workspace.work_branch}:{workspace.work_branch}")
        return True

    def _clone(self, remote_url: str, repository_path: Path) -> None:
        command = ["git", "clone"]
        if self.config.gitlab.clone_depth:
            command.extend(["--depth", str(self.config.gitlab.clone_depth)])
        command.extend([remote_url, str(repository_path)])
        self._run(command)

    def _fetch(self, repository_path: Path, base_branch: str, work_branch: str) -> None:
        self._run(["git", "-C", str(repository_path), "fetch", "origin", f"{base_branch}:refs/remotes/origin/{base_branch}"])
        if self.config.gitlab.repository.reuse_existing_branch:
            self._run(["git", "-C", str(repository_path), "fetch", "origin", f"{work_branch}:{work_branch}"], check=False)

    def _checkout_work_branch(self, repository_path: Path, base_branch: str, work_branch: str) -> None:
        branches = self._git(repository_path, "branch", "--list", work_branch)
        if branches.strip():
            self._git(repository_path, "checkout", work_branch)
            return
        self._git(repository_path, "checkout", "-B", work_branch, f"origin/{base_branch}")

    def _remote_branch_exists(self, repository_path: Path, work_branch: str) -> bool:
        result = self._run(["git", "-C", str(repository_path), "ls-remote", "--exit-code", "--heads", "origin", work_branch], check=False)
        return result.returncode == 0

    def _git(self, repository_path: Path, *args: str) -> str:
        return self._run(["git", "-C", str(repository_path), *args]).stdout

    def _run_hook(self, name: str, cwd: Path, best_effort: bool = False) -> None:
        script = getattr(self.config.hooks, name)
        if not script:
            return
        try:
            subprocess.run(
                ["bash", "-lc", script],
                cwd=cwd,
                timeout=self.config.hooks.timeout_ms / 1000,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as exc:
            if not best_effort:
                raise WorkspaceError(f"{name} hook failed: {exc}") from exc

    @staticmethod
    def _run(command: Sequence[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(command, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except subprocess.CalledProcessError as exc:
            raise WorkspaceError(exc.stderr.strip() or str(exc)) from exc


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _normalize_remote(remote: str) -> str:
    return remote.rstrip("/")
