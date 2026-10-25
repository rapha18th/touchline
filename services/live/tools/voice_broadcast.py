"""Voice the crew's lines in recorded broadcasts with Azure AI Speech.

    python services/live/tools/voice_broadcast.py TL-000007 TL-000011
    python services/live/tools/voice_broadcast.py TL-000008 --dry-run        # cost and size estimate only
    python services/live/tools/voice_broadcast.py TL-000007 --localise       # translate what is missing first

For every verified analyst and casual line, in every voiced language the line carries, this writes an mp3 to
broadcasts/audio/<match>/ and puts its address in the line's payload.audio, {lang: url}. The text
voiced is exactly the text that airs: the verified English line or the Localiser's translation.
Clips are cached by a hash of text and voice, so a rerun only pays for lines that changed, and
files no line points to any more are removed. A budget gate refuses a run whose estimated cost
is over --max-usd before any call is made. The run writes reports/voice.json.

With --localise, crew lines and recaps that lack a translation in a requested language get one from the
crew's Localiser (same model, instructions and number rule as the live crew), and translations in a
language Touchline does not speak are dropped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from touchline_live import voice as tts  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
PERSONAS = ("analyst", "casual")


def plan(cues: list[dict], langs: list[str], personas=PERSONAS) -> list[tuple[int, str, str, str, str]]:
    """(cue index, persona, lang, text, moment) for every clip the broadcast needs."""
    out = []
    for i, c in enumerate(cues):
        p = c.get("payload", {})
        if c.get("kind") != "line" or c.get("author") != "crew" or p.get("persona") not in personas:
            continue
        texts = {"en": p.get("text"), **(p.get("translations") or {})}
        for lang in langs:
            if lang not in tts.LANGS or not texts.get(lang):
                continue
            out.append((i, p["persona"], lang, texts[lang], p.get("moment")))
    return out


def needs_text(cues: list[dict], langs: list[str]) -> tuple[list[tuple[int, str, list[str]]], int]:
    """Crew lines and recaps that lack a translation in langs, and how many translations are in a language
    Touchline does not speak. Those are dropped here, so a broadcast carries only the product's languages."""
    want = [l for l in langs if l != "en"]
    todo, dropped = [], 0
    for i, c in enumerate(cues):
        p = c.get("payload", {})
        if c.get("author") != "crew" or not (c.get("kind") == "recap" or p.get("persona") in PERSONAS):
            continue
        tr = p.setdefault("translations", {})
        for lang in [l for l in tr if l not in tts.LANGS]:
            del tr[lang]
            dropped += 1
        text = p.get("story") if c.get("kind") == "recap" else p.get("text")
        missing = [l for l in want if not tr.get(l)]
        if text and missing:
            todo.append((i, text, missing))
    return todo, dropped


async def localise(cues: list[dict], langs: list[str], agent=None, concurrency: int = 4) -> dict:
    """Fill missing translations with the crew's Localiser: same model, same instructions, same rule.
    A translation whose numbers differ from the English line, that carries a dash, or that is the English
    line handed back untranslated, is left out and recorded as refused with its reason."""
    from touchline_live.crew.verify import translation_problem
    todo, dropped = needs_text(cues, langs)
    rep = {"lines": len(todo), "dropped_other_langs": dropped, "added": 0, "refused": [], "failed": []}
    if not todo:
        return rep
    if agent is None:
        from agent_framework import Agent

        from touchline_live.crew import llm, prompts
        agent = Agent(client=llm.client(llm.FAST), name="localiser", instructions=prompts.TRANSLATOR)
    from touchline_live.crew.models import Translations
    sem = asyncio.Semaphore(concurrency)

    async def one(i: int, text: str, missing: list[str]):
        async with sem:
            for attempt in range(3):
                try:
                    r = await agent.run(json.dumps({"text": text, "languages": missing}, ensure_ascii=False),
                                        options={"response_format": Translations, "reasoning": {"effort": "none"}})
                    return i, text, missing, r.value.items
                except Exception as exc:  # noqa: BLE001 - the network here drops connections
                    err = f"{type(exc).__name__}: {str(exc)[:120]}"
                    await asyncio.sleep(1.5 * 2 ** attempt)
            return i, text, missing, err

    for i, text, missing, items in await asyncio.gather(*[one(*t) for t in todo]):
        if isinstance(items, str):
            rep["failed"].append({"line": cues[i]["id"], "error": items})
            continue
        tr = cues[i]["payload"]["translations"]
        for it in items:
            if it.lang not in missing or not it.text.strip():
                continue
            why = translation_problem(text, it.text)
            if why is None:
                tr[it.lang] = it.text
                rep["added"] += 1
            else:
                rep["refused"].append({"line": cues[i]["id"], "lang": it.lang, "reason": why, "text": it.text[:160]})
    return rep


def referenced(cues: list[dict], mid: str) -> set[str]:
    """Every clip file a line in the broadcast still points to, in any language."""
    prefix = f"/api/audio/{mid}/"
    return {url[len(prefix):] for c in cues if c.get("kind") == "line"
            for url in (c.get("payload", {}).get("audio") or {}).values() if url.startswith(prefix)}


def read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write(path: Path, cues: list[dict]):
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(c, ensure_ascii=False, separators=(",", ":")) + "\n" for c in cues), encoding="utf-8")
    tmp.replace(path)


