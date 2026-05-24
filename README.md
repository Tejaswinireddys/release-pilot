# Release Pilot

Release Pilot is a 5-agent AI system that takes over the moment a pull request is merged and owns the dangerous last mile of the software delivery lifecycle: it assesses deployment risk against RAG-augmented historical incidents, enforces PCI-DSS and SOX compliance policy via OPA, orchestrates a canary rollout through Harness, monitors SLOs in real time and rolls back automatically if they breach, then produces a frozen audit packet and a Confluence release page — all without human intervention unless the risk profile demands it. The demo ships with two placeholder services (ServiceA, a PCI-scoped payment processor, and ServiceB, a non-PCI notification service); any team can swap these out via `config/service_graph.json` in under five minutes.

---

## Architecture

```
PR merged → Webhook (port 9000)
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

The system is built in seven layers:

| Layer | What it does |
|-------|-------------|
| **Orchestrator** | Thin A2A coordinator; emits structured JSON envelopes; propagates `trace_id` |
| **Agent layer** | Five single-purpose agents, each with a least-privilege tool set |
| **OPA Policy Engine** | Evaluates `policies/release_guardrails.rego` before every tool call |
| **PCI/PII Redactor** | Strips card numbers, CVVs, and emails before every LLM call |
| **Prompt Injection Sanitizer** | Cleans diff bodies of injection attempts before ingestion |
| **Knowledge layer** | sqlite-vec RAG index + deployment memory; seeded from `docs/` |
| **Observability** | OpenTelemetry GenAI spans → Jaeger (OTLP gRPC, port 4317) |

No architecture diagram is checked in to this repository; if you add one, place it at `docs/architecture.png` and it will be picked up by the reference above.

---

## Folder Structure

```
release-pilot/
├── .env.example              # Credential template — copy to .env and fill in
├── .github/agents/           # Agent definition files (Copilot 2026 .agent.md spec)
├── config/
│   └── service_graph.json    # Service topology: PCI scope, blast radius, consumers
├── demo_runner.py            # Rich CLI — runs any scenario YAML end-to-end
├── docker-compose.yaml       # Jaeger + OPA + Mock AWS — one command demo environment
├── Dockerfile                # Image for the Mock AWS service
├── docs/                     # Architecture diagrams and supplementary docs
├── mcp-config.yaml           # MCP server manifest (GitHub, Atlassian, Mock AWS)
├── policies/
│   └── release_guardrails.rego  # OPA Rego v1 policy — the single source of truth
├── requirements.txt          # Python dependencies
├── scenarios/                # YAML scenario files that drive the demo
├── src/
│   ├── agents/               # Five agent implementations (one file each)
│   ├── knowledge/            # RAG index, deployment memory, service graph loader
│   ├── orchestrator.py       # Orchestrator + FastAPI webhook server
│   ├── opa_client.py         # OPA client (live server + embedded Rego fallback)
│   ├── redactor.py           # PCI/PII redactor (Luhn-validated PAN detection)
│   ├── sanitizer.py          # Prompt injection sanitizer
│   ├── telemetry.py          # OpenTelemetry setup and span helpers
│   └── tools/                # External service clients (Harness, Mock AWS, Teams)
├── start_demo.sh             # One-command demo environment startup
├── STATUS.md                 # End-to-end component status report
└── tests/                    # pytest suite — 63 tests, 0 failures
```

---

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.11+ | 3.11 recommended; 3.12+ works |
| Docker + Docker Compose | 24+ | Required for `start_demo.sh` (Jaeger, OPA, Mock AWS) |
| Node.js | 18+ | Required only for live MCP servers (pulled via `npx`) |
| OpenAI-compatible API | — | **Live mode only.** The demo runs fully on mocks without this. |
| GitHub token | — | **Live mode only.** Falls back to synthetic PR diff without it. |
| Atlassian API token | — | **Live mode only.** Falls back to Markdown output without it. |

> **The demo runs fully on mocks and embedded fallbacks.** The only credential required to run `demo_runner.py` against real LLM output is `OPENAI_API_KEY`. Everything else (Harness, OPA, Mock AWS, GitHub diff fetch) works without credentials.

---

## Setup & Installation

1. **Clone the repository**

   ```bash
   git clone https://github.com/<USERNAME>/release-pilot.git
   cd release-pilot
   ```

2. **Create and activate a virtual environment**

   ```bash
   python3.11 -m venv .venv
   source .venv/bin/activate      # macOS / Linux
   # .venv\Scripts\activate       # Windows
   ```

3. **Install Python dependencies**

   ```bash
   pip install -r requirements.txt
   ```

4. **Copy the credential template**

   ```bash
   cp .env.example .env
   ```

5. **Fill in credentials** — open `.env` and set at minimum:

   ```
   OPENAI_API_KEY=sk-...        # required for live LLM calls
   ```

   All other values have working defaults for demo mode. See the [Environment Configuration](#environment-configuration) section for the full table.

6. **Start the demo environment** (Jaeger + OPA + Mock AWS + knowledge seeding)

   ```bash
   ./start_demo.sh
   ```

---

## Environment Configuration

| Variable | Purpose | Required for live? | Default / Example |
|----------|---------|-------------------|-------------------|
| `OPENAI_API_KEY` | LLM calls (Risk Analyst, Release Scribe) | Yes | `sk-...` |
| `OPENAI_BASE_URL` | OpenAI-compatible endpoint | No | `https://api.openai.com/v1` |
| `AGENT_MODEL` | Model name for all agents | No | `gpt-4o` |
| `EMBEDDING_MODEL` | Embedding model for RAG index | No | `text-embedding-3-large` |
| `GITHUB_TOKEN` | GitHub MCP — PR diff fetch | No (synthetic fallback) | `ghp_...` |
| `ATLASSIAN_API_TOKEN` | Atlassian MCP — Confluence / Jira | No (Markdown fallback) | `ATATT3x...` |
| `ATLASSIAN_SITE_URL` | Your Atlassian cloud URL | No | `https://your-org.atlassian.net` |
| `CONFLUENCE_SPACE_KEY` | Target Confluence space | No | `ENG` |
| `JIRA_PROJECT_KEY` | Target Jira project for comments | No | `PLAT` |
| `OPA_URL` | Live OPA server endpoint | No (embedded fallback) | `http://localhost:8181` |
| `AWS_MOCK_URL` | Mock AWS MCP server URL | No | `http://localhost:8080` |
| `TEAMS_WEBHOOK_URL` | Teams notification webhook | No | `https://outlook.office.com/webhook/...` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Jaeger / OTLP collector | No (silent drop) | `http://localhost:4317` |
| `OTEL_SERVICE_NAME` | Service name in traces | No | `release-pilot` |
| `SENTINEL_POLL_INTERVAL_SECONDS` | How often SLO Sentinel polls CloudWatch | No | `30` |
| `DEMO_MODE` | Enables Harness mock, fast bake window | No | `true` |
| `DEMO_APPROVAL_TOKEN` | Pre-set human approval token (skips poll) | No | any string |
| `DEMO_APPROVAL_TIMEOUT_SECONDS` | Max seconds to wait for approval | No | `30` |
| `LOG_LEVEL` | Python logging level | No | `INFO` |

