"""The match MCP server mounted at /mcp in the web service, exercised with the official MCP Python client."""

import asyncio
import json

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from touchline_engine import export
from touchline_engine.sim import Match as EngineMatch
from touchline_live import mcp_server, server


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    root = tmp_path_factory.mktemp("matches")
    m = EngineMatch(23)
    m.play_period(1, 600, 0)
    m.period_frames = m.period_frames[:1]
    export.write(m, root / "TL-000023")
    return root


@pytest.fixture
def record(data, monkeypatch):
    monkeypatch.setattr(mcp_server, "DATA", data)
    mcp_server._match.cache_clear()
    mcp_server._state.cache_clear()
    yield data
    mcp_server._match.cache_clear()
    mcp_server._state.cache_clear()


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://touchline.test", timeout=60)


async def _with_session(fn):
    async with server.MCP.lifespan(), _client() as http:
        async with streamable_http_client("http://touchline.test/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)


def test_mcp_lists_only_read_only_tools(record):
    async def go(s):
        return (await s.list_tools()).tools
    tools = asyncio.run(_with_session(go))
    names = {t.name for t in tools}
    assert {"list_matches", "match_info", "get_events", "events_between", "state_at", "player_line", "leaders", "verify_numbers"} == names
    for t in tools:
        assert t.annotations.readOnlyHint is True and t.annotations.destructiveHint is False


def test_mcp_state_at_and_get_events_answer_from_the_record(record):
    async def go(s):
        st = await s.call_tool("state_at", {"match_id": "TL-000023", "match_s": 300})
        ev = await s.call_tool("get_events", {"match_id": "TL-000023", "ids": [0, 1, 2]})
        bad = await s.call_tool("state_at", {"match_id": "../etc", "match_s": 10})
        return st, ev, bad
    st, ev, bad = asyncio.run(_with_session(go))
    assert not st.isError
    snap = json.loads(st.content[0].text)             # a plain dict return comes back as JSON text
    assert {"score", "clock", "teams", "explain"} <= set(snap)
    assert snap["teams"]["home"]["passes"] + snap["teams"]["away"]["passes"] > 0
    events = ev.structuredContent["result"]
    assert [e["id"] for e in events] == [0, 1, 2]
    assert all("truth" not in e and "clock" in e for e in events)
    assert bad.isError


def test_leaders_and_leader_claims(record):
    async def go(s):
        top = await s.call_tool("leaders", {"match_id": "TL-000023", "stat": "passes", "match_s": 500, "team": "home", "limit": 3})
        rows = top.structuredContent["result"]
        claims = [{"kind": "leader", "stat": "passes", "team": "home", "player": rows[0]["id"]},
                  {"kind": "leader", "stat": "passes", "team": "home", "player": rows[-1]["id"]}]
        checked = await s.call_tool("verify_numbers", {"match_id": "TL-000023", "match_s": 500, "claims": claims})
        bad = await s.call_tool("leaders", {"match_id": "TL-000023", "stat": "charisma", "match_s": 500})
        return rows, checked.structuredContent["result"], bad
    rows, checked, bad = asyncio.run(_with_session(go))
    assert len(rows) == 3 and rows[0]["passes"] >= rows[1]["passes"] >= rows[2]["passes"]
    assert all(r["team"] == rows[0]["team"] for r in rows)
    assert checked[0]["ok"] is True
    assert checked[1]["ok"] is (rows[-1]["passes"] == rows[0]["passes"])      # only a tie may share the lead
    assert bad.isError


def test_mcp_answers_a_single_plain_post(record):
    """What the Crew room does in the browser: one stateless JSON-RPC POST, a JSON reply."""
    async def go():
        async with server.MCP.lifespan(), _client() as http:
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "get_events", "arguments": {"match_id": "TL-000023", "ids": [5]}}}
            r = await http.post("/mcp", json=body, headers={"Accept": "application/json, text/event-stream"})
            return r
    r = asyncio.run(go())
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    msg = r.json()
    assert msg["result"]["structuredContent"]["result"][0]["id"] == 5
    assert json.loads(msg["result"]["content"][0]["text"])["id"] == 5
