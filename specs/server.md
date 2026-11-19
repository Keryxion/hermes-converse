# Server spec

Server responsibilities behind the [client contract](client.md).
What the server records is defined in [server tracing](server_tracing.md).

## Voice session instructions

Before sending `ready`, read `CONVERSE.md` from the active Hermes profile. When missing, create
it from the plugin's bundled Markdown. When its SHA-256 is in `defaults/CONVERSE.md.sha256`, the
list of every bundled default released, and it differs from the bundled Markdown, replace it with
the bundled Markdown: it was never edited. An empty file supplies no prose. On a read, creation, or
UTF-8 decoding error, log a warning and use the bundled guidance for this connection.

For each new spoken turn, the Converse channel prompt is the assistant-name preamble, the
connection's copy of the prose, the device identity and its routing guidance, and that turn's
gateway command catalog, in that order. The prose never replaces the generated parts. A reattached turn keeps the prompt it started with.

## Audio transport

Apply `start.audio_format` to both directions as defined in the client spec.
For Opus, use [libopus](https://opus-codec.org/docs/html_api/group__encoderctls.html)
with `OPUS_APPLICATION_VOIP`, 24000 bit/s, 20 ms mono
frames, `OPUS_SET_DTX(0)`, and `OPUS_SET_COMPLEXITY(5)`. Keep microphone codec
state per socket and reset reply codec state for each turn, separate from the
conversation. Decode input to PCM16 before the
existing level, VAD, endpointing and STT pipeline; encode reply PCM after TTS
resampling to `output_rate`.

Opus needs the system libopus library on the server (`libopus0` on Debian and
Ubuntu); install it with the plugin. PCM16 needs nothing. When `start` asks for
a valid format the server cannot set up, such as Opus without libopus, send
`audio_unavailable` and close with `4503`, and log the cause. That is separate
from a malformed `start` (`4400 bad_start`) and from a conversation that cannot
be opened (`4404 conversation_unavailable`); never fall back to another format.

Count decoded samples for input timing and reply pacing; compressed byte
lengths do not measure audio duration. Should a client send DTX anyway, decode
its tiny DTX-only packets as digital silence so comfort noise does not teach the
input gain. Flush remaining speech and encoder delay
before `turn_done`; cancellation discards unsent audio. A replacement socket
gets fresh codecs in its selected format, including for the rest of a live turn.

Verify PCM defaults, Opus opt-in and rates, packet boundaries, a final partial
frame, silence endpointing and `quiet`, wake pre-roll, concurrent playback
and barge-in, cancellation followed by another reply, and reconnect during a
turn with a different format. Repeat the audio acceptance scenarios with Opus.

## Voice lifecycle

The server receives microphone PCM continuously throughout a granted activation,
including capture, transcription, thinking, speech output, and speaker drain.
An open socket alone is not an activation. Keep incoming audio processing
independent of agent generation and outgoing TTS.

Separate three decisions: VAD detects sustained user speech; endpointing decides
when an utterance is complete; turn control cancels obsolete output and admits
completed user utterances. How the input's level reaches each of them is in
[audio.md](audio.md). A wake word opens an activation, not an utterance
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
| Thinking | Stop the reply's output and leave its agent run going; capture and transcribe the continuation. |
| Speaking | Apply playback echo rejection before accepting onset; stop old output and capture genuine user speech. |
| Sent, awaiting speaker drain | Keep playback echo rejection; interrupt queued playback and capture genuine user speech. |

### Barge-in means continuation

Barge-in gives the user the floor. It does not mean “cancel and discard what the
user says.” Capture onset pre-roll and the rest of the utterance exactly once.
Endpoint and transcribe that capture normally. Barge-in stops the reply's audio, not
its agent run: the continuation goes to the same Hermes conversation as a message
sent while the run is going, exactly as a Telegram message typed mid-run does, and
Hermes's `busy_input_mode` decides what it does to the run. With the default,
`interrupt`, Hermes redirects the run: the words are appended as a user message
and the run's next response answers the original request and the continuation
together. For example, “turn on the light” followed during thinking by “and make
it warm white” must retain both instructions. If an earlier request never reached
Hermes, carry its text into the next admitted request in order.

The continuation's turn speaks what the conversation says next: the redirected
run's next response, or, when Hermes admits the continuation as its own run
(`queue`), the old run's reply and then that run's. A reply none of which had
played when the speech began is still the conversation's reply, as a Telegram
reply would be on screen: speak it before the answer to the continuation, or
once the capture ends with nothing to answer. A reply some of which was heard is
not repeated.

Use Silero speech probability with the existing sustained active-turn energy
check. While output or its playback tail is active, also apply the playback echo
baseline. During an active turn, count speech within a short window so pauses
between words do not restart it, and keep the user's own speech out of the echo
baseline. A brief click, room noise, or soft incidental speech must not repeatedly
cancel a reply. Model failure falls back to the energy detector without ending the
socket. Onset acceptance is separate from the decision to capture its words.

Once capture owns input, old completion, cancellation, the client's state report, and tail timeout
must not clear its audio, pre-roll, or pending transcript. An old `standby` call must not
put a client into standby over an accepted continuation. Fence discarded or
superseded output: it must not send more PCM or create another agent run. The
conversation never has two agent runs at once; Hermes orders them.

### Cancellation controls

Cancellation is distinct from conversational continuation. Accept an explicit
`cancel` for the matching turn immediately; ignore a stale targeted cancellation.
An untargeted cancel also abandons a capture in progress. Stop the agent and TTS,
discard that cancelled capture, and send `interrupted` then `turn_done` for the
turn that was cut; a capture abandoned with no turn running sends nothing.
Leave the socket and conversation usable. After a turn's `interrupted`, send
nothing more for it but its `turn_done`, and start no other turn's frames
before that. Until 2026-12-20, also accept the bare `{"stop": true}` frame that
older clients send: stop the turn as for an untargeted `cancel`, then close the
socket. The client spec no longer has it; a client closes the socket instead.

`heard` says the device detected a wake word during a turn. It is neither
cancellation nor barge-in: log it with the session, the turn, and the wake
word, and change nothing else.

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

- **Spoken.** The `approval` tone plays on the device attached to that
  conversation, then a summary is spoken. The tone is how the person knows a
  question is coming and not an answer.
- **On screen.** `approval_request` is sent with the summary, Hermes's redacted
  command, and the choices Hermes allows for this request, for a device that
  can show a box.

The turn keeps waiting with `thinking` frames. Waiting on the approval counts as
progress, so `turn_timeout_seconds` does not end the turn; Hermes's
`approvals.timeout` (default 300 seconds) does.

#### The summary

Hermes's prompt is a command and a description, which is not something to read
aloud. The summary says who is asking, then what will happen:

> Arya wants to open a network connection to the Kitchen speaker to read its
> logs. Okay?

The server writes the start, the device's `assistant_name` and "wants to", and
the end, "Okay?". A separate model call, outside the conversation, writes only
what will happen, in twenty words or fewer: it adds nothing to the history and
does not run as a turn, and it is given the command and the description. If
that call fails, use Hermes's description in its place.

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
must not be taken as the answer. Once the question's audio has played out, listen
as in ordinary listening, not as for a barge-in over playback: no echo gate and
no interruption threshold, so a single quiet "yes" is heard, and send `state`
`listening`. The turn being open does not mean anything is playing. When the
answer is taken, send `state` `thinking`. Do not speak secrets. When the action did not
run, the agent's reply says so.

Verify each allowed choice by voice and by screen; a plain yes answering `once`;
a single short "yes" heard the first time it is said;
a choice that was not offered; an unclear reply once and
twice, a failed summary call, Hermes's timeout, a stale or repeated response,
cancellation, a disconnect and reconnect while pending, a prompt that arrives with
no device attached, a deny-only prompt, and the summary's own echo. Verify
the action runs once after approval and never otherwise, and that neither model
call appears in the conversation's history.

### Endpointing, completion, and recovery

Keep endpointing separate from onset detection. Received silence of
`endpoint_silence_ms` (default 700) is a candidate pause. Smart Turn is on by
default; `smart_turn_enabled: false` turns it off. Without Smart Turn the pause
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

An `endpointing` frame's `"commit": true` ends the capture at once with the
audio already received, bypassing Smart Turn and the wake pause. The utterance
limit still applies. Until 2026-12-20, also accept the bare `{"commit": true}`
frame that older clients send, with the same meaning; the client spec no longer
has it.

The frame's `on` turns endpointing off or on until the device changes it; each
connection starts with it on. While it is off only `commit` ends the utterance;
neither silence nor Smart Turn endpoints it. An `on` sent with `commit` applies
where the commit marker is reached in the audio. Onset detection,
barge-in, and the wake pause are unchanged.

The utterance limit (60 s) is a memory backstop, not an endpoint. It must never
be short enough to cut ordinary speech: a person explaining something for twenty
seconds is one request, and truncating it silently drops half the sentence.
A capture that reaches the limit ends at its last pause, a quiet stretch of
300 ms or more that the person spoke through. What followed that pause begins
the next capture, so no word is cut or lost. With no pause to end at, it ends at
the limit. Either way the reason is `max_utterance`.
Capture traces record the endpoint mode, windows, wake flag, end reason, final
pause, and longest pause spoken through. A failed/empty STT result starts no
run and releases capture ownership. Continue listening within an open activation.
Do not claim a VAD or endpoint model can eliminate every premature endpoint:
continued speech after endpoint must still be handled as continuation.

Emit `thinking`, optional `speaking` and PCM, then the turn's terminal
frames in order. Send `state` each time the turn's state changes, so the client
shows it without inferring it from other frames: `thinking` before the turn's
first `thinking` frame and whenever the agent resumes after waiting on the
person; `speaking` before the first PCM of each stretch of speech; `listening`
when the turn waits on the person and their speech is being listened for.
Ordinary, interrupted, failed, or superseded replies omit `next_action` from
`turn_done`, preserving the device's interaction mode. Only an explicit standby
decision sends `next_action: "standby"`, and only an explicit mute decision
sends `next_action: "mute"`. Send `next_action: "listen"` only to
switch a device that is not listening into listening because the server wants
the person's answer, such as a server-started reply that asks a question; leave
it out otherwise, including for a device already listening.

Pace reply PCM against the playback it implies so the server, not the client,
holds the backlog. Track where the client's queued audio ends; before sending a
chunk, wait until that is at most 1.5 s ahead of now. If it is already in the
past (TTS was slower than real time), restart from now. Send the first chunks
without delay, stop waiting as soon as the turn is interrupted, and do not count
pacing waits toward the turn timeout. The client leaving `speaking` remains the
end of playback.

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
means sending finished. The client's `state` leaving `speaking` means playback
finished or was discarded; it cannot prove the user heard the response. An interruption records that a reply
was cut short, not that a device acknowledged failed delivery. Agent-facing
context must not describe a generated or transmitted response as heard.

After `turn_done`, retain playback protection until the client reports a state
other than `speaking` for that turn, or the bounded fallback timeout. Ignore a
report for another turn. A tail interruption sends an updated `turn_done`
without `next_action` for the same turn, clearing its pending standby action
while preserving the device's interaction mode;
it does not create another reply for that turn. Quiet advisories count received
idle silence, never wall time or time occupied by a reply/capture. Audio
received while a capture is transcribed is heard for speech but is not idle
silence: the count starts again at the first audio received after it. Restart the
count on speech onset, turn start and end, playback drain, and when received
audio resumes after a gap of at least 1 s, so silence from an earlier listening
phase never shortens the next one. `thinking` frames
repeat every 5 seconds and stop with their owning turn.

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
conversation extension.

The device's conversation is a gateway session, described under Gateway
conversation. The gateway creates it, keeps its history, and continues it through
compaction. Preserve the device's routing identity. Do not build a parallel
conversation store or expose Hermes session mechanics to clients.

Reject a missing or empty name; a `profile`, `name`, `conversation`, or
`conversation_id` field; and a combined title over 100 characters with
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

## Wake words

The server sets each device's wake phrase and serves the models that detect it,
so devices using the same phrase send identical wake claims. Every wake-word
device starts with a bundled `Hey Arya` model on first setup.

| Setting | Default | Meaning |
|---|---|---|
| `wake_word` | `Hey Arya` | The default phrase for every named device. |
| `device_wake_words` | none | A phrase for particular devices by `device_name`, such as `{"Den Echo": "Hey Sakura"}`. |
| `wake_word_repositories` | `[{"repository":"Keryxion/wakewords","ref":"main"}]` | Ordered list of public GitHub dictionaries, each with its own branch, tag, or full commit hash. Omitted `ref` means `main`; `[]` uses only bundled and cached models. |

For example, load the shared dictionary and a household dictionary:

```json
{"wake_word_repositories":[
  {"repository":"Keryxion/wakewords","ref":"main"},
  {"repository":"your-org/household-wakewords","ref":"v1"}
]}
```

A dictionary's format is defined in `Keryxion/wakewords`. The server reads its
root `index.json` (`version: 1`), whose `entries` give each `phrase`, its
`description`, entry `version`, and `manifest` path, and each phrase manifest,
whose `models` map gives a pinned `url` and `sha256` for `micro_wake_word` and
`open_wake_word` (microWakeWord also lists its `.tflite` in `files`). Reject
other index versions. Only entries with trained models are offered.

Combine the entries of all configured dictionaries. Phrases that differ only by
case or spacing are one phrase; for a phrase listed more than once, the last
repository in the list supplies its whole entry. A phrase is selected exactly as
the combined listing spells it. Reordering sources affects the next selection;
it does not replace a device's installed model.

Refresh the dictionaries when the `set_wake_word` tool lists or selects a
phrase, and when a configured phrase has no cached models. Resolve each
repository's `ref` to its own commit from the repository's Git refs, and read
its index and manifests from that commit at `raw.githubusercontent.com`; an
unchanged commit is not read again. Keep each dictionary's last good index in
memory when its refresh fails, report that source's failure, and continue
loading the others.

Download a selected phrase's files from its pinned URLs, verify every hash, and
cache them before sending the switch command. Record the source repository,
commit, entry version and artifact hashes in `source.json` beside the cached
model. Refreshing dictionaries does not install new models: a cached phrase
changes only when that phrase is selected again. A cached phrase still works
when GitHub is unavailable. A failed download leaves the cached model and the
active phrase alone.

Cached models live in the profile's `plugin-data/hermes-converse/wake/<slug>/`,
where `<slug>` is the phrase in lowercase with spaces as `_` (`hey_arya`). A
folder holds a microWakeWord `manifest.json` with its `.tflite`, an openWakeWord
`<slug>.onnx`, or both. `ready` and `set_wake_word` offer only the format the
device named in `start`'s `wake_word_format` (client spec, section 8), with its
URL and the file's `sha256`. A phrase with nothing cached is still sent, with
empty `models`. A device that sends no `wake_word_format` has no wake word from
the server: no `ready.wake_word`, no `set_wake_word`, and the tool reports
`model_unavailable`. In the served microWakeWord manifest, name
the `.tflite` by its content hash and rewrite the model reference to that local
filename, so a changed model also changes the manifest hash advertised to
clients.

Serve the files at `GET /v1/audio/wake/<slug>/<file>` on the API server, with
the same API key as the voice route in `Authorization: Bearer`. Serve only
files in that folder; answer anything else with 404, and a missing or wrong key
with 401.

“Hey Arya, change your wake word to Hey Sakura” calls the `set_wake_word` voice
tool with `phrase: "Hey Sakura"`. Without `phrase`, the tool lists the combined
dictionary's phrases, their available model formats and descriptions, and the
device's configured phrase. With a phrase, download and verify its models
before sending the command; a phrase with no model in the device's format fails
with `model_unavailable`, and `Hey Arya` needs no download, since devices
bundle it. The tool acts on the device attached to the calling conversation; it
has no device argument. Send `set_wake_word` with its models on the open socket
(client spec, section 8), and save the phrase in `device_wake_words` when the
device reports that it switched. A device override takes precedence over
`wake_word`.

When a connected device's configured phrase changes in the settings, select
that phrase and send `set_wake_word` the same way, within a few seconds and
without a reconnect. An offline device receives its phrase in `ready` on
reconnect. `ready` never waits for GitHub: it carries the models already
cached, and a configured phrase with none cached is selected and sent in
`set_wake_word` once the device is connected.

Allow one switch at a time per device. Confirm that the device changed only
after its `wake_word_set` reports `ok` for that phrase, within 30 seconds. An
error, disconnect, or missing result does not confirm a switch. Report a failed
switch as failed; the device keeps its previous model and phrase, and
`device_wake_words` is left unchanged, so the device hears its previous phrase
in `ready` when it reconnects. `assistant_name` remains the client's choice,
independent of the wake phrase.

| Tool result | Meaning |
|---|---|
| `{"wake_word":"Hey Arya","phrases":[{"phrase":"Hey Sakura","formats":["micro_wake_word","open_wake_word"],"description":"..."}]}` | No `phrase` was given: the device's configured phrase and the phrases on offer. `unavailable_dictionaries` lists repositories that could not be refreshed. |
| `{"result":"ok","phrase":"Hey Sakura"}` | The device reported that it switched. |
| `{"result":"failed","error":"..."}` | No switch was confirmed. `unknown_phrase`, `model_unavailable`, `download_failed` and `hash_mismatch` come from the server before anything was sent; `model_unavailable`, `download_failed`, `hash_mismatch` and `load_failed` come from the device; `timeout` and `disconnected` mean it did not answer; `setting_not_saved` means the device switched but its setting could not be written. |
| `{"result":"not_attached"}` | No device is connected to this conversation. |
| `{"result":"not_a_device"}` | The call was made from a chat that is not a voice device. |

Verify: a fresh device hears `Hey Arya` without downloading; a device is
offered only its own model format; a live switch
needs no reconnect and is confirmed only by a successful device result; a
duplicate phrase uses the last source's entry; a failed source keeps its last
good index without blocking the others; a bad hash prevents a switch; a changed
model changes its advertised hash; cached models work when GitHub is
unreachable; and unauthorized or out-of-folder requests are refused.

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
| Barge-in | the continuation goes to `handle_message` while the run is going; the run is not interrupted |
| `cancel` | `interrupt_session_activity`, then wait for the turn to settle |
| Tool and reasoning progress | `pre_tool_call` and `post_api_request` plugin hooks |
| `command` from the client | the gateway command named in `name`, with `args`, after stopping any turn in flight; its reply is not spoken and goes back to the client in a `command` frame |
| Model selection | the gateway `/model` command, sent only when the choice differs from the session's; its confirmation is not spoken |

The chat is the device's conversation: chat name
`<assistant_name>: <device_name>`, user `recipient_id`, user name `device_name`.
Hermes owns the session, its history,
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
| Device in Do Not Disturb | Not spoken; the reply remains in the conversation's history. |
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

A `command` frame names any gateway command in `name`, without a leading slash.
`args` is an array of strings, defaulting to `[]` when omitted. Reject other
argument types with `bad_command` before stopping a turn or sending a command.
Join the arguments with spaces after `/<name>` and let the gateway's own
command parser interpret the resulting text for the device's conversation,
as it would a chat user's command. Return its
reply in a `command` frame without speaking it. There is no list of allowed
commands: a device holding the API key can run what a chat user can, including
commands that change approvals.

Answer a `commands` frame with Hermes's command registry as its Telegram
command menu lists it: each command that runs outside a terminal, with its
description, aliases, `args_hint`, and subcommands, then plugin commands. Do not
filter by the slash-access policy; the gateway applies it when a command runs. Answer
a `command` frame whose `name` is not one of Hermes's commands or aliases with
`unknown_command` instead of sending it.

After a command that starts a new conversation (`new`, or any alias of it), send the answer and
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

Never send a user `transcript` without its `message_id`. Send it when Hermes
has stored the message, with the stored message's words, which include any
earlier request carried into it. Look the message up by the turn's own
`turn_id`, which is its platform message id, never by whichever turn is
running. When a turn is cancelled or interrupted around the handover, settle
the turn first, then look: send the frame before `interrupted` if the message
is there, and no frame if it is not. If Hermes's messages cannot be read, send
no user `transcript`.

Words that arrive while a run is going are stored differently: Hermes puts the
message's origin, a line of JSON that carries its id, ahead of the words, gives
the row no platform message id, and wraps a redirected run's copy in its steer
marker. Read the id from the origin, and send and replay the words alone. With
Hermes's `privacy.redact_pii` the origin's id is hashed, so such a message is
not found and has no transcript.

Text that is not a Hermes message gets no `transcript` frame of either kind:
`say`, and the spoken approval summary. A server-started reply is a Hermes
message and closes with its `message_id` like any other.

When a client connects with `transcripts: true`, replay what it missed from
Hermes's own messages for the device's conversation, right after `ready` and
before any live frame. Keep no second copy.

- Replay user and assistant messages only, as `transcript` frames with
  `replay: true`: no tool calls, tool results, reasoning, or system text.
- An assistant message's `text` is what the turn says aloud, worked out from
  Hermes's messages and not from what was synthesized: the reply text with
  Markdown removed, or, for a turn that used `speak_this`, the text given to the
  tool. It is the same whether the reply was played in full, cut short, or
  finished with nobody attached. A turn that says nothing, such as `[SILENT]`
  or `speak_this` with `""`, is not replayed.
- A turn still running is replayed up to its user message only. Its reply so
  far goes in one more frame, as below.
- With `after`, replay every such message after it, up to the most recent 100.
  Without `after`, or when `after` is not a message of the current
  conversation, replay the most recent 20.
- When `start` carried `after`, say in `ready` whether it still applies:
  `new_session` is false when `after` is a message of the current
  conversation and true when it is not. True is how a client learns its conversation
  was replaced, whatever replaced it: a `command` frame, the Hermes web UI, or a
  deleted conversation.
- Include messages that compaction has folded away when Hermes still has them.

### A turn running at reconnect

A replacement connection takes over the running turn. After the replay, send
one assistant `transcript` with `replay: true` and no `message_id` holding the
turn's text so far, by the same rule as replayed text and including what was
generated while nobody was attached; then `state` with the turn's current
state and a `thinking` frame at once, with the turn's counters as they stand;
then any pending `approval_request`. Text pieces sent afterwards continue from
the end of that frame, so the pieces a client appends to it add up to the text
a later replay gives. Audio resumes with the first text generated after the
reconnect, behind a new `speaking`; do not synthesize what was generated while
nobody was attached, and do not resend audio.

### Nobody attached

A turn can run with no device attached, because a turn outlives a dropped
connection and other producers start turns too. Its output goes nowhere, and
that is expected:

- Do not synthesize speech for it. The reply is in the conversation's history
  and is replayed as a transcript when a client catches up, with the text it
  would have said.
- `speak_this`, `play_tone`, `standby`, `mute`, `set_wake_word`, and `countdown` return `not_attached`,
  so the agent can tell nobody is there and tell another room or chat instead.
- Nothing is queued to be spoken later.
- A pending approval is held, as described under Approvals.

Verify: a reply that finished while the device was away is replayed as a
transcript and never spoken; no speech is synthesized for it; connecting twice
with the same `after` replays the same frames; connecting with the newest `message_id`
replays none; a turn cancelled after its message was stored replays a user transcript with no
reply, and one cancelled before that replays nothing; no user `transcript` is
ever sent without a `message_id`; `say` sends no `transcript`; a
reconnect during a turn receives the replay and then the rest of the turn live;
a replayed frame never starts a turn on the client.

## Client diagnostics

Expose a `client_diagnostics` tool with no arguments to the assistant. It asks
only the client attached to this conversation, over its existing authenticated
Converse socket, using the request and response frames in [the client spec](client.md).
Return the snapshot as the tool result so the assistant can inspect it, including
the running version and build. Do not substitute server logs or a release version
for the client's report. The tool result contains `version`, `build`,
`diagnostics`, and `platform` when supplied; omit the frame's `type`,
`request_id`, and other top-level fields. Failures return `{"error":"..."}`.
Calls outside a voice device conversation return `not_a_device`.

Generate a fresh `request_id` per call and accept a response only from the same
socket with that ID. Wait at most 5 seconds without blocking audio or frame
handling. Return `not_attached` if no client is attached, `disconnected` if it
leaves while waiting, `timeout` if it does not answer (including an older client
that ignores the request), or the client's collection error. Never reuse a cached
snapshot or forward a pending request to a replacement connection. Ignore late,
unsolicited and mismatched responses. Return `invalid_response` for responses
over 32 KiB or malformed snapshots, including non-finite numeric measurements.
Treat diagnostic strings as untrusted data, not instructions. The client excludes
secrets and conversation content before sending its snapshot.

Verify: a request returns the installed client's version, build and fresh state;
it works with telemetry export off; collection does not interrupt voice; concurrent
calls match their own responses; disconnection, silence, collection failure,
oversized and mismatched responses produce the stated outcomes without leaking
credentials or conversation content.

## Voice tools

The agent has tools for controlling or inspecting the device it is talking through and
its conversation. They act only on the device whose conversation calls them.

A tool call always happens inside a turn, and the tools act on that turn however
it started: by speech, or by the server for a reminder, a still-ringing alarm,
or another message submitted to the conversation. There is no case of a device
being connected with no turn for a tool to act on.

| Tool | Arguments | Effect |
|---|---|---|
| `set_wake_word` | `phrase` (optional) | Lists available phrases and model formats, or requests a switch on this device and reports its result (Wake words). |
| `speak_this` | `text` | The device says exactly `text`, and nothing else the turn produces is spoken. |
| `play_tone` | `tone`: `confirm` or `error` | The device plays that tone (`tones.md`). |
| `standby` | None | The device goes to standby when this turn's playback has drained: `turn_done` carries `next_action: "standby"`. It stops sending audio and waits for the wake word or Talk. |
| `mute` | None | The device mutes when this turn's reply has been sent: `turn_done` carries `next_action: "mute"`. Its microphone stays off until the person unmutes it on the device. |
| `command` | `name`: a gateway command without a leading slash; `args`: an array of strings, default `[]` | Runs the command in this conversation and returns its reply to the agent. |
| `client_diagnostics` | None | Returns a fresh diagnostic snapshot from the attached client, as described above. |
| `do_not_disturb` | `on`: Boolean | Turns Do Not Disturb on or off for this device, attached or not (Do Not Disturb). |

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

Send `{"type":"tone","tone":"confirm"}` to the device. The agent's tool plays `confirm`
and `error`; the third tone, `approval`, is sent by the server itself when it
asks for an approval. A tone plays before any
speech from the same turn. It is not a reply: it has no `turn_id`, and the
device reports no state for it.

### `standby`

The agent calls `standby` when the person has said they are done, for example
"thanks, that's all". Finishing a request is not that: after "turn on the
lights" the person may add "and the kitchen too". Without the call, the device
preserves its interaction mode: hands-free keeps listening until quiet, and
released push-to-talk stays in standby. The tool, the `next_action` value,
and the device state
share the one name. Standby is not mute: the device still hears its wake word.
The server does not look for a sign-off phrase in the reply. A turn that does
not call `standby` omits `next_action` from `turn_done`, unless the server
switches the device into listening as above. Keep this field absent on
cancellation, interruption, error, and timeout paths too.

| Tool result | Meaning |
|---|---|
| `ok` | Accepted. |
| `not_a_device` | The call was made from a chat that is not a voice device. Nothing happens. |
| `not_attached` | No device is connected to this conversation. Nothing is sent or queued. It has no other meaning. |

### `mute`

The agent calls `mute` when the person asks the device to stop listening, for
example "stop listening" or "mute yourself". It is the device's own mute,
reached by voice: the microphone goes off when `turn_done` arrives and stays
off until the person unmutes on the device, while the reply plays out. The
exchange ends as it does for `standby`. Nothing
can unmute by voice, because a muted device hears nothing. If the turn calls
both `mute` and `standby`, the last call wins. Speech accepted before
`turn_done` interrupts the turn, and its `turn_done` carries no `next_action`:
the device is not muted. Results are the same as for `standby`; `ok` means the
request will be sent, not that the device muted.

### `command`

A person at a device with no screen cannot type a command or tap a button for
one, so they ask: "start over", "compress this, you're getting slow". The agent
runs a gateway command for its own conversation. `name` is the command without
a leading slash. `args` is an array of strings, one per argument; omit it for
no arguments, the same as `[]`. For example:

```json
{"name":"new","args":[]}
```

```json
{"name":"compress","args":["here","3"]}
```

The commands and aliases are the ones listed by the `commands` frame (Commands
from a device), including plugin commands; there is no separate allowlist. Unknown commands return
`unknown_command` without asking for approval or sending them to the gateway.
The gateway's slash-access policy still applies. The reply is the tool result,
not speech; the agent says what matters in its own words.
The voice prompt includes this catalog with descriptions, aliases, argument
hints, and subcommands. It tells the agent to call `command` with
`{"name":"new","args":[]}` when the person asks to start a new session or start over.

The person approves every call before it runs. The agent does not decide what a
command does to the conversation: it is asked as for a Hermes approval
(Approvals) with `approval_choice` `ask`, whatever the device's setting. The
`approval` tone plays, then the server, not the agent, says what will happen,
from the description listed by the `commands` frame and `args`:

> Arya wants to run a command for you: start a new session, with fresh
> session ID + history. Okay?

`approval_request` carries the same summary, the command as given, and choices
`once` and `deny`. Answers use the spoken or on-screen flow under Approvals;
approval applies only to this call. Only `once` accepted before the
`approvals.timeout` deadline permits submission to the gateway. Denial,
cancellation, or expiry returns `denied` and runs nothing. Late answers do not
revive the request. This question does not resolve a Hermes execution approval.
When Hermes asks for confirmation of the same gateway command, satisfy that
confirmation once with this call's approval; do not change any approval setting.

A command that starts a new conversation (`new`, or any alias of it) returns
`{"result":"Approved. The new conversation starts when this reply has played."}`
after approval. This acknowledges deferral, not execution. Wait until the
calling turn's final reply has played (the device's `state` leaves `speaking`
for that reply, or its playback tail ends), then submit the command and close
the socket with `4410 conversation_changed` after its reply, as after the
frame. Close only if Hermes actually changed the session; a refusal or a failed
command leaves the socket open. Playback of the approval question or another
turn does not trigger it.
If the socket disconnects or the turn is cancelled or interrupted before the
final reply has played, drop the deferred command.

