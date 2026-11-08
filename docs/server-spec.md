# Server spec

This document records server responsibilities behind the client protocol in
[client spec.md](../client%20spec.md). It does not change runtime behavior.

## Named Aryas and conversation resolution

Keep one device record per Arya: stable recipient identity, household/profile,
human name, optional room/aliases, permissions, credential, and default Hermes
session reference. Names must be unique in a household. Device names identify
destinations; conversation names select context. Neither is a credential.
`start.name` is the assistant's spoken name for the voice system prompt, often
related to the wake word. It is not the device label and must not be used to
identify a recipient or choose its default conversation. Several devices may
share that assistant name. For example, a device labeled `Den Echo` can answer
to `Arya` and use the default conversation `Arya: Den Echo`.

Authenticate the device before resolving a conversation or accepting a timer
request. Use the same device attachment for conversation and timer extensions.
A replacement fences the previous connection; stop and settle its in-flight
work before starting competing work. Report busy while settlement is pending.
Fencing does not prove a disconnected speaker is silent.

Use Hermes's existing session APIs for title lookup, creation, rename, history,
and continuation after compression. Do not build a parallel conversation store
or a second conversation-ID namespace. Pass the resolved Hermes session ID to
Runs; clients only need the conversation name. Do not expose storage or lineage
mechanics through the client protocol.

At start, resolve the supplied conversation title, defaulting to
`Arya: <device name>`. Reuse an authorized compatible session with that title;
create it only when the lookup confirms it is absent. On a concurrent creation
conflict, re-read the winner through Hermes. A failed lookup, unavailable store,
or inaccessible/incompatible title must not silently create another history.
Follow Hermes title validation and ownership rules; a name grants no access.

Save the default session reference so a device rename can update its title
without losing history. Rename preserves recipient identity and timer targets.
Do not rename a separately chosen custom conversation. Report success only when
the device record and default title agree; recover incomplete renames before
resolving affected names. If saved default history is deleted or inaccessible,
report unavailable instead of silently replacing it.

Bind the resolved session for the socket's lifetime. There are no switch modes,
selection receipts, separate reset frames, or inactivity expiry. Reconnect with
another name to change context. Supply trusted source-device identity/name and
the resolved session to the agent and tools for each request; do not obtain the
source identity from transcript text. This context is request-scoped when
several devices use the same conversation.

Verify that the installed Hermes version supports the required APIs. Reuse its
normal compression, retention, title-conflict, and run-admission behavior.
Recover/replace local sign-off bookkeeping where needed; an empty local reply
list does not mean the Hermes history is empty. Do not replay unacknowledged
utterances after reconnect.

## Acceptance criteria

- Kitchen, Bedroom, and Office open default conversations titled `Arya: Kitchen`,
  `Arya: Bedroom`, and `Arya: Office`.
- Reconnect, restart, concurrent title creation, and compression preserve the
  intended history without duplicate sessions.
- A supplied conversation name opens that history; another name on reconnect
  changes context without changing the Arya identity or existing timer targets.
- Device rename preserves default history and timers, while conflicts/failures
  remain explicit and recoverable.
- Unauthorized names/credentials and failed lookups never silently attach or
  create unrelated context. Old connections cannot admit competing work.
