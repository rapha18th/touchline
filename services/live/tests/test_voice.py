"""Voiced commentary: voices, SSML, the cache, the crew's Voice step, the broadcast tool and the audio route.

No test calls Azure: the speech endpoint is a mock transport and the speaker in the crew is a fake.
"""

import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from touchline_live import voice as tts
from touchline_live.crew.models import Job, PersonaLine
from touchline_live.crew.workflow import Director, Voicer, build

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("voice_broadcast", ROOT / "services" / "live" / "tools" / "voice_broadcast.py")
vb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vb)


# ----- voices and SSML ------------------------------------------------------------------------------

def test_only_the_four_product_languages_are_voiced():
    assert tts.LANGS == ("en", "es", "fr", "pt")
    for lang in ("de", "xx", ""):
        with pytest.raises(ValueError):
            tts.voice_for(lang, "casual")
    assert {lang for lang, _ in tts.VOICES} == set(tts.LANGS)
    assert {v.locale[:2] for v in tts.VOICES.values()} == set(tts.LANGS)


def test_a_caller_for_casual_and_studio_and_a_calmer_analyst():
    for lang in tts.LANGS:
        caller, analyst = tts.voice_for(lang, "casual"), tts.voice_for(lang, "analyst")
        assert tts.voice_for(lang, "studio") == caller
        assert caller.name != analyst.name
        assert analyst.rate is None and analyst.style is None
    assert tts.voice_for("fr", "casual").name.startswith("fr-FR-")
    assert tts.voice_for("pt", "analyst").name.startswith("pt-BR-")
    goal = tts.voice_for("en", "casual", "goal")
    assert goal.name == tts.voice_for("en", "casual").name and goal.rate == "+14%"
    assert tts.voice_for("en", "analyst", "goal") == tts.voice_for("en", "analyst")
    with pytest.raises(ValueError):
        tts.voice_for("en", "commentator")


def test_ssml_escapes_text_and_adds_style_only_where_the_voice_has_it():
    en = tts.ssml("Garo & Ilo <score>  twice", tts.voice_for("en", "casual"))
    assert "Garo &amp; Ilo &lt;score&gt; twice" in en
    assert "mstts:express-as style='cheerful'" in en and "<prosody rate='+6%'>" in en
    fr = tts.ssml("Garo marque encore.", tts.voice_for("fr", "casual"))
    assert "express-as" not in fr and "xml:lang='fr-FR'" in fr and "<prosody rate='+6%' pitch='+2%'>" in fr
    plain = tts.ssml("Calm line.", tts.voice_for("en", "analyst"))
    assert tts.billed_chars(plain) == len("Calm line.")


def test_cache_key_follows_text_and_voice():
    v = tts.voice_for("en", "casual")
    assert tts.key("A  line.", v) == tts.key("A line.", v)
    assert tts.key("A line.", v) != tts.key("A line!", v)
    assert tts.key("A line.", v) != tts.key("A line.", tts.voice_for("en", "analyst"))


# ----- the speaker against a mock Speech endpoint --------------------------------------------------

class FakeToken:
    def __init__(self):
        self.calls = 0

    def get_token(self, scope):
        self.calls += 1
        assert scope == tts.SCOPE
        return type("T", (), {"token": "entra-token", "expires_on": 4_000_000_000})()


def speaker_with(tmp_path, handler, cred=None):
    sp = tts.Speaker(tmp_path, endpoint="https://touchline-ai-rm.cognitiveservices.azure.com/", credential=cred or FakeToken())
    sp._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return sp


def test_speaker_signs_with_entra_and_caches_on_disk(tmp_path):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, content=b"ID3mp3")

    cred = FakeToken()
    sp = speaker_with(tmp_path, handler, cred)

    async def go():
        a = await sp.say("Garo scores.", "en", "casual", "goal")
        b = await sp.say("Garo scores.", "en", "casual", "goal")
        c = await sp.say("Garo marca.", "es", "casual", "goal")
        await sp.aclose()
        return a, b, c
    a, b, c = asyncio.run(go())
    assert a == b and a.read_bytes() == b"ID3mp3" and c != a
    assert len(seen) == 2 and cred.calls == 1
    r = seen[0]
    assert str(r.url) == "https://touchline-ai-rm.cognitiveservices.azure.com/tts/cognitiveservices/v1"
    assert r.headers["Authorization"] == "Bearer entra-token"
    assert r.headers["Content-Type"] == "application/ssml+xml"
    assert r.headers["X-Microsoft-OutputFormat"] == "audio-16khz-32kbitrate-mono-mp3"
    assert b"en-GB-RyanNeural" in r.content
    assert sp.stats["synth"] == 2 and sp.stats["cached"] == 1 and sp.cost_usd() > 0


