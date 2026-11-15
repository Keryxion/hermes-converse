# Reproduce a voice turn stuck after a busy admission

Run the integration test with a Hermes checkout and its test dependencies:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 HERMES_DISABLE_LAZY_INSTALLS=1 \
HERMES_AGENT_ROOT=/path/to/hermes-agent \
python -m pytest -p pytest_asyncio.plugin tests/test_busy_voice_replay.py -q -s
```

The test uses a temporary Hermes home and database. It does not connect to the
running gateway, use real device conversations, or call an LLM, STT, or TTS
provider. It drives the real `GatewayRunner`, `ConverseAdapter`, session
authority, SQLite admission queue, and reply delivery. Only agent inference is
replaced with a gated coroutine returning `Four.`.

## Trigger

1. Start another turn in the same conversation and hold it open with an event.
2. Submit a voice turn through `GatewayTurns.run_turn`.
3. Verify that the voice input is durably `queued` before releasing the first
   turn. No sleep or restart timing determines whether this path is exercised.
4. Release the first turn and let the real authority drain execute the voice
   input and commit its terminal result.
5. Check that the voice callback received `Four.` and the voice turn completed.

An idle control submits the same voice input without an occupied conversation.
The test records the original and executed event IDs, spoken output, completion
flag, and adapter processing callbacks. It also verifies that the queued input
was reconstructed as a separate event and reached terminal state in SQLite.

## Observed on Sakura, 2026-11-15

Baseline plugin: deployed PR #146 head `424362eba36611fde0ecd15c1666a6b0d61191cc`.
Comparison: an identical temporary copy with PR #133's plugin/test diff applied
(PR head reported as `8720e71c70f4544b8f6f155ee049654b0a74f7df`). Both run
against Sakura's Hermes checkout at `/home/hermes/.hermes/hermes-agent`.
Hermes revision: `3b421578588243f920886e6fe79a6faa87ea026e` (clean checkout).

Three separate test processes were run per variant. The idle control passed in
all six runs; the queued voice case failed in all six. Each two-case run took
approximately 2.3–2.8 seconds, excluding process startup.

The idle control speaks and completes. The busy case produces an answer and
settles its admission, but the voice callback receives nothing and its turn
remains unfinished. Cancellation then fails to settle that voice turn. The
test detects the lost result immediately rather than waiting the production
120-second voice deadline.

The replay in this Hermes build retains the original message ID. Neither
`on_processing_start` nor `on_processing_complete` is called for the queued
voice input; only the first turn has those callbacks. PR #133's event-ID
adoption change alone does not fix this reproduction.

In Hermes, `dispatch_shared_busy` in `gateway/session_ingress.py` runs an
observer that calls the handler and delivers its reply without the processing
hooks normally supplied by `BasePlatformAdapter._process_message_background`.

## Recovery in the Converse adapter

When a voice input reaches the installed gateway handler without a processing
start, the adapter awaits that handler's result, delivers its final text through
the voice callback, and completes the voice result. It consumes the reply so
Hermes does not also publish it as proactive output behind the waiting turn.
Failures complete as failed; cancellation and stopped turns do not speak late
answers. Empty and `[SILENT]` replies still complete without audio.

The handler retains the bound runner identity that Hermes uses to select durable
busy admission. Ordinary turns with processing callbacks keep streaming as
before. On Hermes versions with the missing callbacks, queued turns speak once
the final answer returns; they do not stream intermediate deltas.

The integration test covers idle and queued input, streamed and non-streamed
provider output, empty/silent replies, cancellation while queued, and duplicate
delivery. Its contract is spoken output and a completed voice result, even when
the host omits its processing callbacks. Each case also verifies that a subsequent
question completes on the same conversation.

Validation uses Penny's Hermes revision
`2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19` in temporary plugin copies. The
unmodified main adapter reproduces the lost reply; the adapter recovery makes
the same queue test pass. The live Hermes source is unchanged.

The full plugin suite passed on Penny: 457 passed, 9 skipped. The 12 queue
integration cases passed with follow-up questions included. Plugin validation
reports the same undeclared-tool and scanner errors on the baseline and fixed
copies; the fix adds no validation errors.

This isolates the adapter/queue failure. It does not test microphone capture,
speech recognition, WebSocket frames, or speaker playback.
