# Server spec

Server responsibilities behind the [client contract](client.md).
What the server records is defined in [server tracing](server_tracing.md).

## Voice lifecycle

The server receives microphone PCM continuously throughout a granted activation,
including capture, transcription, thinking, speech output, and speaker drain.
An open socket alone is not an activation. Keep incoming audio processing
independent of agent generation and outgoing TTS.

Separate three decisions: VAD detects sustained user speech; endpointing decides
when an utterance is complete; turn control cancels obsolete output and admits
completed user utterances. A wake word opens an activation, not an utterance
boundary. A pause after the wake word must not require another wake word.

### Input and reply ownership

Input capture and reply output have independent lifetimes. At most one agent run
owns the conversation at a time. Each admitted request owns a turn ID, cancellation
signal, TTS pipeline, and terminal result. Short transitions between the input
worker and turn driver are serialized; capture, STT, agent calls, and synthesis do
not hold the shared state lock.

| Reply phase | Accepted microphone speech |
|---|---|
| No reply | Preserve pre-roll, capture through endpoint, transcribe, admit a request. |
| Request queued or being transcribed | Preserve further speech; supersede an unstarted reply without losing the original request. |
| Thinking | Cancel obsolete generation immediately; capture and transcribe the continuation. |
| Speaking | Apply playback echo rejection before accepting onset; stop old output and capture genuine user speech. |
| Sent, awaiting speaker drain | Keep playback echo rejection; interrupt queued playback and capture genuine user speech. |

### Barge-in means continuation

Barge-in gives the user the floor. It does not mean “cancel and discard what the
user says.” Capture onset pre-roll and the rest of the utterance exactly once,
including the interval while the old agent run settles. Endpoint and transcribe
that capture normally. Admit it only after the old run has settled, in the same
Hermes conversation with the original request, prior context, and available tool
results. For example, “turn on the light” followed during thinking by “and make
it warm white” must retain both instructions. If an earlier request never reached
Hermes, carry its text into the next admitted request in order. Do not replay
completed tool actions; cancelling generation cannot undo their side effects.

Use Silero speech probability with the existing sustained active-turn energy
check. While output or its playback tail is active, also apply the playback echo
baseline. During an active turn, count speech within a short window so pauses
between words do not restart it, and keep the user's own speech out of the echo
baseline. A brief click, room noise, or soft incidental speech must not repeatedly
cancel a reply. Model failure falls back to the energy detector without ending the
socket. Onset acceptance is separate from the decision to capture its words.

Once capture owns input, old completion, cancellation, `drained`, and tail timeout
must not clear its audio, pre-roll, or pending transcript. An old `standby` call must not
put a client into standby over an accepted continuation. Fence discarded or
superseded output: it must not send more PCM or create another agent run. Settle
old work before admitting the next run; never execute both concurrently.

### Cancellation controls

Cancellation is distinct from conversational continuation. Accept an explicit
`cancel` for the matching turn immediately; ignore a stale targeted cancellation.
An untargeted cancel also abandons a capture in progress. Stop the agent and TTS,
discard that cancelled capture, emit the existing interruption/terminal frames,
and leave the socket and conversation usable. `stop` closes the attachment.

### Approvals

Approvals are Hermes's, not a tool of this plugin. When the agent is about to
run a command that needs permission, Hermes stops the turn and hands the
platform an approval prompt: the command, a description, and the choices
allowed. Every platform shows that prompt in its own way; a messaging platform
shows buttons. The platform answers with one of Hermes's choices, and Hermes
resumes or refuses. Hermes owns the request, the choices, the timeout
(`approvals.timeout`), and what happens when nobody answers.

For a voice device the prompt is spoken. `approval_choice` says how it is
answered:

| `approval_choice` | Outcome |
|---|---|
| `deny` (default) | Answered `deny` at once. |
| `once` | Answered `once` at once. |
| `ask` | The person is asked, as below. |

With `ask`, the person is asked in two ways at once, and the first answer wins:

- **Spoken.** A summary is spoken on the device attached to that conversation.
- **On screen.** `approval_request` is sent with the summary, Hermes's redacted
  command, and the choices Hermes allows for this request, for a device that
  can show a box.

