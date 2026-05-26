#!/usr/bin/env python3
"""
Summarize the current integration mode configuration and credential status.

Reads env vars (from .env or the shell) and prints a table showing which
integrations are in mock vs live mode and whether required credentials are set.
Does NOT make any API calls — use the individual check_*.py scripts for that.

Usage:
    source .env && python3 scripts/check_all_integrations.py
"""

from __future__ import annotations

import os
import sys


def _mode(env_var: str, default: str = "mock") -> str:
    return os.getenv(env_var, default).strip().lower()


def _set(name: str) -> bool:
    return bool(os.getenv(name, "").strip())


def _present(name: str) -> str:
    return "✓ set" if _set(name) else "✗ missing"


def main() -> None:
    print()
    print("Release Pilot — Integration Configuration Summary")
    print("=" * 62)
    print()

    rows: list[tuple[str, str, str, str]] = []

    # GitHub
    gh_mode = _mode("INTEGRATION_GITHUB_MODE")
    gh_ready = "✓" if (gh_mode == "mock" or (_set("GITHUB_TOKEN") and _set("GITHUB_REPO"))) else "✗"
    rows.append(("GitHub (PR diffs)", gh_mode, gh_ready, "INTEGRATION_GITHUB_MODE"))

    # Confluence
    cf_mode = _mode("INTEGRATION_CONFLUENCE_MODE")
    cf_ready = "✓" if (cf_mode == "mock" or (
        _set("ATLASSIAN_SITE_URL") and _set("ATLASSIAN_API_TOKEN")
    )) else "✗"
    rows.append(("Confluence (release pages)", cf_mode, cf_ready, "INTEGRATION_CONFLUENCE_MODE"))

    # AWS
    aws_mode = _mode("INTEGRATION_AWS_MODE")
    aws_ready = "✓" if (aws_mode == "mock" or (
        _set("AWS_ACCESS_KEY_ID") and _set("AWS_SECRET_ACCESS_KEY")
    )) else "✗"
    rows.append(("AWS CloudWatch (SLO metrics)", aws_mode, aws_ready, "INTEGRATION_AWS_MODE"))

    # Harness
    h_mode = _mode("INTEGRATION_HARNESS_MODE")
    h_ready = "✓" if (h_mode == "mock" or (
        _set("HARNESS_API_KEY") and _set("HARNESS_ACCOUNT_ID") and _set("HARNESS_PROJECT_ID")
    )) else "✗"
    rows.append(("Harness (pipeline execution)", h_mode, h_ready, "INTEGRATION_HARNESS_MODE"))

    col1 = max(len(r[0]) for r in rows) + 2
    col2 = 8
    col3 = 6
    col4 = max(len(r[3]) for r in rows) + 2

    header = (
        f"{'Integration':<{col1}}{'Mode':<{col2}}{'Ready':<{col3}}{'Env flag'}"
    )
    print(header)
    print("─" * (col1 + col2 + col3 + col4))
    for name, mode, ready, flag in rows:
        print(f"{name:<{col1}}{mode:<{col2}}{ready:<{col3}}{flag}")

    print()

    # Safety flag
    allow = os.getenv("SAFETY_ALLOW_LIVE_DEPLOYS", "false").strip().lower()
    safe_icon = "✓ (dry-run active)" if allow != "true" else "⚠  LIVE DEPLOYS ENABLED"
    print(f"SAFETY_ALLOW_LIVE_DEPLOYS = {allow}  →  {safe_icon}")
    print()

    # Credential detail for live integrations
    any_live = any(r[1] == "live" for r in rows)
    if any_live:
        print("Credential detail (live integrations only):")
        print()

        if _mode("INTEGRATION_GITHUB_MODE") == "live":
            print("  GitHub:")
            print(f"    GITHUB_TOKEN  : {_present('GITHUB_TOKEN')}")
            print(f"    GITHUB_REPO   : {_present('GITHUB_REPO')}")
            print()

        if _mode("INTEGRATION_CONFLUENCE_MODE") == "live":
            print("  Confluence:")
            print(f"    ATLASSIAN_SITE_URL  : {_present('ATLASSIAN_SITE_URL')}")
            print(f"    ATLASSIAN_EMAIL     : {_present('ATLASSIAN_EMAIL')}")
            print(f"    ATLASSIAN_API_TOKEN : {_present('ATLASSIAN_API_TOKEN')}")
            print(f"    CONFLUENCE_SPACE_KEY: {_present('CONFLUENCE_SPACE_KEY')}")
            print()

        if _mode("INTEGRATION_AWS_MODE") == "live":
            print("  AWS:")
            print(f"    AWS_ACCESS_KEY_ID     : {_present('AWS_ACCESS_KEY_ID')}")
            print(f"    AWS_SECRET_ACCESS_KEY : {_present('AWS_SECRET_ACCESS_KEY')}")
            print(f"    AWS_REGION            : {os.getenv('AWS_REGION', '(not set)')}")
            print(f"    AWS_SANDBOX_CLUSTER   : {os.getenv('AWS_SANDBOX_CLUSTER', '(not set)')}")
            print(f"    AWS_SANDBOX_SERVICE   : {os.getenv('AWS_SANDBOX_SERVICE', '(not set)')}")
            print()

        if _mode("INTEGRATION_HARNESS_MODE") == "live":
            print("  Harness:")
            print(f"    HARNESS_API_KEY        : {_present('HARNESS_API_KEY')}")
            print(f"    HARNESS_ACCOUNT_ID     : {_present('HARNESS_ACCOUNT_ID')}")
            print(f"    HARNESS_ORG_ID         : {os.getenv('HARNESS_ORG_ID', 'default')}")
            print(f"    HARNESS_PROJECT_ID     : {_present('HARNESS_PROJECT_ID')}")
            print(f"    HARNESS_PIPELINE_ID    : {_present('HARNESS_PIPELINE_ID')}")
            print(f"    HARNESS_SANDBOX_PROJECT: {os.getenv('HARNESS_SANDBOX_PROJECT', '(defaults to PROJECT_ID)')}")
            print()

    print("Verify live credentials with the individual check scripts:")
    print("  python3 scripts/check_aws.py")
    print("  python3 scripts/check_harness.py")
    print("  python3 scripts/check_github.py --pr 1")
    print("  python3 scripts/check_confluence.py")
    print()


if __name__ == "__main__":
    main()
