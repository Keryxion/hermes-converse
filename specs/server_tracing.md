# Server tracing

What the server records about voice turns and wake elections. The server's
records are complete on their own: they show a whole election without any
client exporting anything. What a client adds is in
[client tracing](client_tracing.md).

Tracing is optional. With no collector configured, or with one that is failing,
voice works the same.

## Where it goes

Use the host's OpenTelemetry setup; add no setting of the plugin's own.

| Host configuration | Destination |
|---|---|
| A global tracer provider is configured | That provider. |
| Otherwise, Hermes monitoring has OTLP export enabled with an endpoint | A provider of the plugin's own for that endpoint, with `service.name` `hermes-gateway` and `telemetry.scope` `voice`. The global provider is never replaced. |
| Neither | Nothing is exported. |

## Connections

Record one `voice.connection` span for each socket, from accepting it to
closing it:

| Attribute | Value |
|---|---|
| `device_name` | The device, once `start` has named it. |
| `session.id` | The `session_id` sent in `ready`. |
| `close.code` | The WebSocket close code, such as `4401` or `4409`. |
| `close.by` | Who ended it: `client`, `server`, or `lost` when it dropped with no close. |

A socket refused before `ready`, for a bad key or a bad `start`, is still
recorded, with its close code. A replacement shows as
two spans for one device: the old one closing `4409` as the new one opens.

"It did not respond" is answered here first: either the device had a
connection at that moment or it did not.

## Voice turns

Record one `voice.turn` span for each turn, opened when capture starts, with a
child span for each phase:

| Span | Covers |
|---|---|
| `voice.capture` | From speech onset to the endpoint. |
| `voice.stt` | Transcription. |
| `voice.agent` | From the request reaching the agent to the reply's end. |
| `voice.tts` | From the first sentence submitted for speech to the last PCM sent. |
| `voice.drain` | From the last PCM sent until the device has finished playing it. |

Phases keep the times they were measured at, even when the span is written
later. While a trace is active, the turn's `traceparent` goes out on its frames
(client spec, section 4), so a client can attach its own records to the turn.

`voice.drain` is how the server knows the reply was heard to the end. It
carries:

| Attribute | Value |
|---|---|
| `drain.end` | How it ended: `drained` (the device reported it), `timeout` (no report within `playback_tail_timeout_seconds`), `interrupted`, or `disconnected`. |
| `drain.wait_ms` | How long the device took to report, from the last PCM sent. |

A `timeout` means the device never said it finished.

Every `voice.turn` carries:

| Attribute | Value |
|---|---|
| `device_name`, `session.id`, `turn_id` | Which device, connection, and turn. |
| `claim_id` | The wake that started the exchange, if one did. An exchange runs from a granted wake until the device next returns to standby, and every turn in it carries the claim. |
| `turn.source` | How the turn started: `wake` (a granted claim), `speech` (the device was already listening: a follow-up, Talk, or a client that always listens), or `server` (no speech: a reminder or another message submitted to the conversation). |
| `outcome` | How it ended: `ok`, `interrupted`, `timeout`, or `error`. |
| `turn.attached` | Whether a device was attached when the reply was ready. `false` means the reply went nowhere. |
| `message.user_id`, `message.assistant_id` | The `message_id`s of what was heard and what was said, when there are any. |

The message IDs are the link to the words. Traces never carry text; to see what
was heard or said, look those messages up in Hermes's history.

Beyond those, spans carry measurements and decisions only: rates, endpoint mode
and decision, pause lengths, counts of characters and PCM chunks, time to first
token and first PCM, and `next_action`.

## Wake elections

Record one `voice.wake.election` span for each election, from its first claim
to its decision, with the policy and the window. Record each claim as an event
on it:

| For each claim | Value |
|---|---|
| `device_name` | The claiming device. |
| `session.id` | Its socket's session. |
| `claim_id` | As received. |
| `wake_word` | As received. |
| Ranking inputs | Confidence, volume, and `heard_at`, when the client sent them. |
| Arrival order | Its position among the claims received. |
| Outcome | `granted`, `denied`, `withdrawn`, or `error`. |
| Decision sent | Whether the `wake_grant` or `wake_deny` was sent. |

On the span, say which input decided the winner: confidence, volume, detection
time, or arrival order. When arbitration is off, record the immediate grant as
an election of one claim, marked immediate.

