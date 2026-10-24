"""Tracing for the crew: Agent Framework's OpenTelemetry instrumentation, exported to Application Insights.

Agent Framework already emits spans for every workflow run, executor, agent invocation and model call.
This module gives those spans somewhere to go, and adds two of Touchline's own: one span per crew moment
and one client span per MCP call to the match record.

    APPLICATIONINSIGHTS_CONNECTION_STRING=...   spans and metrics go to Azure Application Insights
    ENABLE_CONSOLE_EXPORTERS=true               spans print to stdout (local checks)
    OTEL_EXPORTER_OTLP_ENDPOINT=...             any OTLP collector

With none of these set, setup() does nothing and the crew runs exactly as before.
Prompts and model replies stay out of the spans unless ENABLE_SENSITIVE_DATA=true.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any

from opentelemetry import trace

log = logging.getLogger(__name__)
_mode: str | None = None


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def setup(service_name: str = "touchline", exporters: list | None = None) -> str:
    """Configure the OpenTelemetry providers once per process. Returns where spans go: azure, console,
    otlp, custom or off."""
    global _mode
    if _mode is not None:
        return _mode
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    console = _truthy("ENABLE_CONSOLE_EXPORTERS")
    otlp = bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"))
    extra = list(exporters or [])
    if conn:
        try:
            from azure.monitor.opentelemetry.exporter import AzureMonitorMetricExporter, AzureMonitorTraceExporter
            extra += [AzureMonitorTraceExporter(connection_string=conn), AzureMonitorMetricExporter(connection_string=conn)]
        except Exception as exc:  # a bad connection string or a missing package must never stop the broadcast
            log.warning("Application Insights export is off: %s", exc)
            conn = ""
    if not (extra or console or otlp):
        _mode = "off"
        return _mode
    from agent_framework.observability import configure_otel_providers
    configure_otel_providers(service_name=os.environ.get("OTEL_SERVICE_NAME", service_name), exporters=extra,
                             enable_console_exporters=console)
    _mode = "azure" if conn else "console" if console else "otlp" if otlp else "custom"
    return _mode


def mode() -> str:
    return _mode or "off"


def tracer() -> trace.Tracer:
    return trace.get_tracer("touchline")


@contextlib.contextmanager
def span(name: str, **attrs: Any):
    """A Touchline span. Attribute values that are None are left out."""
    with tracer().start_as_current_span(name, attributes={k: v for k, v in attrs.items() if v is not None}) as s:
        yield s


@contextlib.contextmanager
def mcp_call(tool: str, args: dict):
    """A client span for one MCP tool call, named and shaped per the OpenTelemetry MCP conventions."""
    attrs = {"gen_ai.tool.name": tool, "gen_ai.operation.name": "execute_tool",
             "touchline.match_id": args.get("match_id"), "touchline.match_s": args.get("match_s")}
    try:
        from agent_framework.observability import create_mcp_client_span
        cm = create_mcp_client_span("tools/call", tool, {k: v for k, v in attrs.items() if v is not None})
    except Exception:  # an older framework without the helper still gets a plain span
        cm = span(f"tools/call {tool}", **{"mcp.method.name": "tools/call", **attrs})
    with cm as s:
        yield s
