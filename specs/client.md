# Client spec — how to be a client to hermes-converse

`hermes-converse` exposes one WebSocket route:

```
GET /v1/audio/converse
```

Each Arya has a name and uses an existing Hermes conversation, titled
`<assistant_name>: <device_name>` by default. A WebSocket carries microphone PCM to the server,
which handles voice activity detection, endpointing, speech-to-text, and the
agent turn, then streams reply PCM back. Conversation history persists across
turns and reconnects; closing the socket does not end the Hermes conversation.
If the conversation is deleted in Hermes, the next turn reuses a compatible
conversation with the same title or starts a fresh one, on the open socket or
after reconnecting. The device name and routing identity stay the same;
deleted history is not restored.

Wake selection is cooperative: it recommends an answering device without
restricting audio processing. Section 8 defines the cooperative wake contract.
Sections 9–10 define conversation output and countdowns. Section 11 defines
changing the chat model.

Server lifecycle and integration details are in [the server spec](server.md).
What a client records for tracing is in [client tracing](client_tracing.md).

## 1. Connect and authenticate

Open the WebSocket. Your first frame must be a `start` frame.

There are two ways to present your `API_SERVER_KEY`:

**A) Browser clients — WebSocket subprotocol (recommended)**

Offer `hermes-voice-v1` plus `hermes-key.<API_SERVER_KEY>` in
`Sec-WebSocket-Protocol`. The key is verified before the socket is accepted
and never echoed back. Once accepted, `start.key` is optional.

**B) Non-browser devices (ESP32, etc.) — `start.key`**

Accept the socket with no subprotocol, then send your first frame as a
`start` that includes `"key": "<API_SERVER_KEY>"`. A bad or missing key
closes with `4401 unauthorized`; a non-`start` first frame closes with
`4400 bad_start`.

## 2. The start frame (send first)

```json
{"type": "start", "key": "<API_SERVER_KEY>", "input_rate": 16000,
 "output_rate": 24000, "quiet_interval": 0, "assistant_name": "Arya", "device_name": "Den Echo"}
```

| field | what it does |
|---|---|
| `type` | always `"start"` |
| `key` | API key (required unless you used the subprotocol) |
| `input_rate` | sample rate of the PCM you'll send (8000–48000, default 16000) |
| `output_rate` | sample rate of PCM the server sends back (default 24000) |
| `quiet_interval` | session mode: seconds of silence between turns (0 = off) |
| `device_name` | The configured device label, such as `Den Echo` or `Kitchen`. Each device keeps its own conversation, titled `<assistant_name>: <device_name>`. |
| `models` | Boolean; only `true` includes the model catalog in `ready`. Defaults to `false`. See section 11. |
| `transcripts` | Boolean; request user and assistant transcript frames when `true`. Defaults to `false`. See section 4. |
| `after` | The `message_id` of the last transcript this client has shown, to catch up on what it missed. Only with `transcripts: true`. See "Catching up" in section 4. |
| `assistant_name` | What you call the assistant, normally `Arya`, without “Hey”. Used in the voice system prompt; separate from the device label, such as `Den Echo`. |

**Client profile selection is removed.**

`start.profile` is not part of the supported protocol. All clients must omit the
field entirely. Do not send it as `null`, an empty string, or `"default"`. Remove profile
selectors from client settings and remove the corresponding per-server saved
configuration; discard legacy saved values instead of sending them on reconnect.
This applies to every client, including macOS, iOS, Echo, and ESP32 clients.

The connection uses the Hermes profile that owns the configured server listener.
Clients select a server URL and authenticate to it; the `start` frame does not
select or switch Hermes profiles. `assistant_name` only sets the assistant's spoken identity
and does not select a profile either. There is no supported profile-prefixed
converse route in this contract; do not construct `/p/<profile>/` URLs to replace
the removed field.

Some deployed servers still parse the legacy field and can reject it with
`profile_unsupported` (close code 4400). Clients must remove the field regardless
of whether their server accepts legacy values. The legacy server parser remains
in the current implementation; this update changes the client contract.

For a wake-word client, detect the wake word locally, buffer microphone
PCM while awaiting arbitration for up to **1000 ms**, and start streaming
only on a matching grant. Send the buffered audio first, in capture order,
then live binary PCM16 mono at `input_rate` without a gap or duplication.
30 ms blocks are ideal (e.g. 480 samples at 16 kHz). Keep streaming throughout
the granted activation, including thinking and playback (sections 4 and 8).
A client that does not use wake detection may start streaming after `ready`.

The server replies with `ready`:

