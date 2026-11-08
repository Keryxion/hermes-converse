# Server spec

Server responsibilities behind [client spec.md](../client%20spec.md).
Named-device behavior is negotiated with `arya-v1`; timer/alarm extensions remain separate work.

## Named Aryas

Identify a device by its `device_name` within the server/profile. Keep its
routing record, optional room/aliases, and Hermes session reference. Device
labels are unique within that scope. `assistant_name` is
the name used in the voice system prompt, often related to the wake word;
`device_name` identifies the particular device. Neither is a credential.

Authenticate using the existing API key before opening the conversation or
accepting timer requests. Do not add a separate device/attachment credential.
The device name identifies a destination, not a separate security principal;
clients sharing a server API key share its access boundary.
The device's conversation title is `<assistant_name>: <device_name>`.
There is no client conversation override, selection protocol, or separate
conversation extension. Reject a supplied `start.conversation` as an invalid
field rather than silently accepting it.

Use Hermes's existing session APIs for title lookup, creation, history,
and compression continuation. Reuse an authorized compatible named session or
create it when confirmed absent; on concurrent creation conflict, re-read the
winner. A failed lookup or inaccessible title must not silently create another
history. Save the session reference for reconnect; do not build a
parallel conversation store or expose Hermes session mechanics to clients.

Use the authenticated socket for named-device and timer behavior. A
replacement fences the old connection and stops/settles its in-flight work
before new admission. Report busy while settlement is pending. Supply trusted
source-device context to the agent/tools; transcript text cannot impersonate it.
Fencing does not prove that a disconnected speaker is silent.

There is no inactivity expiry. Use Hermes's existing compression settings to
keep voice context short and compact it frequently: `compression.threshold`
sets the percentage trigger and `compression.threshold_tokens` caps that trigger
by token count. Configure these server-side; do not add client fields or another
compactor. A compaction trigger is not a hard limit on retained context.
Hermes owns compression and retention. Verify
required Hermes API support, preserve history through continuation, and recover
local sign-off bookkeeping where needed. Never replay an unacknowledged request.

Verify first setup, concurrent creation, reconnect/restart, failed
lookup, unauthorized access, compression continuation, and replacement
attachments without duplicate history or competing work. Include the case
`assistant_name: Alexa`, `device_name: Den Echo`, title `Alexa: Den Echo`.

The implementation stores only routing metadata in the profile's
`plugin-data/hermes-converse/devices.db`. It uses authenticated exact-title lookup and the
public messages resource to resolve compression continuations, and refuses unsupported API
responses or missing saved history. It does not create a replacement history on lookup failure.
The prompt supplies both names and the routing identity, explains proactive output without
another wake word, and requires successful scheduling receipts before confirming timers.
Heartbeat 2's current API-session restriction still requires an integration change; a prompt
does not bypass a scheduling tool's refusal.
