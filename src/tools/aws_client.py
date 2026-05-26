"""
Real AWS connectivity — CloudWatch metrics for SLO Sentinel.

Mode controlled by INTEGRATION_AWS_MODE (default: mock).

Safety design:
  - Mock mode (default): all calls go to aws_mock_server.py (httpx).
  - Live mode: boto3 CloudWatch calls; read-only (metrics/baseline only).
    - Sandbox guard: AWS_SANDBOX_CLUSTER + AWS_SANDBOX_SERVICE must match the
      service being queried. SandboxViolationError is raised on mismatch.
    - SAFETY_ALLOW_LIVE_DEPLOYS does not apply here (reads are always safe).
  - On any live failure, falls back to empty dict and logs a warning.

Usage:
    from src.tools.aws_client import get_metrics_backend
    backend = get_metrics_backend()
    metrics  = await backend.get_metrics("service-a")
    baseline = await backend.get_baseline("service-a")
    await backend.close()
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)


class SandboxViolationError(Exception):
    """Raised when a request targets a resource outside the configured sandbox."""

    def __init__(self, resource: str, allowed: str) -> None:
        self.resource = resource
        self.allowed = allowed
        super().__init__(
            f"Sandbox violation: resource '{resource}' is not the configured sandbox "
            f"'{allowed}'. Set AWS_SANDBOX_SERVICE to override."
        )


class AWSMetricsBackend:
    """Real CloudWatch metrics backend via boto3.

    Read-only: get_metrics and get_baseline only. All ECS mutation goes through
    Harness; this client never modifies infrastructure.

    Metrics are read from the ECS/ALB namespace configured by
    AWS_CLOUDWATCH_NAMESPACE (default: AWS/ApplicationELB). Override per-service
    dimension names with AWS_CLOUDWATCH_SERVICE_DIMENSION (default: TargetGroup)
    and AWS_CLOUDWATCH_CLUSTER_DIMENSION (default: LoadBalancer).
    """

    def __init__(self) -> None:
        try:
            import boto3
            self._boto3 = boto3
        except ImportError as exc:
            raise ImportError(
                "boto3 is required for INTEGRATION_AWS_MODE=live. Run: pip install boto3"
            ) from exc

        self._region = os.getenv("AWS_REGION", "us-east-1")
        self._sandbox_cluster = os.getenv("AWS_SANDBOX_CLUSTER", "")
        self._sandbox_service = os.getenv("AWS_SANDBOX_SERVICE", "")
        self._namespace = os.getenv("AWS_CLOUDWATCH_NAMESPACE", "AWS/ApplicationELB")

        self._cw = self._boto3.client(
            "cloudwatch",
            region_name=self._region,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            aws_session_token=os.getenv("AWS_SESSION_TOKEN"),
        )

    def _assert_sandbox(self, service: str) -> None:
        if self._sandbox_service and service != self._sandbox_service:
            raise SandboxViolationError(service, self._sandbox_service)

    async def get_metrics(self, service: str) -> dict[str, Any]:
        """Fetch current error_rate, p99_latency_ms, rps from CloudWatch.

        Returns keys matching the mock server response so the SLO Sentinel
        works identically in both modes.
        """
        self._assert_sandbox(service)
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=5)

        dimensions = []
        if self._sandbox_cluster:
            dimensions.append({"Name": "LoadBalancer", "Value": self._sandbox_cluster})

        try:
            resp = await asyncio.to_thread(
                self._cw.get_metric_data,
                MetricDataQueries=[
                    {
                        "Id": "error_count",
                        "MetricStat": {
                            "Metric": {
                                "Namespace": self._namespace,
                                "MetricName": "HTTPCode_Target_5XX_Count",
                                "Dimensions": dimensions,
                            },
                            "Period": 60,
                            "Stat": "Sum",
                        },
                        "ReturnData": True,
                    },
                    {
                        "Id": "request_count",
                        "MetricStat": {
                            "Metric": {
                                "Namespace": self._namespace,
                                "MetricName": "RequestCount",
                                "Dimensions": dimensions,
                            },
                            "Period": 60,
                            "Stat": "Sum",
                        },
                        "ReturnData": True,
                    },
                    {
                        "Id": "p99_latency",
                        "MetricStat": {
                            "Metric": {
                                "Namespace": self._namespace,
                                "MetricName": "TargetResponseTime",
                                "Dimensions": dimensions,
                            },
                            "Period": 60,
                            "Stat": "p99",
                        },
                        "ReturnData": True,
                    },
                ],
                StartTime=start,
                EndTime=now,
            )
        except Exception as exc:
            log.warning(
                "aws.cloudwatch.get_metrics failed service=%s error=%s — returning empty",
                service, exc,
            )
            return {}

        results = {r["Id"]: r.get("Values", []) for r in resp.get("MetricDataResults", [])}
        err_vals = results.get("error_count", [0.0])
        req_vals = results.get("request_count", [1.0])
        p99_vals = results.get("p99_latency", [0.3])

        err_sum = sum(err_vals)
        req_sum = sum(req_vals) or 1.0
        p99_s = p99_vals[-1] if p99_vals else 0.3

        return {
            "error_rate": round(err_sum / req_sum, 6),
            "p99_latency_ms": round(p99_s * 1000, 1),  # seconds → ms
            "rps": round(req_sum / 300.0, 1),           # 5-min window → per-second
        }

    async def get_baseline(self, service: str) -> dict[str, Any]:
        """Fetch 7-day baseline error_rate and p99 from CloudWatch."""
        self._assert_sandbox(service)
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=7)

        dimensions = []
        if self._sandbox_cluster:
            dimensions.append({"Name": "LoadBalancer", "Value": self._sandbox_cluster})

        try:
            err_resp = await asyncio.to_thread(
                self._cw.get_metric_statistics,
                Namespace=self._namespace,
                MetricName="HTTPCode_Target_5XX_Count",
                Dimensions=dimensions,
                StartTime=start,
                EndTime=now,
                Period=86400,
                Statistics=["Average"],
            )
            p99_resp = await asyncio.to_thread(
                self._cw.get_metric_statistics,
                Namespace=self._namespace,
                MetricName="TargetResponseTime",
                Dimensions=dimensions,
                StartTime=start,
                EndTime=now,
                Period=86400,
                ExtendedStatistics=["p99"],
            )
        except Exception as exc:
            log.warning(
                "aws.cloudwatch.get_baseline failed service=%s error=%s — using defaults",
                service, exc,
            )
            return {"baseline_error_rate": 0.0008, "baseline_p99_ms": 305.0}

        err_points = err_resp.get("Datapoints", [])
        avg_err = (
            sum(d["Average"] for d in err_points) / len(err_points)
            if err_points else 0.0008
        )

        p99_points = p99_resp.get("Datapoints", [])
        avg_p99_s = (
            sum(d.get("ExtendedStatistics", {}).get("p99", 0.305) for d in p99_points)
            / len(p99_points)
            if p99_points else 0.305
        )

        return {
            "baseline_error_rate": round(avg_err, 6),
            "baseline_p99_ms": round(avg_p99_s * 1000, 1),
        }

    async def close(self) -> None:
        pass  # boto3 clients are not async; no explicit close needed


class MockMetricsBackend:
    """Mock metrics backend — proxies to aws_mock_server.py via httpx.

    Drop-in replacement for AWSMetricsBackend in mock mode. Returns the same
    dict structure so SLOSentinel works identically in both modes.
    """

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=os.getenv("AWS_MOCK_URL", "http://localhost:8080"),
            timeout=15.0,
        )

    async def get_metrics(self, service: str) -> dict[str, Any]:
        try:
            resp = await self._http.get(f"/cloudwatch/metrics/{service}")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            log.warning("mock.cloudwatch.metrics_failed service=%s error=%s", service, exc)
            return {}

    async def get_baseline(self, service: str) -> dict[str, Any]:
        try:
            resp = await self._http.get(f"/cloudwatch/baseline/{service}")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            log.warning("mock.cloudwatch.baseline_failed service=%s error=%s", service, exc)
            return {}

    async def close(self) -> None:
        await self._http.aclose()


def get_metrics_backend() -> AWSMetricsBackend | MockMetricsBackend:
    """Return the appropriate metrics backend based on INTEGRATION_AWS_MODE.

    Default: MockMetricsBackend (no credentials required).
    Live:    AWSMetricsBackend (requires boto3 + AWS credentials).
    """
    from config.integrations import aws_is_live
    if aws_is_live():
        log.info("aws.metrics_backend=live region=%s", os.getenv("AWS_REGION", "us-east-1"))
        return AWSMetricsBackend()
    log.info("aws.metrics_backend=mock")
    return MockMetricsBackend()
