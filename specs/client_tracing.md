# Arya client tracing

## Goal

Show one wake election across all participating Aryas, then follow the winner
into capture, transcription, generation, playback, and drain. A four-device
timeline must show four claims, one grant, three denials, and only the granted
activation sending microphone audio. A three-device election has the same shape
with two denials.

This supplements [the client protocol](client.md), especially sections 4 and 8,
and [server lifecycle responsibilities](server.md). It specifies instrumentation
to implement; it does not claim every client already emits these spans.

At the time of writing, macOS emits a `wake` span with claim ID, wake phrase,
outcome, and decision latency, plus a client `turn` span. The server logs election
IDs and decisions and emits `voice.turn` phase traces. The missing pieces are
consistent device identity, cross-device election tracing, and a link from the
winning wake claim to its activation. The iOS client also needs wake telemetry.
Implement this contract on macOS, iOS, Echo, and ESP32; server observations give
coverage for devices that cannot export OTLP directly.

## What the timeline must distinguish

| Observation | Meaning |
|---|---|
| Local detection | A device heard a wake phrase. |
| Claim sent / received | The device asked to participate; the server received it. |
| Server grant / denial | The server made a decision. |
| Client grant / denial | The device received and accepted a matching decision before its deadline. |
| Client timeout | The client abandoned its buffer. A later server grant does not undo that. |
| First microphone PCM | This activation actually began streaming. |
| User turn / reply | The activation progressed to a request and response. |

A server grant is not proof that the client received it or sent audio. A missing
client span is not proof that the device did not detect a wake word. A winner
that times out can correctly produce **zero** progressing activations. Losing a
normal election is not an error.

## Identity and correlation

Every client span carries:

| Attribute | Value |
|---|---|
| `arya.client.kind` | `macos`, `ios`, `echo`, or `esp32`. |
| `arya.device.name` | Configured device label, e.g. `MacBook Pro`. |
| `arya.assistant.name` | Configured assistant name, e.g. `Arya`. |
| `arya.recipient_id` | Opaque named-device ID from `ready`, when present. |
| `arya.session_id` | Socket session ID from `ready`. |

Resource attributes include `service.name`, `service.version`, and
`service.instance.id`. The instance ID identifies this installation, is persisted
locally, and survives reconnects; it is not a hardware serial number, MAC address,
API key, or device name. Keep service name configurable. Use attributes rather
than a different service name for every room/device.

Wake spans additionally carry:

| Attribute | Value |
|---|---|
| `arya.wake.claim_id` | Client-generated claim ID, exactly as sent. |
| `arya.wake.word` | Exact configured wake phrase used for election grouping. |
| `arya.wake.election_id` | Server-generated ID shared by all claims in one election, when known. |
| `arya.wake.outcome` | Terminal client outcome defined below. |
| `arya.wake.confidence` | Reported confidence, if available. |
| `arya.wake.volume` | Reported normalized RMS, if available. |
| `arya.wake.heard_at_unix_ms` | Detection timestamp, if available. |
| `arya.wake.decision_ms` | Monotonic elapsed time from sending the claim to its terminal outcome. |

A claim is uniquely identified by server instance, socket session, and claim ID.
IDs such as `wake-1` can repeat on other sockets. Never merge claims by claim ID
alone. Device names are display labels, not unique trace keys. Server arbitration
`device_id` and named-device `recipient_id` are distinct namespaces; record their
mapping when available, never assume they are interchangeable.

Election IDs are authoritative server identifiers. Do not invent a shared
election ID by rounding timestamps or grouping by wake phrase. Claims arriving
after a collection window closes belong to a new election even if people think
they heard the same utterance.

## Client wake span

Keep the existing span name `wake`. Open it at local detection/claim preparation,
record `wake.claim.sent` immediately before sending, and end it exactly once at
the decision, timeout, disconnect, cancellation, or buffer failure.

Terminal `arya.wake.outcome` values:

- `granted`: matching grant accepted before the local deadline.
- `denied`: matching denial accepted before the local deadline.
- `timeout`: no accepted decision within 1000 ms.
- `disconnected`: socket lost while pending.
- `cancelled`: local Stop, mute, new configuration, or explicit Talk abandoned it.
- `buffer_overflow`: insufficient space to preserve all buffered audio.
- `bad_wake`: server rejected the claim.

Record decision events with the echoed claim ID and phrase, and the server's
winner device ID when supplied. Record buffered sample count/duration and the
configured 1000 ms deadline. Missing ranking inputs stay absent, not zero.

Late or mismatched decisions must not change the terminal outcome, release PCM,
or create a turn. Emit a diagnostic `wake.decision.ignored` event on a short
diagnostic span with the incoming claim ID, current claim ID if any, and reason
(`late`, `claim_mismatch`, `phrase_mismatch`, or `no_pending_claim`). Do not
reopen an ended span.

Use monotonic time for deadlines and durations. Use epoch timestamps for spans
and `heard_at`. Record the existing handshake clock offset/RTT for cross-device
comparisons; collector arrival time and unsynchronized client clocks cannot
establish the winner.

## Server election trace

The server must emit one `voice.wake.election` root span per actual collection
window, with one `voice.wake.claim` child per received valid claim. Use the same
trace for every claimant; separate unrelated roots are insufficient for the
requested cross-device view.

The election span carries:

- Election ID, server instance, enabled flag, policy, and configured window.
- Number of received claims, number still eligible at decision time, and number
  withdrawn by disconnect/cancellation.
- Winning arbitration device ID and winning compound claim identity, if any.
- Start/close times and decision duration.