```json
{"type": "ready", "session_id": "socket-9", "recipient_id": "device-7",
 "assistant_name": "Arya", "device_name": "Den Echo", "transcripts": false,
 "input": {"sample_rate": 16000, "format": "pcm16", "block_ms": 30},
 "output": {"sample_rate": 24000, "format": "pcm16"},
 "server_recv_unix_ms": 1794142800100, "server_send_unix_ms": 1794142800140}
```

`input.sample_rate` and `output.sample_rate` are the rates in use; play reply
PCM at `output.sample_rate`. The two timestamps are for clock-skew estimation.
`session_id` identifies the socket for diagnostics and `recipient_id` identifies
the device. Treat both as opaque.

Reconnect with the same API key and names to continue the device's conversation.
Do not automatically resend an utterance whose outcome is unknown: its actions
may already have happened.

| close or error | what to do |
|---|---|
| `4409 attachment_replaced` | Another connection with the same names replaced this one. Stop sending on it. |
| `4404 conversation_unavailable` | The conversation could not be opened. Show the failure rather than claiming restored context. |
| `conversation_busy` error before `ready` | Earlier work is still stopping. Retry later. |

## 3. Client control frames (send anytime)

All are JSON text frames:

| frame | meaning |
|---|---|
| `{"stop": true}` | Stops live work and closes the socket. The device's conversation and its history are kept for reconnection. |
| `{"commit": true}` | Ends the current utterance now, with the audio already sent (runs a turn on it). |
| `{"type": "cancel", "turn_id": "..."}` | Cancels the **current** turn — stops TTS and the agent — but keeps the socket, session, and history alive. |
| `{"type": "wake", "claim_id": "...", "wake_word": "Hey Arya"}` | request an advisory wake decision while buffering the new activation locally; see section 8. |
| `{"type": "drained", "turn_id": "..."}` | Tells the server the reply's last audio has left the speaker, so the server won't transcribe its own echo as a new turn. |
| `{"type": "say", "text": "..."}` | Speaks the given text (trimmed, at most 400 characters) as a reply of its own: `thinking`, `speaking` + PCM, then `turn_done` with `next_action: "standby"`. No agent turn runs and nothing is added to the conversation. For fixed device prompts such as a spoken help reminder. Empty or non-string text is ignored. |
| `{"type": "approval_response", "approval_id": "...", "choice": "once"}` | Answer a pending approval from a screen with one of its `choices`; see "Approvals" in section 4. |

**`cancel` vs `stop`:** `stop` tears down the session; `cancel` only kills the
current turn. Use `cancel` when the user hits the action button, says the
wake word again mid-turn, or says the `cancel` wake word, and you want to keep
the conversation going.

## 4. Server frames

| frame | meaning |
|---|---|
| `ready` | `input` and `output` rates, `session_id`, clock-skew timestamps; see section 2 |
| `wake_grant` | recommends this client handle the wake activation; echoes its `claim_id` and `wake_word`. |
| `wake_deny` | recommends this client stand down for the activation; echoes its `claim_id` and `wake_word`. |
| `transcript` | user input or assistant reply text, identified by `role` and `turn_id`; see below |
| `thinking` | starts a turn, then repeats every 5 seconds with `tool_calls` and `reasoning_tokens`; refresh inactivity without resetting speaking/playback |
| `approval_request`, `approval_resolved` | the assistant needs permission before it acts, and when that is settled; see "Approvals" below |
| `speaking` + binary frames | the reply as PCM16 at `output_rate` |
| `interrupted` | the turn was cancelled (you barged in or sent `cancel`); stop playing |
| `turn_done` | the reply is fully sent; `next_action` tells the client to listen or enter standby after drain |
| `quiet` | session mode: the stream has been silent for another `quiet_interval` seconds |
| `error` | the turn failed (spoken-safe text) |
| `{"type":"tone","tone":"confirm"}` | Play the named tone, `confirm` or `error` (see `tones.md`). It is not a reply: no `turn_id`, no `drained`. |
| `countdown`, `countdown_cancel` | Start, extend, or cancel a countdown that ends with a tone (section 10). |

These are the only server frames, apart from `model_changed` and `model_error`
(section 11). Turn liveness is the repeated `thinking` frame; there is no
separate keep-alive frame. Ignore a frame type you do not recognize.

Reply PCM arrives at about playback speed: the first audio is sent immediately,
then the server stays at most about 1.5 s ahead of real-time playback. Keep
reading the socket while you play. A buffer of 1.5 s plus network jitter never
has to stop reading, so `interrupted`, `turn_done`, and pings reach you without
waiting behind queued audio.

### Microphone streaming and barge-in

