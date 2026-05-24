"""
Mock AWS MCP Server — simulates ECS, CloudWatch, and ECR for demo mode.

Reads scenario YAML files (SCENARIO_FILE env var) to drive timeline-based
CloudWatch metrics. Hot-swap scenarios via POST /scenario/load without restart.

Endpoints:
  GET  /cloudwatch/metrics/{service_name}   advance timeline; return next point
  GET  /cloudwatch/baseline/{service_name}  static baseline window
  GET  /cloudwatch/alarms/{service_name}    alarm state
  GET  /ecs/services/{service_name}         service description
  POST /ecs/services/update                 deploy new task definition
  POST /ecs/task-definitions                register task definition
  POST /ecs/traffic                         update canary traffic weight
  POST /ecs/rollback/{service_name}         roll back canary
  GET  /ecr/images/{service_name}/latest    latest ECR image metadata
  POST /scenario/load                       hot-swap scenario YAML
  GET  /scenario/current                    current scenario + timeline position
  POST /scenario/reset                      reset timeline index to 0
  GET  /health                              {"status": "healthy", "scenario": "<name>"}
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_DEFAULT_SCENARIO = "scenarios/scenario_01_healthy_deploy.yaml"

_lock = threading.Lock()
_state: dict[str, Any] = {
    "scenario": {},
    "timeline_index": 0,
    "services": {},         # service_name → ECS service state
    "task_defs": {},        # family → [arn, ...]
    "traffic_weights": {},  # service_name → canary weight 0.0–1.0
}


def _resolve_scenario_path(path_str: str) -> Path:
    p = Path(path_str)
    if p.is_absolute() and p.exists():
        return p
    cwd_path = Path.cwd() / p
    if cwd_path.exists():
        return cwd_path
    root_path = _PROJECT_ROOT / p
    if root_path.exists():
        return root_path
    raise FileNotFoundError(f"Scenario file not found: {path_str!r}")


def _apply_scenario(path_str: str) -> None:
    resolved = _resolve_scenario_path(path_str)
    with open(resolved) as f:
        data = yaml.safe_load(f)
    with _lock:
        _state["scenario"] = data
        _state["timeline_index"] = 0
    print(f"Mock AWS MCP loaded scenario: {data.get('name', resolved.name)}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    scenario_file = os.getenv("SCENARIO_FILE", _DEFAULT_SCENARIO)
    try:
        _apply_scenario(scenario_file)
    except FileNotFoundError as exc:
        print(f"Warning: {exc} — starting with empty scenario")
    yield


app = FastAPI(
    title="Release Pilot — Mock AWS MCP",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request models ────────────────────────────────────────────────────────────

class ScenarioLoadRequest(BaseModel):
    path: str


class ECSUpdateRequest(BaseModel):
    service_name: str
    task_definition_arn: str
    desired_count: int = 2


class TaskDefRequest(BaseModel):
    family: str
    image: str
    cpu: int = 256
    memory: int = 512


class TrafficRequest(BaseModel):
    service_name: str
    canary_weight: float  # 0.0–1.0


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict[str, Any]:
    with _lock:
        name = _state["scenario"].get("name", "none")
    return {"status": "healthy", "scenario": name}


# ── CloudWatch ────────────────────────────────────────────────────────────────

@app.get("/cloudwatch/metrics/{service_name}")
async def get_metrics(service_name: str, response: Response) -> dict[str, Any]:
    with _lock:
        scenario = _state["scenario"]
        timeline: list[dict] = scenario.get("cloudwatch_timeline", [])
        idx = _state["timeline_index"]

        if timeline:
            point = dict(timeline[min(idx, len(timeline) - 1)])
            _state["timeline_index"] = idx + 1
            elapsed = point.pop("elapsed_seconds", idx * 60)
        else:
            baseline = scenario.get("baseline", {})
            point = {
                "error_rate": baseline.get("error_rate", 0.0008),
                "p99_ms": baseline.get("p99_ms", 305),
                "availability": baseline.get("availability", 0.99994),
                "rps": baseline.get("rps", 220),
            }
            elapsed = idx * 60
            _state["timeline_index"] = idx + 1

        canary_weight = _state["traffic_weights"].get(service_name, 0.0)

    response.headers["X-Scenario-Time-Elapsed-Seconds"] = str(elapsed)

    err = point.get("error_rate", 0.0008)
    return {
        "service_name": service_name,
        "sampled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "error_rate": err,
        "p99_latency_ms": point.get("p99_ms", 305),
        "availability": point.get("availability", 0.99994),
        "rps": point.get("rps", 220),
        "canary_error_rate": point.get("canary_error_rate", err),
        "stable_error_rate": point.get("stable_error_rate", err),
        "canary_weight": canary_weight,
        "timeline_index": idx,
    }


@app.get("/cloudwatch/baseline/{service_name}")
async def get_baseline(service_name: str) -> dict[str, Any]:
    with _lock:
        baseline = _state["scenario"].get("baseline", {})
    return {
        "service_name": service_name,
        "window_days": 7,
        "baseline_error_rate": baseline.get("error_rate", 0.0008),
        "baseline_p99_ms": baseline.get("p99_ms", 305),
        "baseline_availability": baseline.get("availability", 0.99994),
        "baseline_rps": baseline.get("rps", 220),
    }


@app.get("/cloudwatch/alarms/{service_name}")
async def describe_alarms(service_name: str) -> dict[str, Any]:
    with _lock:
        alarms: dict = _state["scenario"].get("alarms", {})
    svc_alarms = {k: v for k, v in alarms.items() if service_name in k}
    in_alarm = [k for k, v in svc_alarms.items() if v == "ALARM"]
    return {"service_name": service_name, "alarms": svc_alarms, "in_alarm": in_alarm}


# ── ECS ───────────────────────────────────────────────────────────────────────

@app.get("/ecs/services/{service_name}")
async def ecs_describe_service(service_name: str) -> dict[str, Any]:
    with _lock:
        svc = _state["services"].get(
            service_name,
            {
                "service_name": service_name,
                "status": "ACTIVE",
                "running_count": 2,
                "desired_count": 2,
            },
        )
    return {"status": "ok", "service": svc}


@app.post("/ecs/services/update")
async def ecs_update_service(req: ECSUpdateRequest) -> dict[str, Any]:
    updated = {
        "service_name": req.service_name,
        "task_definition_arn": req.task_definition_arn,
        "desired_count": req.desired_count,
        "running_count": req.desired_count,
        "status": "ACTIVE",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with _lock:
        _state["services"][req.service_name] = updated
    return {"status": "ok", "service": updated}


@app.post("/ecs/task-definitions")
async def register_task_def(req: TaskDefRequest) -> dict[str, Any]:
    with _lock:
        revisions = _state["task_defs"].setdefault(req.family, [])
        rev = len(revisions) + 1
        arn = f"arn:aws:ecs:us-east-1:123456789012:task-definition/{req.family}:{rev}"
        revisions.append(arn)
    return {"task_definition_arn": arn, "revision": rev, "family": req.family}


@app.post("/ecs/traffic")
async def update_traffic(req: TrafficRequest) -> dict[str, Any]:
    if not 0.0 <= req.canary_weight <= 1.0:
        raise HTTPException(400, "canary_weight must be 0.0–1.0")
    with _lock:
        _state["traffic_weights"][req.service_name] = req.canary_weight
    return {
        "service_name": req.service_name,
        "canary_weight": req.canary_weight,
        "stable_weight": round(1 - req.canary_weight, 4),
    }


@app.post("/ecs/rollback/{service_name}")
async def ecs_rollback(service_name: str) -> dict[str, Any]:
    with _lock:
        _state["traffic_weights"][service_name] = 0.0
        if service_name in _state["services"]:
            _state["services"][service_name]["status"] = "ACTIVE"
    return {
        "status": "ok",
        "service_name": service_name,
        "canary_weight": 0.0,
        "action": "rolled_back",
    }


# ── ECR ───────────────────────────────────────────────────────────────────────

@app.get("/ecr/images/{service_name}/latest")
async def ecr_latest_image(service_name: str) -> dict[str, Any]:
    with _lock:
        version = _state["scenario"].get("version", "latest")
        service_id = _state["scenario"].get("service_id", service_name)
    return {
        "service_name": service_name,
        "repository_uri": f"123456789012.dkr.ecr.us-east-1.amazonaws.com/{service_id}",
        "image_tag": version,
        "image_digest": f"sha256:{'a' * 64}",
        "pushed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# ── Scenario management ───────────────────────────────────────────────────────

@app.post("/scenario/load")
async def scenario_load(req: ScenarioLoadRequest) -> dict[str, Any]:
    try:
        _apply_scenario(req.path)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    with _lock:
        name = _state["scenario"].get("name", req.path)
    return {"status": "loaded", "scenario": name}


@app.get("/scenario/current")
async def scenario_current() -> dict[str, Any]:
    with _lock:
        scenario = _state["scenario"]
        idx = _state["timeline_index"]
        timeline_len = len(scenario.get("cloudwatch_timeline", []))
    return {
        "name": scenario.get("name", "none"),
        "service_id": scenario.get("service_id"),
        "expected_outcome": scenario.get("expected_outcome"),
        "timeline_index": idx,
        "timeline_length": timeline_len,
        "timeline_exhausted": timeline_len > 0 and idx >= timeline_len,
    }


@app.post("/scenario/reset")
async def scenario_reset() -> dict[str, Any]:
    with _lock:
        _state["timeline_index"] = 0
        _state["services"].clear()
        _state["traffic_weights"].clear()
        name = _state["scenario"].get("name", "none")
    return {"status": "reset", "scenario": name, "timeline_index": 0}


if __name__ == "__main__":
    port = int(os.getenv("AWS_MOCK_PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
