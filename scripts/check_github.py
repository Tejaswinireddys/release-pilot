#!/usr/bin/env python3
"""
Verify GitHub credentials by fetching a PR diff summary.

Usage:
    # From the project root with credentials in .env:
    source .env && python3 scripts/check_github.py --pr 1

    # Or pass vars inline:
    GITHUB_TOKEN=ghp_... GITHUB_REPO=owner/repo \\
      python3 scripts/check_github.py --pr 42

Required env vars:
    GITHUB_TOKEN   personal access token (pull_requests:read + contents:read)
    GITHUB_REPO    owner/repo, e.g. acme-corp/payment-service

Arguments:
    --pr N   Pull request number to fetch (default: 1)

Exit codes:
    0  — diff fetched successfully; summary printed
    1  — configuration error or API failure
"""

from __future__ import annotations

import argparse
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
    parser = argparse.ArgumentParser(
        description="Verify GitHub credentials by fetching a PR diff summary.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--pr", type=int, default=1, metavar="N",
        help="PR number to fetch (default: 1)",
    )
    args = parser.parse_args()
    pr_number = args.pr

    token = _require("GITHUB_TOKEN")
    repo = _require("GITHUB_REPO")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    print(f"Checking GitHub API for {repo} PR #{pr_number}")
    print()

    with httpx.Client(timeout=15.0) as client:
        # Step 1: fetch PR metadata
        print("Step 1: Fetching PR metadata …", end=" ", flush=True)
        try:
            r = client.get(
                f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
                headers=headers,
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            print(f"FAILED (HTTP {exc.response.status_code})")
            _hint_github_error(exc.response.status_code, repo)
            sys.exit(1)
        except Exception as exc:
            print(f"FAILED\n  {exc}", file=sys.stderr)
            sys.exit(1)

        pr = r.json()
        title = pr.get("title", "(no title)")
        state = pr.get("state", "?")
        author = pr.get("user", {}).get("login", "?")
        base_branch = pr.get("base", {}).get("ref", "?")
        head_branch = pr.get("head", {}).get("ref", "?")
        pr_url = pr.get("html_url", "")
        print(f"OK")
        print(f"  Title  : {title}")
        print(f"  State  : {state}")
        print(f"  Author : {author}")
        print(f"  Branch : {head_branch} → {base_branch}")
        print(f"  URL    : {pr_url}")
        print()

        # Step 2: fetch changed files
        print("Step 2: Fetching changed files …", end=" ", flush=True)
        try:
            r = client.get(
                f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files",
                headers=headers,
                params={"per_page": 100},
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            print(f"FAILED (HTTP {exc.response.status_code})")
            _hint_github_error(exc.response.status_code, repo)
            sys.exit(1)
        except Exception as exc:
            print(f"FAILED\n  {exc}", file=sys.stderr)
            sys.exit(1)

        files: list[dict] = r.json()
        print(f"OK ({len(files)} file(s))")

        # Build a diff summary
        total_additions = sum(f.get("additions", 0) for f in files)
        total_deletions = sum(f.get("deletions", 0) for f in files)

        print()
        print("  Changed files:")
        for f in files[:20]:
            status = f.get("status", "modified")
            additions = f.get("additions", 0)
            deletions = f.get("deletions", 0)
            print(f"    [{status:8s}] +{additions:<4} -{deletions:<4} {f['filename']}")
        if len(files) > 20:
            print(f"    … and {len(files) - 20} more")

        print()
        print(f"  Total: +{total_additions} -{total_deletions} across {len(files)} file(s)")

    print()
    print("=" * 60)
    print("GitHub credentials verified successfully.")
    print(f"Fetched diff for PR #{pr_number} in {repo}")
    print()
    print("Next step:")
    print("  Set INTEGRATION_GITHUB_MODE=live in your .env")
    print("  to enable real PR diff fetches in the Risk Analyst.")
    print("=" * 60)


def _hint_github_error(status: int, repo: str) -> None:
    if status == 401:
        print(
            "  HINT: 401 Unauthorized — check GITHUB_TOKEN.",
            file=sys.stderr,
        )
        print(
            "  Generate a token at: https://github.com/settings/tokens",
            file=sys.stderr,
        )
    elif status == 403:
        print(
            "  HINT: 403 Forbidden — your token may lack pull_requests:read permission.",
            file=sys.stderr,
        )
    elif status == 404:
        print(
            f"  HINT: 404 Not Found — check GITHUB_REPO ('{repo}') and the PR number.",
            file=sys.stderr,
        )
        print(
            "  If the repo is private, your token needs contents:read permission.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
