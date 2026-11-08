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

**Protocol status:** The existing connection, audio, and turn lifecycle below
remain the baseline. Section 8 defines the intended cooperative wake contract.
PR #15 does **not yet implement that contract in full**; section 9 lists the
known differences. Wake examples and table entries marked “intended” are targets
for implementation, not claims about current server support.

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
| `{"type": "wake", "claim_id": "...", "wake_word": "Hey Arya"}` | Intended: request an advisory wake decision before streaming an activation; see section 8. |
| `{"type": "drained", "turn_id": "..."}` | Tells the server the reply's last audio has left the speaker, so the server won't transcribe its own echo as a new turn. |

**`cancel` vs `stop`:** `stop` tears down the session; `cancel` only kills the
current turn. Use `cancel` when the user hits the action button or says the
wake word again mid-turn and you want to keep the conversation going.

## 4. Server frames

| frame | meaning |
|---|---|
| `ready` | rates, `session_id`, clock-skew timestamps |
| `wake_grant` | Intended: recommends this client handle the wake activation; echoes its `claim_id` and `wake_word`. |
| `wake_deny` | Intended: recommends this client stand down for the activation; echoes its `claim_id` and `wake_word`. |
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


## 8. Cooperative wake selection — intended contract

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

### Wake request and decision frames

After `start` / `ready`, a cooperating client buffers microphone audio and sends
this JSON text frame as soon as its local wake detector fires, before streaming
audio for that activation:

```json
{"type":"wake","claim_id":"activation-42","wake_word":"Hey Arya","confidence":0.93,"volume":3200,"heard_at":1794142800123}
```

| request field | meaning |
|---|---|
| `type` | Required; `"wake"`. |
| `claim_id` | Required nonempty string generated by the client; unique for each detection on this socket, including after timeout. The server echoes it unchanged. |
| `wake_word` | Required nonempty string containing the exact detected phrase. `"Arya"` and `"Hey Arya"` elect independently; do not replace both with a canonical assistant name. Clients must use identical spelling and case to compete in the same group. |
| `confidence` | Optional numeric detector confidence, on a 0–1 scale; higher wins. |
| `volume` | Optional numeric RMS level of the detected wake audio in signed PCM16 sample units (0–32768), not dB or a normalized 0–1 level; higher wins. Omit it when unavailable. |
| `heard_at` | Optional numeric Unix timestamp in milliseconds at wake detection, not send time or a monotonic clock; earlier wins. Devices need comparable clocks for this tie-break to be useful. |

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
`arbitrate_wake_window_seconds`. Use `wake_signal` for this ranked selection.
When arbitration is disabled, immediately send `wake_grant`, still echoing the
client's `claim_id` and `wake_word`. The legacy `first_to_endpoint` policy is not
the cooperative selection contract; see section 9.

`start.device_id`, `start.area`, and `start.person` are not supported selection
inputs. The existing `arbitrate_device_id` is server configuration; otherwise the
server generates a socket device identifier. The assistant's `start.name`
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

## 9. PR #15 implementation gaps

Section 8 is the intended contract. The current branch differs in these ways;
this documentation update defers the runtime changes:

| subject | current implementation | intended contract |
|---|---|---|
| Request correlation | The server ignores client `claim_id`, generates its own ID with arbitration enabled, and omits it from grants when arbitration is disabled. | Echo a client-generated ID in every decision, including when disabled, so late responses can be ignored reliably. |
| Audio after denial | The driver drops the next denied utterance before announcing a transcript or running the agent. | Process streamed audio normally; the client chooses whether to honor the recommendation. |
| Nonparticipating clients and follow-ups | Without a recorded wake decision, the driver performs transcript-key arbitration. A recorded decision is attached only to the next captured utterance. | No transcript arbitration; selection advises the client for the whole activation and its follow-ups. |
| `first_to_endpoint` | Wake requests receive immediate grants from the arbitrator. The driver then skips transcript arbitration for a granted utterance, so multiple waking devices can answer. Without a wake grant, the first transcript claim wins. | Ranked cooperative selection uses `wake_signal`; no legacy endpoint election is part of the intended client contract. |

The older `proximity` policy and its `area` / `person` inputs were removed.
Unknown policy settings, including `proximity`, currently fall back to
`first_to_endpoint`. Existing README policy descriptions describe legacy
implementation behavior; use section 8 for the intended client contract.

Contract review scenarios: matching phrases compete within a window; different
phrases and claims after the window elect independently; ties and missing
signals follow the ranking above; grants cover follow-ups; denial and timeout
return cooperating clients to wake detection; late responses cannot affect a
new claim; nonparticipating clients and audio sent after denial are processed
normally. These are acceptance criteria for future runtime work, not assertions
that the current arbitration tests validate this contract.
