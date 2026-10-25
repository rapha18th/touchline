"""Tracing: Agent Framework's workflow spans and Touchline's own spans reach an exporter when one is set."""

import asyncio

from agent_framework import Executor, WorkflowBuilder, WorkflowContext, handler
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from touchline_live import telemetry


class Hop(Executor):
    @handler
    async def run(self, n: int, ctx: WorkflowContext[int]) -> None:
        await ctx.send_message(n + 1)


class End(Executor):
    @handler
    async def run(self, n: int, ctx: WorkflowContext[int, int]) -> None:
        await ctx.yield_output(n)


def test_spans_reach_the_exporter(monkeypatch):
    for k in ("APPLICATIONINSIGHTS_CONNECTION_STRING", "OTEL_EXPORTER_OTLP_ENDPOINT", "ENABLE_CONSOLE_EXPORTERS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(telemetry, "_mode", None)
    assert telemetry.setup("touchline-test") == "off"          # nothing configured: a no-op

    monkeypatch.setattr(telemetry, "_mode", None)
    mem = InMemorySpanExporter()
    assert telemetry.setup("touchline-test", exporters=[mem]) == "custom"

    async def go():
        with telemetry.span("crew.moment goal", **{"touchline.moment": "goal", "touchline.player": None}):
            hop, end = Hop(id="producer"), End(id="director")
            wf = WorkflowBuilder(name="probe", start_executor=hop).add_edge(hop, end).build()
            with telemetry.mcp_call("state_at", {"match_id": "TL-000001", "match_s": 60.0}):
                pass
            return (await wf.run(1)).get_outputs()

    assert asyncio.run(go()) == [2]
    from opentelemetry import trace
    trace.get_tracer_provider().force_flush()
    spans = mem.get_finished_spans()
    names = [s.name for s in spans]
    moment = next(s for s in spans if s.name == "crew.moment goal")
    assert "touchline.player" not in moment.attributes                 # None attributes are dropped
    mcp = next(s for s in spans if "state_at" in s.name)
    assert mcp.attributes["gen_ai.tool.name"] == "state_at" and mcp.parent.span_id == moment.context.span_id
    # Agent Framework's own instrumentation: the workflow run and each executor nest under the moment.
    framework = [s for s in spans if s.name not in ("crew.moment goal", mcp.name)]
    assert framework, names
    assert any("producer" in s.name or s.attributes.get("executor.id") == "producer" for s in framework), names
    assert all(s.context.trace_id == moment.context.trace_id for s in framework), names
    run = next(s for s in framework if s.name == "workflow.run")
    assert run.parent.span_id == moment.context.span_id
    assert {"executor.process producer", "executor.process director"} <= set(names)