def test_speaker_retries_dropped_requests_and_gives_up_cleanly(tmp_path, monkeypatch):
    async def no_wait(_):
        return None
    monkeypatch.setattr(tts.asyncio, "sleep", no_wait)
    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("dropped", request=req)
        return httpx.Response(503) if calls["n"] == 2 else httpx.Response(200, content=b"ok")

    sp = speaker_with(tmp_path, flaky)
    p = asyncio.run(sp.say("Line one.", "es", "analyst"))
    assert p.read_bytes() == b"ok" and calls["n"] == 3

    def refused(req):
        return httpx.Response(403, text="no role")
    sp = speaker_with(tmp_path / "b", refused)
    with pytest.raises(tts.SpeechError, match="403"):
        asyncio.run(sp.say("Line two.", "pt", "analyst"))
    assert not list((tmp_path / "b").glob("*.mp3"))


# ----- the crew's Voice step --------------------------------------------------------------------------

class FakeSpeaker:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.stats = {"chars": 0}
        self.calls = []

    async def say(self, text, lang, persona, moment=None):
        self.calls.append((persona, lang, text, moment))
        if (persona, lang) in self.fail:
            raise tts.SpeechError("dropped")
        self.stats["chars"] += len(text)
        return Path(f"{persona}-{lang}.mp3")


def job():
    return Job(match_id="TL-000099", kind="goal", match_s=600.0, frame=7500, period=1, priority=3,
               facts={}, sources=[12], context={}, personas=["analyst", "casual", "studio"],
               langs=["en", "es", "fr", "de"],
               lines=[PersonaLine(persona="analyst", text="Garo has 2 goals.", sources=[12]),
                      PersonaLine(persona="casual", text="Garo again!", sources=[12]),
                      PersonaLine(persona="studio", text="GOAL GARO 10", sources=[12])],
               translations={"analyst": {"es": "Garo lleva 2 goles.", "fr": "Garo a 2 buts.", "de": "Garo hat 2 Tore."},
                             "casual": {"es": "Â¡Garo otra vez!", "fr": "Encore Garo !", "pt": "Garo de novo!"}})


def run_voice(speaker, budget_s=6.0):
    from agent_framework import WorkflowBuilder
    cues = []
    voicer, director = Voicer(speaker, budget_s=budget_s), Director(cues.append)
    wf = WorkflowBuilder(name="voice-test", start_executor=voicer).add_edge(voicer, director).build()
    out = asyncio.run(wf.run(job())).get_outputs()
    return cues, out


def test_voice_step_attaches_audio_per_language_and_only_in_voiced_languages():
    sp = FakeSpeaker(fail={("casual", "fr")})
    cues, out = run_voice(sp)
    lines = {c["payload"]["persona"]: c for c in cues if c["kind"] == "line"}
    assert lines["analyst"]["payload"]["audio"] == {
        "en": "/api/audio/analyst-en.mp3", "es": "/api/audio/analyst-es.mp3", "fr": "/api/audio/analyst-fr.mp3"}
    # A clip that fails is left out and the line still airs, on time, with the rest of its audio.
    assert lines["casual"]["payload"]["audio"] == {"en": "/api/audio/casual-en.mp3", "es": "/api/audio/casual-es.mp3"}
    assert "audio" not in lines["studio"]["payload"]
    # German has no voice, and Portuguese is not a language this job carries.
    assert not any(lang in ("de", "pt") for _, lang, _, _ in sp.calls)
    assert ("analyst", "es", "Garo lleva 2 goles.", "goal") in sp.calls
    step = [t for t in out[0]["trace"] if t["role"] == "voice"][0]
    assert step["clips"] == 5 and step["failed"] == 1 and step["late"] == 0 and step["chars"] > 0


