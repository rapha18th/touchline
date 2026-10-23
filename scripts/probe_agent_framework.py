"""Probe: one Agent Framework agent on Foundry with a tool and a structured reply."""

import asyncio
import time

from agent_framework import Agent, tool
from agent_framework.openai import OpenAIChatClient
from azure.identity import AzureCliCredential
from pydantic import BaseModel


class Line(BaseModel):
    text: str
    event_ids: list[int]


@tool
def get_event(event_id: int) -> dict:
    """Return one match event by id."""
    return {"id": event_id, "type": "shot", "player": "Ade Okmar", "outcome": "goal", "xg": 0.31, "clock": "63:12"}


async def main():
    client = OpenAIChatClient(
        model="gpt-5.4-mini",
        azure_endpoint="https://touchline-ai-rm.openai.azure.com/",
        credential=AzureCliCredential(),
    )
    agent = Agent(client=client, name="narrator", instructions="You write one broadcast line. Cite event ids.", tools=[get_event])
    t0 = time.perf_counter()
    r = await agent.run("Call event 812 for the casual fan.", options={"response_format": Line})
    print(f"{time.perf_counter() - t0:.2f}s", type(r).__name__)
    print("value:", getattr(r, "value", None))
    print("text:", r.text)


asyncio.run(main())