The turn keeps waiting with `thinking` frames.

#### The summary

Hermes's prompt is a command and a description, which is not something to read
aloud. Make the summary with a separate model call, outside the conversation:
it adds nothing to the history and does not run as a turn. Give it the command
and the description and ask for a yes-or-no question of twenty words or fewer
saying what will happen. If that call fails, use Hermes's description.

#### The answer

Whatever the person says next goes to a second separate model call, with the
summary and the choices Hermes allows, which returns one of those choices or
unclear.

| The person's reply means | Answer to Hermes | `outcome` |
|---|---|---|
| Yes, with nothing more | `once` | `approved` |
| Yes for the rest of this conversation, such as "yes, for now" | `session`, or `once` if `session` is not allowed | `approved` |
| Yes from now on, such as "always allow that" | `always`, or `once` if `always` is not allowed | `approved` |
| No | `deny` | `denied` |
| Unclear, or the call fails | None. Ask once more; a second unclear reply answers `deny`. | `denied` |

A plain yes is never widened: `session` and `always` need the person to say so.

| Otherwise | Answer to Hermes | `outcome` |
|---|---|---|
| `approval_response` from that device for this `approval_id`, with an allowed choice | Its `choice` | `approved` or `denied` |
| Hermes's timeout passes | Hermes ends the request. | `expired` |
| The turn is cancelled | `deny` | `cancelled` |
| The prompt allows only `deny` | `deny` at once; do not ask. | none sent |

A pending approval is held when nobody can answer it. If the device disconnects,
or none is attached when the prompt arrives, do not answer Hermes: the request
stays pending until Hermes's timeout. When a device attaches to the conversation
while it is still pending, ask again in both ways: send `approval_request` and
speak the summary. So a person who put the app away comes back to the question,
not to a refusal.

Send `approval_resolved` when the request is settled. A response for another
request, a second response, one from another device, or one with a choice that
was not offered changes nothing.

While a request is pending, speech from the device is an answer to it, not a new
request and not an interruption of the waiting turn. The spoken summary itself
must not be taken as the answer. Do not speak secrets. When the action did not
run, the agent's reply says so.

Verify each allowed choice by voice and by screen; a plain yes answering `once`;
a choice that was not offered; an unclear reply once and
twice, a failed summary call, Hermes's timeout, a stale or repeated response,
cancellation, a disconnect and reconnect while pending, a prompt that arrives with
no device attached, a deny-only prompt, and the summary's own echo. Verify
the action runs once after approval and never otherwise, and that neither model
call appears in the conversation's history.

### Endpointing, completion, and recovery

Keep endpointing separate from onset detection. Received silence of
`endpoint_silence_ms` (default 700) is a candidate pause. Without Smart Turn it
ends the capture. With Smart Turn, a complete-sounding turn ends it; an incomplete
one keeps listening and is checked again after each further window, until
`endpoint_max_silence_ms` (default 3000) of continuous silence ends it. Speech
resets the pause. Audio that arrived while Smart Turn ran is checked before
committing; if it contains speech, keep listening.

After a granted `wake`, the user often pauses to check the device is listening.
In that activation's first capture, until 1 s of speech has followed onset, a
pause ends the capture only at `endpoint_max_silence_ms`. This applies with or
without Smart Turn, so "Hey Arya … set a timer" is one request while a continuous
"Hey Arya, set a timer" endpoints normally.

Explicit `commit` ends the capture at once with the audio already received,
bypassing Smart Turn and the wake pause. The utterance limit still applies.
Capture traces record the endpoint mode, windows, wake flag, end reason, final
pause, and longest pause spoken through. A failed/empty STT result starts no
run and releases capture ownership. Continue listening within an open activation.
Do not claim a VAD or endpoint model can eliminate every premature endpoint:
continued speech after endpoint must still be handled as continuation.

Emit `thinking`, optional `speaking` and PCM, then the turn's terminal
frames in order. Interrupted, failed, or superseded replies return `listen`.

