#!/usr/bin/env python3
"""
Verify Harness credentials and pipeline access for CanaryOrchestrator live mode.

Usage:
    # From the project root with credentials in .env:
    source .env && python3 scripts/check_harness.py

    # Or pass vars inline:
    HARNESS_API_KEY=pat.xxx... HARNESS_ACCOUNT_ID=abc123 \\
      HARNESS_ORG_ID=default HARNESS_PROJECT_ID=my-project \\
      HARNESS_PIPELINE_ID=canary-deploy \\
      python3 scripts/check_harness.py

Required env vars:
    HARNESS_API_KEY       Personal Access Token or Service Account token
    HARNESS_ACCOUNT_ID    Harness account identifier
    HARNESS_PROJECT_ID    Project identifier (must match HARNESS_SANDBOX_PROJECT)
    HARNESS_PIPELINE_ID   Pipeline identifier to verify

Optional:
    HARNESS_BASE_URL      Default: https://app.harness.io
    HARNESS_ORG_ID        Default: default
    HARNESS_SANDBOX_PROJECT  Sandbox scope (defaults to HARNESS_PROJECT_ID)

Exit codes:
    0  — credentials valid, pipeline accessible
    1  — configuration error or API failure
"""

from __future__ import annotations

import os
import sys

import httpx


def _require(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        print(f"ERROR: {name} is not set.", file=sys.stderr)
        print("       Set it in your environment or .env file.", file=sys.stderr)
        sys.exit(1)
    return val


def main() -> None:
    api_key = _require("HARNESS_API_KEY")
    account_id = _require("HARNESS_ACCOUNT_ID")
    project_id = _require("HARNESS_PROJECT_ID")
    pipeline_id = _require("HARNESS_PIPELINE_ID")
    org_id = os.getenv("HARNESS_ORG_ID", "default")
    base_url = os.getenv("HARNESS_BASE_URL", "https://app.harness.io").rstrip("/")
    sandbox_project = os.getenv("HARNESS_SANDBOX_PROJECT", project_id)

    print("Checking Harness credentials and pipeline access")
    print(f"  Base URL      : {base_url}")
    print(f"  Account ID    : {account_id}")
    print(f"  Org ID        : {org_id}")
    print(f"  Project ID    : {project_id}")
    print(f"  Pipeline ID   : {pipeline_id}")
    print(f"  Sandbox scope : {sandbox_project}")
    print(f"  API key       : {api_key[:12]}…")
    print()

    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }
    qp = {
        "accountIdentifier": account_id,
        "orgIdentifier": org_id,
        "projectIdentifier": project_id,
    }

    with httpx.Client(timeout=15.0, headers=headers) as client:
        # Step 1: verify credentials by listing projects
        print("Step 1: Verifying API key …", end=" ", flush=True)
        try:
            resp = client.get(
                f"{base_url}/v1/orgs/{org_id}/projects",
                params={"accountIdentifier": account_id},
            )
            if resp.status_code == 401:
                print("FAILED")
                print(
                    "  HINT: 401 Unauthorized — check HARNESS_API_KEY.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if resp.status_code == 403:
                print("OK (advisory: 403 — token exists but has limited project list permission)")
            else:
                resp.raise_for_status()
                projects = resp.json().get("data", {}).get("content", [])
                print(f"OK ({len(projects)} project(s) visible)")
        except httpx.HTTPStatusError as exc:
            print(f"FAILED (HTTP {exc.response.status_code})")
            sys.exit(1)
        except Exception as exc:
            print(f"FAILED\n  {exc}", file=sys.stderr)
            sys.exit(1)

        # Step 2: fetch the pipeline definition
        print("Step 2: Fetching pipeline definition …", end=" ", flush=True)
        try:
            resp = client.get(
                f"{base_url}/pipeline/api/pipelines/{pipeline_id}",
                params=qp,
            )
            if resp.status_code == 404:
                print("FAILED")
                print(
                    f"  HINT: Pipeline '{pipeline_id}' not found in project '{project_id}'.",
                    file=sys.stderr,
                )
                print(
                    "  Check HARNESS_PIPELINE_ID and HARNESS_PROJECT_ID.",
                    file=sys.stderr,
                )
                sys.exit(1)
            resp.raise_for_status()
            data = resp.json().get("data", {})
            pipeline_name = data.get("name", pipeline_id)
            print(f"OK — '{pipeline_name}'")
        except httpx.HTTPStatusError as exc:
            print(f"FAILED (HTTP {exc.response.status_code})")
            sys.exit(1)
        except Exception as exc:
            print(f"FAILED\n  {exc}", file=sys.stderr)
            sys.exit(1)

    print()
    print("=" * 60)
    print("Harness credentials verified successfully.")
    print()
    print("Sandbox configuration:")
    print(f"  HARNESS_SANDBOX_PROJECT = {sandbox_project}")
    print()
    print("Safety model:")
    allow = os.getenv("SAFETY_ALLOW_LIVE_DEPLOYS", "false")
    print(f"  SAFETY_ALLOW_LIVE_DEPLOYS = {allow}")
    if allow.lower() != "true":
        print("  Mode: DRY-RUN — live mode will validate but NOT trigger pipelines.")
        print("  Set SAFETY_ALLOW_LIVE_DEPLOYS=true to enable real pipeline execution.")
    else:
        print("  Mode: LIVE — real pipeline executions will be triggered.")
    print()
    print("Next step:")
    print("  Set INTEGRATION_HARNESS_MODE=live in your .env")
    print("=" * 60)


if __name__ == "__main__":
    main()
