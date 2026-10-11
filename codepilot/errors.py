"""Defined error types for CodePilot."""


class CodePilotError(Exception):
    """Base exception for all CodePilot errors."""

    pass


class LLMError(CodePilotError):
    """Base exception for LLM-related errors."""

    pass


class LLMUnavailableError(LLMError):
    """Raised when the LLM service is unavailable or rate-limited beyond retries."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RateLimitError(LLMUnavailableError):
    """Raised when a HTTP 429 rate limit is encountered and retry limits are exhausted."""

    pass


class ToolError(CodePilotError):
    """Base exception for tool execution errors."""

    pass


class ProtectedPathError(ToolError):
    """Raised when an operation attempts to modify a protected path."""

    pass


class WorkspaceError(CodePilotError):
    """Raised for workspace creation, audit, commit or export problems."""

    pass


class GitError(WorkspaceError):
    """Raised when a Git operation fails or is not permitted."""

    pass


class ToolFailure(ToolError):
    """A tool refused or failed in a way the LLM can act on.

    Carries a machine-checkable ``code`` (see TOOLS.md section 2.2) and a
    ``message`` written for the model. The registry turns it into a
    ``ToolResult`` with ``ok=False``; it never reaches the controller loop.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class GitTimeoutError(GitError):
    """Raised when a Git command exceeds its timeout."""

    pass
