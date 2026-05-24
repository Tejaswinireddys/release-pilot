"""
Compliance Auditor — PCI-DSS v4.0 + SOX 302/404 attestation.

NO external tool calls. Works exclusively on the A2A event stream passed by
the orchestrator. Emits a frozen AuditPacket with a SHA-256 audit trail hash.

Deterministic rules (Python, NOT the LLM):
  1. Scan event_stream for: risk_verdict, deploy_handle, sentinel_verdict,
     policy_violation events.
  2. sox_evidence_complete: True if risk + deploy + sentinel present,
     OR if a policy_violation event is in the stream (blocked-before-deploy).
  3. pci_controls_engaged:
       pci_scope_touched → "PCI-DSS 6.4.5 Change Control", "PCI-DSS 10.2 Audit Logging"
       iam_change        → "PCI-DSS 7.1 Access Control"
       schema_migration  → "PCI-DSS 6.4.6 Significant Change"
  4. auditor_verdict:
       blocked_by_opa                              → RELEASE_BLOCKED
       pci_scope_touched AND approvals empty       → ESCALATE_TO_COMPLIANCE
       sox_evidence_complete AND sentinel terminal → RELEASE_APPROVED
       otherwise                                   → ESCALATE_TO_COMPLIANCE
  5. compliance_narrative: 3 LLM sentences; deterministic fallback if LLM fails.
  6. audit_trail_hash: "sha256:" + SHA-256 of json.dumps(canonical, sort_keys=True)
     where canonical excludes audit_trail_hash itself.
  7. AuditEvent: frozen dataclass, append-only via append_event(). Optionally
     persisted to AUDIT_LOG_PATH (newline-delimited JSON).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from openai import OpenAI
from pydantic import BaseModel, ConfigDict

from src.opa_client import OPAClient, PolicyViolationError
from src.redactor import PCIRedactor
from src.telemetry import get_tracer, record_llm_call

log = logging.getLogger(__name__)


# ── A2A message envelope ──────────────────────────────────────────────────────


class A2AMessage(BaseModel):
    agent: str        # "risk-analyst" | "canary-orchestrator" | "slo-sentinel" | "opa"
    event_type: str   # "risk_verdict" | "deploy_handle" | "sentinel_verdict" | "policy_violation"
    timestamp: str    # ISO 8601
    payload: dict[str, Any]
    trace_id: str = ""


# ── Approval record ───────────────────────────────────────────────────────────


class Approval(BaseModel):
    by: str   # approver identity (email or token ID)
    at: str   # ISO 8601
    via: str  # "DEMO_APPROVAL_TOKEN" | "human_review_portal" | "change_record"


# ── Append-only audit event ───────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class AuditEvent:
    event_id: str
    packet_id: str
    agent: str
    event_type: str
    timestamp: str
    payload: dict[str, Any] = dataclasses.field(default_factory=dict)


# Module-level append-only log — never cleared, only grows
_EVENT_LOG: list[AuditEvent] = []


# ── Primary output model ──────────────────────────────────────────────────────


class AuditPacket(BaseModel):
    model_config = ConfigDict(frozen=True)

    release_id: str
    service_id: str = ""    # metadata — not part of audit hash
    pr_number: int = 0      # metadata — not part of audit hash
    pci_scope_touched: bool
    pci_controls_engaged: list[str]
    approvals_captured: list[Approval]
    sox_evidence_complete: bool
    auditor_verdict: Literal[
        "RELEASE_APPROVED", "RELEASE_BLOCKED", "ESCALATE_TO_COMPLIANCE"
    ]
    blocking_reasons: list[str]
    compliance_narrative: str  # the ONLY field the LLM writes
    audit_trail_hash: str      # "sha256:<hex>" of canonical JSON


# ── Backward-compat models (orchestrator imports these) ───────────────────────


class OPADecision(BaseModel):
    allowed: bool
    violations: list[str]
    policy_path: str
    evaluated_at: str
    trace_id: str


class ComplianceAttestation(BaseModel):
    control_id: str
    framework: Literal["PCI-DSS", "SOX"]
    control_description: str
    status: Literal["PASS", "FAIL", "N/A"]
    evidence: str


# ── System prompt ─────────────────────────────────────────────────────────────


_NARRATIVE_SYSTEM = """You are a PCI-DSS v4.0 and SOX compliance officer writing
the narrative section of a deployment audit record.

