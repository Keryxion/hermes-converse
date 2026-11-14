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