---

## Running Locally

### Demo mode (recommended for first run)

```bash
# 1. Bring up the demo environment
./start_demo.sh

# 2. Run scenarios — each shows a different pipeline outcome
python demo_runner.py --scenario 1   # ServiceA healthy → PROMOTED
python demo_runner.py --scenario 3   # Error-rate spike → ROLLED_BACK
python demo_runner.py --scenario 4   # PCI scope, no approver → BLOCKED
python demo_runner.py --scenario 6   # ServiceB (non-PCI) → PROMOTED

# Scenario path can also be given directly:
python demo_runner.py -s scenarios/scenario_06_serviceb_healthy.yaml
```

The demo runner streams Rich-formatted output live as the pipeline progresses, then prints a summary table with risk score, SLO verdict, audit hash, and Jaeger trace link.

### Webhook server mode

```bash
python src/orchestrator.py --server
# Listens on http://localhost:9000
```

Simulate a PR merge:

```bash
curl -X POST http://localhost:9000/webhook/pr-merged \
  -H "Content-Type: application/json" \
  -d '{"repository":{"name":"servicea"},"pull_request":{"number":42}}'
```

### Hot-swap scenario during a running demo

```bash
curl -X POST http://localhost:8080/scenario/load \
  -H "Content-Type: application/json" \
  -d '{"scenario_file":"scenarios/scenario_03_error_rate_spike.yaml"}'
```

