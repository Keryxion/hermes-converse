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
Sections 9–10 define conversation output and timers. Section 11 defines named
Aryas and attachment to Hermes conversations.

Server lifecycle and integration details are in [the server spec](server.md).

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
| `device_name` | The configured device label, such as `Den Echo`; required for named-device connections (section 11). |
| `models` | Boolean; only `true` includes the model catalog in `ready`. Defaults to `false`. See section 12. |
| `transcripts` | Boolean; request user and assistant transcript frames when `true`. Defaults to `false`. See section 4. |
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

The server replies with `ready` and clock-skew timestamps. Named-device
attachment IDs and reconnect behavior are defined in section 11.

## 3. Client control frames (send anytime)

All are JSON text frames:

| frame | meaning |
|---|---|
| `{"stop": true}` | Stops live work and closes the socket. Preserves the device's Hermes conversation and its history for reconnection (section 11). |
| `{"commit": true}` | Ends the current utterance now, with the audio already sent (runs a turn on it). |
| `{"type": "cancel", "turn_id": "..."}` | Cancels the **current** turn — stops TTS and the agent — but keeps the socket, session, and history alive. |
| `{"type": "wake", "claim_id": "...", "wake_word": "Hey Arya"}` | request an advisory wake decision while buffering the new activation locally; see section 8. |
| `{"type": "drained", "turn_id": "..."}` | Tells the server the reply's last audio has left the speaker, so the server won't transcribe its own echo as a new turn. |

**`cancel` vs `stop`:** `stop` tears down the session; `cancel` only kills the
current turn. Use `cancel` when the user hits the action button or says the
wake word again mid-turn and you want to keep the conversation going.

## 4. Server frames

| frame | meaning |
|---|---|
| `ready` | rates, `session_id`, clock-skew timestamps |
| `wake_grant` | recommends this client handle the wake activation; echoes its `claim_id` and `wake_word`. |
| `wake_deny` | recommends this client stand down for the activation; echoes its `claim_id` and `wake_word`. |
| `transcript` | user input or assistant reply text, identified by `role` and `turn_id`; see below |
| `thinking` | starts a turn, then repeats every 5 seconds with `tool_calls` and `reasoning_tokens`; refresh inactivity without resetting speaking/playback |
| `speaking` + binary frames | the reply as PCM16 at `output_rate` |
| `interrupted` | the turn was cancelled (you barged in or sent `cancel`); stop playing |
| `turn_done` | the reply is fully sent; `next_action` tells the client to listen or enter standby after drain |
| `quiet` | session mode: the stream has been silent for another `quiet_interval` seconds |
| `error` | the turn failed (spoken-safe text) |

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
return `listen`; a partial or failed reply cannot put the client in standby.
A normal reply also returns `listen`, even when it does not ask a question.
The current server requests `standby` only for a successful server-side
sign-off in session mode; non-session/continuous mode returns `listen`.

The server decides the action. Clients must not inspect reply text, detect
sign-off phrases, or interpret whether the agent expects a follow-up.

### User and assistant transcripts

The server uses `transcript` for recognized user speech and assistant reply
text. User frames carry `role: "user"`; assistant frames carry
`role: "assistant"`.

A client requests server-sent user and assistant text with
`start.transcripts: true`; omitted or false means no `transcript` frames from
the server. The server confirms support with `ready.transcripts: true` when
enabled, or `false` otherwise. A client still receives lifecycle frames and
audio when transcripts are off.

When enabled, send at most one assistant frame per turn before `turn_done`:

```json
{"type":"transcript","role":"assistant","turn_id":"turn-42","text":"The timer is set."}
```

The text is the complete user-facing text submitted for speech, not a delta. It
applies to ordinary replies and outbound conversation output, and may arrive
before or after `speaking` and PCM. Clients associate it by `turn_id`, display
it once, and continue audio playback. Silent turns need no assistant frame.

