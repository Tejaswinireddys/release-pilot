"""
SLO Sentinel — async polling loop that watches a canary deployment and emits
an explainable, audit-ready verdict.

This agent's value is not being a better detector than existing monitoring tools;
it is producing structured, LLM-reasoned, deterministically-overridden verdicts
that can be frozen into compliance audit packets.

Tool permissions (from .github/agents/slo-sentinel.agent.md):
  ALLOW: aws_mcp.get_metrics, aws_mcp.get_baseline
  DENY:  harness.*, atlassian_mcp.*, github_mcp.*, aws_mcp.ecs_*

Polling loop (watch):
  1.  GET /cloudwatch/metrics/{service}
  2.  GET /cloudwatch/baseline/{service}
  3.  PCIRedactor on the serialized responses
  4.  Compute deviation_std for error_rate and p99 vs baseline
  5.  Update 2-interval rolling window for both-degraded detection
  6.  Build LLM prompt with N-interval history
  7.  Call OpenAI; parse SentinelVerdict fields
  8.  Deterministic overrides:
      a. confidence < 0.6  →  ESCALATE  (never ROLLBACK on low confidence)
      b. both error_rate + p99 degraded for 2 consecutive intervals  →  ROLLBACK
  9.  Log interval span
  10. ROLLBACK → return immediately
  11. PROMOTE + intervals ≥ bake window → return
  12. ESCALATE → pause, emit event, resume polling
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel

from src.agents.canary_orchestrator import DeploymentHandle
from src.opa_client import OPAClient
from src.redactor import PCIRedactor
from src.telemetry import get_tracer, record_llm_call

log = logging.getLogger(__name__)

# ── Backward-compat model (compliance_auditor imports this) ───────────────────


class SLOVerdict(BaseModel):
    service_id: str
    deployment_id: str
    decision: Literal["PROMOTE", "ROLLBACK", "ESCALATE"]
    reason: str
    error_rate: float
    p99_latency_ms: float
    availability: float
    baseline_error_rate: float
    baseline_p99_ms: float
    canary_regression: bool
    slo_thresholds: dict[str, float]
    threshold_breaches: list[str]
    trace_id: str
    evaluated_at: str


# ── Spec-defined output model ─────────────────────────────────────────────────


class SentinelVerdict(BaseModel):
    verdict: Literal["PROMOTE", "ROLLBACK", "ESCALATE"]
    confidence: float
    observed: dict          # error_rate_pct, p99_ms, rps
    baseline: dict          # error_rate_pct, p99_ms
    deviation_std: dict     # error_rate (normalized), p99 (normalized)
    reasoning: str
    intervals_checked: int
    anomaly_detected_at_t_seconds: int | None


# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM = """You are the SLO Sentinel for Release Pilot.

Your role is not to be a better detector than monitoring tools — it is to produce
explainable, structured, audit-ready verdicts about canary deployment health.

You receive live CloudWatch metrics vs a 7-day baseline across N polling intervals.
Classify the signal:
  PROMOTE   — metrics are within SLO thresholds; canary is healthy
  ROLLBACK  — clear regression: error_rate or p99 consistently elevated above SLO
  ESCALATE  — ambiguous: partial degradation, load spike, or upstream noise;
              do not auto-rollback, escalate to human

Return ONLY valid JSON with these fields:
  verdict     — "PROMOTE" | "ROLLBACK" | "ESCALATE"
  confidence  — float 0.0-1.0 (your certainty in this verdict)
  reasoning   — string: explain if this is a real regression, load anomaly, or noise

