"""
Canary Orchestrator — translates RiskVerdict into a Harness canary deployment.

Tool permissions (from .github/agents/canary-orchestrator.agent.md):
  ALLOW: harness.deploy, harness.rollback, aws_mcp.ecs_*, atlassian_mcp.* (none)
  DENY:  atlassian_mcp.*, rag.query, memory.*, github_mcp.*

Pipeline (deploy):
  1.  Determine human_approval_required from strategy + PCI scope
  2.  Poll for human approval token (DEMO_APPROVAL_TIMEOUT_SECONDS, default 30)
  3.  Enforce canary cap: if recommended_pct > 50, cap at 50 + note
  4.  Build OPA context with all required fields
  5.  OPAClient.check("harness.deploy") — never call Harness if denied
  6.  Call HarnessClient.deploy(); capture harness_execution_id
  7.  Return DeploymentHandle with opa_check_passed=True
  8.  Emit OpenTelemetry span with GenAI + release_pilot attributes

rollback:
  1.  OPAClient.check("harness.rollback")
  2.  HarnessClient.rollback(deployment_id)
  3.  Emit span with release_pilot.rollback=True
  4.  Return RollbackResult
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel

from src.agents.risk_analyst import RiskVerdict
from src.opa_client import OPAClient, PolicyViolationError
from src.redactor import PCIRedactor
from src.telemetry import get_tracer, record_tool_call
from src.tools.harness_client import HarnessClient

log = logging.getLogger(__name__)

# ── Backward-compat models (orchestrator + compliance_auditor import these) ───


class CanaryStep(BaseModel):
    step: int
    traffic_pct: int
    status: Literal["IN_PROGRESS", "HEALTHY", "DEGRADED", "ROLLED_BACK"]
    duration_s: float
    task_definition_arn: str
    alb_listener_rule_arn: str


class CanaryResult(BaseModel):
    service_id: str
    pr_number: int
    deployment_id: str
    outcome: Literal["PROMOTED", "ROLLED_BACK", "ABORTED"]
    steps: list[CanaryStep]
    final_traffic_pct: int
    rollback_reason: str | None
    trace_id: str
    started_at: str
    completed_at: str


# ── Spec-defined output models ────────────────────────────────────────────────


class SuccessCriteria(BaseModel):
    max_error_rate_pct: float   # e.g. 0.1 → 0.1%
    max_p99_ms: int
    min_confidence: float


class DeploymentHandle(BaseModel):
    deployment_id: str
    service: str
    canary_pct: int              # POST-CAP value (≤50 in first 30 min)
    bake_minutes: int
    start_time: str              # ISO 8601
    success_criteria: SuccessCriteria
    harness_execution_id: str
    human_approval_required: bool
    human_approval_token: str | None
    opa_check_passed: bool


class RollbackResult(BaseModel):
    deployment_id: str
    service: str
    outcome: Literal["ROLLED_BACK", "ROLLBACK_FAILED"]
    reason: str
    timestamp: str
    opa_check_passed: bool


# ── Error types ───────────────────────────────────────────────────────────────


class ApprovalTimeoutError(Exception):
    """Raised when the approval poll window expires with no token."""

    def __init__(self, service: str, timeout_seconds: int) -> None:
        self.service = service
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Approval timeout after {timeout_seconds}s waiting for {service}"
        )


# ── Agent class ───────────────────────────────────────────────────────────────


class CanaryOrchestrator:
    def __init__(self) -> None:
        self._harness = HarnessClient()
        self._opa = OPAClient()
        self._redactor = PCIRedactor()
        self._http = httpx.AsyncClient(
            base_url=os.getenv("AWS_MOCK_URL", "http://localhost:8080"),
            timeout=15.0,
        )
        # AsyncOpenAI kept for future agentic tool-call extensions
        self._llm = AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY", "demo"),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )
        self._model = os.getenv("AGENT_MODEL", "gpt-4o")
        self._tracer = get_tracer("canary-orchestrator")

    # ── Public interface ──────────────────────────────────────────────────────

    async def deploy(
        self, risk_verdict: RiskVerdict, trace_id: str = ""
    ) -> DeploymentHandle:
        """Translate a RiskVerdict into a live Harness canary deployment.

        Returns a DeploymentHandle that the SLO Sentinel uses to watch progress.
        Raises PolicyViolationError if OPA denies the deploy.
        Raises ApprovalTimeoutError if human approval poll times out.
        """
        service = risk_verdict.blast_radius.service
        strategy = risk_verdict.recommended_strategy

        with self._tracer.start_as_current_span("canary_orchestrator.deploy") as span:
            span.set_attribute("gen_ai.agent.name", "canary-orchestrator")
            span.set_attribute("service.name", service)
            span.set_attribute("release_pilot.risk_level", risk_verdict.risk_level)
            if trace_id:
                span.set_attribute("trace_id", trace_id)

            # 1. Determine human_approval_required
            human_approval_required = (
                not strategy.auto_promote or risk_verdict.pci_scope_touched
            )

            # 2. Poll for approval token (blocks until approved or timeout)
            human_approval_token: str | None = None
            if human_approval_required:
                human_approval_token = await self._poll_for_approval(service)

            # 3. Enforce canary cap deterministically
            recommended_pct = strategy.canary_pct
            if recommended_pct > 50:
                canary_pct = 50
                log.info(
                    "canary.cap_applied service=%s capped_from=%d capped_to=50 "
                    "note='canary capped from %d%% to 50%% by 30-min rule'",
                    service, recommended_pct, recommended_pct,
                )
            else:
                canary_pct = recommended_pct

            # 4. Build OPA context
            touches_iam = any(
                kw in " ".join(risk_verdict.guardrails_triggered).upper()
                for kw in ("IAM", "ROLE", "POLICY")
            )
            approval_chain = [human_approval_token] if human_approval_token else []
            opa_context: dict[str, Any] = {
                "risk_level": risk_verdict.risk_level,
                "pci_scope_touched": risk_verdict.pci_scope_touched,
                "human_approval_token": human_approval_token,
                "terraform_diff": {"touches_iam": touches_iam},
                "approval_chain": approval_chain,
                "canary_pct": canary_pct,
                "canary_elapsed_minutes": 0,
                "confidence": risk_verdict.confidence,
                "metrics_degraded": False,
            }

            # 5. OPA check — Harness is NEVER called if denied
            self._opa.check(action="harness.deploy", context=opa_context)
            # ^ raises PolicyViolationError on denial; not caught here — propagates up

            record_tool_call(span, "harness.deploy", allowed=True)

            # 6. Call Harness
            image_uri = (
                f"123456789012.dkr.ecr.us-east-1.amazonaws.com"
                f"/{service}:latest"
            )
            harness_result = await self._harness.deploy(service, image_uri)
            harness_execution_id = harness_result.get(
                "deployment_id", f"exec-{uuid.uuid4().hex[:8]}"
            )

            # 7. Build DeploymentHandle
            success_criteria = self._build_success_criteria(risk_verdict)
            handle = DeploymentHandle(
                deployment_id=str(uuid.uuid4()),
                service=service,
                canary_pct=canary_pct,
                bake_minutes=strategy.bake_minutes,
                start_time=datetime.now(timezone.utc).isoformat(),
                success_criteria=success_criteria,
                harness_execution_id=harness_execution_id,
                human_approval_required=human_approval_required,
                human_approval_token=human_approval_token,
                opa_check_passed=True,
            )

            # 8. OTel
            span.set_attribute("release_pilot.canary_pct", canary_pct)
            span.set_attribute("release_pilot.opa.allowed", True)
            span.set_attribute("deployment.id", handle.deployment_id)

            log.info(
                "CANARY_DEPLOYED service=%s pct=%d deployment_id=%s harness=%s",
                service, canary_pct, handle.deployment_id, harness_execution_id,
            )
            return handle

    async def rollback(self, handle: DeploymentHandle) -> RollbackResult:
        """OPA-check and execute a Harness rollback.

        Returns RollbackResult with timestamp and outcome.
        """
        with self._tracer.start_as_current_span("canary_orchestrator.rollback") as span:
            span.set_attribute("gen_ai.agent.name", "canary-orchestrator")
            span.set_attribute("release_pilot.rollback", True)
            span.set_attribute("deployment.id", handle.deployment_id)
            span.set_attribute("service.name", handle.service)

            # OPA check for rollback action
            opa_check_passed = True
            try:
                self._opa.check(
                    action="harness.rollback",
                    context={
                        "risk_level": "HIGH",  # conservative for rollback gate
                        "human_approval_token": handle.human_approval_token,
                        "pci_scope_touched": False,
                    },
                )
            except PolicyViolationError as exc:
                log.warning(
                    "opa.rollback_advisory_denial reasons=%s — proceeding anyway",
                    exc.denial_reasons,
                )
                opa_check_passed = False

            record_tool_call(span, "harness.rollback", allowed=opa_check_passed)

            try:
                await self._harness.rollback(
                    handle.deployment_id, reason="SLO Sentinel triggered rollback"
                )
                outcome: Literal["ROLLED_BACK", "ROLLBACK_FAILED"] = "ROLLED_BACK"
            except Exception as exc:
                log.error("harness.rollback_failed error=%s", exc)
                outcome = "ROLLBACK_FAILED"

            result = RollbackResult(
                deployment_id=handle.deployment_id,
                service=handle.service,
                outcome=outcome,
                reason="SLO Sentinel triggered rollback",
                timestamp=datetime.now(timezone.utc).isoformat(),
                opa_check_passed=opa_check_passed,
            )
            log.info(
                "ROLLBACK_RESULT deployment_id=%s service=%s outcome=%s",
                handle.deployment_id, handle.service, outcome,
            )
            return result

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _poll_for_approval(self, service: str) -> str:
        """Poll every 10 s for a human approval token.

        Demo mode: auto-approves after 2 s if DEMO_APPROVAL_TOKEN is not set.
        Production: polls until DEMO_APPROVAL_TIMEOUT_SECONDS expires.
        """
        timeout = int(os.getenv("DEMO_APPROVAL_TIMEOUT_SECONDS", "30"))
        demo_mode = os.getenv("DEMO_MODE", "true").lower() == "true"

        # Check for pre-set token first
        preset = os.getenv("DEMO_APPROVAL_TOKEN")
        if preset:
            log.info("approval.token_preset service=%s", service)
            return preset

        log.warning(
            "deployment_blocked_pending_approval service=%s timeout=%ds",
            service, timeout,
        )

        if demo_mode:
            # Simulate a brief approval delay in demo mode
            await asyncio.sleep(2)
            token = f"demo-approval-{uuid.uuid4().hex[:8]}"
            log.info("approval.auto_granted service=%s token=%s", service, token)
            return token

        # Production: poll every 10s until timeout
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            token = os.getenv("DEMO_APPROVAL_TOKEN")
            if token:
                log.info("approval.token_received service=%s", service)
                return token
            await asyncio.sleep(10)

        raise ApprovalTimeoutError(service, timeout)

    def _build_success_criteria(self, verdict: RiskVerdict) -> SuccessCriteria:
        """Derive SLO thresholds from risk verdict (PCI tighter than non-PCI)."""
        if verdict.pci_scope_touched:
            return SuccessCriteria(
                max_error_rate_pct=0.1,
                max_p99_ms=400,
                min_confidence=0.8,
            )
        if verdict.risk_level == "HIGH":
            return SuccessCriteria(
                max_error_rate_pct=0.3,
                max_p99_ms=500,
                min_confidence=0.75,
            )
        return SuccessCriteria(
            max_error_rate_pct=0.5,
            max_p99_ms=600,
            min_confidence=0.6,
        )

    async def close(self) -> None:
        await self._http.aclose()
        await self._harness.close()
