# Client spec — how to be a client to hermes-converse

`hermes-converse` exposes one WebSocket route:

```
GET /v1/audio/converse
```

Currently, one socket = one conversation. You stream microphone PCM up, the server does
voice activity detection, endpointing, speech-to-text, the agent turn, and
streams the reply back as PCM audio — all on the same socket. The socket,
session, and conversation history stay alive across turns unless you close
them.

Wake selection is cooperative: it recommends an answering device without
restricting audio processing. Section 8 defines the cooperative wake contract.
Sections 9–10 define the proposed conversation-output and timer/alarm
extensions. Section 11 proposes named Aryas attached to existing Hermes conversations. The current server does not implement these extensions yet.

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

For a wake-word client, detect the wake word locally, buffer microphone
PCM while awaiting arbitration for up to **1000 ms**, and start streaming
only on a matching grant. Send the buffered audio first, in capture order,
then live binary PCM16 mono at `input_rate` without a gap or duplication.
30 ms blocks are ideal (e.g. 480 samples at 16 kHz). Keep streaming throughout
the granted activation, including thinking and playback (sections 4 and 8).
A client that does not use wake detection may start streaming after `ready`.

The server replies with `ready`, which includes the socket's `session_id`
and clock-skew timestamps.

## 3. Client control frames (send anytime)

All are JSON text frames:

| frame | meaning |
|---|---|
| `{"stop": true}` | Stops live work and closes the socket. The current protocol cannot resume its conversation; this does not promise deletion of Hermes history. |
| `{"commit": true}` | Forces the current utterance to endpoint now (runs a turn on it). |
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
| `transcript` | what the server heard; carries a `turn_id` repeated on later frames of the turn |
| `thinking` | the agent turn is running (show "thinking", not "listening") |
| `working` | every 15 seconds during an active turn; refresh its inactivity timer without changing thinking/speaking state |
| `speaking` + binary frames | the reply as PCM16 at `output_rate` |
| `interrupted` | the turn was cancelled (you barged in or sent `cancel`); stop playing |
| `turn_done` | the reply is fully sent; `next_action` tells the client to listen or enter standby after drain |
| `quiet` | session mode: the stream has been silent for another `quiet_interval` seconds |
| `stop_word` | session mode: you said a stop phrase; no turn ran |
| `error` | the turn failed (spoken-safe text) |

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
| `transcript` / `thinking` after request endpoint | Keep sending live PCM for interruption/control detection only; do not create another ordinary transcript or queue a user turn. |
| `speaking` and queued speaker playback | Keep sending live PCM for server-side barge-in/control detection, not a queued ordinary transcript. |
| Turn ended and playback drained, activation still open | Keep streaming; server may capture the next user utterance normally. |
| Activation ends / returns to wake detection | Stop upstream PCM and resume local wake detection; keep the socket available for outbound conversation events. |
| Explicit user mute or closed session | Stop sending PCM; server-side barge-in is unavailable. |

After VAD endpoints the request, the server must switch input from ordinary
transcription to **interruption/control detection** for the active turn,
including thinking, speaking, and the playback tail. Audio received in this
mode must not become a new `transcript`, enter conversation history, or
start or queue another agent run. Limited recognition of controls such as
"cancel" or "stop" is allowed to interrupt the current turn; it must not
submit those words as an ordinary user message. This differs from stop
phrases spoken while waiting for a normal follow-up utterance (section 5).

After cancellation/interruption and playback drain, resume normal utterance
capture only if the activation remains open. Do not replay the control-
detection audio into that capture. A fresh user utterance can then start a
new turn. Keep explicit `cancel` separate from `stop`, which closes the socket.

Do not use an `agentTurn`, thinking, or playback flag to suppress PCM during
an activation. Play incoming PCM and send microphone PCM concurrently.
Local echo cancellation may clean the signal, but must preserve user speech.
Do not replace microphone audio with synthetic silence during playback.
On unmute, resume live audio only if the activation is still open; never
replay audio accumulated while muted.

On `interrupted`, stop and discard queued reply audio for that turn. Send
`drained` after its audio has actually left the speaker or been discarded.
`turn_done` means the server finished sending, not that playback drained.
For `next_action = "standby"` or a local decision to end the activation, finish
playback/drain handling and then stop upstream audio and return to wake
detection; do not cut off barge-in monitoring during the playback tail.

