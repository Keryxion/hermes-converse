# Client spec — how to be a client to hermes-converse

`hermes-converse` exposes one WebSocket route:

```
GET /v1/audio/converse
```

Each Arya has a name and uses an existing Hermes conversation, titled
`<assistant_name>: <device_name>` by default. A WebSocket carries microphone audio to the server,
which handles voice activity detection, endpointing, speech-to-text, and the
agent turn, then streams reply audio back. Conversation history persists across
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
 "output_rate": 24000, "quiet_interval": 1, "assistant_name": "Arya", "device_name": "Den Echo"}
```

| field | what it does |
|---|---|
| `type` | always `"start"` |
| `key` | API key (required unless you used the subprotocol) |
| `audio_format` | `"pcm16"` (default) or `"opus"`, for both directions for this socket. |
| `input_rate` | microphone sample rate, an integer (8000–48000 for PCM16, default 16000) |
| `output_rate` | reply sample rate, an integer (8000–48000 for PCM16, default 24000) |
| `quiet_interval` | session mode: seconds of silence between `quiet` frames, a number up to 3600 (0 = none, the default); send 1 |
| `device_name` | The configured device label, such as `Den Echo` or `Kitchen`. Each device keeps its own conversation, titled `<assistant_name>: <device_name>`. |
| `models` | Boolean; only `true` includes the model catalog in `ready`. Defaults to `false`. See section 11. |
| `wake_word_format` | The wake-word model format this device runs: `micro_wake_word` or `open_wake_word`. Only a device that sends it is given a wake word; see section 8. |
| `transcripts` | Boolean; request user and assistant transcript frames when `true`. Defaults to `false`. See section 4. |
| `after` | The `message_id` of the last transcript this client has shown, to catch up on what it missed. Only with `transcripts: true`. See "Catching up" in section 4. |
| `assistant_name` | What you call the assistant, normally `Arya`, without “Hey”. Used in the voice system prompt; separate from the device label, such as `Den Echo`. |

`assistant_name` and `device_name` are both required. `start` has no `profile`,
`name`, `conversation`, or `conversation_id` field. A `start` missing either name,
or carrying one of those fields, closes with `4400 bad_start`. The connection
uses the Hermes profile that owns the server listener; `start` does not select
a profile, and there is no profile-prefixed route.

### What `start` accepts

A name is a string of at most 98 characters as sent, with no control characters.
The server trims it, collapses each run of whitespace inside it to one space, and
applies Unicode NFC; `ready` carries the names in use, and two spellings that
come out the same are the same device.

| In `start` | Result |
|---|---|
| No first frame within 5 seconds; a binary frame; text that is not a JSON object; `type` other than `"start"` | `4400 bad_start` |
| A name that is missing, not a string, empty or only whitespace, over 98 characters, or contains a control character; a title `<assistant_name>: <device_name>` over 100 characters | `4400 bad_start` |
| PCM16 with a rate below 8000 or above 48000 | The nearest limit is used. |
| PCM16 with a rate that is not an integer | The default is used. |
| `quiet_interval` that is not a number, or is zero or negative | No `quiet` frames. |
| `quiet_interval` above 3600 | 3600. |
| `transcripts` or `models` other than `true` | `false`. |
| `after` that is not a string | As if absent. |
| `after` that is not a message of the device's conversation | `new_session: true` ("Catching up", section 4). |
| Any other field | Ignored. |

Every close before `ready` is preceded by an error frame naming it, such as
`{"type":"error","error":"bad_start"}`.

For a wake-word client, detect the wake word locally, buffer microphone
PCM while awaiting arbitration for up to **1000 ms**, and start streaming
only on a matching grant. Send the buffered audio first, in capture order,
then live mono audio at `input_rate` without a gap or duplication.
For PCM16, send little-endian samples; 30 ms blocks are ideal (e.g. 480 samples
at 16 kHz). For Opus, use the 20 ms packets defined below. Keep streaming throughout
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
audio at `output.sample_rate`, decoding Opus first when selected. The two
timestamps are for clock-skew estimation.
`session_id` identifies the socket for diagnostics and `recipient_id` identifies
the device. Treat both as opaque.

### Opt in to Opus

Send `"audio_format": "opus"` in `start` to use Opus in both directions:

```json
{"type": "start", "key": "<API_SERVER_KEY>", "assistant_name": "Arya",
 "device_name": "Den Echo", "audio_format": "opus", "input_rate": 16000,
 "output_rate": 24000}
```

The corresponding `ready` has these `input` and `output` objects; its other
fields are unchanged:

```json
{"input": {"sample_rate": 16000, "format": "opus", "block_ms": 20},
 "output": {"sample_rate": 24000, "format": "opus", "block_ms": 20}}