The text describes what was prepared for speech, not what the user heard. For
an interrupted or failed turn, send only text already submitted to speech if
available, and keep the interruption or error visible. Do not send reasoning,
tool payloads, credentials, or internal errors. This does not provide reconnect
history or replay; those require a separate contract.

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
listening or return to standby after playback drain. The mechanism used by
the server to decide a sign-off is internal; there is no client-side phrase
contract.

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

Converse is an audio transport attached to a conversation. The conversation
can produce output without a new user utterance, microphone transcript,
wake detection, or wake grant. A reminder, scheduled follow-up, or server
event can start an outbound turn while the attached client is idle. Wake
selection concerns user activations; it must not authorize or gate outbound
conversation output. The client need not stream ambient audio while idle
in order to receive outbound events.

The server subscribes to **completed final responses** for attached
conversations through Hermes's public `post_llm_call` hook. It uses the
transformed final text, skips the `[SILENT]` control marker, and suppresses
voice-owned runs that already have streaming TTS. Other processes or
scheduler integrations can publish completed output using the authenticated
endpoint below. External output does not create a synthetic transcript or
another LLM run; TTS begins when the final text is available. Each delivered
output gets a fresh `turn_id` and carries its `event_id` on the initial
`thinking` / `speaking` frames.

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
binary PCM of two turns. Queue an announcement behind an active turn and
its playback drain; discard stale announcements instead of replaying them
late. Ordinary barge-in, `cancel`, and `drained` apply equally to these turns.
Output is routed to the one attached client for its exact conversation ID,
within this plugin profile. It is never implicitly broadcast and never runs
a wake election. Creation and attachment use the client's validated API key.
Cross-platform/messaging-conversation ownership is not changed by this API.

### Publishing completed output

An authenticated producer can send a completed conversation response:

```http
POST /v1/audio/conversations/<conversation_id>/output
Authorization: Bearer <API_SERVER_KEY>
Content-Type: application/json
```

```json
{"event_id":"followup-42","text":"Your task is finished.","expires_at_unix_ms":1794142860000,"next_action":"standby"}
```

`event_id` is a nonempty string of at most 128 characters; `text` is nonempty
and at most 20,000 characters. `expires_at_unix_ms` is required. Optional
`next_action` defaults to `standby`; choose `listen` to request a follow-up.
The key must pass the same profile API authentication as a voice socket.

| HTTP result | meaning |
|---|---|
| 202 `accepted` | Queued for the attached client; not an acknowledgement of audible playback. |
| 200 `duplicate` | This attachment has already accepted this event ID; no second playback is queued. |
| 400 | Invalid payload. |
| 401 | Authentication failed. |
| 409 `not_attached` | No live recipient; output is not retained for reconnect. |
| 410 `expired` | Already expired; not queued. |
| 429 `busy` | The bounded pending-output queue is full; producer must decide whether to retry before expiry. |

An in-progress user capture takes priority over unsolicited output, even
when its STT result is empty. Expired queued output is skipped before playback. Deduplication retains the
last 1024 accepted IDs per attachment; it is not a durable delivery ledger.
Hook output uses `turn:<turn_id>` IDs and a 60-second delivery lifetime.
Detaching removes the subscription and pending output; reattachment does
not replay old conversation responses. The scheduler/producer owns delivery
policy and must not equate an HTTP acceptance with the user hearing it.
This endpoint delivers supplied text; it does not append a user/model turn
to persisted history. Producers that require conversation memory must
record their completed response through the conversation owner.

### Announcement sound and synchronized playback (proposed extension)

An announcement has a distinct lead-in sound before its speech. The server
may target one attached client or an explicitly selected, authorized device
group for a system-wide announcement. Use the same announcement ID, content,
and scheduled playback time on every target; group delivery is deliberate,
not an automatic broadcast of ordinary conversation replies.

Draft header (requires negotiated announcement support):

```json
{"type":"announcement","announcement_id":"announce-42","event_id":"delivery-42","turn_id":"turn-10","sound":"announcement-v1","chime_duration_ms":600,"play_at_unix_ms":1794142805000,"expires_at_unix_ms":1794142806000,"late_policy":"skip"}
```

