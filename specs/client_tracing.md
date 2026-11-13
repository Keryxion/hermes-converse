# Arya client tracing

## Goal and scope

Show one wake election across all participating Aryas, then follow the winner
through microphone audio, the voice turn, playback, and drain. With four devices
in one window, show four claims, one server grant, three server denials, and only
the client that accepts the grant sending activation audio. Repeat with three
devices and two denials.

This supplements [the client protocol](client.md), especially sections 4 and 8,
and [server responsibilities](server.md). It specifies implementation work for
macOS, iOS, Echo, ESP32, and Converse; it does not claim current clients comply.
macOS and iOS already have wake/turn tracing and optional export, but identity,
terminal outcomes, and audio correlation need consistent coverage. Reuse existing
exporters, service names, and connection/turn spans.

## Required observations

| Observation | Meaning |
|---|---|
| Local detection | A device heard a wake phrase. |
| Claim sent / received | The client sent a claim; separately, the server received it. |
| Server grant / denial | The server decided which claimant should respond. |
| Client grant / denial | The client accepted a matching decision before its deadline. |
| Client timeout | The client abandoned its buffer; a later grant cannot revive it. |
| First successful PCM send / first PCM received | Audio actually left the client / reached the server. |
| Voice turn / playback / drain | The request progressed to a reply and finished playing. |

A server grant does not prove delivery, client acceptance, or audio transmission.
A successful WebSocket send does not prove receipt by the peer. Missing client
telemetry does not prove that a device heard nothing. A winner that times out
can correctly produce zero progressing activations. Losing an election is normal.

These observations may be spans, events on existing spans, or structured records
with the IDs below. No particular span tree or tracing SDK is required.

## Identity and correlation

Wake records and first-PCM observations carry platform, server instance, socket
session, and claim identity when applicable. Use these attributes consistently:

| Attribute | Value |
|---|---|
| `arya.client.kind` | `macos`, `ios`, `echo`, or `esp32`. |
| `arya.session_id` | Socket session ID from `ready`. |
| `arya.wake.claim_id` | Client-generated claim ID, exactly as sent. |
| `arya.wake.election_id` | Authoritative server election ID, when known. |
| `arya.wake.word` | Exact wake phrase used for election grouping. |

A claim's key is **(server instance, session_id, claim_id)**. Claim IDs such as
`wake-1` can repeat on other sockets. Device names are display labels, not keys.
Server arbitration `device_id` identifies the requesting socket; named-device
`recipient_id` from `ready` is a separate namespace. Record their mapping on the
server session record when available; never assume they are interchangeable.

Keep existing platform `service.name` and version attributes. Configured device
and assistant labels, `recipient_id`, and an optional persistent opaque
installation ID belong on the resource or session record. They need not repeat
on every diagnostic span. An installation ID must not be a hardware identifier,
credential, or device name. No new service-name setting is required.

Use server-issued election IDs, scoped by server instance. Never infer an
election from rounded timestamps or phrase alone. A claim after a collection
window closes belongs to a new election even if it heard the same utterance.

## Client wake handling

Reuse the existing `wake` span where present. Record detection and
`wake.claim.sent` at the actual claim send, then exactly one terminal outcome:

| `arya.wake.outcome` | Meaning |
|---|---|
| `granted` | Matching grant accepted before the deadline. |
| `denied` | Matching denial accepted before the deadline. |
| `timeout` | No accepted decision within 1000 ms. |
| `disconnected` | Socket lost while pending. |
| `cancelled` | Stop, mute, configuration change, or explicit Talk abandoned the claim. |
| `buffer_overflow` | The client could not preserve the buffered audio. |
| `bad_wake` | The server rejected the claim. |

Record `arya.wake.decision_ms` using monotonic elapsed time from claim send to
terminal outcome; omit it if no claim was sent. Include the echoed claim ID and
phrase, winner device ID when supplied, buffered sample count/duration, and the
1000 ms deadline. Preserve available confidence, normalized RMS volume, and
`heard_at` Unix milliseconds; missing ranking inputs stay absent, not zero.

Late or mismatched decisions never change a terminal outcome, release an
abandoned buffer, or create a turn. Record `wake.decision.ignored` with incoming
and current claim IDs (when present), session, and reason: `late`,
`claim_mismatch`, `phrase_mismatch`, or `no_pending_claim`. Use an existing
connection record or diagnostic event; do not reopen a completed span.

Use monotonic clocks for local deadlines and durations. Preserve measured epoch
timestamps for spans and `heard_at`. Clock offset/RTT collection is optional.
Election/session/claim IDs and server arrival order explain the recorded decision
without synchronized clocks. Unsynchronized `heard_at` values can affect the
existing tie-break; tracing records the values used rather than correcting them.

## Server election and claim records

Record one election summary per actual collection window and every received
claim and decision under its election ID. An election span with per-claim events,
or existing structured records joined by election ID, is sufficient. Optional
`voice.wake.election` and `voice.wake.claim` spans may improve visualization;
a shared root and per-claim child spans are not prerequisites.

The election summary includes server instance, election ID, enabled flag,
policy, configured window, start/close times, duration, received/eligible/
withdrawn counts, and the winning device and compound claim key, if any.
Retain withdrawn claims in telemetry even when removed from live election state.