```

Omitting `audio_format` keeps PCM16. Opus accepts rates 8000, 12000, 16000,
24000 and 48000 in either direction, with the same defaults as PCM16. An unknown
format or invalid Opus rate closes with `4400 bad_start`, and a server that
cannot set Opus up closes with `4503 audio_unavailable`; there is no silent
fallback. The format stays fixed until reconnect.

Each binary WebSocket message is exactly one raw Opus packet containing 20 ms
of mono audio: 320 samples at 16 kHz, or 480 at 24 kHz. No Ogg, RTP, or additional
header. Configure each encoder with libopus `OPUS_APPLICATION_VOIP`, a target
bitrate of 24000 bit/s, DTX off, and complexity 5. Packet sizes vary; bitrate
is a target, not a packet length.

Feed captured samples to the encoder without changing their level. Keep encoding
and sending every 20 ms of captured audio during an activation, silence
included. Do not omit packets: each advances the received audio timeline by
20 ms, so endpointing and `quiet` keep working. This is why DTX stays off: some
encoders, such as Chromium's WebCodecs, drop DTX frames instead of sending
them. Flush wake pre-roll in capture order as Opus packets before live audio.

Keep microphone codec state across turns and activation gaps on the socket.
For replies, start fresh encoder/decoder state at the first `speaking` for each
`turn_id`; further speech in the same turn keeps its state. Reconnect creates
fresh state in both directions, including for a turn already running. Final
partial frames are zero-padded; flush encoder delay so the last speech samples
arrive before `turn_done`. A malformed, non-mono, or non-20-ms input packet
closes with `4400 bad_audio`. Playback, barge-in, mute, and standby
follow the same rules for both formats. References to PCM below describe the
samples before encoding or after decoding; binary messages use `ready`'s format.

Reconnect with the same API key and names to continue the device's conversation.
Do not automatically resend an utterance whose outcome is unknown: its actions
may already have happened.

| close or error | what to do |
|---|---|
| `4409 attachment_replaced` | Another connection with the same names replaced this one. Stop sending on it. |
| `4404 conversation_unavailable` | The conversation could not be opened. Show the failure rather than claiming restored context. It says nothing about audio. |
| `4503 audio_unavailable` | `start` was valid, but the server cannot set up the requested `audio_format`, for example Opus on a server without its codec. Reconnect with `pcm16`, which every server provides. |
| `4410 conversation_changed` | The device's conversation is a different one now, for example after the `new` command. Reconnect at once; `ready` then says whether the cursor still applies ("Catching up", section 4). |
| `conversation_busy` error before `ready` | Earlier work is still stopping. Retry later. |

## 3. Client control frames (send anytime)

All are JSON text frames:

| frame | meaning |
|---|---|
| `{"type": "cancel", "turn_id": "..."}` | Cancels the **current** turn — stops TTS and the agent — but keeps the socket, session, and history alive. |
| `{"type": "wake", "claim_id": "...", "wake_word": "Hey Arya"}` | request an advisory wake decision while buffering the new activation locally; see section 8. |
| `{"type": "heard", "wake_word": "Hey Arya"}` | A wake word was heard during a turn; see "A wake word heard during a turn" in section 4. |
| `{"type": "wake_word_set", "phrase": "Hey Arya", "status": "ok"}` | The result of switching to the wake word the server sent; see section 8. |
| `{"type": "endpointing", "on": false, "commit": true}` | `on` turns silence/Smart Turn endpointing off or on. `"commit": true` ends the current utterance now, with the audio already sent (runs a turn on it). Both fields are optional. See "Push to talk" below. |
| `{"type": "state", "turn_id": "...", "state": "listening"}` | The state the device now shows; see "The turn's state" in section 4. |
| `{"type": "countdown", "countdown_id": "...", "ringing": true}` | A countdown is ringing, or with `ended` has ended; see section 10. |
| `{"type": "commands"}` | Ask which commands the server runs; see "Commands" below. |
| `{"type": "command", "name": "new"}` | Run a Hermes command for this device's conversation, such as starting a fresh one; see "Commands" below. |
| `{"type": "say", "text": "..."}` | Speaks the given text (trimmed, at most 400 characters) as a reply of its own: `thinking`, `speaking` + PCM, then `turn_done` with `next_action: "standby"`. No agent turn runs, nothing is added to the conversation, and no `transcript` frame is sent for it. For fixed device prompts such as a spoken help reminder. Empty or non-string text is ignored. |
| `{"type": "approval_response", "approval_id": "...", "choice": "once"}` | Answer a pending approval from a screen with one of its `choices`; see "Approvals" in section 4. |

Every control frame has a `type`. To end the session, close the socket: the
device's conversation and its history are kept for reconnection, and a turn
still running finishes without being spoken ("Catching up", section 4). To stop
that turn as well, send `cancel` before closing.

`cancel` only kills the current turn; the socket and conversation carry on. Use
it when the user hits the action button. Nothing heard is a `cancel`: neither the
assistant's wake word nor the `cancel` wake word cancels a turn. A wake word
heard during a turn is reported with `heard` (section 4, "A wake word heard
during a turn").

### What a control frame accepts

A frame the server cannot use never closes the connection, with one exception:
a bad Opus packet.

| Sent | Result |
|---|---|
| Text that is not JSON, JSON that is not an object, or an unknown `type` | Ignored. |
| `cancel` whose `turn_id` is not the current turn's | Ignored. A `cancel` with no `turn_id`, or `null`, is untargeted. |
| `endpointing` whose `on` is not a boolean | `on` is ignored; `"commit": true` still commits. |
| `endpointing` whose `commit` is not `true` | No commit; a boolean `on` still applies. |
| `countdown` with no string `countdown_id`, with one the server does not have, or with neither `"ringing": true` nor `"ended": true` | Ignored. |
| `say` whose `text` is not a string or is empty after trimming | Ignored. |
| `say` whose `text` is over 400 characters | The first 400 are spoken. |
| `approval_response` for another request, a second one, or a `choice` not offered | Ignored. |
| `heard`, whatever it carries | Recorded. No frame answers it. |
| `client_diagnostics` the server did not ask for, or sent after its deadline | Ignored. |
| `wake` without a nonempty string `claim_id` and `wake_word` | `{"type":"error","error":"bad_wake"}` (section 8). |
| `command` with a bad `name` or `args` | `command` with `error` ("Commands" below). |
| `set_model` with a bad `request_id`, `model`, or `provider` | `model_error` (section 11). |
| A malformed, non-mono, or non-20-ms Opus packet | `4400 bad_audio` |

### Push to talk

When a talk button is held, turn endpointing off:

```json
{"type":"endpointing","on":false}
```

Binary PCM while the control is held, then on release:

```json
{"type":"endpointing","on":true,"commit":true}
```

While endpointing is off the server does not endpoint on silence or Smart Turn;
a pause mid-sentence remains part of the same request, and only `commit` ends
it. `commit` submits the audio already sent; an `on` in the same frame takes
effect after that audio, so it cannot cut the held utterance short. `on` stays
as last set until the device changes it, and each connection starts with
endpointing on. The utterance cap still applies as a memory backstop.

Quick taps and wake-word turns use normal endpointing. Send no PCM while the
control is released, including while the reply plays, unless the turn's ending
opens an activation ("Handling `turn_done.next_action`", section 4). Barge-in, `cancel`, and
every other frame keep their usual meaning, and `commit` with no utterance in progress is ignored.

### Commands

A chat user types commands such as `/new`. An app gives the person the same
reach with a control, such as a "New conversation" button, that names one:

```json
{"type":"command","name":"new"}
```

`name` is the command without a leading slash. `args` is an array of strings,
one per argument. Omit it for no arguments, the same as `[]`:

```json
{"type":"command","name":"title","args":["Kitchen timers"]}
```

```json
{"type":"command","name":"compress","args":["here","3"]}
```

The server ends any turn in flight, runs the command for the device's
conversation, and answers with the same frame plus the command's own reply:

```json
{"type":"command","name":"new","reply":"Started a new conversation."}
```

`reply` is text to show, not to speak. Any command the server's Hermes runs
for a chat user can be named.

To learn which those are, for a command menu, send `{"type":"commands"}`. The
server answers with every command a chat can run, in Hermes's own order:

```json
{"type":"commands","commands":[
  {"name":"new","description":"Start a new session (fresh session ID + history)","aliases":["reset"],"args_hint":"[name]","subcommands":[]},
  {"name":"title","description":"Set a title for the current session","aliases":[],"args_hint":"[name]","subcommands":[]}
]}
```

| field | Meaning |
|---|---|
| `name` | What to put in a `command` frame's `name`. |
| `description` | One line to show beside it. |
| `aliases` | Other names for the same command. |
| `args_hint` | How the command reads `args`, for display: `<x>` is required, `[x]` optional, `a\|b` a choice. Empty when it takes none. |
| `subcommands` | Values the first argument may take, for completion. Often empty. |

Hermes has no stricter description of arguments than `args_hint`; each command
validates its arguments and returns its own explanation when they are wrong.
Commands that only run in a terminal are not listed. The list is the same for every device, like
Hermes's Telegram command menu: when the server's slash-command access settings
keep a command from a device, it is still listed, and running it brings Hermes's
refusal back in `reply`. The list can change when the server's
Hermes is updated; ask again after each `ready` rather than storing it.

A command that starts a new conversation, `new` or its alias `reset`, needs nothing special from the
app. After the command changes the conversation and the server answers, it closes the connection with
`4410 conversation_changed`. The app reconnects as it would after any drop, and
`ready` comes back with `new_session: true`, which is where it clears its view
("Catching up", section 4). The earlier conversation is not deleted; it stays
in Hermes's history. Other commands, such as `title`, leave the connection
open.

A command that did not run is answered with `error` in place of `reply`, and
nothing changes:

```json
{"type":"command","name":"/new","error":"bad_command"}
```

| `error` | Meaning |
|---|---|
| `bad_command` | `name` is missing or is not lowercase letters, digits, `-` and `_` (at most 40), or a supplied `args` is not an array of strings. |
| `unknown_command` | Hermes has no command by that name that runs outside a terminal. |
| `failed` | The server did not accept the command, or it did not finish. |

A command Hermes refuses after it runs, such as one the access settings keep
from this device or one given bad `args`, is not an error here: its refusal
comes back in `reply`.

Countdowns are unaffected by a command.

## 4. Server frames

| frame | meaning |
|---|---|
| `ready` | `input` and `output` rates, `session_id`, clock-skew timestamps; see section 2 |
| `wake_grant` | recommends this client handle the wake activation; echoes its `claim_id` and `wake_word`. |
| `wake_deny` | recommends this client stand down for the activation; echoes its `claim_id` and `wake_word`. |
| `state` | the state to show during a turn: `thinking`, `speaking`, or `listening`; see "The turn's state" below |
| `transcript` | user input or assistant reply text, identified by `role` and `turn_id`; see below |
| `thinking` | starts a turn, then repeats every 5 seconds with `tool_calls` and `reasoning_tokens`; refresh inactivity without resetting speaking/playback |
| `approval_request`, `approval_resolved` | the assistant needs permission before it acts, and when that is settled; see "Approvals" below |
| `speaking` + binary frames | mono reply audio in `ready.output.format` at `output_rate` |
| `interrupted` | the turn was cancelled (you barged in or sent `cancel`); stop playing |
| `turn_done` | the reply is fully sent; `next_action` is usually absent; `standby` ends the exchange and `listen` switches the client into listening, after drain; `mute` mutes it at once |
| `quiet` | session mode: `quiet_seconds` of silence since listening began; sent every `quiet_interval` seconds |
| `error` | the turn failed (spoken-safe text) |
| `{"type":"tone","tone":"confirm"}` | Play the named tone (see `tones.md`): `confirm` or `error` when the assistant chooses to, `approval` just before the server asks for an approval. It is not a reply: no `turn_id`, and the device reports no state for it. |
| `client_diagnostics` | Request a fresh version, build, and diagnostic snapshot; answer on this socket with the same `request_id`, as below. |
| `countdown`, `countdown_cancel` | Start, extend, or cancel a countdown that ends with a tone (section 10). |
| `set_wake_word` | Switch to this wake phrase and its models, then answer with `wake_word_set` (section 8). |

These are the only server frames, apart from `model_changed` and `model_error`
(section 11). Turn liveness is the repeated `thinking` frame; there is no
separate keep-alive frame. Ignore a frame type you do not recognize.

Reply PCM arrives at about playback speed: the first audio is sent immediately,
then the server stays at most about 1.5 s ahead of real-time playback. Keep
reading the socket while you play. A buffer of 1.5 s plus network jitter never
has to stop reading, so `interrupted`, `turn_done`, and pings reach you without
waiting behind queued audio.

### Client diagnostics

The assistant can request diagnostics from this connected client. On receiving:

```json
{"type":"client_diagnostics","request_id":"diag-1"}
```

Return a fresh snapshot with the same `request_id`:

```json
{"type":"client_diagnostics","request_id":"diag-1","version":"1.2.3","build":"145","platform":"macos","diagnostics":{"state":"listening","muted":false,"conversation_window":"hidden"}}
```

Always include the running app's `version` and `build` as nonempty strings, read
from its installed package or firmware, not a release lookup. `diagnostics` is
an object; `platform` is an optional nonempty string. Include the client's connection,
microphone, wake detection, playback and window state where applicable, plus
recent bounded diagnostic events already collected by the app. Omit unavailable
measurements rather than inventing values. Report collection failure with the
same `request_id` and a nonempty `error` string instead of a snapshot:

```json
{"type":"client_diagnostics","request_id":"diag-1","error":"Microphone diagnostics unavailable."}
```

The response is at most 32 KiB of UTF-8 JSON. Exclude credentials, authentication
headers, audio, transcript text, assistant reply text and other message content.
This request reads existing diagnostics; it does not enable recording, change
settings, reconnect, or interrupt the exchange. It works without telemetry export
being configured. Answer on the socket that received the request; do not send
unsolicited snapshots or carry a pending response to a replacement connection.

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
provide clean microphone audio without suppressing user speech. Send it at the
level it was captured: no automatic gain control and no fixed digital gain on
the upstream path. Echo cancellation, lossless format conversion and analog
preamp settings are allowed; a wake word engine that needs another level changes
only its own copy. The server handles level ([audio.md](audio.md)).

### Barge-in and cancellation

Barge-in yields the floor to the user. For example, after “turn on the light”,
the user may add “and make it warm white” while the reply is thinking. Expect
`interrupted` and `turn_done` without `next_action` for the old turn,
then a new reply turn for the additional instruction (and a `transcript` if enabled). The
new turn uses the same conversation and includes the original request as context.

If the old reply had finished but none of it had played, it is not lost: it
arrives first as a server-started reply turn (it carries `event_id`, and
`next_action` is absent), then the reply to the additional instruction.

The words that follow the assistant's wake word during an activation, such as
"Hey Arya, actually make it the kitchen" while the reply plays, are barge-in
like any other speech: the device is already streaming, and the server hears
them, interrupts the reply, and answers what follows. Do not send `cancel` and
do not send another `wake`. A wake word that cancelled the turn would throw that
request away and make the person say the wake word twice. The same goes for
"cancel", "stop", or "never mind" said during a turn: they are speech to the
server, and the assistant stops.

Explicit `cancel` stops the matching turn. Whether its request is already a
message of the conversation depends on when it lands ("User and assistant
transcripts" below):

```json
{"type":"cancel","turn_id":"turn-1"}
```

A stale targeted cancel cannot stop a newer turn. An untargeted
`{"type":"cancel"}` also abandons a capture in progress: the audio is dropped,
and when no turn is running no frame answers it. The connection and
conversation remain usable.

### Frame order when a turn is cut

Binary audio carries no `turn_id`, so the order is the contract:

- Every binary frame belongs to the turn of the latest `speaking` frame.
- After `interrupted` for a turn, the server sends no more audio and no more
  frames for it except its `turn_done`. `thinking`, transcript pieces, and audio
  sent before `interrupted` may still arrive ahead of it; they belong to the old
  turn.
- A continuation's first frame comes after the old turn's `turn_done`, and its
  audio only after its own `speaking`.

Speech during thinking (turn A is cut, B answers the continuation):

```
← {"type":"state","turn_id":"A","state":"thinking"}
← {"type":"thinking","turn_id":"A","tool_calls":0,"reasoning_tokens":null}
← {"type":"transcript","role":"user","turn_id":"A","message_id":"m-10","text":"Turn on the light"}
     (the person speaks)
