"""
Harness CI/CD platform client.

In DEMO_MODE=true, all calls return deterministic mock data so the pipeline
works without Harness credentials or a live environment.
"""

from __future__ import annotations

import os
import random
import time
import uuid
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)


class HarnessClient:
    def __init__(self) -> None:
        self._base_url = os.getenv("HARNESS_BASE_URL", "http://localhost:9001").rstrip("/")
        self._api_key = os.getenv("HARNESS_API_KEY", "mock-key")
        self._demo = os.getenv("DEMO_MODE", "true").lower() == "true"
        self._http = httpx.AsyncClient(timeout=30.0, headers={"x-api-key": self._api_key})

    async def deploy(self, service_id: str, image_tag: str, pipeline_id: str | None = None) -> dict[str, Any]:
        if self._demo:
            deployment_id = f"deploy-{uuid.uuid4().hex[:8]}"
            log.info("harness.deploy.mock", service_id=service_id, image_tag=image_tag, deployment_id=deployment_id)
            return {"deployment_id": deployment_id, "status": "RUNNING", "pipeline_id": pipeline_id or "mock-pipeline"}
        resp = await self._http.post(
            f"{self._base_url}/api/v1/deployments",
            json={"serviceId": service_id, "imageTag": image_tag, "pipelineId": pipeline_id},
        )
        resp.raise_for_status()
        return resp.json()

    async def rollback(self, deployment_id: str, reason: str = "") -> dict[str, Any]:
        if self._demo:
            log.info("harness.rollback.mock", deployment_id=deployment_id, reason=reason)
            return {"deployment_id": deployment_id, "status": "ROLLED_BACK", "reason": reason}
        resp = await self._http.post(
            f"{self._base_url}/api/v1/deployments/{deployment_id}/rollback",
            json={"reason": reason},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_pipeline_status(self, deployment_id: str) -> dict[str, Any]:
        if self._demo:
            return {"deployment_id": deployment_id, "status": "SUCCEEDED", "duration_ms": random.randint(30_000, 180_000)}
        resp = await self._http.get(f"{self._base_url}/api/v1/deployments/{deployment_id}")
        resp.raise_for_status()
        return resp.json()

    async def cancel(self, deployment_id: str) -> dict[str, Any]:
        if self._demo:
            return {"deployment_id": deployment_id, "status": "CANCELLED"}
        resp = await self._http.post(f"{self._base_url}/api/v1/deployments/{deployment_id}/cancel")
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        await self._http.aclose()
