# Symphony GitLab Service Specification

Status: Draft v2 (GitLab-only, language-agnostic)

Purpose: Define a service that orchestrates coding agents to get project work done using GitLab as
the only task-management, repository, branch, and merge-request channel.

## Normative Language

The key words `MUST`, `MUST NOT`, `REQUIRED`, `SHOULD`, `SHOULD NOT`, `RECOMMENDED`, `MAY`, and
`OPTIONAL` in this document are to be interpreted as described in RFC 2119.

`Implementation-defined` means the behavior is part of the implementation contract, but this
specification does not prescribe one universal policy. Implementations MUST document the selected
behavior.

## 1. Problem Statement

Symphony is a long-running automation service that continuously reads work from GitLab Issues,
creates an isolated GitLab-backed workspace for each eligible issue, and runs a coding agent session
for that issue inside the workspace.

The service solves five operational problems:

- It turns GitLab issue execution into a repeatable daemon workflow instead of manual scripts.
- It isolates agent execution in per-issue workspaces so agent commands run only inside per-issue
  workspace directories.
- It makes GitLab the authoritative source for task selection, code checkout, branch creation,
  merge-request handoff, and operator-visible issue updates.
- It keeps the workflow policy in-repository (`WORKFLOW.md`) so teams version the agent prompt and
  runtime settings with their code.
- It provides enough observability to operate and debug multiple concurrent agent runs.

Important boundaries:

- Symphony is a scheduler, GitLab reader/writer, repository preparer, and agent runner.
- GitLab Issues are the task source.
- GitLab repositories are the only code source.
- GitLab Merge Requests are the normal handoff artifact for completed code work.
- A successful run can end at a workflow-defined handoff state, such as a draft merge request with a
  review label, not necessarily a closed issue.
- Implementations MUST NOT use any non-GitLab service as a core task tracker or code retrieval
  channel for core conformance.

Implementations are expected to document their trust and safety posture explicitly. This
specification does not require a single approval, sandbox, or operator-confirmation policy; some
implementations target trusted environments with a high-trust configuration, while others require
stricter approvals or sandboxing.

## 2. Goals and Non-Goals

### 2.1 Goals

- Poll GitLab on a fixed cadence and dispatch work with bounded concurrency.
- Maintain a single authoritative orchestrator state for dispatch, retries, and reconciliation.
- Use GitLab issue labels as the scheduling state machine.
- Create deterministic per-issue workspaces and preserve them across runs.
- Clone, fetch, branch, and push only GitLab repository remotes derived from the configured GitLab
  project.
- Stop active runs when GitLab issue state or labels make them ineligible.
- Recover from transient failures with exponential backoff.
- Create or update GitLab Merge Requests as the standard code handoff.
- Load runtime behavior from a repository-owned `WORKFLOW.md` contract.
- Expose operator-visible observability, at minimum structured logs and GitLab issue notes for
  important lifecycle events.
- Support GitLab/filesystem-driven restart recovery without requiring a persistent database; exact
  in-memory scheduler state is not restored.

### 2.2 Non-Goals

- Rich web UI or multi-tenant control plane.
- Prescribing a specific dashboard or terminal UI implementation.
- General-purpose workflow engine or distributed job scheduler.
- Supporting task trackers or repository hosts other than GitLab in core conformance.
- Replacing GitLab CI, code review, approval rules, or protected branch policy.
- Mandating strong sandbox controls beyond what the coding agent and host OS provide.
- Mandating a single default approval, sandbox, or operator-confirmation posture for all
  implementations.

## 3. System Overview

### 3.1 Main Components

1. `Workflow Loader`
   - Reads `WORKFLOW.md`.
   - Parses YAML front matter and prompt body.
   - Returns `{config, prompt_template}`.

2. `Config Layer`
   - Exposes typed getters for workflow config values.
   - Applies defaults and environment variable indirection.
   - Performs validation used by the orchestrator before dispatch.

3. `GitLab Client`
   - Fetches configured project metadata.
   - Fetches candidate issues by project, state, labels, and optional filters.
   - Fetches current issue state, labels, links, and merge requests for reconciliation.
   - Normalizes GitLab payloads into stable project, issue, repository, branch, and merge-request
     models.
   - Performs narrow write operations: label claim/release, issue notes, branch creation, and merge
     request creation/update.

4. `Orchestrator`
   - Owns the poll tick.
   - Owns the in-memory runtime state.
   - Decides which issues to dispatch, retry, stop, or release.
   - Applies GitLab label-based claims.
   - Tracks session metrics and retry queue state.

5. `Workspace Manager`
   - Maps GitLab issue identifiers to workspace paths.
   - Ensures per-issue workspace directories exist.
   - Clones or fetches the configured GitLab project repository.
   - Creates or checks out the per-issue work branch.
   - Runs workspace lifecycle hooks.
   - Cleans workspaces for terminal issues when policy requires cleanup.

6. `Agent Runner`
   - Creates or prepares the workspace.
   - Builds prompt from GitLab issue/project/repository context plus workflow template.
   - Launches the coding agent app-server client.
   - Streams agent updates back to the orchestrator.

7. `Merge Request Manager`
   - Detects existing open merge requests for the issue work branch.
   - Creates or updates draft or ready merge requests according to workflow config.
   - Records merge request URLs in issue notes and observability output.

8. `Status Surface` (OPTIONAL)
   - Presents human-readable runtime status, for example terminal output, dashboard, or other
     operator-facing view.

9. `Logging`
   - Emits structured runtime logs to one or more configured sinks.

### 3.2 Abstraction Levels

Symphony is easiest to port when kept in these layers:

1. `Policy Layer` (repository-defined)
   - `WORKFLOW.md` prompt body.
   - Team-specific rules for issue handling, validation, review handoff, and cleanup.

2. `Configuration Layer` (typed getters)
   - Parses front matter into typed runtime settings.
   - Handles defaults, environment tokens, and path normalization.

3. `Coordination Layer` (orchestrator)
   - Polling loop, issue eligibility, label claims, concurrency, retries, and reconciliation.

4. `Execution Layer` (workspace + repository + agent subprocess)
   - Filesystem lifecycle, GitLab clone/fetch/branch, workspace preparation, coding-agent protocol.

5. `Integration Layer` (GitLab adapter)
   - GitLab REST API calls, Git operations against GitLab remotes, and payload normalization.

6. `Observability Layer` (logs + issue notes + OPTIONAL status surface)
   - Operator visibility into orchestrator and agent behavior.

### 3.3 External Dependencies

- GitLab REST API v4 for project, issue, issue-link, branch, and merge-request operations.
- Git CLI for repository clone/fetch/checkout/push unless the implementation uses an equivalent
  Git library.