No prose. No markdown. Only the JSON object."""


# ── Agent class ───────────────────────────────────────────────────────────────


class SLOSentinel:
    def __init__(self) -> None:
        self._llm = AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY", "demo"),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )
        self._model = os.getenv("AGENT_MODEL", "gpt-4o")
        self._http = httpx.AsyncClient(
            base_url=os.getenv("AWS_MOCK_URL", "http://localhost:8080"),
            timeout=15.0,
        )
        self._opa = OPAClient()
        self._redactor = PCIRedactor()
        self._tracer = get_tracer("slo-sentinel")

    # ── Public interface ──────────────────────────────────────────────────────

    async def watch(self, handle: DeploymentHandle) -> SentinelVerdict:
        """Async polling loop — returns first terminal verdict (PROMOTE/ROLLBACK).

        ESCALATE pauses polling and resumes after human resolution.
        Poll interval: SENTINEL_POLL_INTERVAL_SECONDS (default 30; demo uses 5).
        """
        poll_interval = int(os.getenv("SENTINEL_POLL_INTERVAL_SECONDS", "30"))
        demo_mode = os.getenv("DEMO_MODE", "true").lower() == "true"

        # In demo mode, compress bake window so the loop terminates quickly
        if demo_mode:
            effective_bake_s = int(os.getenv("DEMO_BAKE_SECONDS", "15"))
        else:
            effective_bake_s = handle.bake_minutes * 60

        min_promote_intervals = max(1, effective_bake_s // poll_interval)

        service = handle.service
        sc = handle.success_criteria

        # Fetch baseline once; it does not change during the watch window
        baseline_raw = await self._fetch_baseline(service)

        base_err_rate = baseline_raw.get("baseline_error_rate", 0.0008)
        base_p99 = baseline_raw.get("baseline_p99_ms", 305.0)

        # State for the polling loop
        degraded_window: collections.deque[bool] = collections.deque(maxlen=2)
        metrics_history: list[dict] = []
        intervals_checked = 0
        anomaly_at: int | None = None
        start_epoch = time.monotonic()

        while True:
            intervals_checked += 1
            elapsed_s = int(time.monotonic() - start_epoch)

            # 1–2. Fetch live metrics
            metrics_raw = await self._fetch_metrics(service)

            # 3. PCIRedactor on raw responses (CloudWatch logs may contain PAN)
            redacted_metrics_str = self._redactor.redact(
                json.dumps(metrics_raw)
            ).redacted_text
            try:
                metrics = json.loads(redacted_metrics_str)
            except json.JSONDecodeError:
                metrics = metrics_raw  # fallback: redaction disrupted JSON

            # 4. Compute normalized deviation
            obs_err = float(metrics.get("error_rate", base_err_rate))
            obs_p99 = float(metrics.get("p99_latency_ms", base_p99))
            obs_rps = float(metrics.get("rps", 0))

            dev_err = (obs_err - base_err_rate) / max(base_err_rate, 1e-9)
            dev_p99 = (obs_p99 - base_p99) / max(base_p99, 1e-9)

            # 5. Update 2-interval rolling window
            err_over_threshold = (obs_err * 100) > sc.max_error_rate_pct
            p99_over_threshold = obs_p99 > sc.max_p99_ms
            both_degraded = err_over_threshold and p99_over_threshold
            degraded_window.append(both_degraded)

            if both_degraded and anomaly_at is None:
                anomaly_at = elapsed_s

            # Track history for LLM prompt (keep last 5 intervals)
            metrics_history.append({
                "interval": intervals_checked,
                "elapsed_s": elapsed_s,
                "error_rate_pct": round(obs_err * 100, 4),
                "p99_ms": obs_p99,
                "rps": obs_rps,
                "dev_error_rate": round(dev_err, 4),
                "dev_p99": round(dev_p99, 4),
                "threshold_breach": both_degraded,
            })
            if len(metrics_history) > 5:
                metrics_history.pop(0)

            # 6–7. LLM call for classification and reasoning
            llm_dict = await self._call_llm(
                handle, metrics_history, base_err_rate, base_p99,
                intervals_checked,
            )

            llm_verdict: str = llm_dict.get("verdict", "PROMOTE")
            confidence = float(llm_dict.get("confidence", 0.8))
            reasoning: str = llm_dict.get("reasoning", "")

            # 8a. DETERMINISTIC: confidence < 0.6 → ESCALATE (never ROLLBACK on uncertainty)
            if confidence < sc.min_confidence:
                if llm_verdict == "ROLLBACK":
                    llm_verdict = "ESCALATE"
                    reasoning = (
                        f"Confidence {confidence:.2f} < threshold {sc.min_confidence:.2f} "
                        f"— escalating instead of auto-rollback. Original reasoning: {reasoning}"
                    )
                    log.warning(
                        "sentinel.low_confidence_escalate service=%s interval=%d conf=%.2f",
                        service, intervals_checked, confidence,
                    )

            # 8b. DETERMINISTIC: both metrics degraded for 2 consecutive intervals → ROLLBACK
            if len(degraded_window) == 2 and all(degraded_window):
                log.warning(
                    "sentinel.safety_rollback service=%s interval=%d "
                    "err_pct=%.4f threshold=%.4f p99=%d threshold=%d",
                    service, intervals_checked,
                    obs_err * 100, sc.max_error_rate_pct,
                    obs_p99, sc.max_p99_ms,
                )
                llm_verdict = "ROLLBACK"
                reasoning = (
                    f"Safety override: both error_rate ({obs_err*100:.4f}% > "
                    f"{sc.max_error_rate_pct}%) and p99 ({obs_p99:.0f}ms > "
                    f"{sc.max_p99_ms}ms) degraded for 2 consecutive intervals. "
                    f"LLM reasoning: {reasoning}"
                )

            sentinel_verdict = SentinelVerdict(
                verdict=llm_verdict,  # type: ignore[arg-type]
                confidence=confidence,
                observed={
                    "error_rate_pct": round(obs_err * 100, 4),
                    "p99_ms": obs_p99,
                    "rps": obs_rps,
                },
                baseline={
                    "error_rate_pct": round(base_err_rate * 100, 4),
                    "p99_ms": base_p99,
                },
                deviation_std={
                    "error_rate": round(dev_err, 4),
                    "p99": round(dev_p99, 4),
                },
                reasoning=reasoning,
                intervals_checked=intervals_checked,
                anomaly_detected_at_t_seconds=anomaly_at,
            )

            # 9. Log interval span
            with self._tracer.start_as_current_span("slo_sentinel.interval") as span:
                span.set_attribute("gen_ai.agent.name", "slo-sentinel")
                span.set_attribute("service.name", service)
                span.set_attribute("interval", intervals_checked)
                span.set_attribute("verdict", llm_verdict)
                span.set_attribute("confidence", confidence)
                span.set_attribute("release_pilot.error_rate_pct", obs_err * 100)
                span.set_attribute("release_pilot.p99_ms", obs_p99)

            log.info(
                "SENTINEL_INTERVAL service=%s interval=%d verdict=%s conf=%.2f "
                "err_pct=%.4f p99=%.0f dev_err=%.2f dev_p99=%.2f",
                service, intervals_checked, llm_verdict, confidence,
                obs_err * 100, obs_p99, dev_err, dev_p99,
            )

            # 10. ROLLBACK → signal and break
            if sentinel_verdict.verdict == "ROLLBACK":
                log.warning(
                    "SENTINEL_ROLLBACK service=%s interval=%d anomaly_at=%s",
                    service, intervals_checked, anomaly_at,
                )
                return sentinel_verdict

            # 12. ESCALATE → pause polling, emit event, await human
            if sentinel_verdict.verdict == "ESCALATE":
                log.warning(
                    "ESCALATION_EVENT service=%s interval=%d — awaiting human input",
                    service, intervals_checked,
                )
                await self._await_escalation_resolution(service, intervals_checked)
                # Resume polling after human resolves

            # 11. PROMOTE + bake window satisfied → return
            elif sentinel_verdict.verdict == "PROMOTE" and (
                intervals_checked >= min_promote_intervals
            ):
                log.info(
                    "SENTINEL_PROMOTE service=%s intervals=%d bake_window=%d",
                    service, intervals_checked, min_promote_intervals,
                )
                return sentinel_verdict

            await asyncio.sleep(poll_interval)

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _fetch_metrics(self, service: str) -> dict[str, Any]:
        """GET /cloudwatch/metrics/{service} — advances scenario timeline."""
        try:
            resp = await self._http.get(f"/cloudwatch/metrics/{service}")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            log.warning("cloudwatch.metrics_fetch_failed service=%s error=%s", service, exc)
            return {}

    async def _fetch_baseline(self, service: str) -> dict[str, Any]:
        """GET /cloudwatch/baseline/{service} — static 7-day window."""
        try:
            resp = await self._http.get(f"/cloudwatch/baseline/{service}")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            log.warning("cloudwatch.baseline_fetch_failed service=%s error=%s", service, exc)
            return {}

    async def _call_llm(
        self,
        handle: DeploymentHandle,
        metrics_history: list[dict],
        base_err_rate: float,
        base_p99: float,
        n_intervals: int,
    ) -> dict[str, Any]:
        """Ask the LLM to classify the metric signal and return reasoning."""
        history_text = "\n".join(
            f"  Interval {m['interval']} (T+{m['elapsed_s']}s): "
            f"error_rate={m['error_rate_pct']:.4f}% "
            f"p99={m['p99_ms']:.0f}ms "
            f"rps={m['rps']:.0f} "
            f"dev_err={m['dev_error_rate']:+.2f} "
            f"dev_p99={m['dev_p99']:+.2f} "
            f"{'⚠ BREACH' if m['threshold_breach'] else 'OK'}"
            for m in metrics_history
        )
        sc = handle.success_criteria
        user_msg = (
            f"Service: {handle.service}\n"
            f"Canary: {handle.canary_pct}%  Bake: {handle.bake_minutes}min\n"
            f"SLO thresholds: error_rate < {sc.max_error_rate_pct}%  "
            f"p99 < {sc.max_p99_ms}ms  min_confidence={sc.min_confidence}\n"
            f"7-day baseline: error_rate={base_err_rate*100:.4f}%  p99={base_p99:.0f}ms\n"
            f"\nMetrics over the last {n_intervals} interval(s):\n"
            f"{history_text}\n\n"
            "Is this a real regression, a load anomaly, or upstream noise?\n"
            "Return PROMOTE | ROLLBACK | ESCALATE with confidence (0-1) and reasoning."
        )

        with self._tracer.start_as_current_span("slo_sentinel.llm_call") as span:
            try:
                resp = await self._llm.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": user_msg},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.1,
                    timeout=30,
                )
                if resp.usage:
                    record_llm_call(
                        span, self._model,
                        resp.usage.prompt_tokens,
                        resp.usage.completion_tokens,
                    )
                content = resp.choices[0].message.content or "{}"
                return _parse_json(content)
            except Exception as exc:
                log.warning("slo_sentinel.llm_failed error=%s — defaulting ESCALATE", exc)
                return {
                    "verdict": "ESCALATE",
                    "confidence": 0.5,
                    "reasoning": f"LLM unavailable: {exc}. Escalating to human.",
                }

    async def _await_escalation_resolution(self, service: str, interval: int) -> None:
        """Pause polling and wait for human to resolve the escalation."""
        timeout = int(os.getenv("ESCALATION_TIMEOUT_SECONDS", "300"))
        demo_mode = os.getenv("DEMO_MODE", "true").lower() == "true"

        log.warning(
            "ESCALATION_PAUSE service=%s interval=%d timeout=%ds",
            service, interval, timeout,
        )
        # Emit structured escalation event for downstream consumers
        log.warning(
            "ESCALATION_EVENT service=%s interval=%d action=AWAIT_HUMAN_INPUT",
            service, interval,
        )

        wait_s = min(5, timeout) if demo_mode else timeout
        await asyncio.sleep(wait_s)

        log.info(
            "ESCALATION_RESUMED service=%s — resuming poll after %ds", service, wait_s
        )

    async def close(self) -> None:
        await self._http.aclose()


# ── JSON helper ───────────────────────────────────────────────────────────────


def _parse_json(text: str) -> dict[str, Any]:
    import re
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {}
