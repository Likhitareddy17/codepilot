"""Tests for the central path policy and subprocess environment (TASK-021/022)."""

import os

import pytest

from codepilot.safety import (
    is_denied,
    is_editable,
    is_protected,
    normalize_rel_path,
    sanitized_env,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("app/pagination.py", "app/pagination.py"),
        ("app\\pagination.py", "app/pagination.py"),
        ("./app//pagination.py", "app/pagination.py"),
        ("", ""),
        (".", ""),
    ],
)
def test_normalize_rel_path(raw, expected):
    assert normalize_rel_path(raw) == expected


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_pricing.py",
        "Tests\\Test_Pricing.py",
        "app/tests/helpers.py",
        "app/test_helpers.py",
        "app/helpers_test.py",
        "conftest.py",
        "app/conftest.py",
        ".github/workflows/ci.yml",
        ".env",
        ".env.local",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        ".gitignore",
    ],
)
def test_protected_paths(path):
    assert is_protected(path)


@pytest.mark.parametrize(
    "path",
    [
        "app/pagination.py",
        "app/orders.py",
        "app/pricing.py",
        "app/testing_utils.py",
        "app/contests.py",
        "mytests/example.py",
        "metrics.json",
        "logs/app.log",
    ],
)
def test_unprotected_paths(path):
    assert not is_protected(path)


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.production",
        "keys/server.pem",
        "server.key",
        "id_rsa",
        "id_rsa.pub",
        ".git/config",
        "sub/.git/HEAD",
        ".ENV",
    ],
)
def test_denied_paths(path):
    assert is_denied(path)


@pytest.mark.parametrize(
    "path", [".gitignore", "app/env.py", "app/pagination.py", "logs/app.log"]
)
def test_not_denied_paths(path):
    assert not is_denied(path)


def test_protected_files_are_not_denied():
    # Tests must stay readable so the agent can understand failures.
    assert is_protected("tests/test_orders.py")
    assert not is_denied("tests/test_orders.py")


def test_is_editable_defaults_to_py_only():
    assert is_editable("app/orders.py")
    assert is_editable("APP/ORDERS.PY")
    assert not is_editable("README.md")
    assert not is_editable("app/orders.pyc")
    assert not is_editable("Makefile")
    assert not is_editable("")


def test_is_editable_custom_extensions():
    assert is_editable("notes.md", [".md"])
    assert not is_editable("app/orders.py", [".md"])


def test_custom_patterns_override_defaults():
    assert is_protected("docs/secret.txt", ["docs/"])
    assert not is_protected("tests/test_x.py", ["docs/"])


def test_sanitized_env_removes_secrets_and_keeps_the_rest():
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/me",
        "GROQ_API_KEY": "gsk_secret",
        "LLM_API_KEY": "other",
        "GITHUB_TOKEN": "t",
        "db_password": "p",
        "MY_SECRET_THING": "s",
    }
    clean = sanitized_env({"PYTHONDONTWRITEBYTECODE": "1"}, base=base)
    assert clean == {
        "PATH": "/usr/bin",
        "HOME": "/home/me",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def test_sanitized_env_does_not_modify_os_environ(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secret")
    clean = sanitized_env()
    assert "GROQ_API_KEY" not in clean
    assert os.environ["GROQ_API_KEY"] == "gsk_secret"


def test_sanitized_env_drops_git_config_injection_as_a_family():
    base = {
        "PATH": "/usr/bin",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.pager",
        "GIT_CONFIG_VALUE_0": "cat",
        "GIT_CONFIG_PARAMETERS": "x",
        "GIT_DIR": "/elsewhere/.git",
        "GIT_WORK_TREE": "/elsewhere",
        "GIT_INDEX_FILE": "/elsewhere/index",
    }
    assert sanitized_env(base=base) == {"PATH": "/usr/bin"}
