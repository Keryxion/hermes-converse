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
| `name` | the assistant's name — just "Arya", not the full wake phrase. A client can have multiple wake words (e.g. "Hey Arya", "Arya") with a preamble like "Hey", but that doesn't change the name. Arya uses her name in the voice prompt so she knows she's being addressed and doesn't get confused about why she's being called that name |

Then stream binary PCM16 mono frames at `input_rate`. 30 ms blocks are ideal
(e.g. 480 samples at 16 kHz).

The server replies with `ready`, which includes the socket's `session_id`
and clock-skew timestamps.

## 3. Client control frames (send anytime)

All are JSON text frames:

| frame | meaning |
|---|---|
| `{"stop": true}` | Ends the whole session and closes the socket. History is lost. |
| `{"commit": true}` | Forces the current utterance to endpoint now (runs a turn on it). |
| `{"type": "cancel", "turn_id": "..."}` | Cancels the **current** turn — stops TTS and the agent — but keeps the socket, session, and history alive. |
| `{"type": "drained", "turn_id": "..."}` | Tells the server the reply's last audio has left the speaker, so the server won't transcribe its own echo as a new turn. |

**`cancel` vs `stop`:** `stop` tears down the session; `cancel` only kills the
current turn. Use `cancel` when the user hits the action button or says the
wake word again mid-turn and you want to keep the conversation going.

## 4. Server frames

| frame | meaning |
|---|---|
| `ready` | rates, `session_id`, clock-skew timestamps |
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


## 8. Multi-device arbitration (opt-in)

When several voice-capable devices can hear the same user (Echoes, browser
tabs, ESP32s), they can all answer at once — which is noisy and confusing.
Arbitration lets exactly one device answer each utterance while the others
stay silent. It is opt-in and off by default; a single-device setup needs
none of this.

To take part, a client sends identifying info in its `start` frame:

```json
{"type": "start", "key": "...", "input_rate": 16000,
 "output_rate": 24000, "device_id": "echo-kitchen",
 "area": "kitchen", "person": "darrell"}
```

| start field | what it does |
|---|---|
| `device_id` | a stable id for this device/socket; the arbitrator tracks one registration per socket |
| `area` | the room/area this device is in (used by the proximity policy) |
| `person` | who is speaking (used by the proximity policy to match area to speaker) |

### How it works

1. Each device hears the wake word, claims the utterance, and the arbitrator elects one winner.
2. Only the winner runs STT → agent → TTS and speaks aloud.
3. Losers still transcribe the utterance but skip the agent and TTS entirely.
4. The winner releases the turn when `turn_done` is sent or its socket closes, so another device can win the next utterance.

### Policies

All settings are off by default and fully backward compatible:

| setting | default | what it does |
|---|---|---|
| `arbitrate_enabled` | `false` | turn arbitration on or off for this socket |
| `arbitrate_policy` | `first_to_endpoint` | `first_to_endpoint`: first device to claim the utterance wins. `proximity`: the device reporting the same `area` as the speaker's `person` wins; ties fall back to first claim. |
| `arbitrate_device_id` | `""` | override the device id the server registers for this socket |

If you don't have a reliable per-person area feed, stick with the default
`first_to_endpoint` policy — it doesn't need `area`/`person` at all.


## 9. cancel vs stop vs closing

It's worth being explicit since `cancel` was added because `stop` is too destructive:

- **Close the socket** — ends the session; history is lost; cleanup happens on the server's heartbeat timeout, not immediately.
- **`{"stop": true}`** — same as closing, but explicit: the server cancels any in-flight Runs API run, stops the VAD worker, and closes the socket right away instead of waiting for a timeout. Use it when you're genuinely done with the conversation.
- **`{"type": "cancel"}`** — aborts only the current turn (stops TTS + agent, discards any in-progress capture) while keeping the socket, session, and history alive. Use it when the user hits the action button or says the wake word again mid-turn and you want to keep the conversation going.