Other commands are submitted while the turn is running, without first stopping
it. Hermes's mid-run command policy applies: approval does not override a busy
refusal. For example, `compress` can return a busy reply instead of compressing
the conversation. A command queued by the gateway can execute after the turn;
`failed` means no reply was obtained, not that an accepted command was undone.

Tool results are JSON objects with a `result` string, for example
`{"result":"denied"}`:

| Tool result | Meaning |
|---|---|
| The command's reply | The gateway's response, including any access or busy refusal. |
| `Approved. The new conversation starts when this reply has played.` | A new-conversation command is approved and deferred as above. |
| `unknown_command` | Not a command or alias listed by `commands`. Nothing runs. |
| `bad_command` | The name has a leading slash or an invalid form, or `args` is not an array of strings. Nothing runs. |
| `denied` | The person did not approve it. Nothing runs. |
| `failed` | No command reply could be obtained. |
| `not_attached` | No device is connected to ask. Nothing runs. |
| `not_a_device` | The call was made from a chat that is not a voice device. Nothing runs. |

`denied` also includes a `message` telling the agent to acknowledge the lack of
approval and stop; a renewed user request needs a new approval.

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

Keep a countdown until its device sends `countdown` with `ended` for it, or
until its deadline plus the ring timeout has passed. A device's `countdown` with
`ringing` says it is ringing there (client spec, section 10); it started at its
deadline. Send a device every countdown held for it again after `ready`,
including one whose deadline has passed, so one that reconnects or restarts
shows them; the device decides whether it still rings (client spec, section 10).
A device's `ended` for a countdown, whenever it arrives, removes it: one that
was dismissed or rang out while the device was disconnected is reported after
`ready`. Ignore a report for a countdown the server does not hold. The server
does not ring anything itself and does not send a frame at the deadline.

