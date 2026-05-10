from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .app_server import CodexAppServerClient
from .config import EffectiveConfig
from .models import Issue, Project, Workspace, to_context
from .template import TemplateRenderError, render_prompt


class AgentRunError(Exception):
    pass


@dataclass(slots=True)
class AgentRunResult:
    exit_code: int
    stdout: str
    stderr: str
    session_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class AgentRunner:
    def __init__(self, config: EffectiveConfig):
        self.config = config

    def run_once(self, issue: Issue, project: Project, workspace: Workspace, attempt: int | None = None) -> AgentRunResult:
        prompt = self._build_prompt(issue, project, workspace, attempt)
        if _is_codex_app_server_command(self.config.codex.command):
            result = CodexAppServerClient(
                self.config.codex.command,
                workspace.repository_path,
                self.config.codex.read_timeout_ms,
                self.config.codex.turn_timeout_ms,
                approval_policy=self.config.codex.approval_policy,
                thread_sandbox=self.config.codex.thread_sandbox,
                turn_sandbox_policy=self.config.codex.turn_sandbox_policy,
            ).run_turn(prompt)
            return AgentRunResult(
                exit_code=0,
                stdout="",
                stderr="",
                session_id=result.session_id,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                total_tokens=result.total_tokens,
            )
        try:
            completed = subprocess.run(
                ["bash", "-lc", self.config.codex.command],
                cwd=workspace.repository_path,
                input=prompt,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.config.codex.turn_timeout_ms / 1000,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentRunError(f"agent timed out after {self.config.codex.turn_timeout_ms}ms") from exc
        if completed.returncode != 0:
            raise AgentRunError(completed.stderr.strip() or f"agent exited with {completed.returncode}")
        return AgentRunResult(exit_code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)

    def _build_prompt(self, issue: Issue, project: Project, workspace: Workspace, attempt: int | None) -> str:
        template = self.config.prompt_template or (
            "You are working on a GitLab issue. Make the requested code changes, validate them, "
            "push the work branch, and prepare a merge request."
        )
        context = {
            "issue": to_context(issue),
            "project": to_context(project),
            "repository": {
                "base_branch": workspace.base_branch,
                "work_branch": workspace.work_branch,
                "remote_url": workspace.remote_url,
                "repository_path": str(workspace.repository_path),
            },
            "merge_request": to_context(issue.merge_request),
            "attempt": attempt,
        }
        try:
            return render_prompt(template, context)
        except TemplateRenderError as exc:
            raise AgentRunError(f"template_render_error: {exc}") from exc


def _is_codex_app_server_command(command: str) -> bool:
    return "codex" in command and "app-server" in command
