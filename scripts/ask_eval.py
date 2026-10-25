"""Ask the record, measured: a fixed set of fan questions against a running Touchline server.

    python scripts/ask_eval.py http://127.0.0.1:8080 TL-000008 1960 reports/ask_record_TL-000008.json

For each question it records whether the Verifier passed an answer (first try or after one repair) or refused,
the problems it found, the MCP tools the agent called, and the wall time. A request that fails records its
status, the server's detail, the exception text and the number of model calls tried.

The server allows six questions per client in ten minutes; for this run start it with TOUCHLINE_ASK_PER_CLIENT=10.
"""

import json
import statistics
import sys
import time

import httpx

QUESTIONS = [
    "Who scored and when?",
    "How many shots has each side had?",
    "What is the score?",
    "Who has the most passes for the away side?",
    "How many corners has Northgate FC won?",
    "Which player has the highest xG so far?",
    "Who is the referee?",
    "Who is the best player ever?",
    "Did anyone get booked?",
    "Who will win the league?",
]


def main(base: str, match_id: str, match_s: float):
    rows = []
    with httpx.Client(base_url=base, timeout=120) as http:
        for q in QUESTIONS:
            t0 = time.perf_counter()
            r = http.post(f"/api/ask/{match_id}", json={"question": q, "match_s": match_s})
            ms = round((time.perf_counter() - t0) * 1000)
            body = r.json()
            if r.status_code != 200:
                # A 502 carries the exception text and how many model calls were tried; keep both.
                rows.append({"question": q, "status": r.status_code, "detail": body.get("detail"),
                             "error": body.get("error"), "attempts": body.get("attempts"), "ms": ms})
                continue
            v = body["verdicts"]
            rows.append({"question": q, "outcome": "first_pass" if body["ok"] and len(v) == 1 else "repaired" if body["ok"] else "refused",
                         "answer": body["answer"], "sources": body["sources"], "problems": [p for x in v for p in x["problems"]],
                         "claims_checked": sum(x["claims_checked"] for x in v),
                         "tools": [c["tool"] for t in body["trace"] for c in t.get("mcp", [])], "ms": ms})
    done = [r for r in rows if "outcome" in r]
    summary = {"match_id": match_id, "match_s": match_s, "questions": len(rows),
               "first_pass": sum(r["outcome"] == "first_pass" for r in done), "repaired": sum(r["outcome"] == "repaired" for r in done),
               "refused": sum(r["outcome"] == "refused" for r in done), "errors": len(rows) - len(done),
               "median_ms": statistics.median(r["ms"] for r in rows) if rows else None}
    text = json.dumps({"summary": summary, "rows": rows}, indent=1, ensure_ascii=False)
    if len(sys.argv) > 4:
        with open(sys.argv[4], "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], float(sys.argv[3]))