- Local filesystem for workspaces and logs.
- Coding-agent executable that supports the targeted Codex app-server mode.
- Host environment authentication for GitLab and the coding agent.

## 4. Core Domain Model

### 4.1 Entities

#### 4.1.1 GitLab Project

Normalized GitLab project record used by repository preparation and API calls.

Fields:

- `id` (integer or string)
  - Stable GitLab project ID.
- `path_with_namespace` (string)
  - Human-readable project path, for example `group/subgroup/project`.
- `name` (string or null)
- `web_url` (string or null)
- `default_branch` (string)
- `ssh_url_to_repo` (string or null)
- `http_url_to_repo` (string or null)
- `visibility` (string or null)

#### 4.1.2 Issue

Normalized GitLab issue record used by orchestration, prompt rendering, and observability output.

Fields:

- `id` (integer or string)
  - Stable GitLab global issue ID.
- `iid` (integer)
  - Project-local issue number used by GitLab issue endpoints.
- `identifier` (string)
  - Human-readable key, formatted as `<project_path>#<iid>`.
- `project_id` (integer or string)
- `project_path` (string)
- `title` (string)
- `description` (string or null)
- `state` (string)
  - GitLab issue state, normally `opened` or `closed`.
- `labels` (list of strings)
  - Normalized to lowercase for scheduler comparisons.
- `priority` (integer or null)
  - Derived from configured priority labels or GitLab issue weight. Lower numbers sort first.
- `weight` (integer or null)
- `milestone` (object or null)
- `assignees` (list of user summaries)
- `author` (user summary or null)
- `branch_name` (string or null)
  - Work branch for this issue.
- `web_url` (string or null)
- `blocked_by` (list of blocker refs)
  - Each blocker ref contains `id`, `iid`, `identifier`, `state`, `labels`, and `web_url` when
    available.
- `merge_request` (merge request summary or null)
  - Existing or created merge request associated with the work branch.
- `created_at` (timestamp or null)
- `updated_at` (timestamp or null)

#### 4.1.3 Merge Request

Normalized GitLab merge request summary.

Fields:

- `id` (integer or string)
- `iid` (integer)
- `project_id` (integer or string)
- `title` (string)
- `description` (string or null)
- `state` (string)
  - Examples: `opened`, `closed`, `merged`.
- `draft` (boolean)
- `source_branch` (string)
- `target_branch` (string)
- `web_url` (string or null)
- `merge_status` (string or null)
- `pipeline_status` (string or null, if available)

#### 4.1.4 Workflow Definition

Parsed `WORKFLOW.md` payload:

- `config` (map)
  - YAML front matter root object.
- `prompt_template` (string)
  - Markdown body after front matter, trimmed.

#### 4.1.5 Service Config (Typed View)

Typed runtime values derived from `WorkflowDefinition.config` plus environment resolution.

Examples:

- GitLab base URL, project path, API token, clone protocol, and label policy
- poll interval
- workspace root
- concurrency limits
- coding-agent executable/args/timeouts
- merge-request behavior
- workspace hooks

#### 4.1.6 Workspace

Filesystem workspace assigned to one GitLab issue.

Fields:

- `path` (absolute workspace path)
- `workspace_key` (sanitized issue identifier)
- `repository_path` (absolute path to the checked-out repository inside the workspace)
- `project_id`
- `project_path`
- `remote_url`
- `base_branch`
- `work_branch`
- `created_now` (boolean, used to gate `after_create` hook)
- `commit_sha_before_run` (string or null)
- `commit_sha_after_run` (string or null)

#### 4.1.7 Run Attempt

One execution attempt for one issue.

Fields:

- `issue_id`
- `issue_iid`
- `issue_identifier`
- `attempt` (integer or null, `null` for first run, `>=1` for retries/continuation)
- `workspace_path`
- `repository_path`
- `work_branch`
- `started_at`
- `status`
- `error` (OPTIONAL)

#### 4.1.8 Live Session (Agent Session Metadata)

State tracked while a coding-agent subprocess is running.

Fields:

- `session_id` (string, `<thread_id>-<turn_id>`)
- `thread_id` (string)
- `turn_id` (string)
- `codex_app_server_pid` (string or null)
- `last_codex_event` (string/enum or null)
- `last_codex_timestamp` (timestamp or null)
- `last_codex_message` (summarized payload)
- `codex_input_tokens` (integer)
- `codex_output_tokens` (integer)
- `codex_total_tokens` (integer)
- `last_reported_input_tokens` (integer)
- `last_reported_output_tokens` (integer)
- `last_reported_total_tokens` (integer)
- `turn_count` (integer)

#### 4.1.9 Retry Entry

Scheduled retry state for an issue.

Fields:

- `issue_id`
- `issue_iid`
- `identifier` (best-effort human ID for status surfaces/logs)
- `attempt` (integer, 1-based for retry queue)
- `due_at_ms` (monotonic clock timestamp)
- `timer_handle` (runtime-specific timer reference)
- `error` (string or null)

#### 4.1.10 Orchestrator Runtime State

Single authoritative in-memory state owned by the orchestrator.

Fields:

- `poll_interval_ms` (current effective poll interval)
- `max_concurrent_agents` (current effective global concurrency limit)
- `project` (normalized GitLab project)
- `running` (map `issue_id -> running entry`)
- `claimed` (set of issue IDs reserved/running/retrying)
- `retry_attempts` (map `issue_id -> RetryEntry`)
- `completed` (set of issue IDs; bookkeeping only, not dispatch gating)
- `codex_totals` (aggregate tokens + runtime seconds)
- `codex_rate_limits` (latest rate-limit snapshot from agent events)

### 4.2 Stable Identifiers and Normalization Rules

- `Project ID`
  - Use for GitLab project API calls after project discovery.
- `Project Path`
  - Use for human-readable logs and prompt context.
- `Issue ID`
  - Use for internal map keys when available.
- `Issue IID`
  - Use for project-local GitLab issue endpoints.
- `Issue Identifier`
  - Format as `<project_path>#<iid>`.
- `Workspace Key`
  - Derive from `issue.identifier` by replacing any character not in `[A-Za-z0-9._-]` with `_`.
  - Use the sanitized value for the workspace directory name.
- `Normalized Issue State`
  - Compare issue states after `lowercase`.
- `Normalized Label`
  - Compare labels after `lowercase`.
- `Branch Name`
  - Derive from `gitlab.repository.branch_template`.
  - Slug components MUST replace characters outside `[A-Za-z0-9._/-]` with `-`.
  - Final branch name MUST NOT contain `..`, start with `/`, end with `/`, or contain ASCII control
    characters.
- `Session ID`
  - Compose from coding-agent `thread_id` and `turn_id` as `<thread_id>-<turn_id>`.