Wake-word detection is **client-side**. Before a wake activation, keep the
microphone local; an open socket and `ready` do not start upstream audio.
On detection, send `wake` and buffer all microphone audio while waiting up
to **1000 ms** for the matching decision. Retain the detector's available
pre-roll and every sample during the wait so the start of the utterance is
not lost. On a grant, send that buffer once, in capture order, then stream
live PCM continuously for the rest of the activation. Include silence.

| client state or event | microphone transport and server use |
|---|---|
| Idle / waiting for a local wake word | Local wake detection only; no upstream PCM. |
| Awaiting matching wake decision (up to 1000 ms) | Buffer locally; no upstream PCM for this activation yet. |
| Matching `wake_grant` | Flush buffered PCM in order, then continue with live PCM. Server VAD/endpointer captures the request for transcription. |
| Matching `wake_deny` or timeout | Discard the activation buffer and resume local wake detection; do not stream this activation. |
| `transcript` / `thinking` after request endpoint | Keep sending live PCM. Accepted speech interrupts the old reply and becomes a continuation in the same conversation. |
| `speaking` and queued speaker playback | Keep sending live PCM. The server rejects speaker echo and captures accepted user interruptions as continuations. |
| Turn ended and playback drained, activation still open | Keep streaming; server may capture the next user utterance normally. |
| Activation ends / returns to wake detection | Stop upstream PCM and resume local wake detection; keep the socket available for outbound conversation events. |
| Explicit user mute or closed session | Stop sending PCM; server-side barge-in is unavailable. |

Microphone input remains available after request endpointing. When the server
accepts further user speech during thinking, speaking, or playback drain, it
interrupts the old reply, preserves the new utterance, and transcribes it as a
continuation in the same conversation. The original request remains context.
Do not require another wake word or make the user repeat the interruption.
Playback echo rejection remains the server's responsibility; clients should
provide clean microphone audio without suppressing user speech.

### Barge-in and cancellation

Barge-in yields the floor to the user. For example, after “turn on the light”,
the user may add “and make it warm white” while the reply is thinking. Expect
`interrupted` and `turn_done` with `next_action: "listen"` for the old turn,
then a new reply turn for the additional instruction (and a `transcript` if enabled). The
new turn uses the same conversation and includes the original request as context.

Explicit `cancel` stops the matching turn without submitting a user message:

```json
{"type":"cancel","turn_id":"turn-1"}
```

A stale targeted cancel cannot stop a newer turn. An untargeted
`{"type":"cancel"}` also abandons a capture in progress. The connection and
conversation remain usable. `stop` closes the attachment instead.

Do not use an `agentTurn`, thinking, or playback flag to suppress PCM during
an activation. Play incoming PCM and send microphone PCM concurrently.
Local echo cancellation may clean the signal, but must preserve user speech.
Do not replace microphone audio with synthetic silence during playback.
On unmute, resume live audio only if the activation is still open; never
replay audio accumulated while muted.

On `interrupted`, stop and discard queued reply audio for that turn. Send
`drained` after its audio has actually left the speaker or been discarded.
`turn_done` means the server finished sending, not that playback drained.
`drained` acknowledges playback completion or discard, not that a person heard
the reply.
For `next_action = "standby"` or a local decision to end the activation, finish
playback/drain handling and then stop upstream audio and return to wake
detection; do not cut off barge-in monitoring during the playback tail.

The server binds interruption to the old turn before announcing `thinking`.
If accepted speech interrupts playback after `turn_done`, expect `interrupted`
followed by an updated `turn_done` with `next_action: "listen"` for the same
turn, replacing its pending standby action. A continuation receives its own
turn ID. Send `drained` only for the old playback; it must not discard the
continuation. A bounded playback-tail timeout handles a missing `drained`.

### Handling `thinking`

The server sends `thinking` at turn start, then approximately every **5 seconds**
until the turn ends, including agent/TTS waits and speech:

```json
{"type": "thinking", "turn_id": "turn-42", "tool_calls": 3, "reasoning_tokens": 2400}
```

Both counters are measured for this turn and reset for each turn:

- `tool_calls` is the cumulative number of tool calls started in this turn. The
  count includes calls that
  later fail; it is not a count of completed or currently running calls, or a
  percentage complete.
- `reasoning_tokens` is the cumulative reasoning-token count the model provider
  reported for this turn, or `null` when none has been reported. `null` means
  unavailable; `0` is a measured zero. The count is updated when a model call
  finishes, so it can lag during a long call and can increase while speaking.

The initial frame has `tool_calls: 0` and `reasoning_tokens: null`. Replies
prepared without an agent run report `tool_calls: 0` and `reasoning_tokens: null`.
No tool names, arguments, results, or reasoning text are included. When tracing
is active, frames also carry the turn's `traceparent`.

