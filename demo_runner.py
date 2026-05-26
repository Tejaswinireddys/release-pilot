#!/usr/bin/env python3
"""
demo_runner.py — Release Pilot colorized CLI demo runner.

Calls the ACTUAL src/orchestrator.py pipeline and overlays streaming Rich
output by polling run.events every 50 ms in a parallel asyncio task.

Usage:
    python demo_runner.py --scenario 1     # scenario 01 — ServiceA healthy
    python demo_runner.py --scenario 3     # scenario 03 — error-rate spike / rollback
    python demo_runner.py --scenario 4     # scenario 04 — PCI guardrail block
    python demo_runner.py --scenario 6     # scenario 06 — ServiceB healthy (non-PCI)
    python demo_runner.py -s scenarios/scenario_01_healthy_deploy.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import Any

import yaml
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

console = Console()

_SCENARIO_MAP: dict[str, str] = {
    "1":  "scenarios/scenario_01_healthy_deploy.yaml",
    "01": "scenarios/scenario_01_healthy_deploy.yaml",
    "3":  "scenarios/scenario_03_error_rate_spike.yaml",
    "03": "scenarios/scenario_03_error_rate_spike.yaml",
    "4":  "scenarios/scenario_04_pci_guardrail.yaml",
    "04": "scenarios/scenario_04_pci_guardrail.yaml",
    "6":  "scenarios/scenario_06_serviceb_healthy.yaml",
    "06": "scenarios/scenario_06_serviceb_healthy.yaml",
}

_LEVEL_COLOR = {"LOW": "green", "MEDIUM": "yellow", "HIGH": "orange3", "CRITICAL": "red"}
_OUTCOME_COLOR = {"PROMOTED": "green", "ROLLED_BACK": "yellow", "BLOCKED": "red", "FAILED": "red"}
_OUTCOME_ICON = {"PROMOTED": "✅", "ROLLED_BACK": "⚠️ ", "BLOCKED": "🚫", "FAILED": "❌"}


# ── Event renderer ────────────────────────────────────────────────────────────


class _Display:
    """Renders A2A events to the console as the pipeline runs."""

    def __init__(self) -> None:
        self._phase3_hdr = False   # printed once when approval gate fires

    def __call__(self, env: dict[str, Any], run: Any) -> None:
        msg = env.get("event") or env.get("message_type", "")
        payload: dict[str, Any] = env.get("payload") or env.get("data") or {}

        if msg == "pipeline.start":
            self._on_start(env, payload)
        elif msg == "phase.risk_analysis":
            self._on_phase_risk()
        elif msg == "risk_verdict":
            self._on_risk_verdict(payload)
        elif msg == "phase.compliance_precheck":
            self._on_phase_compliance()
        elif msg == "policy_violation":
            self._on_policy_violation(payload)
        elif msg == "deployment_blocked_pending_approval":
            self._on_pending_approval()
        elif msg == "deploy_handle":
            self._on_deploy_handle(payload)
        elif msg == "sentinel_verdict":
            self._on_sentinel_verdict(payload)
        elif msg == "rollback_signal":
            self._on_rollback(payload)
        elif msg == "audit_packet":
            self._on_audit_packet()
        elif msg == "release_page_published":
            self._on_page_published(payload)
        elif msg == "pipeline.complete":
            console.print("\n  [bold green]✓ Pipeline complete[/bold green]")

    def _on_start(self, env: dict, payload: dict) -> None:
        svc = payload.get("service_id", env.get("trace_id", "?")[:8])
        pr = payload.get("pr_number", "?")
        tid = env.get("trace_id", "")
        console.print(Panel(
            f"[bold cyan]service:[/bold cyan] {svc}   "
            f"[bold cyan]PR #[/bold cyan]{pr}   "
            f"[bold cyan]trace:[/bold cyan] {tid[:16]}",
            title="[bold white]Release Pilot — Pipeline Starting[/bold white]",
            border_style="cyan",
        ))

    def _on_phase_risk(self) -> None:
        console.print(Rule("[bold cyan]Phase 1 — Risk Analysis[/bold cyan]", style="cyan"))
        console.print("  [dim]▸ REDACTION: PCI/PII redactor scanning diff...[/dim]")
        console.print("  [dim]▸ SANITIZE: prompt-injection sanitizer running...[/dim]")
        console.print("  [dim]▸ Querying RAG index + service graph...[/dim]")

    def _on_risk_verdict(self, payload: dict) -> None:
        level = payload.get("risk_level", "?")
        score = payload.get("score", "?")
        pci = payload.get("pci_scope_touched", False)
        guards = payload.get("guardrails_triggered", [])
        conf = payload.get("confidence", 0.0)
        br = payload.get("blast_radius", {}) or {}
        color = _LEVEL_COLOR.get(str(level), "white")

        t = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        t.add_column("k", style="dim")
        t.add_column("v")
        t.add_row("Risk Level", f"[bold {color}]{level}[/bold {color}] (score={score}/100)")
        t.add_row("PCI Scope", "[bold red]YES[/bold red]" if pci else "[green]no[/green]")
        t.add_row("Confidence", f"{conf:.2f}")
        t.add_row("Blast Radius", str(br.get("affected_services", br.get("service", "?"))))
        t.add_row("Guardrails", ", ".join(guards) if guards else "[dim]none[/dim]")
        console.print(Panel(t, title="[bold]Risk Verdict[/bold]", border_style=color, expand=False))

    def _on_phase_compliance(self) -> None:
        console.print(Rule("[bold cyan]Phase 2 — Compliance Pre-check[/bold cyan]", style="cyan"))
        console.print("  [dim][OPA] Evaluating harness.deploy policy...[/dim]")

    def _on_policy_violation(self, payload: dict) -> None:
        reasons = payload.get("denial_reasons", [])
        console.print("  [bold red][OPA] action=harness.deploy → DENIED[/bold red]")
        for r in reasons:
            console.print(f"    [red]✗ {r}[/red]")

    def _on_pending_approval(self) -> None:
        if not self._phase3_hdr:
            console.print(Rule("[bold cyan]Phase 3 — Human Approval Gate[/bold cyan]", style="cyan"))
            self._phase3_hdr = True
        console.print("  [yellow]⏸  Awaiting human approval token...[/yellow]")

    def _on_deploy_handle(self, payload: dict) -> None:
        pct = payload.get("canary_pct", "?")
        bake = payload.get("bake_minutes", "?")
        exec_id = payload.get("harness_execution_id", "?")
        console.print("  [bold green][OPA] action=harness.deploy → ALLOWED[/bold green]")
        if not self._phase3_hdr:
            console.print(Rule("[bold cyan]Phase 3-4 — Canary + SLO Watch[/bold cyan]", style="cyan"))
            self._phase3_hdr = True
        console.print(f"  [bold]Harness execution:[/bold] {exec_id}")
        console.print(f"  [bold]Canary traffic:[/bold] {pct}%   [bold]Bake:[/bold] {bake} min")
        console.print("  [dim]SLO Sentinel polling CloudWatch...[/dim]")

    def _on_sentinel_verdict(self, payload: dict) -> None:
        verdict = payload.get("verdict", "?")
        conf = float(payload.get("confidence", 0))
        reasoning = payload.get("reasoning", "")
        intervals = payload.get("intervals_checked", "?")
        color = "green" if verdict == "PROMOTE" else ("red" if verdict == "ROLLBACK" else "yellow")
        icon = "✅" if verdict == "PROMOTE" else ("🔴" if verdict == "ROLLBACK" else "⚠️ ")
        console.print(
            f"\n  {icon} SLO Sentinel: [bold {color}]{verdict}[/bold {color}]"
            f"   conf={conf:.2f}   intervals={intervals}"
        )
        if reasoning:
            console.print(f"  [dim]{reasoning[:140]}[/dim]")

    def _on_rollback(self, payload: dict) -> None:
        reason = payload.get("reason", "SLO breach")
        console.print("\n  [bold red]🔴 ROLLBACK TRIGGERED[/bold red]")
        console.print(f"  [red]Reason: {reason}[/red]")
        console.print("  [dim]Harness rollback executing...[/dim]")

    def _on_audit_packet(self) -> None:
        console.print(Rule(
            "[bold cyan]Phase 5 — Compliance Attestation + Release Docs[/bold cyan]",
            style="cyan",
        ))
        console.print("  [dim]▸ Compliance Auditor: computing audit_trail_hash (SHA-256)...[/dim]")
        console.print("  [dim]▸ Release Scribe: building Confluence page...[/dim]")
        for section in [
            "Executive Summary", "Breaking Changes",
            "Engineering Details", "Compliance Controls", "Incident Timeline",
        ]:
            console.print(f"    [dim]  § {section}[/dim]")

    def _on_page_published(self, payload: dict) -> None:
        url = payload.get("confluence_url") or payload.get("url") or "[dim]demo-mode[/dim]"
        console.print(f"  [green]✓ Confluence page published:[/green] {url}")


# ── Event-polling loop ────────────────────────────────────────────────────────


async def _event_loop(run: Any, display: _Display, stop: asyncio.Event) -> None:
    """Poll run.events every 50 ms and render new events until stop is set."""
    seen = 0
    while not stop.is_set():
        for env in run.events[seen:]:
            display(env, run)
            seen += 1
        await asyncio.sleep(0.05)
    # Drain any events appended after the final poll
    for env in run.events[seen:]:
        display(env, run)


# ── Summary table ─────────────────────────────────────────────────────────────


def _print_summary(run: Any, elapsed: float) -> None:
    status_val = run.status.value
    outcome = {
        "complete": "PROMOTED", "rolled-back": "ROLLED_BACK",
        "blocked": "BLOCKED", "failed": "FAILED",
    }.get(status_val, status_val.upper())
    color = _OUTCOME_COLOR.get(outcome, "white")
    icon = _OUTCOME_ICON.get(outcome, "")

    t = Table(
        title=f"Pipeline Summary — {run.release_id}",
        box=box.ROUNDED, show_header=True, header_style="bold cyan",
    )
    t.add_column("Field", style="dim", width=22)
    t.add_column("Value")

    t.add_row("Final Outcome", f"[bold {color}]{icon} {outcome}[/bold {color}]")
    t.add_row("Elapsed", f"{elapsed:.1f}s")
    t.add_row("Release ID", run.release_id)
    t.add_row("Trace ID", run.trace_id)

    if run.risk_verdict:
        rv = run.risk_verdict
        t.add_row("Risk Score", f"{rv.score}/100 ({rv.risk_level})")
        t.add_row("PCI Scope", "[red]YES[/red]" if rv.pci_scope_touched else "no")
        guards = rv.guardrails_triggered
        t.add_row("Guardrails Triggered", ", ".join(guards) if guards else "none")

    inject_attempts = sum(
        1 for e in run.events if "injection" in str(e.get("event", "")).lower()
    )
    t.add_row("Injection Attempts", str(inject_attempts) if inject_attempts else "0 (clean)")

    opa_denials = sum(1 for e in run.events if e.get("event") == "policy_violation")
    t.add_row("OPA Decisions", f"{opa_denials} denial(s) / {len(run.events)} total events")

    if run.sentinel_verdict:
        t.add_row("SLO Verdict", run.sentinel_verdict.verdict)
        t.add_row("TTD (time-to-decision)", f"{elapsed:.1f}s")

    if run.audit_packet:
        ap = run.audit_packet
        t.add_row("Audit Verdict", ap.auditor_verdict)
        t.add_row("Audit Hash", ap.audit_trail_hash[:32] + "…")
        controls = ap.pci_controls_engaged
        summary = ", ".join(controls[:3]) + ("…" if len(controls) > 3 else "") if controls else "N/A"
        t.add_row("PCI Controls", summary)

    conf_url = run.release_note.confluence_page_url if run.release_note else None
    t.add_row("Confluence", conf_url or "[dim]skipped (demo mode)[/dim]")

    console.print()
    console.print(t)
    console.print()
    import os as _os
    if _os.getenv("OTEL_MODE", "auto").strip().lower() == "file":
        console.print("[dim]Trace summary:  traces/last_run.txt[/dim]")
    elif _os.getenv("OTEL_MODE", "auto").strip().lower() == "console":
        pass  # spans already printed to stdout
    else:
        console.print(
            f"[dim]Open Jaeger:    http://localhost:16686/trace/{run.trace_id}[/dim]"
        )
    if conf_url:
        console.print(f"[dim]Confluence page:   {conf_url}[/dim]")
    console.print()


# ── Main runner ───────────────────────────────────────────────────────────────


async def run_demo(scenario_path: Path) -> int:
    from src.orchestrator import Orchestrator, PipelineRun
    from src.telemetry import setup_telemetry

    setup_telemetry()

    raw = yaml.safe_load(scenario_path.read_text())
    run = PipelineRun(
        service_id=raw.get("service_id", "unknown"),
        pr_number=raw.get("pr_number", 0),
    )

    display = _Display()
    stop = asyncio.Event()
    orch = Orchestrator()
    wall_start = time.time()

    async def _run_pipeline() -> None:
        await orch.run(run)
        stop.set()

    await asyncio.gather(_run_pipeline(), _event_loop(run, display, stop))

    elapsed = time.time() - wall_start
    _print_summary(run, elapsed)
    return 0 if run.status.value == "complete" else 1


def main() -> None:
    p = argparse.ArgumentParser(
        description="Release Pilot demo runner — colorized pipeline walkthrough",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python demo_runner.py -s 1    # ServiceA healthy deploy\n"
               "  python demo_runner.py -s 3    # error-rate spike → rollback\n"
               "  python demo_runner.py -s 4    # PCI guardrail block\n"
               "  python demo_runner.py -s 6    # ServiceB healthy (non-PCI)\n",
    )
    p.add_argument("--scenario", "-s", required=True, metavar="N",
                   help="Scenario number (1/3/4/6) or path to a YAML file")
    args = p.parse_args()

    key = args.scenario
    if key in _SCENARIO_MAP:
        scenario_path = Path(_SCENARIO_MAP[key])
    else:
        scenario_path = Path(key)

    if not scenario_path.exists():
        console.print(f"[red]✗ Scenario not found: {scenario_path}[/red]")
        console.print(f"[dim]Valid numbers: {sorted(set(_SCENARIO_MAP.values()))}[/dim]")
        raise SystemExit(1)

    console.print(Rule(style="cyan"))
    console.print(Panel(
        f"[bold cyan]Release Pilot — Demo Runner[/bold cyan]\n[dim]{scenario_path}[/dim]",
        border_style="cyan",
    ))

    exit_code = asyncio.run(run_demo(scenario_path))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