### Still ringing

Once a device reports a countdown ringing, start a turn in that device's
conversation at 1, 2, 4, 8 minutes after its deadline, doubling, until it
ends. With the default timeout that is four turns. Each says which countdowns
are ringing and for how long, so the agent can do more each time: say something
in the room, tell another room, or tell the owner elsewhere.

When several ring on one device, run one sequence for the device, timed from the
earliest deadline among them, and name every ringing countdown in each turn. When that one ends, time the sequence from the next. No turn starts once
none is ringing.

The turn is an ordinary message submitted to the gateway session. Its reply is
spoken on the device like any other, or not at all when the agent calls
`speak_this` with `""`. Under Do Not Disturb its reply is not spoken
and the countdown keeps ringing. A snooze starts the sequence again once the
countdown rings again.

### Stopping by voice

The `cancel` wake word does not end a countdown (client spec, section 10).
The server does not listen for a stop phrase. A person ends a ringing
countdown with the device's button or tap, which the server learns from the
device's `countdown` with `ended`, or by asking the assistant.

The assistant's wake word followed by a request is an ordinary turn. The
countdown keeps its place: the agent ends it with `cancel` or `snooze`, or leaves it
ringing.

Acceptance scenarios: a countdown set in the Kitchen counts down and rings only in
the Kitchen. Exercise a call from a chat that is not a device, a device that is
not attached, a reconnect before the deadline, a device that stays running but
disconnected through the deadline, a dismissal while disconnected reported
after reconnect, a device that is off at the deadline, local dismissal, the `cancel` wake word while ringing (it does not end the countdown), other speech while ringing, a
reply and a wake word while ringing, snooze while pending and while ringing,
cancel while ringing, still-ringing turns at 1, 2, 4 and 8 minutes and none
after `ended`, two countdowns ringing at once (one turn naming both, a
dismissal ending both, one cancelled while the other rings), a reconnect while
ringing, and the ring timeout from the setting.