Write exactly 3 sentences in plain English suitable for an external auditor:
  1. What was deployed (service name, risk classification, PCI scope status).
  2. Which compliance controls were engaged (cite specific control IDs) and what
     approvals were captured (who, by what mechanism).
  3. The final compliance verdict and any blocking reasons, if applicable.

Rules:
- No markdown, no headers, no bullet points — prose only.
- Use hedge language ("requires validation") only for fields explicitly marked unknown.
- Quote control IDs verbatim (e.g. PCI-DSS 6.4.5, SOX 302).
- Three sentences. No more, no less."""


# ── Agent class ───────────────────────────────────────────────────────────────


class ComplianceAuditor:
    """Produces a frozen, SHA-256-hashed AuditPacket from the A2A event stream.

    No external tool calls. All deterministic rules run in Python.
    The LLM writes only the compliance_narrative field (3 sentences).
    """

    def __init__(self) -> None:
        self._llm = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY", "demo"),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )
        self._model = os.getenv("AGENT_MODEL", "gpt-4o")
        self._redactor = PCIRedactor()
        self._opa = OPAClient()
        self._tracer = get_tracer("compliance-auditor")

    # ── Primary interface ─────────────────────────────────────────────────────

    def audit(self, event_stream: list[A2AMessage]) -> AuditPacket:
        """Deterministic compliance audit from the A2A event stream.

        Implements all 7 spec rules and returns an immutable AuditPacket.
        """
        with self._tracer.start_as_current_span("compliance_auditor.audit") as span:
            span.set_attribute("gen_ai.agent.name", "compliance-auditor")
            span.set_attribute("gen_ai.system", "openai")
            span.set_attribute("gen_ai.operation.name", "agent_invoke")
            span.set_attribute("event_stream.length", len(event_stream))

            # ── Rule 1: Scan event stream ─────────────────────────────────────
            risk_event = next(
                (e for e in event_stream if e.event_type == "risk_verdict"), None
            )
            deploy_event = next(
                (e for e in event_stream if e.event_type == "deploy_handle"), None
            )
            sentinel_event = next(
                (e for e in event_stream if e.event_type == "sentinel_verdict"), None
            )
            violation_event = next(
                (e for e in event_stream if e.event_type == "policy_violation"), None
            )

            # Key scalar fields from risk_verdict payload
            rv: dict[str, Any] = risk_event.payload if risk_event else {}
            pci_scope_touched = bool(rv.get("pci_scope_touched", False))
            risk_level: str = rv.get("risk_level", "MEDIUM")
            guardrails: list[str] = rv.get("guardrails_triggered", [])
            rationale: str = rv.get("rationale", "")
            # blast_radius.service (new format) or service_id (old format)
            service: str = (
                rv.get("service_id")
                or (rv.get("blast_radius") or {}).get("service", "unknown")
            )
            pr_num: int = rv.get("pr_number", 0)

            span.set_attribute("pci_scope_touched", pci_scope_touched)
            span.set_attribute("risk_level", risk_level)
            span.set_attribute("service", service)

            # ── Rule 2: sox_evidence_complete ─────────────────────────────────
            sox_evidence_complete: bool = (
                risk_event is not None
                and deploy_event is not None
                and sentinel_event is not None
            ) or (
                violation_event is not None  # blocked-before-deploy still counts
            )

            # ── Rule 3: pci_controls_engaged ──────────────────────────────────
            pci_controls = self._compute_pci_controls(
                pci_scope_touched=pci_scope_touched,
                guardrails=guardrails,
                rationale=rationale,
                deploy_payload=deploy_event.payload if deploy_event else {},
            )

            # Approvals captured from deploy token + change record
            approvals_captured = self._extract_approvals(deploy_event, risk_event)

            # Sentinel verdict string for rule 4
            sentinel_verdict_str: str | None = None
            if sentinel_event:
                sv = sentinel_event.payload
                # SentinelVerdict uses "verdict"; SLOVerdict uses "decision"
                sentinel_verdict_str = sv.get("verdict") or sv.get("decision")

            # ── Rule 4: auditor_verdict + blocking_reasons ────────────────────
            verdict, blocking_reasons = self._compute_verdict(
                violation_event=violation_event,
                pci_scope_touched=pci_scope_touched,
                approvals_captured=approvals_captured,
                sox_evidence_complete=sox_evidence_complete,
                sentinel_verdict_str=sentinel_verdict_str,
            )

            # Release identifier
            ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            release_id = f"REL-{service}-PR{pr_num}-{ts}"

            # ── Rule 5: compliance_narrative (LLM with deterministic fallback) ─
            narrative = self._generate_narrative(
                release_id=release_id,
                service=service,
                risk_level=risk_level,
                pci_scope_touched=pci_scope_touched,
                pci_controls=pci_controls,
                approvals_captured=approvals_captured,
                verdict=verdict,
                blocking_reasons=blocking_reasons,
            )

            span.set_attribute("auditor_verdict", verdict)

            # ── Rule 6: audit_trail_hash ──────────────────────────────────────
            # Canonical dict excludes audit_trail_hash itself and metadata fields
            canonical_dict: dict[str, Any] = {
                "release_id": release_id,
                "pci_scope_touched": pci_scope_touched,
                "pci_controls_engaged": pci_controls,
                "approvals_captured": [a.model_dump() for a in approvals_captured],
                "sox_evidence_complete": sox_evidence_complete,
                "auditor_verdict": verdict,
                "blocking_reasons": blocking_reasons,
                "compliance_narrative": narrative,
            }
            canonical = json.dumps(canonical_dict, sort_keys=True)
            audit_trail_hash = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()

            packet = AuditPacket(
                release_id=release_id,
                service_id=service,
                pr_number=pr_num,
                pci_scope_touched=pci_scope_touched,
                pci_controls_engaged=pci_controls,
                approvals_captured=approvals_captured,
                sox_evidence_complete=sox_evidence_complete,
                auditor_verdict=verdict,
                blocking_reasons=blocking_reasons,
                compliance_narrative=narrative,
                audit_trail_hash=audit_trail_hash,
            )

            log.info(
                "AUDIT_PACKET release_id=%s verdict=%s pci=%s sox=%s hash=%s",
                release_id, verdict, pci_scope_touched,
                sox_evidence_complete, audit_trail_hash[:24],
            )
            return packet

    # ── Rule 7: append-only audit event log ──────────────────────────────────

    def append_event(self, packet_id: str, event: AuditEvent) -> None:
        """Write event to the module-level append-only log (never cleared).

        Optionally persists to AUDIT_LOG_PATH as newline-delimited JSON.
        """
        _EVENT_LOG.append(event)

        log_path = os.getenv("AUDIT_LOG_PATH")
        if log_path:
            try:
                with open(log_path, "a") as fh:
                    fh.write(json.dumps({
                        "event_id": event.event_id,
                        "packet_id": event.packet_id,
                        "agent": event.agent,
                        "event_type": event.event_type,
                        "timestamp": event.timestamp,
                        "payload": event.payload,
                    }) + "\n")
            except OSError as exc:
                log.warning("audit_log.write_failed path=%s error=%s", log_path, exc)

        log.info(
            "audit_event.appended packet_id=%s event_id=%s type=%s",
            packet_id, event.event_id, event.event_type,
        )

    # ── Deterministic rule helpers ────────────────────────────────────────────

    def _extract_approvals(
        self,
        deploy_event: A2AMessage | None,
        risk_event: A2AMessage | None,
    ) -> list[Approval]:
        approvals: list[Approval] = []
        now = datetime.now(timezone.utc).isoformat()

        # Human approval token from deploy handle
        if deploy_event:
            dh = deploy_event.payload
            token = dh.get("human_approval_token")
            if token:
                approvals.append(Approval(
                    by=str(token),
                    at=dh.get("start_time", now),
                    via="human_review_portal",
                ))

        # Change-record approvers forwarded via risk verdict payload
        if risk_event:
            cr = risk_event.payload.get("change_record") or {}
            for approver in cr.get("approved_by", []):
                if not any(a.by == approver for a in approvals):
                    approvals.append(Approval(by=str(approver), at=now, via="change_record"))

        return approvals

    def _compute_pci_controls(
        self,
        pci_scope_touched: bool,
        guardrails: list[str],
        rationale: str,
        deploy_payload: dict[str, Any],
    ) -> list[str]:
        """Rule 3: derive PCI-DSS control IDs from deterministic signals."""
        controls: list[str] = []

        if pci_scope_touched:
            controls.append("PCI-DSS 6.4.5 Change Control")
            controls.append("PCI-DSS 10.2 Audit Logging")

        # IAM signal: from guardrails text, rationale, or canary OPA context
        guardrails_upper = " ".join(guardrails).upper()
        rationale_lower = rationale.lower()
        terraform = deploy_payload.get("terraform_diff") or {}
        iam_change = (
            any(kw in guardrails_upper for kw in ("IAM", "ROLE", "POLICY", "ACCESS"))
            or any(kw in rationale_lower for kw in ("iam", "role policy", "access control", "assume role"))
            or bool(terraform.get("touches_iam"))
        )
        if iam_change:
            controls.append("PCI-DSS 7.1 Access Control")

        # Schema migration signal: from guardrails or rationale
        schema_migration = (
            "SCHEMA_MIGRATION" in guardrails_upper
            or any(kw in rationale_lower for kw in ("schema", "migration", "alter table", "database"))
        )
        if schema_migration:
            controls.append("PCI-DSS 6.4.6 Significant Change")

        return sorted(set(controls))

    def _compute_verdict(
        self,
        violation_event: A2AMessage | None,
        pci_scope_touched: bool,
        approvals_captured: list[Approval],
        sox_evidence_complete: bool,
        sentinel_verdict_str: str | None,
    ) -> tuple[str, list[str]]:
        """Rule 4: deterministic three-branch verdict."""
        blocking_reasons: list[str] = []

        # Branch 1: OPA blocked deployment → RELEASE_BLOCKED
        if violation_event:
            denial_reasons: list[str] = violation_event.payload.get("denial_reasons", [])
            blocking_reasons.extend(denial_reasons)
            return "RELEASE_BLOCKED", blocking_reasons

        # Branch 2: PCI scoped but no human approval → ESCALATE_TO_COMPLIANCE
        if pci_scope_touched and not approvals_captured:
            blocking_reasons.append(
                "PCI-scoped deployment missing required human approval"
            )
            return "ESCALATE_TO_COMPLIANCE", blocking_reasons

        # Branch 3: complete evidence + terminal sentinel verdict → RELEASE_APPROVED
        if sox_evidence_complete and sentinel_verdict_str in ("PROMOTE", "ROLLBACK"):
            return "RELEASE_APPROVED", []

        # Fallback: incomplete evidence
        if not sox_evidence_complete:
            blocking_reasons.append(
                "Incomplete SOX evidence: one or more required pipeline events missing"
            )
        elif sentinel_verdict_str not in ("PROMOTE", "ROLLBACK"):
            blocking_reasons.append(
                f"Non-terminal sentinel verdict '{sentinel_verdict_str}' — "
                "manual review required"
            )
        return "ESCALATE_TO_COMPLIANCE", blocking_reasons

    def _generate_narrative(
        self,
        release_id: str,
        service: str,
        risk_level: str,
        pci_scope_touched: bool,
        pci_controls: list[str],
        approvals_captured: list[Approval],
        verdict: str,
        blocking_reasons: list[str],
    ) -> str:
        """Rule 5: 3-sentence LLM narrative with deterministic fallback."""
        approval_summary = (
            ", ".join(f"{a.by} via {a.via}" for a in approvals_captured)
            or "no approvals captured"
        )
        controls_summary = ", ".join(pci_controls) or "standard change controls"
        pci_note = (
            f"PCI-DSS scoped; controls engaged: {controls_summary}"
            if pci_scope_touched
            else "not PCI-DSS scoped"
        )
        blocking_note = (
            f" Blocking reasons: {'; '.join(blocking_reasons)}."
            if blocking_reasons
            else ""
        )

        user_msg = self._redactor.redact(
            f"Release ID: {release_id}\n"
            f"Service: {service} ({pci_note})\n"
            f"Risk classification: {risk_level}\n"
            f"Approvals: {approval_summary}\n"
            f"Controls engaged: {controls_summary}\n"
            f"Compliance verdict: {verdict}\n"
            f"{blocking_note}\n\n"
            "Write exactly 3 plain English sentences for a compliance audit record."
        ).redacted_text

        try:
            resp = self._llm.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _NARRATIVE_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.2,
                max_tokens=300,
                timeout=30,
            )
            raw = (resp.choices[0].message.content or "").strip()
            if raw:
                return self._redactor.redact(raw).redacted_text
        except Exception as exc:
            log.warning("narrative.llm_failed error=%s — using template fallback", exc)

        # Deterministic fallback — never fails
        return (
            f"Release {release_id} deployed service '{service}' with {risk_level} risk "
            f"classification ({pci_note}). "
            f"Approvals captured from: {approval_summary}; controls engaged: {controls_summary}. "
            f"Final compliance verdict: {verdict}.{blocking_note}"
        )

    # ── Backward-compat methods (orchestrator still calls these) ─────────────

    async def pre_check(self, verdict: Any, trace_id: str) -> OPADecision:
        """Pre-deployment compliance gate. Returns OPADecision."""
        with self._tracer.start_as_current_span("compliance_auditor.pre_check") as span:
            span.set_attribute("gen_ai.agent.name", "compliance-auditor")
            span.set_attribute("trace_id", trace_id)

            violations: list[str] = []
            risk_level = getattr(verdict, "risk_level", "MEDIUM")
            pci = getattr(verdict, "pci_scope_touched", False)

            # PCI HIGH/CRITICAL with no explicit PCI approval signals → block
            if risk_level in ("CRITICAL", "HIGH") and pci:
                pci_signals = getattr(verdict, "pci_signals", []) or []
                if not pci_signals:
                    violations.append(
                        f"{risk_level} risk on PCI-scoped service requires explicit approval."
                    )

            # Explicit BLOCK recommendation from risk analyst
            recommendation = getattr(verdict, "recommendation", None)
            if recommendation == "BLOCK":
                violations.append(
                    "Risk Analyst recommendation is BLOCK — deployment must not proceed."
                )

            span.set_attribute("pre_check.violations", len(violations))
            log.info(
                "pre_check trace_id=%s allowed=%s violations=%d",
                trace_id, len(violations) == 0, len(violations),
            )
            return OPADecision(
                allowed=len(violations) == 0,
                violations=violations,
                policy_path="release_pilot.guardrails",
                evaluated_at=datetime.now(timezone.utc).isoformat(),
                trace_id=trace_id,
            )

    async def attest(
        self,
        risk_verdict: Any,
        canary_result: Any,
        slo_verdict: Any,
        sox_scope: bool,
        trace_id: str,
    ) -> AuditPacket:
        """Backward-compat attestation wrapper: builds A2A event stream → audit()."""
        now = datetime.now(timezone.utc).isoformat()

        rv_payload: dict[str, Any] = (
            risk_verdict.model_dump() if hasattr(risk_verdict, "model_dump") else {}
        )
        dh_payload: dict[str, Any] = (
            canary_result.model_dump() if hasattr(canary_result, "model_dump") else {}
        )
        sv_payload: dict[str, Any] = (
            slo_verdict.model_dump() if hasattr(slo_verdict, "model_dump") else {}
        )

        # Normalise: SLOVerdict uses "decision", SentinelVerdict uses "verdict"
        if "decision" in sv_payload and "verdict" not in sv_payload:
            sv_payload["verdict"] = sv_payload["decision"]

        event_stream = [
            A2AMessage(
                agent="risk-analyst", event_type="risk_verdict",
                timestamp=now, payload=rv_payload, trace_id=trace_id,
            ),
            A2AMessage(
                agent="canary-orchestrator", event_type="deploy_handle",
                timestamp=now, payload=dh_payload, trace_id=trace_id,
            ),
            A2AMessage(
                agent="slo-sentinel", event_type="sentinel_verdict",
                timestamp=now, payload=sv_payload, trace_id=trace_id,
            ),
        ]
        return self.audit(event_stream)
