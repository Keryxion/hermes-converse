# Arya client tracing

Tracing lets you see one wake phrase, heard by several Aryas, as one election:
every device's claim, the one grant, the denials, and only the winner sending
audio into a voice turn.

Tracing is optional and off unless enabled. A client that does not trace still
follows [the client spec](client.md) in full.

## What to record

| Observation | Recorded by | Meaning |
|---|---|---|
| Wake detected | Client | The device heard a wake phrase. |
| Claim sent | Client | The `wake` frame left the device. |
| Claim received | Server | The `wake` frame arrived. |
| Election decided | Server | Which claim won, and why. |
| Decision received | Client | A matching `wake_grant` or `wake_deny` arrived in time. |
| Claim timed out | Client | No decision in 1000 ms; the buffered audio was dropped. |
| First audio sent | Client | The first PCM of the activation left the device. |
| First audio received | Server | The first PCM of the activation arrived. |
| Turn, playback, drain | Both | The request became a reply and finished playing. |

Record what each side saw, not what it assumes about the other. A grant the
server sent is not a grant the client received, and a device with no records is
not a device that heard nothing.

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

## Server: one record for each election

Record each election with its `election_id`, the policy and window, and every
claim received: its session, its ranking inputs, its arrival order, its outcome,
and whether the decision was sent. Say which input decided the winner:
confidence, volume, detection time, or arrival order.

When arbitration is off, record the immediate grant as an election of one,
marked immediate.

## From grant to audio

The client records the first PCM it sends for an activation and the server the
first it receives, each with the claim and election. Keep the claim and
election on turn records through follow-ups and barge-ins until standby.

Mark where an activation came from with `activation.source`: `wake`, `manual`,
`continuous`, or `outbound`. Only a `wake` activation carries a claim.

A denied or timed-out claim sends no audio. If the server receives audio for
one, it records `wake.audio.unexpected`.

## Export

- Off unless enabled. Use each platform's existing OTLP setting and collector
  destination.
- Asynchronous and bounded. When a queue is full, drop telemetry.
- Tracing never delays audio, changes an election, or extends the 1000 ms
  deadline, whether the collector is up or down.
- Never export audio, transcripts, reply text, credentials, or hardware
  identifiers.

## Checks

1. Four devices hear one phrase: one election, four claims, one grant, three
   denials, and one device's audio leading to a turn.
2. The winner's claim and election stay on its turns through a follow-up and a
   barge-in, with no new election.
3. A grant that arrives after 1000 ms: the client records `timeout` and sends no
   audio; the server's grant and the client's timeout are both visible.
4. A device that ignores its denial and sends audio is recorded as unexpected.
5. With export off or the collector down, grants, denials, and timeouts behave
   the same.