← {"type":"interrupted","turn_id":"A"}
← {"type":"turn_done","turn_id":"A"}
     (the person stops; the server endpoints and transcribes)
← {"type":"state","turn_id":"B","state":"thinking"}
← {"type":"thinking","turn_id":"B","tool_calls":0,"reasoning_tokens":null}
← {"type":"transcript","role":"user","turn_id":"B","message_id":"m-11","text":"and make it warm white"}
← {"type":"state","turn_id":"B","state":"speaking"}
← {"type":"speaking","turn_id":"B"}
← binary audio for B
← {"type":"turn_done","turn_id":"B"}
```

Speech during the reply's audio:

```
← {"type":"speaking","turn_id":"A"}
← binary audio for A
     (the person speaks)
← {"type":"interrupted","turn_id":"A"}      discard A's queued audio
← {"type":"turn_done","turn_id":"A"}
→ {"type":"state","turn_id":"A","state":"listening"}
← {"type":"state","turn_id":"B","state":"thinking"}   and B continues as above
```

Speech after `turn_done`, while the reply is still playing:

```
← binary audio for A
← {"type":"turn_done","turn_id":"A","next_action":"standby"}
     (the device is still playing; the person speaks)
← {"type":"interrupted","turn_id":"A"}      discard A's queued audio
← {"type":"turn_done","turn_id":"A"}        replaces the earlier one: no standby
→ {"type":"state","turn_id":"A","state":"listening"}
← {"type":"state","turn_id":"B","state":"thinking"}   and B continues as above
```

`cancel` gives the same frames for A in each of the three cases, and no turn B:

```
→ {"type":"cancel","turn_id":"A"}
← {"type":"interrupted","turn_id":"A"}
← {"type":"turn_done","turn_id":"A"}
```

Do not use an `agentTurn`, thinking, or playback flag to suppress PCM during
an activation. Play incoming PCM and send microphone PCM concurrently.
Local echo cancellation may clean the signal, but must preserve user speech.
Do not replace microphone audio with synthetic silence during playback.
Unmuting lands in standby ("Handling `turn_done.next_action`"); never replay
audio accumulated while muted.

Muting while listening commits what was already said: stop microphone capture,
send all audio already captured in order, then send
`{"type":"endpointing","commit":true}` before
stopping upstream audio. This ends the utterance without waiting for more
silence. With Opus, finish the partial packet and flush encoder delay before
`commit`. The server ignores a commit with nothing said. Backgrounding and
muting in standby or during a reply do not send `commit`.

On `interrupted`, stop and discard queued reply audio for that turn, then report
the state you move to. `turn_done` means the server finished sending, not that
playback drained. Leaving `speaking` says playback finished or was discarded,
not that a person heard the reply.
Keep streaming until the reply's audio has drained, so the person can still
interrupt it. Report the state for the old turn's playback only; it must not
discard a continuation. A bounded playback-tail timeout handles a missing
report.

### A wake word heard during a turn

A device that detects wake words keeps detecting them during a turn and while
an activation is open. When it hears the assistant's wake word or the `cancel`
wake word (section 10) then, it sends:

```json
{"type":"heard","wake_word":"Hey Arya"}
```

`wake_word` is the exact phrase detected, as in `wake` (section 8); for the
`cancel` wake word it is `"cancel"`. The device reports whatever it heard: it
does not wake, claim or cancel for it. `heard` is neither `cancel` nor barge-in:
the server records it, and the turn carries on unchanged. No frame answers it.
The server may hold a turn against interruption by microphone audio; in such a
turn `heard` is how the person's voice interrupts, and `interrupted` follows it.
It is mostly for half-duplex devices, where the server cannot hear the person
while a reply plays.

### The turn's state

The `state` frame goes both ways. During a turn the server says which state to
show, each time it changes:

```json
{"type":"state","turn_id":"turn-42","state":"listening"}
```

| `state` | Sent when |
|---|---|
| `thinking` | The turn starts, and again when the assistant goes back to work after waiting on the person, such as after an approval is answered. |
| `speaking` | Speech is about to play: the reply, or a spoken approval question. |
| `listening` | The turn is waiting on the person and the server is listening for them, such as for an approval answer once its question has played. |

Show the latest `state` of the active turn. A state that arrives while reply
audio is still playing takes effect when that audio has drained. A `state` for a
new `turn_id` starts that turn, as `thinking` or `speaking` would; ignore one for
a completed, cancelled, or stale turn.

Nothing else changes the state during a turn: `thinking` frames carry liveness
and counters, approval frames open and close the on-screen box, and `speaking`
marks where the audio begins. What follows `turn_done` is in the table
under "Handling `turn_done.next_action`". States the device
knows by itself, such as
connecting, arbitrating, or muted, are the device's own (`animations.md`).

The device sends `state` back each time the state it shows changes, with the
same shape:

```json
{"type":"state","turn_id":"turn-42","state":"listening"}
```

`state` is one of the states in `animations.md` that the device has entered.
Give the `turn_id` of the turn the change belongs to, and omit it for a change
outside a turn, such as returning to standby after a quiet period. Leaving
`speaking` is how the server knows the turn's audio has played or been
discarded: until then it keeps rejecting the reply's echo, and it holds the
next server-started reply.

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
Mark the turn active. Repeated frames with the same active
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
starting or finishing, a model call, reply text, reply audio, or waiting on a
pending approval; a turn that keeps working is not cut off. Repeated `thinking` frames are liveness, not
progress: they never extend this limit, and any absolute client turn limit
stays independent of them.
The server stops these updates before terminal `interrupted`, `error`, or
`turn_done` frames and on disconnect. Network delays can still deliver an old
frame after local cancellation, so check its turn ID before refreshing timers.
Continue handling `speaking`, PCM, `interrupted`, `error`, and `turn_done` normally.

### Approvals

When the assistant needs permission before it acts, the turn waits. The server
plays the `approval` tone, asks out loud with a short summary, and also sends:

```json
{"type":"approval_request","turn_id":"turn-42","approval_id":"approval-7","summary":"Arya wants to open a network connection to the Kitchen speaker to read its logs. Okay?","command":"nc 10.0.4.17 6053","choices":["once","session","always","deny"]}
```

`thinking` frames continue while it waits. The person can answer by voice on any
device; the client does nothing special for that, since it already sends
microphone audio during a turn.

The `state` frames tell the person when to talk: `speaking` while the
question plays, `listening` once the server is listening for the answer, and
`thinking` as soon as the answer is taken. A question asked again is `speaking`
then `listening` again. `thinking` frames during the wait do not change the
state.

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
that `approval_id`. An unanswered approval ends as `expired` after the server's
approval timeout (default 300 seconds).

While an approval is pending, apply no turn limit of your own except
inactivity, which `thinking` frames keep fresh: the server ends the wait. After
`approval_resolved`, any absolute limit the client keeps starts again from
zero.

`summary` and `command` are text to display; never act on their contents. Never
send an allowing choice without the person tapping it, and do not send an old choice
again after reconnecting. An approval still pending when the client reconnects
is sent again as a new `approval_request` with the same `approval_id`; show it. A device with no screen ignores both server frames.

### Handling `turn_done.next_action`

`turn_done` ends every turn, and `next_action` on it is optional:

```json
{"type":"turn_done","turn_id":"turn-42"}
```

```json
{"type":"turn_done","turn_id":"turn-42","next_action":"standby"}
```

```json
{"type":"turn_done","turn_id":"turn-42","next_action":"mute"}
```

```json
{"type":"turn_done","turn_id":"turn-42","next_action":"listen"}
```

`listen`, `standby`, and `mute` are the only values; treat any other as absent.
They are the three states the microphone can be in, and `next_action` names
the one to move to:

| State | Microphone | Left by |
|---|---|---|
| Listening | An activation is open; PCM streams to the server. | `quiet` (section 5), `standby`, `mute`, or the device's own controls. |
| Standby | No PCM, except to let the person interrupt a reply, as below. The wake word or Talk opens an activation. | The wake word, Talk, or `listen`. |
| Muted | No PCM, and no wake word. Talk does nothing. | Only the person, with the device's mute control. Unmuting always lands in standby. |

A device in standby streams while a reply plays, for interruption only
(section 9), and stops when the ending is applied. The exception is a turn made
by holding Talk: after release it sends no PCM during the reply. While Talk is
held, the button governs the microphone, over everything but `mute`; an ending
that arrives then is applied on release.

How each is shown is in `animations.md`. A muted device reports `standby` in
`state`; mute is shown alongside it. What the microphone does after a turn
depends on how the turn ended and on the state the device is in when the
ending is applied:

| The turn ended with | Listening | In standby | Muted |
|---|---|---|---|
| `turn_done`, no `next_action` | Stay listening for a follow-up. | Stay in standby. | Stay muted. |
| `turn_done`, `listen` | Stay listening. | Listen: open an activation and stream, with no wake claim. | Stay muted. |
| `turn_done`, `standby` | Standby. | Stay in standby. | Stay muted. |
| `turn_done`, `mute` | Mute. | Mute. | Stay muted. |
| `error`, then `turn_done` | Stay listening. | Stay in standby. | Stay muted. |
| `interrupted`, then `turn_done`, after this device's `cancel` or `command` | Stay listening. | Stay in standby. | Stay muted. |
| `interrupted`, then `turn_done`, with neither from this device: the person spoke | Stay listening; the continuation follows. | Listen; the continuation follows. | Stay muted. |

Apply `mute` on receipt: the reply keeps playing with the microphone already
off. Apply every other row once the turn's audio has drained or been discarded.
Then report the state you are in.

Muting is the same whoever asks: the device's own control, or `mute`. It ends
the exchange, and nothing the server sends unmutes. `cancel` alone leaves the
microphone as it is, including a `cancel` the device sends for a timeout of its
own. A Stop control the person presses is `cancel` and then standby; only a
control the person uses may add that. A control that only stops listening, like
ending dictation, is not a Stop control: it sends
`{"type":"endpointing","commit":true}` if the person was speaking, then goes to
standby, and a reply in progress plays on.
Label it so it cannot be read as stopping the reply, such as "Stop listening".

An activation that `listen` opens ends as any other does, `quiet` included; it
has no time limit of its own.

The `turn_done` of a failed or interrupted turn never carries `next_action`.
When `interrupted` or `error`, and a second `turn_done`, arrive for a turn
whose `turn_done` already came and whose ending has not been applied, the
second one replaces the first: a pending `standby` or `listen` is dropped, and
the `interrupted` or `error` row applies. A `mute` already applied stays. Once
an ending has been applied, ignore later frames for that turn. Ignore
`next_action` on a `turn_done` for a turn the device never saw start, and do
not apply a completion from an abandoned turn to a newer activation.

The server sends `standby` when the person has said they are done, `mute` when
they have asked the device to stop listening, and `listen` only when it wants
the person's answer from a device that would not otherwise be listening.
Clients must not inspect reply text or interpret whether the agent expects a
follow-up. Countdown tones restore the prior state after drain (section 10).

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
{"type":"transcript","role":"assistant","turn_id":"turn-42","text":"The dentist at nine and lunch with Sam."}
```

