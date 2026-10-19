"""A scripted 'model' that decides only from the file contents visible in the conversation.

It is a simulator of context handling, not of a real LLM: it shows whether a repair is *possible* within the
iteration budget given what the history keeps in full. A passing simulator run is not evidence that a live model
repairs the sandbox.
"""

import json
from typing import Any, Dict, List, Set

from codepilot.llm.client import LLMClient
from codepilot.llm.fake import call, respond
from support_sandbox import load_manifest

FILES = ["app/orders.py", "tests/test_orders.py", "app/pagination.py", "tests/test_pagination.py",
         "app/pricing.py", "tests/test_pricing.py"]
SOURCE_OF = {"BUG-01": "app/pagination.py", "BUG-02": "app/orders.py", "BUG-03": "app/pricing.py", "BUG-04": "app/orders.py"}
TEST_OF = {"BUG-01": "tests/test_pagination.py", "BUG-02": "tests/test_orders.py", "BUG-03": "tests/test_pricing.py",
           "BUG-04": "tests/test_orders.py"}


def visible_files(messages: List[Dict[str, Any]]) -> Set[str]:
    """Paths whose contents appear in full (not as a structured summary) in the messages."""
    seen: Set[str] = set()
    for message in messages:
        if message.get("role") != "tool":
            continue
        try:
            payload = json.loads(message["content"])
        except (TypeError, ValueError):
            continue
        data = payload.get("data") or {}
        if "key_facts" not in payload and payload.get("ok") and isinstance(data, dict) and "path" in data \
                and ("lines" in data or "content" in data):
            seen.add(data["path"])
    return seen


class ReadFirstModel(LLMClient):
    """Lists the repository, reads all six files one per reply, then patches only what it can see in full."""

    def __init__(self) -> None:
        self.manifest = load_manifest()
        self.fixed: Set[str] = set()
        self.steps = 0
        self.log: List[str] = []

    def chat(self, messages, tools):
        self.steps += 1
        visible = visible_files(messages)
        if self.steps == 1:
            self.log.append("list_files")
            return respond(call("list_files", path=".", max_depth=2))
        if self.steps <= 1 + len(FILES):
            path = FILES[self.steps - 2]
            self.log.append("read " + path)
            return respond(call("read_file", path=path, start_line=1, end_line=400))
        for bug in ("BUG-02", "BUG-01", "BUG-03", "BUG-04"):
            if bug in self.fixed:
                continue
            missing = [p for p in (SOURCE_OF[bug], TEST_OF[bug]) if p not in visible]
            if missing:
                self.log.append("re-read " + missing[0])
                return respond(call("read_file", path=missing[0], start_line=1, end_line=400))
            fix = self.manifest["bugs"][bug]["fix"]
            self.fixed.add(bug)
            self.log.append("patch " + bug)
            return respond(call("apply_patch", path=fix["path"], old=fix["old"], new=fix["new"]))
        return respond(text="done")