The first `thinking` starts the turn even without a preceding user `transcript`.
Mark the turn active and show thinking. Repeated frames with the same active
`turn_id` update the count and refresh the inactivity timer; preserve any existing
speaking/playback state. Do not treat them as new turns or restart playback.
Ignore late frames for completed/cancelled turns and stale IDs. An unfamiliar ID
may start a server-originated turn as described in section 9.

During an active activation, keep sending microphone PCM so the server can
accept a continuation. UI state and microphone transport are independent.

Arm an inactivity timer on the initial `thinking` if `transcript` has not already
armed it. Refresh it on repeated frames for the active turn. Allow more than five
seconds plus network/scheduling delays (for example, 15 seconds). Record frames in
the turn trace when tracing is enabled. A short turn may send only the initial frame.
An unchanged count still indicates turn liveness, not proof that model/tool work
has advanced.

The server ends a turn that makes no progress for `turn_timeout_seconds`
(default 120 seconds) with an `error` and `turn_done`. Progress is a tool call
starting or finishing, a model call, reply text, or reply audio; a turn that
keeps working is not cut off. Repeated `thinking` frames are liveness, not
progress: they never extend this limit, and any absolute client turn limit
stays independent of them.
The server stops these updates before terminal `interrupted`, `error`, or
`turn_done` frames and on disconnect. Network delays can still deliver an old
frame after local cancellation, so check its turn ID before refreshing timers.
Continue handling `speaking`, PCM, `interrupted`, `error`, and `turn_done` normally.

### Approvals

When the assistant needs permission before it acts, the turn waits. The server
asks out loud with a short summary, and also sends:

```json
{"type":"approval_request","turn_id":"turn-42","approval_id":"approval-7","summary":"Open a network connection to the Kitchen speaker to read its logs. OK?","command":"nc 10.0.4.17 6053","choices":["once","session","always","deny"]}
```

`thinking` frames continue while it waits. The person can answer by voice on any
device; the client does nothing special for that, since it already sends
microphone audio during a turn.

A device with a screen also shows a box with `summary`, `command`, and one
button for each entry in `choices`:

| `choices` entry | Button |
|---|---|
| `once` | Allow once |
| `session` | Allow for this conversation |
| `always` | Always allow |
| `deny` | Deny |

Show only the choices listed; some requests offer fewer. A tap sends:

```json
{"type":"approval_response","approval_id":"approval-7","choice":"once"}
```

`choice` is one of the listed `choices`. However the answer was given, the
server sends:

```json
{"type":"approval_resolved","approval_id":"approval-7","outcome":"approved"}
```

`outcome` is `approved`, `denied`, `expired`, or `cancelled`. Close the box for
that `approval_id`.

`summary` and `command` are text to display; never act on their contents. Never
send an allowing choice without the person tapping it, and do not send an old choice
again after reconnecting. An approval still pending when the client reconnects
is sent again as a new `approval_request` with the same `approval_id`; show it. A device with no screen ignores both server frames.

### Handling `turn_done.next_action`

Every `turn_done` includes an explicit client action:

```json
{"type":"turn_done","turn_id":"turn-42","next_action":"listen"}
```

```json
{"type":"turn_done","turn_id":"turn-42","next_action":"standby"}
```