Recording never changes an election. Do not rank claims again for the trace;
record the inputs and the result the election used.

Whose audio followed is read from the turns: the winner's `voice.turn` carries
its `claim_id`. The server does not refuse audio from a device it denied, so a
`voice.turn` carrying a denied or withdrawn `claim_id` is a device that ignored
its denial.

The server knows what it decided and what it received. It does not know whether
a device received a grant in time; that is the client's to record.

## Barge-in

Two questions must always have an answer: why did it stop talking, and why
didn't it stop.

### Why it stopped

Record an event on the `voice.turn` that was interrupted, timed at the
interruption:

| Attribute | Value |
|---|---|
| `interrupt.cause` | `speech` when the server heard the person, `cancel` when the client sent `cancel`. |
| `interrupt.phase` | Where the reply was: `thinking`, `speaking`, or `drain` (sent, still playing on the device). |
| `interrupt.result` | What the speech turned out to be: `request` (a new turn) or `empty` (nothing was transcribed). |
| `interrupt.next_turn_id` | The turn the speech became, for `request`. |

For `speech`, also record what the detector saw when it accepted the onset:

| Attribute | Value |
|---|---|
| `vad.model` | The detector in effect, `silero` or `energy`. |
| `vad.probability` | The speech probability at the onset, when the detector gives one. |
| `vad.level` | The input level at the onset, as received. |
| `vad.threshold` | The level it had to exceed, with the barge-in multiplier applied, in the same units. |
| `input.gain_db` | The session's input gain ([audio.md](audio.md)); also on each capture. |

An `empty` result is a false trigger: something was loud and speech-like enough
to stop the reply, and it was not speech.

### Why it didn't stop

A sound that does not interrupt leaves no event, so record a summary on every
`voice.turn` that sent reply audio, covering the time from the first PCM sent to
`drained`:

| Attribute | Value |
|---|---|
| `barge.mic_ms` | How much microphone audio arrived in that time. Zero means the device was not sending audio, so nothing could have interrupted. |
| `barge.rejected_quiet_ms` | Speech by the model, but below the level threshold: the person was too quiet, or it was the reply's own echo. |
| `barge.peak_probability` | The highest speech probability seen. |
| `barge.peak_level` | The highest input level seen. |
| `barge.threshold` | The level threshold in force. |

These are totals and peaks for the turn, never one record for each block of
audio.

## Limits

- Export is asynchronous and bounded. When a queue is full, drop telemetry.
- Tracing never delays audio, moves an election window, or changes a decision.
- Never export audio, transcripts, reply text, exception messages, credentials,
  or hardware identifiers. Only named attributes are exported; anything else is
  dropped.

## Checks

1. With no collector, and with a failing one, a turn and an election behave the
   same as with tracing on.
2. A device that connects, drops, and reconnects shows two connection spans, the
   first closed `lost`; a second device taking the same names closes the first
   with `4409`.
3. A wake, a follow-up, and a reminder produce turns with `turn.source` `wake`,
   `speech`, and `server`.
4. A reply that finishes after its device disconnected has `turn.attached`
   false and no `voice.drain`.
5. A turn's message IDs find its user and assistant messages in Hermes's
   history.
6. A turn produces `voice.turn` with its five phases at their measured times,
   carrying its device, session, and turn.
7. A reply played to the end records `voice.drain` ending `drained`; a device
   that never reports records `timeout`.
8. Four devices claim one phrase, none of them exporting: one election span with
   four claim events naming the four devices, one `granted`, three `denied`, and
   the input that decided it.
9. The winner's `claim_id` is on its `voice.turn` spans through a follow-up.
10. A denied device that sends audio anyway produces a `voice.turn` with its
   denied `claim_id`.
11. Speaking over a reply stops it: the turn has one interrupt event with cause
   `speech`, its phase, the detector's reading, and the turn the speech became.
12. A noise that stops a reply and transcribes to nothing is recorded with result
   `empty`.
13. Speaking too quietly to interrupt leaves no event, and the turn's summary
   shows `barge.rejected_quiet_ms` above zero with the peak level under the
   threshold.
14. A device that sends no microphone audio during a reply shows `barge.mic_ms`
   of zero.
15. No span carries audio, a transcript, or reply text.
