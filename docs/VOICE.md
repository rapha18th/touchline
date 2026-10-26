# Voiced commentary

Touchline speaks its commentary with Azure AI Speech. Every verified analyst and casual fan line is voiced in English, and in Spanish, French and Portuguese wherever its translation passed. Those are the four languages the product speaks. The fan app plays the line that is on air, in the viewer's language, one line at a time. No other language is voiced: `voice.voice_for` raises for any language outside the four.

The voice reads exactly the text that airs: the verified English line, or the Localiser's translation that carried every number intact. Nothing is spoken that the Verifier did not pass.

## Voices

Two voices per language. The caller is lively and speaks for the casual fan and the studio. The analyst is calmer and steadier.

| Language | Caller (casual, studio) | Analyst |
| --- | --- | --- |
| English | en-GB-RyanNeural, style cheerful, rate +6% | en-GB-SoniaNeural |
| Spanish | es-MX-JorgeNeural, style cheerful, rate +6% | es-MX-DaliaNeural |
| French | fr-FR-HenriNeural, rate +6%, pitch +2% | fr-FR-DeniseNeural |
| Portuguese | pt-BR-AntonioNeural, rate +6%, pitch +2% | pt-BR-FranciscaNeural |

On a goal or a red card the caller lifts: rate +14%, pitch +6%. Only the English and Spanish callers carry a speaking style; the French and Portuguese callers lift with rate and pitch alone. Studio captions are terse broadcast text and are not voiced.

In French, Spanish and Portuguese the voice says numbers the way a viewer in that language hears them. A decimal is spoken with a comma, 0,21 for 0.21. A score is spoken in words, "deux à zéro", "dos a cero", "dois a zero" for 2-0. The caption keeps the verified text. `voice.spoken` makes the change inside the SSML only, and the cache key follows the spoken text, so a line whose spoken form changes is voiced again.

Each clip is SSML with only the markup that changes delivery, because markup inside `<voice>` is billed. Clips are mp3 at 32 kbit/s, 16 kHz mono (`audio-16khz-32kbitrate-mono-mp3`, the smallest mp3 the service offers), cached on disk under a hash of the text, the voice settings and the format. A line is billed once.

## Keyless auth

Speech runs on the AIServices account `touchline-ai-rm` in `rg-touchline`, the same account as the language models. No key is used anywhere.

- Endpoint: `https://touchline-ai-rm.cognitiveservices.azure.com/tts/cognitiveservices/v1`, the account's custom domain. Microsoft Learn ("How to configure Microsoft Entra authentication" for Speech) requires a custom domain for Entra auth.
- Token: a Microsoft Entra access token for the scope `https://cognitiveservices.azure.com/.default`, sent as `Authorization: Bearer <token>`. On the custom domain the plain bearer token works; every real call in this document used it.
- Credential: the container's managed identity in Azure, the Azure CLI login on a laptop (`crew/llm.py credential()`). One token serves every request and is refreshed five minutes before expiry.
- Role: Learn names **Cognitive Services Speech User** (or Speech Contributor). The signed in user holds Foundry User on the account, whose data actions cover Speech, and every synthesis here ran on that.

`infra/deploy.sh` grants the container app's system identity AcrPull on the registry and Cognitive Services OpenAI User on the account. Neither covers Speech. The Speech role is a change to access control, so the script leaves it to the owner. Run this once before switching voice on in Azure. The `PRINCIPAL` and `S` lines look up the identity and the account:

```bash
export MSYS_NO_PATHCONV=1
PRINCIPAL=$(az containerapp show -n touchline -g rg-touchline --query identity.principalId -o tsv)
S=$(az cognitiveservices account show -n touchline-ai-rm -g rg-touchline --query id -o tsv)
az role assignment create --assignee-object-id "$PRINCIPAL" \
  --assignee-principal-type ServicePrincipal --role "Cognitive Services Speech User" --scope "$S"
```

## Price and spend

Standard neural voices cost $15 per million characters. That is the `S1 Neural Text To Speech Characters` meter for Azure Speech in swedencentral, read from the Azure Retail Prices API:

```bash
curl -s -G https://prices.azure.com/api/retail/prices \
  --data-urlencode "\$filter=armRegionName eq 'swedencentral' and contains(meterName,'Neural')"
```

`voice.PRICE_PER_M` holds it. The service bills every character inside `<voice>`, markup included, and `voice.billed_chars` counts the same way.

