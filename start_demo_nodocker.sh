#!/usr/bin/env bash
# start_demo_nodocker.sh — Release Pilot demo launcher, no-Docker path only.
#
# Use this when you don't have Docker or want to skip the auto-detection.
# Exactly equivalent to the no-Docker branch of start_demo.sh.
#
# Services started:
#   Mock AWS  — python src/tools/aws_mock_server.py  (port 8080)
#   OPA       — embedded evaluation via opa binary   (no server)
#   Jaeger    — if jaeger-all-in-one is on PATH; otherwise file mode
#   Dashboard — uvicorn src.dashboard.server:app      (port 9100)
set -euo pipefail

_GREEN='\033[0;32m'; _YELLOW='\033[1;33m'; _RED='\033[0;31m'; _NC='\033[0m'
ok()   { echo -e "${_GREEN}[ok]${_NC}  $*"; }
warn() { echo -e "${_YELLOW}[warn]${_NC} $*"; }
die()  { echo -e "${_RED}[error]${_NC} $*" >&2; exit 1; }

PIDS=()
cleanup() {
    if [ ${#PIDS[@]} -gt 0 ]; then
        echo ""; echo "Stopping background processes..."
        kill "${PIDS[@]}" 2>/dev/null || true
    fi
}
trap cleanup INT TERM EXIT

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
echo "  Release Pilot — No-Docker Demo Launcher"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Mock AWS
echo "  Starting Mock AWS server..."
python src/tools/aws_mock_server.py > /tmp/release_pilot_aws_mock.log 2>&1 &
PIDS+=($!)
wait_for "Mock AWS" "http://localhost:8080/health"
AWS_STATUS="python  (http://localhost:8080, log: /tmp/release_pilot_aws_mock.log)"

# OPA (always embedded)
OPA_STATUS="embedded (opa binary; no server needed)"

# Jaeger (optional)
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
    warn "jaeger-all-in-one not on PATH — traces go to ./traces/last_run.txt"
    warn "Optional: run scripts/install_jaeger_binary.sh to get the binary."
fi

# Seed
echo "  Seeding RAG index and deployment memory..."
python -c "from src.knowledge.rag_index import RAGIndex; RAGIndex().seed_demo_data()" \
    2>/dev/null || warn "RAG seed failed (non-fatal)"
python -c "from src.knowledge.memory_store import DeploymentMemory; DeploymentMemory().seed_demo_history()" \
    2>/dev/null || warn "Memory seed failed (non-fatal)"

# Dashboard
echo "  Starting dashboard server..."
python -m uvicorn src.dashboard.server:app \
    --port 9100 --log-level warning &
PIDS+=($!)
wait_for "Dashboard" "http://localhost:9100/"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Release Pilot — Ready (no-Docker mode)"
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

wait
