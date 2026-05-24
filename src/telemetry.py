"""OpenTelemetry setup — GenAI semantic conventions for Release Pilot.

Every agent invocation, LLM call, and tool call is wrapped in a span.
trace_id is set at PR-merge webhook receipt and propagated through every
A2A message via the orchestrator.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_initialized = False
_provider: TracerProvider | None = None


def setup_telemetry(service_name: str | None = None) -> trace.Tracer:
    """Configure OTLP exporter to Jaeger and return a root tracer.

    OTEL_EXPORTER_OTLP_ENDPOINT overrides the endpoint; default http://localhost:4317.
    Resource attributes: service.name="release-pilot", service.version="0.1.0".
    """
    global _initialized, _provider
    if not _initialized:
        name = service_name or os.getenv("OTEL_SERVICE_NAME", "release-pilot")
        resource = Resource.create({
            "service.name": name,
            "service.version": "0.1.0",
        })
        _provider = TracerProvider(resource=resource)
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(_provider)
        _initialized = True
    return trace.get_tracer(f"release-pilot.{service_name or 'core'}")


def get_tracer(component: str) -> trace.Tracer:
    if not _initialized:
        setup_telemetry()
    return trace.get_tracer(f"release-pilot.{component}")


@contextmanager
def agent_span(
    tracer: trace.Tracer,
    agent_name: str,
    **kwargs: object,
) -> Generator[trace.Span, None, None]:
    """Wrap an agent invocation in an OTel span with GenAI semantic attributes.

    Span attributes: gen_ai.agent.name, gen_ai.system, gen_ai.operation.name,
    release_pilot.trace_id (from kwargs), release_pilot.release_id (from kwargs).
    """
    with tracer.start_as_current_span(f"agent.{agent_name}") as span:
        span.set_attribute("gen_ai.agent.name", agent_name)
        span.set_attribute("gen_ai.system", "openai")
        span.set_attribute("gen_ai.operation.name", "agent_invoke")
        if trace_id := kwargs.get("trace_id"):
            span.set_attribute("release_pilot.trace_id", str(trace_id))
        if release_id := kwargs.get("release_id"):
            span.set_attribute("release_pilot.release_id", str(release_id))
        yield span  # type: ignore[misc]


def record_llm_call(
    span: trace.Span,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Set GenAI semantic convention attributes for an LLM call on the span."""
    span.set_attribute("gen_ai.request.model", model)
    span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", output_tokens)


def record_tool_call(
    span: trace.Span,
    tool_name: str,
    allowed: bool,
    denial_reason: str | None = None,
) -> None:
    """Record a tool call and its OPA policy outcome on the span.

    Sets gen_ai.tool.name, release_pilot.tool.allowed,
    and release_pilot.policy.denial_reason when a denial occurred.
    """
    span.set_attribute("gen_ai.tool.name", tool_name)
    span.set_attribute("release_pilot.tool.allowed", allowed)
    if denial_reason is not None:
        span.set_attribute("release_pilot.policy.denial_reason", denial_reason)


def record_a2a_message(
    span: trace.Span,
    from_agent: str,
    to_agent: str,
    message_type: str,
    trace_id: str,
) -> None:
    """Record A2A message routing metadata on the span."""
    span.set_attribute("release_pilot.a2a.from", from_agent)
    span.set_attribute("release_pilot.a2a.to", to_agent)
    span.set_attribute("release_pilot.a2a.type", message_type)
    span.set_attribute("release_pilot.trace_id", trace_id)