```json
{"type":"transcript","message_id":"m-124","role":"assistant","turn_id":"turn-42","text":""}
```

Each frame with text is the next piece of the reply, sent before the PCM that
speaks it. Append the pieces for a `turn_id` in the order they arrive. They
carry no `message_id`.

When the reply is complete, one closing frame follows with empty `text` and the
reply's `message_id`. It adds nothing to show: it says the reply is whole and
gives the ID to keep as the catch-up cursor. Every reply that is a message of
the conversation ends this way, so a one-sentence reply is two frames. That
covers ordinary replies and server-started replies (section 9). A reply that is
interrupted or fails has no closing frame, and a silent turn has no assistant
frame at all.

Speech that is not a message of the conversation has no `transcript` frames,
neither pieces nor a closing frame, and is never replayed: `say`, and the
spoken question of an approval. So every `message_id` a client receives names a
message of the conversation and works as `after`.

When a reply is interrupted and the request is run again, the new reply arrives
under a new `turn_id`, with no user `transcript` between the two. Replace the
interrupted reply with the new one; do not show both. An interrupted reply with
a new user `transcript` after it stays, marked as interrupted.

A user `transcript` is one frame and always carries `message_id`. It is sent
when the request has become a message of the conversation, so it follows that
turn's first `thinking` frame and comes before its `interrupted`, `error`, or
`turn_done`. Its `text` is that message. A `message_id` is unique in the
conversation; treat it as opaque.

