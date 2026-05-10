import os

import pytest


@pytest.mark.real_gitlab
def test_real_gitlab_profile_requires_explicit_environment():
    if os.environ.get("SYMPHONY_REAL_GITLAB") != "1":
        pytest.skip("real GitLab profile skipped; set SYMPHONY_REAL_GITLAB=1 with isolated test resources")
    required = ["GITLAB_API_TOKEN", "SYMPHONY_TEST_GITLAB_BASE_URL", "SYMPHONY_TEST_GITLAB_PROJECT"]
    missing = [name for name in required if not os.environ.get(name)]
    assert not missing, f"missing real GitLab integration variables: {', '.join(missing)}"
