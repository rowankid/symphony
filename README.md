# Symphony GitLab

Symphony is a GitLab-only coding-agent orchestration service implemented from `SPEC.md`.

This repository currently provides a Python package and CLI with the core service pieces:

- `WORKFLOW.md` loader with YAML front matter and strict prompt rendering.
- Typed config defaults, `$VAR` token resolution, path normalization, and validation.
- GitLab REST v4 client for project discovery, issues, labels, notes, issue links, and merge requests.
- Per-issue workspace preparation with Git clone/fetch/branch checkout and origin safety checks.
- Hook execution with timeouts.
- Agent command runner launched in the checked-out repository path.
- Poll/claim/run/MR-handoff/error-retry orchestration path.
- CLI startup validation and long-running service entrypoint.
- Bounded concurrent dispatch, app-server JSON-RPC client support, structured JSONL logging, and status snapshots.

## Quick Start

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
```

Create a repository-owned `WORKFLOW.md`, set `GITLAB_API_TOKEN`, then validate:

```bash
export GITLAB_API_TOKEN=...
.venv/bin/python -m symphony_gitlab path/to/WORKFLOW.md --validate-only
```

Run the service:

```bash
.venv/bin/python -m symphony_gitlab path/to/WORKFLOW.md
```

## Current Scope

The implementation is intentionally conservative: it keeps a local worker model with a bounded thread pool and clear module boundaries for persistence, richer status surfaces, and remote workers later. It does not add a web UI or non-GitLab task sources.

Implementation-defined lifecycle policy: after a successful handoff, Symphony removes the configured active labels from the issue, removes `symphony:running`, and adds `symphony:review`. This prevents the same issue from being selected again on the next poll while the merge request is waiting for review.

See [CONFORMANCE.md](CONFORMANCE.md) for the implemented core behavior and explicit implementation-defined policies.
