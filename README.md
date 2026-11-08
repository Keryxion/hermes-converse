# hermes-converse

Server-owned realtime voice for [Hermes Agent](https://github.com/NousResearch/hermes-agent), as a
plugin. It adds `GET /v1/audio/converse` to the Hermes API server: a client streams microphone PCM
up one WebSocket and the server does the rest — voice activity detection, endpointing, speech to
text, the agent turn, and per-sentence streaming text to speech back down the same socket, with
barge-in. Built for devices that have a microphone and a speaker and nothing else: an ESP32, a
browser tab, a kiosk.

The agent turn runs through Hermes's public Runs API, so every turn is an ordinary Hermes session:
the agent's tools, memory, skills and approvals all apply, and the conversation continues across
turns on one socket.

## Install

```bash
hermes plugins install Keryxion/hermes-converse
hermes plugins enable hermes-converse
hermes converse doctor      # checks audio libraries, STT/TTS config, the API server and its key
hermes gateway restart      # the route is registered when the API server platform connects
```

Requirements: the `api_server` platform enabled with an `API_SERVER_KEY`, an STT provider
(`stt.provider`, local whisper works) and a TTS provider (`tts.provider`; the default `edge` works
through the one-shot path, a provider with a chunked API such as ElevenLabs or OpenAI gives the
lowest latency). The plugin declares `numpy` and `av` (PyAV); Hermes installs them when you enable it.

## Talk to it

Dial `ws://<host>:<port>/v1/audio/converse` and send one `start` frame first:

```json
{"type": "start", "key": "<API_SERVER_KEY>", "input_rate": 16000, "output_rate": 24000,
 "quiet_interval": 0, "name": "Sakura"}
```

A browser client can put the key in the WebSocket subprotocols instead (`hermes-voice-v1` plus
`hermes-key.<API_SERVER_KEY>`); the server accepts only `hermes-voice-v1` and never echoes the key.
Either way the key is verified against the API server itself, so your existing key is the only
secret.

Then stream binary PCM16 mono frames at `input_rate` (30 ms blocks are ideal). The server answers:

| frame | meaning |
|---|---|
| `ready` | rates, the socket's `session_id`, and two server timestamps for clock-skew estimation |
| `transcript` | what it heard; carries a `turn_id` every later frame of the turn repeats |
| `thinking` | the agent turn is running (show "thinking", not "listening") |
| `speaking` + binary frames | the reply as PCM16 at `output_rate` |
| `interrupted` | you spoke over the reply: stop playing, the turn is cancelled |
| `error` | the turn failed (the message is spoken-safe text) |
| `turn_done` | the reply is fully sent; in session mode `expects_more` says whether to keep listening |
| `quiet` | session mode: the stream has been silent for another `quiet_interval` seconds |
| `stop_word` | session mode: you said a stop phrase; no turn ran |

Handle `thinking` as an explicit turn boundary even without a preceding `transcript`:
mark the agent turn active, stop forwarding microphone PCM for the current capture, and show
"thinking" while keeping the socket open. Arm the client's turn deadline if it is not already
running; do not restart it. This frame reports lifecycle status, not reasoning text. See
[the client spec](client%20spec.md#handling-thinking) for the frame format and tracing behavior.

Client control frames: `{"stop": true}` ends the session, `{"commit": true}` forces the current
utterance to endpoint, `{"type": "cancel", "turn_id": "..."}` cancels the current turn while
keeping the socket, session and history alive, `{"type": "drained", "turn_id": "..."}` says the
reply's last audio has left the speaker (the server holds the VAD until then so its own echo is
never transcribed),
`{"type": "wake", "claim_id": "...", "wake_word": "..."}` requests an advisory wake decision
(see below) — send it when the detector fires, then buffer audio while awaiting the decision.

**Session mode** (`quiet_interval` > 0) is for wake-word devices: the server reports quiet in
*received* audio (never wall-clock, so a held-open socket never accrues phantom quiet), ends the
exchange on a stop phrase, and lets the agent sign off by ending a reply with an end phrase
("over and out"), which arrives as `turn_done.expects_more = false`.

## Settings

All optional, under `plugins.entries.hermes-converse.settings` in `config.yaml` (or the Desktop
plugin settings form):

| key | default | what it does |
|---|---|---|
| `name` | `Sakura` | the assistant's name in the voice prompt when the start frame has none |
| `voice_model`, `voice_provider` | `""` | a voice-only model override sent with every run |
| `turn_timeout_seconds` | `120` | a turn with no output for this long is stopped and reported |
| `playback_tail_timeout_seconds` | `45` | how long to wait for the client's `drained` frame |
| `max_tts_chars_per_turn` | `1500` | safety cap on how much of one reply is spoken |
| `stop_phrases` | `[stop, cancel]` | session mode: saying exactly one of these ends the exchange |
| `end_phrases` | `[over and out]` | session mode: a reply ending with one of these signs off |
| `endpoint_model`, `endpoint_threshold` | off, `0.5` | Smart Turn v3 semantic endpointer (see below) |
| `approval_choice` | `deny` | what a voice turn answers when the agent asks for a command approval |
| `api_base_url` | `""` | loopback URL of the API server when it cannot be derived from the listener |
| `arbitrate_enabled` | `false` | enable advisory wake selection for cooperating clients |
| `arbitrate_policy` | `wake_signal` | rank wake claims; legacy `first_to_endpoint` grants immediately without selection |
| `arbitrate_device_id` | `""` | optional label prefix for server-generated unique socket identifiers |
| `arbitrate_wake_window_seconds` | `0.25` | how long the arbitrator collects concurrent wake claims for the same wake word before electing the primary device |

STT and TTS come from your normal Hermes `stt.*` and `tts.*` configuration.

**Cooperative wake selection.** With `arbitrate_enabled: true` and the default
`wake_signal` policy, devices send a `wake` frame with a client-generated `claim_id`,
the exact detected `wake_word` phrase, and optional `confidence` (0–1), normalized
RMS `volume` (0–1), and `heard_at` (Unix milliseconds). Reports for the same phrase
within a 250 ms window compete: highest confidence, then highest volume, then
earliest detection time, then arrival order. The server echoes the request ID
in `wake_grant` or `wake_deny`.

Clients buffer audio while waiting, assume loss after **1000 ms**, and ignore
late decisions. A recommendation covers the activation and its follow-up
conversation. It is advisory: audio streamed after a denial, or without any wake
request, is processed normally. The server does not elect from transcripts.
With arbitration disabled, requests receive immediate grants. The legacy
`first_to_endpoint` setting also grants immediately; unknown policies fall back
to `wake_signal`. See [client spec.md](client%20spec.md) for the full protocol,
and examples.

**Smart Turn v3** (optional): with `endpoint_model: smart-turn-v3` the fixed silence endpoint
becomes adaptive — on each candidate pause the model estimates whether the utterance sounds
finished and keeps listening when you trailed off. It needs `onnxruntime`, `transformers` and
`huggingface_hub` importable in the Hermes environment; `hermes converse doctor` names what is
missing, and the loop falls back to the fixed endpoint if it cannot load.

**Browser clients and CORS.** The API server's origin guard runs before any route. A browser or
Electron client (which sends `Origin: null`) must have its origin listed in
`platforms.api_server.cors_origins` (or `API_SERVER_CORS_ORIGINS`), the same as for any other
API-server client.

## What is where

| module | owns |
|---|---|
| `route.py` | the WebSocket handler, auth flow and the per-connection wiring |
| `loop.py` | the mic shim, the VAD/STT session worker, the turn driver (sentence-by-sentence TTS, barge-in, session mode) |
| `arbitrate.py` | advisory selection among wake claims; audio processing is independent |
| `agent_client.py` | the Runs API client: idempotent run creation, SSE with replay-safe sequence tracking, stop, approval answers |
| `auth.py` | key verification through the API server's own `/v1/capabilities` |
| `vad.py` | the barge/onset detector, endpointer and WAV capture |
| `synth.py` | one `text → int16 PCM` seam over streaming or one-shot TTS, plus resampling |
| `speech.py` | the only module that imports Hermes core speech helpers |
| `phrases.py`, `settings.py` | stop/end phrase policy and the settings schema |
| `turn_detector.py`, `tracing.py` | the optional Smart Turn endpointer and the no-op tracing seam |

`speech.py` imports a handful of core helpers that are not yet on Hermes's public plugin API
(`transcribe_audio`, `resolve_streaming_provider`, `text_to_speech_tool` and the sentence chunker);
`hermes plugins validate` reports them as warnings from that one file. They are the seams an
upstream PR publishes; nothing else in the plugin touches core internals.

## Profiles

The route serves the profile that owns the API server listener. A `start.profile` naming any other
profile is refused with `profile_unsupported` until Hermes registers plugin routes ahead of its
`/p/<profile>/` catch-all.

## Develop and test

Tests need a Hermes checkout importable (`HERMES_AGENT_ROOT=/path/to/hermes-agent`) and an
interpreter with `pytest`, `pytest-asyncio`, `numpy` and `av`; the checkout's own test environment
(`source ./activate --test-extras all,voice`) has all of them:

```bash
HERMES_AGENT_ROOT=/path/to/hermes-agent python -m pytest
hermes plugins validate .
```

The suite drives the real VAD, endpointer, capture, resampler, Runs client and route against a
scripted in-process stand-in for the Runs API; only STT, the TTS provider and the model are faked.

## Disclosure

The plugin opens one authenticated route on the API server you already run. Microphone audio is
transcribed by your configured STT provider and replies are synthesized by your configured TTS
provider; neither leaves the host unless those providers are cloud services. Turns run as API
sessions in the serving profile with its full toolset. Command approvals raised during a voice turn
are answered with `approval_choice` (deny by default); clarifying questions are cancelled. No
telemetry, no third-party network calls of its own, no self-update.

## License

[MIT](LICENSE)
