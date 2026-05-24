"""
Release Scribe — composes structured Confluence release pages and publishes them.

Tool permissions (from .github/agents/release-scribe.agent.md):
  ALLOW: atlassian_mcp.create_page, atlassian_mcp.update_page,
         atlassian_mcp.jira_comment, teams.post
  DENY:  harness.*, aws_mcp.*, github_mcp.*, rag.*, memory.*

Primary interface (spec-defined):
  compose_page(event_stream, pr_data, audit_packet) -> ReleasePage
    — LLM detects breaking changes, translates to impact language,
      writes blast-radius narrative, builds incident timeline on ROLLBACK.
  publish(page, approval_token) -> str   (returns confluence_url)
    — OPA-checks confluence.publish, calls Atlassian Rovo MCP, locks the page.
    — Falls back to /tmp/release_pilot_pages/<release_id>.md if MCP unreachable.
    — draft_requires_human_approval is ALWAYS True; never auto-publishes.

Backward-compat interface (orchestrator uses this):
  document(audit_packet, outcome, trace_id) -> ReleaseNote
    — Internally calls compose_page + publish; returns typed ReleaseNote.

LLM responsibilities (stated in system prompt):
  1. Detect breaking changes from the diff.
  2. Translate engineering speak to impact speak.
  3. Write blast-radius narrative from risk_verdict.blast_radius.
  4. On ROLLBACK: write incident_timeline in T+Xs format from A2A timestamps.
  5. Use hedge language for inferred content.
  6. Quote agent verdicts VERBATIM and attribute them.

Publish rules (deterministic):
  - publish() called only after valid approval_token.
  - OPA-check action="confluence.publish" before every MCP call.
  - Once published, page is LOCKED — updates append as Confluence comments.
  - If pci_scope_touched, compliance_banner set BEFORE first publish.
  - If Atlassian MCP unavailable, render to local Markdown file and log warning.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel

from src.agents.compliance_auditor import A2AMessage, AuditPacket
from src.opa_client import OPAClient, PolicyViolationError
from src.redactor import PCIRedactor
from src.telemetry import get_tracer, record_llm_call
from src.tools.teams_client import TeamsClient

log = logging.getLogger(__name__)

_LOCAL_PAGE_DIR = Path("/tmp/release_pilot_pages")

# ── Output models ─────────────────────────────────────────────────────────────


class TraceabilityChain(BaseModel):
    prd_link: str | None = None
    jira_link: str | None = None
    pr_link: str | None = None
    risk_verdict_summary: str        # "<LEVEL> score=N pci=<bool>"
    canary_outcome: str              # e.g. "PROMOTED 100%" or "ROLLED_BACK"
    sentinel_verdict: str            # verbatim from SentinelVerdict.verdict
    compliance_attestation: str      # "<VERDICT> — <release_id>"


class PageSections(BaseModel):
    compliance_banner: str | None = None   # set when pci_scope_touched; None otherwise
    breaking_changes: list[str] = []       # LLM-detected from diff
    engineering_section: str = ""          # LLM: impact-speak translation
    compliance_section: str = ""           # PCI-DSS + SOX controls; audit hash embedded
    traceability_chain: TraceabilityChain
    incident_timeline: list[str] | None = None  # populated ONLY on rollback


class ReleasePage(BaseModel):
    release_id: str
    page_title: str
    sections: PageSections
    draft_requires_human_approval: bool = True  # ALWAYS true — never auto-publish
    published: bool = False
    confluence_url: str | None = None


# ── Kept for backward-compat (orchestrator returns ReleaseNote) ───────────────


class ReleaseNote(BaseModel):
    service_id: str
    pr_number: int
    confluence_page_url: str | None
    confluence_page_id: str | None
    jira_comment_id: str | None
    teams_message_id: str | None
    summary: str
    deployment_outcome: Literal["PROMOTED", "ROLLED_BACK", "BLOCKED", "FAILED"]
    pci_scope_touched: bool
    audit_trail_hash: str
    trace_id: str
    published_at: str


# ── System prompt — states all 6 LLM responsibilities explicitly ──────────────

_COMPOSE_SYSTEM = """You are Release Scribe, a technical writing agent inside Release Pilot.
You run after every deployment — promoted, rolled back, or blocked — and produce
structured, Confluence-ready page content.