## 5. Workflow Specification (Repository Contract)

### 5.1 File Discovery and Path Resolution

Workflow file path precedence:

1. Explicit application/runtime setting, set by CLI startup path.
2. Default: `WORKFLOW.md` in the current process working directory.

Loader behavior:

- If the file cannot be read, return `missing_workflow_file` error.
- The workflow file is expected to be repository-owned and version-controlled.

### 5.2 File Format

`WORKFLOW.md` is a Markdown file with OPTIONAL YAML front matter.

Design note:

- `WORKFLOW.md` SHOULD be self-contained enough to describe and run different workflows (prompt,
  runtime settings, hooks, GitLab selection/config, and handoff policy) without requiring
  out-of-band service-specific configuration.

Parsing rules:

- If file starts with `---`, parse lines until the next `---` as YAML front matter.
- Remaining lines become the prompt body.
- If front matter is absent, treat the entire file as prompt body and use an empty config map.
- YAML front matter MUST decode to a map/object; non-map YAML is an error.
- Prompt body is trimmed before use.

Returned workflow object:

- `config`: front matter root object, not nested under a `config` key.
- `prompt_template`: trimmed Markdown body.

### 5.3 Front Matter Schema

Top-level keys:

- `gitlab`
- `polling`
- `workspace`
- `hooks`
- `agent`
- `codex`
- `observability`

Unknown keys SHOULD be ignored for forward compatibility.

#### 5.3.1 `gitlab` (object)

Fields:

- `base_url` (string)
  - REQUIRED for dispatch.
  - Example: `https://gitlab.com` or `https://gitlab.example.com`.
  - MUST NOT include a trailing slash after normalization.
- `api_token` (string)
  - REQUIRED for dispatch.
  - MAY be a literal token or `$VAR_NAME`.
  - Canonical environment variable: `GITLAB_API_TOKEN`.
  - If `$VAR_NAME` resolves to an empty string, treat the token as missing.
- `project` (string or integer)
  - REQUIRED for dispatch.
  - Project path, for example `group/subgroup/project`, or numeric project ID.
- `api_version` (string)
  - Default: `v4`.
  - Core conformance requires GitLab REST API v4 semantics.
- `clone_protocol` (string)
  - Allowed values: `ssh`, `https`.
  - Default: `ssh`.
- `clone_depth` (integer or null)
  - Default: `1`.
  - If `null` or `0`, perform a full clone/fetch.
- `allow_insecure_tls` (boolean)
  - Default: `false`.
  - If `true`, implementation MUST document the risk and ensure secret values are not logged.

Nested `issue` object:

- `active_labels` (list of strings)
  - REQUIRED for dispatch unless `allow_unlabeled_open_issues` is true.
  - Default: `["symphony:ready"]`.
- `allow_unlabeled_open_issues` (boolean)
  - Default: `false`.
  - If true, every open issue that passes other filters is eligible.
- `running_label` (string)
  - Default: `symphony:running`.
- `review_label` (string)
  - Default: `symphony:review`.
- `error_label` (string)
  - Default: `symphony:error`.
- `terminal_labels` (list of strings)
  - Default: `["symphony:done", "symphony:cancelled"]`.
- `closed_is_terminal` (boolean)
  - Default: `true`.
- `blocked_link_types` (list of strings)
  - Default: `["is_blocked_by"]`.
  - Implementations MAY also inspect inverse `blocks` links when GitLab returns the relation from
    the opposite direction.
- `priority_labels` (map `label -> integer`)
  - Default: `{}`.
  - Lower integer values dispatch first.
- `claim_ttl_ms` (integer)
  - Default: `3600000` (1 hour).
  - A stale `running_label` MAY be reclaimed when no local run is active and the last Symphony note
    or issue update is older than this value.
- `post_lifecycle_notes` (boolean)
  - Default: `true`.
  - If true, dispatch, error, retry exhaustion, and merge-request handoff events write issue notes.

Nested `repository` object:

- `base_branch` (string or null)
  - Default: GitLab project `default_branch`.
- `branch_template` (string)
  - Default: `symphony/{{ issue.iid }}-{{ issue.slug }}`.
- `worktree_subdir` (string)
  - Default: `repo`.
  - Repository checkout path relative to the issue workspace.
- `require_origin_match` (boolean)
  - Default: `true`.
  - If true, the checked-out repository remote MUST match the normalized GitLab project clone URL.
- `push_work_branch` (boolean)
  - Default: `true`.
  - If true, the work branch is pushed after agent success when commits or branch changes exist.
- `reuse_existing_branch` (boolean)
  - Default: `true`.
  - If true, existing remote work branches are fetched and reused.

Nested `merge_request` object:

- `create` (boolean)
  - Default: `true`.
- `draft` (boolean)
  - Default: `true`.
- `title_template` (string)
  - Default: `Resolve #{{ issue.iid }}: {{ issue.title }}`.
- `description_template` (string)
  - Default: implementation-defined, but SHOULD include the issue URL and a short generated run
    summary when available.
- `target_branch` (string or null)
  - Default: `gitlab.repository.base_branch`.
- `remove_source_branch` (boolean)
  - Default: `true`.
- `squash` (boolean or null)
  - Default: `null`, meaning GitLab project default.
- `labels` (list of strings)
  - Default: `[]`.
- `assign_to_issue_assignees` (boolean)
  - Default: `false`.

#### 5.3.2 `polling` (object)

Fields:

- `interval_ms` (integer)
  - Default: `30000`.
  - Changes SHOULD be re-applied at runtime and affect future tick scheduling without restart.

#### 5.3.3 `workspace` (object)

Fields:

- `root` (path string or `$VAR`)
  - Default: `<system-temp>/symphony_gitlab_workspaces`.
  - `~` is expanded.
  - Relative paths are resolved relative to the directory containing `WORKFLOW.md`.
  - The effective workspace root is normalized to an absolute path before use.
- `cleanup_terminal_workspaces` (boolean)
  - Default: `true`.
- `preserve_failed_workspaces` (boolean)
  - Default: `true`.

#### 5.3.4 `hooks` (object)

Fields:

- `after_create` (multiline shell script string, OPTIONAL)
  - Runs only when an issue workspace directory is newly created.
  - Failure aborts workspace creation.
- `after_clone` (multiline shell script string, OPTIONAL)
  - Runs after the GitLab repository is cloned for the first time.
  - Failure aborts workspace preparation.
- `before_run` (multiline shell script string, OPTIONAL)
  - Runs before each agent attempt after workspace preparation and before launching the coding
    agent.
  - Failure aborts the current attempt.
- `after_run` (multiline shell script string, OPTIONAL)
  - Runs after each agent attempt (success, failure, timeout, or cancellation) once the workspace
    exists.
  - Failure is logged but ignored.