Each claim record includes session/claim IDs, arbitration device ID, available
ranking inputs, server arrival order, outcome (`granted`, `denied`, `withdrawn`,
or `error`), and decision-send result (`sent`, `failed`, or `not_attempted`).
Denials include the winning device ID. Record the ranking tier that decided the
winner: confidence, volume, detection time, or arrival order. Use the arbitration
inputs and result; tracing must never rerank claims or change the decision.

When arbitration is disabled or the legacy policy grants immediately, record a
single-claim summary marked as an immediate grant with its enabled flag and
policy. Assign a distinct correlation ID; do not imply a ranked collection
window occurred. Separate elections and immediate grants must remain distinct
from multiple grants within one ranked election.

## Additive decision metadata

Current wake decisions echo `claim_id` and `wake_word`; a denial may carry
`winner_device_id`. Adding the following fields to `wake_grant` and `wake_deny`
is implementation work, not existing wire coverage:

- `election_id`: the server ID for this claim's election or immediate-grant record.
- `device_id`: the requesting socket's arbitration device ID.
- Optional `traceparent`: valid W3C context for the server election or claim record.

`device_id` always identifies the claimant. Existing `winner_device_id` identifies
the winner on a denial and may differ from the claimant. Do not rename it or use
`device_id` as an alias for the winner. On a grant, the claimant is the winner.

Clients correlate using session/claim IDs and add election ID when available.
Missing or malformed tracing fields never block the existing claim/grant flow.
Optional `traceparent` may add a span link where supported. Preserve measured
timestamps; never rewrite live trace IDs. Retrospective parenting, delayed span
construction, and SDK-specific plumbing are not required. A timeout can remain
correlated by its compound claim key even if no decision metadata arrives.

## From accepted grant to voice turns

Record `wake.audio.started` on the client's first successful PCM send for the
activation, from the actual send path, with sample count and accepted session/
claim/election IDs. Record first PCM received separately on the server, with
session and known claim/election IDs. Reuse connection or turn records; new
activation spans are not required.

Keep the accepted claim/election IDs on existing client `turn` and server
`voice.turn` records across follow-ups and barge-ins until standby or activation
end. A fresh wake detection gets a new claim; a follow-up does not. Keep capture,
endpoint/STT, thinking/working, first reply audio, interruption, `turn_done`,
and actual speaker drain visible. `turn_done` ends transmission, not playback.

Distinguish sources with `activation.source`: `wake`, `manual`, `continuous`,
or `outbound` (temporary interruption monitoring for server-originated output).
Manual, continuous, and outbound activity must not inherit an unrelated wake
claim. The server knows its own decision and received PCM, not whether a client
accepted a grant on time. Without client observations, label that acceptance
unknown; do not infer it from audio or from the most recent grant alone.

Denied or expired claims send no activation audio. Record violations as
`wake.audio.unexpected` with known session/claim identity. Separate them from
an already-open activation, explicit Talk, continuous input, or outbound
monitoring. If the source is unknown, report that uncertainty. The server accepts
noncooperating audio; tracing observes this behavior and does not enforce a gate.

## Export and bounds

Reporting is configurable and off unless explicitly enabled. Reuse each
platform's existing OTLP enable/disable and collector destination settings;
use deployment configuration for embedded clients. Server observations cover
received claims from devices without direct export, but cannot prove their local
outcomes. Forwarding client observations over an authenticated socket requires
a separately specified transport; do not assume it exists.

Telemetry is asynchronous, bounded, and best effort. Bound queues, batches,
retries, and shutdown flushing; drop telemetry on overflow rather than delaying
voice. Collector failure must never block audio, shift the election window,
extend the 1000 ms deadline, or change grant/deny behavior. Record terminal
outcomes when enabled, subject to these bounds; report drops when feasible.
Sampling strategy and dashboard design are optional follow-ups, not prerequisites.

Do not export PCM, transcripts, reply text, credentials, authorization headers,
credential-bearing URLs, or hardware identifiers. Counts, durations, configured
labels/wake phrases, and opaque correlation IDs are sufficient. Keep correlation
IDs on spans or records, not high-cardinality metric labels.

## Acceptance checks

1. **Three/four clients.** One phrase/window yields one election with every
   claim and decision: one server grant, the remaining server denials, matching
   client outcomes, and exactly one first-PCM send/receive path into a voice turn.
   Existing spans/events or structured records with correlation IDs suffice.
2. **Follow-ups.** Follow the winner through reply and speaker drain, then a
   follow-up and barge-in. Claim/election identity persists until standby without
   a fresh election for each turn.
3. **Lost/delayed grant.** A lost grant or one arriving after 1000 ms yields
   client timeout and zero audio from that claim. Keep the server grant, send
   result, timeout, and any ignored late decision distinct.
4. **Abandoned claims.** Denial, stale ID, wrong phrase, buffer failure, mute,
   and disconnect release no abandoned audio. Each pending claim ends once.
5. **Separate elections.** Different phrases/windows and disabled/legacy
   immediate grants remain distinct and explain otherwise valid multiple grants.
6. **Missing telemetry.** A detection that never reaches the server exists only
   in client telemetry. A missing client outcome remains unknown, not denied.
7. **Unexpected audio.** A noncooperating loser sends PCM and remains visible;
   manual, continuous, outbound, and already-open input are distinguishable.
8. **Collector failure / opt-out.** Grant, denial, and timeout behave identically
   with export off or a failing collector; queues and retries stay bounded.
