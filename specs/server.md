# Server spec

Server responsibilities behind the [client contract](client.md).

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
baseline. A brief click, room noise, or soft incidental speech must not repeatedly
cancel a reply. Model failure falls back to the energy detector without ending the
socket. Onset acceptance is separate from the decision to capture its words.

Once capture owns input, old completion, cancellation, `drained`, and tail timeout
must not clear its audio, pre-roll, or pending transcript. An old sign-off must not
put a client into standby over an accepted continuation. Fence discarded or
superseded output: it must not send more PCM or create another agent run. Settle
old work before admitting the next run; never execute both concurrently.

### Cancellation controls

Cancellation is distinct from conversational continuation. Accept an explicit
`cancel` for the matching turn immediately; ignore a stale targeted cancellation.
An untargeted cancel also abandons a capture in progress. Stop the agent and TTS,
discard that cancelled capture, emit the existing interruption/terminal frames,
and leave the socket and conversation usable. `stop` closes the attachment.

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

`thinking` progress is measured, never estimated. `tool_calls` counts the run's
`tool.started` events. `reasoning_tokens` comes from the Runs API's `usage.updated` run event, which
Hermes emits after each model call with the run's cumulative provider-reported
count (`null` when the provider reports none):

```json
{"event": "usage.updated", "run_id": "run_abc", "reasoning_tokens": 2400}
```

Take the latest value rather than summing, so an SSE replay or late report cannot
double count. Do not derive tokens from elapsed time or `reasoning.available`
previews, and never forward reasoning text. Counters reset at each run's start.
Send opt-in user and assistant `transcript` frames as defined in
[the client spec](client.md), for both voice replies and outbound output. Send
reply text before `turn_done` without delaying PCM. Distinguish generated text,
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

On disconnect, stop capture and generation, settle the attachment, and fence late
results before replacement admission. Reconnect uses the same named conversation;
never replay an unacknowledged request. Preserve Hermes's existing tool receipts,
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

Authenticate using the existing API key before opening the conversation or
accepting timer requests. Do not add a separate device/attachment credential.
The device name identifies a destination, not a separate security principal;
clients sharing a server API key share its access boundary.
The device's conversation title is `<assistant_name>: <device_name>`.
There is no client conversation override, selection protocol, or separate
conversation extension. Reject a supplied `start.conversation` as an invalid
field rather than silently accepting it.

Use Hermes's existing session APIs for title lookup, creation, history,
and compression continuation. Reuse an authorized compatible named session or
create it when confirmed absent; on concurrent creation conflict, re-read the
winner. When the saved session returns an authenticated `404`, look up the
exact conversation title again. Reuse a compatible existing conversation or,
when the title lookup confirms none exists, create a fresh session with a new
ID and update the saved reference. Preserve the device's routing identity.
Use a new session ID because deleted IDs may be retired by Hermes. A lost create
response must recover the already-created conversation through title lookup.
The same recovery applies while connected: when Hermes refuses a run with
`not_found` because the session was deleted, resolve the device's conversation
again as on connect, move the live attachment to the result, and retry that
turn once there. Retry only if resolution produced a different session.
Authentication failures, timeouts, malformed responses, ambiguous titles, and
server errors must not create or replace history. Save the session reference
for reconnect; do not build a
parallel conversation store or expose Hermes session mechanics to clients.

Use the authenticated socket for named-device and timer behavior. A
replacement fences the old connection and stops/settles its in-flight work
before new admission. Report busy while settlement is pending. The Runs client must verify a terminal
status for previously accepted runs before admitting a continuation. If admission
is uncertain or settlement cannot be confirmed within its timeout, report an error
and admit no competing run. Supply trusted
source-device context to the agent/tools; transcript text cannot impersonate it.
Fencing does not prove that a disconnected speaker is silent.

There is no inactivity expiry. Use Hermes's existing compression settings to
keep voice context short and compact it frequently: `compression.threshold`
sets the percentage trigger and `compression.threshold_tokens` caps that trigger
by token count. Configure these server-side; do not add client fields or another
compactor. A compaction trigger is not a hard limit on retained context.
Hermes owns compression and retention. Verify
required Hermes API support, preserve history through continuation, and recover
local sign-off bookkeeping where needed. Never replay an unacknowledged request.

Verify first setup, concurrent creation, reconnect/restart, failed
lookup, unauthorized access, compression continuation, and replacement
attachments without duplicate history or competing work. Include the case
`assistant_name: Arya`, `device_name: Den Echo`, title `Arya: Den Echo`.

The implementation stores only routing metadata in the profile's
`plugin-data/hermes-converse/devices.db`. It uses authenticated exact-title lookup and the
public messages resource to resolve compression continuations, and refuses unsupported API
responses. A confirmed missing session recovers through exact-title lookup;
other lookup failures leave the existing reference unchanged.
The prompt supplies both names and the routing identity, explains proactive output without
another wake word, and requires successful scheduling receipts before confirming timers.
Heartbeat 2's current API-session restriction still requires an integration change; a prompt
does not bypass a scheduling tool's refusal.