class SlowSpeaker(FakeSpeaker):
    async def say(self, text, lang, persona, moment=None):
        if (persona, lang) == ("analyst", "fr"):
            await asyncio.sleep(1.0)
        return await super().say(text, lang, persona, moment)


def test_voice_step_never_holds_a_line_past_its_budget():
    cues, out = run_voice(SlowSpeaker(), budget_s=0.2)
    lines = {c["payload"]["persona"]: c for c in cues if c["kind"] == "line"}
    assert set(lines["analyst"]["payload"]["audio"]) == {"en", "es"}       # French was late: the line airs without it
    assert set(lines["casual"]["payload"]["audio"]) == {"en", "es", "fr"}
    step = [t for t in out[0]["trace"] if t["role"] == "voice"][0]
    assert step["late"] == 1 and step["clips"] == 5 and step["ms"] < 900


def test_voice_step_joins_the_graph_only_when_switched_on(monkeypatch):
    monkeypatch.delenv("TOUCHLINE_VOICE", raising=False)
    assert "voice" not in build(None, print).executors
    assert "voice" in build(None, print, speaker=FakeSpeaker()).executors
    monkeypatch.setenv("TOUCHLINE_VOICE", "1")
    monkeypatch.setattr(tts, "_shared", FakeSpeaker())
    assert "voice" in build(None, print).executors


# ----- the broadcast tool ---------------------------------------------------------------------------

def cue(i, persona, text, tr=None, kind="line", author="crew"):
    return {"id": f"TL-000099-l{i:04d}", "kind": kind, "author": author, "match_s": 10.0 * i,
            "payload": {"text": text, "persona": persona, "moment": "shot", "translations": tr or {}}}


class DiskSpeaker(tts.Speaker):
    def __init__(self, cache_dir):
        super().__init__(cache_dir, credential=FakeToken())
        self._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"\xff" * 4000)))


def test_broadcast_tool_voices_lines_writes_refs_and_removes_stale_clips(tmp_path):
    cues = [cue(1, "analyst", "Shot from 20 metres.", {"es": "Tiro desde 20 metros.", "de": "Schuss."}),
            cue(2, "casual", "Close!", {"pt": "Quase!"}),
            cue(3, "studio", "SHOT"),
            cue(4, "analyst", "Template line.", author="state")]
    src = tmp_path / "TL-000099.cues.jsonl"
    src.write_text("".join(json.dumps(c) + "\n" for c in cues), encoding="utf-8")
    stale = tmp_path / "audio" / "TL-000099" / "0123456789abcdef0123.mp3"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")

    assert {(p, l) for _, p, l, _, _ in vb.plan(cues, ["en", "es", "pt", "de"])} == {
        ("analyst", "en"), ("analyst", "es"), ("casual", "en"), ("casual", "pt")}

    refused = asyncio.run(vb.voice_match("TL-000099", tmp_path, ["en", "es", "pt"], DiskSpeaker, max_usd=0.0))
    assert "refused" in refused and "audio" not in src.read_text(encoding="utf-8")

    rep = asyncio.run(vb.voice_match("TL-000099", tmp_path, ["en", "es", "pt"], DiskSpeaker))
    assert rep["voiced"] == 4 and rep["failed"] == [] and rep["removed_stale_files"] == 1 and not stale.exists()
    out = [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines()]
    assert set(out[0]["payload"]["audio"]) == {"en", "es"} and set(out[1]["payload"]["audio"]) == {"en", "pt"}
    assert "audio" not in out[2]["payload"] and "audio" not in out[3]["payload"]
    for url in (*out[0]["payload"]["audio"].values(), *out[1]["payload"]["audio"].values()):
        assert url.startswith("/api/audio/TL-000099/") and (tmp_path / "audio" / url[len("/api/audio/"):]).is_file()

    again = asyncio.run(vb.voice_match("TL-000099", tmp_path, ["en", "es", "pt"], DiskSpeaker))
    assert again["to_synthesise"] == 0 and again["synthesised"] == 0 and again["cached"] == 4


