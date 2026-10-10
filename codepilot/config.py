"""Configuration management for CodePilot."""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

# Default LLM model recommendation:
# We use "openai/gpt-oss-120b" as configured for Groq live model integration.
DEFAULT_LLM_MODEL = "openai/gpt-oss-120b"

# Default protected paths and denylists per spec (AGENTS.md A4, TOOLS.md T7)
DEFAULT_PROTECTED_PATHS = [
    "tests/",
    "test_*.py",
    "*_test.py",
    "conftest.py",
    ".github/",
    ".env",
    ".env.*",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "pyproject.toml",
    "requirements*.txt",
    ".gitignore",
    "codepilot/",
]

DEFAULT_READ_DENYLIST = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    ".git/",
]


@dataclass
class Settings:
    """Application settings loaded from environment / CLI flags."""

    # Python runtime expectations
    min_python_version: tuple[int, int] = (3, 11)

    # Groq API / LLM configuration
    llm_base_url: str = field(
        default_factory=lambda: os.getenv(
            "GROQ_BASE_URL",
            os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1"),
        )
    )
    llm_api_key: str = field(
        default_factory=lambda: os.getenv(
            "GROQ_API_KEY", os.getenv("LLM_API_KEY", "")
        )
    )
    llm_model: str = field(
        default_factory=lambda: os.getenv(
            "GROQ_MODEL", os.getenv("LLM_MODEL", DEFAULT_LLM_MODEL)
        )
    )

    # Iteration limit & tool limits
    max_iterations: int = field(
        default_factory=lambda: int(os.getenv("MAX_ITERATIONS", "6"))
    )
    max_tool_calls_per_iteration: int = field(
        default_factory=lambda: int(
            os.getenv("MAX_TOOL_CALLS_PER_ITERATION", "10")
        )
    )
    command_timeout_s: int = field(
        default_factory=lambda: int(os.getenv("COMMAND_TIMEOUT_SECONDS", "60"))
    )
    max_tool_output_chars: int = field(
        default_factory=lambda: int(os.getenv("MAX_TOOL_OUTPUT_CHARS", "4000"))
    )
    max_patch_attempts_per_file: int = field(
        default_factory=lambda: int(
            os.getenv("MAX_PATCH_ATTEMPTS_PER_FILE", "3")
        )
    )
    llm_max_retries: int = field(
        default_factory=lambda: int(os.getenv("LLM_MAX_RETRIES", "4"))
    )
    max_request_tokens_estimate: int = field(
        default_factory=lambda: int(
            os.getenv("MAX_REQUEST_TOKENS_ESTIMATE", "5000")
        )
    )

    # Context / history limits (HISTORY_KEEP_RECENT_RESULTS defaults to 3)
    history_keep_recent_results: int = field(
        default_factory=lambda: int(
            os.getenv("HISTORY_KEEP_RECENT_RESULTS", "3")
        )
    )
    max_result_retrievals_per_run: int = field(
        default_factory=lambda: int(
            os.getenv("MAX_RESULT_RETRIEVALS_PER_RUN", "10")
        )
    )

    # Workspace behavior & paths
    source_repo: str = field(
        default_factory=lambda: os.getenv("SOURCE_REPO", "")
    )
    runs_dir: str = field(
        default_factory=lambda: os.getenv("RUNS_DIR", "runs")
    )
    log_path: str = field(
        default_factory=lambda: os.getenv("LOG_PATH", "app.log")
    )
    metrics_path: str = field(
        default_factory=lambda: os.getenv("METRICS_PATH", "metrics.json")
    )
    keep_workspace: bool = field(
        default_factory=lambda: os.getenv("KEEP_WORKSPACE", "false").lower()
        == "true"
    )

    # Protected-path rules
    protected_paths: list[str] = field(
        default_factory=lambda: list(DEFAULT_PROTECTED_PATHS)
    )
    read_denylist: list[str] = field(
        default_factory=lambda: list(DEFAULT_READ_DENYLIST)
    )

    # CLI settings
    cli_encoding: str = "utf-8"

    def __repr__(self) -> str:
        # Hide sensitive API key in repr
        masked_key = (
            f"***{self.llm_api_key[-4:]}"
            if len(self.llm_api_key) >= 4
            else ("***" if self.llm_api_key else "")
        )
        return (
            f"Settings(llm_base_url={self.llm_base_url!r}, "
            f"llm_api_key={masked_key!r}, "
            f"llm_model={self.llm_model!r}, "
            f"max_iterations={self.max_iterations}, "
            f"max_tool_calls_per_iteration={self.max_tool_calls_per_iteration}, "
            f"command_timeout_s={self.command_timeout_s}, "
            f"max_tool_output_chars={self.max_tool_output_chars}, "
            f"max_patch_attempts_per_file={self.max_patch_attempts_per_file}, "
            f"llm_max_retries={self.llm_max_retries}, "
            f"history_keep_recent_results={self.history_keep_recent_results}, "
            f"max_result_retrievals_per_run={self.max_result_retrievals_per_run}, "
            f"runs_dir={self.runs_dir!r}, "
            f"log_path={self.log_path!r}, "
            f"metrics_path={self.metrics_path!r}, "
            f"keep_workspace={self.keep_workspace}, "
            f"protected_paths={len(self.protected_paths)} items, "
            f"read_denylist={len(self.read_denylist)} items)"
        )