Pace reply PCM against the playback it implies so the server, not the client,
holds the backlog. Track where the client's queued audio ends; before sending a
chunk, wait until that is at most 1.5 s ahead of now. If it is already in the
past (TTS was slower than real time), restart from now. Send the first chunks
without delay, stop waiting as soon as the turn is interrupted, and do not count
pacing waits toward the turn timeout. `drained` remains the end of playback.

`thinking` progress is measured, never estimated. `tool_calls` counts the turn's
tool calls as each starts. `reasoning_tokens` sums the provider-reported count of
each model call in the turn, and stays `null` until a provider reports one. Do not
derive tokens from elapsed time or reasoning previews, and never forward reasoning
text. Counters reset at each turn's start.
Send opt-in user and assistant `transcript` frames as defined in
[the client spec](client.md), for both voice replies and outbound output. Send
each piece of reply text when it is submitted for speech, before the PCM that
speaks it, and never hold PCM back for it. After the last piece, send a closing frame with empty
text and the message's `message_id`. Distinguish generated text,
PCM sent, and client playback drain. `turn_done`
means sending finished. `drained` means playback finished or was discarded; it
cannot prove the user heard the response. An interruption records that a reply
was cut short, not that a device acknowledged failed delivery. Agent-facing
context must not describe a generated or transmitted response as heard.

After `turn_done`, retain playback protection until matching `drained` or the
bounded fallback timeout. A tail interruption updates the same turn to `listen`;
it does not create another reply for that turn. Quiet advisories count received
idle silence, never wall time or time occupied by a reply/capture. Restart the
count on speech onset, turn start and end, playback drain, and when received
audio resumes after a gap of at least 1 s, so silence from an earlier listening
phase never shortens the next one. Working frames
are sent every 15 seconds and stop with their owning turn.

On disconnect, stop capture and settle the attachment. A turn already running
is not stopped: it finishes in the conversation like a turn on any other gateway
platform. Its reply cannot be spoken, since no device is attached, and is not
queued; it is in the conversation's history. Audio captured but not yet handed
to the agent is dropped. Reconnect uses the same named conversation; never
replay an unacknowledged request. Preserve Hermes's existing tool receipts,
history, and compaction behavior.

### Diagnostics and acceptance

Log accepted onsets with the effective detector, input/reply phase, session, and
turn ID. An energy candidate is not proof Silero accepted an interruption. Trace
capture, endpoint, STT, generation, TTS, and continuation ownership separately;
do not export microphone audio or transcript contents through telemetry.

Verify thinking, speaking, and drain continuations; onset pre-roll preservation;
completion racing a capture; cancellation during capture/STT; multiple queued
corrections; stale cancellation/drain; ordinary requests containing cancel words;
noise and soft-speech rejection; detector fallback; same-conversation context;
and no overlapping runs or stale PCM after cancellation.
Use deterministic PCM and a public speech fixture for automated regression tests.
Private server recordings may inform local diagnostics but must not be committed.

## Named Aryas

Identify a device by its `device_name` within the server/profile. Keep its
routing record, optional room/aliases, and Hermes session reference. Device
labels are unique within that scope. `assistant_name` is
the name used in the voice system prompt, often related to the wake word;
`device_name` identifies the particular device. Neither is a credential.

Authenticate using the existing API key before opening the conversation. Do not add a separate device/attachment credential.
The device name identifies a destination, not a separate security principal;
clients sharing a server API key share its access boundary.
The device's conversation title is `<assistant_name>: <device_name>`.
There is no client conversation override, selection protocol, or separate
conversation extension. Reject a supplied `start.conversation` as an invalid
field rather than silently accepting it.

The device's conversation is a gateway session, described under Gateway
conversation. The gateway creates it, keeps its history, and continues it through
compaction. Preserve the device's routing identity. Do not build a parallel
conversation store or expose Hermes session mechanics to clients.

Reject empty names and a combined title over 100 characters with
`4400 bad_start`. A replacement connection for the same names closes the old one
with `4409 attachment_replaced`. A turn in flight carries on, and its remaining
frames go to the new connection. Supply trusted source-device context to the agent/tools; transcript
text cannot impersonate it. Fencing does not prove that a disconnected speaker is
silent.

