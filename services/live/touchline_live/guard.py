"""Guards for the public endpoints that spend something: /mcp spends CPU, Ask the record spends model credit.

A client is keyed on the first X-Forwarded-For hop, the viewer as Azure Container Apps ingress reports it,
or on the socket peer when there is no proxy. A request from this host to itself (the Ask agent reading
the record over /mcp) carries no X-Forwarded-For and is not counted against anyone.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def client_key(headers, client) -> str:
    """The first X-Forwarded-For hop, else the peer address. `headers` is a mapping or ASGI header pairs."""
    if isinstance(headers, (list, tuple)):
        xff = next((v.decode("latin-1") for k, v in headers if k.lower() == b"x-forwarded-for"), "")
    else:
        xff = headers.get("x-forwarded-for", "")
    first = xff.split(",")[0].strip()
    if first:
        return first
    return (client[0] if client else "") or "unknown"


def is_local(headers, client) -> bool:
    if isinstance(headers, (list, tuple)):
        has_xff = any(k.lower() == b"x-forwarded-for" for k, _ in headers)
    else:
        has_xff = "x-forwarded-for" in headers
    return not has_xff and bool(client) and client[0] in LOOPBACK


class TokenBucket:
    """Per-client token buckets: `burst` requests at once, refilled at `rate` per second."""

    def __init__(self, rate: float, burst: int, now=time.monotonic, max_clients: int = 10000):
        self.rate, self.burst, self.now, self.max_clients = rate, burst, now, max_clients
        self.buckets: dict[str, tuple[float, float]] = {}

    def take(self, key: str) -> float:
        """0 when the request may go ahead, else the seconds until a token is back."""
        t = self.now()
        tokens, last = self.buckets.get(key, (float(self.burst), t))
        tokens = min(float(self.burst), tokens + (t - last) * self.rate)
        if tokens < 1:
            self.buckets[key] = (tokens, t)
            return (1 - tokens) / self.rate
        self.buckets[key] = (tokens - 1, t)
        if len(self.buckets) > self.max_clients:
            # Forget the clients whose buckets have refilled: they would start full anyway.
            full = [k for k, (n, s) in self.buckets.items() if n + (t - s) * self.rate >= self.burst]
            for k in full:
                del self.buckets[k]
        return 0.0


class Window:
    """At most `limit` events per `span` seconds, globally and per client. A slot can be given back."""

    def __init__(self, limit: int, per_client: int, span: float = 600.0, now=time.time):
        self.limit, self.per_client, self.span, self.now = limit, per_client, span, now
        self.all: list[float] = []
        self.by: dict[str, list[float]] = {}

    def take(self, key: str) -> tuple[str | None, float | None]:
        """(None, stamp) when allowed, else ("global" | "client", None)."""
        t = self.now()
        self.all[:] = [x for x in self.all if t - x < self.span]
        mine = [x for x in self.by.get(key, []) if t - x < self.span]
        if len(self.all) >= self.limit:
            self.by[key] = mine
            return "global", None
        if len(mine) >= self.per_client:
            self.by[key] = mine
            return "client", None
        self.all.append(t)
        mine.append(t)
        self.by[key] = mine
        if len(self.by) > 10000:
            self.by = {k: v for k, v in self.by.items() if v and t - v[-1] < self.span}
        return None, t

    def give_back(self, key: str, stamp: float):
        """A request that failed on our side (a 5xx) does not count against the viewer or the page."""
        for lst in (self.all, self.by.get(key, [])):
            try:
                lst.remove(stamp)
            except ValueError:
                pass


class McpGate:
    """/mcp for the public: POST only, one request in the worker at a time, and a token bucket per client.

    A request waits at most `wait_s` for the worker; if it is still busy the reply is 429, so a flood
    queues nowhere. A client past its bucket gets 429 with Retry-After.
    """

    def __init__(self, inner, *, workers: int = 1, wait_s: float | None = None, rate: float | None = None,
                 burst: int | None = None, now=time.monotonic):
        self.inner = inner
        self.workers = workers
        self.wait_s = wait_s if wait_s is not None else float(os.environ.get("TOUCHLINE_MCP_WAIT_S", "2"))
        self.bucket = TokenBucket(rate if rate is not None else float(os.environ.get("TOUCHLINE_MCP_RATE", "1")),
                                  burst if burst is not None else int(os.environ.get("TOUCHLINE_MCP_BURST", "40")), now=now)
        self.inflight = 0
        self.stats = {"served": 0, "busy": 0, "limited": 0, "method": 0}

    async def _reply(self, send, status: int, msg: str, headers: list[tuple[bytes, bytes]] = ()):
        body = json.dumps({"detail": msg}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), *headers]})
        await send({"type": "http.response.body", "body": body})

    async def _acquire(self) -> bool:
        deadline = time.monotonic() + self.wait_s
        while self.inflight >= self.workers:
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.02)
        self.inflight += 1
        return True

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        if scope["method"] != "POST":
            self.stats["method"] += 1
            return await self._reply(send, 405, "the match MCP server takes POST only", [(b"allow", b"POST")])
        headers, client = scope.get("headers") or [], scope.get("client")
        if not is_local(headers, client):
            wait = self.bucket.take(client_key(headers, client))
            if wait:
                self.stats["limited"] += 1
                return await self._reply(send, 429, "too many MCP requests from this client; slow down",
                                         [(b"retry-after", str(max(1, round(wait))).encode())])
        if not await self._acquire():
            self.stats["busy"] += 1
            return await self._reply(send, 429, "the match MCP server is busy; try again in a moment",
                                     [(b"retry-after", b"1")])
        try:
            self.stats["served"] += 1
            await self.inner(scope, receive, send)
        finally:
            self.inflight -= 1
