"""Release Pilot Orchestrator — thin A2A coordinator (Risk → Compliance → Canary → SLO ‖ Scribe)."""
from __future__ import annotations
import argparse, asyncio, os, time, uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
import structlog, yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from src.agents.canary_orchestrator import ApprovalTimeoutError, CanaryOrchestrator, CanaryResult, DeploymentHandle
from src.agents.compliance_auditor import AuditPacket, ComplianceAuditor
from src.agents.release_scribe import ReleaseNote, ReleaseScribe
from src.agents.risk_analyst import RiskAnalyst, RiskVerdict
from src.agents.slo_sentinel import SentinelVerdict, SLOSentinel
from src.knowledge.service_graph import ServiceGraph
from src.opa_client import PolicyViolationError
from src.redactor import PCIRedactor
from src.sanitizer import PromptInjectionSanitizer
from src.telemetry import get_tracer, setup_telemetry
log = structlog.get_logger(__name__)
_runs: dict[str, "PipelineRun"] = {}
_approval_events: dict[str, asyncio.Event] = {}
_approval_tokens: dict[str, dict[str, str]] = {}


class PipelineStatus(str, Enum):
    PENDING = "pending"; RISK_ANALYSIS = "risk-analysis"; AWAITING_APPROVAL = "awaiting-approval"
    CANARY = "canary"; SENTINEL_WATCHING = "sentinel-watching"; ROLLING_BACK = "rolling-back"
    ROLLED_BACK = "rolled-back"; ATTESTING = "attesting"
    COMPLETE = "complete"; BLOCKED = "blocked"; FAILED = "failed"


@dataclass
class PipelineRun:
    release_id: str = field(default_factory=lambda: f"REL-{uuid.uuid4().hex[:8]}")
    trace_id:   str = field(default_factory=lambda: uuid.uuid4().hex)
    run_id:     str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    status: PipelineStatus = PipelineStatus.PENDING
    service_id: str = ""; pr_number: int = 0
    risk_verdict: RiskVerdict | None = None; opa_decision: Any = None
    deploy_handle: DeploymentHandle | None = None; sentinel_verdict: SentinelVerdict | None = None
    canary_result: CanaryResult | None = None; audit_packet: AuditPacket | None = None
    release_note: ReleaseNote | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time); completed_at: float | None = None
    @property
    def risk_report(self) -> dict[str, Any]:
        if not self.risk_verdict: return {}
        d = self.risk_verdict.model_dump(); d["risk_score"] = d.get("score", 0)
        d["requires_human_approval"] = d["risk_score"] >= 70; return d
    @property
    def compliance_result(self) -> dict[str, Any]:
        return {} if not self.opa_decision else {"approved": self.opa_decision.allowed, "violations": self.opa_decision.violations}
    @property
    def slo_result(self) -> dict[str, Any]:
        return {} if not self.sentinel_verdict else {"action": self.sentinel_verdict.verdict.lower(), "reason": self.sentinel_verdict.reasoning, "confidence": self.sentinel_verdict.confidence}
    @property
    def release_doc(self) -> dict[str, Any]:
        if not self.release_note: return {}
        d = self.release_note.model_dump(); d["doc_id"] = self.release_note.confluence_page_id or self.release_note.trace_id; return d


