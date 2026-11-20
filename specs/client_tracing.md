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
| Connection | `device_name`, `session.id`, how long connecting took, and why it ended: the close code, or that it was lost |
| Wake claim | `device_name`, `session.id`, `claim_id`, `wake_word`, `wake.outcome` |
| Turn | `device_name`, `session.id`, `turn_id`, and the `claim_id` of the wake that started the exchange, if one did. An exchange runs from a granted or timed-out wake until the device next returns to standby. |
| Playback | `turn_id`, when the first reply audio played, when the last did, and when the state left `speaking` |

For a client that displays assistant reply status, record status changes so a
false `Interrupted` label can be compared with playback for the same `turn_id`:

| Event | Attributes |
|---|---|
| `reply.status_requested` | `turn_id`, `status` (`replying`, `complete`, `interrupted`, or `error`), `reason` (the frame or local action that requested it), `playback.active` (whether speaker audio was still queued or playing), and `playback.drained` (whether playback for this turn finished) |
| `reply.status_displayed` | `turn_id`, `requested` status, and `displayed` status after the transcript model applies it (`none` if no reply row exists) |

Record the requested status at the client frame/action boundary and the displayed
status immediately after updating the conversation. A later completion that leaves
an interrupted row sticky must therefore produce `requested=complete` and
`displayed=interrupted`. Use bounded status/reason values; never put reply text
or transcript content in these events. A playback `drained` record says the client
drained its queue, not that a person heard every word.

Attributes are named after the fields in the client spec. `session.id` is the
`session_id` from `ready`, under the name the server's spans already use. Set
`client.kind` to `macos`, `ios`, `echo`, or `esp32` once, on the resource.

`session.id` and `claim_id` together identify a claim, and they are how a
client's record is matched to the server's record of the same election.
`turn_id` matches a client's turn to the server's. When a turn's frames carry
`traceparent`, attach the client's turn record to it as well.

## The outcome of a claim

Record exactly one `wake.outcome` for every claim sent. The record runs from
sending the claim to its outcome, so its duration is how long resolution took.

| `wake.outcome` | Meaning |
|---|---|
| `granted` | A matching `wake_grant` arrived before the deadline. |
| `denied` | A matching `wake_deny` arrived before the deadline. |
| `timeout` | No matching decision within 1000 ms. The client proceeded with the buffered activation, then live audio. |
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
3. A grant or denial that arrives at or after 1000 ms: the device records one
   `timeout`, flushes buffered audio once before live audio, and the late
   decision changes nothing. Its turns retain that claim's `claim_id`.
4. When the server received the claim, correlate its election record by
   `session.id` and `claim_id`. A client timeout does not prove server receipt.
5. A reply played to the end records when playback started, when it ended, and
   the state leaving `speaking` after the end, not at `turn_done`.
6. With export off or the collector down, the client behaves the same.
