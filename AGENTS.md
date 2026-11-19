# Repository guidance

## Keep it simple

- Implement only what the user requested. A possible future feature is not a requirement.
- Do not invent extension negotiation, capability frameworks, modes, credentials,
  identifiers, endpoints, or migration protocols to support a simple change.
- Prefer ordinary JSON fields and existing authentication over new abstractions.
- When asked to remove something, delete it and its scaffolding. Do not replace
  it with an "out of scope," "unsupported," or "maybe later" paragraph.
- Keep spec additions small. Do not turn a roughly 50-line request into a framework.
- Inspect Hermes's existing APIs and behavior before duplicating its functionality.

## Code

- Always use async I/O. A coroutine awaits a queue, event, socket or timer; it
  never blocks the event loop and never parks a thread-pool thread waiting for
  one (`run_in_executor`/`to_thread` around `queue.get()` or `Event.wait()`).
  The default pool is small (a 2-CPU host has 6 threads) and shared by every
  connection. `to_thread` is only for short blocking calls such as a database read.

## Database storage

- Keep new columns to a minimum. Prefer JSON stored in a settings or attributes
  column for optional, evolving data, so adding a setting does not require a
  schema migration on every release. Reuse an existing JSON column when available.
- Add dedicated columns when needed for queries, indexes, relationships, or
  database constraints. Keep JSON readers tolerant of missing and unknown keys,
  and preserve unknown keys when updating settings so older releases can coexist.
- Name columns explicitly in SQL inserts; do not depend on the table's column
  count or order.

## Specifications

- The client spec is written from the client's point of view: what JSON to send,
  what JSON comes back, and observable behavior. Show concrete JSON examples.
- Put server storage, Hermes integration, persistence, and recovery details in
  `specs/server.md`, not the client contract.
- State intended behavior directly. Do not qualify it with "currently" or recount
  old implementations and abandoned designs.
- A merged spec is simple, accurate, and not proposed. A proposal lives in a pull
  request until it is decided; do not write "proposed" sections into a spec.
- A spec-only request authorizes documentation changes, not implementation or deployment.
- Keep related PRs consistent, but do not add unrelated features while editing them.
- The Arya conformance suite pinned in `.crow/arya-test.yaml` checks
  `specs/client.md` byte for byte. After changing the client spec, update the
  suite's assertions and snapshot and move the pin, so a deployment never runs
  a suite written for the old contract.
- Do not add new metaphors or names for an existing thing. Keep the spec's word:
  an alarm or a timer is a countdown, in prose and in frame names.
