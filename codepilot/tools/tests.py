"""Test runner, JUnit parsing, ``run_tests`` tool and baseline evidence.

Covers TASK-015 (run_tests), TASK-024 (structured test execution) and
TASK-026 (baseline evidence). pytest is always started as
``sys.executable -m pytest`` in its own process group, with a sanitised
environment and a hard timeout that kills the whole process tree.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from codepilot.config import Settings
from codepilot.errors import ToolFailure, WorkspaceError
from codepilot.models import BaselineEvidence, PytestOutcome, TestRunResult
from codepilot.safety import ensure_readable, is_within, resolve_path, sanitized_env
from codepilot.tools import ToolContext, ToolOutput, ToolSpec, json_len

EXIT_CODE_TIMEOUT = -1
MAX_CAPTURE_BYTES = 1_000_000      # per stream kept in memory
MAX_JUNIT_BYTES = 10_000_000       # larger XML files fall back to the text summary
DEFAULT_EXCERPT_CHARS = 3000
MAX_MESSAGE_CHARS = 200
TOOL_MAX_FAILING_IDS = 50
TOOL_MAX_MESSAGES = 20

# Pytest options that must never come from the environment.
_DROPPED_ENV = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")


# --------------------------------------------------------------- parsing


@dataclass
class ParsedRun:
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    failing_ids: List[str] = field(default_factory=list)
    messages: Dict[str, str] = field(default_factory=dict)


def normalize_message(text: str) -> str:
    """First meaningful line, whitespace collapsed, addresses masked, capped."""
    first = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    first = re.sub(r"0x[0-9a-fA-F]+", "0x?", first)
    first = re.sub(r"\s+", " ", first)
    return first[:MAX_MESSAGE_CHARS]


def _case_message(element: ET.Element) -> str:
    message = element.get("message") or ""
    body = element.text or ""
    if not message.strip() or message.strip() == "collection failure":
        for line in body.splitlines():
            if line.startswith("E "):
                return normalize_message(line[1:])
    return normalize_message(message or body)


def _test_id(case: ET.Element, root: Path) -> str:
    """Rebuild the pytest node id (``path::Class::test[param]``) from a testcase."""
    classname = case.get("classname") or ""
    name = case.get("name") or ""
    file = (case.get("file") or "").replace("\\", "/")
    if file:
        if not classname:
            return file  # collection error: the id is the file
        module = file[:-3].replace("/", ".") if file.endswith(".py") else file
        rest = classname[len(module):].lstrip(".") if classname.startswith(module) else ""
        return "::".join([file, *[p for p in rest.split(".") if p], name])
    # No file attribute: find the module file on disk.
    parts = (classname or name).split(".")
    for i in range(len(parts), 0, -1):
        candidate = "/".join(parts[:i]) + ".py"
        if (root / candidate).is_file():
            return candidate if not classname else "::".join([candidate, *parts[i:], name])
    return "::".join([classname, name]) if classname else name


def parse_junit(xml_text: str, root: Path) -> Optional[ParsedRun]:
    """Parse pytest JUnit XML. Returns None for empty or malformed input."""
    if not xml_text or not xml_text.strip():
        return None
    try:
        tree = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    cases = list(tree.iter("testcase"))
    if not cases and next(tree.iter("testsuite"), None) is None:
        return None
    parsed = ParsedRun()
    failing: Dict[str, str] = {}
    for case in cases:
        failure, error = case.find("failure"), case.find("error")
        if failure is not None or error is not None:
            if failure is not None:
                parsed.failed += 1
            else:
                parsed.errors += 1
            test_id = _test_id(case, root)
            element = failure if failure is not None else error
            failing.setdefault(test_id, _case_message(element))
        elif case.find("skipped") is not None:
            parsed.skipped += 1
        else:
            parsed.passed += 1
    parsed.failing_ids = sorted(failing)
    parsed.messages = {k: failing[k] for k in parsed.failing_ids}
    return parsed


_SUMMARY_COUNT = re.compile(r"(\d+) (failed|passed|skipped|errors?)\b")
_SUMMARY_FAIL_LINE = re.compile(r"^(FAILED|ERROR) (\S+?)(?: - (.*))?$", re.MULTILINE)


def parse_summary_output(stdout: str) -> ParsedRun:
    """Fallback when JUnit is missing: read pytest's own text summary."""
    parsed = ParsedRun()
    last_line = next((ln for ln in reversed(stdout.splitlines()) if _SUMMARY_COUNT.search(ln)), "")
    for count, label in _SUMMARY_COUNT.findall(last_line):
        n = int(count)
        if label == "passed":
            parsed.passed = n
        elif label == "failed":
            parsed.failed = n
        elif label == "skipped":
            parsed.skipped = n
        else:
            parsed.errors = n
    messages: Dict[str, str] = {}
    for _kind, test_id, message in _SUMMARY_FAIL_LINE.findall(stdout):
        messages.setdefault(test_id, normalize_message(message))
    parsed.failing_ids = sorted(messages)
    parsed.messages = {k: messages[k] for k in parsed.failing_ids}
    return parsed


