# Server tracing

What the server records about voice turns and wake elections. The client side
is in [client tracing](client_tracing.md); the two join on the IDs below.

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
(client spec, section 4), so a client can attach its own spans to the turn.

Spans carry measurements and decisions only: the turn and session IDs, rates,
endpoint mode and decision, pause lengths, counts of characters and PCM chunks,
time to first token and first PCM, the outcome, and `next_action`.

## Wake elections

Record each election with its `election_id`, the policy and window, and every
claim received:

| For each claim | Value |
|---|---|
| `arya.session_id` | The claiming socket's `session_id`. |
| `arya.wake.claim_id` | The `claim_id` as received. |
| Ranking inputs | Confidence, volume, and `heard_at`, when the client sent them. |
| Arrival order | Its position among the claims received. |
| Outcome | `granted`, `denied`, `withdrawn`, or `error`. |
| Decision sent | Whether the `wake_grant` or `wake_deny` was sent. |

Say which input decided the winner: confidence, volume, detection time, or
arrival order. Send the `election_id` on both decisions so every device's
records share it.

When arbitration is off, record the immediate grant as an election of one,
marked immediate.

Recording never changes an election. Do not rank claims again for the trace;
record the inputs and the result the election used.

## From grant to audio

Record the first PCM received for an activation, with its session and, for a
wake activation, its claim and election. Keep the claim and election on the
`voice.turn` spans that follow, through follow-ups and barge-ins, until standby.

Mark where an activation came from with `activation.source`: `wake`, `manual`,
`continuous`, or `outbound`.

The server does not refuse audio from a device it denied. If audio arrives for a
denied or timed-out claim, record `wake.audio.unexpected` with the session and
claim.

The server knows what it decided and what it received. It does not know whether
a client received a grant in time; leave that to the client's records.

## Limits

- Export is asynchronous and bounded. When a queue is full, drop telemetry.
- Tracing never delays audio, moves an election window, or changes a decision.
- Never export audio, transcripts, reply text, exception messages, credentials,
  or hardware identifiers. Only named attributes are exported; anything else is
  dropped.

## Checks

1. With no collector, and with a failing one, a turn and an election behave the
   same as with tracing on.
2. A turn produces `voice.turn` with its four phases at their measured times.
3. Four devices claim one phrase: one election record with four claims, one
   grant, three denials, and the input that decided it.
4. The winner's claim and election appear on its turn spans through a follow-up.
5. Audio from a denied device is recorded as unexpected and still accepted.
6. No span carries audio, a transcript, or reply text.
