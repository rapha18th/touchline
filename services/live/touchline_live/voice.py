"""Voiced commentary with Azure AI Speech: a broadcast voice per language and persona, keyless.

    python -m touchline_live.voice "Garo scores from 18 metres." --lang en --persona casual --moment goal

Two voices per language. The caller is lively and speaks for the casual fan and the studio; the
analyst is calmer and steadier. Every clip is SSML to the Speech REST endpoint on the AIServices
account's custom domain, signed with a Microsoft Entra token (the container's managed identity in
Azure, the Azure CLI login on a laptop). Clips are mp3 at 32 kbit/s and cached on disk by a hash of
the text and the voice settings, so a line is billed once.

Languages: English, Spanish, French and Portuguese, the languages Touchline speaks. Nothing else is voiced.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[3]
ENDPOINT = os.environ.get("TOUCHLINE_SPEECH_ENDPOINT", "https://touchline-ai-rm.cognitiveservices.azure.com/")
CACHE = Path(os.environ.get("TOUCHLINE_VOICE_CACHE", ROOT / "data" / "voice"))
FORMAT = "audio-16khz-32kbitrate-mono-mp3"          # the smallest mp3 the service offers
SCOPE = "https://cognitiveservices.azure.com/.default"
PRICE_PER_M = 15.0                                   # USD per million characters, standard neural voices

LANGS = ("en", "es", "fr", "pt")
ROLE = {"casual": "caller", "studio": "caller", "analyst": "analyst"}
LIFT = {"goal", "red_card"}                          # moments where the caller lifts


@dataclass(frozen=True)
class Voice:
    name: str
    locale: str
    style: str | None = None                         # mstts:express-as, only where the voice has it
    rate: str | None = None
    pitch: str | None = None


VOICES: dict[tuple[str, str], Voice] = {
    ("en", "caller"): Voice("en-GB-RyanNeural", "en-GB", style="cheerful", rate="+6%"),
    ("en", "analyst"): Voice("en-GB-SoniaNeural", "en-GB"),
    ("es", "caller"): Voice("es-MX-JorgeNeural", "es-MX", style="cheerful", rate="+6%"),
    ("es", "analyst"): Voice("es-MX-DaliaNeural", "es-MX"),
    ("fr", "caller"): Voice("fr-FR-HenriNeural", "fr-FR", rate="+6%", pitch="+2%"),
    ("fr", "analyst"): Voice("fr-FR-DeniseNeural", "fr-FR"),
    ("pt", "caller"): Voice("pt-BR-AntonioNeural", "pt-BR", rate="+6%", pitch="+2%"),
    ("pt", "analyst"): Voice("pt-BR-FranciscaNeural", "pt-BR"),
}


def voice_for(lang: str, persona: str, moment: str | None = None) -> Voice:
    """The voice for a line. Raises ValueError for a language Touchline does not voice."""
    if lang not in LANGS:
        raise ValueError(f"no voice for language {lang!r}")
    role = ROLE.get(persona)
    if role is None:
        raise ValueError(f"no voice for persona {persona!r}")
    v = VOICES[(lang, role)]
    if role == "caller" and moment in LIFT:
        # A goal lifts the caller: a touch quicker and higher.
        v = Voice(v.name, v.locale, v.style, rate="+14%", pitch="+6%")
    return v


def ssml(text: str, v: Voice) -> str:
    """Compact SSML: markup inside <voice> is billed, so add only what changes the delivery."""
    body = escape(" ".join(text.split()))
    if v.rate or v.pitch:
        attrs = "".join(f" {k}='{val}'" for k, val in (("rate", v.rate), ("pitch", v.pitch)) if val)
        body = f"<prosody{attrs}>{body}</prosody>"
    if v.style:
        body = f"<mstts:express-as style='{v.style}'>{body}</mstts:express-as>"
    return (f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' "
            f"xmlns:mstts='https://www.w3.org/2001/mstts' xml:lang='{v.locale}'>"
            f"<voice name='{v.name}'>{body}</voice></speak>")


def billed_chars(doc: str) -> int:
    """Characters the service bills: everything inside <voice>, the voice tags themselves excluded."""
    inner = doc.split(">", 2)[2] if doc.count(">") >= 2 else doc      # drop <speak ...><voice ...>
    return len(inner.removesuffix("</voice></speak>"))


def key(text: str, v: Voice, fmt: str = FORMAT) -> str:
    blob = json.dumps({"t": " ".join(text.split()), "v": asdict(v), "f": fmt}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:20]


class SpeechError(RuntimeError):
    pass


class Speaker:
    """Synthesises lines to mp3 files, one disk cache, a few requests at a time."""

    def __init__(self, cache_dir: Path | str | None = None, endpoint: str = ENDPOINT, credential=None,
                 concurrency: int = 4, fmt: str = FORMAT):
        self.cache = Path(cache_dir) if cache_dir else CACHE
        self.url = endpoint.rstrip("/") + "/tts/cognitiveservices/v1"
        self.fmt = fmt
        self._cred = credential
        self._token: tuple[str, float] | None = None
        self._lock: asyncio.Lock | None = None
        self._sem = asyncio.Semaphore(concurrency)
        self._client = None
        self.stats = {"synth": 0, "cached": 0, "failed": 0, "chars": 0, "bytes": 0, "ms": 0}
        self.latencies: list[int] = []                 # ms per synthesis call, cache hits excluded

    # ----- auth ---------------------------------------------------------------------------------
    def _credential(self):
        if self._cred is None:
            from .crew.llm import credential           # managed identity in Azure, CLI login on a laptop
            self._cred = credential()
        return self._cred

    async def _bearer(self) -> str:
        # One token for every request; a lock so a burst of lines fetches it once, not once per line.
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._token is None or self._token[1] - time.time() < 300:
                tok = await asyncio.to_thread(self._credential().get_token, SCOPE)
                self._token = (tok.token, float(tok.expires_on))
            return self._token[0]

    # ----- synthesis ------------------------------------------------------------------------------
    def path(self, text: str, v: Voice) -> Path:
        return self.cache / f"{key(text, v, self.fmt)}.mp3"

    async def synth(self, text: str, v: Voice) -> Path:
        """Return the mp3 for text in voice v, from the cache when it is there."""
        p = self.path(text, v)
        if p.exists() and p.stat().st_size > 0:
            self.stats["cached"] += 1
            return p
        import httpx
        doc = ssml(text, v)
        async with self._sem:
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=20)
            t0 = time.perf_counter()
            last = None
            for attempt in range(4):
                try:
                    r = await self._client.post(self.url, content=doc.encode("utf-8"), headers={
                        "Authorization": f"Bearer {await self._bearer()}",
                        "Content-Type": "application/ssml+xml", "X-Microsoft-OutputFormat": self.fmt,
                        "User-Agent": "touchline"})
                except httpx.HTTPError as exc:
                    last = f"{type(exc).__name__}"
                else:
                    if r.status_code == 200 and r.content:
                        break
                    last = f"HTTP {r.status_code} {r.text[:160]}"
                    if r.status_code == 401:
                        self._token = None
                    elif r.status_code not in (408, 429, 500, 502, 503, 504):
                        break
                await asyncio.sleep(0.8 * 2 ** attempt)          # the network here drops connections: back off
            else:
                r = None
            if r is None or r.status_code != 200 or not r.content:
                self.stats["failed"] += 1
                raise SpeechError(f"{v.name}: {last}")
        self.cache.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".part")
        tmp.write_bytes(r.content)
        tmp.replace(p)
        self.stats["synth"] += 1
        self.stats["chars"] += billed_chars(doc)
        self.stats["bytes"] += len(r.content)
        ms = round((time.perf_counter() - t0) * 1000)
        self.stats["ms"] += ms
        self.latencies.append(ms)
        return p

    async def say(self, text: str, lang: str, persona: str, moment: str | None = None) -> Path:
        return await self.synth(text, voice_for(lang, persona, moment))

    def cost_usd(self) -> float:
        return round(self.stats["chars"] * PRICE_PER_M / 1e6, 4)

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None


_shared: Speaker | None = None


def shared() -> Speaker:
    """One speaker per process: one cache, one connection pool, one token."""
    global _shared
    if _shared is None:
        _shared = Speaker()
    return _shared


def main():
    ap = argparse.ArgumentParser(description="Voice one line with Azure AI Speech")
    ap.add_argument("text")
    ap.add_argument("--lang", default="en", choices=LANGS)
    ap.add_argument("--persona", default="casual", choices=sorted(ROLE))
    ap.add_argument("--moment", default=None)
    ap.add_argument("--cache", default=None)
    a = ap.parse_args()

    async def go():
        sp = Speaker(a.cache)
        try:
            p = await sp.say(a.text, a.lang, a.persona, a.moment)
        finally:
            await sp.aclose()
        print(json.dumps({"file": str(p), "voice": voice_for(a.lang, a.persona, a.moment).name, **sp.stats,
                          "usd": sp.cost_usd()}))
    asyncio.run(go())


if __name__ == "__main__":
    main()