`play_at_unix_ms` is the intended **audible start of the lead-in sound**,
in server Unix milliseconds, not the generation time or WebSocket send
time. `expires_at_unix_ms` bounds how long the event is useful. For a group,
all members use the same IDs to identify the same announcement; each client
deduplicates it locally. A rescheduled replacement needs a new delivery
`event_id` and must explicitly supersede the old one before playback.

Send the header and the complete `speaking` + PCM payload sufficiently ahead
of the deadline to buffer it; `turn_done` marks payload completion and may
arrive before scheduled playback. Play the chime at the scheduled time,
then start speech at `play_at_unix_ms + chime_duration_ms`. Negotiate a
versioned chime asset with identical content and duration across clients;
alternatively send one server-rendered chime-plus-speech PCM payload and
schedule its first sample at `play_at_unix_ms`. Do not play the chime twice
when using that fallback. For synchronization, do not generate separate TTS
for each target. Resampling must preserve the shared audio timeline.

Estimate the server-minus-client clock offset from the existing `ready`
timestamps: record client send time `t0` for `start`, server receive/send
`server_recv_unix_ms = t1`, `server_send_unix_ms = t2`, and client receive
time `t3`. The estimate is `offset = ((t1 - t0) + (t2 - t3)) / 2`.
Convert the server deadline to client time with `play_at_unix_ms - offset`,
then schedule against a local monotonic clock. Account for measured output
buffer/device latency so the timestamp refers to sound leaving the speaker,
not merely enqueueing samples. A long-lived connection needs a periodic
clock-sync exchange (still to be specified); a handshake estimate alone
cannot bound drift indefinitely.

A target that is muted, busy with another turn, lacks complete audio or the
chime asset, or has insufficient clock confidence must report that it cannot
meet the scheduled time. Do not silently postpone just that device while
claiming a synchronized broadcast. The default `late_policy: "skip"` skips
playback if the start deadline is missed; an explicitly requested immediate
fallback may play before expiry but must report that it was unsynchronized.
Never replay an expired announcement on reconnect. Preparation/status and
cancellation frames must be defined before implementation so the server can
reschedule or report partial delivery. Group playback is best effort, not
an atomic or sample-accurate guarantee across independent audio devices.

During scheduled buffering, preserve the existing microphone state. When
announcement playback begins, temporarily stream PCM for interruption/control
detection even if previously idle, unless explicitly muted. Stop the temporary
stream after drain if no user activation is open. Mark the full
chime-and-speech interval as playback for server echo/barge-in handling,
acknowledge `drained` only after actual playback or discard, and keep the
scheduled wait separate from the agent-run timeout. User barge-in or cancel
stops that client's chime and speech; group-wide cancellation is an explicit
server operation, not an automatic consequence of one device's interruption.

