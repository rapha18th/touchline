"""Model clients for the crew: Azure OpenAI deployments in the Foundry project, keyless."""

from __future__ import annotations

import functools
import os

from agent_framework.openai import OpenAIChatClient
from azure.identity import AzureCliCredential, ChainedTokenCredential, ManagedIdentityCredential

ENDPOINT = os.environ.get("TOUCHLINE_AI_ENDPOINT", "https://touchline-ai-rm.openai.azure.com/")
FAST = os.environ.get("TOUCHLINE_FAST_MODEL", "gpt-5.4-mini")     # live lines, analysis, translation
DEEP = os.environ.get("TOUCHLINE_DEEP_MODEL", "gpt-5-mini")       # recaps


@functools.lru_cache(maxsize=1)
def credential():
    # In Azure the container's managed identity signs in; on a laptop the Azure CLI login does.
    return ChainedTokenCredential(ManagedIdentityCredential(), AzureCliCredential())


@functools.lru_cache(maxsize=4)
def client(model: str = FAST) -> OpenAIChatClient:
    return OpenAIChatClient(model=model, azure_endpoint=ENDPOINT, credential=credential())
