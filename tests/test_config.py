"""Tests for configuration management."""

import os
import pytest
from codepilot.config import Settings, DEFAULT_LLM_MODEL


def test_settings_defaults():
    settings = Settings()
    assert settings.max_iterations == 6
    assert settings.max_tool_calls_per_iteration == 10
    assert settings.command_timeout_s == 60
    assert settings.max_tool_output_chars == 8000
    assert settings.max_patch_attempts_per_file == 3
    assert settings.llm_max_retries == 4
    # Requirement 2: Confirm setting for 5 recent full tool results (config name and default)
    assert settings.history_keep_recent_results == 5
    assert settings.max_result_retrievals_per_run == 10
    assert settings.min_python_version == (3, 11)
    # Requirement 1: Default model uses DEFAULT_LLM_MODEL constant
    assert settings.llm_model == DEFAULT_LLM_MODEL
    assert len(settings.protected_paths) > 0
    assert len(settings.read_denylist) > 0


def test_settings_env_override(monkeypatch):
    monkeypatch.setenv("MAX_ITERATIONS", "12")
    monkeypatch.setenv("LLM_MODEL", "custom-model")
    monkeypatch.setenv("GROQ_API_KEY", "secret-groq-key-12345")

    settings = Settings()
    assert settings.max_iterations == 12
    assert settings.llm_model == "custom-model"
    assert settings.llm_api_key == "secret-groq-key-12345"

    # Check repr hides/masks full API key
    repr_str = repr(settings)
    assert "secret-groq-key-12345" not in repr_str
    assert "***2345" in repr_str


def test_settings_empty_api_key(monkeypatch):
    # Requirement 4: Confirm that importing config and creating Settings() without any API key works
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    
    settings = Settings()
    assert settings.llm_api_key == ""
    repr_str = repr(settings)
    assert "llm_api_key=''" in repr_str


def test_real_env_overrides_dotenv(monkeypatch):
    # Requirement 3: Real environment variables take precedence over dotenv / defaults
    monkeypatch.setenv("MAX_ITERATIONS", "99")
    settings = Settings()
    assert settings.max_iterations == 99