There is no inactivity expiry. Use Hermes's existing compression settings to
keep voice context short and compact it frequently: `compression.threshold`
sets the percentage trigger and `compression.threshold_tokens` caps that trigger
by token count. Configure these server-side; do not add client fields or another
compactor. A compaction trigger is not a hard limit on retained context.
Hermes owns compression and retention. Verify
required Hermes API support, preserve history through continuation, and recover
local `standby` bookkeeping where needed. Never replay an unacknowledged request.

Verify first setup, concurrent creation, reconnect/restart, failed
lookup, unauthorized access, compression continuation, and replacement
attachments without duplicate history or competing work. Include the case
`assistant_name: Arya`, `device_name: Den Echo`, title `Arya: Den Echo`.

The implementation stores only device identity in the profile's
`plugin-data/hermes-converse/devices.db`.
The prompt supplies both names and the routing identity, explains proactive output without
another wake word, and requires successful tool receipts before confirming countdowns or reminders.
A prompt does not bypass a scheduling tool's refusal.

## Gateway conversation

Every voice conversation is a gateway session on the `converse` platform, the
same kind of session a Telegram or Slack chat has. The plugin registers the platform;
it needs no credentials and is enabled with the plugin. The WebSocket route, its URL,
and API-key authentication stay on the API server.

| Voice event | Gateway interface |
|---|---|
| Endpointed utterance | `handle_message` with a text `MessageEvent`; the voice prompt is its `channel_prompt` |
| Reply text as it is generated | `on_stream_delta` plugin hook |
| Final reply | `send` marked `notify`; speaks only what the stream did not carry |
| Turn settled | `on_processing_complete` |
| Barge-in, `cancel`, `stop` | `interrupt_session_activity`, then wait for the turn to settle |
| Tool and reasoning progress | `pre_tool_call` and `post_api_request` plugin hooks |
| `command` from the client | the gateway command named in `name`, with `args`, after stopping any turn in flight; its reply is not spoken and goes back to the client in a `command` frame |
| Model selection | the gateway `/model` command, sent only when the choice differs from the session's; its confirmation is not spoken |

For a named device the chat is the device's conversation: chat name
`<assistant_name>: <device_name>`, user `recipient_id`, user name `device_name`. A
socket that sends no names gets a chat whose id is its `conversation_id`; sending
that id in a later `start` reattaches to it, and an unknown id is refused. Hermes owns the session, its history,
compaction, and its place in the web UI. Every event comes from a socket that
presented the API key, so the platform is registered as trusted inbound and needs no
user allowlist or pairing.

A message another producer submits to the session, such as a Cadence wake or a
background process completion, runs as an ordinary turn. Its final reply reaches
`send` and is spoken on the attached device without a wake word. Outcomes:

| Situation | Outcome |
|---|---|
| Device attached and idle | Spoken once. |
| Device capturing or replying | Spoken after the current exchange. |
| No device attached, or the reply expired after 60 s | `send` reports failure; nothing is queued for a later connection. |
| `speak_this` with `""`, or an empty or `[SILENT]` reply | No speech. |
| Voice turn interrupted | Its late reply is discarded. |
| Stream delta arriving after the final | Discarded, so nothing is spoken twice. |

Status text meant for a screen (busy acknowledgements, tool progress, error notices)
is never spoken; the platform's display defaults turn it off. Approval prompts are
answered as described under Approvals, and clarify prompts are cancelled.

The gateway serializes the session. A stopped turn can outlive its stop, for
example inside a long tool call; the next request is admitted behind it, and the
stopped turn's late reply is discarded. A device is never refused because an
earlier turn has not settled.

At startup a socket waits up to 30 s for the platform to connect. When the
platform is not hosted, the connection is refused with `conversation_unavailable`.

## Commands from a device

A `command` frame names any gateway command, without its slash, and may carry
`args`. Pass `/<name> <args>` to the gateway for the device's conversation as a
chat user's command would be, and return its
reply in a `command` frame without speaking it. There is no list of allowed
commands: a device holding the API key can run what a chat user can, including
commands that change approvals.

After a command that starts a new conversation (`new`, or its alias `reset`), send the answer and
then close the socket with `4410 conversation_changed`. Do not move the live
socket to the new conversation: the device reconnects and attaches to it in the
ordinary way. The conversation keeps the title
`<assistant_name>: <device_name>`.

