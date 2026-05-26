"""Tests for live integration mode — sandbox guards, dry-run, and factory selection.

These tests verify that:
  1. Mock mode is the default and the pipeline works identically with it.
  2. SandboxViolationError is raised when a resource outside the sandbox is targeted.
  3. Dry-run mode (SAFETY_ALLOW_LIVE_DEPLOYS=false) never calls mutating API methods.
  4. Factory functions return the correct backend based on the mode flag.
  5. safety_allow_live_deploys() is False by default.

No live credentials are required — all live API calls are mocked via unittest.mock.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.integrations import (
    aws_is_live,
    get_sandbox_config,
    harness_is_live,
    safety_allow_live_deploys,
)
from src.tools.aws_client import (
    AWSMetricsBackend,
    MockMetricsBackend,
    SandboxViolationError,
    get_metrics_backend,
)
from src.tools.harness_client import HarnessClient, HarnessSandboxViolationError


# ── Config / integrations ─────────────────────────────────────────────────────


def test_safety_allow_live_deploys_default_false():
    assert safety_allow_live_deploys() is False


def test_safety_allow_live_deploys_explicit_true(monkeypatch):
    monkeypatch.setenv("SAFETY_ALLOW_LIVE_DEPLOYS", "true")
    assert safety_allow_live_deploys() is True


def test_safety_allow_live_deploys_case_insensitive(monkeypatch):
    monkeypatch.setenv("SAFETY_ALLOW_LIVE_DEPLOYS", "TRUE")
    assert safety_allow_live_deploys() is True


def test_aws_is_live_default_false():
    assert aws_is_live() is False


def test_aws_is_live_true(monkeypatch):
    monkeypatch.setenv("INTEGRATION_AWS_MODE", "live")
    assert aws_is_live() is True


def test_harness_is_live_default_false():
    assert harness_is_live() is False


def test_harness_is_live_true(monkeypatch):
    monkeypatch.setenv("INTEGRATION_HARNESS_MODE", "live")
    assert harness_is_live() is True


def test_get_sandbox_config_returns_dict(monkeypatch):
    monkeypatch.setenv("AWS_SANDBOX_CLUSTER", "test-cluster")
    monkeypatch.setenv("AWS_SANDBOX_SERVICE", "test-service")
    monkeypatch.setenv("HARNESS_SANDBOX_PROJECT", "sandbox-proj")
    cfg = get_sandbox_config()
    assert cfg["aws_sandbox_cluster"] == "test-cluster"
    assert cfg["aws_sandbox_service"] == "test-service"
    assert cfg["harness_sandbox_project"] == "sandbox-proj"


# ── AWS metrics backend factory ───────────────────────────────────────────────


def test_metrics_backend_factory_returns_mock_by_default():
    backend = get_metrics_backend()
    assert isinstance(backend, MockMetricsBackend)


def test_metrics_backend_factory_returns_aws_when_live(monkeypatch):
    monkeypatch.setenv("INTEGRATION_AWS_MODE", "live")
    # boto3 may not be importable in all CI envs; patch it
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = MagicMock()
    with patch.dict("sys.modules", {"boto3": mock_boto3}):
        backend = get_metrics_backend()
    assert isinstance(backend, AWSMetricsBackend)


# ── AWSMetricsBackend sandbox guard ──────────────────────────────────────────


def test_aws_sandbox_violation_on_wrong_service(monkeypatch):
    monkeypatch.setenv("AWS_SANDBOX_SERVICE", "allowed-service")
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = MagicMock()
    with patch.dict("sys.modules", {"boto3": mock_boto3}):
        backend = AWSMetricsBackend()

    with pytest.raises(SandboxViolationError) as exc_info:
        backend._assert_sandbox("not-allowed-service")
    assert "not-allowed-service" in str(exc_info.value)
    assert "allowed-service" in str(exc_info.value)


def test_aws_sandbox_passes_matching_service(monkeypatch):
    monkeypatch.setenv("AWS_SANDBOX_SERVICE", "allowed-service")
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = MagicMock()
    with patch.dict("sys.modules", {"boto3": mock_boto3}):
        backend = AWSMetricsBackend()
    # No exception
    backend._assert_sandbox("allowed-service")


def test_aws_sandbox_passes_when_no_restriction_set(monkeypatch):
    monkeypatch.delenv("AWS_SANDBOX_SERVICE", raising=False)
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = MagicMock()
    with patch.dict("sys.modules", {"boto3": mock_boto3}):
        backend = AWSMetricsBackend()
    # No exception — no sandbox configured means unrestricted reads
    backend._assert_sandbox("any-service")


@pytest.mark.asyncio
async def test_aws_get_metrics_falls_back_on_error(monkeypatch):
    monkeypatch.setenv("AWS_SANDBOX_SERVICE", "svc-a")
    mock_cw = MagicMock()
    mock_cw.get_metric_data.side_effect = RuntimeError("simulated CloudWatch error")
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_cw
    with patch.dict("sys.modules", {"boto3": mock_boto3}):
        backend = AWSMetricsBackend()

    result = await backend.get_metrics("svc-a")
    assert result == {}  # graceful fallback


@pytest.mark.asyncio
async def test_aws_get_baseline_falls_back_on_error(monkeypatch):
    monkeypatch.setenv("AWS_SANDBOX_SERVICE", "svc-a")
    mock_cw = MagicMock()
    mock_cw.get_metric_statistics.side_effect = RuntimeError("simulated error")
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_cw
    with patch.dict("sys.modules", {"boto3": mock_boto3}):
        backend = AWSMetricsBackend()

    result = await backend.get_baseline("svc-a")
    assert "baseline_error_rate" in result
    assert "baseline_p99_ms" in result


# ── HarnessClient sandbox guard ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_harness_sandbox_violation_on_wrong_project(monkeypatch):
    monkeypatch.setenv("INTEGRATION_HARNESS_MODE", "live")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("HARNESS_SANDBOX_PROJECT", "sandbox-proj")
    monkeypatch.setenv("HARNESS_PROJECT_ID", "other-proj")  # mismatch!
    monkeypatch.setenv("SAFETY_ALLOW_LIVE_DEPLOYS", "true")

    client = HarnessClient()
    with pytest.raises(HarnessSandboxViolationError) as exc_info:
        client._assert_sandbox("other-proj")
    assert "other-proj" in str(exc_info.value)
    assert "sandbox-proj" in str(exc_info.value)


@pytest.mark.asyncio
async def test_harness_dry_run_does_not_call_api(monkeypatch):
    """When SAFETY_ALLOW_LIVE_DEPLOYS=false, deploy() must not POST to Harness."""
    monkeypatch.setenv("INTEGRATION_HARNESS_MODE", "live")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("SAFETY_ALLOW_LIVE_DEPLOYS", "false")
    monkeypatch.setenv("HARNESS_PROJECT_ID", "sandbox-proj")
    monkeypatch.setenv("HARNESS_SANDBOX_PROJECT", "sandbox-proj")
    monkeypatch.setenv("HARNESS_PIPELINE_ID", "deploy-pipeline")

    client = HarnessClient()
    mock_post = AsyncMock()
    client._http.post = mock_post

    result = await client.deploy("my-service", "v1.2.3")

    mock_post.assert_not_called()
    assert result["status"] == "DRY_RUN"
    assert "dry-run" in result.get("note", "")


@pytest.mark.asyncio
async def test_harness_dry_run_rollback_does_not_call_api(monkeypatch):
    monkeypatch.setenv("INTEGRATION_HARNESS_MODE", "live")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("SAFETY_ALLOW_LIVE_DEPLOYS", "false")
    monkeypatch.setenv("HARNESS_PROJECT_ID", "sandbox-proj")
    monkeypatch.setenv("HARNESS_SANDBOX_PROJECT", "sandbox-proj")

    client = HarnessClient()
    mock_post = AsyncMock()
    client._http.post = mock_post

    result = await client.rollback("exec-abc123", reason="SLO breach")

    mock_post.assert_not_called()
    assert "DRY_RUN" in result["status"]


@pytest.mark.asyncio
async def test_harness_mock_mode_by_default():
    """With DEMO_MODE=true (conftest default), HarnessClient uses mock path."""
    client = HarnessClient()
    assert client._live is False

    result = await client.deploy("payment-service", "sha-abc123")
    assert result["status"] == "RUNNING"
    assert "deployment_id" in result


@pytest.mark.asyncio
async def test_harness_live_falls_back_to_mock_on_error(monkeypatch):
    monkeypatch.setenv("INTEGRATION_HARNESS_MODE", "live")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("SAFETY_ALLOW_LIVE_DEPLOYS", "true")
    monkeypatch.setenv("HARNESS_PROJECT_ID", "sandbox-proj")
    monkeypatch.setenv("HARNESS_SANDBOX_PROJECT", "sandbox-proj")
    monkeypatch.setenv("HARNESS_PIPELINE_ID", "deploy-pipeline")

    client = HarnessClient()
    mock_post = AsyncMock(side_effect=RuntimeError("network error"))
    client._http.post = mock_post

    result = await client.deploy("payment-service", "sha-abc123")
    # Should fall back to mock response, not raise
    assert result["status"] == "RUNNING"
    assert "deployment_id" in result


# ── MockMetricsBackend (ensures mock path is unaffected) ─────────────────────


@pytest.mark.asyncio
async def test_mock_metrics_backend_fallback_on_connection_error():
    backend = MockMetricsBackend()
    # No mock server running — should return {} gracefully
    # (httpx will raise ConnectError which should be caught)
    backend._http = MagicMock()
    backend._http.get = AsyncMock(
        side_effect=__import__("httpx").ConnectError("refused")
    )
    result = await backend.get_metrics("any-service")
    assert result == {}