A request that never became a message has no user `transcript`, live or
replayed. Where a `cancel` or an interruption lands decides which:

| The turn is cut | Live | Replayed later |
|---|---|---|
| Before the request reaches the conversation: during capture, transcription, or before the agent starts | No user `transcript`. | Nothing. Words cut by the person's further speech are not lost: they become part of the continuation's message. |
| While it is being handed over | The server waits to learn which it was. A user `transcript` with its `message_id` comes before `interrupted` if the message was stored; otherwise none. | The same as live. |
| After the message is stored | The user `transcript`, with its `message_id`. | The user `transcript`, with no assistant transcript after it when no reply was stored: show it as not answered. |

Live and replay always agree: a client that saw no user `transcript` for a
turn will not be sent one on catching up, and one it saw will be there.

Assistant text is what the reply says aloud: the reply text with Markdown
removed, or the text given to the assistant's speech tool. Live pieces stop
where the reply was cut, so an interrupted or failed turn shows only the text
already sent for speech; keep the interruption or error visible. It is what was
prepared for speech, not what the person heard. Reasoning, tool payloads,
credentials, and internal errors are never sent.

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

When `start` carries `after`, `ready` carries `new_session`:

```json
{"type":"ready","session_id":"socket-10","recipient_id":"device-7","transcripts":true,"new_session":true}
```

