"""Shared pytest fixtures and environment setup."""

import os

import pytest


@pytest.fixture(autouse=True)
def set_demo_env(monkeypatch):
    """Set demo env vars so agents construct without real credentials."""
    monkeypatch.setenv("OPENAI_API_KEY", "demo-test-key")
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_APPROVAL_TOKEN", "demo-approval-token")
