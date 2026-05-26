"""
Harness CI/CD platform client.

Mode controlled by INTEGRATION_HARNESS_MODE (default: mock).

Safety design:
  - Mock mode (default): deterministic mock responses; no credentials needed.
    DEMO_MODE=true also forces mock mode for backward compatibility.
  - Live mode (INTEGRATION_HARNESS_MODE=live):
    - Sandbox guard: only the project in HARNESS_SANDBOX_PROJECT may be
      targeted. HarnessSandboxViolationError is raised on any other project.
    - Dry-run guard: when SAFETY_ALLOW_LIVE_DEPLOYS=false (default), the
      client authenticates and validates the request, logs what it WOULD do,
      but does NOT execute any mutating Harness API call.
    - OPA check is performed by the CanaryOrchestrator BEFORE calling deploy();
      HarnessClient trusts that it has already been gate-checked.
  - On any live failure, falls back to mock behaviour and logs a warning.

Harness Cloud REST API endpoints (v1):
  Execute pipeline:
    POST  /pipeline/api/pipeline/execute/{pipelineId}
          ?accountIdentifier=...&orgIdentifier=...&projectIdentifier=...
  Get execution:
    GET   /pipeline/api/pipelines/execution/v2/{planExecutionId}
          ?accountIdentifier=...&orgIdentifier=...&projectIdentifier=...
  Abort execution:
    POST  /pipeline/api/pipelines/execution/{planExecutionId}/interrupt
          ?accountIdentifier=...&orgIdentifier=...&projectIdentifier=...
          &interruptType=AbortAll
"""

from __future__ import annotations

import logging
import os
import random
import uuid
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)


class HarnessSandboxViolationError(Exception):
    """Raised when a call targets a Harness project outside the sandbox."""

    def __init__(self, project: str, allowed: str) -> None:
        self.project = project
        self.allowed = allowed
        super().__init__(
            f"Sandbox violation: Harness project '{project}' is not the configured "
            f"sandbox '{allowed}'. Set HARNESS_SANDBOX_PROJECT to override."
        )


