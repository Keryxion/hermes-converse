# Arya client tracing

Tracing lets you see one wake phrase, heard by several Aryas, as one election:
every device's claim, the one grant, the denials, and only the winner sending
audio into a voice turn.

This is what a client records. What the server records is in
[server tracing](server_tracing.md); the two join on the IDs below.

Tracing is optional and off unless enabled. A client that does not trace still
follows [the client spec](client.md) in full.

## What to record

| Observation | Meaning |
|---|---|
| Wake detected | The device heard a wake phrase. |
| Claim sent | The `wake` frame left the device. |
| Decision received | A matching `wake_grant` or `wake_deny` arrived in time. |
| Claim timed out | No decision in 1000 ms; the buffered audio was dropped. |
| First audio sent | The first PCM of the activation left the device. |
| Turn, playback, drain | The request became a reply and finished playing. |

Record what the device saw, not what it assumes the server did. A frame that
was sent is not a frame that arrived.

An observation may be a span, an event on an existing span, or a log record.
No particular span tree or tracing SDK is required.

## Joining the records

Every wake and first-audio record carries these attributes when it has them:

| Attribute | Value |
|---|---|
| `arya.client.kind` | `macos`, `ios`, `echo`, or `esp32`. |
| `arya.session_id` | `session_id` from `ready`. |
| `arya.wake.claim_id` | The `claim_id` the client sent. |
| `arya.wake.election_id` | `election_id` from the decision. |
| `arya.wake.word` | The wake phrase. |

A claim is identified by `session_id` and `claim_id` together; a `claim_id`
such as `wake-1` repeats on other sockets. The server gives each election an
`election_id` and sends it on both decisions (client spec, section 8). Records
from every device in the same election share it.

Keep each platform's existing `service.name`. Device and assistant names are
labels for reading a trace, not keys.

## Client: one outcome for each claim

Record exactly one `arya.wake.outcome` for every claim sent:

| Outcome | Meaning |
|---|---|
| `granted` | A matching grant arrived before the deadline. |
| `denied` | A matching denial arrived before the deadline. |
| `timeout` | No decision within 1000 ms. |
| `disconnected` | The socket was lost while waiting. |
| `cancelled` | Stop, mute, a settings change, or Talk abandoned the claim. |
| `buffer_overflow` | The client could not keep the buffered audio. |
| `bad_wake` | The server rejected the claim. |

Record `arya.wake.decision_ms`, the time from sending the claim to its outcome,
measured with a monotonic clock. A decision that arrives late or for another
claim does not change the outcome; record it as ignored with the reason.

## From grant to audio

Record the first PCM sent for an activation, with its claim and election. Keep
the claim and election on turn records through follow-ups and barge-ins until
standby. When a turn's frames carry `traceparent`, attach the client's turn
records to it.

Mark where an activation came from with `activation.source`: `wake`, `manual`,
`continuous`, or `outbound`. Only a `wake` activation carries a claim.

A denied or timed-out claim sends no audio.

## Export

- Off unless enabled. Use each platform's existing OTLP setting and collector
  destination.
- Asynchronous and bounded. When a queue is full, drop telemetry.
- Tracing never delays audio, changes an election, or extends the 1000 ms
  deadline, whether the collector is up or down.
- Never export audio, transcripts, reply text, credentials, or hardware
  identifiers.

## Checks

1. Four devices hear one phrase: each records one claim and one outcome, one
   `granted` and three `denied`, all with the same `election_id`, and only the
   granted device records first audio sent.
2. The winner's claim and election stay on its turns through a follow-up and a
   barge-in.
3. A grant that arrives after 1000 ms: the client records `timeout` and sends no
   audio.
4. With export off or the collector down, the client behaves the same.
