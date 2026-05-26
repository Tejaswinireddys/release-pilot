# Release Pilot — End-to-End Status Report

Generated: 2026-05-25

---

## Step 1 — Static Integrity

| Check | Result |
|-------|--------|
| Python syntax (all files) | PASS — 16/16 modules parse without errors |
| Module imports (all 16) | PASS — no missing dependencies, no circular imports |
| `.github/agents/*.agent.md` | PASS — all 5 agent definition files present |
| Scenario YAMLs | PASS — 6 files, all well-formed |
| `config/service_graph.json` | PASS — 4 top-level keys, valid JSON |
| `policies/release_guardrails.rego` | PASS — file present, valid Rego v1 syntax |
| `CLAUDE.md → AGENTS.md` symlink | PASS — resolves correctly |
| Supporting files | PASS — `mcp-config.yaml`, `docker-compose.yaml`, `Dockerfile`, `start_demo.sh` all present |

---

## Step 2 — Dependency & Environment

| Item | Status |
|------|--------|
| `requirements.txt` | Complete — all packages pinned to minimum version |
| `.env.example` | Present — all required variables documented with placeholders |
| `.env` (runtime) | Not committed (correct) — operators must copy from `.env.example` and fill in credentials |
| OPA embedded Rego | Loads from `policies/release_guardrails.rego` — no install required |
| Mock AWS server | Importable and starts cleanly on `localhost:8080` |

---

## Step 3 — Test Suite

```
python3.11 -m pytest tests/ -v
```

| Result | Count |
|--------|-------|
| Passed | 84 |
| Skipped | 8 (OPA live-server tests — require `OPA_URL` pointing to a running OPA instance) |
| Failed | 0 |
| Total | 92 |

One `DeprecationWarning` from `opentelemetry.util._importlib_metadata` (upstream package, not our code).

21 new live-integration tests added in `tests/test_live_integrations.py` (sandbox guards, dry-run, factory selection). All 8 spec-required integration tests pass:
- `test_healthy_deploy_end_to_end`
- `test_rollback_pipeline`
- `test_pci_guardrail_blocks_deploy`
- `test_redactor_protects_llm_prompts`
- `test_service_graph_blast_radius_two_hop`
- `test_pci_shared_lib_detected`
- `test_audit_packet_is_immutable`
- `test_low_confidence_sentinel_escalates_not_rolls_back`

---

## Step 4 — Scenario End-to-End (with mocks)

| Scenario | Mock layer | Result |
|----------|-----------|--------|
| 01 — ServiceA healthy | Mock AWS + Harness demo mode + OPA embedded | **BLOCKED at Phase 1** — Risk Analyst LLM call fails without `OPENAI_API_KEY` |
| 03 — Error-rate spike / rollback | Same | **BLOCKED at Phase 1** — same reason |
| 04 — PCI guardrail block | Same | **BLOCKED at Phase 1** — same reason |
| 06 — ServiceB healthy | Same | **BLOCKED at Phase 1** — same reason |

Phases 2–5 are fully implemented and covered by the test suite (mocked LLM). The demo_runner has no internal mock path for the LLM — it calls the actual pipeline, which requires a valid `OPENAI_API_KEY` (or a local OpenAI-compatible endpoint via `OPENAI_BASE_URL`).

**To run scenarios end-to-end:**
```bash
# Option A — real OpenAI key
echo "OPENAI_API_KEY=sk-..." >> .env && python demo_runner.py -s 1

# Option B — local LLM (e.g., ollama with mistral)
OPENAI_BASE_URL=http://localhost:11434/v1 OPENAI_API_KEY=ollama \
  AGENT_MODEL=mistral python demo_runner.py -s 1
```

---

## Step 5 — Graceful Degradation (placeholder credentials)

Tested with `OPENAI_API_KEY=demo`, all other services absent.

| Failure scenario | Behavior |
|-----------------|----------|
| Invalid/placeholder OpenAI key | `AuthenticationError` caught by orchestrator → `status=failed`, `agent_error` event emitted — no crash |
| OPA server unreachable | Falls back to embedded Rego evaluation automatically — confirmed working |
| Harness unavailable | `DEMO_MODE=true` (default) → deterministic mock responses — no network call |
| Mock AWS server down | `httpx.ConnectError` in SLO Sentinel — caught by orchestrator `except Exception` → `status=failed` |
| Jaeger/OTLP exporter unreachable | `BatchSpanProcessor` drops spans silently — no test impact, no crash |
| GitHub MCP not configured | Falls back to synthetic PCI-touching diff — correct demo behavior |
| Atlassian MCP missing | `release_page_published` event emitted with `confluence_url=None` — page skipped, pipeline continues |

No PII/PAN leaks observed in any error path. Redactor runs before every LLM call.

---

## Step 6 — Component Status Table

### External integrations