- `before_remove` (multiline shell script string, OPTIONAL)
  - Runs before workspace deletion if the directory exists.
  - Failure is logged but ignored; cleanup still proceeds.
- `timeout_ms` (integer, OPTIONAL)
  - Default: `60000`.
  - Applies to all workspace hooks.
  - Invalid values fail configuration validation.

#### 5.3.5 `agent` (object)

Fields:

- `max_concurrent_agents` (integer)
  - Default: `10`.
  - Changes SHOULD be re-applied at runtime and affect subsequent dispatch decisions.
- `max_turns` (positive integer)
  - Default: `20`.
  - Limits the number of coding-agent turns within one worker session.
- `max_retry_backoff_ms` (integer)
  - Default: `300000` (5 minutes).
- `max_concurrent_agents_by_label` (map `label -> positive integer`)
  - Default: empty map.
  - Label keys are normalized for lookup.
  - Invalid entries are ignored and logged.

#### 5.3.6 `codex` (object)

Fields:

For Codex-owned config values such as `approval_policy`, `thread_sandbox`, and
`turn_sandbox_policy`, supported values are defined by the targeted Codex app-server version.
Implementors SHOULD treat them as pass-through Codex config values rather than relying on a
hand-maintained enum in this spec.

- `command` (string shell command)
  - Default: `codex app-server`.
  - The runtime launches this command via `bash -lc` in the repository path.
  - The launched process MUST speak a compatible app-server protocol over stdio.
- `approval_policy` (Codex `AskForApproval` value)
  - Default: implementation-defined.
- `thread_sandbox` (Codex `SandboxMode` value)
  - Default: implementation-defined.
- `turn_sandbox_policy` (Codex `SandboxPolicy` value)
  - Default: implementation-defined.
- `turn_timeout_ms` (integer)
  - Default: `3600000` (1 hour).
- `read_timeout_ms` (integer)
  - Default: `5000`.
- `stall_timeout_ms` (integer)
  - Default: `300000` (5 minutes).
  - If `<= 0`, stall detection is disabled.

#### 5.3.7 `observability` (object)

Fields:

- `log_gitlab_api_errors` (boolean)
  - Default: `true`.
- `redact_gitlab_token` (boolean)
  - Default: `true`.
- `emit_issue_notes` (boolean)
  - Default: inherits `gitlab.issue.post_lifecycle_notes`.
- `status_snapshot` (boolean)
  - Default: `true`.

### 5.4 Prompt Template Contract

The Markdown body of `WORKFLOW.md` is the per-issue prompt template.

Rendering requirements:

- Use a strict template engine. Liquid-compatible semantics are sufficient.
- Unknown variables MUST fail rendering.
- Unknown filters MUST fail rendering.

Template input variables:

- `issue`
  - Includes all normalized issue fields, including labels, blockers, branch name, and merge request.
- `project`
  - Includes normalized GitLab project fields.
- `repository`
  - Includes `base_branch`, `work_branch`, `remote_url`, and `repository_path`.
- `merge_request`
  - Existing or planned merge request summary, or null before handoff.
- `attempt`
  - `null` or absent on first attempt; integer on retry or continuation run.

Fallback prompt behavior:

- If the workflow prompt body is empty, the runtime MAY use a minimal default prompt:
  `You are working on a GitLab issue. Make the requested code changes, validate them, push the work branch, and prepare a merge request.`
- Workflow file read/parse failures are configuration/validation errors and SHOULD NOT silently fall
  back to a prompt.

### 5.5 Workflow Validation and Error Surface

Error classes:

- `missing_workflow_file`
- `workflow_parse_error`
- `workflow_front_matter_not_a_map`
- `template_parse_error`
- `template_render_error`

Dispatch gating behavior:

- Workflow file read/YAML errors block new dispatches until fixed.
- Template errors fail only the affected run attempt.

## 6. Configuration Specification

### 6.1 Configuration Resolution Pipeline

Configuration is resolved in this order:

1. Select the workflow file path, explicit runtime setting first, otherwise cwd default.
2. Parse YAML front matter into a raw config map.
3. Apply built-in defaults for missing OPTIONAL fields.
4. Resolve `$VAR_NAME` indirection only for config values that explicitly contain `$VAR_NAME`.
5. Fetch GitLab project metadata needed to complete defaults, such as `default_branch`.
6. Coerce and validate typed values.

Environment variables do not globally override YAML values. They are used only when a config value
explicitly references them.

Value coercion semantics:

- Path fields support `~` and `$VAR` expansion.
- Apply path expansion only to values intended to be local filesystem paths; do not rewrite URIs or
  arbitrary shell command strings.
- Relative `workspace.root` values resolve relative to the directory containing the selected
  `WORKFLOW.md`.
- GitLab `base_url` is normalized by trimming one trailing slash.
- Label comparisons use normalized lowercase values; writes SHOULD preserve configured label
  spelling.

### 6.2 Dynamic Reload Semantics

Dynamic reload is REQUIRED:

- The software MUST detect `WORKFLOW.md` changes.
- On change, it MUST re-read and re-apply workflow config and prompt template without restart.
- Reloaded config applies to future dispatch, retry scheduling, reconciliation decisions, hook
  execution, GitLab API calls, workspace preparation, merge-request behavior, and agent launches.
- Implementations are not REQUIRED to restart in-flight agent sessions automatically when config
  changes.
- Invalid reloads MUST NOT crash the service; keep operating with the last known good effective
  configuration and emit an operator-visible error.
- If the configured GitLab project changes, in-flight runs MAY continue against their original
  project and future dispatch MUST use the new project after validation succeeds.

### 6.3 Dispatch Preflight Validation

Startup validation:

- Validate configuration before starting the scheduling loop.
- If startup validation fails, fail startup and emit an operator-visible error.

Per-tick dispatch validation:

- Re-validate before each dispatch cycle.
- If validation fails, skip dispatch for that tick, keep reconciliation active for known running
  issues where possible, and emit an operator-visible error.

Validation checks:

- Workflow file can be loaded and parsed.
- `gitlab.base_url` is present and valid.
- `gitlab.api_token` is present after `$` resolution.
- `gitlab.project` is present.
- GitLab project metadata can be fetched.
- At least one project clone URL is available for the selected `clone_protocol`.
- `gitlab.issue.active_labels` is non-empty unless `allow_unlabeled_open_issues` is true.
- `codex.command` is present and non-empty.
- `workspace.root` can be resolved to an absolute path.

### 6.4 Core Config Fields Summary

