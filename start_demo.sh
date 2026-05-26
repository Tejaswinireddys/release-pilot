#!/usr/bin/env bash
set -e

echo "Starting Release Pilot demo environment..."
docker-compose up -d

# Wait for a service to become healthy (max 30 seconds), print "ready" when up.
wait_for() {
    local name="$1" url="$2" elapsed=0
    printf "  Waiting for %s..." "$name"
    while ! curl -sf "$url" > /dev/null 2>&1; do
        sleep 2
        elapsed=$((elapsed + 2))
        if [ "$elapsed" -ge 30 ]; then
            echo " TIMEOUT"
            echo "ERROR: $name did not become ready within 30s (tried $url)" >&2
            exit 1
        fi
        printf "."
    done
    echo " ready"
}

wait_for "Jaeger"   "http://localhost:16686/"
wait_for "OPA"      "http://localhost:8181/health"
wait_for "Mock AWS" "http://localhost:8080/health"

# Seed the knowledge layer so the Risk Analyst has RAG context and deployment history.
python -c "from src.knowledge.rag_index import RAGIndex; RAGIndex().seed_demo_data()"
python -c "from src.knowledge.memory_store import DeploymentMemory; DeploymentMemory().seed_demo_history()"

# Start the dashboard server in the background.
python -m uvicorn src.dashboard.server:app --port 9100 --log-level warning &
wait_for "Dashboard" "http://localhost:9100/"

echo ""
echo "Release Pilot demo ready!"
echo "  Dashboard:  http://localhost:9100"
echo "  Jaeger UI:  http://localhost:16686"
echo "  OPA:        http://localhost:8181"
echo "  Mock AWS:   http://localhost:8080/health"
echo ""
echo "Open http://localhost:9100 in your browser and click 'Start Demo'."
echo "Or run from the terminal: python demo_runner.py --scenario 3"
