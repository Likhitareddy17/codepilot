"""CLI entry point for CodePilot."""

import argparse
import sys
import logging
from typing import List, Optional

from codepilot.config import Settings
from codepilot.errors import CodePilotError

logger = logging.getLogger("codepilot.cli")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="CodePilot AI software engineering agent."
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=".",
        help="Path to repository or sandbox repo to inspect and repair.",
    )
    parser.add_argument(
        "--task",
        type=str,
        default="Fix the failing tests",
        help="Task description or bug report for the agent.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Maximum agent repair iterations.",
    )
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Preserve the temporary Git workspace after run.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """Main CLI entry point function."""
    try:
        args = parse_args(argv)
        settings = Settings()
        if args.max_iterations is not None:
            settings.max_iterations = args.max_iterations

        print(f"CodePilot initializing for repository: {args.repo}")
        print(f"Task: {args.task}")
        print(f"Max iterations: {settings.max_iterations}")
        print(f"Keep workspace: {args.keep_workspace}")
        
        # Controller stub for TASK-003 (full controller to be implemented in subsequent tasks)
        print("Controller integration pending in subsequent tasks.")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