- `gitlab.base_url`: string, REQUIRED
- `gitlab.api_token`: string or `$VAR`, REQUIRED, canonical env `GITLAB_API_TOKEN`
- `gitlab.project`: project path or ID, REQUIRED
- `gitlab.clone_protocol`: `ssh` or `https`, default `ssh`
- `gitlab.clone_depth`: integer or null, default `1`
- `gitlab.issue.active_labels`: list, default `["symphony:ready"]`
- `gitlab.issue.running_label`: string, default `symphony:running`
- `gitlab.issue.review_label`: string, default `symphony:review`
- `gitlab.issue.error_label`: string, default `symphony:error`
- `gitlab.issue.terminal_labels`: list, default `["symphony:done", "symphony:cancelled"]`
- `gitlab.issue.closed_is_terminal`: boolean, default `true`
- `gitlab.issue.claim_ttl_ms`: integer, default `3600000`
- `gitlab.repository.base_branch`: string or null, default project `default_branch`
- `gitlab.repository.branch_template`: string, default `symphony/{{ issue.iid }}-{{ issue.slug }}`
- `gitlab.repository.worktree_subdir`: string, default `repo`
- `gitlab.repository.require_origin_match`: boolean, default `true`
- `gitlab.merge_request.create`: boolean, default `true`
- `gitlab.merge_request.draft`: boolean, default `true`
- `gitlab.merge_request.target_branch`: string or null, default repository base branch
- `polling.interval_ms`: integer, default `30000`
- `workspace.root`: path resolved to absolute, default `<system-temp>/symphony_gitlab_workspaces`
- `hooks.timeout_ms`: integer, default `60000`
- `agent.max_concurrent_agents`: integer, default `10`
- `agent.max_turns`: integer, default `20`
- `agent.max_retry_backoff_ms`: integer, default `300000`
- `codex.command`: shell command string, default `codex app-server`
- `codex.turn_timeout_ms`: integer, default `3600000`
- `codex.read_timeout_ms`: integer, default `5000`
- `codex.stall_timeout_ms`: integer, default `300000`

## 7. Orchestration State Machine

The orchestrator is the only component that mutates scheduling state. All worker outcomes are
reported back to it and converted into explicit state transitions.

### 7.1 Internal Issue Orchestration States

This is not the same as GitLab issue state.

1. `Unclaimed`
   - Issue is not running and has no retry scheduled.

2. `Claimed`
   - Orchestrator has reserved the issue locally and, when configured, has applied the GitLab
     `running_label`.

3. `Running`
   - Worker exists and a coding-agent session may be active.

4. `RetryQueued`
   - No worker is running, but a retry timer is scheduled. The issue remains locally claimed until
     retry succeeds, the issue becomes ineligible, or retry is abandoned.

5. `Released`
   - Local claim was removed. The issue may be redispatched later if it remains eligible.

6. `Terminal`
   - GitLab issue is closed or has a configured terminal label.

### 7.2 GitLab Label Lifecycle

Default label transitions:

- Candidate issue:
  - `state=opened`
  - has `active_labels`, unless unlabeled dispatch is enabled
  - lacks `running_label`
  - lacks `terminal_labels`
  - not blocked by an open blocker

- Dispatch claim:
  - add `running_label`
  - remove `error_label` if present
  - optionally write lifecycle note

- Normal agent success:
  - push work branch if configured and changed
  - create or update merge request if configured
  - remove `running_label`
  - add `review_label`
  - optionally remove active labels if configured by implementation policy
  - write lifecycle note with merge request URL

- Agent failure:
  - remove `running_label` when the run is no longer active
  - add `error_label`
  - schedule retry when retry policy allows
  - write lifecycle note with redacted error summary

- Terminal issue:
  - stop active run
  - remove local claim
  - clean workspace if `workspace.cleanup_terminal_workspaces` is true

### 7.3 Dispatch Eligibility

An issue SHOULD dispatch only if all are true:

- The issue belongs to the configured GitLab project.
- `issue.state == opened`.
- It matches configured active label policy.
- It does not have `running_label`, unless stale claim recovery applies.
- It does not have any configured terminal label.
- It has no blockers that are still open or non-terminal.
- It is not already in `running` or `retry_attempts`.
- Dispatching it will not exceed global or label-specific concurrency limits.

### 7.4 Dispatch Sort Order

Candidate issues SHOULD be sorted by:

1. configured priority label or weight, lower first
2. oldest `created_at`
3. lowest `iid`

## 8. GitLab Integration Specification

### 8.1 Authentication

- API requests MUST use the configured `gitlab.api_token`.
- Implementations SHOULD send the token as `PRIVATE-TOKEN` or `Authorization: Bearer` according to
  their GitLab token type.
- Tokens MUST NOT be logged.
- Validation MAY check token presence and project access, but MUST NOT print token values.

### 8.2 Project Discovery

At startup and on successful config reload, the implementation MUST fetch the configured GitLab
project.

The normalized project MUST include:

- project ID
- path with namespace
- default branch
- selected clone URL

If `clone_protocol=ssh`, use `ssh_url_to_repo`.
If `clone_protocol=https`, use `http_url_to_repo`.

### 8.3 Issue Fetching

Candidate issue fetch MUST:

- query only the configured project
- request `state=opened`
- apply active label filtering either server-side or client-side
- paginate until no more pages are available or an implementation-defined page cap is reached
- normalize labels to lowercase for scheduler comparisons

State refresh MUST support fetching specific issues by IID or ID.

### 8.4 Issue Blockers

Implementations MUST inspect GitLab issue links when `blocked_link_types` is non-empty.

An issue is blocked if:

- it has a linked issue whose relation means the candidate is blocked by that linked issue; and
- the linked issue is not closed and lacks terminal labels when terminal labels can be fetched.

If blocker details cannot be fetched due to a transient GitLab error, the candidate SHOULD be
treated as not eligible for that tick and the error SHOULD be logged.

### 8.5 Issue Notes and Labels

The GitLab client MUST provide narrow operations for:

- adding labels
- removing labels
- replacing labels when implementation policy requires an atomic state transition
- adding issue notes

Lifecycle notes SHOULD be concise and SHOULD include:

- run start
- retry scheduling after failure
- final failure after retry exhaustion
- merge request handoff

Lifecycle notes MUST NOT include secrets, full environment dumps, raw tokens, or unredacted command
lines that contain credentials.

### 8.6 Repository Operations

The workspace manager MUST use the GitLab project clone URL selected from project metadata.

Repository preparation:

1. Ensure the issue workspace exists.
2. Clone the repository into `gitlab.repository.worktree_subdir` if missing.
3. If repository already exists, verify it is a Git repository.
4. If `require_origin_match` is true, verify `origin` matches the configured GitLab project clone
   URL after normalization.
5. Fetch the base branch and work branch.
6. Check out the work branch if it exists and reuse is enabled; otherwise create it from base branch.
7. Record `commit_sha_before_run`.