def _main_error(parsed: ParsedRun, stdout: str, exit_code: int) -> Optional[str]:
    """Message of the first failing test (by sorted id): the run's 'main error'."""
    for test_id in parsed.failing_ids:
        if parsed.messages.get(test_id):
            return parsed.messages[test_id]
    if exit_code != 0:
        last = next((ln for ln in reversed(stdout.splitlines()) if ln.strip()), "")
        return normalize_message(last) or None
    return None


def _build_excerpt(stdout: str, stderr: str, limit: int) -> str:
    match = re.search(r"^=+ (?:FAILURES|ERRORS) =+$", stdout, re.MULTILINE)
    text = stdout[match.start():] if match else stdout[-min(limit, 400):]
    text = text.strip()
    if stderr.strip():
        text += "\n[stderr]\n" + stderr.strip()[-max(limit // 4, 200):]
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n... [truncated]"
    return text


# --------------------------------------------------------------- execution


def _read_capped(path: Path) -> str:
    """Read a captured stream, keeping the head and tail if it is huge."""
    size = path.stat().st_size
    with open(path, "rb") as handle:
        if size <= MAX_CAPTURE_BYTES:
            data = handle.read()
        else:
            half = MAX_CAPTURE_BYTES // 2
            head = handle.read(half)
            handle.seek(size - half)
            data = head + b"\n... [output truncated] ...\n" + handle.read()
    return data.decode("utf-8", errors="replace")


def kill_process_tree(proc: "subprocess.Popen[Any]") -> None:
    """Terminate pytest and every process it started."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True, timeout=15, check=False,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # the child leads its own process group
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_pytest(
    root: Path,
    run_dir: Path,
    settings: Settings,
    target: Optional[str] = None,
    excerpt_chars: int = DEFAULT_EXCERPT_CHARS,
) -> PytestOutcome:
    """Run pytest in ``root`` and return structured results plus raw output.

    ``target=None`` is a full-suite run; any target makes the result
    ``scope="targeted"``. JUnit XML is written to ``run_dir`` (outside ``root``).
    """
    root = Path(os.path.realpath(root))
    run_dir = Path(os.path.realpath(run_dir))
    if is_within(run_dir, root):
        raise WorkspaceError("The run directory must be outside the workspace.")
    if target is not None and (not target.strip() or target.startswith("-")):
        raise ValueError("Test target must be a path or test id, not an option.")
    run_dir.mkdir(parents=True, exist_ok=True)

    junit_path = run_dir / "junit.xml"
    junit_path.unlink(missing_ok=True)  # never read a stale file from an earlier run
    cmd = [
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "-o", "junit_family=xunit1", f"--junitxml={junit_path}",
    ]
    if target is not None:
        cmd.append(target)

    env = sanitized_env({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"})
    for name in _DROPPED_ENV:
        env.pop(name, None)

    popen_kwargs: Dict[str, Any] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        popen_kwargs["start_new_session"] = True

    out_fd, out_name = tempfile.mkstemp(prefix="pytest_", suffix=".out", dir=run_dir)
    err_fd, err_name = tempfile.mkstemp(prefix="pytest_", suffix=".err", dir=run_dir)
    out_path, err_path = Path(out_name), Path(err_name)
    timed_out = False
    started = time.monotonic()
    try:
        with os.fdopen(out_fd, "wb") as out_file, os.fdopen(err_fd, "wb") as err_file:
            proc = subprocess.Popen(
                cmd, cwd=root, env=env, stdin=subprocess.DEVNULL,
                stdout=out_file, stderr=err_file, **popen_kwargs,
            )
            try:
                exit_code = proc.wait(timeout=settings.command_timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_process_tree(proc)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                exit_code = EXIT_CODE_TIMEOUT
            else:
                if os.name != "nt":  # clean up any orphans pytest left behind
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
        duration = round(time.monotonic() - started, 3)
        stdout, stderr = _read_capped(out_path), _read_capped(err_path)
    finally:
        out_path.unlink(missing_ok=True)
        err_path.unlink(missing_ok=True)

    parsed: Optional[ParsedRun] = None
    if not timed_out and junit_path.is_file() and junit_path.stat().st_size <= MAX_JUNIT_BYTES:
        parsed = parse_junit(junit_path.read_text(encoding="utf-8", errors="replace"), root)
    if parsed is None:
        parsed = parse_summary_output(stdout) if not timed_out else ParsedRun()

    excerpt = _build_excerpt(stdout, stderr, excerpt_chars)
    main_error = _main_error(parsed, stdout, exit_code)
    if timed_out:
        note = f"Test run timed out after {settings.command_timeout_s}s and was stopped."
        excerpt = (note + "\n" + excerpt).strip()[:excerpt_chars]
        main_error = f"Test run timed out after {settings.command_timeout_s}s"

    result = TestRunResult(
        exit_code=exit_code,
        passed=parsed.passed,
        failed=parsed.failed,
        errors=parsed.errors,
        failing_ids=parsed.failing_ids,
        duration_s=duration,
        timed_out=timed_out,
        output_excerpt=excerpt,
        skipped=parsed.skipped,
        scope="full" if target is None else "targeted",
        main_error=main_error,
        failure_messages=parsed.messages,
    )
    return PytestOutcome(
        result=result, stdout=stdout, stderr=stderr,
        junit_path=str(junit_path) if junit_path.is_file() else None, command=cmd,
    )


# ----------------------------------------------------------------- baseline


def evaluate_baseline(outcome: PytestOutcome) -> BaselineEvidence:
    """Decide from the baseline run whether a repair is needed (TASK-026)."""
    r = outcome.result
    if r.timed_out:
        return BaselineEvidence(BaselineEvidence.UNUSABLE, "The baseline test run timed out.", outcome)
    if r.all_passing:
        return BaselineEvidence(BaselineEvidence.NO_FAILURES, "The full test suite already passes.", outcome)
    if r.exit_code in (3, 4):
        return BaselineEvidence(
            BaselineEvidence.UNUSABLE,
            f"pytest could not run the suite (exit code {r.exit_code}): {r.main_error or 'unknown error'}.",
            outcome,
        )
    if r.exit_code == 5 or (r.passed + r.failed + r.errors + r.skipped) == 0:
        return BaselineEvidence(BaselineEvidence.UNUSABLE, "No tests were collected, so there is nothing to verify a fix against.", outcome)
    if r.failing_ids or r.failed or r.errors:
        return BaselineEvidence(BaselineEvidence.NEEDS_REPAIR, f"{len(r.failing_ids) or r.failed + r.errors} failing test(s) found.", outcome)
    return BaselineEvidence(BaselineEvidence.UNUSABLE, "The suite ran but no test passed or failed (all skipped?).", outcome)


def capture_baseline(root: Path, run_dir: Path, settings: Settings) -> BaselineEvidence:
    """Run the full suite once at the start of a run and classify the result."""
    return evaluate_baseline(run_pytest(root, run_dir, settings, target=None))


# --------------------------------------------------------------------- tool


def run_tests(ctx: ToolContext, args: Dict[str, Any]) -> ToolOutput:
    """Run pytest for the whole suite or one file / directory / test id."""
    if ctx.run_dir is None:
        raise ToolFailure("INTERNAL_ERROR", "No run directory is configured for test results.")
    target = args.get("target")
    argv_target: Optional[str] = None
    if target is not None:
        file_part, separator, rest = target.partition("::")
        if not file_part.strip() or file_part.lstrip().startswith("-"):
            raise ToolFailure("INVALID_ARGUMENTS", "target must be a test path or test id such as tests/test_orders.py::test_total.")
        rp = resolve_path(ctx.root, file_part)
        ensure_readable(rp, ctx.settings.read_denylist)
        if not rp.abs_path.exists():
            raise ToolFailure("NOT_FOUND", f"{file_part!r} does not exist. Use list_files to find test files.")
        rel = rp.rel or "."
        argv_target = ("./" + rel if rel.startswith("-") else rel) + separator + rest

    outcome = run_pytest(
        ctx.root, ctx.run_dir, ctx.settings, argv_target,
        excerpt_chars=max(ctx.budget - 1500, 500),
    )
    r = outcome.result
    if not r.timed_out and r.exit_code in (3, 4):
        text = (outcome.stdout + "\n" + outcome.stderr).lower()
        if r.exit_code == 4 and "not found" in text:
            raise ToolFailure("NOT_FOUND", f"pytest could not find the target {target!r}. Check the test id with search_code.")
        raise ToolFailure("INTERNAL_ERROR", f"pytest could not run (exit code {r.exit_code}): {r.main_error or 'unknown error'}")

    data = r.to_dict()
    data["all_passing"] = r.all_passing
    truncated = r.output_excerpt.endswith("[truncated]")
    if json_len(data) > ctx.budget:
        truncated = True
        for max_ids, max_messages in ((50, 20), (30, 10), (20, 5), (10, 3), (5, 1)):
            data["failing_ids"] = r.failing_ids[:max_ids]
            data["failure_messages"] = {
                k: r.failure_messages[k] for k in data["failing_ids"][:max_messages] if k in r.failure_messages
            }
            excerpt = r.output_excerpt
            data["output_excerpt"] = excerpt
            while json_len(data) > ctx.budget and excerpt:
                excerpt = excerpt[: int(len(excerpt) * 0.8)]
                data["output_excerpt"] = excerpt
            if json_len(data) <= ctx.budget:
                break
    return data, truncated


RUN_TESTS = ToolSpec(
    name="run_tests",
    description=(
        "Run the project's tests with pytest. With no target it runs the full suite. "
        "A target can be a test file, a directory, or one test id such as "
        "tests/test_orders.py::test_total. Always confirm a fix with the full suite."
    ),
    parameters={
        "type": "object",
        "properties": {"target": {"type": "string", "minLength": 1}},
        "additionalProperties": False,
    },
    func=run_tests,
)
