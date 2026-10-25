"""The public endpoints that spend something: /mcp (CPU) and Ask the record (model credit)."""

import asyncio
from types import SimpleNamespace as NS

import httpx

from touchline_live import guard, server
from touchline_live.crew import ask as ask_mod


def _http():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://touchline.test", timeout=30)


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
ACCEPT = {"Accept": "application/json, text/event-stream"}


# ----- /mcp ---------------------------------------------------------------------------------------

def test_mcp_takes_post_only():
    async def go():
        async with server.MCP.lifespan(), _http() as http:
            return [await http.request(m, "/mcp") for m in ("GET", "DELETE", "PUT")]
    for r in asyncio.run(go()):
        assert r.status_code == 405 and r.headers["allow"] == "POST"


def test_mcp_initialize_over_post():
    async def go():
        async with server.MCP.lifespan(), _http() as http:
            return await http.post("/mcp", json=INIT, headers=ACCEPT)
    r = asyncio.run(go())
    assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "touchline-match"


def test_mcp_answers_429_when_its_one_worker_stays_busy(monkeypatch):
    monkeypatch.setattr(server.MCP_GATE, "wait_s", 0.05)
    monkeypatch.setattr(server.MCP_GATE, "inflight", 1)          # someone else holds the worker
    async def go():
        async with server.MCP.lifespan(), _http() as http:
            return await http.post("/mcp", json=INIT, headers=ACCEPT)
    r = asyncio.run(go())
    assert server.MCP_GATE.workers == 1
    assert r.status_code == 429 and "busy" in r.json()["detail"] and r.headers["retry-after"] == "1"


def test_mcp_token_bucket_per_client_keyed_on_the_first_forwarded_hop(monkeypatch):
    t = [0.0]
    monkeypatch.setattr(server.MCP_GATE, "bucket", guard.TokenBucket(rate=0.5, burst=2, now=lambda: t[0]))
    a = {**ACCEPT, "X-Forwarded-For": "203.0.113.9, 10.0.0.4"}
    b = {**ACCEPT, "X-Forwarded-For": "198.51.100.7"}
    async def go():
        async with server.MCP.lifespan(), _http() as http:
            first = [(await http.post("/mcp", json=INIT, headers=a)).status_code for _ in range(3)]
            other = (await http.post("/mcp", json=INIT, headers=b)).status_code
            limited = await http.post("/mcp", json=INIT, headers=a)
            t[0] += 2.0                                             # one token back after two seconds
            again = (await http.post("/mcp", json=INIT, headers=a)).status_code
            return first, other, limited, again
    first, other, limited, again = asyncio.run(go())
    assert first == [200, 200, 429] and other == 200 and again == 200
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1


def test_client_key_and_local_requests():
    assert guard.client_key({"x-forwarded-for": " 203.0.113.9 , 10.0.0.4"}, ("10.0.0.4", 1)) == "203.0.113.9"
    assert guard.client_key([(b"x-forwarded-for", b"198.51.100.7")], None) == "198.51.100.7"
    assert guard.client_key({}, ("192.0.2.1", 5)) == "192.0.2.1"
    assert guard.is_local([], ("127.0.0.1", 9)) and not guard.is_local([(b"x-forwarded-for", b"1.2.3.4")], ("127.0.0.1", 9))


# ----- Ask the record -------------------------------------------------------------------------------

def _ask_rig(monkeypatch, fake, per_client=6):
    monkeypatch.setattr(server, "CREW", True)
    monkeypatch.setattr(server, "match", lambda mid: None)
    monkeypatch.setattr(server, "ASKED", guard.Window(40, per_client, span=600))
    monkeypatch.setattr(ask_mod, "ask", fake)


def test_ask_502_carries_attempts_and_the_error_and_gives_the_slot_back(monkeypatch):
    async def broken(question, mid, match_s, url, progress=None):
        progress["attempts"] = 3
        raise RuntimeError("Connection error.")
    _ask_rig(monkeypatch, broken)
    async def go():
        async with _http() as http:
            return await http.post("/api/ask/TL-000023", json={"question": "Who scored?", "match_s": 60})
    r = asyncio.run(go())
    body = r.json()
    assert r.status_code == 502 and body["attempts"] == 3 and body["error"] == "RuntimeError: Connection error."
    assert body["detail"] == "the record agent did not answer: RuntimeError"
    assert server.ASKED.all == [] and all(not v for v in server.ASKED.by.values())


def test_ask_budget_per_client_beside_the_page_cap(monkeypatch):
    async def fine(question, mid, match_s, url, progress=None):
        return {"ok": True, "answer": "Fine.", "claims": [], "sources": [], "verdicts": [], "trace": []}
    _ask_rig(monkeypatch, fine, per_client=2)
    q = {"question": "Who scored?", "match_s": 60}
    a, b = {"X-Forwarded-For": "203.0.113.9"}, {"X-Forwarded-For": "198.51.100.7"}
    async def go():
        async with _http() as http:
            mine = [await http.post("/api/ask/TL-000023", json=q, headers=a) for _ in range(3)]
            theirs = await http.post("/api/ask/TL-000023", json=q, headers=b)
            return mine, theirs
    mine, theirs = asyncio.run(go())
    assert [r.status_code for r in mine] == [200, 200, 429] and "you have asked 2" in mine[2].json()["detail"]
    assert theirs.status_code == 200
    w = guard.Window(2, 6, span=600)
    assert w.take("x")[0] is None and w.take("y")[0] is None and w.take("z")[0] == "global"


def test_ask_drops_results_of_tool_calls_that_look_past_the_viewer():
    msgs = [NS(contents=[NS(type="function_call", name="state_at", arguments='{"match_id": "TL-1", "match_s": 2000}', call_id="a"),
                         NS(type="function_call", name="events_between", arguments={"from_s": 0, "to_s": 1500}, call_id="b")]),
            NS(contents=[NS(type="function_result", result='{"score": [3, 1]}', call_id="a"),
                         NS(type="function_result", result='[{"id": 7}]', call_id="b")])]
    calls, traffic = ask_mod._tool_traffic(msgs)
    assert [i for i, _ in traffic] == [0, 1]
    kept, problems = ask_mod._in_time(calls, traffic, 1600)
    assert kept == [[{"id": 7}]] and calls[0]["past"] and "past" not in calls[1]
    assert problems == ["state_at asked for match_s 2000, past the viewer's match time 1600"]
    # Within a second of the viewer's clock is allowed.
    assert ask_mod._in_time([{"tool": "state_at", "match_s": 1600.8}], [(0, {})], 1600)[1] == []


def test_ask_prompt_and_quotes():
    assert ask_mod.plain("Garo’s “big” chance") == "Garo's \"big\" chance"
    assert "Saltbridge Athletic lead 2-0" in ask_mod.INSTRUCTIONS
    assert "unless the fan asks" in ask_mod.INSTRUCTIONS