| `new_session` | Meaning | What to do |
|---|---|---|
| `false` | The cursor is a message of the device's conversation. | Append the replayed transcripts. |
| `true` | The conversation is a different one now: a new one was started, from this app or anywhere else, or the old one is gone. | Clear the conversation view and forget the cursor, then show what is replayed. A brand-new conversation replays nothing. |

This is the one place a client learns the conversation was cleared, however it
happened.

A replayed assistant transcript is the whole reply in one frame: everything
the reply says aloud, however much of it was played. A reply that finished
while no device was connected was never spoken, and is replayed with the text
it would have said. A reply that says nothing is not replayed. If the client
already shows pieces for that `turn_id`, replace them with the replayed frame.

A `replay` transcript is only for display. It does not start a turn: do not arm
a timer, show thinking, play audio, resend the request, or open the microphone
because of one. A user transcript with no assistant transcript after it, and no
turn running, was not answered: show it that way.

A turn does not end because the connection dropped. If the person asked
something and the app went away, the assistant still finishes, and the answer is
there to catch up on. It is not spoken later. `cancel` still ends a turn.

### Reconnecting during a turn

When the connection drops, discard any reply audio still queued. A turn still
running when the client reconnects continues on the new connection. After
`ready`, in this order:

1. The replayed transcripts, as above. The running turn's user `transcript` is
   among them once it is a message.
