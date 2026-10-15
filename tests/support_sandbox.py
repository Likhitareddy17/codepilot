"""Helpers for the canonical sandbox (``sandbox_repo/``) and its manifest (TESTING.md section 4)."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = ROOT / "sandbox_repo"
MANIFEST = Path(__file__).resolve().parent / "fixtures" / "sandbox_expected.json"
IGNORED = shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git", "runs")


def load_manifest() -> Dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def sandbox_files(root: Path) -> List[str]:
    """Relative POSIX paths of every file, ignoring caches."""
    found = []
    for path in sorted(Path(root).rglob("*")):
        parts = path.relative_to(root).parts
        if path.is_file() and not any(p in ("__pycache__", ".pytest_cache", ".git") for p in parts):
            found.append("/".join(parts))
    return found


def tree_hash(root: Path) -> str:
    """Hash of every file's path and bytes (caches ignored)."""
    digest = hashlib.sha256()
    for rel in sandbox_files(root):
        digest.update(rel.encode() + b"\0" + (Path(root) / rel).read_bytes() + b"\0")
    return digest.hexdigest()


def copy_sandbox(dest: Path) -> Path:
    shutil.copytree(SANDBOX, dest, ignore=IGNORED)
    return dest


def apply_edit(root: Path, edit: Dict[str, str]) -> None:
    """Replace ``old`` by ``new`` exactly once in ``edit['path']``, keeping its line endings."""
    path = Path(root) / edit["path"]
    raw = path.read_bytes().decode("utf-8")
    crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")
    assert text.count(edit["old"]) == 1, f"{edit['path']}: expected exactly one match of {edit['old']!r}"
    text = text.replace(edit["old"], edit["new"])
    path.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def run_suite(root: Path) -> Dict[str, Any]:
    """Run the sandbox suite with plain pytest (no CodePilot code involved)."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", "PYTHONPATH")}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no", "-rA", "-p", "no:cacheprovider"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120,
    )
    passed, failed, reasons = set(), set(), {}
    for line in proc.stdout.splitlines():
        status, _, rest = line.partition(" ")
        if status in ("PASSED", "FAILED", "ERROR") and "::" in rest:
            test_id, _, reason = rest.partition(" - ")
            (passed if status == "PASSED" else failed).add(test_id)
            if reason:
                reasons[test_id] = reason
    return {"passed": passed, "failed": failed, "reasons": reasons, "exit_code": proc.returncode, "output": proc.stdout}