If origin verification fails, the run MUST fail before launching the agent.

### 8.7 Branch Push

After agent success, if `push_work_branch` is true:

- detect whether the work branch has commits or changes to push
- push the work branch to the configured GitLab project remote
- fail the run if push fails

If the agent leaves uncommitted changes and the workflow requires committed output, the run SHOULD
fail with a clear error. If the workflow explicitly allows uncommitted handoff, the implementation
MUST document how those changes are preserved.

### 8.8 Merge Request Handoff

If `gitlab.merge_request.create` is true, normal agent success MUST create or update a merge request
unless no branch changes exist and the implementation documents that no-op runs skip merge-request
creation.

Merge request lookup SHOULD search for an open merge request with:

- source branch equal to the work branch
- target branch equal to configured target branch
- project equal to configured project

If none exists, create one with configured title, description, draft flag, labels, squash, source
branch removal, assignee policy, source branch, and target branch.

After merge-request handoff:

- issue `running_label` MUST be removed
- issue `review_label` SHOULD be added
- lifecycle note SHOULD include the merge request URL

### 8.9 Error Mapping

GitLab client errors SHOULD be mapped to stable classes:

- `gitlab_auth_error`
- `gitlab_permission_error`
- `gitlab_not_found`
- `gitlab_rate_limited`
- `gitlab_validation_error`
- `gitlab_conflict`
- `gitlab_server_error`
- `gitlab_network_error`
- `gitlab_malformed_response`

Transient errors SHOULD be retried according to retry policy. Permanent errors SHOULD fail startup
or fail the affected run with an operator-visible message.

## 9. Workspace Safety Requirements

Mandatory:

- Workspace path MUST remain under configured workspace root.
- Repository path MUST remain under the issue workspace path.
- Coding-agent cwd MUST be the repository path for the current run.
- Workspace directory names MUST use sanitized identifiers.
- Git remote MUST be derived from the configured GitLab project.
- The service MUST reject workspace preparation if the existing remote points elsewhere and
  `require_origin_match` is true.

RECOMMENDED hardening:

- Run under a dedicated OS user.
- Restrict workspace root permissions.
- Use deploy keys or project-scoped tokens with the minimum required permissions.
- Prefer protected branch rules that prevent direct pushes to the base branch.

## 10. Agent Runner

The agent runner MUST:

1. Prepare the GitLab-backed workspace.
2. Run `before_run` hook if configured.
3. Start the coding-agent app-server in the repository path.
4. Render prompt using strict template input.
5. Run up to `agent.max_turns`.
6. Stop the agent session on normal completion, error, timeout, cancellation, or ineligible issue
   state.
7. Run `after_run` hook best-effort.
8. Push the work branch and create/update merge request on normal success when configured.

The agent runner SHOULD refresh the GitLab issue between turns. If the issue becomes terminal or
loses eligibility, the runner SHOULD stop before starting another turn.

## 11. Client-Side GitLab Tools (OPTIONAL)

If client-side tools are exposed to the coding-agent session, the default tool set SHOULD be narrow:

- `gitlab_get_issue`
- `gitlab_create_issue_note`
- `gitlab_add_issue_labels`
- `gitlab_remove_issue_labels`
- `gitlab_list_issue_links`
- `gitlab_get_merge_request`
- `gitlab_create_or_update_merge_request`

Tool scope requirements:

- Tools MUST be restricted to the configured project unless an extension explicitly documents a
  broader scope.
- Tools MUST reuse the service GitLab auth policy.
- Tools MUST redact tokens and secrets in errors.
- Unsupported tool names MUST fail without stalling the session.

A raw `gitlab_api` passthrough MAY be implemented as an extension, but it is not core conformance
and MUST document permission and data-exposure risks.

## 12. Observability

Structured logs SHOULD include:

- `project_id`
- `project_path`
- `issue_id`
- `issue_iid`
- `issue_identifier`
- `workspace_path`
- `repository_path`
- `work_branch`
- `merge_request_iid`
- `session_id`
- `attempt`
- `event`
- `error_class`

Operator-visible events:

- startup validation failures
- GitLab project discovery failures
- issue claim/release
- worker start/exit
- retry scheduling
- workspace preparation failure
- branch push failure
- merge request creation/update
- terminal issue cleanup

Logging sink failures MUST NOT crash orchestration.

## 13. Security and Trust

### 13.1 GitLab Is Trusted but Not Harmless

GitLab issue titles, descriptions, comments, branch names, and repository contents can contain
untrusted or adversarial text. The service MUST NOT assume prompt input is safe simply because it
comes from GitLab.

### 13.2 Secret Handling

- Support `$VAR` indirection in workflow config.
- Do not log API tokens or secret env values.
- Validate presence of secrets without printing them.
- Redact tokens from Git remote URLs before logging.

### 13.3 Git Remote Safety

- Repository remotes MUST originate from configured GitLab project metadata.
- The agent MUST NOT be launched in a repository whose origin points outside the configured GitLab
  project when `require_origin_match` is true.
- Implementations SHOULD avoid embedding tokens in persisted Git remote URLs. Prefer SSH keys,
  credential helpers, or ephemeral askpass mechanisms.

### 13.4 Hook Script Safety

Workspace hooks are arbitrary shell scripts from `WORKFLOW.md`.

Implications:

- Hooks are fully trusted configuration.
- Hooks run inside the repository path unless a hook explicitly needs workspace path and the
  implementation documents that behavior.
- Hook output SHOULD be truncated in logs.
- Hook timeouts are REQUIRED to avoid hanging the orchestrator.

### 13.5 Harness Hardening Guidance

Running coding agents against GitLab issues and repositories can lead to data leaks, destructive
mutations, or machine compromise if a permissive deployment grants excessive credentials or command
authority.

Possible hardening measures include:

- Tightening Codex approval and sandbox settings.
- Adding OS/container/VM isolation, network restrictions, or separate credentials.
- Filtering eligible GitLab issues by labels, milestones, assignees, or author trust.
- Restricting GitLab tokens to one project and minimum scopes.
- Reducing client-side tools and filesystem paths available to the agent.

## 14. Reference Algorithms

### 14.1 Service Startup

```text
function start_service():
  configure_logging()
  workflow = load_and_validate_workflow()
  project = gitlab.fetch_project(workflow.gitlab.project)
  start_observability_outputs()
  start_workflow_watch(on_change=reload_and_reapply_workflow)

  state = {
    poll_interval_ms: get_config_poll_interval_ms(),
    max_concurrent_agents: get_config_max_concurrent_agents(),
    project,
    running: {},
    claimed: set(),
    retry_attempts: {},
    completed: set(),
    codex_totals: {input_tokens: 0, output_tokens: 0, total_tokens: 0, seconds_running: 0},
    codex_rate_limits: null
  }

  validation = validate_dispatch_config()
  if validation is not ok:
    log_validation_error(validation)
    fail_startup(validation)

  startup_terminal_workspace_cleanup()
  schedule_tick(delay_ms=0)
  event_loop(state)
```