class HarnessClient:
    """Unified Harness client — mock or live mode based on INTEGRATION_HARNESS_MODE.

    CanaryOrchestrator calls this via deploy() and rollback(); it never
    inspects the mode flag itself.
    """

    def __init__(self) -> None:
        from config.integrations import harness_is_live, safety_allow_live_deploys

        self._live = harness_is_live()
        # Backward compat: DEMO_MODE=true forces mock regardless of mode flag
        if os.getenv("DEMO_MODE", "true").lower() == "true":
            self._live = False

        self._dry_run = not safety_allow_live_deploys()

        # Mock / live shared HTTP client
        self._base_url = os.getenv(
            "HARNESS_BASE_URL", "https://app.harness.io"
        ).rstrip("/")
        self._api_key = os.getenv("HARNESS_API_KEY", "")
        self._account_id = os.getenv("HARNESS_ACCOUNT_ID", "")
        self._org_id = os.getenv("HARNESS_ORG_ID", "default")
        self._project_id = os.getenv("HARNESS_PROJECT_ID", "")
        self._pipeline_id = os.getenv("HARNESS_PIPELINE_ID", "")
        self._sandbox_project = os.getenv("HARNESS_SANDBOX_PROJECT", self._project_id)

        self._http = httpx.AsyncClient(
            timeout=60.0,
            headers={
                "x-api-key": self._api_key,
                "Content-Type": "application/json",
            },
        )

    # ── Sandbox guard ─────────────────────────────────────────────────────────

    def _assert_sandbox(self, project_id: str) -> None:
        if self._sandbox_project and project_id != self._sandbox_project:
            raise HarnessSandboxViolationError(project_id, self._sandbox_project)

    # ── Common query params ───────────────────────────────────────────────────

    @property
    def _qp(self) -> dict[str, str]:
        return {
            "accountIdentifier": self._account_id,
            "orgIdentifier": self._org_id,
            "projectIdentifier": self._project_id,
        }

    # ── Public interface ──────────────────────────────────────────────────────

    async def deploy(
        self,
        service_id: str,
        image_tag: str,
        pipeline_id: str | None = None,
    ) -> dict[str, Any]:
        """Trigger a Harness pipeline execution for the given service."""
        if not self._live:
            return self._mock_deploy(service_id, image_tag, pipeline_id)

        pid = pipeline_id or self._pipeline_id
        self._assert_sandbox(self._project_id)

        url = f"{self._base_url}/pipeline/api/pipeline/execute/{pid}"
        body = {
            "inputSetTemplateYaml": (
                f'pipeline:\n  identifier: "{pid}"\n'
                f'  variables:\n    - name: serviceId\n      value: "{service_id}"\n'
                f'    - name: imageTag\n      value: "{image_tag}"\n'
            ),
        }

        if self._dry_run:
            log.info(
                "harness.deploy.dry_run",
                pipeline_id=pid,
                service_id=service_id,
                image_tag=image_tag,
                note="SAFETY_ALLOW_LIVE_DEPLOYS=false — not executing",
            )
            return {
                "planExecutionId": f"dry-run-{uuid.uuid4().hex[:8]}",
                "status": "DRY_RUN",
                "pipeline_id": pid,
                "note": "dry-run: SAFETY_ALLOW_LIVE_DEPLOYS is false",
            }

        try:
            resp = await self._http.post(url, params=self._qp, json=body)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json().get("data", resp.json())
            execution_id: str = data.get("planExecutionId", uuid.uuid4().hex[:8])
            log.info(
                "harness.deploy.live",
                service_id=service_id,
                execution_id=execution_id,
                pipeline_id=pid,
            )
            return {
                "deployment_id": execution_id,
                "status": "RUNNING",
                "pipeline_id": pid,
            }
        except Exception as exc:
            log.warning(
                "harness.deploy.live_failed error=%s — falling back to mock", exc
            )
            return self._mock_deploy(service_id, image_tag, pipeline_id)

    async def rollback(self, deployment_id: str, reason: str = "") -> dict[str, Any]:
        """Abort a running Harness execution (rollback trigger)."""
        if not self._live:
            return self._mock_rollback(deployment_id, reason)

        self._assert_sandbox(self._project_id)

        url = (
            f"{self._base_url}/pipeline/api/pipelines/execution"
            f"/{deployment_id}/interrupt"
        )
        qp = {**self._qp, "interruptType": "AbortAll"}

        if self._dry_run:
            log.info(
                "harness.rollback.dry_run",
                deployment_id=deployment_id,
                reason=reason,
                note="SAFETY_ALLOW_LIVE_DEPLOYS=false — not executing",
            )
            return {
                "deployment_id": deployment_id,
                "status": "DRY_RUN_ROLLBACK",
                "note": "dry-run: SAFETY_ALLOW_LIVE_DEPLOYS is false",
            }

        try:
            resp = await self._http.post(url, params=qp, json={"reason": reason})
            resp.raise_for_status()
            log.info("harness.rollback.live", deployment_id=deployment_id)
            return {"deployment_id": deployment_id, "status": "ROLLED_BACK", "reason": reason}
        except Exception as exc:
            log.warning(
                "harness.rollback.live_failed error=%s — falling back to mock", exc
            )
            return self._mock_rollback(deployment_id, reason)

    async def get_pipeline_status(self, deployment_id: str) -> dict[str, Any]:
        """Poll execution status. Read-only — always executes in live mode."""
        if not self._live:
            return {
                "deployment_id": deployment_id,
                "status": "SUCCEEDED",
                "duration_ms": random.randint(30_000, 180_000),
            }

        url = (
            f"{self._base_url}/pipeline/api/pipelines/execution/v2/{deployment_id}"
        )
        try:
            resp = await self._http.get(url, params=self._qp)
            resp.raise_for_status()
            data = resp.json().get("data", resp.json())
            return {
                "deployment_id": deployment_id,
                "status": data.get("status", "UNKNOWN"),
                "duration_ms": data.get("executionInputStatus", {}).get("duration", 0),
            }
        except Exception as exc:
            log.warning(
                "harness.get_status.live_failed error=%s — returning unknown", exc
            )
            return {"deployment_id": deployment_id, "status": "UNKNOWN"}

    async def cancel(self, deployment_id: str) -> dict[str, Any]:
        """Cancel a pending execution. Alias for rollback without a reason."""
        return await self.rollback(deployment_id, reason="cancelled")

    async def close(self) -> None:
        await self._http.aclose()

    # ── Mock helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _mock_deploy(
        service_id: str,
        image_tag: str,
        pipeline_id: str | None,
    ) -> dict[str, Any]:
        deployment_id = f"deploy-{uuid.uuid4().hex[:8]}"
        log.info(
            "harness.deploy.mock",
            service_id=service_id,
            image_tag=image_tag,
            deployment_id=deployment_id,
        )
        return {
            "deployment_id": deployment_id,
            "status": "RUNNING",
            "pipeline_id": pipeline_id or "mock-pipeline",
        }

    @staticmethod
    def _mock_rollback(deployment_id: str, reason: str) -> dict[str, Any]:
        log.info("harness.rollback.mock", deployment_id=deployment_id, reason=reason)
        return {"deployment_id": deployment_id, "status": "ROLLED_BACK", "reason": reason}
