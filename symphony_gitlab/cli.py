from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import ConfigError
from .service import SymphonyService
from .workflow import WorkflowError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="symphony-gitlab")
    parser.add_argument("workflow", nargs="?", default="WORKFLOW.md", help="Path to WORKFLOW.md")
    parser.add_argument("--validate-only", action="store_true", help="Validate startup config and exit")
    parser.add_argument("--status-json", action="store_true", help="Validate startup config and print a redacted status snapshot")
    args = parser.parse_args(argv)

    workflow_path = Path(args.workflow)
    try:
        service = SymphonyService(workflow_path)
        service.validate_startup()
        if args.status_json:
            print(json.dumps(service.status_snapshot(), indent=2, sort_keys=True))
            return 0
        if args.validate_only:
            print("ok")
            return 0
        service.run_forever()
        return 0
    except WorkflowError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 2
    except ConfigError as exc:
        print(f"config_error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"startup_error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