## Catching up

Give every user `transcript` frame, and the closing assistant frame of each
turn, a `message_id`: Hermes's message id for that message, as an opaque string.
Hermes stores a message only once it is whole, which is why the ID follows the
text instead of travelling with it: the user frame is sent after the agent turn
starts, and the assistant's ID comes in a closing frame with empty text.

When a client connects with `transcripts: true`, replay what it missed from
Hermes's own messages for the device's conversation, right after `ready` and
before any live frame. Keep no second copy.

- Replay user and assistant messages only, as `transcript` frames with
  `replay: true`: no tool calls, tool results, reasoning, or system text.
- An assistant message's `text` is what was submitted for speech in that turn,
  the same text its live `transcript` frame carried. For a turn that used
  `speak_this`, that is the text given to the tool, not the reply text. A turn
  that said nothing is not replayed.
- With `after`, replay every such message after it, up to the most recent 100.
  Without `after`, or when `after` is not a message of the current
  conversation, replay the most recent 20.
- When `start` carried `after`, say in `ready` whether it was found:
  `after_found` true or false. False is how a client learns its conversation
  was replaced, whatever replaced it: a `command` frame, the Hermes web UI, or a
  deleted conversation.
- Include messages that compaction has folded away when Hermes still has them.

### Nobody attached

A turn can run with no device attached, because a turn outlives a dropped
connection and other producers start turns too. Its output goes nowhere, and
that is expected:

- Do not synthesize speech for it. The reply is in the conversation's history
  and is replayed as a transcript when a client catches up.
- `speak_this`, `play_tone`, `standby`, and `countdown` return `not_attached`,
  so the agent can tell nobody is there and tell another room or chat instead.
- Nothing is queued to be spoken later.
- A pending approval is held, as described under Approvals.

Verify: a reply that finished while the device was away is replayed as a
transcript and never spoken; no speech is synthesized for it; connecting twice
with the same `after` replays the same frames; connecting with the newest `message_id`
replays none; a cancelled turn replays a user transcript with no reply; a
reconnect during a turn receives the replay and then the rest of the turn live;
a replayed frame never starts a turn on the client.

## Voice tools

The agent has three tools for controlling the device it is talking through.
They act only on the device whose conversation calls them.

A tool call always happens inside a turn, and the tools act on that turn however
it started: by speech, or by the server for a reminder, a still-ringing alarm,
or another message submitted to the conversation. There is no case of a device
being connected with no turn for a tool to act on.

| Tool | Arguments | Effect |
|---|---|---|
| `speak_this` | `text` | The device says exactly `text`, and nothing else the turn produces is spoken. |
| `play_tone` | `tone`: `confirm` or `error` | The device plays that tone (`tones.md`). |
| `standby` | None | The device goes to standby when this turn's playback has drained: `turn_done` carries `next_action: "standby"`. It stops sending audio and waits for the wake word or Talk. |

### `speak_this`

Without `speak_this`, a turn's reply text is what the device says. The first
`speak_this` call turns the rest of the turn inside out: from that point on,
only text given to the tool is spoken. Reply text written before the call is
spoken as usual; reply text written after it is not.

| In one turn | The device says |
|---|---|
| No `speak_this` call | The turn's reply text. |
| `speak_this` with text | Any reply text written before the call, then that text exactly. Nothing written after the call. |
| `speak_this` with `""` | Any reply text written before the call, then nothing. Called first, the turn is silent. |
| Several `speak_this` calls | Each text in order. An empty one adds nothing. |

The next turn starts with reply text spoken again.

In a turn nobody spoke to start, the reply would be spoken on the attached
device without a wake word; `speak_this` replaces it in the same way, and
`speak_this` with `""` keeps that turn silent. `standby` in such a turn leaves
the device in standby. A turn
that only calls `play_tone` still speaks its reply text; call `speak_this` with
`""` as well to confirm with a tone and no words.

### `play_tone`

Send `{"type":"tone","tone":"confirm"}` to the device. A tone plays before any
speech from the same turn. It is not a reply: it has no `turn_id`, and the
device sends no `drained` for it.

