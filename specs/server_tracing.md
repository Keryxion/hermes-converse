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

## Voice turns

Record one `voice.turn` span for each turn, opened when capture starts, with a
child span for each phase:

| Span | Covers |
|---|---|
| `voice.capture` | From speech onset to the endpoint. |
| `voice.stt` | Transcription. |
| `voice.agent` | From the request reaching the agent to the reply's end. |
| `voice.tts` | From the first sentence submitted for speech to the last PCM sent. |

Phases keep the times they were measured at, even when the span is written
later. While a trace is active, the turn's `traceparent` goes out on its frames
(client spec, section 4), so a client can attach its own records to the turn.

Every `voice.turn` carries `device_name`, `session.id`, and `turn_id`, and the
`claim_id` of the wake that started it, if one did. A follow-up keeps the claim
of the wake that opened the exchange.

Beyond those, spans carry measurements and decisions only: rates, endpoint mode
and decision, pause lengths, counts of characters and PCM chunks, time to first
token and first PCM, the outcome, and `next_action`.

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

## Limits

- Export is asynchronous and bounded. When a queue is full, drop telemetry.
- Tracing never delays audio, moves an election window, or changes a decision.
- Never export audio, transcripts, reply text, exception messages, credentials,
  or hardware identifiers. Only named attributes are exported; anything else is
  dropped.

## Checks

1. With no collector, and with a failing one, a turn and an election behave the
   same as with tracing on.
2. A turn produces `voice.turn` with its four phases at their measured times,
   carrying its device, session, and turn.
3. Four devices claim one phrase, none of them exporting: one election span with
   four claim events naming the four devices, one `granted`, three `denied`, and
   the input that decided it.
4. The winner's `claim_id` is on its `voice.turn` spans through a follow-up.
5. A denied device that sends audio anyway produces a `voice.turn` with its
   denied `claim_id`.
6. No span carries audio, a transcript, or reply text.
