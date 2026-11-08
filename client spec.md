# Client spec — how to be a client to hermes-converse

`hermes-converse` exposes one WebSocket route:

```
GET /v1/audio/converse
```

One socket = one conversation. You stream microphone PCM up, the server does
voice activity detection, endpointing, speech-to-text, the agent turn, and
streams the reply back as PCM audio — all on the same socket. The socket,
session, and conversation history stay alive across turns unless you close
them.

Wake selection is cooperative: it recommends an answering device without
restricting audio processing. Section 8 defines the cooperative wake contract.

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
 "output_rate": 24000, "quiet_interval": 0, "name": "Arya"}
```

| field | what it does |
|---|---|
| `type` | always `"start"` |
| `key` | API key (required unless you used the subprotocol) |
| `input_rate` | sample rate of the PCM you'll send (8000–48000, default 16000) |
| `output_rate` | sample rate of PCM the server sends back (default 24000) |
| `quiet_interval` | session mode: seconds of silence between turns (0 = off) |
| `name` | the assistant's name — set this to "Arya" (not "Hey Arya"). Arya uses her name in the voice prompt so she knows she's being addressed and doesn't get confused about why she's being called that name |

For cooperative wake selection, follow section 8 before sending audio for a
wake activation. Otherwise, stream binary PCM16 mono frames at `input_rate`.
30 ms blocks are ideal (e.g. 480 samples at 16 kHz).

The server replies with `ready`, which includes the socket's `session_id`
and clock-skew timestamps.

## 3. Client control frames (send anytime)

All are JSON text frames:

| frame | meaning |
|---|---|
| `{"stop": true}` | Ends the whole session and closes the socket. History is lost. |
| `{"commit": true}` | Forces the current utterance to endpoint now (runs a turn on it). |
| `{"type": "cancel", "turn_id": "..."}` | Cancels the **current** turn — stops TTS and the agent — but keeps the socket, session, and history alive. |
| `{"type": "wake", "claim_id": "...", "wake_word": "Hey Arya"}` | request an advisory wake decision before streaming an activation; see section 8. |
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
| `transcript` | what the server heard; carries a `turn_id` repeated on later frames of the turn |
| `thinking` | the agent turn is running (show "thinking", not "listening") |
| `speaking` + binary frames | the reply as PCM16 at `output_rate` |
| `interrupted` | the turn was cancelled (you barged in or sent `cancel`); stop playing |
| `turn_done` | the reply is fully sent; in session mode `expects_more` says whether to keep listening |
| `quiet` | session mode: the stream has been silent for another `quiet_interval` seconds |
| `stop_word` | session mode: you said a stop phrase; no turn ran |
| `error` | the turn failed (spoken-safe text) |

## 5. Session mode (`quiet_interval > 0`)

For wake-word devices. The server reports quiet in **received** audio (never
wall-clock, so a held-open socket never accrues phantom quiet), ends the
exchange on a stop phrase, and lets the agent sign off by ending a reply with
an end phrase ("over and out"), which arrives as `turn_done.expects_more =
false`.

## 6. Typical flow

1. Open `ws://<host>:<port>/v1/audio/converse`
2. Send `start`
3. Receive `ready`
4. Stream microphone PCM
5. Receive `transcript` → `thinking` → `speaking` + PCM → `turn_done`
6. Repeat from step 4 for the next turn, or send `cancel`/`stop`

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
exchange ends (for example, a stop phrase, `expects_more = false`, or the client
returning to wake detection), a fresh wake detection starts a new activation.
Other devices may start independent activations while that conversation runs.

A denial neither closes the socket nor cancels a turn. A device may still stream
audio after a denial; the server accepts and processes it normally, including
STT, agent execution, and TTS. Duplicate answers remain possible when clients
do not follow the recommendation. Use the existing `cancel` or `stop` controls
when cancellation or session termination is actually wanted.

### Buffered wake audio

The first nonempty capture after a valid `wake` report may contain only the wake
cue retained in the client's audio buffer. If its entire transcript matches the
reported phrase or `start.name`, ignoring case and punctuation, the server stays
listening and produces no agent turn or reply frames. Aria and Arya are treated
as equivalent name spellings for this check. This works with both Smart Turn
and the fixed silence endpointer.

A continuous utterance such as “Hey Arya, set a five minute timer” is passed to
the agent intact. The wake-only guard applies to one capture per wake report;
ordinary follow-ups and explicit session-mode stop phrases retain their normal
behavior. Wake-only suppression neither closes the socket nor changes the
client's listening timeout. Election grouping still uses the exact reported
`wake_word` string.

### Wake request and decision frames

After `start` / `ready`, a cooperating client buffers microphone audio and sends
this JSON text frame as soon as its local wake detector fires, before streaming
audio for that activation:

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
(default **0.25 seconds**). Claims for that phrase received within the window
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
client's `claim_id` and `wake_word`. The legacy `first_to_endpoint` policy is not
a ranked election; it grants every wake request immediately, without a later
transcript election.

`start.device_id`, `start.area`, and `start.person` are not supported selection
inputs. The existing `arbitrate_device_id` is an optional server-configured label prefix;
the server appends a unique socket identifier to keep candidates distinct. The assistant's `start.name`
remains separate from the exact detected `wake_word` phrase.

### Cooperating client flows

Start a **1000 ms** local monotonic deadline when sending `wake`. Buffer audio
while awaiting the matching decision so the beginning of the request is kept.
The one-second limit bounds audio-buffer memory on constrained clients such as
arya-esp32.

| outcome | client action |
|---|---|
| Matching `wake_grant` before the deadline | Stream buffered audio followed by live PCM. Handle ordinary turn frames and follow-up turns on the same socket. |
| Matching `wake_deny` before the deadline | Discard buffered audio for this activation and resume wake detection. Keep the socket available. |
| No matching decision within 1000 ms | Assume loss, discard this activation's buffered audio, and resume wake detection. No extra cancel or stop frame is needed. |
| Late decision or decision for another `claim_id` | Ignore it. Never reactivate a timed-out or completed claim. |

A later detection always gets a new `claim_id`. A grant does not require audio:
if the activation is abandoned locally, no reservation needs releasing.
