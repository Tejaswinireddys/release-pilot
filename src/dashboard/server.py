"""Release Pilot web dashboard — FastAPI server on port 9100."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from src.orchestrator import Orchestrator, PipelineRun
from src.telemetry import setup_telemetry

_SCENARIO_MAP: dict[int, str] = {
    1: "scenarios/scenario_01_healthy_deploy.yaml",
    3: "scenarios/scenario_03_error_rate_spike.yaml",
    4: "scenarios/scenario_04_pci_guardrail.yaml",
    6: "scenarios/scenario_06_serviceb_healthy.yaml",
}

_SCENARIO_NAMES: dict[int, str] = {
    1: "Healthy Deploy — ServiceA",
    3: "Error Rate Spike → Rollback",
    4: "PCI Guardrail Block",
    6: "Healthy Deploy — ServiceB",
}

_STATIC = Path(__file__).parent / "static"
_dashboard_runs: dict[str, PipelineRun] = {}

app = FastAPI(title="Release Pilot Dashboard", version="0.1.0")
_orch = Orchestrator()


@app.on_event("startup")
async def _startup() -> None:
    setup_telemetry("release-pilot-dashboard")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


class StartBody(BaseModel):
    scenario: int


class ApproveBody(BaseModel):
    token: str = "dashboard-approve"


@app.post("/demo/start")
async def demo_start(body: StartBody) -> dict[str, Any]:
    scenario_num = body.scenario
    scenario_path_str = _SCENARIO_MAP.get(scenario_num)
    if not scenario_path_str:
        return JSONResponse({"error": f"unknown scenario {scenario_num}"}, status_code=400)

    scenario_path = Path(scenario_path_str)
    if not scenario_path.exists():
        return JSONResponse({"error": f"scenario file not found: {scenario_path}"}, status_code=404)

    raw = yaml.safe_load(scenario_path.read_text())
    run = PipelineRun(
        service_id=raw.get("service_id", "unknown"),
        pr_number=raw.get("pr_number", 0),
    )
    _dashboard_runs[run.release_id] = run
    asyncio.create_task(_orch.run(run))

    return {
        "release_id": run.release_id,
        "trace_id": run.trace_id,
        "service_id": run.service_id,
        "pr_number": run.pr_number,
        "scenario": scenario_num,
        "scenario_name": _SCENARIO_NAMES.get(scenario_num, f"Scenario {scenario_num}"),
    }


@app.post("/demo/approve")
async def demo_approve(body: ApproveBody) -> dict[str, Any]:
    os.environ["DEMO_APPROVAL_TOKEN"] = body.token
    return {"approved": True}


@app.websocket("/ws/release/{release_id}")
async def ws_release(ws: WebSocket, release_id: str) -> None:
    await ws.accept()

    run = _dashboard_runs.get(release_id)
    if not run:
        await ws.send_json({"event": "error", "payload": {"message": f"run {release_id!r} not found"}})
        await ws.close()
        return

    seen = 0
    try:
        while True:
            for evt in run.events[seen:]:
                await ws.send_json(evt)
                seen += 1

            if run.completed_at is not None and seen >= len(run.events):
                await ws.send_json({"event": "ws.done", "payload": {"status": run.status.value}})
                break

            await asyncio.sleep(0.05)
    except WebSocketDisconnect:
        pass