class Orchestrator:
    """Chains 5 agents with A2A JSON messaging, OPA gates, and trace propagation."""

    def __init__(self) -> None:
        self._redactor = PCIRedactor(); self._sanitizer = PromptInjectionSanitizer()
        self._graph = ServiceGraph(); self._graph_loaded = False; self._tracer = get_tracer("orchestrator")
        self._risk = RiskAnalyst();          self._risk_analyst        = self._risk
        self._auditor = ComplianceAuditor(); self._compliance_auditor  = self._auditor
        self._canary = CanaryOrchestrator(); self._canary_orchestrator = self._canary
        self._sentinel = SLOSentinel();      self._slo_sentinel        = self._sentinel
        self._scribe = ReleaseScribe();      self._release_scribe      = self._scribe

    def _emit(self, run: PipelineRun, from_agent: str, to_agent: str, msg_type: str, payload: dict) -> dict:
        env = {"from_agent": from_agent, "to_agent": to_agent, "message_type": msg_type,
               "event": msg_type, "agent": from_agent, "payload": payload, "data": payload,
               "timestamp": datetime.now(timezone.utc).isoformat(),
               "trace_id": run.trace_id, "release_id": run.release_id}
        run.events.append(env)
        log.info("A2A_MESSAGE", from_=from_agent, to=to_agent, type=msg_type,
                 trace_id=run.trace_id, release_id=run.release_id)
        return env

    async def run(self, run_or_path: "PipelineRun | str | Path") -> PipelineRun:
        if isinstance(run_or_path, (str, Path)):
            raw = yaml.safe_load(Path(run_or_path).read_text())
            sc = {k: (self._sanitizer.sanitize(v).sanitized_text if isinstance(v, str) else v) for k, v in raw.items()}
            run = PipelineRun(service_id=sc.get("service_id", "unknown"), pr_number=sc.get("pr_number", 0))
        else:
            run = run_or_path
        _runs[run.release_id] = run; return await self._pipeline(run)

    async def _pipeline(self, run: PipelineRun) -> PipelineRun:  # noqa: C901
        if not self._graph_loaded:
            try: self._graph.load(Path(__file__).parent.parent / "config" / "service_graph.json"); self._graph_loaded = True
            except OSError: pass
        svc = self._graph.lookup(run.service_id)
        sox_scope = svc.sox_scope if svc else False; outcome = "FAILED"
        self._emit(run, "orchestrator", "broadcast", "pipeline.start",
                   {"service_id": run.service_id, "pr_number": run.pr_number})
        with self._tracer.start_as_current_span("pipeline.run") as span:
            span.set_attribute("trace_id", run.trace_id); span.set_attribute("release_id", run.release_id)
            try:
                # 1. Risk Analyst — starts immediately on PR merge
                run.status = PipelineStatus.RISK_ANALYSIS
                self._emit(run, "orchestrator", "risk-analyst", "phase.risk_analysis", {})
                run.risk_verdict = await asyncio.to_thread(
                    self._risk.analyze,
                    {"pr_number": run.pr_number, "service_id": run.service_id, "trace_id": run.trace_id})
                self._emit(run, "risk-analyst", "broadcast", "risk_verdict", run.risk_verdict.model_dump())
                # 2. Compliance Auditor pre-check — subscribes to event_stream broadcast
                self._emit(run, "orchestrator", "compliance-auditor", "phase.compliance_precheck", {})
                run.opa_decision = await self._auditor.pre_check(run.risk_verdict, run.trace_id)
                if not run.opa_decision.allowed:
                    run.status = PipelineStatus.BLOCKED; outcome = "BLOCKED"
                    self._emit(run, "compliance-auditor", "broadcast", "policy_violation",
                               {"denial_reasons": run.opa_decision.violations})
                    run.completed_at = time.time(); return await self._finalize(run, outcome, sox_scope)
                # 3. Canary Orchestrator — may pause for human approval
                if run.risk_verdict.recommended_strategy and not run.risk_verdict.recommended_strategy.auto_promote:
                    run.status = PipelineStatus.AWAITING_APPROVAL
                    self._emit(run, "canary-orchestrator", "broadcast",
                               "deployment_blocked_pending_approval", {"service_id": run.service_id})
                run.status = PipelineStatus.CANARY
                run.deploy_handle = await self._canary.deploy(run.risk_verdict, run.trace_id)
                self._emit(run, "canary-orchestrator", "broadcast", "deploy_handle",
                           run.deploy_handle.model_dump())
                # 4. SLO Sentinel + Release Scribe page-building run concurrently on shared event_stream
                run.status = PipelineStatus.SENTINEL_WATCHING
                run.sentinel_verdict, _ = await asyncio.gather(
                    self._sentinel.watch(run.deploy_handle),
                    asyncio.sleep(0))   # Scribe builds draft from run.events in parallel
                self._emit(run, "slo-sentinel", "broadcast", "sentinel_verdict",
                           run.sentinel_verdict.model_dump())
                if run.sentinel_verdict.verdict == "ROLLBACK":
                    run.status = PipelineStatus.ROLLED_BACK; outcome = "ROLLED_BACK"
                    log.warning("slo.rollback", reason=run.sentinel_verdict.reasoning)
                    await self._canary.rollback(run.deploy_handle)
                    self._emit(run, "orchestrator", "broadcast", "rollback_signal",
                               {"reason": run.sentinel_verdict.reasoning})
                else:
                    outcome = "PROMOTED"; run.status = PipelineStatus.COMPLETE
                    self._emit(run, "orchestrator", "broadcast", "pipeline.complete", {})
                run.canary_result = CanaryResult(
                    service_id=run.service_id, pr_number=run.pr_number, deployment_id=run.deploy_handle.deployment_id,
                    outcome=outcome, steps=[], trace_id=run.trace_id,
                    final_traffic_pct=100 if outcome == "PROMOTED" else 0,
                    rollback_reason=run.sentinel_verdict.reasoning if outcome == "ROLLED_BACK" else None,
                    started_at=run.deploy_handle.start_time, completed_at=datetime.now(timezone.utc).isoformat())
            except (PolicyViolationError, ApprovalTimeoutError) as exc:
                reasons = getattr(exc, "denial_reasons", [str(exc)])
                run.status = PipelineStatus.BLOCKED; outcome = "BLOCKED"
                self._emit(run, "opa", "broadcast", "policy_violation", {"denial_reasons": reasons})
                log.error("pipeline.blocked", reasons=reasons)
            except Exception as exc:
                run.status = PipelineStatus.FAILED; outcome = "FAILED"
                self._emit(run, "orchestrator", "broadcast", "agent_error",
                           {"error": str(exc), "type": type(exc).__name__})
                log.exception("pipeline.error", error=str(exc))
            finally:
                run.completed_at = run.completed_at or time.time()
                if run.audit_packet is None: await self._finalize(run, outcome, sox_scope)
        return run

    async def _finalize(self, run: PipelineRun, outcome: str, sox_scope: bool) -> PipelineRun:
        """Attestation + documentation — always runs; restores terminal status afterwards."""
        terminal = run.status
        try:
            run.status = PipelineStatus.ATTESTING
            slo_arg = run.sentinel_verdict or run.canary_result
            if run.risk_verdict and run.canary_result and slo_arg:
                run.audit_packet = await self._auditor.attest(
                    run.risk_verdict, run.canary_result, slo_arg, sox_scope, run.trace_id)
                self._emit(run, "compliance-auditor", "broadcast", "audit_packet",
                           run.audit_packet.model_dump())
            if run.audit_packet:
                run.release_note = await self._scribe.document(run.audit_packet, outcome, run.trace_id)
                self._emit(run, "release-scribe", "broadcast", "release_page_published",
                           {"confluence_url": run.release_note.confluence_page_url})
        except Exception as exc: log.error("finalize.error", error=str(exc))
        finally: run.status = terminal
        return run