2. With `transcripts: true`, when the running turn has reply text: one
   assistant `transcript` with that `turn_id`, `replay: true`, and no
   `message_id`, holding the reply's text so far, including text prepared
   while the client was away. Replace any pieces shown for that `turn_id` with
   it. It is not a cursor.
3. `state` for the turn with its current state, then `thinking` with the turn's
   counters. The counters carry on from where they were; they do not restart at
   zero. `thinking` then repeats every 5 seconds as before.
4. `approval_request` again, if the turn is waiting on one.
5. The rest of the turn, live: further assistant pieces to append after the
   frame from step 2, `speaking` and audio, the closing `transcript` with the
   reply's `message_id`, and `turn_done`.

```
→ {"type":"start", ..., "transcripts":true,"after":"m-122"}
← {"type":"ready", ..., "new_session":false}
← {"type":"transcript","message_id":"m-123","role":"user","turn_id":"turn-41","text":"What's on my calendar tomorrow?","replay":true}
← {"type":"transcript","role":"assistant","turn_id":"turn-41","text":"Two things tomorrow.","replay":true}
← {"type":"state","turn_id":"turn-41","state":"thinking"}
← {"type":"thinking","turn_id":"turn-41","tool_calls":3,"reasoning_tokens":2400}
← {"type":"transcript","role":"assistant","turn_id":"turn-41","text":" The dentist at nine and lunch with Sam."}
← {"type":"state","turn_id":"turn-41","state":"speaking"}
← {"type":"speaking","turn_id":"turn-41"}
← binary audio
← {"type":"transcript","message_id":"m-124","role":"assistant","turn_id":"turn-41","text":""}
← {"type":"turn_done","turn_id":"turn-41"}
```

No audio is sent twice, and none is sent for text prepared while the client was
away: audio starts with the first text prepared after the reconnect, behind a
new `speaking` and with fresh codec state. A turn that finished while the client
was away sends nothing live; it is all in the replay.

## 5. Session mode (`quiet_interval > 0`)

For wake-word devices. Send `"quiet_interval": 1` in `start`. While you are
listening and nobody is speaking, the server sends `quiet` after each further
second of silence, with the total so far:

```json
{"type": "quiet", "quiet_seconds": 1.0}
{"type": "quiet", "quiet_seconds": 2.0}
{"type": "quiet", "quiet_seconds": 3.0}
```

Return to standby when `quiet_seconds` reaches your threshold, 5 seconds by
default. The threshold is the device's: it is not sent to the server, and the
server never ends an exchange for silence. `quiet_interval` is only how often the frames come.

The server counts silence in **received** audio (never wall-clock, so a
held-open socket never accrues phantom quiet). Each listening phase counts from
zero: speech, a turn, and resuming upstream audio after stopping it all restart
the count. Audio sent while a request is captured, transcribed or answered is
not counted.

Silence sent before you stopped streaming does not carry over.

Manual Talk is the special case. When the person opens listening with the Talk
control or the device's action button, wait four times the threshold, 20
seconds by default, for their first request (a new turn starts): they pressed
it to speak, and get extra time to compose their thoughts. After that request,
`quiet` returns to standby at the usual threshold. A wake word gets no extra
time. A `turn_done` with `next_action: "standby"` also returns to standby
("Handling `turn_done.next_action`", section 4).

## 6. Typical flow

1. Open `ws://<host>:<port>/v1/audio/converse`, send `start`, and receive `ready`.
2. Detect the wake word locally; send `wake` and buffer PCM while awaiting
   the matching grant for up to 1000 ms. On denial/timeout, discard the buffer.
3. On grant, flush the buffered PCM and continue with live PCM. Server VAD
   endpoints the user request, then emits `thinking` (followed by the user `transcript` if enabled).
   The user may pause after the wake word, or mid-request, for up to
   `endpoint_max_silence_ms` (default 3 s) without the request being split.
4. Keep sending live PCM during thinking, `speaking` + reply PCM, and queued
   playback. The server captures accepted interruptions as continuations.
5. Receive `turn_done`; when the speaker has drained or the audio was discarded, report the new `state`.
6. If the activation remains open, keep streaming for a follow-up request.
   Otherwise stop upstream audio and return to local wake detection.

This is the typical sequence, not a prerequisite for handling `thinking`:
the client must enter the active-turn state even without a preceding
`transcript` frame.

## 7. turn_id

Every turn gets a `turn_id`. It appears on `transcript`, `thinking`,
`speaking`, `interrupted`, `error`, and `turn_done` frames of that turn, so
you can match frames to a turn. `cancel` and the client's `state` accept an
optional `turn_id` to target a specific turn.


## 8. Cooperative wake selection

### The wake word

Every wake-word device ships with a working `Hey Arya` model and starts
with that phrase on first setup. Name the model format the device runs in
`start`:

| `wake_word_format` | File | Used by |
|---|---|---|
| `micro_wake_word` | An ESPHome microWakeWord manifest. Its `model` names the `.tflite` file beside it. | ESP32, and Echo running microWakeWord |
| `open_wake_word` | An openWakeWord head `.onnx`. The melspectrogram and embedding models stay bundled with the client. | macOS, iOS |

The server sends the configured phrase in `ready`, with a model only in that
format:

```json
{"type":"ready","wake_word":{"phrase":"Hey Sakura","models":{
  "micro_wake_word":{"url":"/v1/audio/wake/hey_sakura/manifest.json","sha256":"9f2c…"}}}}
```

Without `wake_word_format`, `ready` has no `wake_word` and the server never
sends `set_wake_word`.

Fetch a model with `GET` on the server's host and port, with
`Authorization: Bearer <API_SERVER_KEY>`. `sha256` is the hash of the file at
`url`. For microWakeWord, also fetch the manifest's model file, resolving it
relative to the manifest URL and using the same key. Cache downloads by
`sha256`; reuse a cached model when its hash matches. If no download is
offered for a phrase already supplied by a bundled model, use that model.

