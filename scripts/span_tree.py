"""Print the span tree from an OpenTelemetry console export (ENABLE_CONSOLE_EXPORTERS=true).

    python scripts/span_tree.py run_console.txt > reports/crew_trace.txt

The console exporter writes one JSON object per span. This reads them back, nests each span under its
parent and prints name, duration and the attributes that tell the crew's story.
"""

import json
import sys
from collections import defaultdict

KEEP = ("touchline.outcome", "touchline.attempts", "touchline.claims_checked", "touchline.verdict.ok",
        "gen_ai.tool.name", "gen_ai.agent.name", "gen_ai.request.model", "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens", "executor.id", "error.type")


def spans(text: str):
    dec, i = json.JSONDecoder(), 0
    while (i := text.find("{", i)) >= 0:
        try:
            obj, end = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, dict) and "context" in obj and "start_time" in obj:
            yield obj
        i = end


def ms(s) -> float:
    from datetime import datetime
    f = lambda t: datetime.fromisoformat(t.replace("Z", "+00:00"))
    return (f(s["end_time"]) - f(s["start_time"])).total_seconds() * 1000


def main(path: str):
    all_ = list(spans(open(path, encoding="utf-8").read()))
    by_id = {s["context"]["span_id"]: s for s in all_}
    kids = defaultdict(list)
    for s in all_:
        kids[s.get("parent_id") if s.get("parent_id") in by_id else None].append(s)
    for v in kids.values():
        v.sort(key=lambda s: s["start_time"])

    def show(s, depth):
        attrs = {k: v for k, v in (s.get("attributes") or {}).items() if k in KEEP}
        events = [e["name"] for e in s.get("events") or [] if e["name"] != "exception"]
        tail = "  " + json.dumps(attrs, ensure_ascii=False) if attrs else ""
        tail += f"  events={events}" if events else ""
        status = s.get("status", {}).get("status_code", "UNSET")
        print(f"{'  ' * depth}{s['name']}  {ms(s):.0f} ms{'  ' + status if status == 'ERROR' else ''}{tail}")
        for k in kids[s["context"]["span_id"]]:
            show(k, depth + 1)

    traces = {s["context"]["trace_id"] for s in all_}
    print(f"{len(all_)} spans in {len(traces)} trace(s)")
    for root in kids[None]:
        show(root, 0)


if __name__ == "__main__":
    main(sys.argv[1])