**Heartbeat 2 integration.** Heartbeat 2 currently schedules turns for
messaging-gateway chats and rejects API-server sessions. Installing it does
not by itself schedule future voice output. An integration must explicitly
publish approved output to an attached API conversation; the endpoint above
is available for that delivery. Timer/scheduler integration and its policy
remain in [issue #34](https://github.com/Keryxion/hermes-converse/issues/34).
The voice system prompt does not claim unsupported scheduling abilities.

## 10. Server-owned timers and client alarm sounds (proposed extension)

The server owns timer creation, deadlines, updates, cancellation, and expiry.
The client may display a countdown, but does not schedule an independent
offline alarm. An expiry delivered by the server makes the client play a
bundled alarm sound; it need not wait for an LLM response or TTS to hear the
alarm. Spoken context, such as "Your pasta timer is done," can follow using
the outbound-turn lifecycle in section 9. A scheduling confirmation does
not play the expiry alarm.

A draft expiry frame, sent only after alarm support is negotiated:

```json
{"type":"alarm","event_id":"expiry-42","timer_id":"timer-7","turn_id":"turn-9","sound":"timer"}
```

`event_id` identifies one delivery event for deduplication; reconnect or
retry must not ring it twice. `timer_id` identifies the server timer across
connections and lets the user cancel the intended timer. `sound` names a
bundled client asset, not an arbitrary URL or audio file. A client without
alarm-sound support needs a server-rendered audio fallback using the normal
`speaking` + PCM path. Negotiate support explicitly before sending new frame
types; these fields are not supported by the current `start` / `ready`.

During the alarm, stream microphone PCM for interruption/control detection,
even if previously idle, unless explicitly muted. Restore the previous idle
state after dismissal/drain if no user activation is open.
Treat its local sound as playback for echo handling. Specify playback-start
notification to the server, stop/dismiss acknowledgement, bounded ringing,
and `drained` coordination before implementation; an unannounced local
alarm must not be transcribed as a new user request. An alarm has priority
over queued speech, but the server must finish or interrupt the current
turn before delivering it, so local sound and PCM replies do not overlap.

Cancellation changes the server record and succeeds only on server
acknowledgement. If expiry already won the race, report that result and
allow dismissal of the ringing alarm. Dismissal silences that occurrence;
it does not cancel another timer. Use correlated, idempotent operations
and explicit accepted / already-expired / not-found results; do not infer
success from a spoken promise or from the socket accepting a write.

When disconnected, the client can silence audio already playing, but it
cannot create, update, or cancel a server timer. Show cancellation as
unavailable or unconfirmed until acknowledged; do not claim offline
success. The server keeps its timer state. On reconnect, synchronize active
and missed timers before displaying them. Report expiries missed during
the disconnect without automatically replaying stale alarms. There is no
promise of an audible alarm while the client is disconnected.

Exact alarm capability negotiation, timer-operation frames, acknowledgements,
recipient attachment, and playback signalling remain implementation work.
These proposed extensions do not change the current wire protocol by themselves.


## 11. Named Aryas

Use `assistant_name` for what you call the assistant, normally `Arya`.
Use `device_name` for the device label, such as `Den Echo` or `Kitchen`.
Device labels must be unique on the server; configure the label in the client.
Many devices may share the same assistant name. The detected wake phrase is
sent separately in `wake.wake_word` (section 8).

Names are case-sensitive labels. The server normalizes Unicode to NFC, trims and collapses
whitespace, and rejects control/format characters, empty names, or a combined title over 100
characters. Use the same configured names on reconnect. The names are not additional credentials.

Each device uses a persistent conversation titled
**`<assistant_name>: <device_name>`**, for example `Arya: Den Echo`.
Clients cannot choose or override the conversation.

### Start and ready

Authenticate with the existing API key as in section 1. Send both names directly in `start`:

```json
{"type":"start","key":"<API_SERVER_KEY>","input_rate":16000,"output_rate":24000,
 "assistant_name":"Arya","device_name":"Den Echo"}
```

Wait for `ready` before streaming audio:

```json
{"type":"ready","session_id":"socket-9","recipient_id":"device-7",
 "assistant_name":"Arya","device_name":"Den Echo"}
```

`session_id` identifies the socket for diagnostics. `recipient_id` identifies
the device for timer routing. Treat these IDs as opaque. The returned names identify
the assistant and physical device for this conversation.

### Reconnect

Reconnect with the same API key and names to continue its history.
Inactivity, standby, sign-off, cancellation, and socket closure do not reset it.
Do not automatically resend an utterance whose outcome is unknown: its actions
may already have happened.

A replacement connection closes the old one with `4409 attachment_replaced`.
Stop sending on the old connection. Invalid start fields return `4400 bad_start`.
If the conversation cannot be opened, show
`4404 conversation_unavailable` rather than claiming restored context.
A `conversation_busy` error before ready means retry later while earlier work
stops. Normal authentication and first-frame errors remain as in section 1.

### Timers on named Aryas

"Set a timer" targets the requesting device. "Set a timer on Bedroom" targets
Bedroom even when requested from Kitchen. Clarify ambiguous names, and report
an unavailable destination before confirming audible delivery there (section 10).
The requesting device receives confirmation; only the destination rings.

## 12. Changing the chat model on an attached voice connection

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
