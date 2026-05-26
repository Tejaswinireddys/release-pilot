"""OpenTelemetry setup — GenAI semantic conventions for Release Pilot.

Export modes selected by the OTEL_MODE env var (default: auto):

  auto    — TCP-probe OTEL_EXPORTER_OTLP_ENDPOINT at startup; use otlp if
            reachable, file mode otherwise.  Zero configuration required.
  otlp    — gRPC/OTLP to a running collector (Jaeger all-in-one, OTEL
            Collector).  Endpoint: OTEL_EXPORTER_OTLP_ENDPOINT.
  file    — write every span as a JSON line to ./traces/release_pilot_traces.jsonl
            and a human-readable ./traces/last_run.txt after each pipeline run.
            No collector, no Docker required.
  console — print every span to stdout (useful for debugging).

All agent code uses the same span API regardless of which mode is active.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Sequence

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

log = logging.getLogger(__name__)

_initialized = False
_provider: TracerProvider | None = None
_TRACES_DIR = Path("traces")

# Span attributes shown in last_run.txt (in priority order)
_SUMMARY_ATTRS = (
    "gen_ai.agent.name",
    "risk_level",
    "pci_scope_touched",
    "pci_scope_reason",
    "release_pilot.tool.allowed",
    "release_pilot.policy.denial_reason",
    "gen_ai.request.model",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "release_pilot.a2a.from",
    "release_pilot.a2a.to",
    "release_pilot.a2a.type",
    "confluence_url",
    "auditor_verdict",
    "release_pilot.risk_level",
    "release_pilot.pci_scope_touched",
    "redaction_count",
    "injection_attempts_detected",
)


# ── File span exporter ────────────────────────────────────────────────────────

class _FileSpanExporter(SpanExporter):
    """Export spans to JSONL + human-readable last_run.txt.

    Uses SimpleSpanProcessor so each span lands in the file as soon as it
    ends.  The root pipeline.run span ends last (after all child agent spans),
    which triggers the last_run.txt summary write.

    Files:
      ./traces/release_pilot_traces.jsonl  — one JSON line per span (append)
      ./traces/last_run.txt               — overwritten after each pipeline
    """

    def __init__(self, traces_dir: Path = _TRACES_DIR) -> None:
        self._dir = traces_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._jsonl = traces_dir / "release_pilot_traces.jsonl"
        # key: OTel trace_id (int); value: list of ReadableSpan
        self._spans_by_trace: dict[int, list[ReadableSpan]] = {}
        self._trace_to_release: dict[int, str] = {}
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self._lock:
            for span in spans:
                self._append_jsonl(span)
                otel_tid = span.context.trace_id
                self._spans_by_trace.setdefault(otel_tid, []).append(span)

                if span.name == "pipeline.run":
                    attrs = span.attributes or {}
                    release_id = str(
                        attrs.get("release_id")
                        or f"trace-{otel_tid:016x}"
                    )
                    self._trace_to_release[otel_tid] = release_id
                    self._write_summary(otel_tid, release_id)

        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        with self._lock:
            for otel_tid, release_id in self._trace_to_release.items():
                self._write_summary(otel_tid, release_id)
        return True

    def shutdown(self) -> None:
        self.force_flush()

    # ── Private helpers ───────────────────────────────────────────────────────

    def _append_jsonl(self, span: ReadableSpan) -> None:
        try:
            with self._jsonl.open("a", encoding="utf-8") as fh:
                fh.write(span.to_json(indent=None) + "\n")
        except OSError as exc:
            log.warning("telemetry.file: jsonl write failed: %s", exc)

    def _write_summary(self, otel_tid: int, release_id: str) -> None:
        spans = sorted(
            self._spans_by_trace.get(otel_tid, []),
            key=lambda s: s.start_time or 0,
        )
        if not spans:
            return

        root = next((s for s in spans if s.name == "pipeline.run"), spans[0])
        t0_ns: int = root.start_time or 0
        total_ns = (root.end_time or t0_ns) - t0_ns

        root_attrs = root.attributes or {}
        rp_trace_id = str(
            root_attrs.get("trace_id")
            or root_attrs.get("release_pilot.trace_id")
            or f"0x{otel_tid:032x}"
        )

        lines: list[str] = [
            "=" * 72,
            "Release Pilot — Trace Summary",
            "=" * 72,
            f"Release ID  : {release_id}",
            f"Trace ID    : {rp_trace_id}",
            f"Started     : {_ns_to_iso(root.start_time)}",
            f"Completed   : {_ns_to_iso(root.end_time)}",
            f"Duration    : {total_ns / 1e9:.1f}s",
            "=" * 72,
            "",
            "SPAN TIMELINE",
            "─" * 72,
        ]

        for span in spans:
            offset_s = ((span.start_time or t0_ns) - t0_ns) / 1e9
            dur_ns = (
                (span.end_time or span.start_time or t0_ns)
                - (span.start_time or t0_ns)
            )
            dur_s = dur_ns / 1e9
            lines.append(
                f"[{offset_s:07.2f}s]  {span.name:<38}  {dur_s:6.2f}s"
            )
            attrs = span.attributes or {}
            present = [k for k in _SUMMARY_ATTRS if k in attrs]
            for i, key in enumerate(present):
                prefix = "└─" if i == len(present) - 1 else "├─"
                short = key.split(".")[-1]
                lines.append(f"             {prefix} {short:<28}: {attrs[key]}")

        lines += [
            "",
            "=" * 72,
            f"Full JSONL: {self._jsonl}",
            "",
        ]
        try:
            (self._dir / "last_run.txt").write_text(
                "\n".join(lines), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("telemetry.file: last_run.txt write failed: %s", exc)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ns_to_iso(ns: int | None) -> str:
    if ns is None:
        return "—"
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def _otlp_reachable(endpoint: str, timeout: float = 1.0) -> bool:
    """TCP probe: return True if the OTLP gRPC endpoint accepts connections."""
    from urllib.parse import urlparse

    ep = endpoint if "://" in endpoint else f"grpc://{endpoint}"
    parsed = urlparse(ep)
    host = parsed.hostname or "localhost"
    port = parsed.port or 4317
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except OSError:
        return False


def _resolve_mode(mode: str, endpoint: str) -> str:
    """Resolve 'auto' to a concrete mode; return other modes unchanged."""
    if mode != "auto":
        return mode
    if _otlp_reachable(endpoint):
        log.info("telemetry.auto: OTLP reachable at %s — using otlp mode", endpoint)
        return "otlp"
    log.info(
        "telemetry.auto: OTLP not reachable — using file mode (traces/last_run.txt)"
    )
    return "file"


# ── Public API ────────────────────────────────────────────────────────────────

def setup_telemetry(service_name: str | None = None) -> trace.Tracer:
    """Configure the tracer provider for the requested export mode.

    Call once at process startup.  Subsequent calls return the existing tracer
    without re-initialising the provider.

    Mode precedence: OTEL_MODE env var → auto (default).
    """
    global _initialized, _provider
    if _initialized:
        return trace.get_tracer(f"release-pilot.{service_name or 'core'}")

    name = service_name or os.getenv("OTEL_SERVICE_NAME", "release-pilot")
    resource = Resource.create({
        "service.name": name,
        "service.version": "0.1.0",
    })
    _provider = TracerProvider(resource=resource)

    raw_mode = os.getenv("OTEL_MODE", "auto").strip().lower()
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    mode = _resolve_mode(raw_mode, endpoint)
    # Write the resolved concrete mode back so other modules can read it
    # without re-doing the TCP probe (e.g. demo_runner showing trace URLs).
    os.environ["OTEL_MODE"] = mode

    if mode == "otlp":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        exporter: SpanExporter = OTLPSpanExporter(endpoint=endpoint)
        _provider.add_span_processor(BatchSpanProcessor(exporter))
        log.info("telemetry: mode=otlp endpoint=%s", endpoint)

    elif mode == "console":
        _provider.add_span_processor(
            SimpleSpanProcessor(ConsoleSpanExporter())
        )
        log.info("telemetry: mode=console")

    else:
        # file mode (also the fallback for unknown values)
        if mode not in ("file",):
            log.warning(
                "telemetry: unknown OTEL_MODE=%r — defaulting to file", mode
            )
        _TRACES_DIR.mkdir(parents=True, exist_ok=True)
        file_exporter = _FileSpanExporter(_TRACES_DIR)
        # SimpleSpanProcessor exports each span synchronously as it ends,
        # guaranteeing that child spans land in the file before the parent
        # pipeline.run span triggers the last_run.txt summary.
        _provider.add_span_processor(SimpleSpanProcessor(file_exporter))
        log.info("telemetry: mode=file dir=%s", _TRACES_DIR.resolve())

    trace.set_tracer_provider(_provider)
    _initialized = True
    return trace.get_tracer(f"release-pilot.{name}")


def get_tracer(component: str) -> trace.Tracer:
    if not _initialized:
        setup_telemetry()
    return trace.get_tracer(f"release-pilot.{component}")


# ── Span helpers (same API regardless of mode) ────────────────────────────────

@contextmanager
def agent_span(
    tracer: trace.Tracer,
    agent_name: str,
    **kwargs: object,
) -> Generator[trace.Span, None, None]:
    """Wrap an agent invocation in a GenAI OTel span."""
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
    """Record GenAI semantic convention attributes for an LLM call."""
    span.set_attribute("gen_ai.request.model", model)
    span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", output_tokens)


def record_tool_call(
    span: trace.Span,
    tool_name: str,
    allowed: bool,
    denial_reason: str | None = None,
) -> None:
    """Record a tool call and its OPA policy outcome."""
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
    """Record A2A message routing metadata on a span."""
    span.set_attribute("release_pilot.a2a.from", from_agent)
    span.set_attribute("release_pilot.a2a.to", to_agent)
    span.set_attribute("release_pilot.a2a.type", message_type)
    span.set_attribute("release_pilot.trace_id", trace_id)