| What is in the repository | Clips | Characters | USD from an empty cache |
| --- | ---: | ---: | ---: |
| TL-000008 broadcast | 192 | 32,965 | 0.49 |
| TL-000009 broadcast | 165 | 27,026 | 0.41 |
| TL-000012 broadcast | 200 | 32,763 | 0.49 |
| All three | 557 | 92,754 | 1.39 |

These are `clips`, `match_chars` and `match_usd` in `reports/voice.json`, which covers the three broadcasts that ship. Each was voiced from an empty cache, so what its run paid (`billed_chars` and `usd`) equals the whole-match figure. Spend on earlier voicings, since replaced, is kept in no artifact.

## The three places voice lives

### 1. `services/live/touchline_live/voice.py`

`Speaker.say(text, lang, persona, moment)` returns the mp3 path, from the cache when it is there. Four requests at a time, up to four attempts with backoff on dropped connections, 408, 429 and 5xx, and a fresh token on 401.

```bash
python -m touchline_live.voice "Garo marque de 18 mètres." --lang fr --persona casual --moment goal
```

In a spot check, one real call per language, en-GB-RyanNeural (goal lift), es-MX-DaliaNeural, fr-FR-HenriNeural (goal lift) and pt-BR-FranciscaNeural each returned an mp3. A dropped connection on the Spanish call was carried by the retries.

### 2. The crew's Voice step

With `TOUCHLINE_VOICE=1` the crew graph gains a Voice executor between the Localiser and the Director:

```
Producer -> Analyst -> Narrator -> Verifier -ok-> Localiser -> Voice -> Director -> cues
```

It voices each analyst and casual line in every language the Localiser carried and sets `payload.audio = {lang: url}` on the line cue. The step waits at most `TOUCHLINE_VOICE_BUDGET_S` seconds (default 6) per moment. A clip that fails or is late is left out and the line airs without it; a late clip still finishes into the cache. The trace records the step's time in `ms` with `clips`, `failed`, `late` and `chars` for every moment. The server serves clips at `GET /api/audio/{hash}.mp3` from the live cache (`TOUCHLINE_VOICE_CACHE`, default `data/voice`) and `GET /api/audio/{match}/{hash}.mp3` from a recording. Names are hashes, so the route serves with an immutable cache header and refuses any other path.

To run the live crew with voice on a showcase match, up to 23:20, in all four languages:

```bash
python -m touchline_live.live TL-000008 --speed 0 --langs es fr pt --until 1400 --voice
```

The Voice step's time is added before the line airs. The run writes its cues and report under `data/runs/`, and the report gives the speaker's totals under `voice`.

### 3. Recorded broadcasts

```bash
python services/live/tools/voice_broadcast.py TL-000008 TL-000009 TL-000012 --dry-run    # cost estimate, no calls
python services/live/tools/voice_broadcast.py TL-000008 TL-000009 TL-000012 --localise   # translate what is missing, voice, report
python services/live/tools/voice_broadcast.py TL-000008 --localise --no-voice            # translate only
```

The tool voices every crew analyst and casual line of `broadcasts/<match>.cues.jsonl` into `broadcasts/audio/<match>/`, writes the refs into the jsonl, removes clips no line points to, and writes `reports/voice.json`. A budget gate (`--max-usd`, default 3) refuses a match before any call when its estimate is over. Clips the network drops are retried once, one at a time. A rerun pays only for lines that changed.

`--localise` fills the translations a recording lacks. It asks the crew's Localiser (same model, same instructions) for each crew line and recap that is missing a requested language, and keeps a translation only when its numbers match the English line, it carries no dash, and it is really translated. A translation that comes back as the English line, compared with spaces and case folded, is refused with the reason `untranslated`. The live Localiser applies the same rule (`verify.translation_problem`) and records each refusal in the crew trace. Translations in a language Touchline does not speak are dropped.

The three broadcasts that ship were recorded by the crew with the live Voice step off: `voice` is null in each `broadcasts/<match>.cues.report.json`. The live Localiser carried Spanish, French and Portuguese for every analyst and casual line but one. This tool then voiced them with `--localise`, in all four languages. It found nothing missing in TL-000008 and TL-000012. In TL-000009 it asked again for the one analyst line without translations. The Localiser handed the English back in Spanish, French and Portuguese, live and again here, and all three were refused as `untranslated`. That line is voiced in English only. `reports/voice.json` lists the refusals, with the line's text, under `localised`.

A run with `--langs` rebuilds only those languages. The clips of the other languages stay, and a clip file is removed only when no line in any language points to it.

