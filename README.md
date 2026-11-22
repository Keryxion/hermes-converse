# hermes-converse

Server-owned realtime voice for [Hermes Agent](https://github.com/NousResearch/hermes-agent), as a
plugin. It adds `GET /v1/audio/converse` to the Hermes API server: a client streams microphone PCM
up one WebSocket and the server does the rest — voice activity detection, endpointing, speech to
text, the agent turn, and per-sentence streaming text to speech back down the same socket, with
barge-in. Built for devices that have a microphone and a speaker and nothing else: an ESP32, a
browser tab, a kiosk.

Every conversation is a gateway session on the plugin's `converse` platform, the same kind of
session a Telegram or Slack chat has: the agent's tools, memory, skills, scheduling and compaction
all apply, and a named device's conversation shows up in the web UI under its device name.

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

Opus sessions also require the system libopus shared library (`apt install libopus0`
on Debian/Ubuntu, `brew install opus` on macOS). PCM sessions do not load it.

## Talk to it

Dial `ws://<host>:<port>/v1/audio/converse` and send one `start` frame first:

```json
{"type": "start", "key": "<API_SERVER_KEY>", "input_rate": 16000, "output_rate": 24000,
 "quiet_interval": 0, "assistant_name": "Arya", "device_name": "Den Echo"}
```

A browser client can put the key in the WebSocket subprotocols instead (`hermes-voice-v1` plus
`hermes-key.<API_SERVER_KEY>`); the server accepts only `hermes-voice-v1` and never echoes the key.
Either way the key is verified against the API server itself, so your existing key is the only
secret.

`assistant_name` is what you call the assistant. `device_name` is the microphone/speaker label,
unique within this server/profile. The server opens the Hermes conversation titled
`Arya: Den Echo`, reusing its history across reconnects and gateway restarts. The voice prompt
explains both names, the default device destination, and proactive output through available
scheduling tools. Configure the same names on reconnect; changing names does not migrate history.
Send `"models": true` in `start` to receive the model catalog in `ready`.
Omitted or false skips the lookup and omits `model_catalog`; audio and model
selection remain available.

Wait for `ready` before streaming audio. It returns `assistant_name`, `device_name`, and
`recipient_id` directly.

Add `"audio_format": "opus"` to `start` to opt in for both microphone and reply
audio; omitting it keeps PCM16. Opus uses one raw 20 ms mono packet per binary
WebSocket message, VOIP, 24 kbps, DTX off, and complexity 5. Send silence packets too
so endpointing and `quiet` retain their audio timing. `ready.input.format` and
`ready.output.format` confirm the selection. See [the client spec](specs/client.md#opt-in-to-opus)
for sample rates and codec state across turns.

For wake-word clients, detect the wake word locally, send `wake`, and buffer all microphone
PCM while awaiting arbitration for up to **1000 ms**. Only a matching grant starts streaming:
flush the buffered audio in capture order, then continue live PCM16 mono at `input_rate`
(30 ms blocks are ideal). Denial or timeout discards the buffer and returns to local wake detection.
A non-wake client may stream after `ready`. The server answers:

| frame | meaning |
|---|---|
| `ready` | rates, the socket's `session_id`, and two server timestamps for clock-skew estimation |
| `transcript` | what it heard; carries a `turn_id` every later frame of the turn repeats |
| `thinking` | turn start, then progress every 5 seconds with `tool_calls` and `reasoning_tokens` |
| `speaking` + binary frames | the reply as PCM16 at `output_rate` |
| `interrupted` | you spoke over the reply: stop playing, the turn is cancelled |
| `error` | the turn failed (the message is spoken-safe text) |
| `turn_done` | the reply is fully sent; optional `next_action: "standby"` ends the exchange after drain, `"mute"` mutes the device at once |
| `quiet` | session mode: the stream has been silent for another `quiet_interval` seconds |
| `client_diagnostics` | requests a fresh snapshot of the client's installed version, build, and diagnostic state; reply with the same `request_id` ([contract](specs/client.md#client-diagnostics)) |
| `set_wake_word` | switch to this wake phrase and its models, then answer `wake_word_set` ([contract](specs/client.md#the-wake-word)) |

Handle the first `thinking` for a turn as an explicit boundary even without a preceding `transcript`:
mark the agent turn active and show "thinking". During the granted activation, keep sending
microphone PCM through thinking, speaking, and playback drain for server-side interruption/control
detection. Accepted speech interrupts obsolete output and is captured as a continuation in the
same conversation, preserving the original request. Standalone spoken stop phrases cancel without
starting another agent run. Arm the client's turn
deadline if it is not already running; do not restart it. This frame reports lifecycle status,
not reasoning text. See
[the client spec](specs/client.md#handling-thinking) for the frame format and tracing behavior.

During a long agent/TTS turn, `thinking` repeats every 5 seconds with the same
`turn_id` (and `traceparent` when available), the number of tool calls started
so far, and the provider-reported reasoning tokens (`null` when
unavailable). Refresh the active turn's inactivity timer and count without changing
speaking/playback state. Ignore stale/completed turn IDs and keep any absolute
turn limit; progress frames do not extend the server's 120-second default timeout.
They stop before terminal frames and on disconnect. See
[specs/client.md](specs/client.md#handling-thinking) for the full contract.

Client control frames: `{"type": "endpointing", "commit": true}` forces the current
utterance to endpoint, `{"type": "cancel", "turn_id": "..."}` cancels the current turn while
keeping the socket, session and history alive, `{"type": "drained", "turn_id": "..."}` says the
reply's last audio has left the speaker (or queued audio was discarded), ending the server's
playback-tail tracking; keep sending microphone audio throughout playback,
`{"type": "wake", "claim_id": "...", "wake_word": "..."}` requests an advisory wake decision
(see below) — send it when the local detector fires, then buffer audio until a matching grant.

Ordinary `turn_done` frames omit `next_action` and preserve the client's interaction mode:
hands-free continues listening after playback drain; released push-to-talk stays in standby
and sends no PCM. `next_action: "standby"` ends the exchange after drain, stopping upstream
audio while keeping the socket open. Cancellations, interruptions, and errors also omit
`next_action`; they do not reopen microphone capture.

**Session mode** (`quiet_interval` > 0) is for wake-word devices: the server reports quiet in
*received* audio, recognizes stop phrases, and decides when to return the client to standby.
Keep streaming through playback drain, then follow `next_action`. An open socket does not
require sending idle ambient audio. See [the client spec](specs/client.md#handling-turn_donenext_action)
for the full action contract.

Named clients send names and the API key, without a conversation override or another credential.
`ready.session_id` identifies this socket; `ready.recipient_id` identifies the
device for routing. A replacement closes the old socket with `4409 attachment_replaced` and
settles its run before accepting new work. Uncertain admission or unfinished cancellation returns
`conversation_busy`; retry rather than replaying an utterance whose outcome is unknown.
The server keeps device identities/session references in profile-local
`plugin-data/hermes-converse/devices.db`. Messages, compression, and retention stay in Hermes.

Completed responses for the underlying conversation can play without microphone input or a wake word.
Delivery is connected-only and waits for prior speaker drain.
See [client spec section 9](specs/client.md#9-conversation-output-without-user-input).

A countdown is a timer that ends with a tone on a device; see
[client spec section 10](specs/client.md#10-countdowns). Spoken reminders come from a scheduler
plugin such as Cadence and play as ordinary server-started turns.

## Editing voice instructions

On the first voice session, Converse creates `CONVERSE.md` in the active Hermes profile,
alongside `SOUL.md`, with the [default voice guidance](defaults/CONVERSE.md). Edit it directly
or ask an authorized agent to edit it. Save it as UTF-8 Markdown. A fresh voice connection
picks up the edits, and a turn already in progress keeps its instructions.

On every spoken turn, Hermes receives the file's prose as Converse channel guidance. Converse
adds its own text around it: the assistant's name before, and the device identity and gateway
command catalog after. Leave the file empty for just that generated text. Delete it to restore
the defaults on the next connection. If Converse cannot read or initialize the file, it logs a
warning and uses the bundled guidance for that connection.
Other Hermes platforms use their usual instructions.

## Settings

All optional, under `plugins.entries.hermes-converse.settings` in `config.yaml` (or the Desktop
plugin settings form):

| key | default | what it does |
|---|---|---|
| `voice_model`, `voice_provider` | `""` | a voice-only model override sent with every run |
| `turn_timeout_seconds` | `120` | a turn that makes no progress for this long is stopped and reported; each tool call and model call counts as progress |
| `playback_tail_timeout_seconds` | `45` | how long to wait for the client's `drained` frame |
| `max_tts_chars_per_turn` | `1500` | safety cap on how much of one reply is spoken |
| `vad_model` | `silero` | speech detection with automatic energy fallback; set `energy` to use RMS detection only |
| `smart_turn_enabled` | `true` | use Smart Turn v3 to keep listening through unfinished pauses; `false` uses the fixed silence endpoint |
| `endpoint_threshold` | `0.5` | probability at which Smart Turn considers the utterance complete |
| `endpoint_silence_ms` | `700` | silence that ends an utterance; with Smart Turn, the first completion check |
| `endpoint_max_silence_ms` | `3000` | longest pause allowed after the wake word or, with Smart Turn, inside an unfinished request |
| `approval_choice` | `deny` | what a voice turn answers when the agent asks for a command approval |
| `api_base_url` | `""` | loopback URL of the API server when it cannot be derived from the listener |
| `arbitrate_enabled` | `false` | enable advisory wake selection for cooperating clients |
| `arbitrate_policy` | `wake_signal` | rank wake claims with `wake_signal` |
| `arbitrate_device_id` | `""` | optional label prefix for server-generated unique socket identifiers |
| `arbitrate_wake_window_seconds` | `0.75` | how long the arbitrator collects concurrent wake claims for the same wake word before electing the primary device |
| `wake_word` | `Hey Arya` | wake phrase for every named device |
| `device_wake_words` | `{}` | wake phrase for particular devices by `device_name`; the `set_wake_word` voice tool writes it |
| `wake_word_repositories` | `[{repository: Keryxion/wakewords, ref: main}]` | ordered GitHub wake-word dictionaries; the last one listing a phrase supplies it, and `[]` uses only cached models |

STT and TTS come from your normal Hermes `stt.*` and `tts.*` configuration.

**Cooperative wake selection.** With `arbitrate_enabled: true` and the default
`wake_signal` policy, devices send a `wake` frame with a client-generated `claim_id`,
the exact detected `wake_word` phrase, and optional `confidence` (0–1), normalized
RMS `volume` (0–1), and `heard_at` (Unix milliseconds). Reports for the same phrase
within a 750 ms window compete: highest confidence, then highest volume, then
earliest detection time, then arrival order. The server echoes the request ID
in `wake_grant` or `wake_deny`. Different phrases and claims arriving after the
window elect independently; the larger window mitigates detector skew without
making selection exclusive.

The **1000 ms** client timeout starts when each client sends its own claim. A
750 ms collection leaves a nominal 250 ms for transit and scheduling for the
earliest claimant. Measure decision latency on the deployed network before
assuming that margin is sufficient. Existing explicit `0.25` settings must be
changed to `0.75` to use the larger window; changing the default does not override
saved configuration.

At INFO level, arbitration logs include claim receipt timestamps, election IDs,
claim/device IDs, election age, winners, and decision wait times, without audio,
wake phrases, or transcripts. Use election IDs to distinguish a shared election
from a late claim opening a second one. After deployment, verify that only one
physical device responds and the loser silently resumes wake detection.

Clients buffer audio while waiting for up to **1000 ms** and ignore late decisions. On a grant,
send the complete buffer before live audio so the beginning of the utterance is preserved.
On denial or timeout, discard the buffer without streaming. The grant covers the activation
and its follow-up conversation, including uninterrupted upstream audio during thinking and
playback for barge-in/control detection. The server's selection is advisory: it still accepts
audio sent by noncooperating clients after a denial or without a claim. Cooperating clients
must follow the gate; idle and losing devices do not stream their ambient audio.
With arbitration disabled, requests receive immediate grants. See [specs/client.md](specs/client.md) for the full protocol,
and examples.

**Speech detection.** Silero VAD is the default for speech onset, barge-in, and
capture silence. It runs locally on CPU through ONNX Runtime without PyTorch.
On first use, the plugin downloads the ~1.3 MB MIT-licensed Silero v6.2.3 model
from its pinned upstream tag, verifies its SHA-256, and caches it under
`<HERMES_HOME>/cache/voice/silero_vad_16k_op15.onnx`. A verified cache supports
offline use. Run `hermes converse doctor` before restarting the gateway to
prepare the cache and check the selected detector.

Missing ONNX Runtime, unavailable weights, or inference failures fall back to
the energy detector and log the reason. `vad_model: energy` skips model loading
and downloads entirely. Doctor reports the fallback as a failed Silero check.
Intel macOS and Android installations without ONNX Runtime use energy detection.
Silero distinguishes speech from non-speech; it can also hear speaker echo as
speech, so playback uses an echo-relative level guard. Detection alone does not
prevent a spoken wake phrase from becoming an utterance.

Active-turn interruption also requires sustained energy above the existing
barge-in level floor (1500 RMS, adjusted for the quiet floor and capped at 4000).
That applies while waiting for a turn, thinking, speaking, and draining playback:
high speech probability from soft mumbling is insufficient, as is a brief loud
burst over quiet speech. Idle input remains sensitive to quiet voices. This is
an audibility/sustain guard, not semantic recognition of whether speech was
addressed to the assistant.

Silero and Smart Turn work together: Silero finds candidate pauses; Smart Turn,
when enabled, decides whether the person has finished their thought. Speech
onset needs at least 250 ms above Silero's speech threshold (0.5, continuing down
to 0.35). After `endpoint_silence_ms` of silence the utterance ends, unless Smart Turn
judges it unfinished; then it keeps listening, rechecking each window, for up to
`endpoint_max_silence_ms`. After a wake word the first pause also waits up to
`endpoint_max_silence_ms`, so "Hey Arya … set a timer" stays one request. One capture
lasts at most 60 s; one that gets there ends at its last pause, and what followed
begins the next capture.

**Smart Turn v3** is on by default: on each candidate pause the model estimates whether the
utterance sounds finished and keeps listening when you trailed off. The plugin declares
`onnxruntime`, `transformers` and `huggingface_hub` as runtime dependencies, so Hermes installs
them alongside `numpy` and `av`. Model loading uses the NumPy feature-extraction path without
requiring PyTorch. ONNX Runtime is omitted on Intel macOS and Android, which have no current
wheels; those hosts keep fixed silence endpointing.

For an existing install, update/reinstall hermes-converse through Hermes's plugin manager so it
installs the manifest dependencies, then run `hermes converse doctor` and restart the gateway.
To use the fixed silence endpoint instead, set the following under
`plugins.entries.hermes-converse.settings` in `config.yaml`:

```yaml
smart_turn_enabled: false
```

Use YAML booleans (`true` or `false`); other values are rejected.

With Smart Turn on, doctor checks the dependencies **and loads the model**, returning a
failure if either is unavailable. The first load downloads `smart-turn-v3.2-cpu.onnx` from
`pipecat-ai/smart-turn-v3` on Hugging Face into its normal Hub cache. For offline use, populate
that cache first, then set `HF_HUB_OFFLINE=1`. Missing packages, incompatible runtimes, and
unavailable weights all retain the fixed silence endpoint and log the reason once per process.
Doctor leaves the model untouched when Smart Turn is off. A successful doctor check confirms
model loading, not the quality of endpointing on real speech; tune the threshold with real audio.

If a client stops sending audio during a capture, the partial utterance is dropped after
`capture_no_data_seconds` (default 5; 0 disables) without transcription or an agent run.
The next utterance starts a fresh capture on the same socket. This timeout applies to
both endpointers; received silence still ends utterances normally, and idle session quiet
continues to count only received audio.

**Voice tracing** (optional): when the host registers a global OpenTelemetry tracer provider,
the plugin emits a `voice.turn` span with `voice.capture`, `voice.stt`, `voice.agent`, and
`voice.tts` children through that provider. It uses the host's sampling and exporter settings,
including OTLP, and adds no plugin tracing settings or required OTEL dependencies. Install
Hermes's `otlp` extra for its OTEL SDK/exporter support. Importing this plugin does not import
OTEL; missing packages or disabled/unconfigured export keep tracing inert. Provider
initialization after the plugin loads is supported.

A global provider takes priority, including its sampling decisions. Without one, enabling
Hermes's `monitoring.export.otlp.enabled` with an explicit `endpoint` also enables voice
spans through a private provider using that destination and its `headers_env` mapping.
This fallback requires the installed OTLP/HTTP exporter; it never installs dependencies,
changes the global provider, or writes configuration. It uses `service.name=hermes-gateway`
and `telemetry.scope=voice`. No plugin-specific collector settings are needed.

Capture/STT timestamps survive the handoff from capture to turn and remain children of the right
turn, even when turns queue. Smart Turn decisions appear as `endpoint.decision` events with
probabilities, thresholds, and hold/commit reasons. Spans carry only allowlisted metadata
such as timings, counts, outcome, session/turn IDs, and endpoint mode; audio, transcripts,
reply text, and exception messages are excluded. A valid W3C `traceparent` is forwarded on
the existing transcript/thinking/speaking frames, preserving the host's sampling flags.
Disconnected turns are closed, and app cleanup attempts a bounded five-second flush in a
worker thread. Tracing failures do not interrupt voice operation.
Capture spans include `input.audio_ms` and `endpoint.silence_ms` so buffered audio duration
can be distinguished from elapsed processing time when investigating early endpoints. They also
record `endpoint.mode`, `endpoint.max_silence_ms`, `endpoint.wake`, `endpoint.reason`
(`silence`, `threshold`, `max_silence`, `client_commit`, ...), the final `endpoint.pause_ms`,
and `endpoint.longest_pause_ms`, the longest pause the user spoke through. With
`HERMES_VOICE_DEBUG=1` the same decision is logged per capture.

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
| `platform_adapter.py` | the `converse` gateway platform: voice turns as gateway messages, reply text from stream hooks, proactive output |
| `auth.py` | key verification through the API server's own `/v1/capabilities` |
| `vad.py`, `speech_detector.py`, `silero_vad.py` | energy/Silero speech detection, verified model cache, endpointing and WAV capture |
| `synth.py` | one `text → int16 PCM` seam over streaming or one-shot TTS, plus resampling |
| `speech.py` | the only module that imports Hermes core speech helpers |
| `settings.py` | the settings schema |
| `voice_tools.py` | the `speak_this`, `play_tone`, `standby`, `mute`, approved `command`, `client_diagnostics`, and `set_wake_word` agent tools |
| `wake_words.py` | wake-word dictionaries, the verified model cache behind `GET /v1/audio/wake/<slug>/<file>`, and live switches |
| `client_diagnostics.py` | fresh diagnostic requests and responses on one client socket |
| `named.py` | named-device routing records and public Hermes conversation lookup/continuation |
| `turn_detector.py`, `tracing.py` | the optional Smart Turn endpointer and host-configured OTEL tracing backend |

`speech.py` imports a handful of core helpers that are not yet on Hermes's public plugin API
(`transcribe_audio`, `resolve_streaming_provider`, `text_to_speech_tool` and the sentence chunker);
`hermes plugins validate` reports them as warnings from that one file. They are the seams an
upstream PR publishes; nothing else in the plugin touches core internals.

## Profiles

The route serves the Hermes profile that owns the configured API server listener.
A client selects the server URL and authenticates to it; `start` does not select a
profile. `start` carries `assistant_name` and `device_name` and nothing else that
names a profile or a conversation: a `profile`, `name`, `conversation` or
`conversation_id` field closes the socket with `4400 bad_start`, and so does a
`start` missing either name. There is no profile-prefixed `/p/<profile>/` route.
Delete any profile selector and saved per-server profile setting in a client, and
discard legacy values instead of sending `null`, `""` or `"default"`.
See [specs/client.md](specs/client.md) for the client contract.

The voice lifecycle and server integration contract are in [specs/server.md](specs/server.md).

## Develop and test

Tests need a Hermes checkout importable (`HERMES_AGENT_ROOT=/path/to/hermes-agent`) and an
interpreter with `pytest`, `pytest-asyncio`, `numpy` and `av`; the checkout's own test environment
(`source ./activate --test-extras all,voice`) has all of them:

```bash
HERMES_AGENT_ROOT=/path/to/hermes-agent python -m pytest
hermes plugins validate .
```

Tracing SDK/exporter tests run when `opentelemetry-sdk` is installed (included in Hermes's
`otlp` extra); otherwise those cases skip while the missing-OTEL fallback test still runs.

The suite drives the real VAD, endpointer, capture, resampler, route and platform adapter (on
Hermes's real adapter base class) against a scripted stand-in for the API server and the gateway
runner; only STT, the TTS provider and the model are faked.


Real Silero regression tests use the checked-in human-speech fixture and a separately cached
model. Ordinary tests stay offline and exercise the energy fallback. To include real inference:

```bash
HERMES_TEST_SILERO_MODEL=/path/to/silero_vad_16k_op15.onnx HERMES_AGENT_ROOT=/path/to/hermes-agent python -m pytest tests/test_silero_vad.py
```

Tests on long real takes also need a checkout of
[voice-recordings](https://github.com/Keryxion/voice-recordings):

```bash
HERMES_TEST_VOICE_RECORDINGS=/path/to/voice-recordings HERMES_TEST_SILERO_MODEL=/path/to/silero_vad_16k_op15.onnx HERMES_AGENT_ROOT=/path/to/hermes-agent python -m pytest tests/test_silero_vad.py
```

`tests/test_barge_soft_speech.py` exercises pending, thinking, speaking, and
playback-tail interruption against both detectors and inference-failure fallback.
The real-model tests also accept `HERMES_TEST_BARGE_AUDIO=/path/to/reviewed.wav`
for local mono PCM16 recordings at 16 kHz. They replay the recording quietly
(loudest 30 ms block at 900 RMS) to check rejection during a turn, then at its
original volume to verify interruption still works. Quiet speech must also
remain detectable as ordinary idle input. Private room audio is not committed.

## Disclosure

The plugin opens one authenticated route on the API server you already run. Microphone audio is
transcribed by your configured STT provider and replies are synthesized by your configured TTS
provider; neither leaves the host unless those providers are cloud services. Turns run as API
sessions in the serving profile with its full toolset. Command approvals raised during a voice turn
are answered with `approval_choice` (deny by default); clarifying questions are cancelled. Voice timing metadata follows the global OTEL provider or the host's opted-in monitoring OTLP destination.
No third-party network calls of its own beyond the configured providers and optional model downloads;
no self-update.

## License

[MIT](LICENSE)
