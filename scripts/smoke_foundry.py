"""P0 smoke test: one keyless call to each Foundry deployment through the Azure CLI login."""

import os
import sys
import time

from azure.identity import AzureCliCredential, get_bearer_token_provider
from openai import OpenAI

ENDPOINT = os.environ.get("TOUCHLINE_AI_ENDPOINT", "https://touchline-ai-rm.openai.azure.com/openai/v1/")
DEPLOYMENTS = sys.argv[1:] or ["gpt-5.4-mini", "gpt-5-mini"]

token = get_bearer_token_provider(AzureCliCredential(), "https://cognitiveservices.azure.com/.default")
client = OpenAI(base_url=ENDPOINT, api_key=token())

for name in DEPLOYMENTS:
    t0 = time.perf_counter()
    r = client.responses.create(
        model=name,
        input="In eight words, describe a counter-attack in football.",
        max_output_tokens=400,
    )
    print(f"{name}: {time.perf_counter() - t0:.2f}s · {r.output_text.strip()!r} · tokens {r.usage.total_tokens}")
