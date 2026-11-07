# Review: majorminorlabs/hermes-voice (2026-11-06)

Source: https://github.com/majorminorlabs/hermes-voice, reviewed at commit `0c3eea3`.

## What it is

A Home Assistant custom component (`custom_components/hermes_voice`) that registers a conversation agent
for HA Assist. HA owns speech-to-text, text-to-speech and the wake word; each turn reaches Hermes as text
over `POST /v1/chat/completions` (streaming) with a stable `X-Hermes-Session-Id`, so Hermes keeps the
history. Extras: an Echo Show "screen" (web page fed by HA's websocket: transcript, reply, tool calls,
timers, kanban/cron background work), a setup skill, and a trained "Hey Hermes" microWakeWord model.
Target devices are Amazon Echos flashed with echo-dot-assist.

It is turn-based text, not realtime audio: no barge-in, no server-side VAD, no audio socket. It does not
compete with hermes-converse; it is the "I already run HA Assist" path.

## Ideas worth taking into hermes-converse

1. **Working phrase on tool start.** If Hermes starts a tool before any text, they speak "One moment."
   Their measurements: a web-search turn took ~7 s total but speech began at ~3 s. hermes-converse
   ignores `tool.*` events (`agent_client.py`, `_dispatch` returns `None` for them), so tool calls are
   silent. Small change, biggest perceived-latency win.
2. **Never speak the wake word.** On devices whose microphone keeps listening during playback, a reply
   containing "Hermes" re-triggered the wake word mid-reply (observed on an Echo Show 5). Their
   `unsay_wake_word()` (`cleanup.py`) rewrites the name in spoken text only ("the latest Hermes Agent
   release" → "the latest agent release"). hermes-converse already accepts a wake-word `name` in the
   `start` frame, so an ESP32 wake-word client would hit the same issue.
3. **Local date and time in every turn.** Hermes has no clock; they append a line like
   "Current local date and time: Thursday, November 5, 2026, 3:47 PM (America/Chicago)." to the voice
   system message. One line in hermes-converse's per-turn ephemeral prompt (`loop.py`).
4. **Errors disguised as answers.** On `/v1/chat/completions` Hermes can return HTTP 200 with the
   provider's error text as the reply; they treat `finish_reason: "error"`, `hermes.failed`, and
   `X-Hermes-Completed: false` + `X-Hermes-Error` as failures and speak a fallback. hermes-converse uses
   `/v1/runs`, which has `run.failed`, but verify that a provider rate-limit never arrives as
   `run.completed` whose `output` is the error text (it would be spoken).
5. **Speech cleanup.** Their `clean_for_voice` turns table rows into comma-separated sentences, URLs into
   "link" (markdown links keep their label), and their `VoiceChunker` holds back text inside an open code
   fence until it closes. Compare with core `tools/tts_text_normalize._strip_markdown_for_tts` (which
   hermes-converse calls); improvements belong upstream there, not in the plugin.

## Wake word model

`wakeword/model/hey_hermes.tflite` (microWakeWord, ESPHome ≥ 2024.7, cutoff 0.92) plus a reproducible
training pipeline. Useful for an ESP32 converse client, but the licence is ambiguous:
`MODEL_NOTICE.md` says **non-commercial only** (trained with CC BY-NC negatives), while `DECISIONS.md` #25
and a 2026-11-06 commit say the model was retrained on permissively licensed data so it can be MIT.
Confirm with the authors before depending on it.

## Relevant to hermes-agent upstream

- **Community skill admission.** Their setup skill cannot be installed: the skills scanner marks it
  "dangerous" (1 critical: a manual `mkfs` example in a device guide; plus `subprocess`, loopback IP,
  systemd/launchd findings). They document it in `docs/skill-installation.md` and point at upstream
  #141755. A concrete case for a reviewed/pinned admission path for skills that legitimately drive
  Docker or hardware setup.
- **`/p/<profile>/v1` in real use.** They select profiles via the multiplexed gateway route. Another
  consumer to keep in mind for the planned upstream change that registers plugin routes before the
  `/p/{profile}` catch-all.
- **Session header semantics.** Their architecture doc relies on the API server loading history from the
  DB when `X-Hermes-Session-Id` is present, and on `X-Hermes-Session-Key` for memory-provider scoping.
  Both are behaviour contracts worth keeping stable.

## Not useful here

HA config flow, HA `Store`-based per-device session windows, the Echo Show Android app, Echo flashing
guides: all specific to the HA/Echo path.

## Recommendation (to file as an issue once the repo is on GitHub)

Do now:

1. **Tool events.** `agent_client._dispatch` drops `tool.started`. Use it to (a) speak a configurable
   working phrase (default "One moment.", empty = off, once per turn) when a tool starts before any reply
   text, and (b) forward a `tool` frame (name + preview) to the client so screens can show it.
2. **Spoken fallback on failure.** `/v1/runs` already maps failed/partial agent results to `run.failed`
   (`terminal_run_status` in `gateway/platforms/api_server_runs.py`), so error text is never spoken as an
   answer. But `_run_turn` only sends an `error` frame, so screenless clients hear silence. Speak a short
   configurable line; never speak the error text.
3. **Local time in the user message, not the instructions.** Hermes' system prompt only carries the date
   (`Conversation started:` in `agent/system_prompt.py`). Putting the time in the per-turn instructions
   would change the system prompt every turn and break prompt caching, so prefix it to the transcript
   (client timezone from `start` if given, else the server's).

Later, only if a client needs it: wake-word avoidance, keyed off the `start` frame's `name` and only when
the client declares an on-device wake word.

Skip: a plugin-local speech cleanup (improve core `tools/tts_text_normalize.py` instead), the wake-word model
until its licence is clarified, and all HA/Echo-specific parts.