async def voice_match(mid: str, broadcasts: Path, langs: list[str], speaker_factory=tts.Speaker,
                      dry_run: bool = False, max_usd: float = 3.0, localise_with=None, voice: bool = True) -> dict:
    src = broadcasts / f"{mid}.cues.jsonl"
    cues = read(src)
    loc = None
    if localise_with is not None:
        if dry_run:
            loc = {"lines": len(needs_text(cues, langs)[0])}
            cues = read(src)
        else:
            # Translate first and write it down, so a voice run that fails later keeps the text.
            loc = await localise(cues, langs, agent=None if localise_with == "crew" else localise_with)
            write(src, cues)
    out_dir = broadcasts / "audio" / mid
    sp = speaker_factory(out_dir)
    jobs = plan(cues, langs)
    # Estimate before spending: billed characters of every clip not already on disk.
    todo = [(i, persona, lang, text, moment) for i, persona, lang, text, moment in jobs
            if not sp.path(text, tts.voice_for(lang, persona, moment)).exists()]
    est_chars = sum(tts.billed_chars(tts.ssml(text, tts.voice_for(lang, persona, moment)))
                    for _, persona, lang, text, moment in todo)
    est_usd = round(est_chars * tts.PRICE_PER_M / 1e6, 4)
    # What the whole match costs to voice from an empty cache, so a rerun's report still states it.
    all_chars = sum(tts.billed_chars(tts.ssml(text, tts.voice_for(lang, persona, moment)))
                    for _, persona, lang, text, moment in jobs)
    rep = {"match": mid, "clips": len(jobs), "to_synthesise": len(todo), "estimated_chars": est_chars,
           "estimated_usd": est_usd, "match_chars": all_chars,
           "match_usd": round(all_chars * tts.PRICE_PER_M / 1e6, 4)}
    if loc is not None:
        rep["localised"] = loc
    if not voice:
        return rep
    if dry_run:
        return rep
    if est_usd > max_usd:
        rep["refused"] = f"estimated ${est_usd} is over the ${max_usd} budget"
        return rep

    t0 = time.perf_counter()
    done = await asyncio.gather(*[sp.say(text, lang, persona, moment) for _, persona, lang, text, moment in jobs],
                                return_exceptions=True)
    # A second pass for clips the network dropped, one at a time: the first burst is the one that fails.
    retry = [k for k, res in enumerate(done) if isinstance(res, BaseException)]
    for k in retry:
        _, persona, lang, text, moment = jobs[k]
        try:
            done[k] = await sp.say(text, lang, persona, moment)
        except Exception as exc:  # noqa: BLE001 - recorded in the report below
            done[k] = exc
    rep["retried"] = len(retry)
    await sp.aclose()
    failed, voiced = [], set()
    # Only this run's languages are rebuilt: a run with --langs fr keeps the English, Spanish and
    # Portuguese clips the broadcast already carries.
    for c in cues:
        audio = c.get("payload", {}).get("audio") if c.get("kind") == "line" else None
        if audio:
            for lang in langs:
                audio.pop(lang, None)
            if not audio:
                c["payload"].pop("audio")
    for (i, persona, lang, text, _), res in zip(jobs, done):
        if isinstance(res, BaseException):
            failed.append({"line": cues[i]["id"], "lang": lang, "error": str(res)[:200]})
            continue
        cues[i]["payload"].setdefault("audio", {})[lang] = f"/api/audio/{mid}/{res.name}"
        voiced.add(res.name)
    write(src, cues)
    keep = referenced(cues, mid)
    removed = 0
    for f in out_dir.glob("*.mp3"):
        if f.name not in keep:
            f.unlink()
            removed += 1
    sizes = [(out_dir / n).stat().st_size for n in voiced]
    lat = sorted(sp.latencies)
    by_lang = {}
    for (_, persona, lang, _, _), res in zip(jobs, done):
        if not isinstance(res, BaseException):
            b = by_lang.setdefault(lang, {"clips": 0, "seconds": 0.0})
            b["clips"] += 1
            b["seconds"] = round(b["seconds"] + res.stat().st_size * 8 / 32000, 1)
    rep.update({
        "voiced": len(voiced), "failed": failed, "synthesised": sp.stats["synth"], "cached": sp.stats["cached"],
        "billed_chars": sp.stats["chars"], "usd": sp.cost_usd(), "removed_stale_files": removed,
        "audio_mb": round(sum(sizes) / 1e6, 2), "mean_clip_kb": round(statistics.mean(sizes) / 1024, 1) if sizes else 0,
        "seconds_of_audio": round(sum(sizes) * 8 / 32000), "by_lang": by_lang,
        "synth_ms_median": lat[len(lat) // 2] if lat else None,
        "synth_ms_p95": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None,
        "wall_s": round(time.perf_counter() - t0, 1),
        "voices": {f"{lang}/{role}": v.name for (lang, role), v in tts.VOICES.items()}, "format": tts.FORMAT,
    })
    return rep


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("matches", nargs="+")
    ap.add_argument("--broadcasts", default=str(ROOT / "broadcasts"))
    ap.add_argument("--langs", nargs="+", default=list(tts.LANGS))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--localise", action="store_true",
                    help="fill missing translations with the crew's Localiser before voicing")
    ap.add_argument("--no-voice", action="store_true", help="with --localise: translate, and voice nothing")
    ap.add_argument("--max-usd", type=float, default=3.0, help="refuse a match whose estimate is over this")
    ap.add_argument("--report", default=str(ROOT / "reports" / "voice.json"))
    a = ap.parse_args()
    bad = [l for l in a.langs if l not in tts.LANGS]
    if bad:
        ap.error(f"Touchline does not voice {', '.join(bad)}")

    async def go():
        return [await voice_match(m, Path(a.broadcasts), a.langs, dry_run=a.dry_run, max_usd=a.max_usd,
                                  localise_with="crew" if a.localise else None, voice=not a.no_voice)
                for m in a.matches]
    reps = asyncio.run(go())
    for r in reps:
        print(json.dumps(r, ensure_ascii=False))
    if not a.dry_run and not a.no_voice:
        path = Path(a.report)
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        old.update({r["match"]: r for r in reps})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(old, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