---

## Running in "Production" Mode

Release Pilot is a demonstration project. "Production" mode means running against real OpenAI and real Atlassian instead of mocks — actual Harness and AWS deployment targets are intentionally outside scope.

To switch from demo to live:

1. Set `OPENAI_API_KEY` to a real key (or point `OPENAI_BASE_URL` at Azure OpenAI / local vLLM).
2. Set `GITHUB_TOKEN` — the Risk Analyst will fetch real PR diffs instead of the synthetic one.
3. Set `ATLASSIAN_API_TOKEN` + `ATLASSIAN_SITE_URL` — the Release Scribe will publish real Confluence pages.
4. Set `DEMO_MODE=false` — Harness mock is disabled; set `HARNESS_API_KEY` + `HARNESS_BASE_URL`.
5. Point `OPA_URL` at your OPA server if you prefer server mode over the embedded fallback.

Everything else (OPA policy evaluation, PCI redaction, audit packet generation, telemetry) is identical between demo and live mode.

---

## Testing

```bash
python -m pytest tests/ -v
```

| Test file | What it covers |
|-----------|----------------|
| `test_integration.py` | 31 end-to-end integration tests across all 5 agents and the orchestrator |
| `test_opa_policies.py` | 9 OPA policy tests (embedded + live server; live skipped without OPA running) |
| `test_redactor.py` | 13 PCI/PII redactor tests (Luhn, CVV, email, CDE class, idempotency, performance) |
| `test_sanitizer.py` | 5 prompt injection sanitizer tests |
| `test_service_graph.py` | 3 service graph tests (blast radius, two-hop traversal, PCI shared-lib detection) |

**Results:** 63 passed, 8 skipped (OPA live-server tests), 0 failed.

Key integration scenarios validated by the test suite:

- Full happy-path deploy end-to-end (mocked LLM + Harness + OPA)
- Rollback triggered by two consecutive degraded SLO intervals
- PCI scope detected → OPA denies deployment → `BLOCKED` status
- PAN injected in diff → redactor strips it before LLM prompt → verified absent
- Two-hop blast radius computed from service graph
- PCI scope detected via shared-library import path
- `AuditPacket` is immutable — mutation raises `ValidationError`
- Low-confidence ROLLBACK overridden to ESCALATE, then loop continues to PROMOTE

---

## API / Service Configuration

### Orchestrator (FastAPI, port 9000)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/webhook/pr-merged` | Receive GitHub PR-merged webhook; returns `release_id` + `trace_id` |
| `GET` | `/release/{release_id}/status` | Poll pipeline status, current stage, and full A2A event stream |
| `POST` | `/release/{release_id}/approve` | Submit human approval token to unblock a paused pipeline |

**Webhook body example:**
```json
{
  "repository": { "name": "servicea" },
  "pull_request": { "number": 42 }
}
```

**Approve body example:**
```json
{
  "token": "APPROVAL-abc123",
  "approved_by": "alice@example.com",
  "via": "slack"
}
```

