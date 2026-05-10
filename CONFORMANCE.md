# Symphony GitLab Conformance Notes

This implementation targets the core GitLab-only service contract in `SPEC.md`.

## Implemented Core

- Workflow path selection and `WORKFLOW.md` parsing with optional YAML front matter.
- Typed config defaults, `$VAR` resolution, clone URL selection, path normalization, and validation.
- Dynamic reload with last-known-good config retention on invalid reloads.
- GitLab REST v4 project discovery, issue fetch/pagination, issue refresh, issue links/blockers, labels, notes, branch creation, and merge request create/update.
- Label state machine for ready/running/error/review, including stale running-label reclaim via `claim_ttl_ms`.
- Per-issue workspace mapping, clone/fetch/checkout, branch sanitization, origin verification, hooks, cleanup, branch push, and uncommitted-change guard.
- Polling orchestration, retry queue, terminal/non-active reconciliation, terminal cleanup, and bounded thread-pool dispatch.
- Codex app-server stdio JSON-RPC client for `initialize`, `thread/start`, `turn/start`, `turn/completed`, token usage updates, and unsupported server-request rejection.
- Strict prompt rendering with unknown-variable and unknown-filter failure.
- Merge request handoff with existing MR update or new MR creation.
- Structured JSONL logs, issue lifecycle notes, redaction, and status snapshot CLI output.
- Real GitLab integration profile gate: skipped unless explicitly enabled with isolated test environment variables.

## Implementation-Defined Policies

- Successful handoff removes configured active labels, removes the running label, and adds the review label.
- Stale running-label reclaim uses GitLab issue `updated_at` as the age signal.
- Structured logs default to `<workspace.root>/symphony.jsonl`; `observability.log_path` can override this path.
- Codex app-server unsupported server requests receive JSON-RPC `-32601` errors.
- The default local service uses a bounded thread pool sized by `agent.max_concurrent_agents`.
- Persistent retry/session recovery across process restarts is not stored in a database; restart recovery is GitLab/filesystem-driven as allowed by the specification.

## Optional Extensions

Not implemented as core behavior because the specification marks them optional:

- SSH remote worker extension.
- Raw `gitlab_api` dynamic tool passthrough.
- Group-level project scanning.
- Persistent database-backed retry/session metadata.
