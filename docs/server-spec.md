# Server spec

Server responsibilities behind [client spec.md](../client%20spec.md).
This document does not change runtime behavior.

## Named Aryas

Keep a device record with its stable recipient identity, household/profile,
device label, optional room/aliases, permissions, credential, and Hermes session
reference. Device labels are unique within the household. `assistant_name` is
the name used in the voice system prompt, often related to the wake word;
`device_name` identifies the particular device. Neither is a credential.

Authenticate before opening the conversation or accepting timer requests.
The device's conversation title is `<assistant_name>: <device_name>`.
There is no client conversation override, selection protocol, or separate
conversation extension. Reject a supplied `start.conversation` as an invalid
field rather than silently accepting it.

Use Hermes's existing session APIs for title lookup, creation, rename, history,
and compression continuation. Reuse an authorized compatible named session or
create it when confirmed absent; on concurrent creation conflict, re-read the
winner. A failed lookup or inaccessible title must not silently create another
history. Save the session reference for reconnect and rename; do not build a
parallel conversation store or expose Hermes session mechanics to clients.

A change to either configured name updates the existing session title without
losing history. Report rename success only after the device record and title
agree. Recover incomplete renames before accepting affected work. If saved
history is deleted or inaccessible, report unavailable rather than replacing it.

Use one authenticated attachment for named-device and timer behavior. A
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

Verify first setup, concurrent creation, reconnect/restart, rename, failed
lookup, unauthorized access, compression continuation, and replacement
attachments without duplicate history or competing work. Include the case
`assistant_name: Alexa`, `device_name: Den Echo`, title `Alexa: Den Echo`.