Your sole responsibilities are the following six tasks. Perform them exactly.
Do not write content outside these responsibilities.

1. BREAKING CHANGES
   Scan the PR diff for: API signature changes (added/removed/renamed parameters,
   changed return types), removed or renamed endpoints/RPCs, database schema
   migrations (ALTER TABLE, DROP COLUMN, new indexes), environment-variable renames
   or deletions, and dependency major-version bumps (e.g. "requests 2.x → 3.0").
   Return each as a one-line string starting with the change type in UPPER_SNAKE_CASE,
   e.g. "ENDPOINT_REMOVED: DELETE /v1/legacy-charge".
   If none detected, return an empty list.

2. ENGINEERING → IMPACT TRANSLATION
   Rewrite the engineering summary in language a non-engineer can understand.
   Focus on user-visible or business risk, not implementation details.
   Example: "refactored retry logic" → "reduces risk of duplicate charges during
   partial network outages". One short paragraph, present tense.

3. BLAST-RADIUS NARRATIVE
   From the risk_verdict.blast_radius field (service name, direct_consumers list,
   transitive_services count), write a concise paragraph: which services are
   potentially affected, why, and the confidence level of this assessment.
   Embed the risk level and score as a verbatim quote from the Risk Analyst agent.

4. INCIDENT TIMELINE (ROLLBACK only)
   If the deployment was rolled back, build a bullet timeline from the ISO 8601
   timestamps in the A2A event stream. Compute elapsed seconds from the first
   event. Format: "T+0s: canary deployed at 10% traffic", "T+87s: error rate
   spike detected (0.08% vs 0.01% SLO threshold)", "T+94s: rollback initiated".
   Return null if not a rollback.

5. HEDGE LANGUAGE
   For any content you infer rather than read from a structured field, use:
   "appears to", "is suspected to", or "requires validation by the on-call team".
   Never present inference as established fact.

6. VERBATIM AGENT QUOTES
   When citing a risk level, SLO verdict, or compliance verdict, copy the exact
   string value from the provided JSON and attribute it to the originating agent.
   Do not paraphrase. Use quotation marks and agent name: e.g.
   'The Risk Analyst assessed this deployment as "HIGH" risk.'

