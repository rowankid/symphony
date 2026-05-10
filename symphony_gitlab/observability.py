from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .config import EffectiveConfig
from .orchestrator import OrchestratorState


def redact_secrets(value: str, secrets: list[str] | None = None) -> str:
    redacted = value
    for secret in secrets or []:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    redacted = re.sub(r"(https?://[^:/@\s]+:)([^@\s]+)(@)", r"\1[REDACTED]\3", redacted)
    return redacted


class JsonLogger:
    def __init__(self, path: Path, secrets: list[str] | None = None):
        self.path = path
        self.secrets = secrets or []
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields: Any) -> None:
        payload = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event}
        payload.update({key: self._clean(value) for key, value in fields.items()})
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def _clean(self, value: Any) -> Any:
        if isinstance(value, str):
            return redact_secrets(value, self.secrets)
        if isinstance(value, dict):
            return {key: self._clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._clean(item) for item in value]
        return value


def build_status_snapshot(config: EffectiveConfig, state: OrchestratorState) -> dict[str, Any]:
    secrets = [config.gitlab.api_token]
    retry_entries = []
    for retry in state.retry_attempts.values():
        retry_entries.append(
            {
                "issue_id": retry.issue_id,
                "issue_iid": retry.issue_iid,
                "identifier": retry.identifier,
                "attempt": retry.attempt,
                "due_at_ms": retry.due_at_ms,
                "error": redact_secrets(retry.error or "", secrets),
            }
        )
    return {
        "project_id": state.project.id,
        "project_path": state.project.path_with_namespace,
        "poll_interval_ms": state.poll_interval_ms,
        "max_concurrent_agents": state.max_concurrent_agents,
        "running_count": len(state.running),
        "claimed_count": len(state.claimed),
        "retry_count": len(state.retry_attempts),
        "completed_count": len(state.completed),
        "running_issue_ids": [str(issue_id) for issue_id in state.running.keys()],
        "claimed_issue_ids": [str(issue_id) for issue_id in state.claimed],
        "retry_attempts": retry_entries,
        "codex_totals": state.codex_totals,
        "codex_rate_limits": state.codex_rate_limits,
    }
