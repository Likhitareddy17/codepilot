"""Metrics tool (TASK-017). JSON only; the file is parsed, never executed."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from codepilot.errors import ToolFailure
from codepilot.safety import ensure_readable, resolve_path
from codepilot.tools import ToolContext, ToolOutput, ToolSpec, json_len

REDUCTION_STEPS = (50, 20, 10, 6, 4)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _kept_indices(items: List[Any], keep: int) -> List[int]:
    """First, last, min and max positions, padded with evenly spaced ones."""
    n = len(items)
    chosen = {0, n - 1}
    numbers = [(v, i) for i, v in enumerate(items) if _is_number(v)]
    if numbers:
        chosen.add(min(numbers)[1])
        chosen.add(max(numbers)[1])
    step = max(n // keep, 1)
    for i in range(0, n, step):
        if len(chosen) >= keep:
            break
        chosen.add(i)
    return sorted(chosen)


def reduce_metrics(obj: Any, keep: int) -> Any:
    """Shrink long arrays, keeping first/last elements and the extreme values."""
    if isinstance(obj, dict):
        return {k: reduce_metrics(v, keep) for k, v in obj.items()}
    if isinstance(obj, list):
        if len(obj) > keep:
            return {
                "_reduced": True,
                "length": len(obj),
                "kept": [{"index": i, "value": reduce_metrics(obj[i], keep)} for i in _kept_indices(obj, keep)],
            }
        return [reduce_metrics(v, keep) for v in obj]
    return obj


def read_metrics(ctx: ToolContext, args: Dict[str, Any]) -> ToolOutput:
    """Read and parse the configured metrics JSON file."""
    s = ctx.settings
    if not s.metrics_path:
        raise ToolFailure("NOT_FOUND", "No metrics file is configured for this repository.")
    rp = resolve_path(ctx.root, s.metrics_path)
    ensure_readable(rp, s.read_denylist)
    if not rp.abs_path.is_file():
        raise ToolFailure("NOT_FOUND", f"The metrics file {rp.rel!r} does not exist.")
    text = rp.abs_path.read_bytes().decode("utf-8", errors="replace")
    try:
        metrics = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolFailure(
            "INVALID_FORMAT",
            f"{rp.rel!r} is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno}).",
        ) from exc

    def build(value: Any) -> Dict[str, Any]:
        return {"path": rp.rel, "metrics": value}

    if json_len(build(metrics)) <= ctx.budget:
        return build(metrics), False
    for keep in REDUCTION_STEPS:
        reduced = reduce_metrics(metrics, keep)
        if json_len(build(reduced)) <= ctx.budget:
            return build(reduced), True
    # Last resort: a text preview, shrunk until its escaped form fits the budget.
    full = json.dumps(metrics, ensure_ascii=False)
    size = max(ctx.budget - 100, 0)
    data: Dict[str, Any] = {"path": rp.rel, "metrics_preview": full[:size]}
    while size > 0 and json_len(data) > ctx.budget:
        size = size * 3 // 4
        data["metrics_preview"] = full[:size]
    return data, True


READ_METRICS = ToolSpec(
    name="read_metrics",
    description=(
        "Read the application metrics file (for example error rate and latency over "
        "time). Use it to see when a problem started and how large it is."
    ),
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    func=read_metrics,
)
