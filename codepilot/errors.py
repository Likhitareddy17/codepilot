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
