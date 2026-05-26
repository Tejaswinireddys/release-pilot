"""Integration mode flags — single source of truth for INTEGRATION_*_MODE env vars.

Agents call these functions rather than reading os.environ directly so that:
  - the flag name is defined once and not scattered across the codebase
  - tests can override INTEGRATION_*_MODE at the os.environ level before importing agents
  - new integrations are added here and immediately available project-wide

Each function re-reads os.environ on every call so env vars set after process start
(e.g. in test setUp) are always reflected.

Supported values: "mock" (default) | "live"
Default is always "mock" so the project runs without credentials.

Safety model for AWS and Harness live mode:
  - SAFETY_ALLOW_LIVE_DEPLOYS=false (default): live mode authenticates and
    validates but does NOT execute any mutating action (dry-run).
  - SAFETY_ALLOW_LIVE_DEPLOYS=true: live mode executes real API calls.
  - Sandbox identifiers scope which resources live calls may target.
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
    """Return True when INTEGRATION_AWS_MODE=live.

    When live, SLO Sentinel fetches real CloudWatch metrics via boto3.
    Requires: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION.
    Sandbox-scoped to AWS_SANDBOX_CLUSTER + AWS_SANDBOX_SERVICE.
    """
    return os.getenv("INTEGRATION_AWS_MODE", "mock").strip().lower() == "live"


def harness_is_live() -> bool:
    """Return True when INTEGRATION_HARNESS_MODE=live.

    When live, CanaryOrchestrator triggers real Harness pipeline executions.
    Requires: HARNESS_API_KEY, HARNESS_ACCOUNT_ID, HARNESS_PROJECT_ID,
              HARNESS_PIPELINE_ID.
    Sandbox-scoped to HARNESS_SANDBOX_PROJECT.
    Dry-run when SAFETY_ALLOW_LIVE_DEPLOYS=false (default).
    """
    return os.getenv("INTEGRATION_HARNESS_MODE", "mock").strip().lower() == "live"


def safety_allow_live_deploys() -> bool:
    """Return True only when SAFETY_ALLOW_LIVE_DEPLOYS is explicitly set to 'true'.

    Default: False.

    When False (default), AWS and Harness live modes run in DRY-RUN:
    they authenticate and validate the real call, log exactly what they WOULD
    do, but do not execute any mutating action (no ECS update, no Harness
    pipeline trigger).

    Set to 'true' only after verifying sandbox credentials with the
    check_aws.py / check_harness.py scripts and confirming the sandbox
    identifiers are correct.
    """
    return os.getenv("SAFETY_ALLOW_LIVE_DEPLOYS", "false").strip().lower() == "true"


def get_sandbox_config() -> dict[str, str]:
    """Return all sandbox identifiers as a dict for logging and verification."""
    return {
        "aws_region": os.getenv("AWS_REGION", ""),
        "aws_sandbox_cluster": os.getenv("AWS_SANDBOX_CLUSTER", ""),
        "aws_sandbox_service": os.getenv("AWS_SANDBOX_SERVICE", ""),
        "harness_account_id": os.getenv("HARNESS_ACCOUNT_ID", ""),
        "harness_org_id": os.getenv("HARNESS_ORG_ID", "default"),
        "harness_project_id": os.getenv("HARNESS_PROJECT_ID", ""),
        "harness_sandbox_project": os.getenv("HARNESS_SANDBOX_PROJECT", ""),
        "harness_pipeline_id": os.getenv("HARNESS_PIPELINE_ID", ""),
    }