### 14.2 Poll-and-Dispatch Tick

```text
on_tick(state):
  state = reconcile_running_issues(state)

  validation = validate_dispatch_config()
  if validation is not ok:
    log_validation_error(validation)
    notify_observers()
    schedule_tick(state.poll_interval_ms)
    return state

  issues = gitlab.fetch_candidate_issues(project=state.project)
  if issues failed:
    log_gitlab_error()
    notify_observers()
    schedule_tick(state.poll_interval_ms)
    return state

  for issue in sort_for_dispatch(issues):
    if no_available_slots(state):
      break

    if should_dispatch(issue, state):
      claim = gitlab.claim_issue(issue, running_label)
      if claim failed:
        log_claim_failure()
        continue
      state = dispatch_issue(issue, state, attempt=null)

  notify_observers()
  schedule_tick(state.poll_interval_ms)
  return state
```

### 14.3 Reconcile Active Runs

```text
function reconcile_running_issues(state):
  state = reconcile_stalled_runs(state)

  running_ids = keys(state.running)
  if running_ids is empty:
    return state

  refreshed = gitlab.fetch_issues_by_ids_or_iids(running_ids)
  if refreshed failed:
    log_debug("keep workers running")
    return state

  for issue in refreshed:
    if issue is terminal:
      state = terminate_running_issue(state, issue.id, cleanup_workspace=true)
    else if issue is still eligible or has running_label:
      state.running[issue.id].issue = issue
    else:
      state = terminate_running_issue(state, issue.id, cleanup_workspace=false)

  return state
```

### 14.4 Worker Attempt

```text
function run_agent_attempt(issue, attempt, orchestrator_channel):
  workspace = workspace_manager.prepare_gitlab_workspace(issue)
  if workspace failed:
    fail_worker("workspace error")

  if run_hook("before_run", workspace.repository_path) failed:
    fail_worker("before_run hook error")

  session = app_server.start_session(workspace=workspace.repository_path)
  if session failed:
    run_hook_best_effort("after_run", workspace.repository_path)
    fail_worker("agent session startup error")

  max_turns = config.agent.max_turns
  turn_number = 1

  while true:
    prompt = build_turn_prompt(workflow_template, issue, workspace, attempt, turn_number, max_turns)
    if prompt failed:
      app_server.stop_session(session)
      run_hook_best_effort("after_run", workspace.repository_path)
      fail_worker("prompt error")

    turn_result = app_server.run_turn(
      session=session,
      prompt=prompt,
      issue=issue,
      on_message=(msg) -> send(orchestrator_channel, {codex_update, issue.id, msg})
    )

    if turn_result failed:
      app_server.stop_session(session)
      run_hook_best_effort("after_run", workspace.repository_path)
      fail_worker("agent turn error")

    issue = gitlab.refresh_issue(issue)
    if issue is not eligible for continuation:
      break

    if turn_number >= max_turns:
      break

    turn_number = turn_number + 1

  app_server.stop_session(session)
  run_hook_best_effort("after_run", workspace.repository_path)

  if repository_has_changes_to_handoff(workspace):
    git.push_work_branch(workspace)
    merge_request = gitlab.create_or_update_merge_request(issue, workspace)
    gitlab.mark_issue_review(issue, merge_request)
  else:
    gitlab.release_issue_after_noop(issue)

  exit_normal()
```

### 14.5 Worker Exit and Retry Handling

```text
on_worker_exit(issue_id, reason, state):
  running_entry = state.running.remove(issue_id)
  state = add_runtime_seconds_to_totals(state, running_entry)

  if reason == normal:
    state.completed.add(issue_id)
    state.claimed.remove(issue_id)
  else:
    gitlab.mark_issue_error(running_entry.issue, reason)
    state = schedule_retry(state, issue_id, next_attempt_from(running_entry), {
      identifier: running_entry.identifier,
      error: format("worker exited: %reason")
    })

  notify_observers()
  return state
```

## 15. Test and Validation Matrix

A conforming implementation SHOULD include tests that cover the behaviors defined in this
specification.

Validation profiles:

- `Core Conformance`: deterministic tests REQUIRED for all conforming implementations.
- `Extension Conformance`: REQUIRED only for OPTIONAL features that an implementation chooses to
  ship.
- `Real Integration Profile`: environment-dependent smoke/integration checks RECOMMENDED before
  production use.

### 15.1 Workflow and Config Parsing

- Workflow file path precedence works.
- Workflow file changes are detected and trigger re-read/re-apply without restart.
- Invalid workflow reload keeps last known good effective configuration.
- Missing `WORKFLOW.md` returns typed error.
- Invalid YAML front matter returns typed error.
- Front matter non-map returns typed error.
- Config defaults apply when OPTIONAL values are missing.
- `gitlab.base_url`, `gitlab.api_token`, and `gitlab.project` validation works.
- `$VAR` resolution works for GitLab API token and path values.
- `~` path expansion works.
- `codex.command` is preserved as a shell command string.
- Prompt template renders `issue`, `project`, `repository`, `merge_request`, and `attempt`.
- Prompt rendering fails on unknown variables.

### 15.2 GitLab Client

- Project discovery normalizes project ID, path, default branch, and clone URLs.
- Candidate issue fetch uses configured project, `state=opened`, and active labels.
- Empty active labels are rejected unless unlabeled dispatch is enabled.
- Pagination preserves order across multiple pages.
- Labels are normalized for comparisons.
- Priority labels and weights produce deterministic sorting.
- Issue state refresh by ID/IID returns normalized issues.
- Issue links produce `blocked_by` refs.
- Open blockers prevent dispatch.
- Closed or terminal blockers do not prevent dispatch.
- Error mapping covers auth, permissions, not found, rate limit, conflict, server, network, and
  malformed payloads.

### 15.3 Workspace Manager and Repository Safety

- Deterministic workspace path per issue identifier.
- Missing workspace directory is created.
- Existing workspace directory is reused.
- GitLab repository is cloned into configured subdirectory.
- Existing repository origin is verified against configured GitLab project.
- Mismatched origin fails before agent launch.
- Base branch is fetched and checked out.
- Work branch template renders deterministically and is sanitized.
- Existing remote work branch is reused when configured.
- `after_create`, `after_clone`, `before_run`, `after_run`, and `before_remove` hooks behave as
  specified.
- Agent launch uses repository path as cwd and rejects out-of-root paths.

### 15.4 Orchestrator Dispatch, Labels, Reconciliation, and Retry