| Match | Clips | Audio | Spoken | English | Spanish | French | Portuguese |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| TL-000008 | 192 | 8.25 MB | 34.4 min | 494 s | 520 s | 494 s | 555 s |
| TL-000009 | 165 | 6.93 MB | 28.9 min | 424 s | 440 s | 406 s | 463 s |
| TL-000012 | 200 | 8.31 MB | 34.6 min | 500 s | 528 s | 492 s | 557 s |

From `reports/voice.json`. Spoken time is estimated from file size at 32 kbit/s. Every clip was synthesised in its run. None failed and none needed a retry. TL-000009 has 42 English clips and 41 in each other language, because of the refused line. Synthesis took a median 0.99 seconds per clip on TL-000008 (1.6 seconds at the 95th percentile), 0.95 seconds on TL-000009 (1.6 seconds) and 0.93 seconds on TL-000012 (1.9 seconds). The clips are in `broadcasts/audio/<match>/`, one folder per broadcast.

## The fan app

The rail has a **Commentary voice** control, Off or On, Off by default (`web/js/voice.js`, wired in `web/js/app.js`).

- Nothing plays before a gesture. The click on On unlocks one audio element with a tenth of a second of silence. A stored On from an earlier visit waits for the first click or key on the page, and the hint says so.
- A line is spoken when it airs on this viewer's screen, in the chosen language. A line with no clip in that language airs silent.
- One line at a time. A line that waited behind another longer than 3.5 seconds, or longer than its ttl divided by the replay speed when that is shorter, is skipped as stale. The speed control sets it (`Commentary.setSpeed`).
- A goal or a red card (priority 3) clears the lines still waiting and cuts a smaller line that is already speaking.
- Seeking, switching between recorded and live, or changing persona stops the voice. Pause holds the clip and play resumes it.

The test suite replays the three recorded broadcasts through the app's `Commentary` at match pace, for both personas and all four languages. Each line airs at `airTime` from `web/js/match.js`, the crew's measured latency after its moment, exactly when the app airs it, and each clip lasts as long as its file. `node --test web/tests/voice.test.mjs` prints the counts. TL-000008 as the casual fan in French: 24 lines aired, 18 spoken (2 of them cut by a big moment), 6 skipped. TL-000009 as the analyst in Portuguese: 20 lines with a clip aired, 14 spoken (1 cut), 6 skipped. The refused line has no Portuguese clip and airs silent. TL-000012 as the casual fan in English: 25 aired, 21 spoken (4 cut), 4 skipped. Every spoken clip is in the chosen language and on disk.

One earlier run checked the control in a browser against a local server, on a broadcast since retired: the casual line after the first goal played to the end; after a reload with the stored On, nothing played until the play button was pressed; the arrow key seek stopped a clip mid line. No console errors. That browser check has not been repeated on the three broadcasts that ship.

## Tests

```bash
python -m pytest services/live/tests/test_voice.py     # 18 tests: voices, SSML, spoken numbers, Entra signing, cache, Voice step, budget, tool, localise, untranslated lines, one-language rerun, route
node --test web/tests/voice.test.mjs                   # 11 tests: gesture, language, one at a time, stale, speed, goal, cut, stop, a clip the browser refuses, the three broadcasts replayed
```

No test calls Azure: the endpoint is a mock transport, the crew's speaker is a fake, and the Localiser in the tool test is a fake that breaks a number on purpose.

## Limits

- A voiced moment waits for its clips before it airs. The budget caps the wait at six seconds.
- Clusters of moments leave lines unspoken: the TL-000008 analyst replay in English speaks 17 of 24 lines, because a line that waits too long, or waits behind a goal or a red card, is skipped.
- On a slow network the first burst of requests sometimes fails to connect. The retries and the cache cover it; the report records any clip that still failed.
- The Localiser keeps unit words such as "metre" as written in some French and Portuguese lines, because its instructions keep every number and name intact. The number rule passes them.
- `reports/voice.json` keeps the last run per match and covers the three broadcasts that ship. `match_chars` and `match_usd` state the whole match from an empty cache; `synthesised` and `usd` state what that run paid.
- Line ids in the recorded broadcasts repeat across moments (the Director's counter restarts per job). Voice plays by clip address and does not depend on them.
- Voice in Azure stays off until the container identity holds the Speech role and `TOUCHLINE_VOICE=1` is set on the container app. The image sets `TOUCHLINE_VOICE_CACHE=/app/data/voice`, and `infra/deploy.sh` holds the app at one replica, so a live channel's socket and the clips its crew voices share one process and one cache.
