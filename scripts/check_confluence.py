#!/usr/bin/env python3
"""
Verify Confluence credentials by creating a test page.

Usage:
    # From the project root with credentials in .env:
    source .env && python3 scripts/check_confluence.py

    # Or pass vars inline:
    ATLASSIAN_SITE_URL=https://your-org.atlassian.net \\
    ATLASSIAN_EMAIL=you@example.com \\
    ATLASSIAN_API_TOKEN=your-token \\
    CONFLUENCE_SPACE_KEY=ENG \\
    python3 scripts/check_confluence.py

Required env vars:
    ATLASSIAN_SITE_URL       e.g. https://your-org.atlassian.net
    ATLASSIAN_EMAIL          Atlassian account email
    ATLASSIAN_API_TOKEN      API token from id.atlassian.com/manage-profile/security
    CONFLUENCE_SPACE_KEY     Space key, e.g. ENG

Optional env vars:
    CONFLUENCE_PARENT_PAGE_ID  Numeric ID of parent page (creates at space root if absent)

Exit codes:
    0  — test page created successfully; URL printed
    1  — configuration error or API failure
"""

from __future__ import annotations

import base64
import os
import sys
from datetime import datetime, timezone

import httpx


def _require(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        print(f"ERROR: {name} is not set.", file=sys.stderr)
        print("       Set it in your environment or .env file.", file=sys.stderr)
        sys.exit(1)
    return val


def main() -> None:
    site = _require("ATLASSIAN_SITE_URL").rstrip("/")
    email = _require("ATLASSIAN_EMAIL")
    token = _require("ATLASSIAN_API_TOKEN")
    space_key = _require("CONFLUENCE_SPACE_KEY")
    parent_id = os.getenv("CONFLUENCE_PARENT_PAGE_ID", "").strip()

    creds = base64.b64encode(f"{email}:{token}".encode()).decode()
    headers = {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    base = f"{site}/wiki/api/v2"

    print(f"Checking Confluence at {site}")
    print(f"  Space key : {space_key}")
    print(f"  Parent ID : {parent_id or '(space root)'}")
    print()

    with httpx.Client(timeout=15.0) as client:
        # Step 1: verify credentials + resolve space key → space ID
        print("Step 1: Resolving space key …", end=" ", flush=True)
        try:
            r = client.get(
                f"{base}/spaces",
                params={"keys": space_key, "limit": 1},
                headers=headers,
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            print(f"FAILED (HTTP {exc.response.status_code})")
            print(f"  Response: {exc.response.text[:400]}", file=sys.stderr)
            _hint_auth_error(exc.response.status_code)
            sys.exit(1)
        except Exception as exc:
            print(f"FAILED\n  {exc}", file=sys.stderr)
            sys.exit(1)

        results = r.json().get("results", [])
        if not results:
            print("FAILED")
            print(
                f"  ERROR: space key '{space_key}' not found in this Atlassian site.",
                file=sys.stderr,
            )
            print(
                "  Check CONFLUENCE_SPACE_KEY — it must match the key shown in the space URL.",
                file=sys.stderr,
            )
            sys.exit(1)

        space_id = str(results[0]["id"])
        space_name = results[0].get("name", space_key)
        print(f"OK (space_id={space_id}, name={space_name!r})")

        # Step 2: create test page
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        title = f"[Release Pilot] Credential check — {ts}"
        body_html = (
            "<p>This page was automatically created by <code>scripts/check_confluence.py</code> "
            "to verify that Release Pilot can publish release pages.</p>"
            "<p>You can safely delete it.</p>"
        )
        payload: dict = {
            "spaceId": space_id,
            "status": "current",
            "title": title,
            "body": {"representation": "storage", "value": body_html},
        }
        if parent_id:
            payload["parentId"] = parent_id

        print("Step 2: Creating test page …", end=" ", flush=True)
        try:
            r = client.post(f"{base}/pages", json=payload, headers=headers)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            print(f"FAILED (HTTP {exc.response.status_code})")
            print(f"  Response: {exc.response.text[:400]}", file=sys.stderr)
            _hint_auth_error(exc.response.status_code)
            sys.exit(1)
        except Exception as exc:
            print(f"FAILED\n  {exc}", file=sys.stderr)
            sys.exit(1)

        data = r.json()
        page_id = data.get("id", "")
        webui = data.get("_links", {}).get("webui", f"/spaces/{space_key}/pages/{page_id}")
        url = f"{site}/wiki{webui}" if not webui.startswith("http") else webui
        print("OK")

    print()
    print("=" * 60)
    print("Confluence credentials verified successfully.")
    print(f"Test page URL : {url}")
    print(f"Page ID       : {page_id}")
    print()
    print("Next step:")
    print("  Set INTEGRATION_CONFLUENCE_MODE=live in your .env")
    print("  to enable real Confluence publishing in Release Pilot.")
    print("=" * 60)


def _hint_auth_error(status: int) -> None:
    if status == 401:
        print(
            "  HINT: 401 Unauthorized — check ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN.",
            file=sys.stderr,
        )
        print(
            "  Generate a token at: https://id.atlassian.com/manage-profile/security/api-tokens",
            file=sys.stderr,
        )
    elif status == 403:
        print(
            "  HINT: 403 Forbidden — your token may lack 'write' permission for this space.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