Each claim child carries the client session, claim ID, named-device identity
when known, arbitration device ID, available ranking inputs, arrival order,
server outcome (`granted`, `denied`, `withdrawn`, or `error`), and decision-send
result. A denial records the winning device ID. Include why ranking resolved:
confidence, volume, detection time, or arrival order. Preserve missing inputs
as missing. Record the data already used by arbitration; tracing must not rerank
or change its decision.

When arbitration is disabled, emit a single-claim election marked disabled and
an immediate grant. Multiple grants from independent windows or disabled
arbitration must be visibly distinct from multiple grants in one ranked election.

## Joining clients to the server

The current wake frames guarantee claim ID and phrase, but do not yet provide
an election trace context. Implement these additive tracing fields on
`wake_grant` and `wake_deny`:

- `election_id`: the server's election ID.
- `traceparent`: W3C context of that claimant's `voice.wake.claim` span.
- `device_id`: the requesting socket's arbitration device ID.

This is tracing metadata, not contract negotiation or permission to stream.
Existing clients may ignore it. Missing or malformed metadata never blocks the
existing claim/grant flow.

Clients finish the measured `wake` span after the decision and export it as a
child of the supplied server claim context, retaining original detection and
decision timestamps. A client can start earlier than its server parent because
it measured local detection and transit; durations must not be clamped to hide
that. Before receiving context, a timeout/disconnect exports an independent
span with its compound claim identity for correlation with the server's claim.
Never fabricate a parent or attach a later decision to a different activation.

If an exporter cannot set a parent retrospectively, use an OTEL span link.
Do not replace trace IDs manually on a live SDK span.

## Only the winner moves on

The first microphone send after an accepted grant emits
`wake.audio.started` on an activation span, with the winning session/claim/
election IDs and sample count. This observation must come from the actual
send path, not the grant handler.

Create a `voice.activation` server span when first PCM is actually received.
Associate it with the last accepted wake decision for that connection and the
election trace. Mark `activation.source` as `wake`, `manual`, `continuous`,
or `outbound`. The server can know it sent a grant; only the client knows
whether it accepted that grant before its deadline.

Link the client's existing `turn` span and server `voice.turn` spans to this
activation/election. Follow-up turns and barge-ins keep the activation's wake
identity until standby; they do not manufacture fresh wake claims. A client
that stays idle after denial must have no new activation.

Record unexpected PCM after a denied/expired activation as a diagnostic
`wake.audio.unexpected` with device and claim identity. Distinguish it from
audio belonging to an already-open activation, explicit Talk, continuous mode,
or outbound playback monitoring. The server currently accepts noncooperating
audio; tracing must report this honestly rather than imply it enforces the gate.

Keep capture, endpoint/STT, thinking/working, first reply audio, interruption,
`turn_done`, and actual speaker drain visible. `turn_done` means transmission
finished, not that the speaker is quiet.

## Export and operational behavior

Reporting is configurable and disabled by default unless explicitly configured.
Expose enable/disable and collector destination on clients with Settings; support
deployment configuration for embedded clients. Preserve each platform's existing
OTLP configuration. Constrained clients may send structured observations over
their authenticated socket for server-side export; that transport must be
specified before implementation, not assumed to exist.

Telemetry is asynchronous, bounded, and best effort. Collector failure must never
block the audio thread, shift the election window, extend the 1000 ms deadline,
or change grant/deny behavior. Retain terminal wake outcomes when tracing is
enabled; if sampling is used, keep an election and its children together rather
than independently sampling away the losers.

Do not export PCM, transcripts, reply text, credentials, authorization headers,
full credential-bearing URLs, or hardware identifiers. Export counts, durations,
configured display names, exact configured wake phrases, and opaque correlation
IDs. These IDs belong on spans/logs, not high-cardinality metric labels.

Useful dashboard measurements are election claimant count, grant/deny/timeout
counts, decision latency, grant-to-first-PCM latency, and unexpected-audio count.
Show received, eligible, and successfully delivered decisions separately.
A device table should show device name/platform, claim, reported signals,
server decision, client outcome, and whether audio started.

## Acceptance tests

1. **Four simultaneous clients.** One phrase/window, four named clients with
   different confidence scores. The trace contains one election, four claims,
   one server grant, three server denials, four matching client outcomes, and
   exactly one activation/first-PCM path. Repeat with three clients.
2. **Winner proceeds.** Follow the grant through actual PCM send/receive,
   transcript, agent run, playback, and drain. Correlation survives follow-ups
   and barge-in without producing a second wake election.
3. **Lost/delayed grant.** A server grant delivered after 1000 ms yields client
   timeout and zero audio from that abandoned activation. Both observations are
   visible; the late grant must not turn the timeout into a success.
4. **Loss and mismatch.** Denied, stale-ID, wrong-phrase, buffer-overflow,
   mute, and disconnect paths release no buffered activation audio. Terminal
   spans end once; an ignored decision cannot reopen them.
5. **Different windows and phrases.** Show distinct elections and explain why
   two grants can be valid. Do not merge by timestamp proximity.
6. **Disabled arbitration.** Mark immediate-grant behavior explicitly; do not
   present it as a successful multi-device ranked election.
7. **Absent device.** A client that detected but failed to send is visible only
   through its local telemetry; do not fabricate a server claim or denial.
8. **Collector failure / opt-out.** Repeat grant, denial, and timeout with
   reporting disabled or a failing collector. Audio and arbitration behavior
   remain identical; telemetry queues stay bounded.