**Implementation gap:** the current worker has playback-aware barge-in, but
still captures/transcribes detected speech and can queue it as a new turn.
Its active-turn flag currently suppresses quiet accounting, not ordinary
transcription. The interruption-only input mode above requires a server
change; continuous streaming alone does not implement that restriction.

### Handling `thinking`

`thinking` is the server's explicit signal that the agent turn has started.
For example:

```json
{"type": "thinking", "turn_id": "<turn_id>"}
```

Treat it as a turn boundary even if the client has not received a
`transcript` for that turn. Mark the agent turn active and show "thinking".
During an active activation, keep sending microphone PCM for interruption/
control detection; do not transcribe it as a new user request. The UI state
and microphone transport are independent. See section 9 for outbound turns
that start while the client is otherwise idle.

If the client uses a turn deadline, arm it on `thinking` when `transcript`
has not already armed it; do not restart an existing deadline. Record the
event in the turn trace if tracing is enabled. When both frames arrive,
`transcript` identifies what was heard and `thinking` confirms that the
agent has started; they belong to the same `turn_id`.

This is a lifecycle status notification, not streamed reasoning text.
Continue handling `working`, `speaking`, `interrupted`, `error`, and `turn_done` as
usual to play the reply or finish the turn.

### Handling `working`

During an active turn, the server sends a keepalive approximately every **15 seconds**,
starting 15 seconds after `thinking`. It continues through agent and TTS waits,
including speaking, until the turn completes or is interrupted/cancelled. For example:

```json
{"type":"working","turn_id":"turn-42"}
```

When tracing is active, the frame also carries the turn's `traceparent`, as on
`thinking` and `speaking`. There is no tool name or reasoning text in this frame.
Shorter turns normally send no `working` frames.

If `turn_id` matches the client's active turn, refresh that turn's **inactivity**
deadline from receipt time. Choose an inactivity allowance longer than 15 seconds
with room for network/scheduling delays; a 30-second allowance permits a normal
keepalive before expiry. Keep the existing thinking/speaking/playback state and
continue handling PCM and control frames normally. `working` does not begin a turn,
restart a completed/cancelled turn, or imply that model/tool output has advanced.
Ignore frames for inactive, stale, or unknown turn IDs.

An optional absolute client turn limit remains independent; do not extend that
limit on `working`. These frames never extend the server's
`turn_timeout_seconds` (default 120 seconds). The server stops sending them before
terminal `interrupted`, `error`, or `turn_done` frames and when the socket closes;
clients stop refreshing the inactivity timer when their turn ends. Clients that
ignore unknown frame types remain compatible, but need this handling to avoid
abandoning long turns prematurely. Network delays can still deliver an old frame
after local cancellation, so clients must check the active turn ID.

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

## 5. Session mode (`quiet_interval > 0`)

For wake-word devices. The server reports quiet in **received** audio (never
wall-clock, so a held-open socket never accrues phantom quiet). A recognized
stop phrase produces `stop_word` and ends the activation without an agent
run. Otherwise `turn_done.next_action` tells the client whether to continue
listening or return to standby after playback drain. The mechanism used by
the server to decide a sign-off is internal; there is no client-side phrase
contract.

## 6. Typical flow

1. Open `ws://<host>:<port>/v1/audio/converse`, send `start`, and receive `ready`.
2. Detect the wake word locally; send `wake` and buffer PCM while awaiting
   the matching grant for up to 1000 ms. On denial/timeout, discard the buffer.
3. On grant, flush the buffered PCM and continue with live PCM. Server VAD
   endpoints the user request, then emits `transcript` → `thinking`.