| Integration | Status | Mode flag | Notes |
|-------------|--------|-----------|-------|
| **GitHub PR diff** | LIVE-CAPABLE | `INTEGRATION_GITHUB_MODE=live` | Fetches real diff + file list from GitHub REST API; falls back to fixture diff on failure |
| **Confluence pages** | LIVE-CAPABLE | `INTEGRATION_CONFLUENCE_MODE=live` | Publishes via Confluence Cloud REST API v2; falls back to local Markdown on failure |
| **AWS CloudWatch** | LIVE-CAPABLE | `INTEGRATION_AWS_MODE=live` | boto3 CloudWatch reads; sandbox-scoped to `AWS_SANDBOX_SERVICE`; falls back to mock on error |
| **Harness deploy** | LIVE-CAPABLE | `INTEGRATION_HARNESS_MODE=live` | Real Harness REST API; sandbox-scoped; `SAFETY_ALLOW_LIVE_DEPLOYS=false` dry-run by default; falls back to mock on error |

To verify credentials before running a pipeline:
```bash
source .env && python3 scripts/check_confluence.py
source .env && python3 scripts/check_github.py --pr 42
```

### All components

| Component | Status | Notes |
|-----------|--------|-------|
| **Risk Analyst** | NEEDS-LIVE-CREDENTIALS | Requires `OPENAI_API_KEY`; GitHub live diff: set `INTEGRATION_GITHUB_MODE=live` + `GITHUB_TOKEN` + `GITHUB_REPO` |
| **Canary Orchestrator** | LIVE-CAPABLE | Mock (default); live Harness via `INTEGRATION_HARNESS_MODE=live`; `SAFETY_ALLOW_LIVE_DEPLOYS=false` dry-run by default |
| **SLO Sentinel** | LIVE-CAPABLE | Mock (default); live CloudWatch via `INTEGRATION_AWS_MODE=live`; falls back to mock on any boto3 error |
| **Compliance Auditor** | WORKING | Zero external tools; OPA embedded; all tests pass |
| **Release Scribe** | NEEDS-LIVE-CREDENTIALS | Requires `OPENAI_API_KEY`; Confluence live: set `INTEGRATION_CONFLUENCE_MODE=live` + Atlassian vars |
| **OPA Policy Engine (embedded)** | WORKING | Evaluates `release_guardrails.rego` in-process; no server needed |
| **OPA Policy Engine (server)** | NEEDS-LIVE-CREDENTIALS | Requires OPA server at `OPA_URL`; 8 tests skipped without it |
| **Mock AWS Server** | WORKING | Starts cleanly; serves timeline metrics from scenario YAML |
| **PCI/PII Redactor** | WORKING | Luhn-validated PAN detection; CVV, email, CDE class redaction |
| **Prompt Injection Sanitizer** | WORKING | Pattern-based; all sanitizer tests pass |
| **OpenTelemetry (file mode)** | WORKING | Default when Jaeger absent; writes `traces/last_run.txt` + JSONL; no Docker required |
| **OpenTelemetry (OTLP)** | WORKING-WITH-MOCKS | Requires Jaeger or OTEL Collector; `OTEL_MODE=otlp`; falls back to file mode automatically |
| **GitHub integration** | LIVE-CAPABLE | `INTEGRATION_GITHUB_MODE=live`; fails gracefully to fixture diff |
| **Confluence integration** | LIVE-CAPABLE | `INTEGRATION_CONFLUENCE_MODE=live`; fails gracefully to local Markdown |
| **Teams Notification** | NEEDS-LIVE-CREDENTIALS | Requires `TEAMS_WEBHOOK_URL`; skipped gracefully when absent |
| **Harness (demo mode)** | WORKING | Mock by design; no credentials needed; deterministic responses |
| **Web Dashboard** | WORKING | FastAPI on port 9100; real-time WebSocket event stream |
| **Demo Runner CLI** | WORKING-WITH-MOCKS | `demo_runner.py` orchestration correct; blocked at LLM call without `OPENAI_API_KEY` |
| **Test Suite** | WORKING | 63/71 pass; 8 skipped (OPA live mode) |
| **Docker Compose** | OPTIONAL | `jaeger`, `opa`, `aws-mock` services defined; all three also run without Docker |
| **start_demo.sh** | WORKING | Auto-detects Docker; falls back to no-Docker path (Mock AWS + OPA embedded + file traces) |
| **start_demo_nodocker.sh** | WORKING | Explicit no-Docker path; same as start_demo.sh no-Docker branch |
| **scripts/install_jaeger_binary.sh** | WORKING | Downloads `jaeger-all-in-one` to `./bin/`; optional for OTLP mode without Docker |

---

## Quick-Start Checklist

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure credentials
cp .env.example .env
# Edit .env — at minimum set OPENAI_API_KEY

# 3. Start the demo (Docker optional — auto-detected)
./start_demo.sh
# Or explicitly no-Docker:
./start_demo_nodocker.sh

# 4. Run tests (all non-live tests pass without credentials)
python3.11 -m pytest tests/ -v

# 5. Run a scenario
python demo_runner.py --scenario 1
```

Minimum viable demo requires only `OPENAI_API_KEY`. Everything else uses mock/embedded fallbacks. Docker is optional.
