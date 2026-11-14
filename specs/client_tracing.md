# Arya client tracing

The server's record of a wake election already shows every device that
claimed, which one won, and whose audio followed
([server tracing](server_tracing.md)). A client's tracing adds the one thing
the server cannot know: what happened on the device. Did the decision arrive in
time, and what did the device do about it?

Tracing is optional and off unless enabled. A client that does not trace still
follows [the client spec](client.md) in full, and the server's records are
complete without it.

## What to record

One record for each wake claim the client sends, and one for each turn. A
record is a span or an event; either is fine if it carries the attributes
below.

| Record | Attributes |
|---|---|
| Wake claim | `device_name`, `session.id`, `claim_id`, `wake_word`, `wake.outcome` |
| Turn | `device_name`, `session.id`, `turn_id`, and the `claim_id` of the wake that started it, if one did |

Attributes are named after the fields in the client spec. `session.id` is the
`session_id` from `ready`, under the name the server's spans already use. Set
`client.kind` to `macos`, `ios`, `echo`, or `esp32` once, on the resource.

`session.id` and `claim_id` together identify a claim, and they are how a
client's record is matched to the server's record of the same election.
`turn_id` matches a client's turn to the server's. When a turn's frames carry
`traceparent`, attach the client's turn record to it as well.

## The outcome of a claim

Record exactly one `wake.outcome` for every claim sent. The record runs from
sending the claim to its outcome, so its duration is how long the decision took.

| `wake.outcome` | Meaning |
|---|---|
| `granted` | A matching `wake_grant` arrived before the deadline. |
| `denied` | A matching `wake_deny` arrived before the deadline. |
| `timeout` | No decision within 1000 ms. The buffered audio was dropped. |
| `abandoned` | The client gave up first. `wake.reason` says why: `disconnected`, `cancelled`, `buffer_overflow`, or `bad_wake`. |

A decision that arrives late, or for another claim, does not change the
outcome.

## Export

- Off unless enabled. Use each platform's existing OTLP setting and collector
  destination.
- Asynchronous and bounded. When a queue is full, drop telemetry.
- Tracing never delays audio or extends the 1000 ms deadline, whether the
  collector is up or down.
- Never export audio, transcripts, reply text, credentials, or hardware
  identifiers.

## Checks

1. A device that wins records one claim with `granted`, and its turns carry
   that `claim_id` through a follow-up and a barge-in.
2. A device that loses records one claim with `denied` and no turn.
3. A grant that arrives after 1000 ms: the device records `timeout`, sends no
   audio, and the late grant changes nothing.
4. Each claim record matches one claim in the server's election record by
   `session.id` and `claim_id`.
5. With export off or the collector down, the client behaves the same.
