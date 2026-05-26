"""Integration mode flags — single source of truth for INTEGRATION_*_MODE env vars.

Agents call these functions rather than reading os.environ directly so that:
  - the flag name is defined once and not scattered across the codebase
  - tests can override INTEGRATION_*_MODE at the os.environ level before importing agents
  - new integrations are added here and immediately available project-wide

Each function re-reads os.environ on every call so env vars set after process start
(e.g. in test setUp) are always reflected.

Supported values: "mock" (default) | "live"
Default is always "mock" so the project runs without credentials.
"""

from __future__ import annotations

import os


def confluence_is_live() -> bool:
    """Return True when INTEGRATION_CONFLUENCE_MODE=live."""
    return os.getenv("INTEGRATION_CONFLUENCE_MODE", "mock").strip().lower() == "live"


def github_is_live() -> bool:
    """Return True when INTEGRATION_GITHUB_MODE=live."""
    return os.getenv("INTEGRATION_GITHUB_MODE", "mock").strip().lower() == "live"


def aws_is_live() -> bool:
    """AWS is mock by design — real deployment infrastructure is out of scope."""
    return False


def harness_is_live() -> bool:
    """Harness is mock by design — real deployment infrastructure is out of scope."""
    return False
