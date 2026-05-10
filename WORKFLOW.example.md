---
gitlab:
  base_url: https://gitlab.com
  api_token: $GITLAB_API_TOKEN
  project: group/project
  clone_protocol: ssh
  issue:
    active_labels:
      - symphony:ready
  repository:
    base_branch: main
    branch_template: symphony/{{ issue.iid }}-{{ issue.slug }}
  merge_request:
    create: true
    draft: true
polling:
  interval_ms: 30000
workspace:
  root: ./symphony-workspaces
agent:
  max_concurrent_agents: 1
  max_turns: 20
codex:
  command: codex app-server
---

You are working on {{ issue.identifier }}.

Issue title: {{ issue.title }}

Use the repository at {{ repository.repository_path }} on branch {{ repository.work_branch }}.
Make the requested change, validate it, commit it, and leave the branch ready for a merge request.
