"""Tools package: shared base types and output-budget helpers.

A tool is a plain function ``func(ctx, args) -> (data, truncated)`` that
raises ``ToolFailure`` for expected problems. It contains no LLM reasoning.
The registry validates arguments, assigns call ids and builds the result
envelope, so tools never construct a ``ToolResult`` themselves.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple, TypeVar,
)

from codepilot.config import Settings
from codepilot.models import PatchRecord

if TYPE_CHECKING:
    from codepilot.workspace import Workspace

ToolOutput = Tuple[Dict[str, Any], bool]
T = TypeVar("T")


@dataclass
class ToolContext:
    """Everything a tool may use, passed explicitly (no global state)."""

    root: Path
    settings: Settings
    workspace: Optional["Workspace"] = None
    iteration: int = 0
    patch_attempts: Dict[str, int] = field(default_factory=dict)
    patches: List[PatchRecord] = field(default_factory=list)
    undo_stack: List[Tuple[PatchRecord, bytes]] = field(default_factory=list)
    run_dir: Optional[Path] = None  # runs/<run_id>/, outside the workspace

    def __post_init__(self) -> None:
        self.root = Path(os.path.realpath(self.root))

    @property
    def budget(self) -> int:
        """Maximum characters of tool output (MAX_TOOL_OUTPUT_CHARS)."""
        return self.settings.max_tool_output_chars


@dataclass(frozen=True)
class ToolSpec:
    """A tool's name, LLM-facing description, JSON schema and function."""

    name: str
    description: str
    parameters: Dict[str, Any]
    func: Callable[[ToolContext, Dict[str, Any]], ToolOutput]


def json_len(obj: Any) -> int:
    """Approximate output size in characters."""
    return len(json.dumps(obj, ensure_ascii=False))


def cap_text(text: str, limit: int) -> Tuple[str, bool]:
    """Cut ``text`` to ``limit`` characters; return (text, was_cut)."""
    if len(text) <= limit:
        return text, False
    return text[: max(limit, 0)], True


def fit_items(
    items: Sequence[T],
    build: Callable[[List[T]], Any],
    budget: int,
    keep: str = "head",
) -> Tuple[List[T], bool]:
    """Keep as many items as fit in ``budget`` once placed in ``build(items)``.

    ``keep="head"`` keeps the first items, ``"tail"`` the last ones. Returns
    the kept items and whether anything was dropped.
    """
    all_items = list(items)
    if json_len(build(all_items)) <= budget:
        return all_items, False

    def pick(k: int) -> List[T]:
        return all_items[:k] if keep == "head" else all_items[len(all_items) - k:]

    low, high = 0, len(all_items)
    while low < high:
        mid = (low + high + 1) // 2
        if json_len(build(pick(mid))) <= budget:
            low = mid
        else:
            high = mid - 1
    return pick(low), True