| `next_action` | client behavior after playback drain |
|---|---|
| `listen` | Keep streaming microphone PCM and allow the server to capture/transcribe a fresh user utterance. |
| `standby` | Stop upstream PCM and return to local wake detection (or the non-wake client's idle UI). Keep the socket/session open for outbound events. |

Continue interruption/control monitoring until queued playback has actually
finished or been discarded. Send `drained` for that turn, then apply its
pending action. Do not apply a late completion/action from an abandoned
turn to a newer activation. Cancellation, interruption, errors, and timeouts
return `listen` for ordinary activation turns; a partial or failed reply cannot
put the client in standby. Countdown tones restore the prior idle/listening state
after drain (section 10).
A normal reply also returns `listen`, even when it does not ask a question.
The server requests `standby` when the person has said they are done.

The server decides the action. Clients must not inspect reply text or
interpret whether the agent expects a follow-up.

### User and assistant transcripts

The server uses `transcript` for recognized user speech and assistant reply
text. User frames carry `role: "user"`; assistant frames carry
`role: "assistant"`.

A client requests server-sent user and assistant text with
`start.transcripts: true`; omitted or false means no `transcript` frames from
the server. The server confirms support with `ready.transcripts: true` when
enabled, or `false` otherwise. A client still receives lifecycle frames and
audio when transcripts are off.

When enabled, the assistant's text arrives ahead of its audio, a sentence at a
time, so the words are on screen as they are spoken:

```json
{"type":"transcript","role":"assistant","turn_id":"turn-42","text":"Two things tomorrow. "}
```

```json
{"type":"transcript","message_id":"m-124","role":"assistant","turn_id":"turn-42","text":"The dentist at nine and lunch with Sam."}
```

Each frame is the next piece of the reply, sent before the PCM that speaks it.
Append the pieces for a `turn_id` in the order they arrive. The piece that
completes the reply carries `message_id`; the earlier ones do not. A reply of one
sentence is a single frame with `message_id`. This applies to ordinary replies and
outbound conversation output. Silent turns need no assistant frame.

When a reply is interrupted and the request is run again, the new reply arrives
under a new `turn_id`, with no user `transcript` between the two. Replace the
interrupted reply with the new one; do not show both. An interrupted reply with
a new user `transcript` after it stays, marked as interrupted.

A user `transcript` is one frame and always carries `message_id`. A `message_id` is unique in
the conversation; treat it as opaque.

The text describes what was prepared for speech, not what the user heard. For
an interrupted or failed turn, send only text already submitted to speech if
available, and keep the interruption or error visible. Do not send reasoning,
tool payloads, credentials, or internal errors.

### Catching up

A client that was away, for example an app returning to the foreground or
relaunched, is caught up when it connects. Send the `message_id` of the last transcript
it has shown as `after` in `start`:

```json
{"type":"start","key":"<API_SERVER_KEY>","input_rate":16000,"output_rate":24000,
 "assistant_name":"Arya","device_name":"Darrell's iPhone","transcripts":true,"after":"m-122"}
```

Right after `ready`, and before anything live, the server sends the transcripts
since then as ordinary `transcript` frames marked `replay`:

```json
{"type":"transcript","message_id":"m-123","role":"user","turn_id":"turn-41","text":"What's on my calendar tomorrow?","replay":true}
```

```json
{"type":"transcript","message_id":"m-124","role":"assistant","turn_id":"turn-41","text":"Two things: the dentist at nine and lunch with Sam.","replay":true}
```

Omit `after` the first time; the server then sends the most recent 20. Keep the
last `message_id` shown and send it on every connect, so nothing is shown twice.

A replayed assistant transcript is the whole reply in one frame. If the client
already shows pieces for that `turn_id`, replace them with it.

A `replay` transcript is only for display. It does not start a turn: do not arm
a timer, show thinking, play audio, resend the request, or open the microphone
because of one. A user transcript with no assistant transcript after it, and no
turn running, was not answered: show it that way.

A turn does not end because the connection dropped. If the person asked
something and the app went away, the assistant still finishes, and the answer is
there to catch up on. It is not spoken later. A turn still running on reconnect
continues live: its `thinking`, `speaking`, and `turn_done` frames arrive on the
new connection after the replayed transcripts. `{"stop": true}` and `cancel`
still end a turn.

## 5. Session mode (`quiet_interval > 0`)

For wake-word devices. The server reports quiet in **received** audio (never
wall-clock, so a held-open socket never accrues phantom quiet). Each listening
phase counts from zero: speech, a turn, and resuming upstream audio after
stopping it all restart the count. The first `quiet` after you start or resume
listening comes after one full `quiet_interval` of received silence, for example:

```json
{"type": "quiet", "quiet_seconds": 3.0}
```

Silence sent before you stopped streaming does not carry over. `turn_done.next_action` tells the client whether to continue
listening or return to standby after playback drain.

## 6. Typical flow

1. Open `ws://<host>:<port>/v1/audio/converse`, send `start`, and receive `ready`.
2. Detect the wake word locally; send `wake` and buffer PCM while awaiting
   the matching grant for up to 1000 ms. On denial/timeout, discard the buffer.
3. On grant, flush the buffered PCM and continue with live PCM. Server VAD
   endpoints the user request, then emits `thinking` (preceded by `transcript` if enabled).
   The user may pause after the wake word, or mid-request, for up to
   `endpoint_max_silence_ms` (default 3 s) without the request being split.
4. Keep sending live PCM during thinking, `speaking` + reply PCM, and queued
   playback. The server captures accepted interruptions as continuations.
5. Receive `turn_done`; send `drained` after actual speaker drain/discard.
6. If the activation remains open, keep streaming for a follow-up request.
   Otherwise stop upstream audio and return to local wake detection.

This is the typical sequence, not a prerequisite for handling `thinking`:
the client must enter the active-turn state even without a preceding
`transcript` frame.

## 7. turn_id

Every turn gets a `turn_id`. It appears on `transcript`, `thinking`,
`speaking`, `interrupted`, `error`, and `turn_done` frames of that turn, so
you can match frames to a turn. `cancel` and `drained` accept an optional
`turn_id` to target a specific turn.


## 8. Cooperative wake selection

### Participation and lifetime

Wake selection helps cooperating devices decide which one should respond when
several hear a wake phrase. It is advisory, not an exclusive server reservation.
Only clients that send `wake` participate. A client that never sends `wake`
streams and receives answers normally; the server does not arbitrate it using
its transcript.

A recommendation covers one **wake activation**: the detected wake phrase and
its ensuing conversation, including follow-up turns. The selected client does
not request another election for each utterance or `turn_done`. When that
exchange ends (for example, `next_action = "standby"` or the client
returning to wake detection), a fresh wake detection starts a new activation.
Other devices may start independent activations while that conversation runs.

A denial neither closes the socket nor cancels a turn. A device may still stream
audio after a denial; the server accepts and processes it normally, including
STT, agent execution, and TTS. Duplicate answers remain possible when clients
do not follow the recommendation. Use the existing `cancel` or `stop` controls
when cancellation or session termination is actually wanted.

### Wake request and decision frames

After `start` / `ready`, a cooperating client sends this JSON text frame as
soon as its local wake detector fires and buffers microphone PCM while
waiting for the matching decision. Do not stream this activation yet:

```json
{"type":"wake","claim_id":"activation-42","wake_word":"Hey Arya","confidence":0.93,"volume":0.098,"heard_at":1794142800123}
```

| request field | meaning |
|---|---|
| `type` | Required; `"wake"`. |
| `claim_id` | Required nonempty string generated by the client; unique for each detection on this socket, including after timeout. The server echoes it unchanged. |
| `wake_word` | Required nonempty string containing the exact detected phrase. `"Arya"` and `"Hey Arya"` elect independently; do not replace both with a canonical assistant name. Clients must use identical spelling and case to compete in the same group. |
| `confidence` | Optional numeric detector confidence, on a 0–1 scale; higher wins. |
| `volume` | Optional numeric RMS amplitude of the detected wake audio normalized to 0–1, where 1 is full-scale amplitude (PCM16 RMS divided by 32768), not dB; higher wins. Omit it when unavailable. |
| `heard_at` | Optional numeric Unix timestamp in milliseconds at wake detection, not send time or a monotonic clock; earlier wins. Devices need comparable clocks for this tie-break to be useful. |

A wake request without nonempty string `claim_id` and `wake_word` receives
`{"type":"error","error":"bad_wake"}` without closing the socket. Invalid
optional numeric signals (non-numeric, non-finite, negative, or outside 0–1 for
confidence/volume) are treated as missing; omit unavailable signals.

The server responds on the requesting socket with one of:

```json
{"type":"wake_grant","claim_id":"activation-42","wake_word":"Hey Arya"}
```

```json
{"type":"wake_deny","claim_id":"activation-42","wake_word":"Hey Arya","winner_device_id":"device_a1b2c3d4"}
```

Both decisions echo the request's `claim_id` and `wake_word`. A denial may also
include `winner_device_id`, an opaque server device identifier for diagnostics;
clients do not need it to act on the decision. `claim_id` identifies a wake
detection, not a `turn_id`; wake decisions do not create a conversational turn.
The server sends one decision for each valid wake request.

### Election and configuration

With cooperative selection enabled, the first claim for an exact wake phrase
opens a collection window, configured by `arbitrate_wake_window_seconds`
(default **0.75 seconds**). Claims for that phrase received within the window
compete together. A claim after the window closes opens a new election;
different phrases elect independently. This groups reports by server arrival
time, not by transcript or client timestamp.

Choose one claimant by these tie-breaks in order:

1. Highest `confidence`.
2. Highest `volume`.
3. Earliest `heard_at`.
4. Earliest server arrival order.

An omitted optional signal ranks behind a reported signal at that tier. If all
clients omit the signals, arrival order decides. Wake-time ranking uses the
signals in the request; it does not wait for streamed audio or STT.

These are server/plugin settings, not fields of `start`. The existing names
are `arbitrate_enabled` (default `false`), `arbitrate_policy`, and
`arbitrate_wake_window_seconds`. The default policy is `wake_signal`, which
performs this ranked selection.
When arbitration is disabled, immediately send `wake_grant`, still echoing the
client's `claim_id` and `wake_word`.

`start.device_id`, `start.area`, and `start.person` are not supported selection
inputs. The existing `arbitrate_device_id` is an optional server-configured label prefix;
the server appends a unique socket identifier to keep candidates distinct. The assistant's `start.assistant_name`
remains separate from the exact detected `wake_word` phrase.

### Cooperating client flows

Start a **1000 ms** local monotonic deadline when sending `wake`. Buffer
microphone PCM throughout this wait, including speech after the wake word.
This is a maximum decision wait, not a mandatory extra second after a grant:
act as soon as the matching decision arrives. The server's collection window
(default 750 ms) is distinct from the client's one-second timeout. This leaves
only a nominal 250 ms for request/response transit and scheduling for the earliest
claimant; measure actual deployment latency. A larger collection window reduces
detector skew failures but claims arriving after it still open independent
elections. Keep the client deadline at 1000 ms and fail closed on timeout.
Explicit saved server values such as `0.25` require a configuration update to
`0.75`; the new default does not replace them.

| outcome | client action |
|---|---|
| Matching `wake_grant` before the deadline | Flush buffered audio once, oldest first, then live PCM without dropping, duplicating, or reordering samples. Stream continuously through the activation, including thinking and playback. |
| Matching `wake_deny` before the deadline | Discard the activation buffer and resume local wake detection; do not stream it. |
| No matching decision within 1000 ms | Assume loss, discard the activation buffer, and resume local wake detection. No cancel or stop frame is needed. |
| Late decision or decision for another `claim_id` | Ignore it; never release an abandoned buffer or reactivate a completed claim. |

Allocate enough buffer for the full one-second wait plus detector pre-roll,
including on ESP32. At 16 kHz PCM16 mono, the wait alone is 32,000 bytes.
If buffering fails, abandon the activation rather than silently dropping its
beginning. A later detection always gets a new `claim_id`. A grant reserves
nothing server-side; if the activation is abandoned, no release is needed.

Cooperating clients prevent duplicate requests by streaming only after a
grant. The server still accepts audio from noncooperating clients after a
denial or without a wake claim; it does not enforce that client-side gate.
This does not justify streaming from losing or idle wake-word clients.


## 9. Conversation output without user input

The server can start a turn while the client is idle: a reminder, a scheduled
follow-up, or another server event. No wake word, wake grant, or user
transcript precedes it, and wake selection does not gate it. The client need
not stream audio while idle to receive it.

Use the same playback lifecycle as ordinary replies:

| output available | server-to-client sequence |
|---|---|
| Agent run starting | `thinking` → optional `speaking` + PCM → `turn_done` |
| Speech already prepared | `speaking` + PCM → `turn_done` |
| Run fails or is interrupted | `error` or `interrupted`, followed by `turn_done` |

No synthetic user `transcript` or wake event is sent. Both `thinking` and
`speaking` must establish a turn even when no earlier frame exists for it.
A client waiting for a wake word must accept and play that output. For an
idle-originated turn, the server should request `next_action: "standby"`
unless follow-up listening is explicitly requested; do not open a user
activation merely because the server spoke. For a targeted outbound turn while idle, temporarily enable
microphone streaming for interruption/control detection when `thinking` or
`speaking` arrives, subject to explicit user mute. This requires no local
wake or arbitration. After the turn and playback drain, stop that temporary
stream and restore local wake detection unless an activation is now open.

Serialize outbound audio on each socket: never interleave the untagged
binary PCM of two turns. Output waits behind an active turn and its playback
drain; stale output is discarded instead of played late. Ordinary barge-in,
`cancel`, and `drained` apply equally to these turns. Output goes to the one
device attached to that conversation. It is never broadcast and never runs a
wake election.


## 10. Countdowns

A countdown is a timer that ends with a tone. It is set by asking the
assistant; the client sends no frames to create one. A countdown is always for
the device that receives it. A spoken reminder is not a countdown: it arrives
as an ordinary server-started turn (section 9).

The frame arrives when the countdown is set, not when it ends:

```json
{"type":"countdown","countdown_id":"countdown-9","deadline_unix_ms":1794143100000,"ring_seconds":900,"sound":"alarm-1","label":"tea"}
```

`deadline_unix_ms` is server time; convert it with the clock offset from `ready`
(section 2). `label` may be absent. A device with a screen shows the time
remaining and the label. The server sends a device its countdowns again after
`ready`.

A `countdown` with a `countdown_id` the device already has replaces it. This is
how a countdown is extended or snoozed: if it was ringing, stop the tone and
count down to the new deadline.

A countdown can be cancelled, whether it is still counting or already ringing:

```json
{"type":"countdown_cancel","countdown_id":"countdown-9"}
```

### Ringing

At the deadline the device rings, whether or not it is connected. Every client
bundles four sounds: `alarm-1`, `alarm-2`, `alarm-3`, and `alarm-4`. Play the
one named in `sound`, repeating it. A countdown whose deadline passed while the
device was off is not rung late.

The alarm keeps ringing until the first of:

- a local dismissal: a button, a tap, or the `cancel` wake word heard on the
  device;
- `countdown_cancel` for it;
- a new `countdown` for it, which starts it counting again;
- `ring_seconds` of ringing.

When the alarm has ended, send `drained` with the `countdown_id` as its
`turn_id`. Do not send it for a snooze.

```json
{"type":"drained","turn_id":"countdown-9"}
```

### The `cancel` wake word

A device that can detect wake words listens for a second one, `cancel`, next to
the assistant's. It is detected on the device and needs no election:

| Heard | The device |
|---|---|
| `cancel` while an alarm rings | Ends the alarm, as a local dismissal. |
| `cancel` while the assistant is thinking or speaking | Sends `cancel` for the turn and stops playback. |
| `cancel` at any other time | Does nothing. |

A device without the `cancel` wake word ends an alarm with its button or tap,
or by asking the assistant.

### Talking during an alarm

The assistant can speak while an alarm rings. When a reply plays, or the wake
word is heard, duck the tone: keep it playing, quiet enough that speech is
clearly heard over it, and bring it back to full volume when the exchange is
over. A reply or a wake word does not end the alarm and does not restart
`ring_seconds`. "Hey Arya, snooze five minutes" and "Hey Arya, stop" are
ordinary requests; the server answers them with a new `countdown` or a
`countdown_cancel`.

After the alarm ends, return to the state
the device was in before; ringing does not open a user activation.


## 11. Changing the chat model on an attached voice connection

Model discovery and selection both use the existing authenticated voice
WebSocket. No additional client HTTP request or authentication flow is needed.
A client requests the model catalog by setting `models: true` in `start`:

```json
{"type":"start","assistant_name":"Arya","device_name":"Office Mac","models":true}
```

Only Boolean `models: true` includes `model_catalog` in `ready`. If `models` is
omitted or false, skip the lookup and omit the catalog. This does not affect
audio or the ability to select a model with `set_model`.

With `models: true`, the response includes:


```json
{"type":"ready","model_catalog":{"model":"agent","provider":"litellm","providers":[{"slug":"litellm","name":"LiteLLM","models":["agent","beta"]}]}}
```

The example omits the other normal `ready` fields. Clients reuse this catalog
for the connection. There is no separate catalog request, request ID, or refresh message.
`model` / `provider` describe the server's default inventory selection; provider
entries expose only display names, slugs, and model IDs. No upstream credentials
or provider configuration are returned. Unconfigured providers are excluded.
The server reuses its inventory API internally. If lookup fails or exceeds eight
seconds, `model_catalog` contains `{"providers":[]}` and voice remains available.
A new connection receives a new catalog only if its `start` requests one.

On the existing authenticated socket, send:

```json
{"type":"set_model","request_id":"model-7","model":"beta","provider":"litellm"}
```

`request_id` is 1–80 ASCII letters, digits, underscores or hyphens. `model` is a
nonempty string of at most 256 characters; optional `provider` is a nonempty
string of at most 80 characters. Neither accepts control characters. Send both
as `null` to restore this connection's configured `voice_model` / `voice_provider`
defaults (which can themselves defer to the server's normal model selection).

The server stores the selection for subsequent user turns on this socket and
acknowledges it without cancelling an in-flight run or changing its TTS/audio:

```json
{"type":"model_changed","request_id":"model-7","model":"beta","provider":"litellm","uses_default":false,"applies_to":"next_turn"}
```

Each new turn snapshots the current selection at submission. Already-submitted
turns retain their model. Selection changes preserve the conversation, wake
activation, microphone stream, current phase, and queued playback. They do not
create a new voice turn or run another wake election. Outbound supplied-text
announcements have no LLM selection to change.

`model_changed` confirms the stored request, not that an upstream provider has
successfully executed it. Model/provider access and execution still use the
conversation's normal execution and its error reporting. No global configuration
or another device's selection is modified. A client may persist the confirmed
preference per server and resend it after `ready` on reconnect.

Invalid selection data returns `{"type":"model_error","request_id":"model-7","error":"bad_model_selection"}`
and keeps the previous choice. An invalid/missing request ID returns
`model_error` with `error: "bad_request_id"` without echoing untrusted ID data.
These errors do not end the voice connection. A picker keeps its previous
confirmed choice on rejection or acknowledgement timeout and exposes a retry.