### `standby`

The agent calls `standby` when the exchange is over, for example after
"thanks, that's all". The tool, the `next_action` value, and the device state
share the one name. Standby is not mute: the device still hears its wake word.
The server does not look for a sign-off phrase in the reply. A turn that does
not call `standby` ends with `next_action: "listen"`.

| Tool result | Meaning |
|---|---|
| `ok` | Accepted. |
| `not_a_device` | The call was made from a chat that is not a voice device. Nothing happens. |
| `not_attached` | No device is connected to this conversation. Nothing is sent or queued. It has no other meaning. |

## Countdowns

A countdown is a timer that ends with a tone on a device. The device keeps the
time and rings; the server records its countdowns and relays them. Spoken
reminders are ordinary turns from a scheduler plugin and are not countdowns.

A countdown belongs to one device's conversation. The server provides one tool,
which acts only on the device whose conversation calls it:

| `countdown` action | Arguments | Effect |
|---|---|---|
| `set` | The future time as `at` (ISO 8601) or `in_seconds`; `sound` (`alarm-1` to `alarm-4`, default `alarm-1`); `label` (optional) | Sends `countdown` to the device. Returns `countdown_id` and the deadline. |
| `snooze` | `countdown_id`; the new future time as `at` or `in_seconds` | Sends `countdown` again with the same `countdown_id` and the new deadline. Extends a pending countdown; stops a ringing one and starts it counting again. |
| `cancel` | `countdown_id` | Sends `countdown_cancel` to the device, whether the countdown is pending or ringing. |
| `list` | None | Returns the device's countdowns with their label, deadline, and state: `pending` or `ringing`. |

The device comes from the session, so transcript text cannot set it. There is
no device argument. Another room does not operate this device's countdowns; it
asks this device's conversation, for example "stop your alarm" or "set a
five-minute timer", through a session-to-session plugin. The server provides
nothing for that.

| Situation | Result | Outcome |
|---|---|---|
| Device attached | `set` | `countdown` is sent at once. |
| Device not attached | `not_attached` | Nothing is recorded. The caller decides what to do instead. |
| Called from a chat that is not a voice device | `not_a_device` | Nothing is sent. |
| Time not in the future | `bad_time` | Nothing is sent. |
| No such countdown on this device | `unknown_countdown` | Nothing is sent. |

`countdown_ring_timeout_seconds` (default 900) is how long an alarm rings if
nobody ends it. Send it in each `countdown` frame as `ring_seconds`; the device
enforces it.

Keep a countdown until its device sends `drained` for it, or until its deadline
plus the ring timeout has passed. Send a device its countdowns again after
`ready`, so one that reconnects or restarts shows them. The server does not ring
anything itself and does not send a frame at the deadline.

### Still ringing

While a countdown rings and the device has not sent `drained`, start a turn in
that device's conversation at 1, 2, 4, 8 minutes after the deadline, doubling,
until the alarm ends. With the default timeout that is four turns. Each says
which countdown is ringing and for how long, so the agent can do more each time:
say something in the room, tell another room, or tell the owner elsewhere.

The turn is an ordinary message submitted to the gateway session. Its reply is
spoken on the device like any other, or not at all when the agent calls
`speak_this` with `""`. No turn starts after `drained`. A snooze starts the sequence again
from the new deadline.

### Stopping by voice

A ringing alarm is ended on the device, by its `cancel` wake word (client spec,
section 10). The server does not listen for a stop phrase and sends nothing to
end an alarm by voice; it learns the alarm ended from `drained`.

The assistant's wake word followed by a request is an ordinary turn. The alarm
keeps its place: the agent ends it with `cancel` or `snooze`, or leaves it
ringing.

Acceptance scenarios: a countdown set in the Kitchen counts down and rings only in
the Kitchen. Exercise a call from a chat that is not a device, a device that is
not attached, a reconnect before the deadline, a device that is off at the
deadline, local dismissal, the `cancel` wake word, other speech while ringing, a
reply and a wake word while ringing, snooze while pending and while ringing,
cancel while ringing, still-ringing turns at 1, 2, 4 and 8 minutes and none
after `drained`, and the ring timeout from the setting.
