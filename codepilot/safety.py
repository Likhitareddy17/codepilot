"""Central path policy and subprocess-environment safety (TASK-021 / TASK-022).

One place decides which paths are protected (cannot be written), denied
(cannot be read) and editable (may be patched). Every tool and the final
changed-file audit call these helpers instead of keeping their own lists.

This module is pure pattern logic. Resolving a path against the real
filesystem (symlinks, drive letters, ``..`` escapes) is added to this module
when the tools are built.
"""

from __future__ import annotations

import fnmatch
import os
from typing import Dict, List, Mapping, Optional, Sequence

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
