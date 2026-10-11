"""Central path policy and subprocess-environment safety (TASK-021 / TASK-022).

One place decides which paths are protected (cannot be written), denied
(cannot be read) and editable (may be patched). Every tool and the final
changed-file audit call these helpers instead of keeping their own lists.

The pattern helpers are pure string logic. ``resolve_path`` adds the
filesystem-aware checks (symlinks, drive letters, ``..`` escapes).
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from codepilot.errors import ToolFailure
from codepilot.config import (
    DEFAULT_EDITABLE_EXTENSIONS,
    DEFAULT_PROTECTED_PATHS,
    DEFAULT_READ_DENYLIST,
)

# Environment variables whose name contains one of these words never reach a
# subprocess (pytest or git), so code under test cannot read the API key.
SECRET_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD")

# Git variables that can inject configuration or point git at a different
# repository. They are dropped as a family: removing only some of them (for
# example GIT_CONFIG_KEY_0 but not GIT_CONFIG_COUNT) makes git fail.
GIT_ENV_DROP_PREFIXES = ("GIT_CONFIG_",)
GIT_ENV_DROP_NAMES = {
    "GIT_CONFIG", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_NAMESPACE",
}


def _norm_parts(path: str) -> List[str]:
    """Split a path on both separators and drop empty and '.' segments."""
    cleaned = path.replace("\\", "/")
    return [part for part in cleaned.split("/") if part not in ("", ".")]


def normalize_rel_path(path: str) -> str:
    """Return a workspace-relative path with forward slashes only.

    ``..`` segments are kept as they are; rejecting them is the job of the
    filesystem-aware path check, not of this pattern helper.
    """
    return "/".join(_norm_parts(path))


def _matches(path: str, patterns: Sequence[str]) -> bool:
    """Case-insensitive, separator-insensitive match against a pattern list.

    * ``name/``  matches any path that has a directory called ``name``.
    * ``a/b/``   matches that directory prefix.
    * ``a/*.py`` (contains a slash) is matched against the whole path.
    * ``*.pem``  (no slash) is matched against the file name only.
    """
    parts = [p.lower() for p in _norm_parts(path)]
    if not parts:
        return False
    rel = "/".join(parts)
    for raw in patterns:
        pattern = raw.replace("\\", "/").strip().lower()
        if not pattern:
            continue
        if pattern.endswith("/"):
            directory = pattern.rstrip("/")
            if "/" in directory:
                if rel == directory or rel.startswith(directory + "/"):
                    return True
            elif directory in parts:
                return True
        elif "/" in pattern:
            if fnmatch.fnmatchcase(rel, pattern):
                return True
        elif fnmatch.fnmatchcase(parts[-1], pattern):
            return True
    return False


def is_protected(
    path: str, patterns: Optional[Sequence[str]] = None
) -> bool:
    """True if the agent must never write to this path."""
    return _matches(path, DEFAULT_PROTECTED_PATHS if patterns is None else patterns)


def is_denied(path: str, patterns: Optional[Sequence[str]] = None) -> bool:
    """True if the agent must never read, list or search this path."""
    return _matches(path, DEFAULT_READ_DENYLIST if patterns is None else patterns)


def is_editable(
    path: str, extensions: Optional[Sequence[str]] = None
) -> bool:
    """True if the file type may be patched (MVP: existing ``.py`` files)."""
    allowed = DEFAULT_EDITABLE_EXTENSIONS if extensions is None else extensions
    parts = _norm_parts(path)
    if not parts:
        return False
    name = parts[-1].lower()
    return any(name.endswith(ext.lower()) for ext in allowed if ext)


def _is_dropped_env(name: str) -> bool:
    upper = name.upper()
    return (
        any(marker in upper for marker in SECRET_ENV_MARKERS)
        or upper in GIT_ENV_DROP_NAMES
        or upper.startswith(GIT_ENV_DROP_PREFIXES)
    )


def sanitized_env(
    extra: Optional[Mapping[str, str]] = None,
    base: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Copy of the environment without secrets or git redirection, for pytest and git children."""
    source = os.environ if base is None else base
    clean = {
        key: value
        for key, value in source.items()
        if not _is_dropped_env(key)
    }
    if extra:
        clean.update(extra)
    return clean


# --------------------------------------------------------------------------
# Filesystem-aware path resolution (TASK-021)
# --------------------------------------------------------------------------

# Directories no tool lists or searches (TOOLS.md section 2.4).
SKIPPED_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules"}

_RESERVED_DEVICE_NAME = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE)
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def is_within(child: Path, parent: Path) -> bool:
    """True if ``child`` is ``parent`` or inside it (case-insensitive on Windows).

    Compares whole path components, so a sibling that merely shares a string
    prefix (``repo`` vs ``repo-evil``) is correctly treated as outside.
    """
    return Path(os.path.normcase(str(child))).is_relative_to(
        Path(os.path.normcase(str(parent)))
    )


@dataclass(frozen=True)
class ResolvedPath:
    """A path that passed containment checks.

    ``rel`` is derived from the fully resolved location (symlinks followed), so
    protected and denylist rules apply to the real target, not to the name the
    model asked for. It uses forward slashes; the workspace root is ``""``.
    """

    abs_path: Path
    rel: str


def resolve_path(root: Path, path: str) -> ResolvedPath:
    """Resolve a workspace-relative path or raise ``ToolFailure``.

    Rejects absolute, drive-letter and UNC paths, NUL characters, ``:``
    (alternate data streams), Windows device names, trailing dots/spaces, and
    anything whose resolved location is outside the workspace root.
    """
    if not isinstance(path, str):
        raise ToolFailure("INVALID_ARGUMENTS", "path must be a string.")

    def outside(reason: str) -> ToolFailure:
        return ToolFailure(
            "PATH_OUTSIDE_WORKSPACE",
            f"Path {path!r} is not allowed: {reason}. "
            "Use a workspace-relative path such as 'app/module.py'.",
        )

    if "\x00" in path:
        raise outside("it contains a NUL character")
    if path.startswith(("/", "\\")):
        raise outside("absolute and UNC paths are not allowed")
    if _DRIVE_PREFIX.match(path):
        raise outside("drive-letter paths are not allowed")
    if ":" in path:
        raise outside("':' is not allowed (alternate data streams)")
    parts = _norm_parts(path)
    for part in parts:
        if part == "..":
            continue
        if part.endswith((".", " ")):
            raise outside("names ending in a dot or space are not allowed")
        if _RESERVED_DEVICE_NAME.match(part):
            raise outside("Windows device names are not allowed")

    root_real = Path(os.path.realpath(root))
    candidate = Path(os.path.realpath(root_real.joinpath(*parts))) if parts else root_real
    if not is_within(candidate, root_real):
        raise outside("it resolves outside the workspace")
    rel = os.path.relpath(candidate, root_real)
    rel = "" if rel == "." else rel.replace(os.sep, "/")
    return ResolvedPath(abs_path=candidate, rel=rel)


def ensure_readable(
    resolved: ResolvedPath, denylist: Optional[Sequence[str]] = None
) -> None:
    """Raise ``PATH_DENIED`` if the file is on the read denylist."""
    if is_denied(resolved.rel, denylist):
        raise ToolFailure(
            "PATH_DENIED",
            f"{resolved.rel!r} is on the read denylist (secrets and Git internals) "
            "and cannot be read, listed or searched.",
        )


def ensure_writable(
    resolved: ResolvedPath,
    denylist: Optional[Sequence[str]] = None,
    protected: Optional[Sequence[str]] = None,
) -> None:
    """Raise ``PATH_DENIED`` or ``PATH_PROTECTED`` if the path may not be modified."""
    ensure_readable(resolved, denylist)
    if is_protected(resolved.rel, protected):
        raise ToolFailure(
            "PATH_PROTECTED",
            f"{resolved.rel!r} is a protected path (tests, CI or configuration) and "
            "cannot be modified. Fix the source code instead; if a test itself looks "
            "wrong, stop and report it for human review.",
        )
