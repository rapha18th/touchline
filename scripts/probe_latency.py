"""Probe: Responses latency for the live model at each reasoning effort."""
import asyncio, time, sys
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from azure.identity import AzureCliCredential
from pydantic import BaseModel

class Out(BaseModel):
    text: str

async def main():
    c = OpenAIChatClient(model=sys.argv[1] if len(sys.argv) > 1 else "gpt-5.4-mini",
                         azure_endpoint="https://touchline-ai-rm.openai.azure.com/", credential=AzureCliCredential())
    a = Agent(client=c, name="p", instructions="One broadcast line, 20 words.")
    await a.run("warm up", options={"response_format": Out})
    for effort in ("none", "low"):
        ts = []
        for _ in range(3):
            t0 = time.perf_counter()
            try:
                r = await a.run("Goal: Sipho Lunwell, 23 minutes, xG 0.21, from 8.3 m, 101 km/h. Elmstead United lead 1-0.",
                                options={"response_format": Out, "reasoning": {"effort": effort}})
                ts.append(round(time.perf_counter() - t0, 2))
            except Exception as e:
                ts.append(f"{type(e).__name__}: {str(e)[:80]}")
                break
        print(effort, ts)

asyncio.run(main())