class FakeLocaliser:
    """Answers like the crew's Localiser; the French analyst line changes a number, so the rule drops it."""
    def __init__(self):
        self.asked = []

    async def run(self, prompt, options=None):
        from types import SimpleNamespace as NS

        from touchline_live.crew.models import Translation, Translations
        ask = json.loads(prompt)
        self.asked.append(ask)
        tx = {("Shot from 20 metres.", "fr"): "Tir de 21 mÃ¨tres.", ("Shot from 20 metres.", "pt"): "Chute de 20 metros.",
              ("Close!", "fr"): "Presque !", ("Close!", "es"): "Â¡Casi!", ("Half time, 1-0.", "fr"): "Mi-temps, 1-0."}
        return NS(value=Translations(items=[Translation(lang=l, text=tx[(ask["text"], l)])
                                            for l in ask["languages"] if (ask["text"], l) in tx]))


def test_localise_fills_missing_languages_keeps_numbers_and_drops_other_languages(tmp_path):
    cues = [cue(1, "analyst", "Shot from 20 metres.", {"es": "Tiro desde 20 metros.", "de": "Schuss."}),
            cue(2, "casual", "Close!", {"pt": "Quase!"}),
            cue(3, "studio", "SHOT"),
            {"id": "r1", "kind": "recap", "author": "crew", "payload": {"story": "Half time, 1-0.", "translations": {}}}]
    src = tmp_path / "TL-000099.cues.jsonl"
    src.write_text("".join(json.dumps(c) + "\n" for c in cues), encoding="utf-8")
    loc = FakeLocaliser()
    rep = asyncio.run(vb.voice_match("TL-000099", tmp_path, ["en", "es", "fr", "pt"], DiskSpeaker, localise_with=loc))
    out = [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines()]
    assert out[0]["payload"]["translations"] == {"es": "Tiro desde 20 metros.", "pt": "Chute de 20 metros."}
    assert out[1]["payload"]["translations"] == {"pt": "Quase!", "es": "Â¡Casi!", "fr": "Presque !"}
    assert out[3]["payload"]["translations"] == {"fr": "Mi-temps, 1-0."}
    assert "translations" not in out[2]["payload"] or not out[2]["payload"]["translations"]
    assert {"text": "Shot from 20 metres.", "languages": ["fr", "pt"]} in loc.asked     # only what is missing
    lr = rep["localised"]
    assert lr["dropped_other_langs"] == 1 and lr["added"] == 4 and [r["lang"] for r in lr["refused"]] == ["fr"]
    assert set(out[0]["payload"]["audio"]) == {"en", "es", "pt"} and set(out[1]["payload"]["audio"]) == {"en", "es", "fr", "pt"}


def test_recorded_broadcasts_point_at_clips_that_exist():
    for mid in ("TL-000007", "TL-000011"):
        src = ROOT / "broadcasts" / f"{mid}.cues.jsonl"
        refs = [u for c in vb.read(src) for u in (c.get("payload", {}).get("audio") or {}).values()]
        langs = {l for c in vb.read(src) for l in (c.get("payload", {}).get("audio") or {})}
        assert refs and langs == set(tts.LANGS)
        for u in refs:
            assert (ROOT / "broadcasts" / "audio" / u[len("/api/audio/"):]).is_file(), u


# ----- the audio route --------------------------------------------------------------------------------

def test_audio_route_serves_clips_by_hash_only():
    from fastapi.testclient import TestClient

    from touchline_live import server
    c = TestClient(server.app)
    src = ROOT / "broadcasts" / "TL-000007.cues.jsonl"
    url = next(u for cu in vb.read(src) for u in (cu.get("payload", {}).get("audio") or {}).values())
    r = c.get(url)
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg" and r.content[:1] in (b"I", b"\xff")
    assert "immutable" in r.headers["cache-control"]
    for bad in ("/api/audio/TL-000007/..%2F..%2FREADME.md", "/api/audio/notahash.mp3",
                "/api/audio/TL-000007/0123456789abcdef0123.mp3"):
        assert c.get(bad).status_code == 404