### Mock AWS Server (FastAPI, port 8080)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check — returns scenario name |
| `GET` | `/cloudwatch/metrics/{service_name}` | Advance timeline; return next metric point |
| `GET` | `/cloudwatch/baseline/{service_name}` | Return 7-day baseline window |
| `GET` | `/cloudwatch/alarms/{service_name}` | Return alarm state |
| `GET` | `/ecs/services/{service_name}` | ECS service description |
| `POST` | `/ecs/services/update` | Deploy new task definition |
| `POST` | `/ecs/task-definitions` | Register task definition |
| `POST` | `/ecs/traffic` | Update canary traffic weight |
| `POST` | `/ecs/rollback/{service_name}` | Roll back canary |
| `GET` | `/ecr/images/{service_name}/latest` | Latest ECR image metadata |
| `POST` | `/scenario/load` | Hot-swap scenario YAML without restart |
| `GET` | `/scenario/current` | Current scenario name and timeline position |
| `POST` | `/scenario/reset` | Reset timeline index to 0 |

### OPA Policy Engine (port 8181 — server mode, or embedded)

OPA evaluates `policies/release_guardrails.rego` before every tool call. When the OPA server is unreachable, the Python client falls back to embedded evaluation of the same Rego file automatically — no policy drift.

Denial reasons emitted by the policy:

| Reason | Condition |
|--------|-----------|
| `HIGH_RISK_NO_APPROVAL` | `risk_level=HIGH` and no human approval token |
| `PCI_NO_APPROVAL` | `pci_scope_touched=true` and no human approval token |
| `IAM_MULTI_APPROVAL` | IAM change with fewer than 2 approval chain entries |
| `CANARY_CAP_50PCT` | Canary percentage > 50 in the first 30 minutes |
| `LOW_CONFIDENCE_BLOCK` | Confidence below the success-criteria threshold |
| `METRICS_DEGRADED_BLOCK` | Metrics already degraded at promote time |

### MCP Servers (configured in `mcp-config.yaml`)

| Server | Package | Used by |
|--------|---------|---------|
| GitHub MCP | `npx @modelcontextprotocol/server-github` | Risk Analyst — PR diff, file contents |
| Atlassian Rovo MCP | `npx @atlassian/mcp-atlassian` | Release Scribe — Confluence pages, Jira comments |
| Mock AWS MCP | Local FastAPI (`src/tools/aws_mock_server.py`) | Canary Orchestrator (ECS), SLO Sentinel (CloudWatch) |

---

## Troubleshooting

**1. `CLAUDE.md` symlink broken on Windows**

`CLAUDE.md` is a symlink to `AGENTS.md`. Git on Windows may check it out as a text file containing the path `AGENTS.md` rather than a real symlink. Fix:

```bash
# In Git Bash (run as administrator):
git config core.symlinks true
git checkout CLAUDE.md
```

Alternatively, replace the symlink with a plain copy: `cp AGENTS.md CLAUDE.md`.

---

**2. `sqlite-vec` install fails (`pip install` error)**

`sqlite-vec` requires a system SQLite with the `vec` extension. On macOS:

```bash
brew install sqlite
LDFLAGS="-L$(brew --prefix sqlite)/lib" \
CPPFLAGS="-I$(brew --prefix sqlite)/include" \
pip install sqlite-vec
```

On Linux, ensure `libsqlite3-dev` is installed before `pip install`.

---

**3. OPA server not reachable (`opa.live_unavailable` in logs)**

This is not an error — the OPA client automatically falls back to embedded Rego evaluation. You will see the log line:

```
opa.live_unavailable — falling back to embedded evaluation
```

The same `policies/release_guardrails.rego` file is evaluated in both modes. To run OPA as a server:

```bash
docker-compose up -d opa
# or standalone:
opa run --server --addr :8181 policies/release_guardrails.rego
```

---

**4. Docker not running / `docker-compose up` fails**

Start Docker Desktop first, or verify the daemon is running:

```bash
docker info
```

If individual services fail, check logs:

```bash
docker-compose logs jaeger
docker-compose logs opa
docker-compose logs aws-mock
```

---

**5. Port conflicts (8080, 8181, 9000, 16686)**

If a port is already in use:

```bash
# Find what's using the port (e.g., 8080):
lsof -i :8080

# Override in docker-compose.yaml — change the host-side port:
#   "18080:8080"    ← host port 18080 maps to container 8080

# Then update .env to match:
AWS_MOCK_URL=http://localhost:18080
```

---

**6. OpenAI rate limits during the demo**

The Risk Analyst retries once on `RateLimitError` with a 5-second backoff. If you hit sustained rate limits:

- Reduce `AGENT_MODEL` to `gpt-4o-mini` (faster, cheaper, lower rate limits)
- Point `OPENAI_BASE_URL` at a local vLLM or Ollama endpoint with `AGENT_MODEL` set to the local model name

```bash
OPENAI_BASE_URL=http://localhost:11434/v1 OPENAI_API_KEY=ollama \
  AGENT_MODEL=mistral python demo_runner.py --scenario 1
```

---

**7. Atlassian authentication failure**

When `ATLASSIAN_API_TOKEN` is invalid or expired, the Release Scribe falls back to writing a Markdown file in `/tmp/release_pilot_pages/` and emits `confluence_page_url=None`. The pipeline still completes successfully — only the Confluence publish step is skipped.

To verify your token works before the demo:

```bash
curl -u "your-email@example.com:$ATLASSIAN_API_TOKEN" \
  "https://your-org.atlassian.net/wiki/rest/api/space" | python -m json.tool | head -20
```

---

**8. RAG returns empty results / Risk Analyst re-prompts**

If `RAGIndex` was not seeded, the Risk Analyst will re-prompt the LLM once and may fall back to synthetic references. To seed:

```bash
# Seed with bundled demo documents:
python -c "from src.knowledge.rag_index import RAGIndex; RAGIndex().seed_demo_data()"

# Or index your own documentation:
python -c "
from src.knowledge.rag_index import RAGIndex
RAGIndex().build_from_directory('./docs')
"
```

You can verify the index is populated:

```bash
python -c "
from src.knowledge.rag_index import RAGIndex
r = RAGIndex()
results = r.query('payment processor incident', top_k=3)
print(f'{len(results)} results found')
for res in results: print(' -', res.doc_id, f'score={res.similarity_score:.2f}')
"
```

---

## Verification Checklist

Run through this list before a live demo or handoff:

- [ ] `python -m pytest tests/ -v` — 63 passed, 0 failed (8 OPA-live skips are expected)
- [ ] `./start_demo.sh` — all three services reach "ready" within 30 seconds
- [ ] `python demo_runner.py --scenario 1` — pipeline reaches `PROMOTED`, Jaeger trace visible at `http://localhost:16686`
- [ ] `python demo_runner.py --scenario 3` — SLO Sentinel emits `ROLLBACK`, rollback event visible in summary
- [ ] `python demo_runner.py --scenario 4` — OPA denial `PCI_NO_APPROVAL` visible in output, pipeline status `BLOCKED`
- [ ] `python demo_runner.py --scenario 6` — ServiceB (non-PCI) reaches `PROMOTED`, PCI controls shown as `N/A`
- [ ] Scenario 3 summary shows non-empty `Audit Hash` (SHA-256 prefix visible)
- [ ] PAN test: inject `4111111111111111` into a scenario diff field — confirm it is absent from LLM prompt (check `test_redactor_protects_llm_prompts` passes)
- [ ] Confluence page URL appears in scenario 1 summary (or Markdown fallback path in `/tmp/release_pilot_pages/` if Atlassian creds not set)
- [ ] Scenario 6 shows `PCI Scope: no` and `PCI Controls: N/A` in the summary table — demonstrates non-PCI reusability