- Dispatch sort order is priority, oldest creation time, then issue IID.
- Eligible issue receives `running_label` before worker starts.
- Issue with active blocker is not eligible.
- Running issue losing eligibility stops without terminal workspace cleanup.
- Closed issue or terminal label stops running agent and cleans workspace when configured.
- Reconciliation with no running issues is a no-op.
- Normal worker exit releases local claim and marks issue for review when handoff succeeds.
- Abnormal worker exit marks issue error and schedules retry.
- Retry backoff cap uses configured `agent.max_retry_backoff_ms`.
- Stale `running_label` can be reclaimed only according to `claim_ttl_ms`.
- Slot exhaustion requeues retries with explicit error reason.

### 15.5 Merge Request Handoff

- Work branch push occurs after normal success when configured.
- Push failure fails the run and marks issue error.
- Existing open merge request for source/target branch is reused.
- New merge request uses configured title, description, draft flag, labels, source branch removal,
  squash, assignee policy, source branch, and target branch.
- Issue `running_label` is removed after handoff.
- Issue `review_label` is added after handoff.
- Issue note contains merge request URL when lifecycle notes are enabled.

### 15.6 Coding-Agent App-Server Client

- Launch command uses repository cwd and invokes `bash -lc <codex.command>`.
- Session startup follows the targeted Codex app-server protocol.
- Policy-related startup payloads use the implementation's documented approval/sandbox settings.
- Thread and turn identities are extracted and used to emit `session_started`.
- Request/response read timeout is enforced.
- Turn timeout is enforced.
- Transport framing is handled correctly.
- Diagnostic stderr is kept separate from the protocol stream.
- Unsupported dynamic tool calls are rejected without stalling the session.
- Usage and rate-limit telemetry is extracted when exposed by the targeted protocol.
- If GitLab client-side tools are implemented, they are advertised with project-scoped tool specs.

### 15.7 Observability

- Validation failures are operator-visible.
- Structured logging includes project, issue, workspace, branch, merge request, and session context.
- Logging sink failures do not crash orchestration.
- Token/rate-limit aggregation remains correct across repeated agent updates.
- Lifecycle issue notes are emitted when enabled.
- Issue notes redact secrets and tokens.

### 15.8 CLI and Host Lifecycle

- CLI accepts a positional workflow path argument (`path-to-WORKFLOW.md`).
- CLI uses `./WORKFLOW.md` when no workflow path argument is provided.
- CLI errors on nonexistent explicit workflow path or missing default `./WORKFLOW.md`.
- CLI surfaces startup failure cleanly.
- CLI exits with success when application starts and shuts down normally.
- CLI exits nonzero when startup fails or the host process exits abnormally.

### 15.9 Real Integration Profile (RECOMMENDED)

- A real GitLab smoke test can run with valid credentials supplied by `GITLAB_API_TOKEN`.
- Real integration tests SHOULD use isolated test projects, labels, branches, and issues.
- Tests SHOULD clean up temporary branches, merge requests, labels, and issues when practical.
- A skipped real-integration test SHOULD be reported as skipped, not silently treated as passed.
- If a real-integration profile is explicitly enabled in CI or release validation, failures SHOULD
  fail that job.

## 16. Implementation Checklist

### 16.1 REQUIRED for Core Conformance

- Workflow path selection supports explicit runtime path and cwd default.
- `WORKFLOW.md` loader with YAML front matter plus prompt body split.
- Typed config layer with defaults and `$` resolution.
- Dynamic `WORKFLOW.md` watch/reload/re-apply for config and prompt.
- GitLab client with project discovery, candidate issue fetch, state refresh, issue-link fetch,
  label writes, issue notes, branch support, and merge-request support.
- Polling orchestrator with single-authority mutable state.
- Label-based claim/release lifecycle.
- Workspace manager with sanitized per-issue workspaces.
- GitLab repository clone/fetch/checkout from configured project only.
- Work branch creation/reuse from configured branch template.
- Workspace lifecycle hooks (`after_create`, `after_clone`, `before_run`, `after_run`,
  `before_remove`).
- Hook timeout config (`hooks.timeout_ms`, default `60000`).
- Coding-agent app-server subprocess client.
- Codex launch command config (`codex.command`, default `codex app-server`).
- Strict prompt rendering with `issue`, `project`, `repository`, `merge_request`, and `attempt`.
- Exponential retry queue.
- Configurable retry backoff cap.
- Reconciliation that stops runs on terminal/non-active GitLab issue state.
- Merge request create/update handoff.
- Structured logs with GitLab project, issue, branch, merge request, workspace, and session context.
- Secret redaction for GitLab tokens and credential-bearing remotes.

### 16.2 RECOMMENDED Extensions

- HTTP status snapshot endpoint with safe default bind host.
- Project-scoped GitLab client-side tools for the coding agent.
- Persistent retry queue and session metadata across process restarts.
- Configurable observability sinks in workflow front matter.
- Group-level project scanning for explicitly configured project allowlists.
- Remote worker execution over SSH using the same GitLab-only project and repository constraints.

### 16.3 Operational Validation Before Production

- Run the real GitLab integration profile with valid credentials and isolated test resources.
- Verify clone protocol, credential helper, and push permissions on the target host.
- Verify protected branch settings prevent direct base-branch mutation.
- Verify hook execution and workflow path resolution on the target host OS/shell environment.
- Verify merge request creation and label transitions in a non-production project before enabling
  production dispatch.

## Appendix A. SSH Worker Extension (OPTIONAL)

This appendix describes an extension profile in which Symphony keeps one central orchestrator but
executes worker runs on one or more remote hosts over SSH.

Extension config:

- `worker.ssh_hosts` (list of SSH host strings, OPTIONAL)
  - When omitted, work runs locally.
- `worker.max_concurrent_agents_per_host` (positive integer, OPTIONAL)
  - Shared per-host cap applied across configured SSH hosts.

Execution model:

- The orchestrator remains the single source of truth for polling, claims, retries, and
  reconciliation.
- Each remote host MUST satisfy the same GitLab project, token, clone, repository-origin, and
  workspace-boundary requirements as local execution.
- `workspace.root` is interpreted on the remote host.
- The coding-agent app-server is launched over SSH stdio, but the orchestrator still owns session
  lifecycle and GitLab label/MR state.
- Continuation turns inside one worker lifetime SHOULD stay on the same host and workspace.

Scheduling notes:

- SSH hosts MAY be treated as a pool for dispatch.
- Implementations MAY prefer the previously used host on retries when that host is still available.
- When all SSH hosts are at capacity, dispatch SHOULD wait rather than silently falling back to a
  different execution mode.
- Once a run has already produced side effects, a transparent rerun on another host SHOULD be
  treated as a new attempt, not invisible failover.
