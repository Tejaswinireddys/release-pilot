"""
Microsoft Teams webhook client for release-scribe notifications.
Sends Adaptive Card-style message cards to a Teams channel.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

_OUTCOME_COLORS = {
    "PROMOTED": "00AA00",
    "ROLLED_BACK": "FF8800",
    "BLOCKED": "DD0000",
    "FAILED": "880088",
}


class TeamsClient:
    def __init__(self) -> None:
        self._webhook_url = os.getenv("TEAMS_WEBHOOK_URL", "")
        self._demo = os.getenv("DEMO_MODE", "true").lower() == "true"
        self._http = httpx.AsyncClient(timeout=15.0)

    async def post(
        self,
        title: str,
        body: str,
        outcome: str = "PROMOTED",
    ) -> bool:
        color = _OUTCOME_COLORS.get(outcome, "0078D4")

        if self._demo or not self._webhook_url:
            log.info("teams.post.mock", title=title, outcome=outcome)
            return True

        payload: dict[str, Any] = {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "themeColor": color,
            "summary": title,
            "sections": [{"activityTitle": title, "activityText": body, "markdown": True}],
        }
        try:
            resp = await self._http.post(self._webhook_url, json=payload)
            return resp.status_code == 200
        except httpx.HTTPError as exc:
            log.error("teams.post.failed", error=str(exc))
            return False

    async def close(self) -> None:
        await self._http.aclose()
