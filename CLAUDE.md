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
  `docs/server-spec.md`, not the client contract.
- State intended behavior directly. Do not qualify it with "currently" or recount
  old implementations and abandoned designs.
- A spec-only request authorizes documentation changes, not implementation or deployment.
- Keep related PRs consistent, but do not add unrelated features while editing them.

## Names and conversations

- `assistant_name` is what the assistant answers to, such as `Alexa` or `Arya`.
- `device_name` labels the physical device, such as `Den Echo` or `Kitchen`.
- The conversation title is `<assistant_name>: <device_name>`.
- Use Hermes's existing conversations, titles, history, and compaction settings.
- Do not add a conversation override, switch/reset protocol, or inactivity reset.
- Use the existing API key. Do not introduce attachment tokens or device credentials.
- Names identify the device in this contract. Do not promise identity/history
  preservation through renames or invent a rename mechanism.
- Do not add Unicode normalization requirements or other unrequested name machinery.
- Use the colon in conversation titles; do not substitute an em dash.

## Timers

- A timer defaults to the requesting device and may target another named device.
- Confirmation goes to the requesting device; the alarm goes to the destination.
- Do not conflate conversation context with the timer's destination.
- Keep client messages simple; server scheduling details belong in the server spec.