The server can change the wake word on an open socket with `set_wake_word`.
It carries the same `phrase` and `models` as `ready.wake_word`:

```json
{"type":"set_wake_word","phrase":"Hey Sakura","models":{
  "micro_wake_word":{"url":"/v1/audio/wake/hey_sakura/manifest.json","sha256":"9f2c…"}}}
```

For either message, keep detecting with the current model while downloading,
verifying the advertised hash, and loading the new model. Once loading succeeds,
switch the model and phrase together. Send exactly the active `phrase` in wake
claims. Report the result on the same socket:

```json
{"type":"wake_word_set","phrase":"Hey Sakura","status":"ok"}
{"type":"wake_word_set","phrase":"Hey Sakura","status":"error","error":"download_failed"}
```

`phrase` identifies the requested phrase. Errors are `model_unavailable`,
`download_failed`, `hash_mismatch`, or `load_failed`. On failure, keep the
previous phrase and model and show that the requested one is unavailable.
Answer a `set_wake_word` within 30 seconds; the server takes a later answer,
or none, as no switch.
Remember a successful switch across restarts. A `ready` without `wake_word`
keeps the active phrase; before any switch, that is `Hey Arya`. The `cancel`
wake word (section 10) stays the device's own.

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
do not follow the recommendation. Use `cancel`, or close the socket,
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
(default **0.75 seconds**). Claims for that phrase received while the election
is open compete together. The window is the longest the election stays open;
the server decides sooner when it expects no further claim. A claim after the
election closes opens a new election; different phrases elect independently. This groups reports by server arrival
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
| No matching decision within 1000 ms | Assume loss, discard the activation buffer, and resume local wake detection. No frame is needed. |
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
idle-originated turn, the server requests `next_action: "standby"`, or
`listen` when it wants an answer; do not open a user activation merely because
the server spoke. For a targeted outbound turn while idle, temporarily enable
microphone streaming for interruption/control detection when `thinking` or
`speaking` arrives, subject to explicit user mute. This requires no local
wake or arbitration. After the turn and playback drain, stop that temporary
stream and restore local wake detection unless an activation is now open.

Serialize outbound audio on each socket: never interleave the untagged
binary PCM of two turns. Output waits behind an active turn and its playback
drain; stale output is discarded instead of played late. Ordinary barge-in,
`cancel`, and state reports apply equally to these turns. Output goes to the one
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
remaining and the label.

A `countdown` with a `countdown_id` the device already has and a different
deadline replaces it. This is how a countdown is extended or snoozed: if it was
ringing, stop the tone and count down to the new deadline. One with the same
deadline changes nothing: a ringing countdown keeps ringing.

A countdown can be cancelled, whether it is still counting or already ringing:

```json
{"type":"countdown_cancel","countdown_id":"countdown-9"}
```

### Ringing

At the deadline the device rings, whether or not it is connected. Every client
bundles four sounds: `alarm-1`, `alarm-2`, `alarm-3`, and `alarm-4`. Play the
one named in `sound`, repeating it.

A countdown is never rung late. A device starts ringing only within 5 seconds
of the deadline. If it could not, because it was off or suspended then, or
first learns of the countdown later, the countdown has ended without ringing.

The alarm keeps ringing until the first of:

- a local dismissal: a button or a tap on the device;
- `countdown_cancel` for it;
- a new `countdown` for it, which starts it counting again;
- `ring_seconds` of ringing.

The device tells the server about its ringing countdowns with the same
`countdown` frame type. Send it when the countdown starts ringing, and again
after each `ready` while it still rings:

```json
{"type":"countdown","countdown_id":"countdown-9","ringing":true}
```

When it ends, other than by a snooze, send it once more with `ended`:

```json
{"type":"countdown","countdown_id":"countdown-9","ended":true}
```

The frames carry no time. A countdown rings at its `deadline_unix_ms` and is
never rung late, so the server already knows when it started ringing. Ringing
does not change the device's voice state; a ringing countdown is shown
alongside it (`animations.md`).

### Reconnecting

The device keeps its countdowns and their time while disconnected; a dropped
connection changes nothing about them. It also remembers each countdown that
has ended until `ring_seconds` after its deadline.

After each `ready` the server sends every countdown it holds for the device
again, as ordinary `countdown` frames, including ones whose deadline has
passed. It cannot tell a device that kept running from one that restarted; the
device decides from what it has:

| The device receives `countdown` for | It |
|---|---|
| One it has, still counting, same deadline | Changes nothing. |
| One it has, ringing, same deadline | Keeps ringing. |
| One it does not have, deadline ahead or less than 5 seconds past | Counts down and rings as usual. This is a device that restarted before the deadline. |
| One it does not have, deadline more than 5 seconds past | Does not ring. Sends `ended`. This is a device that was off at the deadline. |
| One it remembers as ended: dismissed, or rung out, while disconnected | Does not ring. Sends `ended`. |

After each `ready` the device also sends, without waiting for those frames,
`ringing` for each countdown ringing now and `ended` for each that ended while
it was disconnected. A countdown the server does not send again is not
cancelled: keep it.

### Several at once

Two countdowns can ring at the same time, and the person cannot tell by ear.
The device rings them as one: a single sound, never two mixed, the `sound` of
the countdown that started ringing first among those still ringing. When that
one ends and another still rings, play the other's sound. A screen lists each
ringing countdown with its label and how long it has rung.

A local dismissal ends every ringing countdown, since the person cannot tell
which one they are stopping. `countdown_cancel`, a snooze, and `ring_seconds`
end one; the sound continues while another rings. Each countdown that ends gets
its own `countdown` frame with `ended`.

### The `cancel` wake word

A device that can detect wake words listens for a second one, `cancel`, next to
the assistant's. It is detected on the device and needs no election:

| Heard | The device |
|---|---|
| `cancel` during a turn or while an activation is open | Sends `heard` (section 4). The server also hears it as speech. |
| `cancel` at any other time | Does nothing. |

The `cancel` wake word never ends a countdown, ringing or not. An alarm ends
with the device's button or tap, or by asking the assistant.

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