4. Keep sending live PCM during thinking, `speaking` + reply PCM, and queued
   playback. The server uses it only for interruption/control detection.
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
exchange ends (for example, a stop phrase, `next_action = "standby"`, or the client
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
the server appends a unique socket identifier to keep candidates distinct. The assistant's `start.name`
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


## 9. Conversation output without user input (proposed extension)

Converse is an audio transport attached to a conversation. The conversation
can produce output without a new user utterance, microphone transcript,
wake detection, or wake grant. A reminder, scheduled follow-up, or server
event can start an outbound turn while the attached client is idle. Wake
selection concerns user activations; it must not authorize or gate outbound
conversation output. The client need not stream ambient audio while idle
in order to receive outbound events.

The server must subscribe to outbound conversation events, including turns
started by other producers, rather than only the events of a run started
from this socket's microphone input. Each delivered turn gets a `turn_id`.
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
Choose the intended attached recipient before delivery; do not broadcast
private conversation output to every connected device or run wake elections
for it. Define authenticated conversation attachment, recipient selection,
event deduplication, and expiry before enabling this extension. A socket
connection alone does not grant access to another conversation's output.

### Announcement sound and synchronized playback

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

**Current implementation and Heartbeat 2.** The server currently starts a
run for each microphone transcript and subscribes to that run's events.
It has no conversation-wide output subscription. Heartbeat 2 can generate
later turns, but its documented integration currently targets messaging-
gateway chats and rejects API-server sessions. Integration must connect
its conversation output to this transport; a system prompt cannot create
that missing delivery path. See [Heartbeat 2 compatibility](https://github.com/electricsheephq/heartbeat2#compatibility-and-known-issues).

Once the delivery integration exists, the voice instructions should tell
the agent to use the installed scheduler tools for later speech, retain
the originating conversation and recipient, and confirm scheduling or
cancellation only after the tool acknowledges it. Do not keep a voice run
open or sleep inside it to wait for a timer. Until that integration is
available, do not promise later spoken delivery merely because Heartbeat 2
is installed. A scheduled reminder's timing also follows the scheduler's
receipt and delivery policy; it is not automatically a precise alarm.

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


## 11. Named Aryas and Hermes conversation attachment (proposed extensions)

This section proposes `arya-v1` and `conversation-v1`; neither is implemented
by the current server. Section 10's timer delivery uses this same named-device
attachment. Legacy clients retain today's per-socket conversation behavior.

### Named Aryas and shared device attachment

Every provisioned Arya has a required human-readable name, usually its room:
`Kitchen`, `Bedroom`, or `Office`. Persist a stable `recipient_id`, household,
serving profile, name, optional room and aliases, permissions, and a revocable
device-bound attachment credential. The recipient identifies the physical
Arya across reconnects; it is not a conversation or the transient arbitration
candidate. Internal IDs are opaque strings; their wire format need not be a
GUID. Users identify Aryas by name.

Within one household/profile, require unique names after Unicode NFKC
normalization, whitespace collapse, and case folding. Names are plain text,
1–64 Unicode characters after normalization, without control characters.
Normalize aliases and room lookups the same way. An exact canonical name takes
precedence over an alias or room match; multiple alias/room matches require
clarification. Never silently pick the first match. Directory results expose
only destinations the caller is authorized to use. Setup and authenticated
administration own provisioning, permissions, and rename; a `start` frame
cannot register, rename, or claim an Arya by supplying a name or ID.

For example, "set a timer" targets the requesting Arya. "Set a timer on
Bedroom" or "in the bedroom" resolves an authorized named destination before
creating the timer (section 10). Changing a conversation never changes which
physical Arya a credential identifies.

Negotiate all extensions using the same `extensions` object. For example:

```json
{"type":"start","key":"<API_SERVER_KEY>","input_rate":16000,"output_rate":24000,
 "extensions":{"arya-v1":{"attachment_token":"<device-bound token>"},
 "conversation-v1":{"mode":"default"}}}
```

The server authenticates the normal API credential and device token, which
bind a principal, household, profile, and recipient. A shared API key or a
client-supplied name/device ID alone is not device identity. Provision tokens
out of band, redact them from logs, and use encrypted transport or a secured
tunnel. Token storage, rotation, and revocation are server administration.

```json
{"type":"ready","session_id":"socket-9","extensions":{
 "arya-v1":{"recipient_id":"arya-kitchen","name":"Kitchen","attachment_id":"attachment-9"},
 "conversation-v1":{"hermes_session_id":"api_1794142800_a1b2c3d4",
 "title":"Arya: Kitchen","status":"resumed"}}}
```

`session_id` keeps its existing socket/tracing meaning. `attachment_id` is
fresh per connection. One authenticated replacement attachment fences the old
one and closes it with `4409 attachment_replaced`; stale commands and receipts
are rejected. Atomically fence new admissions and stop/settle any old in-flight
run before admitting new user work on its Hermes session. If settlement is
pending, return `conversation_busy` instead of starting competing runs. A
partitioned old speaker may still sound until its bounded playback stops;
fencing is not proof of physical silence. Section 10 uses the same attachment,
not a second timer-specific token, recipient registry, or lease.

Unknown extensions are omitted from `ready`. `conversation-v1` requires
`arya-v1`; accept dependent extensions together or omit them. A client must
check acknowledgement before using new frames or claiming context was restored.
If unavailable, show that state and explicitly fall back to legacy behavior
or disconnect. Malformed extension fields close with `4402 bad_extension`;
invalid device credentials close with `4403 forbidden_attachment`. A non-start
first frame retains `4400 bad_start`, and ordinary API auth retains 4401.

### Reuse Hermes sessions and titles

Hermes already owns persisted session IDs, titles, history, title conflict
rules, and continuation resolution after compression. Use those resources:
[session API](https://github.com/NousResearch/hermes-agent/blob/main/gateway/platforms/api_server.py),
[title handling](https://github.com/NousResearch/hermes-agent/blob/main/hermes_state_titles.py),
and [Runs continuation resolution](https://github.com/NousResearch/hermes-agent/blob/main/gateway/platforms/api_server_runs.py).
The implementation must check the installed Hermes version for these features
and fail explicitly when required support is missing.

Each Arya defaults to a Hermes conversation titled **`Arya: <name>`**:

| Arya | Default Hermes title |
|---|---|
| Kitchen | Arya: Kitchen |
| Bedroom | Arya: Bedroom |
| Office | Arya: Office |

Persist only the recipient's default/current Hermes session references and
attachment metadata. Do not create another conversation-ID namespace, copy
session titles/history into an independent conversation database, or invent
separate title uniqueness rules. Use Hermes list/get/create/rename operations
and pass the selected Hermes session ID to `/v1/runs`. Keep title/display state
fresh from Hermes and follow its canonical continuation when sessions compress.

On first setup, resolve the exact default title in the authorized serving
profile. Adopt an existing compatible session only after Hermes access and
ownership checks; otherwise create it with that title. A title collision with
an inaccessible/incompatible session is an explicit setup conflict, not grounds
to adopt it, append a random suffix, or mint repeated orphan sessions. Resolve
concurrent creators through Hermes's title-conflict behavior and re-read the
winning authorized session. Persist its reference before reporting success.

After setup, resume by saved Hermes reference; names are for people and lookup,
not a replacement for stable binding. If the saved session was deleted or
becomes inaccessible, report `conversation_unavailable` and require explicit
selection or new-conversation recovery. Storage failure must not silently
create empty context. Follow Hermes ownership/admission rules for sessions used
by another surface; selecting a session does not authorize taking over its run.

There is **no Converse inactivity expiry** for the default or an explicitly
selected saved conversation. Reconnect, standby, sign-off, stop phrases,
`cancel`, barge-in, and socket closure retain the selection. Hermes owns context
compression and retention. Resume restores history, not interrupted audio or a
run; never automatically replay an unacknowledged utterance or promise exactly
once execution of its tools.

### Selection, rename, and trusted turn context

`conversation-v1.mode` supports the following start-time selection. Omitted
mode means `default`; reject unknown modes and incompatible fields.

| mode | fields and behavior |
|---|---|
| `default` | No session ID/title fields; select the Arya's saved default session, resolving/creating it only on first setup. |
| `resume` | Require `hermes_session_id`; select that existing authorized Hermes session, regardless of age. |
| `new` | Require nonempty `title` and unique `request_id`, forbid session ID; explicitly create/select a Hermes session with that title under Hermes validation/conflict rules. |

The selected session becomes current for that attachment. Persist it so a
reconnect can send `resume`; a `default` request deliberately returns to the
Arya's saved default. If `conversation-v1` is negotiated by an empty object,
select `default`. `ready` reports `created` or `resumed` and the actual Hermes
session ID/title. It does not change the Arya's identity or timer destination.

For selection on an open socket, pause microphone upload and send:

```json
{"type":"conversation_select","attachment_id":"attachment-9","request_id":"select-8",
 "mode":"resume","hermes_session_id":"api_1794142800_a1b2c3d4"}
```

Use the same mode/field rules as `start`. Accept only when no capture, queued
utterance, run, or playback drain is active. Otherwise return a correlated
`error` with `error: "conversation_busy"` and leave selection unchanged.
On success acknowledge before new audio or runs:

```json
{"type":"conversation","request_id":"select-8","hermes_session_id":"api_1794142800_a1b2c3d4",
 "title":"Arya: Kitchen","reason":"selected"}
```

Deduplicate selection/new requests durably by authenticated recipient +
`request_id` for at least 7 days; changed payload returns `request_conflict`.
A retry of `new` must not create another session after a crash between Hermes
creation and local acknowledgement: persist the intended Hermes create ID and
operation before creation, then recover by that same ID. Use Hermes's normal
ID generation/validation convention rather than a second conversation ID.
A cached acknowledgement describes the historical operation; after a later
selection, return `selection_superseded` rather than applying it again.
Retain only current/default references plus bounded operation receipts; expire
receipts after the advertised 7-day retry window. Never retry expired operations
blindly. Clients resync current selection by reconnecting before new intent.

An explicit "start fresh" action creates/selects a newly titled Hermes session
through `new`; it does not clear the old history. Offer Hermes's valid next
lineage title, such as `Arya: Kitchen #2`, for user acceptance. On success make
that session the Arya's saved default as well, so reconnecting with `default`
continues the fresh history. `resume` of another saved session changes current
selection only. Model-generated text alone cannot reset or select context.

Renaming an Arya preserves its recipient ID, default Hermes history, and all
timers. Rename its default Hermes title through Hermes to `Arya: <new name>`;
for a fresh lineage retain Hermes's suffix (e.g. `Arya: Downstairs Kitchen #2`).
Do not rename another explicitly selected task conversation. Preflight name
and Hermes-title conflicts. Make the administrative rename a durable,
recoverable operation; announce success only after directory and Hermes agree.
Until recovery settles a partial failure, block affected new name resolutions
and report rename pending, never claim a completed rename with divergent names.

Provide trusted server turn context containing source recipient ID, current
Arya name, and selected Hermes session ID to the agent/tool runtime. For example,
"This request came from Arya: Kitchen." Resolve it from the authenticated
attachment each turn, not transcript text or model-supplied identity. Keep it
request-scoped when conversations are shared. All negotiated user-turn frames
with `turn_id` also carry `hermes_session_id`; pin that reference through turn
completion/drain. Changing selection or receiving old output never rebinds an
in-flight turn. Restore local sign-off bookkeeping from appropriate Hermes
history or replace that dependency; an empty plugin reply list is not evidence
of an empty conversation.

### Timer and outbound boundaries; acceptance criteria

A timer records the originating Hermes session, requesting Arya, and target
Arya separately. "Kitchen, set a timer on Bedroom" stays in `Arya: Kitchen`
while Bedroom rings. Timers survive session selection, fresh history, rename,
and disconnect. Delivery uses the stored target recipient, never whichever
session/name is current at expiry. Outbound events retain their origin without
switching the receiving device's selected conversation. General conversation
subscriptions still require the explicit authorization described in section 9.

Before implementation, verify these scenarios:

- Kitchen/Bedroom/Office each have a required unique name and default Hermes
  title using a colon. Duplicate names, ambiguous rooms, and unauthorized
  destinations fail or clarify without creating sessions/timers.
- First setup reuses a compatible named Hermes session; concurrent setup,
  reconnect, restart, and compression do not fork or lose history.
- Rename preserves identity, history and timer routing; conflicts and partial
  failures recover without presenting inconsistent successful state.
- Idle time never resets context; explicit selection/fresh history uses Hermes
  resources, respects title/ownership rules, and is safe under retries/crashes.
- Replacement attachments fence old admissions/receipts; old runs settle before
  new admission, and active capture/playback cannot switch session midway.
- Trusted source context stays correct with another selected conversation;
  a remote timer rings on its named target and preserves its source session.
- Unsupported extensions and Hermes versions fail explicitly; legacy behavior
  remains unchanged. No feature in this section ships merely by editing this spec.