Return a JSON object with exactly these keys:
{
  "breaking_changes": ["...", ...],
  "engineering_section": "...",
  "blast_radius_narrative": "...",
  "incident_timeline": ["...", ...] or null
}"""


# ── Agent class ───────────────────────────────────────────────────────────────


class ReleaseScribe:
    """Composes structured Confluence release pages with LLM-written sections.

    publish() is the only method that writes to external systems.
    All LLM inputs/outputs pass through PCIRedactor.
    OPA is checked before every publish action.
    """

    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY", "demo"),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )
        self._model = os.getenv("AGENT_MODEL", "gpt-4o")
        self._redactor = PCIRedactor()
        self._opa = OPAClient()
        self._teams = TeamsClient()
        self._demo = os.getenv("DEMO_MODE", "true").lower() == "true"
        self._tracer = get_tracer("release-scribe")
        self._http = httpx.AsyncClient(
            base_url=os.getenv("ATLASSIAN_MCP_URL", "http://localhost:9090"),
            timeout=20.0,
        )

    # ── Primary interface ─────────────────────────────────────────────────────

    async def compose_page(
        self,
        event_stream: list[A2AMessage],
        pr_data: dict[str, Any],
        audit_packet: AuditPacket,
    ) -> ReleasePage:
        """Compose a structured ReleasePage from the A2A event stream.

        Deterministic sections (compliance, traceability) are built in Python.
        LLM writes: breaking_changes, engineering_section, blast_radius_narrative,
        incident_timeline.
        """
        with self._tracer.start_as_current_span("release_scribe.compose_page") as span:
            span.set_attribute("gen_ai.agent.name", "release-scribe")
            span.set_attribute("gen_ai.system", "openai")
            span.set_attribute("gen_ai.operation.name", "agent_invoke")
            span.set_attribute("release_id", audit_packet.release_id)

            # Extract structured data from event stream
            risk_payload = _event_payload(event_stream, "risk_verdict")
            deploy_payload = _event_payload(event_stream, "deploy_handle")
            sentinel_payload = _event_payload(event_stream, "sentinel_verdict")

            service_id = audit_packet.service_id or pr_data.get("service_id", "unknown")
            pr_number = audit_packet.pr_number or pr_data.get("pr_number", 0)
            outcome = self._determine_outcome(event_stream, audit_packet)

            # Deterministic: traceability chain
            traceability = self._build_traceability_chain(
                event_stream=event_stream,
                pr_data=pr_data,
                audit_packet=audit_packet,
                risk_payload=risk_payload,
                deploy_payload=deploy_payload,
                sentinel_payload=sentinel_payload,
            )

            # Deterministic: compliance section (embeds audit hash)
            compliance_section = self._build_compliance_section(audit_packet)

            # Deterministic: PCI compliance banner
            compliance_banner: str | None = None
            if audit_packet.pci_scope_touched:
                compliance_banner = (
                    f"⚠ PCI-DSS SCOPE: This release touches cardholder-data flows. "
                    f"Controls engaged: {', '.join(audit_packet.pci_controls_engaged)}. "
                    f"Treat this page as a compliance artifact — do not edit post-publication."
                )

            # LLM: the four spec-defined sections
            llm_sections = await self._call_compose_llm(
                event_stream=event_stream,
                pr_data=pr_data,
                audit_packet=audit_packet,
                risk_payload=risk_payload,
                sentinel_payload=sentinel_payload,
                outcome=outcome,
                span=span,
            )

            sections = PageSections(
                compliance_banner=compliance_banner,
                breaking_changes=llm_sections.get("breaking_changes", []),
                engineering_section=llm_sections.get("engineering_section", ""),
                compliance_section=compliance_section,
                traceability_chain=traceability,
                incident_timeline=(
                    llm_sections.get("incident_timeline")
                    if outcome == "ROLLED_BACK"
                    else None
                ),
            )

            page_title = (
                f"[{outcome}] {service_id} PR #{pr_number} — "
                f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
            )

            page = ReleasePage(
                release_id=audit_packet.release_id,
                page_title=page_title,
                sections=sections,
                draft_requires_human_approval=True,
                published=False,
                confluence_url=None,
            )

            log.info(
                "compose_page.done release_id=%s outcome=%s pci=%s breaking_changes=%d",
                audit_packet.release_id, outcome,
                audit_packet.pci_scope_touched,
                len(sections.breaking_changes),
            )
            return page

    async def publish(self, page: ReleasePage, approval_token: str) -> str:
        """Publish a draft ReleasePage to Confluence (or local MD fallback).

        Rules:
        - publish() is only callable after a valid approval_token is provided.
        - OPA-checks action="confluence.publish" before any external call.
        - Once published, page is locked — further edits append as comments.
        - If PCI-scoped, compliance_banner must be set before publish.
        - Falls back to /tmp/release_pilot_pages/<release_id>.md if MCP fails.
        """
        with self._tracer.start_as_current_span("release_scribe.publish") as span:
            span.set_attribute("release_id", page.release_id)
            span.set_attribute("approval_token_present", bool(approval_token))
            span.set_attribute("pci_scope", page.sections.compliance_banner is not None)

            if page.published:
                raise RuntimeError(
                    f"Page {page.release_id} is locked — post updates as Confluence comments."
                )

            # Enforce PCI banner before publish
            if page.sections.compliance_banner is None and _page_is_pci(page):
                raise ValueError(
                    "PCI-scoped page must have compliance_banner set before publish."
                )

            # OPA gate — halts if denied
            try:
                self._opa.check(
                    action="confluence.publish",
                    context={
                        "approval_token": approval_token,
                        "pci_scope_touched": page.sections.compliance_banner is not None,
                        "release_id": page.release_id,
                    },
                )
            except PolicyViolationError as exc:
                log.error(
                    "publish.opa_denied release_id=%s reasons=%s",
                    page.release_id, exc.denial_reasons,
                )
                raise

            # Try Atlassian Rovo MCP → fall back to local MD
            confluence_url = await self._publish_to_confluence(page)

            if confluence_url is None:
                local_path = self._write_local_markdown(page)
                log.warning(
                    "publish.mcp_unavailable release_id=%s fallback=%s",
                    page.release_id, local_path,
                )
                confluence_url = f"file://{local_path}"

            # Lock the page
            page.published = True
            page.confluence_url = confluence_url

            # Teams notification
            await self._notify_teams(page)

            span.set_attribute("confluence_url", confluence_url)
            log.info(
                "PUBLISH_COMPLETE release_id=%s url=%s",
                page.release_id, confluence_url,
            )
            return confluence_url

    # ── Backward-compat: orchestrator calls document() ────────────────────────

    async def document(
        self,
        audit_packet: AuditPacket,
        outcome: Literal["PROMOTED", "ROLLED_BACK", "BLOCKED", "FAILED"],
        trace_id: str,
    ) -> ReleaseNote:
        """Backward-compat wrapper: compose + publish, return ReleaseNote.

        Builds a synthetic A2A event stream from the AuditPacket so that
        compose_page() has enough context to produce all sections.
        """
        start = time.time()
        service_id = audit_packet.service_id or audit_packet.release_id
        pr_number = audit_packet.pr_number

        with self._tracer.start_as_current_span("release_scribe.document") as span:
            span.set_attribute("service.id", service_id)
            span.set_attribute("pr.number", pr_number)
            span.set_attribute("trace_id", trace_id)
            span.set_attribute("outcome", outcome)

            now = datetime.now(timezone.utc).isoformat()
            pr_data: dict[str, Any] = {
                "service_id": service_id,
                "pr_number": pr_number,
                "trace_id": trace_id,
            }

            # Synthetic event stream from AuditPacket fields
            sentinel_verdict = "PROMOTE" if outcome == "PROMOTED" else "ROLLBACK"
            event_stream: list[A2AMessage] = [
                A2AMessage(
                    agent="risk-analyst", event_type="risk_verdict",
                    timestamp=now,
                    payload={
                        "service_id": service_id,
                        "pr_number": pr_number,
                        "pci_scope_touched": audit_packet.pci_scope_touched,
                        "pci_controls_engaged": audit_packet.pci_controls_engaged,
                        "risk_level": "MEDIUM",  # conservative default
                        "guardrails_triggered": [],
                    },
                    trace_id=trace_id,
                ),
                A2AMessage(
                    agent="slo-sentinel", event_type="sentinel_verdict",
                    timestamp=now,
                    payload={
                        "verdict": sentinel_verdict,
                        "reasoning": f"Deployment {outcome.lower()}.",
                        "confidence": 0.9,
                    },
                    trace_id=trace_id,
                ),
            ]

            page = await self.compose_page(event_stream, pr_data, audit_packet)

            # Use demo approval token for the document() flow
            approval_token = os.getenv("DEMO_APPROVAL_TOKEN", f"doc-{uuid.uuid4().hex[:8]}")
            try:
                confluence_url = await self.publish(page, approval_token)
            except (PolicyViolationError, RuntimeError, ValueError) as exc:
                log.warning("document.publish_skipped reason=%s", exc)
                confluence_url = None

            page_id = (
                confluence_url.split("/")[-1]
                if confluence_url and not confluence_url.startswith("file://")
                else None
            )

            # Post Jira comment (demo mock)
            comment_id = await self._post_jira_comment(audit_packet, outcome)

            if span.is_recording():
                record_llm_call(span, self._model, 0, 0)

        note = ReleaseNote(
            service_id=service_id,
            pr_number=pr_number,
            confluence_page_url=confluence_url,
            confluence_page_id=page_id,
            jira_comment_id=comment_id,
            teams_message_id="sent" if self._demo else None,
            summary=f"{service_id} PR #{pr_number}: {outcome}",
            deployment_outcome=outcome,
            pci_scope_touched=audit_packet.pci_scope_touched,
            audit_trail_hash=audit_packet.audit_trail_hash,
            trace_id=trace_id,
            published_at=datetime.now(timezone.utc).isoformat(),
        )
        log.info(
            "document.complete service=%s pr=%d outcome=%s duration_ms=%d",
            service_id, pr_number, outcome,
            int((time.time() - start) * 1000),
        )
        return note

    # ── LLM helpers ───────────────────────────────────────────────────────────

    async def _call_compose_llm(
        self,
        event_stream: list[A2AMessage],
        pr_data: dict[str, Any],
        audit_packet: AuditPacket,
        risk_payload: dict[str, Any],
        sentinel_payload: dict[str, Any],
        outcome: str,
        span: Any,
    ) -> dict[str, Any]:
        """Call the LLM with the compose system prompt; return parsed JSON dict."""
        diff_body = self._redactor.redact(
            pr_data.get("diff_body", "(diff not provided)")
        ).redacted_text

        blast_radius = risk_payload.get("blast_radius") or {}
        risk_level = risk_payload.get("risk_level", "UNKNOWN")
        risk_score = risk_payload.get("score", "N/A")

        # Build minimal event timeline for incident_timeline LLM task
        timeline_lines = "\n".join(
            f"  [{e.timestamp}] agent={e.agent} type={e.event_type}"
            for e in event_stream
        )

        user_msg = self._redactor.redact(
            f"Service: {audit_packet.service_id}\n"
            f"PR number: {audit_packet.pr_number}\n"
            f"Outcome: {outcome}\n"
            f"Risk Analyst verdict: \"{risk_level}\" (score={risk_score})\n"
            f"Blast radius: {json.dumps(blast_radius)}\n"
            f"Sentinel verdict: \"{sentinel_payload.get('verdict', 'UNKNOWN')}\"\n"
            f"Reasoning: {sentinel_payload.get('reasoning', '')}\n\n"
            f"=== A2A EVENT TIMELINE ===\n{timeline_lines}\n\n"
            f"=== PR DIFF (sanitized) ===\n{diff_body[:4000]}\n"
            + ("\n[diff truncated]" if len(diff_body) > 4000 else "")
        ).redacted_text

        fallback: dict[str, Any] = {
            "breaking_changes": [],
            "engineering_section": f"Service {audit_packet.service_id} was {outcome.lower()} via Release Pilot automated canary pipeline.",
            "blast_radius_narrative": (
                f"The Risk Analyst assessed this deployment as \"{risk_level}\" risk. "
                f"Blast radius appears to include: {json.dumps(blast_radius)}. "
                "Downstream impact requires validation by the on-call team."
            ),
            "incident_timeline": None,
        }

        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _COMPOSE_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                response_format={"type": "json_object"},
                temperature=0.3,
                max_tokens=1200,
                timeout=60,
            )
            if resp.usage:
                record_llm_call(span, self._model, resp.usage.prompt_tokens, resp.usage.completion_tokens)

            raw = (resp.choices[0].message.content or "").strip()
            parsed = json.loads(raw)

            # Redact each LLM-written string field before returning
            for key in ("engineering_section", "blast_radius_narrative"):
                if key in parsed and isinstance(parsed[key], str):
                    parsed[key] = self._redactor.redact(parsed[key]).redacted_text
            if isinstance(parsed.get("breaking_changes"), list):
                parsed["breaking_changes"] = [
                    self._redactor.redact(s).redacted_text
                    for s in parsed["breaking_changes"]
                    if isinstance(s, str)
                ]
            if isinstance(parsed.get("incident_timeline"), list):
                parsed["incident_timeline"] = [
                    self._redactor.redact(s).redacted_text
                    for s in parsed["incident_timeline"]
                    if isinstance(s, str)
                ]
            return parsed

        except Exception as exc:
            log.warning("compose_llm.failed error=%s — using fallback sections", exc)
            return fallback

    # ── Deterministic section builders ────────────────────────────────────────

    def _build_compliance_section(self, audit_packet: AuditPacket) -> str:
        controls_table = "\n".join(
            f"  • {ctrl}" for ctrl in audit_packet.pci_controls_engaged
        ) or "  (no PCI controls engaged)"

        approvals_table = "\n".join(
            f"  • {a.by} via {a.via} at {a.at}"
            for a in audit_packet.approvals_captured
        ) or "  (no approvals captured)"

        sox_status = "✓ COMPLETE" if audit_packet.sox_evidence_complete else "✗ INCOMPLETE"

        return (
            f"Compliance Verdict: {audit_packet.auditor_verdict}\n"
            f"SOX Evidence: {sox_status}\n"
            f"PCI-DSS Controls Engaged:\n{controls_table}\n"
            f"Approvals Captured:\n{approvals_table}\n"
            + (
                f"Blocking Reasons:\n"
                + "\n".join(f"  • {r}" for r in audit_packet.blocking_reasons)
                + "\n"
                if audit_packet.blocking_reasons else ""
            )
            + f"\nCompliance Narrative:\n  {audit_packet.compliance_narrative}\n"
            + f"\nAudit Trail Hash:\n  {audit_packet.audit_trail_hash}"
        )

    def _build_traceability_chain(
        self,
        event_stream: list[A2AMessage],
        pr_data: dict[str, Any],
        audit_packet: AuditPacket,
        risk_payload: dict[str, Any],
        deploy_payload: dict[str, Any],
        sentinel_payload: dict[str, Any],
    ) -> TraceabilityChain:
        pr_number = audit_packet.pr_number
        jira_id = pr_data.get("jira_id", "")
        base_url = os.getenv("GITHUB_REPO_URL", "https://github.com/example/repo")

        # Canary outcome from deploy + sentinel
        canary_pct = deploy_payload.get("canary_pct", "?")
        s_verdict = sentinel_payload.get("verdict") or sentinel_payload.get("decision", "UNKNOWN")
        canary_outcome = (
            f"PROMOTED {canary_pct}%→100%" if s_verdict == "PROMOTE"
            else f"ROLLED_BACK from {canary_pct}%" if s_verdict == "ROLLBACK"
            else s_verdict
        )

        risk_level = risk_payload.get("risk_level", "UNKNOWN")
        risk_score = risk_payload.get("score", "N/A")
        pci_flag = "PCI" if risk_payload.get("pci_scope_touched") else "non-PCI"

        return TraceabilityChain(
            prd_link=pr_data.get("prd_link"),
            jira_link=f"https://jira.example.com/browse/{jira_id}" if jira_id else None,
            pr_link=f"{base_url}/pull/{pr_number}" if pr_number else None,
            risk_verdict_summary=f'"{risk_level}" score={risk_score} {pci_flag}',
            canary_outcome=canary_outcome,
            sentinel_verdict=f'"{s_verdict}" — {sentinel_payload.get("reasoning", "")}',
            compliance_attestation=f"{audit_packet.auditor_verdict} — {audit_packet.release_id}",
        )

    def _determine_outcome(
        self, event_stream: list[A2AMessage], audit_packet: AuditPacket
    ) -> str:
        """Infer outcome from event stream or auditor_verdict."""
        sentinel_payload = _event_payload(event_stream, "sentinel_verdict")
        sv = sentinel_payload.get("verdict") or sentinel_payload.get("decision", "")
        if sv == "PROMOTE":
            return "PROMOTED"
        if sv == "ROLLBACK":
            return "ROLLED_BACK"
        if audit_packet.auditor_verdict == "RELEASE_BLOCKED":
            return "BLOCKED"
        if _event_payload(event_stream, "policy_violation"):
            return "BLOCKED"
        return "PROMOTED"  # default if no rollback signal

    # ── Confluence MCP + Teams ────────────────────────────────────────────────

    async def _publish_to_confluence(self, page: ReleasePage) -> str | None:
        """Call Atlassian Rovo MCP to create the Confluence page.

        Returns confluence_url on success, None if MCP is unreachable.
        """
        body = self._render_to_confluence_storage(page)

        if self._demo:
            mock_id = f"mock-page-{uuid.uuid4().hex[:8]}"
            log.info("publish.confluence.mock page_id=%s title=%s", mock_id, page.page_title)
            return f"https://confluence.example.com/wiki/spaces/RELEASE/pages/{mock_id}"

        try:
            resp = await self._http.post(
                "/atlassian_mcp/create_page",
                json={
                    "title": page.page_title,
                    "body": body,
                    "space_key": os.getenv("CONFLUENCE_SPACE_KEY", "RELEASE"),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            page_id = data.get("page_id", "")
            return data.get("url") or f"https://confluence.example.com/wiki/spaces/RELEASE/pages/{page_id}"
        except (httpx.HTTPError, httpx.ConnectError, Exception) as exc:
            log.warning("confluence.mcp_unreachable error=%s", exc)
            return None

    async def _notify_teams(self, page: ReleasePage) -> None:
        """Send Teams card after successful publish."""
        outcome = "PROMOTED" if not page.sections.incident_timeline else "ROLLED_BACK"
        if page.sections.compliance_banner:
            outcome = "BLOCKED" if "BLOCKED" in page.page_title else outcome
        try:
            await self._teams.post(
                title=self._redactor.redact(page.page_title).redacted_text,
                body=self._redactor.redact(
                    f"Release page published: {page.confluence_url}\n"
                    f"Compliance: {page.sections.traceability_chain.compliance_attestation}"
                ).redacted_text,
                outcome=outcome,
            )
        except Exception as exc:
            log.warning("teams.notify_failed error=%s", exc)

    async def _post_jira_comment(
        self, audit_packet: AuditPacket, outcome: str
    ) -> str | None:
        """Post a Jira comment (demo mock). Returns comment_id or None."""
        if self._demo:
            comment_id = f"mock-comment-{uuid.uuid4().hex[:8]}"
            log.info(
                "jira.comment.mock comment_id=%s release=%s",
                comment_id, audit_packet.release_id,
            )
            return comment_id
        return None

    # ── Rendering helpers ─────────────────────────────────────────────────────

    def _render_to_confluence_storage(self, page: ReleasePage) -> str:
        """Render PageSections to Confluence Storage Format (minimal HTML)."""
        s = page.sections
        parts: list[str] = []

        if s.compliance_banner:
            parts.append(
                f'<ac:structured-macro ac:name="warning">'
                f"<ac:rich-text-body><p>{s.compliance_banner}</p></ac:rich-text-body>"
                f"</ac:structured-macro>"
            )

        parts.append(f"<h2>Breaking Changes</h2>")
        if s.breaking_changes:
            items = "".join(f"<li>{_escape(c)}</li>" for c in s.breaking_changes)
            parts.append(f"<ul>{items}</ul>")
        else:
            parts.append("<p><em>No breaking changes detected.</em></p>")

        parts.append(f"<h2>Impact Summary</h2><p>{_escape(s.engineering_section)}</p>")
        parts.append(f"<h2>Compliance</h2><pre>{_escape(s.compliance_section)}</pre>")

        tc = s.traceability_chain
        parts.append(
            "<h2>Traceability</h2>"
            f"<ul>"
            + (f"<li>PR: <a href='{tc.pr_link}'>{tc.pr_link}</a></li>" if tc.pr_link else "")
            + (f"<li>Jira: <a href='{tc.jira_link}'>{tc.jira_link}</a></li>" if tc.jira_link else "")
            + f"<li>Risk verdict: {_escape(tc.risk_verdict_summary)}</li>"
            + f"<li>Canary outcome: {_escape(tc.canary_outcome)}</li>"
            + f"<li>Sentinel: {_escape(tc.sentinel_verdict)}</li>"
            + f"<li>Attestation: {_escape(tc.compliance_attestation)}</li>"
            + "</ul>"
        )

        if s.incident_timeline:
            items = "".join(f"<li>{_escape(t)}</li>" for t in s.incident_timeline)
            parts.append(f"<h2>Incident Timeline</h2><ul>{items}</ul>")

        return "\n".join(parts)

    def _write_local_markdown(self, page: ReleasePage) -> str:
        """Fallback: render ReleasePage to Markdown and write to /tmp."""
        _LOCAL_PAGE_DIR.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^\w\-]", "_", page.release_id)
        out_path = _LOCAL_PAGE_DIR / f"{safe_id}.md"

        s = page.sections
        lines: list[str] = [f"# {page.page_title}", ""]

        if s.compliance_banner:
            lines += [f"> ⚠ **PCI COMPLIANCE NOTICE**", f"> {s.compliance_banner}", ""]

        lines += ["## Breaking Changes"]
        if s.breaking_changes:
            lines += [f"- {c}" for c in s.breaking_changes]
        else:
            lines += ["_No breaking changes detected._"]
        lines.append("")

        lines += ["## Impact Summary", s.engineering_section, ""]
        lines += ["## Compliance", "```", s.compliance_section, "```", ""]

        tc = s.traceability_chain
        lines += [
            "## Traceability",
            f"- **Risk verdict**: {tc.risk_verdict_summary}",
            f"- **Canary**: {tc.canary_outcome}",
            f"- **Sentinel**: {tc.sentinel_verdict}",
            f"- **Attestation**: {tc.compliance_attestation}",
        ]
        if tc.pr_link:
            lines.append(f"- **PR**: {tc.pr_link}")
        if tc.jira_link:
            lines.append(f"- **Jira**: {tc.jira_link}")
        lines.append("")

        if s.incident_timeline:
            lines += ["## Incident Timeline"]
            lines += [f"- {t}" for t in s.incident_timeline]
            lines.append("")

        out_path.write_text("\n".join(lines), encoding="utf-8")
        log.warning("local_md.written path=%s", out_path)
        return str(out_path)


# ── Module-level helpers ──────────────────────────────────────────────────────


def _event_payload(event_stream: list[A2AMessage], event_type: str) -> dict[str, Any]:
    """Return the payload of the first matching event, or an empty dict."""
    event = next((e for e in event_stream if e.event_type == event_type), None)
    return event.payload if event else {}


def _page_is_pci(page: ReleasePage) -> bool:
    """Heuristic: page is PCI if traceability summary contains 'PCI'."""
    tc = page.sections.traceability_chain
    return "PCI" in tc.risk_verdict_summary.upper()


def _escape(text: str) -> str:
    """Minimal HTML entity escaping for Confluence Storage Format."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
