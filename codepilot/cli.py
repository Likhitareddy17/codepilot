"""CLI entry point (TASK-003, TASK-051 to TASK-054).

Flow: startup checks -> workspace -> controller (baseline, loop, verification)
-> cleanup -> trace/report -> summary. Everything that can be checked is
checked before a workspace exists, so a startup failure leaves nothing behind.

Exit codes: 0 = verified fix or nothing to repair, 1 = controlled stop or
unsuccessful run, 2 = startup or configuration error.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, TextIO

from codepilot.config import Settings
from codepilot.controller import Controller, RunResult, SuiteRunner
from codepilot.errors import CodePilotError
from codepilot.llm.client import GroqClient, LLMClient
from codepilot.models import StopReason
from codepilot.report import before_after, headline, write_outputs
from codepilot.trace import RunInfo, utc_now_iso, verified_success
from codepilot.workspace import Workspace, check_git_available

EXIT_OK = 0
EXIT_STOPPED = 1
EXIT_STARTUP_ERROR = 2


class StartupError(CodePilotError):
    """A problem found before the run started (printed without a traceback)."""


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        prog="codepilot", description="CodePilot AI software engineering agent."
    )
    parser.add_argument(
        "--repo", type=str, default=".",
        help="Path to the repository or sandbox repo to inspect and repair.",
    )
    parser.add_argument(
        "--task", type=str, default="Fix the failing tests",
        help="Task description or bug report for the agent.",
    )
    parser.add_argument(
        "--max-iterations", type=int, default=None, help="Maximum agent repair iterations.",
    )
    parser.add_argument(
        "--keep-workspace", action="store_true",
        help="Preserve the temporary Git workspace after the run.",
    )
    return parser.parse_args(argv)


def configure_console(streams: Optional[List[TextIO]] = None) -> None:
    """Make printing safe on any console: UTF-8, and never crash on a bad character."""
    for stream in streams if streams is not None else [sys.stdout, sys.stderr]:
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass  # not a real console stream (captured, redirected, closed)


def make_run_id(runs_dir: Path, now: Optional[datetime] = None) -> str:
    """Timestamp run id that does not collide with an existing run folder."""
    base = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    candidate, number = base, 1
    while (Path(runs_dir) / candidate).exists():
        number += 1
        candidate = f"{base}-{number}"
    return candidate


def check_startup(args: argparse.Namespace, settings: Settings, have_llm: bool) -> Path:
    """Validate everything before any workspace exists. Returns the repository path."""
    if sys.version_info < tuple(settings.min_python_version):
        needed = ".".join(map(str, settings.min_python_version))
        raise StartupError(f"Python {needed} or newer is required.")
    try:
        check_git_available()
    except CodePilotError as exc:
        raise StartupError(str(exc)) from None
    repo = Path(args.repo)
    if not repo.exists():
        raise StartupError(f"Repository not found: {repo}")
    if not repo.is_dir():
        raise StartupError(f"The repository path is not a directory: {repo}")
    if settings.max_iterations < 1:
        raise StartupError("--max-iterations must be at least 1.")
    if not have_llm and not settings.llm_api_key:
        raise StartupError("No LLM API key is configured. Set GROQ_API_KEY in your environment or .env file.")
    return repo.resolve()


def exit_code_for(result: RunResult) -> int:
    """0 only for a verified fix or a repository that needed no repair."""
    if verified_success(result) or result.stop_reason == StopReason.NO_FAILURES:
        return EXIT_OK
    return EXIT_STOPPED


def format_summary(result: RunResult, info: RunInfo, run_dir: Path) -> str:
    """The console summary (plain ASCII so any console can show it)."""
    lines = [
        f"Run {result.state.run_id}",
        f"Result: {headline(result)}",
        f"Stop reason: {result.stop_reason.value}: {result.summary}",
        f"Tests: {before_after(result)}",
    ]
    if result.commit_hash:
        lines.append(f"Commit: {result.commit_hash[:8]} on {result.state.branch}")
    lines.append(f"Outputs: {run_dir}")
    if result.patch_path:
        lines.append(f"Fix: {Path(result.patch_path)}")
    if info.workspace_kept:
        lines.append(f"Workspace kept at: {info.workspace_kept}")
    elif info.cleanup.startswith("failed"):
        lines.append(f"Warning: workspace cleanup {info.cleanup}")
    return "\n".join(lines)


def run_session(
    args: argparse.Namespace,
    settings: Settings,
    repo: Path,
    llm: LLMClient,
    suite_runner: Optional[SuiteRunner] = None,
) -> int:
    """Create the workspace, run the controller, clean up, write outputs, print the summary."""
    keep = bool(args.keep_workspace or settings.keep_workspace)
    runs_dir = Path(settings.runs_dir)
    run_id = make_run_id(runs_dir)
    workspace = Workspace(str(repo), run_id=run_id, keep_workspace=keep, timeout_s=settings.command_timeout_s)
    try:
        workspace.create()
    except CodePilotError as exc:
        raise StartupError(f"Could not create the workspace: {exc}") from None

    started = utc_now_iso()
    run_dir = runs_dir / run_id
    cleanup = "pending"
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        controller = Controller(
            workspace, llm, settings, run_dir, task=args.task,
            repository_name=repo.name, suite_runner=suite_runner,
        )
        result = controller.run()
    finally:
        try:
            removed = workspace.cleanup(keep_workspace=keep)
            cleanup = "kept" if keep else ("deleted" if removed else "failed: could not remove all files")
        except Exception as exc:  # noqa: BLE001 - cleanup problems are reported, never raised
            cleanup = f"failed: {type(exc).__name__}: {exc}"

    info = RunInfo(
        repository=str(repo), model=settings.llm_model, started_at=started, finished_at=utc_now_iso(),
        max_iterations=settings.max_iterations, cleanup=cleanup,
        workspace_kept=str(workspace.temp_dir) if keep else None,
        history_keep_recent_results=settings.history_keep_recent_results,
    )
    errors = write_outputs(run_dir, result, info, secrets=[settings.llm_api_key])
    print(format_summary(result, info, run_dir))
    for name, message in errors.items():
        print(f"Warning: could not write {name}: {message}", file=sys.stderr)
    return exit_code_for(result)


def main(
    argv: Optional[List[str]] = None,
    *,
    llm: Optional[LLMClient] = None,
    suite_runner: Optional[SuiteRunner] = None,
) -> int:
    """CLI entry point. ``llm`` and ``suite_runner`` let tests run fully offline."""
    configure_console()
    try:
        args = parse_args(argv)
    except SystemExit as exit_request:  # --help (0) or a usage error (2)
        code = exit_request.code
        return code if isinstance(code, int) else EXIT_STARTUP_ERROR
    try:
        try:
            settings = Settings()
        except ValueError as exc:
            raise StartupError(f"Invalid configuration: {exc}") from None
        if args.max_iterations is not None:
            settings.max_iterations = args.max_iterations
        repo = check_startup(args, settings, have_llm=llm is not None)
        client = llm if llm is not None else GroqClient(settings)
        return run_session(args, settings, repo, client, suite_runner)
    except CodePilotError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_STARTUP_ERROR
    except KeyboardInterrupt:
        print("Interrupted. The temporary workspace was cleaned up.", file=sys.stderr)
        return EXIT_STOPPED


if __name__ == "__main__":
    sys.exit(main())
