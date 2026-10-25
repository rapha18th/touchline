# Voiced commentary

Touchline speaks its commentary with Azure AI Speech. Every verified analyst and casual fan line is voiced in English, Spanish, French and Portuguese, the four languages the product speaks. The fan app plays the line that is on air, in the viewer's language, one line at a time. No other language is voiced: `voice.voice_for` raises for any language outside the four.

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

Each clip is SSML with only the markup that changes delivery, because markup inside `<voice>` is billed. Clips are mp3 at 32 kbit/s, 16 kHz mono (`audio-16khz-32kbitrate-mono-mp3`, the smallest mp3 the service offers), cached on disk under a hash of the text, the voice settings and the format. A line is billed once.

## Keyless auth

Speech runs on the AIServices account `touchline-ai-rm` in `rg-touchline`, the same account as the language models. No key is used anywhere.

- Endpoint: `https://touchline-ai-rm.cognitiveservices.azure.com/tts/cognitiveservices/v1`, the account's custom domain. Microsoft Learn ("How to configure Microsoft Entra authentication" for Speech) requires a custom domain for Entra auth.
- Token: a Microsoft Entra access token for the scope `https://cognitiveservices.azure.com/.default`, sent as `Authorization: Bearer <token>`. On the custom domain the plain bearer token works; every real call in this document used it.
- Credential: the container's managed identity in Azure, the Azure CLI login on a laptop (`crew/llm.py credential()`). One token serves every request and is refreshed five minutes before expiry.
- Role: Learn names **Cognitive Services Speech User** (or Speech Contributor). The signed in user holds Foundry User on the account, whose data actions cover Speech, and every synthesis here ran on that.

The container app's system identity (principal `d42fcd52-7941-4752-a6c7-37d33640514b`) holds AcrPull and Cognitive Services OpenAI User, checked with `az role assignment list` on 25 October. Neither covers Speech. The role is a change to access control, so the build agent leaves it to the owner. Run this once before switching voice on in Azure:

```bash
export MSYS_NO_PATHCONV=1
S=/subscriptions/65cafbe0-a871-49f9-af08-4913a5903ead/resourceGroups/rg-touchline/providers/Microsoft.CognitiveServices/accounts/touchline-ai-rm
az role assignment create --assignee-object-id d42fcd52-7941-4752-a6c7-37d33640514b \
  --assignee-principal-type ServicePrincipal --role "Cognitive Services Speech User" --scope $S
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
| TL-000007 broadcast | 160 | 26,923 | 0.40 |
| TL-000011 broadcast | 128 | 21,230 | 0.32 |

All Speech spend on this workstream so far is about $1.55 of credit, across every run, the clips later replaced included. This run paid $0.34 for the French and Portuguese broadcast clips, $0.04 for one live crew check and under a cent for four spot checks.

## The three places voice lives

### 1. `services/live/touchline_live/voice.py`

`Speaker.say(text, lang, persona, moment)` returns the mp3 path, from the cache when it is there. Four requests at a time, up to four attempts with backoff on dropped connections, 408, 429 and 5xx, and a fresh token on 401.

```bash
python -m touchline_live.voice "Garo marque de 18 mètres." --lang fr --persona casual --moment goal
```

Spot checks on 25 October, one real call per language: en-GB-RyanNeural (goal lift), es-MX-DaliaNeural, fr-FR-HenriNeural (goal lift) and pt-BR-FranciscaNeural each returned an mp3 of 13 to 19 KB. The Spanish call took 40 seconds because the network here dropped the first connections; the retries carried it.

### 2. The crew's Voice step

With `TOUCHLINE_VOICE=1` the crew graph gains a Voice executor between the Localiser and the Director:

```
Producer -> Analyst -> Narrator -> Verifier -ok-> Localiser -> Voice -> Director -> cues
```

It voices each analyst and casual line in every language the Localiser carried and sets `payload.audio = {lang: url}` on the line cue. The step waits at most `TOUCHLINE_VOICE_BUDGET_S` seconds (default 6) per moment. A clip that fails or is late is left out and the line airs without it; a late clip still finishes into the cache. The trace records `clips`, `failed`, `late` and `chars` for every moment. The server serves clips at `GET /api/audio/{hash}.mp3` from the live cache (`TOUCHLINE_VOICE_CACHE`, default `data/voice`) and `GET /api/audio/{match}/{hash}.mp3` from a recording. Names are hashes, so the route serves with an immutable cache header and refuses any other path.

Measured on TL-000007 up to 23:20 with the live crew, in all four languages:

```bash
python -m touchline_live.live TL-000007 --speed 0 --langs es fr pt --until 1400 --voice
```

Two moments, a hard pass and a goal. Each got eight clips (analyst and casual, four languages), no failures, nothing late. The Voice step took 5.4 and 4.5 seconds, 2,980 characters in all. That time is added before the line airs.

### 3. Recorded broadcasts

```bash
python services/live/tools/voice_broadcast.py TL-000007 TL-000011 --dry-run      # cost estimate, no calls
python services/live/tools/voice_broadcast.py TL-000007 TL-000011 --localise     # translate what is missing, voice, report
python services/live/tools/voice_broadcast.py TL-000008 --localise --no-voice    # translate only
```

The tool voices every crew analyst and casual line of `broadcasts/<match>.cues.jsonl` into `broadcasts/audio/<match>/`, writes the refs into the jsonl, removes clips no line points to, and writes `reports/voice.json`. A budget gate (`--max-usd`, default 3) refuses a match before any call when its estimate is over. Clips the network drops are retried once, one at a time. A rerun pays only for lines that changed.

`--localise` fills the translations a recording lacks. It asks the crew's Localiser (same model, same instructions) for each crew line and recap that is missing a requested language, and keeps a translation only when its numbers match the English line and it carries no dash, the rule the live Localiser applies. Translations in a language Touchline does not speak are dropped. On 25 October it filled French and Portuguese for all three recorded broadcasts: 144 lines and recaps, 288 translations added, none refused by the number rule, no call failed.

| Match | Clips | Audio | Spoken | English | Spanish | French | Portuguese |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| TL-000007 | 160 | 6.80 MB | 28.3 min | 414 s | 432 s | 400 s | 456 s |
| TL-000011 | 128 | 5.26 MB | 21.9 min | 316 s | 330 s | 314 s | 354 s |

Spoken time is estimated from file size at 32 kbit/s. Synthesis took a median 0.9 seconds per clip, 3.6 seconds at the 95th percentile. TL-000008 carries the four languages as text and is not voiced, to keep the repository small (its estimate is 264 clips, $0.67, about 11 MB).

## The fan app

The rail has a **Commentary voice** control, Off or On, Off by default (`web/js/voice.js`, wired in `web/js/app.js`).

- Nothing plays before a gesture. The click on On unlocks one audio element with a tenth of a second of silence. A stored On from an earlier visit waits for the first click or key on the page, and the hint says so.
- A line is spoken when it airs on this viewer's screen, in the chosen language. A line with no clip in that language airs silent.
- One line at a time. A line that waited more than 3.5 seconds behind another is skipped as stale. A goal or a red card clears the lines still waiting.
- Seeking, switching between recorded and live, or changing persona stops the voice. Pause holds the clip and play resumes it.

Checked on 25 October by replaying the recorded broadcasts through the app's `Commentary` at match pace, each clip as long as its file, and fetching every clip it played from a local server. TL-000007 as the casual fan in French: 20 lines aired, 12 spoken, 8 skipped as stale. TL-000011 as the analyst in Portuguese: 16 aired, 12 spoken, 4 stale. Every spoken clip was in the chosen language and came back 200 `audio/mpeg`. The test suite runs the same replay for both broadcasts, both personas and all four languages.

An earlier run checked the control in a browser against a local server: the casual line after the first goal of TL-000007 played to the end; after a reload with the stored On, nothing played until the play button was pressed; the arrow key seek stopped a clip mid line. No console errors. This run's browser pane was hidden and did not render the page, so it was not repeated in a browser.

## Tests

```bash
python -m pytest services/live/tests/test_voice.py     # 13 tests: voices, SSML, Entra signing, cache, Voice step, budget, tool, localise, route
node --test web/tests/voice.test.mjs                   # 8 tests: gesture, language, one at a time, stale, goal, stop, both broadcasts replayed
```

No test calls Azure: the endpoint is a mock transport, the crew's speaker is a fake, and the Localiser in the tool test is a fake that breaks a number on purpose.

## Limits

- The live Voice step adds about five seconds before a voiced moment airs. The budget caps it at six.
- Clusters of moments leave lines unspoken: a viewer heard about three lines in five on TL-000007, because a line that waits more than 3.5 seconds is skipped.
- On this PC's network the first burst of requests sometimes fails to connect. The retries and the cache cover it; the report records any clip that still failed.
- The Localiser keeps unit words such as "metre" as written in some French and Portuguese lines, because its instructions keep every number and name intact. The number rule passes them.
- `reports/voice.json` keeps the last run per match. `match_chars` and `match_usd` state the whole match from an empty cache; `synthesised` and `usd` state what that run paid.
- Line ids in the recorded broadcasts repeat across moments (the Director's counter restarts per job). Voice plays by clip address and does not depend on them.
- Voice in Azure stays off until the container identity holds the Speech role and `TOUCHLINE_VOICE=1` is set on the container app.
