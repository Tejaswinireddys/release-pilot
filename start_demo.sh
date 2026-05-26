#!/usr/bin/env bash
# start_demo.sh — Release Pilot demo launcher with automatic Docker detection.
#
# If Docker is available and the daemon is running:
#   → brings up Jaeger + OPA + Mock AWS via docker-compose (full stack)
#
# If Docker is absent or the daemon is not running:
#   → starts Mock AWS directly (python src/tools/aws_mock_server.py)
#   → OPA uses embedded evaluation via the opa binary (no server needed)
#   → Telemetry: if jaeger-all-in-one is on PATH, start it; otherwise file mode
#
# To force the no-Docker path explicitly:   ./start_demo_nodocker.sh
set -euo pipefail

# ── Colour helpers ─────────────────────────────────────────────────────────────
_GREEN='\033[0;32m'; _YELLOW='\033[1;33m'; _RED='\033[0;31m'; _NC='\033[0m'
ok()   { echo -e "${_GREEN}[ok]${_NC}  $*"; }
warn() { echo -e "${_YELLOW}[warn]${_NC} $*"; }
die()  { echo -e "${_RED}[error]${_NC} $*" >&2; exit 1; }

# ── Background process tracking ────────────────────────────────────────────────
PIDS=()
cleanup() {
    if [ ${#PIDS[@]} -gt 0 ]; then
        echo ""
        echo "Stopping background processes..."
        kill "${PIDS[@]}" 2>/dev/null || true
    fi
}
trap cleanup INT TERM EXIT

# ── Health-check poll ──────────────────────────────────────────────────────────
wait_for() {
    local name="$1" url="$2" elapsed=0
    printf "  Waiting for %s..." "$name"
    while ! curl -sf "$url" > /dev/null 2>&1; do
        sleep 2; elapsed=$((elapsed + 2))
        if [ "$elapsed" -ge 40 ]; then
            echo " TIMEOUT"
            die "$name did not become ready within 40s (tried $url)"
        fi
        printf "."
    done
    echo " ready"
}

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Release Pilot — Demo Launcher"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Step 1: Docker detection ───────────────────────────────────────────────────
DOCKER_AVAILABLE=false
if command -v docker &>/dev/null 2>&1; then
    if docker info &>/dev/null 2>&1; then
        DOCKER_AVAILABLE=true
    else
        warn "docker command found but daemon is not running — using no-Docker path"
    fi
fi

# ── Step 2a: Docker path ───────────────────────────────────────────────────────
if [ "$DOCKER_AVAILABLE" = "true" ]; then
    echo "Docker detected — starting Jaeger + OPA + Mock AWS via docker-compose"
    docker-compose up -d

    wait_for "Jaeger"   "http://localhost:16686/"
    wait_for "OPA"      "http://localhost:8181/health"
    wait_for "Mock AWS" "http://localhost:8080/health"

    OPA_STATUS="server  (http://localhost:8181)"
    OTEL_STATUS="otlp    (Jaeger UI → http://localhost:16686)"
    AWS_STATUS="docker  (http://localhost:8080)"
    JAEGER_LINE="  Jaeger UI:  http://localhost:16686"

# ── Step 2b: No-Docker path ────────────────────────────────────────────────────
else
    echo "No Docker — starting services directly"
    echo ""

    # Mock AWS server (plain uvicorn)
    echo "  Starting Mock AWS server..."
    python src/tools/aws_mock_server.py \
        > /tmp/release_pilot_aws_mock.log 2>&1 &
    PIDS+=($!)
    wait_for "Mock AWS" "http://localhost:8080/health"
    AWS_STATUS="python  (http://localhost:8080, log: /tmp/release_pilot_aws_mock.log)"

    # OPA: always embedded — no server to start
    OPA_STATUS="embedded (opa binary; auto-fallback — no server needed)"

    # Jaeger: optional binary
    if command -v jaeger-all-in-one &>/dev/null 2>&1; then
        echo "  Starting Jaeger all-in-one..."
        COLLECTOR_OTLP_ENABLED=true jaeger-all-in-one \
            > /tmp/release_pilot_jaeger.log 2>&1 &
        PIDS+=($!)
        wait_for "Jaeger" "http://localhost:16686/"
        export OTEL_MODE=otlp
        OTEL_STATUS="otlp    (Jaeger UI → http://localhost:16686)"
        JAEGER_LINE="  Jaeger UI:  http://localhost:16686"
    else
        export OTEL_MODE=file
        mkdir -p traces
        OTEL_STATUS="file    (traces/last_run.txt after each run)"
        JAEGER_LINE="  Traces:     ./traces/last_run.txt"
        warn "jaeger-all-in-one not found — traces go to ./traces/last_run.txt"
        warn "Run scripts/install_jaeger_binary.sh for the optional Jaeger binary."
    fi
fi

# ── Step 3: Seed knowledge layer ───────────────────────────────────────────────
echo ""
echo "  Seeding RAG index and deployment memory..."
python -c "from src.knowledge.rag_index import RAGIndex; RAGIndex().seed_demo_data()" \
    2>/dev/null || warn "RAG seed failed (non-fatal)"
python -c "from src.knowledge.memory_store import DeploymentMemory; DeploymentMemory().seed_demo_history()" \
    2>/dev/null || warn "Memory seed failed (non-fatal)"

# ── Step 4: Dashboard ─────────────────────────────────────────────────────────
echo "  Starting dashboard server..."
python -m uvicorn src.dashboard.server:app \
    --port 9100 --log-level warning &
PIDS+=($!)
wait_for "Dashboard" "http://localhost:9100/"

# ── Step 5: Ready message ──────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Release Pilot — Ready"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  OPA:        $OPA_STATUS"
echo "  Telemetry:  $OTEL_STATUS"
echo "  Mock AWS:   $AWS_STATUS"
echo "  Dashboard:  http://localhost:9100"
echo "${JAEGER_LINE}"
echo ""
echo "  Open http://localhost:9100 and click 'Start Demo'."
echo "  Or:  python demo_runner.py --scenario 3"
echo ""
echo "  Press Ctrl+C to stop all background services."
echo ""

# Keep alive so trap fires on Ctrl+C
wait