## Do Not Disturb

Do Not Disturb is a per-device setting: the `do_not_disturb` key of the JSON
`settings` column of the device's row in `devices.db`, off by default, shared by every assistant's conversation on that
device and kept across reconnects and restarts. It is unrelated to `quiet`,
the silence that ends listening.

The device's `do_not_disturb` frame and the agent's `do_not_disturb` tool both
set it; a frame whose `on` is not a Boolean is ignored. Send the setting to the
device as the first frame after every `ready`, and again when the tool changes
its value; do not echo the device's own frame back. The tool works whether or
not the device is attached, returns `ok`, and returns `not_a_device` outside a
voice device conversation.

The server has no timer for it. To end it at a time, the agent schedules a turn
in the device's conversation that calls the tool: with Cadence's `pulse`, an
`at` wake for one night or a `cron` role for each end of a nightly window. The
voice prompt tells the agent how.

While it is on, a turn the server starts for that device (client spec,
section 9) sends nothing to the device:

- Its reply or its `speak_this` text is not spoken. The reply stays in the
  conversation's history as usual, `send` reports success, and nothing is
  queued to be spoken later.
- `play_tone` returns `do_not_disturb` and sends nothing.
- An approval with `ask`, or the `command` tool's question, is denied at once,
  and nothing is sent. One already pending is denied when a device in Do Not
  Disturb attaches.
- What an approved `command` leaves until the reply has played, a new
  conversation, happens when the turn ends.
- The turn is told so, through Hermes's `pre_llm_call` context, so the agent
  can notify the person another way its tools provide. A still-ringing
  countdown turn still runs.

Whether a reply is held is decided when it is ready, so a turn that turns Do
Not Disturb off is spoken and one that turns it on is not. A reply already
accepted for speaking, playing or waiting behind the current exchange, is
spoken. A turn the person starts, by wake word or Talk, is never held, and
neither is `say` or a tone or approval in that turn. Countdowns ring as usual.

Verify: a reminder during Do Not Disturb produces no speech or tones and is not
spoken when it ends; the person's own request is answered aloud; a countdown
rings and its still-ringing turn is silent; the setting follows `ready` and
survives a reconnect and a restart; the tool and the device frame each change
it, the tool also with no device attached; a turn that calls `do_not_disturb`
with `on: false` is spoken and one with `on: true` is not; an approval in a
server-started turn is denied with nothing sent, and one in the person's turn is
asked aloud; the agent is told in a server-started turn and not in the person's
own; a client that never sends the frame is served as before.
