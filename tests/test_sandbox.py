"""Sandbox integrity tests (TASK-057, TESTING.md section 24).

The canonical sandbox is the controlled demonstration target. These tests prove
it is what the specification says: deterministic failures, matching evidence,
reference fixes that really repair it, and no changes caused by a CodePilot run.
Expected values come from tests/fixtures/sandbox_expected.json, not from this file.
"""

import ast
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from codepilot import cli
from codepilot.llm import fake
from codepilot.llm.fake import FakeLLM
from codepilot.tools import ToolContext
from codepilot.tools.registry import build_default_registry
from support_sandbox import ROOT, SANDBOX, apply_edit, copy_sandbox, run_suite, sandbox_files, tree_hash

KEY = "gsk_TESTKEY1234567890abcdef"


def fix_for(manifest, bug):
    return manifest["bugs"][bug]["fix"]


def all_fixes(manifest):
    return [fix_for(manifest, bug) for bug in sorted(manifest["bugs"])]


# ----------------------------------------------------- structure and size (fast)


def test_expected_files_exist_and_nothing_else(sandbox_expected):
    assert sandbox_files(SANDBOX) == sorted(sandbox_expected["files"])


def test_tests_match_the_manifest(sandbox_expected):
    found = set()
    for path in sorted((SANDBOX / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found |= {f"tests/{path.name}::{n.name}" for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")}
    base = sandbox_expected["baseline"]
    assert found == set(base["passing_ids"]) | set(base["failing_ids"])
    assert len(found) == base["total"] == base["passed"] + base["failed"]


def test_size_is_within_the_design_bounds(sandbox_expected):
    low, high = sandbox_expected["bounds"]["tests"]
    assert low <= sandbox_expected["baseline"]["total"] <= high
    code_lines = sum(1 for p in (SANDBOX / "app").glob("*.py")
                     for line in p.read_text(encoding="utf-8").splitlines() if line.strip())
    low, high = sandbox_expected["bounds"]["app_code_lines"]
    assert low <= code_lines <= high, f"{code_lines} non-blank application lines"


def test_every_bug_has_a_failing_test_and_a_unique_fix_target(sandbox_expected):
    bugs = sandbox_expected["bugs"]
    assert sorted(bugs) == ["BUG-01", "BUG-02", "BUG-03", "BUG-04"]
    claimed = [i for b in bugs.values() for i in b["failing_ids"]]
    assert sorted(claimed) == sorted(sandbox_expected["baseline"]["failing_ids"]) and len(set(claimed)) == len(claimed)
    for bug in bugs.values():
        text = (SANDBOX / bug["fix"]["path"]).read_text(encoding="utf-8").replace("\r\n", "\n")
        assert text.count(bug["fix"]["old"]) == 1 and bug["fix"]["path"].startswith("app/")


# ------------------------------------------------------------ evidence (fast)


def test_log_contains_the_required_evidence(sandbox_expected):
    expected = sandbox_expected["evidence"]["log"]
    log = (SANDBOX / "logs" / "app.log").read_text(encoding="utf-8")
    assert log.count(expected["tax_warning_text"]) == expected["tax_warning_count"]
    assert expected["traceback_exception"] in log and "Traceback (most recent call last):" in log
    stamp = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (INFO|WARNING|ERROR) \S+")
    for line in log.splitlines():
        assert stamp.match(line) or line.startswith(("Traceback", "  File ", "    ", "TypeError")), line


def test_log_traceback_points_at_the_real_source_line(sandbox_expected):
    log = (SANDBOX / "logs" / "app.log").read_text(encoding="utf-8")
    match = re.search(r'File "(app/orders\.py)", line (\d+), in get_order_total\n\s+(.+)\n', log)
    assert match, "traceback frame for get_order_total not found"
    source = (SANDBOX / match.group(1)).read_text(encoding="utf-8").splitlines()
    assert source[int(match.group(2)) - 1].strip() == match.group(3)


def test_metrics_are_valid_and_consistent_with_the_log(sandbox_expected):
    metrics = json.loads((SANDBOX / "metrics.json").read_text(encoding="utf-8"))
    expected = sandbox_expected["evidence"]["metrics"]
    assert metrics["requests"] == expected["requests"] and metrics["errors"] == expected["errors"]
    assert metrics["error_rate"] == pytest.approx(metrics["errors"] / metrics["requests"])
    assert {"pagination", "orders", "pricing"} <= set(metrics["operations"])
    failures = metrics["operations"]["orders"]["tax_lookup_failures"]
    log = (SANDBOX / "logs" / "app.log").read_text(encoding="utf-8")
    assert failures == expected["orders_tax_lookup_failures"] == log.count(sandbox_expected["evidence"]["log"]["tax_warning_text"])


def test_bug_04_needs_both_log_and_metrics(sandbox_expected):
    assert sandbox_expected["bugs"]["BUG-04"]["evidence"] == {"log": "required", "metrics": "required"}


def test_no_secrets_and_no_instructions_in_the_sandbox():
    risky = re.compile(r"gsk_|sk-[A-Za-z0-9]|bearer |password|api[_-]?key|secret|token|ignore (all |previous )|you must|assistant",
                       re.IGNORECASE)
    for rel in sandbox_files(SANDBOX):
        for number, line in enumerate((SANDBOX / rel).read_text(encoding="utf-8").splitlines(), 1):
            assert not risky.search(line), f"{rel}:{number}: {line}"


def test_sandbox_needs_no_network_or_extra_packages():
    allowed = {"logging", "pytest", "app"}
    for rel in sandbox_files(SANDBOX):
        if rel.endswith(".py"):
            for node in ast.walk(ast.parse((SANDBOX / rel).read_text(encoding="utf-8"))):
                modules = ([a.name for a in node.names] if isinstance(node, ast.Import)
                           else [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                for module in modules:
                    assert module.split(".")[0] in allowed, f"{rel} imports {module}"


def test_tests_are_protected_and_application_files_are_editable():
    from codepilot.safety import is_editable, is_protected
    for rel in sandbox_files(SANDBOX):
        if rel.startswith("tests/"):
            assert is_protected(rel), rel
        if rel.startswith("app/") and rel.endswith(".py"):
            assert not is_protected(rel) and is_editable(rel), rel


def test_reference_patches_are_accepted_by_the_real_apply_patch_tool(tmp_path, settings, sandbox_expected):
    edits = all_fixes(sandbox_expected) + [sandbox_expected["regression_patch"]] + sandbox_expected["noop_patches"]
    for number, edit in enumerate(edits):
        root = copy_sandbox(tmp_path / f"copy{number}")
        before = tree_hash(root)
        ctx = ToolContext(root=root, settings=settings)
        result = build_default_registry(ctx).execute("apply_patch", {k: edit[k] for k in ("path", "old", "new")})
        assert result.ok and result.data["syntax_ok"], (edit, result.error)
        assert tree_hash(root) != before


# ---------------------------------------------- real pytest on copies (slow)


@pytest.mark.slow
def test_root_collection_never_includes_the_failing_sandbox():
    proc = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "."],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert proc.returncode == 0 and "sandbox_repo" not in proc.stdout and "test_sandbox.py" in proc.stdout


@pytest.mark.slow
def test_baseline_matches_the_manifest_and_is_deterministic(sandbox_copy, sandbox_expected, tmp_path):
    base = sandbox_expected["baseline"]
    first = run_suite(sandbox_copy)
    second = run_suite(copy_sandbox(tmp_path / "second"))
    for run in (first, second):
        assert run["passed"] == set(base["passing_ids"]) and run["failed"] == set(base["failing_ids"]), run["output"]
    assert first["reasons"] == second["reasons"]


@pytest.mark.slow
def test_failures_show_the_intended_evidence(sandbox_copy):
    reasons = run_suite(sandbox_copy)["reasons"]
    assert "[5] == [3, 4]" in reasons["tests/test_pagination.py::test_second_page"]
    assert "TypeError" in reasons["tests/test_orders.py::test_missing_order"]
    assert "100.0 == 108.0" in reasons["tests/test_orders.py::test_order_total"]
    assert "190.0 == " in reasons["tests/test_pricing.py::test_percentage_discount"]


@pytest.mark.slow
@pytest.mark.parametrize("bug", ["BUG-01", "BUG-02", "BUG-03", "BUG-04"])
def test_each_reference_fix_repairs_only_its_own_defect(bug, sandbox_copy, sandbox_expected):
    base = sandbox_expected["baseline"]
    repaired = set(sandbox_expected["bugs"][bug]["failing_ids"])
    apply_edit(sandbox_copy, fix_for(sandbox_expected, bug))
    run = run_suite(sandbox_copy)
    assert run["failed"] == set(base["failing_ids"]) - repaired, run["output"]
    assert run["passed"] == set(base["passing_ids"]) | repaired


@pytest.mark.slow
def test_all_reference_fixes_give_a_fully_passing_suite(sandbox_copy, sandbox_expected):
    for edit in all_fixes(sandbox_expected):
        apply_edit(sandbox_copy, edit)
    run = run_suite(sandbox_copy)
    base = sandbox_expected["baseline"]
    assert run["failed"] == set() and run["passed"] == set(base["passing_ids"]) | set(base["failing_ids"])
    assert run["exit_code"] == 0
    assert sorted(set(sandbox_files(sandbox_copy)) ^ set(sandbox_files(SANDBOX))) == []   # no file added or removed


@pytest.mark.slow
def test_regression_patch_really_causes_a_regression(sandbox_copy, sandbox_expected):
    patch = sandbox_expected["regression_patch"]
    apply_edit(sandbox_copy, patch)
    run = run_suite(sandbox_copy)
    base = sandbox_expected["baseline"]
    assert set(patch["newly_failing_ids"]) <= run["failed"]
    assert set(base["failing_ids"]) <= run["failed"] and len(run["passed"]) < len(base["passing_ids"])


@pytest.mark.slow
@pytest.mark.parametrize("index", [0, 1])
def test_noop_patches_make_no_behavioural_progress(index, sandbox_copy, sandbox_expected):
    apply_edit(sandbox_copy, sandbox_expected["noop_patches"][index])
    run = run_suite(sandbox_copy)
    base = sandbox_expected["baseline"]
    assert run["failed"] == set(base["failing_ids"]) and run["reasons"] == run_suite(copy_sandbox(sandbox_copy.parent / "pristine"))["reasons"]


# ------------------------------------- CodePilot against the sandbox (slow)


@pytest.fixture
def sandbox_cli_env(tmp_path, monkeypatch):
    scratch = tmp_path / "ws_tmp"
    scratch.mkdir()
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    for name in ("LLM_API_KEY", "MAX_ITERATIONS", "KEEP_WORKSPACE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    return tmp_path / "runs", scratch


@pytest.mark.slow
def test_codepilot_repairs_the_sandbox_and_the_patch_holds_up_independently(
    sandbox_cli_env, sandbox_expected, tmp_path, capsys
):
    runs, scratch = sandbox_cli_env
    before = tree_hash(SANDBOX)
    patches = [(f["path"], f["old"], f["new"]) for f in all_fixes(sandbox_expected)]
    code = cli.main(["--repo", str(SANDBOX), "--task", "Fix the failing tests"],
                    llm=FakeLLM(fake.repair_script(patches)))
    out = capsys.readouterr().out
    assert code == 0 and "4/8 -> 8/8 passing" in out, out
    assert tree_hash(SANDBOX) == before, "the canonical sandbox was modified"
    assert not list(scratch.iterdir()), "the temporary workspace was not deleted"

    run = next(runs.iterdir())
    patch = run / "fix.patch"
    changed = re.findall(r"^diff --git a/(\S+) b/", patch.read_text(encoding="utf-8"), re.MULTILINE)
    assert sorted(set(changed)) == ["app/orders.py", "app/pagination.py", "app/pricing.py"]
    trace = json.loads((run / "run_trace.json").read_text(encoding="utf-8"))
    assert trace["outcome"]["success"] and trace["baseline"]["tests"]["failed"] == 4

    # Independent check (TASK-089): only the patch file and plain pytest, no CodePilot state.
    fresh = copy_sandbox(tmp_path / "fresh")
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=fresh, check=True, capture_output=True)
    subprocess.run(["git", "apply", str(patch)], cwd=fresh, check=True, capture_output=True)
    verdict = run_suite(fresh)
    base = sandbox_expected["baseline"]
    assert verdict["failed"] == set() and verdict["passed"] == set(base["passing_ids"]) | set(base["failing_ids"])
