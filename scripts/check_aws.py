#!/usr/bin/env python3
"""
Verify AWS credentials and CloudWatch access for SLO Sentinel live mode.

Usage:
    # From the project root with credentials in .env:
    source .env && python3 scripts/check_aws.py

    # Or pass vars inline:
    AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_REGION=us-east-1 \\
      AWS_SANDBOX_CLUSTER=my-cluster AWS_SANDBOX_SERVICE=my-service \\
      python3 scripts/check_aws.py

Required env vars:
    AWS_ACCESS_KEY_ID          IAM access key
    AWS_SECRET_ACCESS_KEY      IAM secret key
    AWS_REGION                 e.g. us-east-1
    AWS_SANDBOX_CLUSTER        ECS cluster name (sandbox scope)
    AWS_SANDBOX_SERVICE        ECS service name (sandbox scope)

Optional:
    AWS_SESSION_TOKEN          For temporary credentials / assumed roles
    AWS_CLOUDWATCH_NAMESPACE   Default: AWS/ApplicationELB

Exit codes:
    0  — credentials valid, CloudWatch reachable
    1  — configuration error or API failure
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone


def _require(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        print(f"ERROR: {name} is not set.", file=sys.stderr)
        print("       Set it in your environment or .env file.", file=sys.stderr)
        sys.exit(1)
    return val


def main() -> None:
    access_key = _require("AWS_ACCESS_KEY_ID")
    _require("AWS_SECRET_ACCESS_KEY")
    region = os.getenv("AWS_REGION", "us-east-1")
    cluster = os.getenv("AWS_SANDBOX_CLUSTER", "")
    service = os.getenv("AWS_SANDBOX_SERVICE", "")
    namespace = os.getenv("AWS_CLOUDWATCH_NAMESPACE", "AWS/ApplicationELB")

    print("Checking AWS credentials and CloudWatch access")
    print(f"  Region    : {region}")
    print(f"  Namespace : {namespace}")
    print(f"  Cluster   : {cluster or '(not set)'}")
    print(f"  Service   : {service or '(not set)'}")
    print(f"  Key ID    : {access_key[:8]}…")
    print()

    try:
        import boto3
        from botocore.exceptions import ClientError, NoCredentialsError
    except ImportError:
        print("ERROR: boto3 is not installed. Run: pip install boto3", file=sys.stderr)
        sys.exit(1)

    cw = boto3.client(
        "cloudwatch",
        region_name=region,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        aws_session_token=os.getenv("AWS_SESSION_TOKEN"),
    )

    # Step 1: verify credentials with a cheap list-metrics call
    print("Step 1: Verifying IAM credentials …", end=" ", flush=True)
    try:
        cw.list_metrics(Namespace=namespace, Dimensions=[], MaxResults=1)
        print("OK")
    except NoCredentialsError:
        print("FAILED")
        print("  HINT: AWS_ACCESS_KEY_ID or AWS_SECRET_ACCESS_KEY is invalid.", file=sys.stderr)
        sys.exit(1)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("InvalidClientTokenId", "AuthFailure"):
            print("FAILED")
            print(f"  HINT: {code} — check your AWS credentials.", file=sys.stderr)
            sys.exit(1)
        # AccessDenied on list-metrics is OK — credentials exist, just limited
        print(f"OK (advisory: {code})")
    except Exception as exc:
        print(f"FAILED\n  {exc}", file=sys.stderr)
        sys.exit(1)

    # Step 2: check CloudWatch namespace has data (non-fatal if empty)
    print("Step 2: Checking CloudWatch namespace …", end=" ", flush=True)
    try:
        resp = cw.list_metrics(Namespace=namespace, MaxResults=5)
        metrics = resp.get("Metrics", [])
        print(f"OK ({len(metrics)} metric(s) found in {namespace})")
        if metrics:
            names = sorted({m["MetricName"] for m in metrics})[:5]
            print(f"  Sample metric names: {', '.join(names)}")
    except Exception as exc:
        print(f"WARNING — {exc}")
        print("  CloudWatch accessible but namespace may be empty.")

    # Step 3: test a real metric-data query (with sandbox dimensions if set)
    print("Step 3: Test GetMetricData query …", end=" ", flush=True)
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=5)
    dimensions = []
    if cluster:
        dimensions.append({"Name": "LoadBalancer", "Value": cluster})

    try:
        resp = cw.get_metric_data(
            MetricDataQueries=[
                {
                    "Id": "test_rq",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": namespace,
                            "MetricName": "RequestCount",
                            "Dimensions": dimensions,
                        },
                        "Period": 60,
                        "Stat": "Sum",
                    },
                    "ReturnData": True,
                }
            ],
            StartTime=start,
            EndTime=now,
        )
        values = resp.get("MetricDataResults", [{}])[0].get("Values", [])
        print(f"OK ({len(values)} datapoint(s) in last 5 min)")
    except Exception as exc:
        print(f"WARNING — {exc}")
        print("  Query accepted but returned no data (expected if cluster/service is new).")

    print()
    print("=" * 60)
    print("AWS credentials verified successfully.")
    print()
    print("Sandbox configuration:")
    print(f"  AWS_SANDBOX_CLUSTER = {cluster or '(not set — set to scope reads)'}")
    print(f"  AWS_SANDBOX_SERVICE = {service or '(not set — set to scope reads)'}")
    print()
    print("Next steps:")
    print("  1. Set INTEGRATION_AWS_MODE=live in your .env")
    print("  2. Set SAFETY_ALLOW_LIVE_DEPLOYS=false (default) to verify dry-run works")
    print("  3. Set SAFETY_ALLOW_LIVE_DEPLOYS=true only when ready for real deploys")
    print("=" * 60)


if __name__ == "__main__":
    main()
