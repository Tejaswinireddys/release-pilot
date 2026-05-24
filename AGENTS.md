# Release Pilot

## Project Purpose

A 5-agent system that takes over the moment a PR is merged and owns the dangerous last mile of
the SDLC: risk assessment, canary orchestration, SLO monitoring, compliance attestation, and
release documentation.

The demo uses two example services:
- **ServiceA** — a payment-processing service, PCI-DSS scoped, tier: critical
- **ServiceB** — a non-PCI notification service, tier: standard

These are placeholders. Any team can swap in their own service names via `config/service_graph.json`.

```
PR merged → Webhook
                │
        ┌───────▼────────────────────────────────────────┐
        │               Orchestrator                       │
        │  trace_id propagated through every A2A message  │
        └──┬──────────┬──────────┬──────────┬────────────┘
           │          │          │          │          │
       Risk       Canary      SLO      Compliance  Release
      Analyst    Orchestr.  Sentinel   Auditor     Scribe
        [1]        [3]        [4]        [2,5]       [5]
```

**Pipeline order:**
1. **Risk Analyst** — reads PR diff + RAG + service graph → emits `RiskVerdict`
2. **Compliance Auditor** (pre-check) — OPA policy gate on `RiskVerdict`
3. **Canary Orchestrator** — translates verdict into Harness deploy with policy gate
4. **SLO Sentinel** — polls CloudWatch → emits explainable `PROMOTE / ROLLBACK / ESCALATE`
5. **Compliance Auditor** (attest) + **Release Scribe** — frozen audit packet + Confluence page

---

## Build & Run

```bash
pip install -r requirements.txt
docker-compose up -d        # starts Jaeger + OPA + Mock AWS
python -m pytest tests/
python demo_runner.py --scenario 1   # healthy ServiceA deploy
python demo_runner.py --scenario 3   # error-rate spike → rollback
python demo_runner.py --scenario 4   # PCI guardrail block
python demo_runner.py --scenario 6   # ServiceB healthy deploy
```

---

## Conventions

- All agents emit **structured Pydantic JSON output**. Never free text.
- All agent definitions live in `.github/agents/*.agent.md` (Copilot 2026 standard).
- The **PCI/PII Redactor** runs before every LLM call. No exceptions.
- The **Prompt-Injection Sanitizer** runs on every diff body before LLM ingestion.
- The **OPA Policy Engine** runs before every tool call. A deny halts execution.
- **OpenTelemetry GenAI spans** for every agent invocation, LLM call, and tool call.
- `trace_id` is set at PR-merge webhook receipt and propagated through every A2A message.

---

## Agent Roster

| Agent | Module | Output Model | Least-Privilege |
|-------|--------|--------------|-----------------|
| risk-analyst | `src/agents/risk_analyst.py` | `RiskVerdict` | github_mcp + rag + memory + service_graph only |
| canary-orchestrator | `src/agents/canary_orchestrator.py` | `CanaryResult` | harness + aws_mcp ECS only |
| slo-sentinel | `src/agents/slo_sentinel.py` | `SLOVerdict` | aws_mcp metrics only |
| compliance-auditor | `src/agents/compliance_auditor.py` | `AuditPacket` | NO external tools |
| release-scribe | `src/agents/release_scribe.py` | `ReleaseNote` | atlassian_mcp + teams only |

---

## MCP Servers

| Server | Package | Base URL |
|--------|---------|----------|
| GitHub MCP | `npx @modelcontextprotocol/server-github` | stdio |
| Atlassian Rovo MCP | `npx @atlassian/mcp-atlassian` (GA Feb 2026) | stdio |
| Mock AWS MCP | local FastAPI (`src/tools/aws_mock_server.py`) | `http://localhost:8080` |

Configured in `mcp-config.yaml`.

---

## Safety Rules

- Any OPA `deny` raises `PolicyViolationError` and halts execution immediately.
- PCI scope detection has **3 signals** (any one sets `pci_scope_touched = true`):
  1. **Path regex**: files matching `payment/`, `card/`, `billing/`, `cardholder/`, `pci/`, `cvv/`, `pan/`
  2. **Service graph flag**: `pci_scope: true` in `config/service_graph.json`
  3. **Shared-libs**: imports of `pci-shared-*`, `cardholder-*`, `payment-crypto-*`
- On uncertainty, **default `pci_scope_touched = true`**.
- Audit packets are **frozen Pydantic models** with `SHA256 audit_trail_hash`. Once created, they cannot be mutated.
- `compliance-auditor` has **zero external tool calls** — it works exclusively on data passed to it.

---

## Standards

| Standard | Governing Body |
|----------|----------------|
| AGENTS.md / AAIF | Agentic AI Foundation under Linux Foundation |
| `.agent.md` custom-agent spec | GitHub Copilot 2026 |
| MCP (Model Context Protocol) | Anthropic / open standard |
| OPA / Rego | Cloud Native Computing Foundation (CNCF) |
| OpenTelemetry GenAI semantic conventions | OpenTelemetry / CNCF |
| PCI-DSS v4.0 | PCI Security Standards Council |
| SOX Section 302 / 404 | US SEC |

---

## Adoption Guide

Any team can adopt Release Pilot in 5 steps:

1. **Copy the agent scaffold** — copy `AGENTS.md` (symlink `CLAUDE.md → AGENTS.md`),
   `.github/agents/*.agent.md` (all 5 files), and `mcp-config.yaml` into your repo.

2. **Edit AGENTS.md** — update service names in the "Agent Roster" table, CDE file
   path patterns in "Safety Rules" (the three PCI scope signals), and the Jira project
   key used by Release Scribe when posting deployment comments.

3. **Edit `config/service_graph.json`** — add entries for your services with
   `owner_team`, `pci_scope` (boolean), `file_paths` (glob patterns), and
   `direct_consumers`; update `pci_shared_libs` to match your shared-library
   package paths so the PCI scope detector covers cross-service imports.

4. **Re-index your service docs** — point the RAG index at your documentation
   directory so the Risk Analyst has context for historical incidents and runbooks:

   ```bash
   python -c "from src.knowledge.rag_index import RAGIndex; RAGIndex().build_from_directory('./docs')"
   ```

5. **Run the demo against your service** — verify the end-to-end pipeline with
   one of your own scenario YAMLs or the bundled scenarios:

   ```bash
   python demo_runner.py --scenario 1
   ```

No application code changes are needed. All agent behaviour is driven by the
configuration files and `service_graph.json`; the pipeline is service-agnostic.