app = FastAPI(title="Release Pilot", version="0.1.0")
_orch = Orchestrator()


class ApproveBody(BaseModel):
    token: str; approved_by: str; via: str = "manual"


@app.post("/webhook/pr-merged", status_code=202)
async def webhook_pr_merged(body: dict[str, Any]) -> dict[str, Any]:
    """Receive GitHub PR-merged webhook; generate trace_id + release_id, start pipeline."""
    run = PipelineRun(service_id=body.get("repository", {}).get("name", "unknown"),
                      pr_number=body.get("pull_request", {}).get("number", 0))
    _runs[run.release_id] = run; asyncio.create_task(_orch._pipeline(run))
    log.info("webhook.accepted", release_id=run.release_id, trace_id=run.trace_id)
    return {"release_id": run.release_id, "trace_id": run.trace_id, "status": "started"}

@app.get("/release/{release_id}/status")
async def get_release_status(release_id: str) -> dict[str, Any]:
    run = _runs.get(release_id)
    if not run: raise HTTPException(404, f"release_id {release_id!r} not found")
    return {"release_id": run.release_id, "trace_id": run.trace_id,
            "current_stage": run.status.value, "event_stream": run.events,
            "release_page_url": run.release_doc.get("confluence_page_url")}

@app.post("/release/{release_id}/approve")
async def post_approve(release_id: str, body: ApproveBody) -> dict[str, Any]:
    if release_id not in _runs: raise HTTPException(404, f"release_id {release_id!r} not found")
    _approval_tokens[release_id] = {"token": body.token, "approved_by": body.approved_by, "via": body.via}
    os.environ["DEMO_APPROVAL_TOKEN"] = body.token   # unblocks CanaryOrchestrator approval polling
    if ev := _approval_events.get(release_id): ev.set()
    log.info("approval.accepted", release_id=release_id, approved_by=body.approved_by)
    return {"accepted": True, "approval_chain": [_approval_tokens[release_id]]}


def _cli() -> None:
    from rich.console import Console; from rich.table import Table; from rich import print as rprint
    p = argparse.ArgumentParser(description="Release Pilot orchestrator")
    p.add_argument("--scenario", type=Path, help="Run a scenario YAML end-to-end (no server)")
    p.add_argument("--server", action="store_true", help="Start FastAPI webhook server on :9000")
    args = p.parse_args()
    if args.server:
        import uvicorn; setup_telemetry("release-pilot-server")
        uvicorn.run("src.orchestrator:app", host="0.0.0.0", port=9000, reload=False); return
    if not args.scenario: p.print_help(); return
    setup_telemetry("release-pilot-cli"); console = Console()
    console.rule("[bold cyan]Release Pilot — Demo Runner[/bold cyan]"); console.print(f"[dim]Scenario:[/dim] {args.scenario}")
    run = asyncio.run(Orchestrator().run(args.scenario))
    t = Table(title=f"Pipeline Result — {run.release_id}", show_lines=True)
    t.add_column("Field", style="bold"); t.add_column("Value")
    color = {"complete": "green", "rolled-back": "yellow", "blocked": "red", "failed": "red"}.get(run.status.value, "white")
    t.add_row("Status", f"[{color}]{run.status.value}[/{color}]")
    t.add_row("Service", run.service_id); t.add_row("PR #", str(run.pr_number))
    t.add_row("Trace ID", run.trace_id); t.add_row("Release ID", run.release_id)
    if run.risk_verdict: t.add_row("Risk", f"{run.risk_verdict.risk_level} (score={run.risk_verdict.score})")
    if run.sentinel_verdict: t.add_row("SLO Verdict", run.sentinel_verdict.verdict)
    if run.audit_packet:
        t.add_row("Audit Verdict", run.audit_packet.auditor_verdict)
        t.add_row("Audit Hash", run.audit_packet.audit_trail_hash[:32] + "…")
    if run.release_doc.get("confluence_page_url"): t.add_row("Confluence", run.release_doc["confluence_page_url"])
    console.print(t); rprint(f"\n[dim]A2A events emitted: {len(run.events)}[/dim]")

if __name__ == "__main__": _cli()
