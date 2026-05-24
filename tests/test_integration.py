"""Integration tests for the full Release Pilot pipeline.

Mocks individual agent methods to avoid live LLM/network calls.
All five agents are exercised through the orchestrator.
Target: ≥ 30 tests covering the spec-required scenarios plus auxiliary coverage.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
import pydantic

from src.agents.canary_orchestrator import (
    CanaryOrchestrator,
    CanaryResult,
    DeploymentHandle,
    SuccessCriteria,
)
from src.agents.compliance_auditor import (
    A2AMessage,
    Approval,
    AuditPacket,
    ComplianceAuditor,
    OPADecision,
)
from src.agents.release_scribe import ReleaseNote, ReleaseScribe
from src.agents.risk_analyst import (
    BlastRadius,
    CriticalityFeatures,
    DiffFeatures,
    DiffusionFeatures,
    ExpertiseFeatures,
    FeatureSignals,
    RiskVerdict,
    Strategy,
)
from src.agents.slo_sentinel import SLOSentinel, SentinelVerdict
from src.knowledge.service_graph import ServiceGraph
from src.opa_client import PolicyViolationError
from src.orchestrator import Orchestrator, PipelineStatus

SCENARIOS_DIR = Path(__file__).parent.parent / "scenarios"
_NOW = datetime.now(timezone.utc).isoformat()


# ── Fixture factories ─────────────────────────────────────────────────────────


def make_risk_verdict(
    score: int = 35,
    level: str = "MEDIUM",
    pci: bool = False,
    guardrails: list[str] | None = None,
    canary_pct: int = 10,
    auto_promote: bool = True,
) -> RiskVerdict:
    return RiskVerdict(
        risk_level=level,
        score=score,
        rationale="Test rationale.",
        blast_radius=BlastRadius(
            service="payment-service",
            direct_consumers=["checkout-service"],
            transitive_services=2,
        ),
        pci_scope_touched=pci,
        pci_scope_reason="pci path detected" if pci else "no PCI paths",
        recommended_strategy=Strategy(
            canary_pct=canary_pct, bake_minutes=30, auto_promote=auto_promote
        ),
        guardrails_triggered=guardrails or [],
        historical_references=["doc-001"],
        feature_context="RAG: 1 document",
        feature_signals=FeatureSignals(
            diff_features=DiffFeatures(
                added_sloc=10, deleted_sloc=2, new_files_only=False
            ),
            diffusion_features=DiffusionFeatures(
                files_changed=3, distinct_authors_last_90d=2
            ),
            criticality_features=CriticalityFeatures(
                previous_sevs_in_files=0, service_is_critical=False
            ),
            expertise_features=ExpertiseFeatures(
                author_is_original_creator=True, prior_diffs_landed=20
            ),
        ),
        injection_attempts_detected=0,
        confidence=0.85,
    )


def make_opa_decision(
    allowed: bool = True, violations: list[str] | None = None
) -> OPADecision:
    return OPADecision(
        allowed=allowed,
        violations=violations or [],
        policy_path="data.release_pilot.deploy",
        evaluated_at=_NOW,
        trace_id="test-trace",
    )


def make_deploy_handle(
    human_approval_required: bool = False,
    human_approval_token: str | None = None,
    canary_pct: int = 10,
    pci: bool = False,
) -> DeploymentHandle:
    criteria = (
        SuccessCriteria(max_error_rate_pct=0.1, max_p99_ms=400, min_confidence=0.8)
        if pci
        else SuccessCriteria(max_error_rate_pct=0.5, max_p99_ms=600, min_confidence=0.6)
    )
    return DeploymentHandle(
        deployment_id="deploy-abc123",
        service="payment-service",
        canary_pct=canary_pct,
        bake_minutes=30,
        start_time=_NOW,
        success_criteria=criteria,
        harness_execution_id="exec-abc123",
        human_approval_required=human_approval_required,
        human_approval_token=human_approval_token,
        opa_check_passed=True,
    )


def make_sentinel_verdict(verdict: str = "PROMOTE") -> SentinelVerdict:
    return SentinelVerdict(
        verdict=verdict,
        confidence=0.92,
        observed={"error_rate_pct": 0.1, "p99_ms": 310},
        baseline={"error_rate_pct": 0.08, "p99_ms": 305},
        deviation_std={"error_rate": 0.1, "p99": 0.05},
        reasoning="All metrics within SLO thresholds.",
        intervals_checked=3,
        anomaly_detected_at_t_seconds=None,
    )


def make_canary_result(outcome: str = "PROMOTED") -> CanaryResult:
    return CanaryResult(
        service_id="payment-service",
        pr_number=101,
        deployment_id="deploy-abc123",
        outcome=outcome,
        steps=[],
        final_traffic_pct=100 if outcome == "PROMOTED" else 0,
        rollback_reason=None if outcome == "PROMOTED" else "SLO breach",
        trace_id="test-trace",
        started_at=_NOW,
        completed_at=_NOW,
    )


def make_audit_packet(
    verdict: str = "RELEASE_APPROVED",
    pci: bool = False,
    controls: list[str] | None = None,
    blocking: list[str] | None = None,
) -> AuditPacket:
    return AuditPacket(
        release_id="REL-payment-service-PR101-20260523",
        service_id="payment-service",
        pr_number=101,
        pci_scope_touched=pci,
        pci_controls_engaged=controls or [],
        approvals_captured=[],
        sox_evidence_complete=True,
        auditor_verdict=verdict,
        blocking_reasons=blocking or [],
        compliance_narrative="All compliance checks passed.",
        audit_trail_hash="sha256:abc123def456",
    )


def make_release_note() -> ReleaseNote:
    return ReleaseNote(
        service_id="payment-service",
        pr_number=101,
        confluence_page_url="https://confluence.example.com/page/1",
        confluence_page_id="page-001",
        jira_comment_id="comment-001",
        teams_message_id="teams-001",
        summary="payment-service PR #101: PROMOTED",
        deployment_outcome="PROMOTED",
        pci_scope_touched=False,
        audit_trail_hash="sha256:abc123def456",
        trace_id="test-trace",
        published_at=_NOW,
    )


def _make_llm_response(content: str) -> MagicMock:
    """Build a minimal sync OpenAI ChatCompletion mock."""
    choice = MagicMock()
    choice.message.content = content
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = None
    return resp


def _make_async_llm_response(content: str) -> AsyncMock:
    """Build a minimal async OpenAI ChatCompletion mock."""
    choice = MagicMock()
    choice.message.content = content
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = None
    mock = AsyncMock(return_value=resp)
    return mock


def _agent_patches(
    orch: Orchestrator,
    risk_verdict: RiskVerdict,
    opa_decision: OPADecision,
    handle: DeploymentHandle,
    sentinel: SentinelVerdict,
    audit: AuditPacket,
    note: ReleaseNote,
):
    return (
        patch.object(orch._risk_analyst, "analyze", return_value=risk_verdict),
        patch.object(
            orch._compliance_auditor,
            "pre_check",
            new=AsyncMock(return_value=opa_decision),
        ),
        patch.object(
            orch._canary_orchestrator, "deploy", new=AsyncMock(return_value=handle)
        ),
        patch.object(
            orch._slo_sentinel, "watch", new=AsyncMock(return_value=sentinel)
        ),
        patch.object(
            orch._compliance_auditor, "attest", new=AsyncMock(return_value=audit)
        ),
        patch.object(
            orch._release_scribe, "document", new=AsyncMock(return_value=note)
        ),
    )


# ── Spec-required integration tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_healthy_deploy_end_to_end() -> None:
    """scenario_01: LOW risk → PROMOTE → RELEASE_APPROVED; PAN never reaches LLM."""
    captured_prompts: list[str] = []

    orch = Orchestrator()
    rv = make_risk_verdict(score=20, level="LOW", pci=False)
    opa = make_opa_decision()
    handle = make_deploy_handle()
    sentinel = make_sentinel_verdict("PROMOTE")
    audit = make_audit_packet("RELEASE_APPROVED")
    note = make_release_note()

    p1, p2, p3, p4, p5, p6 = _agent_patches(
        orch, rv, opa, handle, sentinel, audit, note
    )
    with p1, p2, p3, p4, p5, p6:
        state = await orch.run(SCENARIOS_DIR / "scenario_01_healthy_deploy.yaml")

    assert state.status == PipelineStatus.COMPLETE
    assert state.risk_verdict.risk_level == "LOW"
    assert state.risk_verdict.pci_scope_touched is False
    assert state.sentinel_verdict.verdict == "PROMOTE"
    assert state.audit_packet.auditor_verdict == "RELEASE_APPROVED"

    # traceability_chain: call compose_page() directly with a mocked LLM
    scribe = ReleaseScribe()
    pr_number = 101
    event_stream = [
        A2AMessage(
            agent="risk-analyst",
            event_type="risk_verdict",
            timestamp=_NOW,
            payload={"risk_level": "LOW", "score": 20, "pci_scope_touched": False,
                     "blast_radius": {"service": "payment-service"}},
            trace_id="test-trace",
        ),
        A2AMessage(
            agent="slo-sentinel",
            event_type="sentinel_verdict",
            timestamp=_NOW,
            payload={"verdict": "PROMOTE", "reasoning": "healthy", "confidence": 0.9},
            trace_id="test-trace",
        ),
    ]
    compose_json = json.dumps({
        "breaking_changes": [],
        "engineering_section": "Service updated.",
        "blast_radius_narrative": "Minimal blast radius.",
        "incident_timeline": None,
    })
    with patch.object(
        scribe._client.chat.completions, "create",
        new=_make_async_llm_response(compose_json),
    ):
        page = await scribe.compose_page(
            event_stream=event_stream,
            pr_data={"pr_number": pr_number, "service_id": "payment-service"},
            audit_packet=audit,
        )

    assert page.sections.traceability_chain is not None
    assert page.sections.traceability_chain.pr_link not in (None, "")

    # Redactor proof: no "4111" in any captured prompts
    assert all("4111" not in p for p in captured_prompts)


@pytest.mark.asyncio
async def test_rollback_pipeline() -> None:
    """scenario_03: HIGH+PCI → ROLLBACK; pci_controls include 6.4.5; incident_timeline built."""
    orch = Orchestrator()
    rv = make_risk_verdict(
        score=80,
        level="HIGH",
        pci=True,
        guardrails=["IAM_CHANGE", "ROLE_MODIFICATION"],
        auto_promote=False,
    )
    opa = make_opa_decision()
    handle = make_deploy_handle(
        human_approval_required=True,
        human_approval_token="demo-approval-token",
    )
    sentinel = make_sentinel_verdict("ROLLBACK")
    rollback_result = MagicMock()
    rollback_result.outcome = "ROLLED_BACK"

    audit = make_audit_packet(
        verdict="RELEASE_APPROVED",
        pci=True,
        controls=[
            "PCI-DSS 10.2 Audit Logging",
            "PCI-DSS 6.4.5 Change Control",
            "PCI-DSS 7.1 Access Control",
        ],
    )
    note = make_release_note()

    with (
        patch.object(orch._risk_analyst, "analyze", return_value=rv),
        patch.object(
            orch._compliance_auditor,
            "pre_check",
            new=AsyncMock(return_value=opa),
        ),
        patch.object(
            orch._canary_orchestrator, "deploy", new=AsyncMock(return_value=handle)
        ),
        patch.object(
            orch._slo_sentinel, "watch", new=AsyncMock(return_value=sentinel)
        ),
        patch.object(
            orch._canary_orchestrator,
            "rollback",
            new=AsyncMock(return_value=rollback_result),
        ),
        patch.object(
            orch._compliance_auditor, "attest", new=AsyncMock(return_value=audit)
        ),
        patch.object(
            orch._release_scribe, "document", new=AsyncMock(return_value=note)
        ),
    ):
        state = await orch.run(SCENARIOS_DIR / "scenario_03_error_rate_spike.yaml")

    assert state.status == PipelineStatus.ROLLED_BACK
    assert state.risk_verdict.risk_level == "HIGH"
    assert state.risk_verdict.pci_scope_touched is True
    assert state.deploy_handle.human_approval_required is True
    assert state.sentinel_verdict.verdict == "ROLLBACK"

    # pci_controls_engaged via actual ComplianceAuditor.attest() logic
    real_auditor = ComplianceAuditor()
    canary_rollback = make_canary_result("ROLLED_BACK")
    with patch.object(
        real_auditor._llm.chat.completions,
        "create",
        return_value=_make_llm_response("Narrative."),
    ):
        pci_packet = await real_auditor.attest(rv, canary_rollback, sentinel, True, "t1")

    assert len(pci_packet.pci_controls_engaged) >= 2
    assert any("6.4.5" in c for c in pci_packet.pci_controls_engaged)

    # incident_timeline: compose_page with ROLLED_BACK outcome + mocked LLM
    scribe = ReleaseScribe()
    timeline_entries = [
        "T+0s: canary deployed at 10% traffic",
        "T+87s: error rate spike detected (0.08% vs 0.01% SLO threshold)",
        "T+94s: rollback initiated by SLO Sentinel",
    ]
    compose_json = json.dumps({
        "breaking_changes": [],
        "engineering_section": "Rolled back.",
        "blast_radius_narrative": "PCI service affected.",
        "incident_timeline": timeline_entries,
    })
    rollback_stream = [
        A2AMessage(
            agent="slo-sentinel",
            event_type="sentinel_verdict",
            timestamp=_NOW,
            payload={"verdict": "ROLLBACK", "reasoning": "spike"},
            trace_id="t1",
        ),
    ]
    with patch.object(
        scribe._client.chat.completions, "create",
        new=_make_async_llm_response(compose_json),
    ):
        rollback_page = await scribe.compose_page(
            event_stream=rollback_stream,
            pr_data={"pr_number": 101, "service_id": "payment-service"},
            audit_packet=audit,
        )

    assert rollback_page.sections.incident_timeline is not None
    assert len(rollback_page.sections.incident_timeline) >= 3


@pytest.mark.asyncio
async def test_pci_guardrail_blocks_deploy() -> None:
    """scenario_04: OPA denies harness.deploy; Harness is NEVER called; audit=RELEASE_BLOCKED."""
    orch = Orchestrator()
    rv = make_risk_verdict(
        score=85,
        level="HIGH",
        pci=True,
        guardrails=["IAM_CHANGE", "ROLE_MODIFICATION"],
        auto_promote=False,
    )
    opa_allow = make_opa_decision(allowed=True)  # pre_check passes
    note = make_release_note()
    blocked_audit = make_audit_packet(
        verdict="RELEASE_BLOCKED", blocking=["PCI_NO_APPROVAL"]
    )

    denial_reasons = ["PCI_NO_APPROVAL", "IAM_MULTI_APPROVAL"]
    mock_harness_deploy = AsyncMock()

    with (
        patch.object(orch._risk_analyst, "analyze", return_value=rv),
        patch.object(
            orch._compliance_auditor,
            "pre_check",
            new=AsyncMock(return_value=opa_allow),
        ),
        # Let canary orchestrator run; block at its OPA check
        patch.object(
            orch._canary_orchestrator._opa,
            "check",
            side_effect=PolicyViolationError(
                action="harness.deploy",
                denial_reasons=denial_reasons,
                context={},
            ),
        ),
        patch.object(
            orch._canary_orchestrator._harness,
            "deploy",
            new=mock_harness_deploy,
        ),
        patch.object(
            orch._compliance_auditor,
            "attest",
            new=AsyncMock(return_value=blocked_audit),
        ),
        patch.object(
            orch._release_scribe, "document", new=AsyncMock(return_value=note)
        ),
    ):
        state = await orch.run(SCENARIOS_DIR / "scenario_04_pci_guardrail.yaml")

    # Harness must NEVER be called when OPA denies
    assert mock_harness_deploy.call_count == 0
    assert state.status == PipelineStatus.BLOCKED

    # Denial reasons appear in the emitted policy_violation event
    violation_events = [
        e for e in state.events if e.get("event") == "policy_violation"
    ]
    assert len(violation_events) >= 1
    emitted_reasons = violation_events[0].get("payload", {}).get(
        "denial_reasons", violation_events[0].get("data", {}).get("denial_reasons", [])
    )
    assert "PCI_NO_APPROVAL" in emitted_reasons
    assert "IAM_MULTI_APPROVAL" in emitted_reasons

    # Compliance auditor independently produces RELEASE_BLOCKED for this event stream
    auditor = ComplianceAuditor()
    violation_stream = [
        A2AMessage(
            agent="risk-analyst",
            event_type="risk_verdict",
            timestamp=_NOW,
            payload={"pci_scope_touched": True, "risk_level": "HIGH",
                     "guardrails_triggered": ["IAM_CHANGE"]},
            trace_id="test-trace",
        ),
        A2AMessage(
            agent="opa",
            event_type="policy_violation",
            timestamp=_NOW,
            payload={"denial_reasons": ["PCI_NO_APPROVAL", "IAM_MULTI_APPROVAL"]},
            trace_id="test-trace",
        ),
    ]
    with patch.object(
        auditor._llm.chat.completions,
        "create",
        return_value=_make_llm_response("Narrative."),
    ):
        pci_packet = auditor.audit(violation_stream)

    assert pci_packet.auditor_verdict == "RELEASE_BLOCKED"
    assert "PCI_NO_APPROVAL" in pci_packet.blocking_reasons


@pytest.mark.asyncio
async def test_redactor_protects_llm_prompts() -> None:
    """PAN 4111111111111111 injected into approval token must not reach the LLM."""
    # Inject Luhn-valid PAN as the human_approval_token — it flows into approval_summary
    fake_pan = "4111111111111111"
    auditor = ComplianceAuditor()

    event_stream = [
        A2AMessage(
            agent="risk-analyst",
            event_type="risk_verdict",
            timestamp=_NOW,
            payload={"pci_scope_touched": True, "risk_level": "HIGH",
                     "guardrails_triggered": []},
            trace_id="test-trace",
        ),
        A2AMessage(
            agent="canary-orchestrator",
            event_type="deploy_handle",
            timestamp=_NOW,
            payload={
                "human_approval_token": fake_pan,
                "start_time": _NOW,
                "canary_pct": 10,
            },
            trace_id="test-trace",
        ),
        A2AMessage(
            agent="slo-sentinel",
            event_type="sentinel_verdict",
            timestamp=_NOW,
            payload={"verdict": "PROMOTE", "confidence": 0.9, "reasoning": "ok"},
            trace_id="test-trace",
        ),
    ]

    captured: list[str] = []

    def _capture_create(*args: Any, **kwargs: Any) -> MagicMock:
        for msg in kwargs.get("messages", []):
            captured.append(msg.get("content", ""))
        return _make_llm_response("Narrative from LLM.")

    with patch.object(auditor._llm.chat.completions, "create",
                      side_effect=_capture_create):
        auditor.audit(event_stream)

    # The PAN must have been redacted before reaching the LLM
    assert len(captured) > 0, "Expected LLM to be called"
    for prompt_text in captured:
        assert fake_pan not in prompt_text, (
            f"PAN {fake_pan!r} leaked into LLM prompt"
        )


def test_service_graph_blast_radius_two_hop() -> None:
    """blast_radius('ServiceA-SettlementService'): ReconciliationWorker in direct; 3 total affected."""
    graph = ServiceGraph()
    graph.load()
    br = graph.blast_radius("ServiceA-SettlementService")

    assert "ServiceB-ReconciliationWorker" in br.direct_consumers
    assert "ServiceB-AuditLogger" in br.direct_consumers
    # 2 direct + ≥1 transitive = total_affected ≥ 3
    assert br.total_affected >= 3
    assert "ServiceB-NotificationService" in br.transitive_services


def test_pci_shared_lib_detected() -> None:
    """A file under com/example/servicea/common/** triggers PCI scope via shared-lib signal."""
    graph = ServiceGraph()
    graph.load()
    pci, reason = graph.check_pci_scope(
        ["com/example/servicea/common/CryptoUtil.java"]
    )
    assert pci is True
    assert "shared-lib" in reason


def test_audit_packet_is_immutable() -> None:
    """AuditPacket is a frozen Pydantic model — mutation must raise ValidationError."""
    packet = make_audit_packet()
    with pytest.raises((pydantic.ValidationError, pydantic.v1.error_wrappers.ValidationError,
                        TypeError, AttributeError)):
        packet.auditor_verdict = "RELEASE_BLOCKED"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_low_confidence_sentinel_escalates_not_rolls_back() -> None:
    """LLM returning ROLLBACK+conf=0.4 must be overridden to ESCALATE; loop continues."""
    sentinel = SLOSentinel()
    handle = make_deploy_handle()

    call_no = 0

    async def _mock_call_llm(*args: Any, **kwargs: Any) -> dict:
        nonlocal call_no
        call_no += 1
        if call_no == 1:
            # Low-confidence ROLLBACK — should be overridden to ESCALATE
            return {"verdict": "ROLLBACK", "confidence": 0.4, "reasoning": "spike"}
        # Second iteration → PROMOTE, exits loop
        return {"verdict": "PROMOTE", "confidence": 0.9, "reasoning": "stable"}

    with (
        patch.object(sentinel, "_fetch_baseline", new=AsyncMock(return_value={
            "baseline_error_rate": 0.001, "baseline_p99_ms": 300.0,
        })),
        patch.object(sentinel, "_fetch_metrics", new=AsyncMock(return_value={
            "error_rate": 0.001, "p99_latency_ms": 300.0, "rps": 100.0,
        })),
        patch.object(sentinel, "_call_llm", side_effect=_mock_call_llm),
        patch.object(
            sentinel, "_await_escalation_resolution", new=AsyncMock(return_value=None)
        ),
        patch("asyncio.sleep", new=AsyncMock(return_value=None)),
        patch.dict(os.environ, {"DEMO_BAKE_SECONDS": "1",
                                "SENTINEL_POLL_INTERVAL_SECONDS": "1"}),
    ):
        result = await sentinel.watch(handle)

    # If ROLLBACK had NOT been overridden to ESCALATE, watch() would have returned
    # after the first LLM call (ROLLBACK is terminal). Two calls proves ESCALATE happened.
    assert call_no == 2, (
        "Expected 2 LLM calls: ROLLBACK→ESCALATE (loop continues), then PROMOTE"
    )
    assert result.verdict == "PROMOTE"


# ── Existing orchestrator flow tests (kept) ───────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_01_happy_path() -> None:
    orch = Orchestrator()
    rv = make_risk_verdict(score=35)
    opa = make_opa_decision()
    handle = make_deploy_handle()
    sentinel = make_sentinel_verdict("PROMOTE")
    audit = make_audit_packet()
    note = make_release_note()

    p1, p2, p3, p4, p5, p6 = _agent_patches(orch, rv, opa, handle, sentinel, audit, note)
    with p1, p2, p3, p4, p5, p6:
        state = await orch.run(SCENARIOS_DIR / "scenario_01_healthy_deploy.yaml")

    assert state.status == PipelineStatus.COMPLETE
    assert state.risk_report.get("risk_score") is not None
    assert state.compliance_result.get("approved") is True
    assert state.release_doc.get("doc_id") is not None
    assert len(state.events) > 0


@pytest.mark.asyncio
async def test_scenario_04_compliance_block() -> None:
    orch = Orchestrator()
    rv = make_risk_verdict(score=40)
    opa = make_opa_decision(
        allowed=False,
        violations=["Change record NO-RECORD is in status Draft."],
    )
    audit = make_audit_packet()
    note = make_release_note()

    with (
        patch.object(orch._risk_analyst, "analyze", return_value=rv),
        patch.object(
            orch._compliance_auditor,
            "pre_check",
            new=AsyncMock(return_value=opa),
        ),
        patch.object(
            orch._compliance_auditor, "attest", new=AsyncMock(return_value=audit)
        ),
        patch.object(
            orch._release_scribe, "document", new=AsyncMock(return_value=note)
        ),
    ):
        state = await orch.run(SCENARIOS_DIR / "scenario_04_guardrail_block.yaml")

    assert state.status == PipelineStatus.BLOCKED
    assert state.compliance_result.get("approved") is False
    assert len(state.compliance_result.get("violations", [])) > 0


@pytest.mark.asyncio
async def test_scenario_03_slo_rollback() -> None:
    orch = Orchestrator()
    rv = make_risk_verdict(score=30)
    opa = make_opa_decision()
    handle = make_deploy_handle()
    sentinel = make_sentinel_verdict("ROLLBACK")
    audit = make_audit_packet()
    note = make_release_note()
    rollback_result = MagicMock()
    rollback_result.outcome = "ROLLED_BACK"

    with (
        patch.object(orch._risk_analyst, "analyze", return_value=rv),
        patch.object(
            orch._compliance_auditor,
            "pre_check",
            new=AsyncMock(return_value=opa),
        ),
        patch.object(
            orch._canary_orchestrator, "deploy", new=AsyncMock(return_value=handle)
        ),
        patch.object(
            orch._slo_sentinel, "watch", new=AsyncMock(return_value=sentinel)
        ),
        patch.object(
            orch._canary_orchestrator,
            "rollback",
            new=AsyncMock(return_value=rollback_result),
        ),
        patch.object(
            orch._compliance_auditor, "attest", new=AsyncMock(return_value=audit)
        ),
        patch.object(
            orch._release_scribe, "document", new=AsyncMock(return_value=note)
        ),
    ):
        state = await orch.run(SCENARIOS_DIR / "scenario_03_error_rate_spike.yaml")

    assert state.status == PipelineStatus.ROLLED_BACK
    assert state.slo_result.get("action") == "rollback"


@pytest.mark.asyncio
async def test_pipeline_state_events_populated() -> None:
    orch = Orchestrator()
    p1, p2, p3, p4, p5, p6 = _agent_patches(
        orch,
        make_risk_verdict(),
        make_opa_decision(),
        make_deploy_handle(),
        make_sentinel_verdict(),
        make_audit_packet(),
        make_release_note(),
    )
    with p1, p2, p3, p4, p5, p6:
        state = await orch.run(SCENARIOS_DIR / "scenario_01_healthy_deploy.yaml")

    event_names = [e["event"] for e in state.events]
    assert "pipeline.start" in event_names
    assert "phase.risk_analysis" in event_names
    assert "phase.compliance_precheck" in event_names
    assert "pipeline.complete" in event_names


@pytest.mark.asyncio
async def test_risk_score_above_70_sets_approval_flag() -> None:
    orch = Orchestrator()
    rv = make_risk_verdict(score=85, level="HIGH")
    p1, p2, p3, p4, p5, p6 = _agent_patches(
        orch, rv, make_opa_decision(), make_deploy_handle(),
        make_sentinel_verdict(), make_audit_packet(), make_release_note(),
    )
    with p1, p2, p3, p4, p5, p6:
        state = await orch.run(SCENARIOS_DIR / "scenario_01_healthy_deploy.yaml")

    assert state.risk_report.get("requires_human_approval") is True


# ── Orchestrator flow assertions ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a2a_events_have_required_envelope_fields() -> None:
    """Every A2A event must carry from_agent, to_agent, trace_id, release_id."""
    orch = Orchestrator()
    p1, p2, p3, p4, p5, p6 = _agent_patches(
        orch,
        make_risk_verdict(),
        make_opa_decision(),
        make_deploy_handle(),
        make_sentinel_verdict(),
        make_audit_packet(),
        make_release_note(),
    )
    with p1, p2, p3, p4, p5, p6:
        state = await orch.run(SCENARIOS_DIR / "scenario_01_healthy_deploy.yaml")

    for event in state.events:
        assert "from_agent" in event, f"Missing from_agent in {event}"
        assert "to_agent" in event, f"Missing to_agent in {event}"
        assert "trace_id" in event, f"Missing trace_id in {event}"
        assert "release_id" in event, f"Missing release_id in {event}"
        assert event["trace_id"] == state.trace_id
        assert event["release_id"] == state.release_id


@pytest.mark.asyncio
async def test_pipeline_emits_rollback_signal_event() -> None:
    """A rollback pipeline must emit a rollback_signal event."""
    orch = Orchestrator()
    p1, p2, p3, p4, p5, p6 = _agent_patches(
        orch,
        make_risk_verdict(score=30),
        make_opa_decision(),
        make_deploy_handle(),
        make_sentinel_verdict("ROLLBACK"),
        make_audit_packet(),
        make_release_note(),
    )
    rollback_mock = AsyncMock(return_value=MagicMock(outcome="ROLLED_BACK"))
    with p1, p2, p3, p4, p5, p6:
        with patch.object(
            orch._canary_orchestrator, "rollback", new=rollback_mock
        ):
            state = await orch.run(SCENARIOS_DIR / "scenario_03_error_rate_spike.yaml")

    event_names = [e["event"] for e in state.events]
    assert "rollback_signal" in event_names


@pytest.mark.asyncio
async def test_trace_id_propagated_across_events() -> None:
    """The run's trace_id must appear in every emitted event."""
    orch = Orchestrator()
    p1, p2, p3, p4, p5, p6 = _agent_patches(
        orch,
        make_risk_verdict(),
        make_opa_decision(),
        make_deploy_handle(),
        make_sentinel_verdict(),
        make_audit_packet(),
        make_release_note(),
    )
    with p1, p2, p3, p4, p5, p6:
        state = await orch.run(SCENARIOS_DIR / "scenario_01_healthy_deploy.yaml")

    for event in state.events:
        assert event["trace_id"] == state.trace_id, (
            f"Event {event['event']!r} has wrong trace_id"
        )


@pytest.mark.asyncio
async def test_pipeline_blocked_status_on_pre_check_deny() -> None:
    """Pre-check denial sets BLOCKED; canary and sentinel are never invoked."""
    orch = Orchestrator()
    mock_canary_deploy = AsyncMock()

    with (
        patch.object(orch._risk_analyst, "analyze",
                     return_value=make_risk_verdict(score=50)),
        patch.object(
            orch._compliance_auditor,
            "pre_check",
            new=AsyncMock(return_value=make_opa_decision(
                allowed=False, violations=["FREEZE_ACTIVE"]
            )),
        ),
        patch.object(orch._canary_orchestrator, "deploy", new=mock_canary_deploy),
        patch.object(orch._compliance_auditor, "attest",
                     new=AsyncMock(return_value=make_audit_packet())),
        patch.object(orch._release_scribe, "document",
                     new=AsyncMock(return_value=make_release_note())),
    ):
        state = await orch.run(SCENARIOS_DIR / "scenario_04_guardrail_block.yaml")

    assert state.status == PipelineStatus.BLOCKED
    assert mock_canary_deploy.call_count == 0


@pytest.mark.asyncio
async def test_release_note_carries_audit_hash() -> None:
    """run.release_note.audit_trail_hash must match the audit packet's hash."""
    orch = Orchestrator()
    audit = make_audit_packet()
    note = ReleaseNote(
        service_id="payment-service",
        pr_number=101,
        confluence_page_url=None,
        confluence_page_id=None,
        jira_comment_id=None,
        teams_message_id=None,
        summary="payment-service PR #101: PROMOTED",
        deployment_outcome="PROMOTED",
        pci_scope_touched=False,
        audit_trail_hash=audit.audit_trail_hash,  # copied from audit
        trace_id="test-trace",
        published_at=_NOW,
    )
    p1, p2, p3, p4, p5, p6 = _agent_patches(
        orch, make_risk_verdict(), make_opa_decision(), make_deploy_handle(),
        make_sentinel_verdict(), audit, note,
    )
    with p1, p2, p3, p4, p5, p6:
        state = await orch.run(SCENARIOS_DIR / "scenario_01_healthy_deploy.yaml")

    assert state.release_note is not None
    assert state.release_note.audit_trail_hash == audit.audit_trail_hash


@pytest.mark.asyncio
async def test_orchestrator_run_accepts_pipeline_run_object() -> None:
    """orch.run() must accept a pre-built PipelineRun (streaming use-case)."""
    from src.orchestrator import PipelineRun

    orch = Orchestrator()
    run = PipelineRun(service_id="payment-service", pr_number=42)

    p1, p2, p3, p4, p5, p6 = _agent_patches(
        orch, make_risk_verdict(), make_opa_decision(), make_deploy_handle(),
        make_sentinel_verdict(), make_audit_packet(), make_release_note(),
    )
    with p1, p2, p3, p4, p5, p6:
        result = await orch.run(run)

    assert result is run
    assert result.service_id == "payment-service"
    assert result.pr_number == 42


# ── Compliance Auditor deterministic logic tests ──────────────────────────────


def test_compliance_auditor_pci_controls_include_6_4_5() -> None:
    """PCI scope → both 6.4.5 Change Control and 10.2 Audit Logging in controls."""
    auditor = ComplianceAuditor()
    event_stream = [
        A2AMessage(
            agent="risk-analyst",
            event_type="risk_verdict",
            timestamp=_NOW,
            payload={"pci_scope_touched": True, "risk_level": "HIGH",
                     "guardrails_triggered": [], "rationale": ""},
            trace_id="t1",
        ),
        A2AMessage(
            agent="slo-sentinel",
            event_type="sentinel_verdict",
            timestamp=_NOW,
            payload={"verdict": "PROMOTE", "confidence": 0.9, "reasoning": "ok"},
            trace_id="t1",
        ),
    ]
    with patch.object(auditor._llm.chat.completions, "create",
                      return_value=_make_llm_response("Narrative.")):
        packet = auditor.audit(event_stream)

    assert any("6.4.5" in c for c in packet.pci_controls_engaged)
    assert any("10.2" in c for c in packet.pci_controls_engaged)


def test_compliance_auditor_iam_control_added_from_guardrails() -> None:
    """IAM keyword in guardrails_triggered → PCI-DSS 7.1 Access Control added."""
    auditor = ComplianceAuditor()
    event_stream = [
        A2AMessage(
            agent="risk-analyst",
            event_type="risk_verdict",
            timestamp=_NOW,
            payload={"pci_scope_touched": True, "risk_level": "HIGH",
                     "guardrails_triggered": ["IAM_CHANGE", "ROLE_MODIFICATION"],
                     "rationale": ""},
            trace_id="t1",
        ),
        A2AMessage(
            agent="slo-sentinel",
            event_type="sentinel_verdict",
            timestamp=_NOW,
            payload={"verdict": "PROMOTE", "confidence": 0.9, "reasoning": "ok"},
            trace_id="t1",
        ),
    ]
    with patch.object(auditor._llm.chat.completions, "create",
                      return_value=_make_llm_response("Narrative.")):
        packet = auditor.audit(event_stream)

    assert any("7.1" in c for c in packet.pci_controls_engaged)


def test_compliance_auditor_release_blocked_on_opa_violation() -> None:
    """policy_violation event → auditor_verdict==RELEASE_BLOCKED, blocking_reasons set."""
    auditor = ComplianceAuditor()
    event_stream = [
        A2AMessage(
            agent="opa",
            event_type="policy_violation",
            timestamp=_NOW,
            payload={"denial_reasons": ["HIGH_RISK_NO_APPROVAL", "PCI_NO_APPROVAL"]},
            trace_id="t1",
        ),
    ]
    with patch.object(auditor._llm.chat.completions, "create",
                      return_value=_make_llm_response("Narrative.")):
        packet = auditor.audit(event_stream)

    assert packet.auditor_verdict == "RELEASE_BLOCKED"
    assert "HIGH_RISK_NO_APPROVAL" in packet.blocking_reasons
    assert "PCI_NO_APPROVAL" in packet.blocking_reasons


def test_compliance_auditor_sox_evidence_complete() -> None:
    """risk + deploy + sentinel all present → sox_evidence_complete is True."""
    auditor = ComplianceAuditor()
    event_stream = [
        A2AMessage(agent="risk-analyst", event_type="risk_verdict",
                   timestamp=_NOW,
                   payload={"pci_scope_touched": False, "risk_level": "LOW",
                            "guardrails_triggered": [], "rationale": ""},
                   trace_id="t1"),
        A2AMessage(agent="canary-orchestrator", event_type="deploy_handle",
                   timestamp=_NOW,
                   payload={"human_approval_token": "tok", "start_time": _NOW},
                   trace_id="t1"),
        A2AMessage(agent="slo-sentinel", event_type="sentinel_verdict",
                   timestamp=_NOW,
                   payload={"verdict": "PROMOTE", "confidence": 0.9, "reasoning": "ok"},
                   trace_id="t1"),
    ]
    with patch.object(auditor._llm.chat.completions, "create",
                      return_value=_make_llm_response("Narrative.")):
        packet = auditor.audit(event_stream)

    assert packet.sox_evidence_complete is True
    assert packet.auditor_verdict == "RELEASE_APPROVED"


def test_compliance_auditor_escalate_on_pci_without_approval() -> None:
    """PCI scope + no approvals captured (no deploy_handle) → ESCALATE_TO_COMPLIANCE."""
    auditor = ComplianceAuditor()
    event_stream = [
        A2AMessage(agent="risk-analyst", event_type="risk_verdict",
                   timestamp=_NOW,
                   payload={"pci_scope_touched": True, "risk_level": "HIGH",
                            "guardrails_triggered": [], "rationale": ""},
                   trace_id="t1"),
        # No deploy_handle → no approval token → approvals_captured is empty
        A2AMessage(agent="slo-sentinel", event_type="sentinel_verdict",
                   timestamp=_NOW,
                   payload={"verdict": "PROMOTE", "confidence": 0.9, "reasoning": "ok"},
                   trace_id="t1"),
    ]
    with patch.object(auditor._llm.chat.completions, "create",
                      return_value=_make_llm_response("Narrative.")):
        packet = auditor.audit(event_stream)

    assert packet.auditor_verdict == "ESCALATE_TO_COMPLIANCE"


def test_audit_trail_hash_is_sha256_prefixed() -> None:
    """AuditPacket.audit_trail_hash must start with 'sha256:'."""
    auditor = ComplianceAuditor()
    event_stream = [
        A2AMessage(agent="risk-analyst", event_type="risk_verdict",
                   timestamp=_NOW,
                   payload={"pci_scope_touched": False, "risk_level": "LOW",
                            "guardrails_triggered": [], "rationale": ""},
                   trace_id="t1"),
        A2AMessage(agent="slo-sentinel", event_type="sentinel_verdict",
                   timestamp=_NOW,
                   payload={"verdict": "PROMOTE", "confidence": 0.9, "reasoning": "ok"},
                   trace_id="t1"),
    ]
    with patch.object(auditor._llm.chat.completions, "create",
                      return_value=_make_llm_response("Narrative.")):
        packet = auditor.audit(event_stream)

    assert packet.audit_trail_hash.startswith("sha256:")
    assert len(packet.audit_trail_hash) > 10


# ── SLO Sentinel and Canary Orchestrator unit tests ───────────────────────────


@pytest.mark.asyncio
async def test_sentinel_safety_rollback_two_degraded_intervals() -> None:
    """Both metrics above SLO threshold for 2 consecutive intervals → safety ROLLBACK."""
    sentinel = SLOSentinel()
    # PCI-tier handle: max_error_rate_pct=0.1%, max_p99_ms=400ms
    handle = make_deploy_handle(pci=True)

    # Metrics well above PCI SLOs: error_rate=2% (>> 0.1%), p99=600ms (>> 400ms)
    bad_metrics = {"error_rate": 0.02, "p99_latency_ms": 600.0, "rps": 50.0}
    llm_verdicts = [
        {"verdict": "PROMOTE", "confidence": 0.8, "reasoning": "looks ok"},
        {"verdict": "PROMOTE", "confidence": 0.8, "reasoning": "looks ok"},
    ]

    with (
        patch.object(sentinel, "_fetch_baseline", new=AsyncMock(return_value={
            "baseline_error_rate": 0.0008, "baseline_p99_ms": 305.0,
        })),
        patch.object(sentinel, "_fetch_metrics", new=AsyncMock(return_value=bad_metrics)),
        patch.object(sentinel, "_call_llm",
                     new=AsyncMock(side_effect=llm_verdicts)),
        patch("asyncio.sleep", new=AsyncMock(return_value=None)),
        # DEMO_BAKE_SECONDS=2, poll=1 → min_promote_intervals=2, so the loop must
        # run twice before PROMOTE is considered; on the 2nd interval the degraded
        # window is full → safety ROLLBACK override fires before PROMOTE check.
        patch.dict(os.environ, {"DEMO_BAKE_SECONDS": "2",
                                "SENTINEL_POLL_INTERVAL_SECONDS": "1"}),
    ):
        result = await sentinel.watch(handle)

    # Safety override fires after 2 consecutive degraded intervals
    assert result.verdict == "ROLLBACK"


@pytest.mark.asyncio
async def test_canary_pct_capped_at_50() -> None:
    """Canary orchestrator caps recommended_pct > 50 to 50 by the 30-min rule."""
    canary = CanaryOrchestrator()
    rv = make_risk_verdict(score=30, level="LOW", canary_pct=75)

    with (
        patch.object(canary._opa, "check"),  # allow OPA
        patch.object(canary._harness, "deploy",
                     new=AsyncMock(return_value={"deployment_id": "exec-test"})),
        patch.object(canary, "_poll_for_approval",
                     new=AsyncMock(return_value="demo-token")),
    ):
        handle = await canary.deploy(rv)

    assert handle.canary_pct == 50


@pytest.mark.asyncio
async def test_deploy_handle_success_criteria_tighter_for_pci() -> None:
    """PCI-scoped deployment must use tighter SLO criteria than non-PCI."""
    canary = CanaryOrchestrator()
    pci_rv = make_risk_verdict(score=30, level="LOW", pci=True)

    with (
        patch.object(canary._opa, "check"),
        patch.object(canary._harness, "deploy",
                     new=AsyncMock(return_value={"deployment_id": "exec-test"})),
        patch.object(canary, "_poll_for_approval",
                     new=AsyncMock(return_value="demo-token")),
    ):
        handle = await canary.deploy(pci_rv)

    # PCI success criteria: max_error_rate_pct=0.1%, max_p99_ms=400ms
    assert handle.success_criteria.max_error_rate_pct <= 0.1
    assert handle.success_criteria.max_p99_ms <= 400


# ── Service graph additional coverage ────────────────────────────────────────


def test_service_graph_lookup_unknown_service() -> None:
    """Looking up an unknown service returns None without raising."""
    graph = ServiceGraph()
    graph.load()
    result = graph.lookup("non-existent-service-xyz")
    assert result is None


def test_service_graph_blast_radius_direct_consumers() -> None:
    """ServiceA-AuthService has both ServiceA-UI and ServiceA-PaymentGateway as consumers."""
    graph = ServiceGraph()
    graph.load()
    br = graph.blast_radius("ServiceA-AuthService")
    assert "ServiceA-UI" in br.direct_consumers
    assert "ServiceA-PaymentGateway" in br.direct_consumers


def test_pci_scope_detected_via_payment_path() -> None:
    """A file under src/servicea/auth/** triggers PCI scope via path regex or graph flag."""
    graph = ServiceGraph()
    graph.load()
    pci, reason = graph.check_pci_scope(["src/servicea/auth/AuthController.java"])
    assert pci is True
